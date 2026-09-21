"""📥 单股票历史数据：按需抓取 iFinD 最近一年并可删除行情快照。"""
from datetime import date, timedelta
import sqlite3
import pandas as pd
import streamlit as st

import datasource
from common import DATA_DIR

st.set_page_config(page_title="单股票历史数据", layout="wide")


def _db_code(raw: str) -> str:
    s = (raw or "").strip().upper()
    if "." in s:
        n, m = s.split(".", 1); return f"{m}{n}"
    if s.startswith(("SH", "SZ", "BJ")) and len(s) == 8: return s
    if len(s) == 6:
        return ("SH" if s.startswith("6") else "BJ" if s.startswith(("4", "8", "92")) else "SZ") + s
    return s


def _load(code: str, start: str, end: str) -> pd.DataFrame:
    with sqlite3.connect(str(DATA_DIR / "market.db")) as c:
        return pd.read_sql("SELECT date,open,high,low,close,volume,amount,fetched_at FROM market_daily WHERE source='ths_ifind' AND code=? AND date BETWEEN ? AND ? ORDER BY date", c, params=(code, start, end))


def _record_job(code, start, end, count, status="success", error=None):
    with sqlite3.connect(str(DATA_DIR / "market.db")) as c:
        c.execute("INSERT OR REPLACE INTO stock_history_jobs(code,source,start_date,end_date,row_count,last_fetched_at,status,error) VALUES(?,?,?,?,?,?,?,?)",
                  (code, "ths_ifind", start, end, count, pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), status, error))


def render():
    st.title("📥 单股票历史数据")
    st.caption("仅抓取同花顺 iFinD 日线并写入 market.db；默认最近一年。删除只影响该股票行情数据，不删除概率模型、因子快照或策略结果。")
    code = st.text_input("股票代码", placeholder="例如 SH600519、600519.SH 或 600519", key="hist_code")
    end = date.today()
    start = end - timedelta(days=365)
    st.write(f"抓取区间：{start} 至 {end}")
    dbcode = _db_code(code)
    valid = bool(dbcode and len(dbcode) == 8 and dbcode[2:].isdigit())
    if not valid:
        st.info("请输入有效的 6 位股票代码后，再点击下方按钮。")
    fetch_clicked = st.button("▶ 开始爬取最近一年数据", type="primary", disabled=not valid,
                              help="只有点击此按钮才会调用同花顺接口；页面加载不会自动爬取。")
    if not valid:
        return
    existing = _load(dbcode, str(start), str(end))
    a, b, c = st.columns(3)
    a.metric("本地记录", f"{len(existing):,} 条")
    b.metric("最早日期", existing.date.min() if not existing.empty else "—")
    c.metric("最新日期", existing.date.max() if not existing.empty else "—")
    if fetch_clicked:
        with st.spinner("正在从同花顺 iFinD 抓取并写入本地库…"):
            try:
                n = datasource._ths_fetch_daily(dbcode, str(start), str(end))
                _record_job(dbcode, str(start), str(end), n)
                st.success(f"抓取完成：写入/覆盖 {n} 条记录。重复抓取会按股票+日期覆盖更新。")
                st.rerun()
            except Exception as exc:
                _record_job(dbcode, str(start), str(end), 0, "failed", str(exc)[:500])
                st.error(f"抓取失败：{exc}")
    if not existing.empty:
        st.subheader("最近历史数据")
        st.dataframe(existing.tail(100), hide_index=True, use_container_width=True)
        st.subheader("删除该股票历史行情")
        st.warning("此操作只删除 market_daily 中该股票的 ths_ifind 行情，不删除概率模型、因子快照、策略或分析结果。")
        confirm = st.checkbox("我确认删除该股票最近一年行情数据", key="hist_delete_confirm")
        if st.button("删除最近一年行情", type="secondary", disabled=not confirm):
            with sqlite3.connect(str(DATA_DIR / "market.db")) as conn:
                cur = conn.execute("DELETE FROM market_daily WHERE source='ths_ifind' AND code=? AND date BETWEEN ? AND ?", (dbcode, str(start), str(end)))
                deleted = cur.rowcount
                conn.execute("UPDATE stock_history_jobs SET row_count=0,status='deleted',last_fetched_at=? WHERE code=? AND source='ths_ifind'", (pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), dbcode))
            st.success(f"已删除 {deleted} 条行情记录；概率模型和因子数据保留。")
            st.rerun()


render()
