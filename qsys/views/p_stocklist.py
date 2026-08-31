"""📋 股票/指数列表：全市场A股和主要指数的完整列表"""

import pandas as pd
import streamlit as st

import datasource


def render():
    st.title("📋 股票/指数列表")

    # 选项卡
    tab_stock, tab_index = st.tabs(["📈 A股列表", "📊 指数列表"])

    with tab_stock:
        _render_stock_list()

    with tab_index:
        _render_index_list()


def _render_stock_list():
    """渲染A股列表"""
    st.subheader("全市场A股")

    # 搜索和筛选
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        search = st.text_input("搜索股票（代码/名称）", key="stock_search",
                               placeholder="输入股票代码或名称...")
    with c2:
        market_filter = st.selectbox("市场筛选", ["全部", "上海主板", "深圳主板", "创业板", "科创板"],
                                     key="stock_market")
    with c3:
        if st.button("🔄 刷新列表", key="stock_refresh"):
            st.cache_data.clear()

    # 获取数据
    with st.spinner("正在获取A股列表..."):
        df = datasource.get_all_a_stocks()

    if df.empty:
        st.warning("未获取到A股列表数据，请检查网络连接")
        return

    # 应用筛选
    filtered = df.copy()

    # 搜索筛选
    if search:
        mask = filtered["code"].str.contains(search, case=False, na=False) | \
               filtered["name"].str.contains(search, case=False, na=False)
        filtered = filtered[mask]

    # 市场筛选
    if market_filter != "全部":
        market_map = {
            "上海主板": "SH",
            "深圳主板": "SZ",
            "创业板": "SZ",  # 创业板代码以300开头
            "科创板": "SH",  # 科创板代码以688开头
        }
        market_code = market_map[market_filter]
        if market_filter == "创业板":
            filtered = filtered[filtered["code"].str.startswith("300")]
        elif market_filter == "科创板":
            filtered = filtered[filtered["code"].str.startswith("688")]
        else:
            filtered = filtered[filtered["market"] == market_code]

    # 显示统计
    st.caption(f"共 {len(filtered)} 只股票 · 数据源：akshare")

    # 显示表格
    if not filtered.empty:
        # 格式化显示
        display_df = filtered.copy()
        display_df.insert(0, "序号", range(1, len(display_df) + 1))

        # 使用 Streamlit 数据表格
        st.dataframe(
            display_df[["序号", "code", "name", "market"]],
            column_config={
                "序号": st.column_config.NumberColumn("序号", width="small"),
                "code": st.column_config.TextColumn("代码", width="medium"),
                "name": st.column_config.TextColumn("名称", width="large"),
                "market": st.column_config.TextColumn("市场", width="small"),
            },
            use_container_width=True,
            height=600,
            hide_index=True,
        )

        # 导出按钮
        csv = display_df.to_csv(index=False, encoding="utf-8-sig")
        st.download_button(
            label="📥 导出CSV",
            data=csv,
            file_name=f"A股列表_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )
    else:
        st.info("没有匹配的股票")


def _render_index_list():
    """渲染指数列表"""
    st.subheader("主要指数")

    # 搜索
    search = st.text_input("搜索指数（代码/名称）", key="index_search",
                           placeholder="输入指数代码或名称...")

    # 获取数据
    with st.spinner("正在获取指数列表..."):
        df = datasource.get_index_list()

    if df.empty:
        st.warning("未获取到指数列表数据，请检查网络连接")
        return

    # 应用筛选
    filtered = df.copy()
    if search:
        mask = filtered["code"].str.contains(search, case=False, na=False) | \
               filtered["name"].str.contains(search, case=False, na=False)
        filtered = filtered[mask]

    # 显示统计
    st.caption(f"共 {len(filtered)} 个指数 · 数据源：akshare")

    # 显示表格
    if not filtered.empty:
        # 格式化显示
        display_df = filtered.copy()
        display_df.insert(0, "序号", range(1, len(display_df) + 1))

        # 使用 Streamlit 数据表格
        st.dataframe(
            display_df[["序号", "code", "name", "market"]],
            column_config={
                "序号": st.column_config.NumberColumn("序号", width="small"),
                "code": st.column_config.TextColumn("代码", width="medium"),
                "name": st.column_config.TextColumn("名称", width="large"),
                "market": st.column_config.TextColumn("市场", width="small"),
            },
            use_container_width=True,
            height=600,
            hide_index=True,
        )

        # 导出按钮
        csv = display_df.to_csv(index=False, encoding="utf-8-sig")
        st.download_button(
            label="📥 导出CSV",
            data=csv,
            file_name=f"指数列表_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )
    else:
        st.info("没有匹配的指数")


render()
