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
        st.info("暂无聚类结果。因子值积累达到共同观测数后，将由每周聚类任务自动生成。")
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
