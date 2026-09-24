"""Deterministic daily ledger. Signal dates are closing decision dates.

Targets execute once at the next observed market session's open. Unfilled
orders expire; they are not silently retried. Daily prices must be on a
consistent price basis. This is a daily simulation, not a live fill model.
"""
import math
import pandas as pd


LIMITATIONS = (
    '日频全额成交模型，不模拟盘口队列、部分成交或盘中路径；未成交委托当日失效。'
    '可选成交限制只按输入字段执行，缺少字段不代表已验证可交易。'
    '交易日历取自行情日期；需调用方保证完整交易日历、历史股票池和一致价格口径；'
    '暂不处理公司行动现金与股数调整。期末持仓按收盘估值，不强制卖出。'
)


def _fee(amount, side, rate=.00025, minimum=5., tax_rate=.0005):
    return max(minimum, amount * rate), amount * tax_rate if side == 'sell' else 0.


def simulate(signals, prices, initial_cash=200000., lot=100, cost_rate=.00025,
             fee_min=5., stamp_tax=.0005, slippage=0.):
    """Execute closing targets on the NEXT session, selling before buying.

    Input: named (date, code) price index; signals date/code and either
    target_shares or target_weight. Weight groups replace the whole portfolio,
    with quantities fixed using decision-day closing equity/prices and a fee
    reserve. Share groups only change named positions. Weights allow unspent
    cash. Gap-up buys are rejected if cash is insufficient (no partial fill).
    Optional volume, suspended, limit_up/down constrain execution conservatively.
    Missing required quotes invalidate the run; known execution blocks are orders.
    """
    params = (initial_cash, cost_rate, fee_min, stamp_tax, slippage)
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in params)
            or initial_cash <= 0 or min(cost_rate, fee_min, stamp_tax, slippage) < 0
            or slippage >= 1 or isinstance(lot, bool) or not isinstance(lot, int) or lot <= 0):
        raise ValueError('参数无效')
    sig = signals.copy()
    weighted = 'target_weight' in sig.columns
    field = 'target_weight' if weighted else 'target_shares'
    if not {'date', 'code', field}.issubset(sig.columns) or (weighted and 'target_shares' in sig.columns):
        raise ValueError('信号必须包含日期、股票和唯一一种目标持仓')
    sig['date'] = pd.to_datetime(sig['date'], errors='raise').dt.strftime('%Y-%m-%d')
    if sig[['date', 'code']].isna().any().any() or sig.duplicated(['date', 'code']).any():
        raise ValueError('日期股票为空或同日同股信号重复')
    targets = pd.to_numeric(sig[field], errors='coerce')
    if any(not math.isfinite(v) or v < 0 or (not weighted and (v != int(v) or v % lot)) for v in targets):
        raise ValueError('目标持仓必须为非负有限值；股数必须为整手')
    sig[field] = targets
    if weighted and (sig.groupby('date')[field].sum() > 1 + 1e-10).any():
        raise ValueError('目标权重合计不得超过1')

    px = prices.copy()
    if not isinstance(px.index, pd.MultiIndex) or set(px.index.names) != {'date', 'code'}:
        raise ValueError('行情索引必须明确命名为date/code')
    px = px.reorder_levels(['date', 'code'])
    px.index = pd.MultiIndex.from_arrays([
        pd.to_datetime(px.index.get_level_values('date'), errors='raise').strftime('%Y-%m-%d'),
        px.index.get_level_values('code')], names=['date', 'code'])
    if px.index.has_duplicates or px.index.to_frame(index=False).isna().any().any():
        raise ValueError('行情日期股票为空或重复')
    if not {'open', 'close'}.issubset(px.columns):
        raise ValueError('缺少开盘或收盘价')
    days = sorted(px.index.get_level_values('date').unique())
    if not days:
        raise ValueError('行情为空')
    if not set(sig.date).issubset(days):
        raise ValueError('信号日期缺少行情交易日')
    if days[-1] in set(sig.date):
        raise ValueError('末日信号缺少下一交易日行情')

    def quote(day, code, column, purpose):
        if (day, code) not in px.index:
            raise ValueError(f'{day} {code}缺少{purpose}行情')
        try:
            value = float(px.loc[(day, code), column])
        except (TypeError, ValueError):
            value = float('nan')
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{day} {code}{purpose}价格无效')
        return value

    cash, positions, bought_on = float(initial_cash), {}, {}
    fills, orders, equity, cashflows, data_issues = [], [], [], [], []
    pending = None
    for day in days:
        if pending is not None:
            signal_day, target = pending
            # Quantities are frozen at the preceding close; deterministic sells first.
            changes = [(code, qty - positions.get(code, 0)) for code, qty in target.items()]
            for code, delta in sorted(changes, key=lambda x: (x[1] > 0, str(x[0]))):
                if not delta:
                    continue
                op = quote(day, code, 'open', '成交')
                q = px.loc[(day, code)]
                side, shares = ('buy', delta) if delta > 0 else ('sell', -delta)
                price = op * (1 + slippage if side == 'buy' else 1 - slippage)
                reason = None
                for column in ('volume', 'suspended', 'limit_up', 'limit_down'):
                    if column in px.columns:
                        try:
                            value = float(q[column])
                        except (TypeError, ValueError):
                            value = float('nan')
                        if (not math.isfinite(value) or
                                (column.startswith('limit_') and value <= 0) or
                                (column == 'volume' and value < 0) or
                                (column == 'suspended' and value not in (0, 1))):
                            if column == 'volume':
                                reason = 'unknown_volume'
                                data_issues.append(dict(date=day, code=code, field=column,
                                                        reason='成交量缺失或无效，无法确认可成交，委托拒绝'))
                                continue
                            raise ValueError(f'{day} {code}成交限制字段{column}无效')
                if reason:
                    pass
                elif q.get('suspended', 0) or q.get('volume', 1) == 0:
                    reason = 'suspended_or_zero_volume'
                elif side == 'buy' and 'limit_up' in q and (op >= q.limit_up or price > q.limit_up):
                    reason = 'limit_up'
                elif side == 'sell' and 'limit_down' in q and (op <= q.limit_down or price < q.limit_down):
                    reason = 'limit_down'
                elif side == 'sell' and (shares > positions.get(code, 0) or bought_on.get(code) == day):
                    reason = 't_plus_one_or_insufficient_position'
                amount = price * shares
                fee, tax = _fee(amount, side, cost_rate, fee_min, stamp_tax)
                if reason is None and side == 'buy' and cash + 1e-9 < amount + fee:
                    reason = 'insufficient_cash'
                order = dict(signal_date=signal_day, date=day, code=code, side=side,
                             shares=shares, status='rejected' if reason else 'filled', reason=reason)
                orders.append(order)
                if reason:
                    continue
                movement = -(amount + fee) if side == 'buy' else amount - fee - tax
                cash += movement
                positions[code] = positions.get(code, 0) + delta
                if side == 'buy':
                    bought_on[code] = day
                fills.append(dict(signal_date=signal_day, date=day, code=code, side=side,
                                  shares=shares, price=price, fee=fee, tax=tax))
                cashflows.append(dict(date=day, code=code, side=side, amount=movement, balance=cash))
        marked = cash + sum(shares * quote(day, code, 'close', '估值')
                            for code, shares in positions.items() if shares)
        equity.append(dict(date=day, cash=cash, market_value=marked-cash, equity=marked,
                           positions={c: n for c, n in positions.items() if n}))
        pending = None
        group = sig[sig.date == day]
        if not group.empty:
            if weighted:
                # Reserve estimated round-trip costs conservatively; quantities do not use future opens.
                reserve = marked * (2 * cost_rate + stamp_tax + slippage) + fee_min * (len(group) + len(positions))
                budget = max(0., marked - reserve)
                target = {c: 0 for c, n in positions.items() if n}
                for row in group.itertuples():
                    close = quote(day, row.code, 'close', '信号')
                    target[row.code] = int(budget * row.target_weight / close / lot) * lot
            else:
                target = {row.code: int(row.target_shares) for row in group.itertuples()}
            pending = (day, target)
    constraints = [c for c in ('volume', 'suspended', 'limit_up', 'limit_down') if c in px.columns]
    return dict(ok=True, initial_cash=initial_cash, cash=cash,
                data_quality_status='incomplete' if data_issues else 'no_detected_order_data_errors',
                data_issues=data_issues,
                fills=pd.DataFrame(fills, columns=['signal_date','date','code','side','shares','price','fee','tax']),
                orders=pd.DataFrame(orders, columns=['signal_date','date','code','side','shares','status','reason']),
                cashflows=pd.DataFrame(cashflows, columns=['date','code','side','amount','balance']),
                equity=pd.DataFrame(equity), positions={c: n for c, n in positions.items() if n},
                assessment_kind='historical_next_open_daily_simulation',
                calculation_contract='execution-next-open-v1', constraints_applied=constraints,
                constraints_missing=[c for c in ('volume','suspended','limit_up','limit_down') if c not in constraints],
                limitations=LIMITATIONS)


