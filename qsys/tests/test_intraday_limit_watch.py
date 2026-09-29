"""intraday_limit_watch 盘中涨停预警影子扫描测试：过滤口径/联动条件/结算/命中率。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datasource
import intraday_limit_watch as ilw

DAY = "2026-09-29"
YDAY = "2026-09-28"

# (code, chg_pct, quantity_ratio, amount, name, industry, prev_board)
FIXTURE = [
    ("SH600001", 4.5, 4.0, 5.0e7, "合格联动", "建材", 0),   # 板块有涨停 → 命中
    ("SH600002", 5.0, 3.5, 4.0e7, "合格接力", "机械", 2),   # 昨日2板 → 命中
    ("SH600004", 4.0, 1.5, 5.0e7, "量比不足", "建材", 0),   # QR<3 → 剔除
    ("SH600005", 4.0, 4.0, 1.0e7, "成交额不足", "建材", 0),  # 额<3000万 → 剔除
    ("SH600006", 4.0, 4.0, 5.0e7, "ST涨停", "建材", 0),      # ST → 剔除
    ("SH600007", 4.0, 4.0, 5.0e7, "孤立无援", "孤板", 0),    # 板块无涨停+非接力 → 剔除
    ("SH600008", 9.9, 5.0, 5.0e7, "板块涨停", "建材", 0),   # 已封板（超7%带上限剔除，但计入板块涨停计数）
]


def _fixture(tmpdir: str):
    db = Path(tmpdir) / "market.db"
    with patch.object(datasource, "MKT_DB", db):
        import limit_up_events as lue
        lue.setup()
        with datasource._conn() as c:
            for code, chg, qr, amt, name, ind, prev_board in FIXTURE:
                c.execute(
                    "INSERT INTO ifind_realtime(code,datetime,price,change_pct,quantity_ratio,"
                    "amount,turnover,speed) VALUES(?,?,?,?,?,?,?,?)",
                    (code, f"{DAY} 09:44:50", 10.0, chg, qr, amt, 1.0, 0.5))
                c.execute("INSERT OR IGNORE INTO stock_master(code,name) VALUES(?,?)",
                          (code, name))
            c.execute(
                "CREATE TABLE IF NOT EXISTS stock_industry"
                "(code TEXT, sector_label TEXT, sector_name TEXT, source TEXT, updated_at TEXT)")
            for code, chg, qr, amt, name, ind, prev_board in FIXTURE:
                c.execute("INSERT INTO stock_industry VALUES(?,?,?,?,?)",
                          (code, "sw", ind, "test", "2026-09-29"))
            # 昨日封板档案：SH600002 2连板
            c.execute(
                "INSERT INTO limit_up_events(date,code,name,touched,sealed_close,one_word,"
                "board_count,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (YDAY, "SH600002", "合格接力", 1, 1, 0, 2, "2026-09-28 19:05:00"))
            # EOD：SH600001 今日封板（10:20 首封），收盘 10.98（alert 10.0 → +9.8%）
            c.execute(
                "INSERT INTO limit_up_events(date,code,name,touched,sealed_close,one_word,"
                "first_seal_ts,board_count,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (DAY, "SH600001", "合格联动", 1, 1, 0, f"{DAY} 10:20:00", 1, "2026-09-29 19:05:00"))
            c.execute(
                "INSERT INTO market_daily(source,code,date,open,high,low,close,volume,amount)"
                " VALUES('ths_ifind','SH600001',?,9.8,10.98,9.7,10.98,1e6,1.1e7)", (DAY,))
            # SH600002 今日未封板，收盘 10.2
            c.execute(
                "INSERT INTO market_daily(source,code,date,open,high,low,close,volume,amount)"
                " VALUES('ths_ifind','SH600002',?,10.0,10.5,9.9,10.2,1e6,1.0e7)", (DAY,))
    return db


def test_scan_filters_and_linkage():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            r = ilw.scan(day=DAY, slot="0945")
            assert r["hits"] == 2, r  # 只有 SH600001(板块联动) 和 SH600002(接力)
            with datasource._conn() as c:
                rows = c.execute(
                    "SELECT code,sector_limit_count,prev_board,reason FROM intraday_limit_watch"
                    " WHERE date=? ORDER BY code", (DAY,)).fetchall()
            by_code = {r[0]: r for r in rows}
            assert by_code["SH600001"][1] == 1 and "板块" in by_code["SH600001"][3]
            assert by_code["SH600002"][2] == 2 and "接力" in by_code["SH600002"][3]
    print("PASS: test_scan_filters_and_linkage")


def test_scan_is_idempotent_per_slot():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            ilw.scan(day=DAY, slot="0945")
            ilw.scan(day=DAY, slot="0945")
            with datasource._conn() as c:
                n = c.execute("SELECT COUNT(*) FROM intraday_limit_watch WHERE date=?", (DAY,)).fetchone()[0]
            assert n == 2, n
            ilw.scan(day=DAY, slot="1015")
            with datasource._conn() as c:
                n = c.execute("SELECT COUNT(*) FROM intraday_limit_watch WHERE date=?", (DAY,)).fetchone()[0]
            assert n == 4, n  # 两个 slot 各 2 条
    print("PASS: test_scan_is_idempotent_per_slot")


def test_settle_and_report():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            ilw.scan(day=DAY, slot="0945")
            r = ilw.settle(DAY)
            assert r["settled"] == 2, r
            with datasource._conn() as c:
                rows = {x[0]: x for x in c.execute(
                    "SELECT code,sealed,first_seal_ts,ret_alert_to_close FROM intraday_limit_watch"
                    " WHERE date=? AND ts LIKE '%09:45%'", (DAY,))}
            assert rows["SH600001"][1] == 1 and rows["SH600001"][2].endswith("10:20:00")
            assert abs(rows["SH600001"][3] - 0.098) < 1e-6, rows["SH600001"]
            assert rows["SH600002"][1] == 0 and abs(rows["SH600002"][3] - 0.02) < 1e-6
            rep = ilw.watch_report(20)
            assert rep["样本"] == 2 and rep["封板命中率"] == 0.5, rep
            assert abs(rep["alert→收盘平均"] - (0.098 + 0.02) / 2) < 1e-6
    print("PASS: test_settle_and_report")


if __name__ == "__main__":
    test_scan_filters_and_linkage()
    test_scan_is_idempotent_per_slot()
    test_settle_and_report()
