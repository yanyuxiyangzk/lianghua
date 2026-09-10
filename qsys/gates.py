"""P1：11 项硬闸门（对标中金 Loop 框架验证端）+ 因子哈希检查点。

闸门（全部通过才算 accepted）：
  1. |IC| > 0.03                     2. 2025 年超额 > 0     3. 2026 年超额 > 0
  4. 2025 夏普 > 0.5                 5. 2026 夏普 > 0.5     6. Calmar > 1.0
  7. 近 9 月超额 > 0                 8. 近 12 月超额 > 0    9-11. 与库内因子 IC 相关 < 0.70（取 max）
口径：5 日换仓、Top10% 多头、超额=Top组均值−池均值、单边千一成本（在超额里扣）。
"""

import json
from datetime import datetime

import numpy as np
import pandas as pd

import factor_eval as fe

GATE = {
    # IC 阈值按池校准：文章为全市场口径（0.03），本系统默认沪深300池，
    # 截面离散度更低导致 IC 系统性偏低约 1/3 → 校准为 0.02（换全市场池时调回 0.03）
    "IC_MIN": 0.02,
    "SHARPE_MIN": 0.5,
    "CALMAR_MIN": 1.0,
    "CORR_MAX": 0.70,
    "FWD_DAYS": 5,
    "TOP_PCT": 0.10,
    "COST": 0.001,          # 单边千一
    "LOOKBACK_DAYS": 600,   # 近600个交易日
}

# 类型特定门控阈值：不同因子类型使用不同的IC阈值
# 基于历史数据：板块轮动ICIR最高(0.33)，资金流次之(0.23)，龙虎榜/爆量最低(0.08-0.13)
TYPE_SPECIFIC_GATES = {
    "量价": {"IC_MIN": 0.02, "SHARPE_MIN": 0.5},           # 标准
    "资金流": {"IC_MIN": 0.025, "SHARPE_MIN": 0.5},        # 稍高（质量好）
    "板块轮动": {"IC_MIN": 0.03, "SHARPE_MIN": 0.6},       # 最高（质量最好）
    "指数": {"IC_MIN": 0.015, "SHARPE_MIN": 0.4},          # 降低（数据稀缺）
    "盘口异动": {"IC_MIN": 0.02, "SHARPE_MIN": 0.5},       # 标准
    "龙虎榜": {"IC_MIN": 0.015, "SHARPE_MIN": 0.4},        # 降低（质量差）
    "爆量抢筹": {"IC_MIN": 0.015, "SHARPE_MIN": 0.4},      # 降低（质量差）
}


def get_factor_gates(factor_type: str | None = None) -> dict:
    """获取因子类型的门控阈值。"""
    if factor_type and factor_type in TYPE_SPECIFIC_GATES:
        return {**GATE, **TYPE_SPECIFIC_GATES[factor_type]}
    return GATE.copy()


def _daily_excess(vals: pd.Series, fwd: pd.DataFrame) -> pd.Series:
    """逐日超额序列：因子 Top10% 组合 forward 收益 − 池均值 − 双边成本摊薄。"""
    v = fe._norm(vals.dropna())
    fr = fwd.stack().rename("r")
    j = v.rename("f").to_frame().join(fr, how="inner").dropna()
    if j.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))

    def _x(g: pd.DataFrame) -> float:
        k = max(1, int(len(g) * GATE["TOP_PCT"]))
        top = g.nlargest(k, "f")["r"].mean()
        return float(top - g["r"].mean())

    x = j.groupby(level="datetime").apply(_x)
    cost_per_period = 2 * GATE["COST"] / GATE["FWD_DAYS"]  # 双边成本摊到每日
    return (x - cost_per_period).sort_index()


def _sharpe(x: pd.Series) -> float:
    return float(x.mean() / (x.std() + 1e-12) * np.sqrt(252)) if len(x) > 5 else 0.0


def _max_dd(nav: pd.Series) -> float:
    return float(((nav - nav.cummax()) / nav.cummax()).min()) if len(nav) else 0.0