def performance(ledger):
    """Daily close-to-close account metrics, including the initial cash baseline."""
    curve = ledger['equity']
    nav = curve.equity.astype(float) / ledger['initial_cash']
    initial = pd.Series([1.0])
    with_initial = pd.concat([initial, nav], ignore_index=True)
    daily = with_initial.pct_change().iloc[1:]
    # The first observation is decision-day cash (no trade); no return interval yet.
    daily = daily.iloc[1:]
    elapsed = len(curve) - 1
    total = float(nav.iloc[-1] - 1)
    std = float(daily.std(ddof=1)) if len(daily) > 1 else 0.
    monthly_nav = pd.Series(nav.to_numpy(), index=pd.to_datetime(curve.date)).groupby(
        pd.to_datetime(curve.date).dt.to_period('M').to_numpy()).last()
    monthly_returns = monthly_nav.pct_change()
    monthly_returns.iloc[0] = monthly_nav.iloc[0] - 1
    return dict(total_return=total,
                ann_return=float((1 + total) ** (252 / elapsed) - 1) if elapsed else None,
                max_drawdown=float((with_initial / with_initial.cummax() - 1).min()),
                sharpe=float(daily.mean() / std * math.sqrt(252)) if std > 0 else None,
                monthly_winrate=float((monthly_returns > 0).mean()),
                monthly_basis='daily_mark_to_market_calendar_month_including_partial',
                sharpe_basis='daily_account_returns_zero_risk_free',
                nav=nav.tolist(), nav_dates=curve.date.tolist(),
                elapsed_trading_days=elapsed)
