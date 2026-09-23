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
    assert pred["up_atr_5d"]["n"] == n
    assert pred["down_atr_5d"]["n"] == n
    assert (pred["up_atr_5d"]["raw"] + pred["down_atr_5d"]["raw"]) <= 1.0
    print("PASS: test_path_probability_uses_no_hit_denominator")


def test_oos_validation_has_no_future_training():
    data = sp._features_and_labels(_synthetic(550))
    result = sp._oos_validate(data)
    assert result["count"] > 30
    assert 0 <= result["accuracy"] <= 1
    assert 0 <= result["brier"] <= 1
    assert 0 <= result["calibration_error"] <= 1
    assert "path_auc" in result
    if result["path_auc"] is not None:
        assert 0 <= result["path_auc"] <= 1
    print("PASS: test_oos_validation_has_no_future_training")


def test_oos_multifold_covers_more_points_than_single_split():
    # 多窗口 walk-forward 的样本外点数必须明显多于原单次 80/20 切分
    data = sp._features_and_labels(_synthetic(550))
    result = sp._oos_validate(data)
    single_split_max = len(data) - 5 - max(80, int(len(data) * 0.8))
    assert result["count"] > single_split_max * 1.5
    print("PASS: test_oos_multifold_covers_more_points_than_single_split")


def test_kernel_matching_uses_continuous_features_and_ess():
    rng = np.random.default_rng(11)
    n = 500
    # 构造动量延续的合成序列：ret_5 高则未来5日大概率上涨
    dates = pd.bdate_range("2024-01-01", periods=n)
    ret = rng.normal(0, 0.012, n)
    for i in range(5, n - 5):
        if ret[i - 5:i].sum() > 0.03:
            ret[i + 1:i + 5] += 0.004
    close = 20 * np.cumprod(1 + ret)
    daily = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"), "open": close * 0.998,
        "high": close * 1.015, "low": close * 0.985, "close": close,
        "volume": rng.integers(1_000_000, 4_000_000, n),
        "amount": close * rng.integers(1_000_000, 4_000_000, n)})
    data = sp._features_and_labels(daily)
    history, current = data.iloc[:-10], data.iloc[-1]
    pred, ess, method = sp._predict_from_history(history, current, "kernel")
    assert ess > 10
    assert "软匹配" in method
    assert 0 < pred["up_5d"]["shrunk"] < 1
    assert 0 < pred["down_atr_5d"]["shrunk"] < 1
    # kernel 候选必须参与模型选择
    _, _, candidates = sp._select_model(data, history, current)
    kc = [c for c in candidates if c["scheme"] == "kernel"]
    assert kc and kc[0]["current_matches"] > 10
    # 当前特征缺失时 kernel 候选自动跳过
    bad = current.copy()
    bad["ret_5"] = np.nan
    _, _, candidates2 = sp._select_model(data, history, bad)
    assert "kernel" not in {c["scheme"] for c in candidates2}
    print("PASS: test_kernel_matching_uses_continuous_features_and_ess")


def test_orderbook_state_joins_and_scheme_participates():
    daily = _synthetic(550)
    ob = pd.DataFrame({
        "trade_date": daily["date"].tail(300),
        "ob_imbalance_close": 0.2, "ob_imbalance_mean": 0.1,
        "spread_median": 0.001, "seal_strength_close": 0.01,
        "auction_imbalance": 0.05,
    })
    data = sp._features_and_labels(daily, None, None, ob)
    assert "ob_imbalance_state" in data.columns
    assert data["ob_imbalance_state"].notna().sum() == 300
    current = data.iloc[-1]
    _, _, candidates = sp._select_model(data, data.iloc[:-10], current)
    assert "ob_balanced" in {c["scheme"] for c in candidates}
    matched, _label = sp._similar(data.iloc[:-10], current, "ob_balanced")
    assert (matched["ob_imbalance_state"].astype(str) == "buy").all()
    # 无盘口数据时候选自动跳过
    plain = sp._features_and_labels(daily)
    _, _, candidates2 = sp._select_model(plain, plain.iloc[:-10], plain.iloc[-1])
    assert "ob_balanced" not in {c["scheme"] for c in candidates2}
    print("PASS: test_orderbook_state_joins_and_scheme_participates")


def test_intraday_candidates_join_without_dropping_daily_history():
    daily = _synthetic(550)
    minute = pd.DataFrame({
        "trade_date": daily["date"].tail(240).to_numpy(), "minute_count": 241,
        "open_ret_30m": 0.002, "morning_ret": 0.004, "afternoon_ret": 0.003,
        "tail_ret_30m": 0.001, "realized_vol": 0.015,
        "max_intraday_drawdown": -0.01, "close_vwap_gap": 0.006,
        "morning_volume_share": 0.55, "tail_volume_share": 0.12,
        "up_minute_ratio": 0.58,
    })
    data = sp._features_and_labels(daily, minute)
    assert len(data) > 500
    assert data["intraday_direction_state"].notna().sum() == 240
    current = data.iloc[-1]
    matched, label = sp._similar(data.iloc[:-10], current, "intraday_broad")
    assert label.startswith("日线趋势")
    assert isinstance(matched, pd.DataFrame)
    print("PASS: test_intraday_candidates_join_without_dropping_daily_history")


