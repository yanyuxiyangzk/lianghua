"""🔬 因子详情：单因子时序曲线、IC衰减、十分层收益、多空对冲净值。

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

import factor_eval as fe
import library
import signals as sig
from common import all_pools, get_last_trade_day


def render():
    st.title("🔬 因子详情")
    st.caption("选择因子 → 查看时序曲线、IC衰减、十分层收益、多空对冲净值")

    registry = library.get_factor_registry()
    if registry.empty:
        st.info("因子库为空。")
        return

    # 因子选择
    factor_names = sorted(registry["name"].tolist())
    c1, c2 = st.columns([2, 1])
    with c1:
        selected = st.selectbox("选择因子", factor_names, key="fd_select")
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
    codes = all_pools().get(pool_name, [])
    if len(codes) < 30:
        st.warning(f"股票池 {pool_name} 不足30只")
        return

    end = get_last_trade_day()

    @st.cache_data(ttl=600, show_spinner="计算因子值中…")
    def _calc_factor(fac_name: str, codes_tuple: tuple, end_str: str):
        reg = library.get_factor_registry()
        r = reg[reg["name"] == fac_name]
        if r.empty:
            return None, None, None
        code = r.iloc[0].get("code")
        fac = {"name": fac_name, "kind": "loopengine", "code": code}
        vals = fe.get_factor_values(fac, list(codes_tuple), end_str)
        panel = sig.get_panel_cached(list(codes_tuple), end_str, 800, source="qlib_local")
        return vals, panel, code

    vals, panel, code = _calc_factor(selected, tuple(codes), end)
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

    # 2. IC衰减曲线
    st.markdown("#### IC 衰减曲线（滚动20日IC均值）")
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

    # 3. 十分层收益柱状图
    st.markdown("#### 十分层平均20日收益")
    fwd_vals = fe.forward_returns(panel, 20)
    j = vals.rename("f").to_frame().join(fwd_vals.stack().rename("r"), how="inner").dropna()
    if not j.empty:
        def _group_mean(g):
            k = max(1, int(len(g) * 0.10))
            groups = {}
            for pct in range(10):
                lo = g["f"].quantile(pct / 10)
                hi = g["f"].quantile((pct + 1) / 10)
                mask = (g["f"] >= lo) & (g["f"] <= hi) if pct < 9 else (g["f"] >= lo)
                groups[f"G{pct+1}"] = g.loc[mask, "r"].mean() if mask.any() else 0
            return pd.Series(groups)

        gm = j.groupby(level="datetime").apply(_group_mean).mean()
        colors = ["#d62728" if v < 0 else "#2ca02c" for v in gm.values]
        fig = go.Figure(go.Bar(x=gm.index, y=gm.values, marker_color=colors))
        fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                          xaxis_title="分组", yaxis_title="平均20日收益")
        st.plotly_chart(fig, width="stretch")
        # 单调性检验
        monotonic = all(gm.values[i] <= gm.values[i+1] for i in range(len(gm.values)-1))
        st.caption(f"{'✅ 单调性良好' if monotonic else '⚠️ 单调性一般'} | 多空收益: {gm.iloc[-1]-gm.iloc[0]:.2%}")

    # 4. 多空对冲净值
    st.markdown("#### 多空对冲净值曲线")
    if not j.empty:
        def _ls_nav(g):
            k = max(1, int(len(g) * 0.10))
            top = g.nlargest(k, "f")["r"].mean()
            bottom = g.nsmallest(k, "f")["r"].mean()
            return top - bottom

        ls = j.groupby(level="datetime").apply(_ls_nav)
        nav = (1 + ls).cumprod()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=nav.index, y=nav.values, mode="lines",
                                 fill="tozeroy", line=dict(color="steelblue")))
        fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                          xaxis_title="日期", yaxis_title="净值")
        st.plotly_chart(fig, width="stretch")
        ann_ret = float(ls.mean() * 252)
        ann_vol = float(ls.std() * np.sqrt(252))
        sharpe = ann_ret / (ann_vol + 1e-12)
        mdd = float(((nav - nav.cummax()) / nav.cummax()).min())
        st.caption(f"年化收益: {ann_ret:.2%} | 年化波动: {ann_vol:.2%} | Sharpe: {sharpe:.2f} | MaxDD: {mdd:.2%}")

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
