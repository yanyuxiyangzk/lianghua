"""组合层面回测：对策略包做完整回测，输出年化收益、最大回撤、夏普比率等指标。

用法:
    python strategy_backtest.py --strategy Alpha101精华_v1
    python strategy_backtest.py --all
"""

import json
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import factor_eval as fe
import library
import signals as sig
import datasource
from common import all_pools, get_last_trade_day, trade_day_offset

COST = 0.0025  # 双边交易成本


def backtest_strategy(strategy_name: str, pool_name: str = "沪深300",
                      top_n: int = 10, hold_days: int = 5) -> dict:
    """对策略包做组合回测，返回完整指标。"""
    # 读策略包
    with library._lconn() as c:
        row = c.execute(
            "SELECT factors, method, pool_name FROM strategies WHERE name=?",
            (strategy_name,)).fetchone()
    if not row or not row[0]:
        return {"ok": False, "msg": f"策略 {strategy_name} 无因子配置"}

    factors = json.loads(row[0])
    method = row[1] or "ICIR加权"
    pool = row[2] or pool_name

    codes = all_pools().get(pool) or []
    if len(codes) < 30:
        return {"ok": False, "msg": f"池 {pool} 为空"}

    end = get_last_trade_day()
    start = trade_day_offset(end, -500)  # 回测窗口约2年

    # 取面板数据
    panel = sig.get_panel_cached(codes, end, 600, source=datasource.get_loop_source())
    fwd = fe.forward_returns(panel, hold_days)

    # 取各因子值
    factor_vals = {}
    for fac in factors:
        try:
            vals = fe.get_factor_values(fac, codes, end, lookback_days=600)
            factor_vals[fac["name"]] = vals
        except Exception:
            continue

    if not factor_vals:
        return {"ok": False, "msg": "无有效因子值"}

    # 构建权重 dict: {name: (weight, direction)}
    weights = {}
    for f in factors:
        if f["name"] in factor_vals:
            weights[f["name"]] = (f.get("weight", 1.0), f.get("direction", 1))
    if not weights:
        return {"ok": False, "msg": "无有效权重"}

    # 预计算归一化因子值（z-score）
    vals_norm = {}
    for name, s in factor_vals.items():
        s2 = fe._norm(s.dropna())
        if not s2.empty:
            vals_norm[name] = s2

    # 获取所有交易日
    all_dates = sorted(set(
        dt for vals in vals_norm.values()
        for dt in vals.index.get_level_values("datetime").unique()
    ))
    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)
    all_dates = [d for d in all_dates if start_dt <= pd.Timestamp(d) <= end_dt]

    if len(all_dates) < hold_days + 1:
        return {"ok": False, "msg": "回测窗口内数据不足"}

    # 逐日选股 + 计算收益
    nav = [1.0]
    nav_dates = [str(all_dates[0])[:10]]
    pick_log = []
    turnovers = []
    prev_picks = set()

    for i in range(0, len(all_dates) - hold_days, hold_days):
        dt = all_dates[i]
        dt_str = str(dt)[:10]

        # 截面打分：z-score × 权重 × 方向（与 walk_forward 一致）
        sc = fe._score_at(vals_norm, weights, dt)
        if sc.empty:
            continue

        # 选 Top-N
        ranked = sc.sort_values(ascending=False)
        top_codes = list(ranked.index[:top_n])
        if not top_codes:
            continue

        # 换手率
        cur_set = set(top_codes)
        turnover = 1.0 if not prev_picks else 1 - len(cur_set & prev_picks) / max(len(cur_set), 1)
        prev_picks = cur_set
        turnovers.append(turnover)

        # 组合收益（扣费）
        if dt in fwd.index:
            day_fwd = fwd.loc[dt]
            returns = []
            for code in top_codes:
                if code in day_fwd.index:
                    r = day_fwd[code]
                    if pd.notna(r):
                        returns.append(float(r))
            if returns:
                gross_ret = np.mean(returns)
                net_ret = gross_ret - turnover * COST
                nav.append(nav[-1] * (1 + net_ret))
                nav_dates.append(dt_str)
                pick_log.append({
                    "date": dt_str,
                    "picks": top_codes[:3],
                    "gross": round(gross_ret, 4),
                    "net": round(net_ret, 4),
                    "turnover": round(turnover, 2),
                })

    if len(nav) < 2:
        return {"ok": False, "msg": "回测无有效交易"}

    nav_series = pd.Series(nav, index=range(len(nav)))
    total_return = nav[-1] / nav[0] - 1
    years = len(nav_series) * hold_days / 252
    ann_return = (1 + total_return) ** (1 / max(years, 0.1)) - 1 if total_return > -1 else -1

    # 最大回撤
    peak = nav_series.cummax()
    drawdown = (nav_series - peak) / peak
    max_dd = float(drawdown.min())

    # 夏普比率
    daily_rets = pd.Series(nav).pct_change().dropna()
    sharpe = float(daily_rets.mean() / (daily_rets.std() + 1e-12) * np.sqrt(252 / hold_days)) if len(daily_rets) > 1 else 0

    # 月度胜率
    monthly_rets = []
    for i in range(0, len(nav) - 22, 22):
        if i + 22 < len(nav):
            monthly_rets.append(nav[i + 22] / nav[i] - 1)
    monthly_wr = sum(1 for r in monthly_rets if r > 0) / max(len(monthly_rets), 1)

    avg_turnover = float(np.mean(turnovers)) if turnovers else 0

    # 等权组合对照（与 walk_forward 一致）
    eq_nav = [1.0]
    for i in range(0, len(all_dates) - hold_days, hold_days):
        dt = all_dates[i]
        if dt in fwd.index:
            day_fwd = fwd.loc[dt]
            available = [c for c in day_fwd.index if pd.notna(day_fwd[c])]
            if available:
                eq_ret = float(day_fwd[available].mean())
                eq_nav.append(eq_nav[-1] * (1 + eq_ret))
    eq_total = eq_nav[-1] / eq_nav[0] - 1 if len(eq_nav) > 1 else 0
    eq_ann = (1 + eq_total) ** (1 / max(years, 0.1)) - 1 if eq_total > -1 else 0

    result = {
        "ok": True,
        "strategy": strategy_name,
        "pool": pool,
        "factors": len(factors),
        "method": method,
        "period": f"{nav_dates[0]} ~ {nav_dates[-1]}",
        "total_return": round(total_return, 4),
        "ann_return": round(ann_return, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe": round(sharpe, 2),
        "monthly_winrate": round(monthly_wr, 2),
        "avg_turnover": round(avg_turnover, 2),
        "trades": len(nav) - 1,
        "eq_ann_return": round(eq_ann, 4),
        "excess_ann_return": round(ann_return - eq_ann, 4),
        "picks": pick_log[:5],
    }
    return result


def print_result(r: dict):
    """格式化输出回测结果。"""
    if not r.get("ok"):
        print(f"  ERROR: {r.get('msg', 'unknown')}")
        return
    print(f"  期间: {r['period']}")
    print(f"  总收益: {r['total_return']:+.1%}  年化: {r['ann_return']:+.1%}  超额年化: {r.get('excess_ann_return',0):+.1%}")
    print(f"  最大回撤: {r['max_drawdown']:.1%}  夏普: {r['sharpe']:.2f}")
    print(f"  月度胜率: {r['monthly_winrate']:.0%}  换手率: {r.get('avg_turnover',0):.0%}  交易次数: {r['trades']}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="策略组合回测")
    parser.add_argument("--strategy", help="策略名称")
    parser.add_argument("--all", action="store_true", help="回测所有策略")
    parser.add_argument("--pool", default="沪深300", help="股票池")
    parser.add_argument("--top-n", type=int, default=10, help="Top-N选股")
    parser.add_argument("--hold-days", type=int, default=5, help="持有天数")
    args = parser.parse_args()

    if args.all:
        with library._lconn() as c:
            names = [r[0] for r in c.execute("SELECT name FROM strategies").fetchall()]
        for name in names:
            print(f"\n{'='*50}")
            print(f"策略: {name}")
            r = backtest_strategy(name, args.pool, args.top_n, args.hold_days)
            print_result(r)
    elif args.strategy:
        r = backtest_strategy(args.strategy, args.pool, args.top_n, args.hold_days)
        print_result(r)
    else:
        print("请指定 --strategy 或 --all")