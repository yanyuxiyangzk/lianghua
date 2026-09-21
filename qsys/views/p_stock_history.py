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
    st.caption("按需抓取同花顺 iFinD 日线或盘中 1 分钟历史数据；只有点击按钮才会调用接口。")
    code = st.text_input("股票代码", placeholder="例如 SH600519、600519.SH 或 600519", key="hist_code")
    data_mode = st.radio("数据类型", ["日线历史", "盘中1分钟历史"], horizontal=True)
    end = date.today()
    range_mode = st.selectbox("历史范围", ["最近1年", "最近3年", "最近5年", "自定义"], key="hist_range")
    if range_mode == "自定义":
        d1, d2 = st.columns(2)
        with d1:
            start = st.date_input("开始日期", end - timedelta(days=365), key="hist_start")
        with d2:
            end = st.date_input("结束日期", end, key="hist_end")
        if start > end:
            st.error("开始日期不能晚于结束日期。")
            return
    else:
        years = {"最近1年": 1, "最近3年": 3, "最近5年": 5}[range_mode]
        start = end - timedelta(days=365 * years)
    st.write(f"抓取区间：{start} 至 {end}（仅点击按钮后触发网络请求）")
    dbcode = _db_code(code)
    valid = bool(dbcode and len(dbcode) == 8 and dbcode[2:].isdigit())
    if not valid:
        st.info("请输入有效的 6 位股票代码后，再点击下方按钮。")
    fetch_clicked = st.button("▶ 开始抓取", type="primary", disabled=not valid,
                              help="只有点击此按钮才会调用同花顺接口；页面加载不会自动爬取。")
    if not valid:
        return
    request_sig = (data_mode, dbcode, str(start), str(end))
    fetched_sig = st.session_state.get("hist_fetched_sig")
    # 输入股票/范围后不主动查库；只有本次点击抓取成功后才查询并展示结果。
    existing = pd.DataFrame()
    if fetched_sig == request_sig:
        if data_mode == "日线历史":
            existing = _load(dbcode, str(start), str(end))
        else:
            with sqlite3.connect(str(DATA_DIR / "market.db")) as conn:
                existing = pd.read_sql("SELECT datetime,open,high,low,close,volume,amount FROM ifind_minute WHERE code=? AND datetime BETWEEN ? AND ? ORDER BY datetime", conn, params=(dbcode, str(start), str(end) + " 23:59:59"))
    a, b, c = st.columns(3)
    a.metric("本地记录", f"{len(existing):,} 条")
    b.metric("最早日期", existing.date.min() if not existing.empty else "—")
    c.metric("最新日期", existing.date.max() if not existing.empty else "—")
    if fetch_clicked:
        with st.spinner("正在从同花顺 iFinD 抓取并写入本地库…"):
            try:
                if data_mode == "日线历史":
                    n = datasource._ths_fetch_daily(dbcode, str(start), str(end))
                    msg = f"抓取完成：写入/覆盖 {n} 条日线记录。"
                else:
                    n, days_ok, failed = datasource.fetch_minute_range_to_db(dbcode, str(start), str(end))
                    msg = f"抓取完成：写入/覆盖 {n:,} 条分钟记录，成功 {days_ok} 个交易日。"
                    if failed:
                        msg += f" 未返回数据 {len(failed)} 天。"
                _record_job(dbcode, str(start), str(end), n)
                st.session_state["hist_fetched_sig"] = request_sig
                st.success(msg + " 重复抓取会覆盖更新。")
                st.rerun()
            except Exception as exc:
                _record_job(dbcode, str(start), str(end), 0, "failed", str(exc)[:500])
                st.error(f"抓取失败：{exc}")
    if fetched_sig == request_sig and not existing.empty:
        st.subheader("最近历史数据")
        st.dataframe(existing.tail(100), hide_index=True, use_container_width=True)
        st.subheader("删除该股票历史行情")
        st.warning("此操作只删除该股票当前类型的历史行情，不删除概率模型、因子快照、策略或分析结果。")
        confirm = st.checkbox("我确认删除该股票历史行情", key="hist_delete_confirm")
        if st.button("删除当前范围历史行情", type="secondary", disabled=not confirm):
            with sqlite3.connect(str(DATA_DIR / "market.db")) as conn:
                if data_mode == "日线历史":
                    cur = conn.execute("DELETE FROM market_daily WHERE source='ths_ifind' AND code=? AND date BETWEEN ? AND ?", (dbcode, str(start), str(end)))
                else:
                    cur = conn.execute("DELETE FROM ifind_minute WHERE code=? AND datetime BETWEEN ? AND ?", (dbcode, str(start), str(end) + " 23:59:59"))
                deleted = cur.rowcount
                conn.execute("UPDATE stock_history_jobs SET row_count=0,status='deleted',last_fetched_at=? WHERE code=? AND source='ths_ifind'", (pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), dbcode))
            st.success(f"已删除 {deleted} 条行情记录；概率模型和因子数据保留。")
            st.rerun()


render()
