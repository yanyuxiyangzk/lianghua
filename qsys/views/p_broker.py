"""💹 普通交易（模拟柜台）：持仓 / 买入 / 卖出 / 撤单 / 查询。

初始资金 100000 元，A股规则（T+1、100股整手、佣金万2.5最低5元、印花税卖出0.05%）。
行情用 ifind_realtime 最新快照（同花顺 iFinD，盘中 5 分钟一批）。
持仓页合并展示：买入页下单 = 手动买入，每日名单自动开仓 = AI买入。
"""

from datetime import datetime

import pandas as pd
import streamlit as st

import broker

UP, DOWN = "#e54545", "#26a69a"


def _money(v) -> str:
    return f"{v:,.2f}" if v is not None and pd.notna(v) else "-"


def _pnl(v) -> str:
    if v is None or pd.notna(v) is False:
        return "-"
    return f"{'+' if v >= 0 else ''}{v:,.2f}"


def _hold_days(d0: str) -> int:
    """两日期间隔交易日数（工作日近似）。"""
    if not d0:
        return 0
    d, n = pd.Timestamp(str(d0)), 0
    today = pd.Timestamp(datetime.now().strftime("%Y-%m-%d"))
    while d < today and n < 60:
        d += pd.Timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _merged_positions() -> pd.DataFrame:
    """合并展示：柜台手动持仓 + AI 自动跟踪持仓（列对齐，含类型/止盈/止损）。"""
    import experience
    rows = []
    poss = broker.get_positions()
    for _, p in poss.iterrows():
        if p["source"] != "manual":
            continue  # AI 持仓以经验库 positions 为准（含每日批次明细），柜台 ai 行仅作资金台账
        tp = p["tp_price"] if pd.notna(p["tp_price"]) else (
            p["cost"] * (1 + broker.TP_RATE) if pd.notna(p["cost"]) else None)
        sl = p["sl_price"] if pd.notna(p["sl_price"]) else (
            p["cost"] * (1 - broker.SL_RATE) if pd.notna(p["cost"]) else None)
        cur = p["最新价"] if pd.notna(p["最新价"]) else None
        rows.append({
            "code": p["code"], "name": p["name"], "类型": "手动买入",
            "buy_ts": str(p["last_buy_date"]) if p["last_buy_date"] else "",
            "shares": int(p["shares"] or 0), "可卖": int(p["sellable"] or 0),
            "cost": p["cost"], "最新价": cur,
            "市值": (cur or p["cost"]) * (p["shares"] or 0),
            "浮动盈亏%": ((cur / p["cost"] - 1) * 100) if cur and p["cost"] else None,
            "盈亏额": ((cur - p["cost"]) * p["shares"]) if cur and p["cost"] else None,
            "持有交易日": _hold_days(str(p["last_buy_date"])),
            "止盈价": tp, "止损价": sl, "来源": "-",
        })
    try:
        autos = experience.get_open_positions()
    except Exception:
        autos = pd.DataFrame()
    for _, a in autos.iterrows():
        rows.append({
            "code": a["code"], "name": a["name"], "类型": "AI买入",
            "buy_ts": str(a["buy_ts"] or a["buy_date"]),
            "shares": int(a["shares"] or 0), "可卖": int(a["可卖(股)"] or 0),
            "cost": a["buy_price"], "最新价": a["最新价"],
            "市值": a["最新价"] * (a["shares"] or 0) if pd.notna(a["最新价"]) else None,
            "浮动盈亏%": a["浮动盈亏%"], "盈亏额": a["浮动盈亏额"],
            "持有交易日": a["持有交易日"],
            "止盈价": a["止盈价"], "止损价": a["止损价"],
            "来源": str(a["pack_name"] or a["source"]),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _position_rows() -> pd.DataFrame:
    """卖出页统一持仓列表：手动（柜台）+ AI（经验库，T+1 可卖校验）。"""
    import experience
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    poss = broker.get_positions()
    for _, p in poss.iterrows():
        if p["source"] != "manual":
            continue
        rows.append({"key": f"m|{p['code']}", "code": p["code"], "name": p["name"],
                     "sellable": int(p["sellable"] or 0), "source": "manual", "pos_id": None})
    try:
        autos = experience.get_open_positions()
    except Exception:
        autos = pd.DataFrame()
    for _, a in autos.iterrows():
        sellable = int(a["shares"] or 0) if str(a["buy_date"]) < today else 0
        rows.append({"key": f"a|{a['id']}", "code": a["code"], "name": a["name"],
                     "sellable": sellable, "source": "ai", "pos_id": int(a["id"])})
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def render():
    st.title("💹 资金账号（模拟柜台）")

    acc = broker.get_account()

    # 账户总览
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("总资产", _money(acc["总资产"]))
    c2.metric("可用资金", _money(acc["可用资金"]))
    c3.metric("持仓市值", _money(acc["持仓市值"]))
    c4.metric("持仓盈亏", _pnl(acc["持仓盈亏"]))
    c5.metric("今日盈亏", _pnl(acc["今日盈亏"]))
    st.caption("初始资金 100,000 元 · 佣金万2.5(最低5元)双边 · 印花税0.05%仅卖出 · T+1 · 100股整手")

    tab_pos, tab_buy, tab_sell, tab_cancel, tab_query, tab_calendar = st.tabs(
        ["💼 持仓", "🛒 买入", "💰 卖出", "❌ 撤单", "🔍 查询", "📅 收益日历"])

    # ---------------------------------------------------------------- 持仓（手动 + AI 合并）
    with tab_pos:
        merged = _merged_positions()
        if merged.empty:
            st.info("暂无持仓——「🛒 买入」页下单为手动买入，每日名单盘中自动开仓为 AI买入")
        else:
            show = pd.DataFrame({
                "代码": merged["code"], "名称": merged["name"], "类型": merged["类型"],
                "买入时间": merged["buy_ts"],
                "持仓(股)": merged["shares"], "可卖(股)": merged["可卖"],
                "成本价": merged["cost"].map(lambda x: round(x, 3) if pd.notna(x) else "-"),
                "最新价": merged["最新价"].map(lambda x: round(x, 2) if pd.notna(x) else "-"),
                "市值": merged["市值"].map(lambda x: f"{x:,.0f}" if pd.notna(x) else "-"),
                "浮动盈亏": merged["浮动盈亏%"].map(lambda x: f"{x:+.2f}%" if pd.notna(x) else "-"),
                "盈亏(元)": merged["盈亏额"].map(lambda x: f"{x:+,.0f}" if pd.notna(x) else "-"),
                "持有(交易日)": merged["持有交易日"],
                "止盈价": merged["止盈价"].map(lambda x: round(x, 2) if pd.notna(x) else "-"),
                "止损价": merged["止损价"].map(lambda x: round(x, 2) if pd.notna(x) else "-"),
                "来源": merged["来源"],
            })
            st.dataframe(show, hide_index=True, width='stretch')
            st.caption("止盈/止损随实盘价滚动触发自动卖出（手动 +15%/-8% · AI +15%/-8%/满20交易日）"
                       " · 两种类型均可在「💰 卖出」页手动卖出（T+1）")

    # ---------------------------------------------------------------- 买入
    with tab_buy:
        _order_form("buy")

    # ---------------------------------------------------------------- 卖出（手动 + AI 统一）
    with tab_sell:
        rows = _position_rows()
        if rows.empty:
            st.info("暂无持仓可卖")
        elif (rows["sellable"] <= 0).all():
            st.info("持仓均为当日买入（T+1，次日可卖）")
        else:
            _order_form("sell", rows[rows["sellable"] > 0])

    # ---------------------------------------------------------------- 撤单
    with tab_cancel:
        orders = broker.list_orders(today_only=False)
        pending = orders[orders["status"] == "已报"]
        if pending.empty:
            st.info("当前没有挂单")
        else:
            show = pending[["id", "ts", "code", "name", "side", "price", "shares", "status"]].rename(
                columns={"id": "委托号", "ts": "时间", "code": "代码", "name": "名称",
                         "side": "方向", "price": "限价", "shares": "数量", "status": "状态"})
            show["方向"] = show["方向"].map({"buy": "买入", "sell": "卖出"})
            st.dataframe(show, hide_index=True, width='stretch')
            sel = st.selectbox("选择要撤销的委托", pending["id"].tolist(),
                               format_func=lambda i: (
                                   f"#{i} {pending[pending['id']==i]['name'].iloc[0]}"
                                   f" {'买' if pending[pending['id']==i]['side'].iloc[0]=='buy' else '卖'}"
                                   f" {int(pending[pending['id']==i]['shares'].iloc[0])}股"
                                   f" @ {pending[pending['id']==i]['price'].iloc[0]:.2f}"),
                               key="cancel_sel")
            if st.button("❌ 撤销该委托", type="primary", key="cancel_go"):
                st.success(broker.cancel_order(int(sel)))
                st.rerun()

    # ---------------------------------------------------------------- 查询
    with tab_query:
        q1, q2, q3 = st.tabs(["当日委托", "当日成交", "资金流水"])
        with q1:
            orders = broker.list_orders(today_only=True)
            if orders.empty:
                st.info("今日无委托")
            else:
                show = orders[["ts", "code", "name", "source", "side", "price", "shares", "status",
                               "filled_price", "filled_ts"]].rename(
                    columns={"ts": "委托时间", "code": "代码", "name": "名称", "source": "类型",
                             "side": "方向", "price": "限价", "shares": "数量", "status": "状态",
                             "filled_price": "成交价", "filled_ts": "成交时间"})
                show["方向"] = show["方向"].map({"buy": "买入", "sell": "卖出"})
                show["类型"] = show["类型"].map({"ai": "AI", "manual": "手动"}).fillna("手动")
                st.dataframe(show, hide_index=True, width='stretch')
        with q2:
            fills = broker.list_fills(today_only=True)
            if fills.empty:
                st.info("今日无成交")
            else:
                show = fills[["ts", "code", "name", "source", "side", "price", "shares", "amount",
                              "fee", "tax"]].rename(
                    columns={"ts": "成交时间", "code": "代码", "name": "名称", "source": "类型",
                             "side": "方向", "price": "成交价", "shares": "数量", "amount": "成交金额",
                             "fee": "佣金", "tax": "印花税"})
                show["方向"] = show["方向"].map({"buy": "买入", "sell": "卖出"})
                show["类型"] = show["类型"].map({"ai": "AI", "manual": "手动"}).fillna("手动")
                st.dataframe(show, hide_index=True, width='stretch')
        with q3:
            flows = broker.list_cashflows()
            if flows.empty:
                st.info("暂无资金流水")
            else:
                show = flows[["ts", "type", "amount", "balance", "note"]].rename(
                    columns={"ts": "时间", "type": "类型", "amount": "发生金额",
                             "balance": "余额", "note": "摘要"})
                st.dataframe(show, hide_index=True, width='stretch')

    # ---------------------------------------------------------------- 收益日历
    with tab_calendar:
        _render_calendar_tab()


def _get_daily_returns() -> pd.DataFrame:
    """计算每日收益率：基于资金流水的余额变化。"""
    import sqlite3
    from pathlib import Path
    
    db_path = Path("/data/experience.db")
    if not db_path.exists():
        return pd.DataFrame()
    
    try:
        with sqlite3.connect(str(db_path), timeout=30) as c:
            c.execute("PRAGMA busy_timeout=30000")
            flows = pd.read_sql(
                "SELECT ts, type, amount, balance FROM broker_cashflows ORDER BY ts", c)
    except Exception:
        return pd.DataFrame()
    
    if flows.empty:
        return pd.DataFrame()
    
    # 解析日期
    flows["date"] = pd.to_datetime(flows["ts"]).dt.date
    flows["balance"] = flows["balance"].astype(float)
    
    # 每日结束时的余额（取每天最后一次交易后的余额）
    daily_balance = flows.groupby("date")["balance"].last().reset_index()
    daily_balance.columns = ["date", "end_balance"]
    daily_balance["date"] = pd.to_datetime(daily_balance["date"])
    
    # 计算日收益率
    daily_balance["prev_balance"] = daily_balance["end_balance"].shift(1)
    daily_balance["return_pct"] = (daily_balance["end_balance"] / daily_balance["prev_balance"] - 1) * 100
    daily_balance = daily_balance.dropna(subset=["return_pct"])
    
    return daily_balance


def _render_calendar_tab():
    """渲染收益日历标签页。"""
    import plotly.graph_objects as go
    import plotly.express as px
    from plotly.subplots import make_subplots
    
    daily_returns = _get_daily_returns()
    
    if daily_returns.empty:
        st.info("暂无收益数据——完成交易后将自动生成收益日历")
        return
    
    # 按月分组
    daily_returns["year_month"] = daily_returns["date"].dt.to_period("M")
    daily_returns["day"] = daily_returns["date"].dt.day
    daily_returns["month"] = daily_returns["date"].dt.month
    daily_returns["year"] = daily_returns["date"].dt.year
    
    # 选择显示模式
    col1, col2 = st.columns([1, 3])
    with col1:
        view_mode = st.radio("显示模式", ["日收益", "月收益"], key="calendar_mode")
        if view_mode == "月收益":
            # 月度收益率
            monthly = daily_returns.groupby("year_month").agg(
                total_return=("return_pct", "sum"),
                trading_days=("return_pct", "count"),
                win_days=("return_pct", lambda x: (x > 0).sum()),
                avg_return=("return_pct", "mean")
            ).reset_index()
            monthly["year_month_str"] = monthly["year_month"].astype(str)
            monthly["win_rate"] = (monthly["win_days"] / monthly["trading_days"] * 100).round(1)
            
            st.metric("累计收益率", f"{daily_returns['return_pct'].sum():.2f}%")
            st.metric("月均收益率", f"{monthly['total_return'].mean():.2f}%")
            st.metric("交易天数", f"{len(daily_returns)}")
            
            # 月度收益柱状图
            fig_monthly = px.bar(monthly, x="year_month_str", y="total_return",
                                color="total_return",
                                color_continuous_scale=["#ef5350", "#26a69a"],
                                title="月度收益率",
                                labels={"total_return": "收益率(%)", "year_month_str": "月份"})
            fig_monthly.update_layout(height=400, showlegend=False)
            st.plotly_chart(fig_monthly, use_container_width=True)
            
            # 月度胜率
            fig_wr = px.bar(monthly, x="year_month_str", y="win_rate",
                           title="月度交易日胜率",
                           labels={"win_rate": "胜率(%)", "year_month_str": "月份"})
            fig_wr.update_layout(height=300)
            st.plotly_chart(fig_wr, use_container_width=True)
        else:
            # 日度收益率
            st.metric("累计收益率", f"{daily_returns['return_pct'].sum():.2f}%")
            st.metric("日均收益率", f"{daily_returns['return_pct'].mean():.2f}%")
            st.metric("最大单日涨幅", f"{daily_returns['return_pct'].max():.2f}%")
            st.metric("最大单日跌幅", f"{daily_returns['return_pct'].min():.2f}%")
            
            # 日收益K线图
            fig_daily = go.Figure(data=[go.Candlestick(
                x=daily_returns["date"],
                open=daily_returns["prev_balance"],
                high=daily_returns["end_balance"],
                low=daily_returns["end_balance"],
                close=daily_returns["end_balance"],
                increasing_line_color="#ef5350",
                decreasing_line_color="#26a69a",
                name="账户余额"
            )])
            fig_daily.update_layout(
                title="日度账户余额变化",
                xaxis_title="日期",
                yaxis_title="账户余额",
                height=400,
                xaxis_rangeslider_visible=False
            )
            st.plotly_chart(fig_daily, use_container_width=True)
            
            # 日收益率柱状图
            colors = ["#ef5350" if r >= 0 else "#26a69a" for r in daily_returns["return_pct"]]
            fig_bar = go.Figure(data=[go.Bar(
                x=daily_returns["date"],
                y=daily_returns["return_pct"],
                marker_color=colors,
                name="日收益率"
            )])
            fig_bar.update_layout(
                title="日度收益率",
                xaxis_title="日期",
                yaxis_title="收益率(%)",
                height=300
            )
            st.plotly_chart(fig_bar, use_container_width=True)
    
    with col2:
        # 收益日历热力图
        st.markdown("###   收益日历")
        
        # 选择月份
        months = sorted(daily_returns["year_month"].unique(), reverse=True)
        selected_month = st.selectbox("选择月份", months, key="calendar_month")
        
        month_data = daily_returns[daily_returns["year_month"] == selected_month].copy()
        
        if not month_data.empty:
            # 创建日历网格
            year = month_data["date"].dt.year.iloc[0]
            month = month_data["date"].dt.month.iloc[0]
            
            # 创建月份日历
            import calendar
            cal = calendar.monthcalendar(year, month)
            
            # 构建热力图数据
            z_data = []
            text_data = []
            for week in cal:
                row_z = []
                row_text = []
                for day in week:
                    if day == 0:
                        row_z.append(None)
                        row_text.append("")
                    else:
                        ret = month_data[month_data["day"] == day]["return_pct"]
                        if len(ret) > 0:
                            row_z.append(ret.iloc[0])
                            row_text.append(f"{ret.iloc[0]:+.2f}%")
                        else:
                            row_z.append(0)
                            row_text.append("休市")
                z_data.append(row_z)
                text_data.append(row_text)
            
            # 绘制热力图
            fig_heat = go.Figure(data=go.Heatmap(
                z=z_data,
                text=text_data,
                texttemplate="%{text}",
                textfont={"size": 14},
                colorscale=["#26a69a", "#ffffff", "#ef5350"],
                colorbar=dict(title="收益率(%)"),
                hovertemplate="日期: %{text}<br>收益率: %{z:.2f}%<extra></extra>"
            ))
            
            fig_heat.update_layout(
                title=f"{year}年{month}月 收益日历",
                xaxis=dict(
                    tickvals=[0, 1, 2, 3, 4],
                    ticktext=["周一", "周二", "周三", "周四", "周五"]
                ),
                yaxis=dict(
                    tickvals=list(range(len(cal))),
                    ticktext=[f"第{i+1}周" for i in range(len(cal))]
                ),
                height=350
            )
            st.plotly_chart(fig_heat, use_container_width=True)
            
            # 月度统计
            total_ret = month_data["return_pct"].sum()
            win_days = (month_data["return_pct"] > 0).sum()
            total_days = len(month_data)
            avg_ret = month_data["return_pct"].mean()
            
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("月度收益", f"{total_ret:+.2f}%")
            c2.metric("交易天数", f"{total_days}")
            c3.metric("上涨天数", f"{win_days}")
            c4.metric("胜率", f"{win_days/total_days*100:.1f}%")
        else:
            st.info(f"{selected_month} 暂无交易数据")


def _sell_label(sellable: pd.DataFrame, k: str) -> str:
    r = sellable[sellable["key"] == k].iloc[0]
    typ = "AI" if r["source"] == "ai" else "手动"
    return f"{r['code']} {r['name']}（{typ} · 可卖 {int(r['sellable'])} 股）"


def _order_form(side: str, sellable: pd.DataFrame | None = None):
    """买入/卖出表单。卖出时 sellable 传可卖持仓（key/source/pos_id）。"""
    is_buy = side == "buy"
    acc = broker.get_account()

    if is_buy:
        code = st.text_input("股票代码", key="buy_code",
                             placeholder="如 600519 / SH600519 / 600519.SH")
        code_norm = _norm_code(code)
        sel, is_ai = None, False
    else:
        keys = sellable["key"].tolist()
        sel = st.selectbox("选择持仓", keys,
                           format_func=lambda k: _sell_label(sellable, k), key="sell_code")
        row = sellable[sellable["key"] == sel].iloc[0]
        code_norm = row["code"]
        is_ai = row["source"] == "ai"

    name = broker.get_name(code_norm) if code_norm else ""
    pr = broker._latest_prices([code_norm]).get(code_norm) if code_norm else None
    cur = pr[0] if pr else None

    c1, c2 = st.columns(2)
    with c1:
        if is_ai:
            price = 0.0
            st.caption("AI 持仓按最新价市价卖出")
        else:
            price = st.number_input("委托价（0=市价，按最新价立即成交）", min_value=0.0,
                                    value=0.0, step=0.01, key=f"{side}_price",
                                    help=f"最新价 {cur:.2f}" if cur else "暂无行情")
        if cur:
            st.caption(f"最新价：**{cur:.2f}**（{name or '未知名称'}）")
    with c2:
        if is_buy:
            max_shares = int(acc["可用资金"] // (cur * 100) * 100) if cur else 0
            shares = st.number_input("买入数量（股，100 整数倍）", min_value=100,
                                     max_value=max(100, max_shares), value=min(100, max(100, max_shares)),
                                     step=100, key="buy_shares",
                                     help=f"可用资金 {acc['可用资金']:,.2f} 元，约可买 {max_shares} 股")
        else:
            mx = int(sellable[sellable["key"] == sel]["sellable"].iloc[0])
            shares = st.number_input("卖出数量（股）", min_value=1, max_value=max(1, mx),
                                     value=mx, step=1, key="sell_shares",
                                     help=f"可卖 {mx} 股（T+1）")

    est = (cur or 0) * shares
    fee = max(broker.FEE_MIN, est * broker.FEE_RATE)
    tax = est * broker.TAX_RATE if not is_buy else 0.0
    st.caption(f"预计{'占用' if is_buy else '回笼'}资金：{est + fee + tax if is_buy else est - fee - tax:,.2f} 元"
               f"（{'含佣金' if is_buy else '扣佣金和印花税'}）")

    if st.button("🛒 买入下单" if is_buy else "💰 卖出下单", type="primary", key=f"{side}_go"):
        if not code_norm:
            st.error("请输入股票代码")
        else:
            if is_ai:
                import experience
                msg = experience.manual_sell(int(sellable[sellable["key"] == sel]["pos_id"].iloc[0]),
                                             int(shares))
            else:
                msg = broker.place_order(code_norm, side, price or None, shares, source="manual")
            if "已成交" in msg or "已挂单" in msg:
                st.success(msg)
            else:
                st.error(msg)
            st.rerun()


def _norm_code(raw: str) -> str:
    raw = (raw or "").strip().upper()
    import re
    if re.match(r"^(SH|SZ|BJ)\d{6}$", raw):
        return raw
    if re.match(r"^\d{6}\.(SH|SZ|BJ)$", raw):
        m = re.match(r"^(\d{6})\.(SH|SZ|BJ)$", raw)
        return f"{m.group(2)}{m.group(1)}"
    if re.match(r"^\d{6}$", raw):
        if raw.startswith("6"):
            return "SH" + raw
        if raw.startswith(("4", "8", "920")):
            return "BJ" + raw
        return "SZ" + raw
    return raw


render()
