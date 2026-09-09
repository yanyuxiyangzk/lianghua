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

    tab1, tab2, tab3, tab4 = st.tabs(["超额分布", "净值曲线", "月度聚合", "  活跃因子"])

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

    # ==================== Tab4: 活跃因子 ====================
    with tab4:
        st.markdown("### 各调仓窗口活跃因子分析")

        if "active_factors" not in df.columns:
            st.info("当前 walk_forward_log 数据无 active_factors 列。")
        else:
            # 解析 active_factors（JSON格式）
            import json
            records = []
            for _, row in df.iterrows():
                try:
                    factors = json.loads(row["active_factors"]) if isinstance(row["active_factors"], str) else row["active_factors"]
                    if isinstance(factors, list):
                        for f in factors:
                            name = f.get("name", "") if isinstance(f, dict) else str(f)
                            weight = f.get("weight", 0) if isinstance(f, dict) else 0
                            records.append({
                                "调仓日": row.get("trade_date", ""),
                                "因子": name,
                                "权重": weight,
                                "超额": row.get("opt_net_excess", 0),
                            })
                except Exception:
                    continue

            if not records:
                st.info("active_factors 数据为空或解析失败。")
            else:
                af_df = pd.DataFrame(records)

                # 概览
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.metric("调仓窗口数", af_df["调仓日"].nunique())
                with c2:
                    st.metric("活跃因子数", af_df["因子"].nunique())
                with c3:
                    avg_factors = af_df.groupby("调仓日")["因子"].count().mean()
                    st.metric("平均每窗口因子数", f"{avg_factors:.1f}")

                st.divider()

                # 因子使用频率
                st.markdown("#### 因子被选为活跃因子的频率")
                freq = af_df.groupby("因子").agg(
                    出现次数=("调仓日", "nunique"),
                    平均权重=("权重", "mean"),
                    平均超额=("超额", "mean"),
                ).reset_index().sort_values("出现次数", ascending=False)

                c1, c2 = st.columns(2)
                with c1:
                    fig = px.bar(freq.head(20), x="因子", y="出现次数",
                                 title="活跃因子出现频率（Top 20）",
                                 labels={"出现次数": "出现次数", "因子": "因子"})
                    fig.update_layout(height=400, xaxis_tickangle=45)
                    st.plotly_chart(fig, use_container_width=True)

                with c2:
                    fig = px.scatter(freq, x="平均权重", y="平均超额",
                                     size="出现次数", hover_name="因子",
                                     title="因子权重 vs 超额表现",
                                     labels={"平均权重": "平均权重", "平均超额": "平均超额"})
                    fig.update_layout(height=400)
                    st.plotly_chart(fig, use_container_width=True)

                # 因子更替分析
                st.markdown("#### 因子更替分析")
                window_factors = af_df.groupby("调仓日")["因子"].apply(set).reset_index()
                window_factors.columns = ["调仓日", "因子集"]

                if len(window_factors) > 1:
                    changes = []
                    for i in range(1, len(window_factors)):
                        prev = window_factors.iloc[i - 1]["因子集"]
                        curr = window_factors.iloc[i]["因子集"]
                        new = curr - prev
                        removed = prev - curr
                        kept = curr & prev
                        changes.append({
                            "调仓日": window_factors.iloc[i]["调仓日"],
                            "新增因子": len(new),
                            "移除因子": len(removed),
                            "保留因子": len(kept),
                            "更替率": f"{(len(new) + len(removed)) / max(len(curr | prev), 1):.0%}",
                        })

                    changes_df = pd.DataFrame(changes)
                    fig = go.Figure()
                    fig.add_trace(go.Bar(x=changes_df["调仓日"], y=changes_df["新增因子"],
                                         name="新增", marker_color="#2ca02c"))
                    fig.add_trace(go.Bar(x=changes_df["调仓日"], y=changes_df["移除因子"],
                                         name="移除", marker_color="#d62728"))
                    fig.add_trace(go.Bar(x=changes_df["调仓日"], y=changes_df["保留因子"],
                                         name="保留", marker_color="#1f77b4"))
                    fig.update_layout(barmode="stack", title="因子更替趋势",
                                      xaxis_title="调仓日", yaxis_title="因子数量", height=400)
                    st.plotly_chart(fig, use_container_width=True)

                    # 详细表格
                    with st.expander("查看更替详情"):
                        st.dataframe(changes_df, use_container_width=True, hide_index=True)

                # 因子权重热力图
                st.markdown("#### 因子权重热力图")
                pivot = af_df.pivot_table(index="因子", columns="调仓日", values="权重", fill_value=0)
                if not pivot.empty:
                    # 取 Top 20 因子
                    top_factors = freq.head(20)["因子"].tolist()
                    pivot_top = pivot.loc[pivot.index.isin(top_factors)]

                    if not pivot_top.empty:
                        fig = px.imshow(pivot_top, color_continuous_scale="Blues",
                                        title="因子权重热力图（Top 20因子）",
                                        labels={"color": "权重"})
                        fig.update_layout(height=600)
                        st.plotly_chart(fig, use_container_width=True)


if __name__ == "__main__":
    render()
