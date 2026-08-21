"""🌐 板块市场：行业板块资金流向与轮动（重构版布局）。

布局：
  顶部：紧凑状态栏（分类库 / 板块日线 / 采集状态 / 同步按钮）
  📊 实时板块榜 —— 板块快照榜 + 板块详情（成分股/当日趋势）
  📅 轮动走势   —— 周期选择 + 热力图 / 成交额占比 / 相对强弱
  💰 资金榜     —— 全板块资金净流入榜 + 单板块资金流日K

口径：板块总成交额及环比（新浪，全市场）；净主动金额（自有快照内外盘净额，覆盖宇宙）；
量价净流（qlib 日线回填，全历史）。真实主力资金流向需 L2，本页为代理指标。
"""

import html as _html

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import sectorflow as sf
from common import get_instruments
from quotefeed import get_feed

FEED_UNIVERSE = "sectorflow:universe"
FEED_SPOT = "sectorflow:spot"


# ---------------------------------------------------------------- 通用小件

def _labeled_select(label: str, options, key: str, index: int = 0, ratio=(0.75, 2.25)):
    """标签与下拉框同一行（Streamlit 原生标签在上方，这里改为行内）。"""
    c1, c2 = st.columns(list(ratio))
    with c1:
        st.markdown(f"<div style='padding-top:9px;color:#999;font-size:14px;white-space:nowrap'>{label}</div>",
                    unsafe_allow_html=True)
    with c2:
        return st.selectbox(label, options, index=index, key=key, label_visibility="collapsed")

def _now_hm() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H%M")


def _html_table(df: pd.DataFrame, signed: set[str], height: int = 540) -> str:
    ths = "".join(f"<th style='padding:7px 10px;text-align:right;color:#bbb;border-bottom:1px solid #444;"
                  f"white-space:nowrap'>{c}</th>" for c in df.columns)
    trs = []
    for _, r in df.iterrows():
        tds = []
        for c in df.columns:
            v = r[c]
            if pd.isna(v):
                txt, style = "—", "color:#666"
            elif c in signed:
                txt = f"{v:+.2f}" if isinstance(v, float) else str(v)
                style = f"color:{'#e54545' if v > 0 else ('#2ca02c' if v < 0 else '#999')}"
            elif isinstance(v, float):
                txt, style = f"{v:.2f}", ""
            else:
                txt, style = str(v), ""
            tds.append(f"<td style='padding:5px 10px;text-align:right;white-space:nowrap;{style}'>"
                       f"{_html.escape(txt)}</td>")
        trs.append("<tr style='border-bottom:1px solid #242424' "
                   "onmouseover=\"this.style.background='#1f1f23'\" "
                   "onmouseout=\"this.style.background='transparent'\">" + "".join(tds) + "</tr>")
    return (f"<div style='height:{height}px;overflow-y:auto;border:1px solid #333;border-radius:6px;background:#101010'>"
            f"<table style='width:100%;border-collapse:collapse;font-size:13px;font-family:ui-monospace,monospace;color:#ddd'>"
            f"<thead style='position:sticky;top:0;background:#1c1c1e;z-index:2'><tr>{ths}</tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table></div>")


def _latest_spot():
    with sf._sconn() as c:
        ts = c.execute("SELECT MAX(ts) FROM sector_flow_snapshots").fetchone()[0]
        if not ts:
            return pd.DataFrame(), None
        return pd.read_sql("SELECT * FROM sector_flow_snapshots WHERE ts=?", c, params=(ts,)), ts


