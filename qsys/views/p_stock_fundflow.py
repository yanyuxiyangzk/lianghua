"""💰 个股资金流向 — 今日分时资金流 / 近20日主力净流入 / 资金流排名 / 异动雷达。

数据源：
  - 东财 push2his（个股日资金流）/ push2（分时资金流）
  - 同花顺 10jqka（全市场资金流排名）
  - 本地库 stock_fundflow_daily / stock_fundflow_intraday 表
"""

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import datasource
import ifind_hub


def _signed_color(v: float) -> str:
    if v > 0:
        return "#e54545"
    if v < 0:
        return "#2ca02c"
    return "#999"


def _format_money(v: float) -> str:
    if abs(v) >= 1e8:
        return f"{v / 1e8:+.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:+.1f}万"
    return f"{v:+.0f}"


def _detect_anomalies(df_daily: pd.DataFrame) -> list[dict]:
    """异动雷达：检测资金流异常信号。"""
    if df_daily.empty or len(df_daily) < 5:
        return []
    alerts = []
    df = df_daily.sort_values("date").tail(20)
    latest = df.iloc[-1]
    code = latest.get("code", "")

    # 1. 主力资金连续流入/流出 >= 3 天
    main_signs = (df["main_net"] > 0).astype(int)
    if len(main_signs) >= 3:
        tail3 = main_signs.tail(3).tolist()
        if all(s == 1 for s in tail3):
            alerts.append({"type": "连续流入", "signal": "🟢",
                           "msg": f"主力资金连续 3 日净流入（累计 {_format_money(df.tail(3)['main_net'].sum())}）"})
        elif all(s == 0 for s in tail3):
            alerts.append({"type": "连续流出", "signal": "🔴",
                           "msg": f"主力资金连续 3 日净流出（累计 {_format_money(df.tail(3)['main_net'].sum())}）"})

    # 2. 主力净流入占成交额比例突增（超大单异动）
    if "super_net" in df.columns and "big_net" in df.columns:
        big_flow = df["super_net"] + df["big_net"]
        if len(big_flow) >= 5:
            avg5 = big_flow.tail(5).mean()
            if abs(latest.get("super_net", 0) + latest.get("big_net", 0)) > abs(avg5) * 2 and abs(avg5) > 0:
                direction = "大幅流入" if (latest.get("super_net", 0) + latest.get("big_net", 0)) > 0 else "大幅流出"
                alerts.append({"type": "大单异动", "signal": "⚡",
                               "msg": f"超大+大单资金{direction}（{_format_money(latest.get('super_net', 0) + latest.get('big_net', 0))}），5日均值 {_format_money(avg5)}"})

    # 3. 主力净流入绝对值创新高/新低
    if len(df) >= 10:
        prev_max = df["main_net"].iloc[:-1].max()
        prev_min = df["main_net"].iloc[:-1].min()
        if latest["main_net"] > prev_max and latest["main_net"] > 0:
            alerts.append({"type": "净流入新高", "signal": "📈",
                           "msg": f"今日主力净流入 {_format_money(latest['main_net'])} 创近20日新高"})
        if latest["main_net"] < prev_min and latest["main_net"] < 0:
            alerts.append({"type": "净流出新低", "signal": "📉",
                           "msg": f"今日主力净流出 {_format_money(latest['main_net'])} 创近20日新低"})

    # 4. 量能异动（主力净流入/总成交额占比超 10%）
    if "main_pct" in df.columns and df["main_pct"].notna().any():
        if abs(latest.get("main_pct", 0)) > 10:
            direction = "流入" if latest["main_pct"] > 0 else "流出"
            alerts.append({"type": "主力占比异动", "signal": "🔥",
                           "msg": f"主力资金{direction}占成交额 {latest['main_pct']:.1f}%（绝对值>10%）"})

    return alerts


