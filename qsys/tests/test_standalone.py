"""独立测试：验证 factor_eval 核心改动（不依赖 streamlit 等重依赖）。
用 sys.modules mock 掉 qsys 内部的重依赖链。"""

import sys
import types
from pathlib import Path

# ---- Mock 重依赖，避免 import chain 报错 ----
import os
os.environ["QSYS_ROOT"] = "/tmp/qsys_test"

for mod_name in ["streamlit", "streamlit.delta_generator", "dotenv", "dotenv.main",
                 "qlib", "qlib.data", "qlib.data.dataset", "qlib.data.dataset.handler",
                 "qlib.contrib.evaluate", "qlib.contrib.strategy", "qlib.contrib.strategy.signal_strategy",
                 "datasource", "common", "signals", "broker", "portfolio", "experience",
                 "library", "structure", "composite", "validate_non_price",
                 "gates", "scheduler", "factor_retire", "loopengine",
                 "loopengine.engine", "loopengine.extra_frames", "loopengine.decay",
                 "loopengine.llm_review"]:
    sys.modules[mod_name] = types.ModuleType(mod_name)

# Mock common 模块所有需要的属性
_common = sys.modules["common"]
_common.DATA_DIR = Path("/tmp/qsys_test")
_common.QLIB_DATA_DIR = Path("/tmp/qsys_test")
_common.get_last_trade_day = lambda: "2025-01-01"
_common.QSYS_ROOT = Path("/tmp/qsys_test")
_common.init_qlib = lambda: None

# Mock datasource
_ds = sys.modules["datasource"]
_ds.get_loop_source = lambda: "custom"

# Mock signals
_sig = sys.modules["signals"]
_sig.fetch_panel = lambda *a, **kw: None
_sig.zscore = lambda s: (s - s.mean()) / (s.std() + 1e-12)  # 简单 z-score

# ---- 现在可以导入 factor_eval ----
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd
import factor_eval as fe


# ============================================================ 辅助函数
def _make_panel_and_vals(n_stocks=50, n_days=300, seed=42):
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2023-01-01", periods=n_days)
    codes = [f"sh{600000 + i}" for i in range(n_stocks)]

    close = pd.DataFrame(
        rng.lognormal(0, 0.02, (n_days, n_stocks)).cumprod(axis=0) * 100,
        index=dates, columns=codes
    )
    panel = pd.DataFrame({
        "$open": (close * (1 + rng.normal(0, 0.005, close.shape))).stack(),
        "$high": (close * (1 + abs(rng.normal(0, 0.01, close.shape)))).stack(),
        "$low": (close * (1 - abs(rng.normal(0, 0.01, close.shape)))).stack(),
        "$close": close.stack(),
        "$volume": pd.Series(rng.uniform(1e6, 1e8, n_days * n_stocks),
                             index=pd.MultiIndex.from_product([dates, codes])),
    })
    panel.index = panel.index.set_names(["datetime", "instrument"])

    signal = rng.normal(0, 1, (n_days, n_stocks))
    vals = pd.DataFrame(signal + rng.normal(0, 0.5, signal.shape),
                        index=dates, columns=codes).stack().rename("f")
    vals.index = vals.index.set_names(["datetime", "instrument"])
    return panel, vals


# ============================================================ 测试用例
def test_bh_fdr_basic():
    p = pd.Series([0.001, 0.01, 0.03, 0.05, 0.1, 0.5, 0.9])
    q = fe.bh_fdr(p, alpha=0.05)
    assert (q >= p - 1e-10).all(), "FDR q 值应 >= 原始 p 值"
    assert q.max() <= 1.0, "q 值不应超过 1.0"
    assert q.iloc[0] < 0.05, "最显著 p 值校正后仍应显著"
    print("PASS: test_bh_fdr_basic")


def test_bh_fdr_empty():
    q = fe.bh_fdr(pd.Series([], dtype=float))
    assert q.empty
    print("PASS: test_bh_fdr_empty")


def test_ic_pvalue_known():
    p = fe.ic_pvalue(0.05, 0.1, 100)
    assert p < 0.001, f"t=5 的 p 值应 < 0.001, got {p}"
    p0 = fe.ic_pvalue(0.0, 0.1, 100)
    assert abs(p0 - 1.0) < 0.01, f"IC=0 的 p 值应 ≈ 1.0, got {p0}"
    print("PASS: test_ic_pvalue_known")


def test_ic_pvalue_edge():
    assert fe.ic_pvalue(0.05, 0.1, 5) == 1.0, "n<10 → 1.0"
    assert fe.ic_pvalue(0.05, 0.0, 100) == 1.0, "std≈0 → 1.0"
    assert fe.ic_pvalue(0.05, 0.1, 0) == 1.0, "n=0 → 1.0"
    print("PASS: test_ic_pvalue_edge")


