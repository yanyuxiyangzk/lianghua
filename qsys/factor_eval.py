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
        hit = sig._read_parquet_safe(ck)
        if hit is not None:
            return hit.iloc[:, 0]
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

    source = source or "qlib_local"
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
# ---------------------------------------------------------------- 截面打分（walk_forward / static_backtest 共用）
def _score_at(vals_norm: dict[str, pd.Series], weights: dict, t) -> pd.Series:
    """调仓日 t 的截面综合分（z-score × 权重 × 方向）。"""
    zl = []
    for n, (w, d) in weights.items():
        if w <= 0:
            continue
        cross = vals_norm[n][vals_norm[n].index.get_level_values("datetime") == t]
        cross.index = cross.index.get_level_values("instrument")
        zl.append(sig.zscore(cross) * w * d)
    return pd.concat(zl, axis=1).mean(axis=1).dropna() if zl else pd.Series(dtype=float)


# ---------------------------------------------------------------- 滚动样本外
def walk_forward(factor_vals: dict[str, pd.Series], panel: pd.DataFrame, method: str,
                 top_n: int, est: int = EST_WINDOW, step: int = STEP_DAYS,
                 fwd_days: int = MAIN_FWD, cost: float = 0.0025,
                 buffer_n: int = 0, ic_full: dict[str, pd.Series] | None = None,
                 min_factors: int = 2) -> pd.DataFrame:
    """滚动样本外：每个应用点 t，用 [t-est, t-fwd] 的 IC 统计定权重与方向，
    在 t 截面打分取 Top-N，记录随后 fwd_days 的超额收益。

    同时输出等权组合对照列。cost=双边往返成本（默认 0.25%）——
    换手率 = 与上期名单的替换比例，扣费超额 = 毛超额 - 换手×cost。
    1 日口径下换手极高，扣费后超额才是能装进口袋的部分。
    buffer_n>0 时启用缓冲带：上期持仓只要没跌出 Top(top_n+buffer_n) 就继续持有，
    是降换手的标准做法（实测可把 1 日口径 80%/日的换手压到 ~30%）。
    ic_full 可传入预计算的全历史 IC 序列（贪心搜索批量评估时避免重复计算）。
    min_factors：估计窗内有效因子的最少个数（贪心搜索单因子起步时用 1）。
    """
    fwd = forward_returns(panel, fwd_days)
    # 全历史 IC 序列（每个因子算一次，应用点只做切片统计 → 快）
    vals_norm = {}
    for name, s in factor_vals.items():
        s2 = _norm(s.dropna())
        if s2.empty:
            continue
        vals_norm[name] = s2
    if ic_full is None:
        ic_full = {name: ic_series(s, fwd) for name, s in vals_norm.items()}
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
        row = {"调仓日": str(t)[:10], "池内中位收益": fr.median()}
        for label, weights in [("优化组合", w_opt), ("等权组合", w_eq)]:
            sc_t = _score_at(vals_norm, weights, t)
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


