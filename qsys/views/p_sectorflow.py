"""🌐 板块资金流 — 行业板块 / 概念板块 / 地域板块分类展示 + 板块轮动图。

数据源：
  - 行业板块：同花顺行业板块 summary（90 板块，含净流入/涨跌幅/成交额/领涨股）
  - 概念板块：同花顺概念板块 list（375 概念）+ 概念指数历史（轮动图）
  - 地域板块：暂无公开 API，预留占位
  - 轮动热力图：行业板块指数历史（stock_board_industry_index_ths）
"""

import time
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import ifind_hub


# ---------------------------------------------------------------- 数据获取（缓存） ----------------------------------------------------------------

@st.cache_data(ttl=600, show_spinner="加载行业板块数据…")
def _fetch_industry_summary() -> pd.DataFrame:
    """同花顺行业板块实时行情（90 板块）。"""
    import akshare as ak
    try:
        df = ak.stock_board_industry_summary_ths()
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner="加载概念板块列表…")
def _fetch_concept_names() -> pd.DataFrame:
    """同花顺概念板块名称列表（375 概念）。"""
    import akshare as ak
    try:
        df = ak.stock_board_concept_name_ths()
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_concept_index(name: str, days: int = 60) -> pd.DataFrame:
    """获取单个概念板块的历史指数数据。"""
    import akshare as ak
    try:
        df = ak.stock_board_concept_index_ths(symbol=name)
        if df is not None and not df.empty:
            df["日期"] = pd.to_datetime(df["日期"])
            cutoff = datetime.now() - timedelta(days=days + 30)
            df = df[df["日期"] >= cutoff].tail(days)
            return df
    except Exception:
        pass
    return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_industry_index(name: str, days: int = 60) -> pd.DataFrame:
    """获取单个行业板块的历史指数数据。"""
    import akshare as ak
    try:
        df = ak.stock_board_industry_index_ths(symbol=name)
        if df is not None and not df.empty:
            df["日期"] = pd.to_datetime(df["日期"])
            cutoff = datetime.now() - timedelta(days=days + 30)
            df = df[df["日期"] >= cutoff].tail(days)
            return df
    except Exception:
        return pd.DataFrame()
    return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_concept_members(name: str) -> pd.DataFrame:
    """获取概念板块成分股。"""
    import akshare as ak
    try:
        df = ak.stock_board_concept_info_ths(symbol=name)
        return df if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------- 通用渲染 ----------------------------------------------------------------

def _signed_color(v: float) -> str:
    if v > 0:
        return "#e54545"
    if v < 0:
        return "#2ca02c"
    return "#999"


def _flow_bar(df: pd.DataFrame, name_col: str, flow_col: str, top_n: int = 20, key: str = ""):
    """横向资金流入/流出条形图。"""
    d = df.nlargest(top_n, flow_col) if top_n > 0 else df.nsmallest(-top_n, flow_col)
    d = d.sort_values(flow_col)
    colors = [_signed_color(v) for v in d[flow_col]]
    fig = go.Figure(go.Bar(
        x=d[flow_col], y=d[name_col], orientation="h",
        marker_color=colors, text=[f"{v:+.2f}" for v in d[flow_col]],
        textposition="outside", textfont=dict(size=10)))
    fig.update_layout(
        height=max(300, 22 * len(d) + 80), margin=dict(l=10, r=10, t=10, b=10),
        template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
        xaxis=dict(title="净流入(亿)", gridcolor="#333"),
        yaxis=dict(gridcolor="#333", automargin=True))
    st.plotly_chart(fig, width="stretch", key=key)


def _rotation_heatmap(daily_pivot: pd.DataFrame, title: str = "板块轮动热力图", key: str = ""):
    """红涨绿跌热力图（行=板块，列=日期）。"""
    if daily_pivot.empty:
        return
    fig = go.Figure(go.Heatmap(
        z=daily_pivot.values,
        x=[str(c)[:10] for c in daily_pivot.columns],
        y=daily_pivot.index,
        zmid=0, zmin=-3, zmax=3, colorscale="RdBu_r",
        colorbar=dict(title="涨跌幅%")))
    fig.update_layout(
        height=max(350, 22 * len(daily_pivot) + 120),
        margin=dict(l=10, r=10, t=10, b=10),
        template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
        xaxis=dict(tickangle=-45, tickfont=dict(size=9)),
        yaxis=dict(tickfont=dict(size=9), automargin=True),
        title=dict(text=title, font=dict(size=13)))
    st.plotly_chart(fig, width="stretch", key=key)


