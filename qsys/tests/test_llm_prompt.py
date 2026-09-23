"""LLM 出题/审查 prompt 改造的单测（2026-09-16 prompt 审查 5 项修复）。

覆盖：字段语义表完整性、prompt 构建（语义表+few-shot+证据注入）、
S 表达式鲁棒抽取、审查端 JSON 抽取、无 key 降级路径。
"""

import os
import sys
import types
from pathlib import Path

os.environ.pop("DEEPSEEK_API_KEY", None)  # 确保无 key 降级路径可测
os.environ["QSYS_ROOT"] = "/tmp/qsys_test"

# ---- Mock 重依赖（engine 顶层导入链）----
for mod_name in ["streamlit", "streamlit.delta_generator", "dotenv", "dotenv.main",
                 "qlib", "qlib.data", "qlib.data.dataset", "qlib.data.dataset.handler",
                 "qlib.contrib.evaluate", "qlib.contrib.strategy", "qlib.contrib.strategy.signal_strategy",
                 "datasource", "common", "signals", "library", "structure", "gates",
                 "event_bus", "composite", "broker", "portfolio", "experience",
                 "validate_non_price", "scheduler", "factor_retire", "loopengine.regime"]:
    sys.modules[mod_name] = types.ModuleType(mod_name)

_common = sys.modules["common"]
_common.DATA_DIR = Path("/tmp/qsys_test")
_common.QLIB_DATA_DIR = Path("/tmp/qsys_test")
_common.get_last_trade_day = lambda: "2025-01-01"
_common.QSYS_ROOT = Path("/tmp/qsys_test")
_common.init_qlib = lambda: None
_common.load_json = lambda *a, **kw: {}

sys.modules["event_bus"].EventType = types.SimpleNamespace()  # 仅 import 绑定需要
sys.modules["event_bus"].bus = None
_reg = sys.modules["loopengine.regime"]  # from-import 的名字必须存在于 mock
_reg.detect_regime = _reg.detect_regime_from_reports = lambda *a, **kw: {}
_reg.get_regime_factor_weight = lambda *a, **kw: 1.0

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import random

from loopengine.tree import FIELD_INFO, TYPE_FIELDS, all_fields, field_table
from loopengine import llm_review as lr
from loopengine.engine import LoopEngine, _build_llm_prompt, _extract_sexpr


def test_field_info_completeness():
    """每个可出题字段都必须有语义条目（否则等于裸名回潮）。"""
    missing = [f for f in all_fields(None) if f not in FIELD_INFO]
    assert not missing, f"FIELD_INFO 缺字段: {missing}"


def test_field_table_scoped_by_type():
    t_mf = field_table("资金流")
    assert "main_net_pct" in t_mf and "主力净流入占比" in t_mf and "close" in t_mf
    assert "fin_np" not in t_mf, "资金流类型不应含财务字段"
    t_fin = field_table("财务")
    assert "fin_np" in t_fin and "极重尾" in t_fin


def test_extract_sexpr_robustness():
    good = "sub(ma(overnight,20),delta(ma(overnight,20),5))"
    assert _extract_sexpr(good) == good
    assert _extract_sexpr(f"```\n{good}\n```") == good
    assert _extract_sexpr(f"答案是：{good}") == good
    assert _extract_sexpr(f"{good}  # 动量衰减") == good
    assert _extract_sexpr(f"思考过程...\n{good}\n解释段落") == good
    assert _extract_sexpr("完全没有表达式") is None
    assert _extract_sexpr("") is None and _extract_sexpr(None) is None
    # 不平衡括号 → None（不返回半成品）
    assert _extract_sexpr("sub(ma(close,20)") is None


def test_build_prompt_contents():
    # _build_llm_prompt 现返回 (system, user) 元组：system 为前缀缓存稳定前缀，
    # user 为每次不同的变动内容（机制族/证据/few-shot/假设/理论）。
    system, user = _build_llm_prompt("动量", "该族覆盖极少", "资金流", "昨日证据：XXX\n",
                                     ["rank_cs(div(main_net_pct,amount))"])
    both = system + "\n" + user
    assert "「动量」" in both and "该族覆盖极少" in both
    assert "昨日证据：XXX" in both
    assert "主力净流入占比" in both, "prompt 应含字段语义"
    assert "rank_cs(div(main_net_pct,amount))" in both, "prompt 应含 few-shot"
    assert "因子类型：资金流" in both and "只输出一个 S 表达式" in both
    # 前缀缓存纪律：system 不得混入变动内容（证据/few-shot 只许在 user）
    assert "昨日证据" not in system and "rank_cs(" not in system
    # 无量价 hint（默认类型不注入类型提示）
    system2, user2 = _build_llm_prompt("反转", "why", "量价", "", [])
    assert "因子类型" not in system2 and "few" not in user2.lower()


def test_llm_review_json_extraction():
    assert lr._extract_json('{"verdict": "pass", "reason": "ok"}')["verdict"] == "pass"
    assert lr._extract_json('```json\n{"verdict": "reject", "reason": "x"}\n```')["verdict"] == "reject"
    # 推理模型前置思考文本
    assert lr._extract_json('让我想想……量纲有问题。\n{"verdict": "reject", "reason": "量纲"}')["verdict"] == "reject"
    assert lr._extract_json("没有 JSON") is None


def test_llm_review_no_key_fail_strict():
    ok, reason = lr.llm_review("sub(ma(close,20),delta(close,5))")
    assert reason == "no-llm-fallback"
    # 规则审查对合法表达式放行、对无窗口算子的表达式拒绝
    assert ok is True
    ok2, _ = lr.llm_review("div(close,open)")  # 无窗口算子
    assert ok2 is False


def test_llm_generate_no_key_returns_none_and_counts_nothing():
    eng = LoopEngine.__new__(LoopEngine)  # 绕过 __init__（无 key 时根本走不到 self.state）
    stats = {"llm_gen_fail": 0}
    assert eng._llm_generate(random.Random(1), ["动量"], [], "资金流", stats=stats) is None
    assert stats["llm_gen_fail"] == 0, "无 key 快速返回不应误计为生成失败"


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
