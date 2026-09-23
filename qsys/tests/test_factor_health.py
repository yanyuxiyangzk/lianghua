"""因子健康度元模型测试：PIT历史、IC状态识别、健康压缩排序与治理闸门。

合成数据约定：6 只股票的日收益为固定漂移率（C1 最强 … C6 最弱），
src_good 名单按强度降序打分（判别有效），src_bad 按强度升序打分（判别失效/过拟合）。
"""
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import datasource
import experience
import factor_health as fh

CODES = ["SZ000001", "SZ000002", "SZ000003", "SZ000004", "SZ000005", "SZ000006"]
DRIFTS = [0.006, 0.004, 0.002, 0.000, -0.002, -0.004]  # 日漂移：C1最强 C6最弱
N_DAYS = 260
DATES = pd.bdate_range("2025-06-02", periods=N_DAYS).strftime("%Y-%m-%d").tolist()
# 名单日：留足 IC 状态回看窗口与5日成熟期
PICK_DATES = DATES[120:200:6]
EVAL_DATE = DATES[205]


def _use_temp_dbs():
    temp = tempfile.TemporaryDirectory()
    old_mkt, old_exp = datasource.MKT_DB, experience.DB_PATH
    datasource.MKT_DB = Path(temp.name) / "market.db"
    experience.DB_PATH = Path(temp.name) / "experience.db"
    return temp, old_mkt, old_exp


def _restore_dbs(temp, old_mkt, old_exp):
    datasource.MKT_DB = old_mkt
    experience.DB_PATH = old_exp
    temp.cleanup()


def _seed_market():
    with datasource._conn() as c:
        # 指数：恒定微涨，提供交易日历与市场 regime
        for i, d in enumerate(DATES):
            c.execute("INSERT OR REPLACE INTO market_daily(source,code,date,close) "
                      "VALUES('ths_ifind','SH000001',?,?)", (d, 3000 * (1.0005 ** i)))
        for code, drift in zip(CODES, DRIFTS):
            for i, d in enumerate(DATES):
                c.execute("INSERT OR REPLACE INTO market_daily(source,code,date,close) "
                          "VALUES('ths_ifind',?,?,?)", (code, d, 100 * ((1 + drift) ** i)))


def _seed_picks(with_bad=True):
    """src_good: 分数降序=C1..C6（与真实强度同序）；src_bad: 分数降序=C6..C1（反序）。"""
    with experience._conn() as c:
        for k, day in enumerate(PICK_DATES):
            c.execute("INSERT OR REPLACE INTO picks"
                      "(id,combo_hash,trade_date,created_at,source,pool_name,pack_name) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (k + 1, f"good{k}", day, f"{day} 20:00:00", "src_good", "测试池", "包A"))
            for rank, code in enumerate(CODES, start=1):
                c.execute("INSERT OR REPLACE INTO pick_items(pick_id,code,rank,score) "
                          "VALUES(?,?,?,?)", (k + 1, code, rank, 10.0 - rank))
        if with_bad:
            base = len(PICK_DATES)
            for k, day in enumerate(PICK_DATES):
                c.execute("INSERT OR REPLACE INTO picks"
                          "(id,combo_hash,trade_date,created_at,source,pool_name,pack_name) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (base + k + 1, f"bad{k}", day, f"{day} 20:10:00", "src_bad",
                           "测试池", "包B"))
                for rank, code in enumerate(reversed(CODES), start=1):
                    c.execute("INSERT OR REPLACE INTO pick_items(pick_id,code,rank,score) "
                              "VALUES(?,?,?,?)", (base + k + 1, code, rank, 10.0 - rank))


def test_pit_history_excludes_future_lists():
    temp, old_mkt, old_exp = _use_temp_dbs()
    try:
        _seed_market()
        _seed_picks()
        cutoff = PICK_DATES[5]
        hist = fh._matured_items(cutoff)
        assert not hist.empty
        assert (hist["eval_date"] <= cutoff).all()
        assert (hist["trade_date"] < cutoff).all()
        assert hist["fwd_5d_return"].notna().all()
        # 好源 top 桶应明显跑赢中位
        good_top = hist[(hist["source"] == "src_good") & (hist["score_bucket"] == "top")]
        assert good_top["beat_median"].mean() > 0.8
    finally:
        _restore_dbs(temp, old_mkt, old_exp)
    print("PASS: test_pit_history_excludes_future_lists")


def test_trailing_ic_state_detects_decay():
    temp, old_mkt, old_exp = _use_temp_dbs()
    try:
        _seed_market()
        _seed_picks()
        hist = fh._matured_items(EVAL_DATE)
        asof = PICK_DATES[10]
        assert fh._trailing_ic_state(hist, "src_good", "包A", "测试池", asof) == "rising"
        assert fh._trailing_ic_state(hist, "src_bad", "包B", "测试池", asof) == "decaying"
        assert fh._trailing_ic_state(hist, "src_none", "包X", "池X", asof) == "unknown"
    finally:
        _restore_dbs(temp, old_mkt, old_exp)
    print("PASS: test_trailing_ic_state_detects_decay")