def _cum_flow_chart(daily_flow: pd.DataFrame, name: str, key: str = ""):
    """累计净流 + 每日净流组合图。"""
    if daily_flow.empty:
        return
    from plotly.subplots import make_subplots
    cum = daily_flow.cumsum()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.6, 0.4], vertical_spacing=0.04)
    fig.add_trace(go.Scatter(x=cum.index, y=cum, name="累计净流(亿)",
                             line=dict(color="#ffd54f", width=1.6),
                             fill="tozeroy", fillcolor="rgba(255,213,79,0.12)"), row=1, col=1)
    colors = [_signed_color(v) for v in daily_flow]
    fig.add_trace(go.Bar(x=daily_flow.index, y=daily_flow, name="每日净流(亿)",
                         marker_color=colors, showlegend=False), row=2, col=1)
    total_net = float(daily_flow.sum())
    nc = _signed_color(total_net)
    fig.update_layout(template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                      height=420, margin=dict(l=10, r=10, t=40, b=10), hovermode="x unified",
                      title=f"{name} · 区间净流 <span style='color:{nc}'>{total_net:+.1f}亿</span>")
    fig.add_hline(y=0, line=dict(color="#666", width=0.8), row=2, col=1)
    st.plotly_chart(fig, width="stretch", key=key)
    st.caption("上=累计净流（持续上行=持续流入）；下=每日净流（红=流入 绿=流出）")


# ---------------------------------------------------------------- 页面 ----------------------------------------------------------------

def render():
    st.title("🌐 板块资金流")
    st.caption("数据源：同花顺行业/概念板块 · 净流入为代理指标 · 行业板块日刷新，概念板块指数日更新")
    ifind_hub.header()

    t_ind, t_con, t_reg = st.tabs(["🏭 行业板块", "💡 概念板块", "🗺️ 地域板块"])

    # ==================== 🏭 行业板块 ====================
    with t_ind:
        _render_industry()

    # ==================== 💡 概念板块 ====================
    with t_con:
        _render_concept()

    # ==================== 🗺️ 地域板块 ====================
    with t_reg:
        _render_regional()

    with st.expander("📖 数据口径说明"):
        st.markdown(
            "- **行业板块资金流**：同花顺行业板块实时行情，净流入=该板块所有个股主力净流入之和（亿）\n"
            "- **行业板块轮动**：行业板块指数历史 K 线（同花顺），涨跌幅热力图展示轮动\n"
            "- **概念板块列表**：同花顺 375 个概念板块名称（实时更新）\n"
            "- **概念板块指数**：同花顺概念指数历史 K 线，用于轮动对比\n"
            "- 真实主力资金流向属 L2 数据，以上净流入为代理指标")


