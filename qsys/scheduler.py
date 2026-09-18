"""QSYS 定时任务调度（程序内调度，非系统 cron）。

机制：
  - APScheduler BackgroundScheduler 跑在 QSYS 容器进程内
  - 手动启动后按计划一直跑；容器停止 = 调度停止；容器重启后需在看板重新启动
  - 任务状态持久化在 /data/scheduler_state.json，运行结果在 /data/scheduler_last.json
"""

import json
import logging
import tarfile
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from common import (QLIB_DATA_DIR, SCHED_LAST_FILE, SCHED_STATE_FILE, SIGNALS_DIR,
                    all_pools, get_evolved_factors, get_last_trade_day, load_watchlist, save_json, load_json,
                    trade_day_offset)
import datasource
import signals as sig

TZ = "Asia/Shanghai"

# ---------------------------------------------------------------- 任务实现

def job_update_data() -> str:
    """每日行情更新：下载最新 qlib_bin 并解压覆盖（走 gh 代理，带校验）。"""
    import requests

    urls = ["https://gh-proxy.com/", "https://ghfast.top/", ""]
    base = "https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz"
    last_err = None
    for prefix in urls:
        try:
            with tempfile.TemporaryDirectory() as td:
                pkg = Path(td) / "qlib_bin.tar.gz"
                with requests.get(prefix + base, stream=True, timeout=600) as r:
                    r.raise_for_status()
                    with pkg.open("wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                if pkg.stat().st_size < 100_000_000:  # 正常 ~560MB，太小视为失败
                    raise RuntimeError(f"包大小异常: {pkg.stat().st_size}")
                with tarfile.open(pkg) as tar:
                    tar.extractall(td, filter="data")
                src = Path(td) / "qlib_bin"
                # 覆盖式同步（qlib 按文件读，单文件覆盖安全）
                import shutil
                shutil.copytree(src, QLIB_DATA_DIR, dirs_exist_ok=True)
            new_last = get_last_trade_day()
            return f"数据已更新至 {new_last}"
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"全部下载源失败: {last_err}")


def _pick_evolved_factors(max_n: int = 3) -> list[dict]:
    """优先被接受的 SOTA 因子；没有则取最新轮次的因子。"""
    fac = get_evolved_factors(only_accepted=True)
    if not fac:
        fac = get_evolved_factors(only_accepted=False)
    return fac[:max_n]


def job_watchlist_signals() -> str:
    """个股任务：自选股 × 最新进化因子 → 最新值与5日变化。"""
    codes = load_watchlist()
    if not codes:
        return "自选股为空，跳过"
    factors = _pick_evolved_factors(3)
    if not factors:
        return "尚无带代码的进化因子（先跑 RD-Agent 进化），跳过"
    end = get_last_trade_day()
    rows = []
    for f in factors:
        df = sig.run_factor_code(f["code"], f["name"], codes, end)
        s = df.iloc[:, 0]
        dt_level = "datetime" if "datetime" in s.index.names else s.index.names[0]
        days = sorted(s.index.get_level_values(dt_level).unique())
        latest, prev = days[-1], days[max(0, len(days) - 6)]
        cur = s[s.index.get_level_values(dt_level) == latest]
        old = s[s.index.get_level_values(dt_level) == prev]
        cur.index = cur.index.get_level_values("instrument")
        old.index = old.index.get_level_values("instrument")
        for c in codes:
            rows.append({"code": c, "factor": f["name"],
                         "最新值": cur.get(c, float("nan")), "5日前": old.get(c, float("nan"))})
    out = pd.DataFrame(rows).pivot(index="code", columns="factor", values=["最新值", "5日前"])
    out.columns = [f"{f}|{k}" for k, f in out.columns]
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    sig._write_parquet_atomic(out, SIGNALS_DIR / f"watchlist_{end}.parquet")
    return f"{end} 自选股信号完成：{len(codes)} 只 × {len(factors)} 因子"


def _best_pack(packs: dict) -> str:
    """综合评分选择策略包：实际表现优先 + 样本量置信度 + OOS验证。
    
    选择逻辑（P0+P1优化）：
      1. 过滤低质量包：OOS<55% 或 gap>25% 的包不参与选择
      2. 有实际数据：score = 实战胜率×0.5 + OOS×0.2 + 置信度×0.3
      3. 无实际数据：score = OOS×0.7（打折表示不确定性）
      4. 样本量置信度：min(n_trades/30, 1.0)
    """
    import sqlite3
    from pathlib import Path
    
    # 获取各策略包的实际胜率和交易次数
    actual_stats = {}
    try:
        db_path = Path("/data/experience.db")
        with sqlite3.connect(str(db_path), timeout=30) as c:
            rows = c.execute('''
                SELECT p.pack_name, 
                       SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as winrate,
                       COUNT(*) as n_trades
                FROM trades t
                JOIN picks p ON t.pick_id = p.id
                WHERE t.pnl_pct IS NOT NULL AND p.pack_name IS NOT NULL
                GROUP BY p.pack_name
            ''').fetchall()
            for pack_name, wr, n in rows:
                # 贝叶斯收缩：小样本向50%收缩
                prior_wr, prior_n = 0.5, 10
                shrunk_wr = (wr * n + prior_wr * prior_n) / (n + prior_n)
                actual_stats[pack_name] = {
                    "winrate": wr * 100,
                    "shrunk_wr": shrunk_wr * 100,
                    "n_trades": n,
                    "confidence": min(n / 30, 1.0),
                }
    except Exception:
        pass
    
    best, best_score = "", -1.0
    for name, pk in packs.items():
        v = str(pk.get("oos_winrate") or "")
        if not v.endswith("%"):
            continue
        try:
            oos_wr = float(v.rstrip("%"))
        except ValueError:
            continue
        
        # P1: 质量门槛 - OOS太低或太虚的包直接排除
        if oos_wr < 55:
            continue
        
        # 排除退化包（定期重验标记）与归档包（LE 日期版仅留档，固定名 current 参赛）
        if pk.get("status") in ("degraded", "archived"):
            continue
        
        stats = actual_stats.get(name)
        
        if stats is not None:
            actual_wr = stats["winrate"]
            shrunk_wr = stats["shrunk_wr"]
            confidence = stats["confidence"]
            n_trades = stats["n_trades"]
            
            gap = abs(oos_wr - actual_wr)
            
            # P1: gap过大（过拟合）直接排除
            if gap > 25:
                continue
            
            # 综合评分：实际表现为主 + OOS验证 + 样本量置信度
            # 实战胜率用贝叶斯收缩版本（更稳定）
            score = shrunk_wr * 0.5 + oos_wr * 0.2 + confidence * 30
        else:
            # 无实际数据：OOS打折
            score = oos_wr * 0.7
        
        if score > best_score:
            best, best_score = name, score
    return best


def _top_packs(packs: dict, top_n: int = 3) -> list[tuple[str, dict]]:
    """返回评分最高的top_n个策略包列表，用于多包投票。"""
    import sqlite3
    from pathlib import Path
    
    actual_stats = {}
    try:
        db_path = Path("/data/experience.db")
        with sqlite3.connect(str(db_path), timeout=30) as c:
            rows = c.execute('''
                SELECT p.pack_name, 
                       SUM(CASE WHEN t.pnl_pct > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*) as winrate,
                       COUNT(*) as n_trades
                FROM trades t
                JOIN picks p ON t.pick_id = p.id
                WHERE t.pnl_pct IS NOT NULL AND p.pack_name IS NOT NULL
                GROUP BY p.pack_name
            ''').fetchall()
            for pack_name, wr, n in rows:
                prior_wr, prior_n = 0.5, 10
                shrunk_wr = (wr * n + prior_wr * prior_n) / (n + prior_n)
                actual_stats[pack_name] = {
                    "winrate": wr * 100,
                    "shrunk_wr": shrunk_wr * 100,
                    "n_trades": n,
                    "confidence": min(n / 30, 1.0),
                }
    except Exception:
        pass
    
    scored = []
    for name, pk in packs.items():
        # 退化/归档/停赛包不参与投票（M6：paused 由 pack_lifecycle 每日评估写入）
        if pk.get("status") in ("degraded", "archived", "paused"):
            continue
        v = str(pk.get("oos_winrate") or "")
        if not v.endswith("%"):
            continue
        try:
            oos_wr = float(v.rstrip("%"))
        except ValueError:
            continue
        if oos_wr < 55:
            continue
        
        stats = actual_stats.get(name)
        if stats is not None:
            gap = abs(oos_wr - stats["winrate"])
            if gap > 25:
                continue
            score = stats["shrunk_wr"] * 0.5 + oos_wr * 0.2 + stats["confidence"] * 30
        else:
            score = oos_wr * 0.7
        scored.append((name, pk, score))
    
    scored.sort(key=lambda x: x[2], reverse=True)
    return [(name, pk) for name, pk, _ in scored[:top_n]]


def compute_pack_picks(pk: dict, codes: list[str], end: str, top_n: int):
    """按策略包配置计算 Top-N 名单（job_pool_scan 与 🧩选股组合页共用，同一套逻辑）。
    返回 (picks 综合分 Series, note, weights, f_series)；因子全部无法解析时抛错。"""
    import library

    f_series, weights = {}, {}
    panel = sig.get_panel_cached(codes, end)
    # 策略包：按其因子+权重+方向+过滤器
    evolved_by_name = {f["name"]: f for f in get_evolved_factors(only_accepted=False)}
    # LoopEngine 因子从注册表取代码（之前只查 evolved，loopengine/tech 因子会被静默丢弃）
    try:
        reg = library.get_factor_registry()
        le_code = {r["name"]: r["code"] for _, r in reg[reg["engine"] == "loopengine"].iterrows()}
    except Exception:
        le_code = {}
    dropped = []
    for f in pk["factors"]:
        kind, fname = f.get("kind"), f["name"]
        if kind == "builtin":
            s = sig.compute_builtin(panel, fname)
        elif kind == "tech":
            s = sig.compute_common(panel, fname) if fname in sig.CATALOG_NAMES \
                else sig.compute_tech(panel, fname)
        else:
            ef = evolved_by_name.get(fname)
            code = (ef or {}).get("code") or le_code.get(fname)
            if not code:
                dropped.append(fname)
                continue
            if code.startswith("# sexpr:"):
                # 树因子进程内向量直算（和 le_factor_eval 同款快速路径，~0.1s/个；
                # 避开 run_factor_code 的子进程——慢且会因子代码缺陷挂起，2026-09 实测把扫描拖死）
                try:
                    from loopengine.tree import build_field_frames, evaluate_tree, parse

                    tree = parse(code.split("\n", 1)[0][len("# sexpr: "):])
                    s = evaluate_tree(tree, build_field_frames(panel)).stack().rename(fname)
                    s.index = s.index.set_names(["datetime", "instrument"])
                except Exception:
                    df = sig.run_factor_code(code, fname, codes, end)
                    s = df.iloc[:, 0]
            else:
                df = sig.run_factor_code(code, fname, codes, end)
                s = df.iloc[:, 0]
        f_series[fname] = s
        weights[fname] = (f["weight"], f["direction"])
    if not weights:
        raise RuntimeError("策略包因子全部无法解析，未出名单")
    
    # 尝试用最新评分卡重算权重（避免使用过时的快照权重）
    try:
        sc = library.get_latest_scorecard(pk["pool_name"])
        if not sc.empty:
            scm = sc.drop_duplicates(subset=["因子"]).set_index("因子")
            updated_weights = {}
            for fname, (old_weight, direction) in weights.items():
                if fname in scm.index and "ICIR" in scm.columns and pd.notna(scm.loc[fname, "ICIR"]):
                    # 用最新ICIR重算权重
                    icir = abs(float(scm.loc[fname, "ICIR"]))
                    updated_weights[fname] = (max(icir, 0.0), direction)
                else:
                    updated_weights[fname] = (old_weight, direction)
            # 归一化
            total = sum(w for w, _ in updated_weights.values())
            if total > 0:
                weights = {n: (w / total, d) for n, (w, d) in updated_weights.items()}
    except Exception:
        pass  # 使用原权重
    
    score = sig.composite_score(f_series, weights,
                                norms=sig.scoring_norms(list(weights), pk.get("factors")))
    survived = sig.apply_filters(score.index.tolist(), panel, pk.get("filters", []))
    sel = score[score.index.isin(survived)]
    reso_note = ""
    # 多周期共振：包带持有期时，用最新评分卡在 主口径+另一短线口径 各配权取交集
    try:
        sc = library.get_latest_scorecard(pk["pool_name"])
        if not sc.empty and pk.get("horizon"):
            scm = sc.drop_duplicates(subset=["因子"]).set_index("因子")
            h_main = pk["horizon"] if pk["horizon"] in ("1日", "5日") else "5日"
            h_pair = "1日" if h_main == "5日" else "5日"

            def _hw(h):
                col = f"{h}胜率"
                out = {}
                for f in pk["factors"]:
                    n = f["name"]
                    if n in scm.index and col in scm.columns and pd.notna(scm.loc[n, col]):
                        out[n] = (max(float(scm.loc[n, col]) - 0.5, 0.0), f["direction"])
                t = sum(w for w, _ in out.values())
                return {n: (w / t, d) for n, (w, d) in out.items()} if t > 0 else None

            wa, wb = _hw(h_main), _hw(h_pair)
            if wa and wb and len(wa) >= 2:
                sel = sig.resonance_select(f_series, wa, wb, top_n, k=top_n * 3)
                sel = sel[sel.index.isin(survived)]
                reso_note = f"·{h_main}+{h_pair}共振"
    except Exception:
        pass
    picks = sig.industry_cap_select(sel, cap=2).head(top_n)
    note = f"{len(weights)} 因子·行业≤2{reso_note}" + \
        (f"，{len(dropped)} 个无法解析已跳过" if dropped else "")

    # 记录因子使用
    try:
        import library
        library.record_factor_usage(list(weights.keys()), usage_type="pick", pick_date=end)
    except Exception:
        pass

    return picks, note, weights, f_series


