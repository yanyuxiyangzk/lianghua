"""cs_norm / resolve_norms 单测（typed_v2 缺口 1/2 的核心语义）。

cs_norm 走真实 signals（只 mock common）；resolve_norms 走真实 library
（mock common/datasource，_registry_rows 用合成行 monkeypatch）；
norm 列迁移用真实临时 sqlite 验证。
"""

import os
import sqlite3
import sys
import types
from pathlib import Path

os.environ["QSYS_ROOT"] = "/tmp/qsys_test"

# ---- Mock common / datasource（signals 与 library 顶层各只需这两个）----
for mod_name in ["common", "datasource"]:
    sys.modules[mod_name] = types.ModuleType(mod_name)

import json as _json
_common = sys.modules["common"]
_common.DATA_DIR = Path("/tmp/qsys_test")
_common.QLIB_DATA_DIR = Path("/tmp/qsys_test")
_common.init_qlib = lambda: None
# 与真实 common.load_json 同语义（真读文件，不存在才退 default）——开关文件测试依赖真实读盘
_common.load_json = lambda p, default=None: (_json.loads(Path(p).read_text())
                                             if Path(p).exists()
                                             else (default if default is not None else {}))
_common.get_last_trade_day = lambda: "2025-01-01"

_MIG_DB = "/tmp/qsys_test_mig.db"
sys.modules["datasource"]._qconn = lambda: sqlite3.connect(_MIG_DB)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd
import signals as sig
import library


def _cross(vals) -> pd.Series:
    return pd.Series(vals, index=[f"S{i:03d}" for i in range(len(vals))])


# ---------------------------------------------------------------- cs_norm
def test_zscore_path_matches_legacy_when_n_large():
    rng = np.random.RandomState(7)
    x = _cross(rng.randn(50))
    pd.testing.assert_series_equal(sig.cs_norm(x, "zscore"), sig.zscore(x))


def test_rank_z_is_centered_bounded_monotonic():
    rng = np.random.RandomState(11)
    x = _cross(rng.randn(100))
    z = sig.cs_norm(x, "rank")
    assert abs(float(z.mean())) < 1e-9, "秩的 z 分应天然居中"
    assert float(z.abs().max()) <= 3.0 + 1e-9
    order = x.sort_values().index
    assert list(z[order].round(6)) == sorted(z[order].round(6)), "秩变换应保持单调"


def test_rank_z_ties_do_not_shrink_variance():
    """50% 并列（20 个 0 + 20 个 1）：线性定标 (p-0.5)×2√3 下实现 σ≈0.83 被压缩；
    秩的 z 分按实现离散度重标定，σ 必须仍为 1（专家评审裁决 1 的核心收益）。"""
    x = _cross([0.0] * 20 + [1.0] * 20)
    z = sig.cs_norm(x, "rank")
    assert abs(float(z.std()) - 1.0) < 1e-9, f"ties 日实现 σ 被压缩: {z.std()}"
    u = z.unique()
    assert len(u) == 2 and abs(float(u[0] + u[1])) < 1e-9, "两组值应关于 0 对称"


def test_small_cross_section_downgrades_to_rank():
    rng = np.random.RandomState(13)
    x = _cross(rng.randn(29))  # N=29 < CS_ZSCORE_MIN_N
    pd.testing.assert_series_equal(sig.cs_norm(x, "zscore"), sig.cs_norm(x, "rank"))


def test_threshold_boundary_30_keeps_zscore():
    rng = np.random.RandomState(17)
    x = _cross(rng.randn(30))  # 恰好到阈值
    pd.testing.assert_series_equal(sig.cs_norm(x, "zscore"), sig.zscore(x))


def test_circuit_breaker_n_below_3():
    for n in (0, 1, 2):
        z = sig.cs_norm(_cross([1.0, 2.0][:n] if n else []), "zscore")
        assert z.isna().all(), f"N={n} 应熔断为全 NaN"
        z = sig.cs_norm(_cross([1.0, 2.0][:n] if n else []), "rank")
        assert z.isna().all(), f"N={n} rank 同样熔断（N=1 时 rank 会白送满分）"


def test_unknown_method_raises():
    try:
        sig.cs_norm(_cross([1.0, 2.0, 3.0, 4.0]), "rnak")
        raise AssertionError("未知 method 应抛 ValueError")
    except ValueError:
        pass


def test_nan_dropped_before_norm():
    x = _cross([1.0, 2.0, 3.0, 4.0, np.nan])
    z = sig.cs_norm(x, "zscore")
    assert z.isna().sum() == 0 and len(z) == 4, "NaN 应先剔除再归一"


