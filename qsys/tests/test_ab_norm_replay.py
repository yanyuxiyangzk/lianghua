"""ab_norm_replay.analyze_pack 冒烟测试（合成数据，无真实数据依赖）。

验证：双口径回放结构完整、legacy 与直接 static_backtest 一致、
typed σ 归一（全异值截面的秩 z 分 σ=1 精确成立）、报告可渲染。
"""

import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ["QSYS_ROOT"] = "/tmp/qsys_test"

for mod_name in ["common", "datasource"]:
    sys.modules[mod_name] = types.ModuleType(mod_name)

import json as _json
_common = sys.modules["common"]
_common.DATA_DIR = Path("/tmp/qsys_test")
_common.QLIB_DATA_DIR = Path("/tmp/qsys_test")
_common.init_qlib = lambda: None
_common.load_json = lambda p, default=None: (_json.loads(Path(p).read_text())
                                             if Path(p).exists()
                                             else (default if default is not None else {}))
_common.get_last_trade_day = lambda: "2025-01-01"
_common.all_pools = lambda: {}
sys.modules["datasource"].get_loop_source = lambda: "custom"
sys.modules["datasource"]._qconn = lambda: (_ for _ in ()).throw(RuntimeError("test: 禁连库"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pandas as pd
import factor_eval as fe
import library

# registry 合成行：f_fat=资金流→rank，f_mom=量价→zscore（typed_v2 分派生效的前提）
library._registry_rows = lambda: {
    "f_fat": {"kind": "loopengine", "factor_type": "资金流", "norm": None},
    "f_mom": {"kind": "loopengine", "factor_type": "量价", "norm": None},
}

_spec = importlib.util.spec_from_file_location(
    "ab_norm_replay", Path(__file__).resolve().parent.parent.parent / "scripts" / "ab_norm_replay.py")
ab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ab)


def _synthetic(n_stocks=60, n_days=450, seed=42):
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_days)
    codes = [f"S{i:03d}" for i in range(n_stocks)]
    close = pd.DataFrame(rng.lognormal(0, 0.02, (n_days, n_stocks)).cumprod(axis=0) * 100,
                         index=dates, columns=codes)
    panel = pd.DataFrame({"$close": close.stack()})
    panel.index = panel.index.set_names(["datetime", "instrument"])

    idx = pd.MultiIndex.from_product([dates, codes], names=["datetime", "instrument"])
    fvals = {
        # 重尾因子（lognormal，模拟财务/资金流类）
        "f_fat": pd.Series(rng.lognormal(0, 1.0, len(idx)) ** 4, index=idx),
        # 常态因子（量价类）；与收益弱相关以便回测有信号
        "f_mom": close.pct_change(20).stack().reindex(idx),
    }
    return panel, fvals


def test_analyze_pack_structure_and_invariants():
    panel, fvals = _synthetic()
    weights = {"f_fat": (0.5, 1), "f_mom": (0.5, 1)}
    r = ab.analyze_pack("测试包", weights, None, fvals, panel, top_n=10, step=10)
    assert "error" not in r, r.get("error")
    for k in ["static", "wf", "drift", "sigma", "norms_typed"]:
        assert k in r, f"缺 {k}"
    for scheme in ["legacy", "typed_v2"]:
        assert r["static"][scheme]["调仓点数"] > 5, "回放点数过少"
        assert r["wf"][scheme]["调仓点数"] > 5
    d = r["drift"]
    assert -1.0 <= d["spearman_mean"] <= 1.0 and 0.0 <= d["top_overlap_mean"] <= 1.0
    # 立论验证（分派：f_fat=资金流→rank，f_mom=量价→zscore）：
    # 全异值截面下 typed 的秩 z 分 σ 精确=1（ties/clip 不压缩）；常态因子 zscore σ≈1；
    # 而 legacy 对重尾因子 clip±3 后实现 σ 明显<1——有效权重失真的量化证据。
    sig_t = r["sigma"]["σ_typed"]["mean"]
    sig_l = r["sigma"]["σ_legacy"]["mean"]
    assert abs(sig_t["f_fat"] - 1.0) < 1e-9, f"f_fat typed σ 应=1，实际 {sig_t['f_fat']}"
    assert 0.9 < sig_t["f_mom"] <= 1.0 + 1e-9, f"f_mom typed σ 异常: {sig_t['f_mom']}"
    assert sig_l["f_fat"] < 0.9, f"重尾因子 legacy σ 应明显缩水（clip 失真），实际 {sig_l['f_fat']}"


def test_legacy_matches_direct_static_backtest():
    """analyze_pack 的 legacy 口径 == 直接调 fe.static_backtest（norms=None）。"""
    panel, fvals = _synthetic()
    weights = {"f_fat": (0.5, 1), "f_mom": (0.5, 1)}
    r = ab.analyze_pack("对照包", weights, None, fvals, panel, top_n=10, step=10)
    direct = fe.static_backtest(fvals, panel, weights, 10, step=10, norms=None)
    assert r["static"]["legacy"]["胜率"] == ab._bt_stats(direct, "组合扣费超额")["胜率"]


def test_render_report_smoke():
    panel, fvals = _synthetic()
    weights = {"f_fat": (0.5, 1), "f_mom": (0.5, 1)}
    md = ab.render_report([ab.analyze_pack("渲染包", weights, None, fvals, panel, 10, step=10),
                           {"name": "坏包", "error": "有效因子不足（0<2）"}])
    assert "渲染包" in md and "坏包" in md and "legacy" in md and "typed_v2" in md


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