def auto_select_factors(pool_name: str = "沪深300", top_n: int = 10,
                        min_per_type: int = 1, max_per_type: int = 3) -> tuple:
    """自动选股 v5：技术审查修复版。

    核心标准（双胜率+置信度）：
    1. 因子回测胜率（factor_scorecards.top_winrate）
    2. 实盘交易胜率（trades/pick_items关联，贝叶斯收缩+时间衰减）
    3. 策略包OOS胜率（加权平均+鲁棒性）

    v5修复：
    - 贝叶斯收缩：小样本胜率向先验收缩，避免3笔交易=100%的极端
    - 样本量置信度：无实盘数据的因子实盘维度贡献归零
    - 收益率归一化：sigmoid映射到[0,1]，避免量纲失衡
    - 时间衰减：近期交易权重更高（90天半衰期）
    - 因子相关性：计算因子值后检查相关系数，>0.7剔除
    - 因子方向：使用IC均值符号判断，而非IC综合分
    - 负收益clip：收益率winsorize到5%-95%分位

    返回: (picks Series, note str, weights dict, f_series dict)
    """
    import library
    from common import all_pools, get_last_trade_day
    import signals as sig
    import numpy as np
    import sqlite3
    from pathlib import Path

    end = get_last_trade_day()
    codes = all_pools().get(pool_name, all_pools()["沪深300"])

    # ========== 第一部分：因子回测评分 ==========
    vs = library.factor_value_scores()
    if vs.empty:
        raise RuntimeError("无因子价值评分数据")

    reg = library.get_factor_registry()
    if reg.empty:
        raise RuntimeError("无因子注册表")

    vs = vs.merge(reg[["name", "skeleton", "family", "factor_type", "code"]], on="name", how="left", suffixes=("", "_reg"))
    if "factor_type_reg" in vs.columns:
        vs["factor_type"] = vs["factor_type_reg"].fillna(vs["factor_type"])

    # 去相关：同骨架只保留最高分
    vs = vs.sort_values("total_score", ascending=False)
    vs = vs.drop_duplicates(subset="skeleton", keep="first")

    # ========== 第二部分：实盘交易胜率（贝叶斯收缩+时间衰减） ==========
    MIN_TRADES = 10  # 至少10笔才有统计意义
    HALF_LIFE_DAYS = 90  # 时间衰减半衰期

    def _shrink_winrate(observed_wr, n_trades, prior=0.5, prior_strength=10):
        """贝叶斯收缩：样本越少越靠近先验"""
        return (observed_wr * n_trades + prior * prior_strength) / (n_trades + prior_strength)

    def _decay_weight(trade_date_str):
        """指数衰减：最近的交易权重最高"""
        try:
            days_ago = (pd.Timestamp.now() - pd.Timestamp(trade_date_str)).days
            return np.exp(-np.log(2) * days_ago / HALF_LIFE_DAYS)
        except Exception:
            # 异常情况返回低权重（等同于365天前）
            return np.exp(-np.log(2) * 365 / HALF_LIFE_DAYS)

    actual_winrate_map = {}  # {因子名: (收缩后胜率, 归一化收益率, 交易次数, 置信度)}
    try:
        edb = Path("/data/experience.db")
        if not edb.exists():
            raise FileNotFoundError(f"经验库不存在: {edb}")
        with sqlite3.connect(str(edb), timeout=30) as conn:
            rows = conn.execute("""
                SELECT p.factors, t.code, t.pnl_pct, p.trade_date
                FROM picks p
                JOIN trades t ON p.id = t.pick_id
                WHERE t.pnl_pct IS NOT NULL
            """).fetchall()

            # 解析因子并统计（时间衰减加权）
            factor_data = {}  # {因子名: [(pnl, weight)]}
            for factors_json, code, pnl, trade_date in rows:
                try:
                    factors = json.loads(factors_json) if factors_json else []
                except Exception:
                    continue
                w = _decay_weight(trade_date)
                for f in factors:
                    # 兼容旧格式（字符串列表）和新格式（dict列表）
                    fname = f.get("name", "") if isinstance(f, dict) else str(f)
                    if fname not in factor_data:
                        factor_data[fname] = []
                    factor_data[fname].append((pnl, w))

            # 计算胜率和收益率
            for fname, pnls_weights in factor_data.items():
                pnls = [p for p, _ in pnls_weights]
                weights = [w for _, w in pnls_weights]
                n_trades = len(pnls)

                if n_trades < MIN_TRADES:
                    continue

                # 时间衰减加权胜率
                weighted_wins = sum(p * w for p, w in zip(pnls, weights) if p > 0)
                weighted_total = sum(weights)
                raw_winrate = weighted_wins / weighted_total if weighted_total > 0 else 0.5

                # 贝叶斯收缩
                shrunk_winrate = _shrink_winrate(raw_winrate, n_trades)

                # 时间衰减加权收益率
                avg_ret = sum(p * w for p, w in zip(pnls, weights)) / weighted_total if weighted_total > 0 else 0.0

                # Winsorize收益率到5%-95%分位
                p5, p95 = np.percentile(pnls, 5), np.percentile(pnls, 95)
                clipped_ret = np.clip(avg_ret, p5, p95)

                # Sigmoid归一化到[0,1]
                ret_norm = 1.0 / (1.0 + np.exp(-clipped_ret * 10))  # 10为灵敏度

                # 置信度：30笔以上满置信
                confidence = min(n_trades / 30, 1.0)

                actual_winrate_map[fname] = (shrunk_winrate, ret_norm, n_trades, confidence)
    except Exception as e:
        import logging
        logging.getLogger("scheduler").warning(f"实盘胜率计算异常: {e}")

    # 无实盘数据：胜率默认0.4（惩罚性），收益率0.0，置信度0
    vs["actual_winrate"] = vs["name"].map(lambda n: actual_winrate_map.get(n, (0.4, 0.0, 0, 0.0))[0])
    vs["actual_return"] = vs["name"].map(lambda n: actual_winrate_map.get(n, (0.4, 0.0, 0, 0.0))[1])
    vs["actual_trades"] = vs["name"].map(lambda n: actual_winrate_map.get(n, (0.4, 0.0, 0, 0.0))[2])
    vs["actual_confidence"] = vs["name"].map(lambda n: actual_winrate_map.get(n, (0.4, 0.0, 0, 0.0))[3])

    # ========== 第三部分：策略包投票（加权平均+鲁棒性） ==========
    packs = library.list_strategies()
    pack_scores = {}  # {因子名: [(包分, 因子权重)]}
    pack_count_map = {}  # {因子名: 被几个包选中}

    for pk_name, pk in packs.items():
        oos_str = str(pk.get("oos_winrate") or "")
        is_wr_str = str(pk.get("is_winrate") or "")

        oos_score = 0.0
        if oos_str.endswith("%"):
            try:
                oos_score = float(oos_str.rstrip("%")) / 100
            except ValueError:
                pass
        elif "夏普" in oos_str:
            try:
                oos_score = min(1.0, float(oos_str.replace("夏普", "")) / 2)
            except ValueError:
                pass

        is_score = 0.0
        if is_wr_str.endswith("%"):
            try:
                is_score = float(is_wr_str.rstrip("%")) / 100
            except ValueError:
                pass

        pack_score = oos_score * 0.6 + is_score * 0.4

        for f in pk.get("factors", []):
            fname = f["name"]
            factor_weight = f.get("weight", 1.0)
            if fname not in pack_scores:
                pack_scores[fname] = []
            pack_scores[fname].append((pack_score, factor_weight))

    # 综合投票：加权平均 + 鲁棒性
    def _pack_vote(n):
        if n not in pack_scores or not pack_scores[n]:
            return 0.0, 0
        scores_weights = pack_scores[n]
        # 加权平均（按因子在包内的权重）
        total_w = sum(w for _, w in scores_weights)
        if total_w > 0:
            avg_score = sum(s * w for s, w in scores_weights) / total_w
        else:
            avg_score = np.mean([s for s, _ in scores_weights])
        return avg_score, len(scores_weights)

    vs["pack_vote_raw"] = vs["name"].map(lambda n: _pack_vote(n)[0])
    vs["pack_count"] = vs["name"].map(lambda n: _pack_vote(n)[1])
    vs["pack_robustness"] = np.minimum(vs["pack_count"] / 3, 1.0)  # 3个包以上满鲁棒性
    vs["pack_vote"] = vs["pack_vote_raw"] * 0.7 + vs["pack_robustness"] * 0.3

    # ========== 第四部分：综合评分 v5 ==========
    try:
        with library._lconn() as c:
            sc = pd.read_sql(
                "SELECT name, ic_mean, ic_winrate, top_winrate, days FROM factor_scorecards WHERE days >= 5",
                c)
    except Exception:
        sc = pd.DataFrame()

    stability_map = {}
    if not sc.empty:
        for name, group in sc.groupby("name"):
            if len(group) >= 2:
                ic_std = group["ic_mean"].std()
                stability_map[name] = max(0, 1 - ic_std * 10)
    vs["stability_extra"] = vs["name"].map(stability_map).fillna(0.3)

    momentum_map = {}
    if not sc.empty:
        for name, group in sc.groupby("name"):
            if len(group) >= 2:
                group = group.sort_values("days")
                recent_ic = group.iloc[-1]["ic_mean"]
                older_ic = group.iloc[0]["ic_mean"]
                momentum_map[name] = min(1.0, max(0, 0.5 + (recent_ic - older_ic) * 5))
    vs["momentum"] = vs["name"].map(momentum_map).fillna(0.5)

    # 回测胜率
    backtest_winrate_map = {}
    if not sc.empty:
        latest_sc = sc.sort_values("days", ascending=False).drop_duplicates(subset="name", keep="first")
        backtest_winrate_map = dict(zip(latest_sc["name"], latest_sc["top_winrate"].fillna(0.5)))
    vs["backtest_winrate"] = vs["name"].map(backtest_winrate_map).fillna(0.5)

    # 综合评分 v5：双胜率核心 + 置信度缩放
    vs["score_v5"] = (
        vs["backtest_winrate"] * 0.25 +
        vs["actual_winrate"] * vs["actual_confidence"] * 0.25 +
        vs["actual_return"] * vs["actual_confidence"] * 0.10 +
        vs["pack_vote"] * 0.15 +
        vs["total_score"] * 0.10 +
        vs["stability_extra"] * 0.08 +
        vs["momentum"] * 0.07
    )

    # ========== 第五部分：类型+族分散选因子 ==========
    selected_factors = []
    used_families = set()

    vs_sorted = vs.sort_values("score_v5", ascending=False)

    for _, row in vs_sorted.iterrows():
        ftype = row.get("factor_type", "量价")
        fam = row.get("family", "其他")

        type_count = len([f for f in selected_factors if f.get("factor_type") == ftype])
        if type_count >= max_per_type:
            continue

        fam_key = f"{ftype}_{fam}"
        fam_count = len([f for f in selected_factors if f.get("factor_type") == ftype and f.get("family") == fam])
        if fam_count >= 2:
            continue

        selected_factors.append(row.to_dict())
        used_families.add(fam_key)

        if len(selected_factors) >= max_per_type * 5:
            break

    if not selected_factors:
        raise RuntimeError("无选中因子")

    # ========== 第六部分：计算因子值 + 相关性过滤 ==========
    panel = sig.get_panel_cached(codes, end)
    f_series = {}
    weights = {}
    ic_mean_map = {}  # 用于判断方向

    # 从scorecards获取IC均值符号（用于判断因子方向）
    if not sc.empty:
        latest_sc = sc.sort_values("days", ascending=False).drop_duplicates(subset="name", keep="first")
        ic_mean_map = dict(zip(latest_sc["name"], latest_sc["ic_mean"]))

    for f in selected_factors:
        fname = f["name"]
        score = f["score_v5"]

        try:
            row = reg[reg["name"] == fname]
            if row.empty:
                continue
            code = row.iloc[0]["code"]
            if not code:
                continue

            if code.startswith("# sexpr:"):
                from loopengine.tree import build_field_frames, evaluate_tree, parse
                tree = parse(code.split("\n", 1)[0][len("# sexpr: "):])
                s = evaluate_tree(tree, build_field_frames(panel)).stack().rename(fname)
                s.index = s.index.set_names(["datetime", "instrument"])
            else:
                df = sig.run_factor_code(code, fname, codes, end)
                s = df.iloc[:, 0]

            f_series[fname] = s
            # 方向：使用IC均值符号判断（IC>0→1, IC<0→-1）
            ic_mean = ic_mean_map.get(fname, 0.0)
            direction = 1 if ic_mean >= 0 else -1
            weights[fname] = (score, direction)
        except Exception as e:
            import logging
            logging.getLogger("scheduler").debug(f"因子{fname}计算失败: {e}")
            continue

    if not weights:
        raise RuntimeError("所有因子计算失败")

    # 相关性过滤：>0.7剔除低分因子
    def _remove_correlated(f_series_dict, threshold=0.7):
        """按score降序，贪心保留不相关的因子"""
        names = list(f_series_dict.keys())
        if len(names) <= 1:
            return names

        # 构造截面因子矩阵
        panel_df = pd.DataFrame(f_series_dict)
        corr = panel_df.corr().abs()

        # 按score降序
        scored = [(n, weights.get(n, (0, 1))[0]) for n in names]
        scored.sort(key=lambda x: -x[1])

        kept = [scored[0][0]]
        for name, _ in scored[1:]:
            max_corr = corr.loc[name, kept].max() if kept else 0
            if max_corr < threshold:
                kept.append(name)
        return kept

    kept_names = _remove_correlated(f_series, threshold=0.7)
    f_series = {k: v for k, v in f_series.items() if k in kept_names}
    weights = {k: v for k, v in weights.items() if k in kept_names}

    # ========== 第七部分：合成 + 行业分散 ==========
    score = sig.composite_score(f_series, weights)
    survived = sig.apply_filters(score.index.tolist(), panel, ["tradable"])
    sel = score[score.index.isin(survived)]
    picks = sig.industry_cap_select(sel, cap=2).head(top_n)

    try:
        library.record_factor_usage(list(weights.keys()), usage_type="pick", pick_date=end)
    except Exception:
        pass

    # 生成note
    type_counts = {}
    for f in selected_factors:
        if f["name"] in weights:
            ft = f.get("factor_type", "量价")
            type_counts[ft] = type_counts.get(ft, 0) + 1
    type_note = " ".join(f"{k}:{v}" for k, v in type_counts.items())
    pack_count = len([f for f in selected_factors if f.get("pack_vote", 0) > 0])
    actual_count = len([f for f in selected_factors if f.get("actual_trades", 0) >= MIN_TRADES])
    corr_removed = len(selected_factors) - len(kept_names)
    note = f"自动选股v5 · {len(weights)}因子({type_note}) · {pack_count}策略包 · {actual_count}有实盘 · 去相关{corr_removed}"

    return picks, note, weights, f_series


def _satellite_pack_name(packs: dict) -> str | None:
    """卫星轨策略包：优先名字含'卫星'的包；其次含 ev_ 事件因子最多的包；最后名字含涨停/事件的包。"""
    # 优先：名字含"卫星"的包（新策略包命名规范）
    for n in packs:
        if "卫星" in n:
            return n
    # 其次：含 ev_ 事件因子最多的包
    best, best_n = None, 0
    for n, pk in packs.items():
        k = sum(1 for f in pk.get("factors", []) if str(f["name"]).startswith("ev_"))
        if k > best_n:
            best, best_n = n, k
    if best:
        return best
    return next((n for n in packs if "涨停" in n or "事件" in n), None)


def job_pool_scan(pool_name: str = "沪深300", top_n: int = 10, pack: str = "") -> str:
    """板块/池任务：综合打分输出 Top-N。pack 为空时自动选用 OOS 胜率最高的策略包。
    主包扫完后顺带扫卫星包（涨停轨），今日执行页两条轨每天都有当天名单。"""
    end = get_last_trade_day()
    import library
    packs = library.list_strategies()
    
    # P2: 多包投票机制 - 用Top3包投票选股票
    if not pack:
        top_list = _top_packs(packs, top_n=3)
        if len(top_list) >= 2:
            # 多包投票：取Top3包的交集
            all_picks = {}
            for pk_name, pk in top_list:
                try:
                    p_pool = pk.get("pool_name", "沪深300")
                    p_top = min(int(pk.get("top_n", 10)), int(top_n))
                    p_codes = (all_pools().get(p_pool) or all_pools().get("沪深300"))
                    picks, _, _, _ = compute_pack_picks(pk, p_codes, end, p_top)
                    for code in picks.index:
                        all_picks[code] = all_picks.get(code, 0) + 1
                except Exception:
                    continue
            
            # 取至少2个包选中的股票
            voted = [c for c, n in all_picks.items() if n >= 2]
            if len(voted) >= top_n:
                # 用最佳包的分数排序
                best_pk = top_list[0][1]
                best_pool = best_pk.get("pool_name", "沪深300")
                best_codes = (all_pools().get(best_pool) or all_pools().get("沪深300"))
                best_picks, pnote, weights, f_series = compute_pack_picks(best_pk, best_codes, end, top_n * 2)
                # 只保留投票通过的股票
                voted_scores = best_picks[best_picks.index.isin(voted)]
                picks = voted_scores.head(top_n)
                # 归因补记参与包名（此前只写"多包投票(N包)"，成员包实战归因断链）
                pack_name = f"多包投票({len(top_list)}包:{'+'.join(n for n, _ in top_list)})"
                note = f"多包投票 · {len(voted)}只候选 · {len(picks)}只入选（{pnote}）"
                pk = top_list[0][1]
            else:
                # 投票不足，回退到最佳包
                pack = top_list[0][0]
                pk = top_list[0][1]
                pack_codes = all_pools().get(pk.get("pool_name", "沪深300")) or all_pools().get("沪深300")
                picks, pnote, weights, f_series = compute_pack_picks(pk, pack_codes, end, top_n)
                pack_name = pack
                note = f"策略包「{pack}」（{pnote}）"
        else:
            pack = _best_pack(packs) if not pack else pack
            pk = packs.get(pack) if pack else None
            if pk:
                pack_codes = all_pools().get(pk.get("pool_name", "沪深300")) or all_pools().get("沪深300")
                picks, pnote, weights, f_series = compute_pack_picks(pk, pack_codes, end, top_n)
                pack_name = pack
                note = f"策略包「{pack}」（{pnote}）"
            else:
                pk = None
                pack_name = None
    else:
        pk = packs.get(pack) if pack else None
        pack_name = pack
    
    if pk:
        pool_name = pk["pool_name"]
        top_n = min(int(pk["top_n"]), int(top_n))
    pools = all_pools()
    codes = pools.get(pool_name) or pools.get("沪深300")

    if pk and 'picks' not in locals():
        pool_name = pk["pool_name"]
        top_n = min(int(pk["top_n"]), int(top_n))
        codes = (all_pools().get(pool_name) or all_pools().get("沪深300"))
        picks, pnote, weights, f_series = compute_pack_picks(pk, codes, end, top_n)
        pack_name = pack or pk.get("name", "")
        note = f"策略包「{pack_name}」（{pnote}）"
    elif not pk:  # 默认组合：最新进化因子 + 内置三件套
        panel = sig.get_panel_cached(codes, end)
        f_series, weights = {}, {}
        factors = _pick_evolved_factors(2)
        for f in factors:
            df = sig.run_factor_code(f["code"], f["name"], codes, end)
            f_series[f["name"]] = df.iloc[:, 0]
            weights[f["name"]] = (1.0, 1)
        for b in ["mom_20d", "vol_20d", "volume_ratio_5_20"]:
            f_series[b] = sig.compute_builtin(panel, b)
        weights.update({"mom_20d": (1.0, 1), "vol_20d": (1.0, -1), "volume_ratio_5_20": (1.0, -1)})
        score = sig.composite_score(f_series, weights)
        picks = score.head(top_n)
        note = f"默认组合（进化因子 {len(factors)} 个参与）"
        pack_name = None

    out = pd.DataFrame({"score": picks})
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    safe_pool = pool_name.replace("/", "_")
    sig._write_parquet_atomic(out, SIGNALS_DIR / f"scan_{safe_pool}_{end}.parquet")

    # 经验库落库（不管对错都记，到期由 outcome_backfill 回填战果）
    import experience
    _norms = sig.scoring_norms(list(weights), pk.get("factors") if pk else None) or {}
    # norm 快照仅在 typed_v2 下写入——legacy 期保持旧 JSON 形状（避免存量包被
    # "zscore" 快照钉死、切换后享受不到 rank 分派；身份与存储分离见 experience.save_pick）
    fcfg = [{"name": n, "kind": ("builtin" if n in sig.BUILTIN_FACTORS else "evolved"),
             "weight": float(w), "direction": int(d),
             **({"norm": _norms[n]} if _norms else {})}
            for n, (w, d) in weights.items()]
    oos = None
    if pk and pk.get("oos_winrate"):
        try:
            oos = float(str(pk["oos_winrate"]).strip("%")) / 100
        except (TypeError, ValueError):
            oos = None
    experience.save_pick(source="sched_pool_scan", pool_name=pool_name, top_n=top_n,
                         method=(pk.get("method") if pk else "默认组合"), filters=(pk.get("filters", []) if pk else []),
                         factors=fcfg, final_scores=picks, pack_name=(pack_name or None),
                         oos_winrate=oos, trade_date=end)

    # 卫星包顺带扫描：给「博涨停」轨出每日名单（今日执行页卫星轨按包名读取）
    sat_msg = ""
    try:
        sat_name = _satellite_pack_name(packs)
        if sat_name and sat_name != pack:
            spk = packs[sat_name]
            scodes = pools.get(spk["pool_name"]) or codes
            spicks, _sn, _sw, _sf = compute_pack_picks(spk, scodes, end, int(spk["top_n"]))
            experience.save_pick(source="sched_satellite_scan", pool_name=spk["pool_name"],
                                 top_n=int(spk["top_n"]), method=spk.get("method"),
                                 filters=spk.get("filters", []), factors=spk["factors"],
                                 final_scores=spicks, pack_name=sat_name, trade_date=end)
            sat_msg = f" · 卫星包「{sat_name}」Top{len(spicks)}"
    except Exception as e:
        sat_msg = f" · 卫星包扫描失败({e})"

    # LE 影子名单：最新 LE 包每日出名单落库但不开仓（le_shadow 不开仓、不上今日执行页），
    # outcome 到期照常结算——积累真实战绩后凭实力参与多包投票竞争
    shadow_msg = ""
    try:
        le_name = f"LE_{pool_name}_current"
        le_pk = packs.get(le_name)
        if le_pk and le_pk.get("status") == "active":
            le_codes = pools.get(le_pk.get("pool_name") or pool_name) or codes
            le_top = int(le_pk.get("top_n", 10))
            le_picks, _ln, _lw, _lf = compute_pack_picks(le_pk, le_codes, end, le_top)
            le_oos = None
            try:
                le_oos = float(str(le_pk.get("oos_winrate") or "").strip("%")) / 100
            except (TypeError, ValueError):
                le_oos = None
            experience.save_pick(source="le_shadow", pool_name=le_pk.get("pool_name") or pool_name,
                                 top_n=le_top, method=le_pk.get("method"),
                                 filters=le_pk.get("filters", []), factors=le_pk["factors"],
                                 final_scores=le_picks, pack_name=le_name,
                                 oos_winrate=le_oos, trade_date=end)
            shadow_msg = f" · LE影子名单 Top{len(le_picks)}"
    except Exception as e:
        shadow_msg = f" · LE影子名单失败({e})"
    return f"{end} {pool_name} 扫描完成：Top{top_n} 已出（{note}）{sat_msg}{shadow_msg}"


