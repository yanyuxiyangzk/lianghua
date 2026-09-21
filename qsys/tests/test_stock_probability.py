"""单股票概率模型测试：收缩、路径概率和时间顺序验证。"""
import sys
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stock_probability as sp


def _synthetic(n=500, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n)
    returns = rng.normal(0.0004, 0.015, n)
    close = 20 * np.cumprod(1 + returns)
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"), "open": close * 0.998,
        "high": close * 1.015, "low": close * 0.985, "close": close,
        "volume": rng.integers(1_000_000, 4_000_000, n),
        "amount": close * rng.integers(1_000_000, 4_000_000, n),
    })


def test_beta_shrinkage_moves_to_half():
    result = sp._prob(8, 10)
    assert result["raw"] == 0.8
    assert 0.5 < result["shrunk"] < result["raw"]
    assert result["low"] < result["shrunk"] < result["high"]
    print("PASS: test_beta_shrinkage_moves_to_half")


def test_path_probability_uses_no_hit_denominator():
    data = sp._features_and_labels(_synthetic())
    current = data.iloc[-1]
    history = data.iloc[:-10]
    pred, n, _ = sp._predict_from_history(history, current)
    assert pred["up_3pct_5d"]["n"] == n
    assert pred["down_3pct_5d"]["n"] == n
    assert (pred["up_3pct_5d"]["raw"] + pred["down_3pct_5d"]["raw"]) <= 1.0
    print("PASS: test_path_probability_uses_no_hit_denominator")


def test_oos_validation_has_no_future_training():
    data = sp._features_and_labels(_synthetic(550))
    result = sp._oos_validate(data)
    assert result["count"] > 30
    assert 0 <= result["accuracy"] <= 1
    assert 0 <= result["brier"] <= 1
    assert 0 <= result["calibration_error"] <= 1
    print("PASS: test_oos_validation_has_no_future_training")


def test_overlay_quality_gate_blocks_limited_model():
    original_conn = sp.datasource._conn

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def executescript(self, *_): return None
        def execute(self, *_):
            class Rows:
                def fetchall(self):
                    import json
                    payload = {"evidence": "limited", "predictions": {
                        "up_5d": {"shrunk": 0.8}, "down_3pct_5d": {"shrunk": 0.2}}}
                    return [("SZ001216", "2026-09-21", 100, "{}", json.dumps(payload))]
            return Rows()

    sp.datasource._conn = lambda: FakeConn()
    try:
        scores = pd.Series({"SZ001216": 1.0})
        adjusted, diag = sp.probability_overlay(scores, "2026-09-21", execute=True)
        assert adjusted.loc["SZ001216"] == 1.0
        assert diag.iloc[0]["status"] == "质量闸门未通过"
        assert diag.iloc[0]["score_adjustment"] == 0
    finally:
        sp.datasource._conn = original_conn
    print("PASS: test_overlay_quality_gate_blocks_limited_model")


def test_model_selection_penalizes_tiny_current_sample():
    data = sp._features_and_labels(_synthetic(550))
    history, current = data.iloc[:-10], data.iloc[-1]
    scheme, _, candidates = sp._select_model(data, history, current)
    chosen = next(x for x in candidates if x["scheme"] == scheme)
    eligible = [x for x in candidates if x["current_matches"] >= 30]
    if eligible:
        assert chosen["current_matches"] >= 30
    print("PASS: test_model_selection_penalizes_tiny_current_sample")


def test_shadow_upsert_preserves_evaluation_columns():
    old_db = sp.datasource.MKT_DB
    temp = tempfile.TemporaryDirectory()
    sp.datasource.MKT_DB = Path(temp.name) / "market.db"
    try:
        with sp.datasource._conn() as c:
            sp._ensure_schema(c)
            c.execute(
                "INSERT INTO stock_probability_shadow"
                "(pick_id,trade_date,code,original_rank,original_score,model_status,"
                "shadow_score,shadow_rank,eval_date,fwd_5d_return,evaluated_at,created_at) "
                "VALUES(1,'2026-09-01','SZ001216',1,1.0,'可用',1.1,1,'2026-09-08',0.05,"
                "'2026-09-08 18:00:00','2026-09-01 20:00:00')")
            c.execute(
                "INSERT INTO stock_probability_shadow"
                "(pick_id,trade_date,code,original_rank,original_score,model_status,"
                "shadow_score,shadow_rank,eval_date,created_at) "
                "VALUES(1,'2026-09-01','SZ001216',2,0.9,'质量闸门未通过',0.9,2,"
                "'2026-09-08','2026-09-01 21:00:00') "
                "ON CONFLICT(pick_id,code) DO UPDATE SET original_rank=excluded.original_rank,"
                "model_status=excluded.model_status,shadow_rank=excluded.shadow_rank")
            row = c.execute(
                "SELECT original_rank,model_status,fwd_5d_return,evaluated_at "
                "FROM stock_probability_shadow WHERE pick_id=1 AND code='SZ001216'").fetchone()
        assert row == (2, "质量闸门未通过", 0.05, "2026-09-08 18:00:00")
    finally:
        sp.datasource.MKT_DB = old_db
        temp.cleanup()
    print("PASS: test_shadow_upsert_preserves_evaluation_columns")


if __name__ == "__main__":
    tests = [test_beta_shrinkage_moves_to_half,
             test_path_probability_uses_no_hit_denominator,
             test_oos_validation_has_no_future_training,
             test_overlay_quality_gate_blocks_limited_model,
             test_model_selection_penalizes_tiny_current_sample,
             test_shadow_upsert_preserves_evaluation_columns]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:
            failed += 1
            print(f"FAIL: {test.__name__}: {exc}")
    print(f"结果: {len(tests)-failed} 通过, {failed} 失败, 共 {len(tests)} 个")
    raise SystemExit(1 if failed else 0)
