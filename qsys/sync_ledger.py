"""盘后增量同步与游标账本（sync_cursor）。

每只股票的每类数据记录：上次已同步到哪天（last_synced_date）→ 本次目标（target_date）、
写入行数、成败。全市场日线增量用 THS_HQ 批量（50 只/次）；分钟线只抓三类热票
（当日触板股 + 持仓 + 今日名单）——全市场日级分钟会烧掉 26% 周配额，不抓。

调度：16:05 job_incremental_sync（盘后）。
"""
import time
from datetime import datetime

import pandas as pd

import datasource
from crawl_journal import record as _cj

_CURSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_cursor(
    code TEXT NOT NULL, data_type TEXT NOT NULL,
    last_synced_date TEXT,      -- 上次实际同步到的行情日期（游标）
    target_date TEXT,           -- 本次目标日期
    rows_written INTEGER,
    status TEXT,                -- ok / no_data(停牌等) / failed
    updated_at TEXT,
    PRIMARY KEY(code, data_type));
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def snapshot_minutes(code: str, day: str) -> pd.DataFrame:
    """盘中分时 0 配额方案：把当日 ifind_realtime 快照（热码 15s）聚合成 1 分钟 K 线。

    盘后 15:05 的 THS_HF 校准（minute_hot_incremental）会写入正式分钟线；
    本函数只用于盘中/缺分钟线时的临时展示，不回写 ifind_minute。
    """
    with datasource._conn() as c:
        df = pd.read_sql_query(
            "SELECT datetime, price, volume, amount FROM ifind_realtime"
            " WHERE code=? AND datetime BETWEEN ? AND ? AND price IS NOT NULL"
            " ORDER BY datetime", c, params=(code, day, day + " 23:59:59"))
    if df.empty:
        return pd.DataFrame()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["minute"] = df["datetime"].dt.floor("min")
    g = df.groupby("minute")
    out = pd.DataFrame({
        "open": g["price"].first(), "high": g["price"].max(),
        "low": g["price"].min(), "close": g["price"].last(),
        "amount": g["amount"].last() - g["amount"].first(),  # 累计额差分
    })
    out["volume"] = (g["volume"].last() - g["volume"].first()).clip(lower=0)
    return out.reset_index().rename(columns={"minute": "datetime"})



def _to_db_code(code: str) -> str:
    """600519.SH / sh600519 → SH600519（库内版式）。"""
    import re
    m = re.match(r"^(\d{6})\.([A-Za-z]{2})$", str(code).strip())
    if m:
        return f"{m.group(2).upper()}{m.group(1)}"
    return str(code).strip().upper()


def setup():
    with datasource._conn() as c:
        c.executescript(_CURSOR_SCHEMA)


def cursor_report() -> pd.DataFrame:
    """监控页用：游标台账全量。"""
    setup()
    with datasource._conn() as c:
        return pd.read_sql_query(
            "SELECT * FROM sync_cursor ORDER BY updated_at DESC", c)


def _bump(c, code, dtype, last_synced, target, rows, status):
    c.execute("INSERT OR REPLACE INTO sync_cursor VALUES(?,?,?,?,?,?,?)",
              (code, dtype, last_synced, target, int(rows), status, _now()))


