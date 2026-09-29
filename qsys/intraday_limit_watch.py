"""盘中涨停预警影子扫描（只记录，不下单）。

信号（09:45 / 10:15 各扫一次，全市场快照）：
  涨幅带 2%~7%（不追已封板/过高位）· 量比 ≥3 · 当时成交额 ≥3000万 · 非ST
  联动条件（至少其一）：同板块当前已有涨停（板块效应）/ 昨日封板（连板接力）

EOD 结算（19:20，涨停事件加工之后）：封板与否、首封时间、收盘价、alert→收盘收益。
攒 2-4 周命中率表后再评估是否接入选股系统（source='intraday_limit_watch'）。
"""
from datetime import datetime, timedelta

import pandas as pd

import datasource
from limit_up_events import limit_threshold

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_limit_watch (
    date TEXT NOT NULL, ts TEXT NOT NULL, code TEXT NOT NULL,
    name TEXT, alert_price REAL, chg_pct REAL, quantity_ratio REAL,
    amount REAL, turnover REAL, speed REAL,
    sector_name TEXT, sector_limit_count INTEGER,
    prev_sealed INTEGER, prev_board INTEGER,
    reason TEXT,
    sealed INTEGER, first_seal_ts TEXT, close REAL, ret_alert_to_close REAL,
    PRIMARY KEY (date, ts, code)
);
"""

MIN_CHG, MAX_CHG = 2.0, 7.0        # 涨幅带（%）
MIN_QR = 3.0                       # 量比下限
MIN_AMOUNT = 3e7                   # 当时成交额下限（元）


def setup():
    with datasource._conn() as c:
        c.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _latest_snapshot(day: str) -> pd.DataFrame:
    """全市场每代码最新快照（取扫描时点之前最近一批）。"""
    with datasource._conn() as c:
        return pd.read_sql_query(
            "SELECT r.code, r.datetime, r.price, r.change_pct, r.quantity_ratio,"
            " r.amount, r.turnover, r.speed FROM ifind_realtime r"
            " JOIN (SELECT code, MAX(datetime) md FROM ifind_realtime"
            "       WHERE datetime BETWEEN ? AND ? GROUP BY code) t"
            " ON t.code=r.code AND t.md=r.datetime",
            c, params=(day + " 00:00:00", day + " 23:59:59"))


def _names(codes: list[str]) -> dict:
    if not codes:
        return {}
    with datasource._conn() as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ifind_stocklist'")}
        if tables:
            rows = c.execute(
                f"SELECT code,name FROM ifind_stocklist WHERE code IN ({','.join('?' * len(codes))})",
                codes).fetchall()
            if rows:
                return dict(rows)
        rows = c.execute(
            f"SELECT code,name FROM stock_master WHERE code IN ({','.join('?' * len(codes))})",
            codes).fetchall()
    return dict(rows)


def _industries(codes: list[str]) -> dict:
    if not codes:
        return {}
    with datasource._conn() as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_industry'")}
        if not tables:
            return {}
        rows = c.execute(
            f"SELECT code,sector_name FROM stock_industry WHERE code IN ({','.join('?' * len(codes))})"
            " ORDER BY updated_at DESC", codes).fetchall()
    out = {}
    for code, sector in rows:
        out.setdefault(code, sector)
    return out


def _prev_sealed_map(day: str) -> dict:
    """昨日（上一交易日）封板档案：code → board_count。"""
    with datasource._conn() as c:
        prev = c.execute(
            "SELECT MAX(date) FROM limit_up_events WHERE date<?", (day,)).fetchone()[0]
        if not prev:
            return {}
        rows = c.execute(
            "SELECT code,board_count FROM limit_up_events WHERE date=? AND sealed_close=1",
            (prev,)).fetchall()
    return {r[0]: int(r[1] or 1) for r in rows}


def scan(day: str | None = None, slot: str = "0945") -> dict:
    """扫一次全市场快照，命中即落库（幂等：同日同 slot 重跑覆盖）。返回扫描摘要。"""
    setup()
    day = day or datetime.now().strftime("%Y-%m-%d")
    ts = f"{day} {'09:45' if slot == '0945' else '10:15'}:00"
    snap = _latest_snapshot(day)
    if snap.empty:
        return {"day": day, "slot": slot, "hits": 0, "note": "无当日快照"}
    names = _names(snap["code"].tolist())

    # 板块涨停计数（当前快照口径：涨幅达阈值即视为涨停/贴板）
    snap = snap.dropna(subset=["price", "change_pct"])
    # limit_threshold 返回小数（0.098），change_pct 是百分数（4.5）——统一到百分数比较
    thr = [limit_threshold(c, names.get(c, "")) * 100 for c in snap["code"]]
    snap = snap.assign(_thr=thr)
    ind_map = _industries(snap["code"].tolist())
    snap["_ind"] = snap["code"].map(ind_map)
    sealed_now = snap[snap["change_pct"] >= snap["_thr"]]
    sector_limit = sealed_now.groupby("_ind")["code"].count().to_dict()

    prev = _prev_sealed_map(day)
    rows = []
    for r in snap.itertuples():
        name = names.get(r.code, "")
        if "ST" in str(name).upper():
            continue
        if not (MIN_CHG <= r.change_pct <= MAX_CHG):
            continue
        if not r.quantity_ratio or r.quantity_ratio < MIN_QR:
            continue
        if not r.amount or r.amount < MIN_AMOUNT:
            continue
        sector_n = sector_limit.get(ind_map.get(r.code), 0)
        prev_board = prev.get(r.code, 0)
        if sector_n < 1 and prev_board < 1:
            continue  # 无板块联动也非接力，孤立异动不追
        reason = []
        if sector_n:
            reason.append(f"板块{sector_n}只涨停")
        if prev_board:
            reason.append(f"昨日{prev_board}板接力")
        rows.append((day, ts, r.code, name, float(r.price), float(r.change_pct),
                     float(r.quantity_ratio), float(r.amount),
                     float(r.turnover) if pd.notna(r.turnover) else None,
                     float(r.speed) if pd.notna(r.speed) else None,
                     ind_map.get(r.code), int(sector_n), 1 if prev_board else 0,
                     int(prev_board), "；".join(reason), None, None, None, None))
    with datasource._conn() as c:
        c.executemany("INSERT OR REPLACE INTO intraday_limit_watch VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return {"day": day, "slot": slot, "hits": len(rows),
            "snapshot_time": str(snap["datetime"].max())}


def settle(day: str) -> dict:
    """EOD 结算：封板与否/首封时间（limit_up_events）+ 收盘价与 alert→收盘收益。

    同时回补历史未结算行（某日事件加工失败/缺跑时，记录不会永久悬NULL）。
    """
    setup()
    with datasource._conn() as c:
        rows = c.execute(
            "SELECT date,code,alert_price FROM intraday_limit_watch WHERE sealed IS NULL AND date<=?",
            (day,)).fetchall()
        if not rows:
            return {"day": day, "settled": 0}
        n = 0
        for rday, code, alert in rows:
            ev = c.execute(
                "SELECT sealed_close,first_seal_ts FROM limit_up_events WHERE date=? AND code=?",
                (rday, code)).fetchone()
            close = c.execute(
                "SELECT close FROM market_daily WHERE source='ths_ifind' AND code=? AND date=?",
                (code, rday)).fetchone()
            if not ev and not close:
                continue  # 该日事件与日线都未落库，留待下次结算
            sealed = int(ev[0]) if ev else 0
            seal_ts = ev[1] if ev else None
            cl = float(close[0]) if close and close[0] else None
            ret = (cl / float(alert) - 1) if (cl and alert) else None
            c.execute(
                "UPDATE intraday_limit_watch SET sealed=?,first_seal_ts=?,close=?,"
                "ret_alert_to_close=? WHERE date=? AND code=?",
                (sealed, seal_ts, cl, ret, rday, code))
            n += 1
    return {"day": day, "settled": n}


def watch_report(days: int = 20) -> dict:
    """影子战绩：封板命中率、alert→收盘收益、按触发原因分桶。"""
    setup()
    with datasource._conn() as c:
        df = pd.read_sql_query(
            "SELECT * FROM intraday_limit_watch WHERE sealed IS NOT NULL ORDER BY date DESC",
            c)
    if df.empty:
        return {"days": 0, "note": "尚无已结算记录"}
    df = df[df["date"] >= (pd.Timestamp.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")]
    out = {
        "样本": len(df), "覆盖天数": df["date"].nunique(),
        "封板命中率": float(df["sealed"].mean()),
        "alert→收盘平均": float(df["ret_alert_to_close"].dropna().mean())
        if df["ret_alert_to_close"].notna().any() else None,
    }
    by_reason = []
    for reason, g in df.groupby(df["reason"].str.contains("接力")):
        by_reason.append({"接力" if reason else "板块联动": True,
                          "样本": len(g), "封板率": float(g["sealed"].mean()),
                          "alert→收盘": float(g["ret_alert_to_close"].dropna().mean())
                          if g["ret_alert_to_close"].notna().any() else None})
    out["分桶"] = by_reason
    return out
