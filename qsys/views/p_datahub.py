"""🛰 数据采集中心：所有自动爬取管道的实时状态监控。

管道视角（通道 → 任务 → 落库表 → 新鲜度），默认 5 秒自动刷新：
  - 概览：正在爬取数 / 今日执行 / 成功率 / 异常管道数
  - 管道矩阵：状态灯（🔵正在爬取/🟢正常/🟡待命/🔴异常/⚪停用）+ 通道 + 调度规则
    + 目标表行数与最新数据时间 + 上次执行结果；展开看历史、可手动触发
  - 数据表健康：关键表行数与新鲜度交通灯
  - 实时活动流：最近执行瀑布（sched_exec_log）

状态数据来自 SchedulerManager.view()（同进程内存，含 running_since）+
sched_exec_log / 各业务表 MAX(时间列)。
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

TZ = "Asia/Shanghai"

# ---------------------------------------------------------------- 管道注册表（展示配置）
# channel: 数据源通道 badge；tables: (表名, 时间列|None)；
# sla_trading_sec: 盘中任务——交易时段内表数据年龄超过该秒数判 🔴；
# daily_cutoff: 盘后/例行任务——交易日过了该时刻表应有今日数据（无时间列的表看上次运行）。
PIPELINES = [
    # ---- 盘中高频 ----
    {"key": "tick_sync", "channel": "通达信TCP", "group": "盘中高频",
     "tables": [("tick_data", "datetime")], "sla_trading_sec": 120},
    {"key": "realtime_kline", "channel": "本地聚合", "group": "盘中高频",
     "tables": [("realtime_daily", "updated_at")], "sla_trading_sec": 120},
    {"key": "quote_collect", "channel": "腾讯行情", "group": "盘中高频",
     "tables": [("quote_snapshots", "ts")], "sla_trading_sec": 120},
    {"key": "sector_flow_collect", "channel": "akshare·东财", "group": "盘中高频",
     "tables": [("sector_flow_snapshots", "ts"), ("sector_inflow_snapshots", "ts")],
     "sla_trading_sec": 120},
    {"key": "minute_sync", "channel": "iFinD HTTP", "group": "盘中高频",
     "tables": [("ifind_minute", "datetime")], "sla_trading_sec": 600},
    {"key": "ifind_realtime_sync", "channel": "iFinD HTTP", "group": "盘中高频",
     "tables": [("ifind_realtime", "datetime")], "sla_trading_sec": 600},
    # ---- 盘后批量 ----
    {"key": "ifind_daily_sync", "channel": "iFinD HTTP", "group": "盘后批量",
     "tables": [("market_daily", "date")], "daily_cutoff": "16:00"},
    {"key": "ifind_basic_daily", "channel": "iFinD HTTP", "group": "盘后批量",
     "tables": [("ifind_basic_daily", "date")], "daily_cutoff": "16:30"},
    {"key": "fundflow_sync", "channel": "iFinD 问财", "group": "盘后批量",
     "tables": [("stock_fundflow_daily", "date")], "daily_cutoff": "18:30"},
    {"key": "lhb_sync", "channel": "iFinD 问财", "group": "盘后批量",
     "tables": [("lhb_daily", "date")], "daily_cutoff": "18:30"},
    {"key": "ifind_announce", "channel": "iFinD HTTP", "group": "盘后批量",
     "tables": [("ifind_announcements", "fetched_at")], "daily_cutoff": "17:00"},
    {"key": "update_data", "channel": "GitHub qlib包", "group": "盘后批量",
     "tables": [], "daily_cutoff": "18:00"},
    # ---- 每日例行 ----
    {"key": "ifind_calendar", "channel": "iFinD HTTP", "group": "每日例行",
     "tables": [("ifind_calendar", "date")], "daily_cutoff": "09:00"},
    {"key": "ifind_stocklist_sync", "channel": "iFinD HTTP", "group": "每日例行",
     "tables": [("ifind_stocklist", None)], "daily_cutoff": "09:30"},
    {"key": "ifind_indexlist_sync", "channel": "iFinD HTTP", "group": "每日例行",
     "tables": [("ifind_indexlist", None)], "daily_cutoff": "09:30"},
    {"key": "ifind_cleanup", "channel": "本地清理", "group": "每日例行",
     "tables": [], "daily_cutoff": "16:30"},
]

# 数据表健康看板额外关注的表（不属于任何单一管道）
EXTRA_TABLES = [
    ("sector_daily", "date"),
    ("stock_fundflow_intraday", "datetime"),
]

_CHANNEL_COLOR = {
    "iFinD HTTP": "rgba(26,115,232,.14)", "iFinD 问财": "rgba(26,115,232,.14)",
    "通达信TCP": "rgba(234,67,53,.14)", "腾讯行情": "rgba(52,168,83,.14)",
    "akshare·东财": "rgba(251,188,4,.18)", "GitHub qlib包": "rgba(128,128,128,.15)",
    "本地聚合": "rgba(128,128,128,.15)", "本地清理": "rgba(128,128,128,.15)",
}


# ---------------------------------------------------------------- 数据加载
@st.cache_data(ttl=60, show_spinner=False)
def _table_stats() -> dict:
    """{表名: {"rows": 行数, "latest": 最新时间 str|None}}。"""
    import datasource
    specs = {t: col for pl in PIPELINES for t, col in pl["tables"]}
    specs.update(dict(EXTRA_TABLES))
    out = {}
    try:
        with datasource._qconn() as c:
            for t, col in specs.items():
                try:
                    n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    latest = None
                    if col:
                        r = c.execute(f"SELECT MAX({col}) FROM {t}").fetchone()
                        latest = r[0] if r else None
                    out[t] = {"rows": n, "latest": latest}
                except Exception:
                    out[t] = {"rows": None, "latest": None}
    except Exception:
        pass
    return out


@st.cache_data(ttl=300, show_spinner=False)
def _is_trading_day(day: str) -> bool:
    """优先查 ifind_calendar 交易日历，查不到退化为周一~周五。"""
    try:
        import datasource
        with datasource._qconn() as c:
            total = c.execute("SELECT COUNT(*) FROM ifind_calendar").fetchone()[0]
            if total > 0:
                n = c.execute("SELECT COUNT(*) FROM ifind_calendar WHERE date=?",
                              (day,)).fetchone()[0]
                return n > 0
    except Exception:
        pass
    d = datetime.strptime(day, "%Y-%m-%d")
    return d.weekday() < 5


@st.cache_data(ttl=30, show_spinner=False)
def _today_stats(today: str) -> dict:
    """今日执行次数与成功率（sched_exec_log）。"""
    try:
        import library
        with library._lconn() as c:
            total, ok = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(success),0) FROM sched_exec_log"
                " WHERE created_at >= ?", (today,)).fetchone()
        return {"total": int(total), "ok": int(ok)}
    except Exception:
        return {"total": 0, "ok": 0}


def _job_history(key: str, limit: int = 10) -> pd.DataFrame:
    try:
        import library
        with library._lconn() as c:
            return pd.read_sql(
                "SELECT started_at, success, duration_ms, message FROM sched_exec_log"
                " WHERE job_key=? ORDER BY id DESC LIMIT ?", c, params=(key, limit))
    except Exception:
        return pd.DataFrame()


def _recent_activity(limit: int = 30) -> pd.DataFrame:
    try:
        import library
        with library._lconn() as c:
            return pd.read_sql(
                "SELECT job_name, started_at, success, duration_ms, message"
                " FROM sched_exec_log ORDER BY id DESC LIMIT ?", c, params=(limit,))
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------- 状态判定
def _freshness(pl: dict, stats: dict, now: datetime, trading_day: bool) -> str:
    """ok / stale / idle（待命中：非交易时段或未到执行点）。"""
    sla = pl.get("sla_trading_sec")
    if sla is not None:
        in_window = (trading_day and now.weekday() < 5
                     and "0915" <= now.strftime("%H%M") <= "1505")
        if not in_window:
            return "idle"
        latest = max((stats.get(t, {}).get("latest") or "" for t, _ in pl["tables"]), default="")
        if not latest:
            return "stale"
        age = (now.replace(tzinfo=None)
               - pd.Timestamp(str(latest)).to_pydatetime()).total_seconds()
        return "ok" if age <= sla else "stale"
    cutoff = pl.get("daily_cutoff")
    if cutoff:
        if not trading_day:
            return "idle"
        hh, mm = int(cutoff[:2]), int(cutoff[3:])
        if (now.hour, now.minute) < (hh, mm):
            return "idle"  # 还没到执行点
        today = now.strftime("%Y-%m-%d")
        dated = [(t, col) for t, col in pl["tables"] if col]
        if dated:
            latest = max((stats.get(t, {}).get("latest") or "" for t, _ in dated), default="")
            return "ok" if str(latest)[:10] >= today else "stale"
        return "ok"  # 无落库表的任务（清理/qlib包）由上次执行结果判定
    return "ok"


def _pipe_status(pl: dict, view: dict, stats: dict, now: datetime, trading_day: bool):
    """→ (level, 灯+文案)。level: run/off/bad/idle/ok"""
    cfg = view.get(pl["key"], {})
    since = cfg.get("running_since")
    if since:
        return "run", f"🔵 正在爬取（{int(time.time() - since)}s）"
    if not cfg.get("enabled"):
        return "off", "⚪ 已停用"
    fresh = _freshness(pl, stats, now, trading_day)
    last = cfg.get("last")
    if last and not last.get("ok"):
        return "bad", "🔴 上次失败"
    if fresh == "stale":
        return "bad", "🔴 数据超期"
    if fresh == "idle":
        return "idle", "🟡 待命"
    if not last:
        return "idle", "🟡 未运行过"
    return "ok", "🟢 正常"


def _fmt_schedule(cfg: dict) -> str:
    if cfg.get("trigger") == "interval":
        return f"每 {int(cfg.get('params', {}).get('interval_sec', 30))} 秒"
    return f"交易日 {int(cfg.get('hour', 0)):02d}:{int(cfg.get('minute', 0)):02d}"


def _badge(text: str, bg: str) -> str:
    return (f"<span style='background:{bg};padding:1px 8px;border-radius:8px;"
            f"font-size:12px;white-space:nowrap'>{text}</span>")


# ---------------------------------------------------------------- 页面
def render():
    st.markdown("## 🛰 数据采集中心")
    interval = st.selectbox("自动刷新", [5, 10, 30, 0], index=0,
                            format_func=lambda s: "关闭" if s == 0 else f"{s} 秒",
                            key="datahub_refresh")
    st.caption("管道视角：通道 → 调度任务 → 落库表 → 新鲜度。"
               "🔵正在爬取 · 🟢正常 · 🟡待命（非交易时段/未到执行点）· 🔴异常 · ⚪停用")

    if interval:
        st.fragment(_body, run_every=interval)()
    else:
        _body()


def _body():
    from scheduler import get_scheduler

    now = datetime.now(ZoneInfo(TZ))
    today = now.strftime("%Y-%m-%d")
    trading_day = _is_trading_day(today)
    view = get_scheduler().view()
    stats = _table_stats()
    day_stats = _today_stats(today)

    statuses = {pl["key"]: _pipe_status(pl, view, stats, now, trading_day)
                for pl in PIPELINES}
    running = [(k, v) for k, v in view.items() if v.get("running_since")]
    n_bad = sum(1 for lv, _ in statuses.values() if lv == "bad")

    # ---- 概览 ----
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("🔵 正在爬取", len(running))
    c2.metric("今日执行", f"{day_stats['total']:,} 次")
    c3.metric("今日成功率",
              f"{day_stats['ok'] / day_stats['total'] * 100:.1f}%" if day_stats["total"] else "-")
    c4.metric("异常管道", n_bad, delta=f"-{n_bad}" if n_bad else None, delta_color="inverse")
    c5.metric("交易日", "是" if trading_day else "否")
    if running:
        names = "、".join(f"{v['label']}（{int(time.time() - v['running_since'])}s）"
                          for _, v in running)
        st.info(f"正在爬取：{names}", icon="🔵")

    # ---- 管道矩阵 ----
    for group in ("盘中高频", "盘后批量", "每日例行"):
        pls = [pl for pl in PIPELINES if pl["group"] == group]
        bad_in_group = sum(1 for pl in pls if statuses[pl["key"]][0] == "bad")
        st.markdown(f"### {group}　<small style='opacity:.6'>{len(pls)} 条管道"
                    + (f" · {bad_in_group} 条异常" if bad_in_group else "")
                    + "</small>", unsafe_allow_html=True)
        for pl in pls:
            _render_pipeline(pl, view.get(pl["key"], {}), stats,
                             *statuses[pl["key"]])

    # ---- 数据表健康 ----
    st.markdown("### 数据表健康")
    rows = []
    all_tables = {t: col for pl in PIPELINES for t, col in pl["tables"]}
    all_tables.update(dict(EXTRA_TABLES))
    for t, col in all_tables.items():
        s = stats.get(t, {})
        latest = s.get("latest")
        rows.append({"表": t, "行数": s.get("rows"),
                     "最新数据": str(latest) if latest else "-"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    # ---- 实时活动流 ----
    st.markdown("### 实时活动流")
    act = _recent_activity(30)
    if act.empty:
        st.caption("暂无执行记录")
    else:
        show = act.copy()
        show["状态"] = show["success"].map({1: "✅", 0: "❌"})
        show["耗时"] = show["duration_ms"].map(lambda x: f"{x:,}ms" if pd.notna(x) else "-")
        st.dataframe(
            show[["started_at", "job_name", "状态", "耗时", "message"]].rename(
                columns={"started_at": "时间", "job_name": "任务", "message": "结果"}),
            hide_index=True, width="stretch", height=360)


def _render_pipeline(pl: dict, cfg: dict, stats: dict, level: str, lamp: str):
    label = cfg.get("label", pl["key"])
    tbl_parts = []
    for t, _col in pl["tables"]:
        s = stats.get(t, {})
        n = s.get("rows")
        latest = s.get("latest") or "-"
        tbl_parts.append(f"`{t}` {f'{n:,}' if n is not None else '?'} 行 · 最新 {latest}")
    tbl_txt = "　".join(tbl_parts) if tbl_parts else "无落库表"
    last = cfg.get("last") or {}
    last_txt = (f"{'✅' if last.get('ok') else '❌'} {last.get('time', '?')[5:]}"
                f"　{str(last.get('msg', ''))[:60]}") if last else "暂无记录"

    with st.expander(f"{lamp}　**{label}**　{tbl_txt}", expanded=(level == "bad")):
        st.markdown(
            f"{_badge(pl['channel'], _CHANNEL_COLOR.get(pl['channel'], 'rgba(128,128,128,.15)'))}"
            f"　{_badge(_fmt_schedule(cfg), 'rgba(128,128,128,.12)')}"
            f"　下次运行：{cfg.get('next') or '—'}",
            unsafe_allow_html=True)
        st.caption(f"上次执行：{last_txt}")
        hist = _job_history(pl["key"], 10)
        if not hist.empty:
            show = hist.copy()
            show["状态"] = show["success"].map({1: "✅", 0: "❌"})
            show["耗时"] = show["duration_ms"].map(lambda x: f"{x:,}ms")
            st.dataframe(show[["started_at", "状态", "耗时", "message"]].rename(
                columns={"started_at": "时间", "message": "结果"}),
                hide_index=True, width="stretch")
        if st.button("▶️ 立即执行一次", key=f"datahub_run_{pl['key']}"):
            from scheduler import get_scheduler
            get_scheduler().run_now(pl["key"])
            st.toast(f"已触发：{label}")


render()