def _render_industry():
    """🏭 行业板块：资金排行 + 轮动图 + 板块详情。"""
    df = _fetch_industry_summary()
    if df.empty:
        st.warning("行业板块数据加载失败，请稍后重试")
        return

    t1, t2, t3 = st.tabs(["💰 资金排行", "📊 轮动热力图", "📋 板块详情"])

    with t1:
        st.markdown(f"**同花顺 90 行业板块 · 实时资金流**（{datetime.now():%Y-%m-%d %H:%M}）")
        # 排序
        sort_options = ["净流入", "涨跌幅", "总成交额", "上涨家数"]
        sort_col = st.radio("排序", sort_options, horizontal=True, key="ind_sort")
        ascending = sort_col == "下跌家数"
        d = df.sort_values(sort_col, ascending=ascending).reset_index(drop=True)
        d.insert(0, "排名", range(1, len(d) + 1))
        d["总成交额(亿)"] = d["总成交额"].round(2)
        d["净流入(亿)"] = d["净流入"].round(2)
        show = d[["排名", "板块", "涨跌幅", "总成交额(亿)", "净流入(亿)", "上涨家数", "下跌家数", "领涨股", "领涨股-涨跌幅"]]
        st.dataframe(show, width="stretch", hide_index=True, height=min(32 * (len(show) + 1) + 3, 620))

        st.download_button(f"📥 导出CSV（{len(d)}行）",
                           d.to_csv(index=False, encoding="utf-8-sig"),
                           file_name=f"industry_flow_{datetime.now():%Y%m%d}.csv",
                           key="ind_dl")

        # 资金流入/流出 TOP20
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**资金净流入 TOP15**")
            _flow_bar(df, "板块", "净流入", top_n=15, key="ind_flow_in")
        with c2:
            st.markdown("**资金净流出 TOP15**")
            top_out = df.nsmallest(15, "净流入").sort_values("净流入", ascending=False)
            _flow_bar(top_out, "板块", "净流入", top_n=15, key="ind_flow_out")

    with t2:
        st.markdown("**行业板块轮动热力图**（红=上涨 绿=下跌；颜色漂移=轮动）")
        period = st.radio("回看天数", [10, 20, 30, 60], index=1, horizontal=True, key="ind_rot_period")

        # 加载行业板块历史数据做热力图
        # 取净流入最大的 N 个行业
        top_sectors = df.nlargest(20, "净流入")["板块"].tolist()
        selected = st.multiselect("选择板块（默认净流入 TOP20）", df["板块"].tolist(),
                                  default=top_sectors[:10], key="ind_rot_sel")
        if not selected:
            selected = top_sectors[:10]

        # 逐个加载历史指数
        all_hist = {}
        prog = st.progress(0, text="加载板块历史数据…")
        for i, name in enumerate(selected):
            prog.progress((i + 1) / len(selected), text=f"加载 {name}…")
            hist = _fetch_industry_index(name, days=period)
            if not hist.empty:
                all_hist[name] = hist
        prog.empty()

        if all_hist:
            # 构建涨跌幅 pivot
            chg_data = {}
            for name, hist in all_hist.items():
                s = hist.set_index("日期")["收盘价"].pct_change().dropna() * 100
                chg_data[name] = s
            chg_df = pd.DataFrame(chg_data)
            if not chg_df.empty:
                chg_df = chg_df.tail(period)
                _rotation_heatmap(chg_df.T, title=f"行业板块轮动（近{period}交易日）", key="ind_heatmap")

            # 相对强弱
            st.markdown("**相对强弱指数（期初=100）**")
            rs_data = {}
            for name, hist in all_hist.items():
                s = hist.set_index("日期")["收盘价"]
                rs_data[name] = s / s.iloc[0] * 100 if len(s) > 0 else pd.Series()
            rs_df = pd.DataFrame(rs_data).dropna()
            if not rs_df.empty:
                fig = go.Figure()
                for col in rs_df.columns:
                    fig.add_trace(go.Scatter(x=rs_df.index, y=rs_df[col], name=col))
                fig.add_trace(go.Scatter(x=rs_df.index, y=rs_df.mean(axis=1), name="均值",
                                         line=dict(dash="dash", color="#888")))
                fig.update_layout(template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                                  height=350, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h"))
                st.plotly_chart(fig, width="stretch", key="ind_rs")
        else:
            st.info("板块历史数据暂不可用")

    with t3:
        st.markdown("**选择板块查看成分股与资金流**")
        sel = st.selectbox("选择行业板块", df["板块"].tolist(), key="ind_detail_sel")
        if sel:
            row = df[df["板块"] == sel].iloc[0]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("涨跌幅", f"{row['涨跌幅']:+.2f}%")
            c2.metric("净流入", f"{row['净流入']:+.2f}亿")
            c3.metric("总成交额", f"{row['总成交额']:.2f}亿")
            c4.metric("涨/跌", f"{row['上涨家数']}/{row['下跌家数']}")

            # 板块资金流历史
            hist = _fetch_industry_index(sel, days=60)
            if not hist.empty:
                daily_flow = hist.set_index("日期")["成交额"].pct_change().dropna() * 0  # placeholder
                # 用收盘价变化作为资金流代理
                close = hist.set_index("日期")["收盘价"]
                daily_chg = close.pct_change().dropna() * 100
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=daily_chg.index, y=daily_chg, name="涨跌幅%",
                                         fill="tozeroy", fillcolor="rgba(229,69,69,0.15)"))
                fig.update_layout(template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                                  height=250, margin=dict(l=10, r=10, t=10, b=10))
                st.plotly_chart(fig, width="stretch")

            # 成分股
            try:
                import akshare as ak
                cons = ak.stock_board_industry_cons_em(symbol=sel)
                if cons is not None and not cons.empty:
                    st.caption(f"成分股 {len(cons)} 只")
                    st.dataframe(cons.head(30), width="stretch", hide_index=True, height=300)
            except Exception as e:
                st.warning(f"成分股获取失败：{e}")


