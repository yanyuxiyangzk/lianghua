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
                      top_n: int = 10, hold_days: int = 5,
                      mode: str = "research") -> dict:
    """对策略包做组合回测，返回完整指标。"""
    if any(isinstance(v, bool) or not isinstance(v, (int, np.integer)) or v <= 0
           for v in (top_n, hold_days)):
        return {"ok": False, "msg": "选股数量和持有期必须为正整数"}
    if mode not in ("research", "execution"):
        return {"ok": False, "msg": "mode 必须为 research 或 execution"}
    # 读策略包
    with library._lconn() as c:
        row = c.execute(
            "SELECT factors, method, pool_name FROM strategies WHERE name=?",
            (strategy_name,)).fetchone()
        registry = {r[0]: {"kind": r[1], "code": r[2], "factor_type": r[3]}
                    for r in c.execute("SELECT name,kind,code,factor_type FROM factor_registry").fetchall()}
    if not row or not row[0]:
        return {"ok": False, "msg": f"策略 {strategy_name} 无因子配置"}

    factors = library.resolve_strategy_factors(json.loads(row[0]), registry)
    names = [f.get('name') for f in factors]
    if any(not n for n in names) or len(set(names)) != len(names):
        return {"ok": False, "msg": "策略因子名称为空或重复，请修正策略配置"}
    method = row[1] or "ICIR加权"
    pool = row[2] or pool_name

    codes = all_pools().get(pool) or []
    if len(codes) < 30:
        return {"ok": False, "msg": f"池 {pool} 为空"}

    end = get_last_trade_day()
    start = trade_day_offset(end, -500)  # 回测窗口约2年

    # 取面板数据
    source = datasource.get_loop_source()
    panel = sig.get_panel_cached(codes, end, 600, source=source)
    fwd = fe.forward_returns(panel, hold_days) if mode == "research" else None

    # 取各因子值
    factor_vals = {}
    failures = []
    for fac in factors:
        try:
            vals = fe.get_factor_values(fac, codes, end, lookback_days=600, source=source)
            factor_vals[fac["name"]] = vals
        except Exception as exc:
            failures.append({"factor": fac['name'], "kind": fac.get('kind'),
                             "error_type": type(exc).__name__, "reason": str(exc)[:1000]})

    if len(factor_vals) != len(factors):
        detail = '；'.join(f"{f['factor']}：{f['error_type']} {f['reason']}" for f in failures)
        return {"ok": False, "msg": "部分因子求值失败，不能回测残缺策略。" + detail,
                "factor_errors": failures, "source": source}
    if not factor_vals:
        return {"ok": False, "msg": "无有效因子值"}

    # 构建权重 dict: {name: (weight, direction)}
    weights = {}
    for f in factors:
        if f["name"] in factor_vals:
            try:
                weight = float(f.get("weight", 1.0))
                direction = float(f.get("direction", 1))
            except (TypeError, ValueError, OverflowError):
                return {"ok": False, "msg": "因子权重或方向无效"}
            if not np.isfinite(weight) or weight < 0 or direction not in (-1, 1):
                return {"ok": False, "msg": "因子权重或方向无效"}
            weights[f["name"]] = (weight, direction)
    if not weights or not any(w > 0 for w, _ in weights.values()):
        return {"ok": False, "msg": "无有效权重"}

    # 预计算归一化因子值（z-score）
    vals_norm = {}
    for name, s in factor_vals.items():
        s2 = fe._norm(s.dropna())
        if s2.empty or not np.isfinite(s2.to_numpy(dtype=float)).all():
            return {"ok": False, "msg": f"因子 {name} 无有效有限值，不能回测残缺策略"}
        vals_norm[name] = s2

    # 归一化分派：pack 快照（factors 条目的 norm 键）> 全局开关自动映射；legacy → None
    norms = sig.scoring_norms(list(weights), factors)

    # 获取所有交易日
    # 持有期以行情交易日为准，不能让因子缺失日期改变回测时钟。
    all_dates = sorted(pd.to_datetime(panel.index.get_level_values("datetime")).unique())
    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)
    all_dates = [d for d in all_dates if start_dt <= pd.Timestamp(d) <= end_dt]

    if len(all_dates) < hold_days + 1:
        return {"ok": False, "msg": "回测窗口内数据不足"}

    if mode == "execution":
        return _execution_result(strategy_name, pool, method, factors, panel,
                                 vals_norm, weights, norms, all_dates, top_n, hold_days)

    # 逐日选股 + 计算收益
    nav = [1.0]
    nav_dates = [str(all_dates[0])[:10]]
    pick_log = []
    turnovers = []
    prev_picks = set()

    for i in range(0, len(all_dates) - hold_days, hold_days):
        dt = all_dates[i]
        dt_str = str(dt)[:10]

        for name, (weight, _) in weights.items():
            if weight > 0 and dt not in vals_norm[name].index.get_level_values("datetime"):
                return {"ok": False, "msg": f"调仓日因子 {name} 缺失，不能回测残缺策略"}

        # 截面打分：cs_norm 分派 × 权重 × 方向（与 walk_forward 一致；legacy 开关下为原 zscore）
        sc = fe._score_at(vals_norm, weights, dt, norms=norms)
        if sc.empty:
            return {"ok": False, "msg": "调仓日无有效因子评分，回测不完整"}

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
        if dt not in fwd.index:
            return {"ok": False, "msg": "选股日期未来收益缺失，回测不完整"}
        if dt in fwd.index:
            day_fwd = fwd.loc[dt]
            if not np.isfinite(day_fwd.reindex(top_codes).to_numpy(dtype=float)).all():
                return {"ok": False, "msg": "选中股票未来收益缺失，回测不完整"}
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
                nav_dates.append(str(all_dates[i + hold_days])[:10])
                pick_log.append({
                    "date": dt_str,
                    "picks": top_codes,
                    "end_date": str(all_dates[i + hold_days])[:10],
                    "gross": round(gross_ret, 4),
                    "net": round(net_ret, 4),
                    "turnover": round(turnover, 2),
                })

    if len(nav) < 2:
        return {"ok": False, "msg": "回测无有效交易"}

    from research_backtest import window_metrics
    try:
        metrics = window_metrics(pd.Series(nav).pct_change().dropna(),
                                 [r["date"] for r in pick_log],
                                 [r["end_date"] for r in pick_log], all_dates)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}
    total_return = metrics["total_return"]
    ann_return = metrics["ann_return"]
    max_dd = metrics["max_drawdown"]
    sharpe = metrics["sharpe"]
    monthly_wr = metrics["monthly_winrate"]

    avg_turnover = float(np.mean(turnovers)) if turnovers else 0

    from research_backtest import benchmark_returns, window_contract
    close = panel["$close"].unstack("instrument").sort_index()
    # 等权组合对照（与 walk_forward 一致）
    eq_nav = [1.0]
    for i in range(0, len(all_dates) - hold_days, hold_days):
        dt = all_dates[i]
        if dt in fwd.index:
            try:
                available_returns = benchmark_returns(close, fwd, dt)
            except ValueError as exc:
                return {"ok": False, "msg": str(exc)}
            eq_ret = float(available_returns.mean())
            eq_nav.append(eq_nav[-1] * (1 + eq_ret))
    eq_total = eq_nav[-1] / eq_nav[0] - 1 if len(eq_nav) > 1 else 0
    eq_ann = (1 + eq_total) ** (252 / metrics["elapsed_trading_days"]) - 1 if eq_total > -1 else -1

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
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "monthly_winrate": round(monthly_wr, 2),
        "avg_turnover": round(avg_turnover, 2),
        "trades": len(nav) - 1,
        "eq_ann_return": round(eq_ann, 4),
        "excess_ann_return": round(ann_return - eq_ann, 4),
        "picks": pick_log,
        "nav_dates": nav_dates,
        "nav": nav,
        "assessment_kind": "research_forward_returns_not_execution",
        "monthly_basis": "window_return_booked_at_maturity",
    }
    result.update(window_contract())
    return result


