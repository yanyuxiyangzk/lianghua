"""QSYS 定时任务调度（程序内调度，非系统 cron）。

机制：
  - APScheduler BackgroundScheduler 跑在 QSYS 容器进程内
  - 手动启动后按计划一直跑；容器停止 = 调度停止；容器重启后需在看板重新启动
  - 任务状态持久化在 /data/scheduler_state.json，运行结果在 /data/scheduler_last.json
"""

import json
import tarfile
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from common import (QLIB_DATA_DIR, SCHED_LAST_FILE, SCHED_STATE_FILE, SIGNALS_DIR,
                    all_pools, get_evolved_factors, get_last_trade_day, load_watchlist, save_json, load_json)
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
    out.to_parquet(SIGNALS_DIR / f"watchlist_{end}.parquet")
    return f"{end} 自选股信号完成：{len(codes)} 只 × {len(factors)} 因子"


def job_pool_scan(pool_name: str = "沪深300", top_n: int = 20, pack: str = "") -> str:
    """板块/池任务：综合打分输出 Top-N。指定策略包时按包配置执行。"""
    end = get_last_trade_day()
    import library
    packs = library.list_strategies()
    pk = packs.get(pack) if pack else None

    if pk:
        pool_name, top_n = pk["pool_name"], pk["top_n"]
    pools = all_pools()
    codes = pools.get(pool_name) or pools.get("沪深300")

    f_series, weights = {}, {}
    panel = sig.get_panel_cached(codes, end)
    if pk:  # 策略包：按其因子+权重+方向+过滤器
        evolved_by_name = {f["name"]: f for f in get_evolved_factors(only_accepted=False)}
        for f in pk["factors"]:
            if f["kind"] == "builtin":
                f_series[f["name"]] = sig.compute_builtin(panel, f["name"])
            else:
                fac = evolved_by_name.get(f["name"])
                if not fac or not fac["code"]:
                    continue
                df = sig.run_factor_code(fac["code"], f["name"], codes, end)
                f_series[f["name"]] = df.iloc[:, 0]
            weights[f["name"]] = (f["weight"], f["direction"])
        score = sig.composite_score(f_series, weights)
        survived = sig.apply_filters(score.index.tolist(), panel, pk.get("filters", []))
        picks = score[score.index.isin(survived)].head(top_n)
        note = f"策略包「{pack}」（{len(weights)} 因子）"
    else:  # 默认组合：最新进化因子 + 内置三件套
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

    out = pd.DataFrame({"score": picks})
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    safe_pool = pool_name.replace("/", "_")
    out.to_parquet(SIGNALS_DIR / f"scan_{safe_pool}_{end}.parquet")

    # 经验库落库（不管对错都记，到期由 outcome_backfill 回填战果）
    import experience
    fcfg = [{"name": n, "kind": ("builtin" if n in sig.BUILTIN_FACTORS else "evolved"),
             "weight": float(w), "direction": int(d)} for n, (w, d) in weights.items()]
    oos = None
    if pk and pk.get("oos_winrate"):
        try:
            oos = float(str(pk["oos_winrate"]).strip("%")) / 100
        except (TypeError, ValueError):
            oos = None
    experience.save_pick(source="sched_pool_scan", pool_name=pool_name, top_n=top_n,
                         method=(pk.get("method") if pk else "默认组合"), filters=(pk.get("filters", []) if pk else []),
                         factors=fcfg, final_scores=picks, pack_name=(pack or None),
                         oos_winrate=oos, trade_date=end)
    return f"{end} {pool_name} 扫描完成：Top{top_n} 已出（{note}）"


def job_outcome_backfill() -> str:
    """经验库战果回填：到期的历史名单按交易日历结算 5/10/20 日战绩。"""
    import experience
    return experience.backfill_outcomes()


def job_gate_check(pool_name: str = "沪深300") -> str:
    """因子库硬闸门筛查（每日）：新因子过 11 项闸门 + 重算 FSA。"""
    import gaterun

    res = gaterun.run_gates_for_pool(pool_name, only_pending=True)
    return f"硬闸门：评估 {res['evaluated']} · 通过 {res['passed']} · FSA冻结 {res['frozen']}"


def job_loopengine(batch: int = 30, **_ignored) -> str:
    """LoopEngine 演化引擎：每轮 生成→审查→验证→入库（检查点自动保存）。
    容忍调度界面写入的多余参数（如 pool_name）。"""
    from loopengine.engine import LoopEngine

    eng = LoopEngine("沪深300")
    r = eng.run_round(batch=batch)
    return (f"第{r['iteration']}轮 · 测试{r['tested']} · 过审拒绝{r['rejected_review']} · "
            f"LLM否决{r.get('llm_rejected', 0)} · 重复{r['dup']} · FSA拦截{r['frozen']} · 入库{r['passed']} {r['new'][:3]}")


