"""event_eval 事件接力评估合同测试：候选过滤/开盘即板剔除/窗口成熟度/指标与样本门槛。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datasource
import event_eval

CODES = ["SH600001", "SH600002", "SH600003"]
PACK = {"name": "事件包", "pool_name": "测试池", "top_n": 2, "method": "等权合成",
        "status": "shadow", "risk_class": "event", "filters": [],
        "factors": [{"name": "f1", "weight": 1.0, "direction": 1}]}


def _fixture(tmpdir: str, n_days: int = 70, event_days=None):
    """构造 70 个交易日：三只票每日封板（非一字），收益模式固定且可预期。"""
    db = Path(tmpdir) / "market.db"
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-06-01", periods=n_days)]
    drift = {"SH600001": 0.008, "SH600002": 0.002, "SH600003": -0.004}
    base = {"SH600001": 10.0, "SH600002": 20.0, "SH600003": 30.0}
    with patch.object(datasource, "MKT_DB", db):
        import limit_up_events as lue
        lue.setup()
        with datasource._conn() as c:
            for code in CODES:
                close = base[code]
                for i, day in enumerate(days):
                    close = close * (1 + drift[code])
                    open_p = close / (1 + drift[code]) * 1.001
                    # 每第 7 天：SH600001 开盘直接涨停（买不进场景）
                    if code == "SH600001" and i % 7 == 3:
                        open_p = close / (1 + drift[code]) * 1.0999
                    high = max(open_p, close) * 1.01
                    c.execute(
                        "INSERT INTO market_daily(source,code,date,open,high,low,close,volume,amount)"
                        " VALUES('ths_ifind',?,?,?,?,?,?,?,?)",
                        (code, day, open_p, high, min(open_p, close) * 0.99, close, 10000, 1e6))
                    c.execute("INSERT OR IGNORE INTO ifind_calendar(exchange,date) VALUES('SSE',?)", (day,))
            ev_days = event_days if event_days is not None else days[6:64]
            for day in ev_days:
                for code in CODES:
                    c.execute(
                        "INSERT INTO limit_up_events(date,code,name,touched,sealed_close,one_word,"
                        "board_count,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (day, code, "测试", 1, 1, 0, 1, "2026-09-29 00:00:00"))
    return db, days


def _patch_common(db):
    scores_series = pd.Series({"SH600001": 3.0, "SH600002": 2.0, "SH600003": 1.0})
    return (patch.object(datasource, "MKT_DB", db),
            patch("factor_eval.get_factor_values",
                  side_effect=lambda *a, **k: pd.Series([1.0], index=[pd.Timestamp("2026-06-01")])
                  .rename_axis("datetime")),
            patch("selection_policy.rank_snapshot", return_value=(scores_series, 3)),
            patch("signals.get_panel_cached", return_value=None))


def test_event_eval_passes_with_selection_skill():
    with tempfile.TemporaryDirectory() as tmp:
        db, days = _fixture(tmp)
        p1, p2, p3, p4 = _patch_common(db)
        with p1, p2, p3, p4:
            r = event_eval.event_relay_eval("事件包", PACK, CODES, days[-1])
        assert r["ok"], r
        assert r["contract"] == "event_relay_v1"
        assert r["oos_windows"] >= 40 and r["trades"] >= 100, r
        # 组合（前2名）跑赢全部候选等权 → 超额为正
        assert r["avg_net_excess"] > 0, r["avg_net_excess"]
        assert r["expect_per_window"] > 0
        assert r["status"] == "shadow", r["status"]  # 通过但 shadow 不直升 active
    print("PASS: test_event_eval_passes_with_selection_skill")


def test_open_at_limit_excluded_from_trades():
    with tempfile.TemporaryDirectory() as tmp:
        db, days = _fixture(tmp)
        p1, p2, p3, p4 = _patch_common(db)
        with p1, p2, p3, p4:
            r = event_eval.event_relay_eval("事件包", PACK, CODES, days[-1])
        assert r["ok"], r
        # 每个窗口笔数 ≤ 2（top_n），且开盘即板的日期里 SH600001 被剔除
        wins = pd.DataFrame(r["event_windows"])
        assert (wins["笔数"] <= 2).all()
        # 存在因开盘即板导致候选数变少的窗口
        assert (wins["候选数"] < 3).any(), wins["候选数"].unique()
    print("PASS: test_open_at_limit_excluded_from_trades")


def test_sample_insufficient_is_not_degradation():
    with tempfile.TemporaryDirectory() as tmp:
        db, days = _fixture(tmp, event_days=["2026-06-15", "2026-06-16"])
        p1, p2, p3, p4 = _patch_common(db)
        with p1, p2, p3, p4:
            r = event_eval.event_relay_eval("事件包", PACK, CODES, days[-1])
        assert not r["ok"]
        assert r["assessment_status"] == "sample_insufficient", r
        assert "degraded" not in str(r.get("status", "")), r  # 样本不足不得给出退化结论
    print("PASS: test_sample_insufficient_is_not_degradation")


def test_empty_events_table_reports_pipeline_missing():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "market.db"
        with patch.object(datasource, "MKT_DB", db):
            with datasource._conn() as c:
                c.execute("INSERT OR IGNORE INTO ifind_calendar(exchange,date) VALUES('SSE','2026-06-01')")
            import limit_up_events as lue
            lue.setup()
            r = event_eval.event_relay_eval("事件包", PACK, CODES, "2026-06-30")
        assert not r["ok"] and r["assessment_status"] == "sample_insufficient"
        assert "涨停事件档案" in r["error"]
    print("PASS: test_empty_events_table_reports_pipeline_missing")


if __name__ == "__main__":
    test_event_eval_passes_with_selection_skill()
    test_open_at_limit_excluded_from_trades()
    test_sample_insufficient_is_not_degradation()
    test_empty_events_table_reports_pipeline_missing()
