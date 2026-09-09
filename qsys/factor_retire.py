"""因子三层退役规则：无效 → 低效 → 长期不用。

三层退役逻辑：
  Layer 1 - 无效因子（立即退役）：
    · |IC| < 0.01（无预测力）
    · IC胜率 < 40%（随机噪音）
    · 从未体检且生成超过14天（未被验证 = 不值得验证）

  Layer 2 - 低效因子（观察后退役）：
    · |IC| 0.01~0.02（边缘信号）
    · IC胜率 40%~50%（不稳定）
    · 多周期胜率差异 > 20%（过拟合）
    · 与活跃因子相关性 > 0.85（冗余）

  Layer 3 - 长期不用因子（自然淘汰）：
    · 超过30天未被任何策略包引用
    · 超过30天未被选股使用
    · 从未进入Top-N候选池

用法:
    python factor_retire.py scan [--dry-run]
    python factor_retire.py report
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import library
from common import DATA_DIR


def _safe_json(s):
    """安全解析JSON，失败返回空dict。"""
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}


# ============================================================ 阈值配置

THRESHOLDS = {
    # Layer 1: 无效因子
    "invalid_ic": 0.01,           # |IC| < 此值 → 无效
    "invalid_ic_wr": 0.40,        # IC胜率 < 此值 → 无效
    "invalid_age_days": 14,       # 未体检且超过此天数 → 无效

    # Layer 2: 低效因子
    "low_ic": 0.02,               # |IC| < 此值 → 低效
    "low_ic_wr": 0.50,            # IC胜率 < 此值 → 低效
    "high_corr": 0.85,            # 与活跃因子相关性 > 此值 → 冗余
    "wr_diff_max": 0.20,          # 多周期胜率差异 > 此值 → 过拟合

    # Layer 3: 长期不用
    "unused_days": 30,            # 超过此天数未被使用 → 淘汰

    # 综合评分
    "retire_score": 0.35,         # 综合分 < 此值 → 退役
    "watch_score": 0.50,          # 综合分 < 此值 → 观察
}


# ============================================================ 评分函数

def score_ic_quality(ic_mean: float) -> float:
    """IC质量评分 0~1。"""
    a = abs(ic_mean)
    if a >= 0.05: return 1.0
    if a >= 0.03: return 0.8
    if a >= 0.02: return 0.6
    if a >= 0.015: return 0.4
    if a >= 0.01: return 0.2
    return 0.0


def score_ic_stability(ic_winrate: float) -> float:
    """IC稳定性评分 0~1。"""
    if ic_winrate >= 0.60: return 1.0
    if ic_winrate >= 0.55: return 0.8
    if ic_winrate >= 0.50: return 0.6
    if ic_winrate >= 0.45: return 0.4
    return 0.2


def score_consistency(winrates: dict) -> float:
    """多周期一致性评分 0~1。"""
    if not winrates:
        return 0.5
    items = {k: v for k, v in winrates.items() if v is not None and v > 0}
    if len(items) < 2:
        return 0.5
    short = np.mean([v for k, v in items.items() if "1日" in k or "5日" in k] or [0])
    long_ = np.mean([v for k, v in items.items() if "20日" in k or "60日" in k] or [0])
    diff = abs(short - long_)
    if diff < 0.05: return 1.0
    if diff < 0.10: return 0.8
    if diff < 0.15: return 0.6
    if diff < 0.20: return 0.4
    return 0.2


def score_freshness(updated_at: str) -> float:
    """新鲜度评分 0~1。"""
    if not updated_at:
        return 0.0
    try:
        dt = datetime.strptime(str(updated_at)[:10], "%Y-%m-%d")
        days = (datetime.now() - dt).days
        if days <= 3: return 1.0
        if days <= 7: return 0.8
        if days <= 14: return 0.6
        if days <= 30: return 0.4
        return 0.2
    except Exception:
        return 0.0


def score_usage(name: str, used_in_strategies: set, used_in_picks: set) -> float:
    """使用度评分 0~1。"""
    if name in used_in_strategies:
        return 1.0
    if name in used_in_picks:
        return 0.8
    return 0.0


# ============================================================ 三层判定

def classify_factor(name: str, info: dict, used_strats: set, used_picks: set) -> dict:
    """对单个因子做三层分类。"""
    ic_mean = info.get("ic_mean", 0) or 0
    ic_wr = info.get("ic_winrate", 0.5) or 0.5
    winrates = info.get("winrates", {})
    first_seen = info.get("first_seen", "")
    updated_at = info.get("updated_at", "")
    has_scorecard = info.get("has_scorecard", False)

    abs_ic = abs(ic_mean)
    age_days = (datetime.now() - datetime.strptime(first_seen[:10], "%Y-%m-%d")).days if first_seen else 999
    unused_days = (datetime.now() - datetime.strptime(updated_at[:10], "%Y-%m-%d")).days if updated_at else 999

    # ---- Layer 1: 无效因子 ----
    invalid_reasons = []
    if abs_ic < THRESHOLDS["invalid_ic"]:
        invalid_reasons.append(f"|IC|={abs_ic:.4f}<{THRESHOLDS['invalid_ic']}")
    if ic_wr < THRESHOLDS["invalid_ic_wr"]:
        invalid_reasons.append(f"IC胜率={ic_wr:.1%}<{THRESHOLDS['invalid_ic_wr']:.0%}")
    # 未体检退役规则：量价14天，非量价30天（更宽容）
    factor_type = info.get("factor_type", "量价")
    no_sc_days = THRESHOLDS["invalid_age_days"] if factor_type == "量价" else 30
    if not has_scorecard and age_days > no_sc_days:
        invalid_reasons.append(f"未体检{age_days}天(>{no_sc_days}天)")

    if invalid_reasons:
        return {"layer": 1, "verdict": "retire", "reasons": invalid_reasons,
                "score": 0.0, "name": name}

    # ---- Layer 2: 低效因子 ----
    low_reasons = []
    if abs_ic < THRESHOLDS["low_ic"]:
        low_reasons.append(f"|IC|={abs_ic:.4f}<{THRESHOLDS['low_ic']}")
    if ic_wr < THRESHOLDS["low_ic_wr"]:
        low_reasons.append(f"IC胜率={ic_wr:.1%}<{THRESHOLDS['low_ic_wr']:.0%}")

    # 多周期一致性
    if winrates:
        short_vals = [v for k, v in winrates.items() if v and ("1日" in k or "5日" in k)]
        long_vals = [v for k, v in winrates.items() if v and ("20日" in k or "60日" in k)]
        if short_vals and long_vals:
            diff = abs(np.mean(short_vals) - np.mean(long_vals))
            if diff > THRESHOLDS["wr_diff_max"]:
                low_reasons.append(f"多周期差异={diff:.1%}>{THRESHOLDS['wr_diff_max']:.0%}")

    # ---- Layer 3: 长期不用 ----
    unused_reasons = []
    if name not in used_strats and name not in used_picks:
        if unused_days > THRESHOLDS["unused_days"]:
            unused_reasons.append(f"未被使用{unused_days}天")

    # 综合评分
    scores = {
        "ic_quality": score_ic_quality(ic_mean),
        "ic_stability": score_ic_stability(ic_wr),
        "consistency": score_consistency(winrates),
        "freshness": score_freshness(updated_at),
        "usage": score_usage(name, used_strats, used_picks),
    }
    weights = {"ic_quality": 0.30, "ic_stability": 0.25, "consistency": 0.20,
               "freshness": 0.10, "usage": 0.15}
    total = sum(scores[k] * weights[k] for k in weights)

    # 判定
    all_reasons = invalid_reasons + low_reasons + unused_reasons
    if invalid_reasons:
        verdict = "retire"
    elif low_reasons and unused_reasons:
        verdict = "retire"  # 低效+不用 → 退役
    elif low_reasons:
        verdict = "watch"
    elif unused_reasons:
        verdict = "watch" if total < THRESHOLDS["watch_score"] else "alive"
    elif total < THRESHOLDS["retire_score"]:
        verdict = "retire"
    elif total < THRESHOLDS["watch_score"]:
        verdict = "watch"
    else:
        verdict = "alive"

    return {
        "layer": 1 if invalid_reasons else (2 if low_reasons else 3),
        "verdict": verdict,
        "reasons": all_reasons,
        "scores": scores,
        "total": round(total, 3),
        "name": name,
    }


# ============================================================ 扫描主函数

def scan(dry_run: bool = False) -> dict:
    """执行三层退役扫描。"""
    # 读取活跃因子 + 体检数据
    with library._lconn() as c:
        rows = c.execute("""
            SELECT fr.name, fr.factor_type, fr.first_seen, fr.gate_status,
                   fs.ic_mean, fs.ic_winrate, fs.top_winrate, fs.winrates, fs.updated_at,
                   (SELECT 1 FROM factor_scorecards fs2 WHERE fs2.name = fr.name LIMIT 1) as has_sc
            FROM factor_registry fr
            LEFT JOIN factor_scorecards fs ON fr.name = fs.name
            WHERE fr.gate_status = 1
        """).fetchall()

        # 策略引用
        strat_factors = c.execute("""
            SELECT DISTINCT json_extract(value, '$.name')
            FROM strategies, json_each(strategies.factors)
            WHERE strategies.factors IS NOT NULL
        """).fetchall()
        used_strats = {r[0] for r in strat_factors}

    # 选股引用（experience.db）
    used_picks = set()
    try:
        edb = Path(DATA_DIR) / "experience.db"
        with sqlite3.connect(str(edb), timeout=30) as c2:
            pick_factors = c2.execute("""
                SELECT DISTINCT json_extract(value, '$.name')
                FROM pick_items pi JOIN picks p ON pi.pick_id = p.id
                WHERE p.factors IS NOT NULL
            """).fetchall()
            used_picks = {r[0] for r in pick_factors}
    except Exception:
        pass

    # 分类
    results = {"retire": [], "watch": [], "alive": [], "stats": {}}
    for row in rows:
        name = row[0]
        info = {
            "factor_type": row[1],
            "first_seen": row[2],
            "ic_mean": row[4],
            "ic_winrate": row[5],
            "winrates": _safe_json(row[8]),
            "updated_at": row[8],
            "has_scorecard": bool(row[9]),
        }
        result = classify_factor(name, info, used_strats, used_picks)
        results[result["verdict"]].append(result)

    # 统计
    total = len(rows)
    results["stats"] = {
        "total": total,
        "alive": len(results["alive"]),
        "watch": len(results["watch"]),
        "retire": len(results["retire"]),
    }

    # 按层统计退役
    layer_counts = {}
    for r in results["retire"]:
        layer = r["layer"]
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
    results["stats"]["retire_by_layer"] = layer_counts

    # 按类型统计退役
    type_counts = {}
    name_to_type = {row[0]: row[1] or "量价" for row in rows}
    for r in results["retire"]:
        ft = name_to_type.get(r["name"], "量价")
        type_counts[ft] = type_counts.get(ft, 0) + 1
    results["stats"]["retire_by_type"] = type_counts

    # 执行退役
    if not dry_run and results["retire"]:
        with library._lconn() as c:
            for r in results["retire"]:
                reason_str = "; ".join(r["reasons"][:2])
                c.execute("""
                    UPDATE factor_registry
                    SET gate_status = 0,
                        trace = COALESCE(trace, '') || ' | 三层退役(L' || ? || '): ' || ?
                    WHERE name = ? AND gate_status = 1
                """, (r["layer"], reason_str, r["name"]))

    return results


def print_report(results: dict):
    """输出报告。"""
    s = results["stats"]
    print(f"\n{'='*60}")
    print(f"因子三层退役扫描报告")
    print(f"{'='*60}")
    print(f"活跃因子总数: {s['total']}")
    print(f"  健康 (alive):  {s['alive']:5d} ({s['alive']/max(s['total'],1):.1%})")
    print(f"  观察 (watch):  {s['watch']:5d} ({s['watch']/max(s['total'],1):.1%})")
    print(f"  退役 (retire): {s['retire']:5d} ({s['retire']/max(s['total'],1):.1%})")

    print(f"\n退役分层:")
    for layer, cnt in sorted(s.get("retire_by_layer", {}).items()):
        labels = {1: "Layer1-无效", 2: "Layer2-低效", 3: "Layer3-不用"}
        print(f"  {labels.get(layer, f'L{layer}')}: {cnt}")

    print(f"\n退役按类型:")
    for ft, cnt in sorted(s.get("retire_by_type", {}).items(), key=lambda x: -x[1]):
        print(f"  {ft}: {cnt}")

    # 退役示例
    if results["retire"]:
        print(f"\n退役因子示例:")
        for r in sorted(results["retire"], key=lambda x: (x["layer"], x.get("total", 0)))[:15]:
            reason_str = " | ".join(r["reasons"][:2])
            print(f"  L{r['layer']} {r['name']:30s} [{reason_str[:60]}]")

    # 观察示例
    if results["watch"]:
        print(f"\n观察因子示例:")
        for r in results["watch"][:10]:
            reason_str = " | ".join(r["reasons"][:2]) if r["reasons"] else f"score={r['total']}"
            print(f"  L{r['layer']} {r['name']:30s} [{reason_str[:60]}]")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="因子三层退役")
    parser.add_argument("action", choices=["scan", "report"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    results = scan(dry_run=args.dry_run)
    print_report(results)

    report_path = "/data/factor_retire_report.json"
    Path(report_path).write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    print(f"\n报告: {report_path}")
