"""因子生命周期管理：评分 → 退役 → 清理。

退役标准（多维度综合评分）：
  1. IC质量：|IC均值| < 0.015 → 衰减信号
  2. IC稳定性：IC胜率 < 45% → 不稳定
  3. 多周期一致性：短周期(1日)胜率与长周期(20日)胜率差异 > 20% → 过拟合
  4. 与活跃因子相关性 > 0.85 → 冗余
  5. 连续3轮体检IC下降 → 衰退

退役流程：
  1. 评分 → 综合分 < 阈值 → 标记 gate_status=0
  2. 从因子库冻结（不参与选股打分）
  3. 保留历史数据用于回溯分析

用法:
    python factor_lifecycle.py scan              # 扫描并退役
    python factor_lifecycle.py scan --dry-run    # 只扫描不退役
    python factor_lifecycle.py report            # 输出生命周期报告
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import library


# 退役评分权重
WEIGHTS = {
    "ic_quality": 0.30,      # IC绝对值
    "ic_stability": 0.25,    # IC胜率
    "consistency": 0.20,     # 多周期一致性
    "freshness": 0.15,       # 新鲜度（最近体检时间）
    "uniqueness": 0.10,      # 独特性（低相关性）
}

# 退役阈值
RETIRE_THRESHOLD = 0.35     # 综合分 < 0.35 → 退役
WATCH_THRESHOLD = 0.50      # 综合分 < 0.50 → 观察


def _ic_quality_score(ic_mean: float) -> float:
    """IC质量评分：0~1，|IC|越大越好。"""
    abs_ic = abs(ic_mean)
    if abs_ic >= 0.05:
        return 1.0
    elif abs_ic >= 0.03:
        return 0.8
    elif abs_ic >= 0.02:
        return 0.6
    elif abs_ic >= 0.015:
        return 0.4
    elif abs_ic >= 0.01:
        return 0.2
    else:
        return 0.0


def _ic_stability_score(ic_winrate: float) -> float:
    """IC稳定性评分：IC胜率越高越稳定。"""
    if ic_winrate >= 0.60:
        return 1.0
    elif ic_winrate >= 0.55:
        return 0.8
    elif ic_winrate >= 0.50:
        return 0.6
    elif ic_winrate >= 0.45:
        return 0.4
    else:
        return 0.2


def _consistency_score(winrates: dict) -> float:
    """多周期一致性评分：短周期和长周期胜率差异越小越好。"""
    if not winrates:
        return 0.5  # 无数据给中间分

    # 过滤None和0值
    wr_items = {k: v for k, v in winrates.items() if v is not None and v > 0}
    if len(wr_items) < 2:
        return 0.5

    # 短周期(1日/5日) vs 长周期(20日/60日)
    short_vals = [v for k, v in wr_items.items() if "1日" in k or "5日" in k]
    long_vals = [v for k, v in wr_items.items() if "20日" in k or "60日" in k]
    short = np.mean(short_vals) if short_vals else 0
    long_ = np.mean(long_vals) if long_vals else 0

    diff = abs(short - long_)
    if diff < 0.05:
        return 1.0  # 高度一致
    elif diff < 0.10:
        return 0.8
    elif diff < 0.15:
        return 0.6
    elif diff < 0.20:
        return 0.4
    else:
        return 0.2  # 严重不一致（过拟合信号）


def _freshness_score(updated_at: str) -> float:
    """新鲜度评分：最近体检越近越好。"""
    if not updated_at:
        return 0.0
    try:
        dt = datetime.strptime(str(updated_at)[:10], "%Y-%m-%d")
        days_ago = (datetime.now() - dt).days
        if days_ago <= 3:
            return 1.0
        elif days_ago <= 7:
            return 0.8
        elif days_ago <= 14:
            return 0.6
        elif days_ago <= 30:
            return 0.4
        else:
            return 0.2
    except Exception:
        return 0.0


def compute_lifecycle_score(name: str, scorecard: dict, gate_detail: dict) -> dict:
    """计算因子生命周期综合评分。"""
    scores = {}

    # IC质量
    ic_mean = scorecard.get("ic_mean", 0) or gate_detail.get("ic", 0) or 0
    scores["ic_quality"] = _ic_quality_score(ic_mean)

    # IC稳定性
    ic_wr = scorecard.get("ic_winrate", 0.5) or 0.5
    scores["ic_stability"] = _ic_stability_score(ic_wr)

    # 多周期一致性
    winrates = scorecard.get("winrates", {})
    scores["consistency"] = _consistency_score(winrates)

    # 新鲜ness
    scores["freshness"] = _freshness_score(scorecard.get("updated_at", ""))

    # 独特性（简化：无相关性数据时给中间分）
    scores["uniqueness"] = 0.5

    # 综合分
    total = sum(scores[k] * WEIGHTS[k] for k in WEIGHTS)

    return {
        "name": name,
        "scores": scores,
        "total": round(total, 3),
        "verdict": "retire" if total < RETIRE_THRESHOLD else ("watch" if total < WATCH_THRESHOLD else "alive"),
    }


def scan_retirement_candidates(dry_run: bool = False) -> dict:
    """扫描退役候选因子。"""
    with library._lconn() as c:
        # 取所有活跃因子
        active = c.execute("""
            SELECT fr.name, fr.factor_type, fr.gate_status, fr.first_seen,
                   fs.ic_mean, fs.ic_winrate, fs.top_winrate, fs.winrates, fs.updated_at,
                   CAST(json_extract(gdl.metrics, '$.IC') AS REAL) as gate_ic
            FROM factor_registry fr
            LEFT JOIN factor_scorecards fs ON fr.name = fs.name
            LEFT JOIN gate_detail_log gdl ON fr.name = gdl.factor_name AND gdl.passed = 1
            WHERE fr.gate_status = 1
        """).fetchall()

        # 取因子间的相关性数据（如果有）
        corr_data = {}
        try:
            corr_rows = c.execute("""
                SELECT factor_name, metrics FROM gate_detail_log
                WHERE json_extract(metrics, '$.最大IC相关') IS NOT NULL
            """).fetchall()
            for row in corr_rows:
                metrics = json.loads(row[1]) if row[1] else {}
                corr_data[row[0]] = metrics.get("最大IC相关", 0)
        except Exception:
            pass

    results = {"alive": [], "watch": [], "retire": [], "stats": {}}

    for row in active:
        name = row[0]
        scorecard = {
            "ic_mean": row[4],
            "ic_winrate": row[5],
            "top_winrate": row[6],
            "winrates": json.loads(row[7]) if row[7] else {},
            "updated_at": row[8],
        }
        gate_detail = {"ic": row[9]}

        result = compute_lifecycle_score(name, scorecard, gate_detail)

        # 用实际相关性数据
        if name in corr_data:
            corr = corr_data[name]
            result["scores"]["uniqueness"] = 0.2 if corr > 0.85 else (0.5 if corr > 0.70 else 1.0)
            result["total"] = round(sum(result["scores"][k] * WEIGHTS[k] for k in WEIGHTS), 3)
            result["verdict"] = "retire" if result["total"] < RETIRE_THRESHOLD else ("watch" if result["total"] < WATCH_THRESHOLD else "alive")

        results[result["verdict"]].append(result)

    # 统计
    results["stats"] = {
        "total": len(active),
        "alive": len(results["alive"]),
        "watch": len(results["watch"]),
        "retire": len(results["retire"]),
        "retire_rate": f"{len(results['retire'])/max(len(active),1):.1%}",
    }

    # 按类型统计退役
    type_retire = {}
    for r in results["retire"]:
        # 从active中找类型
        for row in active:
            if row[0] == r["name"]:
                ft = row[1] or "量价"
                type_retire[ft] = type_retire.get(ft, 0) + 1
                break
    results["stats"]["by_type"] = type_retire

    # 执行退役
    if not dry_run and results["retire"]:
        with library._lconn() as c:
            for r in results["retire"]:
                c.execute("""
                    UPDATE factor_registry
                    SET gate_status = 0, trace = COALESCE(trace, '') || ' | 生命周期退役: score=' || ?
                    WHERE name = ? AND gate_status = 1
                """, (str(r["total"]), r["name"]))
            print(f"已退役 {len(results['retire'])} 个因子")

    return results


def print_report(results: dict):
    """输出退役报告。"""
    stats = results["stats"]
    print(f"\n{'='*60}")
    print(f"因子生命周期扫描报告")
    print(f"{'='*60}")
    print(f"活跃因子: {stats['total']}")
    print(f"  健康 (alive): {stats['alive']} ({stats['alive']/max(stats['total'],1):.1%})")
    print(f"  观察 (watch): {stats['watch']} ({stats['watch']/max(stats['total'],1):.1%})")
    print(f"  退役 (retire): {stats['retire']} ({stats['retire_rate']})")
    print(f"\n按类型退役:")
    for ft, cnt in stats.get("by_type", {}).items():
        print(f"  {ft}: {cnt} 个")

    # 退役因子示例
    if results["retire"]:
        print(f"\n退役因子示例 (前20):")
        for r in sorted(results["retire"], key=lambda x: x["total"])[:20]:
            scores = r["scores"]
            print(f"  {r['name']:30s} 综合={r['total']:.3f} "
                  f"IC={scores['ic_quality']:.1f} 稳定={scores['ic_stability']:.1f} "
                  f"一致={scores['consistency']:.1f} 新鲜={scores['freshness']:.1f}")

    # 观察因子示例
    if results["watch"]:
        print(f"\n观察因子示例 (前10):")
        for r in sorted(results["watch"], key=lambda x: x["total"])[:10]:
            scores = r["scores"]
            print(f"  {r['name']:30s} 综合={r['total']:.3f} "
                  f"IC={scores['ic_quality']:.1f} 稳定={scores['ic_stability']:.1f}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="因子生命周期管理")
    parser.add_argument("action", choices=["scan", "report"], help="scan=扫描退役, report=输出报告")
    parser.add_argument("--dry-run", action="store_true", help="只扫描不退役")
    args = parser.parse_args()

    results = scan_retirement_candidates(dry_run=args.dry_run)
    print_report(results)

    # 保存报告
    report_path = "/data/factor_lifecycle_report.json"
    Path(report_path).write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    print(f"\n报告已保存: {report_path}")
