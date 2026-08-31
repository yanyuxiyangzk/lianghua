"""📋 股票/指数列表（同花顺 iFinD）：全市场A股和主要指数的完整列表"""

import pandas as pd
import streamlit as st

import datasource
import ifind_hub


def render():
    st.title("📋 股票/指数列表（同花顺 iFinD）")
    ifind_hub.header()

    # 选项卡
    tab_stock, tab_index = st.tabs(["📈 A股列表", "📊 指数列表"])

    with tab_stock:
        _render_stock_list()

    with tab_index:
        _render_index_list()


def _render_stock_list():
    """渲染A股列表（使用 iFinD 智能选股接口）"""
    st.subheader("全市场A股")

    # 查询参数
    with st.expander("⚙️ 查询参数", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            # 智能选股条件
            condition = st.text_input(
                "选股条件",
                "全部A股",
                key="stock_condition",
                help="iFinD 智能选股条件，如：'全部A股'、'沪深300成分股'、'市盈率小于20' 等"
            )
        with c2:
            # 返回字段
            indicators = st.text_input(
                "返回字段",
                "thscode,ths_lh_latest_stock,ths_lh_change_ratio_stock,ths_pe_ttm_stock,ths_pb_stock,ths_market_value_stock",
                key="stock_indicators",
                help="逗号分隔的指标代码"
            )

        if st.button("查询A股列表", key="stock_go"):
            _go_stock_list(condition, indicators)

    # 自动查询
    _auto_stock_list(condition, indicators)

    # 渲染结果
    _render_stock_result("stock_list")


def _go_stock_list(condition: str, indicators: str):
    """执行A股列表查询"""
    try:
        # 使用 THS_WC 智能选股接口
        result = datasource.ths_call(
            "THS_WC",
            condition,
            "stock",
            indicators
        )
        st.session_state["ifind_res_stock_list"] = result
        st.session_state["ifind_call_stock_list"] = (condition, indicators)
        st.session_state["ifind_ts_stock_list"] = pd.Timestamp.now()
    except Exception as e:
        st.error(f"查询失败：{e}")


def _auto_stock_list(condition: str, indicators: str):
    """首次进页面自动查询"""
    if "ifind_res_stock_list" not in st.session_state:
        _go_stock_list(condition, indicators)


def _render_stock_result(key: str):
    """渲染股票列表结果"""
    res = st.session_state.get(f"ifind_res_{key}")
    ts = st.session_state.get(f"ifind_ts_{key}")

    if res is None:
        st.info("点击「查询A股列表」开始查询")
        return

    # 解析结果
    if isinstance(res, tuple) and len(res) >= 2:
        df, err = res[0], res[1]
        if err not in (0, None):
            st.error(f"查询失败，错误码：{err}")
            return
        if df is not None and not df.empty:
            # 显示统计
            st.caption(f"共 {len(df)} 条记录 · 数据源：同花顺 iFinD"
                       + (f" · 查询于 {ts:%H:%M:%S}" if ts else ""))

            # 显示表格
            st.dataframe(df, use_container_width=True, height=600)

            # 导出按钮
            csv = df.to_csv(index=False, encoding="utf-8-sig")
            st.download_button(
                label="📥 导出CSV",
                data=csv,
                file_name=f"A股列表_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )
        else:
            st.warning("查询成功但返回为空")
    else:
        st.warning("结果格式异常")


def _render_index_list():
    """渲染指数列表（使用 iFinD 接口）"""
    st.subheader("主要指数")

    # 查询参数
    with st.expander("⚙️ 查询参数", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            # 指数代码列表
            codes = st.text_input(
                "指数代码（逗号分隔）",
                "000001.SH,399001.SZ,399006.SZ,000300.SH,000905.SH,000852.SH,000016.SH,000688.SH",
                key="index_codes",
                help="多个指数代码用逗号分隔"
            )
        with c2:
            # 返回字段
            indicators = st.text_input(
                "返回字段",
                "thscode,ths_lh_latest_stock,ths_lh_change_ratio_stock,ths_pe_ttm_stock,ths_pb_stock",
                key="index_indicators",
                help="逗号分隔的指标代码"
            )

        if st.button("查询指数列表", key="index_go"):
            _go_index_list(codes, indicators)

    # 自动查询
    _auto_index_list(codes, indicators)

    # 渲染结果
    _render_index_result("index_list")


def _go_index_list(codes: str, indicators: str):
    """执行指数列表查询"""
    try:
        # 使用 THS_Sequence 接口获取指数实时行情
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        result = datasource.ths_realtime(code_list, indicators)
        st.session_state["ifind_res_index_list"] = result
        st.session_state["ifind_call_index_list"] = (codes, indicators)
        st.session_state["ifind_ts_index_list"] = pd.Timestamp.now()
    except Exception as e:
        st.error(f"查询失败：{e}")


def _auto_index_list(codes: str, indicators: str):
    """首次进页面自动查询"""
    if "ifind_res_index_list" not in st.session_state:
        _go_index_list(codes, indicators)


def _render_index_result(key: str):
    """渲染指数列表结果"""
    res = st.session_state.get(f"ifind_res_{key}")
    ts = st.session_state.get(f"ifind_ts_{key}")

    if res is None:
        st.info("点击「查询指数列表」开始查询")
        return

    # 解析结果
    if isinstance(res, tuple) and len(res) >= 2:
        df, err = res[0], res[1]
        if err not in (0, None):
            st.error(f"查询失败，错误码：{err}")
            return
        if df is not None and not df.empty:
            # 显示统计
            st.caption(f"共 {len(df)} 条记录 · 数据源：同花顺 iFinD"
                       + (f" · 查询于 {ts:%H:%M:%S}" if ts else ""))

            # 显示表格
            st.dataframe(df, use_container_width=True, height=600)

            # 导出按钮
            csv = df.to_csv(index=False, encoding="utf-8-sig")
            st.download_button(
                label="📥 导出CSV",
                data=csv,
                file_name=f"指数列表_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )
        else:
            st.warning("查询成功但返回为空")
    else:
        st.warning("结果格式异常")


render()
