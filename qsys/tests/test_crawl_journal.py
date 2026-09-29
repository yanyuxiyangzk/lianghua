"""crawl_journal 打点测试：record/timed 上下文/失败记录/异常不拖垮主流程。"""
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crawl_journal as cj


def _fixture(tmpdir: str):
    db = Path(tmpdir) / "market.db"
    import datasource
    with patch.object(datasource, "MKT_DB", db):
        with datasource._conn() as c:
            c.execute("SELECT 1")  # 建库
    return db


def _read(db):
    import datasource
    with patch.object(datasource, "MKT_DB", db):
        with datasource._conn() as c:
            return c.execute(
                "SELECT source,action,target,rows,status,detail FROM crawl_events").fetchall()


def test_record_and_timed_success():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        import datasource
        with patch.object(datasource, "MKT_DB", db):
            cj.setup()
            cj.record("test", "daily_fetch", "SH600519", rows=250, duration_sec=1.2)
            with cj.timed("test", "minute_fetch", "SZ000001") as t:
                t.rows = 58081
        rows = _read(db)
        assert len(rows) == 2, rows
        assert rows[0][:5] == ("test", "daily_fetch", "SH600519", 250, "ok")
        assert rows[1][3] == 58081 and rows[1][4] == "ok"
    print("PASS: test_record_and_timed_success")


def test_timed_marks_failure_without_swallowing():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        import datasource
        with patch.object(datasource, "MKT_DB", db):
            cj.setup()
            try:
                with cj.timed("test", "orderbook_fetch", "SH600001"):
                    raise RuntimeError("同花顺 -4302")
            except RuntimeError:
                pass
            else:
                raise AssertionError("异常应继续抛出")
        rows = _read(db)
        assert rows[0][4] == "failed" and "-4302" in rows[0][5], rows
    print("PASS: test_timed_marks_failure_without_swallowing")


def test_record_never_breaks_caller():
    # 数据库不可用时 record 静默
    import datasource
    with patch.object(datasource, "MKT_DB", Path("/nonexistent/x/y.db")):
        cj.record("test", "x")  # 不应抛异常
    print("PASS: test_record_never_breaks_caller")


if __name__ == "__main__":
    test_record_and_timed_success()
    test_timed_marks_failure_without_swallowing()
    test_record_never_breaks_caller()
