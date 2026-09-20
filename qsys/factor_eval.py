"""因子评估与组合引擎：胜率体检 → 去冗余 → 加权 → 样本外验证。

方法学红线：
  - 一切统计 point-in-time：估计权重只用决策日之前已"可观测"的 IC
    （IC 用到未来 fwd 日收益，故估计窗右端再回退 fwd_days）
  - 决策只看 walk-forward 样本外（OOS）结果；样本内（IS）仅作对照
  - QSYS 不生成新因子表达式，只对已有因子做权重/过滤配置
"""

import hashlib
import random
from pathlib import Path

import numpy as np
import pandas as pd

import signals as sig
from common import DATA_DIR, get_last_trade_day

EVAL_DIR = DATA_DIR / "cache" / "eval"
FWD_DAYS = [1, 5, 10, 20, 40]
MAIN_FWD = 5            # 主评估窗口（交易日）：与闸门统一为5日
EST_WINDOW = 250       # walk-forward 估计窗（交易日）
STEP_DAYS = 5          # walk-forward 应用窗/步长：与持有期对齐
CORR_THRESHOLD = 0.7   # 去冗余相关性阈值
DEFAULT_COST = 0.0025  # 双边交易成本（千一×2）
# 多周期胜率标准（交易日）：1天/5天/1月/3月/6月 —— 因子与策略统一按此衡量
WIN_HORIZONS = {"1日": 1, "5日": 5, "20日": 20, "60日": 60, "120日": 120}

# 多目标评分权重（经验校准：walk-forward OOS 验证）
MULTI_OBJECTIVE_WEIGHTS = {
    'ic': 0.65,        # IC主导（对数缩放+ICIR加权）
    'risk': 0.10,      # 风险阈值惩罚
    'sharpe': 0.10,    # 夏普阈值惩罚
    'crowding': 0.10,  # 拥挤度评分
    'stability': 0.05, # IC稳定性
}


# ---------------------------------------------------------------- 多重检验校正
def bh_fdr(pvalues: pd.Series, alpha: float = 0.05) -> pd.Series:
    """Benjamini-Hochberg FDR 校正：返回 adjusted p-values。
    
    解决批量评估 N 个因子时的第一类错误膨胀问题。
    当同时测试 1000 个因子时，即使每个因子真实 IC=0，
    5% 显著性水平下也会有 ~50 个因子"看起来显著"。
    FDR 校正控制的是"被拒绝的假设中假阳性的比例"。"""
    p = pvalues.dropna().copy()
    if p.empty:
        return pvalues
    n = len(p)
    ranked = p.rank(method="first")
    adjusted = p * n / ranked
    adjusted = adjusted.clip(upper=1.0)
    # 保持单调性（从最大 p 值开始）
    adjusted = adjusted.sort_values(ascending=False).cummin()
    return adjusted.reindex(pvalues.index)


def ic_pvalue(ic_mean: float, ic_std: float, n_days: int) -> float:
    """基于 IC 均值和标准差计算近似 p-value（双侧 t 检验）。
    
    H0: IC = 0（因子无预测能力）
    t = IC_mean / (IC_std / sqrt(n))
    p = 2 * (1 - CDF(|t|, df=n-1))
    
    使用正态近似（大样本下 t 分布趋近正态），无需 scipy。"""
    if n_days < 10 or ic_std < 1e-12:
        return 1.0
    t_stat = ic_mean / (ic_std / (n_days ** 0.5))
    # 正态分布 CDF 近似：Φ(x) ≈ 1 - φ(x)(b1*t + b2*t² + b3*t³ + b4*t⁴ + b5*t⁵)
    # 其中 t = 1/(1+0.2316419*|x|), φ(x) = exp(-x²/2)/sqrt(2π)
    import math
    x = abs(t_stat)
    t = 1.0 / (1.0 + 0.2316419 * x)
    phi = math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)
    p = phi * (0.319381530 * t - 0.356563782 * t**2 + 1.781477937 * t**3 
               - 1.821255978 * t**4 + 1.330274429 * t**5)
    if t_stat < 0:
        p = 1.0 - p
    return float(2.0 * min(p, 1.0 - p))


def ic_pvalue_robust(ic_series: pd.Series, max_lag: int | None = None) -> float:
    """Newey-West HAC 标准误的 p-value。

    金融因子 IC 序列存在自相关（波动聚集），简单标准误会低估不确定性，
    导致 p-value 偏小、假阳性增加。Newey-West 在方差估计中加入自相关项，
    给出更保守（更诚实）的 p-value。

    Args:
        ic_series: IC 时间序列（非 IC 均值/标准差）
        max_lag: 最大滞后阶数，默认 int(n^(1/3))（Newey-West 经典选择）
    """
    ic = ic_series.dropna()
    n = len(ic)
    if n < 20:
        return 1.0
    mean = float(ic.mean())
    demeaned = ic.values - mean
    if max_lag is None:
        max_lag = max(1, int(n ** (1.0 / 3.0)))
    # Newey-West variance with Bartlett kernel
    gamma_0 = float(np.mean(demeaned ** 2))
    nw_var = gamma_0
    for lag in range(1, max_lag + 1):
        weight = 1.0 - lag / (max_lag + 1)  # Bartlett kernel（保证正定）
        gamma_lag = float(np.mean(demeaned[:-lag] * demeaned[lag:]))
        nw_var += 2.0 * weight * gamma_lag
    se = np.sqrt(max(nw_var, 0.0) / n)
    if se < 1e-12:
        return 1.0
    t_stat = mean / se
    # 正态分布 CDF 近似
    import math
    x = abs(t_stat)
    t = 1.0 / (1.0 + 0.2316419 * x)
    phi = math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)
    p = phi * (0.319381530 * t - 0.356563782 * t**2 + 1.781477937 * t**3
               - 1.821255978 * t**4 + 1.330274429 * t**5)
    if t_stat < 0:
        p = 1.0 - p
    return float(2.0 * min(p, 1.0 - p))


def apply_fdr_correction(scorecard: pd.DataFrame, alpha: float = 0.05) -> pd.DataFrame:
    """对因子体检表应用 FDR 校正，新增 p_value 和 q_value（校正后）列。
    
    只对有有效 IC 的因子计算 p-value，失败因子的 p-value 设为 1.0。
    alpha 控制 FDR 水平（默认 5%）。"""
    pvals = []
    for _, row in scorecard.iterrows():
        ic_mean = row.get("IC均值", np.nan)
        ic_std = row.get("ICIR", np.nan)
        n_days = row.get("天数", 0)
        if pd.notna(ic_mean) and pd.notna(ic_std) and n_days > 0:
            # ICIR = IC_mean / IC_std → IC_std = IC_mean / ICIR
            ic_std_val = abs(ic_mean / ic_std) if abs(ic_std) > 1e-12 else 1.0
            pvals.append(ic_pvalue(ic_mean, ic_std_val, n_days))
        else:
            pvals.append(1.0)
    
    scorecard = scorecard.copy()
    scorecard["p_value"] = pvals
    scorecard["q_value"] = bh_fdr(scorecard["p_value"], alpha)
    scorecard["FDR显著"] = scorecard["q_value"] < alpha
    return scorecard


def backtest_credibility_score(wf_result: pd.DataFrame) -> dict:
    """回测可信度评分：综合评估 walk_forward 结果的可信度。
    
    评分维度（0-100 分）：
    1. IS/OOS 差距（25分）：差距越小越可信
    2. 样本量（15分）：调仓期越多越可信
    3. 夏普比率（20分）：越高越可信
    4. 最大回撤（15分）：回撤越小越可信
    5. 盈亏比（15分）：越高越可信
    6. 月度胜率（10分）：越高越可信
    
    返回 {score, grade, details, warnings}"""
    if wf_result is None or (isinstance(wf_result, pd.DataFrame) and wf_result.empty):
        return {"score": 0, "grade": "F", "details": {}, "warnings": ["无回测数据"]}

    attrs = wf_result.attrs if hasattr(wf_result, "attrs") else {}
    details = {}
    warnings = []
    score = 0

    # 1. IS/OOS 差距（25分）
    ann_ret = attrs.get("ann_return", 0)
    sharpe = attrs.get("sharpe", 0)
    max_dd = attrs.get("max_drawdown", 0)
    profit_factor = attrs.get("profit_factor", 0)
    win_rate = attrs.get("win_rate", 0)
    monthly_wr = attrs.get("monthly_winrate", 0)
    n_periods = attrs.get("n_periods", 0)
    max_consec = attrs.get("max_consec_loss_months", 0)

    # IS/OOS 差距：如果能获取 IS 结果，计算差距
    # 这里简化为用 OOS 胜率作为代理
    if win_rate > 0:
        # 胜率越高，IS/OOS差距可能越小
        is_oos_score = min(25, win_rate * 40)
        details["IS/OOS差距"] = round(is_oos_score, 1)
        score += is_oos_score
    else:
        warnings.append("胜率数据缺失")

    # 2. 样本量（15分）
    if n_periods >= 50:
        sample_score = 15
    elif n_periods >= 20:
        sample_score = 10
    elif n_periods >= 10:
        sample_score = 5
    else:
        sample_score = 0
        warnings.append(f"样本量不足: {n_periods}期")
    details["样本量"] = sample_score
    score += sample_score

    # 3. 夏普比率（20分）
    if sharpe >= 2.0:
        sharpe_score = 20
    elif sharpe >= 1.0:
        sharpe_score = 15
    elif sharpe >= 0.5:
        sharpe_score = 10
    elif sharpe > 0:
        sharpe_score = 5
    else:
        sharpe_score = 0
        warnings.append(f"夏普为负: {sharpe:.2f}")
    details["夏普比率"] = sharpe_score
    score += sharpe_score

    # 4. 最大回撤（15分）
    if max_dd > -0.05:
        dd_score = 15
    elif max_dd > -0.10:
        dd_score = 12
    elif max_dd > -0.20:
        dd_score = 8
    elif max_dd > -0.30:
        dd_score = 4
    else:
        dd_score = 0
        warnings.append(f"最大回撤过大: {max_dd:.1%}")
    details["最大回撤"] = dd_score
    score += dd_score

    # 5. 盈亏比（15分）
    if profit_factor >= 2.0:
        pf_score = 15
    elif profit_factor >= 1.5:
        pf_score = 12
    elif profit_factor >= 1.0:
        pf_score = 8
    elif profit_factor > 0:
        pf_score = 4
    else:
        pf_score = 0
        warnings.append(f"盈亏比不足: {profit_factor:.2f}")
    details["盈亏比"] = pf_score
    score += pf_score

    # 6. 月度胜率（10分）
    if monthly_wr >= 0.60:
        mw_score = 10
    elif monthly_wr >= 0.50:
        mw_score = 7
    elif monthly_wr >= 0.40:
        mw_score = 4
    else:
        mw_score = 0
        warnings.append(f"月度胜率偏低: {monthly_wr:.1%}")
    details["月度胜率"] = mw_score
    score += mw_score

    # 评级
    if score >= 80:
        grade = "A"
    elif score >= 65:
        grade = "B"
    elif score >= 50:
        grade = "C"
    elif score >= 35:
        grade = "D"
    else:
        grade = "F"

    return {"score": round(score), "grade": grade, "details": details, "warnings": warnings}


# ---------------------------------------------------------------- 基础件
def resolve_factor(name: str, kind: str | None = None,
                   evo_map: dict | None = None, le_map: dict | None = None) -> dict | None:
    """因子名 → get_factor_values 可用的 fac dict（自动补进化/LoopEngine 因子代码）。
    evo_map/le_map 可传入预建的 {name: code} 避免逐因子查库；解析不到代码返回 None。"""
    import datasource  # noqa: F401  保持与 get_factor_values 相同的延迟导入约定
    if name in sig.BUILTIN_FACTORS:
        return {"name": name, "kind": "builtin", "code": None}
    if name in sig.CATALOG_NAMES or name in sig.TECH_INDICATORS:
        return {"name": name, "kind": "tech", "code": None}
    code = None
    if evo_map is not None or le_map is not None:
        code = (evo_map or {}).get(name) or (le_map or {}).get(name)
    else:
        from common import get_evolved_factors
        import library
        for f in get_evolved_factors(only_accepted=False):
            if f["name"] == name:
                code = f["code"]
                break
        if not code:
            try:
                reg = library.get_factor_registry()
                r = reg[reg["name"] == name]
                if not r.empty:
                    code = r.iloc[0]["code"]
            except Exception:
                pass
    return {"name": name, "kind": kind or "evolved", "code": code} if code else None


def _norm(s: pd.Series) -> pd.Series:
    """统一成长表索引 (datetime, instrument) 并排序（容忍历史遗留的 date 层名）。"""
    if "date" in (s.index.names or []):
        s = s.copy()
        s.index = s.index.set_names(["datetime" if n == "date" else n for n in s.index.names])
    if list(s.index.names) != ["datetime", "instrument"]:
        s = s.reorder_levels(["datetime", "instrument"])
    return s.sort_index()


