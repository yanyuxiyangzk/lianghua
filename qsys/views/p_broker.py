"""💹 普通交易（模拟柜台）：持仓 / 买入 / 卖出 / 撤单 / 查询。

初始资金 100000 元，A股规则（T+1、100股整手、佣金万2.5最低5元、印花税卖出0.05%）。
行情用 ifind_realtime 最新快照（同花顺 iFinD，盘中 5 分钟一批）。
"""

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

    tab_pos, tab_buy, tab_sell, tab_cancel, tab_query = st.tabs(
        ["💼 持仓", "🛒 买入", "💰 卖出", "❌ 撤单", "🔍 查询"])

    # ---------------------------------------------------------------- 持仓
    with tab_pos:
        poss = broker.get_positions()
        if poss.empty:
            st.info("柜台暂无持仓。去「🛒 买入」下单。")
        else:
            show = pd.DataFrame({
                "代码": poss["code"], "名称": poss["name"],
                "持仓(股)": poss["shares"], "可卖(股)": poss["sellable"],
                "成本价": poss["cost"].round(3),
                "最新价": poss["最新价"].map(lambda x: round(x, 2) if pd.notna(x) else None),
                "市值": poss["市值"].map(lambda x: f"{x:,.0f}"),
                "持仓盈亏": poss["持仓盈亏"].map(lambda x: f"{x:+,.0f}"),
                "今日盈亏": poss["今日盈亏"].map(lambda x: f"{x:+,.0f}" if pd.notna(x) else "-"),
            })
            st.dataframe(show, hide_index=True, width='stretch')

        # 自动跟踪持仓（每日名单盘中自动开仓，不占柜台资金）
        st.markdown("#### 🤖 自动跟踪持仓（每日选股名单自动开仓，不占柜台资金）")
        try:
            import experience
            autos = experience.get_open_positions()
        except Exception:
            autos = pd.DataFrame()
        if autos.empty:
            st.caption("暂无自动持仓——每日名单在盘中 9:30 起按快照价自动开仓")
        else:
            show_a = pd.DataFrame({
                "代码": autos["code"], "名称": autos["name"],
                "买入时间": autos["buy_ts"],
                "买入价": autos["buy_price"].round(2),
                "最新价": autos["最新价"].round(2),
                "浮动盈亏": autos["浮动盈亏%"].map(lambda x: f"{x:+.2f}%" if pd.notna(x) else "-"),
                "持有(交易日)": autos["持有交易日"],
                "来源": autos["pack_name"].fillna(autos["source"]),
            })
            st.dataframe(show_a, hide_index=True, width='stretch')

    # ---------------------------------------------------------------- 买入
    with tab_buy:
        _order_form("buy")

    # ---------------------------------------------------------------- 卖出
    with tab_sell:
        poss = broker.get_positions()
        if poss.empty:
            st.info("暂无持仓可卖")
        else:
            sellable = poss[poss["sellable"] > 0]
            if sellable.empty:
                st.info("持仓均为当日买入（T+1，次日可卖）")
            _order_form("sell", sellable)

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
                show = orders[["ts", "code", "name", "side", "price", "shares", "status",
                               "filled_price", "filled_ts"]].rename(
                    columns={"ts": "委托时间", "code": "代码", "name": "名称", "side": "方向",
                             "price": "限价", "shares": "数量", "status": "状态",
                             "filled_price": "成交价", "filled_ts": "成交时间"})
                show["方向"] = show["方向"].map({"buy": "买入", "sell": "卖出"})
                st.dataframe(show, hide_index=True, width='stretch')
        with q2:
            fills = broker.list_fills(today_only=True)
            if fills.empty:
                st.info("今日无成交")
            else:
                show = fills[["ts", "code", "name", "side", "price", "shares", "amount",
                              "fee", "tax"]].rename(
                    columns={"ts": "成交时间", "code": "代码", "name": "名称", "side": "方向",
                             "price": "成交价", "shares": "数量", "amount": "成交金额",
                             "fee": "佣金", "tax": "印花税"})
                show["方向"] = show["方向"].map({"buy": "买入", "sell": "卖出"})
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


def _order_form(side: str, sellable: pd.DataFrame | None = None):
    """买入/卖出表单。卖出时 sellable 传可卖持仓。"""
    is_buy = side == "buy"
    acc = broker.get_account()

    if is_buy:
        code = st.text_input("股票代码", key="buy_code",
                             placeholder="如 600519 / SH600519 / 600519.SH")
        code_norm = _norm_code(code)
    else:
        codes = sellable["code"].tolist()
        sel = st.selectbox("选择持仓", codes,
                           format_func=lambda c: f"{c} {sellable[sellable['code']==c]['name'].iloc[0]}"
                                                 f"（可卖 {int(sellable[sellable['code']==c]['sellable'].iloc[0])} 股）",
                           key="sell_code")
        code_norm = sel

    name = broker.get_name(code_norm) if code_norm else ""
    pr = broker._latest_prices([code_norm]).get(code_norm) if code_norm else None
    cur = pr[0] if pr else None

    c1, c2 = st.columns(2)
    with c1:
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
            mx = int(sellable[sellable["code"] == code_norm]["sellable"].iloc[0])
            shares = st.number_input("卖出数量（股）", min_value=100, max_value=max(100, mx),
                                     value=min(100, mx), step=100, key="sell_shares",
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
            msg = broker.place_order(code_norm, side, price or None, shares)
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
