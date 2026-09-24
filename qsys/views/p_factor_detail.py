"""🔬 因子详情：单因子时序曲线、滚动IC、十分层收益、多空对冲净值。

数据来源：
  - factor_registry：因子元数据
  - factor_scorecards：体检指标
  - factor_eval：因子求值 + IC计算 + 回测
  - signals：面板数据
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st
import sqlite3

import factor_eval as fe
import library
import signals as sig
from common import DATA_DIR, all_pools, get_last_trade_day


def render():
    st.title("🔬 因子详情")
    st.caption("选择因子 → 查看时序曲线、滚动IC、十分层收益、多空对冲净值")

    registry = library.get_factor_registry()
    if registry.empty:
        st.info("因子库为空。")
        return

    # 因子选择
    factor_names = sorted(registry["name"].dropna().unique().tolist())
    default_factor = "mom_20d" if "mom_20d" in factor_names else factor_names[0]
    c1, c2 = st.columns([2, 1])
    with c1:
        selected = st.selectbox("选择因子", factor_names,
                                index=factor_names.index(default_factor), key="fd_select")
    with c2:
        pool_name = st.selectbox("股票池", list(all_pools().keys()), key="fd_pool")

    if not selected:
        return

    # 因子元数据
    row = registry[registry["name"] == selected].iloc[0] if not registry.empty else None
    if row is not None:
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("因子类型", row.get("factor_type", "量价"))
        with c2:
            st.metric("机制族", row.get("family", "—"))
        with c3:
            st.metric("引擎", row.get("engine", "—"))
        with c4:
            gs = row.get("gate_status")
            st.metric("闸门", "✅通过" if gs == 1 else ("事件✅" if gs == 2 else "❌未通过"))

    # 体检指标
    scorecard = library.get_latest_scorecard(pool_name)
    if not scorecard.empty:
        from scorecard_evidence import current_valid_mask
        scorecard = scorecard.loc[current_valid_mask(scorecard, registry)]
        sc_row = scorecard[scorecard["因子"] == selected]
        if not sc_row.empty:
            sc = sc_row.iloc[0]
            st.divider()
            st.markdown("### 体检指标")
            m1, m2, m3, m4, m5, m6 = st.columns(6)
            with m1:
                ic_val = sc.get("IC均值")
                st.metric("IC均值", f"{ic_val:.4f}" if pd.notna(ic_val) else "—")
            with m2:
                icir_val = sc.get("ICIR")
                st.metric("ICIR", f"{icir_val:.3f}" if pd.notna(icir_val) else "—")
            with m3:
                icwr_val = sc.get("IC胜率")
                st.metric("IC胜率", f"{icwr_val:.0%}" if pd.notna(icwr_val) else "—")
            with m4:
                topwr_val = sc.get("Top组胜率")
                st.metric("Top组胜率", f"{topwr_val:.0%}" if pd.notna(topwr_val) else "—")
            with m5:
                st.metric("建议方向", str(sc.get("建议方向", "—")))
            with m6:
                st.metric("有效天数", f"{int(sc.get('天数', 0))}")

            # 多周期胜率
            win_cols = [c for c in sc.index if c.endswith("日胜率")]
            if win_cols:
                st.markdown("**多周期胜率**")
                wr_data = {c.replace("胜率", ""): sc[c] for c in win_cols if pd.notna(sc[c])}
                if wr_data:
                    wr_df = pd.DataFrame({"周期": list(wr_data.keys()), "胜率": list(wr_data.values())})
                    fig = px.bar(wr_df, x="周期", y="胜率", color="胜率",
                                 color_continuous_scale="RdYlGn", range_y=[0, 1])
                    fig.update_layout(height=250, margin=dict(l=20, r=20, t=10, b=20),
                                      coloraxis_showscale=False, yaxis_tickformat=".0%")
                    st.plotly_chart(fig, width="stretch")

    st.divider()

    # 回测可视化
    st.markdown("### 回测可视化")
    request_key = (selected, pool_name)
    if st.button("运行回测可视化", type="primary", key="fd_run"):
        st.session_state["fd_requested"] = request_key
    if st.session_state.get("fd_requested") != request_key:
        st.info("选择因子和股票池后，点击“运行回测可视化”加载数据。")
        return

    codes = all_pools().get(pool_name, [])
    if len(codes) < 30:
        st.warning(f"股票池 {pool_name} 不足30只")
        return

    # 在线源的“今天”可能晚于本地已落库的最后交易日；若直接拿今天逐票补数，
    # 详情页会对沪深300逐只发起网络请求并长时间转圈。使用当前源的本地数据边界。
    import datasource
    source = datasource.get_loop_source()
    end = get_last_trade_day()
    if source != "qlib_local":
        try:
            with sqlite3.connect(str(DATA_DIR / "market.db")) as c:
                latest = c.execute("SELECT MAX(date) FROM market_daily WHERE source=?", (source,)).fetchone()[0]
            if latest:
                end = min(end, str(latest)[:10])
        except Exception:
            pass

    @st.cache_data(ttl=600, show_spinner="计算因子值中…")
    def _calc_factor(fac_name: str, codes_tuple: tuple, end_str: str):
        reg = library.get_factor_registry()
        r = reg[reg["name"] == fac_name]
        if r.empty:
            return None, None, None
        meta = r.iloc[0]
        # 详情页此前把所有因子强制标成 loopengine，内置因子没有 code，
        # 导致 factor_eval 无法路由求值，页面一律显示“因子值为空”。
        # 按注册表中的 kind 保留 builtin/evolved/loopengine 类型；代码只
        # 对有值的进化因子传递。
        kind = str(meta.get("kind") or "builtin")
        code = meta.get("code")
        if pd.isna(code):
            code = None
        fac = {"name": fac_name, "kind": kind}
        if code:
            fac["code"] = code
        # 详情页只需用于图表的近一年数据；此前默认 800 个交易日，
        # 在 iFinD 数据源下首次读取 300 只股票会长时间占满内存，看起来像无限转圈。
        lookback_days = 400
        vals = fe.get_factor_values(fac, list(codes_tuple), end_str,
                                    lookback_days=lookback_days)
        import datasource
        panel = sig.get_panel_cached(list(codes_tuple), end_str, lookback_days,
                                     source=datasource.get_loop_source())
        return vals, panel, code

    try:
        vals, panel, code = _calc_factor(selected, tuple(codes), end)
    except Exception as exc:
        st.error(f"因子回测加载失败：{exc}")
        return
    if vals is None or vals.empty:
        st.warning("因子值计算失败或为空")
        return

    # 1. 因子时序曲线（选3只代表股）
    st.markdown("#### 因子时序曲线")
    # 取最近60天，选成交量最大的3只股票
    recent_dates = vals.index.get_level_values("datetime").unique().sort_values()[-60:]
    vals_recent = vals[vals.index.get_level_values("datetime").isin(recent_dates)]
    # 选因子值方差最大的3只
    top_instruments = vals_recent.groupby(level="instrument").std().nlargest(3).index.tolist()
    if top_instruments:
        fig = go.Figure()
        for inst in top_instruments:
            try:
                s = vals_recent.xs(inst, level="instrument")
                fig.add_trace(go.Scatter(x=s.index, y=s.values, name=inst, mode="lines"))
            except KeyError:
                continue
        fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                          xaxis_title="日期", yaxis_title="因子值")
        st.plotly_chart(fig, width="stretch")

    # 2. 滚动IC曲线
    st.markdown("#### 滚动IC（20日前向收益，20日滚动均值）")
    fwd = fe.forward_returns(panel, 20)
    ic = fe.ic_series(vals, fwd)
    if not ic.empty:
        ic_rolling = ic.rolling(20, min_periods=5).mean()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=ic.index, y=ic.values, name="日IC",
                                 mode="lines", opacity=0.3, line=dict(color="gray")))
        fig.add_trace(go.Scatter(x=ic_rolling.index, y=ic_rolling.values, name="20日滚动IC",
                                 mode="lines", line=dict(color="blue", width=2)))
        fig.add_hline(y=0, line_dash="dash", line_color="red")
        fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                          xaxis_title="日期", yaxis_title="IC")
        st.plotly_chart(fig, width="stretch")
        st.caption(f"IC 均值: {ic.mean():.4f} | ICIR: {ic.mean()/(ic.std()+1e-12):.3f} | IC胜率: {(ic>0).mean():.0%}")

    # Shared non-overlapping research engine; never compound overlapping labels.
    bt = fe.factor_group_backtest(vals, panel, n_groups=10, fwd_days=20, step=20)
    st.caption(bt["note"])
    if bt["status"] != "valid":
        st.warning("研究报告不完整，停止展示净值与绩效。" + "；".join(bt["reasons"][:3]))
    st.markdown("#### 十分层平均20日收益（非重叠取样）")
    gm = pd.Series(bt["group_mean"], dtype=float).dropna()
    if not gm.empty:
        fig = go.Figure(go.Bar(x=gm.index, y=gm.values))
        fig.update_layout(height=300, yaxis_tickformat=".1%")
        st.plotly_chart(fig, width="stretch")
    if not bt["ls_nav"].empty:
        st.markdown("#### 理论多空研究净值（持有期末）")
        st.line_chart(bt["ls_nav"])
        st.caption(" · ".join(f"{k}: {v}" for k, v in bt["ls_stats"].items()))
    st.markdown("#### 持有期IC诊断")
    horizon_rows = []
    for horizon in (1, 5, 10, 20):
        series = fe.ic_series(vals, fe.forward_returns(panel, horizon))
        horizon_rows.append({"前向交易日": horizon, "IC均值": series.mean(), "有效日期数": len(series)})
    st.dataframe(pd.DataFrame(horizon_rows), hide_index=True)

    # 5. 因子值分布直方图
    st.markdown("#### 因子值分布")
    sample = vals.dropna().sample(min(5000, len(vals.dropna())), random_state=42)
    fig = px.histogram(x=sample.values, nbins=80, color_discrete_sequence=["steelblue"])
    fig.update_layout(height=250, margin=dict(l=20, r=20, t=10, b=20),
                      xaxis_title="因子值", yaxis_title="频次")
    st.plotly_chart(fig, width="stretch")

    # 因子代码
    if code:
        with st.expander("因子代码"):
            st.code(code, language="python")


render()
