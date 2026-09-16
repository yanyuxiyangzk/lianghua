#!/usr/bin/env python3
"""shadow 期量化验收：进化信号的偏置方向 vs 当日实际过闸族分布（rank 相关）。

判据（docs/report-distill-evolution-plan.md v2 评审必修 3）：
  shadow_bias 的族偏置方向与当日实际过闸因子族分布的 Spearman > 0，
  且观察期内多数日为正 → 信号有信息量，才允许开阶段 1（prompt 层）。

用法（容器内）：
  docker exec lh-qsys python /tmp/shadow_eval.py [--days 10]
"""
import argparse
import os
import sys
from pathlib import Path

import pandas as pd

QSYS = Path(os.environ.get("QSYS_APP_DIR") or (
    "/app" if Path("/app/signals.py").exists() else Path(__file__).resolve().parent.parent / "qsys"))
sys.path.insert(0, str(QSYS))

import experience  # noqa: E402
import library  # noqa: E402


def eval_day(sig_date: str, boost: dict, c) -> dict | None:
    """单日：信号族偏置 vs 当日实际过闸族分布的 Spearman。"""
    rows = c.execute(
        "SELECT family, COUNT(*) FROM factor_registry"
        " WHERE gate_status=1 AND substr(first_seen, 1, 10)=? AND family IS NOT NULL"
        " GROUP BY family", (sig_date,)).fetchall()
    if not rows:
        return None  # 当日无过闸因子（验收分母缺失，不计）
    actual = dict(rows)
    fams = sorted(set(actual) | set(boost))
    if len(fams) < 3:
        return None  # 族太少，rank 相关无意义
    a = pd.Series({f: float(boost.get(f, 0.0)) for f in fams})
    b = pd.Series({f: float(actual.get(f, 0)) for f in fams})
    if a.nunique() < 2 or b.nunique() < 2:
        return None  # 常数列无法算相关
    return {"date": sig_date, "spearman": round(float(a.corr(b, method="spearman")), 3),
            "boost_fams": {k: v for k, v in boost.items() if v != 0},
            "actual_pass": actual}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()

    sigs = experience.list_evolution_signals(limit=args.days)
    if sigs.empty:
        print("evolution_signals 表无记录——蒸馏 job 还没跑过（或 mode=off）")
        return 1

    import json
    results, skipped = [], []
    with library._lconn() as c:
        for _, r in sigs.iterrows():
            bias = r.get("shadow_bias_json")
            if not bias:
                skipped.append((r["date"], "无 shadow_bias（引擎未挂钩/无轮次）"))
                continue
            boost = (json.loads(bias).get("would_boost_families") or {})
            if not boost:
                skipped.append((r["date"], "信号无族偏置（insufficient_evidence 或空 steer）"))
                continue
            res = eval_day(r["date"], boost, c)
            if res is None:
                skipped.append((r["date"], "当日无过闸因子或族太少"))
                continue
            results.append(res)

    print(f"== shadow 验收：偏置方向 × 实际过闸族分布（{len(results)} 个有效日）==\n")
    for r in results:
        mark = "✅" if r["spearman"] > 0 else "❌"
        print(f"{mark} {r['date']}: spearman={r['spearman']:+.3f}  "
          f"偏置={r['boost_fams']}  实际过闸={r['actual_pass']}")
    if skipped:
        print(f"\n跳过 {len(skipped)} 天: " + "; ".join(f"{d}({w})" for d, w in skipped))

    if results:
        s = pd.Series([r["spearman"] for r in results])
        pos = float((s > 0).mean())
        print(f"\n汇总: 均值 {s.mean():+.3f} · 为正日占比 {pos:.0%}（{len(s)} 天）")
        if s.mean() > 0 and pos >= 0.6:
            print("结论: 信号偏置方向与实际过闸分布正相关占优——可进入阶段 1（prompt 层）。")
        else:
            print("结论: 信号方向性证据不足——继续 shadow 观察或检查蒸馏输入质量。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
