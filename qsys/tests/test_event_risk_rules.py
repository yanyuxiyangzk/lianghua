"""卫星轨机械风控测试（2026-09-29 改版）：移动止盈/破均价/尾盘不封板/到期不展期/止损距离口径。
开仓闸经一年回放校准后只保留"同板块≤1"（唯一全面正向）；候选数/盈亏比闸被数据否决不设。
"""
import sys
import tempfile
from datetime import datetime as _dt
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datasource
import experience as exp

TODAY = "2026-09-29"
D0, D1, D2, D3 = "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-28"
CAL = [D0, D1, D2, D3, TODAY]


class FakeDateTime(_dt):
    """固定时钟：position_close_check 的 now_hm 依赖它。"""

    fixed = "2026-09-29 10:00:00"

    @classmethod
    def now(cls, tz=None):
        return cls.strptime(cls.fixed, "%Y-%m-%d %H:%M:%S")


def _fixture(tmpdir: str, positions: list[dict], realtime_rows=None, event_rows=None):
    """构造 experience.db（positions）与 market.db（VWAP 快照 + limit_up_events）。"""
    edb = Path(tmpdir) / "experience.db"
    mdb = Path(tmpdir) / "market.db"
    with patch.object(exp, "DB_PATH", edb), patch.object(datasource, "MKT_DB", mdb):
        with exp._conn() as c:
            for p in positions:
                c.execute(
                    "INSERT INTO positions(code,name,buy_date,buy_price,source,status,shares,"
                    "buy_amount,max_close,pack_name,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (p["code"], "测试", p["buy_date"], p["buy_price"], "satellite_scan", "open",
                     p.get("shares", 100), p["buy_price"] * p.get("shares", 100),
                     p.get("max_close"), p.get("pack_name", "涨停卫星_v1"), "2026-09-28 09:00:00"))
        import limit_up_events as lue
        lue.setup()
        with datasource._conn() as c:
            for code, ts, amt, vol in (realtime_rows or []):
                c.execute(
                    "INSERT INTO ifind_realtime(code,datetime,price,amount,volume) VALUES(?,?,?,?,?)",
                    (code, ts, amt / (vol * 100) if vol else 0, amt, vol))
            for day, code, sealed in (event_rows or []):
                c.execute(
                    "INSERT INTO limit_up_events(date,code,name,touched,sealed_close,one_word,"
                    "board_count,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (day, code, "测试", 1, sealed, 0, 1, "2026-09-29 00:00:00"))
    return edb, mdb


def _run_close_check(edb, mdb, quotes, clock="2026-09-29 10:00:00"):
    """在夹具上跑一次持仓检查；返回 broker.sell_position 收到的 (pos_id, limit_price, reason)。"""
    sells = []

    class _B:
        @staticmethod
        def get_positions():
            codes = [c for c in quotes]
            return pd.DataFrame([{"code": c, "shares": 100, "source": "ai"} for c in codes])

        @staticmethod
        def sell_position(pid, shares, limit_price, reason):
            sells.append((pid, limit_price, reason))
            return "已成交"

    FakeDateTime.fixed = clock
    with patch.object(exp, "DB_PATH", edb), \
         patch.object(datasource, "MKT_DB", mdb), \
         patch.object(exp, "datetime", FakeDateTime), \
         patch.object(exp, "_latest_prices", return_value=quotes), \
         patch.object(exp, "_latest_price_times",
                      return_value={c: clock for c in quotes}), \
         patch.object(exp, "_calendar", return_value=list(CAL)), \
         patch("signals.get_panel_cached", side_effect=RuntimeError("no panel")), \
         patch("density_sr.latest_sr_map", return_value=pd.DataFrame()), \
         patch.dict(sys.modules, {"broker": _B}):
        import broker as bk_patch  # noqa: F401  # 确保 sys.modules 注入
        exp.position_close_check(TODAY)
    return sells


def test_event_trailing_exit():
    """浮盈曾到 +6%，从最高确认收盘回撤 3% → 移动止盈。"""
    pos = [dict(code="SH600001", buy_date=D3, buy_price=10.0, max_close=10.8)]
    with tempfile.TemporaryDirectory() as tmp:
        edb, mdb = _fixture(tmp, pos)
        sells = _run_close_check(edb, mdb, {"SH600001": (10.4, 10.6, 10.5, 11.0)})
        assert sells and sells[0][2] == "移动止盈", sells
    print("PASS: test_event_trailing_exit")


def test_event_vwap_exit():
    """价格破当日 VWAP → 破均价（未触发其他退出时）。"""
    pos = [dict(code="SH600002", buy_date=D3, buy_price=10.0, max_close=10.1)]
    with tempfile.TemporaryDirectory() as tmp:
        edb, mdb = _fixture(tmp, pos,
                            realtime_rows=[("SH600002", f"{TODAY} 10:00:00", 10.05e8, 1.0e6)])
        sells = _run_close_check(edb, mdb, {"SH600002": (9.9, 10.0, 10.05, 11.0)})
        assert sells and sells[0][2] == "破均价", sells
    print("PASS: test_event_vwap_exit")


def test_relay_not_sealed_at_close():
    """接力票（买入前一日封板）14:30 后未封板 → 尾盘不封板。"""
    pos = [dict(code="SH600003", buy_date=D3, buy_price=10.0, max_close=10.1)]
    with tempfile.TemporaryDirectory() as tmp:
        edb, mdb = _fixture(tmp, pos, event_rows=[(D2, "SH600003", 1)])
        quote = {"SH600003": (10.1, 10.0, 10.05, 11.0)}
        # 10:00 不触发（时间闸）
        sells_am = _run_close_check(edb, mdb, quote, clock="2026-09-29 10:00:00")
        # 14:35 触发
        sells_pm = _run_close_check(edb, mdb, quote, clock="2026-09-29 14:35:00")
        assert not any(s[2] == "尾盘不封板" for s in sells_am), sells_am
        assert any(s[2] == "尾盘不封板" for s in sells_pm), sells_pm
    print("PASS: test_relay_not_sealed_at_close")


def test_relay_sealed_stays():
    """接力票 14:30 后仍封板 → 不走。"""
    pos = [dict(code="SH600004", buy_date=D3, buy_price=10.0, max_close=10.1)]
    with tempfile.TemporaryDirectory() as tmp:
        edb, mdb = _fixture(tmp, pos, event_rows=[(D2, "SH600004", 1)])
        sells = _run_close_check(edb, mdb, {"SH600004": (11.0, 10.5, 10.4, 11.0)},
                                 clock="2026-09-29 14:35:00")
        # 封板价=11.0，现价 11.0 ≥ 11.0×0.995 → 不触发尾盘不封板；但会触发固定止盈 11.2？11.0<11.2 不触发
        assert not any(s[2] == "尾盘不封板" for s in sells), sells
    print("PASS: test_relay_sealed_stays")


def test_event_expiry_without_extension():
    """事件轨满 hold_days=2 到期即平，不按强弱展期。"""
    pos = [dict(code="SH600005", buy_date=D0, buy_price=10.0, max_close=10.05)]
    with tempfile.TemporaryDirectory() as tmp:
        edb, mdb = _fixture(tmp, pos)
        sells = _run_close_check(edb, mdb, {"SH600005": (10.0, 10.0, 10.02, 11.0)})
        assert sells and sells[0][2] == "到期", sells
    print("PASS: test_event_expiry_without_extension")


def test_event_stop_distance():
    """止损距离口径（卖出侧 M4 自适应共用）：min(5%, max(2%, 1.5×ATR%))。"""
    assert exp._event_stop_distance(None, 10.0) == 0.05
    assert abs(exp._event_stop_distance(0.2, 10.0) - 0.03) < 1e-9   # 1.5×2%
    assert exp._event_stop_distance(0.5, 10.0) == 0.05              # 夹到 5%
    assert exp._event_stop_distance(0.05, 10.0) == 0.02             # 夹到 2%
    print("PASS: test_event_stop_distance")


def test_event_rules_defaults():
    rules = exp.get_risk_rules("event")
    assert rules["hold_days"] == 2 and rules["stop_loss"] == -0.05, rules
    print("PASS: test_event_rules_defaults")


def test_satellite_month_halt():
    """月度熔断：当月已实现亏损 ≤ -8%×2万=-1600 → 停开仓；未超线 → 放行。"""
    with tempfile.TemporaryDirectory() as tmp:
        edb = Path(tmp) / "experience.db"
        with patch.object(exp, "DB_PATH", edb):
            with exp._conn() as c:
                # 当月两笔已平仓亏损：-1000 + -700 = -1700 ≤ -1600 → 熔断
                c.execute("INSERT INTO positions(code,buy_date,source,status,shares,buy_amount,"
                          "sell_date,pnl_pct,created_at) VALUES('SH600001','2026-09-10',"
                          "'satellite_scan','closed',100,10000,'2026-09-12',-0.10,'x')")
                c.execute("INSERT INTO positions(code,buy_date,source,status,shares,buy_amount,"
                          "sell_date,pnl_pct,created_at) VALUES('SH600002','2026-09-10',"
                          "'satellite_scan','closed',100,10000,'2026-09-15',-0.07,'x')")
                assert exp._satellite_month_halt(c, TODAY) is True
                # 上月亏损不计入当月
                assert exp._satellite_month_halt(c, "2026-10-05") is False
            with exp._conn() as c:
                c.execute("UPDATE positions SET pnl_pct=-0.05 WHERE code='SH600002'")
            with exp._conn() as c:
                # -1000-500=-1500 > -1600 → 不熔断
                assert exp._satellite_month_halt(c, TODAY) is False
    print("PASS: test_satellite_month_halt")


if __name__ == "__main__":
    test_event_trailing_exit()
    test_event_vwap_exit()
    test_relay_not_sealed_at_close()
    test_relay_sealed_stays()
    test_event_expiry_without_extension()
    test_event_stop_distance()
    test_event_rules_defaults()
    test_satellite_month_halt()
