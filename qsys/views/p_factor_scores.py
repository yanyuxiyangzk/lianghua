"""5维因子评分可视化 — IC质量/稳定性/一致性/使用度/新鲜度。"""

import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import library


def _compute_factor_value_scores(registry: pd.DataFrame, scorecards: pd.DataFrame,
                                 usage: pd.DataFrame) -> pd.DataFrame:
    """计算5维因子评分。"""
    if registry.empty:
        return pd.DataFrame()

    records = []
    for _, row in registry.iterrows():
        name = row.get("name", "")
        ic_mean = abs(row.get("multi_objective_score", 0) or 0)

        # 从 scorecards 获取 IC/ICIR/胜率
        sc = scorecards[scorecards["name"] == name] if not scorecards.empty else pd.DataFrame()
        ic_winrate = float(sc["ic_winrate"].iloc[0]) if not sc.empty and "ic_winrate" in sc.columns else 0.5
        top_winrate = float(sc["top_winrate"].iloc[0]) if not sc.empty and "top_winrate" in sc.columns else 0.5

        # 从 usage 获取使用度
        u = usage[usage["factor_name"] == name] if not usage.empty else pd.DataFrame()
        pick_count = float(u["pick_count"].iloc[0]) if not u.empty and "pick_count" in u.columns else 0
        trade_count = float(u["trade_count"].iloc[0]) if not u.empty and "trade_count" in u.columns else 0

        # 新鲜度
        first_seen = row.get("first_seen", "")
        if first_seen:
            from datetime import datetime
            try:
                days_old = (datetime.now() - datetime.strptime(str(first_seen)[:10], "%Y-%m-%d")).days
            except Exception:
                days_old = 90
        else:
            days_old = 90

        # === 5维评分 ===
        # 1. IC质量 (30%) = min(1.0, |IC| * 10 * 0.5 + IC胜率 * 0.5)
        ic_score = min(1.0, ic_mean * 10 * 0.5 + ic_winrate * 0.5)

        # 2. IC稳定性 (25%) = IC胜率
        stability_score = min(1.0, ic_winrate)

        # 3. 一致性 (20%) = Top组胜率
        consistency_score = min(1.0, top_winrate)

        # 4. 使用度 (15%) = min(1.0, total_usage / 10)
        total_usage = pick_count + trade_count
        usage_score = min(1.0, total_usage / 10)

        # 5. 新鲜度 (10%) = max(0.1, 1.0 - days_old / 180)
        freshness_score = max(0.1, 1.0 - days_old / 180)

        # 总分
        total_score = (ic_score * 0.30 + stability_score * 0.25 +
                       consistency_score * 0.20 + usage_score * 0.15 +
                       freshness_score * 0.10)

        records.append({
            "因子": name,
            "来源": row.get("kind", ""),
            "机制族": row.get("family", ""),
            "闸门状态": "通过" if row.get("gate_status") == 1 else "未通过",
            "IC质量": round(ic_score, 3),
            "稳定性": round(stability_score, 3),
            "一致性": round(consistency_score, 3),
            "使用度": round(usage_score, 3),
            "新鲜度": round(freshness_score, 3),
            "总分": round(total_score, 3),
            "IC均值": round(ic_mean, 4),
            "IC胜率": round(ic_winrate, 3),
            "Top胜率": round(top_winrate, 3),
            "选股次数": int(pick_count),
            "交易次数": int(trade_count),
            "天数": days_old,
        })

    return pd.DataFrame(records)


def _radar_chart(scores_df: pd.DataFrame, top_n: int = 10):
    """绘制Top因子的雷达图。"""
    top = scores_df.nlargest(top_n, "总分")
    dims = ["IC质量", "稳定性", "一致性", "使用度", "新鲜度"]

    fig = go.Figure()
    for _, row in top.iterrows():
        fig.add_trace(go.Scatterpolar(
            r=[row[d] for d in dims] + [row[dims[0]]],
            theta=dims + [dims[0]],
            fill="toself",
            name=row["因子"],
            opacity=0.6
        ))
    fig.update_layout(
        polar=dict(radialaxis=dict(visible=True, range=[0, 1])),
        title=f"Top {top_n} 因子评分雷达图",
        height=500
    )
    return fig


