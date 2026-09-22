"""单股票经验概率模型：只使用该股票自身历史，时间顺序验证，结果持久化。"""
import json
import hashlib
import math
import re
import sqlite3
from datetime import datetime

import numpy as np
import pandas as pd

import datasource

MODEL_VERSION = "single-stock-empirical-v2"
HORIZONS = (1, 3, 5, 10)
MATCH_SCHEMES = {
    "strict": ("trend_state", "momentum_state", "volume_state", "vol_state"),
    "balanced": ("trend_state", "momentum_state", "vol_state"),
    "broad": ("trend_state", "vol_state"),
    # 分钟线不直接堆入高维模型，只压缩成少量日内状态，作为独立候选模型参加
    # 时间顺序样本外比较；数据不足时这些候选会被自动跳过。
    "intraday_balanced": ("trend_state", "vol_state", "intraday_direction_state",
                          "close_vwap_state", "pressure_state"),
    "intraday_broad": ("trend_state", "intraday_direction_state", "pressure_state"),
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
CREATE TABLE IF NOT EXISTS stock_probability_llm_profiles(
    profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id INTEGER NOT NULL, code TEXT NOT NULL, model_id INTEGER NOT NULL,
    model_version TEXT NOT NULL, asof_date TEXT NOT NULL,
    prompt_version TEXT NOT NULL, data_hash TEXT NOT NULL,
    status TEXT NOT NULL, profile_json TEXT, raw_response TEXT,
    created_at TEXT,
    UNIQUE(stock_id, model_id, prompt_version, data_hash));
CREATE INDEX IF NOT EXISTS idx_probability_llm_code
ON stock_probability_llm_profiles(code, asof_date DESC);
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
CREATE TABLE IF NOT EXISTS stock_probability_governance(
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_date TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
    metrics_json TEXT, reasons_json TEXT, created_at TEXT);
"""

GOVERNANCE_DEFAULTS = {
    "min_groups": 30,
    "min_usable_coverage": 0.30,
    "min_positive_group_rate": 0.55,
    "min_recent_positive_rate": 0.60,
    "recent_groups": 10,
    "bootstrap_samples": 2000,
}

LLM_PROFILE_PROMPT_VERSION = "single-stock-profile-v1"
LLM_PROFILE_SYSTEM = """你是单股票统计概率模型的只读量化审查器。
输入全部来自程序已经完成的时间顺序样本外验证。不得重新编造概率、收益、基本面、新闻或行情；不得把相关性描述成因果性；不得建议绕过质量闸门。
你的作用是为这一只股票形成可保存的模型画像：解释适用状态、识别脆弱性、提出后续数据和验证建议。只输出一个JSON对象：
{"model_character":"不超过80字","regime_fit":["最多3项"],"risk_flags":["最多4项"],"feature_guidance":[{"feature":"字段名","action":"keep|watch|drop","reason":"不超过60字"}],"validation_plan":["最多4项"],"trading_use":"shadow_only|manual_review","confidence":0到1,"summary":"不超过120字"}
当统计证据等级不是sufficient时，trading_use必须是shadow_only。"""


def _ensure_schema(c) -> None:
    c.executescript(_SCHEMA)


def _load_daily(code: str) -> pd.DataFrame:
    with datasource._conn() as c:
        return pd.read_sql_query(
            "SELECT date,open,high,low,close,volume,amount FROM market_daily "
            "WHERE source='ths_ifind' AND code=? ORDER BY date", c, params=(code,))


def _load_intraday(code: str) -> pd.DataFrame:
    """读取已由完整分钟交易日压缩出的日内特征，不直接在建模时扫描分钟明细。"""
    with datasource._conn() as c:
        return pd.read_sql_query(
            "SELECT trade_date,minute_count,open_ret_30m,morning_ret,afternoon_ret,"
            "tail_ret_30m,realized_vol,max_intraday_drawdown,close_vwap_gap,"
            "morning_volume_share,tail_volume_share,up_minute_ratio "
            "FROM stock_intraday_features WHERE code=? AND minute_count>=200 "
            "ORDER BY trade_date", c, params=(code,))


def intraday_quality_report(code: str, min_rows: int = 200,
                            expected_rows: int = 241) -> dict:
    """检查单股票分钟数据是否足以进入日内模型；不把不完整交易日混入训练。"""
    with datasource._conn() as c:
        raw = pd.read_sql_query(
            "SELECT substr(datetime,1,10) AS trade_date, COUNT(*) AS rows, "
            "COUNT(DISTINCT datetime) AS distinct_rows "
            "FROM ifind_minute WHERE code=? GROUP BY substr(datetime,1,10) "
            "ORDER BY trade_date", c, params=(code,))
    if raw.empty:
        return {"code": code, "days": 0, "complete_days": 0, "coverage": 0.0,
                "duplicate_days": 0, "partial_days": 0, "quality": "insufficient",
                "reason": "没有分钟数据"}
    complete = raw[(raw["rows"] >= min_rows) &
                   (raw["distinct_rows"] >= min_rows)]
    duplicate_days = int((raw["rows"] != raw["distinct_rows"]).sum())
    partial_days = int((raw["rows"] < expected_rows).sum())
    coverage = len(complete) / len(raw)
    quality = "ready" if len(complete) >= 60 and coverage >= 0.95 else "insufficient"
    return {"code": code, "days": int(len(raw)), "complete_days": int(len(complete)),
            "coverage": float(coverage), "duplicate_days": duplicate_days,
            "partial_days": partial_days, "quality": quality,
            "reason": "达到日内模型最低数据门槛" if quality == "ready"
                      else "完整分钟交易日少于60天或覆盖率不足95%"}


def _features_and_labels(df: pd.DataFrame, intraday: pd.DataFrame | None = None) -> pd.DataFrame:
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
    if intraday is not None and not intraday.empty:
        minute = intraday.copy()
        minute["date"] = pd.to_datetime(minute.pop("trade_date"))
        numeric_cols = [c for c in minute.columns if c != "date"]
        for col in numeric_cols:
            minute[col] = pd.to_numeric(minute[col], errors="coerce")
        d = d.merge(minute, on="date", how="left")
        has_intraday = d[["morning_ret", "afternoon_ret"]].notna().all(axis=1)
        intraday_ret = (d["morning_ret"] + d["afternoon_ret"]).where(has_intraday)
        d["intraday_direction_state"] = pd.cut(
            intraday_ret, [-np.inf, -0.005, 0.005, np.inf],
            labels=["weak", "flat", "strong"])
        d["close_vwap_state"] = pd.cut(
            d["close_vwap_gap"], [-np.inf, -0.005, 0.005, np.inf],
            labels=["below", "near", "above"])
        d["pressure_state"] = pd.cut(
            d["up_minute_ratio"], [-np.inf, 0.45, 0.55, np.inf],
            labels=["sell", "balanced", "buy"])
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
              "broad": "放宽动量和成交量状态",
              "intraday_balanced": "日线趋势+日内方向/均价/买卖压力",
              "intraday_broad": "日线趋势+日内方向/买卖压力"}
    schemes = ("strict", "balanced", "broad") if scheme == "auto" else (scheme,)
    last = history.iloc[0:0]
    for name in schemes:
        required = MATCH_SCHEMES[name]
        if any(col not in history.columns or col not in current.index
               or pd.isna(current[col]) for col in required):
            continue
        selected = history.copy()
        for col in required:
            selected = selected[selected[col].notna()]
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
        # 所有候选模型同时预测最长10日标签；训练末端必须至少滞后10个交易日，
        # 否则靠近预测日的训练标签实际使用了预测日之后的价格，造成标签泄漏。
        history_end = i - max(HORIZONS)
        if history_end <= 0:
            continue
        preds, n, _ = _predict_from_history(data.iloc[:history_end], data.iloc[i], scheme)
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
        required = MATCH_SCHEMES[scheme]
        if any(col not in data.columns or col not in current.index
               or pd.isna(current[col]) for col in required):
            continue
        metrics = _oos_validate(data, scheme)
        current_matches = len(_similar(history, current, scheme)[0])
        # Brier为主，校准误差与样本不足作惩罚；不使用方向准确率调参。
        objective = ((metrics["brier"] if metrics["brier"] is not None else 1.0)
                     + 0.25 * (metrics["calibration_error"] if metrics["calibration_error"] is not None else 1.0)
                     + (0.1 if metrics["count"] < 30 else 0.0)
                     + (0.2 if current_matches < 30 else 0.0))
        # 日内模型必须有足够的共同状态样本和至少30个样本外窗口，
        # 否则即使偶然Brier很低也不能战胜稳定的日线模型。
        if scheme.startswith("intraday_") and (metrics["count"] < 30 or current_matches < 30):
            objective += 1.0
        candidates.append({"scheme": scheme, "objective": objective,
                           "current_matches": current_matches, **metrics})
    candidates.sort(key=lambda x: (x["objective"], -x["count"]))
    best = candidates[0]
    return best["scheme"], {k: best[k] for k in
                            ("count", "brier", "accuracy", "calibration_error")}, candidates


def build_model(code: str) -> dict:
    raw = _load_daily(code)
    intraday = _load_intraday(code)
    intraday_quality = intraday_quality_report(code)
    data = _features_and_labels(raw, intraday)
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
    intraday_state = {}
    for key in ("intraday_direction_state", "close_vwap_state", "pressure_state"):
        if key in current.index and pd.notna(current[key]):
            intraday_state[key] = str(current[key])
    for key in ("open_ret_30m", "morning_ret", "afternoon_ret", "tail_ret_30m",
                "realized_vol", "max_intraday_drawdown", "close_vwap_gap",
                "up_minute_ratio"):
        if key in current.index and pd.notna(current[key]):
            intraday_state[key] = float(current[key])
    state.update(intraday_state)
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
              "intraday_days": int(len(intraday)),
              "intraday_quality": intraday_quality,
              "uses_intraday": selected_scheme.startswith("intraday_"),
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
                         "intraday_days": result["intraday_days"],
                         "intraday_quality": result["intraday_quality"],
                         "uses_intraday": result["uses_intraday"],
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
            "intraday_days": pred.get("intraday_days", 0),
            "intraday_quality": pred.get("intraday_quality", {}),
            "uses_intraday": bool(pred.get("uses_intraday", False)),
            "created_at": row[9]}


def _latest_model_row(code: str):
    with datasource._conn() as c:
        _ensure_schema(c)
        return c.execute(
            "SELECT model_id,stock_id,code,model_version,asof_date,train_start,train_end,"
            "sample_count,oos_count,metrics_json,state_json,prediction_json,created_at "
            "FROM stock_probability_models WHERE code=? "
            "ORDER BY asof_date DESC,model_id DESC LIMIT 1", (code,)).fetchone()


def _llm_profile_evidence(row) -> dict:
    metrics = json.loads(row[9] or "{}")
    state = json.loads(row[10] or "{}")
    payload = json.loads(row[11] or "{}")
    predictions = payload.get("predictions") or {}
    compact_predictions = {}
    for key in ("up_1d", "up_3d", "up_5d", "up_10d", "up_3pct_5d", "down_3pct_5d"):
        item = predictions.get(key) or {}
        compact_predictions[key] = {
            "probability": round(float(item.get("shrunk", 0.5)), 6),
            "ci_low": round(float(item.get("low", 0)), 6),
            "ci_high": round(float(item.get("high", 1)), 6),
            "n": int(item.get("n") or 0),
        }
    keep_state = ("trend_state", "momentum_state", "volume_state", "vol_state",
                  "ret_5", "ret_20", "vol_20", "volume_ratio", "atr_pct",
                  "intraday_direction_state", "close_vwap_state", "pressure_state",
                  "realized_vol", "max_intraday_drawdown", "close_vwap_gap",
                  "up_minute_ratio")
    compact_state = {k: state[k] for k in keep_state if k in state}
    candidates = []
    for item in (payload.get("model_candidates") or [])[:8]:
        candidates.append({k: item.get(k) for k in
                           ("scheme", "current_matches", "count", "brier", "accuracy",
                            "calibration_error")})
    return {
        "code": row[2], "model_version": row[3], "asof_date": row[4],
        "train_start": row[5], "train_end": row[6],
        "sample_count": int(row[7] or 0), "oos_count": int(row[8] or 0),
        "evidence": payload.get("evidence"),
        "selected_scheme": payload.get("selected_scheme"),
        "intraday_days": int(payload.get("intraday_days") or 0),
        "uses_intraday": bool(payload.get("uses_intraday", False)),
        "oos": metrics, "state": compact_state,
        "predictions": compact_predictions, "model_candidates": candidates,
    }


def build_llm_profile(code: str, force: bool = False) -> dict:
    """为最新统计模型生成只读LLM画像，并与股票和统计模型版本永久关联。"""
    import llmutil

    row = _latest_model_row(code)
    if not row:
        raise ValueError("尚无统计概率模型，请先构建/更新模型")
    evidence = _llm_profile_evidence(row)
    evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), default=str)
    data_hash = hashlib.sha256(
        f"{LLM_PROFILE_PROMPT_VERSION}\n{evidence_json}".encode()).hexdigest()[:20]
    if not force:
        with datasource._conn() as c:
            _ensure_schema(c)
            cached = c.execute(
                "SELECT status,profile_json,created_at FROM stock_probability_llm_profiles "
                "WHERE stock_id=? AND model_id=? AND prompt_version=? AND data_hash=?",
                (row[1], row[0], LLM_PROFILE_PROMPT_VERSION, data_hash)).fetchone()
        if cached:
            profile = json.loads(cached[1] or "{}")
            return {"status": cached[0], "code": code, "model_id": row[0],
                    "data_hash": data_hash, "profile": profile,
                    "created_at": cached[2], "cache_hit": True}
    reply = llmutil.llm_chat(
        LLM_PROFILE_SYSTEM, "单股票统计模型证据JSON：\n" + evidence_json,
        max_tokens=700, label="stock_probability_profile_v1", use_cache=True)
    if not reply:
        reason = llmutil.llm_failure_reason()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        profile = {"summary": reason, "trading_use": "shadow_only", "confidence": 0.0}
        with datasource._conn() as c:
            _ensure_schema(c)
            c.execute(
                "INSERT OR REPLACE INTO stock_probability_llm_profiles"
                "(stock_id,code,model_id,model_version,asof_date,prompt_version,data_hash,status,"
                "profile_json,raw_response,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (row[1], code, row[0], row[3], row[4], LLM_PROFILE_PROMPT_VERSION,
                 data_hash, "unavailable", json.dumps(profile, ensure_ascii=False), "", now))
        return {"status": "unavailable", "code": code, "model_id": row[0],
                "data_hash": data_hash, "reason": reason, "profile": profile,
                "created_at": now, "cache_hit": False}
    status, profile = "ok", {}
    try:
        match = re.search(r"\{[\s\S]*\}", reply)
        profile = json.loads(match.group()) if match else {}
        if not isinstance(profile, dict):
            raise ValueError("LLM结果不是JSON对象")
        profile["trading_use"] = ("manual_review" if
            evidence.get("evidence") == "sufficient"
            and profile.get("trading_use") == "manual_review" else "shadow_only")
        profile["confidence"] = max(0.0, min(1.0, float(profile.get("confidence") or 0)))
        profile["regime_fit"] = [str(x)[:100] for x in (profile.get("regime_fit") or [])[:3]]
        profile["risk_flags"] = [str(x)[:120] for x in (profile.get("risk_flags") or [])[:4]]
        profile["validation_plan"] = [str(x)[:120] for x in
                                      (profile.get("validation_plan") or [])[:4]]
        profile["model_character"] = str(profile.get("model_character") or "")[:120]
        profile["summary"] = str(profile.get("summary") or "")[:180]
        valid_features = set(evidence["state"])
        guidance = []
        for item in (profile.get("feature_guidance") or [])[:8]:
            if not isinstance(item, dict) or str(item.get("feature")) not in valid_features:
                continue
            action = str(item.get("action") or "watch")
            if action not in ("keep", "watch", "drop"):
                action = "watch"
            guidance.append({"feature": str(item["feature"]), "action": action,
                             "reason": str(item.get("reason") or "")[:100]})
        profile["feature_guidance"] = guidance
    except Exception as exc:
        status = "invalid"
        profile = {"summary": f"LLM输出解析失败：{type(exc).__name__}",
                   "trading_use": "shadow_only", "confidence": 0.0}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with datasource._conn() as c:
        _ensure_schema(c)
        c.execute(
            "INSERT OR REPLACE INTO stock_probability_llm_profiles"
            "(stock_id,code,model_id,model_version,asof_date,prompt_version,data_hash,status,"
            "profile_json,raw_response,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (row[1], code, row[0], row[3], row[4], LLM_PROFILE_PROMPT_VERSION, data_hash,
             status, json.dumps(profile, ensure_ascii=False), reply[:12000], now))
    return {"status": status, "code": code, "model_id": row[0],
            "data_hash": data_hash, "profile": profile, "created_at": now,
            "cache_hit": False}


def load_latest_llm_profile(code: str) -> dict | None:
    with datasource._conn() as c:
        _ensure_schema(c)
        row = c.execute(
            "SELECT model_id,model_version,asof_date,prompt_version,data_hash,status,"
            "profile_json,created_at FROM stock_probability_llm_profiles WHERE code=? "
            "ORDER BY profile_id DESC LIMIT 1", (code,)).fetchone()
    if not row:
        return None
    return {"code": code, "model_id": row[0], "model_version": row[1],
            "asof_date": row[2], "prompt_version": row[3], "data_hash": row[4],
            "status": row[5], "profile": json.loads(row[6] or "{}"),
            "created_at": row[7]}


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


def _group_shadow_lifts() -> pd.DataFrame:
    """每个名单作为一个独立配对样本，避免股票多的名单获得更高权重。"""
    with datasource._conn() as c:
        _ensure_schema(c)
        done = pd.read_sql_query(
            "SELECT trade_date,pick_id,model_status,fwd_5d_return,original_top,shadow_top "
            "FROM stock_probability_shadow WHERE evaluated_at IS NOT NULL", c)
    if done.empty:
        return pd.DataFrame()
    rows = []
    for (trade_date, pick_id), group in done.groupby(["trade_date", "pick_id"]):
        original = group[group["original_top"] == 1]["fwd_5d_return"].dropna()
        shadow = group[group["shadow_top"] == 1]["fwd_5d_return"].dropna()
        if original.empty or shadow.empty:
            continue
        rows.append({
            "trade_date": trade_date, "pick_id": int(pick_id),
            "original_return": float(original.mean()),
            "shadow_return": float(shadow.mean()),
            "lift": float(shadow.mean() - original.mean()),
            "candidate_count": int(len(group)),
            "usable_count": int((group["model_status"] == "可用").sum()),
        })
    return pd.DataFrame(rows).sort_values(["trade_date", "pick_id"]) if rows else pd.DataFrame()


def _bootstrap_mean_ci(values: np.ndarray, samples: int = 2000,
                       seed: int = 20260921) -> tuple[float | None, float | None]:
    if values is None or len(values) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    draws = rng.choice(values, size=(samples, len(values)), replace=True).mean(axis=1)
    return float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))


def governance_audit(asof: str | None = None, cfg: dict | None = None,
                     persist: bool = True) -> dict:
    """评估概率影子是否具备人工晋级资格；永远不改变执行开关。"""
    cfg = {**GOVERNANCE_DEFAULTS, **(cfg or {})}
    groups = _group_shadow_lifts()
    audit_date = asof or datetime.now().strftime("%Y-%m-%d")
    if groups.empty:
        metrics = {"groups": 0, "usable_coverage": 0.0, "mean_lift": None,
                   "positive_group_rate": None, "recent_positive_rate": None,
                   "ci_low": None, "ci_high": None}
    else:
        lifts = groups["lift"].to_numpy(dtype=float)
        total_candidates = int(groups["candidate_count"].sum())
        usable = int(groups["usable_count"].sum())
        recent = groups.tail(int(cfg["recent_groups"]))
        ci_low, ci_high = _bootstrap_mean_ci(
            lifts, int(cfg["bootstrap_samples"]))
        metrics = {
            "groups": int(len(groups)),
            "usable_coverage": usable / total_candidates if total_candidates else 0.0,
            "mean_lift": float(lifts.mean()),
            "median_lift": float(np.median(lifts)),
            "positive_group_rate": float((lifts > 0).mean()),
            "recent_groups": int(len(recent)),
            "recent_positive_rate": float((recent["lift"] > 0).mean()),
            "ci_low": ci_low, "ci_high": ci_high,
            "original_avg": float(groups["original_return"].mean()),
            "shadow_avg": float(groups["shadow_return"].mean()),
        }
    checks = [
        (metrics["groups"] >= cfg["min_groups"],
         f"成熟名单至少 {cfg['min_groups']} 组（当前 {metrics['groups']}）"),
        (metrics["usable_coverage"] >= cfg["min_usable_coverage"],
         f"模型可用覆盖率至少 {cfg['min_usable_coverage']:.0%}（当前 {metrics['usable_coverage']:.1%}）"),
        ((metrics["mean_lift"] or 0) > 0,
         f"平均5日增益必须为正（当前 {(metrics['mean_lift'] or 0):+.2%}）"),
        ((metrics["positive_group_rate"] or 0) >= cfg["min_positive_group_rate"],
         f"正增益名单占比至少 {cfg['min_positive_group_rate']:.0%}（当前 {(metrics['positive_group_rate'] or 0):.1%}）"),
        ((metrics["recent_positive_rate"] or 0) >= cfg["min_recent_positive_rate"]
         and metrics.get("recent_groups", 0) >= cfg["recent_groups"],
         f"最近 {cfg['recent_groups']} 组正增益占比至少 {cfg['min_recent_positive_rate']:.0%}"),
        ((metrics["ci_low"] or 0) > 0,
         f"Bootstrap 95%增益下限必须大于0（当前 {(metrics['ci_low'] or 0):+.2%}）"),
    ]
    reasons = [{"passed": bool(ok), "rule": text} for ok, text in checks]
    status = "eligible_for_manual_review" if all(ok for ok, _ in checks) else "shadow_continue"
    result = {"audit_date": audit_date, "status": status, "metrics": metrics,
              "reasons": reasons, "config": cfg, "automatic_activation": False}
    if persist:
        with datasource._conn() as c:
            _ensure_schema(c)
            c.execute(
                "INSERT OR REPLACE INTO stock_probability_governance"
                "(audit_date,status,metrics_json,reasons_json,created_at) VALUES(?,?,?,?,?)",
                (audit_date, status, json.dumps({"metrics": metrics, "config": cfg},
                                                ensure_ascii=False),
                 json.dumps(reasons, ensure_ascii=False),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return result


def shadow_detail(limit: int = 200) -> pd.DataFrame:
    with datasource._conn() as c:
        _ensure_schema(c)
        return pd.read_sql_query(
            "SELECT trade_date,pick_id,code,original_rank,shadow_rank,model_status,"
            "probability_edge,shadow_adjustment,eval_date,fwd_5d_return,original_top,"
            "shadow_top,evaluated_at FROM stock_probability_shadow "
            "ORDER BY trade_date DESC,pick_id DESC,original_rank LIMIT ?", c,
            params=(int(limit),))
