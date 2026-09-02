"""📋 股票/指数列表：全市场A股和主要指数的完整列表（iFinD 定时落库 → 读库展示）"""

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
    """渲染A股列表（iFinD 定时落库 → 读库展示，与「个股行情」页同一数据链路：HTTP 优先 / SDK 其次，全同花顺数据）"""
    st.subheader("全市场A股")

    df = datasource.get_stocklist_from_db()

    # 搜索和筛选
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        search = st.text_input("搜索股票（代码/名称）", key="stock_search",
                               placeholder="输入股票代码或名称...")
    with c2:
        market_filter = st.selectbox("市场筛选",
                                     ["全部", "上海主板", "深圳主板", "创业板", "科创板", "北交所"],
                                     key="stock_market")
    with c3:
        if st.button("🔄 同步数据", key="stock_sync",
                     help="调用 iFinD 接口拉取全量A股+北交所数据写入本地库（约10分钟）"):
            with st.spinner("正在通过 iFinD 同步A股数据（约10分钟）…"):
                from scheduler import job_ifind_stocklist_sync
                msg = job_ifind_stocklist_sync()
            st.success(msg)
            st.rerun()

    if df.empty:
        st.info("数据库暂无A股数据：点击「🔄 同步数据」立即拉取，"
                "或在 ⏰定时任务 页开启「iFinD A股列表同步」每日自动入库。")
        return

    # 应用筛选
    filtered = df.copy()

    # 搜索筛选
    if search:
        mask = filtered["code"].str.contains(search, case=False, na=False) | \
               filtered["name"].str.contains(search, case=False, na=False)
        filtered = filtered[mask]

    # 市场筛选（代码前缀：SH600519 / SZ000001 / BJ920002 格式）
    if market_filter == "上海主板":
        filtered = filtered[filtered["code"].str.startswith("SH6")
                            & ~filtered["code"].str.startswith("SH688")]
    elif market_filter == "深圳主板":
        filtered = filtered[filtered["code"].str.startswith("SZ0")]
    elif market_filter == "创业板":
        filtered = filtered[filtered["code"].str.startswith("SZ300")]
    elif market_filter == "科创板":
        filtered = filtered[filtered["code"].str.startswith("SH688")]
    elif market_filter == "北交所":
        filtered = filtered[filtered["code"].str.startswith("BJ")]

    # 显示统计
    fetched_at = df["fetched_at"].max() if "fetched_at" in df.columns else ""
    st.caption(f"共 {len(filtered)} 只股票 · 数据源：同花顺 iFinD（HTTP 优先）· 更新于 {fetched_at}")

    # 显示表格
    if not filtered.empty:
        # 格式化显示
        display_df = filtered.copy()
        display_df.insert(0, "序号", range(1, len(display_df) + 1))

        # 使用 Streamlit 数据表格
        st.dataframe(
            display_df[["序号", "code", "name", "market", "price", "change_pct", "pe_ttm"]],
            column_config={
                "序号": st.column_config.NumberColumn("序号", width="small"),
                "code": st.column_config.TextColumn("代码", width="medium"),
                "name": st.column_config.TextColumn("名称", width="large"),
                "market": st.column_config.TextColumn("市场", width="small"),
                "price": st.column_config.NumberColumn("最新价", format="%.2f"),
                "change_pct": st.column_config.NumberColumn("涨跌幅%", format="%+.2f"),
                "pe_ttm": st.column_config.NumberColumn("市盈率", format="%.2f"),
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
    """渲染指数列表（iFinD 定时落库 → 读库展示，与「个股行情」页同一数据链路：HTTP 优先 / SDK 其次，全同花顺数据）"""
    st.subheader("主要指数")

    df = datasource.get_indexlist_from_db()

    # 搜索/分类/同步
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        search = st.text_input("搜索指数（代码/名称）", key="index_search",
                               placeholder="输入指数代码或名称...")
    with c2:
        cats = ["全部"]
        if not df.empty and "category" in df.columns:
            cats += [x for x in ["宽基指数", "沪深指数", "行业指数", "主题指数"]
                     if x in set(df["category"].dropna())]
        cat_filter = st.selectbox("分类筛选", cats, key="index_cat")
    with c3:
        if st.button("🔄 同步指数数据", key="index_sync",
                     help="调用 iFinD 接口拉取最新指数列表与行情写入本地库（约30秒）"):
            with st.spinner("正在通过 iFinD 同步指数数据…"):
                n = datasource.fetch_indexlist_to_db()
            if n > 0:
                st.success(f"同步完成：{n} 条指数")
                st.rerun()
            else:
                st.error("同步失败：请检查 iFinD 凭证/额度（可到 ⏰定时任务 页看日志）")

    if df.empty:
        st.info("数据库暂无指数数据：点击「🔄 同步指数数据」立即拉取，"
                "或在 ⏰定时任务 页开启「iFinD 指数列表同步」每日自动入库。")
        return

    # 应用筛选
    filtered = df.copy()
    if search:
        mask = filtered["code"].str.contains(search, case=False, na=False) | \
               filtered["name"].str.contains(search, case=False, na=False)
        filtered = filtered[mask]
    if cat_filter != "全部":
        filtered = filtered[filtered["category"] == cat_filter]

    # 显示统计
    fetched_at = df["fetched_at"].max() if "fetched_at" in df.columns else ""
    st.caption(f"共 {len(filtered)} 个指数 · 数据源：同花顺 iFinD（HTTP 优先）· 更新于 {fetched_at}")

    # 显示表格
    if not filtered.empty:
        display_df = filtered.copy()
        display_df.insert(0, "序号", range(1, len(display_df) + 1))
        display_df["amount_yi"] = display_df["amount"] / 1e8  # 成交额 → 亿元

        st.dataframe(
            display_df[["序号", "code", "name", "category", "price",
                        "change_pct", "amount_yi"]],
            column_config={
                "序号": st.column_config.NumberColumn("序号", width="small"),
                "code": st.column_config.TextColumn("代码", width="medium"),
                "name": st.column_config.TextColumn("名称", width="large"),
                "category": st.column_config.TextColumn("分类", width="small"),
                "price": st.column_config.NumberColumn("最新价", format="%.3f"),
                "change_pct": st.column_config.NumberColumn("涨跌幅%", format="%+.2f"),
                "amount_yi": st.column_config.NumberColumn("成交额(亿)", format="%.1f"),
            },
            use_container_width=True,
            height=600,
            hide_index=True,
        )

        # 导出按钮
        csv = display_df.drop(columns=["amount_yi"]).to_csv(index=False, encoding="utf-8-sig")
        st.download_button(
            label="📥 导出CSV",
            data=csv,
            file_name=f"指数列表_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )
    else:
        st.info("没有匹配的指数")



render()