def _cache(name: str, payload: str) -> Path:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    return EVAL_DIR / f"{name}_{hashlib.md5(payload.encode()).hexdigest()[:16]}.parquet"


def forward_returns(panel: pd.DataFrame, days: int) -> pd.DataFrame:
    """datetime × instrument 的远期收益表。"""
    close = panel["$close"].unstack("instrument")
    return close.shift(-days) / close - 1


def _eval_source() -> str:
    """评估数据源：跟随演化闭环源（loop_factor_source，当前=同花顺 iFinD）。

    选拔（engine._frames）、体检（本模块）、打包（_try_generate_pack）、
    实盘交易（ifind_realtime）必须同一数据口径——此前此处硬编码 qlib_local，
    同一个因子"选拔一套数据、打分另一套数据、实盘再一套"（2026-09-11 排查）。"""
    import datasource
    try:
        return datasource.get_loop_source()
    except Exception:
        return "qlib_local"


def get_factor_values(fac: dict, codes: list[str], end: str, lookback_days: int = 800,
                       source: str | None = None) -> pd.Series:
    """统一取因子长表 Series[(datetime, instrument)]。

    fac: {"name":..., "kind": "builtin"|"evolved"|"loopengine", "code": 进化因子代码}
    """
    import datasource

    source = source or _eval_source()
    ck = _cache("fvals", f"{source}|{fac['name']}|{fac['kind']}|{'|'.join(sorted(codes))}|{end}|{lookback_days}")
    if ck.exists():
        hit = sig._read_parquet_safe(ck)
        if hit is not None:
            return hit.iloc[:, 0]
    # Density-SR 支撑阻力因子：值在 sr_scan_daily（逐日快照），不走 panel 计算
    if fac["name"] in ("sr_entry", "sr_hold", "sr_strength"):
        import density_sr
        s = density_sr.factor_series(fac["name"], codes, end, lookback_days)
    elif fac["kind"] == "builtin":
        panel = sig.get_panel_cached(codes, end, lookback_days, source=source)
        s = sig.compute_builtin(panel, fac["name"])
    elif fac["kind"] == "tech":
        panel = sig.get_panel_cached(codes, end, lookback_days, source=source)
        if fac["name"] in sig.CATALOG_NAMES:
            s = sig.compute_common(panel, fac["name"])
        else:
            s = sig.compute_tech(panel, fac["name"])
    elif fac["kind"] in ("loopengine", "manual"):
        # 树直算快速路径（loopengine 演化因子 + manual 手工表达式因子共用）：
        # 避免子进程执行（~5秒/个 → ~0.02秒/个）；无 sexpr 前缀的手工 Python 因子走子进程
        code = fac.get("code") or ""
        if code.startswith("# sexpr: "):
            try:
                from loopengine.tree import evaluate_tree, parse
                from loopengine.extra_frames import frames_with_extras_for
                panel = sig.get_panel_cached(codes, end, lookback_days, source=source)
                sexpr = code.split("\n", 1)[0][len("# sexpr: "):]
                # 非量价字段（资金流/财务等）按 sexpr 引用自动附加额外帧
                frames = frames_with_extras_for(sexpr, panel, codes, end, lookback_days)
                tree = parse(sexpr, "任意")  # 评估端不限类型字段（生成端才约束类型）
                vals = evaluate_tree(tree, frames).stack().rename(fac["name"]).dropna()
                vals.index = vals.index.set_names(["datetime", "instrument"])
                s = vals
            except Exception:
                # 回退到子进程执行
                df = sig.run_factor_code(code, fac["name"], codes, end, lookback_days, source=source)
                s = df.iloc[:, 0]
        else:
            df = sig.run_factor_code(code, fac["name"], codes, end, lookback_days, source=source)
            s = df.iloc[:, 0]
    else:
        df = sig.run_factor_code(fac["code"], fac["name"], codes, end, lookback_days, source=source)
        s = df.iloc[:, 0]
    s = _norm(s.dropna())
    # 仅保存通过静态审查且实际算出的因子值，供后续相关性聚类使用。
    try:
        import library
        library.store_factor_values(fac["name"], s, source=source)
    except Exception:
        pass
    sig._write_parquet_atomic(s.to_frame(fac["name"]), ck)
    return s


# ---------------------------------------------------------------- IC 序列与体检表
def ic_series(vals: pd.Series, fwd: pd.DataFrame, min_n: int = 30) -> pd.Series:
    """逐日 RankIC（spearman）序列。vals 长表，fwd 为 datetime×instrument。"""
    v = _norm(vals).rename("f").to_frame()
    r = fwd.stack().rename("r")
    j = v.join(r, how="inner").dropna()
    if j.empty:
        return pd.Series(dtype=float)

    def _ic(g):
        return g["f"].corr(g["r"], method="spearman") if len(g) >= min_n else np.nan

    return j.groupby(level="datetime").apply(_ic).dropna()


def get_ic_series(fac: dict, codes: list[str], end: str, fwd_days: int = MAIN_FWD,
                  lookback_days: int = 800, source: str | None = None) -> pd.Series:
    import datasource

    source = source or _eval_source()
    ck = _cache("ic", f"{source}|{fac['name']}|{fac['kind']}|{'|'.join(sorted(codes))}|{end}|{fwd_days}|{lookback_days}")
    if ck.exists():
        hit = sig._read_parquet_safe(ck)
        if hit is not None:
            return hit.iloc[:, 0]
    vals = get_factor_values(fac, codes, end, lookback_days, source=source)
    panel = sig.get_panel_cached(codes, end, lookback_days, source=source)
    ic = ic_series(vals, forward_returns(panel, fwd_days))
    sig._write_parquet_atomic(ic.to_frame("ic"), ck)
    return ic


def decay_curve(fac: dict, codes: list[str], end: str, source: str | None = None) -> dict:
    """各 forward 窗口的平均 RankIC，用于看因子持仓周期属性。"""
    vals = get_factor_values(fac, codes, end, source=source)
    panel = sig.get_panel_cached(codes, end, 800, source=source)
    return {d: ic_series(vals, forward_returns(panel, d)).mean() for d in FWD_DAYS}


def top_group_winrate(vals: pd.Series, panel: pd.DataFrame, fwd_days: int = MAIN_FWD,
                      step: int = STEP_DAYS, pct: float = 0.1, cost: float = DEFAULT_COST,
                      fwd: pd.DataFrame | None = None) -> float:
    """每 step 个交易日取 Top 十分位组合，forward 超额>0 的占比。
    统一口径：基准=等权均值（非中位数），扣除双边交易成本。
    fwd 可传入预算好的远期收益表（批量多周期评估时避免重复计算）。"""
    if fwd is None:
        fwd = forward_returns(panel, fwd_days)
    v = _norm(vals.dropna())
    days = v.index.get_level_values("datetime").unique()[::step]
    cost_per_period = cost  # 持有期内总成本（双边）
    wins = []
    for t in days:
        if t not in fwd.index:
            continue
        cross = v[v.index.get_level_values("datetime") == t].droplevel("datetime")
        fr = fwd.loc[t].dropna()
        cross = cross[cross.index.isin(fr.index)]
        if len(cross) < 30:
            continue
        top = cross.nlargest(max(1, int(len(cross) * pct)))
        # 基准改为等权均值，扣除成本
        excess = fr[top.index].mean() - fr.mean() - cost_per_period
        wins.append(excess > 0)
    return float(np.mean(wins)) if wins else float("nan")


def build_scorecard(factors: list[dict], codes: list[str], end: str,
                    source: str | None = None, train_end: str | None = None) -> pd.DataFrame:
    """因子体检表：每个因子一行，含 1/5/20/60/120 日五档 Top 组胜率（统一多周期标准）。

    train_end（防未来函数预选）：给定则把面板/因子值/IC 全部物理截断到该日——
    截断后长周期远期收益为 NaN 自然跳过，统计零泄漏；"这批因子好不好"的结论
    只来自 train_end 之前，其后区间留给 walk-forward 做真样本外。"""
    rows = []
    panel, fwds = None, {}
    for fac in factors:
        try:
            ic = get_ic_series(fac, codes, end, source=source)
            if ic.empty:
                raise RuntimeError("IC 序列为空")
            vals = get_factor_values(fac, codes, end, source=source)
            if panel is None:
                panel = sig.get_panel_cached(codes, end, 800, source=source)
                if train_end:
                    panel = panel[panel.index.get_level_values("datetime") <= train_end]
                fwds = {d: forward_returns(panel, d) for d in WIN_HORIZONS.values()}
            if train_end:
                ic = ic[ic.index <= train_end]
                vals = vals[vals.index.get_level_values("datetime") <= train_end]
                if ic.empty or vals.empty:
                    raise RuntimeError("预选窗内无数据")
            kind_label = {"evolved": "进化", "builtin": "内置", "tech": "技术指标",
                            "loopengine": "演化引擎"}.get(fac["kind"], fac["kind"])
            row = {
                "因子": fac["name"], "来源": kind_label,
                "IC均值": ic.mean(), "ICIR": ic.mean() / (ic.std() + 1e-12),
                "IC胜率": (ic > 0).mean(),
                "Top组胜率": top_group_winrate(vals, panel, fwd=fwds[MAIN_FWD]),
                "建议方向": "正向" if ic.mean() >= 0 else "负向",
                "天数": len(ic),
            }
            for label, d in WIN_HORIZONS.items():
                # 短周期加密采样（1日/5日 step=5），长周期按默认步长
                row[f"{label}胜率"] = top_group_winrate(
                    vals, panel, fwd_days=d, step=(5 if d <= 5 else STEP_DAYS), fwd=fwds[d])
            rows.append(row)
        except Exception as e:
            rows.append({"因子": fac["name"], "来源": fac["kind"], "IC均值": np.nan,
                         "ICIR": np.nan, "IC胜率": np.nan, "Top组_winrate": np.nan,
                         "建议方向": f"评估失败: {str(e)[:40]}", "天数": 0})
    return pd.DataFrame(rows)


def build_scorecard_batch(factors: list[dict], codes: list[str], end: str,
                          source: str | None = None, train_end: str | None = None) -> pd.DataFrame:
    """批量因子体检表：一次构建面板，批量计算所有因子的IC和胜率，避免重复IO。

    P2优化：面板只构建1次，帧缓存复用，IC批量计算。
    P4优化：跳过已有缓存的因子值。"""
    import re
    import datasource
    from loopengine.tree import TYPE_FIELDS, build_field_frames, evaluate_tree, parse
    from loopengine.extra_frames import build_extra_frames

    source = source or _eval_source()
    rows = []

    # 1. 一次性构建面板和帧（最大开销；全窗口，IS/OOS 切片在评估循环内做）
    panel = sig.get_panel_cached(codes, end, 800, source=source)
    fwds = {d: forward_returns(panel, d) for d in WIN_HORIZONS.values()}
    # 非量价字段（资金流/财务等）按批内 sexpr 引用的并集一次附加
    need_types = set()
    for fac in factors:
        code = fac.get("code") or ""
        if code.startswith("# sexpr: "):
            leaves = set(re.findall(r"[a-z_]+", code.split("\n", 1)[0]))
            need_types |= {t for t, fs in TYPE_FIELDS.items() if any(f in leaves for f in fs)}
    extra = {}
    for t in need_types:
        try:
            extra.update(build_extra_frames(t, codes, end, 800) or {})
        except Exception:
            continue
    frames = build_field_frames(panel, extra or None)

    # 2. 批量计算所有因子的值（树直算快速路径）
    factor_values = {}
    ck_prefix = "|".join(sorted(codes))
    for fac in factors:
        try:
            code = fac.get("code") or ""
            if not code.startswith("# sexpr: "):
                # 非树因子回退到单因子计算
                factor_values[fac["name"]] = get_factor_values(fac, codes, end, source=source)
                continue

            # P4: 检查缓存是否已存在（"800f"=全窗口值）
            ck = _cache("fvals", f"{source}|{fac['name']}|{fac['kind']}|{ck_prefix}|{end}|800f")
            if ck.exists():
                hit = sig._read_parquet_safe(ck)
                if hit is not None:
                    factor_values[fac["name"]] = hit.iloc[:, 0]
                    continue

            # 树直算批量计算
            sexpr = code.split("\n", 1)[0][len("# sexpr: "):]
            tree = parse(sexpr, "任意")  # 评估端不限类型字段（生成端才约束类型）
            vals = evaluate_tree(tree, frames).stack().rename(fac["name"]).dropna()
            vals.index = vals.index.set_names(["datetime", "instrument"])
            vals = _norm(vals)
            sig._write_parquet_atomic(vals.to_frame(fac["name"]), ck)
            factor_values[fac["name"]] = vals
        except Exception:
            # 回退到单因子计算
            try:
                factor_values[fac["name"]] = get_factor_values(fac, codes, end, source=source)
            except Exception:
                continue

    # 3. 批量计算IC和胜率
    for fac in factors:
        fname = fac["name"]
        if fname not in factor_values:
            rows.append({"因子": fname, "来源": fac["kind"], "IC均值": np.nan,
                         "ICIR": np.nan, "IC胜率": np.nan, "Top组_winrate": np.nan,
                         "建议方向": "计算失败", "天数": 0})
            continue

        try:
            vals = factor_values[fname]
            if vals.empty:
                raise RuntimeError("因子值为空")

            # 计算IC序列（全窗口；OOS 统计用 train_end 之后的段）
            ic = ic_series(vals, fwds[MAIN_FWD])
            if ic.empty:
                raise RuntimeError("IC 序列为空")
            oos = _oos_stats(ic, fac.get("first_seen"), train_end,
                             engine_selected=fac.get("kind") not in ("builtin", "tech"))

            if train_end:
                ic = ic[ic.index <= train_end]
                vals = vals[vals.index.get_level_values("datetime") <= train_end]
                if ic.empty or vals.empty:
                    raise RuntimeError("预选窗内无数据")

            kind_label = {"evolved": "进化", "builtin": "内置", "tech": "技术指标",
                          "loopengine": "演化引擎"}.get(fac["kind"], fac["kind"])
            row = {
                "因子": fname, "来源": kind_label,
                "IC均值": ic.mean(), "ICIR": ic.mean() / (ic.std() + 1e-12),
                "IC胜率": (ic > 0).mean(),
                "Top组胜率": top_group_winrate(vals, panel, fwd=fwds[MAIN_FWD]),
                "建议方向": "正向" if ic.mean() >= 0 else "负向",
                "天数": len(ic),
                **oos,
            }
            for label, d in WIN_HORIZONS.items():
                row[f"{label}胜率"] = top_group_winrate(
                    vals, panel, fwd_days=d, step=(5 if d <= 5 else STEP_DAYS), fwd=fwds[d])
            rows.append(row)
        except Exception as e:
            rows.append({"因子": fname, "来源": fac["kind"], "IC均值": np.nan,
                         "ICIR": np.nan, "IC胜率": np.nan, "Top组_winrate": np.nan,
                         "建议方向": f"评估失败: {str(e)[:40]}", "天数": 0})

    df = pd.DataFrame(rows)
    # 批量评估后应用 FDR 校正：解决多重比较问题
    if len(df) > 1:
        df = apply_fdr_correction(df)
    return df


