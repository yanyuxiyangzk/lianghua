"""🗄 本地数据仓库：market.db 所有落库数据的总览与浏览。

定位：凡是定时任务/采集线程拉回来的数据（iFinD 日线/基本面/公告/日历、
行情快照、板块资金流、因子库……）都在这里统一可查——
拉到本地但没专属页面的数据，用这页兜底展示，不用等新菜单。
"""

import pandas as pd
import streamlit as st

import datasource

st.title("🗄 本地数据仓库")
st.caption("所有自动落库的数据（iFinD 同步 / 行情快照 / 板块资金 / 因子库）统一浏览；"
           "同步任务开关在 **⏰ 定时任务** 页。")

with datasource._conn() as conn:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    stats = []
    for t in tables:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            # 探测时间列，取最新时间做 freshness 参考
            tcol = next((c for c in ("date", "ts", "trade_date", "report_date", "updated_at", "fetched_at")
                         if c in cols), None)
            latest = conn.execute(f"SELECT MAX({tcol}) FROM {t}").fetchone()[0] if tcol else ""
            stats.append({"表": t, "行数": n, "列数": len(cols), "最新": latest or "-"})
        except Exception:
            stats.append({"表": t, "行数": -1, "列数": 0, "最新": "读取出错"})

st.subheader("📊 表总览", anchor=False)
st.dataframe(pd.DataFrame(stats), width="stretch", hide_index=True)

st.subheader("🔍 表浏览", anchor=False)
c1, c2, c3 = st.columns([2, 2, 1])
with c1:
    tbl = st.selectbox("选择表", tables,
                       index=tables.index("ifind_basic_daily") if "ifind_basic_daily" in tables else 0)
with c3:
    limit = st.selectbox("行数", [200, 500, 1000], index=1)

with datasource._conn() as conn:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
    where, params = [], []
    if "code" in cols:
        kw = c2.text_input("代码过滤（可空，如 600519）", "")
        if kw.strip():
            where.append("code LIKE ?")
            params.append(f"%{kw.strip()}%")
    tcol = next((c for c in ("date", "ts", "report_date", "updated_at", "fetched_at") if c in cols), None)
    sql = f"SELECT * FROM {tbl}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if tcol:
        sql += f" ORDER BY {tcol} DESC"
    sql += f" LIMIT {limit}"
    df = pd.read_sql(sql, conn, params=params)

st.caption(f"{tbl} · 显示最新 {len(df)} 行（共 {stats[[s['表'] for s in stats].index(tbl)]['行数']} 行）")
st.dataframe(df, width="stretch", height=560)
