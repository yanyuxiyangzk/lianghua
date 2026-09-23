"""理论发现引擎测试：注册闭环与 LLM prompt 前缀缓存纪律。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import datasource
from loopengine.theory_discovery import (HypothesisGenerator, TheoryNamer,
                                         _extract_hypotheses, register_theory_factor)


def test_register_theory_factor_closes_loop():
    temp = tempfile.TemporaryDirectory()
    old = datasource.MKT_DB
    datasource.MKT_DB = Path(temp.name) / "market.db"
    try:
        ok = register_theory_factor(
            "尾盘缩量", "mul(zscore(close,20),sign(delta(close,5)))",
            "量价", {"ic_mean": 0.02})
        assert ok
        with datasource._conn() as c:
            row = c.execute(
                "SELECT name,kind,engine,code,gate_status FROM factor_registry "
                "WHERE name='theory_尾盘缩量'").fetchone()
        assert row is not None, "理论因子未注册进 factor_registry"
        assert row[1] == "loopengine" and row[2] == "theory"
        assert row[3].startswith("# sexpr: mul(zscore(close,20)")
        assert row[4] in (None, ""), "gate_status 必须为 NULL（待 gate_check 评估）"
    finally:
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_register_theory_factor_closes_loop")


def test_llm_prompt_prefix_cache_discipline():
    # system prompt 为模块常量（DeepSeek 前缀缓存的稳定前缀），含算子约束
    for prompt in (HypothesisGenerator.SYSTEM_PROMPT, TheoryNamer.SYSTEM_PROMPT):
        assert isinstance(prompt, str) and len(prompt) > 50
    assert "S表达式" in HypothesisGenerator.SYSTEM_PROMPT
    print("PASS: test_llm_prompt_prefix_cache_discipline")


def test_extract_hypotheses_salvages_truncated_json():
    # 完整包：正常解析
    full = '{"hypotheses": [{"name": "a", "sexpr": "ma(close,20)"}]}'
    assert len(_extract_hypotheses(full)) == 1
    # max_tokens 截断：第二个对象不完整 → 挽救第一个完整对象
    truncated = ('{"hypotheses": [{"name": "a", "sexpr": "ma(close,20)", "confidence": 0.6}, '
                 '{"name": "b", "sexpr": "zscore(clo')
    got = _extract_hypotheses(truncated)
    assert len(got) == 1 and got[0]["sexpr"] == "ma(close,20)"
    # 空文本/无对象：空列表不抛异常
    assert _extract_hypotheses("") == []
    assert _extract_hypotheses("no json here") == []
    print("PASS: test_extract_hypotheses_salvages_truncated_json")


if __name__ == "__main__":
    failed = 0
    for t in (test_register_theory_factor_closes_loop,
              test_llm_prompt_prefix_cache_discipline,
              test_extract_hypotheses_salvages_truncated_json):
        try:
            t()
        except Exception as exc:
            failed += 1
            print(f"FAIL: {t.__name__}: {exc}")
    print(f"结果: {3 - failed} 通过, {failed} 失败, 共 3 个")
    raise SystemExit(1 if failed else 0)