def _oos_stats(ic_full: pd.Series, first_seen: str | None, train_end: str | None,
               engine_selected: bool = True, factor_type: str | None = None) -> dict:
    """OOS 指标：IC 序列在 OOS 窗口内的切片统计（含小样本Bayesian shrinkage）。

    窗口起点：引擎选拔过的因子（loopengine/rdagent）取 max(train_end, 首次入库日)——
    被发现之后的数据才未被选择过程污染；builtin/tech 是外生标准因子，无选择偏差，
    直接用 train_end 起算（约近 250 交易日，2026-09-11 实测否则只剩 ~10 天太噪）。
    
    小样本处理：当 OOS 天数 < 30 时，使用 Bayesian shrinkage 向 0 收缩，
    避免小样本点估计过噪导致的假阳性/假阴性。
    
    非量价类型：历史数据短（~45天），无OOS盲区，评分打折处理。"""
    empty = {"IC_OOS": None, "ICIR_OOS": None, "OOS天数": 0, "OOS_confidence": 0.0}
    if ic_full is None or ic_full.empty or not train_end:
        return empty
    base = str(train_end)[:10]
    oos_start = max(base, str(first_seen or "")[:10]) if engine_selected else base
    try:
        seg = ic_full[ic_full.index > pd.Timestamp(oos_start)]
    except Exception:
        return empty
    n_oos = len(seg)
    if n_oos < 5:
        return {"IC_OOS": None, "ICIR_OOS": None, "OOS天数": int(n_oos), "OOS_confidence": 0.0}
    
    oos_ic = float(seg.mean())
    oos_icir = float(seg.mean() / (seg.std() + 1e-12))
    
    # 非量价类型标记：历史数据短，无OOS盲区
    no_oos_blind = factor_type in ("资金流", "板块轮动", "龙虎榜", "盘口异动", "指数", "爆量抢筹")
    
    # 小样本 Bayesian shrinkage：向 0 收缩（保守估计）
    # 收缩因子 = n / (n + k)，k 为先验强度（默认20，即等效20个先验样本）
    if n_oos < 30:
        prior_strength = 20
        shrinkage_factor = n_oos / (n_oos + prior_strength)  # n=10 → 0.33, n=20 → 0.5, n=30 → 0.6
        oos_ic_shrunk = oos_ic * shrinkage_factor
        # ICIR = mean / std，只 shrink mean，std 保持原始值
        oos_icir_corrected = oos_ic_shrunk / (seg.std() + 1e-12)
        confidence = n_oos / 60  # 60 天满信心
        result = {
            "IC_OOS": round(oos_ic_shrunk, 4),
            "ICIR_OOS": round(oos_icir_corrected, 4),
            "OOS天数": int(n_oos),
            "OOS_confidence": round(confidence, 3),
            "OOS_raw_ic": round(oos_ic, 4),  # 保留原始值供诊断
            "OOS_shrinkage": round(shrinkage_factor, 3)
        }
    else:
        result = {
            "IC_OOS": round(oos_ic, 4),
            "ICIR_OOS": round(oos_icir, 4),
            "OOS天数": int(n_oos),
            "OOS_confidence": 1.0
        }
    
    # 非量价类型标记
    if no_oos_blind:
        result["no_oos_blind"] = True
        result["OOS_confidence"] = result["OOS_confidence"] * 0.8  # 评分打折20%
    
    return result


def _eval_single_factor(args):
    """单因子评估函数（用于并行执行）。"""
    fac, vals, panel, fwds, train_end = args
    fname = fac["name"]
    try:
        if vals is None or vals.empty:
            raise RuntimeError("因子值为空")

        # 计算IC序列（全窗口——OOS 统计要用 train_end 之后的段）
        ic = ic_series(vals, fwds[MAIN_FWD])
        if ic.empty:
            raise RuntimeError("IC 序列为空")
        oos = _oos_stats(ic, fac.get("first_seen"), train_end,
                         engine_selected=fac.get("kind") not in ("builtin", "tech"))

        if train_end:
            ic = ic[ic.index <= train_end]
            vals = vals[vals.index.get_level_values("datetime") <= train_end]
            if ic.empty or vals.empty:
                raise RuntimeError("预选窗内无数据")

        kind_label = {"evolved": "进化", "builtin": "内置", "tech": "技术指标",
                      "loopengine": "演化引擎"}.get(fac["kind"], fac["kind"])
        row = {
            "因子": fname, "来源": kind_label,
            "IC均值": ic.mean(), "ICIR": ic.mean() / (ic.std() + 1e-12),
            "IC胜率": (ic > 0).mean(),
            "Top组胜率": top_group_winrate(vals, panel, fwd=fwds[MAIN_FWD]),
            "建议方向": "正向" if ic.mean() >= 0 else "负向",
            "天数": len(ic),
            **oos,
        }
        for label, d in WIN_HORIZONS.items():
            row[f"{label}胜率"] = top_group_winrate(
                vals, panel, fwd_days=d, step=(5 if d <= 5 else STEP_DAYS), fwd=fwds[d])
        return row
    except Exception as e:
        return {"因子": fname, "来源": fac["kind"], "IC均值": np.nan,
                "ICIR": np.nan, "IC胜率": np.nan, "Top组_winrate": np.nan,
                "建议方向": f"评估失败: {str(e)[:40]}", "天数": 0}


def build_scorecard_parallel(factors: list[dict], codes: list[str], end: str,
                             source: str | None = None, train_end: str | None = None,
                             max_workers: int = 4) -> pd.DataFrame:
    """并行因子体检表：多进程计算IC和胜率，加速批量评估。

    P2+P3优化：面板+帧只构建1次，因子值批量计算，并行评估IC和胜率。"""
    import re
    import datasource
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from loopengine.tree import TYPE_FIELDS, build_field_frames, evaluate_tree, parse
    from loopengine.extra_frames import build_extra_frames

    source = source or _eval_source()

    # 1. 一次性构建面板和帧（全窗口：IS 统计在 worker 内按 train_end 切片，
    #    OOS 统计需要 train_end 之后的段——面板不能预截断）
    panel = sig.get_panel_cached(codes, end, 800, source=source)
    fwds = {d: forward_returns(panel, d) for d in WIN_HORIZONS.values()}
    # 非量价字段按批内 sexpr 引用并集附加（资金流/财务等）
    need_types = set()
    for fac in factors:
        code = fac.get("code") or ""
        if code.startswith("# sexpr: "):
            leaves = set(re.findall(r"[a-z_]+", code.split("\n", 1)[0]))
            need_types |= {t for t, fs in TYPE_FIELDS.items() if any(f in leaves for f in fs)}
    extra = {}
    for t in need_types:
        try:
            extra.update(build_extra_frames(t, codes, end, 800) or {})
        except Exception:
            continue
    frames = build_field_frames(panel, extra or None)

    # 2. 批量计算所有因子的值
    factor_values = {}
    ck_prefix = "|".join(sorted(codes))
    for fac in factors:
        try:
            code = fac.get("code") or ""
            if not code.startswith("# sexpr: "):
                factor_values[fac["name"]] = get_factor_values(fac, codes, end, source=source)
                continue

            # P4: 检查缓存（"800f"=全窗口值；旧的截断窗口缓存在口径变更后作废）
            ck = _cache("fvals", f"{source}|{fac['name']}|{fac['kind']}|{ck_prefix}|{end}|800f")
            if ck.exists():
                hit = sig._read_parquet_safe(ck)
                if hit is not None:
                    factor_values[fac["name"]] = hit.iloc[:, 0]
                    continue

            # 树直算
            sexpr = code.split("\n", 1)[0][len("# sexpr: "):]
            tree = parse(sexpr, "任意")  # 评估端不限类型字段（生成端才约束类型）
            vals = evaluate_tree(tree, frames).stack().rename(fac["name"]).dropna()
            vals.index = vals.index.set_names(["datetime", "instrument"])
            vals = _norm(vals)
            sig._write_parquet_atomic(vals.to_frame(fac["name"]), ck)
            factor_values[fac["name"]] = vals
        except Exception:
            try:
                factor_values[fac["name"]] = get_factor_values(fac, codes, end, source=source)
            except Exception:
                continue

    # 3. 并行计算IC和胜率
    tasks = []
    for fac in factors:
        fname = fac["name"]
        if fname in factor_values:
            tasks.append((fac, factor_values[fname], panel, fwds, train_end))

    rows = []
    if max_workers > 1 and len(tasks) > 10:
        # 并行执行
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_eval_single_factor, task): task for task in tasks}
            for future in as_completed(futures):
                rows.append(future.result())
    else:
        # 串行执行（任务少时无需并行）
        for task in tasks:
            rows.append(_eval_single_factor(task))

    # 补充计算失败的因子
    evaluated_names = {r["因子"] for r in rows}
    for fac in factors:
        if fac["name"] not in evaluated_names:
            rows.append({"因子": fac["name"], "来源": fac["kind"], "IC均值": np.nan,
                         "ICIR": np.nan, "IC胜率": np.nan, "Top组_winrate": np.nan,
                         "建议方向": "计算失败", "天数": 0})

    df = pd.DataFrame(rows)
    # FDR 校正（与串行路径一致——此前并行路径漏挂，>50 因子的批量全都没校正）
    if len(df) > 1:
        df = apply_fdr_correction(df)
    return df


# ---------------------------------------------------------------- 相关性与去冗余
def ic_corr_matrix(factors: list[dict], codes: list[str], end: str,
                   source: str | None = None) -> pd.DataFrame:
    series = {}
    for fac in factors:
        try:
            ic = get_ic_series(fac, codes, end, source=source)
            if not ic.empty:
                series[fac["name"]] = ic
        except Exception:
            continue
    return pd.DataFrame(series).corr()


def dedup_factors(corr: pd.DataFrame, scorecard: pd.DataFrame, threshold: float = CORR_THRESHOLD,
                  family_map: dict | None = None, same_family_threshold: float = 0.6):
    """按 |ICIR| 降序贪心去冗余。返回 (保留名单, 剔除原因 dict)。
    family_map 给定（{因子: 机制族}）时同族因子用更严的阈值——
    同族因子天然共享机制，0.7 的宽松线会放走大量近亲（实测跳空族两两 0.9+）。"""
    strength = scorecard.set_index("因子")["ICIR"].abs()
    order = [n for n in strength.sort_values(ascending=False).index if n in corr.columns]
    kept, dropped = [], {}
    for n in order:
        conflict = None
        for k in kept:
            thr = (same_family_threshold
                   if family_map and family_map.get(n, "") == family_map.get(k, "") else threshold)
            if abs(corr.loc[n, k]) > thr:
                conflict = (k, thr)
                break
        if conflict:
            dropped[n] = f"与 {conflict[0]} 相关 {corr.loc[n, conflict[0]]:.2f} > {conflict[1]}"
        else:
            kept.append(n)
    return kept, dropped


