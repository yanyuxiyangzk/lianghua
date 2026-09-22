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


def _stock_inventory() -> pd.DataFrame:
    """按股票汇总本地日线、分钟线覆盖，作为页面查询入口。"""
    with datasource._conn() as c:
        return pd.read_sql_query(
            """
            WITH daily AS (
                SELECT code, COUNT(*) AS daily_rows, COUNT(DISTINCT date) AS daily_days,
                       MIN(date) AS daily_start, MAX(date) AS daily_end
                FROM market_daily WHERE source='ths_ifind' GROUP BY code
            ), minute AS (
                SELECT code, COUNT(*) AS minute_rows,
                       COUNT(DISTINCT substr(datetime,1,10)) AS minute_days,
                       MIN(substr(datetime,1,10)) AS minute_start,
                       MAX(substr(datetime,1,10)) AS minute_end
                FROM ifind_minute GROUP BY code
            ), codes AS (
                SELECT DISTINCT code FROM stock_history_jobs_v2
                WHERE status <> 'deleted' AND row_count > 0
            )
            SELECT codes.code, COALESCE(s.name,'') AS name,
                   COALESCE(daily.daily_rows,0) AS daily_rows,
                   COALESCE(daily.daily_days,0) AS daily_days,
                   daily.daily_start, daily.daily_end,
                   COALESCE(minute.minute_rows,0) AS minute_rows,
                   COALESCE(minute.minute_days,0) AS minute_days,
                   minute.minute_start, minute.minute_end
            FROM codes
            LEFT JOIN stock_master s ON s.code=codes.code
            LEFT JOIN daily ON daily.code=codes.code
            LEFT JOIN minute ON minute.code=codes.code
            ORDER BY COALESCE(minute.minute_end,daily.daily_end) DESC, codes.code
            """, c)


def _minute_daily_summary(code: str, start: str, end: str) -> pd.DataFrame:
    with datasource._conn() as c:
        return pd.read_sql_query(
            """SELECT substr(datetime,1,10) AS trade_date, COUNT(*) AS minute_count,
                      MIN(close) AS low_close, MAX(close) AS high_close,
                      SUM(volume) AS volume, SUM(amount) AS amount
               FROM ifind_minute
               WHERE code=? AND datetime BETWEEN ? AND ?
               GROUP BY substr(datetime,1,10) ORDER BY trade_date DESC""",
            c, params=(code, start, end + " 23:59:59"))


def _load_minute_day(code: str, trade_date: str) -> pd.DataFrame:
    with datasource._conn() as c:
        return pd.read_sql_query(
            """SELECT datetime,open,high,low,close,volume,amount
               FROM ifind_minute WHERE code=? AND datetime BETWEEN ? AND ?
               ORDER BY datetime""", c,
            params=(code, trade_date, trade_date + " 23:59:59"))


def _load_market_depth(code: str, minute_ts: str) -> dict[str, pd.DataFrame]:
    """读取指定分钟内已有盘口快照/行情快照/逐笔成交，不跨分钟匹配。"""
    minute = pd.Timestamp(minute_ts).strftime("%Y-%m-%d %H:%M")
    start, end = minute + ":00", minute + ":59"
    with datasource._conn() as c:
        realtime = pd.read_sql_query(
            """SELECT datetime,price,bid1,ask1,volume,amount,turnover,quantity_ratio,
                      speed,change_pct,high,low,limit_up,limit_down
               FROM ifind_realtime WHERE code=? AND datetime BETWEEN ? AND ?
               ORDER BY datetime""", c, params=(code, start, end))
        snapshots = pd.read_sql_query(
            """SELECT ts,price,bid1,ask1,bid_vol_sum,ask_vol_sum,last_tick_vol,
                      volume,amount,turnover,avg_price,outer_vol,inner_vol,
                      quantity_ratio,trade_time,source
               FROM quote_snapshots WHERE code=? AND ts BETWEEN ? AND ? ORDER BY ts""",
            c, params=(code, start, end))
        ticks = pd.read_sql_query(
            """SELECT datetime,price,volume,buyorsell,source
               FROM tick_data WHERE code=? AND datetime BETWEEN ? AND ? ORDER BY datetime""",
            c, params=(code, start, end))
    return {"ifind_realtime": realtime, "quote_snapshots": snapshots, "tick_data": ticks}


