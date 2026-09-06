"""硬闸门诊断 — 11项闸门逐项 pass/fail 可视化。"""

import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="硬闸门诊断", layout="wide")


def _load_data():
    import library
    with library._lconn() as c:
        df = pd.read_sql("SELECT * FROM gate_detail_log ORDER BY created_at DESC LIMIT 5000", c)
    return df


def _parse_metrics(metrics_str):
    try:
        return json.loads(metrics_str) if isinstance(metrics_str, str) else {}
    except Exception:
        return {}


def _parse_reasons(reasons_str):
    try:
        return json.loads(reasons_str) if isinstance(reasons_str, str) else []
    except Exception:
        return []


GATE_LABELS = {
    "IC": "|IC|", "超额2025": "2025超额", "夏普2025": "2025夏普",
    "超额2026": "2026超额", "夏普2026": "2026夏普", "Calmar": "Calmar",
    "近9月": "近9月超额", "近12月": "近12月超额", "最大IC相关": "IC相关"
}
GATE_THRESHOLDS = {
    "IC": 0.02, "超额2025": 0, "夏普2025": 0.5,
    "超额2026": 0, "夏普2026": 0.5, "Calmar": 1.0,
    "近9月": 0, "近12月": 0, "最大IC相关": 0.70
}


def render():
    st.markdown("## 硬闸门诊断")
    df = _load_data()
    if df.empty:
        st.info("暂无闸门评估数据，等待 LoopEngine 或 gate_check 任务运行。")
        return

    df["metrics_parsed"] = df["metrics"].apply(_parse_metrics)
    df["reasons_parsed"] = df["fail_reasons"].apply(_parse_reasons)

    # 概览指标
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("总评估数", len(df))
    with c2:
        pass_rate = df["passed"].mean()
        st.metric("通过率", f"{pass_rate:.1%}")
    with c3:
        st.metric("通过数", int(df["passed"].sum()))
    with c4:
        latest = df["gate_date"].max() if "gate_date" in df.columns else "-"
        st.metric("最新评估日", latest)

    st.divider()

    tab1, tab2, tab3, tab4 = st.tabs(["漏斗图", "每日趋势", "失败原因", "单因子雷达"])

    with tab1:
        st.markdown("### 11项闸门淘汰漏斗")
        reasons_all = []
        for reasons in df["reasons_parsed"]:
            reasons_all.extend(reasons)
        if reasons_all:
            reason_counts = {}
            for r in reasons_all:
                for gate_key in GATE_LABELS:
                    if gate_key in r:
                        reason_counts[gate_key] = reason_counts.get(gate_key, 0) + 1
                        break
            if reason_counts:
                funnel_df = pd.DataFrame({
                    "闸门": [GATE_LABELS.get(k, k) for k in reason_counts],
                    "淘汰数": list(reason_counts.values())
                }).sort_values("淘汰数", ascending=False)
                fig = px.funnel(funnel_df, x="淘汰数", y="闸门")
                fig.update_layout(height=400, margin=dict(l=20, r=20, t=30, b=20))
                st.plotly_chart(fig, width="stretch")
            else:
                st.info("无淘汰数据")
        else:
            st.info("无失败原因数据")

    with tab2:
        st.markdown("### 每日通过率趋势")
        if "gate_date" in df.columns:
            daily = df.groupby("gate_date").agg(
                total=("passed", "count"), passed=("passed", "sum")
            ).reset_index()
            daily["pass_rate"] = daily["passed"] / daily["total"]
            daily = daily.sort_values("gate_date")
            fig = go.Figure()
            fig.add_trace(go.Bar(x=daily["gate_date"], y=daily["total"],
                                 name="总评估", marker_color="lightblue"))
            fig.add_trace(go.Bar(x=daily["gate_date"], y=daily["passed"],
                                 name="通过", marker_color="green"))
            fig.update_layout(barmode="overlay", height=350,
                              margin=dict(l=20, r=20, t=30, b=20),
                              xaxis_title="日期", yaxis_title="数量")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("无日期数据")

    with tab3:
        st.markdown("### 失败原因分布")
        if reasons_all:
            from collections import Counter
            rc = Counter()
            for r in reasons_all:
                for gate_key in GATE_LABELS:
                    if gate_key in r:
                        rc[gate_key] += 1
                        break
            if rc:
                pie_df = pd.DataFrame({"原因": [GATE_LABELS.get(k, k) for k in rc],
                                       "数量": list(rc.values())})
                fig = px.pie(pie_df, names="原因", values="数量",
                             color_discrete_sequence=px.colors.qualitative.Set2)
                fig.update_layout(height=350, margin=dict(l=20, r=20, t=30, b=20))
                st.plotly_chart(fig, width="stretch")

    with tab4:
        st.markdown("### 单因子雷达图")
        names = [n for n in df["factor_name"].unique() if n]
        if not names:
            st.info("暂无因子数据")
        else:
            sel = st.selectbox("选择因子", names[:100] if len(names) > 100 else names)
            if sel:
                row = df[df["factor_name"] == sel].iloc[0]
                metrics = _parse_metrics(row["metrics"])
                if metrics:
                    cats = list(GATE_LABELS.values())
                    vals_raw = []
                    thresholds = []
                    for k in GATE_LABELS:
                        v = abs(metrics.get(k, 0))
                        t = GATE_THRESHOLDS.get(k, 0)
                        if k == "最大IC相关":
                            vals_raw.append(min(v, 1.0))
                            thresholds.append(t)
                        elif "夏普" in k or "Calmar" in k:
                            vals_raw.append(min(v, 3.0))
                            thresholds.append(t)
                        else:
                            vals_raw.append(v)
                            thresholds.append(t)
                    fig = go.Figure()
                    fig.add_trace(go.Scatterpolar(r=vals_raw + [vals_raw[0]],
                                                   theta=cats + [cats[0]],
                                                   fill="toself", name="实际值"))
                    fig.add_trace(go.Scatterpolar(r=thresholds + [thresholds[0]],
                                                   theta=cats + [cats[0]],
                                                   fill="toself", name="阈值",
                                                   line=dict(dash="dash")))
                    fig.update_layout(height=400, polar=dict(radialaxis=dict(visible=True)),
                                      margin=dict(l=40, r=40, t=30, b=30))
                    st.plotly_chart(fig, width="stretch")
                    st.json(metrics)


if __name__ == "__main__":
    render()
