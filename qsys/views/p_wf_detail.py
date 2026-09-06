"""Walk-Forward 衰减分析 — 逐调仓日超额/净值对比/月度聚合。"""

import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Walk-Forward衰减分析", layout="wide")


def _load_wf_data():
    import library
    try:
        with library._lconn() as c:
            df = pd.read_sql(
                "SELECT * FROM walk_forward_log ORDER BY trade_date DESC LIMIT 2000", c)
        return df
    except Exception:
        return pd.DataFrame()


def render():
    st.markdown("## 📉 Walk-Forward 衰减分析")

    df = _load_wf_data()
    if df.empty:
        st.info(
            "暂无 walk-forward 数据。当使用选股组合页执行 greedy/MMR 组合搜索时，"
            "逐调仓日结果会自动记录到此处。"
        )
        # 展示已有因子体检数据作为替代
        st.markdown("### 已有因子体检数据（替代参考）")
        import library
        try:
            with library._lconn() as c:
                sc = pd.read_sql(
                    "SELECT name, ic_mean, icir, ic_winrate, top_winrate, winrates "
                    "FROM factor_scorecards ORDER BY updated_at DESC LIMIT 100", c)
            if not sc.empty:
                st.dataframe(sc, use_container_width=True)
        except Exception:
            pass
        return

    # 概览
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("调仓日数", len(df))
    with c2:
        avg_exc = df["opt_net_excess"].mean() if "opt_net_excess" in df else 0
        st.metric("平均扣费超额", f"{avg_exc:.2%}")
    with c3:
        if "opt_net_excess" in df:
            win = (df["opt_net_excess"] > 0).mean()
            st.metric("胜率", f"{win:.1%}")
    with c4:
        avg_turn = df["opt_turnover"].mean() if "opt_turnover" in df else 0
        st.metric("平均换手率", f"{avg_turn:.1%}")

    st.divider()

    tab1, tab2, tab3 = st.tabs(["超额分布", "净值曲线", "月度聚合"])

    with tab1:
        st.markdown("### 逐调仓日扣费超额")
        if "opt_net_excess" in df:
            colors = ["green" if v > 0 else "red" for v in df["opt_net_excess"]]
            fig = go.Figure(go.Bar(
                x=df["trade_date"], y=df["opt_net_excess"],
                marker_color=colors))
            fig.update_layout(height=350, margin=dict(l=20, r=20, t=30, b=20),
                              xaxis_title="调仓日", yaxis_title="扣费超额")
            st.plotly_chart(fig, width="stretch")

    with tab2:
        st.markdown("### 净值曲线对比")
        if "opt_net_excess" in df and "eq_net_excess" in df:
            wf = df.sort_values("trade_date")
            wf["opt_nav"] = (1 + wf["opt_net_excess"]).cumprod()
            wf["eq_nav"] = (1 + wf["eq_net_excess"]).cumprod()
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=wf["trade_date"], y=wf["opt_nav"],
                                     name="优化组合", line=dict(color="steelblue")))
            fig.add_trace(go.Scatter(x=wf["trade_date"], y=wf["eq_nav"],
                                     name="等权对照", line=dict(color="coral", dash="dash")))
            fig.update_layout(height=350, margin=dict(l=20, r=20, t=30, b=20),
                              xaxis_title="日期", yaxis_title="净值")
            st.plotly_chart(fig, width="stretch")

    with tab3:
        st.markdown("### 月度聚合")
        if "trade_date" in df and "opt_net_excess" in df:
            wf = df.copy()
            wf["month"] = pd.to_datetime(wf["trade_date"]).dt.to_period("M").astype(str)
            monthly = wf.groupby("month").agg(
                cnt=("opt_net_excess", "count"),
                avg_exc=("opt_net_excess", "mean"),
                win=("opt_net_excess", lambda x: (x > 0).mean())
            ).reset_index()
            fig = go.Figure()
            fig.add_trace(go.Bar(x=monthly["month"], y=monthly["avg_exc"],
                                 name="平均超额", marker_color="steelblue"))
            fig.update_layout(height=300, margin=dict(l=20, r=20, t=30, b=20),
                              xaxis_title="月份", yaxis_title="平均扣费超额")
            st.plotly_chart(fig, width="stretch")


if __name__ == "__main__":
    render()
