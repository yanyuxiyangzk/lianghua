"""单股票历史数据地基测试：稳定主键、交易日覆盖和分钟完整率。"""
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import datasource


def _use_temp_db():
    temp = tempfile.TemporaryDirectory()
    old = datasource.MKT_DB
    datasource.MKT_DB = Path(temp.name) / "market.db"
    return temp, old


def test_stock_identity_is_stable():
    temp, old = _use_temp_db()
    try:
        first = datasource.get_or_create_stock_id("SZ001216")
        second = datasource.get_or_create_stock_id("SZ001216")
        assert first == second
        with datasource._conn() as c:
            assert c.execute(
                "SELECT COUNT(*) FROM stock_master WHERE code='SZ001216'").fetchone()[0] == 1
    finally:
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_stock_identity_is_stable")


def test_expected_trade_days_uses_union():
    temp, old = _use_temp_db()
    try:
        with datasource._conn() as c:
            c.executemany("INSERT INTO ifind_calendar(exchange,date) VALUES('SSE',?)",
                          [("2026-09-18",), ("2026-09-21",)])
            c.executemany(
                "INSERT INTO market_daily(source,code,date,close) VALUES('ths_ifind','SH000001',?,3000)",
                [("2026-09-17",), ("2026-09-18",)])
        assert datasource.expected_trade_days("2026-09-17", "2026-09-21") == [
            "2026-09-17", "2026-09-18", "2026-09-21"]
    finally:
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_expected_trade_days_uses_union")


def test_minute_completeness_marks_partial_day():
    temp, old = _use_temp_db()
    try:
        stock_id = datasource.get_or_create_stock_id("SZ001216")
        with datasource._conn() as c:
            c.executemany("INSERT INTO ifind_calendar(exchange,date) VALUES('SSE',?)",
                          [("2026-09-18",), ("2026-09-21",)])
            full = [("SZ001216", f"2026-09-18 09:{i // 60:02d}:{i % 60:02d}", 10, stock_id)
                    for i in range(200)]
            partial = [("SZ001216", f"2026-09-21 09:30:{i:02d}", 10, stock_id)
                       for i in range(20)]
            c.executemany(
                "INSERT INTO ifind_minute(code,datetime,close,stock_id) VALUES(?,?,?,?)",
                full + partial)
        result = datasource.minute_completeness(
            "SZ001216", "2026-09-18", "2026-09-21", min_rows_per_day=200)
        assert result["expected_days"] == 2
        assert result["complete_days"] == 1
        assert result["missing_days"] == ["2026-09-21"]
        assert result["completeness"] == 0.5
    finally:
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_minute_completeness_marks_partial_day")


def _insert_minute_day(connection, stock_id, day, rows=241):
    values = []
    for i in range(rows):
        if i < 121:
            minutes = 30 + i
            hour, minute = 9 + minutes // 60, minutes % 60
        else:
            minutes = i - 121
            hour, minute = 13 + minutes // 60, minutes % 60
        close = 10.0 + i * 0.001
        volume = 1000 + i
        values.append(("SZ001216", f"{day} {hour:02d}:{minute:02d}:00",
                       close, close + 0.01, close - 0.01, close, volume,
                       close * volume, stock_id))
    connection.executemany(
        "INSERT INTO ifind_minute(code,datetime,open,high,low,close,volume,amount,stock_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)", values)