def test_top_group_winrate_unified():
    panel, vals = _make_panel_and_vals(n_stocks=50, n_days=200)
    wr = fe.top_group_winrate(vals, panel, fwd_days=5, step=10, cost=0.0025)
    assert 0 <= wr <= 1, f"胜率应在 [0,1], got {wr}"
    wr_nocost = fe.top_group_winrate(vals, panel, fwd_days=5, step=10, cost=0.0)
    assert wr <= wr_nocost + 0.02, "有成本 ≤ 无成本"
    print(f"PASS: test_top_group_winrate_unified (wr={wr:.3f}, wr_nocost={wr_nocost:.3f})")


def test_walk_forward_has_metrics():
    panel, vals = _make_panel_and_vals(n_stocks=30, n_days=400)
    wf = fe.walk_forward({"test_factor": vals}, panel, method="等权",
                         top_n=10, est=100, step=5, fwd_days=5, min_factors=1)
    assert not wf.empty, "walk_forward 不应为空"
    assert "优化组合扣费超额" in wf.columns
    expected_attrs = ["ann_return", "sharpe", "max_drawdown", "profit_factor",
                      "win_loss_ratio", "win_rate", "monthly_winrate",
                      "max_consec_loss_months", "total_return", "n_periods"]
    missing = [a for a in expected_attrs if a not in wf.attrs]
    assert not missing, f"缺少 attrs: {missing}"
    print(f"PASS: test_walk_forward_has_metrics "
          f"(sharpe={wf.attrs['sharpe']:.2f}, max_dd={wf.attrs['max_drawdown']:.2%})")


def test_backtest_credibility_score():
    wf = pd.DataFrame({"扣费超额": np.random.normal(0.005, 0.02, 50)})
    wf.attrs.update({
        "ann_return": 0.15, "sharpe": 1.2, "max_drawdown": -0.08,
        "profit_factor": 1.8, "win_loss_ratio": 1.5, "win_rate": 0.55,
        "monthly_winrate": 0.58, "max_consec_loss_months": 2,
        "total_return": 0.15, "n_periods": 50,
    })
    result = fe.backtest_credibility_score(wf)
    assert 0 <= result["score"] <= 100
    assert result["grade"] in "ABCDF"
    print(f"PASS: test_backtest_credibility_score (score={result['score']}, grade={result['grade']})")


def test_backtest_credibility_empty():
    result = fe.backtest_credibility_score(None)
    assert result["score"] == 0 and result["grade"] == "F"
    print("PASS: test_backtest_credibility_empty")


def test_default_fwd_days():
    assert fe.MAIN_FWD == 5, f"MAIN_FWD 应为 5, got {fe.MAIN_FWD}"
    print(f"PASS: test_default_fwd_days (MAIN_FWD={fe.MAIN_FWD})")


def test_default_cost():
    assert fe.DEFAULT_COST == 0.0025, f"DEFAULT_COST 应为 0.0025, got {fe.DEFAULT_COST}"
    print(f"PASS: test_default_cost (DEFAULT_COST={fe.DEFAULT_COST})")


def test_walk_forward_output_columns():
    """walk_forward 输出列名应包含 '均值' 而非 '中位'。"""
    panel, vals = _make_panel_and_vals(n_stocks=30, n_days=400)
    wf = fe.walk_forward({"test_factor": vals}, panel, method="等权",
                         top_n=10, est=100, step=5, fwd_days=5, min_factors=1)
    cols = list(wf.columns)
    has_median = any("中位" in c for c in cols)
    has_mean = any("均值" in c for c in cols)
    assert not has_median, f"不应有 '中位' 列: {cols}"
    assert has_mean, f"应有 '均值' 列: {cols}"
    print(f"PASS: test_walk_forward_output_columns (用均值替代中位数)")


# ============================================================ 主函数
if __name__ == "__main__":
    tests = [
        test_bh_fdr_basic,
        test_bh_fdr_empty,
        test_ic_pvalue_known,
        test_ic_pvalue_edge,
        test_top_group_winrate_unified,
        test_walk_forward_has_metrics,
        test_walk_forward_output_columns,
        test_backtest_credibility_score,
        test_backtest_credibility_empty,
        test_default_fwd_days,
        test_default_cost,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print(f"\n{'='*50}")
    print(f"结果: {passed} 通过, {failed} 失败, 共 {len(tests)} 个")
    sys.exit(1 if failed else 0)
