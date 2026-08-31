"""QSYS 共享层：路径配置、qlib 数据访问、RD-Agent 日志解析、SOTA 因子提取。

职责边界：本模块只做"读"——读行情、读 RD-Agent 产出；不含任何因子生成逻辑。
"""

import json
import os
import pickle
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

# ---------------------------------------------------------------- 路径
QLIB_DATA_DIR = Path(os.environ.get("QLIB_DATA_DIR", "/data/qlib/cn_data"))
LOG_DIR = Path(os.environ.get("LOG_DIR", "/work/log"))
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/work"))
DATA_DIR = Path(os.environ.get("QSYS_DATA_DIR", "/data"))
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
GROUPS_FILE = DATA_DIR / "groups.json"          # 自定义板块组
SIGNALS_DIR = DATA_DIR / "signals"              # 定时任务产出
SCHED_STATE_FILE = DATA_DIR / "scheduler_state.json"
SCHED_LAST_FILE = DATA_DIR / "scheduler_last.json"

# RD-Agent 容器内绝对路径前缀 → QSYS 内 /work 的映射
HOST_PREFIX = os.environ.get("LIANGHUA_ROOT", "/home/zk/code/lianghua")

KEY_METRICS = [
    "IC", "ICIR", "Rank IC", "Rank ICIR",
    "1day.excess_return_with_cost.annualized_return",
    "1day.excess_return_with_cost.information_ratio",
    "1day.excess_return_with_cost.max_drawdown",
]

DEFAULT_WATCHLIST = ["SH600519", "SZ300750", "SH601318"]

# 指数池（qlib 社区数据自带 instruments 文件）
POOLS = {
    "沪深300": "csi300",
    "中证500": "csi500",
    "中证800": "csi800",
    "中证1000": "csi1000",
    "全市场": "all",
}


# ---------------------------------------------------------------- qlib 数据
@st.cache_resource
def init_qlib():
    import qlib

    qlib.init(provider_uri=str(QLIB_DATA_DIR), region="cn")
    return True


def _qlib_cal_last_day() -> str:
    """qlib 本地库日历末日——成分股任期文件的天然参照系。"""
    cal = QLIB_DATA_DIR / "calendars" / "day.txt"
    return cal.read_text().splitlines()[-1].strip() if cal.exists() else "未知"


def trade_day_offset(day: str, n: int) -> str:
    """日历上 day 偏移 n 个交易日（负=往前），越界钳到端点。"""
    import bisect

    cal = QLIB_DATA_DIR / "calendars" / "day.txt"
    days = [x.strip() for x in cal.read_text().splitlines() if x.strip()] if cal.exists() else []
    if not days:
        return day
    i = bisect.bisect_left(days, day)
    j = min(max(i + n, 0), len(days) - 1)
    return days[j]


@st.cache_data(ttl=3600)
def get_instruments(pool_file: str = "all") -> list[str]:
    """读取指数成分文件。qlib instruments 文件含历史任期段（code\\tstart\\tend 多行），
    指数池默认取"当前成分"（asof 落在任期内）；all 返回全部。"""
    f = QLIB_DATA_DIR / "instruments" / f"{pool_file}.txt"
    if not f.exists():
        return []
    if pool_file == "all":
        return sorted({line.split("\t")[0].strip() for line in f.read_text().splitlines() if line.strip()})
    # asof 必须用 qlib 日历末日而非今天：任期 end 跟随数据包截止日，
    # 用今天会超出所有任期、筛出空池（2026-08-25 实测踩坑）
    asof = _qlib_cal_last_day()
    codes = set()
    for line in f.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1].strip() <= asof <= parts[2].strip():
            codes.add(parts[0].strip())
    return sorted(codes)


@st.cache_data(ttl=3600)
def get_ohlcv(code: str, start: str, end: str, source: str | None = None) -> pd.DataFrame:
    """单票日线，走数据源层（source=None 时用全局当前源）。"""
    import datasource

    source = source or datasource.get_source()  # 先解析再查缓存，缓存键才含真实源
    return datasource.get_daily(code, start, end, source)


def get_data_source() -> str:
    import datasource

    return datasource.get_source()


def set_data_source(source: str):
    import datasource

    datasource.set_source(source)


