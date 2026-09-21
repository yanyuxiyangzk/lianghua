"""单股票经验概率模型：只使用该股票自身历史，时间顺序验证，结果持久化。"""
import json
import math
import sqlite3
from datetime import datetime

import numpy as np
import pandas as pd

import datasource

MODEL_VERSION = "single-stock-empirical-v1"
HORIZONS = (1, 3, 5, 10)
MATCH_SCHEMES = {
    "strict": ("trend_state", "momentum_state", "volume_state", "vol_state"),
    "balanced": ("trend_state", "momentum_state", "vol_state"),
    "broad": ("trend_state", "vol_state"),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_probability_models(
    model_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id INTEGER NOT NULL, code TEXT NOT NULL, model_version TEXT NOT NULL,
    asof_date TEXT NOT NULL, train_start TEXT, train_end TEXT,
    sample_count INTEGER, oos_count INTEGER, metrics_json TEXT,
    state_json TEXT, prediction_json TEXT, created_at TEXT,
    UNIQUE(stock_id, model_version, asof_date));
CREATE INDEX IF NOT EXISTS idx_stock_probability_code
ON stock_probability_models(code, asof_date DESC);
CREATE TABLE IF NOT EXISTS stock_probability_shadow(
    shadow_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_id INTEGER NOT NULL, trade_date TEXT NOT NULL, code TEXT NOT NULL,
    original_rank INTEGER, original_score REAL, model_date TEXT,
    model_status TEXT, probability_edge REAL, shadow_adjustment REAL,
    shadow_score REAL, shadow_rank INTEGER, eval_date TEXT,
    fwd_5d_return REAL, original_top INTEGER DEFAULT 0,
    shadow_top INTEGER DEFAULT 0, evaluated_at TEXT, created_at TEXT,
    UNIQUE(pick_id, code));
CREATE INDEX IF NOT EXISTS idx_probability_shadow_eval
ON stock_probability_shadow(eval_date, evaluated_at);
"""


def _ensure_schema(c) -> None:
    c.executescript(_SCHEMA)


def _load_daily(code: str) -> pd.DataFrame:
    with datasource._conn() as c:
        return pd.read_sql_query(
            "SELECT date,open,high,low,close,volume,amount FROM market_daily "
            "WHERE source='ths_ifind' AND code=? ORDER BY date", c, params=(code,))


def _features_and_labels(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    for col in ("open", "high", "low", "close", "volume", "amount"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["close", "high", "low"]).sort_values("date").reset_index(drop=True)
    ret = d["close"].pct_change()
    d["ret_5"] = d["close"].pct_change(5)
    d["ret_20"] = d["close"].pct_change(20)
    d["vol_20"] = ret.rolling(20).std() * math.sqrt(252)
    d["ma20_gap"] = d["close"] / d["close"].rolling(20).mean() - 1
    d["volume_ratio"] = d["volume"] / d["volume"].rolling(20).mean()
    tr = pd.concat([(d["high"] - d["low"]),
                    (d["high"] - d["close"].shift()).abs(),
                    (d["low"] - d["close"].shift()).abs()], axis=1).max(axis=1)
    d["atr_pct"] = tr.rolling(14).mean() / d["close"]
    # 离散状态降低维度，避免相似样本搜索过拟合。
    d["trend_state"] = pd.cut(d["ret_20"], [-np.inf, -0.05, 0.05, np.inf],
                              labels=["down", "flat", "up"])
    d["momentum_state"] = pd.cut(d["ret_5"], [-np.inf, -0.02, 0.02, np.inf],
                                 labels=["weak", "neutral", "strong"])
    d["volume_state"] = pd.cut(d["volume_ratio"], [-np.inf, 0.8, 1.2, np.inf],
                               labels=["shrink", "normal", "expand"])
    # 波动状态使用该股票历史分位数，只在模型训练切片内重新计算会更严格；
    # 第一版采用扩张分位数，避免使用未来样本确定当前阈值。
    q33 = d["vol_20"].expanding(60).quantile(0.33)
    q67 = d["vol_20"].expanding(60).quantile(0.67)
    d["vol_state"] = np.where(d["vol_20"] <= q33, "low",
                              np.where(d["vol_20"] >= q67, "high", "mid"))
    for h in HORIZONS:
        fwd = d["close"].shift(-h) / d["close"] - 1
        d[f"fwd_{h}"] = fwd
        d[f"up_{h}"] = (fwd > 0).astype(float).where(fwd.notna())
    # 5日路径标签：未来窗口内先触及 +3% / -3%。
    outcomes = []
    for i in range(len(d)):
        base = d.at[i, "close"]
        future = d.iloc[i + 1:i + 6]
        hit = "no_hit"
        for row in future.itertuples():
            if row.low / base - 1 <= -0.03:
                hit = "down3"; break  # 同根双触保守按下跌
            if row.high / base - 1 >= 0.03:
                hit = "up3"; break
        outcomes.append(hit)
    d["path_5"] = outcomes
    return d.dropna(subset=["ret_5", "ret_20", "vol_20", "volume_ratio"])


def _similar(history: pd.DataFrame, current: pd.Series,
             scheme: str = "auto") -> tuple[pd.DataFrame, str]:
    labels = {"strict": "四状态精确匹配", "balanced": "放宽成交量状态",
              "broad": "放宽动量和成交量状态"}
    schemes = ("strict", "balanced", "broad") if scheme == "auto" else (scheme,)
    last = history.iloc[0:0]
    for name in schemes:
        selected = history.copy()
        for col in MATCH_SCHEMES[name]:
            selected = selected[selected[col].astype(str) == str(current[col])]
        last = selected
        if scheme != "auto" or len(selected) >= 30:
            return selected, labels[name]
    return last, labels["broad"]


def _prob(success: int, n: int, prior_strength: int = 20) -> dict:
    """Beta(10,10) 收缩 + Wilson 95%区间。"""
    if n <= 0:
        return {"raw": None, "shrunk": 0.5, "low": 0.0, "high": 1.0, "n": 0}
    raw = success / n
    shrunk = (success + prior_strength / 2) / (n + prior_strength)
    z = 1.96
    den = 1 + z * z / n
    center = (raw + z * z / (2 * n)) / den
    half = z * math.sqrt(raw * (1 - raw) / n + z * z / (4 * n * n)) / den
    return {"raw": raw, "shrunk": shrunk, "low": max(0, center - half),
            "high": min(1, center + half), "n": n}


def _predict_from_history(history: pd.DataFrame, current: pd.Series,
                          scheme: str = "auto") -> tuple[dict, int, str]:
    similar, method = _similar(history, current, scheme)
    result = {}
    for h in HORIZONS:
        valid = similar[f"up_{h}"].dropna()
        result[f"up_{h}d"] = _prob(int(valid.sum()), len(valid))
    path = similar["path_5"].dropna()
    result["up_3pct_5d"] = _prob(int((path == "up3").sum()), len(path))
    result["down_3pct_5d"] = _prob(int((path == "down3").sum()), len(path))
    return result, len(similar), method


def _oos_validate(data: pd.DataFrame, scheme: str = "auto") -> dict:
    """最后20%时间段逐点预测，训练集永远只取预测日之前。"""
    start = max(80, int(len(data) * 0.8))
    rows = []
    for i in range(start, len(data) - 5):
        preds, n, _ = _predict_from_history(data.iloc[:i], data.iloc[i], scheme)
        p = preds["up_5d"]["shrunk"]
        y = data.iloc[i]["up_5"]
        if pd.notna(y) and n >= 10:
            rows.append((p, float(y)))
    if not rows:
        return {"count": 0, "brier": None, "accuracy": None, "calibration_error": None}
    arr = np.array(rows)
    brier = float(np.mean((arr[:, 0] - arr[:, 1]) ** 2))
    accuracy = float(np.mean((arr[:, 0] >= 0.5) == (arr[:, 1] > 0.5)))
    bins = pd.cut(arr[:, 0], [0, .4, .5, .6, 1], include_lowest=True)
    cal = pd.DataFrame({"p": arr[:, 0], "y": arr[:, 1], "bin": bins}).groupby(
        "bin", observed=True).agg(p=("p", "mean"), y=("y", "mean"), n=("y", "size"))
    ece = float(((cal["p"] - cal["y"]).abs() * cal["n"]).sum() / cal["n"].sum())
    return {"count": len(rows), "brier": brier, "accuracy": accuracy,
            "calibration_error": ece}


def _select_model(data: pd.DataFrame, history: pd.DataFrame,
                  current: pd.Series) -> tuple[str, dict, list[dict]]:
    """只用滚动样本外结果选择状态复杂度，避免按当前预测结果挑模型。"""
    candidates = []
    for scheme in MATCH_SCHEMES:
        metrics = _oos_validate(data, scheme)
        current_matches = len(_similar(history, current, scheme)[0])
        # Brier为主，校准误差与样本不足作惩罚；不使用方向准确率调参。
        objective = ((metrics["brier"] if metrics["brier"] is not None else 1.0)
                     + 0.25 * (metrics["calibration_error"] if metrics["calibration_error"] is not None else 1.0)
                     + (0.1 if metrics["count"] < 30 else 0.0)
                     + (0.2 if current_matches < 30 else 0.0))
        candidates.append({"scheme": scheme, "objective": objective,
                           "current_matches": current_matches, **metrics})
    candidates.sort(key=lambda x: (x["objective"], -x["count"]))
    best = candidates[0]
    return best["scheme"], {k: best[k] for k in
                            ("count", "brier", "accuracy", "calibration_error")}, candidates


def build_model(code: str) -> dict:
    raw = _load_daily(code)
    data = _features_and_labels(raw)
    if len(data) < 120:
        raise ValueError(f"有效日线仅 {len(data)} 条，至少需要120条")
    current = data.iloc[-1]
    history = data.iloc[:-10].copy()  # 给所有标签留出至少10日成熟窗口
    selected_scheme, oos, model_candidates = _select_model(data, history, current)
    predictions, sample_count, match_method = _predict_from_history(
        history, current, selected_scheme)
    state = {k: str(current[k]) for k in
             ("trend_state", "momentum_state", "volume_state", "vol_state")}
    state.update({"ret_5": float(current["ret_5"]), "ret_20": float(current["ret_20"]),
                  "vol_20": float(current["vol_20"]),
                  "volume_ratio": float(current["volume_ratio"]),
                  "atr_pct": float(current["atr_pct"])})
    # 不只看样本量：样本外方向不能明显劣于随机，概率误差也要受控。
    quality_ok = (oos["count"] >= 30 and oos["brier"] is not None
                  and oos["brier"] <= 0.25 and oos["accuracy"] is not None
                  and oos["accuracy"] >= 0.52
                  and oos["calibration_error"] is not None
                  and oos["calibration_error"] <= 0.12)
    evidence = "sufficient" if sample_count >= 50 and quality_ok else "limited"
    result = {"code": code, "model_version": MODEL_VERSION,
              "asof_date": current["date"].strftime("%Y-%m-%d"),
              "train_start": history["date"].min().strftime("%Y-%m-%d"),
              "train_end": history["date"].max().strftime("%Y-%m-%d"),
              "sample_count": sample_count, "match_method": match_method,
              "selected_scheme": selected_scheme, "model_candidates": model_candidates,
              "evidence": evidence, "state": state, "predictions": predictions,
              "oos": oos, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    stock_id = datasource.get_or_create_stock_id(code)
    with datasource._conn() as c:
        _ensure_schema(c)
        c.execute(
            "INSERT OR REPLACE INTO stock_probability_models"
            "(stock_id,code,model_version,asof_date,train_start,train_end,sample_count,oos_count,"
            "metrics_json,state_json,prediction_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (stock_id, code, MODEL_VERSION, result["asof_date"], result["train_start"],
             result["train_end"], sample_count, oos["count"],
             json.dumps(oos, ensure_ascii=False), json.dumps(state, ensure_ascii=False),
             json.dumps({"predictions": predictions, "match_method": match_method,
                         "selected_scheme": selected_scheme,
                         "model_candidates": model_candidates,
                         "evidence": evidence}, ensure_ascii=False), result["created_at"]))
    return result


def load_latest(code: str) -> dict | None:
    with datasource._conn() as c:
        _ensure_schema(c)
        row = c.execute(
            "SELECT model_version,asof_date,train_start,train_end,sample_count,oos_count,"
            "metrics_json,state_json,prediction_json,created_at FROM stock_probability_models "
            "WHERE code=? ORDER BY asof_date DESC,model_id DESC LIMIT 1", (code,)).fetchone()
    if not row:
        return None
    pred = json.loads(row[8] or "{}")
    return {"code": code, "model_version": row[0], "asof_date": row[1],
            "train_start": row[2], "train_end": row[3], "sample_count": row[4],
            "oos_count": row[5], "oos": json.loads(row[6] or "{}"),
            "state": json.loads(row[7] or "{}"), "predictions": pred.get("predictions", {}),
            "match_method": pred.get("match_method"), "evidence": pred.get("evidence"),
            "selected_scheme": pred.get("selected_scheme"),
            "model_candidates": pred.get("model_candidates", []),
            "created_at": row[9]}


def probability_overlay(scores: pd.Series, asof: str, execute: bool = False,
                        max_adjustment: float = 0.15, max_age_days: int = 7) -> tuple[pd.Series, pd.DataFrame]:
    """策略/因子组合接口：用通过闸门的单票概率做有限二级修正。

    默认 execute=False 为影子模式，只返回诊断，不改变 scores。只有模型证据充足、
    模型日期不晚于 asof 且足够新鲜时才参与；修正幅度上限为原分数横截面标准差的15%。
    """
    if scores is None or scores.empty:
        return scores, pd.DataFrame()
    codes = [str(x) for x in scores.index]
    marks = ",".join("?" * len(codes))
    with datasource._conn() as c:
        _ensure_schema(c)
        rows = c.execute(
            f"SELECT code,asof_date,sample_count,metrics_json,prediction_json FROM "
            f"stock_probability_models WHERE code IN ({marks}) AND asof_date<=? "
            f"ORDER BY code,asof_date DESC,model_id DESC", codes + [asof]).fetchall()
    latest = {}
    for row in rows:
        latest.setdefault(row[0], row)
    scale = float(scores.std()) if len(scores) > 1 and pd.notna(scores.std()) else 1.0
    adjusted = scores.copy().astype(float)
    diagnostics = []
    asof_ts = pd.Timestamp(asof)
    for code in codes:
        row = latest.get(code)
        status, edge, adjustment = "无模型", 0.0, 0.0
        model_date = evidence = None
        if row:
            model_date = row[1]
            payload = json.loads(row[4] or "{}")
            evidence = payload.get("evidence")
            age = (asof_ts - pd.Timestamp(model_date)).days
            pred = payload.get("predictions", {})
            p_up = float((pred.get("up_5d") or {}).get("shrunk", 0.5))
            p_down = float((pred.get("down_3pct_5d") or {}).get("shrunk", 0.5))
            edge = (p_up - 0.5) - 0.5 * max(0.0, p_down - 0.5)
            if evidence != "sufficient":
                status = "质量闸门未通过"
            elif age < 0 or age > max_age_days:
                status = "模型过期"
            elif int(row[2] or 0) < 50:
                status = "样本不足"
            else:
                status = "可用"
                adjustment = float(np.clip(edge * 2, -max_adjustment, max_adjustment)) * scale
                if execute:
                    adjusted.loc[code] += adjustment
        diagnostics.append({"code": code, "model_date": model_date, "evidence": evidence,
                            "status": status, "probability_edge": edge,
                            "score_adjustment": adjustment if execute else 0.0,
                            "shadow_adjustment": adjustment})
    return adjusted.sort_values(ascending=False), pd.DataFrame(diagnostics)


def _trade_day_offset(day: str, offset: int) -> str | None:
    days = datasource.expected_trade_days("1990-01-01", "2099-12-31")
    if day not in days:
        return None
    i = days.index(day) + offset
    return days[i] if 0 <= i < len(days) else None


def update_models_and_record_shadow(trade_date: str, max_codes: int = 30,
                                    top_n: int = 10) -> dict:
    """为当天正式候选增量构建模型并保存影子排序，不修改正式名单。"""
    import experience

    picks = experience.picks_on_date(trade_date)
    if picks.empty:
        return {"models_ok": 0, "models_failed": 0, "shadow_rows": 0, "picks": 0}
    # 每个来源/策略包的最新一条名单，避免同组合重复处理。
    picks = picks.drop_duplicates(subset=["source", "pack_name", "pool_name"], keep="first")
    models_ok = models_failed = shadow_rows = 0
    built = set()
    eval_date = _trade_day_offset(trade_date, 5)
    for pick in picks.itertuples():
        items = experience.pick_items_detail(int(pick.id))
        if items.empty:
            continue
        scores = items.set_index("code")["score"].dropna().sort_values(ascending=False)
        if scores.empty:
            continue
        for code in list(scores.index)[:max_codes]:
            if code in built:
                continue
            built.add(code)
            try:
                build_model(code)
                models_ok += 1
            except Exception:
                models_failed += 1
        shadow_scores, diag = probability_overlay(scores, trade_date, execute=True)
        diag = diag.set_index("code") if not diag.empty else pd.DataFrame()
        original_rank = {c: i + 1 for i, c in enumerate(scores.index)}
        shadow_rank = {c: i + 1 for i, c in enumerate(shadow_scores.index)}
        # 当保存名单本身仅有 top_n 条时，比较全体没有辨识度；改用前半截评估排序增益。
        eval_top_n = min(top_n, max(1, len(scores) // 2))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for code, score in scores.items():
            d = diag.loc[code] if not diag.empty and code in diag.index else {}
            rows.append((int(pick.id), trade_date, code, original_rank[code], float(score),
                         d.get("model_date") if hasattr(d, "get") else None,
                         d.get("status", "无模型") if hasattr(d, "get") else "无模型",
                         float(d.get("probability_edge", 0) or 0) if hasattr(d, "get") else 0.0,
                         float(d.get("shadow_adjustment", 0) or 0) if hasattr(d, "get") else 0.0,
                         float(shadow_scores.loc[code]), shadow_rank[code], eval_date,
                         int(original_rank[code] <= eval_top_n),
                         int(shadow_rank[code] <= eval_top_n), now))
        with datasource._conn() as c:
            _ensure_schema(c)
            c.executemany(
                "INSERT INTO stock_probability_shadow"
                "(pick_id,trade_date,code,original_rank,original_score,model_date,model_status,"
                "probability_edge,shadow_adjustment,shadow_score,shadow_rank,eval_date,"
                "original_top,shadow_top,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(pick_id,code) DO UPDATE SET "
                "original_rank=excluded.original_rank,original_score=excluded.original_score,"
                "model_date=excluded.model_date,model_status=excluded.model_status,"
                "probability_edge=excluded.probability_edge,"
                "shadow_adjustment=excluded.shadow_adjustment,shadow_score=excluded.shadow_score,"
                "shadow_rank=excluded.shadow_rank,eval_date=excluded.eval_date,"
                "original_top=excluded.original_top,shadow_top=excluded.shadow_top,"
                "created_at=excluded.created_at", rows)
        shadow_rows += len(rows)
    return {"models_ok": models_ok, "models_failed": models_failed,
            "shadow_rows": shadow_rows, "picks": len(picks), "eval_date": eval_date}


def evaluate_shadow(asof: str) -> dict:
    """回填已满5个交易日的影子结果，并比较原Top与影子Top的平均收益。"""
    with datasource._conn() as c:
        _ensure_schema(c)
        pending = pd.read_sql_query(
            "SELECT * FROM stock_probability_shadow WHERE evaluated_at IS NULL "
            "AND eval_date IS NOT NULL AND eval_date<=? ORDER BY trade_date,pick_id", c,
            params=(asof,))
    if pending.empty:
        return {"evaluated": 0, "groups": 0, "original_avg": None,
                "shadow_avg": None, "lift": None}
    updated = []
    for row in pending.itertuples():
        with datasource._conn() as c:
            prices = c.execute(
                "SELECT date,close FROM market_daily WHERE source='ths_ifind' AND code=? "
                "AND date IN (?,?)", (row.code, row.trade_date, row.eval_date)).fetchall()
        px = {d: v for d, v in prices if v is not None}
        if row.trade_date not in px or row.eval_date not in px or not px[row.trade_date]:
            continue
        ret = float(px[row.eval_date] / px[row.trade_date] - 1)
        updated.append((ret, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row.shadow_id))
    if updated:
        with datasource._conn() as c:
            c.executemany(
                "UPDATE stock_probability_shadow SET fwd_5d_return=?,evaluated_at=? "
                "WHERE shadow_id=?", updated)
    with datasource._conn() as c:
        done = pd.read_sql_query(
            "SELECT * FROM stock_probability_shadow WHERE evaluated_at IS NOT NULL", c)
    if done.empty:
        return {"evaluated": 0, "groups": 0, "original_avg": None,
                "shadow_avg": None, "lift": None}
    original = done[done["original_top"] == 1]["fwd_5d_return"].mean()
    shadow = done[done["shadow_top"] == 1]["fwd_5d_return"].mean()
    groups = done[["trade_date", "pick_id"]].drop_duplicates().shape[0]
    return {"evaluated": len(updated), "total_evaluated": len(done), "groups": groups,
            "original_avg": float(original) if pd.notna(original) else None,
            "shadow_avg": float(shadow) if pd.notna(shadow) else None,
            "lift": float(shadow - original) if pd.notna(original) and pd.notna(shadow) else None}


def shadow_summary() -> dict:
    with datasource._conn() as c:
        _ensure_schema(c)
        total = c.execute("SELECT COUNT(*) FROM stock_probability_shadow").fetchone()[0]
        usable = c.execute(
            "SELECT COUNT(*) FROM stock_probability_shadow WHERE model_status='可用'").fetchone()[0]
        evaluated = c.execute(
            "SELECT COUNT(*) FROM stock_probability_shadow WHERE evaluated_at IS NOT NULL").fetchone()[0]
        if evaluated:
            row = c.execute(
                "SELECT AVG(CASE WHEN original_top=1 THEN fwd_5d_return END),"
                "AVG(CASE WHEN shadow_top=1 THEN fwd_5d_return END),"
                "COUNT(DISTINCT trade_date || ':' || pick_id) "
                "FROM stock_probability_shadow WHERE evaluated_at IS NOT NULL").fetchone()
            original_avg, shadow_avg, groups = row
        else:
            original_avg = shadow_avg = None; groups = 0
    lift = (float(shadow_avg - original_avg)
            if original_avg is not None and shadow_avg is not None else None)
    return {"total": total, "usable": usable, "evaluated": evaluated,
            "groups": groups, "original_avg": original_avg,
            "shadow_avg": shadow_avg, "lift": lift}