# ---------------------------------------------------------------- 加权
# ---------------------------------------------------------------- 因子方向状态机（M5：磁滞防抖）
_DIR_SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_direction(
    name TEXT PRIMARY KEY, direction INTEGER NOT NULL,
    last_flip TEXT, cooldown_until TEXT, updated_at TEXT);
"""
DIR_FLIP_ICIR = 0.08   # |ICIR| 超此值才允许反转（防噪声翻转）
DIR_COOLDOWN_DAYS = 10  # 反转后冷却交易日数


def direction_map() -> dict:
    """读因子方向状态机：{name: ±1}。无记录返回空 dict（调用方回退到 IC 符号）。"""
    import sqlite3
    from common import DATA_DIR
    try:
        with sqlite3.connect(str(DATA_DIR / "market.db"), timeout=30) as c:
            c.executescript(_DIR_SCHEMA)
            return {r[0]: r[1] for r in c.execute("SELECT name, direction FROM factor_direction")}
    except Exception:
        return {}


def update_direction_states(scorecard: pd.DataFrame) -> dict:
    """每日体检后更新方向状态机（磁滞：符号翻转 + |ICIR|>阈值 + 冷却期 三重闸门）。

    返回 {name: (old, new)} 本次发生反转的因子。"""
    import sqlite3
    from datetime import datetime
    from common import DATA_DIR, trade_day_offset

    if scorecard.empty or "IC均值" not in scorecard.columns:
        return {}
    today = datetime.now().strftime("%Y-%m-%d")
    sc = scorecard.set_index("因子") if "因子" in scorecard.columns else scorecard
    cur = direction_map()
    with sqlite3.connect(str(DATA_DIR / "market.db"), timeout=30) as c:
        c.executescript(_DIR_SCHEMA)
        meta = {r[0]: (r[1], r[2]) for r in c.execute("SELECT name, last_flip, cooldown_until FROM factor_direction")}
        flipped = {}
        for name in sc.index:
            ic = sc.loc[name, "IC均值"]
            icir = sc.loc[name, "ICIR"] if "ICIR" in sc.columns else 0.0
            if not (np.isfinite(ic) and np.isfinite(icir)):
                continue
            sign = 1 if ic >= 0 else -1
            old_dir = cur.get(name)
            if old_dir is None:
                c.execute("INSERT OR REPLACE INTO factor_direction VALUES (?,?,?,?,?)",
                          (name, sign, None, None, datetime.now().strftime("%F %T")))
                continue
            if sign != old_dir and abs(icir) > DIR_FLIP_ICIR:
                _, cool_until = meta.get(name, (None, None))
                if cool_until and today <= cool_until:
                    continue  # 冷却期内不反转
                cool_until = trade_day_offset(today, DIR_COOLDOWN_DAYS)
                c.execute("INSERT OR REPLACE INTO factor_direction VALUES (?,?,?,?,?)",
                          (name, sign, today, cool_until, datetime.now().strftime("%F %T")))
                flipped[name] = (old_dir, sign)
    return flipped


# ---------------------------------------------------------------- 族级多重检验（M5）
def apply_family_fdr(scorecard: pd.DataFrame, family_map: dict | None = None) -> pd.DataFrame:
    """族级 FDR：族内变体高度相关时，逐因子 BH 会放水。按机制族取 |IC| 最大代表，
    族内成员继承代表因子的 q 值（保守）。新增 family/family_q 列，不改原 q_value。"""
    if scorecard.empty or "q_value" not in scorecard.columns:
        return scorecard
    if "因子" not in scorecard.columns or not family_map:
        return scorecard
    sc = scorecard.copy()
    fam_best = {}
    for _, r in sc.iterrows():
        fam = family_map.get(r["因子"], "其他")
        ic = abs(r.get("IC均值", 0) or 0)
        if fam not in fam_best or ic > fam_best[fam][0]:
            fam_best[fam] = (ic, r.get("q_value", 1.0))
    sc["family"] = sc["因子"].map(lambda n: family_map.get(n, "其他"))
    sc["family_q"] = sc["family"].map(lambda f: fam_best.get(f, (0, 1.0))[1])
    return sc


def compute_weights(scorecard: pd.DataFrame, method: str, names: list[str],
                    win_col: str = "Top组胜率") -> dict:
    """返回 {因子名: (权重, 方向±1)}。方向自动修正：IC 均值为负 → 负向。
    win_col 指定胜率来源列（多周期标准下用所选持有期的胜率，如 "1日胜率"）。

    M5：方向优先读状态机（磁滞防抖），无记录才回退当日 IC 符号。"""
    sc = scorecard.set_index("因子")
    if win_col not in sc.columns:
        win_col = "Top组胜率"
    dmap = direction_map()
    direction = {n: dmap.get(n) or (1 if sc.loc[n, "IC均值"] >= 0 else -1) for n in names}
    raw = {}
    for n in names:
        icir = abs(sc.loc[n, "ICIR"]) if np.isfinite(sc.loc[n, "ICIR"]) else 0
        win = sc.loc[n, win_col] if np.isfinite(sc.loc[n, win_col]) else 0.5
        if method == "等权":
            raw[n] = 1.0
        elif method == "ICIR加权":
            raw[n] = max(icir, 0.0)
        elif method == "胜率加权":
            raw[n] = max(win - 0.5, 0.0)
        elif method == "均值方差":
            mu = abs(sc.loc[n, "IC均值"]) if np.isfinite(sc.loc[n, "IC均值"]) else 0
            raw[n] = max(mu, 0.0) / max(icir_var_hint(sc, n), 1e-6)
    total = sum(raw.values())
    if total <= 0:  # 全体失效时退化为等权
        raw = {n: 1.0 for n in names}
        total = len(names)
    return {n: (raw[n] / total, direction[n]) for n in names}


def icir_var_hint(sc: pd.DataFrame, n: str) -> float:
    """均值方差法的方差近似：σ² = (μ/ICIR)²。"""
    mu = abs(sc.loc[n, "IC均值"])
    icir = abs(sc.loc[n, "ICIR"])
    return (mu / icir) ** 2 if icir > 1e-9 else 1.0


# ---------------------------------------------------------------- 单因子回测（分层 + 多空对冲）
def factor_group_backtest(vals: pd.Series, panel: pd.DataFrame, n_groups: int = 10,
                          fwd_days: int = MAIN_FWD, step: int = STEP_DAYS) -> dict:
    """单因子分层回测：每 step 天按因子值分 n_groups 组，
    输出各组平均 forward 收益 + 顶组-底组多空净值曲线与绩效。"""
    fwd = forward_returns(panel, fwd_days)
    v = _norm(vals.dropna())
    days = v.index.get_level_values("datetime").unique()[::step]
    group_rets = {i: [] for i in range(n_groups)}
    ls = {}
    for t in days:
        if t not in fwd.index:
            continue
        cross = v[v.index.get_level_values("datetime") == t].droplevel("datetime")
        fr = fwd.loc[t].dropna()
        cross = cross[cross.index.isin(fr.index)]
        if len(cross) < n_groups * 5:
            continue
        ranks = cross.rank(pct=True)
        for i in range(n_groups):
            sel = cross[(ranks > i / n_groups) & (ranks <= (i + 1) / n_groups)]
            if len(sel):
                group_rets[i].append(float(fr[sel.index].median()))  # 中位数抗妖股 outliers
        top = cross[ranks > 1 - 1 / n_groups]
        bot = cross[ranks <= 1 / n_groups]
        if len(top) and len(bot):
            ls[str(t)[:10]] = float(fr[top.index].median() - fr[bot.index].median())

    group_mean = {f"G{i + 1}": (float(np.mean(rs)) if rs else None) for i, rs in group_rets.items()}
    ls_ret = pd.Series(ls).sort_index()
    nav = (1 + ls_ret).cumprod()
    stats = {}
    if len(ls_ret) >= 3:
        ann = nav.iloc[-1] ** (252 / step / len(ls_ret)) - 1
        sharpe = ls_ret.mean() / (ls_ret.std() + 1e-12) * np.sqrt(252 / step)
        mdd = ((nav - nav.cummax()) / nav.cummax()).min()
        stats = {"年化多空收益": f"{ann:.2%}", "夏普": f"{sharpe:.2f}",
                 "最大回撤": f"{mdd:.2%}", "胜率": f"{(ls_ret > 0).mean():.0%}",
                 "调仓点数": str(len(ls_ret))}
    return {"group_mean": group_mean, "ls_ret": ls_ret, "ls_nav": nav, "ls_stats": stats,
            "ic": ic_series(vals, forward_returns(panel, fwd_days))}
# ---------------------------------------------------------------- 截面打分（walk_forward / static_backtest 共用）
def _score_at(vals_norm: dict[str, pd.Series], weights: dict, t,
              norms: dict[str, str] | None = None) -> pd.Series:
    """调仓日 t 的截面综合分（归一化 × 权重 × 方向）。
    norms=None=legacy 原 zscore 口径；传映射=typed_v2 cs_norm 分派（含小截面降级/熔断）。"""
    zl = []
    for n, (w, d) in weights.items():
        if w <= 0:
            continue
        cross = vals_norm[n][vals_norm[n].index.get_level_values("datetime") == t]
        cross.index = cross.index.get_level_values("instrument")
        nv = norms.get(n) if norms else None
        zl.append((sig.cs_norm(cross, nv) if nv else sig.zscore(cross)) * w * d)
    return pd.concat(zl, axis=1).mean(axis=1).dropna() if zl else pd.Series(dtype=float)


# ---------------------------------------------------------------- 滚动样本外
def walk_forward(factor_vals: dict[str, pd.Series], panel: pd.DataFrame, method: str,
                 top_n: int, est: int = EST_WINDOW, step: int = STEP_DAYS,
                 fwd_days: int = MAIN_FWD, cost: float = 0.0025,
                 buffer_n: int = 0, ic_full: dict[str, pd.Series] | None = None,
                 min_factors: int = 2,
                 norms: dict[str, str] | None = None,
                 start_idx: int | None = None,
                 end_idx: int | None = None) -> pd.DataFrame:
    """滚动样本外：每个应用点 t，用 [t-est, t-fwd] 的 IC 统计定权重与方向，
    在 t 截面打分取 Top-N，记录随后 fwd_days 的超额收益。

    同时输出等权组合对照列。cost=双边往返成本（默认 0.25%）——
    换手率 = 与上期名单的替换比例，扣费超额 = 毛超额 - 换手×cost。
    1 日口径下换手极高，扣费后超额才是能装进口袋的部分。
    buffer_n>0 时启用缓冲带：上期持仓只要没跌出 Top(top_n+buffer_n) 就继续持有，
    是降换手的标准做法（实测可把 1 日口径 80%/日的换手压到 ~30%）。
    ic_full 可传入预计算的全历史 IC 序列（贪心搜索批量评估时避免重复计算）。
    min_factors：估计窗内有效因子的最少个数（贪心搜索单因子起步时用 1）。
    norms=None 时按全局开关解析（legacy=原 zscore 口径；typed_v2=cs_norm 自动映射）。
    start_idx/end_idx：可选，限制 walk-forward 仅使用 days[start_idx:end_idx] 区间，
    用于将 OOS 数据切分为验证段和测试段（防过拟合：选择用验证段，评估用测试段）。
    """
    fwd = forward_returns(panel, fwd_days)
    # 全历史 IC 序列（每个因子算一次，应用点只做切片统计 → 快）
    vals_norm = {}
    for name, s in factor_vals.items():
        s2 = _norm(s.dropna())
        if s2.empty:
            continue
        vals_norm[name] = s2
    if norms is None:
        norms = sig.scoring_norms(list(vals_norm))
    if ic_full is None:
        ic_full = {name: ic_series(s, fwd) for name, s in vals_norm.items()}
    days = sorted(set.intersection(*[set(s.index.get_level_values("datetime").unique())
                                     for s in vals_norm.values()])) if vals_norm else []
    # 切割 OOS 区间（start_idx/end_idx 用于验证/测试段分离）
    oos_start = start_idx if start_idx is not None else 0
    oos_end = end_idx if end_idx is not None else len(days)
    oos_days = days[oos_start:oos_end]
    if len(oos_days) < est + fwd_days + step:
        return pd.DataFrame()

    prev_picks: dict[str, set] = {"优化组合": set(), "等权组合": set()}
    rows = []
    for t_idx in range(est, len(oos_days) - fwd_days, step):
        t = oos_days[t_idx]
        # 估计窗右端：t 之前的 fwd_days 天（IC 观测端点回退防未来函数）
        t_global = days.index(t)
        est_lo = days[t_global - est]
        est_hi = days[t_global - fwd_days]  # IC 可观测右端（防未来函数）
        # 切片统计 → 权重
        stats = {}
        for name, ic in ic_full.items():
            if name not in vals_norm:
                continue
            seg = ic[(ic.index >= est_lo) & (ic.index <= est_hi)]
            if len(seg) < 60:
                stats[name] = None
                continue
            stats[name] = (seg.mean(), seg.mean() / (seg.std() + 1e-12), (seg > 0).mean())
        valid = {n: s for n, s in stats.items() if s is not None}
        if len(valid) < min_factors:
            continue
        sc = pd.DataFrame({n: {"IC均值": v[0], "ICIR": v[1], "Top组胜率": v[2]}
                           for n, v in valid.items()}).T
        names = list(valid.keys())
        w_opt = compute_weights(sc.reset_index(names="因子"), method, names)
        w_eq = {n: (1.0 / len(names), w_opt[n][1]) for n in names}

        fr = fwd.loc[t].dropna() if t in fwd.index else pd.Series(dtype=float)
        if fr.empty:
            continue
        # 基准改为等权均值（消除中位数低估超额的偏差）
        row = {"调仓日": str(t)[:10], "池内均值收益": fr.mean()}
        for label, weights in [("优化组合", w_opt), ("等权组合", w_eq)]:
            sc_t = _score_at(vals_norm, weights, t, norms=norms)
            ranked = sc_t[sc_t.index.isin(fr.index)].sort_values(ascending=False)
            prev = prev_picks[label]
            if buffer_n > 0 and prev:
                # 缓冲带：上期持仓未跌出 Top(top_n+buffer_n) 的保留，空位按分补
                eligible = set(ranked.index[:top_n + buffer_n])
                keep = [c for c in prev if c in eligible]
                picks_codes = (keep + [c for c in ranked.index if c not in keep])[:top_n]
            else:
                picks_codes = list(ranked.index[:top_n])
            picks = ranked[ranked.index.isin(picks_codes)]
            if len(picks) >= max(3, top_n // 2):
                cur = set(picks.index)
                turnover = 1.0 if not prev else 1 - len(cur & prev) / len(picks)
                prev_picks[label] = cur
                row[f"{label}收益"] = fr[picks.index].mean()
                row[f"{label}超额"] = row[f"{label}收益"] - fr.mean()
                row[f"{label}换手率"] = turnover
                row[f"{label}扣费超额"] = row[f"{label}超额"] - turnover * cost
        if "优化组合超额" in row:
            rows.append(row)
    df = pd.DataFrame(rows)
    # 计算汇总指标：年化收益/夏普/最大回撤/盈亏比/利润因子/月度胜率
    if not df.empty and "优化组合扣费超额" in df.columns:
        net = df["优化组合扣费超额"]
        nav = (1 + net).cumprod()
        n_periods = len(net)
        # 年化（假设每期 fwd_days 个交易日）
        periods_per_year = 252 / fwd_days
        total_ret = float(nav.iloc[-1] / nav.iloc[0] - 1) if len(nav) > 1 else 0.0
        ann_ret = float((1 + total_ret) ** (periods_per_year / n_periods) - 1) if n_periods > 0 else 0.0
        # 夏普
        sharpe = float(net.mean() / (net.std() + 1e-12) * np.sqrt(periods_per_year)) if n_periods > 5 else 0.0
        # 最大回撤
        max_dd = float(((nav - nav.cummax()) / nav.cummax()).min()) if len(nav) > 1 else 0.0
        # 盈亏比
        wins = net[net > 0]
        losses = net[net < 0]
        profit_factor = float(wins.sum() / (abs(losses.sum()) + 1e-12)) if len(losses) > 0 else float("inf")
        avg_win = float(wins.mean()) if len(wins) > 0 else 0.0
        avg_loss = float(abs(losses.mean())) if len(losses) > 0 else 1e-12
        win_loss_ratio = avg_win / avg_loss
        # 胜率
        win_rate = float((net > 0).mean())
        # 月度胜率（按 fwd_days*22 个周期聚合）
        month_len = max(1, int(22 / fwd_days))
        monthly = net.groupby(net.index // month_len).sum()
        monthly_wr = float((monthly > 0).mean()) if len(monthly) > 0 else 0.0
        # 最大连续亏损月数
        max_consec_loss = 0
        cur_consec = 0
        for m in monthly:
            if m <= 0:
                cur_consec += 1
                max_consec_loss = max(max_consec_loss, cur_consec)
            else:
                cur_consec = 0
        # 汇总到 df 的属性（供外部读取）
        df.attrs["ann_return"] = ann_ret
        df.attrs["sharpe"] = sharpe
        df.attrs["max_drawdown"] = max_dd
        df.attrs["profit_factor"] = profit_factor
        df.attrs["win_loss_ratio"] = win_loss_ratio
        df.attrs["win_rate"] = win_rate
        df.attrs["monthly_winrate"] = monthly_wr
        df.attrs["max_consec_loss_months"] = max_consec_loss
        df.attrs["total_return"] = total_ret
        df.attrs["n_periods"] = n_periods
    return df


# ---------------------------------------------------------------- 组合级多重检验校正
def combo_false_discovery_rate(n_candidates: int, n_rounds: int,
                                selected_winrate: float, n_oos_periods: int) -> float:
    """估计组合选择的假发现率（简化版 White's Reality Check）。

    greedy_combo 每轮评估 ~n_candidates 个因子，共 n_rounds 轮，
    等效于做了大量隐式多次检验。此函数估计"在 H0 下（所有因子真实胜率=50%），
    看到当前最优胜率"的概率。

    Args:
        n_candidates: 候选因子数
        n_rounds: 贪心迭代轮数（≈len(selected)）
        selected_winrate: 最终 OOS 胜率（0-1）
        n_oos_periods: OOS 应用点数
    """
    if n_oos_periods < 5 or selected_winrate <= 0.5:
        return 1.0
    import math
    # 有效检验数：贪心逐轮收敛，有效 < 全排列
    effective_tests = max(1, n_candidates * n_rounds / 2)
    # H0 下：每个因子胜率=50%，max(WR) 的分布近似
    # P(WR >= observed | H0) via binomial tail
    k = int(selected_winrate * n_oos_periods)
    # 用正态近似 binomial tail
    mu = n_oos_periods * 0.5
    sigma = math.sqrt(n_oos_periods * 0.25)
    if sigma < 1e-12:
        return 1.0
    z = (k - mu) / sigma
    # 单侧 p-value（正态近似）
    x = abs(z)
    t = 1.0 / (1.0 + 0.2316419 * x)
    phi = math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)
    p_one = phi * (0.319381530 * t - 0.356563782 * t**2 + 1.781477937 * t**3
                   - 1.821255978 * t**4 + 1.330274429 * t**5)
    p_single = min(1.0, p_one)
    # Bonferroni 上界
    p_combo = min(1.0, p_single * effective_tests)
    return p_combo


# ---------------------------------------------------------------- 时间序列交叉验证
def time_series_cv(factor_vals: dict[str, pd.Series], panel: pd.DataFrame, method: str,
                   top_n: int, n_folds: int = 5, fwd_days: int = MAIN_FWD,
                   step: int = STEP_DAYS, cost: float = 0.0025,
                   buffer_n: int = 0, min_points: int = 8) -> dict:
    """时间序列交叉验证：n_folds 个时间切分，每个切分独立 walk-forward。

    返回各 fold 的 OOS 胜率分布，用中位数（而非均值）作为稳健估计。
    比单次 walk-forward 更稳健：结果不依赖于单一时间切分。
    """
    vals_norm = {n: _norm(s.dropna()) for n, s in factor_vals.items()
                 if not s.dropna().empty}
    if not vals_norm:
        return {"median_winrate": 0, "median_excess": 0, "folds": []}
    fwd = forward_returns(panel, fwd_days)
    days_all = sorted(set.intersection(*[set(s.index.get_level_values("datetime").unique())
                                         for s in vals_norm.values()]))
    ic_full = {n: ic_series(vals_norm[n], fwd) for n in vals_norm}

    fold_size = len(days_all) // (n_folds + 1)
    if fold_size < EST_WINDOW + fwd_days + min_points * step:
        return {"median_winrate": 0, "median_excess": 0, "folds": [],
                "error": "样本不足"}

    results = []
    for fold in range(n_folds):
        # 每个 fold：估计窗从 fold_size*(fold) 开始，OOS 从 fold_size*(fold+1) 开始
        test_start = fold_size * (fold + 1)
        test_end = min(test_start + fold_size, len(days_all))
        est_start = max(0, test_start - EST_WINDOW)

        wf = walk_forward(
            vals_norm, panel, method, top_n,
            est=test_start - est_start,
            step=step, fwd_days=fwd_days, cost=cost,
            buffer_n=buffer_n, ic_full=ic_full, min_factors=1,
            start_idx=test_start, end_idx=test_end,
        )
        if not wf.empty and "优化组合扣费超额" in wf and len(wf) >= min_points:
            net = wf["优化组合扣费超额"]
            results.append({
                "fold": fold,
                "winrate": round(float((net > 0).mean()), 3),
                "mean_excess": round(float(net.mean()), 4),
                "n_periods": len(net),
            })

    if not results:
        return {"median_winrate": 0, "median_excess": 0, "folds": []}

    df = pd.DataFrame(results)
    return {
        "median_winrate": round(float(df["winrate"].median()), 3),
        "median_excess": round(float(df["mean_excess"].median()), 4),
        "std_winrate": round(float(df["winrate"].std()), 3) if len(df) > 1 else 0,
        "worst_fold_winrate": round(float(df["winrate"].min()), 3),
        "best_fold_winrate": round(float(df["winrate"].max()), 3),
        "folds": df.to_dict("records"),
    }


# ---------------------------------------------------------------- 样本内对照（固定权重）
def static_backtest(factor_vals: dict[str, pd.Series], panel: pd.DataFrame,
                    weights: dict, top_n: int, fwd_days: int = MAIN_FWD,
                    step: int = STEP_DAYS, cost: float = 0.0025,
                    upto: str | None = None, collect_picks: bool = False,
                    norms: dict[str, str] | None = None) -> pd.DataFrame:
    """样本内对照回测：用 ② 组合构建算好的**固定权重**（不滚动重估），
    在 upto（默认全历史）之前的调仓点上截面打分取 Top-N。

    输出与 walk_forward 同构，用于 ③ 的 IS/OOS 双轨对比：
    IS 胜率高、OOS 胜率低 = 权重过拟合样本内的直接证据。
    collect_picks=True 时附 "picks" 列（每点名单），供策略组合投票复用。
    norms=None 时按全局开关解析（legacy=原 zscore 口径）。"""
    fwd = forward_returns(panel, fwd_days)
    vals_norm = {n: _norm(s.dropna()) for n, s in factor_vals.items() if not s.dropna().empty}
    if not vals_norm:
        return pd.DataFrame()
    if norms is None:
        norms = sig.scoring_norms(list(vals_norm))
    days = sorted(set.intersection(*[set(s.index.get_level_values("datetime").unique())
                                     for s in vals_norm.values()]))
    if upto:
        days = [d for d in days if str(d)[:10] <= str(upto)[:10]]
    prev: set = set()
    rows = []
    for t in days[::step]:
        if t not in fwd.index:
            continue
        fr = fwd.loc[t].dropna()
        if fr.empty:
            continue
        sc_t = _score_at(vals_norm, weights, t, norms=norms)
        ranked = sc_t[sc_t.index.isin(fr.index)].sort_values(ascending=False)
        picks = ranked.head(top_n)
        if len(picks) < max(3, top_n // 2):
            continue
        cur = set(picks.index)
        turnover = 1.0 if not prev else 1 - len(cur & prev) / len(picks)
        prev = cur
        ret = float(fr[picks.index].mean())
        row = {"调仓日": str(t)[:10], "池内均值收益": float(fr.mean()),
               "组合收益": ret, "组合超额": ret - float(fr.mean()),
               "组合换手率": turnover,
               "组合扣费超额": ret - float(fr.median()) - turnover * cost}
        if collect_picks:
            row["picks"] = list(picks.index)
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 贪心组合推荐（OOS 前向选择）
def greedy_combo(factor_vals: dict[str, pd.Series], panel: pd.DataFrame, method: str,
                 top_n: int, candidates: list[str], fwd_days: int = MAIN_FWD,
                 step: int = STEP_DAYS, cost: float = 0.0025, max_n: int = 8,
                 min_points: int = 8, buffer_n: int = 0,
                 oos_test_ratio: float = 0.20) -> dict:
    """前向贪心选因子：从空集开始，每轮把使 walk-forward **扣费胜率**提升最大
    的因子加入组合（胜率并列时比平均净超额），直到无提升或满 max_n 个。

    IC 全序列只预计算一次并注入 walk_forward，单轮评估亚秒级；
    候选建议先去冗余再截到 ~12 个（调用方负责）。

    防过拟合：OOS 数据切为验证段（前 80%）和测试段（后 20%）。
    贪心选择在验证段上进行，最终 OOS 胜率以测试段为准——
    避免"用验证集选模型"导致的 OOS 胜率虚高。
    oos_test_ratio: 测试段占比（默认 20%），设为 0 则退化为原始行为。
    """
    vals_norm = {n: _norm(factor_vals[n].dropna()) for n in candidates
                 if n in factor_vals and not factor_vals[n].dropna().empty}
    avail = [n for n in candidates if n in vals_norm]
    if not avail:
        return {"selected": [], "history": pd.DataFrame(), "wf": pd.DataFrame(),
                "wf_test": pd.DataFrame(), "oos_winrate_test": None}
    fwd = forward_returns(panel, fwd_days)
    ic_full = {n: ic_series(vals_norm[n], fwd) for n in avail}

    # --- 计算验证段/测试段边界 ---
    days_all = sorted(set.intersection(*[set(s.index.get_level_values("datetime").unique())
                                         for s in vals_norm.values()])) if vals_norm else []
    split_idx = int(len(days_all) * (1 - oos_test_ratio)) if oos_test_ratio > 0 else len(days_all)
    # 验证段：0..split_idx（用于 greedy 选择）
    # 测试段：split_idx..end（用于最终评估）
    n_val = split_idx
    n_test = len(days_all) - split_idx
    # 验证段需要足够的样本：est + fwd_days + step
    min_val = EST_WINDOW + fwd_days + step + min_points * step
    if n_val < min_val or n_test < fwd_days + step + min_points * step:
        # 样本不足，退化为全量 OOS
        split_idx = len(days_all)

    def _eval_segment(names: list[str], si: int | None, ei: int | None):
        wf = walk_forward({n: vals_norm[n] for n in names}, panel, method, top_n,
                          step=step, fwd_days=fwd_days, cost=cost,
                          buffer_n=buffer_n, ic_full=ic_full, min_factors=1,
                          start_idx=si, end_idx=ei)
        if wf.empty or len(wf) < min_points or "优化组合扣费超额" not in wf:
            return None, wf
        net = wf["优化组合扣费超额"]
        return (float((net > 0).mean()), float(net.mean())), wf

    # --- 阶段 1：在验证段上贪心选择 ---
    selected, history = [], []
    best, best_wf = (-1.0, -9e9), pd.DataFrame()
    while avail and len(selected) < max_n:
        round_best, round_name, round_wf = None, None, None
        for n in avail:
            obj, wf = _eval_segment(selected + [n], 0, split_idx if split_idx < len(days_all) else None)
            if obj and (round_best is None or obj > round_best):
                round_best, round_name, round_wf = obj, n, wf
        if round_name is None or (selected and round_best <= best):
            break
        selected.append(round_name)
        avail.remove(round_name)
        best, best_wf = round_best, round_wf
        history.append({"步骤": len(selected), "加入因子": round_name,
                        "验证段胜率": f"{round_best[0]:.0%}",
                        "验证段净超额": f"{round_best[1]:+.2%}"})

    # --- 阶段 2：在测试段上最终评估 ---
    oos_winrate_test = None
    wf_test = pd.DataFrame()
    if selected and split_idx < len(days_all):
        obj_test, wf_test = _eval_segment(selected, split_idx, None)
        if obj_test:
            oos_winrate_test = obj_test[0]

    # 组合级多重检验 p-value
    n_oos = len(best_wf) if not best_wf.empty else 0
    final_wr = oos_winrate_test if oos_winrate_test is not None else (
        float((best_wf["优化组合扣费超额"] > 0).mean()) if not best_wf.empty and "优化组合扣费超额" in best_wf else 0.5)
    combo_fdr = combo_false_discovery_rate(
        n_candidates=len(candidates), n_rounds=len(selected),
        selected_winrate=final_wr, n_oos_periods=n_oos)

    return {"selected": selected, "history": pd.DataFrame(history),
            "wf": best_wf, "wf_test": wf_test,
            "oos_winrate_test": oos_winrate_test,
            "combo_fdr": combo_fdr}


# ---------------------------------------------------------------- MMR 组合选择
# 质量加权 + 相关性惩罚 + 概率采样：高胜率/高ICIR 因子入选概率大，但会因与已选
# 因子高度相关而被压低概率——"选出一系列"互有差异又互补的因子组合。
def quality_scores(scorecard: pd.DataFrame, win_col: str = "5日胜率") -> pd.Series:
    """因子质量分（非负）：胜率超额 + ICIR 归一。

    q = 2×max(胜率-0.5, 0) + |ICIR|/max|ICIR|　→ 范围约 [0, 2]
    胜率列不存在时退回 Top组胜率；分数只用于 softmax 相对概率，量纲无关紧要。
    """
    sc = scorecard.set_index("因子")
    win_col = win_col if win_col in sc.columns else "Top组胜率"
    win = pd.to_numeric(sc[win_col], errors="coerce").fillna(0.5)
    icir = pd.to_numeric(sc["ICIR"], errors="coerce").abs().fillna(0.0)
    max_icir = float(icir.max()) if (icir > 0).any() else 1.0
    return (win - 0.5).clip(lower=0) * 2.0 + icir / max_icir


def _softmax_probs(scores: np.ndarray, tau: float) -> np.ndarray:
    """数值稳定 softmax。tau 越大越均匀，越小越逼近"必选最高分"。"""
    z = (scores - scores.max()) / max(tau, 1e-6)
    e = np.exp(z)
    return e / e.sum()


def _mmr_sample(scores: pd.Series, corr: pd.DataFrame, k_max: int,
                tau: float, lam: float, rng: random.Random) -> list[str]:
    """单组 MMR 采样：softmax(质量分) 选第一个，之后每选一个就把其余因子的
    分数减去 lam × 与已选因子的最大 |IC 相关|，再 softmax 抽下一个。

    返回选中的因子名列表（分数全为非正 或 选满 k_max 时停止）。
    """
    remain = [n for n in scores.index if n in corr.columns]
    cur = scores.copy()
    selected: list[str] = []
    for _ in range(k_max):
        if not remain:
            break
        vals = np.array([cur[n] for n in remain], dtype=float)
        if np.nanmax(vals) <= 0:
            break
        probs = _softmax_probs(np.nan_to_num(vals), tau)
        pick = rng.choices(remain, weights=probs, k=1)[0]
        selected.append(pick)
        remain.remove(pick)
        for n in remain:
            c = max((abs(corr.loc[n, j]) for j in selected
                     if j in corr.columns and pd.notna(corr.loc[n, j])), default=0.0)
            cur[n] = float(scores[n]) - lam * c
    return selected


def mmr_combo(factor_vals: dict[str, pd.Series], panel: pd.DataFrame, method: str,
              top_n: int, candidates: list[str], scorecard: pd.DataFrame | None = None,
              corr: pd.DataFrame | None = None, k_max: int = 5, tau: float = 0.2,
              lam: float = 1.0, num_samples: int = 12,
              fwd_days: int = MAIN_FWD, step: int = STEP_DAYS, cost: float = 0.0025,
              min_points: int = 8, buffer_n: int = 0, seed: int = 42,
              oos_test_ratio: float = 0.20) -> dict:
    """MMR 迭代采样选因子组合：软最大化采样（胜率/ICIR 高的入选概率大）+
    相关性软惩罚（与已选因子越像概率越低），采样多组后各自 walk-forward 验证，
    取 OOS 扣费胜率最高（并列比平均净超额）的一组；贪心结果作为保底候选之一。

    与 greedy_combo 同输入输出形态（selected/history/wf/wf_test/oos_winrate_test），
    另加 samples（采样组数）。scorecard/corr 缺任一即退化为 greedy_combo。
    """
    # 退化路径：缺评分卡/相关性矩阵，或候选太少，直接贪心
    if scorecard is None or corr is None or len(candidates) < 2:
        g = greedy_combo(factor_vals, panel, method, top_n, candidates,
                         fwd_days=fwd_days, step=step, cost=cost,
                         min_points=min_points, buffer_n=buffer_n,
                         oos_test_ratio=oos_test_ratio)
        g["samples"] = 1
        return g
    vals_norm = {n: _norm(factor_vals[n].dropna()) for n in candidates
                 if n in factor_vals and not factor_vals[n].dropna().empty}
    avail = [n for n in candidates if n in vals_norm]
    if len(avail) < 2:
        g = greedy_combo(factor_vals, panel, method, top_n, avail,
                         fwd_days=fwd_days, step=step, cost=cost,
                         min_points=min_points, buffer_n=buffer_n,
                         oos_test_ratio=oos_test_ratio)
        g["samples"] = 1
        return g

    # 胜率列：按持有期映射到体检表的多周期胜率列
    h_label = next((lab for lab, d in WIN_HORIZONS.items() if d == fwd_days), "5日")
    q = quality_scores(scorecard, win_col=f"{h_label}胜率")
    q = q.reindex(avail).fillna(0.0)
    fwd = forward_returns(panel, fwd_days)
    ic_full = {n: ic_series(vals_norm[n], fwd) for n in avail}

    # --- 计算验证段/测试段边界 ---
    days_all = sorted(set.intersection(*[set(s.index.get_level_values("datetime").unique())
                                         for s in vals_norm.values()])) if vals_norm else []
    split_idx = int(len(days_all) * (1 - oos_test_ratio)) if oos_test_ratio > 0 else len(days_all)

    def _eval_segment(names: list[str], si: int | None, ei: int | None):
        wf = walk_forward({n: vals_norm[n] for n in names}, panel, method, top_n,
                          step=step, fwd_days=fwd_days, cost=cost,
                          buffer_n=buffer_n, ic_full=ic_full, min_factors=1,
                          start_idx=si, end_idx=ei)
        if wf.empty or len(wf) < min_points or "优化组合扣费超额" not in wf:
            return None, wf
        net = wf["优化组合扣费超额"]
        return (float((net > 0).mean()), float(net.mean())), wf

    rng = random.Random(seed)
    combos: list[list[str]] = []
    # 贪心保底（在验证段上选）
    g = greedy_combo(factor_vals, panel, method, top_n, candidates,
                     fwd_days=fwd_days, step=step, cost=cost,
                     min_points=min_points, buffer_n=buffer_n,
                     oos_test_ratio=oos_test_ratio)
    if g.get("selected"):
        combos.append(list(g["selected"]))  # 贪心结果保底参与竞争
    for _ in range(num_samples):
        sel = _mmr_sample(q, corr, min(k_max, len(avail)), tau, lam, rng)
        if len(sel) >= 1 and sel not in combos:
            combos.append(sel)

    memo: dict[tuple, tuple] = {}
    best_names, best_obj, best_wf = None, (-1.0, -9e9), pd.DataFrame()
    eval_rows = []
    for names in combos:
        key = tuple(names)
        if key not in memo:
            memo[key] = _eval_segment(list(names), 0, split_idx if split_idx < len(days_all) else None)
        obj, wf = memo[key]
        eval_rows.append({"组合": " + ".join(names), "因子数": len(names),
                          "验证段胜率": f"{obj[0]:.0%}" if obj else "评估失败",
                          "验证段净超额": f"{obj[1]:+.2%}" if obj else "-"})
        if obj and obj > best_obj:
            best_obj, best_names, best_wf = obj, list(names), wf

    # --- 测试段最终评估 ---
    oos_winrate_test = None
    wf_test = pd.DataFrame()
    if best_names and split_idx < len(days_all):
        obj_test, wf_test = _eval_segment(best_names, split_idx, None)
        if obj_test:
            oos_winrate_test = obj_test[0]

    # 组合级多重检验 p-value
    n_oos = len(best_wf) if not best_wf.empty else 0
    final_wr = oos_winrate_test if oos_winrate_test is not None else (
        float((best_wf["优化组合扣费超额"] > 0).mean()) if not best_wf.empty and "优化组合扣费超额" in best_wf else 0.5)
    combo_fdr = combo_false_discovery_rate(
        n_candidates=len(avail), n_rounds=len(combos),
        selected_winrate=final_wr, n_oos_periods=n_oos)

    history = pd.DataFrame(eval_rows)
    return {"selected": best_names or [], "history": history, "wf": best_wf,
            "wf_test": wf_test, "oos_winrate_test": oos_winrate_test,
            "combo_fdr": combo_fdr,
            "samples": len(combos), "obj": best_obj}


# ---------------------------------------------------------------- 事件研究（事件前兆因子挖掘）
EVENT_KINDS = ["涨停", "大涨≥7%", "跌停", "创60日新高"]


def _limit_ratio(code: str) -> float:
    """各板块涨跌停幅度：北交所 30% / 创业板(30)科创板(68) 20% / 主板 10%。"""
    if code.startswith("BJ"):
        return 0.30
    d = "".join(ch for ch in code if ch.isdigit())
    return 0.20 if d.startswith(("30", "68")) else 0.10


def _event_mask(panel: pd.DataFrame, kind: str) -> pd.DataFrame:
    """事件布尔矩阵（datetime × instrument）。"""
    close = panel["$close"].unstack("instrument")
    ret = close.pct_change()
    if kind == "创60日新高":
        return close >= close.rolling(60).max() * 0.999
    thr = pd.Series({c: _limit_ratio(c) - 0.002 for c in close.columns})
    if kind == "涨停":
        return ret.ge(thr, axis=1)
    if kind == "跌停":
        return ret.le(-thr, axis=1)
    return ret.ge(0.07)  # 大涨≥7%


def find_events(panel: pd.DataFrame, kind: str = "涨停") -> pd.DataFrame:
    """在面板上找事件点，返回 [(datetime, instrument)] 索引 + 当日涨幅列。
    涨停判定用日涨幅阈值（留 0.2% 余量）；创60日新高为收盘≥60日最高价×0.999。"""
    close = panel["$close"].unstack("instrument")
    ret = close.pct_change()
    m = _event_mask(panel, kind)
    hit = m.stack().rename("hit")
    df = pd.concat([hit, ret.stack().rename("ret")], axis=1)
    return df[df["hit"]].drop(columns="hit").dropna()


def event_premonition(factor_vals: dict[str, pd.Series], events: pd.DataFrame,
                      panel: pd.DataFrame, lag: int = 1, mode: str = "cs",
                      min_n: int = 5) -> pd.DataFrame:
    """事件前兆分析：事件前 lag 个交易日的因子分位 vs 基准 0.5。

    mode="cs"（池模式）：横截面分位——事件日的因子值在全池中的位置；
    mode="ts"（单票模式）：时序分位——在该股自身历史中的位置（单票无截面）。
    返回按 |差值| 降序的表：因子/方向/事件前平均分位/差值/前20%分位占比/t值/样本数。
    """
    cal = sorted(panel.index.get_level_values("datetime").unique())
    pos = {d: i for i, d in enumerate(cal)}
    pairs = set()
    for d, c in zip(events.index.get_level_values("datetime"),
                    events.index.get_level_values("instrument")):
        i = pos.get(d)
        if i is not None and i >= lag:
            pairs.add((cal[i - lag], c))
    if len(pairs) < min_n:
        return pd.DataFrame()
    pdf = pd.DataFrame(list(pairs), columns=["datetime", "instrument"])
    # 统一 datetime 列类型（防止 datetime64 vs object 合并不兼容）
    if pdf["datetime"].dtype != "datetime64[ns]":
        pdf["datetime"] = pd.to_datetime(pdf["datetime"])
    rows = []
    for name, s in factor_vals.items():
        s = _norm(s.dropna())
        if s.empty:
            continue
        if mode == "ts":
            cs = s.groupby(level="instrument", group_keys=False).apply(lambda x: x.rank(pct=True))
        else:
            cs = s.groupby(level="datetime").rank(pct=True)
        cs_df = cs.rename("cs").reset_index()
        if cs_df["datetime"].dtype != "datetime64[ns]":
            cs_df["datetime"] = pd.to_datetime(cs_df["datetime"])
        j = pdf.merge(cs_df, on=["datetime", "instrument"])["cs"].dropna()
        if len(j) < min_n:
            continue
        diff = float(j.mean() - 0.5)
        t = diff / (float(j.std()) / np.sqrt(len(j)) + 1e-12)
        rows.append({"因子": name, "方向": "事件前偏高" if diff >= 0 else "事件前偏低",
                     "事件前平均分位": round(float(j.mean()), 3), "差值": round(diff, 3),
                     "前20%分位占比": round(float((j >= 0.8).mean()), 3),
                     "t值": round(t, 2), "样本数": len(j)})
    out = pd.DataFrame(rows)
    return out.sort_values("差值", key=abs, ascending=False).reset_index(drop=True) if not out.empty else out


# ---------------------------------------------------------------- 策略组合（多包投票）回测
def combo_backtest(pack_defs: list[dict], panel: pd.DataFrame, min_votes: int = 2,
                   fwd_days: int = MAIN_FWD, step: int = STEP_DAYS,
                   cost: float = 0.0025) -> pd.DataFrame:
    """策略组合回测：统一调仓网格上每个策略包各自打分取 Top-N，按票数合成名单。

    pack_defs: [{"name": str, "weights": {因子: (w, d)}, "fvals": {因子: Series}, "top_n": int}]
    合成规则：票数 ≥ min_votes 入选；不足 3 只时按票数降序放宽到 3~5 只。
    返回 DataFrame：调仓日 / 组合超额 / 组合扣费超额 / 组合换手率 / 入选只数 / 各包超额（对比曲线用）。
    各包权重为保存时的固定权重（不做滚动重估）——回测的是"这组包按此规则合用"的表现。
    """
    fwd = forward_returns(panel, fwd_days)
    packs = []
    for pd_ in pack_defs:
        vals = {n: _norm(s.dropna()) for n, s in pd_["fvals"].items() if not s.dropna().empty}
        if vals:
            # 包快照（pack_defs 带 factors 条目时）> 全局开关自动映射；legacy → None
            packs.append({"name": pd_["name"], "weights": pd_["weights"],
                          "top_n": int(pd_["top_n"]), "vals": vals,
                          "norms": sig.scoring_norms(list(vals), pd_.get("factors"))})
    if len(packs) < 2:
        return pd.DataFrame()
    days = list(panel.index.get_level_values("datetime").unique())
    prev: set = set()
    rows = []
    for t in days[::step]:
        if t not in fwd.index:
            continue
        fr = fwd.loc[t].dropna()
        if fr.empty:
            continue
        med = float(fr.median())
        row = {"调仓日": str(t)[:10], "池内中位收益": med}
        votes: dict[str, int] = {}
        for p in packs:
            sc_t = _score_at(p["vals"], p["weights"], t, norms=p["norms"])
            ranked = sc_t[sc_t.index.isin(fr.index)].sort_values(ascending=False)
            picks = list(ranked.head(p["top_n"]).index)
            if len(picks) < max(3, p["top_n"] // 2):
                continue
            row[f"{p['name']}超额"] = float(fr[picks].mean()) - med
            for c in picks:
                votes[c] = votes.get(c, 0) + 1
        merged = [c for c, v in votes.items() if v >= min_votes]
        if len(merged) < 3 and votes:  # 太严格时放宽：按票数降序取 3~5 只
            merged = sorted(votes, key=lambda c: -votes[c])[:max(3, min(5, len(votes)))]
        merged = [c for c in merged if c in fr.index]
        if len(merged) < 3:
            continue
        cur = set(merged)
        turnover = 1.0 if not prev else 1 - len(cur & prev) / len(merged)
        prev = cur
        excess = float(fr[merged].mean()) - med
        row["入选只数"] = len(merged)
        row["组合超额"] = excess
        row["组合换手率"] = turnover
        row["组合扣费超额"] = excess - turnover * cost
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 多目标评分
def _calc_max_drawdown(returns: pd.Series) -> float:
    """计算最大回撤"""
    if returns.empty:
        return 0.0
    nav = (1 + returns).cumprod()
    drawdown = (nav - nav.cummax()) / nav.cummax()
    return float(drawdown.min()) if len(drawdown) > 0 else 0.0


def _calc_sharpe(returns: pd.Series, risk_free: float = 0.0) -> float:
    """计算夏普比率"""
    if returns.empty or returns.std() < 1e-10:
        return 0.0
    excess = returns.mean() - risk_free
    return float(excess / returns.std() * np.sqrt(252))


def _calc_sortino(returns: pd.Series, risk_free: float = 0.0) -> float:
    """计算索提诺比率"""
    if returns.empty:
        return 0.0
    excess = returns.mean() - risk_free
    downside = returns[returns < 0]
    if downside.empty or downside.std() < 1e-10:
        return 0.0
    return float(excess / downside.std() * np.sqrt(252))


def _calc_calmar(returns: pd.Series) -> float:
    """计算卡玛比率"""
    if returns.empty:
        return 0.0
    ann_return = returns.mean() * 252
    max_dd = abs(_calc_max_drawdown(returns))
    if max_dd < 1e-10:
        return 0.0
    return float(ann_return / max_dd)


def _calc_ic_stability(ic_series: pd.Series) -> float:
    """IC稳定性：IC标准差相对IC均值的逆指标（0-1，越高越稳定）。

    经验验证：原公式 1 - rolling_std*10 对典型 IC（std≈0.1）恒为 0（死分量）。
    改为相对指标：std/|mean| 越小越稳定，4 倍以上视为不稳定。"""
    if ic_series.empty or len(ic_series) < 60:
        return 0.5
    ic_mean = abs(float(ic_series.mean()))
    ic_std = float(ic_series.std())
    denom = max(ic_mean, 0.005)
    ratio = ic_std / denom
    return float(max(0.0, min(1.0, 1.0 - ratio / 8.0)))


def _calc_ic_trend(ic_series: pd.Series) -> float:
    """IC趋势：近60天相对斜率（0-1，0.5 为持平，>0.5 改善，<0.5 恶化）。

    经验验证：原公式 0.5 + slope*100 缩放失当（死分量）。
    改为相对变化：60 天累计变化 / |IC均值|，变化 ±4 倍饱和。"""
    if ic_series.empty or len(ic_series) < 60:
        return 0.5
    y = ic_series[-60:].values.astype(float)
    slope = float(np.polyfit(np.arange(len(y)), y, 1)[0])
    denom = max(abs(float(y.mean())), 0.005)
    frac = slope * len(y) / denom
    return float(max(0.0, min(1.0, 0.5 + frac / 4.0)))


def _calc_crowding_score(vals: pd.Series, panel: pd.DataFrame, lookback: int = 60) -> float:
    """
    因子拥挤度评分：0=极度拥挤（需惩罚），1=不拥挤。
    
    拥挤度代理指标（无需持仓数据）：
    1. 因子截面离散度下降 → 因子驱动的价格趋同 → 拥挤信号
    2. 因子 Top/Bottom 组的成交额集中度上升 → 拥挤信号
    3. 因子 IC 的自相关性突然增强 → 同质交易 → 拥挤信号
    """
    v = vals.dropna()
    if v.empty:
        return 0.5
    
    # 指标1：因子截面离散度趋势
    cs_std = v.groupby(level="datetime").std()
    if len(cs_std) < lookback:
        return 0.5
    recent_std = float(cs_std[-lookback//2:].mean())
    earlier_std = float(cs_std[-lookback:-lookback//2].mean())
    dispersion_ratio = recent_std / (earlier_std + 1e-12)
    # 离散度下降 = 拥挤
    dispersion_signal = max(0.0, min(1.0, 1.0 - (1.0 - dispersion_ratio) * 3))
    
    # 指标2：因子 Top 组换手率（如果 volume 可用）
    crowding_from_turnover = 0.5
    if "$volume" in panel.columns:
        try:
            volume = panel["$volume"].unstack("instrument") if "instrument" in panel.index.names else panel["$volume"]
            ranks = v.groupby(level="datetime").rank(pct=True)
            top_mask = ranks > 0.9
            # Top 组股票的成交额占比趋势
            if hasattr(volume, 'sum'):
                top_vol_share = (volume[top_mask].sum() / volume.sum()).rolling(20).mean()
                if len(top_vol_share.dropna()) > 20:
                    recent_share = float(top_vol_share[-20:].mean())
                    earlier_share = float(top_vol_share[-40:-20].mean()) if len(top_vol_share) > 40 else recent_share
                    vol_signal = max(0.0, min(1.0, 1.0 - (recent_share - earlier_share) * 10))
                    crowding_from_turnover = vol_signal
        except Exception:
            pass
    
    # 指标3：IC 序列的自相关性（拥挤 → 同步交易 → IC 自相关增强）
    ic_signal = 0.5
    try:
        fwd = forward_returns(panel, 5)
        ic = ic_series(v, fwd)
        if len(ic) > 30:
            autocorr = float(ic.autocorr(lag=1))
            ic_signal = max(0.0, min(1.0, 1.0 - autocorr * 2))
    except Exception:
        pass
    
    # 综合：离散度(40%) + 成交集中度(30%) + IC自相关(30%)
    return float(dispersion_signal * 0.4 + crowding_from_turnover * 0.3 + ic_signal * 0.3)


def complexity_penalty(code: str) -> float:
    """复杂度惩罚：基于奥卡姆剃刀，复杂因子需要更高 IC 才能获得同等评分。

    depth=2→1.0, depth=4→0.9, depth=6→0.7, depth=7→0.5
    ops=3→1.0, ops=8→0.85, ops=12→0.65, ops=18→0.5
    """
    if not code:
        return 0.95
    try:
        from loopengine.tree import parse
        sexpr = code.split("\n", 1)[0].replace("# sexpr: ", "") if "# sexpr:" in code else code
        tree = parse(sexpr)
        depth = tree.depth()
        ops_count = 0
        def _count_ops(node):
            nonlocal ops_count
            if hasattr(node, "op"):
                ops_count += 1
                for ch in node.children:
                    _count_ops(ch)
        _count_ops(tree)
        depth_pen = max(0.5, 1.0 - (depth - 2) * 0.1)
        ops_pen = max(0.5, 1.0 - max(0, ops_count - 5) * 0.05)
        return depth_pen * ops_pen
    except Exception:
        return 0.95  # 无法解析时轻微惩罚


def multi_objective_score(factor_name: str, codes: list[str], end: str,
                         weights: dict | None = None, code: str | None = None) -> dict:
    """
    多目标评分：平衡收益和风险
    
    Args:
        factor_name: 因子名称
        codes: 股票池代码
        end: 截止日期
        weights: 权重配置（默认 {'ic': 0.65, 'risk': 0.10, 'sharpe': 0.10, 'crowding': 0.10, 'stability': 0.05}，经验校准）
        code: 因子代码（loopengine 因子入库前传 emit_code 结果，避免依赖注册表）
    
    Returns:
        dict: {
            'score': 综合评分 (0-1)
            'ic_score': IC评分（对数缩放+ICIR加权）
            'risk_score': 风险评分
            'sharpe_score': 夏普评分
            'crowding_score': 拥挤度评分（0=拥挤，1=不拥挤）
            'stability_score': 稳定性评分
            'trend_score': 趋势评分
            'max_drawdown': 最大回撤
            'sharpe': 夏普比率
            'sortino': 索提诺比率
            'calmar': 卡玛比率
            'ic_mean': IC均值
            'ic_std': IC标准差
            'icir': IC信息比率
            'details': 详细指标
        }
    """
    if weights is None:
        # 经验验证（walk-forward OOS，n=67~69，两个 OOS 窗口）：
        # 1) 过闸因子间 样本内夏普/索提诺/卡玛/回撤 与 OOS IC 显著负相关
        #    （ρ=-0.45~-0.60，p<0.001）——线性奖励平滑曲线=奖励过拟合；
        # 2) IC 稳定性/趋势分量同样负向预测 OOS（trend ρ=-0.45）——仅作诊断展示，不计入评分；
        # 3) 风险/夏普只保留宽松的灾难阈值惩罚（多数因子满分，不产生有害排序）；
        # 4) 新增：拥挤度评分（因子拥挤是A股因子失效首要原因）。
        weights = {'ic': 0.65, 'risk': 0.10, 'sharpe': 0.10, 'crowding': 0.10, 'stability': 0.05}
    
    try:
        # 获取因子值和IC序列
        fac = {"name": factor_name, "kind": "loopengine", "code": code}
        if not fac["code"]:
            fac = resolve_factor(factor_name) or fac
        if not fac.get("code"):
            return {'score': 0.0, 'error': f'无法解析因子代码: {factor_name}'}
        vals = get_factor_values(fac, codes, end, source=_eval_source())
        ic_series = get_ic_series(fac, codes, end, source=_eval_source())
        
        if vals.empty or ic_series.empty:
            return {'score': 0.0, 'error': '数据为空'}
        
        # 获取面板和远期收益
        panel = sig.get_panel_cached(codes, end, 800, source=_eval_source())
        fwd = forward_returns(panel, 5)  # 5日远期收益
        
        # 计算IC指标
        ic_mean = float(ic_series.mean())
        ic_std = float(ic_series.std())
        ic_winrate = float((ic_series > 0).mean())
        icir = ic_mean / (ic_std + 1e-12)  # IC信息比率
        
        # 计算收益序列
        from gates import _daily_excess  # 延迟导入避免与 gates 的循环依赖
        excess_series = _daily_excess(vals, fwd)
        
        # 计算风险指标
        max_dd = _calc_max_drawdown(excess_series)
        sharpe = _calc_sharpe(excess_series)
        sortino = _calc_sortino(excess_series)
        calmar = _calc_calmar(excess_series)
        
        # 计算IC稳定性
        stability = _calc_ic_stability(ic_series)
        
        # 计算IC趋势
        trend = _calc_ic_trend(ic_series)
        
        # 计算各维度评分
        # 1. IC评分 (0-1)：对数缩放 + ICIR加权（避免线性饱和，提升因子区分度）
        #    |IC|=0.01 → ~0.26, |IC|=0.03 → ~0.52, |IC|=0.05 → ~0.67, |IC|=0.10 → ~0.85
        import math
        abs_ic = abs(ic_mean)
        ic_component = math.log(1 + abs_ic * 50) / math.log(6)
        ic_component = min(1.0, ic_component)
        
        # ICIR修正：高ICIR说明IC稳定可靠，给予加成
        icir_factor = 1.0
        if icir > 1.5:
            icir_factor = 1.15
        elif icir > 1.0:
            icir_factor = 1.08
        elif icir < 0.3:
            icir_factor = 0.85
        
        ic_score = min(1.0, ic_component * icir_factor * 0.6 + ic_winrate * 0.4)

        # 2. 风险评分 (0-1)：灾难阈值惩罚（回撤 ≤70% 满分；更平滑不额外奖励）
        risk_score = 1.0 if abs(max_dd) <= 0.70 else max(0.0, 1.0 - (abs(max_dd) - 0.70) / 0.30)

        # 3. 夏普评分 (0-1)：门槛惩罚（夏普 ≥0.5 满分；更高不额外奖励）
        sharpe_score = 1.0 if sharpe >= 0.5 else max(0.0, sharpe / 0.5)
        
        # 4. 拥挤度评分 (0-1)：检测因子拥挤度（拥挤度高则惩罚）
        crowding_score = _calc_crowding_score(vals, panel)
        
        # 5. 稳定性评分 (0-1)：IC稳定性（作为辅助维度）
        stability_score = max(0.0, min(1.0, stability))
        
        # 综合评分 × 复杂度惩罚（奥卡姆剃刀：复杂因子打折）
        raw_score = (ic_score * weights['ic'] + 
                     risk_score * weights['risk'] + 
                     sharpe_score * weights['sharpe'] +
                     crowding_score * weights['crowding'] +
                     stability_score * weights['stability'])
        pen = complexity_penalty(code or "")
        score = raw_score * pen
        
        return {
            'score': round(score, 4),
            'ic_score': round(ic_score, 4),
            'risk_score': round(risk_score, 4),
            'sharpe_score': round(sharpe_score, 4),
            'crowding_score': round(crowding_score, 4),
            'stability_score': round(stability, 4),
            'trend_score': round(trend, 4),
            'max_drawdown': round(max_dd, 4),
            'sharpe': round(sharpe, 4),
            'sortino': round(sortino, 4),
            'calmar': round(calmar, 4),
            'ic_mean': round(ic_mean, 4),
            'ic_std': round(ic_std, 4),
            'icir': round(icir, 4),
            'details': {
                'ic_winrate': round(ic_winrate, 4),
                'excess_mean': round(float(excess_series.mean()), 4),
                'excess_std': round(float(excess_series.std()), 4),
            }
        }
    
    except Exception as e:
        return {'score': 0.0, 'error': str(e)}


def risk_budget_check(strategy_pack: dict, codes: list[str], end: str,
                     max_drawdown_limit: float = 0.15,
                     industry_limit: float = 0.20,
                     stock_limit: float = 0.05) -> dict:
    """
    风险预算检查
    
    Args:
        strategy_pack: 策略包配置
        codes: 股票池代码
        end: 截止日期
        max_drawdown_limit: 最大回撤限制 (默认15%)
        industry_limit: 行业暴露限制 (默认20%)
        stock_limit: 个股集中度限制 (默认5%)
    
    Returns:
        dict: {
            'passed': 是否通过
            'violations': 违规项列表
            'metrics': 详细指标
        }
    """
    violations = []
    metrics = {}
    
    try:
        # 获取策略包的因子值
        factors = strategy_pack.get('factors', [])
        if not factors:
            return {'passed': False, 'violations': ['策略包无因子'], 'metrics': {}}
        
        # 计算组合得分
        panel = sig.get_panel_cached(codes, end, 800, source=_eval_source())
        fwd = forward_returns(panel, 5)
        
        # 模拟组合收益
        combo_vals = None
        for fac in factors:
            try:
                vals = get_factor_values(fac, codes, end, source=_eval_source())
                if combo_vals is None:
                    combo_vals = vals
                else:
                    combo_vals = combo_vals + vals
            except:
                continue
        
        if combo_vals is None or combo_vals.empty:
            return {'passed': False, 'violations': ['无法计算因子值'], 'metrics': {}}
        
        # 计算超额收益
        from gates import _daily_excess  # 延迟导入避免与 gates 的循环依赖
        excess_series = _daily_excess(combo_vals, fwd)
        
        # 1. 检查最大回撤
        max_dd = _calc_max_drawdown(excess_series)
        metrics['max_drawdown'] = max_dd
        if abs(max_dd) > max_drawdown_limit:
            violations.append(f"最大回撤 {abs(max_dd):.2%} > {max_drawdown_limit:.2%}")
        
        # 2. 检查夏普比率
        sharpe = _calc_sharpe(excess_series)
        metrics['sharpe'] = sharpe
        if sharpe < 0.5:
            violations.append(f"夏普比率 {sharpe:.2f} < 0.5")
        
        # 3. 检查行业暴露（简化版：检查Top10%股票的行业分布）
        # 这里需要行业数据，暂时简化处理
        metrics['industry_check'] = 'skipped'
        
        # 4. 检查个股集中度
        # 简化版：检查Top10%股票的数量
        top_count = int(len(codes) * 0.1)
        metrics['top_stock_count'] = top_count
        if top_count < 10:
            violations.append(f"Top组股票数量 {top_count} < 10")
        
        return {
            'passed': len(violations) == 0,
            'violations': violations,
            'metrics': metrics
        }
    
    except Exception as e:
        return {'passed': False, 'violations': [f'检查失败: {str(e)}'], 'metrics': metrics}


def diversity_score(tree, factor_type: str = "量价") -> dict:
    """
    计算因子的多样性评分
    
    Args:
        tree: 因子表达式树
        factor_type: 因子类型
    
    Returns:
        dict: {
            'score': 多样性评分 (0-1)
            'op_diversity': 算子多样性
            'depth_score': 结构多样性
            'family_score': 机制族多样性
        }
    """
    details = {}
    
    # 1. 算子多样性
    ops_used = set()

    def _walk(node):
        if hasattr(node, "op"):
            ops_used.add(node.op)
            for ch in node.children:
                _walk(ch)

    _walk(tree)
    
    all_ops = {'sub', 'mul', 'div', 'abs', 'sign', 'rank_cs', 'ma', 'ts_min', 'ts_max',
               'ts_rank', 'decay_linear', 'std', 'skew', 'delta', 'roc', 'ema', 'zscore', 'corr'}
    op_diversity = len(ops_used) / len(all_ops) if all_ops else 0.0
    details['op_diversity'] = op_diversity
    
    # 2. 结构多样性（树深度）
    depth = tree.depth()
    # 中等深度(3-5)获得奖励
    if 3 <= depth <= 5:
        depth_score = 1.0
    elif depth < 3:
        depth_score = depth / 3
    else:
        depth_score = max(0.5, 1.0 - (depth - 5) / 5)
    details['depth_score'] = depth_score
    
    # 3. 机制族多样性
    # 获取当前机制族覆盖率
    try:
        import library
        registry = library.get_factor_registry()
        if not registry.empty:
            family_counts = registry.groupby('family').size()
            total = family_counts.sum()
            family_coverage = family_counts / total if total > 0 else family_counts * 0
            # 覆盖率越低，多样性评分越高
            family_score = 1.0 - family_coverage.mean()
        else:
            family_score = 0.5
    except:
        family_score = 0.5
    details['family_score'] = family_score
    
    # 4. 综合评分
    score = (op_diversity * 0.3 + 
             depth_score * 0.3 + 
             family_score * 0.4)
    
    return {
        'score': round(float(score), 4),
        'op_diversity': round(float(op_diversity), 4),
        'depth_score': round(float(depth_score), 4),
        'family_score': round(float(family_score), 4)
    }


def adjust_operator_weights() -> dict:
    """
    根据多样性调整算子权重
    
    Returns:
        dict: 调整后的算子权重
    """
    try:
        import library
        registry = library.get_factor_registry()
        if registry.empty:
            return {}
        
        # 统计算子使用频率
        op_freq = {}
        for _, row in registry.iterrows():
            code = row.get('code', '')
            if not code:
                continue
            # 简单统计算子出现次数
            for op in ['sub', 'mul', 'div', 'ma', 'delta', 'roc', 'ts_min', 'ts_max']:
                if op in code:
                    op_freq[op] = op_freq.get(op, 0) + 1
        
        total = sum(op_freq.values()) if op_freq else 1
        
        # 计算多样性奖励
        weights = {}
        for op, freq in op_freq.items():
            freq_ratio = freq / total
            # 使用频率越高，权重越低（鼓励探索）
            if freq_ratio > 0.1:  # 高频算子
                weights[op] = max(0.5, 1.0 - freq_ratio)
            else:  # 低频算子
                weights[op] = min(2.0, 1.0 + freq_ratio)
        
        return weights
    
    except Exception as e:
        return {}