@st.cache_data(ttl=1800)
def get_last_trade_day() -> str:
    """评估/取数截止日，跟随当前数据源：
    在线源（easytdx/ths_ifind/akshare）取到今天（抓取器自然截到最近交易日）；
    qlib_local 用 qlib 日历末日（本地库的真实边界）。"""
    try:
        import datasource
        from datetime import datetime as _dt

        if datasource.get_source() != "qlib_local":
            return _dt.now().strftime("%Y-%m-%d")
    except Exception:
        pass
    return _qlib_cal_last_day()


# ---------------------------------------------------------------- 自选/板块组
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def load_watchlist() -> list[str]:
    return load_json(WATCHLIST_FILE, DEFAULT_WATCHLIST.copy())


def load_groups() -> dict:
    """自定义板块组: {组名: [codes]}"""
    return load_json(GROUPS_FILE, {})


def all_pools() -> dict:
    """指数池 + 自定义板块组 合并视图: {显示名: codes 或 pool_file}"""
    pools = {name: get_instruments(pf) for name, pf in POOLS.items()}
    for gname, codes in load_groups().items():
        pools[f"组·{gname}"] = codes
    pools["自选股"] = load_watchlist()
    return pools


# ---------------------------------------------------------------- RD-Agent 日志解析
def trace_last_activity(path: Path) -> float:
    """trace 的最近活动时间(浅扫两层,避免目录 mtime 不冒泡导致误判)。"""
    latest = path.stat().st_mtime
    try:
        for sub in path.iterdir():
            latest = max(latest, sub.stat().st_mtime)
            if sub.is_dir():
                for f in sub.iterdir():
                    latest = max(latest, f.stat().st_mtime)
    except OSError:
        pass
    return latest


def list_traces() -> list[Path]:
    """按最近活动时间倒序(长跑会话靠断点续跑,目录名永远是启动日,按名字排序会把活跃会话排错)。"""
    if not LOG_DIR.exists():
        return []
    traces = [p for p in LOG_DIR.iterdir() if p.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}_", p.name)]
    return sorted(traces, key=trace_last_activity, reverse=True)


def _rebase_workspace(ws_path: str) -> Path | None:
    """RD-Agent 容器内路径 → QSYS 挂载路径。"""
    p = str(ws_path)
    if p.startswith(HOST_PREFIX):
        p = "/work" + p[len(HOST_PREFIX):]
    p = p.replace("/git_ignore_folder", "/git_ignore_folder")
    q = Path(p)
    return q if q.exists() else None


@st.cache_data(ttl=300)
def load_trace(trace_path: str) -> dict:
    """解析一个 RD-Agent trace，返回按轮组织的假设/指标/因子代码/反馈。"""
    rounds, errors, current = [], [], None
    baseline_done = False
    try:
        from rdagent.log.storage import FileStorage

        msgs = list(FileStorage(trace_path).iter_msg())
    except Exception as e:
        return {"rounds": [], "errors": [f"trace 解析失败: {e}"]}

    def _tasks_with_code(content):
        tasks = []
        workspaces = getattr(content, "sub_workspace_list", None) or []
        for i, t in enumerate(getattr(content, "sub_tasks", []) or []):
            code = None
            impl = getattr(t, "factor_implementation", None)
            if isinstance(impl, str) and impl.strip():
                code = impl
            if not code and i < len(workspaces):
                ws = _rebase_workspace(getattr(workspaces[i], "workspace_path", ""))
                if ws:
                    fp = ws / "factor.py"
                    if fp.exists():
                        try:
                            code = fp.read_text()
                        except Exception:
                            pass
            tasks.append({
                "name": getattr(t, "factor_name", getattr(t, "task_name", f"factor_{i}")),
                "description": str(getattr(t, "factor_description", ""))[:500],
                "formulation": str(getattr(t, "factor_formulation", ""))[:300],
                "code": code,
            })
        return tasks

    for msg in msgs:
        tag = re.sub(r"\.evo_loop_\d+", "", msg.tag)
        tag = re.sub(r"Loop_\d+\.[^.]+", "", tag).strip(".")
        try:
            if "hypothesis generation" in tag:
                current = {
                    "round": len([r for r in rounds if r["round"] > 0]) + 1,
                    "hypothesis": str(getattr(msg.content, "hypothesis", msg.content)),
                    "reason": str(getattr(msg.content, "reason", "")),
                    "metrics": None, "feedback": None, "tasks": [],
                    "time": msg.timestamp.strftime("%m-%d %H:%M"),
                }
                rounds.append(current)
            elif "runner result" in tag:
                content = msg.content
                metrics = getattr(content, "result", None)
                if metrics is None:
                    metrics = getattr(content, "__dict__", {}).get("result")
                # 基线实验（Alpha158 基准）只登记一次为 Round 0
                if not baseline_done:
                    based = getattr(content, "based_experiments", None) or []
                    if based:
                        bm = getattr(based[0], "result", None) or getattr(based[0], "__dict__", {}).get("result")
                        if bm is not None:
                            rounds.insert(0, {"round": 0, "hypothesis": "(Alpha158 基线)", "reason": "",
                                              "metrics": bm, "feedback": None, "tasks": [],
                                              "time": msg.timestamp.strftime("%m-%d %H:%M")})
                            baseline_done = True
                if current is None:
                    continue
                current["metrics"] = metrics
                current["tasks"] = _tasks_with_code(content)
            elif tag == "feedback" and current is not None:  # 注意排除 evolving feedback
                current["feedback"] = {
                    "decision": getattr(msg.content, "final_decision", None),
                    "reason": str(getattr(msg.content, "reason", ""))[:800],
                }
        except Exception as e:
            errors.append(f"消息 {msg.tag}: {e}")
    return {"rounds": rounds, "errors": errors}