def job_auto_scan(pool_name: str = "沪深300", top_n: int = 10, **_ignored) -> str:
    """自动选股：基于因子价值5维评分 + 类型分散 + 动态选因子。
    不依赖策略包，自动从活跃因子池中选取最优组合。"""
    end = get_last_trade_day()
    try:
        picks, note, weights, f_series = auto_select_factors(pool_name, top_n)
    except Exception as e:
        return f"自动选股失败: {e}"

    # 保存信号
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    import signals as sig
    safe_pool = pool_name.replace("/", "_")
    out = pd.DataFrame({"score": picks})
    sig._write_parquet_atomic(out, SIGNALS_DIR / f"auto_{safe_pool}_{end}.parquet")

    # 经验库落库
    import experience
    _norms = sig.scoring_norms(list(weights)) or {}
    fcfg = [{"name": n, "kind": ("builtin" if n in sig.BUILTIN_FACTORS else "evolved"),
             "weight": float(w), "direction": int(d),
             **({"norm": _norms[n]} if _norms else {})}  # norm 快照仅 typed_v2 期写入
            for n, (w, d) in weights.items()]
    experience.save_pick(source="sched_auto_scan", pool_name=pool_name, top_n=top_n,
                         method="auto_select", filters=[], factors=fcfg,
                         final_scores=picks, pack_name=None, trade_date=end)

    return f"{end} 自动选股完成：Top{top_n} 已出（{note}）"


def job_outcome_backfill() -> str:
    """经验库战果回填：到期的历史名单按交易日历结算 5/10/20 日战绩。"""
    import experience
    return experience.backfill_outcomes()


def job_gate_check(pool_name: str = "沪深300") -> str:
    """因子库硬闸门筛查（每日）：新因子过 11 项闸门 + 重算 FSA。"""
    import gaterun

    res = gaterun.run_gates_for_pool(pool_name, only_pending=True)
    return f"硬闸门：评估 {res['evaluated']} · 通过 {res['passed']} · FSA冻结 {res['frozen']}"


def job_loopengine(batch: int = 50, **_ignored) -> str:
    """LoopEngine 演化引擎：每轮 生成→审查→验证→入库（检查点自动保存）。
    按 iteration 轮转7种因子类型：量价/资金流/板块轮动/指数/盘口异动/龙虎榜/爆量抢筹。
    每 4 轮自动插入 1 轮事件定向挖掘（涨停/大涨/跌停轮转）。"""
    from loopengine.engine import LoopEngine, DEFAULT_FACTOR_TYPES

    eng = LoopEngine("沪深300")
    factor_type = DEFAULT_FACTOR_TYPES[eng.state["iteration"] % len(DEFAULT_FACTOR_TYPES)]
    r = eng.run_round(batch=batch, factor_type=factor_type)
    msg = (f"第{r['iteration']}轮[{factor_type}] · 测试{r['tested']} · 过审拒绝{r.get('rejected_review', 0)} · "
           f"LLM否决{r.get('llm_rejected', 0)} · 重复{r.get('dup', 0)} · FSA拦截{r.get('frozen', 0)} · 入库{r.get('passed', 0)} {r.get('new', [])[:3]}")
    ev = r.get("event_round")
    if ev:
        msg += f" | 事件[{ev['kind']}]:测试{ev['tested']}·重复{ev['dup']}·入库{ev['passed']} {ev.get('new', [])[:2]}"
    return msg


def job_multitype_mine(batch_per_type: int = 25, pool_name: str = "沪深300",
                       factor_types: str = "", **_ignored) -> str:
    """多类型因子挖掘：遍历量价/资金流/板块轮动/指数/盘口异动/龙虎榜/爆量抢筹。
    factor_types 为空时挖掘全部类型，逗号分隔指定子集。"""
    from loopengine.engine import LoopEngine, DEFAULT_FACTOR_TYPES

    eng = LoopEngine(pool_name)
    types = [t.strip() for t in factor_types.split(",") if t.strip()] if factor_types else None
    result = eng.run_multi_type_round(batch_per_type=batch_per_type, factor_types=types)
    rounds = result.get("rounds", {})
    parts = []
    for ft, r in rounds.items():
        parts.append(f"{ft}:{r['passed']}个")
    return (f"多类型挖掘完成 · 类型={','.join(result.get('types_mined', []))} · "
            f"{' · '.join(parts)}")


def job_event_mine(kind: str = "涨停", batch: int = 30, horizon: int = 5,
                   pool_name: str = "沪深300", factor_type: str = "量价", **_ignored) -> str:
    """事件定向挖因子：围绕「涨停/大涨/跌停/创新高」做事件目标演化，
    入库前缀 ev_（gate_status=2 事件闸门，区别于收益管线）。
    factor_type 可切换挖掘字段域（如 资金流——汉王复盘：首板的核心是资金突变）。"""
    from loopengine.engine import LoopEngine

    eng = LoopEngine(pool_name)
    r = eng.run_event_round(kind, batch=batch, horizon=horizon, factor_type=factor_type)
    return (f"事件[{kind}|{horizon}日] 第{r['iteration']}轮 · 测试{r['tested']} · "
            f"重复{r['dup']} · FSA拦截{r['frozen']} · 入库{r['passed']} {r['new'][:3]}")


def job_ev_dual_gate(pool_name: str = "沪深300", **_ignored) -> str:
    """ev_ 因子双闸门评估：对事件闸门已通过（gate_status=2）的因子，
    跑放宽版收益闸门（p-value 0.05），通过者升级为 gate_status=3（双闸门通过），
    可进入主选股管线。"""
    import signals as sig
    import gates as G
    from loopengine.tree import build_field_frames, evaluate_tree, parse

    codes = all_pools().get(pool_name, [])
    end = get_last_trade_day()
    panel = sig.get_panel_cached(codes, end, 800, source=datasource.get_loop_source())

    with library._lconn() as c:
        ev_rows = c.execute(
            "SELECT name, code FROM factor_registry WHERE gate_status=2 AND name LIKE 'ev_%'"
        ).fetchall()

    if not ev_rows:
        return "无 gate_status=2 的 ev_ 因子需要评估"

    n_passed = 0
    n_failed = 0
    passed_names = []
    for name, code in ev_rows:
        if not code or not code.startswith("# sexpr:"):
            continue
        try:
            sexpr = code.split("\n", 1)[0][len("# sexpr: "):]
            tree = parse(sexpr)
            vals = evaluate_tree(tree, build_field_frames(panel)).stack().rename("f")
            vals.index = vals.index.set_names(["datetime", "instrument"])
            r = G.evaluate_gates_relaxed(vals, panel)
            if r["pass"]:
                with library._lconn() as c:
                    c.execute("UPDATE factor_registry SET gate_status=3 WHERE name=?", (name,))
                n_passed += 1
                passed_names.append(name)
            else:
                n_failed += 1
        except Exception:
            n_failed += 1

    return (f"ev_双闸门评估：{len(ev_rows)}个因子 · "
            f"通过{n_passed}个 → gate_status=3 · 未通过{n_failed}个" +
            (f" · {', '.join(passed_names[:5])}" if passed_names else ""))


def job_top5_composite() -> str:
    """Top5 复合因子：过硬闸门因子按夏普取 Top5，方向修正等权合成并固化策略包。"""
    import composite

    r = composite.build_top5_composite("沪深300")
    if not r.get("ok"):
        return r.get("msg", "合成失败")
    members = "、".join(f"{m['name']}({'+' if m['direction'] > 0 else '-'})" for m in r["members"])
    return f"Top5复合：IC={r['IC']} 夏普={r['sharpe']} 年化超额={r['年化超额']:.1%} | {members}"


def job_position_track(**_ignored) -> str:
    """持仓跟踪（盘中每5分钟）：名单挂限价委托 → 触及成交开仓 → 持仓止盈/止损/到期平仓（T+1）。

    流程贴近实盘：委托（参考价=名单价）→ 现价触及才成交 → 买入日当天不卖（T+1）。
    手动持仓同样随实盘价滚动止盈/止损自动卖出。
    同时初始化 PriceMonitor 事件驱动监控。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    # 窗口放宽到 15:05：保证收盘后至少命中一次 tick，
    # 当日未成交限价单在 15:00 后及时标失效（position_fill_check 内部判定）
    if not ("0930" <= now.strftime("%H%M") <= "1505"):
        return "非交易时段，跳过"

    import experience
    today = now.strftime("%Y-%m-%d")

    # 初始化 PriceMonitor（从数据库加载持仓）
    try:
        from price_monitor import init_monitor
        n_registered = init_monitor()
    except Exception as e:
        n_registered = 0
        import logging
        logging.getLogger("scheduler").warning(f"PriceMonitor 初始化失败: {e}")

    latest = experience.list_pick_dates(limit=1)
    m0 = experience.position_reconcile(today)  # 双账本对账：先接住孤儿仓再谈开平仓
    m1 = experience.position_open_from_picks(latest[0], today) if latest else "无名单"
    m_fill = experience.position_fill_check(today)
    m2 = experience.position_close_check(today)
    # 顺带撮合模拟柜台的挂单（限价单价格触及即成交）+ 手动持仓止盈/止损自动卖出
    import broker
    n_fill = broker.fill_pending_orders()
    n_stop = broker.check_stop_exits()
    n_expire = broker.expire_day_orders()  # 实盘规则：委托当日有效，15:00 未成交全撤
    parts = ([m0] if m0 != "对账一致" else []) + [m1, m_fill, m2] \
        + ([f"挂单成交 {n_fill} 笔"] if n_fill else []) \
        + ([f"手动止盈止损 {n_stop} 笔"] if n_stop else []) \
        + ([f"日终撤单 {n_expire} 笔"] if n_expire else [])
    if n_registered:
        parts.append(f"PriceMonitor 监控 {n_registered} 个持仓")
    return "；".join(p for p in parts if p)


def job_minute_sync(**_ignored) -> str:
    """盘中分钟线同步（每5分钟）：自选股+当前持仓+今日名单的 1 分钟线落库（ifind_minute 表）。
    页面分时/分钟K 直接读本地库，不再每次直连 iFinD。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    if not ("0930" <= now.strftime("%H%M") <= "1505"):
        return "非交易时段，跳过"

    import experience
    today = now.strftime("%Y-%m-%d")
    codes = set(load_watchlist())
    with experience._conn() as c:
        for r in c.execute("SELECT code FROM positions WHERE status IN ('open','pending')").fetchall():
            codes.add(r[0])
    latest = experience.list_pick_dates(limit=1)
    if latest:
        for r in experience.picks_on_date(latest[0]).itertuples():
            for it in experience.pick_items_detail(int(r.id)).itertuples():
                codes.add(it.code)
    n_rows = n_ok = 0
    for code in codes:
        try:
            n = datasource.fetch_minute_to_db(code, today)
            n_rows += n
            n_ok += 1 if n else 0
        except Exception:
            continue
    return f"{now.strftime('%H:%M')} 分钟线同步：{n_ok}/{len(codes)} 只 · 写入 {n_rows} 行"


_TICK_FAIL = {"n": 0}  # tick_sync 连续全灭计数（TDX 断链退避用）