def daily_incremental(day: str, pause: float = 0.5) -> dict:
    """全市场非 ST 日线增量：THS_HQ 50 只/批；游标逐股推进。"""
    setup()
    import bulk_history_fetch as bf
    codes = bf.non_st_codes()
    with datasource._conn() as c:
        cursor = {r[0]: r[1] for r in c.execute(
            "SELECT code, last_synced_date FROM sync_cursor WHERE data_type='daily'")}
    # 注意：网络 I/O 期间不持有数据库连接（嵌套写会撞锁）；每批开短连接写入
    written = failed = 0
    t0 = time.time()
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        try:
            df, _res, err = datasource.ths_history(
                batch, "open,high,low,close,volume,amount", day, day,
                "Fill:Original,Interval:D")
            if err not in (0, None):
                raise RuntimeError(f"THS_HQ 错误码 {err}")
        except Exception as exc:
            failed += len(batch)
            with datasource._conn() as c:
                for code in batch:
                    _bump(c, code, "daily", cursor.get(code), day, 0,
                          f"failed:{type(exc).__name__}")
            continue
        got = {}
        if df is not None and not df.empty:
            d = df.copy()
            d.columns = [str(x).strip().lower() for x in d.columns]
            tcol = next((x for x in ("time", "date", "trade_date") if x in d.columns), None)
            ccol = next((x for x in ("thscode", "code") if x in d.columns), None)
            if tcol and ccol:
                d["date"] = pd.to_datetime(d[tcol], errors="coerce").dt.strftime("%Y-%m-%d")
                d = d[d["date"] == day]
                # 接口返回 thscode 为 600519.SH 版式，转回库内 SH600519 版式
                d["_db_code"] = d[ccol].map(_to_db_code)
                for code, g in d.groupby("_db_code"):
                    got[code] = g
        # 先在自己连接里解析 stock_id，再开短写事务（嵌套写连接会撞 SQLite 锁）
        rows_to_write = []
        for raw_code, g in got.items():
            stock_id = datasource.get_or_create_stock_id(raw_code)
            r = g.iloc[-1]
            rows_to_write.append((raw_code, stock_id, r, len(g)))
        with datasource._conn() as c:
            for code, stock_id, r, n_rows in rows_to_write:
                c.execute(
                    "INSERT OR REPLACE INTO market_daily"
                    "(source,code,date,open,high,low,close,volume,amount,fetched_at,stock_id)"
                    " VALUES('ths_ifind',?,?,?,?,?,?,?,?,?,?)",
                    (code, day, r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                     r.get("volume"), r.get("amount"), _now(), stock_id))
                _bump(c, code, "daily", day, day, n_rows, "ok")
                written += 1
            for code in batch:
                if code not in got:
                    # 停牌/无数据：游标不动，状态标注
                    _bump(c, code, "daily", cursor.get(code), day, 0, "no_data")
        time.sleep(pause)
    _cj("sync_ledger.daily_incremental", "daily_incremental", day, rows=written,
        detail=f"失败 {failed}")
    return {"written": written, "failed": failed, "elapsed_sec": round(time.time() - t0, 1)}


def _hot_minute_codes(day: str) -> list[str]:
    """分钟增量三类名单：当日触板股 + 持仓 + 今日选股名单。"""
    codes = set()
    with datasource._conn() as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='limit_up_events'")}
        if tables:
            for r in c.execute(
                    "SELECT code FROM limit_up_events WHERE date=? AND touched=1", (day,)):
                codes.add(r[0])
    try:
        import experience
        with experience._conn() as c:
            for r in c.execute("SELECT code FROM positions WHERE status IN ('open','pending')"):
                codes.add(r[0])
        latest = experience.list_pick_dates(limit=1)
        if latest:
            for r in experience.picks_on_date(latest[0]).itertuples():
                for it in experience.pick_items_detail(int(r.id)).itertuples():
                    codes.add(it.code)
    except Exception:
        pass
    return sorted(codes)


def minute_hot_incremental(day: str, pause: float = 1.0) -> dict:
    """三类热票的当日分钟线补齐（THS_HF），游标推进。"""
    setup()
    codes = _hot_minute_codes(day)
    ok = failed = 0
    with datasource._conn() as c:
        cursor = {r[0]: r[1] for r in c.execute(
            "SELECT code, last_synced_date FROM sync_cursor WHERE data_type='minute'")}
    for code in codes:
        try:
            r = datasource.fetch_minute_period_to_db(code, day, day)
            with datasource._conn() as c:
                _bump(c, code, "minute", day, day, r["written"],
                      "ok" if r["complete_days"] else "partial")
            ok += 1
        except Exception as exc:
            with datasource._conn() as c:
                _bump(c, code, "minute", cursor.get(code), day, 0,
                      f"failed:{type(exc).__name__}")
            failed += 1
        time.sleep(pause)
    _cj("sync_ledger.minute_hot", "minute_hot_incremental", day, rows=ok,
        detail=f"失败 {failed} · 名单 {len(codes)}")
    return {"ok": ok, "failed": failed, "codes": len(codes)}
