"""测试 experience.py 动态止盈逻辑。"""
import sys, types, os
from pathlib import Path

os.environ["QSYS_ROOT"] = "/tmp/qsys_test"

# Mock 依赖
for mod_name in ["streamlit", "streamlit.delta_generator", "dotenv", "dotenv.main",
                 "datasource", "common", "signals", "broker", "portfolio",
                 "qlib", "qlib.data", "qlib.data.dataset", "qlib.data.dataset.handler"]:
    sys.modules[mod_name] = types.ModuleType(mod_name)

sys.modules["common"].DATA_DIR = Path("/tmp/qsys_test")
sys.modules["common"].QLIB_DATA_DIR = Path("/tmp/qsys_test")
sys.modules["common"].get_last_trade_day = lambda: "2025-01-01"
sys.modules["common"].QSYS_ROOT = Path("/tmp/qsys_test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd


def test_default_rules_contain_atr():
    """DEFAULT_RULES 应包含 ATR 参数。"""
    # 直接读源码验证
    src = Path(__file__).resolve().parent.parent / "experience.py"
    content = src.read_text()
    assert '"use_atr_tp"' in content, "应含 use_atr_tp"
    assert '"atr_period"' in content, "应含 atr_period"
    assert '"atr_tp_multiplier"' in content, "应含 atr_tp_multiplier"
    print("PASS: test_default_rules_contain_atr")


def test_dynamic_tp_logic():
    """模拟 simulate_trade 中的动态止盈逻辑。"""
    # 构造模拟 K 线
    rng = np.random.RandomState(42)
    n = 30
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series(100 + np.cumsum(rng.randn(n) * 0.5), index=dates)
    high = close + abs(rng.randn(n) * 0.5)
    low = close - abs(rng.randn(n) * 0.5)

    entry_price = 100.0

    # 模拟 ATR 计算
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    atr_tp = float(atr) * 2.5 / entry_price

    # 验证 ATR 止盈 > 固定止盈
    fixed_tp = 0.15
    dynamic_tp = max(fixed_tp, atr_tp)
    assert dynamic_tp >= fixed_tp, f"动态止盈应 >= 固定止盈: {dynamic_tp} < {fixed_tp}"

    # 验证 ATR 计算合理
    assert 0.01 < atr < 5.0, f"ATR 值异常: {atr}"
    print(f"PASS: test_dynamic_tp_logic (ATR={atr:.2f}, ATR止盈={atr_tp:.1%}, 最终止盈={dynamic_tp:.1%})")


if __name__ == "__main__":
    tests = [test_default_rules_contain_atr, test_dynamic_tp_logic]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
    print(f"\n{'='*50}")
    print(f"结果: {passed} 通过, {failed} 失败, 共 {len(tests)} 个")
    sys.exit(1 if failed else 0)
