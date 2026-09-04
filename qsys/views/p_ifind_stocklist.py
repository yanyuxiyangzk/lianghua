"""📋 个股行情：全市场A股和主要指数的完整列表"""

import math

import pandas as pd
import streamlit as st

import datasource
import ifind_hub

# 列定义：(数据库列名, 显示名, 格式化)
# 涨跌 = price - prev_close（计算列）
STOCK_COLS = [
    ("code", "代码", None),
    ("_name_display", "名称", None),
    ("price", "现价(元)", "{:.2f}"),
    ("change_pct", "涨跌幅(%)", "{:.2f}"),
    ("_change", "涨跌(元)", "{:.2f}"),
    ("speed", "涨速(%)", "{:.4f}"),
    ("turnover", "换手(%)", "{:.2f}"),
    ("quantity_ratio", "量比", "{:.2f}"),
    ("amplitude", "振幅(%)", "{:.2f}"),
    ("amount", "成交额(万)", lambda v: f"{v/1e4:.2f}" if v and pd.notna(v) else "-"),
    ("_float_shares_display", "流通股(万)", lambda v: f"{v/1e4:.2f}" if v and pd.notna(v) else "-"),
    ("_float_mv_display", "流通市值(亿)", lambda v: f"{v/1e8:.2f}" if v and pd.notna(v) else "-"),
    ("pe_ttm", "市盈率", "{:.2f}"),
]

# 计算列：涨跌 = price - prev_close
def _compute_change(row):
    p, pc = row.get("price"), row.get("prev_close")
    if p and pc:
        return round(p - pc, 2)
    return None

PAGE_SIZE = 20

# 市场分类定义：(标签, 筛选函数)
MARKET_TABS = [
    ("全部股票", lambda df: df),
    ("上证A股", lambda df: df[df["code"].str.startswith("SH6") & ~df["code"].str.startswith("SH688")]),
    ("深证A股", lambda df: df[df["code"].str.startswith(("SZ0", "SZ3")) & ~df["code"].str.startswith("SZ3")]),
    ("京证A股", lambda df: df[df["code"].str.startswith("BJ")]),
    ("创业板", lambda df: df[df["code"].str.startswith("SZ3")]),
    ("科创板", lambda df: df[df["code"].str.startswith("SH688")]),
]

SORT_OPTIONS = {
    "change_pct": "涨跌幅", "turnover": "换手率", "amount": "成交额",
    "pe_ttm": "市盈率", "total_mv": "总市值", "price": "现价",
    "quantity_ratio": "量比", "amplitude": "振幅",
}


def render():
    st.title("📋 行情")
    ifind_hub.header()

    tab_stock, tab_index = st.tabs(["📈 A股市场", "📊 A股指数"])

    with tab_stock:
        _render_stock_list()

    with tab_index:
        _render_index_list()