def test_backfill_runs_in_batches_and_resumes():
    temp, old = _use_temp_db()
    original_fetch = datasource.fetch_minute_to_db
    try:
        with datasource._conn() as c:
            c.executemany("INSERT INTO ifind_calendar(exchange,date) VALUES('SSE',?)",
                          [("2026-09-17",), ("2026-09-18",), ("2026-09-21",)])

        calls = []

        def fake_fetch(code, day, frequency):
            calls.append(day)
            stock_id = datasource.get_or_create_stock_id(code)
            with datasource._conn() as c:
                _insert_minute_day(c, stock_id, day, 200)
            return 200

        datasource.fetch_minute_to_db = fake_fetch
        first = datasource.backfill_missing_minutes(
            "SZ001216", "2026-09-17", "2026-09-21", batch_days=2, pause=0)
        assert first["attempted_days"] == 2
        assert first["remaining_days"] == 1
        assert calls == ["2026-09-17", "2026-09-18"]

        second = datasource.backfill_missing_minutes(
            "SZ001216", "2026-09-17", "2026-09-21", batch_days=2, pause=0)
        assert second["attempted_days"] == 1
        assert second["remaining_days"] == 0
        assert second["status"] == "complete"
        assert calls[-1] == "2026-09-21"
        status = datasource.minute_backfill_status(
            "SZ001216", "2026-09-17", "2026-09-21")
        assert status["attempted_days"] == 3
        assert status["repaired_days"] == 3
    finally:
        datasource.fetch_minute_to_db = original_fetch
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_backfill_runs_in_batches_and_resumes")


def test_intraday_features_are_bounded_and_idempotent():
    temp, old = _use_temp_db()
    try:
        stock_id = datasource.get_or_create_stock_id("SZ001216")
        with datasource._conn() as c:
            _insert_minute_day(c, stock_id, "2026-09-18", 241)
            _insert_minute_day(c, stock_id, "2026-09-21", 50)
        first = datasource.compute_intraday_features(
            "SZ001216", "2026-09-18", "2026-09-21", min_rows_per_day=200)
        second = datasource.compute_intraday_features(
            "SZ001216", "2026-09-18", "2026-09-21", min_rows_per_day=200)
        assert first["computed_days"] == second["computed_days"] == 1
        assert first["skipped_days"] == 1
        features = datasource.get_intraday_features("SZ001216")
        assert len(features) == 1
        row = features.iloc[0]
        assert row["minute_count"] == 241
        assert row["realized_vol"] >= 0
        assert row["max_intraday_drawdown"] <= 0
        assert 0 <= row["morning_volume_share"] <= 1
        assert 0 <= row["tail_volume_share"] <= 1
        assert 0 <= row["up_minute_ratio"] <= 1
        assert row["vwap"] > 0
        numeric = features.select_dtypes(include=[np.number])
        assert np.isfinite(numeric.drop(columns=["price_volume_corr"], errors="ignore")).all().all()
    finally:
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_intraday_features_are_bounded_and_idempotent")


def test_orderbook_sync_rejects_wrong_day_and_writes_valid_rows():
    temp, old = _use_temp_db()
    original_login, original_call = datasource._ths_login, datasource.ths_call
    try:
        datasource._ths_login = lambda: True
        datasource.ths_call = lambda *args, **kwargs: (
            pd.DataFrame({"time": ["2026-09-22 09:30:01"], "latest": [10.0],
                          "bid1": [9.99], "ask1": [10.01], "volume": [100],
                          "amount": [1000]}), None, 0)
        try:
            datasource.fetch_orderbook_day_to_db("SZ001216", "2026-09-21")
            raise AssertionError("wrong-day response should be rejected")
        except RuntimeError as exc:
            assert "返回日期与请求不符" in str(exc)

        datasource.ths_call = lambda *args, **kwargs: (
            pd.DataFrame({"time": ["2026-09-21 09:30:01", "2026-09-21 09:31:01"],
                          "latest": [10.0, 10.01], "bid1": [9.99, 10.0],
                          "ask1": [10.01, 10.02], "bid2": [9.98, 9.99],
                          "ask2": [10.02, 10.03], "bidSize1": [100, 200],
                          "askSize1": [300, 400], "volume": [100, 200],
                          "amount": [1000, 2002]}), None, 0)
        result = datasource.fetch_orderbook_day_to_db("SZ001216", "2026-09-21")
        assert result["written"] == result["new_rows"] == 2
        with datasource._conn() as c:
            rows = c.execute(
                "SELECT datetime,bid1,ask1,bid2,ask2,bid_size1,ask_size1,"
                "typeof(bid_size1) FROM ifind_realtime WHERE code=? ORDER BY datetime",
                ("SZ001216",)).fetchall()
        assert len(rows) == 2
        assert tuple(rows[0]) == ("2026-09-21 09:30:01", 9.99, 10.01, 9.98, 10.02,
                                  100.0, 300.0, "real")
    finally:
        datasource._ths_login, datasource.ths_call = original_login, original_call
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_orderbook_sync_rejects_wrong_day_and_writes_valid_rows")


