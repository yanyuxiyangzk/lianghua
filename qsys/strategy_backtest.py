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

    # 合成因子得分
    weights = {f["name"]: f.get("weight", 1.0) * f.get("direction", 1) for f in factors if f["name"] in factor_vals}
    total_w = sum(abs(w) for w in weights.values())
    if total_w == 0:
        return {"ok": False, "msg": "权重总和为0"}

    # 逐日合成
    all_dates = sorted(set(
        dt for vals in factor_vals.values()
        for dt in vals.index.get_level_values("datetime").unique()
    ))
    # 只保留回测窗口内的日期
    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)
    all_dates = [d for d in all_dates if start_dt <= pd.Timestamp(d) <= end_dt]

    daily_scores = {}
    for dt in all_dates:
        score = 0
        for name, w in weights.items():
            vals = factor_vals[name]
            v = vals[vals.index.get_level_values("datetime") == dt]
            if not v.empty:
                score += v.mean() * w / total_w
        daily_scores[dt] = score

    score_series = pd.Series(daily_scores).sort_index()

    # 逐日选股+计算收益
    nav = [1.0]
    nav_dates = [str(all_dates[0])[:10]]
    pick_log = []

    for i in range(0, len(score_series) - hold_days, hold_days):
        dt = score_series.index[i]
        dt_str = str(dt)[:10]

        # 取该日截面
        day_vals = {}
        for name in factor_vals:
            v = factor_vals[name]
            dv = v[v.index.get_level_values("datetime") == dt]
            if not dv.empty:
                for inst in dv.index.get_level_values("instrument").unique():
                    iv = dv[dv.index.get_level_values("instrument") == inst]
                    if not iv.empty:
                        day_vals.setdefault(inst, 0)
                        day_vals[inst] += iv.iloc[0] * weights.get(name, 0) / total_w

        if not day_vals:
            continue

        # 选Top-N
        ranked = sorted(day_vals.items(), key=lambda x: -x[1])
        top_codes = [c for c, _ in ranked[:top_n]]

        # 计算组合收益
        if dt in fwd.index:
            day_fwd = fwd.loc[dt]  # Series: instrument -> return
            returns = []
            for code in top_codes:
                if code in day_fwd.index:
                    r = day_fwd[code]
                    if pd.notna(r):
                        returns.append(float(r))
            if returns:
                port_ret = np.mean(returns)
                nav.append(nav[-1] * (1 + port_ret))
                nav_dates.append(dt_str)
                pick_log.append({"date": dt_str, "picks": top_codes[:3], "return": round(port_ret, 4)})

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
        "trades": len(nav) - 1,
        "picks": pick_log[:5],
    }
    return result


def print_result(r: dict):
    """格式化输出回测结果。"""
    if not r.get("ok"):
        print(f"  ERROR: {r.get('msg', 'unknown')}")
        return
    print(f"  期间: {r['period']}")
    print(f"  总收益: {r['total_return']:+.1%}  年化: {r['ann_return']:+.1%}")
    print(f"  最大回撤: {r['max_drawdown']:.1%}  夏普: {r['sharpe']:.2f}")
    print(f"  月度胜率: {r['monthly_winrate']:.0%}  交易次数: {r['trades']}")


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
