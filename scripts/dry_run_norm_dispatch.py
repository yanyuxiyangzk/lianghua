#!/usr/bin/env python3
"""norm 分派 dry-run（typed_v2 缺口1落地前的人工过目环节）。

对真实 factor_registry 全量跑 resolve_norms 分派逻辑，打印：
  - factor_type × 分派结果 分布
  - 人工覆盖（norm 列非空）明细
  - 重点因子抽验（sr_entry / mom_5d / rsi_6 / 财务类）
只读 market.db，不写库、不改缓存。

用法: .venv/bin/python scripts/dry_run_norm_dispatch.py
"""
import sqlite3
import sys
import types
from collections import Counter
from pathlib import Path

QSYS = Path(__file__).resolve().parent.parent / "qsys"
DB = QSYS / "data" / "market.db"

# ---- mock common/datasource，使可导入真实 signals/library（不触库）----
for mod_name in ["common", "datasource"]:
    sys.modules[mod_name] = types.ModuleType(mod_name)
sys.modules["common"].DATA_DIR = Path("/tmp/qsys_dryrun")
sys.modules["common"].QLIB_DATA_DIR = Path("/tmp/qsys_dryrun")
sys.modules["common"].init_qlib = lambda: None
sys.modules["common"].load_json = lambda *a, **kw: {}
sys.modules["datasource"]._qconn = lambda: (_ for _ in ()).throw(RuntimeError("dry-run 禁连库"))

sys.path.insert(0, str(QSYS))
import library  # noqa: E402


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cols = [r[1] for r in con.execute("PRAGMA table_info(factor_registry)")]
    has_norm = "norm" in cols
    if not has_norm:
        print("（注：market.db 尚无 norm 列——迁移将在容器内应用下次 _lconn 时执行；"
              "本 dry-run 按全部 NULL 处理）")
    sel = "name, kind, factor_type, norm" if has_norm else "name, kind, factor_type, NULL"
    rows_raw = con.execute(f"SELECT {sel} FROM factor_registry").fetchall()
    con.close()
    rows = {r[0]: {"kind": r[1], "factor_type": r[2], "norm": r[3]} for r in rows_raw}
    library._registry_rows = lambda: rows  # 绕过 _lconn（只读直查）

    names = list(rows)
    out = library.resolve_norms(names)

    dist = Counter()
    for n in names:
        dist[(rows[n].get("factor_type") or "∅", out[n])] += 1
    print(f"== factor_type × 分派结果 分布（共 {len(names)} 因子）==")
    for (ft, m), cnt in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"  {ft:<8} → {m:<7} : {cnt}")

    overrides = [(n, rows[n]["norm"], rows[n].get("factor_type"))
                 for n in names if (rows[n].get("norm") or "").strip()]
    print(f"\n== 人工覆盖（norm 列非空）: {len(overrides)} ==")
    for n, nv, ft in overrides[:20]:
        print(f"  {n}  norm={nv!r}  factor_type={ft}")

    print("\n== 重点因子抽验 ==")
    for probe in ["sr_entry", "sr_hold", "sr_strength", "mom_5d", "rsi_6",
                  "f_vol_surge", "f_main_z5", "f_retail_bid", "f_sector_excess5"]:
        if probe in rows:
            r = rows[probe]
            print(f"  {probe:<20} type={r.get('factor_type'):<6} kind={r.get('kind'):<10} → {out[probe]}")
        else:
            print(f"  {probe:<20} （registry 无记录 → 兜底 {out.get(probe, 'zscore')}）")

    rank_pct = sum(1 for n in names if out[n] == "rank") / max(len(names), 1)
    print(f"\n汇总: rank {sum(1 for n in names if out[n]=='rank')} / zscore "
          f"{sum(1 for n in names if out[n]=='zscore')}（rank 占比 {rank_pct:.1%}）")


if __name__ == "__main__":
    main()