def job_tick_sync(**_ignored) -> str:
    """盘中tick数据同步（每10秒）：自选股+当前持仓的逐笔成交落库（tick_data 表）。
    分时图页面直接读本地库，不再每次直连数据源。
    退避：TDX 通道连续全灭 30 次后降为每分钟试一次（2026-09-10 起通道协议失配，
    每 10 秒空转 6 台服务器超时白白霸占 interval 线程池）。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    if not ("0930" <= now.strftime("%H%M") <= "1505"):
        return "非交易时段，跳过"

    # 退避：连续全灭时 6 次周期只真正试 1 次
    if _TICK_FAIL["n"] >= 30 and _TICK_FAIL["n"] % 6 != 0:
        _TICK_FAIL["n"] += 1
        return f"TDX断链退避中（第{_TICK_FAIL['n']}次）"

    import experience
    codes = set(load_watchlist())
    with experience._conn() as c:
        for r in c.execute("SELECT code FROM positions WHERE status IN ('open','pending')").fetchall():
            codes.add(r[0])
    latest = experience.list_pick_dates(limit=1)
    if latest:
        for r in experience.picks_on_date(latest[0]).itertuples():
            for it in experience.pick_items_detail(int(r.id)).itertuples():
                codes.add(it.code)

    n_rows = n_ok = 0

    # 批量收集所有 tick 数据，最后一次性写入
    all_tick_rows = []

    for code in codes:
        try:
            ticks = datasource.get_ticks_tdx(code, max_pages=5)
            if ticks is None or ticks.empty:
                continue
            # 收集 tick 数据用于批量写入
            for _, row in ticks.iterrows():
                price = row.get("price", 0) or 0
                vol = int(row.get("vol", 0)) or 0
                all_tick_rows.append((
                    code,
                    str(row.get("datetime", "")),
                    price,
                    vol,
                    price * vol,  # amount = price * volume
                    int(row.get("buyorsell", 0)),
                    'tdx'
                ))
            n_rows += len(ticks)
            n_ok += 1
        except Exception:
            continue

    # 批量写入 SQLite（executemany 比逐行 INSERT 快 10-50 倍）
    if all_tick_rows:
        try:
            with datasource._qconn() as c:
                c.executemany(
                    "INSERT OR IGNORE INTO tick_data (code, datetime, price, volume, amount, buyorsell, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    all_tick_rows
                )
        except Exception as e:
            return f"{now.strftime('%H:%M')} tick同步：{n_ok}/{len(codes)} 只 · 写入失败: {e}"

    # 退避计数：全灭 +1，有货清零
    if n_ok == 0:
        _TICK_FAIL["n"] += 1
    else:
        _TICK_FAIL["n"] = 0
    return f"{now.strftime('%H:%M')} tick同步：{n_ok}/{len(codes)} 只 · 写入 {n_rows} 行"


def job_realtime_kline(**_ignored) -> str:
    """实时日K线聚合（每10秒）：从tick_data聚合今日OHLCV，写入realtime_daily表。
    分时图/K线页面直接读取，实现实时滚动更新。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    if not ("0930" <= now.strftime("%H%M") <= "1505"):
        return "非交易时段，跳过"

    import sqlite3 as sq
    from pathlib import Path
    db_path = Path("/data/market.db")
    today = now.strftime("%Y-%m-%d")

    with sq.connect(str(db_path), timeout=30) as c:
        c.execute("PRAGMA busy_timeout=30000")
        # 创建realtime_daily表（如果不存在）
        c.execute('''
            CREATE TABLE IF NOT EXISTS realtime_daily (
                code TEXT PRIMARY KEY,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume INTEGER,
                amount REAL,
                avg_price REAL,
                prev_close REAL,
                change_pct REAL,
                updated_at TEXT
            )
        ''')

        # 从tick_data聚合今日数据
        rows = c.execute('''
            SELECT code,
                   (SELECT price FROM tick_data WHERE code=t.code AND datetime LIKE ?
                    ORDER BY datetime ASC LIMIT 1) as open,
                   MAX(price) as high,
                   MIN(price) as low,
                   (SELECT price FROM tick_data WHERE code=t.code AND datetime LIKE ?
                    ORDER BY datetime DESC LIMIT 1) as close,
                   SUM(volume) as volume,
                   SUM(price * volume) as amount,
                   SUM(price * volume) / SUM(volume) as avg_price
            FROM tick_data t
            WHERE datetime LIKE ?
            GROUP BY code
        ''', (f'{today}%', f'{today}%', f'{today}%')).fetchall()

        src = "tick"
        if not rows:
            # TDX 通道故障兜底（2026-09-10 起 TDX 全服务器协议失配）：
            # 改用 iFinD 1分钟线（ifind_minute，minute_sync 每5分钟落库）聚合今日 OHLCV
            rows = c.execute('''
                SELECT code,
                       (SELECT open FROM ifind_minute WHERE code=m.code AND datetime LIKE ?
                        ORDER BY datetime ASC LIMIT 1) as open,
                       MAX(high) as high,
                       MIN(low) as low,
                       (SELECT close FROM ifind_minute WHERE code=m.code AND datetime LIKE ?
                        ORDER BY datetime DESC LIMIT 1) as close,
                       SUM(volume) as volume,
                       SUM(amount) as amount,
                       SUM(amount) / SUM(volume) as avg_price
                FROM ifind_minute m
                WHERE datetime LIKE ?
                GROUP BY code
            ''', (f'{today}%', f'{today}%', f'{today}%')).fetchall()
            src = "ifind分钟线"

        n_updated = 0
        for row in rows:
            code, open_p, high, low, close, vol, amt, avg = row
            if not close or vol == 0:
                continue

            # 获取昨收
            prev = c.execute(
                'SELECT close FROM market_daily WHERE code=? ORDER BY date DESC LIMIT 1',
                (code,)
            ).fetchone()
            prev_close = prev[0] if prev else open_p
            change_pct = (close / prev_close - 1) * 100 if prev_close else 0

            c.execute('''
                INSERT OR REPLACE INTO realtime_daily 
                (code, date, open, high, low, close, volume, amount, avg_price, 
                 prev_close, change_pct, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (code, today, open_p, high, low, close, vol, amt, avg,
                  prev_close, round(change_pct, 2), now.strftime('%Y-%m-%d %H:%M:%S')))
            n_updated += 1

    return f"{now.strftime('%H:%M')} 实时日K线聚合：{n_updated} 只（{src}）"


def job_trade_simulate() -> str:
    """模拟交易回填：对经验库新名单按默认规则（止盈15%/止损-8%/持有20日）逐笔模拟平仓。"""
    import experience

    return experience.backfill_trades()


def job_auction_confirm() -> str:
    """竞价确认（09:26，集合竞价落锤后）：对昨晚名单逐只检查竞价表现，标记回避信号。

    规则（保守，宁缺毋滥）：
      回避 = 竞价低开 ≤ -2%（隔夜利空跳空）或 竞价量 < 20日均量的 0.3%（无量承接）
    结果存 signals/auction_<当日>.parquet，选股列表页次日名单旁显示确认状态。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    import experience

    dates = experience.list_pick_dates(5)
    if not dates:
        return "无选股名单，跳过"
    # 名单必须足够新：最近名单日期 = 上一交易日（周末近似往前推）
    prev = now - pd.Timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= pd.Timedelta(days=1)
    if dates[0] < prev.strftime("%Y-%m-%d"):
        return f"最新名单为 {dates[0]}（过旧），跳过"
    picks = experience.picks_on_date(dates[0])
    if picks.empty:
        return f"日期 {dates[0]} 无名单数据"
    picks = picks[picks["source"] != "le_shadow"]  # 影子名单不做竞价确认
    if picks.empty:
        return f"日期 {dates[0]} 无正式名单（仅影子）"
    # 优先确认主轨名单（picks 按 id 倒序，影子名单最后插入会在最前）
    sched = picks[picks["source"] == "sched_pool_scan"]
    target = sched.iloc[0] if not sched.empty else picks.iloc[0]
    items = experience.pick_items_detail(int(target["id"]))
    rows = []
    for code in items["code"]:
        try:
            snap = datasource.get_realtime_snapshot(code)
            price, prev_close = snap.get("price"), snap.get("prev_close")
            if not price or not prev_close:
                continue
            gap = price / prev_close - 1
            d40 = (now - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
            daily = datasource.get_daily(code, d40, now.strftime("%Y-%m-%d"))
            avg20 = daily["$volume"].tail(20).mean() if not daily.empty else None
            # 快照 volume 单位为手，×100 对齐日线（股）
            ratio = (snap.get("volume") or 0) * 100 / avg20 if avg20 else None
            verdict = "回避" if (gap <= -0.02 or (ratio is not None and ratio < 0.003)) else "确认"
            rows.append({"code": code, "竞价涨幅%": round(gap * 100, 2),
                         "竞价量比%": round(ratio * 100, 2) if ratio is not None else None,
                         "竞价结论": verdict})
        except Exception:
            continue
    if not rows:
        return "竞价数据为空（可能尚未开盘）"
    out = pd.DataFrame(rows)
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    day = now.strftime("%Y-%m-%d")
    sig._write_parquet_atomic(out, SIGNALS_DIR / f"auction_{day}.parquet")
    avoid = out[out["竞价结论"] == "回避"]["code"].tolist()
    return f"{day} 竞价确认：{len(rows)} 只 · 回避 {len(avoid)} 只（{','.join(avoid) or '无'}）"


def job_quote_collect(pool_name: str = "沪深300", interval_sec: int = 30) -> str:
    """行情快照采集：交易时段内批量拉取并落库（给 1分钟涨速/现手 供历史）。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5 or not ("0915" <= now.strftime("%H%M") <= "1505"):
        return "非交易时段，跳过"
    pools = all_pools()
    codes = pools.get(pool_name) or []
    if not codes:
        return f"池 {pool_name} 为空，跳过"
    rows = datasource.get_batch_snapshots(codes)
    n = datasource.save_snapshots(rows)
    return f"{now.strftime('%H:%M:%S')} 采集 {pool_name} {n} 只快照"


def job_snapshots_archive(**_ignored) -> str:
    """每日快照归档：将前一日的 quote_snapshots 聚合为日均值写入 archive 表（永久保留）。"""
    yesterday = (datetime.now() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    n = datasource.archive_snapshots_daily(yesterday)
    return f"归档 {yesterday} 快照 {n} 只"


def job_sector_flow_collect(interval_sec: int = 30, **_ignored) -> str:
    """板块资金流采集：交易时段内抓板块快照+资金净流入落库
    （sector_flow_snapshots / sector_inflow_snapshots），给 🌐资金趋势/🏛板块行情 页供数。
    页面开关的采集线程随容器重启消失，此任务让采集不依赖页面是否打开。"""
    from zoneinfo import ZoneInfo

    import sectorflow as sf

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5 or not ("0915" <= now.strftime("%H%M") <= "1505"):
        return "非交易时段，跳过"
    n = sf.save_sector_spot(sf.fetch_sector_spot())
    sf.save_sector_inflow_snapshot()
    return f"{now.strftime('%H:%M:%S')} 板块快照 {n} 个 + 资金流快照已存"


def job_ifind_daily_sync(pool_name: str = "自选股", lookback_days: int = 10, **_ignored) -> str:
    """iFinD 日线自动入库：每日盘后把自选股/池子的日线增量写入 market.db
    （market_daily 表，source='ths_ifind'）。INSERT OR REPLACE 幂等，
    lookback 留冗余覆盖缺数；交易日判断交给 cron（mon-fri），节假日空跑无害。"""
    from zoneinfo import ZoneInfo

    codes = load_watchlist() if pool_name == "自选股" else (all_pools().get(pool_name) or [])
    if not codes:
        return f"{pool_name} 为空，跳过"
    now = datetime.now(ZoneInfo(TZ))
    end = now.strftime("%Y-%m-%d")
    # 日历日 ≈ 交易日×2+5，保证覆盖 lookback_days 个交易日
    start = (now - pd.Timedelta(days=int(lookback_days) * 2 + 5)).strftime("%Y-%m-%d")
    total, failed = 0, []
    for code in codes:
        try:
            total += datasource._ths_fetch_daily(code, start, end)
        except Exception:
            failed.append(code)
    msg = f"{end} iFinD 日线入库：{len(codes)} 只 → {total} 行（回看 {lookback_days} 个交易日）"
    if failed:
        msg += f" · 失败 {len(failed)} 只（{','.join(failed[:5])}{'…' if len(failed) > 5 else ''}）"
    return msg


def job_ifind_calendar(exchange: str = "SSE", **_ignored) -> str:
    """iFinD 交易日历入库（ifind_calendar 表）——给各页面提供真实交易日历。"""
    df, res, err = datasource.ths_trade_dates(exchange)
    if err not in (0, None) or df is None or df.empty:
        return f"交易日历拉取失败 err={err}（凭证问题见 📡 iFinD 页状态）"
    col = next((c for c in df.columns if "date" in c.lower() or "time" in c.lower()), df.columns[0])
    dates = sorted(pd.to_datetime(df[col]).dt.strftime("%Y-%m-%d").tolist())
    with datasource._conn() as c:
        c.executemany("INSERT OR IGNORE INTO ifind_calendar(exchange, date) VALUES (?,?)",
                      [(exchange, d) for d in dates])
    return f"{exchange} 交易日历 {len(dates)} 天（{dates[0]}~{dates[-1]}）"


def job_ifind_basic_daily(pool_name: str = "沪深300", **_ignored) -> str:
    """基本面指标包每日入库（ifind_basic_daily 长表 code/date/indicator/value）。
    走行情端点（cmd_history_quotation/THS_HQ）单日截面。
    扩展指标：close/pe_ttm/pb/ps_ttm/pcf_ocf_ttm/totalShares/totalCapital/
              floatCapitalOfAShares/turnoverRatio/eps/bps/roe/dividend_yield"""
    codes = load_watchlist() if pool_name == "自选股" else (all_pools().get(pool_name) or [])
    if not codes:
        return f"{pool_name} 为空，跳过"
    # 扩展到 15+ 指标
    inds = ("close,pe_ttm,pb,ps_ttm,pcf_ocf_ttm,totalShares,totalCapital,"
            "floatCapitalOfAShares,turnoverRatio,eps,bps,roe,dividend_yield,"
            "amplitude,changeRatio")
    today = datetime.now().strftime("%Y-%m-%d")
    df, res, err = datasource.ths_history(codes, inds, today, today, "")
    if err not in (0, None) or df is None or df.empty:
        return f"基本面拉取失败 err={err}（凭证问题见 📡 iFinD 页状态）"
    date_col = "date" if "date" in df.columns else ("time" if "time" in df.columns else None)
    ind_cols = [c for c in df.columns if c not in ("time", "date", "thscode")]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for _, r in df.iterrows():
        rdate = str(r[date_col])[:10] if date_col else today
        for ind in ind_cols:
            v = r.get(ind)
            if pd.notna(v):
                rows.append((str(r.get("thscode")), rdate, ind, float(v), now))
    with datasource._conn() as c:
        c.executemany("INSERT OR REPLACE INTO ifind_basic_daily"
                      "(code,date,indicator,value,fetched_at) VALUES (?,?,?,?,?)", rows)
    return f"{today} 基本面入库 {len(rows)} 行（{len(codes)} 只 × {len(ind_cols)} 指标）"


def job_ifind_announce(pool_name: str = "自选股", days: int = 7, **_ignored) -> str:
    """公告每日抓取得入 ifind_announcements 表（按 seq 去重，幂等）。

    覆盖范围 = 池内 + 今日涨停 + 今日选股名单（2026-09-15 汉王复盘暴露：
    涨停票多半不在自选池，数据包"公告未覆盖"）。"""
    codes = load_watchlist() if pool_name == "自选股" else (all_pools().get(pool_name) or [])
    extra = set()
    try:  # 今日（或最近交易日）涨停票
        with datasource._conn() as c:
            mx = c.execute("SELECT MAX(date) FROM limit_up_watch").fetchone()[0]
            if mx:
                extra |= {r[0] for r in c.execute(
                    "SELECT code FROM limit_up_watch WHERE date=?", (mx,))}
    except Exception:
        pass
    try:  # 最近选股名单
        import experience
        with experience._conn() as ec:
            ld = ec.execute("SELECT MAX(trade_date) FROM picks").fetchone()[0]
        if ld:
            for p in experience.picks_on_date(ld).itertuples():
                with experience._conn() as ec:
                    extra |= {r[0] for r in ec.execute(
                        "SELECT code FROM pick_items WHERE pick_id=?", (p.id,))}
    except Exception:
        pass
    codes = list(dict.fromkeys(list(codes) + sorted(extra)))
    if not codes:
        return f"{pool_name} 为空，跳过"
    df, res, err = datasource.ths_announce(codes, days=int(days))
    if err not in (0, None) or df is None or df.empty:
        return f"近 {days} 天无公告或拉取失败 err={err}"
    df.columns = [str(c).lower() for c in df.columns]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    with datasource._conn() as c:
        for _, r in df.iterrows():
            seq = str(r.get("seq") or "")
            if not seq:
                continue
            cur = c.execute("INSERT OR IGNORE INTO ifind_announcements"
                            "(seq,code,report_date,title,pdf_url,ctime,fetched_at)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (seq, str(r.get("thscode", "")), str(r.get("reportdate", ""))[:10],
                             str(r.get("reporttitle", "")), str(r.get("pdfurl", "")),
                             str(r.get("ctime", "")), now))
            n += cur.rowcount
    return f"公告入库：{len(codes)} 只拉到 {len(df)} 条，新增 {n} 条（seq 去重）"


def job_limit_up_watch(**_ignored) -> str:
    """涨停/放量异动观察清单（盘后 17:25）：今日涨停 + 量比≥3 且涨幅≥5% 的票落库
    limit_up_watch 表，供复盘对话页与次日选股体检用（2026-09-15 汉王复盘：首板票次日无系统视角）。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    sl = datasource.get_stocklist_from_db()
    if sl.empty:
        return "stocklist 为空，跳过"
    date = now.strftime("%Y-%m-%d")

    def _thr(code, name):
        if "ST" in str(name).upper():
            return 4.8
        if code.startswith("BJ"):
            return 29.8
        if code.startswith(("SZ30", "SH688")):
            return 19.8
        return 9.8

    rows = []
    for r in sl.itertuples():
        chg = getattr(r, "change_pct", None)
        if chg is None or pd.isna(chg):
            continue
        thr = _thr(r.code, getattr(r, "name", ""))
        qr = getattr(r, "quantity_ratio", None) or 0
        if chg >= thr:
            kind = "涨停"
        elif (qr and qr >= 3) and chg >= 5:
            kind = "放量异动"
        else:
            continue
        rows.append((date, r.code, getattr(r, "name", ""), float(chg),
                     float(getattr(r, "amount", 0) or 0), float(qr or 0), kind, now.strftime("%F %T")))

    with datasource._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS limit_up_watch(
            date TEXT, code TEXT, name TEXT, chg_pct REAL, amount REAL,
            quantity_ratio REAL, kind TEXT, created_at TEXT,
            PRIMARY KEY(date, code, kind))""")
        c.executemany("INSERT OR REPLACE INTO limit_up_watch VALUES (?,?,?,?,?,?,?,?)", rows)
    return f"{date} 涨停/异动观察清单：{sum(1 for r in rows if r[6]=='涨停')} 只涨停 + {sum(1 for r in rows if r[6]=='放量异动')} 只放量异动"


def job_ifind_financial_sync(pool_name: str = "沪深300", **_ignored) -> str:
    """财务报表自动入库：每日盘后拉取三大报表 + 财务指标写入 ifind_financial 表。

    走 iFinD THS_DateSerial 接口，按报告期去重（INSERT OR REPLACE 幂等）。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    # 财务报表通常按季度发布，非交易日也可同步
    codes = load_watchlist() if pool_name == "自选股" else (all_pools().get(pool_name) or [])
    if not codes:
        return f"{pool_name} 为空，跳过"

    # 拉取最近2年的财务数据
    start = f"{now.year - 2}-01-01"
    end = now.strftime("%Y-%m-%d")

    total = 0
    results = []
    for stmt_type in ["利润表", "资产负债表", "现金流量表", "财务指标"]:
        n = datasource.ths_financial_to_db(codes, stmt_type, start, end)
        total += n
        results.append(f"{stmt_type}:{n}")

    msg = f"{end} 财务报表入库：{len(codes)} 只 · {total} 行（{' + '.join(results)}）"
    return msg


def job_pack_lifecycle(**_ignored) -> str:
    """策略包生命周期（盘后 21:50）：实盘胜率贝叶斯收缩评估，连败自动停赛（M6）。

    shrunk_wr = (n_live×live_wr + 20×oos_wr)/(n_live+20)；n_live≥10 且 shrunk<0.35 → paused。
    先验向 OOS 收缩防小样本误杀；paused 包不参与 _top_packs 投票；恢复需人工/重验。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    import experience
    import library

    packs = library.list_strategies()
    with experience._conn() as c:
        live = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT pack_name, AVG(CASE WHEN pnl_pct>0 THEN 1.0 ELSE 0 END), COUNT(*) "
            "FROM positions WHERE status='closed' AND pack_name IS NOT NULL AND pack_name!='' "
            "GROUP BY pack_name").fetchall()}
    paused = []
    for name, pk in packs.items():
        if pk.get("status") not in (None, "active"):
            continue
        n, wr = (0, None)
        if name in live:
            wr, n = live[name]
            n = int(n)
        if n < 10 or wr is None:
            continue  # 样本不足不评判
        oos = pk.get("oos_winrate")
        try:
            oos_wr = float(str(oos).rstrip("%")) / 100 if oos is not None else 0.5
        except (ValueError, AttributeError):
            oos_wr = 0.5
        if oos_wr > 1.5:  # 兼容 0-1 与百分数两种存储
            oos_wr /= 100
        shrunk = (n * wr + 20 * oos_wr) / (n + 20)
        if shrunk < 0.35:
            library.set_strategy_status(name, "paused")
            paused.append(f"{name}（实盘{n}笔 胜率{wr:.0%} 收缩后{shrunk:.0%}）")
    return (f"策略包体检：{len(paused)} 个停赛——" + "、".join(paused)) if paused else \
        "策略包体检：全部在营包通过（无停赛）"


def job_factor_direction(**_ignored) -> str:
    """因子方向状态机日更（21:45，体检之后）：读最新评分卡，磁滞更新方向。
    （M5：反转需 符号翻转+|ICIR|>0.08+过冷却期，防震荡期 whipsaw 反复横跳）"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    import factor_eval as fe
    with datasource._conn() as c:
        sc = pd.read_sql(
            "SELECT name AS 因子, ic_mean AS 'IC均值', icir AS 'ICIR', ic_winrate, top_winrate "
            "FROM factor_scorecards WHERE eval_date=(SELECT MAX(eval_date) FROM factor_scorecards)", c)
    if sc.empty:
        return "无评分卡，跳过"
    flipped = fe.update_direction_states(sc)
    dm = fe.direction_map()
    if flipped:
        return f"方向状态机：{len(dm)} 因子在册 · 本次反转 {len(flipped)} 个：" + \
               "、".join(f"{n}({'+' if o>0 else '-'}→{'+' if n_>0 else '-'})" for n, (o, n_) in flipped.items())
    return f"方向状态机：{len(dm)} 因子在册 · 今日无反转（磁滞生效）"


def job_risk_guard(**_ignored) -> str:
    """组合风控评估（开盘前 09:20）：净值波动率 → 日 VaR + 熔断状态写
    risk_state.json（开仓闸，position_open_from_picks 每日开盘前读取）。

    P1-3修复：使用实时数据计算当前回撤。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    import experience
    today = now.strftime("%Y-%m-%d")
    # P1-3修复：use_live=True 从实时持仓计算当前回撤
    rk = experience.portfolio_risk(use_live=True)
    if not rk.get("ok"):
        return f"风控评估跳过：{rk.get('reason')}"
    if rk["circuit"]:
        experience._write_risk_flag(today, True,
                                    f"净值回撤 {rk['dd_now']*100:.1f}% 触及熔断线 {rk['circuit_line']*100:.1f}%")
        return (f"⛔ 熔断：净值回撤 {rk['dd_now']*100:.2f}% ≤ 熔断线 {rk['circuit_line']*100:.2f}%"
                f"（σ={rk['sigma']*100:.2f}%），今日停止开新仓")
    experience._write_risk_flag(today, False, "")
    return (f"风控正常：净值 {rk['nav']:.4f} · 日VaR {rk['var_pct']*100:.2f}% · "
            f"当前回撤 {rk['dd_now']*100:.2f}% · 熔断线 {rk['circuit_line']*100:.2f}%")


def job_risk_guard_intraday(**_ignored) -> str:
    """盘中风控重评估（10:00 和 13:30）：用实时持仓重新计算回撤，更新熔断状态。

    P1-4修复：添加盘中熔断重评估，防止09:20后暴跌无法触发熔断。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    hm = now.strftime("%H%M")
    if not ("1000" <= hm <= "1015" or "1330" <= hm <= "1345"):
        return "非盘中重评估时段，跳过"

    import experience
    today = now.strftime("%Y-%m-%d")
    # 检查当前是否已熔断
    is_halt, reason = experience.risk_halt_today(today)
    # P1-3修复：use_live=True 从实时持仓计算当前回撤
    rk = experience.portfolio_risk(use_live=True)
    if not rk.get("ok"):
        return f"盘中风控跳过：{rk.get('reason')}"

    if rk["circuit"]:
        if not is_halt:
            # 新触发熔断
            experience._write_risk_flag(today, True,
                                        f"盘中熔断：净值回撤 {rk['dd_now']*100:.1f}% 触及熔断线 {rk['circuit_line']*100:.1f}%")
            return (f"⛔ 盘中熔断：净值回撤 {rk['dd_now']*100:.2f}% ≤ 熔断线 {rk['circuit_line']*100:.2f}%"
                    f"（σ={rk['sigma']*100:.2f}%），停止开新仓")
        else:
            return f"盘中风控：维持熔断状态（{reason}）"
    else:
        if is_halt:
            # 熔断解除（回撤恢复）
            experience._write_risk_flag(today, False, "")
            return (f"✅ 熔断解除：净值回撤 {rk['dd_now']*100:.2f}% > 熔断线 {rk['circuit_line']*100:.2f}%")
        else:
            return (f"盘中风控正常：回撤 {rk['dd_now']*100:.2f}% · 熔断线 {rk['circuit_line']*100:.2f}%")


def job_account_snapshot(**_ignored) -> str:
    """账户净值每日快照（盘后 15:35）：总资产/现金/持仓市值/净值/回撤落库
    account_nav_daily——回撤统计与组合熔断的真值源（M1）。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    import experience
    day = experience.snapshot_nav_today()
    nv = experience.nav_stats()
    return (f"{day} 净值快照完成：净值 {nv.get('当前净值', 0):.4f} · "
            f"最大回撤 {(nv.get('最大回撤') or 0)*100:.2f}%")


def job_max_close_update(**_ignored) -> str:
    """收盘后更新吊灯止盈基准（盘后 15:35）：用当日确认收盘价更新 max_close。

    P1-1修复：吊灯止盈的 max_close 只用已确认收盘价，不用盘中快照。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    import experience
    today = now.strftime("%Y-%m-%d")
    return experience.update_max_close(today)


def job_daily_report(**_ignored) -> str:
    """每日量化战报自动生成（盘后 18:35）：数据采集 + DeepSeek 分析 + 落库。
    战报同时作为"昨日复盘证据"被 LoopEngine 的 LLM 假设生成引用（复盘→改进闭环）。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    from views.p_daily_report import _collect_all_data, _generate_report
    data = _collect_all_data()
    report = _generate_report(data)
    import experience
    experience.save_daily_report(data["date"], report, data)
    return f"{data['date']} 战报已生成入库（{len(report)} 字）· 已入进化引擎证据链"


# ---------------------------------------------------------------- 战报蒸馏 → 进化信号
# 方案 docs/report-distill-evolution-plan.md v2。铁律：信号只影响出题分布，永不改闸门；
# fail-quiet——蒸馏失败/校验不过 = 当天无信号，引擎照常跑。
def _distill_leaderboard_txt(top: int = 10, flop: int = 5) -> str:
    """因子实战榜（outcome_backfill 之后从 DB 重取——战报落库时的快照是回填前算的）。"""
    import experience
    try:
        flb = experience.factor_leaderboard(fwd=5)
        if flb is None or flb.empty:
            return "（空）"
        win_col = next((c for c in flb.columns if "胜率" in str(c)), None)
        name_col = next((c for c in flb.columns if str(c) in ("因子", "name")), None)
        if not win_col or not name_col:
            return f"（列结构不符: {list(flb.columns)[:6]}）"
        df = flb.dropna(subset=[win_col]).sort_values(win_col, ascending=False)
        lines = ["TOP: " + ", ".join(f"{r[name_col]}({float(r[win_col]):.0%})"
                                     for _, r in df.head(top).iterrows())]
        if len(df) > top:
            lines.append("FLOP: " + ", ".join(f"{r[name_col]}({float(r[win_col]):.0%})"
                                              for _, r in df.tail(flop).iterrows()))
        return "\n".join(lines)
    except Exception as e:
        return f"（获取失败: {e}）"


def _distill_watch_txt(today: str, limit: int = 15) -> str:
    """涨停/放量异动观察清单摘要（14:00 盘中场 + 17:25 盘后场）——hypotheses 最肥原料。"""
    try:
        with datasource._conn() as c:
            rows = c.execute(
                "SELECT kind, name, chg_pct, quantity_ratio FROM limit_up_watch"
                " WHERE date=? ORDER BY amount DESC LIMIT ?", (today, limit)).fetchall()
        if not rows:
            return "（当日无清单）"
        by_kind: dict[str, list[str]] = {}
        for kind, name, chg, qr in rows:
            by_kind.setdefault(kind, []).append(f"{name}({float(chg):+.1f}%,量比{float(qr):.1f})")
        return "\n".join(f"{k} {len(v)}只: " + "、".join(v[:10]) for k, v in by_kind.items())
    except Exception as e:
        return f"（读取失败: {e}）"


def _distill_sr_txt(today: str, limit: int = 8) -> str:
    """支撑阻力共振 Top（18:10 sr_scan 落库）。"""
    try:
        with datasource._conn() as c:
            rows = c.execute(
                "SELECT code, score, resonance, p_hold, sup_dist_atr FROM sr_scan_daily"
                " WHERE date=? ORDER BY score DESC LIMIT ?", (today, limit)).fetchall()
        if not rows:
            return "（当日无扫描）"
        return "\n".join(f"{c_}: score={float(s):.1f} 共振{int(bool(rz))} "
                         f"守住概率{float(p):.2f} 距支撑{float(dd):.1f}ATR"
                         for c_, s, rz, p, dd in rows)
    except Exception as e:
        return f"（读取失败: {e}）"


def _distill_sector_txt(today: str, limit: int = 5) -> str:
    """板块资金流 Top/Bottom（sector_daily 盘后聚合）。"""
    try:
        with datasource._conn() as c:
            tops = c.execute(
                "SELECT sector_name, flow_net, avg_chg_pct FROM sector_daily"
                " WHERE date=? ORDER BY flow_net DESC LIMIT ?", (today, limit)).fetchall()
            bots = c.execute(
                "SELECT sector_name, flow_net, avg_chg_pct FROM sector_daily"
                " WHERE date=? ORDER BY flow_net ASC LIMIT ?", (today, limit)).fetchall()
        if not tops:
            return "（当日无板块数据）"
        fmt = lambda rs: "、".join(f"{n}({float(f) / 1e8:+.1f}亿,{float(c_):+.1f}%)" for n, f, c_ in rs)
        return f"流入: {fmt(tops)}\n流出: {fmt(bots)}"
    except Exception as e:
        return f"（读取失败: {e}）"


def _distill_chat_index_txt(today: str) -> str:
    """复盘数据包索引（当日哪些票被复盘组装过数据包——纯结构化，无 LLM 二手失真）。"""
    try:
        with datasource._conn() as c:
            rows = c.execute(
                "SELECT channel FROM chat_contexts WHERE date=?", (today,)).fetchall()
        if not rows:
            return "（当日无复盘数据包）"
        return "今日已复盘: " + "、".join(sorted({r[0] for r in rows})[:20])
    except Exception as e:
        return f"（读取失败: {e}）"


def job_evolution_distill(**_ignored) -> str:
    """战报蒸馏 → 进化信号（盘后 18:55，必须在 outcome_backfill(18:45) 之后——
    战报 18:35 生成时当天战果尚未回填，排行榜要从 DB 重取；专家评审必修 1）。

    模式开关 /data/evolution_signals.json {"mode": off|shadow|prompt|weights}，缺省 shadow
    （shadow=蒸馏照常落库、引擎只记偏置快照不改行为）。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    from loopengine import evolution_signals as es
    mode = es.get_mode()
    if mode == "off":
        return "进化信号已关闭（mode=off）"
    import os
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return "无 DEEPSEEK_API_KEY，跳过蒸馏"

    import experience
    today = now.strftime("%Y-%m-%d")
    rep = experience.get_daily_report(today)
    content = (rep or {}).get("content") or ""
    if es.is_degenerate_report(content):
        return f"{today} 战报缺失或退化（LLM 不可用/过短），跳过蒸馏"

    def _clip(value, limit):
        raw = str(value or "").strip()
        # 保留换行，优先按完整段落/表格行裁剪；仅在单行过长时再压缩空白
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        text = "\n".join(lines)
        if len(text) <= limit:
            return text
        kept_lines = []
        used = 0
        for line in lines:
            extra = len(line) + (1 if kept_lines else 0)
            if used + extra > limit:
                break
            kept_lines.append(line)
            used += extra
        if kept_lines:
            return "\n".join(kept_lines) + "\n[其余段落已截断]"
        # 单行本身超过预算时，按空白边界兜底；中文无空格时按字符截断
        one = " ".join(raw.split())
        cut = one[:max(0, limit - 8)]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        return cut + " [截断]"

    payload = {
        "report_date": today,
        "report_text": _clip(content, 3000),
        "leaderboard_txt": _clip(_distill_leaderboard_txt(), 1200),
        "watch_txt": _clip(_distill_watch_txt(today), 900),
        "sr_txt": _clip(_distill_sr_txt(today), 900),
        "sector_txt": _clip(_distill_sector_txt(today), 900),
        "chat_index_txt": _clip(_distill_chat_index_txt(today), 700),
    }
    try:
        from llmutil import llm_chat

        system_prompt, user_prompt = es.build_distill_prompt(payload)
        text = llm_chat(system_prompt, user_prompt, max_tokens=800, label="distill")
        d = es.extract_json_obj(text or "")
        if d is None:
            return f"{today} 蒸馏输出无法抽取 JSON（当天无信号）"
        ok, why, clean = es.validate_signals(d)
        if not ok:
            return f"{today} 信号 schema 未过（{why}；当天无信号）"
        import signals as sig
        experience.save_evolution_signal(today, today, clean, norm_scheme=sig.current_norm_scheme())
        return (f"{today} 进化信号已落库（mode={mode}）：effective {len(clean['effective'])} · "
                f"decaying {len(clean['decaying'])} · hypotheses {len(clean['hypotheses'])} · "
                f"confidence {clean['confidence']:.2f}")
    except Exception as e:
        return f"蒸馏调用失败（当天无信号）: {e}"


def job_sr_scan(**_ignored) -> str:
    """支撑/阻力扫描（Density-SR）：每交易日 18:10，全市场四信号融合扫描落库 sr_scan_daily。

    纯本地数据（market_daily 的 ths_ifind 日线），不调外部 API；全市场约 1-2 分钟。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    import density_sr
    n = density_sr.scan_and_store()
    # 注册进因子注册表（kind=tech → 体检经典层每日自动重评，评分卡驱动选因子）
    try:
        import library
        library.sync_factor_registry([
            {"name": f, "kind": "tech", "engine": "density_sr", "factor_type": "量价"}
            for f in sorted(density_sr.SR_FACTOR_NAMES)])
    except Exception:
        pass
    return f"{now.strftime('%Y-%m-%d')} 支撑阻力扫描完成：{n} 只"


def job_ifind_stocklist_sync(**_ignored) -> str:
    """iFinD 全市场A股列表同步（每日09:00执行）。

    调用 datasource.fetch_stocklist_to_db() 拉取全量数据。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    # 交易日判断：周一到周五
    if now.weekday() >= 5:
        return "非交易日，跳过"

    n = datasource.fetch_stocklist_to_db()
    if n > 0:
        return f"{now.strftime('%Y-%m-%d')} A股列表同步完成：{n} 只"
    else:
        return "A股列表同步失败（可能iFinD限流或凭证问题）"


def job_ifind_indexlist_sync(**_ignored) -> str:
    """iFinD 指数列表同步（每日09:05执行，A股列表之后）。

    调用 datasource.fetch_indexlist_to_db()：问财取指数全集（沪深/行业/主题）
    + 宽基种子，iFinD 实时行情补价格，写入 ifind_indexlist 表。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    # 交易日判断：周一到周五
    if now.weekday() >= 5:
        return "非交易日，跳过"

    n = datasource.fetch_indexlist_to_db()
    if n > 0:
        return f"{now.strftime('%Y-%m-%d')} 指数列表同步完成：{n} 条"
    else:
        return "指数列表同步失败（可能iFinD限流或凭证问题）"


def job_ifind_realtime_sync(**_ignored) -> str:
    """iFinD 实时行情快照同步（盘中每5分钟执行）。

    调用 datasource.fetch_realtime_to_db() 写入 ifind_realtime 表。
    同时触发 PriceMonitor 事件驱动评估。
    """
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    # 交易日判断：周一到周五
    if now.weekday() >= 5:
        return "非交易日，跳过"
    # 交易时段判断：09:30-15:05（放宽到 15:05 确保采到 15:00 收盘价快照）
    if not ("0930" <= now.strftime("%H%M") <= "1505"):
        return "非交易时段，跳过"

    n = datasource.fetch_realtime_to_db()

    # 触发 PriceMonitor 事件驱动评估
    if n > 0:
        try:
            from price_monitor import monitor
            watched = monitor.get_watched_codes()
            if watched:
                # 从数据库读取最新价格，触发事件
                import sqlite3
                from pathlib import Path
                db_path = datasource.MKT_DB
                if db_path.exists():
                    with sqlite3.connect(str(db_path), timeout=30) as c:
                        placeholders = ",".join(["?" for _ in watched])
                        rows = c.execute(
                            f"SELECT code, price FROM ifind_realtime WHERE code IN ({placeholders})",
                            watched
                        ).fetchall()
                    events = []
                    for code, price in rows:
                        if price:
                            evts = monitor.on_price_update(code, price, now.strftime("%Y-%m-%d %H:%M:%S"))
                            events.extend(evts)
                    if events:
                        return f"{now.strftime('%H:%M:%S')} 实时快照 {n} 只，触发 {len(events)} 个事件"
        except Exception as e:
            import logging
            logging.getLogger("scheduler").warning(f"PriceMonitor 评估异常: {e}")

        return f"{now.strftime('%H:%M:%S')} 实时快照写入完成：{n} 只"
    else:
        return "实时快照写入失败（可能iFinD限流或无数据）"


def job_ifind_hot_sync(**_ignored) -> str:
    """热码高频快照（每15秒）：持仓+自选+最新名单的实时价落库（ifind_realtime）。

    全市场批次（5分钟/112次调用）物理上快不了；热码仅 ~80 只（2 次调用/2-4秒），
    高频后止盈止损触发与限价单撮合才真正接近实盘（用户要求：交易必须用同花顺
    实盘高频数据驱动）。同时推给 PriceMonitor 事件驱动评估。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    if now.weekday() >= 5:
        return "非交易日，跳过"
    if not ("0925" <= now.strftime("%H%M") <= "1505"):
        return "非交易时段，跳过"

    import experience
    codes = set(load_watchlist())
    # 驾驶舱 5 大指数也进热码（实盘高频价）
    codes.update(["SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905"])
    with experience._conn() as c:
        for r in c.execute("SELECT code FROM positions WHERE status IN ('open','pending')").fetchall():
            codes.add(r[0])
        for r in c.execute("SELECT DISTINCT code FROM broker_positions WHERE shares>0").fetchall():
            codes.add(r[0])
    latest = experience.list_pick_dates(limit=1)
    if latest:
        for r in experience.picks_on_date(latest[0]).itertuples():
            for it in experience.pick_items_detail(int(r.id)).itertuples():
                codes.add(it.code)

    n = datasource.fetch_realtime_hot(sorted(codes))

    # PriceMonitor 事件驱动（读回热码最新价触发止盈止损评估）
    if n > 0:
        try:
            from price_monitor import monitor
            watched = monitor.get_watched_codes()
            if watched:
                prices = experience._latest_prices(watched)
                for code, (price, _o, _p) in prices.items():
                    if price:
                        monitor.on_price_update(code, price, now.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
    return f"{now.strftime('%H:%M:%S')} 热码快照 {n}/{len(codes)} 只"


def job_sector_industry_sync(**_ignored) -> str:
    """行业分类同步（每日盘前，iFinD 问财）：全市场同花顺一级行业 → stock_industry。"""
    import sectorflow as sf
    return sf.sync_industry_ifind()


def job_sector_daily(**_ignored) -> str:
    """板块日线聚合（盘后）：stock_industry 映射 × iFinD 日线 → sector_daily（近3天幂等重算）。
    此前只在页面手动触发回填，停更快两周（2026-09-12 排查）——改为每日自动。"""
    import sectorflow as sf
    sf.backfill_sector_daily(days=3, background=False)
    s = sf.sector_daily_status()
    return f"板块日线聚合完成：{s['rows']} 行 · 最新 {s['max_date']}"


def job_ifind_cleanup(**_ignored) -> str:
    """清理过期数据（每日16:00执行）：SQLite 过期数据。"""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(TZ))
    datasource.cleanup_old_data()
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} SQLite 过期数据清理完成"


def job_le_factor_eval(batch: int = 500, pool_name: str = "沪深300") -> str:
    """因子滚动体检（每日三批）：经典层全量重评 + 进化层边际价值清队列。

    两层结构：
      经典层（tech/builtin，每次全量重评）——评分卡驱动每日策略包选因子/定权重/定方向，
        必须当日新鲜（2026-09-11 踩坑：LE 包用了 17 天前的评分，方向与最新 IC 相反）；
      进化层（loopengine，按边际价值排序清未体检队列）：
        1. 非量价因子优先（资金流/板块轮动/龙虎榜/盘口异动/指数）—— 多元化验证
        2. gate_detail_log 中 IC 最高的未体检因子 —— 高质量因子优先验证
        3. 与已体检因子相关性 < 0.70 的因子 —— 增加多样性
        4. 族配额兜底：同族覆盖越少越优先 —— 避免单一族垄断
    """
    import factor_eval as fe
    import library

    reg = library.get_factor_registry()
    le = reg[reg["engine"] == "loopengine"] if not reg.empty else reg
    # 经典层 = tech/builtin + manual（在线因子实验室手工入库的因子同管线每日体检）
    classic = reg[reg["kind"].isin(["tech", "builtin", "manual"])] if not reg.empty else reg
    if le.empty and classic.empty:
        return "无因子，跳过"

    with library._lconn() as c:
        # 已体检因子
        evaluated = dict(c.execute(
            "SELECT name, MAX(updated_at) FROM factor_scorecards GROUP BY name").fetchall())
        # 闸门IC数据（用于质量排序）
        ic_map = {}
        for row in c.execute(
            "SELECT factor_name, CAST(json_extract(metrics, '$.IC') AS REAL) "
            "FROM gate_detail_log WHERE passed=1"
        ).fetchall():
            ic_map[row[0]] = abs(row[1]) if row[1] else 0

    # 经典层：tech/builtin 全量（每次重评）
    classic_facs = [{"name": r["name"], "kind": r["kind"], "code": None,
                     "first_seen": r.get("first_seen")}
                    for _, r in classic.iterrows()]

    # 进化层：未体检队列按边际价值取剩余配额
    picked = pd.DataFrame()
    if not le.empty:
        # 标记未体检因子
        le = le.assign(
            _eval_at=le["name"].map(lambda n: evaluated.get(n, "")),
            _ic=le["name"].map(lambda n: ic_map.get(n, 0)),
        )
        le["_fam"] = le["family"].fillna("其他").astype(str) if "family" in le.columns else "其他"
        uneval = le[le["_eval_at"] == ""].copy()
    else:
        uneval = le
    if not uneval.empty:
        fam_cov = le.groupby("_fam", dropna=False)["_eval_at"].apply(lambda s: int((s != "").sum()))
        fam_total = le.groupby("_fam", dropna=False).size()
        # 族覆盖率越低，优先级越高（0~1，越小越优先）
        uneval["_fam_score"] = uneval["_fam"].map(
            lambda f: fam_cov.get(f, 0) / max(fam_total.get(f, 1), 1))

        # 因子类型权重：非量价优先
        type_weights = {"量价": 0.0, "资金流": 1.0, "板块轮动": 0.9,
                        "龙虎榜": 0.8, "盘口异动": 0.7, "指数": 0.6}
        uneval["_type_weight"] = uneval["factor_type"].map(
            lambda t: type_weights.get(t, 0.3) if pd.notna(t) else 0.3)

        # 综合边际价值分 = IC质量(40%) + 类型多样性(35%) + 族覆盖(25%)
        uneval["_marginal"] = (
            uneval["_ic"].clip(0, 0.1) / 0.1 * 0.4   # IC归一化到0~1
            + uneval["_type_weight"] * 0.35
            + (1 - uneval["_fam_score"]) * 0.25         # 族覆盖越少分越高
        )

        # 按边际价值降序取剩余配额（经典层占掉的名额先扣）
        quota = max(int(batch) - len(classic_facs), 0)
        picked = uneval.nlargest(quota, "_marginal") if quota else uneval.iloc[0:0]

    codes = all_pools().get(pool_name) or []
    if len(codes) < 30:
        return f"池 {pool_name} 为空，跳过"
    end = get_last_trade_day()
    train_end = trade_day_offset(end, -250)
    facs = classic_facs + [{"name": r["name"], "kind": "loopengine", "code": r["code"],
                            "first_seen": r.get("first_seen")}
                           for _, r in picked.iterrows()]
    if not facs:
        return "所有进化因子已体检，经典层无可评，跳过"

    # P2+P3+P4: 批量计算因子值（一次构建面板，批量计算所有因子，跳过已有缓存，大批次并行）
    if len(facs) > 50:
        card = fe.build_scorecard_parallel(facs, codes, end, train_end=train_end, max_workers=4)
    else:
        card = fe.build_scorecard_batch(facs, codes, end, train_end=train_end)
    library.save_scorecard(card, pool_name, end)
    ok = card.dropna(subset=["ICIR"])

    # 统计边际价值分布
    type_counts = picked["factor_type"].value_counts() if not picked.empty else {}
    type_summary = " ".join(f"{t}:{n}" for t, n in type_counts.items()) if len(type_counts) else ""

    n_le = len(facs) - len(classic_facs)
    n_le_new = n_le - len([n for n in picked["name"] if n in evaluated]) if "name" in picked else n_le
    return (f"体检 {len(facs)} 个（经典 {len(classic_facs)} · 进化 {n_le} · 有效 {len(ok)} 个），"
            f"类型: {type_summary or '—'}，"
            f"进化累计已评估 {len(evaluated) + n_le_new}"
            f"/{len(reg[reg['engine']=='loopengine'])}")


def job_fundflow_sync(pool_name: str = "自选股", lookback_days: int = 30, **_ignored) -> str:
    """个股资金流每日入库（同花顺 iFinD）：按日批量获取全市场资金流数据。"""
    now = datetime.now()
    end = now.strftime("%Y-%m-%d")
    # 获取最近 N 个交易日（简单推算：跳过周末）
    dates = []
    d = now
    while len(dates) < lookback_days:
        if d.weekday() < 5:  # 周一到周五
            dates.append(d.strftime("%Y-%m-%d"))
        d -= pd.Timedelta(days=1)
    dates = sorted(dates)

    n_total = 0
    failed_dates = []
    for date in dates:
        try:
            n = datasource.fetch_fundflow_via_ths(date)
            n_total += n
            if n > 0:
                time.sleep(0.5)  # 限速
        except Exception as e:
            failed_dates.append(date)
            import logging
            logging.getLogger("scheduler").warning(f"资金流入库失败 {date}: {e}")
    msg = f"资金流入库（iFinD）：{len(dates)} 天 → {n_total} 条"
    if failed_dates:
        msg += f" · 失败 {len(failed_dates)} 天"
    return msg


def job_lhb_sync(lookback_days: int = 30, **_ignored) -> str:
    """龙虎榜每日入库（同花顺 iFinD）：按日获取龙虎榜数据。"""
    now = datetime.now()
    end = now.strftime("%Y-%m-%d")
    dates = []
    d = now
    while len(dates) < lookback_days:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= pd.Timedelta(days=1)
    dates = sorted(dates)

    n_total = 0
    failed_dates = []
    for date in dates:
        try:
            n = datasource.fetch_lhb_via_ths(date)
            n_total += n
            if n > 0:
                time.sleep(0.5)
        except Exception as e:
            failed_dates.append(date)
            import logging
            logging.getLogger("scheduler").warning(f"龙虎榜入库失败 {date}: {e}")
    msg = f"龙虎榜入库（iFinD）：{len(dates)} 天 → {n_total} 条"
    if failed_dates:
        msg += f" · 失败 {len(failed_dates)} 天"
    return msg


# ---------------------------------------------------------------- 策略包自动生成
def _get_top_factors_for_pack(pool_name: str, top_n: int = 15) -> list[dict]:
    """从因子评分表取Top因子用于策略包生成（builtin + evolved 同台竞争，按 ICIR 排序）。"""
    import sqlite3
    from pathlib import Path
    
    try:
        db_path = Path("/data/market.db")
        with sqlite3.connect(str(db_path), timeout=30) as c:
            # builtin + evolved 因子都参与，按 ICIR 绝对值排序
            rows = c.execute('''
                SELECT fs.name, fs.kind, fs.ic_mean, fs.icir, fs.ic_winrate,
                       fs.top_winrate, fs.direction, fr.code
                FROM factor_scorecards fs
                LEFT JOIN factor_registry fr ON fs.name = fr.name
                WHERE fs.pool_name = ? AND fs.eval_date >= date('now', '-30 days')
                  AND fs.icir IS NOT NULL
                  AND fs.kind IN ('内置', '技术指标', 'loopengine')
                ORDER BY ABS(fs.icir) DESC
            ''', (pool_name,)).fetchall()
            
            if not rows:
                # 回退：取所有池的因子
                rows = c.execute('''
                    SELECT fs.name, fs.kind, fs.ic_mean, fs.icir, fs.ic_winrate,
                           fs.top_winrate, fs.direction, fr.code
                    FROM factor_scorecards fs
                    LEFT JOIN factor_registry fr ON fs.name = fr.name
                    WHERE fs.eval_date >= date('now', '-30 days')
                      AND fs.icir IS NOT NULL
                      AND fs.kind IN ('内置', '技术指标', 'loopengine')
                    ORDER BY ABS(fs.icir) DESC
                ''').fetchall()
            
            factors = []
            for name, kind, ic_mean, icir, ic_wr, top_wr, direction, code in rows:
                if icir is None:
                    continue
                # kind 映射：scorecards 用中文，策略包用英文
                kind_map = {
                    "内置": "builtin", "技术指标": "tech", 
                    "loopengine": "evolved", "evolved": "evolved",
                    "进化": "evolved", "演化引擎": "evolved",
                }
                factors.append({
                    "name": name,
                    "kind": kind_map.get(kind, "builtin"),
                    "code": code,
                    "ic": abs(float(ic_mean or 0)),
                    "icir": abs(float(icir or 0)),
                    "ic_winrate": float(ic_wr or 0.5),
                    "top_winrate": float(top_wr or 0.5),
                    "direction": 1 if direction == "正向" else -1,
                })
            
            # 去重（同名因子取评分最高的）
            seen = set()
            unique = []
            for f in factors:
                if f["name"] not in seen:
                    seen.add(f["name"])
                    unique.append(f)
            
            return unique[:top_n]
    except Exception:
        return []


def _generate_pack_candidates(pool_name: str, top_n: int = 10,
                               methods: list[str] | None = None) -> list[dict]:
    """生成候选策略包：贪心选因子 + Walk-forward验证。"""
    import factor_eval as fe
    import sqlite3 as sq
    from pathlib import Path
    
    if methods is None:
        methods = ["ICIR加权", "等权", "胜率加权", "均值方差"]
    
    factors = _get_top_factors_for_pack(pool_name, top_n=12)
    if len(factors) < 3:
        return []
    
    codes = (all_pools().get(pool_name) or all_pools().get("沪深300"))
    end = get_last_trade_day()
    
    # 预读 scorecards 缓存（真实 ICIR/IC均值/胜率，替代硬编码）
    scorecards_cache = {}
    try:
        with sq.connect(str(Path("/data/market.db")), timeout=30) as c:
            c.execute("PRAGMA busy_timeout=30000")
            rows = c.execute('''SELECT name, ic_mean, icir, top_winrate
                                FROM factor_scorecards
                                WHERE pool_name=? AND eval_date>=date('now','-30 days')''',
                             (pool_name,)).fetchall()
            for r in rows:
                scorecards_cache[r[0]] = {"ic_mean": r[1], "icir": r[2], "top_winrate": r[3]}
    except Exception:
        pass
    
    # 获取因子值（支持 builtin + evolved）
    factor_vals = {}
    panel = sig.get_panel_cached(codes, end)
    for f in factors:
        try:
            if f["kind"] == "builtin":
                vals = sig.compute_builtin(panel, f["name"])
            elif f.get("code") and "# sexpr:" in f.get("code", ""):
                vals = sig.run_factor_code(f["code"], f["name"], codes, end)
            else:
                continue
            if not vals.dropna().empty:
                factor_vals[f["name"]] = vals
        except Exception:
            continue
    
    if len(factor_vals) < 3:
        return []
    
    candidates = []
    for method in methods:
        try:
            # walk-forward 验证（内部用真实 ICIR 计算权重）
            wf = fe.walk_forward(
                factor_vals, panel, method, top_n, fwd_days=5, step=10, min_factors=2
            )
            if wf.empty or "优化组合扣费超额" not in wf:
                continue
            
            net = wf["优化组合扣费超额"]
            oos_wr = float((net > 0).mean())
            avg_excess = float(net.mean())
            
            # 质量门槛
            if oos_wr < 0.55:
                continue
            
            # 构建策略包定义（使用真实 scorecard 数据赋权）
            selected = list(factor_vals.keys())
            # kind 映射：scorecards 中文 → 策略包英文
            kind_map = {"内置": "builtin", "技术指标": "tech", "loopengine": "evolved"}
            factor_kind = {f["name"]: kind_map.get(f.get("kind", ""), "builtin") for f in factors}
            sc_rows = []
            for n in selected:
                sc = scorecards_cache.get(n, {})
                sc_rows.append({
                    "因子": n,
                    "IC均值": sc.get("ic_mean") or 0,
                    "ICIR": sc.get("icir") or 1.0,
                    "Top组胜率": sc.get("top_winrate") or 0.5,
                })
            sc = pd.DataFrame(sc_rows)
            w = fe.compute_weights(sc, method, selected)
            
            pack_def = {
                "name": f"Auto_{pool_name}_{method}_{len(candidates)+1}",
                "pool_name": pool_name,
                "top_n": top_n,
                "method": method,
                "factors": [{"name": n, "kind": factor_kind.get(n, "builtin"), "weight": w[n][0], "direction": w[n][1]}
                           for n in selected],
                "weights": {n: w[n] for n in selected},
                "filters": ["tradable"],
                "oos_winrate": f"{oos_wr:.0%}",
                "is_winrate": None,  # IS胜率在walk-forward中无法直接获取
                "horizon": "5日",
                "avg_excess": avg_excess,
            }
            
            # 尝试计算IS胜率：用静态回测（非walk-forward）
            try:
                from factor_eval import static_backtest, compute_weights
                # 用固定权重做IS回测
                is_weights = {n: (w[n][0], w[n][1]) for n in selected}
                is_bt = static_backtest(factor_vals, panel, is_weights, top_n, fwd_days=5, cost=0.0025)
                if not is_bt.empty and "组合扣费超额" in is_bt.columns:
                    is_wr = float((is_bt["组合扣费超额"] > 0).mean())
                    pack_def["is_winrate"] = f"{is_wr:.0%}"
            except Exception:
                pass
            
            candidates.append(pack_def)
        except Exception:
            continue
    
    return candidates


def job_strategy_gen(pool_name: str = "沪深300", top_n: int = 10,
                     max_packs: int = 3) -> str:
    """策略包自动生成：每日自动发现、验证、保存新策略包。"""
    import library
    
    # 生成候选包
    candidates = _generate_pack_candidates(pool_name, top_n)
    
    # 按OOS胜率排序，取Top
    candidates.sort(key=lambda x: float(x["oos_winrate"].rstrip("%")), reverse=True)
    saved = []
    
    for pack_def in candidates[:max_packs]:
        try:
            # 检查是否已存在同名包
            existing = library.list_strategies()
            if pack_def["name"] in existing:
                continue
            
            # 保存到strategies表
            library.save_strategy(pack_def["name"], {
                "pool_name": pack_def["pool_name"],
                "top_n": pack_def["top_n"],
                "method": pack_def["method"],
                "factors": pack_def["factors"],
                "filters": pack_def["filters"],
                "oos_winrate": pack_def["oos_winrate"],
                "is_winrate": pack_def.get("is_winrate"),
                "horizon": pack_def.get("horizon"),
            })
            saved.append(f"{pack_def['name']}({pack_def['oos_winrate']})")
        except Exception:
            continue
    
    if saved:
        return f"策略包自动生成：{len(saved)}个新包 → {', '.join(saved)}"
    return f"策略包自动生成：无新包（候选{len(candidates)}个，均未达门槛）"


def job_strategy_revalidate(pool_name: str = "沪深300") -> str:
    """每周重验所有策略包的 OOS 表现，淘汰退化包。"""
    import factor_eval as fe
    import library
    import sqlite3 as sq
    from pathlib import Path
    
    packs = library.list_strategies()
    if not packs:
        return "无策略包需重验"
    
    codes = (all_pools().get(pool_name) or all_pools().get("沪深300"))
    end = get_last_trade_day()
    panel = sig.get_panel_cached(codes, end)
    
    results = []
    for name, pk in packs.items():
        try:
            factors_list = pk.get("factors", [])
            if not factors_list:
                continue
            
            # 取因子值
            factor_vals = {}
            for fac in factors_list:
                try:
                    if fac.get("kind") == "builtin":
                        vals = sig.compute_builtin(panel, fac["name"])
                    elif fac.get("code") and "# sexpr:" in fac.get("code", ""):
                        vals = sig.run_factor_code(fac["code"], fac["name"], codes, end)
                    else:
                        vals = sig.compute_builtin(panel, fac["name"])
                    if not vals.dropna().empty:
                        factor_vals[fac["name"]] = vals
                except Exception:
                    continue
            
            if len(factor_vals) < 2:
                continue
            
            # walk-forward 重验
            wf = fe.walk_forward(
                factor_vals, panel, pk.get("method", "等权"), pk.get("top_n", 10),
                fwd_days=5, step=10, min_factors=2
            )
            if wf.empty or "优化组合扣费超额" not in wf:
                continue
            
            net = wf["优化组合扣费超额"]
            new_oos = float((net > 0).mean())
            old_oos_str = pk.get("oos_winrate", "50%")
            old_oos = float(str(old_oos_str).rstrip("%")) / 100
            
            # 判断退化：OOS 下降 >5% 或跌破 50%
            if old_oos - new_oos > 0.05 or new_oos < 0.50:
                library.update_strategy_oos(name, new_oos, status="degraded")
                results.append(f"{name}: 退化 {old_oos:.0%}→{new_oos:.0%}")
            else:
                library.update_strategy_oos(name, new_oos, status="active")
                results.append(f"{name}: 正常 {new_oos:.0%}")
        except Exception as e:
            results.append(f"{name}: 异常 {e}")
    
    return f"策略包重验完成：{len(results)}个 → " + "; ".join(results[:10])


# ====================================================================
# 🎲 卫星轨 · 独立交易系统
# ====================================================================

def job_satellite_scan(pool_name: str = "沪深300", top_n: int = 5, **_ignored) -> str:
    """卫星轨独立选股：Top5 候选 → 剔除涨停/追高 → LLM 决策（规则兜底）→ 真实资金下单。"""
    import experience
    import broker as bk
    import signals as sig
    import llmutil
    end = get_last_trade_day()
    today = end
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 取卫星包
    import library
    packs = library.list_strategies()
    sat_name = _satellite_pack_name(packs)
    if not sat_name:
        return "卫星轨：无可用事件策略包"

    pk = packs[sat_name]
    pools = all_pools()
    codes = pools.get(pk["pool_name"]) or pools.get(pool_name) or pools.get("沪深300")

    # 2. 选股（Top5）
    try:
        spicks, _sn, _sw, _sf = compute_pack_picks(pk, codes, end, int(top_n))
    except Exception as e:
        return f"卫星轨选股失败：{e}"

    if spicks.empty:
        return "卫星轨：无候选票"

    # 3. 保存 picks 到经验库
    experience.save_pick(source="satellite_scan", pool_name=pk["pool_name"],
                         top_n=int(top_n), method=pk.get("method"),
                         filters=pk.get("filters", []), factors=pk["factors"],
                         final_scores=spicks, pack_name=sat_name, trade_date=end)

    # 4. 组装 LLM 决策数据包
    candidates = []
    try:
        panel = sig.get_panel_cached(codes, end)
        for code in spicks.index:
            item = {"code": code, "name": "", "score": float(spicks[code])}
            # 附加因子值
            if panel is not None and code in panel.index:
                row = panel.loc[code]
                fv = {}
                for f in pk.get("factors", []):
                    fn = f["name"] if isinstance(f, dict) else str(f)
                    if fn in row.index:
                        v = row[fn]
                        if pd.notna(v):
                            fv[fn] = float(v)
                item["factors"] = fv
                # 涨跌幅
                if "close" in row.index and "open" in row.index:
                    try:
                        item["change_pct"] = (float(row["close"]) / float(row["open"]) - 1) * 100
                    except Exception:
                        item["change_pct"] = 0
            candidates.append(item)
    except Exception:
        # 组装失败，直接给 LLM 纯列表
        for code in spicks.index:
            candidates.append({"code": code, "name": "", "score": float(spicks[code]),
                               "factors": {}, "change_pct": 0})

    # 5. 市场上下文（使用卫星轨独立现金池）
    cash = bk._get_satellite_cash()
    hold_count = 0
    try:
        hold_count = len(experience.satellite_positions("open"))
    except Exception:
        pass
    market_context = {
        "regime": "未知",
        "sentiment": 0.5,
        "cash": cash,
        "hold_count": hold_count
    }

    # 6. LLM 决策（失败回退到规则决策）
    llm_result = experience.satellite_llm_decide(candidates, market_context)
    decisions = llm_result.get("decisions", [])
    is_fallback = llm_result.get("fallback", True)

    if decisions and not is_fallback:
        # LLM 决策成功 → 用 satellite_open_from_llm
        msg = experience.satellite_open_from_llm(decisions, cash, today)
        return (f"{end} 卫星轨扫描完成 · {sat_name} · LLM决策 · "
                f"选中{len(decisions)}只 · {msg}")
    else:
        # LLM 失败或未选中 → 规则兜底 Top3
        picks_df = pd.DataFrame({"code": spicks.index, "name": "", "score": spicks.values})
        msg = experience.satellite_open_from_picks(picks_df, cash, today)
        return (f"{end} 卫星轨扫描完成 · {sat_name} · 规则兜底 · {msg}")


def job_satellite_fill(**_ignored) -> str:
    """卫星轨盘中撮合：pending 限价单触及即成交。"""
    import experience
    today = get_last_trade_day()
    return experience.satellite_fill_check(today)


def job_satellite_close(**_ignored) -> str:
    """卫星轨止损/止盈/到期平仓 + 净值更新。"""
    import experience
    today = get_last_trade_day()
    close_msg = experience.satellite_close_check(today)
    nav_msg = experience.satellite_nav_update(today)
    return f"{close_msg} | {nav_msg}"


# ---------------------------------------------------------------- 调度器
JOBS = {
    "update_data": {"name": "📥 每日数据更新", "func": job_update_data,
                    "default": {"enabled": True, "hour": 17, "minute": 35, "params": {}}},
    "ifind_daily_sync": {"name": "📡 iFinD 日线入库（盘后）", "func": job_ifind_daily_sync,
                         "default": {"enabled": True, "hour": 15, "minute": 40,
                                     "params": {"pool_name": "自选股", "lookback_days": 10}}},
    "ifind_calendar": {"name": "🗓 iFinD 交易日历入库", "func": job_ifind_calendar,
                       "default": {"enabled": True, "hour": 8, "minute": 30,
                                   "params": {"exchange": "SSE"}}},
    "ifind_basic_daily": {"name": "🏢 iFinD 基本面指标入库（盘后）", "func": job_ifind_basic_daily,
                          "default": {"enabled": True, "hour": 15, "minute": 50,
                                      "params": {"pool_name": "沪深300"}}},
    "ifind_announce": {"name": "📜 iFinD 公告抓取入库", "func": job_ifind_announce,
                       "default": {"enabled": True, "hour": 16, "minute": 30,
                                   "params": {"pool_name": "自选股", "days": 7}}},
    "limit_up_watch": {"name": "🚀 涨停/放量异动观察清单", "func": job_limit_up_watch,
                       "default": {"enabled": True, "hour": 17, "minute": 25, "params": {}}},
    "limit_up_watch_1400": {"name": "🚀 涨停/放量异动观察（盘中14:00）", "func": job_limit_up_watch,
                            "default": {"enabled": True, "hour": 14, "minute": 0, "params": {}}},
    "ifind_financial_sync": {"name": "💰 iFinD 财务报表入库", "func": job_ifind_financial_sync,
                             "default": {"enabled": True, "hour": 17, "minute": 0,
                                         "params": {"pool_name": "沪深300"}}},
    "ifind_stocklist_sync": {"name": "📋 iFinD A股列表同步（每日）", "func": job_ifind_stocklist_sync,
                             "default": {"enabled": True, "hour": 9, "minute": 0, "params": {}}},
    "sr_scan": {"name": "🧭 支撑阻力扫描（每日盘后）", "func": job_sr_scan,
                "default": {"enabled": True, "hour": 18, "minute": 10, "params": {}}},
    "daily_report": {"name": "📊 每日量化战报（自动生成）", "func": job_daily_report,
                     "default": {"enabled": True, "hour": 18, "minute": 35, "params": {}}},
    "account_snapshot": {"name": "📈 账户净值快照（回撤/熔断真值源）", "func": job_account_snapshot,
                         "default": {"enabled": True, "hour": 15, "minute": 35, "params": {}}},
    "max_close_update": {"name": "📉 吊灯止盈基准更新（收盘价）", "func": job_max_close_update,
                         "default": {"enabled": True, "hour": 15, "minute": 36, "params": {}}},
    "risk_guard": {"name": "🛡 组合风控评估（开盘前）", "func": job_risk_guard,
                   "default": {"enabled": True, "hour": 9, "minute": 20, "params": {}}},
    "risk_guard_intraday": {"name": "🛡 盘中风控重评估（10:00/13:30）", "func": job_risk_guard_intraday,
                            "default": {"enabled": True, "hour": 10, "minute": 0,
                                        "params": {}, "trigger": "cron",
                                        "cron_expr": "0 10,13 * * 1-5"}},
    "factor_direction": {"name": "🧭 因子方向状态机（磁滞日更）", "func": job_factor_direction,
                         "default": {"enabled": True, "hour": 21, "minute": 45, "params": {}}},
    "pack_lifecycle": {"name": "📦 策略包生命周期（连败停赛）", "func": job_pack_lifecycle,
                       "default": {"enabled": True, "hour": 21, "minute": 50, "params": {}}},
    "ifind_indexlist_sync": {"name": "📉 iFinD 指数列表同步（每日）", "func": job_ifind_indexlist_sync,
                             "default": {"enabled": True, "hour": 9, "minute": 5, "params": {}}},
    "ifind_realtime_sync": {"name": "📊 iFinD 实时快照同步（盘中）", "func": job_ifind_realtime_sync,
                            "default": {"enabled": True, "hour": 9, "minute": 30,
                                        "params": {"interval_sec": 300},
                                        "trigger": "interval"}},  # interval_sec 必须放 params 里（调度器从 params 读）
    "ifind_hot_sync": {"name": "⚡ 热码高频快照（盘中·15s）", "func": job_ifind_hot_sync,
                       "default": {"enabled": True, "hour": 9, "minute": 30,
                                   "params": {"interval_sec": 15},
                                   "trigger": "interval"}},
    "ifind_cleanup": {"name": "🧹 iFinD 过期数据清理", "func": job_ifind_cleanup,
                      "default": {"enabled": True, "hour": 16, "minute": 0, "params": {}}},
    "sector_industry_sync": {"name": "🏭 行业分类同步（每日·iFinD）", "func": job_sector_industry_sync,
                             "default": {"enabled": True, "hour": 8, "minute": 50, "params": {}}},
    "sector_daily": {"name": "🏛 板块日线聚合（盘后）", "func": job_sector_daily,
                     "default": {"enabled": True, "hour": 16, "minute": 20, "params": {}}},
    "watchlist_signals": {"name": "📈 个股信号（自选股 × 进化因子）", "func": job_watchlist_signals,
                          "default": {"enabled": True, "hour": 18, "minute": 30, "params": {}}},
    "pool_scan": {"name": "🏛️ 板块/股票池扫描（Top-N）", "func": job_pool_scan,
                  "default": {"enabled": True, "hour": 19, "minute": 0,
                              "params": {"pool_name": "沪深300", "top_n": 10, "pack": ""}}},
    "auto_scan": {"name": "🤖 自动选股（因子价值评分）", "func": job_auto_scan,
                  "default": {"enabled": True, "hour": 19, "minute": 30,
                              "params": {"pool_name": "沪深300", "top_n": 10}}},
    "outcome_backfill": {"name": "🎯 战果回填（经验库）", "func": job_outcome_backfill,
                         "default": {"enabled": True, "hour": 18, "minute": 45, "params": {}}},
    "evolution_distill": {"name": "🧬 进化信号蒸馏（战报→引擎）", "func": job_evolution_distill,
                          "default": {"enabled": True, "hour": 18, "minute": 55, "params": {}}},
    "gate_check": {"name": "🛡 硬闸门筛查（因子库）", "func": job_gate_check,
                   "default": {"enabled": True, "hour": 18, "minute": 0,
                               "params": {"pool_name": "沪深300"}}},
    "quote_collect": {"name": "📡 行情快照采集（盘中）", "func": job_quote_collect,
                      "default": {"enabled": True, "hour": 0, "minute": 0,
                                  "params": {"pool_name": "沪深300", "interval_sec": 30},
                                  "trigger": "interval"}},
    "snapshots_archive": {"name": "🗄 快照日归档（盘后）", "func": job_snapshots_archive,
                          "default": {"enabled": True, "hour": 16, "minute": 5, "params": {}}},
    "sector_flow_collect": {"name": "🌐 板块资金流采集（盘中·资金趋势页供数）", "func": job_sector_flow_collect,
                            "default": {"enabled": True, "hour": 0, "minute": 0,
                                        "params": {"interval_sec": 30},
                                        "trigger": "interval"}},
    "loopengine": {"name": "🧬 LoopEngine 演化引擎", "func": job_loopengine,
                   "default": {"enabled": True, "hour": 0, "minute": 0,
                               "params": {"batch": 50, "interval_sec": 300},
                               "trigger": "interval"}},
    "multitype_mine": {"name": "🌐 多类型因子挖掘（资金流/板块/龙虎榜/盘口/指数）",
                       "func": job_multitype_mine,
                       "default": {"enabled": True, "hour": 1, "minute": 0,
                                   "params": {"batch_per_type": 25, "factor_types": ""}}},
    "top5_composite": {"name": "🏆 Top5 复合因子（每日合成）", "func": job_top5_composite,
                       "default": {"enabled": True, "hour": 18, "minute": 20, "params": {}}},
    "trade_simulate": {"name": "📈 模拟交易回填（每日）", "func": job_trade_simulate,
                       "default": {"enabled": True, "hour": 20, "minute": 5, "params": {}}},
    "position_track": {"name": "📦 持仓跟踪（盘中开平仓）", "func": job_position_track,
                       "default": {"enabled": True, "hour": 9, "minute": 30,
                                   "params": {"interval_sec": 300},
                                   "trigger": "interval"}},
    "minute_sync": {"name": "⏱ 分钟线同步（盘中）", "func": job_minute_sync,
                     "default": {"enabled": True, "hour": 9, "minute": 30,
                                 "params": {"interval_sec": 300},
                                 "trigger": "interval"}},
    "tick_sync": {"name": "📈 Tick数据同步（盘中·秒级·TDX已断链停用）", "func": job_tick_sync,
                  "default": {"enabled": False, "hour": 9, "minute": 30,
                              "params": {"interval_sec": 10},
                              "trigger": "interval"}},  # 2026-09-10 起 TDX 全服务器协议失配；tick 无页面消费，realtime_kline 由 iFinD 分钟线兜底
    "realtime_kline": {"name": "📊 实时日K线聚合（盘中·秒级）", "func": job_realtime_kline,
                       "default": {"enabled": True, "hour": 9, "minute": 30,
                                   "params": {"interval_sec": 10},
                                   "trigger": "interval"}},
    "auction_confirm": {"name": "🔔 竞价确认（09:26 对最新名单）", "func": job_auction_confirm,
                        "default": {"enabled": True, "hour": 9, "minute": 26, "params": {}}},
    "le_factor_eval": {"name": "🧪 LoopEngine 因子滚动体检", "func": job_le_factor_eval,
                       "default": {"enabled": True, "hour": 21, "minute": 30,
                                   "params": {"batch": 1000, "pool_name": "沪深300"}}},
    "fundflow_sync": {"name": "💰 个股资金流入库（盘后·iFinD）", "func": job_fundflow_sync,
                       "default": {"enabled": True, "hour": 17, "minute": 45,
                                   "params": {"pool_name": "自选股", "lookback_days": 30}}},
    "ev_dual_gate": {"name": "🔬 ev_因子双闸门评估（事件→收益）", "func": job_ev_dual_gate,
                     "default": {"enabled": True, "hour": 23, "minute": 0,
                                 "params": {"pool_name": "沪深300"}}},
    "lhb_sync": {"name": "🐉 龙虎榜入库（盘后·iFinD）", "func": job_lhb_sync,
                 "default": {"enabled": True, "hour": 17, "minute": 50,
                             "params": {"lookback_days": 30}}},
    "factor_lifecycle": {"name": "♻️ 因子三层退役扫描（每周）", "func": lambda: __import__("factor_retire").scan(dry_run=False) or "退役扫描完成",
                         "default": {"enabled": True, "hour": 2, "minute": 0, "params": {},
                                     "day_of_week": "sun"}},
    "le_factor_eval_noon": {"name": "🧪 LoopEngine 因子体检（午间）", "func": job_le_factor_eval,
                             "default": {"enabled": True, "hour": 12, "minute": 30,
                                         "params": {"batch": 500, "pool_name": "沪深300"}}},
    "le_factor_eval_pm": {"name": "🧪 LoopEngine 因子体检（盘后）", "func": job_le_factor_eval,
                            "default": {"enabled": True, "hour": 18, "minute": 0,
                                        "params": {"batch": 500, "pool_name": "沪深300"}}},
    "strategy_gen": {"name": "🧬 策略包自动生成", "func": job_strategy_gen,
                     "default": {"enabled": True, "hour": 18, "minute": 30,
                                 "params": {"pool_name": "沪深300", "top_n": 10, "max_packs": 3}}},
    "strategy_revalidate": {"name": "♻️ 策略包重验（每周）", "func": job_strategy_revalidate,
                             "default": {"enabled": True, "hour": 3, "minute": 0,
                                         "params": {"pool_name": "沪深300"},
                                         "day_of_week": "sun"}},
    "satellite_scan": {"name": "🎲 卫星轨选股（独立）", "func": job_satellite_scan,
                       "default": {"enabled": True, "hour": 19, "minute": 10,
                                   "params": {"pool_name": "沪深300", "top_n": 5}}},
    "satellite_fill": {"name": "🎲 卫星轨盘中撮合", "func": job_satellite_fill,
                       "default": {"enabled": True, "hour": 9, "minute": 30,
                                   "params": {"interval_sec": 300},
                                   "trigger": "interval"}},
    "satellite_close": {"name": "🎲 卫星轨止损/结算（盘后）", "func": job_satellite_close,
                        "default": {"enabled": True, "hour": 15, "minute": 35, "params": {}}},
}


class SchedulerManager:
    """调度器管理。容器内有两个进程会实例化（entrypoint 调度进程 + streamlit 页面进程），
    用 /data/scheduler_owner.json 抢锁保证只有 owner 真正注册任务，否则全部任务双跑
    （历史上成对的执行记录与频发 database is locked 的部分原因）。owner 每 60s 心跳，
    超 180s 无心跳允许接管。被动实例只读状态（view/手动触发不受影响）。"""

    _LOCK_FILE = Path(SCHED_STATE_FILE).parent / "scheduler_owner.json"
    _LIVE_FILE = Path(SCHED_STATE_FILE).parent / "scheduler_live.json"
    _HEARTBEAT_S = 60
    _STALE_S = 180

    def _claim_ownership(self) -> bool:
        import os
        my_pid = os.getpid()
        try:
            if self._LOCK_FILE.exists():
                info = json.loads(self._LOCK_FILE.read_text())
                fresh = time.time() - float(info.get("ts", 0)) < self._STALE_S
                if fresh and info.get("pid") != my_pid:
                    return False
        except Exception:
            pass
        self._write_owner(my_pid)
        import threading
        threading.Thread(target=self._heartbeat, args=(my_pid,), daemon=True,
                         name="sched-owner-heartbeat").start()
        return True

    def _write_owner(self, pid: int):
        try:
            self._LOCK_FILE.write_text(json.dumps({"pid": pid, "ts": time.time()}))
        except Exception:
            pass

    def _write_live(self):
        """owner 落盘实时状态（running + 下次运行时间），供被动进程的页面读取。"""
        import os
        try:
            nxt = {}
            for key in self._state():
                job = self.sched.get_job(key)
                nxt[key] = job.next_run_time.strftime("%m-%d %H:%M") if job else None
            self._LIVE_FILE.write_text(json.dumps(
                {"pid": os.getpid(), "ts": time.time(),
                 "running": dict(self._running), "next": nxt}, ensure_ascii=False))
        except Exception:
            pass

    def _heartbeat(self, pid: int):
        last_state_mtime = 0.0
        while True:
            time.sleep(self._HEARTBEAT_S)
            self._write_owner(pid)
            self._write_live()
            # 配置热更新：页面进程（被动）改状态文件后，owner 在本心跳内重新应用
            try:
                m = SCHED_STATE_FILE.stat().st_mtime
                if m != last_state_mtime:
                    last_state_mtime = m
                    self._apply_state()
            except Exception:
                pass

    def __init__(self):
        from apscheduler.executors.pool import ThreadPoolExecutor
        from apscheduler.schedulers.background import BackgroundScheduler

        # 三池隔离（2026-09-09 优化）：
        #   "default" (3) — cron 任务（数据更新/扫描/回填等），盘后高峰不超过 4 个并行
        #   "interval" (2) — 轻量高频 interval（tick_sync/realtime_kline/minute_sync/position_track/ifind_realtime_sync）
        #   "le" (1) — LoopEngine 演化引擎，单次可跑 5-10 分钟，独立池避免霸占 interval 线程
        # ——实测 LoopEngine 2天 524 次执行、avg 105s/max 991s，挤在 interval 池导致
        # tick_sync 间隔从 10s 退化到 14s、8.8% 超 15s。
        self.sched = BackgroundScheduler(
            timezone=TZ,
            executors={
                "default": ThreadPoolExecutor(3),
                "interval": ThreadPoolExecutor(2),
                "le": ThreadPoolExecutor(1),
            },
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
        )
        self.sched.start()
        self._running: dict[str, float] = {}  # job_key → 开始时间戳（供采集监控页显示"正在爬取"）
        import threading
        self._last_lock = threading.Lock()  # scheduler_last.json 读改写并发保护
        self._owner = self._claim_ownership()
        if not self._owner:
            logging.info("[scheduler] 另一进程持有调度权（%s），本实例被动运行（只读/手动触发）",
                         self._LOCK_FILE)
        self._apply_state()

    # ---- 状态持久化 ----
    def _state(self) -> dict:
        saved = load_json(SCHED_STATE_FILE, {})
        return {k: {**v["default"], **saved.get(k, {})} for k, v in JOBS.items()}

    def _save_state(self, st_: dict):
        save_json(SCHED_STATE_FILE, st_)

    def _apply_state(self):
        if not self._owner:
            return  # 被动实例不注册任务，避免双进程双跑
        state = self._state()
        for key, cfg in state.items():
            existing = self.sched.get_job(key)
            if not cfg["enabled"]:
                if existing:
                    self.sched.remove_job(key)
                continue
            if cfg.get("trigger") == "interval":
                executor = "le" if key in ("loopengine",) else "interval"
                params = {"seconds": int(cfg["params"].get("interval_sec", 30)),
                          "executor": executor}
                if existing:
                    try:
                        self.sched.modify_job(key, **params)
                        continue
                    except Exception:
                        self.sched.remove_job(key)
                self.sched.add_job(lambda k=key: self._run(k), "interval", id=key,
                                   **params, replace_existing=True)
            else:
                params = {"day_of_week": "mon-fri", "hour": cfg["hour"], "minute": cfg["minute"]}
                if existing:
                    try:
                        self.sched.modify_job(key, **params)
                        continue
                    except Exception:
                        self.sched.remove_job(key)
                self.sched.add_job(lambda k=key: self._run(k), "cron", id=key,
                                   **params, replace_existing=True)

    # ---- 运行与记录 ----
    def _run(self, key: str):
        cfg = self._state()[key]
        t0 = time.time()
        self._running[key] = t0
        if self._owner:
            self._write_live()
        now_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 推送 JOB_START 事件
        try:
            from event_bus import bus, EventType
            bus.push(EventType.JOB_START, job_key=key, job_name=JOBS[key]["name"],
                     params=cfg.get("params", {}))
        except Exception:
            pass
        try:
            msg = JOBS[key]["func"](**cfg.get("params", {}))
            ok, detail = True, msg
        except Exception as e:
            ok, detail = False, f"{e}"
            traceback.print_exc()
        dur_ms = int((time.time() - t0) * 1000)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 推送 JOB_END 事件
        try:
            from event_bus import bus, EventType
            bus.push(EventType.JOB_END, job_key=key, job_name=JOBS[key]["name"],
                     success=ok, message=detail if isinstance(detail, str) else str(detail),
                     duration_ms=dur_ms)
        except Exception:
            pass
        with self._last_lock:
            last = load_json(SCHED_LAST_FILE, {})
            last[key] = {"time": now, "ok": ok, "msg": detail}
            save_json(SCHED_LAST_FILE, last)
        hist = Path(SCHED_LAST_FILE).parent / "scheduler_history.jsonl"
        with hist.open("a") as f:
            f.write(json.dumps({"job": key, **last[key]}, ensure_ascii=False) + "\n")
            f.flush()
        # 落库 sched_exec_log
        try:
            import library
            with library._lconn() as c:
                c.execute(
                    "INSERT INTO sched_exec_log (job_key,job_name,started_at,finished_at,"
                    "duration_ms,success,message,params,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, JOBS[key]["name"], now, now, dur_ms, 1 if ok else 0,
                     detail if isinstance(detail, str) else str(detail),
                     json.dumps(cfg.get("params", {}), ensure_ascii=False), now))
        except Exception as e:
            logging.warning("[scheduler] sched_exec_log insert failed for %s: %s", key, e)
        self._running.pop(key, None)
        if self._owner:
            self._write_live()

    # ---- 对外 API ----
    def view(self) -> dict:
        state, last = self._state(), load_json(SCHED_LAST_FILE, {})
        # 被动进程（streamlit 页面）从 owner 的实时文件读 running/next
        live = {} if self._owner else load_json(self._LIVE_FILE, {})
        live_running = live.get("running") or {}
        live_next = live.get("next") or {}
        out = {}
        for key, cfg in state.items():
            job = self.sched.get_job(key)
            if self._owner:
                nxt = job.next_run_time.strftime("%m-%d %H:%M") if job else None
                running = self._running.get(key)
            else:
                nxt = live_next.get(key)
                running = live_running.get(key, self._running.get(key))
            out[key] = {**cfg, "label": JOBS[key]["name"], "next": nxt,
                        "last": last.get(key), "running_since": running}
        return out

    def set_enabled(self, key: str, enabled: bool):
        state = self._state()
        state[key]["enabled"] = enabled
        self._save_state(state)
        self._apply_state()

    def set_schedule(self, key: str, hour: int, minute: int):
        state = self._state()
        state[key].update(hour=hour, minute=minute)
        self._save_state(state)
        self._apply_state()

    def set_params(self, key: str, params: dict):
        state = self._state()
        state[key]["params"] = params
        self._save_state(state)

    def run_now(self, key: str):
        import threading

        threading.Thread(target=self._run, args=(key,), daemon=True).start()


@st.cache_resource
def get_scheduler() -> SchedulerManager:
    """Streamlit 进程级单例：容器存活期间调度器一直存在；容器停 = 调度停。"""
    return SchedulerManager()