# ---------------------------------------------------------------- resolve_norms
def _with_rows(rows: dict):
    library._reg_rows_cache["date"] = None  # 失效按日缓存
    library._registry_rows = lambda: rows
def test_resolve_manual_override_wins():
    _with_rows({"fin_np": {"kind": "builtin", "factor_type": "量价", "norm": "rank"}})
    assert library.resolve_norms(["fin_np"])["fin_np"] == "rank"


def test_resolve_factor_type_mapping():
    _with_rows({
        "f_fin": {"kind": "loopengine", "factor_type": "财务", "norm": None},
        "f_mf": {"kind": "loopengine", "factor_type": "资金流", "norm": None},
        "f_evt": {"kind": "loopengine", "factor_type": "事件记忆", "norm": None},
        "f_pv": {"kind": "loopengine", "factor_type": "量价", "norm": None},
        "f_sr": {"kind": "loopengine", "factor_type": "支撑阻力", "norm": None},
    })
    out = library.resolve_norms(["f_fin", "f_mf", "f_evt", "f_pv", "f_sr"])
    assert out == {"f_fin": "rank", "f_mf": "rank", "f_evt": "rank",
                   "f_pv": "zscore", "f_sr": "zscore"}


def test_resolve_invalid_override_falls_through():
    _with_rows({"f_x": {"kind": "loopengine", "factor_type": "财务", "norm": "rnak"}})
    assert library.resolve_norms(["f_x"])["f_x"] == "rank", "非法覆盖应回退类型映射"


def test_resolve_unknown_registry_row_and_names_fallback():
    _with_rows({"f_y": {"kind": "loopengine", "factor_type": "不存在", "norm": None}})
    out = library.resolve_norms(["f_y", "mom_5d", "rsi_6", "完全不存在的因子"])
    assert out["f_y"] == "zscore"          # 未知类型兜底
    assert out["mom_5d"] == "zscore"       # 内置目录名称推断
    assert out["rsi_6"] == "zscore"        # 技术指标名称推断
    assert out["完全不存在的因子"] == "zscore"  # 全兜底


def test_resolve_registry_failure_returns_zscore():
    library._registry_rows = lambda: (_ for _ in ()).throw(RuntimeError("db locked"))
    assert library.resolve_norms(["whatever"])["whatever"] == "zscore"


# ---------------------------------------------------------------- 迁移
def test_norm_column_migration():
    if os.path.exists(_MIG_DB):
        os.remove(_MIG_DB)
    with library._lconn() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(factor_registry)")]
    assert "norm" in cols, f"factor_registry 应有 norm 列，实际: {cols}"
    # 幂等：再连一次不报错
    with library._lconn() as c:
        pass
    os.remove(_MIG_DB)


# ---------------------------------------------------------------- 接线（typed_v2 分派进合成打分）
import factor_eval as fe


def _mk_panel_series(name2vals: dict[str, list[float]], days=("2025-01-02",)) -> dict:
    """{factor: [v...]} × days → {factor: (datetime,instrument) 长表}，40 票（≥30 不触发降级）。"""
    codes = [f"S{i:03d}" for i in range(40)]
    out = {}
    for name, vals in name2vals.items():
        idx = [(pd.Timestamp(d), c) for d in days for c in codes]
        out[name] = pd.Series(list(vals) * len(days), index=pd.MultiIndex.from_tuples(
            idx, names=["datetime", "instrument"]))
    return out


def _scheme(s: str | None):
    sig._NORM_SCHEME_OVERRIDE = s


def test_legacy_is_default_and_byte_identical():
    """无开关文件/无覆盖 → legacy：scoring_norms=None，composite_score 与手工 zscore 合成一致。"""
    _scheme(None)
    rng = np.random.RandomState(23)
    fs = _mk_panel_series({"a": rng.randn(40), "b": rng.randn(40)})
    w = {"a": (0.6, 1), "b": (0.4, -1)}
    assert sig.scoring_norms(["a", "b"]) is None
    got = sig.composite_score(fs, w)
    za = sig.zscore(fs["a"].droplevel(0)) * 0.6
    zb = sig.zscore(fs["b"].droplevel(0)) * -0.4
    exp = pd.concat([za, zb], axis=1).mean(axis=1) / 1.0
    pd.testing.assert_series_equal(got.sort_index(), exp.sort_index(), check_names=False)


