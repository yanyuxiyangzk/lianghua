"""组合策略详情页 — 策略包组合对比 + 投票分析。"""

import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import library


def _load_data():
    with library._lconn() as c:
        strategies = pd.read_sql("SELECT * FROM strategies", c)
        combos = pd.read_sql("SELECT * FROM combo_strategies", c)
    return strategies, combos


def render():
    st.title("  组合策略详情")

    strategies, combos = _load_data()

    if strategies.empty:
        st.info("策略库为空。")
        return

    # ===== Tab 布局 =====
    tab1, tab2, tab3 = st.tabs(["  策略包对比", "  组合策略", "  投票分析"])

    # ==================== Tab1: 策略包对比 ====================
    with tab1:
        st.markdown("## 策略包对比")

        # 解析策略包信息
        strat_records = []
        for _, row in strategies.iterrows():
            factors = json.loads(row["factors"]) if isinstance(row["factors"], str) and row["factors"] else []
            strat_records.append({
                "名称": row.get("name", ""),
                "股票池": row.get("pool_name", ""),
                "Top N": row.get("top_n", 0),
                "方法": row.get("method", ""),
                "OOS胜率": row.get("oos_winrate", "N/A"),
                "因子数": len(factors),
                "因子列表": ", ".join([f.get("name", "") if isinstance(f, dict) else str(f) for f in factors[:5]]),
            })

        strat_df = pd.DataFrame(strat_records)
        st.dataframe(strat_df, use_container_width=True, hide_index=True)

        # OOS胜率对比
        valid = strat_df[strat_df["OOS胜率"].apply(lambda x: x not in [None, "N/A", "None", ""])]
        if not valid.empty:
            # 将胜率字符串转为数字
            valid = valid.copy()
            valid["OOS胜率_num"] = valid["OOS胜率"].apply(
                lambda x: float(str(x).replace("%", "")) if str(x).replace(".", "").replace("-", "").isdigit() else 0
            )
            valid = valid.sort_values("OOS胜率_num", ascending=False)

            fig = px.bar(valid, x="名称", y="OOS胜率_num", color="股票池",
                         title="策略包 OOS 胜率对比",
                         labels={"OOS胜率_num": "OOS胜率 (%)", "名称": "策略包"})
            fig.update_layout(xaxis_tickangle=45, height=400)
            st.plotly_chart(fig, use_container_width=True)

    # ==================== Tab2: 组合策略 ====================
    with tab2:
        st.markdown("## 组合策略")

        if combos.empty:
            st.info("暂无组合策略。")
        else:
            for _, row in combos.iterrows():
                with st.expander(f"  {row.get('name', 'N/A')}"):
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        st.metric("股票池", row.get("pool_name", "N/A"))
                    with c2:
                        st.metric("Top N", row.get("top_n", "N/A"))
                    with c3:
                        st.metric("规则", row.get("rule", "N/A"))

                    if row.get("packs"):
                        packs = json.loads(row["packs"]) if isinstance(row["packs"], str) else row["packs"]
                        st.markdown("**子策略包：**")
                        for p in packs:
                            if isinstance(p, dict):
                                st.write(f"  - {p.get('name', 'N/A')}: 权重 {p.get('weight', 'N/A')}")
                            else:
                                st.write(f"  - {p}")

    # ==================== Tab3: 投票分析 ====================
    with tab3:
        st.markdown("## 投票分析")

        # 分析每个因子被多少个策略包使用
        factor_votes = {}
        for _, row in strategies.iterrows():
            factors = json.loads(row["factors"]) if isinstance(row["factors"], str) and row["factors"] else []
            strat_name = row.get("name", "")
            for f in factors:
                name = f.get("name", "") if isinstance(f, dict) else str(f)
                if name not in factor_votes:
                    factor_votes[name] = {"strategies": [], "kinds": set()}
                factor_votes[name]["strategies"].append(strat_name)
                factor_votes[name]["kinds"].add(f.get("kind", "") if isinstance(f, dict) else "")

        if factor_votes:
            vote_df = pd.DataFrame([
                {
                    "因子": k,
                    "被引用次数": len(v["strategies"]),
                    "来源": ", ".join(v["kinds"]) if v["kinds"] else "",
                    "策略包列表": ", ".join(v["strategies"]),
                }
                for k, v in factor_votes.items()
            ]).sort_values("被引用次数", ascending=False)

            st.markdown("### 因子被策略包引用次数")
            fig = px.bar(vote_df.head(30), x="因子", y="被引用次数", color="来源",
                         title="因子被策略包引用次数（Top 30）",
                         labels={"被引用次数": "引用次数"})
            fig.update_layout(xaxis_tickangle=45, height=500)
            st.plotly_chart(fig, use_container_width=True)

            # 多包投票交集分析
            st.markdown("### 多包投票分析")
            st.markdown("""
            **投票规则**：取至少2个策略包的交集，降低过拟合风险。

            - 1票：不入选
            - 2票：可入选（需额外验证）
            - 3票以上：高置信度
            """)

            vote_counts = vote_df["被引用次数"].value_counts().sort_index()
            fig = px.bar(x=vote_counts.index, y=vote_counts.values,
                         title="因子被引用次数分布",
                         labels={"x": "引用次数", "y": "因子数量"})
            st.plotly_chart(fig, use_container_width=True)

            # 详细表格
            with st.expander("查看详细投票数据"):
                st.dataframe(vote_df, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    render()
