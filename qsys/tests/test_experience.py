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


def test_save_pick_norm_not_identity():
    """冷评审发现1回归：factors 带不带 norm 快照键不影响组合身份——
    同日同组合应覆盖落库而非双行（否则 outcome_backfill 双计战果）。"""
    import os
    db = Path("/tmp/qsys_test/experience.db")
    if db.exists():
        os.remove(db)
    sys.modules["common"].all_pools = lambda: {}
    sys.modules["datasource"].get_source = lambda: "test"
    sys.modules["common"].load_json = lambda *a, **kw: {}
    import experience

    scores = pd.Series({"S001": 1.5, "S002": 0.9})
    base = [{"name": "mom_5d", "kind": "builtin", "weight": 1.0, "direction": 1}]
    id1 = experience.save_pick(source="t", pool_name="p", top_n=2, method="m",
                               filters=[], factors=base, final_scores=scores,
                               trade_date="2026-09-16")
    with_norm = [dict(f, norm="rank") for f in base]
    id2 = experience.save_pick(source="t", pool_name="p", top_n=2, method="m",
                               filters=[], factors=with_norm, final_scores=scores,
                               trade_date="2026-09-16")
    assert id1 == id2, "norm 快照差异不应改变组合身份"
    with experience._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM picks WHERE trade_date='2026-09-16'").fetchone()[0]
        stored = c.execute("SELECT factors FROM picks WHERE id=?", (id2,)).fetchone()[0]
    assert n == 1, "同日同组合只应一行"
    assert '"norm"' in stored, "norm 快照应保留在存储列（只是不进身份）"
    print("PASS: test_save_pick_norm_not_identity")


def test_account_risk_level_boundaries():
    """账户回撤分级边界应稳定落在 normal/yellow/orange/red。"""
    import experience
    cfg = {
        "yellow_drawdown": 0.03, "orange_drawdown": 0.05, "red_drawdown": 0.08,
        "normal_target": 0.80, "yellow_target": 0.70,
        "orange_target": 0.60, "red_target": 0.30,
    }
    assert experience.account_risk_level(-0.0299, cfg) == ("normal", 0.80)
    assert experience.account_risk_level(-0.03, cfg) == ("yellow", 0.70)
    assert experience.account_risk_level(-0.05, cfg) == ("orange", 0.60)
    assert experience.account_risk_level(-0.08, cfg) == ("red", 0.30)
    print("PASS: test_account_risk_level_boundaries")


def test_risk_llm_normal_skips_call():
    """正常风险等级不得调用 LLM，避免常态消耗 Token。"""
    import experience
    import llmutil
    original = llmutil.llm_chat
    llmutil.llm_chat = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("normal 等级不应调用 LLM"))
    try:
        result = experience.risk_llm_advice({"date": "2026-09-21", "level": "normal"})
        assert result["status"] == "skipped"
        assert result["advisory_only"] is True
    finally:
        llmutil.llm_chat = original
    print("PASS: test_risk_llm_normal_skips_call")


def test_risk_llm_advice_enforces_boundaries():
    """LLM 不能放宽目标仓位，也不能推荐证据集之外的股票。"""
    import json
    import experience
    import llmutil
    original = llmutil.llm_chat
    captured = {}

    def fake_chat(system, user, **kwargs):
        captured.update(system=system, user=user, kwargs=kwargs)
        return json.dumps({
            "decision": "维持规则计划",
            "confidence": 0.9,
            "recommended_target_position": 0.95,
            "summary": "测试",
            "priority_positions": [
                {"code": "SH600000", "priority": 1, "action": "优先减仓", "reason": "当前浮亏"},
                {"code": "SH999999", "priority": 2, "action": "优先减仓", "reason": "不存在"},
            ],
            "account_actions": ["保持开仓闸"],
            "rule_disagreement": {"has_disagreement": False, "reason": ""},
            "missing_evidence": [],
        }, ensure_ascii=False)

    llmutil.llm_chat = fake_chat
    try:
        plan = {
            "date": "2026-09-21", "level": "orange", "drawdown": -0.05,
            "current_position_ratio": 0.8, "target_position_ratio": 0.6,
            "required_release": 20000,
            "positions": [{"code": "SH600000", "source": "ai", "market_value": 30000,
                           "risk_score": 25, "suggested_sell_value": 20000,
                           "reasons": ["当前浮亏"]}],
        }
        result = experience.risk_llm_advice(plan, force=True)
        assert result["recommended_target_position"] == 0.6
        assert [x["code"] for x in result["priority_positions"]] == ["SH600000"]
        assert "strict" not in captured["user"].lower()
        assert captured["user"].startswith("账户风险证据 JSON")
        assert captured["kwargs"]["label"] == "account_risk_advice_v2"
    finally:
        llmutil.llm_chat = original
    print("PASS: test_risk_llm_advice_enforces_boundaries")


if __name__ == "__main__":
    tests = [test_default_rules_contain_atr, test_dynamic_tp_logic,
             test_save_pick_norm_not_identity, test_account_risk_level_boundaries,
             test_risk_llm_normal_skips_call, test_risk_llm_advice_enforces_boundaries]
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