def test_market_regime_joins_and_new_schemes_participate():
    daily = _synthetic(550)
    market = pd.DataFrame({
        "date": daily["date"],
        "market_trend_state": ["up"] * 550,
        "market_vol_state": ["mid"] * 550,
    })
    data = sp._features_and_labels(daily, None, market)
    assert data["market_trend_state"].notna().all()
    current = data.iloc[-1]
    # 含 regime 的候选在当前状态齐全时必须参与比较
    scheme, _, candidates = sp._select_model(data, data.iloc[:-10], current)
    names = {c["scheme"] for c in candidates}
    assert "regime_balanced" in names
    matched, label = sp._similar(data.iloc[:-10], current, "regime_balanced")
    assert (matched["market_trend_state"].astype(str) == "up").all()
    # 无市场数据时 regime 候选自动跳过，不报错
    plain = sp._features_and_labels(daily)
    scheme2, _, candidates2 = sp._select_model(plain, plain.iloc[:-10], plain.iloc[-1])
    assert "regime_balanced" not in {c["scheme"] for c in candidates2}
    print("PASS: test_market_regime_joins_and_new_schemes_participate")


def test_overlay_quality_gate_blocks_limited_model():
    original_conn = sp.datasource._conn

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def executescript(self, *_): return None
        def execute(self, *_):
            class Rows:
                def __iter__(self): return iter([])  # PRAGMA 迁移路径
                def fetchall(self):
                    import json
                    payload = {"evidence": "limited", "predictions": {
                        "up_5d": {"shrunk": 0.8}, "down_atr_5d": {"shrunk": 0.2}}}
                    return [("SZ001216", "2026-09-21", 100, "{}", json.dumps(payload),
                             json.dumps({"atr_pct": 0.05}))]
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


def test_overlay_down_path_veto_is_asymmetric():
    original_conn = sp.datasource._conn

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def executescript(self, *_): return None
        def execute(self, *_):
            class Rows:
                def __iter__(self): return iter([])  # PRAGMA 迁移路径
                def fetchall(self):
                    import json
                    payload = {"evidence": "sufficient", "predictions": {
                        "up_5d": {"shrunk": 0.55}, "down_atr_5d": {"shrunk": 0.575}}}
                    state = {"atr_pct": 0.05}
                    return [("SZ001216", "2026-09-21", 100, "{}", json.dumps(payload),
                             json.dumps(state))]
            return Rows()

    sp.datasource._conn = lambda: FakeConn()
    try:
        scores = pd.Series({"SZ001216": 1.0, "SZ000002": 1.2})
        adjusted, diag = sp.probability_overlay(scores, "2026-09-21", execute=True)
        d = diag.set_index("code").loc["SZ001216"]
        # up_edge=0.05, down_edge=0.075 → edge=0.05-2×0.075=-0.10 → 负修正
        assert d["status"] == "可用"
        assert d["score_adjustment"] < 0
        assert adjusted.loc["SZ001216"] < 1.0
        # 无模型的票不受影响
        assert adjusted.loc["SZ000002"] == 1.2
    finally:
        sp.datasource._conn = original_conn
    print("PASS: test_overlay_down_path_veto_is_asymmetric")


def test_match_detail_replays_stored_model_scheme():
    old_db = sp.datasource.MKT_DB
    temp = tempfile.TemporaryDirectory()
    sp.datasource.MKT_DB = Path(temp.name) / "market.db"
    try:
        daily = _synthetic(550)
        with sp.datasource._conn() as c:
            for r in daily.itertuples():
                c.execute(
                    "INSERT OR REPLACE INTO market_daily"
                    "(source,code,date,open,high,low,close,volume,amount) "
                    "VALUES('ths_ifind','SZ001216',?,?,?,?,?,?,?)",
                    (r.date, r.open, r.high, r.low, r.close, r.volume, r.amount))
        model = sp.build_model("SZ001216")
        detail = sp.get_match_detail("SZ001216")
        assert detail is not None
        assert detail["scheme"] == model["selected_scheme"]
        assert not detail["matched"].empty
        for col in ("date", "close", "fwd_5", "path_5"):
            assert col in detail["matched"].columns
        assert len(detail["kline"]) == 550
        # 样本明细条数与模型口径一致（kernel 展示 top25% 近邻，其余为全量匹配）
        if detail["scheme"] != "kernel":
            assert detail["matched_total"] == model["sample_count"]
        else:
            assert "weight" in detail["matched"].columns
        inv = sp.data_inventory("SZ001216")
        daily_row = inv[inv["数据层"] == "日线（前复权）"].iloc[0]
        assert daily_row["记录数"] == 550
    finally:
        sp.datasource.MKT_DB = old_db
        temp.cleanup()
    print("PASS: test_match_detail_replays_stored_model_scheme")


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