# ---------------------------------------------------------------- 样本内对照（固定权重）
def static_backtest(factor_vals: dict[str, pd.Series], panel: pd.DataFrame,
                    weights: dict, top_n: int, fwd_days: int = MAIN_FWD,
                    step: int = STEP_DAYS, cost: float = 0.0025,
                    upto: str | None = None, collect_picks: bool = False) -> pd.DataFrame:
    """样本内对照回测：用 ② 组合构建算好的**固定权重**（不滚动重估），
    在 upto（默认全历史）之前的调仓点上截面打分取 Top-N。

    输出与 walk_forward 同构，用于 ③ 的 IS/OOS 双轨对比：
    IS 胜率高、OOS 胜率低 = 权重过拟合样本内的直接证据。
    collect_picks=True 时附 "picks" 列（每点名单），供策略组合投票复用。"""
    fwd = forward_returns(panel, fwd_days)
    vals_norm = {n: _norm(s.dropna()) for n, s in factor_vals.items() if not s.dropna().empty}
    if not vals_norm:
        return pd.DataFrame()
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
        sc_t = _score_at(vals_norm, weights, t)
        ranked = sc_t[sc_t.index.isin(fr.index)].sort_values(ascending=False)
        picks = ranked.head(top_n)
        if len(picks) < max(3, top_n // 2):
            continue
        cur = set(picks.index)
        turnover = 1.0 if not prev else 1 - len(cur & prev) / len(picks)
        prev = cur
        ret = float(fr[picks.index].mean())
        row = {"调仓日": str(t)[:10], "池内中位收益": float(fr.median()),
               "组合收益": ret, "组合超额": ret - float(fr.median()),
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
                 min_points: int = 8, buffer_n: int = 0) -> dict:
    """前向贪心选因子：从空集开始，每轮把使 walk-forward **扣费胜率**提升最大
    的因子加入组合（胜率并列时比平均净超额），直到无提升或满 max_n 个。

    IC 全序列只预计算一次并注入 walk_forward，单轮评估亚秒级；
    候选建议先去冗余再截到 ~12 个（调用方负责）。选择本身用了 OOS 信息，
    属于"用验证集选模型"——配合 ③ 的多重检验提示解读，别当作无偏胜率。
    """
    vals_norm = {n: _norm(factor_vals[n].dropna()) for n in candidates
                 if n in factor_vals and not factor_vals[n].dropna().empty}
    avail = [n for n in candidates if n in vals_norm]
    if not avail:
        return {"selected": [], "history": pd.DataFrame(), "wf": pd.DataFrame()}
    fwd = forward_returns(panel, fwd_days)
    ic_full = {n: ic_series(vals_norm[n], fwd) for n in avail}

    def _eval(names: list[str]):
        wf = walk_forward({n: vals_norm[n] for n in names}, panel, method, top_n,
                          step=step, fwd_days=fwd_days, cost=cost,
                          buffer_n=buffer_n, ic_full=ic_full, min_factors=1)
        if wf.empty or len(wf) < min_points or "优化组合扣费超额" not in wf:
            return None, wf
        net = wf["优化组合扣费超额"]
        return (float((net > 0).mean()), float(net.mean())), wf

    selected, history = [], []
    best, best_wf = (-1.0, -9e9), pd.DataFrame()
    while avail and len(selected) < max_n:
        round_best, round_name, round_wf = None, None, None
        for n in avail:
            obj, wf = _eval(selected + [n])
            if obj and (round_best is None or obj > round_best):
                round_best, round_name, round_wf = obj, n, wf
        if round_name is None or (selected and round_best <= best):
            break
        selected.append(round_name)
        avail.remove(round_name)
        best, best_wf = round_best, round_wf
        history.append({"步骤": len(selected), "加入因子": round_name,
                        "OOS扣费胜率": f"{round_best[0]:.0%}",
                        "平均净超额": f"{round_best[1]:+.2%}"})
    return {"selected": selected, "history": pd.DataFrame(history), "wf": best_wf}


# ---------------------------------------------------------------- 事件研究（事件前兆因子挖掘）
EVENT_KINDS = ["涨停", "大涨≥7%", "跌停", "创60日新高"]


def _limit_ratio(code: str) -> float:
    """各板块涨跌停幅度：北交所 30% / 创业板(30)科创板(68) 20% / 主板 10%。"""
    if code.startswith("BJ"):
        return 0.30
    d = "".join(ch for ch in code if ch.isdigit())
    return 0.20 if d.startswith(("30", "68")) else 0.10


def find_events(panel: pd.DataFrame, kind: str = "涨停") -> pd.DataFrame:
    """在面板上找事件点，返回 [(datetime, instrument)] 索引 + 当日涨幅列。
    涨停判定用日涨幅阈值（留 0.2% 余量）；创60日新高为收盘≥60日最高价×0.999。"""
    close = panel["$close"].unstack("instrument")
    ret = close.pct_change()
    if kind == "创60日新高":
        m = close >= close.rolling(60).max() * 0.999
    else:
        thr = pd.Series({c: _limit_ratio(c) - 0.002 for c in close.columns})
        if kind == "涨停":
            m = ret.ge(thr, axis=1)
        elif kind == "跌停":
            m = ret.le(-thr, axis=1)
        else:  # 大涨≥7%
            m = ret.ge(0.07)
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
    rows = []
    for name, s in factor_vals.items():
        s = _norm(s.dropna())
        if s.empty:
            continue
        if mode == "ts":
            cs = s.groupby(level="instrument", group_keys=False).apply(lambda x: x.rank(pct=True))
        else:
            cs = s.groupby(level="datetime").rank(pct=True)
        j = pdf.merge(cs.rename("cs").reset_index(), on=["datetime", "instrument"])["cs"].dropna()
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
            packs.append({"name": pd_["name"], "weights": pd_["weights"],
                          "top_n": int(pd_["top_n"]), "vals": vals})
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
            sc_t = _score_at(p["vals"], p["weights"], t)
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
