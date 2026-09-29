"""卫星轨真实战绩口径：读主账户 positions 表（卫星来源），而非废弃的 satellite_* 台账。"""
import ast
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import experience as exp
import broker as bk

_ROWS = [
    # code, name, buy_date, buy_price, source, status, shares, buy_amount,
    # sell_date, sell_price, pnl_pct, hold_days
    ("SH600001", "正式持仓", "2026-09-20", 10.0, "satellite_scan", "open", 100, 1000.0,
     None, None, None, None),
    ("SH600002", "正式挂单", "2026-09-24", None, "satellite_scan", "pending", None, None,
     None, None, None, None),
    ("SH600003", "盈利平仓", "2026-09-01", 10.0, "satellite_scan", "closed", 100, 1000.0,
     "2026-09-05", 11.0, 0.0975, 4),
    ("SH600004", "亏损平仓", "2026-09-02", 20.0, "sched_satellite_scan", "closed", 100, 2000.0,
     "2026-09-08", 19.0, -0.0525, 6),
    ("SH600005", "对账合并", "2026-09-03", 5.0, "satellite_scan", "closed", 100, 500.0,
     "2026-09-09", None, None, None),
    ("SH600006", "主轨平仓", "2026-09-01", 10.0, "sched_pool_scan", "closed", 100, 1000.0,
     "2026-09-05", 11.0, 0.0975, 4),
    ("SH600007", "主轨持仓", "2026-09-20", 10.0, "sched_pool_scan", "open", 100, 1000.0,
     None, None, None, None),
]


def _fixture(tmpdir):
    db = Path(tmpdir) / "experience.db"
    with patch.object(exp, "DB_PATH", db):
        with exp._conn() as c:
            for r in _ROWS:
                c.execute(
                    "INSERT INTO positions(code,name,buy_date,buy_price,source,status,shares,"
                    "buy_amount,sell_date,sell_price,pnl_pct,hold_days,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*r, "2026-09-20 09:00:00"))
    return db


def test_track_positions_filters_satellite_sources():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(exp, "DB_PATH", db):
            opens = exp.satellite_track_positions(("open", "closing"))
            assert list(opens["code"]) == ["SH600001"], opens
            pendings = exp.satellite_track_positions(("pending",))
            assert list(pendings["code"]) == ["SH600002"], pendings
    print("PASS: test_track_positions_filters_satellite_sources")


def test_track_record_returns_closed_satellite_only():
    with tempfile.TemporaryDirectory() as tmp:
        db = _fixture(tmp)
        with patch.object(exp, "DB_PATH", db):
            closed = exp.satellite_track_record(200)
            codes = set(closed["code"])
            assert codes == {"SH600003", "SH600004", "SH600005"}, codes
            traded = closed[closed["pnl_pct"].notna()]
            assert set(traded["code"]) == {"SH600003", "SH600004"}
            realized = float((traded["pnl_pct"] * traded["buy_amount"]).sum())
            assert abs(realized - (0.0975 * 1000 - 0.0525 * 2000)) < 1e-6, realized
    print("PASS: test_track_record_returns_closed_satellite_only")


def test_track_record_section_renders_real_data():
    from streamlit.testing.v1 import AppTest
    source = Path("/app/views/p_satellite.py").read_text()
    module = ast.parse(source)
    nodes = []
    for n in module.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_render_track_record":
            nodes.append(ast.get_source_segment(source, n))
        elif isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == "_SOURCE_LABEL" for t in n.targets):
            nodes.append(ast.get_source_segment(source, n))
    assert len(nodes) == 2, nodes
    app_source = ("import streamlit as st\nimport pandas as pd\n"
                  "import experience as exp\nimport broker as bk\n" + "\n\n".join(nodes) +
                  "\n_render_track_record()\n")
    opens = pd.DataFrame([{"code": "SH600001", "name": "正式持仓", "buy_date": "2026-09-20",
                           "buy_price": 10.0, "shares": 100, "source": "satellite_scan"}])
    closed = pd.DataFrame([
        {"id": 1, "code": "SH600003", "name": "盈利平仓", "buy_date": "2026-09-01",
         "buy_price": 10.0, "sell_date": "2026-09-05", "sell_price": 11.0,
         "pnl_pct": 0.0975, "buy_amount": 1000.0, "hold_days": 4,
         "sell_reason": "止盈", "source": "satellite_scan"},
        {"id": 2, "code": "SH600005", "name": "对账合并", "buy_date": "2026-09-03",
         "buy_price": 5.0, "sell_date": "2026-09-09", "sell_price": None,
         "pnl_pct": None, "buy_amount": 500.0, "hold_days": None,
         "sell_reason": "重复记录合并", "source": "sched_satellite_scan"},
    ])
    with patch.object(exp, "satellite_track_positions",
                      side_effect=lambda statuses=("open", "pending", "closing"):
                          opens if "open" in statuses else pd.DataFrame()), \
         patch.object(exp, "satellite_track_record", return_value=closed), \
         patch.object(bk, "_latest_prices", return_value={"SH600001": (11.0, 10.5)}):
        app = AppTest.from_string(app_source).run()
    assert not app.exception
    metrics = {m.label: m.value for m in app.metric}
    assert metrics["已平仓"] == "1 笔", metrics
    assert metrics["胜率"] == "100%", metrics
    assert metrics["累计已实现盈亏"] == "+98 元", metrics
    captions = " ".join(c.value or "" for c in app.caption)
    assert "对账合并等非交易记录不计入统计" in captions
    print("PASS: test_track_record_section_renders_real_data")


if __name__ == "__main__":
    test_track_positions_filters_satellite_sources()
    test_track_record_returns_closed_satellite_only()
    test_track_record_section_renders_real_data()
