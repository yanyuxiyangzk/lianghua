"""因子使用率仪表盘 — 哪些因子被实际使用？哪些是孤儿因子？"""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import library


def _load_data():
    with library._lconn() as c:
        usage = pd.read_sql("SELECT * FROM factor_usage", c)
        registry = library.get_factor_registry()
        strategies = pd.read_sql("SELECT name, factors FROM strategies", c)
    return usage, registry, strategies


def _count_factor_strategy_refs(strategies: pd.DataFrame) -> dict:
    """统计每个因子被策略包引用的次数。"""
    ref_counts = {}
    for _, row in strategies.iterrows():
        if row["factors"]:
            import json
            factors = json.loads(row["factors"]) if isinstance(row["factors"], str) else row["factors"]
            for f in factors:
                name = f.get("name", "") if isinstance(f, dict) else str(f)
                ref_counts[name] = ref_counts.get(name, 0) + 1
    return ref_counts


def render():
    st.title("  因子使用率仪表盘")

    usage, registry, strategies = _load_data()

    if usage.empty:
        st.info("factor_usage 表为空，因子使用数据尚未采集。")
        st.markdown("""
        **数据来源**：当因子在 `pool_scan` 或 `auto_scan` 中被选中时，`library.record_factor_usage()` 会更新此表。
        """)
        return

    # 计算策略包引用次数
    strat_refs = _count_factor_strategy_refs(strategies)

    # 合并使用数据和注册信息
    merged = usage.merge(
        registry[["name", "kind", "family", "gate_status", "multi_objective_score", "decay_status"]].rename(
            columns={"name": "factor_name"}),
        on="factor_name", how="left", suffixes=("_usage", "_reg")
    )

    # ===== 概览指标 =====
    st.markdown("## 概览")
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("已使用因子数", f"{len(usage):,}")
    with c2:
        used_names = set(usage["factor_name"])
        all_names = set(registry["name"]) if "name" in registry.columns else set()
        orphan = all_names - used_names
        st.metric("孤儿因子", f"{len(orphan):,}", help="在注册表中但从未被使用的因子")
    with c3:
        total_picks = usage["pick_count"].sum() if "pick_count" in usage.columns else 0
        st.metric("总选股次数", f"{int(total_picks):,}")
    with c4:
        total_trades = usage["trade_count"].sum() if "trade_count" in usage.columns else 0
        st.metric("总交易次数", f"{int(total_trades):,}")
    with c5:
        if "last_used" in usage.columns:
            recent = pd.to_datetime(usage["last_used"], errors="coerce").max()
            st.metric("最近使用", str(recent)[:10] if pd.notna(recent) else "N/A")
        else:
            st.metric("最近使用", "N/A")

    st.divider()

    # ===== Tab 布局 =====
    tab1, tab2, tab3, tab4 = st.tabs(["  Top 使用因子", "  使用分布", "  孤儿因子", "  因子类型对比"])

    # ==================== Tab1: Top 使用因子 ====================
    with tab1:
        st.markdown("### Top 使用因子（按选股次数排序）")
        top_n = st.slider("显示数量", 10, 100, 30, key="usage_top_n")
        top_df = usage.nlargest(top_n, "pick_count") if "pick_count" in usage.columns else usage.head(top_n)

        if not top_df.empty:
            fig = px.bar(
                top_df, x="factor_name", y="pick_count",
                color="trade_count" if "trade_count" in top_df.columns else None,
                color_continuous_scale="Viridis",
                labels={"pick_count": "选股次数", "factor_name": "因子", "trade_count": "交易次数"},
                title=f"Top {top_n} 使用因子"
            )
            fig.update_layout(xaxis_tickangle=45, height=500)
            st.plotly_chart(fig, use_container_width=True)

            # 详细表格
            with st.expander("查看详细数据"):
                st.dataframe(top_df, use_container_width=True, hide_index=True)

    # ==================== Tab2: 使用分布 ====================
    with tab2:
        st.markdown("### 使用分布分析")

        c1, c2 = st.columns(2)
        with c1:
            # 按 kind 分布
            if "kind" in merged.columns:
                kind_counts = merged.groupby("kind").agg(
                    factor_count=("factor_name", "nunique"),
                    total_picks=("pick_count", "sum") if "pick_count" in merged.columns else ("factor_name", "count"),
                ).reset_index()
                fig = px.bar(kind_counts, x="kind", y="total_picks", color="factor_count",
                             title="按来源类型的使用次数", labels={"total_picks": "总选股次数", "kind": "来源"})
                st.plotly_chart(fig, use_container_width=True)

        with c2:
            # 按 family 分布
            if "family" in merged.columns:
                fam_counts = merged.groupby("family").agg(
                    factor_count=("factor_name", "nunique"),
                    total_picks=("pick_count", "sum") if "pick_count" in merged.columns else ("factor_name", "count"),
                ).reset_index().nlargest(15, "total_picks")
                fig = px.bar(fam_counts, x="family", y="total_picks", color="factor_count",
                             title="按机制族的使用次数（Top 15）", labels={"total_picks": "总选股次数", "family": "机制族"})
                fig.update_layout(xaxis_tickangle=45)
                st.plotly_chart(fig, use_container_width=True)

        # 使用次数分布直方图
        if "pick_count" in usage.columns:
            st.markdown("### 选股次数分布")
            fig = px.histogram(usage, x="pick_count", nbins=50,
                               title="因子选股次数分布", labels={"pick_count": "选股次数"})
            fig.update_layout(height=300)
            st.plotly_chart(fig, use_container_width=True)

    # ==================== Tab3: 孤儿因子 ====================
    with tab3:
        st.markdown("### 孤儿因子（从未被使用）")
        if orphan:
            orphan_df = registry[registry["name"].isin(orphan)].copy()
            st.metric("孤儿因子数", f"{len(orphan_df):,}")

            # 按 kind 分布
            if "kind" in orphan_df.columns:
                kind_orphan = orphan_df["kind"].value_counts()
                fig = px.pie(values=kind_orphan.values, names=kind_orphan.index,
                             title="孤儿因子来源分布", color_discrete_sequence=px.colors.qualitative.Pastel)
                fig.update_traces(textposition="inside", textinfo="percent+label")
                st.plotly_chart(fig, use_container_width=True)

            # 按 family 分布
            if "family" in orphan_df.columns:
                fam_orphan = orphan_df["family"].value_counts().nlargest(10)
                fig = px.bar(x=fam_orphan.index, y=fam_orphan.values,
                             title="孤儿因子机制族分布（Top 10）",
                             labels={"x": "机制族", "y": "数量"})
                st.plotly_chart(fig, use_container_width=True)

            # 孤儿因子列表
            with st.expander("查看孤儿因子列表"):
                display_cols = [c for c in ["name", "kind", "family", "gate_status", "multi_objective_score",
                                            "decay_status", "first_seen"]
                                if c in orphan_df.columns]
                st.dataframe(orphan_df[display_cols].sort_values("multi_objective_score", ascending=False),
                             use_container_width=True, hide_index=True)
        else:
            st.success("没有孤儿因子！所有注册因子都被使用过。")

    # ==================== Tab4: 因子类型对比 ====================
    with tab4:
        st.markdown("### 因子类型使用效率对比")

        if "factor_type" in registry.columns or "kind" in merged.columns:
            # 合并使用数据和注册表
            if "factor_type" in registry.columns:
                type_usage = merged.merge(
                    registry[["name", "factor_type"]].rename(columns={"name": "factor_name"}),
                    on="factor_name", how="left", suffixes=("", "_reg2")
                )
                if "factor_type" in type_usage.columns:
                    type_stats = type_usage.groupby("factor_type").agg(
                        registered=("factor_name", "nunique"),
                        used=("factor_name", "nunique") if "pick_count" in type_usage.columns else ("factor_name", "count"),
                        total_picks=("pick_count", "sum") if "pick_count" in type_usage.columns else ("factor_name", "count"),
                        avg_picks_per_factor=("pick_count", "mean") if "pick_count" in type_usage.columns else ("factor_name", "count"),
                    ).reset_index()

                    fig = px.bar(type_stats, x="factor_type", y=["registered", "used"],
                                 title="注册 vs 已使用（按因子类型）",
                                 labels={"value": "数量", "factor_type": "因子类型", "variable": "类别"})
                    st.plotly_chart(fig, use_container_width=True)

                    # 使用效率 = 已使用/注册
                    type_stats["usage_rate"] = (type_stats["used"] / type_stats["registered"] * 100).round(1)
                    fig2 = px.bar(type_stats, x="factor_type", y="usage_rate",
                                  title="使用率（已使用/注册）",
                                  labels={"usage_rate": "使用率 (%)", "factor_type": "因子类型"})
                    st.plotly_chart(fig2, use_container_width=True)

        # 策略包引用统计
        if strat_refs:
            st.markdown("### 因子被策略包引用次数")
            ref_df = pd.DataFrame([{"factor": k, "ref_count": v} for k, v in strat_refs.items()])
            ref_df = ref_df.nlargest(30, "ref_count")

            fig = px.bar(ref_df, x="factor", y="ref_count",
                         title="因子被策略包引用次数（Top 30）",
                         labels={"ref_count": "引用次数", "factor": "因子"})
            fig.update_layout(xaxis_tickangle=45, height=500)
            st.plotly_chart(fig, use_container_width=True)


if __name__ == "__main__":
    render()
