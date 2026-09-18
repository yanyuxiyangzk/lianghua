"""🎲 卫星轨 · 事件策略

独立仓位、独立结算、独立净值。Top5 候选 → 剔除涨停/追高 → 规则/LLM 决策 → 真实资金下单。
"""

import json
import pandas as pd
import streamlit as st

import experience as exp
import broker as bk
import library
from common import get_last_trade_day

# 卫星轨常量
LIMIT_UP_THRESHOLD = 0.999  # 涨停判断阈值（相对涨停价）
CHASE_HIGH_THRESHOLD = 15.0  # 追高阈值（涨幅百分比）
SL_SAMPLE_SIZE = 200  # 止损记录样本量


def _get_satellite_pack_info() -> dict | None:
    """获取卫星轨策略包信息。"""
    packs = library.list_strategies()
    for name in packs:
        if "卫星" in name:
            return {"name": name, **packs[name]}
    # fallback: 涨停/事件
    for name in packs:
        if "涨停" in name or "事件" in name:
            return {"name": name, **packs[name]}
    return None


def _render_pack_info(pack: dict):
    """渲染策略包信息卡片。"""
    st.caption(f"策略包: **{pack['name']}** | 股票池: {pack.get('pool_name', '沪深300')} | "
               f"Top-N: {pack.get('top_n', 5)} | 加权: {pack.get('method', 'ICIR加权')}")
    oos = pack.get("oos_winrate")
    if oos:
        st.caption(f"OOS胜率: {oos}")
    factors = pack.get("factors", [])
    if factors:
        rows = []
        for f in factors:
            rows.append({
                "因子": f["name"],
                "权重": f"{f['weight']:.2%}",
                "方向": "正向" if f["direction"] == 1 else "反向",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _get_todays_satellite_pick() -> pd.DataFrame:
    """取今日卫星轨选股结果（从经验库）。"""
    try:
        with exp._conn() as c:
            df = pd.read_sql(
                "SELECT p.trade_date, pi.code, pi.score FROM picks p"
                " JOIN pick_items pi ON pi.pick_id = p.id"
                " WHERE p.source='satellite_scan' AND p.trade_date=(SELECT MAX(trade_date) FROM picks WHERE source='satellite_scan')"
                " ORDER BY pi.rank", c)
        return df
    except Exception:
        return pd.DataFrame()


def _render_tab_today():
    """Tab 1: 今日决策"""
    today = get_last_trade_day()
    st.caption(f"交易日 {today}")

    # 策略包信息
    pack = _get_satellite_pack_info()
    if pack:
        _render_pack_info(pack)

    # 今日选股结果
    picks = _get_todays_satellite_pick()
    if picks.empty:
        st.info("今日卫星轨无选股结果，请等待 19:10 扫描完成")
        return

    # 批量获取价格（减少API调用）
    codes = picks["code"].tolist()
    try:
        all_prices = bk._latest_prices(codes)
    except Exception as e:
        st.warning(f"获取价格失败: {e}")
        all_prices = {}

    # 剔除涨停/追高后的候选
    eligible = []
    for _, row in picks.iterrows():
        code = row["code"]
        pr = all_prices.get(code)
        if pr is None:
            continue
        try:
            cur = pr[0]
            prev_close = pr[1] if len(pr) > 1 else None
            limit_up = pr[3] if len(pr) > 3 else None
            chg = ((cur / prev_close - 1) * 100) if prev_close and prev_close > 0 else None
            is_lu = limit_up and cur >= limit_up * LIMIT_UP_THRESHOLD
            is_chase = chg is not None and pd.notna(chg) and chg > CHASE_HIGH_THRESHOLD
            if not is_lu and not is_chase:
                eligible.append({"code": code, "score": row["score"],
                                 "price": cur, "change_pct": chg})
        except Exception as e:
            st.warning(f"处理 {code} 价格时出错: {e}")
            continue

    if not eligible:
        st.warning("全部候选被剔除（涨停/追高）")
        return

    st.subheader(f"合格候选 {len(eligible)} 只")
    df_elig = pd.DataFrame(eligible)
    st.dataframe(df_elig, use_container_width=True, hide_index=True)

    # 当前持仓（含 LLM 决策信息）
    st.subheader("当前持仓")
    opens = exp.satellite_positions("open")
    if opens.empty:
        st.info("暂无持仓")
    else:
        for _, p in opens.iterrows():
            try:
                pr = bk._latest_prices([p["code"]]).get(p["code"])
                cur = pr[0] if pr and pr[0] else p["buy_price"]
                pnl = (cur - p["buy_price"]) * int(p["buy_shares"])
                pnl_pct = (cur / p["buy_price"] - 1) if p["buy_price"] else 0
                color = "🟢" if pnl >= 0 else "🔴"

                # LLM 决策信息
                llm_info = ""
                if pd.notna(p.get("llm_conviction")) and p["llm_conviction"]:
                    llm_info = f" | 🤖置信度 {p['llm_conviction']:.2f}"
                if p.get("llm_reason"):
                    llm_info += f" | {p['llm_reason'][:50]}"

                st.write(f"{color} **{p['code']}** {p.get('name','')} | "
                         f"买入 {p['buy_price']:.2f} × {int(p['buy_shares'])}股 | "
                         f"现价 {cur:.2f} | 盈亏 {pnl:+,.0f}元 ({pnl_pct:+.2%})"
                         f"{llm_info}")
            except Exception:
                st.write(f"⚪ **{p['code']}** {p.get('name','')} | 买入 {p['buy_price']:.2f}")

    # pending 挂单
    pendings = exp.satellite_positions("pending")
    if not pendings.empty:
        st.subheader("挂单中")
        for _, p in pendings.iterrows():
            llm_info = ""
            if pd.notna(p.get("llm_conviction")) and p["llm_conviction"]:
                llm_info = f" | 🤖{p['llm_conviction']:.2f}"
            st.write(f"⏳ **{p['code']}** {p.get('name','')} | "
                     f"限价 {p['limit_price']:.2f} × {int(p['buy_shares'])}股"
                     f"{llm_info}")


def _render_tab_performance():
    """Tab 2: 战绩统计"""
    st.subheader("净值曲线")
    nav = exp.satellite_nav_history(60)
    if nav.empty:
        st.info("暂无净值数据")
    else:
        nav = nav.sort_values("date")
        st.line_chart(nav.set_index("date")["nav"])

    st.subheader("战绩统计")
    lb = exp.satellite_leaderboard()
    if lb.empty:
        st.info("暂无战果数据")
    else:
        st.dataframe(lb, use_container_width=True, hide_index=True)

    # 总览
    nav_now = exp.satellite_nav_history(1)
    if not nav_now.empty:
        row = nav_now.iloc[0]
        cols = st.columns(4)
        cols[0].metric("当前净值", f"{row['nav']:.0f}元")
        cols[1].metric("累计收益", f"{row['cumulative_return']:+.2%}")
        cols[2].metric("最大回撤", f"{row['max_drawdown']:+.2%}")
        cols[3].metric("持仓市值", f"{row['positions_value']:.0f}元")


def _render_tab_history():
    """Tab 3: 历史决策"""
    st.subheader("已平仓记录")
    outcomes = exp.satellite_outcomes(100)
    if outcomes.empty:
        st.info("暂无已平仓记录")
    else:
        # 筛选条件（移到顶部）
        col1, col2 = st.columns(2)
        with col1:
            exit_reasons = ["全部"] + sorted(outcomes["exit_reason"].dropna().unique().tolist())
            selected_reason = st.selectbox("退出原因", exit_reasons)
        with col2:
            min_hold = st.number_input("最少持有天数", min_value=0, max_value=30, value=0)

        # 应用筛选
        filtered = outcomes.copy()
        if selected_reason != "全部":
            filtered = filtered[filtered["exit_reason"] == selected_reason]
        if min_hold > 0:
            filtered = filtered[filtered["hold_days"] >= min_hold]

        # 显示数据
        display = filtered[["code", "name", "buy_date", "buy_price",
                            "sell_date", "sell_price", "pnl", "pnl_pct",
                            "hold_days", "exit_reason"]].copy()
        display.columns = ["代码", "名称", "买入日", "买入价",
                           "卖出日", "卖出价", "盈亏(元)", "盈亏(%)",
                           "持有天数", "退出原因"]
        st.dataframe(display, use_container_width=True, hide_index=True)

        # 统计摘要
        if not filtered.empty:
            st.caption(f"共 {len(filtered)} 笔 | 平均持有 {filtered['hold_days'].mean():.1f} 天 | "
                       f"胜率 {(filtered['pnl_pct'] > 0).mean():.0%}")

    st.subheader("过期/未成交")
    expired = exp.satellite_positions("expired")
    if not expired.empty:
        st.dataframe(expired[["code", "name", "buy_date", "created_at"]],
                     use_container_width=True, hide_index=True)


def _render_tab_risk():
    """Tab 4: 风控"""
    st.subheader("可用资金")
    try:
        cash = bk._get_cash()
        st.metric("可用资金", f"{cash:,.0f}元")
    except Exception as e:
        st.warning(f"无法读取资金账号: {e}")

    st.subheader("持仓明细")
    opens = exp.satellite_positions("open")
    if opens.empty:
        st.info("暂无持仓")
    else:
        total_value = 0.0
        total_cost = 0.0
        for _, p in opens.iterrows():
            try:
                pr = bk._latest_prices([p["code"]]).get(p["code"])
                cur = pr[0] if pr and pr[0] else p["buy_price"]
                val = cur * int(p["buy_shares"])
                total_value += val
                total_cost += p["buy_price"] * int(p["buy_shares"])
                pnl_pct = (cur / p["buy_price"] - 1) if p["buy_price"] else 0
                st.write(f"**{p['code']}** {p['name']} | "
                         f"买入 {p['buy_price']:.2f} × {int(p['buy_shares'])}股 | "
                         f"现价 {cur:.2f} | {pnl_pct:+.2%}")
            except Exception as e:
                st.warning(f"获取 {p['code']} 价格失败: {e}")
                st.write(f"**{p['code']}** {p['name']}")

        if total_value > 0:
            st.metric("总持仓市值", f"{total_value:,.0f}元")
            st.metric("持仓成本", f"{total_cost:,.0f}元")

    st.subheader("止损触发记录")
    outcomes = exp.satellite_outcomes(SL_SAMPLE_SIZE)  # 使用常量
    if not outcomes.empty:
        sl_records = outcomes[outcomes["exit_reason"] == "止损"]
        if not sl_records.empty:
            # 止损统计
            total_sl = len(sl_records)
            avg_sl_pct = sl_records["pnl_pct"].mean()
            st.caption(f"共 {total_sl} 笔止损 | 平均亏损 {avg_sl_pct:+.2%}")
            # 显示完整字段
            display_cols = ["code", "name", "buy_date", "buy_price", "buy_shares",
                           "sell_date", "sell_price", "pnl", "pnl_pct", "hold_days",
                           "llm_conviction", "exit_reason"]
            available_cols = [col for col in display_cols if col in sl_records.columns]
            st.dataframe(sl_records[available_cols], use_container_width=True, hide_index=True)
        else:
            st.info("暂无止损记录")
    else:
        st.info("暂无记录")


def render():
    st.title("🎲 卫星轨 · 事件策略")

    tab1, tab2, tab3, tab4 = st.tabs([
        "📋 今日决策", "📊 战绩统计", "📝 历史决策", "⚠️ 风控"])

    with tab1:
        _render_tab_today()
    with tab2:
        _render_tab_performance()
    with tab3:
        _render_tab_history()
    with tab4:
        _render_tab_risk()


render()
