"""limit_up_events 涨停事件与情绪加工测试：日线判定/连板/分钟富化/封单提取/情绪汇总。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datasource
import limit_up_events as lue

D0, D1, D2, D3 = "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"  # 连续交易日（跨周末）

# code: {day: (open, high, low, close)}
DAILY = {
    "SH600001": {  # 主板：D2 首板，D3 二连板
        D0: (9.7, 9.9, 9.6, 9.8),
        D1: (9.9, 10.1, 9.8, 10.0),
        D2: (10.2, 11.0, 10.1, 11.0),
        D3: (11.2, 12.1, 11.1, 12.1),
    },
    "SH600002": {  # D3 触板未封（炸板）
        D0: (19.6, 19.9, 19.5, 19.7),
        D1: (19.8, 20.2, 19.6, 20.0),
        D2: (20.1, 20.3, 19.9, 20.0),
        D3: (20.3, 22.05, 20.2, 21.0),
    },
    "SH600003": {  # D3 一字板
        D0: (29.3, 29.7, 29.2, 29.5),
        D1: (29.5, 30.2, 29.4, 30.0),
        D2: (30.1, 30.4, 29.8, 30.0),
        D3: (33.0, 33.0, 33.0, 33.0),
    },
    "SZ300001": {  # 创业板 20%：D3 恰触 20% 未封
        D0: (9.6, 9.9, 9.5, 9.7),
        D1: (9.8, 10.1, 9.7, 10.0),
        D2: (10.0, 10.2, 9.9, 10.0),
        D3: (10.5, 12.0, 10.4, 11.5),
    },
    "SH600004": {  # 从未触板的对照票
        D0: (4.9, 5.0, 4.8, 4.9),
        D1: (5.0, 5.1, 4.9, 5.0),
        D2: (5.0, 5.2, 4.9, 5.0),
        D3: (5.0, 5.3, 4.9, 5.1),
    },
    "SH600006": {  # D1 封板、D2 断板、D3 再封：D3 必须重新计首板而非续板
        D0: (9.8, 10.0, 9.7, 10.0),
        D1: (9.9, 11.0, 9.8, 11.0),
        D2: (11.1, 11.3, 10.7, 10.9),
        D3: (11.5, 12.1, 11.4, 12.1),
    },
}

# D3 分钟线：SH600001 10:00 首封、13:05 开板、13:30 回封
MINUTE_D3 = {
    "SH600001": [("09:31", 11.5), ("10:00", 12.1), ("13:05", 12.0), ("13:30", 12.1)],
}

# D3 快照：SH600001 两条轮询行（仅 float_mv）+ 两条封板快照（bid1=涨停价, bid_size1 手）
REALTIME_D3 = [
    ("SH600001", "09:35:00", None, 5.0e9, None, None),
    ("SH600001", "14:00:00", None, 5.0e9, 12.1, 5000.0),
    ("SH600001", "15:00:00", None, 5.0e9, 12.1, 5200.0),
    # SH600002 THS_SS 盘口行（无 limit_up/float_mv），bid1 未达推算涨停价
    ("SH600002", "10:00:00", None, None, 21.5, 300.0),
]


def _fixture(tmpdir: str) -> Path:
    db = Path(tmpdir) / "market.db"
    with patch.object(datasource, "MKT_DB", db):
        with datasource._conn() as c:
            for code, days in DAILY.items():
                for day, (o, h, l, cl) in days.items():
                    c.execute(
                        "INSERT INTO market_daily(source,code,date,open,high,low,close,volume,amount)"
                        " VALUES('ths_ifind',?,?,?,?,?,?,?,?)",
                        (code, day, o, h, l, cl, 10000, 100000))
                c.execute("INSERT OR IGNORE INTO stock_master(code,name) VALUES(?,?)",
                          (code, "测试" + code[-4:]))
            for day in (D0, D1, D2, D3):
                c.execute("INSERT OR IGNORE INTO ifind_calendar(exchange,date) VALUES('SSE',?)", (day,))
            for code, rows in MINUTE_D3.items():
                for hm, cl in rows:
                    c.execute(
                        "INSERT INTO ifind_minute(code,datetime,open,high,low,close,volume,amount)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (code, f"{D3} {hm}:00", cl, cl, cl, cl, 100, 10000))
            for code, hms, limit_up, mv, bid1, sz in REALTIME_D3:
                c.execute(
                    "INSERT INTO ifind_realtime(code,datetime,price,limit_up,float_mv,bid1,bid_size1)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (code, f"{D3} {hms}", bid1 or 12.0, limit_up, mv, bid1, sz))
            c.execute(
                "CREATE TABLE IF NOT EXISTS stock_industry"
                "(code TEXT, sector_label TEXT, sector_name TEXT, source TEXT, updated_at TEXT)")
            c.execute(
                "INSERT INTO stock_industry VALUES('SH600001','sw','建筑材料','test','2026-09-28')")
        lue.setup()
    return db


def test_build_events_touch_seal_board_chain():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            lue.build_events(D1)  # SH600006 首板
            r2 = lue.build_events(D2)  # SH600001 首板
            assert r2["sealed"] == 1, r2
            r3 = lue.build_events(D3)
            assert r3["events"] == 5 and r3["sealed"] == 3, r3
            with datasource._conn() as c:
                rows = {r[1]: r for r in c.execute(
                    "SELECT date,code,touched,sealed_close,one_word,first_seal_ts,open_count,"
                    "seal_amount_avg,seal_amount_close,board_count,limit_price,float_mv,industry"
                    " FROM limit_up_events WHERE date=?", (D3,))}
            e1 = rows["SH600001"]
            assert e1[3] == 1 and e1[9] == 2, e1            # 封板且二连板（跨周末不断链）
            assert e1[5].endswith("10:00:00") and e1[6] == 1, e1  # 首封时间/开板次数
            assert abs(e1[7] - 12.1 * 5100 * 100) < 1, e1   # 平均封单金额（元）
            assert abs(e1[8] - 12.1 * 5200 * 100) < 1, e1   # 收盘封单取最后一条封板快照
            assert e1[11] == 5.0e9 and e1[12] == "建筑材料", e1
            e2 = rows["SH600002"]
            assert e2[2] == 1 and e2[3] == 0 and e2[9] == 0, e2   # 炸板不计板数
            e3 = rows["SH600003"]
            assert e3[4] == 1 and e3[9] == 1, e3            # 一字板记首板
            e4 = rows["SZ300001"]
            assert e4[2] == 1 and e4[3] == 0, e4            # 创业板 20% 阈值
            assert "SH600004" not in rows                   # 未触板不入表
            e6 = rows["SH600006"]
            assert e6[9] == 1, e6  # D1 封板但 D2 断板：D3 重新计首板（非相邻交易日不续板）
    print("PASS: test_build_events_touch_seal_board_chain")


def test_build_sentiment_metrics():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            lue.build_events(D1)
            lue.build_events(D2)
            lue.build_events(D3)
            s = lue.build_sentiment(D3)
            assert s["封板"] == 3 and s["触板"] == 5, s
            assert s["炸板率"] == 0.4, s
            assert s["最高板"] == 2 and s["连板家数"] == 1, s
            # 打板指数：D2 封板股仅 SH600001，D3 收盘 12.1/11.0-1 = +10%
            assert abs(s["打板指数"] - 0.1) < 1e-6, s
            # 可成交口径：SH600001 D3 开盘 11.2 买（非一字、开盘未封板）→ 收盘 12.1，扣千1滑点
            assert abs(s["可成交接力"] - round(12.1 / 11.2 - 1 - 0.001, 4)) < 1e-9, s
    print("PASS: test_build_sentiment_metrics")


def test_backfill_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            r = lue.backfill(D1, D3)
            assert r["days"] == 3, r
            with datasource._conn() as c:
                n1 = c.execute("SELECT COUNT(*) FROM limit_up_events").fetchone()[0]
                s1 = c.execute("SELECT COUNT(*) FROM limit_up_sentiment").fetchone()[0]
            lue.backfill(D1, D3)
            with datasource._conn() as c:
                n2 = c.execute("SELECT COUNT(*) FROM limit_up_events").fetchone()[0]
                s2 = c.execute("SELECT COUNT(*) FROM limit_up_sentiment").fetchone()[0]
            assert (n1, s1) == (n2, s2), (n1, s1, n2, s2)
    print("PASS: test_backfill_is_idempotent")


def test_prepare_orderbook_skips_existing_coverage():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        calls = []
        with patch.object(datasource, "MKT_DB", db), \
             patch.object(datasource, "fetch_orderbook_day_to_db",
                          side_effect=lambda code, day: calls.append(code) or {"written": 10}):
            r = lue.prepare_orderbook(D3, codes=["SH600001", "SH600003"])
            assert calls == ["SH600003"], calls  # SH600001 已有盘口数据，不调接口
            assert r["fetched"] == 1 and r["skipped"] == 1, r
    print("PASS: test_prepare_orderbook_skips_existing_coverage")


if __name__ == "__main__":
    test_build_events_touch_seal_board_chain()
    test_build_sentiment_metrics()
    test_backfill_is_idempotent()
    test_prepare_orderbook_skips_existing_coverage()