def test_governance_requires_enough_groups():
    original = sp._group_shadow_lifts
    sp._group_shadow_lifts = lambda: pd.DataFrame([
        {"trade_date": "2026-09-01", "pick_id": 1, "original_return": 0.01,
         "shadow_return": 0.02, "lift": 0.01, "candidate_count": 10,
         "usable_count": 10}
    ])
    try:
        result = sp.governance_audit("2026-09-21", persist=False)
        assert result["status"] == "shadow_continue"
        assert result["automatic_activation"] is False
        assert result["reasons"][0]["passed"] is False
    finally:
        sp._group_shadow_lifts = original
    print("PASS: test_governance_requires_enough_groups")


def test_governance_only_allows_manual_review():
    original = sp._group_shadow_lifts
    rows = []
    for i in range(40):
        lift = 0.01 + (i % 3) * 0.001
        rows.append({"trade_date": f"2026-08-{(i % 28) + 1:02d}", "pick_id": i,
                     "original_return": 0.005, "shadow_return": 0.005 + lift,
                     "baseline_return": 0.005 + lift * 0.5,
                     "lift": lift, "candidate_count": 10, "usable_count": 8})
    sp._group_shadow_lifts = lambda: pd.DataFrame(rows)
    try:
        result = sp.governance_audit("2026-09-21", persist=False,
                                     cfg={"bootstrap_samples": 500})
        assert result["status"] == "eligible_for_manual_review"
        assert result["automatic_activation"] is False
        assert all(x["passed"] for x in result["reasons"])
        # overlay 不如 ATR 基线时，基线对照检查必须拦截晋级
        for row in rows:
            row["baseline_return"] = row["shadow_return"] + 0.01
        result2 = sp.governance_audit("2026-09-21", persist=False,
                                      cfg={"bootstrap_samples": 500})
        assert result2["status"] == "shadow_continue"
        assert result2["reasons"][-1]["passed"] is False
    finally:
        sp._group_shadow_lifts = original
    print("PASS: test_governance_only_allows_manual_review")


def test_llm_evidence_is_compact_and_stock_bound():
    metrics = json.dumps({"count": 40, "brier": .24, "accuracy": .55,
                          "calibration_error": .1})
    state = json.dumps({"trend_state": "up", "ret_5": .03, "unknown": 999})
    pred = json.dumps({"evidence": "limited", "selected_scheme": "broad",
                       "intraday_days": 200, "uses_intraday": False,
                       "predictions": {"up_5d": {"shrunk": .6, "low": .5,
                                                  "high": .7, "n": 80}},
                       "model_candidates": []})
    evidence = sp._llm_profile_evidence(
        (1, 9, "SZ001216", sp.MODEL_VERSION, "2026-09-21", "2024-01-01",
         "2026-09-07", 80, 40, metrics, state, pred, "2026-09-22 10:00:00"))
    assert evidence["code"] == "SZ001216"
    assert evidence["evidence"] == "limited"
    assert evidence["predictions"]["up_5d"]["probability"] == .6
    assert "unknown" not in evidence["state"]
    print("PASS: test_llm_evidence_is_compact_and_stock_bound")


def test_intraday_model_candidates_require_minimum_walk_forward_and_matches():
    data = _synthetic(550)
    minute = pd.DataFrame({
        "trade_date": data["date"].tail(240).to_numpy(), "minute_count": 241,
        "morning_ret": .004, "afternoon_ret": .003, "close_vwap_gap": .006,
        "up_minute_ratio": .58,
    })
    features = sp._features_and_labels(data, minute)
    history, current = features.iloc[:-10], features.iloc[-1]
    _, _, candidates = sp._select_model(features, history, current)
    intraday = [x for x in candidates if x["scheme"].startswith("intraday_")]
    assert intraday
    assert all(x["objective"] >= 1.0 for x in intraday if
               x["count"] < 30 or x["current_matches"] < 30)
    print("PASS: test_intraday_model_candidates_require_minimum_walk_forward_and_matches")


if __name__ == "__main__":
    tests = [test_beta_shrinkage_moves_to_half,
             test_path_probability_uses_no_hit_denominator,
             test_oos_validation_has_no_future_training,
             test_oos_multifold_covers_more_points_than_single_split,
             test_kernel_matching_uses_continuous_features_and_ess,
             test_orderbook_state_joins_and_scheme_participates,
             test_intraday_candidates_join_without_dropping_daily_history,
             test_market_regime_joins_and_new_schemes_participate,
             test_overlay_quality_gate_blocks_limited_model,
             test_overlay_down_path_veto_is_asymmetric,
             test_match_detail_replays_stored_model_scheme,
             test_model_selection_penalizes_tiny_current_sample,
             test_shadow_upsert_preserves_evaluation_columns,
             test_governance_requires_enough_groups,
             test_governance_only_allows_manual_review]
    tests.extend([test_llm_evidence_is_compact_and_stock_bound,
                  test_intraday_model_candidates_require_minimum_walk_forward_and_matches])
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:
            failed += 1
            print(f"FAIL: {test.__name__}: {exc}")
    print(f"结果: {len(tests)-failed} 通过, {failed} 失败, 共 {len(tests)} 个")
    raise SystemExit(1 if failed else 0)
