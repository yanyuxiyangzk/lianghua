"""Read-only selection checks shared by signal creation and execution."""
import math

SELECTION_POLICY = 'fixed-complete-v2'


def rank_snapshot(factor_series, factors, panel, codes, day, filters=()):
    """One selector for live candidates and frozen-strategy research."""
    import pandas as pd
    import signals as sig
    validate_factors(factors)
    # Work on one date: repeatedly normalizing the full history per window
    # makes fixed-strategy review unnecessarily expensive.
    daily = {}
    for name, values in factor_series.items():
        try:
            daily[name] = values.xs(pd.Timestamp(day), level='datetime', drop_level=False)
        except KeyError:
            raise ValueError(f'因子{name}在{day}无有效值，不能使用残缺策略选股')
    complete = complete_cross_section(daily, factors, day)
    weights = {f['name']: (f['weight'], f['direction']) for f in factors}
    score = sig.composite_score(daily, weights, asof=pd.Timestamp(day),
                                norms=sig.scoring_norms(list(weights), factors))
    score = score[score.index.isin(complete) & score.index.isin(codes)]
    history = panel[panel.index.get_level_values('datetime') <= pd.Timestamp(day)]
    survived = sig.apply_filters(score.index.tolist(), history, list(filters))
    ranked = sig.industry_cap_select(score[score.index.isin(survived)], cap=2)
    return ranked, len(complete)


def completed_signal_day(now=None):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from trading_calendar import day_status, previous_session
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    today = now.strftime('%Y-%m-%d')
    status = day_status(today)
    if status is None:
        raise ValueError('交易日历覆盖不足，不能确定已收盘选股日期')
    day = today if status is True and (now.hour, now.minute) >= (15, 0) else previous_session(today)
    if not day:
        raise ValueError('缺少已确认的上一交易日，暂停选股')
    return day


def signal_date_rejection(signal_date, today):
    from trading_calendar import day_status, previous_session
    if day_status(today) is not True:
        return '交易日历未确认今日可交易，暂停使用名单'
    expected = previous_session(today)
    if expected is None:
        return '交易日历覆盖不足，无法核验名单时效'
    if str(signal_date) != expected:
        return f'名单日期{signal_date}不符合上一交易日{expected}，禁止使用过期或未收盘名单开仓'
    return ''


def validate_factors(factors):
    names = [f.get('name') for f in factors]
    if not names or any(not n for n in names) or len(set(names)) != len(names):
        raise ValueError('策略因子为空、缺少名称或名称重复')
    positive = False
    for f in factors:
        w, d = f.get('weight'), f.get('direction')
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0:
            raise ValueError(f"因子{f['name']}权重无效")
        if isinstance(d, bool) or d not in (-1, 1):
            raise ValueError(f"因子{f['name']}方向无效")
        positive |= w > 0
    if not positive:
        raise ValueError('策略因子权重全部为零')


def complete_cross_section(series, factors, end):
    """Never score a stock using only a subset of its positive-weight factors."""
    import numpy as np
    import pandas as pd
    columns = {}
    for f in factors:
        name = f['name']
        s = series[name]
        if not isinstance(s, pd.Series) or not {'datetime', 'instrument'} <= set(s.index.names):
            raise ValueError(f'因子{name}输出索引无效')
        if s.index.has_duplicates:
            raise ValueError(f'因子{name}输出重复股票日期')
        cross = s[pd.to_datetime(s.index.get_level_values('datetime')).normalize() == pd.Timestamp(end)]
        cross = pd.to_numeric(cross, errors='coerce')
        cross.index = cross.index.get_level_values('instrument')
        cross = cross.where(np.isfinite(cross))
        if not cross.notna().any():
            raise ValueError(f'因子{name}在{end}无有效值，不能使用残缺策略选股')
        if f['weight'] > 0:
            columns[name] = cross
    frame = pd.DataFrame(columns)
    complete = frame.dropna().index
    if len(complete) < 3:
        raise ValueError(f'{end}完整因子交集不足3只股票，暂停选股')
    return complete
