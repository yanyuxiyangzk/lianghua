"""事件类策略包专用评估合同（event_relay_v1）。

与主轨"5 日前瞻 walk-forward + OOS 胜率"不同的尺子，为打板/事件接力策略设计：

- 候选：T 日收盘封板且非一字板的股票（limit_up_events，池内交集）
- 排序：按包的因子与权重在候选内排序（复用生产 rank_snapshot，口径一致）
- 入场：T+1 开盘买入；开盘即涨停（一字/秒板）视为买不进，剔除该笔
- 出场：T+1+fwd_days 收盘卖出（默认持有约 2 日）
- 成本：往返 cost（默认 0.2%）
- 基准：当日全部可买候选等权（衡量排序本身的能力，不含择时）
- 指标：窗口数、总笔数、期望收益/窗口、超额、胜率、最大回撤、夏普

样本不足（窗口 < 40 或总笔数 < 100）返回 ok=False + sample_insufficient：
证据不可用而非策略退化——由 revalidate_strategy 归档且不改变包状态。
"""
import numpy as np
import pandas as pd

CONTRACT = 'event_relay_v1'
MIN_WINDOWS = 40
MIN_TRADES = 100


def _limit_threshold(code: str) -> float:
    if code.startswith("BJ"):
        return 0.298
    if code.startswith(("SZ30", "SH688")):
        return 0.198
    return 0.098


def _daily_panel(codes: list[str], start: str, end: str) -> pd.DataFrame:
    import datasource
    with datasource._conn() as c:
        return pd.read_sql_query(
            f"SELECT code,date,open,high,low,close FROM market_daily WHERE source='ths_ifind'"
            f" AND date BETWEEN ? AND ? AND code IN ({','.join('?' * len(codes))})"
            f" ORDER BY code,date",
            c, params=[start, end, *codes])


def _events(start: str, end: str) -> pd.DataFrame:
    import datasource
    with datasource._conn() as c:
        return pd.read_sql_query(
            "SELECT date,code,industry FROM limit_up_events WHERE sealed_close=1 AND one_word=0"
            " AND date BETWEEN ? AND ?", c, params=(start, end))


def _atr_pct_map(panel: pd.DataFrame, period: int = 14) -> dict:
    """(code,date) → ATR%（Wilder 平滑，与 experience.atr_pct_of 同口径）。"""
    out = {}
    for code, g in panel.groupby("code"):
        h, l, c = g["high"], g["low"], g["close"]
        pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, adjust=False).mean()
        for d, a, cl in zip(g["date"], atr, c):
            if cl > 0 and pd.notna(a):
                out[(code, d)] = float(a / cl)
    return out


def _stop_distance(atr_pct: float | None) -> float:
    """事件轨止损距离 = min(5%, max(2%, 1.5×ATR%))。"""
    if atr_pct is None:
        return 0.05
    return min(0.05, max(0.02, 1.5 * atr_pct))


