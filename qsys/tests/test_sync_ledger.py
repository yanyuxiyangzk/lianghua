"""sync_ledger 游标账本测试：日线增量（批量+代码版式回转）/游标推进/热票名单/快照聚合分钟。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datasource
import sync_ledger as sl

DAY = "2026-09-29"


def _fixture(tmpdir: str):
    db = Path(tmpdir) / "market.db"
    with patch.object(datasource, "MKT_DB", db):
        with datasource._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS ifind_stocklist(code TEXT, name TEXT)")
            for code in ["SH600001", "SZ000001", "SH600002"]:
                c.execute("INSERT INTO ifind_stocklist(code, name) VALUES(?,?)",
                          (code, "测试" + code[-4:]))
            # 快照：SH600001 今日 3 条 15s 快照（聚合分钟用）
            for ts, price, vol, amt in [("09:31:10", 10.0, 100, 1000.0),
                                        ("09:31:40", 10.2, 300, 3060.0),
                                        ("09:32:05", 10.1, 500, 5050.0)]:
                c.execute("INSERT INTO ifind_realtime(code,datetime,price,volume,amount)"
                          " VALUES(?,?,?,?,?)", ("SH600001", f"{DAY} {ts}", price, vol, amt))
        sl.setup()
    return db


def _fake_history(codes, indicators, start, end, params):
    """模拟 THS_HQ 批量返回（thscode 为接口版式 600001.SH；日期按请求的 start）。"""
    rows = []
    for code in codes:
        if code == "SZ000001":
            continue  # 停牌无数据
        rows.append({"time": f"{start} 00:00:00", "thscode": f"{code[2:]}.{code[:2]}",
                     "open": 10.0, "high": 10.5, "low": 9.9, "close": 10.2,
                     "volume": 1000, "amount": 10200})
    return pd.DataFrame(rows), None, 0


def test_daily_incremental_batch_and_cursor():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db), \
             patch.object(datasource, "ths_history", side_effect=_fake_history), \
             patch("bulk_history_fetch.non_st_codes", return_value=["SH600001", "SZ000001", "SH600002"]):
            r = sl.daily_incremental(DAY, pause=0)
            assert r["written"] == 2 and r["failed"] == 0, r
            with datasource._conn() as c:
                cur = {x[0]: x for x in c.execute(
                    "SELECT code,last_synced_date,target_date,status FROM sync_cursor WHERE data_type='daily'")}
                n_daily = c.execute("SELECT COUNT(*) FROM market_daily WHERE date=?", (DAY,)).fetchone()[0]
            assert n_daily == 2
            assert cur["SH600001"][1] == DAY and cur["SH600001"][3] == "ok", cur
            assert cur["SZ000001"][1] is None and cur["SZ000001"][3] == "no_data", cur
            # 再跑一天：游标推进到新区间
            day2 = "2026-09-30"
            r2 = sl.daily_incremental(day2, pause=0)
            assert r2["written"] == 2, r2
            with datasource._conn() as c:
                cur2 = c.execute("SELECT last_synced_date FROM sync_cursor WHERE code='SH600001' AND data_type='daily'").fetchone()
            assert cur2[0] == day2, cur2  # 游标推进到新日
    print("PASS: test_daily_incremental_batch_and_cursor")


def test_snapshot_minutes_aggregation():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            df = sl.snapshot_minutes("SH600001", DAY)
            assert len(df) == 2, df  # 09:31 和 09:32 两根
            m31 = df[df["datetime"].dt.strftime("%H:%M") == "09:31"].iloc[0]
            assert m31["open"] == 10.0 and m31["close"] == 10.2 and m31["high"] == 10.2
            assert m31["volume"] == 200 and abs(m31["amount"] - 2060.0) < 1e-6  # 差分
            assert sl.snapshot_minutes("SZ399999", DAY).empty
    print("PASS: test_snapshot_minutes_aggregation")


def test_hot_minute_codes_union():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            import limit_up_events as lue
            lue.setup()
            with datasource._conn() as c:
                c.execute(
                    "INSERT INTO limit_up_events(date,code,name,touched,sealed_close,one_word,"
                    "board_count,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (DAY, "SH600002", "触板", 1, 0, 0, 0, "x"))
            codes = sl._hot_minute_codes(DAY)
            assert "SH600002" in codes  # 触板股入选
    print("PASS: test_hot_minute_codes_union")


if __name__ == "__main__":
    test_daily_incremental_batch_and_cursor()
    test_snapshot_minutes_aggregation()
    test_hot_minute_codes_union()
