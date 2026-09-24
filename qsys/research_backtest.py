"""Non-overlapping, close-to-close factor diagnostics, not execution simulation."""
import numpy as np
import pandas as pd

POLICY = 'factor-group-v2'
WINDOW_POLICY = 'research-window-v3'


def window_contract():
    return dict(calculation_version=WINDOW_POLICY,
                benchmark_basis='equal_weight_start_price_available_panel_universe',
                universe_basis='supplied_panel_not_verified_historical_constituents',
                monthly_basis='window_return_booked_at_maturity',
                drawdown_basis='maturity_endpoints_not_daily_mark_to_market',
                cost_model='replacement_fraction_times_roundtrip_cost',
                trading_eligible=False)


def benchmark_returns(close, forward, date):
    """Fix benchmark members using start prices, never future availability.

    The supplied panel universe is not evidence of historical constituents.
    A missing terminal quote invalidates the benchmark instead of dropping it.
    """
    start = close.loc[date]
    members = start.index[np.isfinite(start) & (start > 0)]
    if not len(members):
        raise ValueError('对照组合起始日无有效价格')
    ret = forward.loc[date].reindex(members)
    if not np.isfinite(ret).all() or (ret <= -1).any():
        raise ValueError('对照组合未来收益缺失或非有限值，禁止事后剔除股票')
    return ret


def validate_windows(fwd_days, step, top_n, cost):
    for value in (fwd_days, step, top_n):
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value <= 0:
            raise ValueError('持有期、步长和选股数必须为正整数')
    if step < fwd_days or not np.isfinite(cost) or cost < 0:
        raise ValueError('禁止重叠持有期或无效成本')


def window_metrics(returns, starts, ends, calendar):
    """Endpoint research metrics; missing/gapped windows have no Sharpe.

    Monthly observations book the entire window return in its maturity month;
    these are not daily marked-to-market calendar month returns.
    """
    r = pd.Series(returns, dtype=float).reset_index(drop=True)
    starts, ends = pd.DatetimeIndex(starts), pd.DatetimeIndex(ends)
    calendar = pd.DatetimeIndex(calendar)
    if len(r) != len(starts) or len(r) != len(ends) or r.empty:
        raise ValueError('窗口长度不一致或为空')
    a, b = calendar.get_indexer(starts), calendar.get_indexer(ends)
    if (a < 0).any() or (b <= a).any() or (a[1:] < b[:-1]).any():
        raise ValueError('窗口日期不在行情日历、倒序或重叠')
    out = period_metrics(r, int(b[-1] - a[0]))
    if (a[1:] != b[:-1]).any() or len(set(b-a)) != 1:
        out['sharpe'] = None
    monthly = (1 + r).groupby(ends.to_period('M')).prod() - 1
    run = longest = 0
    for v in monthly.reindex(pd.period_range(monthly.index[0], monthly.index[-1], freq='M')):
        run = run + 1 if pd.notna(v) and v <= 0 else 0
        longest = max(longest, run)
    out.update(monthly_winrate=float((monthly > 0).mean()),
               max_consec_loss_months=longest,
               monthly_basis='window_return_booked_at_maturity',
               elapsed_trading_days=int(b[-1] - a[0]))
    return out


def period_metrics(returns, elapsed_days):
    """Metrics for complete, equally spaced periods, including initial equity."""
    r = pd.Series(returns, dtype=float)
    if r.empty:
        return {}
    if elapsed_days <= 0 or not np.isfinite(r).all() or (r <= -1).any():
        raise ValueError('收益无效或研究多空账户亏损达到本金，不能继续复利')
    nav = (1 + r).cumprod()
    peak = nav.cummax().clip(lower=1)
    std = r.std()
    return dict(total_return=float(nav.iloc[-1] - 1),
                ann_return=float(nav.iloc[-1] ** (252 / elapsed_days) - 1),
                max_drawdown=float((nav / peak - 1).min()),
                sharpe=float(r.mean() / std * np.sqrt(252 * len(r) / elapsed_days))
                if len(r) > 1 and std > 1e-12 else None,
                win_rate=float((r > 0).mean()))