def _delete_minute_bar(code: str, minute_ts: str) -> int:
    """删除一根分钟K并使当天派生日内特征失效；盘口原始快照不联删。"""
    day = str(minute_ts)[:10]
    with datasource._conn() as c:
        deleted = c.execute(
            "DELETE FROM ifind_minute WHERE code=? AND datetime=?", (code, minute_ts)).rowcount
        c.execute("DELETE FROM stock_intraday_features WHERE code=? AND trade_date=?", (code, day))
        stock = c.execute("SELECT stock_id FROM stock_master WHERE code=?", (code,)).fetchone()
        if stock:
            c.execute(
                "UPDATE stock_history_jobs_v2 SET row_count=(SELECT COUNT(*) FROM ifind_minute "
                "WHERE code=?), complete_days=MAX(complete_days-1,0), missing_days=missing_days+1,"
                "status='partial',last_fetched_at=? WHERE stock_id=? AND data_type='minute_1m'",
                (code, pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), stock[0]))
    return deleted


def _record_job(code, data_type, start, end, count, status="success", error=None,
                completeness=None):
    """日线和分钟线分别记录，避免同一股票的任务状态互相覆盖。"""
    stock_id = datasource.get_or_create_stock_id(code)
    comp = completeness or {}
    now = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
    with datasource._conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO stock_history_jobs_v2"
            "(stock_id,code,source,data_type,start_date,end_date,row_count,expected_days,"
            "complete_days,missing_days,last_fetched_at,status,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (stock_id, code, "ths_ifind", data_type, start, end, count,
             int(comp.get("expected_days", 0)), int(comp.get("complete_days", 0)),
             int(comp.get("missing_count", 0)), now, status, error))


@st.fragment(run_every=5)
def _render_minute_sync_progress(code: str):
    """同步任务运行时每5秒局部刷新，不重绘整张日线表。"""
    task = datasource.minute_sync_task_status(code)
    if not task:
        return
    api_processed = int(task.get("synced_days") or 0) + int(task.get("failed_days") or 0)
    total = max(1, int(task.get("total_days") or 0))
    remaining = max(0, total - api_processed)
    st.progress(min(1.0, api_processed / total),
                text=(f"状态：{task.get('status')} · 接口进度 {api_processed}/{total} · "
                      f"新同步 {task.get('synced_days', 0)} · 已有数据跳过 {task.get('skipped_days', 0)} · "
                      f"失败 {task.get('failed_days', 0)}"
                      + (f" · 当前 {task.get('current_date')}" if task.get("current_date") else "")))
    if task.get("status") == "running" and task.get("worker_alive"):
        st.caption(f"进度每5秒自动刷新。按最低10秒间隔估算，剩余限速等待约 "
                   f"{remaining * 10 // 60} 分钟；实际时间还包括同花顺接口响应。")
    elif task.get("status") == "interrupted":
        st.info("上次后台任务已中断，可点击“全局同步分钟”从尚未完整的日期继续。")
    if task.get("last_error"):
        st.warning("最近一次同步异常：" + str(task["last_error"]))