# ---------------------------------------------------------------- 页面
def render():
    st.title("🌐 资金趋势（板块资金流向）")

    # ---------- 顶部状态栏 ----------
    istatus = sf.industry_status()
    dstatus = sf.sector_daily_status()
    prog = sf.sync_progress()
    bf = sf.backfill_progress()
    s1, s2, s3, s4, s5 = st.columns([2.2, 2.4, 1.6, 1.4, 1.6])
    s1.caption(f"🗂 分类库：**{istatus['stocks']}** 只 / **{istatus['sectors']}** 板块")
    s2.caption(f"🗓 板块日线：**{dstatus['rows']}** 行 · {dstatus['min_date']}~{dstatus['max_date']}")
    with s3:
        if st.button("🔁 同步行业分类", key="sf_sync"):
            sf.sync_industry_map(background=True)
    with s4:
        if st.button("🗓 更新板块日线", key="sf_bf"):
            sf.backfill_sector_daily(days=15, background=True)
    in_session = "0915" <= _now_hm() <= "1505"
    with s5:
        live = st.toggle("🔄 实时采集(30秒)", value=in_session, key="sf_live")

    if prog["running"]:
        st.progress(prog["done"] / max(prog["total"], 1), text=f"行业分类同步中 {prog['done']}/{prog['total']}")
    elif istatus["stocks"] == 0:
        sf.sync_industry_map(background=True)
    if bf["running"]:
        st.progress(bf["done"] / max(bf["total"], 1), text=f"板块日线回填中 {bf['done']}/{bf['total']}")
    elif dstatus["rows"] == 0:
        sf.backfill_sector_daily(days=260, background=True)

    feed = get_feed()
    if live:
        feed.stop_all_except(FEED_UNIVERSE)
        feed.ensure(FEED_UNIVERSE, get_instruments("csi300") + get_instruments("csi500"), interval=30)

        def _collect():
            n = sf.save_sector_spot(sf.fetch_sector_spot())
            sf.save_sector_inflow_snapshot()
            return n

        feed.ensure_custom(FEED_SPOT, _collect, interval=30)
    else:
        feed.stop(FEED_UNIVERSE)
        feed.stop(FEED_SPOT)

    tab1, tab2, tab3 = st.tabs(["📊 实时板块榜", "📅 轮动走势", "💰 资金榜"])

    # ================= Tab1 实时板块榜 =================
    with tab1:
        c1, c2, c3 = st.columns([1.2, 1.6, 3.2])
        with c1:
            st.caption(" ")  # 三列统一标题行高度 → 控件自然同高对齐
            clicked = st.button("🔄 立即抓取一次", key="sf_once")
        with c2:
            sort_col = _labeled_select("榜单排序", ["净主动亿", "成交额亿", "环比%", "平均涨跌幅%"],
                                       index=0, key="sf_sort")
        with c3:
            st.caption(" ")
            fetch_status = st.empty()
        if clicked:
            fetch_status.markdown("⏳ 抓取中…")
            df = sf.fetch_sector_spot()
            n = sf.save_sector_spot(df)
            fetch_status.markdown(f"✅ 已抓取 {n} 个板块")

        interval = "5s" if live else None
        body = st.fragment(_board_tab1, run_every=interval) if interval else _board_tab1
        body(sort_col)

        st.markdown("**板块详情**")
        spot, _ = _latest_spot()
        if not spot.empty:
            detail = st.selectbox("选择板块", spot["sector_name"].tolist(), key="sf_detail")
            if detail:
                _render_detail(spot, detail)

    # ================= Tab2 轮动走势 =================
    with tab2:
        period = st.radio("周期", [5, 10, 20, 60], index=2, horizontal=True,
                          format_func=lambda x: f"近{x}交易日", key="sf_period")
        daily = sf.sector_daily_range(period)
        if daily.empty:
            st.info("板块日线就绪后展示轮动图（顶部状态栏可看回填进度）。")
        else:
            _render_rotation(daily, period)

    # ================= Tab3 资金榜 =================
    with tab3:
        daily3 = sf.sector_daily_range(60)
        if daily3.empty:
            st.info("板块日线就绪后展示资金榜。")
        else:
            _render_flow_board(daily3)

    with st.expander("📖 数据口径说明"):
        st.markdown(
            "- **板块快照**（实时榜）：新浪 84 行业板块现货，含总成交额/涨跌幅/领涨股，全市场口径\n"
            "- **净主动金额**：自有行情快照的外盘−内盘×价格按板块聚合（主动买-主动卖代理），覆盖=沪深300+中证500\n"
            "- **板块日线**（轮动/资金榜）：行业映射 × qlib 股票日线回填 260 交易日，等权涨跌幅/成交额/涨跌家数\n"
            "- **量价净流**：个股当日涨记+成交额、跌记−成交额按板块求和（日线资金流代理）\n"
            "- 真实主力资金流向属 L2 数据，免费通道不可得，以上均为代理指标。")


