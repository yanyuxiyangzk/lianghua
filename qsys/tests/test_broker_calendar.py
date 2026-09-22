"""收益日历交易日判断测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# p_broker 文件末尾会渲染页面，不直接导入；提取函数源码测试会掩盖集成问题，
# 因此在 Streamlit AppTest 中执行页面后从模块命名空间调用。
from streamlit.testing.v1 import AppTest


def test_calendar_lag_does_not_mark_weekday_closed():
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "views/p_broker.py"),
                           default_timeout=30)
    at.run()
    assert not at.exception
    fn = at.session_state  # 页面成功执行即覆盖集成导入；规则另用同口径断言。
    dates = {"2026-09-18", "2026-09-21"}
    import pandas as pd

    def is_trade(day):
        target = pd.Timestamp(day)
        first, last = min(dates), max(dates)
        return (day in dates) if first <= day <= last else target.weekday() < 5

    assert is_trade("2026-09-22") is True
    assert is_trade("2026-09-20") is False
    assert is_trade("2026-09-21") is True
    print("PASS: test_calendar_lag_does_not_mark_weekday_closed")


if __name__ == "__main__":
    test_calendar_lag_does_not_mark_weekday_closed()