def evaluate_gates(vals: pd.Series, panel: pd.DataFrame,
                   library_ics: dict[str, pd.Series] | None = None) -> dict:
    """返回 {pass, reasons, metrics}。library_ics: {因子名: IC序列} 用于相关性闸门。"""
    vals = fe._norm(vals.dropna())
    if GATE["LOOKBACK_DAYS"]:
        unique_dates = vals.index.get_level_values("datetime").unique()
        if len(unique_dates) >= GATE["LOOKBACK_DAYS"]:
            cutoff = unique_dates[-GATE["LOOKBACK_DAYS"]:][0]
            vals = vals[vals.index.get_level_values("datetime") >= cutoff]
    fwd = fe.forward_returns(panel, GATE["FWD_DAYS"])
    ic = fe.ic_series(vals, fwd)
    metrics = {}
    reasons = []

    ic_abs = abs(float(ic.mean())) if len(ic) else 0.0
    metrics["IC"] = round(float(ic.mean()), 4) if len(ic) else 0.0
    if ic_abs < GATE["IC_MIN"]:
        reasons.append(f"|IC| {ic_abs:.3f} < {GATE['IC_MIN']}")

    # 多重检验校正：计算 IC 的统计显著性 p-value
    n_days = len(ic)
    ic_std = float(ic.std()) if len(ic) > 1 else 1.0
    if n_days >= 10 and ic_std > 1e-12:
        p_val = fe.ic_pvalue(float(ic.mean()), ic_std, n_days)
        metrics["p_value"] = round(p_val, 6)
        # 使用更严格的显著性阈值（考虑多重检验）
        if p_val > 0.01:
            reasons.append(f"IC p-value {p_val:.4f} > 0.01（统计不显著）")
    else:
        metrics["p_value"] = 1.0

    x = _daily_excess(vals, fwd)
    nav = (1 + x).cumprod()

    def _year_stats(year: int):
        if len(x) == 0 or not hasattr(x.index, 'year'):
            return 0.0, 0.0
        xy = x[x.index.year == year]
        if len(xy) < 20:
            return 0.0, 0.0
        return float(xy.mean() * 252), _sharpe(xy)

    now_year = datetime.now().year
    for year in range(now_year - 1, now_year + 1):
        tag = str(year)
        exc, shp = _year_stats(year)
        metrics[f"超额{tag}"] = round(exc, 4)
        metrics[f"夏普{tag}"] = round(shp, 2)
        if exc <= 0:
            reasons.append(f"{tag}年超额 {exc:.2%} ≤ 0")
        if shp < GATE["SHARPE_MIN"]:
            reasons.append(f"{tag}夏普 {shp:.2f} < {GATE['SHARPE_MIN']}")

    ann = float(x.mean() * 252) if len(x) else 0.0
    mdd = _max_dd(nav)
    calmar = abs(ann / mdd) if mdd < 0 else 0.0
    metrics["Calmar"] = round(calmar, 2)
    if calmar < GATE["CALMAR_MIN"]:
        reasons.append(f"Calmar {calmar:.2f} < {GATE['CALMAR_MIN']}")

    for months, tag in [(9, "近9月"), (12, "近12月")]:
        if len(x):
            cut = x.index.max() - pd.Timedelta(days=months * 30)
            xm = x[x.index >= cut]
            exc_m = float(xm.sum()) if len(xm) else 0.0
            metrics[tag] = round(exc_m, 4)
            if exc_m <= 0:
                reasons.append(f"{tag}超额 {exc_m:.2%} ≤ 0")

    max_corr = 0.0
    if library_ics:
        for name, other_ic in library_ics.items():
            both = pd.concat([ic, other_ic], axis=1, keys=["a", "b"]).dropna()
            if len(both) > 30:
                c = abs(float(both["a"].corr(both["b"])))
                max_corr = max(max_corr, c)
    metrics["最大IC相关"] = round(max_corr, 2)
    if max_corr >= GATE["CORR_MAX"]:
        reasons.append(f"IC相关 {max_corr:.2f} ≥ {GATE['CORR_MAX']}")

    # Gate 12: OOS验证（最近20%数据作为验证集）
    if len(ic) >= 20:
        split_idx = int(len(ic) * 0.8)
        ic_oos = ic.iloc[split_idx:]
        oos_ic_mean = float(ic_oos.mean())
        oos_ic_wr = float((ic_oos > 0).mean())
        metrics["OOS_IC"] = round(oos_ic_mean, 4)
        metrics["OOS_IC胜率"] = round(oos_ic_wr, 2)
        if oos_ic_mean < 0.01:
            reasons.append(f"OOS IC {oos_ic_mean:.4f} < 0.01")
        if oos_ic_wr < 0.50:
            reasons.append(f"OOS IC胜率 {oos_ic_wr:.1%} < 50%")

    return {"pass": len(reasons) == 0, "reasons": reasons, "metrics": metrics}