def get_evolved_factors(only_accepted: bool = False) -> list[dict]:
    """从所有 trace（新→旧）收集带进化的因子代码。

    only_accepted=True 时只取被反馈接受的轮次；否则优先最新轮次。
    返回 [{name, code, round, trace, decision, metrics}]，按时间倒序、按因子名去重。
    """
    out, seen = [], set()
    for tr in list_traces():
        data = load_trace(str(tr))
        for r in reversed(data["rounds"]):
            if r["round"] == 0 or not r["tasks"]:
                continue
            decision = (r["feedback"] or {}).get("decision")
            if only_accepted and decision is not True:
                continue
            for t in r["tasks"]:
                if t["code"] and t["name"] not in seen:
                    seen.add(t["name"])
                    out.append({"name": t["name"], "code": t["code"], "round": r["round"],
                                "trace": tr.name, "decision": decision, "metrics": r["metrics"]})
    return out


def load_positions(trace_path: str, loop_num: int) -> pd.DataFrame | None:
    """从 trace 目录中加载指定 Loop 的每日持仓数据。"""
    # 把 /work/log 映射回容器内路径
    container_path = trace_path
    if trace_path.startswith("/work/log"):
        container_path = HOST_PREFIX + trace_path[len("/work/log"):]
    
    trace_dir = Path(container_path)
    if not trace_dir.exists():
        return None
    
    # 找到对应的 Loop 目录
    loop_dir = trace_dir / f"Loop_{loop_num}"
    if not loop_dir.exists():
        return None
    
    # 查找 positions.csv 文件
    positions_path = loop_dir / "running" / "positions.csv"
    if not positions_path.exists():
        # 尝试在 workspace 中查找
        workspace_dirs = list((loop_dir / "running").glob("*"))
        for wd in workspace_dirs:
            if wd.is_dir():
                p = wd / "positions.csv"
                if p.exists():
                    positions_path = p
                    break
    
    if not positions_path.exists():
        return None
    
    try:
        df = pd.read_csv(positions_path)
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
        return df
    except Exception:
        return None


# ---------------------------------------------------------------- 工具
def metric_subset(series) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    s = pd.Series(series)
    got = [m for m in KEY_METRICS if m in s.index]
    return s.loc[got] if got else s.head(10)


def quick_stats(ret: pd.Series) -> dict:
    ret = ret.dropna()
    if len(ret) < 2:
        return {}
    nav = (1 + ret).cumprod()
    ann = nav.iloc[-1] ** (252 / len(ret)) - 1
    sharpe = ret.mean() / (ret.std() + 1e-12) * np.sqrt(252)
    mdd = ((nav - nav.cummax()) / nav.cummax()).min()
    win = (ret > 0).mean()
    # 全部转字符串：混合 object 列会让 st.table 的 Arrow 转换抛 ArrowTypeError
    return {"年化收益": f"{ann:.2%}", "夏普": f"{sharpe:.2f}", "最大回撤": f"{mdd:.2%}",
            "胜率": f"{win:.2%}", "交易日数": str(len(ret))}