def _execution_result(strategy_name, pool, method, factors, panel, vals_norm,
                      weights, norms, all_dates, top_n, hold_days):
    """Independent execution path: never use forward returns or research metrics."""
    from historical_execution import simulate, performance
    records, picks = [], []
    try:
        for i in range(0, len(all_dates) - 1, hold_days):
            dt = pd.Timestamp(all_dates[i])
            for name, (weight, _) in weights.items():
                if weight > 0 and dt not in vals_norm[name].index.get_level_values("datetime"):
                    raise ValueError(f"调仓日因子 {name} 缺失，不能回测残缺策略")
            scores = fe._score_at(vals_norm, weights, dt, norms=norms)
            if scores.empty or not np.isfinite(scores.to_numpy(dtype=float)).all() or scores.index.has_duplicates:
                raise ValueError("调仓日评分为空、非有限值或股票重复")
            codes = list(scores.sort_values(ascending=False, kind="stable").index[:top_n])
            if len(codes) < top_n:
                raise ValueError("调仓日有效股票不足Top-N，不能缩减组合")
            day = dt.strftime("%Y-%m-%d")
            records.extend(dict(date=day, code=c, target_weight=1 / len(codes)) for c in codes)
            picks.append(dict(date=day, execution_date=str(all_dates[i + 1])[:10], picks=codes))
        fields = {"$open": "open", "$close": "close", "$volume": "volume",
                  "$limit_up": "limit_up", "$limit_down": "limit_down", "$suspended": "suspended"}
        if not {"$open", "$close"}.issubset(panel.columns):
            raise ValueError("缺少执行开盘或收盘价")
        prices = panel[[c for c in fields if c in panel.columns]].rename(columns=fields)
        prices = prices.reorder_levels(["datetime", "instrument"])
        dates = pd.to_datetime(prices.index.get_level_values("datetime"))
        prices = prices[(dates >= pd.Timestamp(all_dates[0])) & (dates <= pd.Timestamp(all_dates[-1]))].copy()
        prices.index = prices.index.set_names(["date", "code"])
        ledger = simulate(pd.DataFrame(records), prices)
        metrics = performance(ledger)
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return {"ok": False, "msg": f"执行回放失败: {exc}", "mode": "execution"}
    # Plain records are JSON-exportable and keep execution evidence separate.
    execution = {k: v.to_dict("records") if isinstance(v, pd.DataFrame) else v
                 for k, v in ledger.items()}
    return {"ok": True, "mode": "execution", "strategy": strategy_name, "pool": pool,
            "factors": len(factors), "method": method, **metrics,
            "period": f"{metrics['nav_dates'][0]} ~ {metrics['nav_dates'][-1]}",
            "trades": len(ledger["fills"]), "rebalances": len(picks),
            "rejected_orders": int((ledger["orders"].status == "rejected").sum()),
            "picks": picks, "cash": ledger["cash"], "equity": float(ledger["equity"].iloc[-1].equity),
            "execution": execution, "assessment_kind": ledger["assessment_kind"],
            "data_quality_status": ledger["data_quality_status"],
            "data_issues": ledger["data_issues"],
            "calculation_contract": ledger["calculation_contract"],
            "execution_limitations": ledger["limitations"],
            "benchmark_status": "execution_benchmark_not_implemented"}