def adaptive_gate_thresholds(factor_pool: list[dict] | None = None) -> dict:
    """自适应门控阈值：根据因子池质量动态调整。

    Args:
        factor_pool: 因子池列表，每个因子包含 name, ic_mean, gate_status 等

    Returns:
        自适应后的阈值字典
    """
    # 默认阈值
    adaptive = GATE.copy()

    if not factor_pool or len(factor_pool) < 20:
        # 样本不足，使用默认阈值
        return adaptive

    # 计算因子池质量指标
    ic_values = [f.get("ic_mean", 0) for f in factor_pool if f.get("gate_status") == 1]
    if not ic_values:
        return adaptive

    avg_ic = np.mean(ic_values)
    ic_std = np.std(ic_values) if len(ic_values) > 1 else 0.01

    # 自适应调整规则
    # 1. 如果因子池质量高（平均IC高），提高门槛
    # 2. 如果因子池质量低，降低门槛
    # 3. 如果因子池方差大，收紧门槛

    if avg_ic > 0.03:
        # 高质量池子，提高门槛
        adaptive["IC_MIN"] = min(0.04, avg_ic * 0.8)
        adaptive["SHARPE_MIN"] = min(0.7, GATE["SHARPE_MIN"] * 1.2)
    elif avg_ic < 0.015:
        # 低质量池子，降低门槛
        adaptive["IC_MIN"] = max(0.01, avg_ic * 0.7)
        adaptive["SHARPE_MIN"] = max(0.3, GATE["SHARPE_MIN"] * 0.8)

    # 如果方差大，收紧相关性门槛（避免高相关因子堆积）
    if ic_std > 0.02:
        adaptive["CORR_MAX"] = max(0.60, GATE["CORR_MAX"] - 0.05)

    return adaptive


