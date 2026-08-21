"""🧬 进化看板：RD-Agent 因子进化过程（假设/因子代码/指标/反馈）。"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import LOG_DIR, list_traces, load_trace, metric_subset

st.title("🧬 进化看板")
st.caption("RD-Agent + Qlib 因子进化闭环的过程回放 · 数据源固定为 qlib 本地库（RD-Agent 产物）")

traces = list_traces()
if not traces:
    st.info(f"未发现 RD-Agent 运行记录（{LOG_DIR} 下没有 trace 目录）。先跑 `rdagent fin_factor`。")
    st.stop()

sel = st.selectbox("选择一次进化运行（trace）", traces, format_func=lambda p: p.name)
data = load_trace(str(sel))
rounds = data["rounds"]
for err in data["errors"][:5]:
    st.warning(err)

rows = []
for r in rounds:
    if r["metrics"] is not None:
        s = metric_subset(r["metrics"])
        s.name = "基线" if r["round"] == 0 else f"Round {r['round']}"
        rows.append(s)
if rows:
    df = pd.DataFrame(rows)
    df.index.name = "轮次"
    st.subheader("各轮回测指标")
    st.dataframe(df.style.format("{:.4f}"), width='stretch')

    ic_col = next((c for c in ["IC", "Rank IC"] if c in df.columns), None)
    ar_col = "1day.excess_return_with_cost.annualized_return"
    fig = go.Figure()
    if ic_col:
        fig.add_trace(go.Bar(x=df.index, y=df[ic_col], name=ic_col))
    if ar_col in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df[ar_col], name="年化超额(含费)", yaxis="y2"))
    fig.update_layout(yaxis2=dict(overlaying="y", side="right"), height=380,
                      legend=dict(orientation="h"), margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width='stretch')

    if ic_col:
        comp = df.drop(index="基线", errors="ignore")
        if not comp.empty:
            bi = comp[ic_col].astype(float).idxmax()
            st.success(f"当前最佳（按 {ic_col}，除基线）：**{bi}** · " +
                       " · ".join(f"{k}={v:.4f}" for k, v in comp.loc[bi].items()))
else:
    st.info("该 trace 尚无包含回测指标的轮次（可能还在提案/编码阶段）。")

st.subheader("假设与因子明细")
for r in reversed(rounds):
    label = ("基线" if r["round"] == 0 else f"Round {r['round']}") + f" · {r.get('time','')}"
    if r["feedback"] and r["feedback"]["decision"] is not None:
        label += " ✅ 被接受" if r["feedback"]["decision"] else " ❌ 被拒绝"
    with st.expander(label, expanded=(r == rounds[-1])):
        st.markdown(f"**假设**：{r['hypothesis']}")
        if r["reason"]:
            st.markdown(f"**理由**：{r['reason'][:1000]}")
        if r["feedback"]:
            st.markdown(f"**反馈**：{'被接受' if r['feedback']['decision'] else '被拒绝'} — {r['feedback']['reason']}")
        for t in r["tasks"]:
            st.markdown(f"**因子 `{t['name']}`**：{t['description']}")
            if t.get("formulation"):
                st.caption(f"公式：{t['formulation']}")
            if t["code"]:
                st.code(t["code"], language="python")
            else:
                st.caption("（因子代码见对应 workspace 的 factor.py）")
