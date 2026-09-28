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


def _load_cache_stats() -> tuple[int, int, int]:
    """返回缓存条目数、今日调用数、今日预留 token；无记录时也正常展示。"""
    db = DATA_DIR / "experience.db"
    try:
        with sqlite3.connect(str(db)) as c:
            cache_n = int(c.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0])
            today = pd.Timestamp.now().strftime("%Y-%m-%d")
            row = c.execute("SELECT calls, reserved_tokens FROM llm_usage WHERE day=?", (today,)).fetchone()
            return cache_n, int(row[0]) if row else 0, int(row[1]) if row else 0
    except Exception:
        return 0, 0, 0


st.title("🧾 LLM 用量与缓存")
days = st.selectbox("统计范围", [1, 7, 30], index=1, format_func=lambda x: f"最近 {x} 天")
df = _load_usage(days)
cache_count, today_calls, today_reserved = _load_cache_stats()
if df.empty:
    st.info("暂无可计费调用明细；缓存和预算状态仍可正常查看。")
    a, b, c = st.columns(3)
    a.metric("缓存条目", f"{cache_count:,}")
    b.metric("今日普通请求预留次数", f"{today_calls:,}")
    c.metric("今日预留 Token", f"{today_reserved:,}")
else:
    calls = len(df)
    hits = int(df["cache_hit"].fillna(0).sum())
    reserved = int(df["reserved_tokens"].fillna(0).sum())
    actual = int(df[["input_tokens", "output_tokens"]].fillna(0).sum().sum())
    a, b, c, d = st.columns(4)
    a.metric("调用记录", calls)
    b.metric("缓存命中率", f"{hits / calls:.1%}")
    c.metric("预留 Token", f"{reserved:,}")
    d.metric("接口已报告 Token", f"{actual:,}")
    st.caption(f"当前响应缓存：{cache_count:,} 条 · 今日预算记录：{today_calls:,} 次 / {today_reserved:,} Token")

    by_label = df.groupby("label", dropna=False).agg(
        调用次数=("id", "count"), 缓存命中=("cache_hit", "sum"),
        预留Token=("reserved_tokens", "sum"), 输入Token=("input_tokens", "sum"),
        输出Token=("output_tokens", "sum")).reset_index()
    by_label["命中率"] = by_label["缓存命中"] / by_label["调用次数"].clip(lower=1)
    st.subheader("按任务统计")
    st.dataframe(by_label.sort_values("预留Token", ascending=False), hide_index=True, width="stretch")

    daily = df.assign(日期=pd.to_datetime(df["created_at"], unit="s").dt.date)
    daily = daily.groupby("日期").agg(调用次数=("id", "count"), 预留Token=("reserved_tokens", "sum"),
                                      缓存命中=("cache_hit", "sum")).reset_index()
    st.subheader("每日趋势")
    st.line_chart(daily.set_index("日期")[["调用次数", "预留Token", "缓存命中"]])

    st.caption('新版预算按保守输入估算＋输出上限预留，每次重试单独计数。失败或接口未返回用量的请求记为未知，不代表零消耗；历史记录未补算输入预算。')
    if 'status' in df:
        unknown=int(((df['cache_hit']==0) & (df['input_tokens'].isna() | df['output_tokens'].isna())).sum())
        if unknown:
            st.warning(f'{unknown} 条非缓存请求缺少完整接口用量，已报告 Token 不是完整账单。')
    st.subheader("最近调用明细")
    st.dataframe(df.head(200), hide_index=True, width="stretch")

st.subheader('RD-Agent 独立调用预算')
st.caption('RD-Agent 请求另行记录。以下为分词估算输入与输出预留额度，不是供应商账单；失败请求也占用次数预算。')
rd_db=DATA_DIR/'rdagent_llm_usage.db'
if rd_db.exists():
    try:
        with sqlite3.connect(rd_db.resolve().as_uri()+'?mode=ro',uri=True) as c:
            rd=pd.read_sql_query('SELECT day AS 日期,model AS 模型,COUNT(*) AS 请求数,SUM(input_estimate) AS 预估输入,SUM(output_limit) AS 预留输出,SUM(output_estimate) AS 预估输出,SUM(status="failed") AS 失败数 FROM attempts GROUP BY day,model ORDER BY day DESC LIMIT 30',c)
            circuits=pd.read_sql_query('SELECT model AS 模型,datetime(until,"unixepoch","+8 hours") AS 熔断截止北京时间 FROM circuits WHERE until>strftime("%s","now")',c)
        st.dataframe(rd,hide_index=True,width='stretch')
        if not circuits.empty:
            st.warning('存在模型权限错误熔断，暂停请求以避免无效重试。')
            st.dataframe(circuits,hide_index=True,width='stretch')
    except sqlite3.Error:
        st.info('RD-Agent 用量表尚未初始化，等待首次受预算控制的请求。')
else:
    st.info('尚无 RD-Agent 新版用量记录，历史调用未补录。')
