"""⏰ 定时任务 页面（包装 tab_sched.render + 执行日志可视化）。"""

import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import tab_sched


def _load_exec_log():
    import library
    try:
        with library._lconn() as c:
            df = pd.read_sql(
                "SELECT * FROM sched_exec_log ORDER BY created_at DESC LIMIT 3000", c)
        return df
    except Exception:
        return pd.DataFrame()


def _render_exec_log():
    df = _load_exec_log()
    if df.empty:
        st.info("暂无执行日志，等待调度任务运行。")
        return

    # 概览
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("总执行次数", len(df))
    with c2:
        st.metric("成功率", f"{df['success'].mean():.1%}")
    with c3:
        avg_dur = df["duration_ms"].mean()
        st.metric("平均耗时", f"{avg_dur:.0f}ms" if avg_dur > 0 else "-")
    with c4:
        st.metric("任务数", df["job_key"].nunique())

    st.divider()

    tab1, tab2, tab3 = st.tabs(["任务耗时", "成功率趋势", "执行明细"])

    with tab1:
        st.markdown("### 各任务耗时分布")
        job_stats = df.groupby("job_name").agg(
            avg_ms=("duration_ms", "mean"),
            p50_ms=("duration_ms", "median"),
            p95_ms=("duration_ms", lambda x: x.quantile(0.95) if len(x) > 5 else x.max()),
            count=("duration_ms", "count")
        ).reset_index().sort_values("avg_ms", ascending=True)
        if not job_stats.empty:
            fig = go.Figure()
            fig.add_trace(go.Bar(y=job_stats["job_name"], x=job_stats["avg_ms"],
                                 name="平均", orientation="h", marker_color="steelblue"))
            fig.add_trace(go.Bar(y=job_stats["job_name"], x=job_stats["p95_ms"],
                                 name="P95", orientation="h", marker_color="coral"))
            fig.update_layout(barmode="group", height=max(300, len(job_stats) * 25),
                              margin=dict(l=20, r=20, t=30, b=20),
                              xaxis_title="耗时 (ms)", yaxis_title="")
            st.plotly_chart(fig, width="stretch")

    with tab2:
        st.markdown("### 每日成功率趋势")
        if "created_at" in df.columns:
            df["date"] = pd.to_datetime(df["created_at"]).dt.date
            daily = df.groupby("date").agg(
                total=("success", "count"),
                ok=("success", "sum")
            ).reset_index()
            daily["rate"] = daily["ok"] / daily["total"]
            daily = daily.sort_values("date")
            fig = px.line(daily, x="date", y="rate", markers=True,
                          labels={"rate": "成功率", "date": "日期"})
            fig.update_layout(height=300, yaxis_tickformat=".0%",
                              margin=dict(l=20, r=20, t=30, b=20))
            st.plotly_chart(fig, width="stretch")

    with tab3:
        st.markdown("### 最近执行记录")
        show_df = df[["job_name", "started_at", "success", "duration_ms", "message"]].head(100)
        show_df["状态"] = show_df["success"].map({1: "OK", 0: "FAIL"})
        show_df["耗时"] = show_df["duration_ms"].apply(lambda x: f"{x}ms" if x else "-")
        st.dataframe(show_df[["job_name", "started_at", "状态", "耗时", "message"]],
                     use_container_width=True, height=400)


def render():
    st.markdown("## ⏰ 定时任务调度")
    tab_main, tab_log = st.tabs(["任务管理", "执行日志"])
    with tab_main:
        tab_sched.render()
    with tab_log:
        _render_exec_log()


if __name__ == "__main__":
    render()