def print_result(r: dict):
    """格式化输出回测结果。"""
    if not r.get("ok"):
        print(f"  ERROR: {r.get('msg', 'unknown')}")
        return
    print(f"  期间: {r['period']}")
    print(f"  总收益: {r['total_return']:+.1%}  年化: {r['ann_return']:+.1%}")
    if "excess_ann_return" in r:
        print(f"  超额年化: {r['excess_ann_return']:+.1%}")
    if r.get("mode") == "execution":
        print(f"  成交笔数: {r['trades']}  拒单: {r['rejected_orders']}")
        print(f"  限制: {r['execution_limitations']}")
    sharpe_label = f"{r['sharpe']:.2f}" if r["sharpe"] is not None else "不适用"
    print(f"  最大回撤: {r['max_drawdown']:.1%}  夏普: {sharpe_label}")
    print(f"  月胜率: {r['monthly_winrate']:.0%}  换手率: {r.get('avg_turnover',0):.0%}  交易次数: {r['trades']}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="策略组合回测")
    parser.add_argument("--strategy", help="策略名称")
    parser.add_argument("--all", action="store_true", help="回测所有策略")
    parser.add_argument("--pool", default="沪深300", help="股票池")
    parser.add_argument("--top-n", type=int, default=10, help="Top-N选股")
    parser.add_argument("--hold-days", type=int, default=5, help="持有天数")
    parser.add_argument("--mode", choices=["research", "execution"], default="research", help="研究收益或历史日频执行回放")
    args = parser.parse_args()

    if args.all:
        with library._lconn() as c:
            names = [r[0] for r in c.execute("SELECT name FROM strategies").fetchall()]
        for name in names:
            print(f"\n{'='*50}")
            print(f"策略: {name}")
            r = backtest_strategy(name, args.pool, args.top_n, args.hold_days, mode=args.mode)
            print_result(r)
    elif args.strategy:
        r = backtest_strategy(args.strategy, args.pool, args.top_n, args.hold_days, mode=args.mode)
        print_result(r)
    else:
        print("请指定 --strategy 或 --all")