def _render_stock_list():
    """渲染A股列表（只读数据库，不直接调用iFinD API）"""
    # 从数据库读取数据
    try:
        db_df = datasource.get_stocklist_from_db()
    except Exception:
        db_df = pd.DataFrame()

    # 添加显示列：优先使用 float_shares/float_mv，否则使用 total_shares/total_mv
    if not db_df.empty:
        # 流通股：优先使用 float_shares，否则从 float_mv/price 计算，最后用 total_shares
        if "float_shares" in db_df.columns and "total_shares" in db_df.columns:
            db_df["_float_shares_display"] = db_df["float_shares"].fillna(db_df["total_shares"])
        elif "float_shares" in db_df.columns:
            db_df["_float_shares_display"] = db_df["float_shares"]
        elif "total_shares" in db_df.columns:
            db_df["_float_shares_display"] = db_df["total_shares"]
        else:
            db_df["_float_shares_display"] = None

        # 如果 float_shares_display 仍为 NaN，尝试从 float_mv/price 计算
        if "_float_shares_display" in db_df.columns:
            need_calc = db_df["_float_shares_display"].isna()
            if need_calc.any() and "float_mv" in db_df.columns and "price" in db_df.columns:
                calc = db_df.loc[need_calc, "float_mv"] / db_df.loc[need_calc, "price"]
                db_df.loc[need_calc, "_float_shares_display"] = calc

        # 流通市值：优先使用 float_mv，否则使用 total_mv
        if "float_mv" in db_df.columns and "total_mv" in db_df.columns:
            db_df["_float_mv_display"] = db_df["float_mv"].fillna(db_df["total_mv"])
        elif "float_mv" in db_df.columns:
            db_df["_float_mv_display"] = db_df["float_mv"]
        elif "total_mv" in db_df.columns:
            db_df["_float_mv_display"] = db_df["total_mv"]
        else:
            db_df["_float_mv_display"] = None
        
        # 判断新股（N字头）：涨跌幅 > 44%（主板）或 > 20%（创业板/科创板）
        def _is_new_stock(row):
            change_pct = row.get("change_pct")
            code = row.get("code", "")
            if change_pct is None:
                return False
            # 主板（SH6/SZ0）涨跌幅 > 44%
            if code.startswith("SH6") or code.startswith("SZ0"):
                return change_pct > 44
            # 创业板（SZ3）/ 科创板（SH688）涨跌幅 > 20%
            elif code.startswith("SZ3") or code.startswith("SH688"):
                return change_pct > 20
            return False
        
        # 添加新股标记
        db_df["_is_new"] = db_df.apply(_is_new_stock, axis=1)
        
        # 修改名称显示：新股加上 N 前缀
        db_df["_name_display"] = db_df.apply(
            lambda row: f"N{row['name']}" if row.get("_is_new") else row["name"], axis=1
        )

    fetched_at = None
    data_age_hours = None
    if not db_df.empty and "fetched_at" in db_df.columns:
        fetched_at = db_df["fetched_at"].iloc[0]
        if fetched_at:
            try:
                from datetime import datetime
                last = datetime.strptime(str(fetched_at), "%Y-%m-%d %H:%M:%S")
                data_age_hours = (datetime.now() - last).total_seconds() / 3600
            except Exception:
                pass

    # 数据库为空时提示
    if db_df.empty:
        st.info("数据库中暂无A股列表数据，请点击「🔄 同步数据」按钮首次拉取。")
        # 手动触发同步
        if st.button("🔄 同步数据", type="primary", key="stock_sync_first"):
            with st.spinner("正在同步A股列表（约4-5分钟）…"):
                from scheduler import job_ifind_stocklist_sync
                msg = job_ifind_stocklist_sync()
            st.success(msg)
            st.rerun()
        return

    # 状态提示
    age_text = ""
    if data_age_hours is not None:
        if data_age_hours < 1:
            age_text = f"{data_age_hours*60:.0f}分钟前"
        elif data_age_hours < 24:
            age_text = f"{data_age_hours:.0f}小时前"
        else:
            age_text = f"{data_age_hours/24:.0f}天前"

    # 过期提示
    if data_age_hours is not None and data_age_hours > 24:
        st.warning(f"⚠️ 数据已{age_text}未更新，建议点击「🔄 同步数据」按钮更新。")

    # 手动刷新按钮（触发调度器任务，不直接调用API）
    col1, col2, col3 = st.columns([1, 4, 1])
    with col1:
        if st.button("🔄 同步数据", type="secondary", key="stock_sync"):
            with st.spinner("正在同步A股列表（约4-5分钟）…"):
                from scheduler import job_ifind_stocklist_sync
                msg = job_ifind_stocklist_sync()
            st.success(msg)
            st.rerun()
    with col3:
        st.caption(f"更新于 {age_text or '未知'}")

    st.caption(f"共 {len(db_df)} 只 · 数据源：iFinD THS_RQ + THS_BD")

    # 搜索 + 排序（放在一行）
    c1, c2, c3 = st.columns([2, 1.5, 1.5])
    with c1:
        keyword = st.text_input("搜索代码/名称", "", key="stock_kw", label_visibility="collapsed",
                                placeholder="输入代码或名称关键字…")
    with c2:
        sort_col = st.selectbox("排序", list(SORT_OPTIONS.keys()),
                                format_func=lambda x: SORT_OPTIONS[x], key="stock_sort",
                                label_visibility="collapsed")
    with c3:
        sort_asc = st.radio("方向", ["降序", "升序"], horizontal=True, key="stock_asc",
                            label_visibility="collapsed")

    # 市场分类 tabs
    tabs = st.tabs([label for label, _ in MARKET_TABS])

    for tab, (label, filter_fn) in zip(tabs, MARKET_TABS):
        with tab:
            df = filter_fn(db_df.copy())

            # 搜索过滤
            if keyword:
                mask = df["code"].str.contains(keyword, case=False, na=False) | \
                       df["name"].str.contains(keyword, case=False, na=False)
                df = df[mask]

            # 排序
            if sort_col in df.columns:
                df = df.sort_values(sort_col, ascending=(sort_asc == "升序"), na_position="last")

            total = len(df)
            total_pages = max(1, math.ceil(total / PAGE_SIZE))

            # 页码
            page_key = f"page_{label}"
            if page_key not in st.session_state:
                st.session_state[page_key] = 0
            page = st.session_state[page_key]

            # 确保页码合法
            if page >= total_pages:
                page = total_pages - 1
                st.session_state[page_key] = page

            # 取当前页数据
            start = page * PAGE_SIZE
            page_df = df.iloc[start:start + PAGE_SIZE]

            # 格式化显示
            display_df = pd.DataFrame()
            for col_db, col_name, fmt in STOCK_COLS:
                if col_db == "_change":
                    # 计算列：涨跌 = price - prev_close
                    vals = [_compute_change(row) for _, row in page_df.iterrows()]
                    display_df[col_name] = [fmt.format(v) if v is not None and fmt else (v if v is not None else "-") for v in vals]
                elif col_db not in page_df.columns:
                    continue
                else:
                    series = page_df[col_db]
                    if fmt is None:
                        display_df[col_name] = series.values
                    elif callable(fmt):
                        display_df[col_name] = [fmt(v) for v in series]
                    else:
                        display_df[col_name] = [fmt.format(v) if pd.notna(v) and v is not None else "" for v in series]

            # 添加序号列
            display_df.insert(0, "序号", range(start + 1, start + 1 + len(display_df)))

            # 代码/名称列加 K线页超链接（点击跳转；名称嵌在 URL 里供正则提取显示文本）
            _codes = page_df["code"].values
            _names = page_df["_name_display"].values if "_name_display" in page_df.columns else page_df["name"].values
            display_df["代码"] = [f"/ifind-kline?code={c}" for c in _codes]
            display_df["名称"] = [f"/ifind-kline?code={c}&n={n}" for c, n in zip(_codes, _names)]

            # 单击选中行（仅高亮）；再点同一行（双击语义）跳转到该股K线页
            sel_event = st.dataframe(
                display_df, use_container_width=True, hide_index=True,
                height=35 * (len(display_df) + 1) + 3,
                column_config={
                    "代码": st.column_config.LinkColumn("代码", display_text=r"[?&]code=(.+)$"),
                    "名称": st.column_config.LinkColumn("名称", display_text=r"[?&]n=(.+)$"),
                },
                on_select="rerun", selection_mode="single-row",
                key=f"tbl_{label}")
            _pk = "_stk_pending"
            _rows = sel_event.selection.rows if sel_event else []
            if _rows:
                st.session_state[_pk] = (label, page, _rows[0])
            else:
                _pend = st.session_state.get(_pk)
                if _pend and _pend[0] == label and _pend[1] == page:
                    st.session_state["kline_code"] = page_df.iloc[_pend[2]]["code"]
                    st.session_state.pop(_pk, None)
                    st.switch_page("views/p_ifind_kline.py")
                elif _pend and _pend[0] == label and _pend[1] != page:
                    st.session_state.pop(_pk, None)  # 翻页后清空待跳转状态
            if label == "全部股票":
                st.caption("💡 单击选中行，**双击**（再点一次同一行）跳转到该股的「📈 K线数据」页")

            # 分页导航（数据下方）
            nav1, nav2, nav3, nav4, nav5 = st.columns([1, 1, 2, 1, 1])
            with nav1:
                if st.button("◀ 上一页", key=f"prev_{label}", disabled=(page <= 0)):
                    st.session_state[page_key] = page - 1
                    st.rerun()
            with nav2:
                if st.button("下一页 ▶", key=f"next_{label}", disabled=(page >= total_pages - 1)):
                    st.session_state[page_key] = page + 1
                    st.rerun()
            with nav3:
                st.caption(f"共 {total} 只 · 第 {page + 1}/{total_pages} 页 · 每页 {PAGE_SIZE} 条")
            with nav4:
                jump = st.number_input("跳转", min_value=1, max_value=total_pages,
                                       value=page + 1, key=f"jump_{label}",
                                       label_visibility="collapsed")
            with nav5:
                if st.button("跳转", key=f"go_{label}"):
                    st.session_state[page_key] = jump - 1
                    st.rerun()

            # 导出当前筛选结果（全部，非单页）
            csv = df.to_csv(index=False, encoding="utf-8-sig")
            st.download_button(
                label=f"📥 导出{label}CSV（{total}条）",
                data=csv,
                file_name=f"A股列表_{label}_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                key=f"dl_{label}",
            )


