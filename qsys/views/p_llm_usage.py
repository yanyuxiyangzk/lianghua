"""LLM 用量、缓存命中与预算监控。"""
import sqlite3
import pandas as pd
import streamlit as st

from common import DATA_DIR


def _load_usage(days: int = 30) -> pd.DataFrame:
    db = DATA_DIR / "experience.db"
    try:
        with sqlite3.connect(str(db)) as c:
            return pd.read_sql_query(
                "SELECT * FROM llm_usage_log WHERE created_at >= ? ORDER BY created_at DESC",
                c, params=((pd.Timestamp.now().timestamp() - days * 86400),))
    except Exception:
        return pd.DataFrame()


st.title("🧾 LLM 用量与缓存")
days = st.selectbox("统计范围", [1, 7, 30], index=1, format_func=lambda x: f"最近 {x} 天")
df = _load_usage(days)
if df.empty:
    st.info("暂无 LLM 用量记录")
else:
    calls = len(df)
    hits = int(df["cache_hit"].fillna(0).sum())
    reserved = int(df["reserved_tokens"].fillna(0).sum())
    actual = int(df[["input_tokens", "output_tokens"]].fillna(0).sum().sum())
    a, b, c, d = st.columns(4)
    a.metric("调用记录", calls)
    b.metric("缓存命中率", f"{hits / calls:.1%}")
    c.metric("预留 Token", f"{reserved:,}")
    d.metric("实际 Token", f"{actual:,}")

    by_label = df.groupby("label", dropna=False).agg(
        调用次数=("id", "count"), 缓存命中=("cache_hit", "sum"),
        预留Token=("reserved_tokens", "sum"), 输入Token=("input_tokens", "sum"),
        输出Token=("output_tokens", "sum")).reset_index()
    by_label["命中率"] = by_label["缓存命中"] / by_label["调用次数"].clip(lower=1)
    st.subheader("按任务统计")
    st.dataframe(by_label.sort_values("预留Token", ascending=False), hide_index=True, width="stretch")

    daily = df.assign日期=pd.to_datetime(df["created_at"], unit="s").dt.date
    daily = daily.groupby("日期").agg(调用次数=("id", "count"), 预留Token=("reserved_tokens", "sum"),
                                      缓存命中=("cache_hit", "sum")).reset_index()
    st.subheader("每日趋势")
    st.line_chart(daily.set_index("日期")[["调用次数", "预留Token", "缓存命中"]])

    st.subheader("最近调用明细")
    st.dataframe(df.head(200), hide_index=True, width="stretch")