def test_typed_dispatch_uses_rank_for_mapped_type():
    """typed_v2 + registry 映射 资金流→rank：单因子合成应等于 rank 路径而非 legacy zscore。"""
    _with_rows({"f_mf": {"kind": "loopengine", "factor_type": "资金流", "norm": None}})
    rng = np.random.RandomState(29)
    vals = rng.randn(39).tolist() + [100.0]  # 重尾离群
    fs = _mk_panel_series({"f_mf": vals})
    w = {"f_mf": (1.0, 1)}
    try:
        _scheme("typed_v2")
        got = sig.composite_score(fs, w)
        exp_rank = sig.cs_norm(fs["f_mf"].droplevel(0), "rank")
        exp_z = sig.zscore(fs["f_mf"].droplevel(0))
        pd.testing.assert_series_equal(got.sort_index(), exp_rank.sort_index(), check_names=False)
        assert abs(float(got.max()) - float(exp_rank.max())) < 1e-9
        assert not np.isclose(float(got.max()), float(exp_z.max())), "typed 下不应走 legacy zscore"
    finally:
        _scheme(None)


def test_snapshot_beats_auto_mapping():
    """pack 快照 norm=zscore 优先于 registry 的 rank 映射（N≥40 不触发降级，即原 zscore）。"""
    _with_rows({"f_mf": {"kind": "loopengine", "factor_type": "资金流", "norm": None}})
    rng = np.random.RandomState(31)
    fs = _mk_panel_series({"f_mf": rng.randn(39).tolist() + [100.0]})
    try:
        _scheme("typed_v2")
        norms = sig.scoring_norms(["f_mf"], [{"name": "f_mf", "norm": "zscore"}])
        assert norms == {"f_mf": "zscore"}
        got = sig.composite_score(fs, {"f_mf": (1.0, 1)}, norms=norms)
        exp = sig.zscore(fs["f_mf"].droplevel(0))
        pd.testing.assert_series_equal(got.sort_index(), exp.sort_index(), check_names=False)
    finally:
        _scheme(None)


def test_factor_contributions_same_dispatch_and_fuse():
    """归因与 composite_score 同分派；N<3 熔断日无 NaN 贡献泄漏。"""
    _with_rows({"f_mf": {"kind": "loopengine", "factor_type": "资金流", "norm": None}})
    rng = np.random.RandomState(37)
    fs = _mk_panel_series({"f_mf": rng.randn(40)})
    try:
        _scheme("typed_v2")
        contribs = sig.factor_contributions(fs, {"f_mf": (0.8, -1)}, "S005")
        exp = float(sig.cs_norm(fs["f_mf"].droplevel(0), "rank")["S005"]) * 0.8 * -1
        assert len(contribs) == 1 and abs(contribs[0][1] - exp) < 1e-9
        # 熔断：截面只剩 2 票
        tiny = {"f_mf": fs["f_mf"].iloc[:2]}
        assert sig.factor_contributions(tiny, {"f_mf": (0.8, -1)}, "S000") == []
    finally:
        _scheme(None)


def test_score_at_matches_composite_score_ranking():
    """_score_at 与 composite_score 同口径：typed 下同一输入逐日 Spearman=1（相差 w_total 常数倍）。"""
    _with_rows({"f_mf": {"kind": "loopengine", "factor_type": "资金流", "norm": None},
                "f_pv": {"kind": "loopengine", "factor_type": "量价", "norm": None}})
    rng = np.random.RandomState(41)
    fs = _mk_panel_series({"f_mf": rng.randn(39).tolist() + [50.0],
                           "f_pv": rng.randn(40)})
    w = {"f_mf": (0.7, 1), "f_pv": (0.3, -1)}
    try:
        _scheme("typed_v2")
        norms = sig.scoring_norms(["f_mf", "f_pv"])
        t = fs["f_mf"].index.get_level_values("datetime").max()
        vals_norm = {n: fe._norm(s) for n, s in fs.items()}
        a = fe._score_at(vals_norm, w, t, norms=norms)
        b = sig.composite_score(fs, w, norms=norms)
        assert a.corr(b, method="spearman") == 1.0
    finally:
        _scheme(None)


def test_scheme_switch_via_file():
    """开关文件 norm_scheme.json 生效；缺文件=legacy。"""
    p = Path("/tmp/qsys_test/norm_scheme.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text('{"scheme": "typed_v2"}')
        assert sig.current_norm_scheme() == "typed_v2"
        p.unlink()
        assert sig.current_norm_scheme() == "legacy"
    finally:
        if p.exists():
            p.unlink()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS: {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL: {fn.__name__}: {e}")
    print(f"\n结果: {len(fns) - failed} 通过, {failed} 失败, 共 {len(fns)} 个")
    sys.exit(1 if failed else 0)