def test_tencent_tick_sync_migrates_amount_and_coerces_types():
    temp, old = _use_temp_db()
    import akshare as ak
    original_tick = ak.stock_zh_a_tick_tx_js
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    try:
        # 预建老版 tick_data（无 amount 列），验证 _conn() 幂等补列迁移
        with sqlite3.connect(str(datasource.MKT_DB)) as c:
            c.execute("""CREATE TABLE tick_data(
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL,
                datetime TEXT NOT NULL, price REAL, volume INTEGER,
                buyorsell INTEGER, source TEXT DEFAULT 'tdx',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(code, datetime, price, volume))""")
        ak.stock_zh_a_tick_tx_js = lambda symbol: pd.DataFrame({
            "成交时间": ["09:30:00", "09:30:03"], "成交价格": [10.0, 10.01],
            "价格变动": [0.0, 0.01], "成交量": [100, 200],
            "成交金额": [100000, 200200], "性质": ["买盘", "卖盘"]})
        try:
            datasource.fetch_ticks_tx_to_db("SZ001216", "2020-01-02")
            raise AssertionError("non-today should be rejected")
        except RuntimeError as exc:
            assert "只提供当日" in str(exc)

        result = datasource.fetch_ticks_tx_to_db("SZ001216", today)
        assert result["written"] == 2 and result["new_rows"] == 2
        with datasource._conn() as c:
            rows = c.execute(
                "SELECT datetime,price,volume,amount,typeof(amount),buyorsell,source "
                "FROM tick_data WHERE code=? ORDER BY datetime", ("SZ001216",)).fetchall()
        # 纯整数的成交金额经 numpy.int64 必须强转原生 float，否则落库成 BLOB
        assert rows[0] == (f"{today} 09:30:00", 10.0, 100, 100000.0, "real", 0, "tencent_tx")
        assert rows[1][5] == 1  # 卖盘
        again = datasource.fetch_ticks_tx_to_db("SZ001216", today)
        assert again["new_rows"] == 0  # 重复执行幂等
    finally:
        ak.stock_zh_a_tick_tx_js = original_tick
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_tencent_tick_sync_migrates_amount_and_coerces_types")


def test_background_minute_sync_is_serial_and_skips_complete_days():
    temp, old = _use_temp_db()
    original_fetch = datasource.fetch_minute_to_db
    try:
        stock_id = datasource.get_or_create_stock_id("SZ001216")
        with datasource._conn() as c:
            _insert_minute_day(c, stock_id, "2026-09-18", 241)
        calls = []

        def fake_fetch(code, day, interval):
            calls.append(day)
            with datasource._conn() as c:
                _insert_minute_day(c, stock_id, day, 241)
            return 241

        datasource.fetch_minute_to_db = fake_fetch
        result = datasource.start_minute_sync_task(
            "SZ001216", ["2026-09-18", "2026-09-21", "2026-09-22"],
            pause_seconds=0)
        assert result["started"] is True
        for _ in range(100):
            status = datasource.minute_sync_task_status("SZ001216")
            if status.get("status") != "running":
                break
            time.sleep(.01)
        assert status["status"] == "completed"
        assert status["skipped_days"] == 1
        assert status["synced_days"] == 2
        assert status["failed_days"] == 0
        assert calls == ["2026-09-21", "2026-09-22"]
    finally:
        datasource.fetch_minute_to_db = original_fetch
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_background_minute_sync_is_serial_and_skips_complete_days")


