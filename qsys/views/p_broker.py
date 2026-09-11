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


@st.cache_data(ttl=60, show_spinner=False)
def _load_equity_curve() -> tuple[pd.DataFrame, dict]:
    """逐交易日重建账户总资产（现金 + 持仓市值×收盘价），计算日收益。

    - 现金取资金流水的 balance（权威记录，含手动出入金）；持仓由 broker_fills 回放
    - 出入金属于外部现金流，日收益按修正迪茨法剔除：ret = 权益/(昨日权益+今日净入金) - 1
    - 不能用现金余额直接算收益率：买入让现金下降，建仓会被误判为大亏
    """
    import sqlite3
    if not broker.DB_PATH.exists():
        return pd.DataFrame(), {}
    try:
        with sqlite3.connect(str(broker.DB_PATH), timeout=30) as c:
            c.execute("PRAGMA busy_timeout=30000")
            fills = pd.read_sql(
                "SELECT date, code, side, price, shares, amount, fee, tax"
                " FROM broker_fills ORDER BY date, id", c)
            flows = pd.read_sql(
                "SELECT ts, type, amount, balance FROM broker_cashflows ORDER BY id", c)
    except Exception:
        return pd.DataFrame(), {}
    if fills.empty or flows.empty:
        return pd.DataFrame(), {}

    flows["date"] = flows["ts"].str[:10]
    eod_cash = flows.groupby("date")["balance"].last().astype(float)
    ext = flows[~flows["type"].isin(["买入", "卖出"])].groupby("date")["amount"].sum()
    init_cash = float(flows.loc[flows["type"] == "初始入金", "amount"].sum())
    if init_cash <= 0:
        init_cash = broker.INIT_CASH
    start = str(min(flows["date"].min(), fills["date"].min()))
    today = datetime.now().strftime("%Y-%m-%d")

    # 各持仓股票的日线收盘价（含今日实时合并）；缺行情的日用最近价/成本兜底
    import datasource
    close = {}
    for code in fills["code"].unique():
        try:
            d = datasource.get_daily_from_db(code, start, today)
        except Exception:
            d = pd.DataFrame()
        if not d.empty:
            s = pd.to_numeric(d.set_index("date")["close"], errors="coerce").dropna()
            close[code] = s[~s.index.duplicated(keep="last")]

    days = sorted({x for s in close.values() for x in s.index}
                  | set(fills["date"]) | set(eod_cash.index))
    days = [str(d) for d in days if start <= str(d) <= today]
    eod_cash = eod_cash.reindex(days).ffill()

    fills_by_date = {d: g for d, g in fills.groupby("date")}
    shares, px_last, cost_last = {}, {}, {}
    rows = []
    for d in days:
        g = fills_by_date.get(d)
        if g is not None:
            for f in g.itertuples():
                if f.side == "buy":
                    shares[f.code] = shares.get(f.code, 0) + int(f.shares)
                else:
                    left = shares.get(f.code, 0) - int(f.shares)
                    if left > 0:
                        shares[f.code] = left
                    else:
                        shares.pop(f.code, None)
                cost_last[f.code] = float(f.price)
        for code, s in close.items():
            v = s.get(d)
            if v is not None:
                px_last[code] = float(v)
        mv = sum(n * px_last.get(c_, cost_last.get(c_, 0.0)) for c_, n in shares.items())
        cash = float(eod_cash[d]) if pd.notna(eod_cash[d]) else 0.0
        equity = cash + mv
        denom = (rows[-1]["equity"] if rows else 0.0) + float(ext.get(d, 0.0))
        pnl = equity - denom
        rows.append({"date": d, "cash": round(cash, 2), "mv": round(mv, 2),
                     "equity": round(equity, 2), "pnl": round(pnl, 2),
                     "ret_pct": pnl / denom * 100 if denom else 0.0})
    if not rows:
        return pd.DataFrame(), {}
    eq = pd.DataFrame(rows)
    eq["date"] = pd.to_datetime(eq["date"])
    meta = {"init": init_cash, "ext_total": float(ext.sum())}
    return eq, meta