def group_research(vals, panel, n_groups=10, fwd_days=5, step=5, cost_per_leg=0.0):
    """Equal-weight groups fixed before observing returns. Ties split by instrument.

    Each period opens/closes both legs; cost_per_leg is round-trip cost per
    unit of each leg. Long 1 + short 1 per unit capital; no borrow/fill model.
    Missing evaluation periods invalidate the curve instead of compressing time.
    """
    for value in (n_groups, fwd_days, step):
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value <= 0:
            raise ValueError('分组数、持有期和步长必须为正整数')
    if n_groups < 2 or step < fwd_days:
        raise ValueError('至少两组，且步长不得小于持有期（禁止重叠复利）')
    if not np.isfinite(cost_per_leg) or cost_per_leg < 0:
        raise ValueError('成本必须为非负有限数')
    close = panel['$close'].unstack('instrument').sort_index()
    factors = vals.unstack('instrument').sort_index()
    if not close.index.is_unique or not factors.index.is_unique:
        raise ValueError('日期重复')
    rows, reasons = [], []
    group_returns = []
    for pos in range(0, len(close) - fwd_days, step):
        date, end = close.index[pos], close.index[pos + fwd_days]
        if date not in factors.index:
            reasons.append(f'{date}: 缺少因子截面')
            continue
        cross = factors.loc[date].replace([np.inf, -np.inf], np.nan).dropna()
        cross = cross.sort_index().sort_values(kind='stable')
        if len(cross) < n_groups * 5:
            reasons.append(f'{date}: 截面样本不足{n_groups * 5}只')
            continue
        start_px = close.loc[date].reindex(cross.index)
        end_px = close.loc[end].reindex(cross.index)
        if (not np.isfinite(start_px).all() or not np.isfinite(end_px).all()
                or (start_px <= 0).any() or (end_px <= 0).any()):
            reasons.append(f'{date}: 已分组股票价格缺失或无效，禁止事后换股')
            continue
        ret = end_px / start_px - 1
        groups = np.array_split(np.arange(len(cross)), n_groups)
        means = [float(ret.iloc[g].mean()) for g in groups]
        group_returns.append(means)
        rows.append(dict(signal_date=date, end_date=end, gross=means[-1] - means[0],
                         cost=2 * cost_per_leg, net=means[-1] - means[0] - 2 * cost_per_leg))
    windows = pd.DataFrame(rows)
    mean = np.mean(group_returns, axis=0) if group_returns else [None] * n_groups
    result = dict(policy=POLICY, fwd_days=fwd_days, step=step,
                  assessment_kind='research_close_to_close_not_execution',
                  note=f'研究口径：{fwd_days}日持有，每{step}交易日取样；等权分层；同日收盘至到期收盘；多空各1倍本金；每腿往返成本{cost_per_leg:.2%}；未模拟成交、融券及持有期间回撤。',
                  status='incomplete' if reasons else ('valid' if rows else 'sample_insufficient'),
                  reasons=reasons, windows=windows,
                  group_mean={f'G{i+1}': v for i, v in enumerate(mean)},
                  ls_ret=pd.Series(dtype=float), ls_nav=pd.Series(dtype=float), ls_stats={})
    if reasons or windows.empty:
        return result
    r = pd.Series(windows.net.to_numpy(), index=pd.DatetimeIndex(windows.end_date))
    elapsed = close.index.get_loc(windows.end_date.iloc[-1]) - close.index.get_loc(windows.signal_date.iloc[0])
    # A larger sampling step leaves flat cash gaps; no annualized Sharpe is
    # reported for these unequally exposed intervals.
    try:
        metrics = period_metrics(r, elapsed)
    except ValueError as exc:
        result.update(status='incomplete', reasons=[str(exc)])
        return result
    nav = pd.concat([pd.Series([1.0], index=[windows.signal_date.iloc[0]]), (1 + r).cumprod()])
    result.update(ls_ret=r, ls_nav=nav, metrics=metrics)
    result['ls_stats'] = {'研究累计收益': f"{metrics['total_return']:.2%}",
                          '研究年化收益': f"{metrics['ann_return']:.2%}",
                          '期末序列最大回撤': f"{metrics['max_drawdown']:.2%}",
                          '夏普': f"{metrics['sharpe']:.2f}" if step == fwd_days and metrics['sharpe'] is not None else '不适用',
                          '胜率': f"{metrics['win_rate']:.0%}", '完整持有期数': str(len(r))}
    if step != fwd_days:
        result['metrics']['sharpe'] = None
    return result
