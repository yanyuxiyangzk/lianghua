"""composite._daily_cs_z 回归测试：前视修复（composite.py:78 全局 z → 逐日截面 z）。

核心断言：未来日的数据变化不得改变历史日的归一化值（修复前全局均值方差会）。
沿用 test_standalone 的 mock 模式（composite 走真实模块，重依赖 mock）。
"""

import sys
import types
from pathlib import Path

import os
os.environ["QSYS_ROOT"] = "/tmp/qsys_test"

# ---- Mock 重依赖（与 test_standalone 同清单，但 composite 用真实模块）----
for mod_name in ["streamlit", "streamlit.delta_generator", "dotenv", "dotenv.main",
                 "qlib", "qlib.data", "qlib.data.dataset", "qlib.data.dataset.handler",
                 "qlib.contrib.evaluate", "qlib.contrib.strategy", "qlib.contrib.strategy.signal_strategy",
                 "datasource", "common", "signals", "broker", "portfolio", "experience",
                 "library", "structure", "validate_non_price",
                 "gates", "scheduler", "factor_retire", "loopengine",
                 "loopengine.engine", "loopengine.extra_frames", "loopengine.decay",
                 "loopengine.llm_review"]:
    sys.modules[mod_name] = types.ModuleType(mod_name)

_common = sys.modules["common"]
_common.DATA_DIR = Path("/tmp/qsys_test")
_common.QLIB_DATA_DIR = Path("/tmp/qsys_test")
_common.get_last_trade_day = lambda: "2025-01-01"
_common.QSYS_ROOT = Path("/tmp/qsys_test")
_common.init_qlib = lambda: None
_common.all_pools = lambda: {}

sys.modules["datasource"].get_loop_source = lambda: "custom"

# signals 的 zscore 必须与真实语义一致（含 clip±3），否则测的是假实现
_sig = sys.modules["signals"]
_sig.fetch_panel = lambda *a, **kw: None
_sig.zscore = lambda s: ((s - s.mean()) / (s.std() + 1e-12)).clip(-3, 3)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd
import composite


def _mk(days_vals: dict[str, dict[str, float]]) -> pd.Series:
    """{day: {code: val}} → (datetime, instrument) 长表 Series。"""
    idx, vals = [], []
    for d, m in days_vals.items():
        for code, v in m.items():
            idx.append((pd.Timestamp(d), code))
            vals.append(v)
    s = pd.Series(vals, index=pd.MultiIndex.from_tuples(idx, names=["datetime", "instrument"]))
    return s.sort_index()


def test_no_lookahead_future_days_do_not_change_history():
    """前视回归：第 3 天数据缩放 100 倍，第 1/2 天的 z 值必须不变。
    旧实现（全历史均值方差）下此断言必然失败。"""
    base = {"2025-01-02": {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0},
            "2025-01-03": {"A": 2.0, "B": 2.5, "C": 3.5, "D": 4.5},
            "2025-01-06": {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}}
    z1 = composite._daily_cs_z(_mk(base))
    base["2025-01-06"] = {k: v * 100 for k, v in base["2025-01-06"].items()}
    z2 = composite._daily_cs_z(_mk(base))
    for day in ["2025-01-02", "2025-01-03"]:
        a = z1[z1.index.get_level_values("datetime") == day]
        b = z2[z2.index.get_level_values("datetime") == day]
        pd.testing.assert_series_equal(a, b, check_names=False)


def test_clip_bounds():
    """单日截面一个极端离群票，输出必须被 clip 到 ±3。"""
    day = {f"S{i:02d}": float(i) for i in range(30)}
    day["OUT"] = 1e6
    z = composite._daily_cs_z(_mk({"2025-01-02": day}))
    assert float(z.abs().max()) <= 3.0 + 1e-9


def test_direction_flips_sign():
    v = _mk({"2025-01-02": {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}})
    z_pos = composite._daily_cs_z(v, 1)
    z_neg = composite._daily_cs_z(v, -1)
    pd.testing.assert_series_equal(z_pos, -z_neg, check_names=False)


def test_per_day_centered():
    """逐日均值≈0（截面中心化）。"""
    v = _mk({"2025-01-02": {"A": 1.0, "B": 5.0, "C": 9.0, "D": 13.0},
             "2025-01-03": {"A": 100.0, "B": 100.5, "C": 101.0, "D": 102.0}})
    z = composite._daily_cs_z(v)
    for day, g in z.groupby(level="datetime"):
        assert abs(float(g.mean())) < 1e-9, f"{day} 未居中"


if __name__ == "__main__":
    test_no_lookahead_future_days_do_not_change_history()
    test_clip_bounds()
    test_direction_flips_sign()
    test_per_day_centered()
    print("composite._daily_cs_z 全部测试通过")
