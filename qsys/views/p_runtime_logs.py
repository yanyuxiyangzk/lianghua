"""统一运行日志中心：任务健康状态、错误恢复和结构化日志查询。"""
from datetime import datetime, timedelta
import json

import pandas as pd
import streamlit as st

import library

st.set_page_config(page_title="运行日志中心", layout="wide")


def _read(sql: str, params=()) -> pd.DataFrame:
    with library._lconn() as c:
        return pd.read_sql(sql, c, params=params)


def render():
    st.title("🧾 运行日志中心")
    st.caption("统一查看任务运行、错误、耗时与恢复状态；完整原始输出仍由滚动文件日志保存。")

    since_24h = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    stats = _read("""SELECT
        SUM(CASE WHEN event='START' AND created_at>=? THEN 1 ELSE 0 END) starts,
        SUM(CASE WHEN event='END' AND success=1 AND created_at>=? THEN 1 ELSE 0 END) success_count,
        SUM(CASE WHEN event='ERROR' AND created_at>=? THEN 1 ELSE 0 END) error_count,
        MAX(created_at) last_time FROM runtime_log""", (since_24h, since_24h, since_24h))
    row = stats.iloc[0] if not stats.empty else {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("24小时启动", int(row.get("starts") or 0))
    c2.metric("24小时成功", int(row.get("success_count") or 0))
    c3.metric("24小时失败", int(row.get("error_count") or 0))
    c4.metric("最后日志", str(row.get("last_time") or "—"))

    st.subheader("模块健康状态")
    latest = _read("""WITH ranked AS (
        SELECT *, ROW_NUMBER() OVER(PARTITION BY job_key ORDER BY id DESC) rn
        FROM runtime_log WHERE job_key IS NOT NULL)
        SELECT job_key,module,event,level,success,duration_ms,message,created_at
        FROM ranked WHERE rn=1 ORDER BY created_at DESC""")
    if latest.empty:
        st.info("暂无新结构日志。任务下一次执行后会自动写入。")
    else:
        latest["状态"] = latest.apply(
            lambda r: "🔵 运行中" if r.event == "START" else
            ("🟢 成功" if r.success == 1 else "🔴 失败"), axis=1)
        latest["耗时"] = latest["duration_ms"].map(
            lambda x: f"{x/1000:.1f}s" if pd.notna(x) else "—")
        st.dataframe(latest[["状态", "module", "job_key", "created_at", "耗时", "message"]],
                     hide_index=True, width="stretch")

    st.subheader("错误与恢复")
    failures = _read("""SELECT e.job_key,e.module,e.error_type,e.message,e.created_at last_error,
        (SELECT MIN(s.created_at) FROM runtime_log s WHERE s.job_key=e.job_key
         AND s.event='END' AND s.success=1 AND s.created_at>e.created_at) recovered_at
        FROM runtime_log e WHERE e.event='ERROR'
        ORDER BY e.id DESC LIMIT 100""")
    if failures.empty:
        st.success("暂无结构化错误记录")
    else:
        failures["状态"] = failures["recovered_at"].map(
            lambda x: "✅ 已恢复" if pd.notna(x) else "❌ 未恢复")
        st.dataframe(failures[["状态", "module", "job_key", "error_type", "last_error", "recovered_at", "message"]],
                     hide_index=True, width="stretch")

    st.subheader("日志查询")
    modules = _read("SELECT DISTINCT module FROM runtime_log ORDER BY module")["module"].tolist()
    a, b, c = st.columns(3)
    with a:
        hours = st.selectbox("时间范围", [1, 6, 24, 72, 168], index=2,
                             format_func=lambda x: f"最近{x}小时")
    with b:
        module = st.selectbox("模块", ["全部"] + modules)
    with c:
        level = st.selectbox("级别", ["全部", "ERROR", "WARNING", "INFO"])
    keyword = st.text_input("关键词", placeholder="任务名、错误信息、因子名或股票代码")
    where = ["created_at>=?"]
    params = [(datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")]
    if module != "全部":
        where.append("module=?"); params.append(module)
    if level != "全部":
        where.append("level=?"); params.append(level)
    if keyword:
        where.append("(message LIKE ? OR job_key LIKE ? OR step LIKE ?)")
        params.extend([f"%{keyword}%"] * 3)
    logs = _read("SELECT created_at,environment,module,job_key,level,event,step,message,"
                 "duration_ms,success,error_type,run_id FROM runtime_log WHERE "
                 + " AND ".join(where) + " ORDER BY id DESC LIMIT 500", params)
    st.dataframe(logs, hide_index=True, width="stretch", height=520)
    if not logs.empty:
        run_ids = logs["run_id"].dropna().unique().tolist()
        selected = st.selectbox("单次运行详情", run_ids)
        detail = _read("SELECT created_at,event,step,level,message,duration_ms,success,metrics "
                       "FROM runtime_log WHERE run_id=? ORDER BY id", (selected,))
        if "metrics" in detail:
            detail["metrics"] = detail["metrics"].map(
                lambda x: json.dumps(json.loads(x or "{}"), ensure_ascii=False))
        st.dataframe(detail, hide_index=True, width="stretch")


render()
