"""进化信号（战报蒸馏）单测：抽取/校验/退化检测/有效期/shadow 挂钩/prompt 渲染。

方案 docs/report-distill-evolution-plan.md v2。shadow 挂钩测引擎侧"只记快照不改行为"。
"""

import json
import os
import sys
import types
from pathlib import Path

os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ["QSYS_ROOT"] = "/tmp/qsys_test"

for mod_name in ["streamlit", "streamlit.delta_generator", "dotenv", "dotenv.main",
                 "qlib", "qlib.data", "qlib.data.dataset", "qlib.data.dataset.handler",
                 "qlib.contrib.evaluate", "qlib.contrib.strategy", "qlib.contrib.strategy.signal_strategy",
                 "datasource", "common", "signals", "library", "structure", "gates",
                 "event_bus", "composite", "broker", "portfolio", "experience",
                 "validate_non_price", "scheduler", "factor_retire", "loopengine.regime"]:
    sys.modules[mod_name] = types.ModuleType(mod_name)

import json as _json
_common = sys.modules["common"]
_common.DATA_DIR = Path("/tmp/qsys_test")
_common.QLIB_DATA_DIR = Path("/tmp/qsys_test")
_common.get_last_trade_day = lambda: "2025-01-01"
_common.QSYS_ROOT = Path("/tmp/qsys_test")
_common.init_qlib = lambda: None
_common.load_json = lambda p, default=None: (_json.loads(Path(p).read_text())
                                             if Path(p).exists()
                                             else (default if default is not None else {}))


def _fake_trade_day_offset(day: str, n: int) -> str:
    """测试用伪交易日历：自然日偏移（断言按此设计）。"""
    from datetime import datetime, timedelta
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


_common.trade_day_offset = _fake_trade_day_offset

sys.modules["event_bus"].EventType = types.SimpleNamespace()
sys.modules["event_bus"].bus = None
_reg = sys.modules["loopengine.regime"]
_reg.detect_regime = _reg.detect_regime_from_reports = lambda *a, **kw: {}
_reg.get_regime_factor_weight = lambda *a, **kw: 1.0

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loopengine import evolution_signals as es
from loopengine.engine import LoopEngine, _build_llm_prompt


# ---------------------------------------------------------------- 抽取
def test_extract_json_obj_nested_and_messy():
    d = {"steer": {"families_boost": {"动量": 0.3}}, "confidence": 0.8}
    assert es.extract_json_obj(json.dumps(d)) == d
    assert es.extract_json_obj("```json\n" + json.dumps(d) + "\n```") == d
    assert es.extract_json_obj("思考中……\n" + json.dumps(d) + "\n以上") == d
    # 字符串内含花括号不应截断
    d2 = {"hypotheses": ["开板{异常}回封"], "confidence": 0.5}
    assert es.extract_json_obj(json.dumps(d2, ensure_ascii=False)) == d2
    assert es.extract_json_obj("没有对象") is None
    assert es.extract_json_obj('{"a": 1') is None  # 不平衡
    assert es.extract_json_obj(None) is None


# ---------------------------------------------------------------- 校验
def test_validate_full_signal():
    ok, why, clean = es.validate_signals({
        "regime_hint": "sideways", "confidence": 0.75,
        "effective": [{"target": "资金流", "evidence": "主力净流入榜占 6 席", "support": "data"}],
        "decaying": [{"target": "动量", "evidence": "动量因子胜率跌破 45%", "support": "bogus"}],
        "hypotheses": ["开板回封次日缩量企稳的溢价"],
        "steer": {"families_boost": {"资金流": 0.9, "动量": -0.9}, "fields_boost": {},
                  "types_boost": {"资金流": 0.3}}})
    assert ok and why == ""
    # boost clamp ±0.5
    assert clean["steer"]["families_boost"]["资金流"] == 0.5
    assert clean["steer"]["families_boost"]["动量"] == -0.5
    # 非法 support → narrative 兜底
    assert clean["decaying"][0]["support"] == "narrative"
    assert clean["regime_hint"] == "sideways" and clean["confidence"] == 0.75


def test_validate_insufficient_evidence_clears_sections():
    ok, _, clean = es.validate_signals({
        "insufficient_evidence": True, "confidence": 0.2,
        "effective": [{"target": "动量", "evidence": "编的"}],
        "regime_hint": "bear"})
    assert ok and clean["effective"] == [] and clean["hypotheses"] == []
    assert clean["regime_hint"] == "bear"  # regime_hint 保留


def test_validate_rejects_empty_and_nondict():
    assert es.validate_signals({"confidence": 0.5})[0] is False
    assert es.validate_signals(["not a dict"])[0] is False
    # 只有 regime_hint 也算有效（市场状态本身就是信号）
    assert es.validate_signals({"regime_hint": "bull"})[0] is True


# ---------------------------------------------------------------- 退化检测
def test_degenerate_report_detection():
    assert es.is_degenerate_report("")
    assert es.is_degenerate_report("⚠️ LLM 服务不可用，战报生成失败")
    assert es.is_degenerate_report("短")
    long_ok = "今日账户上涨，因子表现分化。" * 30
    assert not es.is_degenerate_report(long_ok)