def _render_index_list():
    """渲染A股指数（iFinD 定时落库 → 读库展示，默认查询 + 20条/页分页）"""
    db_df = datasource.get_indexlist_from_db()

    # 数据库为空时提示
    if db_df.empty:
        st.info("数据库中暂无指数数据，请点击「🔄 同步数据」按钮首次拉取。")
        if st.button("🔄 同步数据", type="primary", key="index_sync_first"):
            with st.spinner("正在通过 iFinD 同步指数数据（约30秒）…"):
                n = datasource.fetch_indexlist_to_db()
            st.success(f"同步完成：{n} 条指数")
            st.rerun()
        return

    # 搜索 + 分类 + 同步（搜索框默认空）
    c1, c2, c3 = st.columns([2, 1.5, 1])
    with c1:
        keyword = st.text_input("搜索代码/名称", "", key="index_kw", label_visibility="collapsed",
                                placeholder="输入指数代码或名称关键字…")
    with c2:
        cats = ["全部"] + [x for x in ["宽基指数", "沪深指数", "行业指数", "主题指数"]
                           if x in set(db_df["category"].dropna())]
        cat_filter = st.selectbox("分类筛选", cats, key="index_cat", label_visibility="collapsed")
    with c3:
        if st.button("🔄 同步数据", key="index_sync"):
            with st.spinner("正在通过 iFinD 同步指数数据（约30秒）…"):
                n = datasource.fetch_indexlist_to_db()
            st.success(f"同步完成：{n} 条指数")
            st.rerun()

    # 应用筛选
    df = db_df.copy()
    if keyword:
        mask = df["code"].str.contains(keyword, case=False, na=False) | \
               df["name"].str.contains(keyword, case=False, na=False)
        df = df[mask]
    if cat_filter != "全部":
        df = df[df["category"] == cat_filter]

    fetched_at = db_df["fetched_at"].max() if "fetched_at" in db_df.columns else ""
    st.caption(f"共 {len(df)} 个指数 · 数据源：同花顺 iFinD（HTTP 优先）· 更新于 {fetched_at}")

    # 分页（20条/页，与 A股市场 tab 同款）
    INDEX_PAGE_SIZE = 20
    total = len(df)
    total_pages = max(1, math.ceil(total / INDEX_PAGE_SIZE))

    page_key = "page_index"
    if page_key not in st.session_state:
        st.session_state[page_key] = 0
    page = st.session_state[page_key]
    if page >= total_pages:
        page = total_pages - 1
        st.session_state[page_key] = page

    start = page * INDEX_PAGE_SIZE
    page_df = df.iloc[start:start + INDEX_PAGE_SIZE]

    # 格式化显示
    display_df = pd.DataFrame()
    _icodes = page_df["code"].values
    _inames = page_df["name"].values
    display_df["指数代码"] = [f"/ifind-kline?code={c}" for c in _icodes]
    display_df["指数名称"] = [f"/ifind-kline?code={c}&n={n}" for c, n in zip(_icodes, _inames)]
    display_df["最新价"] = [f"{v:.2f}" if pd.notna(v) else "" for v in page_df["price"]]
    display_df["涨跌额"] = [f"{(p - pc):+.2f}" if pd.notna(p) and pd.notna(pc) else ""
                          for p, pc in zip(page_df["price"], page_df["prev_close"])]
    display_df["涨跌幅(%)"] = [f"{v:+.2f}" if pd.notna(v) else "" for v in page_df["change_pct"]]
    display_df["昨收"] = [f"{v:.2f}" if pd.notna(v) else "" for v in page_df["prev_close"]]
    display_df["今开"] = [f"{v:.2f}" if pd.notna(v) else "" for v in page_df["open"]]
    display_df["最高价"] = [f"{v:.2f}" if pd.notna(v) else "" for v in page_df["high"]]
    display_df["成交量(亿手)"] = [f"{v/1e8:.2f}" if pd.notna(v) else "" for v in page_df["volume"]]
    display_df["成交额(亿)"] = [f"{v/1e8:.1f}" if pd.notna(v) else "" for v in page_df["amount"]]
    display_df.insert(0, "序号", range(start + 1, start + 1 + len(display_df)))

    # 单击选中行（仅高亮）；再点同一行（双击语义）跳转到该指数K线页
    idx_sel = st.dataframe(display_df, use_container_width=True, hide_index=True,
                           height=35 * (len(display_df) + 1) + 3,
                           column_config={
                               "指数代码": st.column_config.LinkColumn("指数代码", display_text=r"[?&]code=(.+)$"),
                               "指数名称": st.column_config.LinkColumn("指数名称", display_text=r"[?&]n=(.+)$"),
                           },
                           on_select="rerun", selection_mode="single-row",
                           key="tbl_index")
    _ipk = "_idx_pending"
    _irows = idx_sel.selection.rows if idx_sel else []
    if _irows:
        st.session_state[_ipk] = (page, _irows[0])
    else:
        _ipend = st.session_state.get(_ipk)
        if _ipend and _ipend[0] == page:
            st.session_state["kline_code"] = page_df.iloc[_ipend[1]]["code"]
            st.session_state.pop(_ipk, None)
            st.switch_page("views/p_ifind_kline.py")
        elif _ipend:
            st.session_state.pop(_ipk, None)
    st.caption("💡 单击选中行，**双击**（再点一次同一行）跳转到该指数的「📈 K线数据」页")

    # 分页导航
    nav1, nav2, nav3, nav4, nav5 = st.columns([1, 1, 2, 1, 1])
    with nav1:
        if st.button("◀ 上一页", key="prev_index", disabled=(page <= 0)):
            st.session_state[page_key] = page - 1
            st.rerun()
    with nav2:
        if st.button("下一页 ▶", key="next_index", disabled=(page >= total_pages - 1)):
            st.session_state[page_key] = page + 1
            st.rerun()
    with nav3:
        st.caption(f"共 {total} 个指数 · 第 {page + 1}/{total_pages} 页 · 每页 {INDEX_PAGE_SIZE} 条")
    with nav4:
        jump = st.number_input("跳转", min_value=1, max_value=total_pages,
                               value=page + 1, key="jump_index",
                               label_visibility="collapsed")
    with nav5:
        if st.button("跳转", key="go_index"):
            st.session_state[page_key] = jump - 1
            st.rerun()

    # 导出当前筛选结果（全部，非单页）
    csv = df.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        label=f"📥 导出指数CSV（{total}条）",
        data=csv,
        file_name=f"A股指数_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key="dl_index",
    )


render()
