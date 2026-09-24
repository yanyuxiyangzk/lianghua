"""理论发现引擎测试：注册闭环与 LLM prompt 前缀缓存纪律。"""
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR'] = _TMP.name
import datasource
from loopengine.theory_discovery import (HypothesisGenerator, TheoryNamer,
                                         _extract_hypotheses, _filter_falsified,
                                         load_theory_track_record,
                                         register_theory_factor)


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


def test_track_record_and_falsified_filter_and_prompt():
    temp = tempfile.TemporaryDirectory()
    old = datasource.MKT_DB
    datasource.MKT_DB = Path(temp.name) / "market.db"
    try:
        # 种两个理论因子：一个过闸一个被拒（附拒绝原因）
        assert register_theory_factor("尾盘缩量", "mul(zscore(close,20),sign(delta(close,5)))",
                                      "量价", {})
        assert register_theory_factor("跳空高开", "mul(zscore(open,20),sign(delta(open,5)))",
                                      "量价", {})
        import library
        with library._lconn() as c:
            c.execute("UPDATE factor_registry SET gate_status=1 WHERE name='theory_尾盘缩量'")
            c.execute("UPDATE factor_registry SET gate_status=0 WHERE name='theory_跳空高开'")
            c.execute("""CREATE TABLE IF NOT EXISTS gate_detail_log (
                factor_name TEXT, gate_date TEXT, pool_name TEXT, metrics TEXT,
                passed INTEGER, fail_reasons TEXT, created_at TEXT,
                PRIMARY KEY(factor_name, gate_date, pool_name))""")
            c.execute("INSERT INTO gate_detail_log"
                      "(factor_name,gate_date,pool_name,metrics,passed,fail_reasons,created_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      ("theory_跳空高开", "2026-09-23", "沪深300", "{}", 0,
                       "|IC| 0.003 < 0.02", "2026-09-23 18:00:00"))

        rec = load_theory_track_record()
        assert [e["name"] for e in rec["effective"]] == ["theory_尾盘缩量"]
        assert [f["name"] for f in rec["falsified"]] == ["theory_跳空高开"]
        assert "IC" in rec["falsified"][0]["reason"]

        # 骨架去重：同构（仅窗口不同）的假说被丢弃，异构保留
        hyps = [{"name": "新假说A", "sexpr": "mul(zscore(open,30),sign(delta(open,10)))"},
                {"name": "新假说B", "sexpr": "ma(volume, 20)"}]
        kept = _filter_falsified(hyps, rec)
        assert [h["name"] for h in kept] == ["新假说B"]

        # prompt 注入：战绩在 user 段，且绝不进 system（前缀纪律）
        captured = {}
        import llmutil
        original = llmutil.llm_chat
        llmutil.llm_chat = lambda system, user, **kw: (
            captured.update(system=system, user=user) or '{"hypotheses": []}')
        try:
            HypothesisGenerator.generate(
                [{"type": "尾盘缩量", "description": "尾盘缩量上涨", "severity": 0.7}],
                {}, rec, budget=__import__("theory_policy").Budget("test-hypothesis"))
        finally:
            llmutil.llm_chat = original
        assert "已被证伪" in captured["user"] and "已验证有效" in captured["user"]
        assert "已被证伪" not in captured["system"]
    finally:
        datasource.MKT_DB = old
        temp.cleanup()
    print("PASS: test_track_record_and_falsified_filter_and_prompt")


if __name__ == "__main__":
    failed = 0
    for t in (test_register_theory_factor_closes_loop,
              test_llm_prompt_prefix_cache_discipline,
              test_extract_hypotheses_salvages_truncated_json,
              test_track_record_and_falsified_filter_and_prompt):
        try:
            t()
        except Exception as exc:
            failed += 1
            print(f"FAIL: {t.__name__}: {exc}")
    print(f"结果: {4 - failed} 通过, {failed} 失败, 共 4 个")
    raise SystemExit(1 if failed else 0)
