"""🌐 板块资金流（同花顺 iFinD）：行业板块资金流向与轮动"""

import pandas as pd
import streamlit as st

import datasource
import ifind_hub


def render():
    st.title("🌐 板块资金流（同花顺 iFinD）")
    ifind_hub.header()

    # 选项卡
    tab_flow, tab_sector, tab_turnover = st.tabs(["💰 资金流向", "🏛️ 板块行情", "🔄 板块轮动"])

    with tab_flow:
        _render_flow()

    with tab_sector:
        _render_sector()

    with tab_turnover:
        _render_turnover()


def _render_flow():
    """渲染板块资金流向"""
    st.subheader("板块资金流向")

    # 查询参数
    with st.expander("⚙️ 查询参数", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            # 板块类型
            sector_type = st.selectbox(
                "板块类型",
                ["行业板块", "概念板块", "地域板块"],
                key="flow_sector_type",
                help="选择要查询的板块类型"
            )
        with c2:
            # 返回字段
            indicators = st.text_input(
                "返回字段",
                "thscode,latest,changeRatio,volume,amount",
                key="flow_indicators",
                help="逗号分隔的指标代码，如：thscode,latest,changeRatio,volume,amount"
            )

        if st.button("查询板块资金流向", key="flow_go"):
            _go_flow(sector_type, indicators)

    # 自动查询
    _auto_flow(sector_type, indicators)

    # 渲染结果
    _render_flow_result("flow_data")


def _go_flow(sector_type: str, indicators: str):
    """执行板块资金流向查询"""
    try:
        # 使用 THS_WC 智能选股接口查询板块
        condition = f"{sector_type}全部"
        result = datasource.ths_call(
            "THS_WC",
            condition,
            "index",
            indicators
        )
        st.session_state["ifind_res_flow_data"] = result
        st.session_state["ifind_call_flow_data"] = (sector_type, indicators)
        st.session_state["ifind_ts_flow_data"] = pd.Timestamp.now()
    except Exception as e:
        st.error(f"查询失败：{e}")


def _auto_flow(sector_type: str, indicators: str):
    """首次进页面自动查询"""
    if "ifind_res_flow_data" not in st.session_state:
        _go_flow(sector_type, indicators)


def _render_flow_result(key: str):
    """渲染资金流向结果"""
    res = st.session_state.get(f"ifind_res_{key}")
    ts = st.session_state.get(f"ifind_ts_{key}")

    if res is None:
        st.info("点击「查询板块资金流向」开始查询")
        return

    # 解析结果（ths_call 返回三元组：df, res, err）
    if isinstance(res, tuple) and len(res) >= 3:
        df, 原始对象, err = res
        if err not in (0, None):
            st.error(f"查询失败，错误码：{err}")
            return
        if df is not None and not df.empty:
            # 显示统计
            st.caption(f"共 {len(df)} 个板块 · 数据源：同花顺 iFinD"
                       + (f" · 查询于 {ts:%H:%M:%S}" if ts else ""))

            # 显示表格
            st.dataframe(df, use_container_width=True, height=600)

            # 导出按钮
            csv = df.to_csv(index=False, encoding="utf-8-sig")
            st.download_button(
                label="📥 导出CSV",
                data=csv,
                file_name=f"板块资金流向_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )
        else:
            st.warning("查询成功但返回为空")
    else:
        st.warning("结果格式异常")


def _render_sector():
    """渲染板块行情"""
    st.subheader("板块行情")

    # 查询参数
    with st.expander("⚙️ 查询参数", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            # 指数代码列表
            codes = st.text_input(
                "板块代码（逗号分隔）",
                "000001.SH,399001.SZ,399006.SZ,000300.SH,000905.SH",
                key="sector_codes",
                help="多个板块代码用逗号分隔"
            )
        with c2:
            # 返回字段
            indicators = st.text_input(
                "返回字段",
                "thscode,latest,changeRatio,volume,amount",
                key="sector_indicators",
                help="逗号分隔的指标代码"
            )

        if st.button("查询板块行情", key="sector_go"):
            _go_sector(codes, indicators)

    # 自动查询
    _auto_sector(codes, indicators)

    # 渲染结果
    _render_sector_result("sector_data")


def _go_sector(codes: str, indicators: str):
    """执行板块行情查询"""
    try:
        # 使用 THS_RQ 接口获取板块实时行情
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        result = datasource.ths_realtime(code_list, indicators)
        st.session_state["ifind_res_sector_data"] = result
        st.session_state["ifind_call_sector_data"] = (codes, indicators)
        st.session_state["ifind_ts_sector_data"] = pd.Timestamp.now()
    except Exception as e:
        st.error(f"查询失败：{e}")


def _auto_sector(codes: str, indicators: str):
    """首次进页面自动查询"""
    if "ifind_res_sector_data" not in st.session_state:
        _go_sector(codes, indicators)


def _render_sector_result(key: str):
    """渲染板块行情结果"""
    res = st.session_state.get(f"ifind_res_{key}")
    ts = st.session_state.get(f"ifind_ts_{key}")

    if res is None:
        st.info("点击「查询板块行情」开始查询")
        return

    # 解析结果（ths_realtime 返回三元组：df, res, err）
    if isinstance(res, tuple) and len(res) >= 3:
        df, 原始对象, err = res
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
                file_name=f"板块行情_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )
        else:
            st.warning("查询成功但返回为空")
    else:
        st.warning("结果格式异常")


def _render_turnover():
    """渲染板块轮动"""
    st.subheader("板块轮动")

    # 查询参数
    with st.expander("⚙️ 查询参数", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            # 指数代码
            code = st.text_input(
                "指数代码",
                "000300.SH",
                key="turnover_code",
                help="要查询轮动的指数代码"
            )
        with c2:
            # 时间范围
            c21, c22 = st.columns(2)
            with c21:
                start = st.text_input(
                    "开始日期",
                    (pd.Timestamp.now() - pd.Timedelta(days=30)).strftime("%Y-%m-%d"),
                    key="turnover_start"
                )
            with c22:
                end = st.text_input(
                    "结束日期",
                    pd.Timestamp.now().strftime("%Y-%m-%d"),
                    key="turnover_end"
                )

        if st.button("查询板块轮动", key="turnover_go"):
            _go_turnover(code, start, end)

    # 自动查询
    _auto_turnover(code, start, end)

    # 渲染结果
    _render_turnover_result("turnover_data")


def _go_turnover(code: str, start: str, end: str):
    """执行板块轮动查询"""
    try:
        # 使用 THS_HQ 接口获取历史行情
        indicators = "thscode,open,high,low,close,volume,amount"
        result = datasource.ths_history([code], indicators, start, end)
        st.session_state["ifind_res_turnover_data"] = result
        st.session_state["ifind_call_turnover_data"] = (code, start, end)
        st.session_state["ifind_ts_turnover_data"] = pd.Timestamp.now()
    except Exception as e:
        st.error(f"查询失败：{e}")


def _auto_turnover(code: str, start: str, end: str):
    """首次进页面自动查询"""
    if "ifind_res_turnover_data" not in st.session_state:
        _go_turnover(code, start, end)


def _render_turnover_result(key: str):
    """渲染板块轮动结果"""
    res = st.session_state.get(f"ifind_res_{key}")
    ts = st.session_state.get(f"ifind_ts_{key}")

    if res is None:
        st.info("点击「查询板块轮动」开始查询")
        return

    # 解析结果（ths_history 返回三元组：df, res, err）
    if isinstance(res, tuple) and len(res) >= 3:
        df,原始对象, err = res
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
                file_name=f"板块轮动_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )
        else:
            st.warning("查询成功但返回为空")
    else:
        st.warning("结果格式异常")


render()
