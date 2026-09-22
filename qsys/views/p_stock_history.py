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
    page_size = st.selectbox("每页分钟条数", [20, 50, 100], index=1,
                             key=f"minute_page_size_{code}_{trade_date}")
    pages = max(1, (len(minute_day) + page_size - 1) // page_size)
    page = st.number_input("页码", min_value=1, max_value=pages, value=1, step=1,
                           key=f"minute_page_{code}_{trade_date}")
    part = minute_day.iloc[(int(page) - 1) * page_size:int(page) * page_size]
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


def _render_stock_detail(code: str):
    if st.button("← 返回已抓取股票列表"):
        _go("list")
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
    if daily.empty:
        st.info("该股票暂无日线数据。请返回列表页抓取日线历史。")
        summary = _minute_daily_summary(code, "1900-01-01", "2999-12-31")
        if not summary.empty:
            st.caption(f"分钟数据仍有 {len(summary)} 个交易日。")
        return
    p1, p2, p3 = st.columns(3)
    p1.metric("日线交易日", len(daily))
    p2.metric("最早日期", daily["date"].min())
    p3.metric("最新日期", daily["date"].max())
    page_size = st.selectbox("每页日线条数", [20, 50, 100], index=1,
                             key=f"daily_page_size_{code}")
    pages = max(1, (len(daily) + page_size - 1) // page_size)
    page = st.number_input("页码", min_value=1, max_value=pages, value=1, step=1,
                           key=f"daily_page_{code}")
    part = daily.iloc[(int(page) - 1) * page_size:int(page) * page_size]
    headers = st.columns([1.0, 0.75, 0.75, 0.75, 0.75, 1.0, 1.15, 0.8])
    for col, label in zip(headers, ["日期", "开盘", "最高", "最低", "收盘",
                                    "成交量", "成交额", "操作"]):
        col.markdown(f"**{label}**")
    for row in part.itertuples(index=False):
        cols = st.columns([1.0, 0.75, 0.75, 0.75, 0.75, 1.0, 1.15, 0.8])
        vals = [row.date, row.open, row.high, row.low, row.close,
                f"{float(row.volume or 0):,.0f}", f"{float(row.amount or 0):,.0f}"]
        for col, value in zip(cols[:7], vals):
            col.write(value)
        if cols[7].button("分钟详情", key=f"day_detail_{code}_{row.date}",
                          use_container_width=True):
            _go("day", code, str(row.date))


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
