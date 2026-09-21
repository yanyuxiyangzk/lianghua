"""单股票历史数据地基测试：稳定主键、交易日覆盖和分钟完整率。"""
import sqlite3
import sys
import tempfile
from pathlib import Path

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


if __name__ == "__main__":
    tests = [test_stock_identity_is_stable, test_expected_trade_days_uses_union,
             test_minute_completeness_marks_partial_day]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:
            failed += 1
            print(f"FAIL: {test.__name__}: {exc}")
    print(f"结果: {len(tests) - failed} 通过, {failed} 失败, 共 {len(tests)} 个")
    raise SystemExit(1 if failed else 0)