def test_single_request_minute_period_writes_multiple_days():
    temp, old = _use_temp_db()
    original_highfreq = datasource.ths_highfreq
    try:
        frames = []
        for day in ("2026-09-21", "2026-09-22"):
            for i in range(241):
                hour = 9 + (30 + i) // 60 if i < 121 else 13 + (i - 121) // 60
                minute = (30 + i) % 60 if i < 121 else (i - 121) % 60
                frames.append({"time": f"{day} {hour:02d}:{minute:02d}:00",
                               "open": 10, "high": 10.1, "low": 9.9,
                               "close": 10, "volume": 100, "amount": 1000})
        datasource.ths_highfreq = lambda *args, **kwargs: (pd.DataFrame(frames), None, 0)
        result = datasource.fetch_minute_period_to_db(
            "SZ001216", "2026-09-21", "2026-09-22")
        assert result["written"] == 482
        assert result["days"] == result["complete_days"] == 2
        with datasource._conn() as c:
            assert c.execute("SELECT COUNT(*) FROM ifind_minute").fetchone()[0] == 482
    finally:
        datasource.ths_highfreq = original_highfreq
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_single_request_minute_period_writes_multiple_days")


def test_minute_period_chunks_long_range_under_4304_cap():
    # THS_HF 单次上限 200 万数据点（-4304）：初始按 4 年分段，超限自动对半
    chunks = datasource._hf_splits("1990-12-19", "2026-09-22",
                                   datasource._HF_INIT_SPAN_DAYS)
    assert len(chunks) > 1
    assert chunks[0][0] == "1990-12-19" and chunks[-1][1] == "2026-09-22"
    for (a_start, a_end), (b_start, _b_end) in zip(chunks, chunks[1:]):
        assert pd.Timestamp(a_end) + pd.Timedelta(days=1) == pd.Timestamp(b_start)

    temp, old = _use_temp_db()
    original_highfreq = datasource.ths_highfreq
    try:
        calls = []

        def fake_highfreq(code, indicators, start, end, interval):
            calls.append((start, end))
            # 模拟服务端：跨度 >1000 天即超量拒绝，触发对半拆分
            if (pd.Timestamp(end) - pd.Timestamp(start)).days > 1000:
                return None, None, -4304
            day = start[:10]
            return pd.DataFrame({
                "time": [f"{day} 09:30:00", f"{day} 09:31:00"],
                "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
                "close": [1.0, 1.0], "volume": [1.0, 1.0],
                "amount": [1.0, 1.0]}), None, 0

        datasource.ths_highfreq = fake_highfreq
        result = datasource.fetch_minute_period_to_db("SH000001", "1990-12-19", "2026-09-22")
        ok_calls = [c for c in calls
                    if (pd.Timestamp(c[1]) - pd.Timestamp(c[0])).days <= 1000]
        assert any((pd.Timestamp(c[1]) - pd.Timestamp(c[0])).days > 1000 for c in calls)
        assert result["written"] == 2 * len(ok_calls)
        assert result["days"] == len(ok_calls)

        # 其他错误码原样抛出并带分段区间，便于定位
        datasource.ths_highfreq = lambda *args, **kwargs: (None, None, -999)
        try:
            datasource.fetch_minute_period_to_db("SH000001", "1990-12-19", "2026-09-22")
            raise AssertionError("-999 should propagate")
        except RuntimeError as exc:
            assert "-999" in str(exc) and "分段" in str(exc)
    finally:
        datasource.ths_highfreq = original_highfreq
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_minute_period_chunks_long_range_under_4304_cap")


if __name__ == "__main__":
    tests = [test_stock_identity_is_stable, test_expected_trade_days_uses_union,
             test_minute_completeness_marks_partial_day,
             test_backfill_runs_in_batches_and_resumes,
             test_intraday_features_are_bounded_and_idempotent,
             test_orderbook_sync_rejects_wrong_day_and_writes_valid_rows,
             test_tencent_tick_sync_migrates_amount_and_coerces_types,
             test_background_minute_sync_is_serial_and_skips_complete_days,
             test_single_request_minute_period_writes_multiple_days,
             test_minute_period_chunks_long_range_under_4304_cap]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:
            failed += 1
            print(f"FAIL: {test.__name__}: {exc}")
    print(f"结果: {len(tests) - failed} 通过, {failed} 失败, 共 {len(tests)} 个")
    raise SystemExit(1 if failed else 0)
