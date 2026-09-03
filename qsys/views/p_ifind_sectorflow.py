"""🌐 板块资金流（同花顺 iFinD）：板块资金流向 / 板块行情 / 板块轮动。

全部走问财语义查询（THS_WCQuery / HTTP smart_stock_picking，HTTP 优先），
一次查询返回全量板块数据（行业板块 410 / 概念板块若干），带 [日期] 后缀的列名归一化。
"""

import math
import re

import pandas as pd
import streamlit as st

import datasource
import ifind_hub

PAGE_SIZE = 20


@st.cache_data(ttl=300, show_spinner=False)
def _wc(query: str) -> pd.DataFrame:
    """问财查询（HTTP 优先；缓存5分钟避免页面交互反复触发）。"""
    df, _res, err = datasource.ths_wcquery(query, "index")
    if err not in (0, None) or df is None or df.empty:
        return pd.DataFrame()
    # 列名归一："指数@涨跌幅:前复权[20260902]" → "涨跌幅"；"指数@主力资金流向[20260902]" → "主力资金流向"
    out = {}
    for c in df.columns:
        name = str(c)
        if "@" in name:
            name = name.split("@", 1)[1]
        name = re.sub(r"\[.*?\]", "", name)
        name = re.sub(r":.*$", "", name).strip()
        out[c] = name
    df = df.rename(columns=out)
    df = df.loc[:, ~df.columns.duplicated()]  # 归一化后去重名列（排名/排名名次 等与主列重名）
    # 数值列转 float（问财返回字符串）
    for c in df.columns:
        if c not in ("指数代码", "指数简称", "股票市场类型") and df[c].dtype == object:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _show(df: pd.DataFrame, caption: str, sort_col: str = None, export_name: str = "板块",
          page_key: str = "pg"):
    if df.empty:
        st.warning("查询成功但返回为空（非交易时段/接口限流），可稍后刷新重试")
        return
    if sort_col and sort_col in df.columns:
        df = df.sort_values(sort_col, ascending=False, na_position="last")
    st.caption(caption + f" · 共 {len(df)} 个板块")

    # 分页（20条/页）
    total = len(df)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    pk = f"page_{page_key}"
    if pk not in st.session_state:
        st.session_state[pk] = 0
    page = st.session_state[pk]
    if page >= total_pages:
        page = total_pages - 1
        st.session_state[pk] = page

    start = page * PAGE_SIZE
    page_df = df.iloc[start:start + PAGE_SIZE]
    st.dataframe(page_df, use_container_width=True, hide_index=True,
                 height=35 * (len(page_df) + 1) + 3)

    # 分页导航
    nav1, nav2, nav3, nav4, nav5 = st.columns([1, 1, 2, 1, 1])
    with nav1:
        if st.button("◀ 上一页", key=f"prev_{page_key}", disabled=(page <= 0)):
            st.session_state[pk] = page - 1
            st.rerun()
    with nav2:
        if st.button("下一页 ▶", key=f"next_{page_key}", disabled=(page >= total_pages - 1)):
            st.session_state[pk] = page + 1
            st.rerun()
    with nav3:
        st.caption(f"共 {total} 个板块 · 第 {page + 1}/{total_pages} 页 · 每页 {PAGE_SIZE} 条")
    with nav4:
        jump = st.number_input("跳转", min_value=1, max_value=total_pages,
                               value=page + 1, key=f"jump_{page_key}",
                               label_visibility="collapsed")
    with nav5:
        if st.button("跳转", key=f"go_{page_key}"):
            st.session_state[pk] = jump - 1
            st.rerun()

    st.download_button("📥 导出CSV", df.to_csv(index=False, encoding="utf-8-sig"),
                       file_name=f"{export_name}_{pd.Timestamp.now():%Y%m%d}.csv",
                       mime="text/csv", key=f"dl_{page_key}")


