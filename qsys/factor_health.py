"""因子健康度元模型（机制B）：P(名单内标的跑赢同批中位 | 分数位置 × 名单源IC状态 × 市场regime)。

与单股票概率模型互补：单股票模型受小样本天花板限制（~3.5年日线、相似匹配仅数十例），
元模型池化全部名单历史（每天数十只×数百天），样本效率高一个数量级；它直接回答
"这个策略包当前是否处于过拟合失效期"——IC 衰减会被条件频率直接捕捉成 score 压缩。
默认影子模式：只记录对照排序并配对评估，永远不改变正式名单；晋级只出人工评审资格。
"""
import json
from datetime import datetime

import numpy as np
import pandas as pd

import datasource
import stock_probability as sp  # 复用 bootstrap CI、交易日历、市场 regime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_health_shadow(
    shadow_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_id INTEGER NOT NULL, trade_date TEXT NOT NULL,
    source TEXT, pack_name TEXT, pool_name TEXT,
    code TEXT NOT NULL, score REAL, score_rank INTEGER,
    score_bucket TEXT, ic_state TEXT, market_trend_state TEXT,
    health REAL, health_adjustment REAL, health_score REAL, health_rank INTEGER,
    eval_date TEXT, fwd_5d_return REAL,
    original_top INTEGER DEFAULT 0, health_top INTEGER DEFAULT 0,
    evaluated_at TEXT, created_at TEXT,
    UNIQUE(pick_id, code));
CREATE INDEX IF NOT EXISTS idx_factor_health_eval
ON factor_health_shadow(eval_date, evaluated_at);
CREATE TABLE IF NOT EXISTS factor_health_governance(
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_date TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
    metrics_json TEXT, reasons_json TEXT, created_at TEXT);