def _render_calendar_tab():
    """收益日历：总资产曲线 + 逐月日历（收益 = 总资产日变动，含持仓市值）。"""
    import calendar as cal_mod
    import plotly.graph_objects as go

    eq, meta = _load_equity_curve()
    if eq.empty:
        st.info("暂无成交记录——完成交易后将自动生成收益日历")
        return
    init_cash = meta.get("init", broker.INIT_CASH)

    # 累计收益率用时间加权（剔除出入金影响）；累计盈亏为金额口径
    total_ret = ((1 + eq["ret_pct"] / 100).prod() - 1) * 100
    total_pnl = eq["equity"].iloc[-1] - meta.get("ext_total", init_cash)
    win, lose = int((eq["ret_pct"] > 0).sum()), int((eq["ret_pct"] < 0).sum())
    n = len(eq)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("累计收益率", f"{total_ret:+.2f}%")
    c2.metric("累计盈亏", f"{total_pnl:+,.0f} 元")
    c3.metric("交易天数", n)
    c4.metric("日胜率", f"{win / n * 100:.1f}%")
    c5.metric("最大单日涨幅", f"{eq['ret_pct'].max():+.2f}%")
    c6.metric("最大单日跌幅", f"{eq['ret_pct'].min():+.2f}%")
    st.caption("收益率按时间加权计算，手动入金/出金不影响收益率；盈亏金额 = 当前总资产 − 累计净入金")

    # 总资产曲线
    line_color = UP if total_ret >= 0 else DOWN
    fig = go.Figure(go.Scatter(
        x=eq["date"], y=eq["equity"], mode="lines", name="总资产",
        line=dict(color=line_color, width=2),
        hovertemplate="%{x|%Y-%m-%d}<br>总资产 %{y:,.0f} 元<extra></extra>"))
    fig.add_hline(y=init_cash, line_dash="dot", line_color="gray", line_width=1,
                  annotation_text=f"初始资金 {init_cash:,.0f}", annotation_position="top left")
    fig.update_layout(height=240, margin=dict(l=10, r=10, t=20, b=10),
                      xaxis_title=None, yaxis_title=None, showlegend=False)
    st.plotly_chart(fig, use_container_width=True)

    # 月历
    eq["year_month"] = eq["date"].dt.to_period("M")
    months = sorted(eq["year_month"].unique(), reverse=True)
    sel = st.selectbox("选择月份", [str(m) for m in months], key="cal_month")
    period = pd.Period(sel)
    md = eq[eq["year_month"] == period].copy()
    year, month = period.year, period.month

    m_ret = ((1 + md["ret_pct"] / 100).prod() - 1) * 100
    m_pnl = md["pnl"].sum()
    m_win, m_lose = int((md["ret_pct"] > 0).sum()), int((md["ret_pct"] < 0).sum())
    m_color = UP if m_ret >= 0 else DOWN
    st.markdown(
        f"**{year} 年 {month} 月**　<span style='color:{m_color};font-weight:700'>"
        f"{m_ret:+.2f}%（{m_pnl:+,.0f} 元）</span>　"
        f"<span style='opacity:.65'>盈利 {m_win} 天 / 亏损 {m_lose} 天</span>",
        unsafe_allow_html=True)

    by_day = {int(d.day): r for d, r in zip(md["date"], md.itertuples())}
    max_abs = md["ret_pct"].abs().max() or 1.0

    today_str = datetime.now().strftime("%Y-%m-%d")

    def _cell(day: int) -> str:
        if day == 0:
            return "<td class='cal-off'></td>"
        r = by_day.get(day)
        if r is None:
            if f"{year:04d}-{month:02d}-{day:02d}" > today_str:
                return (f"<td class='cal-off'><div class='cal-d' style='opacity:.3'>"
                        f"{day}</div></td>")
            return (f"<td class='cal-closed'><div class='cal-d'>{day}</div>"
                    f"<div class='cal-r' style='opacity:.35'>休市</div></td>")
        ret, pnl = r.ret_pct, r.pnl
        a = 0.10 + 0.62 * min(abs(ret) / max_abs, 1.0)
        if ret > 0:
            bg, fg = f"rgba(229,69,69,{a:.2f})", ("#fff" if a > 0.42 else "inherit")
        elif ret < 0:
            bg, fg = f"rgba(38,166,154,{a:.2f})", ("#fff" if a > 0.42 else "inherit")
        else:
            bg, fg = "rgba(128,128,128,.10)", "inherit"
        return (f"<td style='background:{bg};color:{fg}'>"
                f"<div class='cal-d'>{day}</div>"
                f"<div class='cal-r'>{ret:+.2f}%</div>"
                f"<div class='cal-p'>{pnl:+,.0f}</div></td>")

    weeks = cal_mod.monthcalendar(year, month)
    head = "".join(f"<th>{w}</th>" for w in ["周一", "周二", "周三", "周四", "周五"])
    body = "".join(
        "<tr>" + "".join(_cell(day) for day in week[:5]) + "</tr>" for week in weeks)
    st.markdown(f"""<style>
.cal {{width:100%;border-collapse:separate;border-spacing:6px;table-layout:fixed;}}
.cal th {{text-align:center;font-size:13px;font-weight:500;opacity:.6;padding:2px 0;}}
.cal td {{height:84px;vertical-align:top;border-radius:10px;padding:8px 10px;}}
.cal .cal-d {{font-size:13px;font-weight:600;opacity:.8;}}
.cal .cal-r {{font-size:16px;font-weight:700;margin-top:8px;}}
.cal .cal-p {{font-size:12px;opacity:.85;margin-top:2px;}}
.cal .cal-off {{background:transparent;}}
.cal .cal-closed {{background:rgba(128,128,128,.07);}}
</style>
<table class="cal"><tr>{head}</tr>{body}</table>""", unsafe_allow_html=True)

    # 按月汇总
    with st.expander("按月汇总"):
        rows = []
        for m in months:
            g = eq[eq["year_month"] == m]
            w = int((g["ret_pct"] > 0).sum())
            rows.append({"月份": str(m),
                         "收益率": f"{((1 + g['ret_pct'] / 100).prod() - 1) * 100:+.2f}%",
                         "盈亏(元)": f"{g['pnl'].sum():+,.0f}", "交易天数": len(g),
                         "盈利天数": w, "胜率": f"{w / len(g) * 100:.1f}%"})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch')


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
