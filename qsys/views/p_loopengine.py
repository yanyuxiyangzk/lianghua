"""🧬 LoopEngine 挖掘监控：因子类型分布、机制族覆盖、闸门拦截、遗传算法收敛。

数据来源：
  - factor_registry：因子注册表（factor_type/family/gate_status/engine）
  - tested_hashes：哈希检查点（去重统计）
  - failure_patterns：失败模式（拦截分析）
  - engine_state：引擎状态（budget/momentum/field_weights）
"""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import library
import structure


def _load_data():
    registry = library.get_factor_registry()
    with library._lconn() as c:
        tested = pd.read_sql("SELECT * FROM tested_hashes", c)
        failures = pd.read_sql("SELECT * FROM failure_patterns", c)
        engine_row = c.execute("SELECT * FROM engine_state WHERE id='loopengine'").fetchone()
    return registry, tested, failures, engine_row


def _parse_engine_state(row):
    import json
    if not row:
        return {}
    return {
        "iteration": row[1],
        "budget": json.loads(row[2] or "{}"),
        "momentum": json.loads(row[3] or "{}"),
        "field_weights": json.loads(row[4] or "{}"),
        "accepted": row[5] or 0,
    }


def render():
    st.title("🧬 LoopEngine 挖掘监控")
    registry, tested, failures, engine_row = _load_data()
    state = _parse_engine_state(engine_row)

    if registry.empty:
        st.info("因子库为空，请先运行 LoopEngine 挖掘。")
        return

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["📊 挖掘总览", "🧬 遗传算法", "❌ 失败分析", "🔄 挖掘日志", "  失败趋势", "  多类型对比"])

    # ================================ Tab1: 挖掘总览 ================================
    with tab1:
        st.markdown("## 挖掘总览")

        # 概览指标
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("注册因子总数", f"{len(registry):,}")
        with c2:
            n_loop = len(registry[registry["engine"] == "loopengine"]) if "engine" in registry.columns else 0
            st.metric("LoopEngine 因子", f"{n_loop:,}")
        with c3:
            n_pass = int((registry.get("gate_status") == 1).sum()) if "gate_status" in registry.columns else 0
            st.metric("闸门通过", f"{n_pass:,}")
        with c4:
            n_tested = len(tested)
            st.metric("已测哈希", f"{n_tested:,}")

        st.divider()

        # 因子类型分布 + 机制族覆盖
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### 因子类型分布")
            if "factor_type" in registry.columns:
                ft_counts = registry["factor_type"].fillna("量价").value_counts()
                fig = px.pie(values=ft_counts.values, names=ft_counts.index,
                             color_discrete_sequence=px.colors.qualitative.Set2)
                fig.update_traces(textposition="inside", textinfo="percent+label")
                fig.update_layout(height=350, margin=dict(l=20, r=20, t=30, b=20))
                st.plotly_chart(fig, width="stretch")
            else:
                st.info("无 factor_type 数据")

        with c2:
            st.markdown("### 机制族覆盖")
            cov = structure.family_coverage(registry)
            cov_df = pd.DataFrame({"机制族": list(cov.keys()), "因子数": list(cov.values())})
            cov_df = cov_df.sort_values("因子数", ascending=True)
            fig = px.bar(cov_df, x="因子数", y="机制族", orientation="h",
                         color="因子数", color_continuous_scale="Blues")
            fig.update_layout(height=350, margin=dict(l=20, r=20, t=30, b=20),
                              showlegend=False, coloraxis_showscale=False)
            st.plotly_chart(fig, width="stretch")

        st.divider()

        # gate_status 分布
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### 闸门状态分布")
            if "gate_status" in registry.columns:
                gs = registry["gate_status"].fillna(-1).map({1: "收益通过", 0: "未通过", 2: "事件通过", -1: "未测"}).value_counts()
                fig = px.bar(x=gs.index, y=gs.values, color=gs.index,
                             color_discrete_map={"收益通过": "#2ca02c", "未通过": "#d62728",
                                                  "事件通过": "#1f77b4", "未测": "#999"})
                fig.update_layout(height=300, xaxis_title="", yaxis_title="数量",
                                  margin=dict(l=20, r=20, t=30, b=20))
                st.plotly_chart(fig, width="stretch")

        with c2:
            st.markdown("### 因子来源引擎")
            if "engine" in registry.columns:
                eng_counts = registry["engine"].fillna("unknown").value_counts()
                fig = px.pie(values=eng_counts.values, names=eng_counts.index,
                             color_discrete_sequence=px.colors.qualitative.Pastel)
                fig.update_traces(textposition="inside", textinfo="percent+label")
                fig.update_layout(height=300, margin=dict(l=20, r=20, t=30, b=20))
                st.plotly_chart(fig, width="stretch")

        st.divider()

        # 因子类型 × 机制族 交叉表热力图
        if "factor_type" in registry.columns and "family" in registry.columns:
            st.markdown("### 因子类型 × 机制族 热力图")
            cross = pd.crosstab(registry["factor_type"].fillna("量价"),
                                registry["family"].fillna("其他"))
            if not cross.empty:
                fig = px.imshow(cross, text_auto=True, color_continuous_scale="YlOrRd",
                                aspect="auto")
                fig.update_layout(height=400, margin=dict(l=20, r=20, t=30, b=20))
                st.plotly_chart(fig, width="stretch")

    # ================================ Tab2: 遗传算法 ================================
    with tab2:
        st.markdown("## 遗传算法监控")

        if not state:
            st.info("引擎状态未初始化，请先运行 LoopEngine。")
        else:
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("当前迭代轮次", state.get("iteration", 0))
            with c2:
                st.metric("累计入库", state.get("accepted", 0))
            with c3:
                budget_raw = state.get("budget", {})
                budget_p = budget_raw.get("p", budget_raw) if isinstance(budget_raw, dict) else {}
                if budget_p:
                    st.metric("LLM 概率", f"{budget_p.get('llm', 0):.0%}")

            st.divider()

            # Budget 概率分布
            budget_raw = state.get("budget", {})
            budget_p = budget_raw.get("p", budget_raw) if isinstance(budget_raw, dict) else {}
            if budget_p and isinstance(budget_p, dict):
                st.markdown("### 生成方式概率分布")
                valid_items = [(k, v) for k, v in budget_p.items() if isinstance(v, (int, float))]
                if valid_items:
                    bud_df = pd.DataFrame({"方式": [x[0] for x in valid_items], "概率": [x[1] for x in valid_items]})
                    bud_df = bud_df.sort_values("概率", ascending=False)
                    fig = px.bar(bud_df, x="方式", y="概率", color="方式",
                                 color_discrete_sequence=px.colors.qualitative.Set2)
                    fig.update_layout(height=300, xaxis_title="", yaxis_title="概率",
                                      yaxis_tickformat=".0%", margin=dict(l=20, r=20, t=30, b=20))
                    st.plotly_chart(fig, width="stretch")

            # 字段权重 TOP15
            fw = state.get("field_weights", {})
            fw_data = fw.get("w", fw) if isinstance(fw, dict) else {}
            if fw_data:
                st.markdown("### 字段权重 TOP15")
                fw_df = pd.DataFrame({"字段": list(fw_data.keys()), "权重": list(fw_data.values())})
                fw_df = fw_df.sort_values("权重", ascending=True).tail(15)
                fig = px.bar(fw_df, x="权重", y="字段", orientation="h",
                             color="权重", color_continuous_scale="Greens")
                fig.update_layout(height=400, margin=dict(l=20, r=20, t=30, b=20),
                                  coloraxis_showscale=False)
                st.plotly_chart(fig, width="stretch")

            # Momentum
            momentum = state.get("momentum", {})
            if momentum:
                st.markdown("### 窗口微调 Momentum")
                mom_df = pd.DataFrame({"骨架": list(momentum.keys()), "方向": list(momentum.values())})
                mom_df = mom_df.sort_values("方向", ascending=False).head(20)
                fig = px.bar(mom_df, x="方向", y="骨架", orientation="h",
                             color="方向", color_continuous_scale="RdYlGn")
                fig.update_layout(height=350, margin=dict(l=20, r=20, t=30, b=20),
                                  coloraxis_showscale=False)
                st.plotly_chart(fig, width="stretch")

    # ================================ Tab3: 失败分析 ================================
    with tab3:
        st.markdown("## 失败分析")

        if failures.empty:
            st.info("暂无失败记录。")
        else:
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("### 闸门拦截原因 TOP10")
                # 从 failure_patterns 的 reason 字段提取闸门原因
                reasons = failures["reason"].dropna().tolist()
                # 简单统计关键词
                gate_keywords = ["IC", "超额", "夏普", "Calmar", "相关", "深度", "审查"]
                gate_counts = {}
                for kw in gate_keywords:
                    cnt = sum(1 for r in reasons if kw in str(r))
                    if cnt > 0:
                        gate_counts[kw] = cnt
                if gate_counts:
                    gc_df = pd.DataFrame({"闸门": list(gate_counts.keys()), "拦截次数": list(gate_counts.values())})
                    gc_df = gc_df.sort_values("拦截次数", ascending=False)
                    fig = px.bar(gc_df, x="闸门", y="拦截次数", color="拦截次数",
                                 color_continuous_scale="Reds")
                    fig.update_layout(height=350, margin=dict(l=20, r=20, t=30, b=20),
                                      coloraxis_showscale=False)
                    st.plotly_chart(fig, width="stretch")

            with c2:
                st.markdown("### 失败机制族分布")
                fam_counts = failures["family"].dropna().value_counts().head(15)
                if not fam_counts.empty:
                    fig = px.pie(values=fam_counts.values, names=fam_counts.index,
                                 color_discrete_sequence=px.colors.qualitative.Set3)
                    fig.update_traces(textposition="inside", textinfo="percent+label")
                    fig.update_layout(height=350, margin=dict(l=20, r=20, t=30, b=20))
                    st.plotly_chart(fig, width="stretch")

            st.divider()

            # 失败骨架 TOP20
            st.markdown("### 失败骨架 TOP20")
            sk_counts = failures["skeleton"].dropna().value_counts().head(20)
            if not sk_counts.empty:
                sk_df = pd.DataFrame({"骨架": sk_counts.index, "次数": sk_counts.values})
                st.dataframe(sk_df, width="stretch", hide_index=True, height=500)

    # ================================ Tab4: 挖掘日志 ================================
    with tab4:
        st.markdown("## 挖掘日志")

        # 最近入库的因子
        st.markdown("### 最近入库因子（最新20个）")
        if "first_seen" in registry.columns:
            recent = registry.sort_values("first_seen", ascending=False).head(20)
            cols_show = ["name", "factor_type", "family", "engine", "gate_status", "first_seen"]
            cols_avail = [c for c in cols_show if c in recent.columns]
            disp = recent[cols_avail].rename(columns={
                "name": "因子名", "factor_type": "类型", "family": "机制族",
                "engine": "引擎", "gate_status": "闸门", "first_seen": "入库时间"
            })
            st.dataframe(disp, width="stretch", hide_index=True)
        else:
            st.info("无入库时间数据")

        st.divider()

        # 测试统计
        st.markdown("### 哈希检查点统计")
        if not tested.empty:
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("总测试数", f"{len(tested):,}")
            with c2:
                n_passed = int(tested["passed"].sum()) if "passed" in tested.columns else 0
                st.metric("通过数", f"{n_passed:,}")
            with c3:
                rate = n_passed / max(len(tested), 1)
                st.metric("通过率", f"{rate:.2%}")
        else:
            st.info("无测试记录")

    # ================================ Tab5: 失败趋势 ================================
    with tab5:
        st.markdown("## 失败模式趋势分析")

        if failures.empty:
            st.info("暂无失败记录。")
        else:
            # 失败时间趋势
            if "created_at" in failures.columns:
                st.markdown("### 失败数量时间趋势")
                failures["date"] = pd.to_datetime(failures["created_at"], errors="coerce").dt.date
                daily_failures = failures.dropna(subset=["date"]).groupby("date").size().reset_index(name="count")
                if not daily_failures.empty:
                    fig = px.line(daily_failures, x="date", y="count",
                                  title="每日失败因子数量",
                                  labels={"count": "失败数量", "date": "日期"})
                    fig.update_layout(height=350)
                    st.plotly_chart(fig, width="stretch")

            st.divider()

            # 失败→成功转化追踪
            st.markdown("### 失败→成功转化追踪")
            st.markdown("同一骨架的因子失败后，变体是否成功？")

            if "skeleton" in failures.columns and "skeleton" in registry.columns:
                failed_skeletons = set(failures["skeleton"].dropna().unique())
                succeeded_skeletons = set(registry[registry["gate_status"] == 1]["skeleton"].dropna().unique())
                converted = failed_skeletons & succeeded_skeletons

                c1, c2, c3 = st.columns(3)
                with c1:
                    st.metric("失败骨架数", f"{len(failed_skeletons):,}")
                with c2:
                    st.metric("成功骨架数", f"{len(succeeded_skeletons):,}")
                with c3:
                    rate = len(converted) / max(len(failed_skeletons), 1) * 100
                    st.metric("转化率", f"{rate:.1f}%")

                if converted:
                    with st.expander(f"查看已转化的骨架 ({len(converted)}个)"):
                        for sk in sorted(converted):
                            fail_cnt = len(failures[failures["skeleton"] == sk])
                            st.write(f"  - `{sk}` (失败{fail_cnt}次后成功)")

            st.divider()

            # LLM拒绝 vs 规则拒绝
            st.markdown("### LLM拒绝 vs 规则拒绝")
            if "reason" in failures.columns:
                reasons = failures["reason"].fillna("").tolist()
                llm_count = sum(1 for r in reasons if "LLM" in str(r) or "llm" in str(r))
                rule_count = len(reasons) - llm_count

                fig = px.pie(values=[llm_count, rule_count],
                             names=["LLM拒绝", "规则拒绝"],
                             title="拒绝来源分布",
                             color_discrete_sequence=["#FF6B6B", "#4ECDC4"])
                fig.update_traces(textposition="inside", textinfo="percent+label")
                st.plotly_chart(fig, width="stretch")

    # ================================ Tab6: 多类型对比 ================================
    with tab6:
        st.markdown("## 多类型因子效率对比")

        if "factor_type" in registry.columns:
            # 按类型统计
            type_stats = registry.groupby("factor_type").agg(
                总因子数=("name", "count"),
                通过闸门=("gate_status", lambda x: (x == 1).sum()),
                平均评分=("multi_objective_score", "mean"),
            ).reset_index()
            type_stats["通过率"] = (type_stats["通过闸门"] / type_stats["总因子数"] * 100).round(1)

            c1, c2 = st.columns(2)
            with c1:
                fig = px.bar(type_stats, x="factor_type", y=["总因子数", "通过闸门"],
                             title="各类型因子数量对比",
                             labels={"value": "数量", "factor_type": "因子类型", "variable": "类别"},
                             barmode="group")
                fig.update_layout(height=400)
                st.plotly_chart(fig, use_container_width=True)

            with c2:
                fig = px.bar(type_stats, x="factor_type", y="通过率",
                             title="各类型因子通过率",
                             labels={"通过率": "通过率 (%)", "factor_type": "因子类型"},
                             color="通过率", color_continuous_scale="RdYlGn")
                fig.update_layout(height=400)
                st.plotly_chart(fig, use_container_width=True)

            # 按类型 × 机制族交叉分析
            st.markdown("### 类型 × 机制族交叉热力图")
            cross = pd.crosstab(registry["factor_type"].fillna("量价"),
                                registry["family"].fillna("其他"))
            if not cross.empty:
                fig = px.imshow(cross, text_auto=True, color_continuous_scale="YlOrRd",
                                aspect="auto", title="因子类型 × 机制族分布")
                fig.update_layout(height=500)
                st.plotly_chart(fig, use_container_width=True)

            # 按类型对比衰减状态
            if "decay_status" in registry.columns:
                st.markdown("### 各类型衰减状态对比")
                decay_by_type = pd.crosstab(
                    registry["factor_type"].fillna("量价"),
                    registry["decay_status"].fillna("unknown")
                )
                if not decay_by_type.empty:
                    fig = px.bar(decay_by_type.reset_index(), x="factor_type",
                                 y=decay_by_type.columns.tolist(),
                                 title="各类型因子衰减状态分布",
                                 barmode="stack")
                    fig.update_layout(height=400)
                    st.plotly_chart(fig, use_container_width=True)

        else:
            st.info("无 factor_type 数据")


render()