def test_health_estimate_shrinks_and_discriminates():
    temp, old_mkt, old_exp = _use_temp_dbs()
    try:
        _seed_market()
        _seed_picks()
        hist = fh._matured_items(EVAL_DATE)
        good_top, m1 = fh._estimate_health(hist, "top", "rising", "up")
        good_bottom, _m2 = fh._estimate_health(hist, "bottom", "rising", "up")
        assert good_top["shrunk"] > 0.5 > good_bottom["shrunk"]
        assert good_top["n"] >= fh.MIN_CELL_LISTS
        # 空历史必须退回先验 0.5，不装知道
        empty, m0 = fh._estimate_health(hist.iloc[0:0], "top", "rising", "up")
        assert empty["shrunk"] == 0.5 and empty["n"] == 0
    finally:
        _restore_dbs(temp, old_mkt, old_exp)
    print("PASS: test_health_estimate_shrinks_and_discriminates")


def test_shadow_update_evaluate_and_governance():
    temp, old_mkt, old_exp = _use_temp_dbs()
    try:
        _seed_market()
        _seed_picks()
        trade_date = PICK_DATES[12]
        result = fh.update_health_shadow(trade_date)
        assert result["rows"] == 2 * len(CODES)
        with datasource._conn() as c:
            rows = c.execute(
                "SELECT source,score_bucket,health,health_adjustment FROM factor_health_shadow",
                ).fetchall()
        # 好源 top 桶获得正修正，bottom 桶负修正
        for source, bucket, health, adj in rows:
            if source == "src_good" and bucket == "top":
                assert health > 0.5 and adj > 0
            if source == "src_good" and bucket == "bottom":
                assert health < 0.5 and adj < 0
        # 回填评估：eval_date 之后可查
        done = fh.evaluate_health(EVAL_DATE)
        assert done["evaluated"] == 2 * len(CODES)
        summary = fh.health_summary()
        assert summary["evaluated"] == 2 * len(CODES)
        # 坏源的反判别被健康压缩纠正 → 整体增益非负
        assert summary["lift"] is not None and summary["lift"] >= 0
        audit = fh.health_governance(EVAL_DATE, persist=False)
        assert audit["automatic_activation"] is False
        assert audit["status"] in ("shadow_continue", "eligible_for_manual_review")
    finally:
        _restore_dbs(temp, old_mkt, old_exp)
    print("PASS: test_shadow_update_evaluate_and_governance")


def test_empty_history_shadow_is_neutral():
    temp, old_mkt, old_exp = _use_temp_dbs()
    try:
        _seed_market()
        _seed_picks(with_bad=False)
        result = fh.update_health_shadow(PICK_DATES[0])  # 之前无成熟名单
        assert result["rows"] == len(CODES)
        with datasource._conn() as c:
            rows = c.execute(
                "SELECT health,health_adjustment FROM factor_health_shadow").fetchall()
        assert all(h == 0.5 and a == 0.0 for h, a in rows)
    finally:
        _restore_dbs(temp, old_mkt, old_exp)
    print("PASS: test_empty_history_shadow_is_neutral")


def test_upsert_preserves_evaluated_columns():
    temp, old_mkt, old_exp = _use_temp_dbs()
    try:
        _seed_market()
        _seed_picks(with_bad=False)
        trade_date = PICK_DATES[8]
        fh.update_health_shadow(trade_date)
        fh.evaluate_health(EVAL_DATE)
        with datasource._conn() as c:
            before = c.execute(
                "SELECT fwd_5d_return,evaluated_at FROM factor_health_shadow "
                "WHERE code='SZ000001'").fetchone()
        assert before[0] is not None and before[1] is not None
        fh.update_health_shadow(trade_date)  # 重跑不得清掉回填结果
        with datasource._conn() as c:
            after = c.execute(
                "SELECT fwd_5d_return,evaluated_at FROM factor_health_shadow "
                "WHERE code='SZ000001'").fetchone()
        assert after == before
    finally:
        _restore_dbs(temp, old_mkt, old_exp)
    print("PASS: test_upsert_preserves_evaluated_columns")


def test_pack_shadow_evidence_gate():
    temp, old_mkt, old_exp = _use_temp_dbs()
    try:
        _seed_market()
        _seed_picks()
        # 逐日回放影子（PIT），再统一回填评估
        for d in PICK_DATES:
            fh.update_health_shadow(d)
        fh.evaluate_health(EVAL_DATE)
        # 好源（包A）：名单内判别力显著为正且组数达标 → 允许转正
        ev_good = fh.pack_shadow_evidence("包A")
        assert ev_good["ok"] and ev_good["disc"] > 0
        assert ev_good["groups"] >= 8
        # 坏源（包B）：判别力为负 → 拒绝转正
        ev_bad = fh.pack_shadow_evidence("包B")
        assert not ev_bad["ok"] and ev_bad["disc"] < 0
        # 无证据的包：保守拒绝
        ev_none = fh.pack_shadow_evidence("不存在的包")
        assert not ev_none["ok"] and ev_none["groups"] == 0
    finally:
        _restore_dbs(temp, old_mkt, old_exp)
    print("PASS: test_pack_shadow_evidence_gate")


if __name__ == "__main__":
    tests = [test_pit_history_excludes_future_lists,
             test_trailing_ic_state_detects_decay,
             test_health_estimate_shrinks_and_discriminates,
             test_shadow_update_evaluate_and_governance,
             test_empty_history_shadow_is_neutral,
             test_upsert_preserves_evaluated_columns,
             test_pack_shadow_evidence_gate]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:
            failed += 1
            print(f"FAIL: {test.__name__}: {exc}")
    print(f"结果: {len(tests) - failed} 通过, {failed} 失败, 共 {len(tests)} 个")
    raise SystemExit(1 if failed else 0)