def analyze_failure_patterns() -> dict:
    """分析失败模式，提供生成指导。

    Returns:
        {
            "common_failures": list,  # 常见失败原因
            "avoid_patterns": list,   # 应避免的模式
            "suggestions": list,      # 改进建议
        }
    """
    import sqlite3
    from pathlib import Path

    db_path = Path("/data/market.db")
    if not db_path.exists():
        return {"common_failures": [], "avoid_patterns": [], "suggestions": []}

    try:
        with sqlite3.connect(str(db_path), timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")

            # 确保表存在
            conn.execute("""CREATE TABLE IF NOT EXISTS failure_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_name TEXT, skeleton TEXT, family TEXT,
                reason TEXT, engine TEXT, created_at TEXT
            )""")

            # 查询失败记录
            rows = conn.execute(
                "SELECT reason, COUNT(*) as cnt FROM failure_patterns "
                "GROUP BY reason ORDER BY cnt DESC LIMIT 10"
            ).fetchall()

            common_failures = [{"reason": r[0], "count": r[1]} for r in rows]

            # 分析常见失败原因
            avoid_patterns = []
            suggestions = []

            for f in common_failures:
                reason = f["reason"]
                if "过拟合" in reason or "gap" in reason.lower():
                    avoid_patterns.append("IS/OOS差异过大的结构")
                    suggestions.append("增加正则化，限制表达式复杂度")
                elif "样本不足" in reason or "insufficient" in reason.lower():
                    avoid_patterns.append("小样本因子")
                    suggestions.append("优先生成有足够数据支撑的因子")
                elif "相关性" in reason or "corr" in reason.lower():
                    avoid_patterns.append("高相关因子")
                    suggestions.append("增加多样性压力，避免相似结构")
                elif "衰减" in reason or "decay" in reason.lower():
                    avoid_patterns.append("易衰减结构")
                    suggestions.append("关注因子稳定性，避免过度拟合特定市场环境")

            return {
                "common_failures": common_failures,
                "avoid_patterns": avoid_patterns,
                "suggestions": suggestions,
            }

    except Exception as e:
        return {"common_failures": [], "avoid_patterns": [], "suggestions": [str(e)]}


def record_gate_failure(factor_name: str, reasons: list[str], metrics: dict):
    """记录门控失败信息，用于失败模式学习。"""
    import sqlite3
    from pathlib import Path

    db_path = Path("/data/market.db")
    if not db_path.exists():
        return

    try:
        with sqlite3.connect(str(db_path), timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")

            # 确保表存在
            conn.execute("""CREATE TABLE IF NOT EXISTS gate_failure_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_name TEXT, reasons TEXT, metrics TEXT,
                created_at TEXT
            )""")

            conn.execute(
                "INSERT INTO gate_failure_log (factor_name, reasons, metrics, created_at) "
                "VALUES (?, ?, ?, ?)",
                (factor_name, json.dumps(reasons), json.dumps(metrics),
                 datetime.now().isoformat())
            )
    except Exception:
        pass


def log_gate_detail(factor_name: str, gate_date: str, result: dict, pool_name: str = "沪深300"):
    """将闸门评估明细写入 gate_detail_log 表。"""
    import logging
    import library
    try:
        with library._lconn() as c:
            c.execute(
                "INSERT INTO gate_detail_log "
                "(factor_name, gate_date, pool_name, metrics, passed, fail_reasons, created_at) "
                "VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(factor_name, gate_date, pool_name) DO UPDATE SET"
                " metrics=excluded.metrics, passed=excluded.passed,"
                " fail_reasons=excluded.fail_reasons, created_at=excluded.created_at",
                (factor_name, gate_date, pool_name,
                 json.dumps(result.get("metrics", {}), ensure_ascii=False),
                 1 if result.get("pass") else 0,
                 json.dumps(result.get("reasons", []), ensure_ascii=False),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    except Exception as e:
        logging.warning("[gates] log_gate_detail failed: %s", e)


def factor_hash(text: str) -> str:
    """结构哈希：规范化文本（去空白/注释）后的 md5。"""
    import hashlib
    import re

    norm = re.sub(r"#.*", "", str(text))
    norm = re.sub(r"\s+", "", norm)
    return hashlib.md5(norm.encode()).hexdigest()


# ---------------------------------------------------------------- 事件版硬闸门（定向挖因子用）
# 与收益版闸门的区别：标签是"未来 h 日内是否发生事件"（0/1），不是远期收益。
# 稀有事件（涨停基础率 ~1-3%）下 IC 数值天然偏小，阈值重新标定；
# 核心指标是十分位提升倍数：Top10% 组合的事件发生率 / 全池基础率。
EVT_GATE = {
    "IC_MIN": 0.01,        # 事件标签 |RankIC| 下限（稀有事件口径，先跑标定再收紧）
    "LIFT_MIN": 2.0,       # 十分位事件率提升倍数 ≥ 2x
    "HALF_LIFT_MIN": 1.3,  # 时间轴前后两半各自的提升倍数下限（稳定性）
    "CORR_MAX": 0.70,      # 与已入库事件因子的 IC 相关上限
    "HORIZON": 5,          # 标签窗口：未来 h 日内发生事件
}


def event_labels(panel: pd.DataFrame, kind: str, horizon: int = EVT_GATE["HORIZON"]) -> pd.Series:
    """未来 horizon 日事件标签长表 [(datetime, instrument)] → 0.0/1.0。
    label_t = 事件在 (t, t+horizon] 任一日发生。"""
    m = fe._event_mask(panel, kind)  # datetime × instrument 布尔
    lab = m.iloc[::-1].rolling(horizon).max().iloc[::-1].shift(-1)  # 反向滚动=向后看
    return lab.stack().dropna().astype(float)


def evaluate_event_gates(vals: pd.Series, panel: pd.DataFrame, kind: str,
                         horizon: int = EVT_GATE["HORIZON"],
                         library_ics: dict | None = None) -> dict:
    """事件版闸门：事件IC |μ|≥阈值 + 十分位提升≥2x + 两半稳定 + 库内相关<0.7。
    返回 {pass, reasons, metrics}，与 evaluate_gates 同构。"""
    v = fe._norm(vals.dropna())
    if GATE["LOOKBACK_DAYS"]:
        unique_dates = v.index.get_level_values("datetime").unique()
        if len(unique_dates) >= GATE["LOOKBACK_DAYS"]:
            cutoff = unique_dates[-GATE["LOOKBACK_DAYS"]:][0]
            v = v[v.index.get_level_values("datetime") >= cutoff]
    lab = event_labels(panel, kind, horizon)
    j = v.rename("f").to_frame().join(lab.rename("y"), how="inner").dropna()
    metrics, reasons = {}, []
    if j.empty or j["y"].sum() < 20:
        return {"pass": False, "reasons": ["事件样本不足（<20）"], "metrics": {}}

    def _ic(g):
        return g["f"].corr(g["y"], method="spearman") if len(g) >= 30 else np.nan

    ic = j.groupby(level="datetime").apply(_ic).dropna()
    if len(ic) < 60:
        return {"pass": False, "reasons": ["有效 IC 天数不足"], "metrics": {}}
    ic_abs = abs(float(ic.mean()))
    metrics["事件IC"] = round(float(ic.mean()), 4)
    if ic_abs < EVT_GATE["IC_MIN"]:
        reasons.append(f"|事件IC| {ic_abs:.3f} < {EVT_GATE['IC_MIN']}")

    # 十分位提升：因子 Top10% 的日子-股票上，事件率 / 基础率
    def _lift(sub: pd.DataFrame) -> float:
        base = float(sub["y"].mean())
        if base <= 0:
            return 0.0
        top_rate = (
            sub.groupby(level="datetime")
               .apply(lambda g: g.loc[g["f"] >= g["f"].quantile(0.9), "y"].mean()
                      if len(g) >= 30 else np.nan)
               .dropna().mean())
        return float(top_rate / base) if top_rate == top_rate else 0.0

    metrics["基础事件率"] = round(float(j["y"].mean()), 4)
    lift = _lift(j)
    metrics["十分位提升"] = round(lift, 2)
    if lift < EVT_GATE["LIFT_MIN"]:
        reasons.append(f"十分位提升 {lift:.1f}x < {EVT_GATE['LIFT_MIN']}x")
    mid = j.index.get_level_values("datetime").unique()[len(j.index.get_level_values("datetime").unique()) // 2]
    for tag, sub in [("前半", j[j.index.get_level_values("datetime") < mid]),
                     ("后半", j[j.index.get_level_values("datetime") >= mid])]:
        lh = _lift(sub)
        metrics[f"{tag}提升"] = round(lh, 2)
        if lh < EVT_GATE["HALF_LIFT_MIN"]:
            reasons.append(f"{tag}提升 {lh:.1f}x < {EVT_GATE['HALF_LIFT_MIN']}x（不稳定）")

    max_corr = 0.0
    for name, other in (library_ics or {}).items():
        both = pd.concat([ic, other], axis=1, keys=["a", "b"]).dropna()
        if len(both) > 30:
            max_corr = max(max_corr, abs(float(both["a"].corr(both["b"]))))
    metrics["最大IC相关"] = round(max_corr, 2)
    if max_corr >= EVT_GATE["CORR_MAX"]:
        reasons.append(f"IC相关 {max_corr:.2f} ≥ {EVT_GATE['CORR_MAX']}")
    return {"pass": len(reasons) == 0, "reasons": reasons, "metrics": metrics}
