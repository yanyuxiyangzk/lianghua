"""因子评估与组合引擎：胜率体检 → 去冗余 → 加权 → 样本外验证。

方法学红线：
  - 一切统计 point-in-time：估计权重只用决策日之前已"可观测"的 IC
    （IC 用到未来 fwd 日收益，故估计窗右端再回退 fwd_days）
  - 决策只看 walk-forward 样本外（OOS）结果；样本内（IS）仅作对照
  - QSYS 不生成新因子表达式，只对已有因子做权重/过滤配置
"""

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

import signals as sig
from common import DATA_DIR, get_last_trade_day

EVAL_DIR = DATA_DIR / "cache" / "eval"
FWD_DAYS = [1, 5, 10, 20, 40]
MAIN_FWD = 20          # 主评估窗口（交易日）
EST_WINDOW = 250       # walk-forward 估计窗（交易日）
STEP_DAYS = 20         # walk-forward 应用窗/步长
CORR_THRESHOLD = 0.7   # 去冗余相关性阈值
# 多周期胜率标准（交易日）：1天/5天/1月/3月/6月 —— 因子与策略统一按此衡量
WIN_HORIZONS = {"1日": 1, "5日": 5, "20日": 20, "60日": 60, "120日": 120}


# ---------------------------------------------------------------- 基础件
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


def get_factor_values(fac: dict, codes: list[str], end: str, lookback_days: int = 800,
                      source: str | None = None) -> pd.Series:
    """统一取因子长表 Series[(datetime, instrument)]。

    fac: {"name":..., "kind": "builtin"|"evolved", "code": 进化因子代码}
    """
    import datasource

    # 因子评估强制 qlib_local：与 RD-Agent 同源 + 批量取数（全局切换只影响展示层）
    source = source or "qlib_local"
    ck = _cache("fvals", f"{source}|{fac['name']}|{fac['kind']}|{'|'.join(sorted(codes))}|{end}|{lookback_days}")
    if ck.exists():
        return pd.read_parquet(ck).iloc[:, 0]
    if fac["kind"] == "builtin":
        panel = sig.get_panel_cached(codes, end, lookback_days, source=source)
        s = sig.compute_builtin(panel, fac["name"])
    elif fac["kind"] == "tech":
        panel = sig.get_panel_cached(codes, end, lookback_days, source=source)
        if fac["name"] in sig.CATALOG_NAMES:
            s = sig.compute_common(panel, fac["name"])
        else:
            s = sig.compute_tech(panel, fac["name"])
    else:
        df = sig.run_factor_code(fac["code"], fac["name"], codes, end, lookback_days, source=source)
        s = df.iloc[:, 0]
    s = _norm(s.dropna())
    s.to_frame(fac["name"]).to_parquet(ck)
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

    source = source or "qlib_local"
    ck = _cache("ic", f"{source}|{fac['name']}|{fac['kind']}|{'|'.join(sorted(codes))}|{end}|{fwd_days}|{lookback_days}")
    if ck.exists():
        return pd.read_parquet(ck).iloc[:, 0]
    vals = get_factor_values(fac, codes, end, lookback_days, source=source)
    panel = sig.get_panel_cached(codes, end, lookback_days, source=source)
    ic = ic_series(vals, forward_returns(panel, fwd_days))
    ic.to_frame("ic").to_parquet(ck)
    return ic


def decay_curve(fac: dict, codes: list[str], end: str, source: str | None = None) -> dict:
    """各 forward 窗口的平均 RankIC，用于看因子持仓周期属性。"""
    vals = get_factor_values(fac, codes, end, source=source)
    panel = sig.get_panel_cached(codes, end, 800, source=source)
    return {d: ic_series(vals, forward_returns(panel, d)).mean() for d in FWD_DAYS}


def top_group_winrate(vals: pd.Series, panel: pd.DataFrame, fwd_days: int = MAIN_FWD,
                      step: int = STEP_DAYS, pct: float = 0.1,
                      fwd: pd.DataFrame | None = None) -> float:
    """每 step 个交易日取 Top 十分位组合，forward 超额>0 的占比。
    fwd 可传入预算好的远期收益表（批量多周期评估时避免重复计算）。"""
    if fwd is None:
        fwd = forward_returns(panel, fwd_days)
    v = _norm(vals.dropna())
    days = v.index.get_level_values("datetime").unique()[::step]
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
        wins.append(fr[top.index].mean() - fr.mean() > 0)
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
                         "ICIR": np.nan, "IC胜率": np.nan, "Top组胜率": np.nan,
                         "建议方向": f"评估失败: {str(e)[:40]}", "天数": 0})
    return pd.DataFrame(rows)


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


