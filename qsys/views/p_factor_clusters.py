"""🧩 因子相关性聚类：簇总览、代表因子与局部关系图。"""
import sqlite3
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import DATA_DIR

st.set_page_config(page_title="因子相关性聚类", layout="wide")


def _load():
    with sqlite3.connect(str(DATA_DIR / "market.db")) as c:
        clusters = pd.read_sql("SELECT * FROM factor_clusters", c)
        corr = pd.read_sql("SELECT * FROM factor_correlation", c)
        reg = pd.read_sql("SELECT name, family, theory_id, gate_status, validation_status FROM factor_registry", c)
    return clusters, corr, reg


def _network(cluster_id: str, clusters: pd.DataFrame, corr: pd.DataFrame):
    members = clusters[clusters["cluster_id"] == cluster_id]
    names = set(members["factor_name"])
    edges = corr[corr["factor_a"].isin(names) & corr["factor_b"].isin(names)].copy()
    edges = edges.reindex(edges["corr"].abs().sort_values(ascending=False).index).head(300)
    nodes = list(names)
    if not nodes:
        return
    import math
    pos = {n: (math.cos(2 * math.pi * i / len(nodes)), math.sin(2 * math.pi * i / len(nodes))) for i, n in enumerate(nodes)}
    fig = go.Figure()
    for _, e in edges.iterrows():
        x0, y0 = pos[e.factor_a]; x1, y1 = pos[e.factor_b]
        fig.add_trace(go.Scatter(x=[x0, x1, None], y=[y0, y1, None], mode="lines",
                                 line=dict(width=max(1, abs(e.corr) * 4), color="#d95f02" if e.corr > 0 else "#1b9e77"),
                                 hoverinfo="none", showlegend=False))
    rep = set(members.loc[members.cluster_role == "representative", "factor_name"])
    fig.add_trace(go.Scatter(x=[pos[n][0] for n in nodes], y=[pos[n][1] for n in nodes], mode="markers+text",
                             text=[n[:18] for n in nodes], textposition="top center",
                             marker=dict(size=[18 if n in rep else 9 for n in nodes], color=["#e74c3c" if n in rep else "#4c78a8" for n in nodes]),
                             hovertext=nodes, hoverinfo="text", name="因子"))
    fig.update_layout(height=560, xaxis=dict(visible=False), yaxis=dict(visible=False),
                      margin=dict(l=10, r=10, t=20, b=10), title=f"{cluster_id} 关系图（边数 {len(edges)}）", showlegend=False)
    st.plotly_chart(fig, use_container_width=True)


def render():
    st.title("🧩 因子相关性聚类")
    st.caption("基于已落库因子值的 Spearman 相关；红点为簇代表因子，红/绿边分别为正/负相关。")
    try:
        clusters, corr, reg = _load()
    except Exception as exc:
        st.error(f"聚类数据读取失败：{exc}")
        return
    if clusters.empty:
        st.info("暂无聚类结果。先运行一次因子体检/回测积累因子值，再点击下方按钮生成聚类。")
        with st.expander("没有快照数据？先积累一批内置因子样本"):
            st.caption("仅计算 6 个内置因子和沪深300，使用本地行情缓存，不调用 LLM。")
            if st.button("积累内置因子样本", key="cluster_seed"):
                import factor_eval
                import signals
                from common import all_pools, get_last_trade_day
                import datasource
                codes = all_pools().get("沪深300", [])
                end = get_last_trade_day()
                ok = 0
                with st.spinner("正在计算并落库因子快照…"):
                    for name in signals.BUILTIN_FACTORS:
                        try:
                            factor_eval.get_factor_values({"name": name, "kind": "builtin"}, codes, end, lookback_days=180, source=datasource.get_loop_source())
                            ok += 1
                        except Exception:
                            continue
                st.success(f"已完成 {ok} 个内置因子快照，请再次点击生成聚类。")
        if st.button("立即尝试生成聚类", type="primary"):
            import library
            result = library.cluster_factors(threshold=0.85, min_obs=60)
            if result.empty:
                st.warning("当前因子值快照仍不足 60 个共同观测，请先运行因子体检或因子详情回测。")
            else:
                st.success(f"已生成 {result.cluster_id.nunique()} 个聚类簇")
                st.rerun()
        return
    latest = clusters[clusters.cluster_version == clusters.cluster_version.max()].copy()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("聚类簇", latest.cluster_id.nunique())
    c2.metric("参与因子", latest.factor_name.nunique())
    c3.metric("代表因子", int((latest.cluster_role == "representative").sum()))
    c4.metric("冗余因子", int((latest.cluster_role == "redundant").sum()))
    summary = latest.groupby("cluster_id").agg(成员数=("factor_name", "count"), 代表因子=("cluster_role", lambda s: latest.loc[s.index[s == "representative"], "factor_name"].iloc[0] if (s == "representative").any() else "—"), 机制族=("family", lambda s: "、".join(sorted(set(str(x) for x in s if pd.notna(x)))))).reset_index()
    summary = summary.sort_values("成员数", ascending=False)
    st.subheader("聚类簇总览")
    st.dataframe(summary, hide_index=True, use_container_width=True)
    selected = st.selectbox("选择聚类簇查看关系图", summary.cluster_id.tolist())
    _network(selected, latest, corr)
    detail = latest[latest.cluster_id == selected].merge(reg, left_on="factor_name", right_on="name", how="left")
    st.subheader(f"{selected} 成员明细")
    st.dataframe(detail[["factor_name", "cluster_role", "cluster_score", "family", "theory_id", "gate_status", "validation_status"]], hide_index=True, use_container_width=True)


render()