# ---------------------------------------------------------------- 模式开关
def test_mode_default_and_file():
    p = Path("/tmp/qsys_test/evolution_signals.json")
    if p.exists():
        p.unlink()
    assert es.get_mode() == "shadow"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text('{"mode": "prompt"}')
        assert es.get_mode() == "prompt"
        p.write_text('{"mode": "bogus"}')
        assert es.get_mode() == "shadow", "非法值应回退 shadow"
    finally:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- 有效期（伪日历=自然日）
def _sig(report_date, conf=0.8, insufficient=False):
    return {"report_date": report_date, "signals": {"confidence": conf,
            "insufficient_evidence": insufficient, "steer": {}}}


def test_usable_layer():
    today = "2026-09-16"
    fresh = _sig("2026-09-15")
    # usable_layer 接收的是 signals dict + today——这里直接测底层语义
    sig = {"report_date": "2026-09-15", "confidence": 0.8, "insufficient_evidence": False}
    assert es.usable_layer(sig, today) == "struct"        # 隔 1 日
    sig["report_date"] = "2026-09-14"
    assert es.usable_layer(sig, today) == "struct"        # 隔 2 日
    sig["report_date"] = "2026-09-13"
    assert es.usable_layer(sig, today) == "text"          # 隔 3 日 → 仅文本层
    sig["report_date"] = "2026-09-10"
    assert es.usable_layer(sig, today) == "none"          # 超 3 日 → 过期
    sig2 = dict(sig, report_date="2026-09-15", confidence=0.3)
    assert es.usable_layer(sig2, today) == "text"         # 低置信 → 最多 text
    sig3 = dict(sig, report_date="2026-09-15", insufficient_evidence=True)
    assert es.usable_layer(sig3, today) == "text"


def test_usable_layer_row_level_report_date():
    """行级形态：signals JSON 里没有 report_date（它是行级列），调用方显式传入——
    回归 2026-09-16 修复（usable_layer 曾从 signals JSON 里读不到日期恒判 none）。"""
    today = "2026-09-16"
    bare_signals = {"confidence": 0.8, "insufficient_evidence": False}
    assert es.usable_layer(bare_signals, today) == "none"  # 两处都没有日期 → none
    assert es.usable_layer(bare_signals, today, report_date="2026-09-15") == "struct"


# ---------------------------------------------------------------- shadow 挂钩（引擎侧，只记不改）
def test_shadow_hook_records_snapshot_without_side_effects():
    calls = {}
    exp = sys.modules["experience"]
    exp.get_latest_evolution_signal = lambda: {
        "date": "2026-09-15", "report_date": "2026-09-15",
        "signals": {"confidence": 0.8, "insufficient_evidence": False,
                    "regime_hint": "sideways",
                    "steer": {"families_boost": {"资金流": 0.4}, "fields_boost": {},
                              "types_boost": {"资金流": 0.2}}},
        "shadow_bias_json": None, "norm_scheme": "legacy"}
    exp.update_signal_shadow_bias = lambda d, snap: calls.update(date=d, snap=snap)

    p = Path("/tmp/qsys_test/evolution_signals.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"mode": "shadow"}')
    try:
        eng = LoopEngine.__new__(LoopEngine)
        eng.state = {"iteration": 42}
        eng._signal_shadow_hook(["冷族A"], ["热族B"])
        assert calls.get("date") == "2026-09-15", "应回写 shadow_bias"
        snap = calls["snap"]
        assert snap["would_boost_families"] == {"资金流": 0.4}
        assert snap["usable_layer"] in ("struct", "text")  # 伪日历下隔 1 日
        assert snap["gaps_now"] == ["冷族A"] and snap["proven_now"] == ["热族B"]
        # off 模式不写
        p.write_text('{"mode": "off"}')
        calls.clear()
        eng._signal_shadow_hook(["冷族A"], ["热族B"])
        assert not calls, "mode=off 不应写快照"
    finally:
        p.unlink(missing_ok=True)


def test_shadow_hook_no_signal_no_write():
    exp = sys.modules["experience"]
    exp.get_latest_evolution_signal = lambda: None
    exp.update_signal_shadow_bias = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应调用"))
    eng = LoopEngine.__new__(LoopEngine)
    eng.state = {"iteration": 1}
    eng._signal_shadow_hook([], [])  # 无信号 = 静默返回


# ---------------------------------------------------------------- prompt 渲染与假设分槽
def test_render_and_hypotheses_slots():
    sig = {"regime_hint": "transition",
           "effective": [{"target": "资金流", "evidence": "榜 6 席", "support": "data"}],
           "decaying": [{"target": "动量", "evidence": "胜率 43%", "support": "data"}],
           "hypotheses": ["开板回封缩量企稳溢价"]}
    txt = es.render_for_prompt(sig)
    assert "transition" in txt and "资金流" in txt and "失效规避" in txt
    p = _build_llm_prompt("资金流", "why", "资金流", txt, ["rank_cs(close)"],
                          hypotheses=es.hypotheses_of(sig))
    assert "通过统计闸门的真实因子" in p and "待验证机制假设" in p and "开板回封" in p
    # 分槽：假设段落在 few-shot 段落之后
    assert p.index("待验证机制假设") > p.index("通过统计闸门的真实因子")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS: {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL: {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n结果: {len(fns) - failed} 通过, {failed} 失败, 共 {len(fns)} 个")
    sys.exit(1 if failed else 0)