def event_relay_eval(name: str, pk: dict, codes: list[str], end: str,
                     lookback: int = 280, fwd_days: int = 2, cost: float = 0.002,
                     min_candidates: int = 1, sector_cap: bool = False,
                     rr_gate: bool = False, hard_stop: bool = False) -> dict:
    """评估事件包在涨停候选上的排序能力。返回与 save_strategy_validation 兼容的 dict。

    闸门开关（机械风控改版的回测验证用，默认全关=原口径）：
    - min_candidates：当日可买候选少于此数则整日不交易（候选<3 不交易闸）
    - sector_cap：同板块每日最多 1 只（按排名取先）
    - rr_gate：盈亏比闸，止损距离 >4%（ATR 太宽）的票不纳入组合
    - hard_stop：持有期内任一交易日最低价触止损 → 按止损价退出（否则到期收盘卖）
    """
    import datasource
    from selection_policy import rank_snapshot
    from factor_eval import get_factor_values
    from trading_calendar import day_status

    top_n = int(pk.get("top_n") or 5)
    start = (pd.Timestamp(end) - pd.tseries.offsets.BDay(int(lookback * 1.5))).strftime("%Y-%m-%d")
    events = _events(start, end)
    if events.empty:
        return {"ok": False, "name": name, "assessment_status": "sample_insufficient",
                "error": "评估区间无涨停事件档案（limit_up_events 为空），请先运行涨停事件加工",
                "contract": CONTRACT}
    pool = set(codes)
    events = events[events["code"].isin(pool)]
    days_with = events.groupby("date").size()
    if len(days_with) < MIN_WINDOWS:
        return {"ok": False, "name": name, "assessment_status": "sample_insufficient",
                "error": f"池内涨停候选日仅 {len(days_with)} 天（<{MIN_WINDOWS}）："
                         f"股票池涨停事件过少，无法评估事件策略——请检查池子是否匹配",
                "contract": CONTRACT, "candidate_days": len(days_with)}

    panel = _daily_panel(sorted(pool), start, end)
    trade_days = sorted(panel["date"].unique())
    day_pos = {d: i for i, d in enumerate(trade_days)}
    px = {(r.code, r.date): (r.open, r.high, r.low, r.close) for r in panel.itertuples()}
    yclose = {}
    for code, g in panel.groupby("code"):
        closes = g["close"].to_numpy()
        dates = g["date"].to_numpy()
        for i in range(1, len(dates)):
            yclose[(code, dates[i])] = closes[i - 1]
    atr_map = _atr_pct_map(panel) if (rr_gate or hard_stop) else {}

    # 因子求值（生产口径），逐日在池内排名后过滤到涨停候选
    import signals as sig
    panel_q = sig.get_panel_cached(sorted(pool), end, 800, source=datasource.get_loop_source())
    factor_vals = {}
    failed = []
    for fac in pk.get("factors", []):
        try:
            vals = get_factor_values(fac, sorted(pool), end, lookback_days=800,
                                     source=datasource.get_loop_source())
            if not vals.dropna().empty:
                factor_vals[fac["name"]] = vals
            else:
                failed.append(f"{fac.get('name')}:empty_values")
        except Exception as exc:
            failed.append(f"{fac.get('name')}:{type(exc).__name__}")
    if failed or len(factor_vals) != len(pk.get("factors", [])):
        return {"ok": False, "name": name, "assessment_status": "compute_failed",
                "error": "有效因子不足或部分因子失败，不能重验残缺策略",
                "valid_factors": len(factor_vals), "failed_factors": failed, "contract": CONTRACT}
    rank_cache = {}

    def scores(day):
        if day not in rank_cache:
            rank_cache[day] = rank_snapshot(factor_vals, pk["factors"], panel_q, sorted(pool),
                                            day, pk.get("filters", []))[0]
        return rank_cache[day]

    def trade_ret(code, entry_px, entry_day, exit_day, signal_day):
        """出场收益；hard_stop 时持有期内最低价触止损按止损价退出。"""
        out = px.get((code, exit_day))
        exit_px = out[3] if out else None
        if hard_stop:
            stop_px = entry_px * (1 - _stop_distance(atr_map.get((code, signal_day))))
            for k in range(day_pos[entry_day], day_pos[exit_day] + 1):
                bar = px.get((code, trade_days[k]))
                if bar and bar[2] and bar[2] <= stop_px:
                    exit_px = stop_px
                    break
        if exit_px is None or exit_px <= 0:
            return None
        return exit_px / entry_px - 1 - cost

    rows = []
    total_trades = 0
    for day, cand in events.groupby("date"):
        i = day_pos.get(day)
        if i is None or i + 1 + fwd_days >= len(trade_days):
            continue  # 入场/出场日未成熟，跳过（窗口级，非股票级剔除）
        entry_day, exit_day = trade_days[i + 1], trade_days[i + 1 + fwd_days]
        sc = scores(day).dropna().sort_values(ascending=False)
        ranked = [c for c in sc.index if c in set(cand["code"])]
        ind_by_code = dict(zip(cand["code"], cand["industry"])) if sector_cap else {}

        # 当日可买宇宙（开盘未封板且有估值）
        buyable = {}
        for code in cand["code"]:
            yc = yclose.get((code, entry_day))
            bar = px.get((code, entry_day))
            if yc is None or bar is None or not bar[0] or bar[0] <= 0:
                continue
            if bar[0] >= yc * (1 + _limit_threshold(code)) * 0.995:
                continue  # 开盘即板，买不进
            buyable[code] = bar[0]
        if not buyable or len(buyable) < min_candidates:
            continue  # 候选 <min_candidates 的日子不交易（集中度闸）

        picks = []
        seen_ind = set()
        for code in ranked:
            if code not in buyable:
                continue
            ind = ind_by_code.get(code)
            if sector_cap and ind and ind in seen_ind:
                continue  # 同板块 ≤1 只
            if rr_gate and 0.06 / _stop_distance(atr_map.get((code, day))) < 1.5:
                continue  # 盈亏比 <1.5（止损太远），赔率不够
            picks.append(code)
            if sector_cap and ind:
                seen_ind.add(ind)
            if len(picks) >= top_n:
                break

        bench, rets = [], []
        for code, entry_px in buyable.items():
            r = trade_ret(code, entry_px, entry_day, exit_day, day)
            if r is None:
                continue  # 出场日无估值（停牌），该笔剔除
            bench.append(r)
            if code in picks:
                rets.append(r)
        if not bench or not rets:
            continue
        total_trades += len(rets)
        rows.append({"调仓日": day, "候选数": len(bench), "笔数": len(rets),
                     "组合收益": float(np.mean(rets)), "候选等权": float(np.mean(bench)),
                     "超额": float(np.mean(rets) - np.mean(bench))})
    if len(rows) < MIN_WINDOWS or total_trades < MIN_TRADES:
        return {"ok": False, "name": name, "assessment_status": "sample_insufficient",
                "error": f"可成交窗口 {len(rows)} 天（≥{MIN_WINDOWS}）、总笔数 {total_trades}"
                         f"（≥{MIN_TRADES}）不足，事件证据不充分",
                "contract": CONTRACT, "windows": len(rows), "trades": total_trades}
    df = pd.DataFrame(rows)
    net = df["组合收益"]
    excess = df["超额"]
    nav = (1 + net).cumprod()
    max_dd = float((nav / nav.cummax() - 1).min())
    sharpe = float(net.mean() / (net.std() + 1e-12) * np.sqrt(244)) if net.std() > 0 else None
    win = float((net > 0).mean())
    avg_net = float(net.mean())
    passed = avg_net > 0 and max_dd >= -0.15 and (sharpe is not None and sharpe >= 0.5)
    status = ("active" if pk.get("status") == "active" else "shadow") if passed else "degraded"
    return {"ok": True, "name": name, "eval_date": end, "pool_name": pk.get("pool_name"),
            "method": pk.get("method"), "top_n": top_n, "fwd_days": fwd_days,
            "contract": CONTRACT, "cost": cost,
            "gates": {"min_candidates": min_candidates, "sector_cap": sector_cap,
                      "rr_gate": rr_gate, "hard_stop": hard_stop},
            "oos_windows": len(df), "trades": int(total_trades),
            "oos_winrate": win, "avg_net_excess": float(excess.mean()),
            "expect_per_window": avg_net, "max_drawdown": max_dd, "sharpe": sharpe,
            "status": status,
            "event_windows": df.to_dict("records"),
            "note": "事件接力评估：T日封板候选包内排序 Top-N，T+1开盘买（开盘即板剔除），"
                    f"T+{1 + fwd_days}收盘卖，往返成本{cost:.1%}；基准=当日全部可买候选等权"}
