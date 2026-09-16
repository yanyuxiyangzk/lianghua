#!/usr/bin/env python3
"""A/B 回放：legacy vs typed_v2 归一化口径对比报告（typed_v2 切换前的硬闸门）。

对每个在役策略包，同一组因子值分别按两套归一化方案回放，产出对比：
  口径A 包固定权重 static_backtest（as-traded）：胜率 / 扣费超额
  口径B 滚动重估 walk_forward（因子集 OOS 质量）：胜率 / 扣费超额
  口径漂移：逐日综合分 Spearman 相关、Top-N 名单逐日重合度
  立论验证：各因子列实现 σ 的逐日分布（typed_v2 应把 rank/zscore 两类都拉到 ≈1；
           legacy 下重尾因子经 clip 后偏离 1 = 有效权重失真量化）
  （IC 序列归一化不变，两套共用一次预计算，省一半时间）

人工审阅报告通过后才允许把 DATA_DIR/norm_scheme.json 切到 typed_v2。

用法（容器内）：
  python scripts/ab_norm_replay.py                      # 全部 active 包
  python scripts/ab_norm_replay.py --packs LE_沪深300_current,Top5复合因子
  python scripts/ab_norm_replay.py --max-packs 5 --lookback 800
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

QSYS = Path(__file__).resolve().parent.parent / "qsys"
sys.path.insert(0, str(QSYS))

import datasource  # noqa: E402
import factor_eval as fe  # noqa: E402
import signals as sig  # noqa: E402
from common import all_pools, get_last_trade_day  # noqa: E402


# ================================================================ 纯分析层（可合成数据单测）
def _bt_stats(df: pd.DataFrame, col: str) -> dict:
    """从回测帧提取 胜率/均值/夏普比（扣费口径）。"""
    if df.empty or col not in df:
        return {"胜率": None, "均值": None, "夏普": None, "调仓点数": 0}
    x = df[col].dropna()
    return {"胜率": round(float((x > 0).mean()), 4),
            "均值": round(float(x.mean()), 5),
            "夏普": round(float(x.mean() / (x.std() + 1e-12) * np.sqrt(252 / fe.STEP_DAYS)), 2),
            "调仓点数": int(len(x))}


def _typed_norms(names: list[str], pack_factors: list[dict] | None) -> dict[str, str]:
    """以 typed_v2 语义解析分派（快照 > 自动映射），与全局开关无关。"""
    sig._NORM_SCHEME_OVERRIDE = "typed_v2"
    try:
        return sig.scoring_norms(names, pack_factors) or {n: "zscore" for n in names}
    finally:
        sig._NORM_SCHEME_OVERRIDE = None


def analyze_pack(name: str, weights: dict, pack_factors: list[dict] | None,
                 fvals: dict[str, pd.Series], panel: pd.DataFrame,
                 top_n: int, step: int | None = None) -> dict:
    """单包双口径回放 + 漂移 + σ 分布。fvals/panel 由调用方备好（纯函数，无 IO）。

    返回 {name, static: {legacy:.., typed_v2:..}, wf: {...}, drift: {...}, sigma: DataFrame}
    """
    step = step or fe.STEP_DAYS
    names = list(weights)
    vals_norm = {n: fe._norm(s.dropna()) for n, s in fvals.items()
                 if not s.dropna().empty and n in weights}
    weights = {n: weights[n] for n in vals_norm}
    if len(weights) < 2:
        return {"name": name, "error": f"有效因子不足（{len(weights)}<2）"}

    typed = _typed_norms(names, pack_factors)
    fwd = fe.forward_returns(panel, fe.MAIN_FWD)
    ic_full = {n: fe.ic_series(s, fwd) for n, s in vals_norm.items()}  # 归一化不变，两套共用

    # ---- 口径A：包固定权重（as-traded）----
    bt_static = {
        "legacy": fe.static_backtest(fvals, panel, weights, top_n, step=step, norms=None),
        "typed_v2": fe.static_backtest(fvals, panel, weights, top_n, step=step, norms=typed),
    }
    # ---- 口径B：滚动重估（因子集 OOS 质量）----
    bt_wf = {
        "legacy": fe.walk_forward(fvals, panel, "等权", top_n, step=step, ic_full=ic_full, norms=None),
        "typed_v2": fe.walk_forward(fvals, panel, "等权", top_n, step=step, ic_full=ic_full, norms=typed),
    }

    # ---- 逐日漂移 + σ 分布（同一调仓网格、包固定权重）----
    days = sorted(set.intersection(*[set(s.index.get_level_values("datetime").unique())
                                     for s in vals_norm.values()]))
    drift_rows, sigma_rows = [], []
    for t in days[::step]:
        a = fe._score_at(vals_norm, weights, t, norms=None)     # legacy
        b = fe._score_at(vals_norm, weights, t, norms=typed)    # typed_v2
        common = a.dropna().index.intersection(b.dropna().index)
        if len(common) < 3:
            continue
        sp = float(a[common].corr(b[common], method="spearman"))
        top_a, top_b = set(a.nlargest(top_n).index), set(b.nlargest(top_n).index)
        drift_rows.append({"调仓日": str(t)[:10], "spearman": sp,
                           "top_overlap": len(top_a & top_b) / max(top_n, 1)})
        for n in weights:
            cross = vals_norm[n][vals_norm[n].index.get_level_values("datetime") == t]
            cross.index = cross.index.get_level_values("instrument")
            cross = cross.dropna()
            if len(cross) < 3:
                continue
            z_leg = sig.zscore(cross)
            z_typ = sig.cs_norm(cross, typed[n])
            sigma_rows.append({"因子": n, "调仓日": str(t)[:10],
                               "σ_legacy": float(z_leg.std()), "σ_typed": float(z_typ.std()),
                               "max|z|_legacy": float(z_leg.abs().max()),
                               "max|z|_typed": float(z_typ.abs().max())})
    drift = pd.DataFrame(drift_rows)
    sigma = pd.DataFrame(sigma_rows)
    sigma_by_factor = (sigma.groupby("因子")[["σ_legacy", "σ_typed",
                                              "max|z|_legacy", "max|z|_typed"]]
                       .agg(["mean", "min"]).round(3) if not sigma.empty else pd.DataFrame())

    return {
        "name": name,
        "static": {k: _bt_stats(v, "组合扣费超额") for k, v in bt_static.items()},
        "wf": {k: _bt_stats(v, "优化组合扣费超额") for k, v in bt_wf.items()},
        "drift": {"spearman_mean": round(float(drift["spearman"].mean()), 4) if len(drift) else None,
                  "spearman_min": round(float(drift["spearman"].min()), 4) if len(drift) else None,
                  "top_overlap_mean": round(float(drift["top_overlap"].mean()), 4) if len(drift) else None,
                  "低重合日占比(<50%)": round(float((drift["top_overlap"] < 0.5).mean()), 4) if len(drift) else None},
        "sigma": sigma_by_factor,
        "norms_typed": typed,
    }


# ================================================================ IO 层（容器内运行）
def fetch_pack_inputs(pack: dict, codes: list[str], end: str, lookback: int):
    """取因子值 + 面板（生产数据源）。返回 (weights, fvals, panel)。"""
    factors = pack["factors"]
    weights = {f["name"]: (float(f.get("weight", 1.0)), int(f.get("direction", 1)))
               for f in factors}
    fvals = {}
    for f in factors:
        try:
            s = fe.get_factor_values(f, codes, end, lookback_days=lookback)
            if not s.dropna().empty:
                fvals[f["name"]] = s
        except Exception as e:
            print(f"  [warn] 因子 {f['name']} 取值失败，跳过: {e}")
    panel = sig.get_panel_cached(codes, end, lookback, source=datasource.get_loop_source())
    return weights, fvals, panel


def render_report(results: list[dict]) -> str:
    lines = ["# 归一化 A/B 回放报告（legacy vs typed_v2）",
             f"\n生成时间：{pd.Timestamp.now()}\n"]
    for r in results:
        if "error" in r:
            lines.append(f"## {r['name']}\n\n> {r['error']}\n")
            continue
        lines.append(f"## {r['name']}")
        lines.append(f"typed_v2 分派：{r['norms_typed']}\n")
        lines.append("| 口径 | 方案 | 胜率 | 均值 | 夏普 | 点数 |")
        lines.append("|---|---|---|---|---|---|")
        for scope, key in [("A 包固定权重", "static"), ("B 滚动重估 OOS", "wf")]:
            for scheme in ["legacy", "typed_v2"]:
                s = r[key][scheme]
                wr = f"{s['胜率']:.1%}" if s["胜率"] is not None else "-"
                lines.append(f"| {scope} | {scheme} | {wr} | {s['均值']} | {s['夏普']} | {s['调仓点数']} |")
        d = r["drift"]
        lines.append(f"\n口径漂移：Spearman 均值 {d['spearman_mean']} / 最低 {d['spearman_min']}；"
                     f"Top-N 重合均值 {d['top_overlap_mean']:.1%}；"
                     f"低重合日占比 {d['低重合日占比(<50%)']:.1%}\n")
        if not r["sigma"].empty:
            lines.append("各因子实现 σ（逐日均值/最低）与单票最大 |z|：")
            lines.append("```")
            lines.append(r["sigma"].to_string())
            lines.append("```\n")
        # 审阅提示（非自动判决）
        leg, typ = r["wf"]["legacy"]["胜率"], r["wf"]["typed_v2"]["胜率"]
        if leg is not None and typ is not None:
            if typ < leg - 0.05:
                lines.append("⚠️ typed_v2 的 OOS 胜率较 legacy 下降 >5pp——**建议维持 legacy**。\n")
            else:
                lines.append("✅ typed_v2 的 OOS 胜率不劣于 legacy（>5pp 内）——可进入灰度切换流程。\n")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--packs", default=None, help="逗号分隔包名；缺省=全部 active 包")
    ap.add_argument("--max-packs", type=int, default=8, help="最多评估包数（控制取值耗时）")
    ap.add_argument("--lookback", type=int, default=800)
    ap.add_argument("--step", type=int, default=None)
    args = ap.parse_args()

    import library
    saved = library.list_strategies()
    if args.packs:
        wanted = [n.strip() for n in args.packs.split(",") if n.strip()]
    else:
        wanted = [n for n, p in saved.items() if p.get("status", "active") == "active"]
        wanted = wanted[: args.max_packs]
    if not wanted:
        print("无待评估策略包")
        return 1

    end = get_last_trade_day()
    pools = all_pools()
    results = []
    for name in wanted:
        pk = saved.get(name)
        if not pk:
            print(f"[skip] 包 {name} 不在 strategies 表")
            continue
        print(f"[ab] {name}（{len(pk['factors'])} 因子）…")
        t0 = time.time()
        try:
            codes = pools.get(pk["pool_name"]) or pools.get("沪深300")
            weights, fvals, panel = fetch_pack_inputs(pk, codes, end, args.lookback)
            r = analyze_pack(name, weights, pk.get("factors"), fvals, panel,
                             int(pk.get("top_n", 10)), step=args.step)
            results.append(r)
            print(f"  完成（{time.time() - t0:.0f}s）")
        except Exception as e:
            results.append({"name": name, "error": f"回放失败: {e}"})
            print(f"  [error] {e}")

    report = render_report(results)
    out_dir = Path(__file__).resolve().parent.parent / "log"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"ab_norm_report_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.md"
    out.write_text(report)
    print(f"\n报告已写入 {out}\n")
    print(report[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