def render():
    st.title("💰 个股资金流向")
    st.caption("数据源：东财（日资金流/分时资金流）· 同花顺（排名）· 盘后自动入库")
    ifind_hub.header()

    # 股票选择
    col1, col2 = st.columns([3, 1])
    with col1:
        code_input = st.text_input("输入股票代码", value="SH600519", placeholder="如 SH600519 / SZ000001",
                                   key="ff_code")
    with col2:
        refresh = st.button("🔄 刷新", key="ff_refresh")

    # 从 market_daily 获取自选股列表供选择
    with datasource._conn() as c:
        watch_codes = [r[0] for r in c.execute(
            "SELECT DISTINCT code FROM market_daily ORDER BY code LIMIT 200").fetchall()]
    code_options = watch_codes or ["SH600519", "SZ000001"]
    code = code_input.strip().upper()
    if not code:
        code = code_options[0]

    # 获取名称
    stock_name = ""
    try:
        with datasource._conn() as c:
            r = c.execute("SELECT name FROM ifind_stocklist WHERE code=?", (code,)).fetchone()
            if r:
                stock_name = r[0]
    except Exception:
        pass
    if stock_name:
        st.subheader(f"{stock_name}（{code}）")
    else:
        st.subheader(code)

    # Tab 布局
    t1, t2, t3, t4 = st.tabs(["📈 今日分时资金流", "📊 近20日主力净流入", "🏆 资金流排名", "🔔 异动雷达"])

    with t1:
        st.markdown("**今日分时资金流**（主力/大单/中单/小单净流入，元）")
        with st.spinner("加载分时数据…"):
            df_intra = datasource.get_fundflow_intraday(code)
        if df_intra.empty:
            st.info("分时数据暂无（可能非交易时段或该股无数据）")
        else:
            # 解析时间
            df_intra["time"] = pd.to_datetime(df_intra["datetime"]).dt.strftime("%H:%M")
            # 累计净流入
            df_intra["main_cum"] = df_intra["main_net"].cumsum()
            df_intra["super_cum"] = df_intra["super_net"].cumsum()
            df_intra["big_cum"] = df_intra["big_net"].cumsum()

            fig = go.Figure()
            fig.add_trace(go.Scatter(x=df_intra["time"], y=df_intra["main_cum"], name="主力净流入",
                                     line=dict(color="#e54545", width=2), fill="tozeroy",
                                     fillcolor="rgba(229,69,69,0.1)"))
            fig.add_trace(go.Scatter(x=df_intra["time"], y=df_intra["super_cum"], name="超大单",
                                     line=dict(color="#ff9800", width=1.5)))
            fig.add_trace(go.Scatter(x=df_intra["time"], y=df_intra["big_cum"], name="大单",
                                     line=dict(color="#2196f3", width=1.5)))
            fig.add_hline(y=0, line=dict(color="#666", width=0.8))
            fig.update_layout(template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                              height=380, margin=dict(l=10, r=10, t=10, b=10),
                              xaxis=dict(title="时间", gridcolor="#333"),
                              yaxis=dict(title="累计净流入(元)", gridcolor="#333"),
                              legend=dict(orientation="h"))
            st.plotly_chart(fig, width="stretch")

            # 最新状态
            last = df_intra.iloc[-1]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("主力净流入", _format_money(last["main_net"]),
                       delta=_format_money(last["main_net"]) if last["main_net"] != 0 else None)
            c2.metric("超大单", _format_money(last["super_net"]))
            c3.metric("大单", _format_money(last["big_net"]))
            c4.metric("中单", _format_money(last["mid_net"]))

    with t2:
        st.markdown("**近20日主力净流入**")
        with st.spinner("加载日资金流数据…"):
            df_daily = datasource.get_fundflow_daily(code, days=20)
        if df_daily.empty:
            st.info("日资金流数据暂无")
        else:
            df_daily = df_daily.sort_values("date")
            # 主力净流入柱状图
            colors = [_signed_color(v) for v in df_daily["main_net"]]
            fig = go.Figure(go.Bar(
                x=df_daily["date"], y=df_daily["main_net"], name="主力净流入",
                marker_color=colors,
                text=[_format_money(v) for v in df_daily["main_net"]],
                textposition="outside", textfont=dict(size=9)))
            fig.add_hline(y=0, line=dict(color="#666", width=0.8))
            total = df_daily["main_net"].sum()
            nc = _signed_color(total)
            fig.update_layout(template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                              height=380, margin=dict(l=10, r=10, t=40, b=10),
                              title=f"主力净流入合计 <span style='color:{nc}'>{_format_money(total)}</span>")
            st.plotly_chart(fig, width="stretch")

            # 分类资金流堆叠图
            st.markdown("**分类资金流**（超大单/大单/中单/小单）")
            fig2 = go.Figure()
            for col, name, color in [("super_net", "超大单", "#ff9800"), ("big_net", "大单", "#2196f3"),
                                     ("mid_net", "中单", "#9c27b0"), ("small_net", "小单", "#607d8b")]:
                if col in df_daily.columns:
                    fig2.add_trace(go.Bar(x=df_daily["date"], y=df_daily[col], name=name,
                                          marker_color=color))
            fig2.update_layout(barmode="stack", template="plotly_dark", paper_bgcolor="#101010",
                               plot_bgcolor="#101010", height=350, margin=dict(l=10, r=10, t=10, b=10),
                               legend=dict(orientation="h"))
            st.plotly_chart(fig2, width="stretch")

            # 明细表
            show = df_daily[["date", "main_net", "super_net", "big_net", "mid_net", "small_net"]].copy()
            show.columns = ["日期", "主力净流入", "超大单", "大单", "中单", "小单"]
            for c in show.columns[1:]:
                show[c] = show[c].apply(lambda x: _format_money(x) if pd.notna(x) else "")
            st.dataframe(show, width="stretch", hide_index=True, height=min(32 * (len(show) + 1) + 3, 400))

    with t3:
        st.markdown("**全市场个股资金流排名**（当日）")
        st.info("排名数据来自同花顺公开页面，受反爬限制可能加载失败")
        ranking = st.radio("排序方式", ["净额降序", "流入降序", "大单流入降序"], horizontal=True, key="ff_rank_sort")
        with st.spinner("加载排名数据…（约需15秒）"):
            try:
                import requests, re as _re
                url = "https://data.10jqka.com.cn/funds/ggzjl/field/zdf/order/desc/page/1/ajax/1/free/1/"
                r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                r.encoding = "gbk"
                html = r.text
                if "<table" not in html:
                    st.warning("同花顺排名数据被反爬拦截，请稍后重试")
                else:
                    t = html.find("<table")
                    te = html.find("</table>", t) + 8
                    ths = _re.findall(r"<th[^>]*>(.*?)</th>", html[t:te], re.S)
                    headers = [_re.sub(r"<[^>]+>", "", th).strip() for th in ths]
                    tds = _re.findall(r"<td[^>]*>(.*?)</td>", html[t:te], _re.S)
                    ncols = len(headers)
                    rows_data = []
                    for i in range(len(tds) // ncols):
                        cells = [_re.sub(r"<[^>]+>", "", tds[i * ncols + j]).strip() for j in range(ncols)]
                        def _parse(s):
                            s = s.strip().replace(",", "")
                            if "亿" in s:
                                return float(s.replace("亿", "")) * 1e8
                            if "万" in s:
                                return float(s.replace("万", "")) * 1e4
                            try:
                                return float(s)
                            except ValueError:
                                return 0
                        rows_data.append({
                            "排名": cells[0], "代码": cells[1], "名称": cells[2],
                            "最新价": cells[3], "涨跌幅": cells[4],
                            "流入(元)": _parse(cells[6]), "流出(元)": _parse(cells[7]),
                            "净额(元)": _parse(cells[8]), "成交额(元)": _parse(cells[9]),
                            "大单流入(元)": _parse(cells[10]) if len(cells) > 10 else 0,
                        })
                    df_rank = pd.DataFrame(rows_data)
                    if ranking == "净额降序":
                        df_rank = df_rank.sort_values("净额(元)", ascending=False)
                    elif ranking == "流入降序":
                        df_rank = df_rank.sort_values("流入(元)", ascending=False)
                    else:
                        df_rank = df_rank.sort_values("大单流入(元)", ascending=False)
                    df_rank["排名"] = range(1, len(df_rank) + 1)
                    st.dataframe(df_rank, width="stretch", hide_index=True, height=500)
            except Exception as e:
                st.warning(f"排名加载失败：{e}")

    with t4:
        st.markdown("**异动雷达**")
        with st.spinner("分析资金流异常信号…"):
            df_alert = datasource.get_fundflow_daily(code, days=20)
            alerts = _detect_anomalies(df_alert)
        if not alerts:
            st.success("暂无异动信号")
        else:
            for a in alerts:
                st.markdown(f"{a['signal']} **{a['type']}**：{a['msg']}")

        # 异动检测说明
        with st.expander("📖 异动检测规则"):
            st.markdown("""
            - **连续流入/流出**：主力资金连续 3 日同方向净流入/流出
            - **大单异动**：超大单+大单净流入绝对值超过近5日均值的 2 倍
            - **净流入/流出新高/低**：当日主力净流入创近20日极值
            - **主力占比异动**：主力资金净流入占成交额绝对值超过 10%

            ⚠️ 异动信号仅供参考，需结合基本面和技术面综合判断
            """)

    # 导出
    with st.expander("📥 导出数据"):
        c1, c2 = st.columns(2)
        with c1:
            if not df_daily.empty:
                st.download_button("导出日资金流 CSV",
                                   df_daily.to_csv(index=False, encoding="utf-8-sig"),
                                   file_name=f"fundflow_daily_{code}_{datetime.now():%Y%m%d}.csv",
                                   key="ff_dl_daily")
        with c2:
            if not df_intra.empty:
                st.download_button("导出分时资金流 CSV",
                                   df_intra.to_csv(index=False, encoding="utf-8-sig"),
                                   file_name=f"fundflow_intra_{code}_{datetime.now():%Y%m%d}.csv",
                                   key="ff_dl_intra")


render()
