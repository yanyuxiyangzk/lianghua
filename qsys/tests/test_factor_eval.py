"""测试 factor_eval.py 关键改动：统一口径、FDR校正、可信度评分。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 确保 qsys 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import factor_eval as fe


# ================================================================ 辅助函数
def _make_ic_series(n=100, mean=0.03, std=0.05, seed=42):
    """构造模拟 IC 序列。"""
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2024-01-01", periods=n)
    vals = rng.normal(mean, std, n)
    return pd.Series(vals, index=dates)


def _make_panel_and_vals(n_stocks=50, n_days=300, seed=42):
    """构造模拟面板和因子值。"""
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2023-01-01", periods=n_days)
    codes = [f"sh{600000 + i}" for i in range(n_stocks)]

    idx = pd.MultiIndex.from_product([dates, codes], names=["datetime", "instrument"])
    # 面板：open/high/low/close/volume
    close = pd.DataFrame(
        rng.lognormal(0, 0.02, (n_days, n_stocks)).cumprod(axis=0) * 100,
        index=dates, columns=codes
    )
    panel = pd.DataFrame({
        "$open": (close * (1 + rng.normal(0, 0.005, close.shape))).stack(),
        "$high": (close * (1 + abs(rng.normal(0, 0.01, close.shape)))).stack(),
        "$low": (close * (1 - abs(rng.normal(0, 0.01, close.shape)))).stack(),
        "$close": close.stack(),
        "$volume": pd.Series(rng.uniform(1e6, 1e8, n_days * n_stocks), index=idx),
    })
    panel.index = panel.index.set_names(["datetime", "instrument"])

    # 因子值：带微弱信号的噪声
    signal = rng.normal(0, 1, (n_days, n_stocks))
    vals = pd.DataFrame(signal + rng.normal(0, 0.5, signal.shape), index=dates, columns=codes)
    vals = vals.stack().rename("f")
    vals.index = vals.index.set_names(["datetime", "instrument"])

    return panel, vals


# ================================================================ 测试用例
def test_bh_fdr_basic():
    """FDR 校正：基本功能。"""
    p = pd.Series([0.001, 0.01, 0.03, 0.05, 0.1, 0.5, 0.9])
    q = fe.bh_fdr(p, alpha=0.05)
    # 校正后 p 值应该 >= 原始 p 值
    assert (q >= p - 1e-10).all(), "FDR 校正后 q 值应 >= 原始 p 值"
    # 校正后最大值为 1.0
    assert q.max() <= 1.0, "q 值不应超过 1.0"
    # 原始 p=0.001 应该仍然显著
    assert q.iloc[0] < 0.05, "最显著的 p 值校正后仍应显著"
    print("PASS: test_bh_fdr_basic")


def test_bh_fdr_empty():
    """FDR 校正：空输入。"""
    p = pd.Series([], dtype=float)
    q = fe.bh_fdr(p)
    assert q.empty, "空输入应返回空"
    print("PASS: test_bh_fdr_empty")


def test_ic_pvalue_known():
    """p-value 计算：已知 t 统计量。"""
    # IC=0.05, std=0.1, n=100 → t = 0.05 / (0.1 / 10) = 5.0 → p 应非常小
    p = fe.ic_pvalue(0.05, 0.1, 100)
    assert p < 0.001, f"t=5 的 p 值应 < 0.001, 实际 {p}"
    # IC=0, std=0.1, n=100 → t=0 → p=1.0
    p0 = fe.ic_pvalue(0.0, 0.1, 100)
    assert abs(p0 - 1.0) < 0.01, f"IC=0 的 p 值应 ≈ 1.0, 实际 {p0}"
    print("PASS: test_ic_pvalue_known")


def test_ic_pvalue_edge():
    """p-value 计算：边界情况。"""
    assert fe.ic_pvalue(0.05, 0.1, 5) == 1.0, "n<10 应返回 1.0"
    assert fe.ic_pvalue(0.05, 0.0, 100) == 1.0, "std≈0 应返回 1.0"
    assert fe.ic_pvalue(0.05, 0.1, 0) == 1.0, "n=0 应返回 1.0"
    print("PASS: test_ic_pvalue_edge")


def test_apply_fdr_correction():
    """FDR 校正：对体检表应用。"""
    df = pd.DataFrame({
        "因子": ["A", "B", "C", "D"],
        "IC均值": [0.05, 0.03, 0.01, 0.001],
        "ICIR": [0.5, 0.3, 0.1, 0.01],
        "天数": [200, 200, 200, 200],
    })
    result = fe.apply_fdr_correction(df, alpha=0.05)
    assert "p_value" in result.columns, "应新增 p_value 列"
    assert "q_value" in result.columns, "应新增 q_value 列"
    assert "FDR显著" in result.columns, "应新增 FDR显著 列"
    # IC 最大的因子应该最显著
    assert result.loc[0, "p_value"] < result.loc[3, "p_value"], "IC 越大 p 值应越小"
    print("PASS: test_apply_fdr_correction")


def test_top_group_winrate_unified():
    """top_group_winrate：统一口径（等权均值+成本）。"""
    panel, vals = _make_panel_and_vals(n_stocks=50, n_days=200)
    wr = fe.top_group_winrate(vals, panel, fwd_days=5, step=5, cost=0.0025)
    assert 0 <= wr <= 1, f"胜率应在 [0,1], 实际 {wr}"
    # 有成本时胜率应略低于无成本时
    wr_nocost = fe.top_group_winrate(vals, panel, fwd_days=5, step=5, cost=0.0)
    assert wr <= wr_nocost + 0.01, "有成本胜率应 <= 无成本胜率"
    print(f"PASS: test_top_group_winrate_unified (wr={wr:.3f}, wr_nocost={wr_nocost:.3f})")


def test_walk_forward_has_metrics():
    """walk_forward：输出包含汇总指标。"""
    panel, vals = _make_panel_and_vals(n_stocks=30, n_days=400)
    factor_vals = {"test_factor": vals}
    wf = fe.walk_forward(factor_vals, panel, method="等权", top_n=10,
                         est=100, step=5, fwd_days=5, min_factors=1)
    assert not wf.empty, "walk_forward 不应为空"
    assert "优化组合扣费超额" in wf.columns, "应有扣费超额列"
    # 检查 attrs 汇总指标
    expected_attrs = ["ann_return", "sharpe", "max_drawdown", "profit_factor",
                      "win_loss_ratio", "win_rate", "monthly_winrate",
                      "max_consec_loss_months", "total_return", "n_periods"]
    for attr in expected_attrs:
        assert attr in wf.attrs, f"缺少 attrs: {attr}"
    print(f"PASS: test_walk_forward_has_metrics "
          f"(sharpe={wf.attrs['sharpe']:.2f}, max_dd={wf.attrs['max_drawdown']:.2%})")


def test_backtest_credibility_score():
    """回测可信度评分。"""
    # 构造一个假装是 walk_forward 结果的 DataFrame
    wf = pd.DataFrame({"扣费超额": np.random.normal(0.005, 0.02, 50)})
    wf.attrs["ann_return"] = 0.15
    wf.attrs["sharpe"] = 1.2
    wf.attrs["max_drawdown"] = -0.08
    wf.attrs["profit_factor"] = 1.8
    wf.attrs["win_loss_ratio"] = 1.5
    wf.attrs["win_rate"] = 0.55
    wf.attrs["monthly_winrate"] = 0.58
    wf.attrs["max_consec_loss_months"] = 2
    wf.attrs["total_return"] = 0.15
    wf.attrs["n_periods"] = 50

    result = fe.backtest_credibility_score(wf)
    assert "score" in result, "应有 score"
    assert "grade" in result, "应有 grade"
    assert "details" in result, "应有 details"
    assert "warnings" in result, "应有 warnings"
    assert 0 <= result["score"] <= 100, f"分数应在 [0,100], 实际 {result['score']}"
    assert result["grade"] in "ABCDF", f"评级应在 ABCDF, 实际 {result['grade']}"
    print(f"PASS: test_backtest_credibility_score (score={result['score']}, grade={result['grade']})")


def test_backtest_credibility_empty():
    """回测可信度评分：空输入。"""
    result = fe.backtest_credibility_score(None)
    assert result["score"] == 0
    assert result["grade"] == "F"
    print("PASS: test_backtest_credibility_empty")


def test_consistency_fwd_days():
    """口径一致性：MAIN_FWD 与 GATE['FWD_DAYS'] 对齐。"""
    from gates import GATE
    assert fe.MAIN_FWD == GATE["FWD_DAYS"], \
        f"MAIN_FWD={fe.MAIN_FWD} 应等于 GATE['FWD_DAYS']={GATE['FWD_DAYS']}"
    print(f"PASS: test_consistency_fwd_days (MAIN_FWD={fe.MAIN_FWD}, GATE={GATE['FWD_DAYS']})")


# ================================================================ 主函数
if __name__ == "__main__":
    tests = [
        test_bh_fdr_basic,
        test_bh_fdr_empty,
        test_ic_pvalue_known,
        test_ic_pvalue_edge,
        test_apply_fdr_correction,
        test_top_group_winrate_unified,
        test_walk_forward_has_metrics,
        test_backtest_credibility_score,
        test_backtest_credibility_empty,
        test_consistency_fwd_days,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
    print(f"\n{'='*50}")
    print(f"结果: {passed} 通过, {failed} 失败, 共 {len(tests)} 个")