"""

GOVERNANCE_DEFAULTS = {
    "min_groups": 30,
    "min_positive_group_rate": 0.55,
    "recent_groups": 10,
    "min_recent_positive_rate": 0.60,
    "bootstrap_samples": 2000,
}

HEALTH_CAP = 0.15       # 单票修正上限 = 名单截面分数标准差的 15%（与单股票 overlay 一致）
MIN_LIST_ITEMS = 4      # 名单少于 4 只不做前半/后半切分
TRAILING_LISTS = 10     # 名单源近期判别力状态的回看名单数
DISC_EPS = 0.005        # 判别力阈值：top-half 与 bottom-half 平均5日收益差（±0.5%）
MIN_CELL_LISTS = 8      # 条件单元最少名单数（按名单聚簇而非标的数，名单内收益相关）
MIN_BUCKET_LISTS = 5    # 仅按分数位置放宽时的最少名单数


def _ensure_schema(c) -> None:
    c.executescript(_SCHEMA)


def _trade_day_offset(day: str, offset: int) -> str | None:
    days = datasource.expected_trade_days("1990-01-01", "2099-12-31")
    if day not in days:
        return None
    i = days.index(day) + offset
    return days[i] if 0 <= i < len(days) else None


def _matured_items(before_date: str) -> pd.DataFrame:
    """before_date 当天及之前已满 5 个交易日的名单明细 + 实际5日收益。

    严格 point-in-time：只含 trade_date < before_date 且 eval_date <= before_date
    的名单；影子创建时的历史估计不会看到未来名单的表现。
    """
    import experience
    with experience._conn() as c:
        picks = pd.read_sql_query(
            "SELECT id,trade_date,source,pack_name,pool_name FROM picks WHERE trade_date<?",
            c, params=(before_date,))
        items = pd.read_sql_query("SELECT pick_id,code,rank,score FROM pick_items", c)
    if picks.empty or items.empty:
        return pd.DataFrame()
    picks = picks.copy()
    picks["eval_date"] = picks["trade_date"].map(lambda d: _trade_day_offset(d, 5))
    picks = picks[picks["eval_date"].notna() & (picks["eval_date"] <= before_date)]
    df = items.merge(picks, left_on="pick_id", right_on="id")
    if df.empty:
        return pd.DataFrame()
    out = []
    with datasource._conn() as c:
        for code, grp in df.groupby("code"):
            need = sorted(set(grp["trade_date"]) | set(grp["eval_date"]))
            marks = ",".join("?" * len(need))
            px = dict(c.execute(
                f"SELECT date,close FROM market_daily WHERE source='ths_ifind' "
                f"AND code=? AND date IN ({marks})", (code, *need)).fetchall())
            for r in grp.itertuples():
                t0, t1 = px.get(r.trade_date), px.get(r.eval_date)
                if t0 and t1:
                    out.append({"pick_id": int(r.pick_id), "trade_date": r.trade_date,
                                "eval_date": r.eval_date, "source": r.source,
                                "pack_name": r.pack_name, "pool_name": r.pool_name,
                                "code": r.code, "score": float(r.score),
                                "fwd_5d_return": float(t1 / t0 - 1)})
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out)
    # 名单内分数位置（top=前半 / bottom=后半）与是否跑赢名单中位数
    df["score_rank"] = df.groupby("pick_id")["score"].rank(ascending=False, method="first")
    size = df.groupby("pick_id")["code"].transform("size")
    df["score_bucket"] = np.where(df["score_rank"] <= np.ceil(size / 2), "top", "bottom")
    med = df.groupby("pick_id")["fwd_5d_return"].transform("median")
    df["beat_median"] = (df["fwd_5d_return"] > med).astype(float)
    # 每个历史名单自己的 PIT 条件变量：其来源截至当日的 IC 状态 + 当日市场 regime
    df = df.sort_values("trade_date")
    ic_cache = {}
    for r in (df[["pick_id", "trade_date", "source", "pack_name", "pool_name"]]
              .drop_duplicates("pick_id").itertuples()):
        ic_cache[r.pick_id] = _trailing_ic_state(df, r.source, r.pack_name,
                                                 r.pool_name, r.trade_date)
    df["ic_state"] = df["pick_id"].map(ic_cache)
    market = sp._load_market()
    if market.empty:
        df["market_trend_state"] = None
    else:
        regime_map = dict(zip(market["date"].dt.strftime("%Y-%m-%d"),
                              market["market_trend_state"].astype(str)))
        df["market_trend_state"] = df["trade_date"].map(regime_map)
    return df


def _trailing_ic_state(history: pd.DataFrame, source: str, pack_name, pool_name,
                       asof: str) -> str:
    """名单源近期判别力状态：最近 ≤10 个成熟名单的 top-half 与 bottom-half
    平均5日收益差的均值。不足 3 个成熟名单为 unknown（证据不足不装知道）。"""
    if history.empty:
        return "unknown"
    h = history[(history["source"] == source)
                & (history["pack_name"].fillna("") == (pack_name or ""))
                & (history["pool_name"].fillna("") == (pool_name or ""))
                & (history["trade_date"] < asof)]
    if h.empty:
        return "unknown"
    latest_picks = (h[["pick_id", "trade_date"]].drop_duplicates()
                    .sort_values("trade_date", ascending=False)
                    .head(TRAILING_LISTS)["pick_id"])
    discs = []
    for pick_id in latest_picks:
        g = h[h["pick_id"] == pick_id]
        top = g.loc[g["score_bucket"] == "top", "fwd_5d_return"]
        bottom = g.loc[g["score_bucket"] == "bottom", "fwd_5d_return"]
        if len(top) and len(bottom):
            discs.append(float(top.mean() - bottom.mean()))
    if len(discs) < 3:
        return "unknown"
    d = float(np.mean(discs))
    if d > DISC_EPS:
        return "rising"
    if d < -DISC_EPS:
        return "decaying"
    return "flat"


def _shrink_rate(rates: pd.Series, prior: float = 0.5, strength: int = 10) -> dict:
    """名单级聚簇频率收缩。同一名单内标的收益相关，样本量按名单数而非标的数计。"""
    n = len(rates)
    if n == 0:
        return {"raw": None, "shrunk": prior, "n": 0}
    raw = float(rates.mean())
    return {"raw": raw, "shrunk": (raw * n + prior * strength) / (n + strength), "n": n}


def _estimate_health(history: pd.DataFrame, bucket: str, ic_state: str,
                     regime: str | None) -> tuple[dict, str]:
    """条件频率估计 P(跑赢同批中位 | 分数位置 × 名单源IC状态 × 市场regime)。

    逐级放宽：单元名单数不足时先丢 regime、再丢 IC 状态、最后只按分数位置；
    仍不足则返回先验 0.5（证据不足不装知道）。
    """
    if history.empty:
        return _shrink_rate(pd.Series(dtype=float)), "先验(无历史)"
    cell = history[history["score_bucket"] == bucket]
    cond_steps = []
    if regime:
        cond_steps.append(({"ic_state": ic_state, "market_trend_state": regime},
                           "分数位置+IC状态+市场regime"))
    cond_steps.append(({"ic_state": ic_state}, "分数位置+IC状态"))
    if regime:
        cond_steps.append(({"market_trend_state": regime}, "分数位置+市场regime"))
    cond_steps.append(({}, "仅分数位置"))
    for cond, label in cond_steps:
        sub = cell
        for col, val in cond.items():
            sub = sub[sub[col] == val]
        rates = sub.groupby("pick_id")["beat_median"].mean()
        need = MIN_CELL_LISTS if cond else MIN_BUCKET_LISTS
        if len(rates) >= need:
            return _shrink_rate(rates), label
    return _shrink_rate(pd.Series(dtype=float)), "先验(证据不足)"


def update_health_shadow(trade_date: str, top_n: int = 10) -> dict:
    """为当天正式候选记录健康压缩对照排序（影子，不改正式名单）。"""
    import experience
    picks = experience.picks_on_date(trade_date)
    if picks.empty:
        return {"picks": 0, "rows": 0, "eval_date": None}
    # 每个来源/策略包/股票池的最新一条名单，避免同组合重复处理
    picks = picks.drop_duplicates(subset=["source", "pack_name", "pool_name"], keep="first")
    history = _matured_items(trade_date)
    market = sp._load_market()
    regime = None
    if not market.empty:
        hit = market[market["date"] == pd.Timestamp(trade_date)]
        if not hit.empty:
            regime = str(hit.iloc[0]["market_trend_state"])
    eval_date = _trade_day_offset(trade_date, 5)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_rows = 0
    for pick in picks.itertuples():
        items = experience.pick_items_detail(int(pick.id))
        if items.empty or len(items) < MIN_LIST_ITEMS:
            continue
        scores = items.set_index("code")["score"].dropna().astype(float).sort_values(
            ascending=False)
        if len(scores) < MIN_LIST_ITEMS or scores.std() == 0 or pd.isna(scores.std()):
            continue
        ic_state = _trailing_ic_state(history, pick.source, pick.pack_name,
                                      pick.pool_name, trade_date)
        scale = float(scores.std())
        eval_top_n = min(top_n, max(1, len(scores) // 2))
        entries = []
        for rank, (code, score) in enumerate(scores.items(), start=1):
            bucket = "top" if rank <= int(np.ceil(len(scores) / 2)) else "bottom"
            est, _method = _estimate_health(history, bucket, ic_state, regime)
            adjustment = float(np.clip((est["shrunk"] - 0.5) * 2, -1.0, 1.0)
                               * HEALTH_CAP * scale)
            entries.append({"code": code, "score": float(score), "score_rank": rank,
                            "bucket": bucket, "health": est["shrunk"],
                            "adjustment": adjustment})
        health_order = sorted(entries, key=lambda e: e["score"] + e["adjustment"],
                              reverse=True)
        health_rank = {e["code"]: i + 1 for i, e in enumerate(health_order)}
        rows = []
        for e in entries:
            rows.append((int(pick.id), trade_date, pick.source, pick.pack_name,
                         pick.pool_name, e["code"], e["score"], e["score_rank"],
                         e["bucket"], ic_state, regime, e["health"], e["adjustment"],
                         e["score"] + e["adjustment"], health_rank[e["code"]], eval_date,
                         int(e["score_rank"] <= eval_top_n),
                         int(health_rank[e["code"]] <= eval_top_n), now))
        with datasource._conn() as c:
            _ensure_schema(c)
            c.executemany(
                "INSERT INTO factor_health_shadow"
                "(pick_id,trade_date,source,pack_name,pool_name,code,score,score_rank,"
                "score_bucket,ic_state,market_trend_state,health,health_adjustment,"
                "health_score,health_rank,eval_date,original_top,health_top,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(pick_id,code) DO UPDATE SET "
                "score=excluded.score,score_rank=excluded.score_rank,"
                "score_bucket=excluded.score_bucket,ic_state=excluded.ic_state,"
                "market_trend_state=excluded.market_trend_state,health=excluded.health,"
                "health_adjustment=excluded.health_adjustment,"
                "health_score=excluded.health_score,health_rank=excluded.health_rank,"
                "eval_date=excluded.eval_date,original_top=excluded.original_top,"
                "health_top=excluded.health_top,created_at=excluded.created_at", rows)
        total_rows += len(rows)
    return {"picks": len(picks), "rows": total_rows, "eval_date": eval_date}


def evaluate_health(asof: str) -> dict:
    """回填已满5个交易日的影子记录，并比较健康排序与原排序的名单级收益。"""
    with datasource._conn() as c:
        _ensure_schema(c)
        pending = pd.read_sql_query(
            "SELECT shadow_id,code,trade_date,eval_date FROM factor_health_shadow "
            "WHERE evaluated_at IS NULL AND eval_date IS NOT NULL AND eval_date<=?",
            c, params=(asof,))
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
                "UPDATE factor_health_shadow SET fwd_5d_return=?,evaluated_at=? "
                "WHERE shadow_id=?", updated)
    groups = _group_health_lifts()
    if groups.empty:
        return {"evaluated": len(updated), "groups": 0, "original_avg": None,
                "health_avg": None, "lift": None}
    return {"evaluated": len(updated), "total_rows": int(groups["rows"].sum()),
            "groups": int(len(groups)),
            "original_avg": float(groups["original_return"].mean()),
            "health_avg": float(groups["health_return"].mean()),
            "lift": float(groups["lift"].mean())}


def _group_health_lifts() -> pd.DataFrame:
    """每个名单作为一个独立配对样本，避免股票多的名单获得更高权重。"""
    with datasource._conn() as c:
        _ensure_schema(c)
        done = pd.read_sql_query(
            "SELECT trade_date,pick_id,fwd_5d_return,original_top,health_top "
            "FROM factor_health_shadow WHERE evaluated_at IS NOT NULL", c)
    if done.empty:
        return pd.DataFrame()
    rows = []
    for (trade_date, pick_id), group in done.groupby(["trade_date", "pick_id"]):
        original = group[group["original_top"] == 1]["fwd_5d_return"].dropna()
        health = group[group["health_top"] == 1]["fwd_5d_return"].dropna()
        if original.empty or health.empty:
            continue
        rows.append({"trade_date": trade_date, "pick_id": int(pick_id),
                     "original_return": float(original.mean()),
                     "health_return": float(health.mean()),
                     "lift": float(health.mean() - original.mean()),
                     "rows": int(len(group))})
    return pd.DataFrame(rows).sort_values(["trade_date", "pick_id"]) if rows else pd.DataFrame()


def health_governance(asof: str | None = None, cfg: dict | None = None,
                      persist: bool = True) -> dict:
    """评估健康压缩是否具备人工晋级资格；永远不改变执行开关。"""
    cfg = {**GOVERNANCE_DEFAULTS, **(cfg or {})}
    groups = _group_health_lifts()
    audit_date = asof or datetime.now().strftime("%Y-%m-%d")
    if groups.empty:
        metrics = {"groups": 0, "mean_lift": None, "positive_group_rate": None,
                   "recent_positive_rate": None, "ci_low": None, "ci_high": None}
    else:
        lifts = groups["lift"].to_numpy(dtype=float)
        recent = groups.tail(int(cfg["recent_groups"]))
        ci_low, ci_high = sp._bootstrap_mean_ci(lifts, int(cfg["bootstrap_samples"]))
        metrics = {"groups": int(len(groups)),
                   "mean_lift": float(lifts.mean()),
                   "median_lift": float(np.median(lifts)),
                   "positive_group_rate": float((lifts > 0).mean()),
                   "recent_groups": int(len(recent)),
                   "recent_positive_rate": float((recent["lift"] > 0).mean()),
                   "ci_low": ci_low, "ci_high": ci_high,
                   "original_avg": float(groups["original_return"].mean()),
                   "health_avg": float(groups["health_return"].mean())}
    checks = [
        (metrics["groups"] >= cfg["min_groups"],
         f"成熟名单至少 {cfg['min_groups']} 组（当前 {metrics['groups']}）"),
        ((metrics["mean_lift"] or 0) > 0,
         f"平均5日增益必须为正（当前 {(metrics['mean_lift'] or 0):+.2%}）"),
        ((metrics["positive_group_rate"] or 0) >= cfg["min_positive_group_rate"],
         f"正增益名单占比至少 {cfg['min_positive_group_rate']:.0%}"
         f"（当前 {(metrics['positive_group_rate'] or 0):.1%}）"),
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
                "INSERT OR REPLACE INTO factor_health_governance"
                "(audit_date,status,metrics_json,reasons_json,created_at) VALUES(?,?,?,?,?)",
                (audit_date, status, json.dumps({"metrics": metrics, "config": cfg},
                                                ensure_ascii=False),
                 json.dumps(reasons, ensure_ascii=False),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return result


def health_summary() -> dict:
    with datasource._conn() as c:
        _ensure_schema(c)
        total = c.execute("SELECT COUNT(*) FROM factor_health_shadow").fetchone()[0]
        evaluated = c.execute(
            "SELECT COUNT(*) FROM factor_health_shadow WHERE evaluated_at IS NOT NULL"
        ).fetchone()[0]
        if evaluated:
            row = c.execute(
                "SELECT AVG(CASE WHEN original_top=1 THEN fwd_5d_return END),"
                "AVG(CASE WHEN health_top=1 THEN fwd_5d_return END),"
                "COUNT(DISTINCT trade_date || ':' || pick_id) "
                "FROM factor_health_shadow WHERE evaluated_at IS NOT NULL").fetchone()
            original_avg, health_avg, groups = row
        else:
            original_avg = health_avg = None; groups = 0
    lift = (float(health_avg - original_avg)
            if original_avg is not None and health_avg is not None else None)
    return {"total": total, "evaluated": evaluated, "groups": groups,
            "original_avg": original_avg, "health_avg": health_avg, "lift": lift}


def health_detail(limit: int = 200) -> pd.DataFrame:
    with datasource._conn() as c:
        _ensure_schema(c)
        return pd.read_sql_query(
            "SELECT trade_date,source,pack_name,code,score_rank,score_bucket,ic_state,"
            "market_trend_state,health,health_adjustment,health_rank,eval_date,"
            "fwd_5d_return,original_top,health_top,evaluated_at "
            "FROM factor_health_shadow ORDER BY trade_date DESC,pick_id DESC,score_rank "
            "LIMIT ?", c, params=(int(limit),))