def render():
    st.title("🌐 板块资金流（同花顺 iFinD）")
    ifind_hub.header()
    tab_flow, tab_sector, tab_turnover = st.tabs(["💰 资金流向", "🏛️ 板块行情", "🔄 板块轮动"])
    with tab_flow:
        _render_flow()
    with tab_sector:
        _render_sector()
    with tab_turnover:
        _render_turnover()


def _render_flow():
    st.subheader("板块资金流向（主力资金净流入，当日）")
    if st.button("🔄 刷新", key="flow_refresh"):
        _wc.clear()
    for label, query in [("行业板块", "行业板块，主力净流入"),
                         ("概念板块", "概念板块，主力净流入")]:
        df = _wc(query)
        if df.empty:
            continue
        keep = [c for c in ["指数代码", "指数简称", "主力资金流向"] if c in df.columns]
        df = df[keep].rename(columns={"指数代码": "板块代码", "指数简称": "板块",
                                      "主力资金流向": "主力净流入(元)"})
        if "主力净流入(元)" in df.columns:
            df["主力净流入(亿)"] = df["主力净流入(元)"] / 1e8
            df = df.drop(columns=["主力净流入(元)"])
            df["主力净流入(亿)"] = df["主力净流入(亿)"].round(2)
        st.markdown(f"**{label}**")
        _show(df, "数据源：同花顺问财 · 主力资金流向", "主力净流入(亿)", f"板块资金流向_{label}", page_key=f"flow_{label}")


def _render_sector():
    st.subheader("板块行情（全量板块指数）")
    c1, c2 = st.columns([1, 3])
    with c1:
        kind = st.radio("板块类型", ["行业板块", "概念板块"], horizontal=True, key="sector_kind")
    with c2:
        if st.button("🔄 刷新", key="sector_refresh"):
            _wc.clear()
    df = _wc(f"{kind}，最新价，涨跌幅，成交额")
    if df.empty:
        st.warning("查询成功但返回为空（非交易时段/接口限流），可稍后刷新重试")
        return
    keep = [c for c in ["指数代码", "指数简称", "收盘价", "涨跌幅", "成交额"] if c in df.columns]
    df = df[keep].rename(columns={"指数代码": "板块代码", "指数简称": "板块",
                                  "收盘价": "最新价", "成交额": "成交额(元)"})
    if "成交额(元)" in df.columns:
        df["成交额(亿)"] = (df["成交额(元)"] / 1e8).round(1)
        df = df.drop(columns=["成交额(元)"])
    if "涨跌幅" in df.columns:
        df["涨跌幅"] = df["涨跌幅"].round(2)
    _show(df, f"数据源：同花顺问财 · {kind}", "涨跌幅", f"板块行情_{kind}", page_key=f"sector_{kind}")


def _render_turnover():
    st.subheader("板块轮动（区间涨跌幅排名）")
    c1, c2 = st.columns([1, 3])
    with c1:
        span = st.radio("轮动区间", ["近5日", "近20日", "近60日"], horizontal=True, key="turnover_span")
        kind = st.radio("板块类型", ["行业板块", "概念板块"], horizontal=True, key="turnover_kind")
    with c2:
        if st.button("🔄 刷新", key="turnover_refresh"):
            _wc.clear()
    df = _wc(f"{kind}{span}涨跌幅排名")
    if df.empty:
        st.warning("查询成功但返回为空（非交易时段/接口限流），可稍后刷新重试")
        return
    ret_col = next((c for c in df.columns if "区间涨跌幅" in c and "排名" not in c), None)
    keep = [c for c in ["指数代码", "指数简称"] if c in df.columns] + ([ret_col] if ret_col else [])
    df = df[keep].rename(columns={"指数代码": "板块代码", "指数简称": "板块",
                                  ret_col: f"{span}涨跌幅(%)" if ret_col else ""})
    _show(df, f"数据源：同花顺问财 · {kind}{span}区间涨跌幅排名", f"{span}涨跌幅(%)",
          f"板块轮动_{kind}_{span}", page_key=f"turnover_{kind}_{span}")


render()