# ---------------------------------------------------------------- Tab1 区块
def _board_tab1(sort_col: str):
    spot, ts = _latest_spot()
    if spot.empty:
        st.info("暂无板块数据，点「立即抓取一次」。")
        return
    inflow = sf.sector_net_inflow()
    net_map = dict(zip(inflow["sector"], inflow["净主动金额亿"])) if not inflow.empty else {}
    cover_map = dict(zip(inflow["sector"], inflow["覆盖家数"])) if not inflow.empty else {}
    baseline = sf.sector_amount_baseline(days=5)

    total_amt = spot["total_amount"].sum() or 1
    spot = spot.copy()
    spot["成交额亿"] = spot["total_amount"] / 1e8
    spot["占比%"] = spot["total_amount"] / total_amt * 100
    spot["环比%"] = spot.apply(lambda r: (r["total_amount"] / baseline.get(r["sector_label"], 0) - 1) * 100
                              if baseline.get(r["sector_label"]) else None, axis=1)
    spot["净主动亿"] = spot["sector_name"].map(net_map)
    spot["覆盖"] = spot["sector_name"].map(cover_map)
    key = {"平均涨跌幅%": "avg_chg_pct"}.get(sort_col, sort_col)
    spot = spot.sort_values(key, ascending=False, na_position="last").reset_index(drop=True)
    spot.insert(0, "排名", spot.index + 1)

    st.caption(f"板块快照 **{ts}** · 净主动统计时点 {inflow.attrs.get('ts','—') if not inflow.empty else '—'}")
    show = spot[["排名", "sector_name", "members", "avg_chg_pct", "成交额亿", "占比%", "环比%",
                 "净主动亿", "覆盖", "leader_name", "leader_chg_pct"]].rename(columns={
        "sector_name": "板块", "members": "家数", "avg_chg_pct": "平均涨跌幅%",
        "leader_name": "领涨股", "leader_chg_pct": "领涨涨幅%"})
    st.markdown(_html_table(show, signed={"平均涨跌幅%", "环比%", "净主动亿", "领涨涨幅%"}),
                unsafe_allow_html=True)


def _render_detail(spot: pd.DataFrame, sector_name: str):
    label = spot[spot["sector_name"] == sector_name]["sector_label"].iloc[0]
    trend = sf.sector_trend(label)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**{sector_name} · 今日成交额趋势**")
        if len(trend) >= 2:
            st.line_chart(trend.set_index("ts")[["total_amount"]], height=200)
        else:
            st.caption("时序积累中（采集几轮后可见）")
    with c2:
        st.markdown(f"**{sector_name} · 成分股**")
        try:
            import akshare as ak

            d = ak.stock_sector_detail(sector=label)
            if d is not None and not d.empty:
                d = d[["code", "name", "trade", "changepercent", "volume", "amount", "turnoverratio"]].copy()
                d.columns = ["代码", "名称", "现价", "涨幅%", "成交量", "成交额", "换手%"]
                d["成交额"] = (d["成交额"] / 1e8).round(2)
                st.dataframe(d.head(30), width='stretch', height=280, hide_index=True)
        except Exception as e:
            st.warning(f"成分获取失败：{e}")


# ---------------------------------------------------------------- Tab2 区块
def _render_rotation(daily: pd.DataFrame, period: int):
    dmin, dmax = daily["date"].min(), daily["date"].max()
    st.caption(f"统计区间：**{dmin} ~ {dmax}**（{daily['date'].nunique()} 个交易日）")
    act = daily.groupby("sector_name")["total_amount"].sum().sort_values(ascending=False)
    top_secs = act.head(15).index.tolist()

    st.markdown("**板块轮动热力图**（红=板块当日上涨 绿=下跌；颜色漂移即轮动）")
    pv = (daily[daily["sector_name"].isin(top_secs)]
          .pivot(index="sector_name", columns="date", values="avg_chg_pct"))
    pv = pv.loc[act.head(15).index]
    fig = go.Figure(go.Heatmap(z=pv.values, x=[str(c)[:10] for c in pv.columns], y=pv.index,
                               zmid=0, zmin=-3, zmax=3, colorscale="RdBu_r",
                               colorbar=dict(title="涨跌幅%")))
    fig.update_layout(height=max(400, 26 * len(pv) + 160), margin=dict(l=10, r=10, t=10, b=10),
                      xaxis=dict(tickangle=-45, tickfont=dict(size=10)),
                      yaxis=dict(tickfont=dict(size=10)))
    st.plotly_chart(fig, width='stretch')

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**成交额占比走势（Top8）**")
        share = (daily[daily["sector_name"].isin(top_secs[:8])]
                 .pivot(index="date", columns="sector_name", values="total_amount"))
        st.line_chart(share.div(share.sum(axis=1) + 1e-12) * 100, height=300)
    with c2:
        st.markdown("**相对强弱指数（期初=100）**")
        chg_pv = daily.pivot(index="date", columns="sector_name", values="avg_chg_pct")
        idx = (1 + chg_pv.fillna(0) / 100).cumprod() * 100
        chosen = st.multiselect("对比板块", idx.columns.tolist(),
                                default=[s for s in top_secs[:3] if s in idx.columns], key="sf_rs")
        if chosen:
            fig2 = go.Figure()
            for s in chosen:
                fig2.add_trace(go.Scatter(x=idx.index, y=idx[s], name=s))
            fig2.add_trace(go.Scatter(x=idx.index, y=idx.mean(axis=1), name="市场均值",
                                      line=dict(dash="dash", color="#888")))
            fig2.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                               legend=dict(orientation="h"))
            st.plotly_chart(fig2, width='stretch')


