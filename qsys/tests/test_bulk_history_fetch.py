"""bulk_history_fetch 批量补齐脚本测试：非ST过滤/待办检测/断点续跑/-9会话退避。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datasource
import bulk_history_fetch as bf


def _fixture(tmpdir: str):
    db = Path(tmpdir) / "market.db"
    with patch.object(datasource, "MKT_DB", db):
        with datasource._conn() as c:
            for code, name in [("SH600001", "甲"), ("SH600002", "乙ST"), ("SZ000001", "丙"),
                               ("SZ000002", "丁")]:
                c.execute("INSERT INTO ifind_stocklist(code, name) VALUES(?,?)", (code, name))
            for d in ["2026-09-25", "2026-09-28"]:
                c.execute("INSERT OR IGNORE INTO ifind_calendar(exchange,date) VALUES('SSE',?)", (d,))
            # 日线：SH600001 齐，SZ000001 缺最新，SZ000002 完全没有
            for d in ["2026-09-25", "2026-09-28"]:
                c.execute("INSERT INTO market_daily(source,code,date,open,high,low,close,"
                          "volume,amount) VALUES('ths_ifind','SH600001',?,1,1,1,1,1,1)", (d,))
            c.execute("INSERT INTO market_daily(source,code,date,open,high,low,close,"
                      "volume,amount) VALUES('ths_ifind','SZ000001','2026-09-25',1,1,1,1,1,1)")
            # 分钟线：SH600001 两天各 240 条（完整）
            for d in ["2026-09-25", "2026-09-28"]:
                for i in range(240):
                    c.execute("INSERT INTO ifind_minute(code,datetime,open,high,low,close,"
                              "volume,amount) VALUES('SH600001',?,1,1,1,1,1,1)",
                              (f"{d} 09:{31+i:02d}:00" if i < 120 else f"{d} 13:{i-120:02d}:00",))
    return db


def test_non_st_filter():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            codes = bf.non_st_codes()
            assert "SH600002" not in codes, codes
            assert set(codes) == {"SH600001", "SZ000001", "SZ000002"}
    print("PASS: test_non_st_filter")


def test_pending_daily_detection():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            todo = bf.pending_daily(bf.non_st_codes(), "2026-09-25", "2026-09-28")
            assert "SH600001" not in todo, todo          # 齐
            assert "SZ000001" in todo and "SZ000002" in todo, todo
    print("PASS: test_pending_daily_detection")


def test_pending_minute_detection():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(datasource, "MKT_DB", db):
            todo = bf.pending_minute(bf.non_st_codes(), "2026-09-25", "2026-09-28")
            assert "SH600001" not in todo, todo          # 分钟已完整
            assert "SZ000001" in todo, todo              # 无分钟
    print("PASS: test_pending_minute_detection")


def test_run_resume_and_session_backoff():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        calls = []
        with patch.object(datasource, "MKT_DB", db), \
             patch.object(bf, "non_st_codes", return_value=["SH600001", "SZ000001", "SZ000002"]), \
             patch.object(bf, "pending_minute", return_value=["SH600001", "SZ000001", "SZ000002"]), \
             patch.object(bf.time, "sleep", lambda s: None):
            def fetch_one(code):
                calls.append(code)
                if code == "SZ000001" and calls.count("SZ000001") == 1:
                    raise RuntimeError("iFinD -9 会话超限")
                return "ok"
            r = bf._run("minute", ["SH600001", "SZ000001", "SZ000002"], "2026-09-25",
                        "2026-09-28", 0, fetch_one, "测试")
            assert r == {"ok": 3, "failed": 0, "aborted": False}, r
            assert calls.count("SZ000001") == 2  # -9 退避后重试成功
            # 断点续跑：全部 done → 不再调用
            calls.clear()
            r2 = bf._run("minute", ["SH600001", "SZ000001", "SZ000002"], "2026-09-25",
                         "2026-09-28", 0, fetch_one, "测试")
            assert r2["ok"] == 0 and not calls, r2
    print("PASS: test_run_resume_and_session_backoff")


if __name__ == "__main__":
    test_non_st_filter()
    test_pending_daily_detection()
    test_pending_minute_detection()
    test_run_resume_and_session_backoff()