def _render_concept():
    """💡 概念板块：概念列表 + 指数轮动对比。"""
    t1, t2 = st.tabs(["📋 概念列表", "📈 指数轮动对比"])

    with t1:
        df = _fetch_concept_names()
        if df.empty:
            st.warning("概念板块数据加载失败，请稍后重试")
            return
        st.markdown(f"**同花顺 {len(df)} 个概念板块**")
        # 搜索过滤
        q = st.text_input("🔍 搜索概念", placeholder="输入关键词如 AI、芯片、新能源…", key="con_search")
        filtered = df
        if q:
            filtered = df[df["name"].str.contains(q, case=False, na=False)]
            st.caption(f"匹配 {len(filtered)} 个概念")
        st.dataframe(filtered, width="stretch", hide_index=True, height=min(32 * (len(filtered) + 1) + 3, 600))

    with t2:
        st.markdown("**概念板块指数轮动对比**（选取概念，对比历史走势）")
        all_names = _fetch_concept_names()
        if all_names.empty:
            st.warning("概念列表加载失败")
            return
        options = all_names["name"].tolist()
        default_concepts = [n for n in ["人工智能", "芯片概念", "光伏概念", "白酒概念", "锂电池"] if n in options][:3]
        chosen = st.multiselect("选择概念板块（最多 8 个）", options, default=default_concepts,
                                max_selections=8, key="con_compare")
        period = st.radio("回看天数", [30, 60, 120, 250], index=1, horizontal=True, key="con_period")

        if chosen:
            prog = st.progress(0, text="加载概念指数…")
            indices = {}
            for i, name in enumerate(chosen):
                prog.progress((i + 1) / len(chosen), text=f"加载 {name}…")
                hist = _fetch_concept_index(name, days=period)
                if not hist.empty:
                    indices[name] = hist.set_index("日期")["收盘价"]
                time.sleep(0.3)  # 避免请求过快
            prog.empty()

            if indices:
                # 走势对比（归一化）
                st.markdown("**走势对比（期初=100）**")
                fig = go.Figure()
                for name, s in indices.items():
                    norm = s / s.iloc[0] * 100
                    fig.add_trace(go.Scatter(x=norm.index, y=norm, name=name))
                fig.add_trace(go.Scatter(x=norm.index, y=pd.DataFrame(indices).mean(axis=1) / pd.DataFrame(indices).iloc[0].mean() * 100,
                                         name="均值", line=dict(dash="dash", color="#888")))
                fig.update_layout(template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                                  height=380, margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h"))
                st.plotly_chart(fig, width="stretch")

                # 轮动热力图
                st.markdown("**概念轮动热力图**")
                chg_data = {}
                for name, s in indices.items():
                    chg_data[name] = s.pct_change().dropna() * 100
                chg_df = pd.DataFrame(chg_data).tail(period).T
                if not chg_df.empty:
                    _rotation_heatmap(chg_df, title=f"概念板块轮动（近{period}交易日）", key="con_heatmap")
            else:
                st.info("概念指数数据暂不可用")


def _render_regional():
    """🗺️ 地域板块（占位）。"""
    st.info("地域板块数据暂未接入（同花顺/东财公开 API 暂无地域板块实时资金流接口）。")
    st.markdown("""
    **可替代方案：**
    - 东财地域板块行情（`stock_board_region_name_em`，东财连接不稳定）
    - 手工维护地域-股票映射表，结合快照计算资金流
    - 接入 iFinD 地域板块报表（需开通权限）

    当前可用的板块分类：**行业板块**（90 个，含资金流）和 **概念板块**（375 个，含指数走势）。
    """)


render()
