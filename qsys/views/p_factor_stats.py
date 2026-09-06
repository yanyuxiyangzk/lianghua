"""📈 因子体检统计：IC分布、胜率分布、多周期胜率热力图、因子对比。

数据来源：
  - factor_scorecards：体检表（IC/ICIR/多周期胜率）
  - factor_registry：因子元数据（factor_type/family）
"""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import library


def render():
    st.title("📈 因子体检统计")
    st.caption("体检结果全局统计 · IC/胜率分布 · 因子对比 · 多周期热力图")

    pools = library.list_scorecard_pools()
    if not pools:
        st.info("暂无体检数据。到 🧩选股组合 页运行体检。")
        return

    pool_name = st.selectbox("选择股票池", pools, key="fs_pool")
    card = library.get_latest_scorecard(pool_name)

    if card.empty:
        st.info(f"「{pool_name}」暂无体检数据。")
        return

    # 合并 factor_type 和 family
    registry = library.get_factor_registry()
    if not registry.empty:
        ft_map = registry.set_index("name")["factor_type"].to_dict() if "factor_type" in registry.columns else {}
        fam_map = registry.set_index("name")["family"].to_dict() if "family" in registry.columns else {}
        card["因子类型"] = card["因子"].map(lambda n: ft_map.get(n, "量价"))
        card["机制族"] = card["因子"].map(lambda n: fam_map.get(n, "其他"))

    tab1, tab2, tab3, tab4 = st.tabs(["📊 体检概览", "🔍 因子对比", "🌡️ 多周期热力图", "📋 体检明细"])

    # ================================ Tab1: 体检概览 ================================
    with tab1:
        st.markdown("## 体检概览")

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("已体检因子", f"{len(card):,}")
        with c2:
            ic_mean = card["IC均值"].mean() if "IC均值" in card.columns else 0
            st.metric("平均IC均值", f"{ic_mean:.4f}")
        with c3:
            icir_mean = card["ICIR"].mean() if "ICIR" in card.columns else 0
            st.metric("平均ICIR", f"{icir_mean:.3f}")
        with c4:
            wr_mean = card["Top组胜率"].mean() if "Top组胜率" in card.columns else 0
            st.metric("平均Top组胜率", f"{wr_mean:.0%}")

        st.divider()

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### IC均值分布")
            ic_vals = card["IC均值"].dropna()
            if not ic_vals.empty:
                fig = px.histogram(x=ic_vals, nbins=50, color_discrete_sequence=["steelblue"])
                fig.add_vline(x=0, line_dash="dash", line_color="red")
                fig.add_vline(x=ic_vals.mean(), line_dash="dot", line_color="green",
                              annotation_text=f"均值={ic_vals.mean():.4f}")
                fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                                  xaxis_title="IC均值", yaxis_title="频次")
                st.plotly_chart(fig, width="stretch")

        with c2:
            st.markdown("### Top组胜率分布")
            wr_vals = card["Top组胜率"].dropna()
            if not wr_vals.empty:
                fig = px.histogram(x=wr_vals, nbins=50, color_discrete_sequence=["#2ca02c"])
                fig.add_vline(x=0.5, line_dash="dash", line_color="red",
                              annotation_text="50%基准线")
                fig.add_vline(x=wr_vals.mean(), line_dash="dot", line_color="blue",
                              annotation_text=f"均值={wr_vals.mean():.0%}")
                fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                                  xaxis_title="Top组胜率", yaxis_title="频次")
                st.plotly_chart(fig, width="stretch")

        st.divider()

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### IC vs ICIR 散点图")
            scatter_df = card[["因子", "IC均值", "ICIR", "因子类型"]].dropna()
            if not scatter_df.empty:
                fig = px.scatter(scatter_df, x="IC均值", y="ICIR", color="因子类型",
                                 hover_data=["因子"], color_discrete_sequence=px.colors.qualitative.Set2)
                fig.add_hline(y=0, line_dash="dash", line_color="gray")
                fig.add_vline(x=0, line_dash="dash", line_color="gray")
                fig.update_layout(height=400, margin=dict(l=20, r=20, t=10, b=20))
                st.plotly_chart(fig, width="stretch")

        with c2:
            st.markdown("### 因子类型分布")
            if "因子类型" in card.columns:
                ft_counts = card["因子类型"].value_counts()
                fig = px.pie(values=ft_counts.values, names=ft_counts.index,
                             color_discrete_sequence=px.colors.qualitative.Set2)
                fig.update_traces(textposition="inside", textinfo="percent+label")
                fig.update_layout(height=400, margin=dict(l=20, r=20, t=10, b=20))
                st.plotly_chart(fig, width="stretch")

        st.divider()

        # 机制族 × 因子类型 热力图
        if "因子类型" in card.columns and "机制族" in card.columns:
            st.markdown("### 机制族 × 因子类型 热力图（因子数）")
            cross = pd.crosstab(card["机制族"], card["因子类型"])
            if not cross.empty:
                fig = px.imshow(cross, text_auto=True, color_continuous_scale="YlOrRd",
                                aspect="auto")
                fig.update_layout(height=400, margin=dict(l=20, r=20, t=10, b=20))
                st.plotly_chart(fig, width="stretch")

    # ================================ Tab2: 因子对比 ================================
    with tab2:
        st.markdown("## 因子对比")
        st.caption("选择2-4个因子，对比IC曲线、胜率")

        factor_list = card["因子"].tolist()
        selected = st.multiselect("选择因子（2-4个）", factor_list, default=factor_list[:2],
                                  max_selections=4, key="fs_compare")

        if len(selected) >= 2:
            compare_df = card[card["因子"].isin(selected)].set_index("因子")

            # IC曲线对比
            st.markdown("### IC均值对比")
            fig = go.Figure()
            for name in selected:
                if name in compare_df.index:
                    row = compare_df.loc[name]
                    fig.add_trace(go.Bar(name=name, x=["IC均值"],
                                         y=[row.get("IC均值", 0)]))
            fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20))
            st.plotly_chart(fig, width="stretch")

            # 多周期胜率对比
            win_cols = [c for c in card.columns if c.endswith("日胜率")]
            if win_cols:
                st.markdown("### 多周期胜率对比")
                fig = go.Figure()
                for name in selected:
                    if name in compare_df.index:
                        wr = [compare_df.loc[name].get(c, 0) for c in win_cols]
                        labels = [c.replace("胜率", "") for c in win_cols]
                        fig.add_trace(go.Scatter(name=name, x=labels, y=wr, mode="lines+markers"))
                fig.update_layout(height=350, margin=dict(l=20, r=20, t=10, b=20),
                                  yaxis_tickformat=".0%", yaxis_range=[0, 1])
                st.plotly_chart(fig, width="stretch")

            # 体检指标对比表
            st.markdown("### 体检指标对比")
            st.dataframe(compare_df[["IC均值", "ICIR", "IC胜率", "Top组胜率", "建议方向", "因子类型"]].round(4),
                         width="stretch")
        else:
            st.info("请选择至少2个因子进行对比")

    # ================================ Tab3: 多周期热力图 ================================
    with tab3:
        st.markdown("## 多周期胜率热力图")
        st.caption("行=因子，列=持有期，颜色=胜率（绿=高，红=低）")

        win_cols = [c for c in card.columns if c.endswith("日胜率")]
        if not win_cols:
            st.info("无多周期胜率数据")
        else:
            # 选TOP N因子展示
            n_factors = st.slider("展示因子数", 10, 100, 30, key="fs_heatmap_n")
            top_factors = card.nlargest(n_factors, "Top组胜率") if "Top组胜率" in card.columns else card.head(n_factors)

            heat_data = top_factors.set_index("因子")[win_cols].copy()
            heat_data.columns = [c.replace("胜率", "") for c in heat_data.columns]

            fig = px.imshow(heat_data, color_continuous_scale="RdYlGn",
                            aspect="auto", zmin=0, zmax=1)
            fig.update_layout(height=max(400, n_factors * 20 + 100),
                              margin=dict(l=20, r=20, t=10, b=20))
            st.plotly_chart(fig, width="stretch")

            # 统计摘要
            st.markdown("### 各周期胜率统计")
            stat_df = heat_data.describe().loc[["mean", "std", "min", "max"]].T
            stat_df.columns = ["均值", "标准差", "最小值", "最大值"]
            st.dataframe(stat_df.round(4), width="stretch")

    # ================================ Tab4: 体检明细 ================================
    with tab4:
        st.markdown("## 体检明细表")

        # 搜索/筛选
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            kw = st.text_input("🔍 搜索因子名", "", key="fs_kw")
        with c2:
            if "因子类型" in card.columns:
                ftypes = ["全部"] + sorted(card["因子类型"].unique().tolist())
                ft_sel = st.selectbox("因子类型", ftypes, key="fs_ft")
            else:
                ft_sel = "全部"
        with c3:
            sort_by = st.selectbox("排序", ["IC均值", "ICIR", "Top组胜率", "因子"],
                                   key="fs_sort")

        show = card.copy()
        if kw.strip():
            show = show[show["因子"].str.contains(kw.strip(), case=False)]
        if ft_sel != "全部" and "因子类型" in show.columns:
            show = show[show["因子类型"] == ft_sel]
        show = show.sort_values(sort_by, ascending=False) if sort_by in show.columns else show

        # 格式化
        disp_cols = ["因子", "因子类型", "机制族", "IC均值", "ICIR", "IC胜率", "Top组胜率", "建议方向", "天数"]
        avail_cols = [c for c in disp_cols if c in show.columns]
        disp = show[avail_cols].copy()
        for c in ["IC均值", "ICIR"]:
            if c in disp.columns:
                disp[c] = disp[c].map(lambda x: f"{x:.4f}" if pd.notna(x) else "—")
        for c in ["IC胜率", "Top组胜率"]:
            if c in disp.columns:
                disp[c] = disp[c].map(lambda x: f"{x:.0%}" if pd.notna(x) else "—")

        st.caption(f"命中 {len(disp)} 个因子")
        st.dataframe(disp, width="stretch", hide_index=True, height=500)


render()