# ---------------------------------------------------------------- Tab3 区块
def _render_flow_board(daily: pd.DataFrame):
    flow_pv = daily.pivot(index="date", columns="sector_name", values="flow_net") / 1e8
    amt_pv = daily.pivot(index="date", columns="sector_name", values="total_amount") / 1e8
    chg_pv = daily.pivot(index="date", columns="sector_name", values="avg_chg_pct")
    up_pv = daily.pivot(index="date", columns="sector_name", values="up_count")
    dn_pv = daily.pivot(index="date", columns="sector_name", values="down_count")
    if flow_pv.empty:
        st.info("量价净流数据未就绪（重新回填板块日线后生成）。")
        return

    def _streak(s: pd.Series) -> int:
        n = 0
        for v in s.iloc[::-1]:
            if v > 0:
                n += 1
            else:
                break
        return n

    last_date = flow_pv.index[-1]
    board = pd.DataFrame({
        "板块": flow_pv.columns,
        "当日净流": flow_pv.iloc[-1].values,
        "近5日净流": flow_pv.tail(5).sum().values,
        "近10日净流": flow_pv.tail(10).sum().values,
        "近20日净流": flow_pv.tail(20).sum().values,
        "当日涨跌幅%": chg_pv.iloc[-1].values,
        "当日成交额": amt_pv.iloc[-1].values,
        "涨/跌家数": (up_pv.iloc[-1].astype(int).astype(str) + "/" + dn_pv.iloc[-1].astype(int).astype(str)).values,
        "连续净流天数": [(_streak(flow_pv[s]) if flow_pv[s].iloc[-1] > 0 else -_streak(-flow_pv[s]))
                       for s in flow_pv.columns],
    })
    c1, c2 = st.columns([2, 2.4])
    with c1:
        sort_col = _labeled_select("榜单排序", ["当日净流", "近5日净流", "近10日净流", "近20日净流", "连续净流天数"],
                                   index=0, key="sf_flow_sort")
    with c2:
        ksec = _labeled_select("日K板块", board["板块"].tolist(), index=0, key="sf_ksec", ratio=(0.7, 2.3))
    board = board.sort_values(sort_col, ascending=False).reset_index(drop=True)
    board.insert(0, "排名", board.index + 1)

    st.caption(f"榜单日期：**{last_date}** · 共 {len(board)} 个板块（净流入最大在前）")
    st.markdown(_html_table(board, signed={"当日净流", "近5日净流", "近10日净流", "近20日净流",
                                           "当日涨跌幅%", "连续净流天数"}, height=460),
                unsafe_allow_html=True)

    if ksec:
        s_flow = flow_pv[ksec]
        cum = s_flow.fillna(0).cumsum()
        from plotly.subplots import make_subplots

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.62, 0.38],
                            vertical_spacing=0.03)
        fig.add_trace(go.Scatter(x=cum.index, y=cum, name="累计净流(亿)",
                                 line=dict(color="#ffd54f", width=1.6),
                                 fill="tozeroy", fillcolor="rgba(255,213,79,0.12)"), row=1, col=1)
        colors = ["#e54545" if v >= 0 else "#2ca02c" for v in s_flow]
        fig.add_trace(go.Bar(x=s_flow.index, y=s_flow, name="每日净流(亿)",
                             marker_color=colors, showlegend=False), row=2, col=1)
        in_days = int((s_flow > 0).sum())
        total_net = float(s_flow.sum())
        nc = "#e54545" if total_net >= 0 else "#2ca02c"
        fig.update_layout(template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                          height=500, margin=dict(l=10, r=10, t=46, b=10), hovermode="x unified",
                          title=f"{ksec} · 区间 {s_flow.index[0]} ~ {s_flow.index[-1]} · "
                                f"区间净流 <span style='color:{nc}'>{total_net:+.1f}亿</span> · "
                                f"流入天数 {in_days}/{len(s_flow)}")
        fig.add_hline(y=0, line=dict(color="#666", width=0.8), row=2, col=1)
        st.plotly_chart(fig, width='stretch')
        st.caption("上图=期间累计净流（资金走势，持续上行=持续流入）；下图=每日净流（红=流入 绿=流出）")

    inflow_cnt = sf.sector_inflow_range(30)
    st.caption(f"真实内外盘净主动金额（快照口径）已积累 {len(inflow_cnt)} 条时序，随采集自动增厚。")


render()