def render():
    st.title("  5维因子评分")

    # 加载数据
    with library._lconn() as c:
        registry = library.get_factor_registry()
        scorecards = pd.read_sql("SELECT * FROM factor_scorecards", c)
        usage = pd.read_sql("SELECT * FROM factor_usage", c)

    if registry.empty:
        st.info("因子库为空。")
        return

    with st.spinner("计算5维因子评分..."):
        scores_df = _compute_factor_value_scores(registry, scorecards, usage)

    if scores_df.empty:
        st.warning("无法计算评分。")
        return

    # ===== 概览 =====
    st.markdown("## 评分概览")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("总因子数", f"{len(scores_df):,}")
    with c2:
        st.metric("平均总分", f"{scores_df['总分'].mean():.3f}")
    with c3:
        high_score = (scores_df["总分"] >= 0.7).sum()
        st.metric("高分因子 (≥0.7)", f"{high_score:,}")
    with c4:
        used = (scores_df["选股次数"] > 0).sum()
        st.metric("被使用因子", f"{used:,}")

    st.divider()

    # ===== Tab 布局 =====
    tab1, tab2, tab3, tab4 = st.tabs(["  排名列表", "  雷达图", "  分布分析", "  相关性"])

    # ==================== Tab1: 排名列表 ====================
    with tab1:
        st.markdown("### 因子评分排名")

        # 筛选
        c1, c2, c3 = st.columns(3)
        with c1:
            kind_filter = st.multiselect("来源筛选", scores_df["来源"].unique().tolist(), key="score_kind")
        with c2:
            family_filter = st.multiselect("机制族筛选", scores_df["机制族"].unique().tolist(), key="score_fam")
        with c3:
            gate_filter = st.selectbox("闸门状态", ["全部", "通过", "未通过"], key="score_gate")

        filtered = scores_df.copy()
        if kind_filter:
            filtered = filtered[filtered["来源"].isin(kind_filter)]
        if family_filter:
            filtered = filtered[filtered["机制族"].isin(family_filter)]
        if gate_filter != "全部":
            filtered = filtered[filtered["闸门状态"] == gate_filter]

        # 排序
        sort_by = st.selectbox("排序依据", ["总分", "IC质量", "稳定性", "一致性", "使用度", "新鲜度"],
                               key="score_sort")
        filtered = filtered.sort_values(sort_by, ascending=False)

        # 表格
        display_cols = ["因子", "来源", "机制族", "闸门状态", "总分",
                        "IC质量", "稳定性", "一致性", "使用度", "新鲜度",
                        "IC均值", "IC胜率", "Top胜率", "选股次数", "交易次数"]
        st.dataframe(filtered[display_cols], use_container_width=True, hide_index=True, height=600)

        # 下载
        csv = filtered[display_cols].to_csv(index=False).encode("utf-8")
        st.download_button("下载 CSV", csv, "factor_scores.csv", "text/csv")

    # ==================== Tab2: 雷达图 ====================
    with tab2:
        st.markdown("### Top 因子评分雷达图")
        top_n = st.slider("显示数量", 3, 15, 5, key="radar_n")
        fig = _radar_chart(scores_df, top_n)
        st.plotly_chart(fig, use_container_width=True)

        # 逐因子详情
        st.markdown("### 单因子评分详情")
        selected = st.selectbox("选择因子", scores_df["因子"].tolist(), key="radar_factor")
        if selected:
            row = scores_df[scores_df["因子"] == selected].iloc[0]
            c1, c2, c3, c4, c5 = st.columns(5)
            with c1:
                st.metric("IC质量", f"{row['IC质量']:.3f}", help="IC均值 × IC胜率")
            with c2:
                st.metric("稳定性", f"{row['稳定性']:.3f}", help="IC胜率")
            with c3:
                st.metric("一致性", f"{row['一致性']:.3f}", help="Top组胜率")
            with c4:
                st.metric("使用度", f"{row['使用度']:.3f}", help="选股+交易次数/10")
            with c5:
                st.metric("新鲜度", f"{row['新鲜度']:.3f}", help=f"距首次发现 {row['天数']} 天")

            # 单因子雷达
            dims = ["IC质量", "稳定性", "一致性", "使用度", "新鲜度"]
            fig = go.Figure()
            fig.add_trace(go.Scatterpolar(
                r=[row[d] for d in dims] + [row[dims[0]]],
                theta=dims + [dims[0]],
                fill="toself", name=selected
            ))
            fig.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, 1])),
                              title=f"{selected} 评分雷达", height=400)
            st.plotly_chart(fig, use_container_width=True)

    # ==================== Tab3: 分布分析 ====================
    with tab3:
        st.markdown("### 评分分布")

        # 总分分布
        fig = px.histogram(scores_df, x="总分", nbins=50, title="总分分布",
                           color="来源", barmode="overlay", opacity=0.7)
        st.plotly_chart(fig, use_container_width=True)

        # 各维度分布
        dims = ["IC质量", "稳定性", "一致性", "使用度", "新鲜度"]
        fig = go.Figure()
        for d in dims:
            fig.add_trace(go.Violin(y=scores_df[d], name=d, box_visible=True, meanline_visible=True))
        fig.update_layout(title="各维度评分分布", yaxis_title="评分", height=400)
        st.plotly_chart(fig, use_container_width=True)

        # 按来源对比
        st.markdown("### 按来源对比")
        kind_means = scores_df.groupby("来源")[dims + ["总分"]].mean()
        fig = px.bar(kind_means.reset_index(), x="来源", y=dims + ["总分"],
                     title="各来源平均评分", barmode="group")
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)

    # ==================== Tab4: 相关性 ====================
    with tab4:
        st.markdown("### 维度间相关性")

        dims = ["IC质量", "稳定性", "一致性", "使用度", "新鲜度", "总分"]
        corr = scores_df[dims].corr()

        fig = px.imshow(corr, text_auto=".2f", color_continuous_scale="RdYlGn",
                        title="维度间相关性矩阵", zmin=-1, zmax=1)
        fig.update_layout(height=500)
        st.plotly_chart(fig, use_container_width=True)

        # 各维度对总分的贡献
        st.markdown("### 各维度对总分的贡献度")
        contributions = {
            "IC质量": 0.30,
            "稳定性": 0.25,
            "一致性": 0.20,
            "使用度": 0.15,
            "新鲜度": 0.10
        }
        contrib_df = pd.DataFrame([{"维度": k, "权重": v} for k, v in contributions.items()])
        fig = px.pie(contrib_df, values="权重", names="维度", title="评分权重分布")
        st.plotly_chart(fig, use_container_width=True)


if __name__ == "__main__":
    render()