def job_top5_composite() -> str:
    """Top5 复合因子：过硬闸门因子按夏普取 Top5，方向修正等权合成并固化策略包。"""
    import composite

    r = composite.build_top5_composite("沪深300")
    if not r.get("ok"):
        return r.get("msg", "合成失败")
    members = "、".join(f"{m['name']}({'+' if m['direction'] > 0 else '-'})" for m in r["members"])
    return f"Top5复合：IC={r['IC']} 夏普={r['sharpe']} 年化超额={r['年化超额']:.1%} | {members}"


def job_trade_simulate() -> str:
    """模拟交易回填：对经验库新名单按默认规则（止盈15%/止损-8%/持有20日）逐笔模拟平仓。"""
    import experience

    return experience.backfill_trades()


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


# ---------------------------------------------------------------- 调度器
JOBS = {
    "update_data": {"name": "📥 每日数据更新", "func": job_update_data,
                    "default": {"enabled": False, "hour": 17, "minute": 35, "params": {}}},
    "watchlist_signals": {"name": "📈 个股信号（自选股 × 进化因子）", "func": job_watchlist_signals,
                          "default": {"enabled": False, "hour": 18, "minute": 30, "params": {}}},
    "pool_scan": {"name": "🏛️ 板块/股票池扫描（Top-N）", "func": job_pool_scan,
                  "default": {"enabled": False, "hour": 19, "minute": 0,
                              "params": {"pool_name": "沪深300", "top_n": 20, "pack": ""}}},
    "outcome_backfill": {"name": "🎯 战果回填（经验库）", "func": job_outcome_backfill,
                         "default": {"enabled": False, "hour": 18, "minute": 45, "params": {}}},
    "gate_check": {"name": "🛡 硬闸门筛查（因子库）", "func": job_gate_check,
                   "default": {"enabled": False, "hour": 18, "minute": 0,
                               "params": {"pool_name": "沪深300"}}},
    "quote_collect": {"name": "📡 行情快照采集（盘中）", "func": job_quote_collect,
                      "default": {"enabled": False, "hour": 0, "minute": 0,
                                  "params": {"pool_name": "沪深300", "interval_sec": 30},
                                  "trigger": "interval"}},
    "loopengine": {"name": "🧬 LoopEngine 演化引擎", "func": job_loopengine,
                   "default": {"enabled": False, "hour": 0, "minute": 0,
                               "params": {"batch": 30, "interval_sec": 300},
                               "trigger": "interval"}},
    "top5_composite": {"name": "🏆 Top5 复合因子（每日合成）", "func": job_top5_composite,
                       "default": {"enabled": False, "hour": 18, "minute": 20, "params": {}}},
    "trade_simulate": {"name": "📈 模拟交易回填（每日）", "func": job_trade_simulate,
                       "default": {"enabled": False, "hour": 20, "minute": 5, "params": {}}},
}


class SchedulerManager:
    def __init__(self):
        from apscheduler.schedulers.background import BackgroundScheduler

        self.sched = BackgroundScheduler(timezone=TZ)
        self.sched.start()
        self._apply_state()

    # ---- 状态持久化 ----
    def _state(self) -> dict:
        saved = load_json(SCHED_STATE_FILE, {})
        return {k: {**v["default"], **saved.get(k, {})} for k, v in JOBS.items()}

    def _save_state(self, st_: dict):
        save_json(SCHED_STATE_FILE, st_)

    def _apply_state(self):
        state = self._state()
        for key, cfg in state.items():
            self.sched.remove_job(key) if self.sched.get_job(key) else None
            if not cfg["enabled"]:
                continue
            if cfg.get("trigger") == "interval":
                self.sched.add_job(lambda k=key: self._run(k), "interval", id=key,
                                   seconds=int(cfg["params"].get("interval_sec", 30)),
                                   replace_existing=True)
            else:
                self.sched.add_job(lambda k=key: self._run(k), "cron", id=key,
                                   day_of_week="mon-fri", hour=cfg["hour"], minute=cfg["minute"],
                                   replace_existing=True)

    # ---- 运行与记录 ----
    def _run(self, key: str):
        cfg = self._state()[key]
        try:
            msg = JOBS[key]["func"](**cfg.get("params", {}))
            ok, detail = True, msg
        except Exception as e:
            ok, detail = False, f"{e}"
            traceback.print_exc()
        last = load_json(SCHED_LAST_FILE, {})
        last[key] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": ok, "msg": detail}
        save_json(SCHED_LAST_FILE, last)
        hist = Path(SCHED_LAST_FILE).parent / "scheduler_history.jsonl"
        with hist.open("a") as f:
            f.write(json.dumps({"job": key, **last[key]}, ensure_ascii=False) + "\n")

    # ---- 对外 API ----
    def view(self) -> dict:
        state, last = self._state(), load_json(SCHED_LAST_FILE, {})
        out = {}
        for key, cfg in state.items():
            job = self.sched.get_job(key)
            out[key] = {**cfg, "label": JOBS[key]["name"],
                        "next": (job.next_run_time.strftime("%m-%d %H:%M") if job else None),
                        "last": last.get(key)}
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
