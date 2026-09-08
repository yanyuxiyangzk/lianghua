"""非量价因子验证：体检 → 过滤 → 清理无效因子。

用法:
    python validate_non_price.py [--dry-run] [--batch 30]

流程:
    1. 从 factor_registry 取所有 factor_type != '量价' 的因子
    2. 逐批跑 build_scorecard (IC/ICIR/多周期胜率)
    3. 过滤: IC < 0.02 或 1日胜率 < 45% → 标记 gate_status=0
    4. 输出验证报告
"""

import json
import sys
import time
from datetime import datetime

import library


def get_non_price_factors(factor_type: str | None = None) -> list[dict]:
    """取非量价因子列表，返回 build_scorecard 所需格式。"""
    with library._lconn() as c:
        where = "factor_type != '量价'" if not factor_type else f"factor_type = '{factor_type}'"
        rows = c.execute(f"""
            SELECT name, kind, code, factor_type
            FROM factor_registry
            WHERE {where} AND gate_status = 1 AND code IS NOT NULL
            ORDER BY factor_type, name
        """).fetchall()
    return [{"name": r[0], "kind": r[1], "code": r[2], "factor_type": r[3]} for r in rows]


def validate_batch(factors: list[dict], pool: str = "沪深300", end: str | None = None) -> dict:
    """对一批因子跑体检，返回结果。"""
    import factor_eval as fe
    from common import all_pools, get_last_trade_day

    end = end or get_last_trade_day()
    codes = all_pools()[pool]

    factors_for_sc = [{"name": f["name"], "kind": f["kind"], "code": f["code"]} for f in factors]
    try:
        df = fe.build_scorecard(factors_for_sc, codes, end, source="qlib_local")
    except Exception as e:
        return {"error": str(e), "results": []}

    results = []
    for _, row in df.iterrows():
        results.append({
            "name": row.get("因子", ""),
            "ic_mean": float(row.get("IC均值", 0) or 0),
            "icir": float(row.get("ICIR", 0) or 0),
            "ic_winrate": float(row.get("IC胜率", 0) or 0),
            "top_winrate": float(row.get("Top组胜率", 0) or 0),
            "direction": row.get("建议方向", ""),
            "days": int(row.get("天数", 0) or 0),
        })
    return {"results": results}


def mark_invalid_factors(invalid_names: list[str], reason: str = "IC<0.02或胜率<45%"):
    """将无效因子标记为 gate_status=0。"""
    if not invalid_names:
        return 0
    with library._lconn() as c:
        placeholders = ",".join(["?"] * len(invalid_names))
        c.execute(f"""
            UPDATE factor_registry
            SET gate_status = 0, trace = COALESCE(trace, '') || ' | 验证淘汰: ' || ?
            WHERE name IN ({placeholders}) AND gate_status = 1
        """, [reason] + invalid_names)
        return c.total_changes


def run_validation(factor_type: str | None = None, batch: int = 30, dry_run: bool = False):
    """执行非量价因子验证。"""
    factors = get_non_price_factors(factor_type)
    if not factors:
        print("无非量价因子需要验证")
        return

    type_label = factor_type or "全部非量价"
    print(f"\n{'='*60}")
    print(f"非量价因子验证: {type_label}")
    print(f"因子总数: {len(factors)}, 批大小: {batch}, dry_run={dry_run}")
    print(f"{'='*60}\n")

    all_results = []
    valid_count = 0
    invalid_count = 0
    invalid_names = []

    for i in range(0, len(factors), batch):
        batch_factors = factors[i:i+batch]
        batch_types = set(f["factor_type"] for f in batch_factors)
        print(f"[{i+1:3d}-{min(i+batch, len(factors)):3d}] {', '.join(batch_types)} ({len(batch_factors)}个)...", end=" ", flush=True)

        t0 = time.time()
        result = validate_batch(batch_factors)
        dur = time.time() - t0

        if "error" in result:
            print(f"ERROR: {result['error'][:60]}")
            continue

        results = result["results"]
        batch_valid = 0
        batch_invalid = 0

        for r in results:
            ic = r["ic_mean"]
            wr = r.get("top_winrate", 0)
            # 判断标准: IC >= 0.02 且 Top组胜率 >= 45%
            if ic >= 0.02 and wr >= 0.45:
                batch_valid += 1
                valid_count += 1
            else:
                batch_invalid += 1
                invalid_count += 1
                invalid_names.append(r["name"])

        all_results.extend(results)
        print(f"✓ {batch_valid}有效 ✗ {batch_invalid}无效 ({dur:.1f}s)")

    # 汇总
    print(f"\n{'='*60}")
    print(f"验证完成: {valid_count}有效 / {invalid_count}无效 / {len(all_results)}总计")
    print(f"有效率: {valid_count/max(len(all_results),1):.1%}")

    if invalid_names and not dry_run:
        marked = mark_invalid_factors(invalid_names)
        print(f"已标记 {marked} 个无效因子为 gate_status=0")
    elif invalid_names and dry_run:
        print(f"[DRY RUN] 将标记 {len(invalid_names)} 个无效因子:")
        for n in invalid_names[:10]:
            print(f"  - {n}")
        if len(invalid_names) > 10:
            print(f"  ... 还有 {len(invalid_names)-10} 个")

    # 按类型汇总
    type_stats = {}
    for r in all_results:
        # 从因子名推断类型
        name = r["name"]
        with library._lconn() as c:
            row = c.execute("SELECT factor_type FROM factor_registry WHERE name=?", (name,)).fetchone()
            ft = row[0] if row else "未知"
        if ft not in type_stats:
            type_stats[ft] = {"valid": 0, "invalid": 0, "avg_ic": 0, "cnt": 0}
        type_stats[ft]["cnt"] += 1
        type_stats[ft]["avg_ic"] += abs(r["ic_mean"])
        if r["ic_mean"] >= 0.02 and r.get("top_winrate", 0) >= 0.45:
            type_stats[ft]["valid"] += 1
        else:
            type_stats[ft]["invalid"] += 1

    print(f"\n按类型汇总:")
    for ft, s in sorted(type_stats.items()):
        avg_ic = s["avg_ic"] / max(s["cnt"], 1)
        print(f"  {ft:8s}: {s['valid']:3d}有效 {s['invalid']:3d}无效 (avg|IC|={avg_ic:.4f})")

    # 输出JSON报告
    report = {
        "timestamp": datetime.now().isoformat(),
        "factor_type": factor_type,
        "total": len(all_results),
        "valid": valid_count,
        "invalid": invalid_count,
        "invalid_names": invalid_names,
        "results": all_results,
    }
    report_path = f"/data/validate_non_price_{factor_type or 'all'}.json"
    from pathlib import Path
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已保存: {report_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="非量价因子验证")
    parser.add_argument("--type", help="指定因子类型 (资金流/板块轮动/龙虎榜/盘口异动/指数)")
    parser.add_argument("--batch", type=int, default=30, help="批大小")
    parser.add_argument("--dry-run", action="store_true", help="只验证不修改")
    args = parser.parse_args()
    run_validation(factor_type=args.type, batch=args.batch, dry_run=args.dry_run)