def _delete_stock_history(code: str) -> dict:
    """删除单票原始历史与派生特征，保留概率模型、因子和策略结果。"""
    with datasource._conn() as c:
        daily = c.execute(
            "DELETE FROM market_daily WHERE source='ths_ifind' AND code=?", (code,)).rowcount
        minute = c.execute("DELETE FROM ifind_minute WHERE code=?", (code,)).rowcount
        features = c.execute(
            "DELETE FROM stock_intraday_features WHERE code=?", (code,)).rowcount
        c.execute("DELETE FROM minute_backfill_progress WHERE code=?", (code,))
        c.execute(
            "UPDATE stock_history_jobs_v2 SET row_count=0,status='deleted',last_fetched_at=? "
            "WHERE code=?", (pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"), code))
    return {"daily": daily, "minute": minute, "features": features}


def _go(view: str, code: str = "", trade_date: str = "", minute_ts: str = ""):
    st.session_state["stock_history_view"] = view
    st.session_state["stock_history_code"] = code
    st.session_state["stock_history_day"] = trade_date
    st.session_state["stock_history_minute"] = minute_ts
    st.rerun()


def _page_slice(total: int, key: str, page_size: int = 20) -> tuple[int, int, int]:
    """读取当前分页状态，返回当前页、总页数和起始下标。"""
    pages = max(1, (int(total) + page_size - 1) // page_size)
    state_key = f"{key}_page"
    current = max(1, min(int(st.session_state.get(state_key, 1)), pages))
    st.session_state[state_key] = current
    return current, pages, (current - 1) * page_size


def _pagination_bottom(total: int, key: str, current: int,
                       pages: int, page_size: int = 20) -> None:
    """列表底部的上一页、页码输入跳转和下一页控件。"""
    state_key = f"{key}_page"
    # 输入框按当前页使用独立 key，避免组件实例化后再修改同一个 session_state key。
    input_key = f"{key}_page_input_{current}"
    prev_col, info_col, input_col, jump_col, next_col = st.columns([1, 2.2, 1, .8, 1])
    if prev_col.button("← 上一页", key=f"{key}_prev", disabled=current <= 1,
                       use_container_width=True):
        st.session_state[state_key] = current - 1
        st.rerun()
    info_col.markdown(
        f"<div style='text-align:center;padding:8px'>第 {current} / {pages} 页 · "
        f"共 {int(total):,} 条 · 每页 {page_size} 条</div>", unsafe_allow_html=True)
    target = input_col.number_input(
        "页码", min_value=1, max_value=pages, value=current, step=1,
        key=input_key, label_visibility="collapsed")
    if jump_col.button("跳转", key=f"{key}_jump", use_container_width=True):
        st.session_state[state_key] = int(target)
        st.rerun()
    if next_col.button("下一页 →", key=f"{key}_next", disabled=current >= pages,
                       use_container_width=True):
        st.session_state[state_key] = current + 1
        st.rerun()


def _render_stock_rows(inventory: pd.DataFrame):
    headers = st.columns([1.7, 0.8, 1.45, 0.9, 1.0, 1.45, 1.0])
    for col, label in zip(headers, ["股票", "日线天数", "日线范围", "分钟交易日",
                                    "分钟记录", "分钟范围", "操作"]):
        col.markdown(f"**{label}**")
    for row in inventory.itertuples(index=False):
        cols = st.columns([1.7, 0.8, 1.45, 0.9, 1.0, 1.45, 1.0])
        cols[0].write(f"{row.code} {row.name}".strip())
        cols[1].write(f"{int(row.daily_days):,}")
        cols[2].write(f"{row.daily_start or '—'} ～ {row.daily_end or '—'}")
        cols[3].write(f"{int(row.minute_days):,}")
        cols[4].write(f"{int(row.minute_rows):,}")
        cols[5].write(f"{row.minute_start or '—'} ～ {row.minute_end or '—'}")
        with cols[6]:
            b1, b2 = st.columns(2)
            if b1.button("详情", key=f"stock_detail_{row.code}", use_container_width=True):
                _go("detail", row.code)
            if b2.button("删除", key=f"stock_delete_{row.code}", use_container_width=True):
                st.session_state["stock_history_delete"] = row.code
                st.rerun()
        if st.session_state.get("stock_history_delete") == row.code:
            st.warning(f"确认删除 {row.code} 的全部日线、分钟线和日内特征？概率模型结果会保留。")
            yes, no, _ = st.columns([1, 1, 5])
            if yes.button("确认删除", key=f"stock_delete_yes_{row.code}", type="primary"):
                deleted = _delete_stock_history(row.code)
                st.session_state.pop("stock_history_delete", None)
                st.success(f"已删除日线 {deleted['daily']} 条、分钟线 {deleted['minute']} 条、"
                           f"日内特征 {deleted['features']} 条。")
                st.rerun()
            if no.button("取消", key=f"stock_delete_no_{row.code}"):
                st.session_state.pop("stock_history_delete", None)
                st.rerun()


def _render_market_depth(code: str, trade_date: str, minute_ts: str):
    if st.button("← 返回当天分钟列表"):
        _go("day", code, trade_date)
    st.title(f"{code} · {minute_ts} 盘口高频数据")
    st.caption("仅展示该分钟内数据库实际保存的盘口快照与逐笔数据，不使用相邻分钟数据填充。")
    data = _load_market_depth(code, minute_ts)
    total = sum(len(df) for df in data.values())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("高频记录合计", total)
    m2.metric("iFinD盘口快照", len(data["ifind_realtime"]))
    m3.metric("行情深度快照", len(data["quote_snapshots"]))
    m4.metric("逐笔成交", len(data["tick_data"]))
    labels = {
        "ifind_realtime": "iFinD 实时盘口快照",
        "quote_snapshots": "行情深度快照",
        "tick_data": "逐笔成交",
    }
    for key, df in data.items():
        st.subheader(labels[key])
        if df.empty:
            st.caption("该分钟没有保存此类高频数据。")
        else:
            st.dataframe(df, hide_index=True, width="stretch")
    if total == 0:
        st.info("该分钟没有已落库的盘口高频数据。历史1分钟K线不等于历史盘口快照；"
                "只有盘中高频采集任务当时覆盖到该股票，才会产生盘口记录。")


def _render_day_detail(code: str, trade_date: str):
    if st.button("← 返回日线列表"):
        _go("detail", code)
    st.title(f"{code} · {trade_date} 分钟数据")
    minute_day = _load_minute_day(code, trade_date)
    d1, d2, d3 = st.columns(3)
    d1.metric("分钟记录", len(minute_day))
    d2.metric("最早时间", str(minute_day["datetime"].min())[11:19] if not minute_day.empty else "—")
    d3.metric("最晚时间", str(minute_day["datetime"].max())[11:19] if not minute_day.empty else "—")
    if minute_day.empty:
        st.info("该交易日没有分钟历史数据。")
        return
    sync_col, note_col = st.columns([1, 4])
    if sync_col.button("🔄 同步全天盘口", type="primary", use_container_width=True,
                       help="通过同花顺历史快照接口一次同步当前交易日全部可用盘口数据"):
        with st.spinner(f"正在从同花顺同步 {trade_date} 全天盘口快照…"):
            try:
                result = datasource.fetch_orderbook_day_to_db(code, trade_date)
                st.success(f"盘口同步完成：返回 {result['returned']} 条，写入/覆盖 "
                           f"{result['written']} 条，新增 {result['new_rows']} 条；"
                           f"覆盖 {result['start']}～{result['end']}。")
                st.rerun()
            except Exception as exc:
                st.error(f"盘口同步失败：{exc}")
    note_col.caption("同步范围是当前交易日的全部分钟；严格校验返回日期和买一/卖一字段，"
                     "不会用今天的实时盘口冒充历史盘口。")
    if minute_day["close"].nunique(dropna=True) <= 1:
        st.warning("该交易日分钟收盘价没有变化，建议在进入模型前标记为数据异常并复核数据源。")
    page_size = 20
    page_key = f"minute_{code}_{trade_date}"
    page, pages, start_idx = _page_slice(
        len(minute_day), page_key, page_size)
    part = minute_day.iloc[start_idx:start_idx + page_size]
    headers = st.columns([1.25, .65, .65, .65, .65, .9, 1.0, 1.0])
    for col, label in zip(headers, ["时间", "开盘", "最高", "最低", "收盘",
                                    "成交量", "成交额", "操作"]):
        col.markdown(f"**{label}**")
    for row in part.itertuples(index=False):
        cols = st.columns([1.25, .65, .65, .65, .65, .9, 1.0, 1.0])
        values = [str(row.datetime)[11:19], row.open, row.high, row.low, row.close,
                  f"{float(row.volume or 0):,.0f}", f"{float(row.amount or 0):,.0f}"]
        for col, value in zip(cols[:7], values):
            col.write(value)
        minute_key = str(row.datetime).replace(" ", "_").replace(":", "")
        with cols[7]:
            detail_col, delete_col = st.columns(2)
            if detail_col.button("详情", key=f"minute_detail_{code}_{minute_key}",
                                 use_container_width=True):
                _go("depth", code, trade_date, str(row.datetime))
            if delete_col.button("删除", key=f"minute_delete_{code}_{minute_key}",
                                 use_container_width=True):
                st.session_state["stock_history_minute_delete"] = str(row.datetime)
                st.rerun()
        if st.session_state.get("stock_history_minute_delete") == str(row.datetime):
            st.warning(f"确认删除 {row.datetime} 这一根分钟K线？当天日内特征将同时失效。")
            yes, no, _ = st.columns([1, 1, 5])
            if yes.button("确认删除", key=f"minute_delete_yes_{code}_{minute_key}", type="primary"):
                deleted = _delete_minute_bar(code, str(row.datetime))
                st.session_state.pop("stock_history_minute_delete", None)
                st.success(f"已删除 {deleted} 条分钟K线；当天日内特征已清除。")
                st.rerun()
            if no.button("取消", key=f"minute_delete_no_{code}_{minute_key}"):
                st.session_state.pop("stock_history_minute_delete", None)
                st.rerun()
    _pagination_bottom(len(minute_day), page_key, page, pages, page_size)


def _render_stock_detail(code: str):
    if st.button("← 返回已抓取股票列表"):
        _go("list")
    flash = st.session_state.pop("stock_history_detail_flash", None)
    if flash:
        level, message = flash
        getattr(st, level, st.info)(message)
    inventory = _stock_inventory()
    matched = inventory[inventory["code"] == code]
    name = str(matched.iloc[0]["name"]) if not matched.empty else ""
    st.title(f"{code} {name}".strip())
    st.caption("股票详情以日线为主列表；点击每行右侧“分钟详情”查看当天全部1分钟数据。")
    with datasource._conn() as c:
        daily = pd.read_sql_query(
            """SELECT date,open,high,low,close,volume,amount,fetched_at
               FROM market_daily WHERE source='ths_ifind' AND code=? ORDER BY date DESC""",
            c, params=(code,))
        minute_counts = pd.read_sql_query(
            "SELECT substr(datetime,1,10) AS date,COUNT(*) AS minute_count "
            "FROM ifind_minute WHERE code=? GROUP BY substr(datetime,1,10)",
            c, params=(code,))
    if daily.empty:
        st.info("该股票暂无日线数据。请返回列表页抓取日线历史。")
        summary = _minute_daily_summary(code, "1900-01-01", "2999-12-31")
        if not summary.empty:
            st.caption(f"分钟数据仍有 {len(summary)} 个交易日。")
        return
    count_map = (minute_counts.set_index("date")["minute_count"].to_dict()
                 if not minute_counts.empty else {})
    synced_days = sum(1 for day in daily["date"].astype(str) if int(count_map.get(day, 0)) >= 200)
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("日线交易日", len(daily))
    p2.metric("最早日期", daily["date"].min())
    p3.metric("最新日期", daily["date"].max())
    p4.metric("分钟已同步", f"{synced_days}/{len(daily)}")

    sync_all_col, note_col = st.columns([1.4, 4.6])
    if sync_all_col.button("🔄 一次性同步全部分钟", type="primary",
                           use_container_width=True,
                           help="单次调用同花顺THS_HF，抓取当前日线范围内全部1分钟历史"):
        range_start, range_end = str(daily["date"].min()), str(daily["date"].max())
        try:
            with st.spinner(f"正在单次抓取 {range_start} 至 {range_end} 的1分钟历史…"):
                result = datasource.fetch_minute_period_to_db(
                    code, range_start, range_end, "1min")
                features = datasource.compute_intraday_features(
                    code, range_start, range_end, min_rows_per_day=200)
                completeness = datasource.minute_completeness(
                    code, range_start, range_end)
                with datasource._conn() as c:
                    total_rows = c.execute(
                        "SELECT COUNT(*) FROM ifind_minute WHERE code=?",
                        (code,)).fetchone()[0]
                _record_job(code, "minute_1m", range_start, range_end, total_rows,
                            "success" if not completeness["missing_count"] else "partial",
                            completeness=completeness)
            message = (f"一次性同步完成：单次接口返回并写入/覆盖 {result['written']:,} 条，"
                       f"覆盖 {result['days']} 个交易日（完整 {result['complete_days']} 天），"
                       f"重算日内特征 {features['computed_days']} 天；"
                       f"数据范围 {result['first']} 至 {result['last']}。")
            if completeness["missing_count"]:
                message += f" 与日线相比仍缺少或不完整 {completeness['missing_count']} 天。"
            st.session_state["stock_history_detail_flash"] = ("success", message)
            st.rerun()
        except Exception as exc:
            st.session_state["stock_history_detail_flash"] = (
                "error", f"一次性分钟同步失败：{exc}")
            st.rerun()
    note_col.caption("只调用一次同花顺高频接口并批量入库，不再逐日循环，也不需要10秒间隔。"
                     "同步完成后会校验返回日期覆盖并重算日内特征。")
    page_size = 20
    page_key = f"daily_{code}"
    page, pages, start_idx = _page_slice(len(daily), page_key, page_size)
    part = daily.iloc[start_idx:start_idx + page_size]
    headers = st.columns([1.0, 0.7, 0.7, 0.7, 0.7, .9, 1.05, .85, 1.45])
    for col, label in zip(headers, ["日期", "开盘", "最高", "最低", "收盘",
                                    "成交量", "成交额", "分钟状态", "操作"]):
        col.markdown(f"**{label}**")
    for row in part.itertuples(index=False):
        cols = st.columns([1.0, 0.7, 0.7, 0.7, 0.7, .9, 1.05, .85, 1.45])
        vals = [row.date, row.open, row.high, row.low, row.close,
                f"{float(row.volume or 0):,.0f}", f"{float(row.amount or 0):,.0f}"]
        for col, value in zip(cols[:7], vals):
            col.write(value)
        minute_count = int(count_map.get(str(row.date), 0))
        if minute_count >= 200:
            cols[7].success(f"已同步 {minute_count}")
        elif minute_count > 0:
            cols[7].warning(f"不完整 {minute_count}")
        else:
            cols[7].caption("待同步")
        with cols[8]:
            sync_col, detail_col = st.columns(2)
            if sync_col.button("同步分钟", key=f"day_sync_{code}_{row.date}",
                               disabled=minute_count >= 200,
                               use_container_width=True,
                               help="从同花顺抓取该交易日的1分钟历史并写入本地库"):
                try:
                    with st.spinner(f"正在同步 {row.date} 的1分钟数据…"):
                        written = datasource.fetch_minute_to_db(code, str(row.date), "1min")
                        if written:
                            datasource.compute_intraday_features(
                                code, str(row.date), str(row.date), min_rows_per_day=200)
                        with datasource._conn() as c:
                            total_rows = c.execute(
                                "SELECT COUNT(*) FROM ifind_minute WHERE code=?",
                                (code,)).fetchone()[0]
                        range_start, range_end = (str(daily["date"].min()),
                                                  str(daily["date"].max()))
                        completeness = datasource.minute_completeness(
                            code, range_start, range_end)
                        _record_job(code, "minute_1m", range_start, range_end, total_rows,
                                    "success" if not completeness["missing_count"] else "partial",
                                    completeness=completeness)
                    if written:
                        message = f"{row.date} 分钟数据同步完成，写入/覆盖 {written:,} 条。"
                        level = "success"
                    else:
                        message = (f"{row.date} 同花顺未返回分钟数据；可能为非交易日、"
                                   "接口权限不足或该日数据暂不可用。")
                        level = "warning"
                    st.session_state["stock_history_detail_flash"] = (level, message)
                    st.rerun()
                except Exception as exc:
                    st.session_state["stock_history_detail_flash"] = (
                        "error", f"{row.date} 分钟数据同步失败：{exc}")
                    st.rerun()
            if detail_col.button("分钟详情", key=f"day_detail_{code}_{row.date}",
                                 use_container_width=True):
                _go("day", code, str(row.date))
    _pagination_bottom(len(daily), page_key, page, pages, page_size)


def _render_fetch_form():
    st.subheader("抓取新股票或更新历史")
    flash = st.session_state.pop("stock_history_fetch_flash", None)
    if flash:
        level, message = flash
        getattr(st, level, st.info)(message)
    code = st.text_input(
        "股票代码", placeholder="例如 SH600519、600519.SH 或 600519", key="hist_code",
        help="输入代码后，只有点击开始抓取才访问同花顺接口。")
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
    batch_days = 10
    if data_mode == "盘中1分钟历史":
        batch_days = st.select_slider(
            "每次抓取交易日数", options=[5, 10, 20], value=10,
            help="分钟历史按交易日逐日调用同花顺。分批可避免页面长时间无响应；"
                 "完成一批后可继续抓取，已完成日期不会重复请求。")
    dbcode = _db_code(code)
    valid = bool(dbcode and len(dbcode) == 8 and dbcode[2:].isdigit())
    if not valid:
        st.info("请输入有效的 6 位股票代码后，再点击下方按钮。")
    fetch_label = "▶ 开始抓取" if data_mode == "日线历史" else "▶ 开始/继续抓取本批"
    fetch_clicked = st.button(fetch_label, type="primary", disabled=not valid,
                              help="只有点击此按钮才会调用同花顺接口；页面加载不会自动爬取。")
    if not valid:
        return
    if fetch_clicked:
        with st.spinner("正在从同花顺 iFinD 抓取并写入本地库…"):
            try:
                if data_mode == "日线历史":
                    n = datasource._ths_fetch_daily(dbcode, str(start), str(end))
                    msg = f"抓取完成：写入/覆盖 {n} 条日线记录。"
                    _record_job(dbcode, "daily", str(start), str(end), n)
                else:
                    result = datasource.backfill_missing_minutes(
                        dbcode, str(start), str(end), batch_days=int(batch_days))
                    comp = result["after"]
                    with datasource._conn() as c:
                        total_rows = c.execute(
                            "SELECT COUNT(*) FROM ifind_minute WHERE code=? AND datetime BETWEEN ? AND ?",
                            (dbcode, str(start), str(end) + " 23:59:59")).fetchone()[0]
                    msg = (f"本批完成：尝试 {result['attempted_days']} 个交易日，"
                           f"修复 {result['repaired_days']} 天，写入/覆盖 {result['written']:,} 条；"
                           f"当前完整 {comp['complete_days']}/{comp['expected_days']} 天，"
                           f"剩余 {result['remaining_days']} 天。")
                    if result["failed_days"]:
                        msg += " 本批未成功日期：" + "、".join(result["failed_days"][:10])
                    _record_job(dbcode, "minute_1m", str(start), str(end), total_rows,
                                "success" if result["status"] == "complete" else "partial",
                                completeness=comp)
                    if result["remaining_days"]:
                        msg += " 请点击“开始/继续抓取本批”继续，进度已保存。"
                st.session_state["stock_history_fetch_flash"] = ("success", msg)
                st.rerun()
            except Exception as exc:
                _record_job(dbcode, "daily" if data_mode == "日线历史" else "minute_1m",
                            str(start), str(end), 0, "failed", str(exc)[:500])
                st.error(f"抓取失败：{exc}")


def render():
    view = st.session_state.get("stock_history_view", "list")
    code = st.session_state.get("stock_history_code", "")
    trade_date = st.session_state.get("stock_history_day", "")
    minute_ts = st.session_state.get("stock_history_minute", "")
    if view == "depth" and code and trade_date and minute_ts:
        _render_market_depth(code, trade_date, minute_ts)
        return
    if view == "day" and code and trade_date:
        _render_day_detail(code, trade_date)
        return
    if view == "detail" and code:
        _render_stock_detail(code)
        return

    st.title("📥 单股票历史数据")
    st.caption("按需抓取同花顺 iFinD 日线或盘中1分钟历史；本地详情查询不会调用接口。")
    inventory = _stock_inventory()
    st.subheader("已抓取股票列表")
    if inventory.empty:
        st.info("本地数据库暂无已抓取股票。")
    else:
        i1, i2, i3 = st.columns(3)
        i1.metric("已抓取股票", inventory["code"].nunique())
        i2.metric("有分钟数据", int((inventory["minute_rows"] > 0).sum()))
        i3.metric("分钟数据总量", f"{int(inventory['minute_rows'].sum()):,} 条")
        _render_stock_rows(inventory)
    st.divider()
    _render_fetch_form()


render()
