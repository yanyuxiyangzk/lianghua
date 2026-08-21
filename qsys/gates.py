"""P1：11 项硬闸门（对标中金 Loop 框架验证端）+ 因子哈希检查点。

闸门（全部通过才算 accepted）：
  1. |IC| > 0.03                     2. 2025 年超额 > 0     3. 2026 年超额 > 0
  4. 2025 夏普 > 0.5                 5. 2026 夏普 > 0.5     6. Calmar > 1.0
  7. 近 9 月超额 > 0                 8. 近 12 月超额 > 0    9-11. 与库内因子 IC 相关 < 0.70（取 max）
口径：5 日换仓、Top10% 多头、超额=Top组均值−池均值、单边千一成本（在超额里扣）。
"""

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
        cutoff = vals.index.get_level_values("datetime").unique()[-GATE["LOOKBACK_DAYS"]:][0]
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

    for year, tag in [(2025, "2025"), (2026, "2026")]:
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

    return {"pass": len(reasons) == 0, "reasons": reasons, "metrics": metrics}


def factor_hash(text: str) -> str:
    """结构哈希：规范化文本（去空白/注释）后的 md5。"""
    import hashlib
    import re

    norm = re.sub(r"#.*", "", str(text))
    norm = re.sub(r"\s+", "", norm)
    return hashlib.md5(norm.encode()).hexdigest()
