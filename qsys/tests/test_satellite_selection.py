"""卫星轨策略包与交易日选择测试。"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scheduler
import pandas as pd


def test_satellite_pack_prefers_event_factor_pack():
    packs = {
        "涨停卫星_v1": {"status": "active", "updated": "2026-09-02 20:20",
                         "factors": [{"name": "ev_涨停_a"}, {"name": "ev_跳空_b"}]},
        "卫星轨_ICIR6因子_v1": {"status": "active", "updated": "2026-09-18 12:00",
                              "factors": [{"name": "le_跳空_a"}]},
    }
    assert scheduler._satellite_pack_name(packs) == "涨停卫星_v1"
    print("PASS: test_satellite_pack_prefers_event_factor_pack")


def test_degraded_satellite_still_selected_for_observation():
    packs = {"涨停卫星_v1": {"status": "degraded", "updated": "2026-09-22",
                              "factors": [{"name": "ev_涨停_a"}]}}
    assert scheduler._satellite_pack_name(packs) == "涨停卫星_v1"
    assert scheduler._satellite_pack_name(packs, execution_only=True) is None
    print("PASS: test_degraded_satellite_still_selected_for_observation")


def test_satellite_weekday_not_blocked_by_stale_calendar():
    # 数据库日历末端早于目标日时，工作日必须兜底放行。
    assert scheduler._satellite_trading_day(datetime(2026, 9, 22, 19, 10)) is True
    assert scheduler._satellite_trading_day(datetime(2026, 9, 20, 19, 10)) is False
    print("PASS: test_satellite_weekday_not_blocked_by_stale_calendar")


def test_satellite_observation_top5_shape():
    observed = pd.DataFrame({"code": [f"SH60000{i}" for i in range(8)],
                             "score": [0.8 - i * 0.05 for i in range(8)]})
    picks = observed.set_index("code")["score"].head(5)
    assert len(picks) == 5
    assert list(picks.index) == [f"SH60000{i}" for i in range(5)]
    print("PASS: test_satellite_observation_top5_shape")


if __name__ == "__main__":
    test_satellite_pack_prefers_event_factor_pack()
    test_degraded_satellite_still_selected_for_observation()
    test_satellite_weekday_not_blocked_by_stale_calendar()
    test_satellite_observation_top5_shape()
