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


def _daily_excess(vals: pd.Series, fwd: pd.DataFrame) -> pd.Series:
    """逐日超额序列：因子 Top10% 组合 forward 收益 − 池均值 − 双边成本摊薄。"""
    v = fe._norm(vals.dropna())
    fr = fwd.stack().rename("r")
    j = v.rename("f").to_frame().join(fr, how="inner").dropna()
    if j.empty:
        return pd.Series(dtype=float)

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

    x = _daily_excess(vals, fwd)
    nav = (1 + x).cumprod()

    def _year_stats(year: int):
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