def dedup_factors(corr: pd.DataFrame, scorecard: pd.DataFrame, threshold: float = CORR_THRESHOLD):
    """按 |ICIR| 降序贪心去冗余。返回 (保留名单, 剔除原因 dict)。"""
    strength = scorecard.set_index("因子")["ICIR"].abs()
    order = [n for n in strength.sort_values(ascending=False).index if n in corr.columns]
    kept, dropped = [], {}
    for n in order:
        conflict = next((k for k in kept if abs(corr.loc[n, k]) > threshold), None)
        if conflict:
            dropped[n] = f"与 {conflict} 相关 {corr.loc[n, conflict]:.2f} > {threshold}"
        else:
            kept.append(n)
    return kept, dropped


# ---------------------------------------------------------------- 加权
def compute_weights(scorecard: pd.DataFrame, method: str, names: list[str],
                    win_col: str = "Top组胜率") -> dict:
    """返回 {因子名: (权重, 方向±1)}。方向自动修正：IC 均值为负 → 负向。
    win_col 指定胜率来源列（多周期标准下用所选持有期的胜率，如 "1日胜率"）。"""
    sc = scorecard.set_index("因子")
    if win_col not in sc.columns:
        win_col = "Top组胜率"
    direction = {n: (1 if sc.loc[n, "IC均值"] >= 0 else -1) for n in names}
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
def walk_forward(factor_vals: dict[str, pd.Series], panel: pd.DataFrame, method: str,
                 top_n: int, est: int = EST_WINDOW, step: int = STEP_DAYS,
                 fwd_days: int = MAIN_FWD, cost: float = 0.0025,
                 buffer_n: int = 0) -> pd.DataFrame:
    """滚动样本外：每个应用点 t，用 [t-est, t-fwd] 的 IC 统计定权重与方向，
    在 t 截面打分取 Top-N，记录随后 fwd_days 的超额收益。

    同时输出等权组合对照列。cost=双边往返成本（默认 0.25%）——
    换手率 = 与上期名单的替换比例，扣费超额 = 毛超额 - 换手×cost。
    1 日口径下换手极高，扣费后超额才是能装进口袋的部分。
    buffer_n>0 时启用缓冲带：上期持仓只要没跌出 Top(top_n+buffer_n) 就继续持有，
    是降换手的标准做法（实测可把 1 日口径 80%/日的换手压到 ~30%）。
    """
    fwd = forward_returns(panel, fwd_days)
    # 全历史 IC 序列（每个因子算一次，应用点只做切片统计 → 快）
    ic_full = {}
    vals_norm = {}
    for name, s in factor_vals.items():
        s2 = _norm(s.dropna())
        if s2.empty:
            continue
        vals_norm[name] = s2
        ic_full[name] = ic_series(s2, fwd)
    days = sorted(set.intersection(*[set(s.index.get_level_values("datetime").unique())
                                     for s in vals_norm.values()])) if vals_norm else []
    if len(days) < est + fwd_days + step:
        return pd.DataFrame()

    prev_picks: dict[str, set] = {"优化组合": set(), "等权组合": set()}
    rows = []
    for t_idx in range(est, len(days) - fwd_days, step):
        t = days[t_idx]
        est_lo = days[t_idx - est]
        est_hi = days[t_idx - fwd_days]  # IC 可观测右端（防未来函数）
        # 切片统计 → 权重
        stats = {}
        for name, ic in ic_full.items():
            seg = ic[(ic.index >= est_lo) & (ic.index <= est_hi)]
            if len(seg) < 60:
                stats[name] = None
                continue
            stats[name] = (seg.mean(), seg.mean() / (seg.std() + 1e-12), (seg > 0).mean())
        valid = {n: s for n, s in stats.items() if s is not None}
        if len(valid) < 2:
            continue
        sc = pd.DataFrame({n: {"IC均值": v[0], "ICIR": v[1], "Top组胜率": v[2]}
                           for n, v in valid.items()}).T
        names = list(valid.keys())
        w_opt = compute_weights(sc.reset_index(names="因子"), method, names)
        w_eq = {n: (1.0 / len(names), w_opt[n][1]) for n in names}

        def _score(weights):
            zl = []
            for n, (w, d) in weights.items():
                if w <= 0:
                    continue
                cross = vals_norm[n][vals_norm[n].index.get_level_values("datetime") == t]
                cross.index = cross.index.get_level_values("instrument")
                zl.append(sig.zscore(cross) * w * d)
            return pd.concat(zl, axis=1).mean(axis=1).dropna() if zl else pd.Series(dtype=float)

        fr = fwd.loc[t].dropna() if t in fwd.index else pd.Series(dtype=float)
        if fr.empty:
            continue
        row = {"调仓日": str(t)[:10], "池内中位收益": fr.median()}
        for label, weights in [("优化组合", w_opt), ("等权组合", w_eq)]:
            sc_t = _score(weights)
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
                row[f"{label}超额"] = row[f"{label}收益"] - fr.median()
                row[f"{label}换手率"] = turnover
                row[f"{label}扣费超额"] = row[f"{label}超额"] - turnover * cost
        if "优化组合超额" in row:
            rows.append(row)
    return pd.DataFrame(rows)
