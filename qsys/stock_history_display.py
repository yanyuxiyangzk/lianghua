"""Display-only daily values; never replace missing observations in storage."""
import math


def daily_value(value, *, suspended=False, integer=False):
    try:
        number=float(value)
    except (TypeError, ValueError):
        number=float('nan')
    if not math.isfinite(number):
        return '停牌' if suspended else '缺失'
    return f'{number:,.0f}' if integer else f'{number:.2f}'


def confirmed_suspensions(code, daily):
    from execution_constraints import attach
    if daily.empty:
        return set()
    frame=daily.assign(code=code).set_index(['date','code'])
    states=attach(frame,'ths_ifind')['suspended']
    return {str(day) for (day,_),state in states.items() if state==1}
