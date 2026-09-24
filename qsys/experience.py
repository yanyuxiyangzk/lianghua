"""经验库：选股结果落库 → 到期回填战果 → 实战榜单（经验积累，供组合进化使用）。

设计：
  - SQLite（/data/experience.db），三表：picks / pick_items / outcomes
  - 每次生成名单自动落库（combo_hash + trade_date 去重，同组合同日覆盖）
  - 定时任务 outcome_backfill 按交易日历回填 5/10/20 日远期战果（不管对错都记）
  - 榜单：策略包实战胜率（可与回测 OOS 胜率对照校准）、因子实战近似归因
"""

import json
import os
import re
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import signals as sig
from common import DATA_DIR, QLIB_DATA_DIR, all_pools, get_last_trade_day

DB_PATH = DATA_DIR / "experience.db"
# 实战结算周期（交易日）：1天/5天/1月/3月/6月 —— 与因子体检的多周期胜率标准一致
FWD_LIST = [1, 5, 20, 60, 120]

# P2-1修复：缓存 strategies 字典，避免每次调用 _is_event_enhanced_pick 重复查询
_strategies_cache = None
_strategies_cache_ts = None

_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    signal_date TEXT, entry_date TEXT, entry_price REAL,
    exit_date TEXT, exit_price REAL, exit_reason TEXT,
    pnl_pct REAL, hold_days INTEGER, rules TEXT,
    UNIQUE(pick_id, code)
);
"""

_POSITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL, name TEXT,
    buy_date TEXT NOT NULL, buy_price REAL, buy_ts TEXT,
    pick_id INTEGER, source TEXT, pack_name TEXT,
    status TEXT DEFAULT 'open',
    sell_date TEXT, sell_price REAL, sell_ts TEXT, sell_reason TEXT,
    pnl_pct REAL, hold_days INTEGER,
    created_at TEXT, closed_at TEXT,
    UNIQUE(code, buy_date, source)
);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    combo_hash TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,            -- manual_picker / sched_pool_scan
    pool_name TEXT, pack_name TEXT, method TEXT,
    top_n INTEGER, filters TEXT, factors TEXT,   -- JSON
    oos_winrate_at_save REAL,
    theory_family TEXT, regime_scope TEXT, evidence_type TEXT, risk_class TEXT,
    UNIQUE(combo_hash, trade_date)
);
CREATE TABLE IF NOT EXISTS pick_items (
    pick_id INTEGER NOT NULL, code TEXT NOT NULL, rank INTEGER, score REAL,
    UNIQUE(pick_id, code)
);
CREATE TABLE IF NOT EXISTS pick_decision_evidence (
    pick_id INTEGER PRIMARY KEY,
    market_state TEXT,
    sector_json TEXT,
    financial_json TEXT,
    technical_json TEXT,
    risk_json TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
    pick_id INTEGER NOT NULL, fwd_days INTEGER NOT NULL, eval_date TEXT,
    avg_ret REAL, pool_median REAL, excess REAL, hit INTEGER,
    UNIQUE(pick_id, fwd_days)
);
"""


_DAILY_REPORTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_reports (
    date TEXT PRIMARY KEY,
    content TEXT,
    account_json TEXT,
    positions_json TEXT,
    fills_json TEXT,
    factors_json TEXT,
    strategies_json TEXT,
    market_json TEXT,
    stats_json TEXT,
    data_hash TEXT,
    pnl_today REAL,
    pnl_total REAL,
    generated_at TEXT
);
"""

# 进化信号（战报蒸馏，docs/report-distill-evolution-plan.md v2）：
# 蒸馏 job 写 signals；引擎 shadow 挂钩回写 shadow_bias_json（若启用会怎么偏置的快照）。
_EVOLUTION_SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_signals (
    date TEXT PRIMARY KEY,          -- 信号生成日
    report_date TEXT,               -- 蒸馏依据的战报日期（消费端新鲜度校验）
    signals TEXT,                   -- 蒸馏 JSON（已过 schema 校验）
    shadow_bias_json TEXT,          -- shadow 期偏置快照（引擎侧回写）
    norm_scheme TEXT,               -- 落库时归一化口径（lift 分层用）
    created_at TEXT
);
"""


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")  # 写冲突时等待30秒，避免 database is locked
    c.execute("PRAGMA journal_mode=WAL")    # 读写不互斥（此前默认 DELETE，锁升级死锁频发）
    c.executescript(_SCHEMA)
    c.executescript(_TRADES_SCHEMA)
    c.executescript(_POSITIONS_SCHEMA)
    c.executescript(_DAILY_REPORTS_SCHEMA)
    # 兼容旧库：日报输入快照哈希用于同日幂等缓存
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(daily_reports)").fetchall()}
        if "data_hash" not in cols:
            c.execute("ALTER TABLE daily_reports ADD COLUMN data_hash TEXT")
    except Exception:
        pass
    c.executescript(_EVOLUTION_SIGNALS_SCHEMA)
    # 迁移：positions 增加限价字段（委托买入用，老库无此列则补上）
    pcols = [r[1] for r in c.execute("PRAGMA table_info(positions)")]
    if "limit_price" not in pcols:
        c.execute("ALTER TABLE positions ADD COLUMN limit_price REAL")
    if "shares" not in pcols:
        c.execute("ALTER TABLE positions ADD COLUMN shares INTEGER")  # 成交股数（100股整手）
    if "buy_amount" not in pcols:
        c.execute("ALTER TABLE positions ADD COLUMN buy_amount REAL")  # 买入金额 = 股数×成交价
    if "sell_order_id" not in pcols:
        c.execute("ALTER TABLE positions ADD COLUMN sell_order_id INTEGER")  # 卖出委托号（委托制）
    # M3（2026-09-15 战报整改）：开仓时登记支撑区/ATR/regime 上下文，供破位/吊灯双腿卖出；
    # max_close 滚动跟踪入场以来最高收盘（吊灯止盈基准）；extend_count 到期顺延计数
    for col, ddl in [("sup_lo_entry", "REAL"), ("atr_entry", "REAL"),
                     ("regime_entry", "TEXT"), ("max_close", "REAL"),
                     ("extend_count", "INTEGER DEFAULT 0"),
                     ("last_extend_date", "TEXT"), ("next_review_date", "TEXT"),
                     ("risk_take_profit", "REAL"), ("risk_stop_loss", "REAL"),
                     ("risk_hold_days", "INTEGER"), ("risk_rule_version", "TEXT"),
                     ("sell_attempts", "INTEGER DEFAULT 0"),
                     ("last_sell_attempt", "TEXT")]:
        if col not in pcols:
            c.execute(f"ALTER TABLE positions ADD COLUMN {col} {ddl}")
    # 迁移：picks 增加 data_source（生产库为历史手工添加，全新建库走不到——
    # 2026-09-16 测试暴露；补上后新环境 save_pick 不再炸）
    pkcols = [r[1] for r in c.execute("PRAGMA table_info(picks)")]
    if "data_source" not in pkcols:
        c.execute("ALTER TABLE picks ADD COLUMN data_source TEXT")
    for col in ("theory_family", "regime_scope", "evidence_type", "risk_class"):
        if col not in pkcols:
            c.execute(f"ALTER TABLE picks ADD COLUMN {col} TEXT")
    return c


# ---------------------------------------------------------------- 落库
def save_pick(source: str, pool_name: str, top_n: int, method: str,
              filters: list, factors: list, final_scores: pd.Series,
              pack_name: str | None = None, oos_winrate: float | None = None,
              trade_date: str | None = None, data_source: str | None = None,
              theory_family: str | None = None, regime_scope: str | None = None,
              evidence_type: str | None = None, risk_class: str | None = None,
              decision_evidence: dict | None = None) -> int | None:
    """保存一次选股结果。factors: [{name,kind,weight,direction}]。同组合同日去重覆盖。"""
    import datasource

    if final_scores is None or len(final_scores) == 0:
        return None
    trade_date = trade_date or get_last_trade_day()
    data_source = data_source or datasource.get_source()
    # norm 是归一化口径快照（存储用），不是组合身份——含它会让同组合在口径切换
    # 前后 hash 不同 → 同日双行落库、outcome 双计（2026-09-16 冷评审发现）
    fac_identity = [{k: v for k, v in f.items() if k != "norm"} for f in factors]
    combo_key = json.dumps({"s": source, "p": pool_name, "n": top_n, "m": method,
                            "f": filters, "fac": fac_identity, "pk": pack_name, "ds": data_source},
                           sort_keys=True, ensure_ascii=False)
    combo_hash = hashlib.md5(combo_key.encode()).hexdigest()[:16]
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO picks (combo_hash, trade_date, created_at, source, pool_name,
                                  pack_name, method, top_n, filters, factors, oos_winrate_at_save, data_source,
                                  theory_family, regime_scope, evidence_type, risk_class)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(combo_hash, trade_date) DO UPDATE SET
                 created_at=excluded.created_at, factors=excluded.factors,
                 oos_winrate_at_save=excluded.oos_winrate_at_save, data_source=excluded.data_source""",
            (combo_hash, trade_date, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source,
             pool_name, pack_name, method, top_n, json.dumps(filters, ensure_ascii=False),
             json.dumps(factors, ensure_ascii=False), oos_winrate, data_source,
             theory_family, regime_scope, evidence_type, risk_class))
        row = c.execute("SELECT id FROM picks WHERE combo_hash=? AND trade_date=?",
                        (combo_hash, trade_date))
        pick_id = row.fetchone()[0]
        c.execute("DELETE FROM pick_items WHERE pick_id=?", (pick_id,))
        c.executemany("INSERT INTO pick_items (pick_id, code, rank, score) VALUES (?,?,?,?)",
                      [(pick_id, code, i + 1, float(sc))
                       for i, (code, sc) in enumerate(final_scores.items())])
        if decision_evidence is not None:
            c.execute("INSERT OR REPLACE INTO pick_decision_evidence "
                      "(pick_id,market_state,sector_json,financial_json,technical_json,risk_json,created_at) "
                      "VALUES (?,?,?,?,?,?,?)", (pick_id, decision_evidence.get("market_state"),
                      json.dumps(decision_evidence.get("sector", {}), ensure_ascii=False),
                      json.dumps(decision_evidence.get("financial", {}), ensure_ascii=False),
                      json.dumps(decision_evidence.get("technical", {}), ensure_ascii=False),
                      json.dumps(decision_evidence.get("risk", {}), ensure_ascii=False),
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return pick_id


# ---------------------------------------------------------------- 战果回填
def _calendar() -> list[str]:
    f = QLIB_DATA_DIR / "calendars" / "day.txt"
    return [x.strip() for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


def _calendar_for(source: str | None) -> list[str]:
    """结算用交易日历：qlib 本地库为准；名单来自在线源时，把该源 market_daily
    已落库的更新日期接在 qlib 日历尾部（在线源当日即新，qlib 社区包滞后约 4 天，
    不接上则"次日结算"会被日历卡住——2026-08-25 实测）。"""
    cal = _calendar()
    if not source or source == "qlib_local":
        return cal
    try:
        import datasource
        with datasource._conn() as c:
            days = [r[0] for r in c.execute(
                "SELECT DISTINCT date FROM market_daily WHERE source=? ORDER BY date", (source,))]
        extra = [d for d in days if not cal or d > cal[-1]]
        return cal + extra
    except Exception:
        return cal


def backfill_outcomes() -> str:
    """到期回填：对每条 pick，按交易日历计算 1/5/20/60/120 日后的等权收益与池内中位。
    日历按名单数据源选择（在线源接到今日，qlib 源用本地库日历）。"""
    with _conn() as c:
        picks = c.execute("SELECT id, trade_date, pool_name, COALESCE(data_source,'qlib_local') FROM picks").fetchall()
        filled, skipped = 0, 0
        for pick_id, trade_date, pool_name, p_source in picks:
            cal = _calendar_for(p_source)
            if not cal or trade_date not in cal:
                continue
            last_day = cal[-1]
            t_idx = cal.index(trade_date)
            for fwd in FWD_LIST:
                if t_idx + fwd >= len(cal):
                    continue
                eval_date = cal[t_idx + fwd]
                if eval_date > last_day:
                    continue
                exists = c.execute("SELECT 1 FROM outcomes WHERE pick_id=? AND fwd_days=?",
                                   (pick_id, fwd)).fetchone()
                if exists:
                    continue
                items = [r[0] for r in c.execute(
                    "SELECT code FROM pick_items WHERE pick_id=?", (pick_id,)).fetchall()]
                if not items:
                    continue
                pool = all_pools().get(pool_name) or items
                closes = _close_at(sorted(set(items) | set(pool)), trade_date, eval_date, p_source)
                if closes is None:
                    skipped += 1
                    continue
                r0, r1 = closes
                rets = (r1 / r0 - 1).dropna()
                if rets.empty:
                    skipped += 1
                    continue
                pick_rets = rets[rets.index.isin(items)]
                avg_ret = float(pick_rets.mean()) if len(pick_rets) else float("nan")
                median = float(rets[rets.index.isin(pool)].median())
                excess = avg_ret - median
                c.execute(
                    "INSERT OR REPLACE INTO outcomes (pick_id, fwd_days, eval_date, avg_ret, pool_median, excess, hit)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (pick_id, fwd, eval_date, avg_ret, median, excess, int(excess > 0)))
                filled += 1
    return f"回填完成：新增 {filled} 条战果（跳过 {skipped}）"


def _close_at(codes: list[str], d0: str, d1: str, source: str | None = None):
    """两个日期的收盘价（取区间内最近可得交易日，容忍停牌）。按名单来源取数。"""
    df = sig.fetch_panel(codes, (pd.Timestamp(d0) - pd.Timedelta(days=7)).strftime("%Y-%m-%d"),
                         (pd.Timestamp(d1) + pd.Timedelta(days=3)).strftime("%Y-%m-%d"), ["$close"],
                         source=source)
    if df.empty:
        return None
    close = df["$close"].unstack("instrument").sort_index()
    days = list(close.index)
    i0 = max([i for i, d in enumerate(days) if str(d)[:10] <= d0], default=None)
    i1 = max([i for i, d in enumerate(days) if str(d)[:10] <= d1], default=None)
    if i0 is None or i1 is None or i1 <= i0:
        return None
    return close.iloc[i0], close.iloc[i1]


# ---------------------------------------------------------------- 榜单
def pack_leaderboard() -> pd.DataFrame:
    """策略包/组合实战榜：实战胜率 vs 保存时的回测胜率（校准对照）。"""
    with _conn() as c:
        picks = pd.read_sql("SELECT * FROM picks", c)
        outs = pd.read_sql("SELECT * FROM outcomes", c)
    if picks.empty:
        return pd.DataFrame()
    rows = []
    for (src, pack, pool), grp in picks.groupby(["source", picks["pack_name"].fillna("(未存包)"), "pool_name"]):
        o = outs[outs["pick_id"].isin(grp["id"])]
        row = {"来源": src, "策略包": pack, "股票池": pool, "选股次数": len(grp),
               "已回填战果": len(o),
               "数据源": grp["data_source"].dropna().iloc[0] if grp["data_source"].notna().any() else "qlib_local"}
        for fwd in FWD_LIST:
            of = o[o["fwd_days"] == fwd]
            if len(of):
                row[f"{fwd}日胜率"] = (of["hit"] == 1).mean()
                row[f"{fwd}日均超额"] = of["excess"].mean()
                row[f"{fwd}日收益率"] = of["avg_ret"].mean()
        row["回测OOS胜率"] = grp["oos_winrate_at_save"].dropna().map(
            lambda x: f"{x:.0%}" if pd.notna(x) else None).dropna().unique()
        row["回测OOS胜率"] = row["回测OOS胜率"][0] if len(row["回测OOS胜率"]) else "—"
        rows.append(row)
    return pd.DataFrame(rows)


def factor_leaderboard(fwd: int = 20) -> pd.DataFrame:
    """因子实战近似归因：含有该因子的组合，其后战果均值（有混杂，仅作参考）。
    fwd 可指定结算周期（默认 20 日；回喂生成端等场景可用 5/1 日提前获得信号）。"""
    with _conn() as c:
        picks = pd.read_sql("SELECT id, factors FROM picks", c)
        outs = pd.read_sql("SELECT * FROM outcomes", c)
    if picks.empty or outs.empty:
        return pd.DataFrame()
    rows = []
    name2hits, name2ex = {}, {}
    for _, p in picks.iterrows():
        try:
            facs = json.loads(p["factors"])
        except Exception:
            continue
        o = outs[(outs["pick_id"] == p["id"]) & (outs["fwd_days"] == fwd)]
        if o.empty:
            continue
        for f in facs:
            name2hits.setdefault(f["name"], []).extend(o["hit"].tolist())
            name2ex.setdefault(f["name"], []).extend(o["excess"].tolist())
    for name, hits in name2hits.items():
        rows.append({"因子": name, "参与且有战果的次数": len(hits),
                     f"{fwd}日胜率(近似)": float(np.mean(hits)),
                     f"{fwd}日均超额(近似)": float(np.mean(name2ex[name]))})
    return pd.DataFrame(rows).sort_values(f"{fwd}日胜率(近似)", ascending=False) if rows else pd.DataFrame()


def pick_history(limit: int = 50) -> pd.DataFrame:
    with _conn() as c:
        picks = pd.read_sql(
            "SELECT id, created_at, trade_date, source, pool_name, pack_name, method, top_n,"
            " COALESCE(data_source,'qlib_local') AS data_source FROM picks"
            " ORDER BY id DESC LIMIT ?", c, params=(limit,))
        outs = pd.read_sql("SELECT pick_id, fwd_days, excess, hit FROM outcomes", c)
    if picks.empty:
        return picks
    agg = outs.groupby("pick_id").agg(战果数=("hit", "count"), 命中率=("hit", "mean"),
                                      平均超额=("excess", "mean")).reset_index()
    return picks.merge(agg, left_on="id", right_on="pick_id", how="left").drop(columns=["pick_id"])


# ---------------------------------------------------------------- 选股列表页查询
def list_pick_dates(limit: int = 120) -> list[str]:
    """有选股记录的交易日（新→旧）。"""
    with _conn() as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT trade_date FROM picks ORDER BY trade_date DESC LIMIT ?", (limit,))]


def picks_on_date(trade_date: str) -> pd.DataFrame:
    """某交易日的全部选股记录（同一日可能有手动+自动多条）。含 factors/filters 供解释页复算。"""
    with _conn() as c:
        return pd.read_sql(
            "SELECT id, created_at, source, pool_name, pack_name, method, top_n,"
            " filters, factors, oos_winrate_at_save,"
            " COALESCE(data_source,'qlib_local') AS data_source"
            " FROM picks WHERE trade_date=? ORDER BY id DESC", c, params=(trade_date,))


def pick_items_detail(pick_id: int) -> pd.DataFrame:
    """名单明细（rank/score）+ 模拟交易结果（有则并入：入场/出场/盈亏/持有天数）。"""
    with _conn() as c:
        items = pd.read_sql(
            "SELECT code, rank, score FROM pick_items WHERE pick_id=? ORDER BY rank",
            c, params=(pick_id,))
        trades = pd.read_sql(
            "SELECT code, entry_date, entry_price, exit_date, exit_price, exit_reason,"
            " pnl_pct, hold_days FROM trades WHERE pick_id=?", c, params=(pick_id,))
    if not trades.empty:
        items = items.merge(trades, on="code", how="left")
    return items


def pick_outcomes(pick_id: int) -> pd.DataFrame:
    """某次选股的 5/10/20 日结算战果（未到期的 horizon 不会出现）。"""
    with _conn() as c:
        return pd.read_sql(
            "SELECT fwd_days, eval_date, avg_ret, pool_median, excess, hit FROM outcomes"
            " WHERE pick_id=? ORDER BY fwd_days", c, params=(pick_id,))


def expected_eval_dates(trade_date: str, source: str | None = None) -> dict:
    """按交易日历推算各周期结算日；超出日历末端的 horizon 不返回（页面显示"待结算"）。"""
    cal = _calendar_for(source)
    if trade_date not in cal:
        return {}
    i = cal.index(trade_date)
    return {fwd: cal[i + fwd] for fwd in FWD_LIST if i + fwd < len(cal)}


# ---------------------------------------------------------------- 模拟交易（买入价→卖出价→平仓→盈亏）
DEFAULT_RULES = {"take_profit": 0.15, "stop_loss": -0.08, "hold_days": 20, "cost": 0.0025,
                  "atr_period": 14, "atr_tp_multiplier": 2.5, "use_atr_tp": True}
# 事件增强票规则：止损更紧（-5% vs -8%）、止盈更保守（+12% vs +15%）
EVENT_RULES = {"take_profit": 0.12, "stop_loss": -0.05, "hold_days": 15, "cost": 0.0025,
               "atr_period": 14, "atr_tp_multiplier": 2.0, "use_atr_tp": True}
RISK_RULES_FILE = DATA_DIR / "risk_rules.json"
ACCOUNT_RISK_DEFAULTS = {
    "yellow_drawdown": 0.03, "orange_drawdown": 0.05, "red_drawdown": 0.08,
    "normal_target": 0.80, "yellow_target": 0.70,
    "orange_target": 0.60, "red_target": 0.30,
}
RISK_PLAN_FILE = DATA_DIR / "risk_reduction_plan.json"
RISK_LLM_ADVICE_FILE = DATA_DIR / "risk_llm_advice.json"
RISK_LLM_PROMPT_VERSION = "risk-shadow-v2"
RISK_LLM_SYSTEM_PROMPT = """你是量化账户的只读影子风控决策器。你的职责是审查规则引擎生成的账户风险计划，并提供结构化的第二意见。

优先级与权限边界：
1. 硬规则、风险等级、止损约束和账户目标仓位始终拥有最高优先级。
2. 你只能建议“维持规则计划”“建议收紧”“转人工复核”，不得建议放宽规则计划。
3. recommended_target_position 必须小于或等于输入的 target_position_ratio，且范围为 0 到 1。
4. 你不能下单、撤单、恢复被禁止的开仓、改变风险状态或承诺收益。
5. 只能引用输入证据；数据不足时写入 missing_evidence 并选择“转人工复核”，禁止猜测。

审查顺序：
A. 比较当前仓位、目标仓位和需释放金额。
B. 检查卫星来源、浮亏、持有期及规则风险分，按风险优先级排序。
C. 判断规则计划是否遗漏显著风险；若需更保守，只能降低建议目标仓位。
D. 每项持仓建议必须对应输入 positions 中的 code，并给出可由输入验证的 reason。

严格输出一个 JSON 对象，不要 Markdown、代码围栏或额外文字：
{"decision":"维持规则计划|建议收紧|转人工复核","confidence":0.0,
"recommended_target_position":0.0,"summary":"不超过80字",
"priority_positions":[{"code":"输入中的代码","priority":1,"action":"优先减仓|继续观察|人工复核","reason":"输入证据"}],
"account_actions":["最多3项，只能是建议"],
"rule_disagreement":{"has_disagreement":false,"reason":"无分歧时为空字符串"},
"missing_evidence":["最多3项"]}

限制：priority_positions 最多5项，priority 从1开始且不得重复；confidence 范围0到1。"""


def get_risk_rules(track: str = "main") -> dict:
    """动态读取持仓风控参数；文件异常时安全回退到代码默认值。"""
    base = DEFAULT_RULES if track == "main" else EVENT_RULES
    try:
        saved = json.loads(RISK_RULES_FILE.read_text()) if RISK_RULES_FILE.exists() else {}
    except Exception:
        saved = {}
    cfg = saved.get(track, {}) if isinstance(saved, dict) else {}
    out = {**base}
    try:
        tp = float(cfg.get("take_profit", out["take_profit"]))
        sl = float(cfg.get("stop_loss", out["stop_loss"]))
        days = int(cfg.get("hold_days", out["hold_days"]))
        if 0.01 <= tp <= 1.0:
            out["take_profit"] = tp
        if -0.5 <= sl <= -0.005:
            out["stop_loss"] = sl
        if 1 <= days <= 250:
            out["hold_days"] = days
    except (TypeError, ValueError):
        pass
    return out


def save_risk_rules(main: dict, event: dict) -> None:
    """保存主轨/卫星来源风控参数，并同步当前页面进程中的兼容常量。"""
    old = {}
    try:
        old = json.loads(RISK_RULES_FILE.read_text()) if RISK_RULES_FILE.exists() else {}
    except Exception:
        pass
    payload = {"main": main, "event": event, "account": old.get("account", {}),
               "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    RISK_RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RISK_RULES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(RISK_RULES_FILE)
    DEFAULT_RULES.update(get_risk_rules("main"))
    EVENT_RULES.update(get_risk_rules("event"))


def get_account_risk_config() -> dict:
    try:
        saved = json.loads(RISK_RULES_FILE.read_text()) if RISK_RULES_FILE.exists() else {}
        cfg = saved.get("account", {}) if isinstance(saved, dict) else {}
    except Exception:
        cfg = {}
    out = {**ACCOUNT_RISK_DEFAULTS}
    for key in out:
        try:
            value = float(cfg.get(key, out[key]))
            if 0.005 <= value <= 0.95:
                out[key] = value
        except (TypeError, ValueError):
            pass
    return out


def save_account_risk_config(cfg: dict) -> None:
    try:
        saved = json.loads(RISK_RULES_FILE.read_text()) if RISK_RULES_FILE.exists() else {}
    except Exception:
        saved = {}
    saved["account"] = cfg
    saved["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    RISK_RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RISK_RULES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=2))
    tmp.replace(RISK_RULES_FILE)
# 规则：信号日次日开盘价买入；盘中先触止损按止损价、先触止盈按止盈价（同日双触按保守止损）；
# 到期未触发则第 N 日收盘卖出。成本按往返 0.25% 计。


def trade_plan(ref_price: float | None, signal_date: str, rules: dict | None = None) -> dict:
    """把名单翻译成可执行计划：买入时间/参考价/止盈价/止损价/最迟平仓日（规则同模拟交易）。

    买入时间 = 信号日次一交易日开盘；最迟平仓 = 买入后第 hold_days 个交易日收盘。
    日历超出 qlib 数据末端时按 weekday 顺延近似（遇节假日再顺延，仅作参考）。
    """
    rules = rules or get_risk_rules("main")
    cal = _calendar()
    if signal_date in cal and cal.index(signal_date) + 1 < len(cal):
        nxt = cal[cal.index(signal_date) + 1]
    else:
        d = pd.Timestamp(signal_date) + pd.Timedelta(days=1)
        while d.weekday() >= 5:
            d += pd.Timedelta(days=1)
        nxt = d.strftime("%Y-%m-%d")
    d, n = pd.Timestamp(nxt), 0
    while n < rules["hold_days"]:
        d += pd.Timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    out = {"买入时间": f"{nxt} 开盘", "最迟平仓": f"{d.strftime('%Y-%m-%d')} 收盘",
           "规则": f"止盈 +{rules['take_profit']:.0%} / 止损 {rules['stop_loss']:.0%} / 持有≤{rules['hold_days']}交易日"}
    if ref_price:
        out["参考买入价"] = round(float(ref_price), 2)
        out["止盈价"] = round(float(ref_price) * (1 + rules["take_profit"]), 2)
        out["止损价"] = round(float(ref_price) * (1 + rules["stop_loss"]), 2)
    return out


def simulate_trade(code: str, signal_date: str, rules: dict | None = None,
                   entry_price_override: float | None = None,
                   entry_date_override: str | None = None) -> dict | None:
    """对单只标的从 signal_date 起模拟一笔交易。返回成交明细或 None（数据不足）。
    entry_price_override 给定则以指定买入价入场（手动模拟）；entry_date_override 指定入场日。"""
    import datasource
    r = {**get_risk_rules("main"), **(rules or {})}
    cal = _calendar()
    if signal_date not in cal:
        return None
    i0 = cal.index(signal_date)
    if i0 + 1 >= len(cal):
        return None
    entry_date = entry_date_override or cal[i0 + 1]
    end = cal[min(i0 + 1 + r["hold_days"], len(cal) - 1)]
    df = sig.fetch_panel([code], signal_date, end, ["$open", "$high", "$low", "$close"],
                         source=datasource.get_loop_source())
    if df.empty:
        return None
    s = df.droplevel("instrument").sort_index()
    s.index = pd.to_datetime(s.index)
    s = s[s.index >= pd.Timestamp(entry_date)]
    if s.empty:
        return None
    entry_price = entry_price_override or float(s.iloc[0]["$open"])

    # 动态止盈：基于 ATR（真实波幅）计算止盈目标
    if r.get("use_atr_tp", False) and len(s) >= r.get("atr_period", 14):
        atr_period = r.get("atr_period", 14)
        # 计算 ATR：True Range 的移动平均
        tr = pd.concat([
            s["$high"] - s["$low"],
            (s["$high"] - s["$close"].shift(1)).abs(),
            (s["$low"] - s["$close"].shift(1)).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(atr_period).mean().iloc[-1]
        # 动态止盈 = 入场价 + ATR × 倍数
        atr_tp = float(atr) * r.get("atr_tp_multiplier", 2.5) / entry_price
        tp_price = entry_price * (1 + max(r["take_profit"], atr_tp))
    else:
        tp_price = entry_price * (1 + r["take_profit"])

    sl_price = entry_price * (1 + r["stop_loss"])

    exit_date, exit_price, reason = None, None, None
    for dt, row in s.iloc[1:].iterrows():
        hit_sl = row["$low"] <= sl_price
        hit_tp = row["$high"] >= tp_price
        if hit_sl:  # 同日双触保守按止损
            exit_date, exit_price, reason = dt, sl_price, "止损"
            break
        if hit_tp:
            exit_date, exit_price, reason = dt, tp_price, "止盈"
            break
    if exit_date is None:
        last = s.iloc[-1]
        exit_date, exit_price, reason = s.index[-1], float(last["$close"]), "到期"

    pnl = exit_price / entry_price - 1 - r["cost"]
    hold_days = len(s[s.index <= exit_date]) - 1
    return {"code": code, "signal_date": signal_date, "entry_date": entry_date,
            "entry_price": round(entry_price, 3), "exit_date": str(exit_date)[:10],
            "exit_price": round(exit_price, 3), "exit_reason": reason,
            "pnl_pct": round(pnl, 4), "hold_days": int(hold_days), "rules": r}


def backfill_trades(rules: dict | None = None, limit: int = 500) -> str:
    """对经验库中未模拟的 picks 逐条模拟交易并落 trades 表。"""
    done = 0
    with _conn() as c:
        picks = c.execute("SELECT id, trade_date FROM picks ORDER BY id").fetchall()
        for pick_id, trade_date in picks:
            existing = c.execute("SELECT COUNT(*) FROM trades WHERE pick_id=?", (pick_id,)).fetchone()[0]
            if existing:
                continue
            codes = [r[0] for r in c.execute("SELECT code FROM pick_items WHERE pick_id=?", (pick_id,)).fetchall()]
            for code in codes:
                if done >= limit:
                    break
                t = simulate_trade(code, trade_date, rules)
                if t:
                    c.execute(
                        "INSERT OR REPLACE INTO trades (pick_id, code, signal_date, entry_date, entry_price,"
                        " exit_date, exit_price, exit_reason, pnl_pct, hold_days, rules)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (pick_id, code, t["signal_date"], t["entry_date"], t["entry_price"],
                         t["exit_date"], t["exit_price"], t["exit_reason"], t["pnl_pct"],
                         t["hold_days"], json.dumps(t["rules"])))
                    done += 1
    return f"模拟交易完成：新增 {done} 笔"


def trade_ledger(pack_name: str | None = None, limit: int = 200) -> pd.DataFrame:
    """交易台账（可选按策略包过滤）。"""
    with _conn() as c:
        q = ("SELECT t.*, p.pack_name, p.source FROM trades t JOIN picks p ON p.id=t.pick_id")
        if pack_name:
            q += f" WHERE p.pack_name='{pack_name}'"
        q += " ORDER BY t.id DESC LIMIT ?"
        return pd.read_sql(q, c, params=(limit,))


def trade_stats(pack_name: str | None = None) -> dict:
    """组合级交易绩效：胜率/平均盈亏/盈亏比/利润因子/净值曲线。"""
    df = trade_ledger(pack_name, limit=2000)
    if df.empty:
        return {}
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    nav = df.sort_values("exit_date").groupby("exit_date")["pnl_pct"].mean().add(1).cumprod()
    stats = {
        "交易笔数": len(df),
        "胜率": float((df["pnl_pct"] > 0).mean()),
        "平均盈亏": float(df["pnl_pct"].mean()),
        "平均盈利": float(wins["pnl_pct"].mean()) if len(wins) else 0.0,
        "平均亏损": float(losses["pnl_pct"].mean()) if len(losses) else 0.0,
        "盈亏比": float(abs(wins["pnl_pct"].mean() / losses["pnl_pct"].mean())) if len(wins) and len(losses) else None,
        "利润因子": float(wins["pnl_pct"].sum() / abs(losses["pnl_pct"].sum())) if len(losses) and losses["pnl_pct"].sum() != 0 else None,
        "净值": float(nav.iloc[-1]) if len(nav) else 1.0,
        "最大回撤": float(((nav - nav.cummax()) / nav.cummax()).min()) if len(nav) else 0.0,
        "nav": nav,
    }
    return stats
def _md_table(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in df.columns) + " |")
    return "\n".join(lines)


def export_experience_report(out_path: Path | None = None) -> Path:
    out_path = out_path or (DATA_DIR / "experience_report.md")
    pack_lb = pack_leaderboard()
    fac_lb = factor_leaderboard()
    lines = ["# 选股实战经验报告", f"生成时间: {datetime.now():%Y-%m-%d %H:%M}", ""]
    if not pack_lb.empty:
        lines += ["## 组合/策略包实战表现", _md_table(pack_lb), ""]
    if not fac_lb.empty:
        lines += ["## 因子实战近似归因（20日前瞻）", _md_table(fac_lb), ""]
    lines += ["> 说明：胜率=组合等权收益跑赢池内中位的比例；归因近似存在因子间混杂，",
              "> 仅作方向参考。该报告可由 RD-Agent 的 fin_factor_report 场景作为先验知识读取。"]
    out_path.write_text("\n".join(lines))
    return out_path


# ---------------------------------------------------------------- 持仓跟踪（盘中触发开平仓，T+1）
def _latest_prices(codes: list[str]) -> dict:
    """每代码各自最新一行快照 {code: (price, open, prev_close)}。

    按代码取最新（而非整批 MAX(datetime)）：高频热码快照（持仓/自选/名单，
    15s 一批）与全市场快照（5min 一批）同表共存时，冷码不会被新批次的
    整批时间戳"挤没"。"""
    import datasource
    if not codes:
        return {}
    with datasource._qconn() as c:
        df = pd.read_sql(
            f"""SELECT r.code, r.price, r.open, r.prev_close FROM ifind_realtime r
                JOIN (SELECT code, MAX(datetime) md FROM ifind_realtime
                      WHERE code IN ({','.join('?' * len(codes))}) GROUP BY code) t
                  ON r.code = t.code AND r.datetime = t.md""", c, params=codes)
    return {r.code: (r.price, r.open, r.prev_close) for r in df.itertuples()}


def _latest_price_times(codes: list[str]) -> dict[str, str]:
    """返回每只股票最新行情时间，用于阻断陈旧行情上的错误交易。"""
    import datasource
    if not codes:
        return {}
    with datasource._qconn() as c:
        rows = c.execute(
            f"SELECT code,MAX(datetime) FROM ifind_realtime WHERE code IN "
            f"({','.join('?' * len(codes))}) GROUP BY code", codes).fetchall()
    return {str(code): str(ts) for code, ts in rows if ts}


def _quote_is_fresh(ts: str | None, max_minutes: int = 10) -> bool:
    if not ts:
        return False
    try:
        dt = pd.to_datetime(ts).to_pydatetime()
        return -60 <= (datetime.now() - dt).total_seconds() <= max_minutes * 60
    except Exception:
        return False


def _position_names(codes: list[str]) -> dict:
    import datasource
    if not codes:
        return {}
    try:
        with datasource._qconn() as c:
            return dict(c.execute(
                f"SELECT code, name FROM ifind_stocklist"
                f" WHERE code IN ({','.join('?' * len(codes))})", codes).fetchall())
    except Exception:
        return {}


def _trade_days_between(d0: str, d1: str) -> int:
    """两个日期间隔的交易日数（日历在库内则用交易日历，否则按工作日估算）。"""
    cal = _calendar()
    if d0 in cal and d1 in cal:
        return cal.index(d1) - cal.index(d0)
    d, n = pd.Timestamp(d0), 0
    while d.strftime("%Y-%m-%d") < d1 and n < 60:
        d += pd.Timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _quote_change_pct(quote):
    """本模块快照顺序为(price, open, prev_close)，涨幅以昨收为基准。"""
    if quote and len(quote) >= 3 and quote[0] and quote[2] and quote[2] > 0:
        value = (quote[0] / quote[2] - 1) * 100
        return float(value) if pd.notna(value) and abs(value) != float('inf') else None
    return None


def position_open_from_picks(trade_date: str, today: str) -> str:
    """盘中委托买入：对名单挂限价单（限价 = 名单参考买入价 = 扫描日收盘价）。

    遵守真实交易规则：挂出委托（pending）后不立即成交，现价 ≤ 限价才触发成交（开仓）；
    当日收盘仍未成交的委托自动失效（次日不再补）。竞价确认"回避"的股票跳过。
    已有 open/closing/pending 时不新增；柜台在事务内再次校验，禁止自动加仓。
    """
    from common import SIGNALS_DIR
    picks = picks_on_date(trade_date)
    if picks.empty:
        return "无名单可委托"
    # M4 风控熔断：当日净值回撤触及熔断线 → 停止开新仓
    halt, halt_why = risk_halt_today(today)
    if halt:
        return f"⛔ 风控熔断生效（{halt_why}）：今日停止开新仓"
    # 竞价回避名单
    avoid: set = set()
    af = SIGNALS_DIR / f"auction_{today}.parquet"
    if af.exists():
        try:
            adf = pd.read_parquet(af)
            if "竞价结论" in adf.columns:
                avoid = set(adf[adf["竞价结论"] == "回避"]["code"])
        except Exception:
            pass
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_new = 0
    n_defer = n_chase = 0
    # M3 买点约束（2026-09-15 战报整改）：regime 化的支撑距离许可 + 追高保护
    theta_entry = 1.0  # 默认（bear/transition/unknown）
    regime_now = "unknown"
    try:
        from loopengine.regime import detect_regime
        rg = detect_regime()
        regime_now = rg.get("regime", "unknown")
        theta_entry = {"bull": 1.5, "sideways": 0.5, "bear": 1.0,
                       "transition": 1.0}.get(regime_now, 1.0)
    except Exception:
        pass
    with _conn() as c:
        for r in picks.itertuples():
            if r.source in ("le_shadow", "sched_satellite_scan"):
                continue  # 影子/顺带卫星扫描只攒战绩；专用 satellite_scan 才能进入主轨
            items = pick_items_detail(int(r.id))
            if items.empty:
                continue
            if r.source == "satellite_scan":
                # 自动选股与自动执行分离：降级策略继续产候选积累样本，但不得开仓。
                try:
                    import library
                    pack = library.list_strategies().get(str(r.pack_name)) or {}
                    if pack.get("status", "active") != "active":
                        n_defer += len(items)
                        continue
                except Exception:
                    n_defer += len(items)
                    continue
                sat_halt, _sat_why = satellite_halt_today(today)
                if sat_halt:
                    n_defer += len(items)
                    continue
            codes = list(items["code"])
            names = _position_names(codes)
            # 参考买入价 = 扫描日收盘价（名单生成时的价格）
            ref_prices = {}
            try:
                import datasource
                with datasource._qconn() as dc:
                    for row in dc.execute(
                            f"SELECT code, price FROM ifind_stocklist"
                            f" WHERE code IN ({','.join('?' * len(codes))})", codes):
                        if row[1]:
                            ref_prices[row[0]] = row[1]
            except Exception:
                pass
            # P1-6修复：使用实时快照计算追高阈值，不用ifind_stocklist的stale change_pct
            ref_chg = {}
            try:
                rt = _latest_prices(codes)
                for code in codes:
                    pr = rt.get(code)
                    change = _quote_change_pct(pr)
                    if change is not None:
                        ref_chg[code] = change
            except Exception:
                pass
            # 该名单的支撑阻力上下文（≤名单生成日，无未来信息）
            try:
                import density_sr
                sr_map = density_sr.latest_sr_map(codes, asof=trade_date)
            except Exception:
                sr_map = None
            for it in items.itertuples():
                if it.code in avoid:
                    continue
                # 多层选股硬闸：市场/板块/财务/技术任一拒绝或数据不足均不得生成新委托。
                try:
                    import selection_gate
                    pack_meta = {}
                    try:
                        import library
                        pack_meta = library.list_strategies().get(str(r.pack_name)) or {}
                    except Exception:
                        pass
                    gate = selection_gate.evaluate_candidate(it.code, pack_meta, regime_now,
                                                             financial_required=True)
                    if gate.status != "pass":
                        n_defer += 1
                        continue
                except Exception:
                    n_defer += 1
                    continue
                if c.execute("SELECT 1 FROM positions WHERE code=? "
                             "AND status IN ('open','closing','pending')", (it.code,)).fetchone():
                    continue
                limit = ref_prices.get(it.code)
                if not limit:
                    continue
                # M3 闸门：距强支撑过远 → 延迟开仓等回踩；当日已涨 >5% → 追高保护
                sr = sr_map.loc[it.code] if (sr_map is not None and not sr_map.empty
                                             and it.code in sr_map.index) else None
                if sr is not None and pd.notna(sr.get("sup_dist_atr")) \
                        and sr["sup_dist_atr"] > theta_entry:
                    n_defer += 1
                    continue
                chg = ref_chg.get(it.code)
                # 追高保护分层：事件增强票容忍更高涨幅（追强逻辑）
                chase_thr = CHASE_THRESHOLD_EVENT if _is_event_enhanced_pick(r.pack_name) else CHASE_THRESHOLD_DEFAULT
                if chg is not None and pd.notna(chg) and chg > chase_thr:
                    n_chase += 1
                    continue
                # P2-7修复：最小流动性过滤（流通市值 < 30亿 → 跳过，避免低流动性股票）
                try:
                    import datasource
                    with datasource._qconn() as dc:
                        mv_row = dc.execute(
                            "SELECT float_mv FROM ifind_realtime"
                            " WHERE code=? ORDER BY datetime DESC LIMIT 1", (it.code,)).fetchone()
                        if mv_row and mv_row[0] and float(mv_row[0]) < 3e9:
                            continue  # 流通市值不足30亿，流动性风险
                except Exception:
                    pass
                entry_rules = (get_risk_rules("event") if r.source == "satellite_scan"
                               or _is_event_enhanced_pick(r.pack_name)
                               else get_risk_rules("main"))
                cur = c.execute(
                    "INSERT OR IGNORE INTO positions"
                    "(code, name, buy_date, buy_price, buy_ts, pick_id, source, pack_name,"
                    " status, limit_price, created_at, sup_lo_entry, atr_entry, regime_entry,"
                    " risk_take_profit,risk_stop_loss,risk_hold_days,risk_rule_version)"
                    " VALUES (?,?,?,NULL,?,?,?,?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (it.code, names.get(it.code, ""), today, now,
                     int(r.id), r.source, r.pack_name, float(limit), now,
                     float(sr["sup_lo"]) if sr is not None and pd.notna(sr.get("sup_lo")) else None,
                     float(sr["atr"]) if sr is not None and pd.notna(sr.get("atr")) else None,
                     regime_now, entry_rules["take_profit"], entry_rules["stop_loss"],
                     entry_rules["hold_days"], now))
                n_new += cur.rowcount
    msg = f"委托挂单：新增 {n_new} 笔限价单"
    if n_defer:
        msg += f" · 资格/数据/支撑约束延迟 {n_defer} 笔"
    if n_chase:
        msg += f" · 追高保护拦 {n_chase} 笔"
    return msg if n_new or n_defer or n_chase else "委托挂单：无新增（已挂或竞价回避）"


# 每轨虚拟资金（等分买入）：主轨 7 万 / 卫星轨 2 万 / 双闸门事件票 1.5 万（高风险更小仓位）
TRACK_BUDGET = {
    "satellite_scan": 20000.0,
    "sched_satellite_scan": 20000.0,
}

# 事件增强票的追高保护阈值（放宽：追强逻辑允许更高涨幅进入）
CHASE_THRESHOLD_DEFAULT = 5.0
CHASE_THRESHOLD_EVENT = 8.0


def _budget_per_stock(source: str, top_n: int) -> float:
    """每股预算 = 轨道虚拟资金 / 名单只数。"""
    budget = TRACK_BUDGET.get(source, 70000.0)
    return budget / max(int(top_n or 10), 1)


def _is_event_enhanced_pick(pack_name: str) -> bool:
    """判断是否为事件增强包（含 ev_ 因子的策略包）。"""
    if not pack_name:
        return False
    try:
        import library
        # P2-1修复：使用缓存避免每次重复调用 library.list_strategies()
        global _strategies_cache
        if _strategies_cache is None:
            _strategies_cache = library.list_strategies()
        pk = _strategies_cache.get(pack_name, {})
        factors = pk.get("factors", [])
        return any(f.get("name", "").startswith("ev_") for f in factors)
    except Exception:
        return False


def _get_position_rules(pos_row) -> dict:
    """根据仓位的 pack_name 确定使用的规则集（DEFAULT_RULES 或 EVENT_RULES）。"""
    pk_name = pos_row.get("pack_name") if hasattr(pos_row, "get") else (
        pos_row["pack_name"] if "pack_name" in pos_row.index else None)
    source = pos_row.get("source") if hasattr(pos_row, "get") else (
        pos_row["source"] if "source" in pos_row.index else None)
    track = ("event" if source in ("satellite_scan", "sched_satellite_scan")
             or _is_event_enhanced_pick(pk_name) else "main")
    rules = get_risk_rules(track)
    # 新仓使用入场时保存的规则快照；老仓无快照时兼容动态配置。
    try:
        tp = pos_row.get("risk_take_profit")
        sl = pos_row.get("risk_stop_loss")
        days = pos_row.get("risk_hold_days")
        if pd.notna(tp) and pd.notna(sl) and pd.notna(days):
            rules = {**rules, "take_profit": float(tp), "stop_loss": float(sl),
                     "hold_days": int(days)}
    except Exception:
        pass
    return rules


def position_fill_check(today: str) -> str:
    """盘中撮合：pending 限价单现价触及即成交开仓（成交于限价或更优价，按预算整手配股）；
    隔夜未成交挂单自动失效（当日委托当日有效）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_hm = datetime.now().strftime("%H%M")
    with _conn() as c:
        pend = pd.read_sql("SELECT * FROM positions WHERE status='pending'", c)
    if pend.empty:
        return "无挂单"
    prices = _latest_prices(list(pend["code"]))
    quote_times = _latest_price_times(list(pend["code"]))
    n_fill = n_expire = 0
    n_stale = n_sat_limit = 0
    # P2-6修复：统计实际pending数量，按实际数量分配预算
    n_pending_total = len(pend)
    with _conn() as c:
        for _, p in pend.iterrows():
            # 隔夜挂单 / 当日收盘(15:00)后 → 失效
            if str(p["buy_date"]) < today or (str(p["buy_date"]) == today and now_hm >= "1500"):
                c.execute("UPDATE positions SET status='expired', closed_at=? WHERE id=? AND status='pending'",
                          (now, int(p["id"])))
                c.commit()
                n_expire += 1
                continue
            pr = prices.get(p["code"])
            cur = pr[0] if pr else None
            if not _quote_is_fresh(quote_times.get(str(p["code"]))):
                n_stale += 1
                continue
            if cur is None or not p["limit_price"]:
                continue
            if cur <= p["limit_price"]:  # 现价触及限价 → 成交（取更优价）
                fill = min(cur, float(p["limit_price"]))
                # P2-6修复：按实际pending数量分配预算，不用声明的top_n
                per = _budget_per_stock(p["source"], n_pending_total)
                # M4 波动率倒数 sizing：高波动票预算收缩（clip 0.4×~1.2×，基准 2% 日波动）
                atr_pct = atr_pct_of(p["code"], today)
                if atr_pct:
                    per = per * float(np.clip(0.02 / atr_pct, 0.4, 1.2))
                # M7 置信度乘数：该票在名单内的分位 → 校准胜率 → 乘数（0.3~1.3）
                try:
                    sc_row = c.execute("SELECT score FROM pick_items WHERE pick_id=? AND code=?",
                                       (int(p["pick_id"]), p["code"])).fetchone()
                    if sc_row and sc_row[0] is not None:
                        pct_row = c.execute(
                            "SELECT COUNT(*) FILTER (WHERE score<=?), COUNT(*) FROM pick_items WHERE pick_id=?",
                            (sc_row[0], int(p["pick_id"]))).fetchone()
                        pct = pct_row[0] / pct_row[1] if pct_row and pct_row[1] else None
                        per = per * conviction_multiplier(calibrated_pwin(pct))
                except Exception:
                    pass
                # 预估预算；柜台在成交事务内再次检查总敞口和重复持仓。
                try:
                    import broker as _bk
                    _total = _bk.get_account().get("总资产", 0) or 0
                    if _total > 0:
                        per = min(per, 0.15 * _total)
                        if p["source"] == "satellite_scan":
                            per = min(per, 0.05 * _total)
                            sat = pd.read_sql(
                                "SELECT code,COALESCE(shares,0) shares,COALESCE(buy_price,0) buy_price"
                                " FROM positions WHERE source='satellite_scan'"
                                " AND status IN ('open','closing')", c)
                            sat_codes = set(sat["code"]) if not sat.empty else set()
                            sat_prices = _latest_prices(list(sat_codes)) if sat_codes else {}
                            sat_value = (sum(float(row["shares"] or 0) * float(
                                (sat_prices.get(row["code"]) or (row["buy_price"],))[0]
                                or row["buy_price"] or 0) for _, row in sat.iterrows())
                                if not sat.empty else 0.0)
                            if ((len(sat_codes) >= 3 and p["code"] not in sat_codes)
                                    or sat_value + per > 0.15 * _total):
                                n_sat_limit += 1
                                continue
                except Exception:
                    continue  # 风险数据读取失败时不得放行买入
                # 候选 pending 不占已成交名额；柜台另校验真实未成交买单。
                try:
                    n_open_pending = c.execute(
                        "SELECT COUNT(DISTINCT code) FROM positions WHERE status IN ('open','closing')").fetchone()[0]
                    if n_open_pending >= 8 and p["code"] not in {
                        r[0] for r in c.execute(
                            "SELECT DISTINCT code FROM positions WHERE status IN ('open','closing')").fetchall()}:
                        continue
                except Exception:
                    pass
                shares = int(per // fill // 100 * 100)
                if shares <= 0:
                    continue  # 预算买不起一手就不开（实盘如此：100股整手是硬约束）
                import broker
                msg = broker.buy_position(int(p["id"]), shares)
                if "已成交" not in msg:
                    continue  # 柜台资金不足等 → 留挂（收盘仍未成交自动失效）
                m = re.search(r"@ ([\d.]+)", msg)
                fill = float(m.group(1)) if m else fill
                n_fill += 1

                # 注册 PriceMonitor 事件驱动监控
                try:
                    from price_monitor import monitor
                    # 使用正确的规则集（DEFAULT_RULES 或 EVENT_RULES）
                    rules = _get_position_rules(p)
                    monitor.register_position(
                        position_id=int(p["id"]),
                        code=p["code"],
                        buy_price=fill,
                        shares=shares,
                        buy_date=today,
                        rules=rules
                    )
                except Exception as e:
                    import logging
                    logging.getLogger("experience").warning(f"PriceMonitor 注册失败: {e}")
    parts = []
    if n_fill:
        parts.append(f"成交开仓 {n_fill} 笔")
    if n_expire:
        parts.append(f"失效撤单 {n_expire} 笔")
    if n_stale:
        parts.append(f"行情过期跳过 {n_stale} 笔")
    if n_sat_limit:
        parts.append(f"卫星仓位上限拦截 {n_sat_limit} 笔")
    return "；".join(parts) if parts else "挂单无触发"


def get_pending_positions() -> pd.DataFrame:
    """已挂单待成交（限价委托）。"""
    with _conn() as c:
        df = pd.read_sql(
            "SELECT * FROM positions WHERE status='pending' ORDER BY id DESC", c)
    if df.empty:
        return df
    prices = _latest_prices(list(df["code"]))
    df["最新价"] = df["code"].map(lambda x: (prices.get(x) or (None,))[0])
    return df


def position_reconcile(today: str) -> str:
    """双账本对账自愈：柜台 ai 持仓 vs 经验库 open 持仓，双向修复。

    背景（2026-09-11 踩坑）：fill_check 中柜台买入（独立事务已扣款）与
    positions 记账 UPDATE 是两条事务，锁冲突/进程被杀会让"钱花了账没记"，
    产生孤儿仓（柜台有、账本无——止盈止损永远覆盖不到）。对账补记后，
    孤儿仓重新进入止盈/止损/到期管理（成本取柜台成本价，T+1 同样生效）。

    2026-09-18 修复：补充 diff<0 处理（experience中幽灵持仓，broker已无此股）。
    """
    import broker
    # ---- closing 仓结算（委托制）：委托成交→closed；日终撤单→回 open 次日重估 ----
    settled = []
    with _conn() as c:
        closing = pd.read_sql("SELECT * FROM positions WHERE status='closing'", c)
    if not closing.empty:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _conn() as c:
            c.execute("BEGIN IMMEDIATE")
            bc = c
            closing = pd.read_sql("SELECT * FROM positions WHERE status='closing'", c)
            for _, p in closing.iterrows():
                oid = p.get("sell_order_id")
                if pd.isna(oid) or not oid:
                    # 无委托号的 closing（异常残留）→ 回 open
                    c.execute("UPDATE positions SET status='open', sell_order_id=NULL,"
                              " sell_reason=NULL WHERE id=?", (int(p["id"]),))
                    continue
                row = bc.execute(
                    "SELECT status, filled_price, filled_ts, shares FROM broker_orders WHERE id=?",
                    (int(oid),)).fetchone()
                if not row:
                    continue
                st_, fprice, fts, sold_shares = row
                if st_ == "已成":
                    if not fprice or sold_shares <= 0 or sold_shares > int(p["shares"] or 0):
                        continue
                    remaining = int(p["shares"]) - sold_shares
                    if remaining:
                        c.execute("UPDATE positions SET status='open',shares=?,buy_amount=?,sell_order_id=NULL WHERE id=?",
                                  (remaining, round(remaining * p["buy_price"], 2), int(p["id"])))
                    else:
                        rules = _get_position_rules(p)
                        pnl = round(fprice / p["buy_price"] - 1 - rules["cost"], 6)
                        fill_day = str(fts or today)[:10]
                        c.execute("UPDATE positions SET status='closed',sell_date=?,sell_price=?,sell_ts=?,"
                                  "pnl_pct=?,hold_days=?,closed_at=? WHERE id=?",
                                  (fill_day, fprice, fts or now, pnl,
                                   _trade_days_between(str(p["buy_date"]), fill_day), now, int(p["id"])))
                    settled.append(f"{p.get('name') or p['code']}·成交")
                elif st_ == "已撤":
                    c.execute("UPDATE positions SET status='open', sell_order_id=NULL,"
                              " sell_reason=NULL WHERE id=?", (int(p["id"]),))
                    settled.append(f"{p.get('name') or p['code']}·撤单重持")
    fixed = []
    orphan_fixed = []
    mismatches = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        bc = c
        bposs = pd.read_sql("SELECT * FROM broker_positions WHERE shares>0", c)
        broker_ai_shares = {str(r["code"]): int(r["shares"]) for _, r in bposs.iterrows() if r["source"] == "ai"}
        exp_shares = dict(c.execute("SELECT code,SUM(COALESCE(shares,0)) FROM positions WHERE status IN ('open','closing') GROUP BY code").fetchall())
        # 按股票聚合后对账。旧逻辑逐行拿单笔仓位与柜台汇总比较，多个批次时会
        # 重复补记，并触发 UNIQUE(code,buy_date,source)。
        all_opens = pd.read_sql(
            "SELECT code, MAX(name) name, SUM(COALESCE(shares,0)) shares,"
            " MIN(buy_date) buy_date FROM positions WHERE status IN ('open','closing')"
            " GROUP BY code", c)
        for _, exp_row in all_opens.iterrows():
            code = str(exp_row["code"])
            exp_sh = int(exp_row["shares"] or 0)
            broker_sh = broker_ai_shares.get(code, 0)
            diff = broker_sh - exp_sh

            if diff > 0:
                # broker 有多余的 shares → 补记 experience
                cost = 0
                for _, bp in bposs.iterrows():
                    if str(bp["code"]) == code and (bp["source"] or "") == "ai":
                        cost = float(bp["cost"] or 0)
                        break
                if cost <= 0:
                    continue
                actual_buy_ts = now
                try:
                    fill_row = bc.execute(
                        "SELECT ts FROM broker_fills WHERE code=? AND side='buy'"
                        " AND source='ai' ORDER BY id DESC LIMIT 1", (code,)).fetchone()
                    if fill_row and fill_row[0]:
                        actual_buy_ts = fill_row[0]
                except Exception:
                    pass
                tracked_cost = c.execute("SELECT COALESCE(SUM(shares*buy_price),0) FROM positions WHERE code=? AND status IN ('open','closing')", (code,)).fetchone()[0]
                missing_cost = (broker_sh * cost - tracked_cost) / diff
                if missing_cost <= 0 or not np.isfinite(missing_cost):
                    mismatches.append(f"{code} 补记成本无法可靠还原")
                    continue
                cost = missing_cost
                buy_date = str(actual_buy_ts)[:10] if actual_buy_ts else today
                existing = c.execute(
                    "SELECT id,COALESCE(shares,0),status FROM positions"
                    " WHERE code=? AND buy_date=? AND source='reconcile_fix'",
                    (code, buy_date)).fetchone()
                if existing and existing[2] != 'open':
                    mismatches.append(f"{code} 已有历史或在途对账记录，需人工核对")
                    continue
                if existing:
                    new_shares = int(existing[1] or 0) + diff
                    old_cost = c.execute('SELECT buy_price FROM positions WHERE id=?', (existing[0],)).fetchone()[0]
                    cost = ((existing[1] or 0) * old_cost + diff * missing_cost) / new_shares
                    c.execute(
                        "UPDATE positions SET status='open',buy_price=?,buy_ts=?,shares=?,"
                        " buy_amount=?,closed_at=NULL WHERE id=?",
                        (cost, actual_buy_ts, new_shares,
                         round(new_shares * cost, 2), int(existing[0])))
                else:
                    c.execute(
                        "INSERT INTO positions (code,name,buy_date,buy_price,buy_ts,pick_id,"
                        " source,pack_name,status,limit_price,shares,buy_amount,created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,'open',NULL,?,?,?)",
                        (code, exp_row.get("name") or code, buy_date, cost, actual_buy_ts,
                         None, "reconcile_fix", "对账补记", diff,
                         round(diff * cost, 2), actual_buy_ts))
                fixed.append(f"{exp_row.get('name') or code}×+{diff}")

            elif diff < 0:
                if broker_sh > 0:
                    mismatches.append(f"{code} 策略{exp_sh}股/柜台{broker_sh}股，需核对成交")
                # experience 有多余的 shares → broker 已无此股，标记为幽灵仓
                # 检查 broker 是否完全无此股
                if broker_sh == 0:
                    # broker 已完全卖出，experience 未记录 → 关闭幽灵仓
                    c.execute(
                        "UPDATE positions SET status='closed', sell_reason='对账清理(幽灵仓)',"
                        " sell_date=?, closed_at=? WHERE code=? AND status IN ('open','closing')",
                        (today, now, code))
                    orphan_fixed.append(f"{exp_row.get('name') or code}×{exp_sh}")

        # 处理 broker 有但 experience 完全没有的 code（补记）
        for _, bp in bposs.iterrows():
            if (bp["source"] or "") != "ai":
                continue
            code = str(bp["code"])
            broker_sh = int(bp["shares"] or 0)
            if broker_sh <= 0:
                continue
            if code not in exp_shares:
                cost = float(bp["cost"] or 0)
                if cost <= 0:
                    continue
                actual_buy_ts = now
                try:
                    fill_row = bc.execute(
                        "SELECT ts FROM broker_fills WHERE code=? AND side='buy'"
                        " AND source='ai' ORDER BY id DESC LIMIT 1", (code,)).fetchone()
                    if fill_row and fill_row[0]:
                        actual_buy_ts = fill_row[0]
                except Exception:
                    pass
                inserted = c.execute(
                    "INSERT OR IGNORE INTO positions (code, name, buy_date, buy_price, buy_ts, pick_id,"
                    " source, pack_name, status, limit_price, shares, buy_amount, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?, 'open', NULL, ?, ?, ?)",
                    (code, bp.get("name") or code, str(bp.get("last_buy_date") or today),
                     cost, actual_buy_ts, None, "reconcile_fix", "对账补记",
                     broker_sh, round(broker_sh * cost, 2), actual_buy_ts))
                if inserted.rowcount:
                    fixed.append(f"{bp.get('name') or code}×{broker_sh}")
                else:
                    mismatches.append(f"{code} 与历史对账记录冲突，未覆盖历史")

    parts = []
    if fixed:
        parts.append("对账补记：" + ",".join(fixed))
    if orphan_fixed:
        parts.append("幽灵仓清理：" + ",".join(orphan_fixed))
    if mismatches:
        parts.append("账本差异：" + ";".join(mismatches))
    if not parts:
        parts.append("对账一致")
    return " · ".join(parts) + (" · closing结算：" + ",".join(settled) if settled else "")


def position_close_check(today: str) -> str:
    """盘中检查持仓：止盈/止损/到期平仓（T+1：买入日当天不卖）。

    价格取 ifind_realtime 最新快照；触发后走柜台市价卖出，盈亏按实际成交价
    记账（不按触发价）；卖出前做双账本校验（经验库 open 股数 vs 柜台 ai 持仓
    股数，不一致则跳过该代码并计入消息，避免账本漂移后卖错数量）。
    """
    with _conn() as c:
        opens = pd.read_sql("SELECT * FROM positions WHERE status='open'", c)
    if opens.empty:
        return "无持仓"
    prices = _latest_prices(list(opens["code"]))
    quote_times = _latest_price_times(list(opens["code"]))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # M3 逻辑腿要用的强弱三态（持仓股；失败则 None→到期即平，维持现状）
    _states_map = None
    try:
        import signals as sig
        codes_open = list(opens["code"])
        panel = sig.get_panel_cached(codes_open, today, 800)
        _states_map = sig.strength_states(codes_open, panel)
    except Exception:
        _states_map = None
    # 双账本一致性：柜台 ai 股数 应等于 经验库同代码 open 股数合计
    import broker
    try:
        bposs = broker.get_positions()
        broker_shares = {p["code"]: int(p["shares"] or 0)
                         for _, p in bposs.iterrows() if (p["source"] or "") == "ai"}
    except Exception:
        broker_shares = {}
    open_sum = opens.groupby("code")["shares"].sum()
    n_close = n_skip = n_order = n_stale = n_sell_fail = 0
    _mc_updates = []  # M3 吊灯基准的滚动最高收盘，循环末统一写（防与柜台写锁自锁）
    with _conn() as c:
        for _, p in opens.iterrows():
            if str(p["buy_date"]) >= today:
                continue  # T+1：买入日当天不卖
            code = str(p["code"])
            bs = broker_shares.get(code, 0)
            es = int(open_sum.get(code) or 0)
            # 双账本校验：broker无持仓但experience有 → 跳过（实际未买入）
            if bs == 0 and es > 0:
                n_skip += 1
                continue
            # broker有持仓但experience无 → 跳过（数据异常）
            if es == 0 and bs > 0:
                n_skip += 1
                continue
            # 两者都有持仓但数量不一致 → 允许卖出experience记录的数量（broker有足够库存）
            # 只有broker库存不足时才跳过
            pr = prices.get(code)
            cur = pr[0] if pr and pr[0] else None
            if not _quote_is_fresh(quote_times.get(code)):
                n_stale += 1
                continue
            if not cur:
                continue
            # 每个仓位独立选规则：事件增强票用更紧的止损/止盈
            pk_name = str(p.get("pack_name") or "")
            r = _get_position_rules(p)
            entry = p["buy_price"]
            tp, sl = entry * (1 + r["take_profit"]), entry * (1 + r["stop_loss"])
            # M4 自适应止损：有入场 ATR 上下文的仓位，止损收紧到 1.5×ATR%（夹取 [-8%,-2%]）
            atr_e0 = p["atr_entry"] if "atr_entry" in p.index else None
            if atr_e0 is not None and pd.notna(atr_e0) and entry:
                sl_dyn = -min(0.08, max(0.02, 1.5 * atr_e0 / entry))
                sl = max(sl, entry * (1 + sl_dyn))  # 取更紧（更高）者，更早止血
            reason = limit_price = None
            if cur >= tp:
                # 止盈：挂止盈价，价格再次触及才成交（回落不成交=继续持有，实盘如此）
                reason, limit_price = "止盈", round(tp, 2)
            elif cur <= sl:
                # 止损：要务是成交——限价略低于触发价让半步（跌停板上broker会自动转挂等开板）
                reason, limit_price = "止损", round(cur * 0.995, 2)
            else:
                # ---- M3 双腿：破位（跌破入场时登记的支撑区下沿）/ 吊灯止盈（入场最高点回撤）----
                atr_e = p["atr_entry"] if "atr_entry" in p.index else None
                sup_e = p["sup_lo_entry"] if "sup_lo_entry" in p.index else None
                mc_old = p["max_close"] if "max_close" in p.index else None
                # P1-1修复：吊灯止盈的 max_close 只用已确认收盘价，不用盘中快照
                # 盘中检查时用 mc_old 做比较；收盘后 job_max_close_update 统一用当日收盘更新
                mc = max(x for x in [entry, mc_old] if x and pd.notna(x))
                if atr_e is not None and pd.notna(atr_e) and sup_e is not None and pd.notna(sup_e):
                    if cur < sup_e - 0.5 * atr_e:
                        reason, limit_price = "破位(SR)", round(cur * 0.995, 2)
                # P1-2修复：吊灯止盈使用当前ATR（更贴近当前波动率），不用入场ATR
                if not reason:
                    try:
                        import density_sr
                        cur_sr = density_sr.latest_sr_map([code], asof=today)
                        cur_atr = float(cur_sr.loc[code, "atr"]) if (cur_sr is not None
                                    and not cur_sr.empty and code in cur_sr.index
                                    and pd.notna(cur_sr.loc[code, "atr"])) else atr_e
                    except Exception:
                        cur_atr = atr_e
                    if cur_atr is not None and pd.notna(cur_atr) and mc and cur < mc - 3.0 * cur_atr:
                        reason, limit_price = "吊灯止盈", round(cur * 0.995, 2)
                if not reason:
                    hd = _trade_days_between(str(p["buy_date"]), today)
                    if hd >= r["hold_days"]:
                        next_review = str(p.get("next_review_date") or "")
                        if next_review and today < next_review:
                            continue
                        # M3 逻辑腿：到期且转弱才平；仍强则顺延（防好票被日历赶下车）
                        extend = int(p["extend_count"] or 0) if "extend_count" in p.index else 0
                        # P2-10修复：strength_states()失败时默认"strong"而非"weak"，避免强制平仓
                        state = "strong" if _states_map is None else _states_map.get(code, "weak")
                        # P2-3修复：自适应顺延上限（strong=2次, neutral=1次, weak=0次）
                        max_extend = 2 if state == "strong" else (1 if state == "neutral" else 0)
                        if extend < max_extend and str(p.get("last_extend_date") or "") != today:
                            # 强势每次顺延3个交易日，中性顺延2个；同一交易日只记一次。
                            review_days = 3 if state == "strong" else 2
                            cal = _calendar()
                            if today in cal:
                                idx = min(cal.index(today) + review_days, len(cal) - 1)
                                review_date = cal[idx]
                            else:
                                d = pd.Timestamp(today)
                                n = 0
                                while n < review_days:
                                    d += pd.Timedelta(days=1)
                                    if d.weekday() < 5:
                                        n += 1
                                review_date = d.strftime("%Y-%m-%d")
                            c.execute("UPDATE positions SET extend_count=?,last_extend_date=?,"
                                      " next_review_date=? WHERE id=? AND status='open'",
                                      (extend + 1, today, review_date, int(p["id"])))
                            c.commit()
                            continue
                        reason, limit_price = "到期", round(cur * 0.995, 2)
            if reason:
                # 委托制（实盘规则）：触发只挂单，触及才成交；当日未成交收盘自动撤，次日重估重挂
                c.commit()  # 先提交顺延元数据，避免持有写锁调用柜台
                try:
                    msg = broker.sell_position(int(p["id"]), int(p["shares"] or 0),
                                               limit_price, reason)
                except Exception:
                    import logging
                    logging.getLogger("experience").exception("持仓 %s 卖出失败", p["id"])
                    n_sell_fail += 1
                    continue
                if "已成交" in msg:
                    n_close += 1
                    # 取消 PriceMonitor 监控（仅成交后；挂单中继续持有、继续监控）
                    try:
                        from price_monitor import monitor
                        monitor.unregister(code, int(p["id"]))
                    except Exception:
                        pass
                elif "已挂单" in msg:
                    n_order += 1
                else:
                    n_sell_fail += 1  # 可卖不足、行情异常等：保持 open，下周期重试并告警

    parts = [f"平仓 {n_close} 笔"] if n_close else ["持仓检查：无触发"]
    if n_order:
        parts.append(f"挂出卖单 {n_order} 笔")
    if n_skip:
        parts.append(f"账本不一致跳过 {n_skip} 笔（经验库与柜台股数对不上）")
    if n_stale:
        parts.append(f"行情超过10分钟跳过 {n_stale} 笔")
    if n_sell_fail:
        parts.append(f"卖出触发但下单失败 {n_sell_fail} 笔（将重试）")
    return "；".join(parts)


def update_max_close(today: str) -> str:
    """收盘后更新所有 open 持仓的 max_close（用当日确认收盘价）。

    P1-1修复：吊灯止盈的 max_close 只用已确认收盘价，不用盘中快照。
    此函数在盘后由 scheduler 调用，用当日收盘价更新 max_close。
    """
    with _conn() as c:
        opens = pd.read_sql(
            "SELECT id, code, max_close FROM positions WHERE status='open'", c)
    if opens.empty:
        return "无持仓需更新"
    prices = _latest_prices(list(opens["code"]))
    updated = 0
    with _conn() as c:
        for _, p in opens.iterrows():
            code = str(p["code"])
            pr = prices.get(code)
            close = pr[0] if pr and pr[0] else None
            if not close:
                continue
            mc_old = p["max_close"] if "max_close" in p.index else None
            mc_new = max(x for x in [close, mc_old] if x and pd.notna(x))
            if mc_new != mc_old:
                c.execute("UPDATE positions SET max_close=? WHERE id=?", (mc_new, int(p["id"])))
                updated += 1
    return f"更新 {updated} 条 max_close"


def get_open_positions() -> pd.DataFrame:
    """当前持仓（open）+ 最新快照价 + 浮动盈亏（% 和 金额元）+ 可卖/止盈止损价。"""
    with _conn() as c:
        df = pd.read_sql("SELECT * FROM positions WHERE status IN ('open','closing') ORDER BY id DESC", c)
    if df.empty:
        return df
    today = datetime.now().strftime("%Y-%m-%d")
    prices = _latest_prices(list(df["code"]))
    df["最新价"] = df["code"].map(lambda x: (prices.get(x) or (None,))[0])
    df["浮动盈亏%"] = (df["最新价"] / df["buy_price"] - 1) * 100
    df["浮动盈亏额"] = (df["最新价"] - df["buy_price"]) * df["shares"].fillna(0)
    df["市值"] = df["最新价"].fillna(df["buy_price"]) * df["shares"].fillna(0)
    df["持有交易日"] = df["buy_date"].map(
        lambda d: _trade_days_between(str(d), today))
    df["可卖(股)"] = df.apply(
        lambda r: int(r["shares"] or 0) if str(r["buy_date"]) < today and r["status"] == "open" else 0, axis=1)
    # 根据每行的 pack_name 使用正确的规则集计算止盈止损价
    df["止盈价"] = df.apply(
        lambda r: round(r["buy_price"] * (1 + _get_position_rules(r)["take_profit"]), 2), axis=1)
    df["止损价"] = df.apply(
        lambda r: round(r["buy_price"] * (1 + _get_position_rules(r)["stop_loss"]), 2), axis=1)
    return df


def manual_sell(position_id: int, shares: int) -> str:
    """手动卖出 AI 自动持仓：走柜台真实卖出（回笼资金、T+1 校验），成功后平仓/减仓记录。"""
    import broker
    return broker.sell_position(int(position_id), shares)


def get_position_history(limit: int = 100) -> pd.DataFrame:
    """已平仓持仓（新→旧）。"""
    with _conn() as c:
        return pd.read_sql(
            "SELECT * FROM positions WHERE status='closed' ORDER BY id DESC LIMIT ?",
            c, params=(limit,))


def position_stats() -> dict:
    """持仓汇总：胜率/平均收益率/累计收益率（按已平仓）+ 当前持仓数 + 净值口径回撤（M1 修复）。"""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*), AVG(pnl_pct), SUM(pnl_pct),"
            " SUM(CASE WHEN pnl_pct>0 THEN 1 ELSE 0 END) FROM positions WHERE status='closed'"
        ).fetchone()
        n_open = c.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
    n, avg, total, wins = row
    out = {"已平仓": n or 0, "胜率": (wins / n if n else None),
           "平均收益率": avg, "累计收益率": total, "当前持仓": n_open}
    # 净值口径统计（account_nav_daily，2026-09-15 M1 起）——回撤不再是占位的 0
    nv = nav_stats()
    if nv:
        out["最大回撤"] = -nv["最大回撤"]  # 存的是负数，展示用正数口径
        out["当前净值"] = nv["当前净值"]
        out["年化收益率"] = nv["年化收益率"]
    return out


# ---------------------------------------------------------------- 净值序列（M1 数据地基）
_NAV_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_nav_daily(
    date TEXT PRIMARY KEY, total_assets REAL, cash REAL, position_mv REAL,
    nav REAL, daily_ret REAL, drawdown REAL, created_at TEXT);
"""


def rebuild_nav_history() -> int:
    """回放法重建净值曲线：从首笔成交起，每日 前日持仓×当日收盘 + 当日现金流。
    现金流水（fills）+ 外部出入金（cashflows 的 入金/初始入金/出金）都参与；
    净值用时间加权（TWR）链式：r_t = (当日总资产 − 当日净入金)/昨日总资产 − 1，
    nav_t = nav_{t−1}×(1+r_t)——入金不再虚增收益（2026-09-15 对账发现三次追加入金）。
    全量重算幂等覆盖。返回写入天数。"""
    import broker

    with broker._conn() as c:
        fills = pd.read_sql(
            "SELECT date, ts, code, side, amount, fee, tax, shares FROM broker_fills ORDER BY ts", c)
        cfs = pd.read_sql(
            "SELECT ts, type, amount FROM broker_cashflows ORDER BY ts", c)
    if fills.empty and cfs.empty:
        return 0
    first_day = min(fills["date"].min() if not fills.empty else "9999",
                    cfs["ts"].str[:10].min() if not cfs.empty else "9999")

    import datasource
    with datasource._conn() as c:
        px = pd.read_sql(
            "SELECT code, date, close FROM market_daily WHERE source='ths_ifind' AND date>=?",
            c, params=(first_day,))
    if px.empty:
        raise ValueError("行情为空，拒绝覆盖历史净值")
    cal = sorted(px["date"].unique())
    close = px.pivot(index="date", columns="code", values="close").ffill()  # 停牌沿用前收

    cash, hold = 0.0, {}
    fills_by_date = {d: g for d, g in fills.groupby("date")} if not fills.empty else {}
    ext_by_date = {}
    if not cfs.empty:
        cfs["d"] = cfs["ts"].str[:10]
        # 只取外部出入金；买入/卖出腿在 fills 里已逐笔处理，不能再算（否则双重计数）
        ext_types = ("入金", "初始入金", "出金")
        for d, g in cfs[cfs["type"].isin(ext_types)].groupby("d"):
            ext_by_date[d] = float(g["amount"].sum())  # 入金为正/出金为负（按表内符号惯例）

    nav, peak_nav, prev_total = 1.0, 1.0, None
    rows = []
    now = datetime.now().strftime("%F %T")
    for day in cal:
        ext = ext_by_date.get(day, 0.0)
        g = fills_by_date.get(day)
        if g is not None:
            for f in g.itertuples():
                if f.side == "buy":
                    cash -= (f.amount or 0) + (f.fee or 0)
                    hold[f.code] = hold.get(f.code, 0) + (f.shares or 0)
                else:
                    cash += (f.amount or 0) - (f.fee or 0) - (f.tax or 0)
                    hold[f.code] = hold.get(f.code, 0) - (f.shares or 0)
        cash += ext  # 当日净入金
        mv = 0.0
        if day in close.index:
            prow = close.loc[day]
            for cd, sh in hold.items():
                if sh > 0:
                    p = prow.get(cd)
                    if pd.isna(p) or not np.isfinite(float(p)) or float(p) <= 0:
                        raise ValueError(f"{day} {cd} 持仓缺少有效价格，拒绝覆盖历史净值")
                    mv += float(p) * sh
        total = cash + mv
        if prev_total:
            r = (total - ext) / prev_total - 1
            nav *= (1 + r)
        peak_nav = max(peak_nav, nav)
        dd = nav / peak_nav - 1
        rows.append((day, round(total, 2), round(cash, 2), round(mv, 2),
                     round(nav, 6), round(r if prev_total else 0.0, 6), round(dd, 6), now))
        prev_total = total
    with _conn() as c:
        c.executescript(_NAV_SCHEMA)
        c.execute("DELETE FROM account_nav_daily")
        c.executemany("INSERT INTO account_nav_daily VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def snapshot_nav_today() -> str:
    """每日收盘后落库当日净值（TWR 口径，与 rebuild 同源）。返回日期。"""
    import broker
    acc = broker.get_account()
    if acc.get("估值有效") is not True:
        raise ValueError("持仓估值无效，拒绝写入净值快照")
    total = acc.get("总资产", 0) or 0
    cash = (acc.get("可用资金", 0) or 0) + (acc.get("冻结资金", 0) or 0)
    mv = acc.get("持仓市值", 0) or 0
    if not all(np.isfinite(v) for v in (total, cash, mv)) or total <= 0 or mv < 0:
        raise ValueError("账户资产数据无效，拒绝写入净值快照")
    day = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        c.executescript(_NAV_SCHEMA)
        # prev 必须是"今日之前"的最后净值——同日已存在回放行时拿来当基准会把日收益算成 0
        prev = c.execute("SELECT total_assets, nav FROM account_nav_daily WHERE date<? "
                         "ORDER BY date DESC LIMIT 1", (day,)).fetchone()
        peak_nav = c.execute("SELECT MAX(nav) FROM account_nav_daily WHERE date<?", (day,)).fetchone()[0] or 1.0
    with broker._conn() as c:
        ext = c.execute("SELECT COALESCE(SUM(amount),0) FROM broker_cashflows WHERE ts LIKE ?"
                        " AND type IN ('入金','初始入金','出金')",  # 买卖腿在 fills 里，不重复计
                        (day + "%",)).fetchone()[0]
    if prev and prev[0]:
        r = (total - ext) / prev[0] - 1
        nav = prev[1] * (1 + r)
    else:
        r, nav = 0.0, 1.0
    peak_nav = max(peak_nav, nav)
    dd = nav / peak_nav - 1
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO account_nav_daily VALUES (?,?,?,?,?,?,?,?)",
                  (day, round(total, 2), round(cash, 2), round(mv, 2),
                   round(nav, 6), round(r, 6), round(dd, 6), datetime.now().strftime("%F %T")))
    return day


def nav_stats() -> dict:
    """净值统计：当前净值/最大回撤/最大回撤日期/年化（供战报与风控熔断）。"""
    with _conn() as c:
        c.executescript(_NAV_SCHEMA)
        df = pd.read_sql("SELECT * FROM account_nav_daily ORDER BY date", c)
    if df.empty:
        return {}
    mdd_i = df["drawdown"].idxmin()
    n_days = len(df)
    nav_last = float(df["nav"].iloc[-1])
    ann = (nav_last ** (252 / n_days) - 1) if n_days > 1 else 0.0
    return {"当前净值": nav_last,
            "最大回撤": float(df["drawdown"].min()),
            "最大回撤日期": df.loc[mdd_i, "date"],
            "年化收益率": ann,
            "净值天数": n_days}


# ---------------------------------------------------------------- 组合风控（M4）
_RISK_FLAG = DATA_DIR / "risk_state.json"  # 当日风控状态（开仓闸）


def account_risk_level(drawdown: float, cfg: dict | None = None) -> tuple[str, float]:
    """按正数回撤幅度返回 normal/yellow/orange/red 及目标仓位。"""
    cfg = cfg or get_account_risk_config()
    import math
    try:
        value = float(drawdown)
    except (TypeError, ValueError):
        return "red", cfg["red_target"]
    if not math.isfinite(value):
        return "red", cfg["red_target"]
    dd = abs(min(value, 0.0))
    if dd >= cfg["red_drawdown"]:
        return "red", cfg["red_target"]
    if dd >= cfg["orange_drawdown"]:
        return "orange", cfg["orange_target"]
    if dd >= cfg["yellow_drawdown"]:
        return "yellow", cfg["yellow_target"]
    return "normal", cfg["normal_target"]


def build_risk_reduction_plan(rk: dict, today: str, level_override: str | None = None,
                              target_override: float | None = None) -> dict:
    """生成影子减仓计划；只计算建议，不产生任何真实卖单。"""
    import broker
    cfg = get_account_risk_config()
    level, target = account_risk_level(rk.get("dd_now", 0), cfg)
    if level_override in ("normal", "yellow", "orange", "red"):
        level = level_override
    if target_override is not None:
        target = float(target_override)
    acc = broker.get_account()
    total = float(acc.get("总资产", 0) or 0)
    opens = get_open_positions()
    current_mv = float(opens["市值"].sum()) if (not opens.empty and "市值" in opens) else 0.0
    current_ratio = current_mv / total if total > 0 else 0.0
    release = max(0.0, current_mv - total * target)
    rows = []
    if release > 0 and not opens.empty:
        for _, p in opens.iterrows():
            source = str(p.get("source") or "")
            pnl = float(p.get("浮动盈亏%") or 0) / 100
            hold = int(p.get("持有交易日") or 0)
            rules = _get_position_rules(p)
            score = 0.0
            reasons = []
            if source in ("satellite_scan", "sched_satellite_scan"):
                score += 35; reasons.append("卫星来源")
            if pnl < 0:
                score += min(25, abs(pnl) * 200); reasons.append("当前浮亏")
            if hold >= int(rules["hold_days"]):
                score += 20; reasons.append("已到持有期")
            if str(p.get("sell_reason") or ""):
                score += 20; reasons.append(str(p.get("sell_reason")))
            value = float(p.get("市值") or 0)
            rows.append({"position_id": int(p["id"]), "code": p["code"],
                         "name": p.get("name") or "", "source": source,
                         "market_value": round(value, 2), "risk_score": round(score, 2),
                         "reasons": reasons})
        rows.sort(key=lambda x: (x["risk_score"], x["market_value"]), reverse=True)
        remain = release
        for row in rows:
            suggested = min(row["market_value"], remain)
            row["suggested_sell_value"] = round(max(0.0, suggested), 2)
            remain -= suggested
    plan = {"date": today, "level": level, "drawdown": rk.get("dd_now"),
            "target_position_ratio": target, "current_position_ratio": current_ratio,
            "required_release": round(release, 2), "shadow_only": True,
            "positions": rows, "created_at": datetime.now().strftime("%F %T")}
    tmp = RISK_PLAN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    tmp.replace(RISK_PLAN_FILE)
    return plan


def latest_risk_plan() -> dict:
    try:
        return json.loads(RISK_PLAN_FILE.read_text()) if RISK_PLAN_FILE.exists() else {}
    except Exception:
        return {}


def risk_llm_advice(plan: dict, force: bool = False) -> dict:
    """生成只读 LLM 风控意见；按日期和结构化风险数据哈希缓存。

    LLM 不得修改风险等级、目标仓位或触发交易；失败时保留规则引擎结果。
    """
    import hashlib
    import llmutil

    level = str(plan.get("level") or "normal")
    if level == "normal":
        advice = {"status": "skipped", "reason": "正常等级不调用 LLM，节省 Token",
                  "date": plan.get("date"), "level": level, "advisory_only": True}
        RISK_LLM_ADVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = RISK_LLM_ADVICE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(advice, ensure_ascii=False, indent=2))
        tmp.replace(RISK_LLM_ADVICE_FILE)
        return advice

    top = []
    for row in (plan.get("positions") or [])[:8]:
        top.append({
            "code": str(row.get("code") or ""),
            "source": str(row.get("source") or ""),
            "market_value": round(float(row.get("market_value") or 0), 2),
            "risk_score": round(float(row.get("risk_score") or 0), 2),
            "suggested_sell_value": round(float(row.get("suggested_sell_value") or 0), 2),
            "reasons": [str(x)[:40] for x in (row.get("reasons") or [])[:3]],
        })
    evidence = {
        "date": str(plan.get("date") or ""),
        "level": level,
        "drawdown": round(float(plan.get("drawdown") or 0), 6),
        "current_position_ratio": round(float(plan.get("current_position_ratio") or 0), 4),
        "target_position_ratio": round(float(plan.get("target_position_ratio") or 0), 4),
        "required_release": round(float(plan.get("required_release") or 0), 2),
        "positions": top,
    }
    raw_evidence = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    data_hash = hashlib.sha256(
        f"{RISK_LLM_PROMPT_VERSION}\n{raw_evidence}".encode()).hexdigest()[:16]
    if not force:
        try:
            cached = json.loads(RISK_LLM_ADVICE_FILE.read_text())
            if cached.get("date") == evidence["date"] and cached.get("data_hash") == data_hash:
                cached["cache_hit"] = True
                return cached
        except Exception:
            pass

    # 固定规则与 Schema 全部位于稳定 system 前缀；user 只承载末尾动态数据，
    # 同时保留应用层“日期 + 数据 hash”精确缓存。
    user = "账户风险证据 JSON：\n" + raw_evidence
    reply = llmutil.llm_chat(
        RISK_LLM_SYSTEM_PROMPT, user, max_tokens=650,
        label="account_risk_advice_v2")
    if not reply:
        return {"status": "unavailable", "date": evidence["date"], "level": level,
                "data_hash": data_hash, "reason": llmutil.llm_failure_reason()}
    try:
        import re
        match = re.search(r"\{[\s\S]*\}", reply)
        parsed = json.loads(match.group()) if match else {}
        decision = str(parsed.get("decision") or "转人工复核")
        if decision not in ("维持规则计划", "建议收紧", "转人工复核"):
            decision = "转人工复核"
        rule_target = float(evidence["target_position_ratio"])
        try:
            recommended_target = float(parsed.get("recommended_target_position"))
        except (TypeError, ValueError):
            recommended_target = rule_target
            decision = "转人工复核"
        # LLM 只可维持或收紧，绝不能突破规则引擎的目标仓位上限。
        recommended_target = max(0.0, min(rule_target, recommended_target))
        valid_codes = {x["code"] for x in top}
        priority_positions = []
        used_priorities = set()
        for item in (parsed.get("priority_positions") or [])[:5]:
            if not isinstance(item, dict) or str(item.get("code") or "") not in valid_codes:
                continue
            try:
                priority = int(item.get("priority"))
            except (TypeError, ValueError):
                continue
            action = str(item.get("action") or "人工复核")
            if priority < 1 or priority in used_priorities or action not in ("优先减仓", "继续观察", "人工复核"):
                continue
            used_priorities.add(priority)
            priority_positions.append({
                "code": str(item["code"]), "priority": priority, "action": action,
                "reason": str(item.get("reason") or "")[:120],
            })
        priority_positions.sort(key=lambda x: x["priority"])
        disagreement = parsed.get("rule_disagreement") or {}
        if not isinstance(disagreement, dict):
            disagreement = {}
        advice = {
            "status": "ok", "date": evidence["date"], "level": level,
            "data_hash": data_hash, "prompt_version": RISK_LLM_PROMPT_VERSION,
            "cache_hit": False, "decision": decision,
            # 兼容调度结果和旧页面字段。
            "stance": {"维持规则计划": "维持", "建议收紧": "收紧",
                       "转人工复核": "人工复核"}[decision],
            "confidence": max(0.0, min(1.0, float(parsed.get("confidence") or 0))),
            "recommended_target_position": recommended_target,
            "summary": str(parsed.get("summary") or "")[:160],
            "priority_positions": priority_positions,
            "account_actions": [str(x)[:120] for x in (parsed.get("account_actions") or [])[:3]],
            "rule_disagreement": {
                "has_disagreement": bool(disagreement.get("has_disagreement", False)),
                "reason": str(disagreement.get("reason") or "")[:160],
            },
            "missing_evidence": [str(x)[:120] for x in (parsed.get("missing_evidence") or [])[:3]],
            "created_at": datetime.now().strftime("%F %T"),
            "advisory_only": True,
        }
    except Exception as exc:
        advice = {"status": "invalid", "date": evidence["date"], "level": level,
                  "data_hash": data_hash, "reason": f"LLM 输出解析失败：{type(exc).__name__}"}
    RISK_LLM_ADVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RISK_LLM_ADVICE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(advice, ensure_ascii=False, indent=2))
    tmp.replace(RISK_LLM_ADVICE_FILE)
    return advice


def latest_risk_llm_advice() -> dict:
    try:
        return json.loads(RISK_LLM_ADVICE_FILE.read_text()) if RISK_LLM_ADVICE_FILE.exists() else {}
    except Exception:
        return {}

def portfolio_risk(use_live: bool = False) -> dict:
    """组合风控评估（M4）：账户净值序列波动率 → 日 VaR 近似 + 熔断状态。

    口径：σ = 净值日收益 20 日标准差（含真实持仓结构信息，比 60 日个股全相关矩阵稳——
    A 股个股两两全相关噪声大、伪相关多）；日 VaR(95%) ≈ 1.65×σ×总资产；
    熔断线 = −2×σ×√5（约当 95% 置信单周极端损失）。

    P1-3修复：支持 use_live=True 时从实时持仓计算当前回撤，不用昨日快照。
    """
    import broker
    nv = nav_stats()
    if not nv or nv["净值天数"] < 5:
        return {"ok": False, "reason": "净值序列不足 5 日"}
    with _conn() as c:
        df = pd.read_sql("SELECT date, daily_ret, drawdown FROM account_nav_daily ORDER BY date", c)
    acc = broker.get_account(require_fresh=use_live)
    if acc.get("估值有效") is not True:
        return {"ok": False, "reason": "持仓估值缺失、无效或行情过期"}
    total = acc.get("总资产", 0) or 0
    if not np.isfinite(total) or total <= 0:
        return {"ok": False, "reason": "账户总资产无效"}
    returns = pd.to_numeric(df["daily_ret"].iloc[-20:], errors="coerce")
    if len(returns) < 5 or not np.isfinite(returns.to_numpy()).all():
        return {"ok": False, "reason": "净值收益序列缺失或无效"}
    sigma = float(returns.std())
    var_day = 1.65 * sigma * total
    circuit_line = -2 * sigma * np.sqrt(5)

    # use_live=True：用当前总资产推算实时净值，再相对历史净值峰值计算回撤。
    if use_live:
        try:
            with _conn() as c2:
                nav_df = pd.read_sql(
                    "SELECT date,total_assets,nav FROM account_nav_daily ORDER BY date", c2)
            if not nav_df.empty and float(nav_df.iloc[-1]["total_assets"] or 0) > 0:
                last = nav_df.iloc[-1]
                live_nav = float(last["nav"]) * total / float(last["total_assets"])
                peak_nav = max(float(nav_df["nav"].max()), live_nav)
                dd_now = live_nav / peak_nav - 1 if peak_nav > 0 else 0.0
                nav_values = pd.to_numeric(nav_df["nav"], errors="coerce")
                if (not np.isfinite(nav_values.to_numpy()).all()
                        or (nav_values <= 0).any()
                        or not np.isfinite(float(last["total_assets"]))):
                    return {"ok": False, "reason": "实时风控净值基准无效"}
            else:
                return {"ok": False, "reason": "实时风控缺少净值基准"}
        except Exception:
            return {"ok": False, "reason": "实时风控净值计算失败"}
    else:
        dd_now = float(df["drawdown"].iloc[-1])
    if not np.isfinite(dd_now) or not -1 <= dd_now <= 0:
        return {"ok": False, "reason": "账户回撤无效"}

    # σ 地板：净值近乎不动的账户（新建/空仓）熔断线≈0 会永久停开仓——无波动信号不熔断
    # P1-5修复：降低 σ 地板到 0.001，减少新账户豁免
    circuit = bool(dd_now <= circuit_line) if sigma >= 0.001 else False
    return {"ok": True, "sigma": sigma, "var_day": var_day,
            "var_pct": 1.65 * sigma, "circuit_line": circuit_line,
            "dd_now": dd_now, "circuit": circuit,
            "nav": nv["当前净值"], "mdd": nv["最大回撤"]}


def risk_halt_today(today: str) -> tuple[bool, str]:
    """风险评估缺失、损坏或非当日时关闭开仓闸；卖出不受影响。"""
    try:
        state = json.loads(_RISK_FLAG.read_text())
        if state.get("date") != today or not isinstance(state.get("halt"), bool):
            return True, "缺少有效的当日风控评估"
        if state.get("level") not in ("normal", "yellow", "orange", "red"):
            return True, "风控等级无效"
        return bool(state["halt"] or state["level"] in ("orange", "red")), state.get("reason", "")
    except Exception:
        return True, "风控状态读取失败，暂停买入"


def satellite_halt_today(today: str) -> tuple[bool, str]:
    halt, reason = risk_halt_today(today)
    if halt:
        return halt, reason
    try:
        state = json.loads(_RISK_FLAG.read_text())
        return state.get("level") != "normal", state.get("reason", "账户风险预警")
    except Exception:
        return True, "风控状态读取失败"


def _write_risk_flag(today: str, halt: bool, reason: str, level: str = "normal",
                     target_position_ratio: float | None = None):
    """P2-4修复：使用原子写入（先写临时文件再重命名），防止中断导致JSON损坏。"""
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump({"date": today, "halt": halt, "level": level,
                       "target_position_ratio": target_position_ratio, "reason": reason,
                       "ts": datetime.now().strftime("%F %T")}, f, ensure_ascii=False)
        # 原子重命名
        os.replace(tmp_path, str(_RISK_FLAG))
    except Exception:
        # 清理临时文件
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def atr_pct_of(code: str, end: str, n: int = 14) -> float | None:
    """个股的 ATR 占价比（波动率代理）：Wilder ATR(n)/最新收盘。无数据返回 None。"""
    import datasource
    with datasource._conn() as c:
        df = pd.read_sql(
            "SELECT date, high, low, close FROM market_daily WHERE source='ths_ifind' "
            "AND code=? AND date<=? ORDER BY date DESC LIMIT ?", c, params=(code, end, n + 40))
    if len(df) < n + 2:
        return None
    df = df.iloc[::-1].reset_index(drop=True)
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean().iloc[-1]
    close = df["close"].iloc[-1]
    return float(atr / close) if close > 0 else None


# ---------------------------------------------------------------- 分数→胜率校准（M7）
def score_winrate_calibration() -> dict:
    """picks 综合分（包内分位归一）→ 实盘胜率的单调校准曲线。

    综合分是序数不是概率，必须先校准再进仓位公式（M7 评审意见）。
    用 trades 表实盘结果拟合；样本 <30 返回空（调用方回退 0.5 中性）。"""
    with _conn() as c:
        df = pd.read_sql(
            """SELECT pi.pick_id, pi.score, t.pnl_pct FROM pick_items pi
               JOIN trades t ON t.pick_id=pi.pick_id AND t.code=pi.code
               WHERE t.pnl_pct IS NOT NULL AND pi.score IS NOT NULL""", c)
    if len(df) < 30:
        return {"n": len(df), "curve": []}
    # 包内分位归一（跨包可比）
    df["pct"] = df.groupby("pick_id")["score"].rank(pct=True)
    df["win"] = df["pnl_pct"] > 0
    df = df.sort_values("pct")
    # 十分桶胜率 + 单调化（累积最大，近似 isotonic）
    df["bucket"] = pd.cut(df["pct"], bins=10, labels=False, include_lowest=True)
    curve = []
    best = 0.0
    for b, g in df.groupby("bucket"):
        wr = float(g["win"].mean())
        best = max(best, wr)
        curve.append(((b + 0.5) / 10, best))
    return {"n": len(df), "curve": curve}


def calibrated_pwin(pct: float | None) -> float:
    """包内分位 → 校准胜率；无曲线/无分位回退 0.5 中性。"""
    if pct is None:
        return 0.5
    cal = score_winrate_calibration()
    if not cal["curve"]:
        return 0.5
    for mid, wr in cal["curve"]:
        if pct <= mid:
            return wr
    return cal["curve"][-1][1]


def conviction_multiplier(pwin: float) -> float:
    """校准胜率 → 仓位置信乘数：0.5→1.0，0.35→0.3，≥0.75→1.3（封顶）。"""
    return float(np.clip((pwin - 0.35) / 0.3, 0.3, 1.3))


# ---------------------------------------------------------------- 每日战报
def save_daily_report(date: str, content: str, data: dict, data_hash: str | None = None) -> None:
    """保存每日战报到 DB。同日覆盖。"""
    account = data.get("account", {})
    pnl_today = account.get("今日盈亏", 0) or 0
    pnl_total = account.get("持仓盈亏", 0) or 0
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO daily_reports
            (date, content, account_json, positions_json, fills_json,
             factors_json, strategies_json, market_json, stats_json, data_hash,
             pnl_today, pnl_total, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date,
            content,
            json.dumps(account, ensure_ascii=False, default=str),
            json.dumps(data.get("positions"), ensure_ascii=False, default=str) if hasattr(data.get("positions"), 'to_json') else json.dumps(data.get("positions"), ensure_ascii=False, default=str),
            json.dumps(data.get("fills"), ensure_ascii=False, default=str) if hasattr(data.get("fills"), 'to_json') else json.dumps(data.get("fills"), ensure_ascii=False, default=str),
            json.dumps(data.get("factors"), ensure_ascii=False, default=str) if hasattr(data.get("factors"), 'to_json') else json.dumps(data.get("factors"), ensure_ascii=False, default=str),
            json.dumps(data.get("strategies"), ensure_ascii=False, default=str) if hasattr(data.get("strategies"), 'to_json') else json.dumps(data.get("strategies"), ensure_ascii=False, default=str),
            json.dumps(data.get("indices"), ensure_ascii=False, default=str),
            json.dumps(data.get("stats"), ensure_ascii=False, default=str),
            data_hash,
            pnl_today,
            pnl_total,
            datetime.now().isoformat(),
        ))


def get_daily_report(date: str) -> dict | None:
    """读取指定日期的战报。"""
    with _conn() as c:
        row = c.execute(
            "SELECT date, content, account_json, positions_json, fills_json, factors_json, "
            "strategies_json, market_json, stats_json, data_hash, pnl_today, pnl_total, generated_at "
            "FROM daily_reports WHERE date=?", (date,)).fetchone()
    if not row:
        return None
    cols = ["date", "content", "account_json", "positions_json", "fills_json",
            "factors_json", "strategies_json", "market_json", "stats_json", "data_hash",
            "pnl_today", "pnl_total", "generated_at"]
    d = dict(zip(cols, row))
    for k in ["account_json", "positions_json", "fills_json", "factors_json",
              "strategies_json", "market_json", "stats_json"]:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


def list_daily_reports(limit: int = 30) -> pd.DataFrame:
    """列出最近 N 天的战报摘要。"""
    with _conn() as c:
        return pd.read_sql(
            "SELECT date, pnl_today, pnl_total, generated_at FROM daily_reports ORDER BY date DESC LIMIT ?",
            c, params=(limit,))


# ---------------------------------------------------------------- 进化信号（战报蒸馏）
def save_evolution_signal(date: str, report_date: str, signals: dict,
                          norm_scheme: str = "legacy") -> None:
    """蒸馏 job 落库一条进化信号（同日覆盖，幂等）。"""
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO evolution_signals (date, report_date, signals, norm_scheme, created_at)"
            " VALUES (?,?,?,?,?)",
            (date, report_date, json.dumps(signals, ensure_ascii=False), norm_scheme,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def get_latest_evolution_signal() -> dict | None:
    """取最新一条信号（新鲜度/有效期校验在调用方）。返回 {date, report_date, signals(dict),
    shadow_bias_json, norm_scheme} 或 None。任何异常返回 None（无信号不是错误）。"""
    try:
        with _conn() as c:
            r = c.execute(
                "SELECT date, report_date, signals, shadow_bias_json, norm_scheme"
                " FROM evolution_signals ORDER BY date DESC LIMIT 1").fetchone()
        if not r:
            return None
        return {"date": r[0], "report_date": r[1], "signals": json.loads(r[2] or "{}"),
                "shadow_bias_json": r[3], "norm_scheme": r[4]}
    except Exception:
        return None


def update_signal_shadow_bias(date: str, shadow_bias: dict) -> None:
    """引擎 shadow 挂钩回写偏置快照（仅当该日有信号行）。"""
    with _conn() as c:
        c.execute("UPDATE evolution_signals SET shadow_bias_json=? WHERE date=?",
                  (json.dumps(shadow_bias, ensure_ascii=False), date))


def list_evolution_signals(limit: int = 30) -> pd.DataFrame:
    """最近 N 条信号（shadow 评估脚本用）。"""
    with _conn() as c:
        return pd.read_sql(
            "SELECT date, report_date, signals, shadow_bias_json, norm_scheme, created_at"
            " FROM evolution_signals ORDER BY date DESC LIMIT ?",
            c, params=(limit,))


# ====================================================================
# 🎲 卫星轨 · 独立交易系统
# ====================================================================

_SATELLITE_POSITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS satellite_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL, name TEXT,
    buy_date TEXT NOT NULL, buy_price REAL, buy_shares INTEGER,
    buy_amount REAL,
    sell_date TEXT, sell_price REAL, sell_amount REAL,
    status TEXT DEFAULT 'pending',
    limit_price REAL,
    tp_price REAL, sl_price REAL,
    pnl REAL, pnl_pct REAL,
    hold_days INTEGER,
    llm_conviction REAL,
    llm_reason TEXT,
    created_at TEXT, closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sat_pos_status ON satellite_positions(status);
CREATE INDEX IF NOT EXISTS idx_sat_pos_date ON satellite_positions(buy_date);
"""

_SATELLITE_OUTCOMES_SCHEMA = """
CREATE TABLE IF NOT EXISTS satellite_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL, name TEXT,
    buy_date TEXT, buy_price REAL, buy_shares INTEGER,
    sell_date TEXT, sell_price REAL,
    pnl REAL, pnl_pct REAL,
    hold_days INTEGER,
    fwd_1d_ret REAL, fwd_5d_ret REAL,
    llm_conviction REAL,
    exit_reason TEXT,
    created_at TEXT
);
"""

_SATELLITE_NAV_SCHEMA = """
CREATE TABLE IF NOT EXISTS satellite_nav (
    date TEXT PRIMARY KEY,
    nav REAL,
    cash REAL,
    positions_value REAL,
    daily_pnl REAL,
    daily_return REAL,
    cumulative_return REAL,
    max_drawdown REAL
);
"""


def _sat_conn():
    """卫星轨专用连接（复用 experience.db）。"""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SATELLITE_POSITIONS_SCHEMA)
    conn.executescript(_SATELLITE_OUTCOMES_SCHEMA)
    conn.executescript(_SATELLITE_NAV_SCHEMA)
    return conn


def satellite_positions(status: str = None) -> pd.DataFrame:
    """读卫星轨仓位。status=None 返回全部。"""
    with _sat_conn() as c:
        if status:
            return pd.read_sql(
                "SELECT * FROM satellite_positions WHERE status=? ORDER BY id DESC",
                c, params=(status,))
        return pd.read_sql("SELECT * FROM satellite_positions ORDER BY id DESC", c)


def satellite_outcomes(limit: int = 100) -> pd.DataFrame:
    """读卫星轨战果。"""
    with _sat_conn() as c:
        return pd.read_sql(
            "SELECT * FROM satellite_outcomes ORDER BY id DESC LIMIT ?",
            c, params=(limit,))


def satellite_nav_history(limit: int = 60) -> pd.DataFrame:
    """读卫星轨净值曲线。"""
    with _sat_conn() as c:
        return pd.read_sql(
            "SELECT * FROM satellite_nav ORDER BY date DESC LIMIT ?",
            c, params=(limit,))


def satellite_open_from_picks(picks_df: pd.DataFrame, available_cash: float,
                              today: str) -> str:
    """卫星轨独立开仓：真实资金下单。

    picks_df: 含 code, name, score 列的 DataFrame（已按 score 降序）
    available_cash: 卫星轨可用资金（从 broker._get_satellite_cash() 获取）
    today: 交易日
    返回: 操作消息
    """
    import broker as bk

    if picks_df.empty:
        return "卫星轨：无候选票"
    if available_cash < 1000:
        return f"卫星轨：可用资金不足（{available_cash:.0f}元）"

    # 剔除涨停/追高票
    eligible = []
    for _, row in picks_df.iterrows():
        code = row["code"]
        try:
            pr = bk._latest_prices([code]).get(code)
            cur = pr[0] if pr else None
            if cur is None:
                continue
            # 涨停检查
            limit_up = pr[3] if pr and len(pr) > 3 else None
            if limit_up and cur >= limit_up * 0.999:
                continue
            # 追高检查（阈值从8%放宽至15%，与卫星轨事件增强逻辑一致）
            prev_close = pr[1] if pr and len(pr) > 1 else None
            chg = ((cur / prev_close - 1) * 100) if prev_close and prev_close > 0 else None
            if chg is not None and pd.notna(chg) and chg > 15.0:
                continue
        except Exception:
            continue
        eligible.append(row)

    if not eligible:
        return "卫星轨：全部候选被剔除（涨停/追高）"

    # Phase1 规则决策：取 Top3 等权
    top = eligible[:3]
    per_stock = available_cash / len(top)
    cash = available_cash  # 使用传入的卫星轨现金
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_order = 0

    with _sat_conn() as c:
        for row in top:
            code = row["code"]
            name = row.get("name", "")
            pr = bk._latest_prices([code]).get(code)
            cur = pr[0] if pr else None
            if cur is None or cur <= 0:
                continue
            shares = int(per_stock // cur // 100 * 100)
            if shares < 100:
                continue
            need = cur * shares + max(5.0, cur * shares * 0.00025)
            if need > cash:
                shares = int(cash * 0.95 // cur // 100 * 100)
                if shares < 100:
                    continue
                need = cur * shares + max(5.0, cur * shares * 0.00025)

            # 下单
            msg = bk.place_order(code, "buy", None, shares, source="satellite")
            if "已报" in msg or "已成" in msg:
                cost = cur * shares + max(5.0, cur * shares * 0.00025)
                cash -= cost
                # broker 已按 source='satellite' 扣减卫星现金；此处仅更新本地配额
                # 止损止盈价（EVENT_RULES）
                tp = round(cur * 1.12, 2)
                sl = round(cur * 0.95, 2)
                c.execute(
                    "INSERT INTO satellite_positions"
                    "(code, name, buy_date, buy_price, buy_shares, buy_amount,"
                    " status, limit_price, tp_price, sl_price, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (code, name, today, cur, shares, cur * shares,
                     "pending" if "已报" in msg else "open", cur, tp, sl, now))
                n_order += 1

    return f"卫星轨：委托 {n_order} 笔（合格候选 {len(eligible)} 只，可用资金 {available_cash:.0f}元）"


def satellite_llm_decide(candidates: list[dict], market_context: dict) -> dict:
    """LLM 决策：从候选票中选 0-5 只买入。

    candidates: [{"code": "SH600XXX", "name": "xxx", "price": 25.3, "change_pct": 3.2,
                  "score": 0.82, "factors": {...}}]
    market_context: {"regime": "sideways", "sentiment": 0.6, "sector_flow": ...}

    返回: {"decisions": [...], "raw": "...", "fallback": bool}
    """
    import broker as bk
    import llmutil

    # 硬约束前置：剔除涨停/追高（LLM 不可见这些票）
    eligible = []
    for c in candidates:
        code = c["code"]
        try:
            pr = bk._latest_prices([code]).get(code)
            cur = pr[0] if pr else None
            if cur is None:
                continue
            limit_up = pr[3] if pr and len(pr) > 3 else None
            if limit_up and cur >= limit_up * 0.999:
                continue
            prev_close = pr[1] if pr and len(pr) > 1 else None
            chg = ((cur / prev_close - 1) * 100) if prev_close and prev_close > 0 else None
            if chg is not None and pd.notna(chg) and chg > 15.0:
                continue
        except Exception:
            continue
        c["price"] = cur
        eligible.append(c)

    if not eligible:
        return {"decisions": [], "raw": "全部被剔除", "fallback": True}

    # 组装 prompt
    lines = []
    for i, c in enumerate(eligible, 1):
        line = (f"{i}. {c['code']} {c.get('name','')}\n"
                f"   现价 {c['price']:.2f} | 涨跌 {c.get('change_pct',0):+.2f}% | "
                f"综合评分 {c.get('score',0):.3f}")
        if c.get("factors"):
            fv = " | ".join(f"{k}={v:.3f}" for k, v in list(c["factors"].items())[:5])
            line += f"\n   因子值: {fv}"
        lines.append(line)

    stock_block = "\n".join(lines)
    regime = market_context.get("regime", "未知")
    sentiment = market_context.get("sentiment", 0.5)
    cash = market_context.get("cash", 0)
    hold_count = market_context.get("hold_count", 0)

    user_prompt = f"""当前市场环境：{regime}，大盘情绪 {sentiment:.2f}（0=恐慌 1=贪婪）
当前可用资金：{cash:,.0f}元 | 已持仓 {hold_count} 只

今日卫星轨候选票（已剔除涨停/追高）：
{stock_block}

卫星轨规则：止损-5%，止盈+12%，持有≤15天。

请决定买哪几只（从以上候选中选0-5只），每只的置信度(0-1)和仓位比例。
如果全部不值得买，输出空列表。

输出严格 JSON（不要其他文字）：
{{"decisions": [{{"code": "SH600XXX", "conviction": 0.8, "weight": 0.4, "reason": "一句话理由"}}],
  "market_view": "看多/中性/看空", "risk_note": "风险提示"}}"""

    system_prompt = (
        "你是卫星轨量化交易决策者。\n\n"
        "卫星轨特点：\n"
        "- 高风险高回报，止损-5%，止盈+12%，持有≤15天\n"
        "- 从候选票中选0-5只买入\n"
        "- 必须输出合法 JSON，不要任何额外文字\n\n"
        "输出格式：\n"
        '{"decisions": [{"code": "SH600XXX", "conviction": 0.8, "weight": 0.4, "reason": "一句话理由"}], '
        '"market_view": "看多/中性/看空", "risk_note": "风险提示"}\n\n'
        "约束：\n"
        "- 单只仓位上限30%\n"
        "- 总仓位不超过可用资金\n"
        "- 不买涨停/追高票（已剔除）"
    )

    # 卫星轨只需返回候选决策 JSON，限制输出避免无关解释
    reply = llmutil.llm_chat(system_prompt, user_prompt, max_tokens=700, label="satellite")

    # 解析 + 校验
    if not reply:
        return {"decisions": [], "raw": "LLM 无响应", "fallback": True}

    try:
        import json
        # 尝试提取 JSON 块
        m = re.search(r'\{[\s\S]*\}', reply)
        if not m:
            return {"decisions": [], "raw": reply, "fallback": True}
        data = json.loads(m.group())
    except Exception:
        return {"decisions": [], "raw": reply, "fallback": True}

    decisions = data.get("decisions", [])
    if not isinstance(decisions, list):
        return {"decisions": [], "raw": reply, "fallback": True}

    # 校验：每只票必须在候选列表中，权重归一化
    valid_codes = {c["code"] for c in eligible}
    cleaned = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        code = d.get("code", "")
        if code not in valid_codes:
            continue
        w = max(0.0, min(0.30, float(d.get("weight", 0.3))))  # 单只上限 30%
        conv = max(0.0, min(1.0, float(d.get("conviction", 0.5))))
        cleaned.append({
            "code": code,
            "conviction": conv,
            "weight": w,
            "reason": str(d.get("reason", ""))[:100]
        })

    # 归一化权重
    total_w = sum(d["weight"] for d in cleaned)
    if total_w > 0 and abs(total_w - 1.0) > 0.01:
        for d in cleaned:
            d["weight"] = d["weight"] / total_w

    return {
        "decisions": cleaned[:5],  # 最多 5 只
        "raw": reply,
        "market_view": data.get("market_view", ""),
        "risk_note": data.get("risk_note", ""),
        "fallback": False
    }


def satellite_open_from_llm(decisions: list[dict], available_cash: float,
                             today: str) -> str:
    """按 LLM 决策用真实资金下单。

    decisions: [{"code": "SH600XXX", "conviction": 0.8, "weight": 0.4, "reason": "..."}]
    available_cash: 卫星轨可用现金（从 broker._get_satellite_cash() 获取）
    """
    import broker as bk

    if not decisions:
        return "卫星轨 LLM：未选中任何票"
    if available_cash < 1000:
        return f"卫星轨 LLM：可用资金不足（{available_cash:.0f}元）"

    total_weight = sum(d["weight"] for d in decisions)
    if total_weight <= 0:
        return "卫星轨 LLM：总权重为0"

    cash = available_cash  # 使用传入的卫星轨现金
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_order = 0

    with _sat_conn() as c:
        for d in decisions:
            code = d["code"]
            pr = bk._latest_prices([code]).get(code)
            cur = pr[0] if pr else None
            if cur is None or cur <= 0:
                continue

            # 按权重分配资金
            alloc = available_cash * (d["weight"] / total_weight)
            shares = int(alloc // cur // 100 * 100)
            if shares < 100:
                continue
            need = cur * shares + max(5.0, cur * shares * 0.00025)
            if need > cash:
                shares = int(cash * 0.95 // cur // 100 * 100)
                if shares < 100:
                    continue
                need = cur * shares + max(5.0, cur * shares * 0.00025)

            msg = bk.place_order(code, "buy", None, shares, source="satellite")
            if "已报" in msg or "已成" in msg:
                cost = cur * shares + max(5.0, cur * shares * 0.00025)
                cash -= cost
                # broker 已按 source='satellite' 扣减卫星现金；此处仅更新本地配额
                tp = round(cur * 1.12, 2)
                sl = round(cur * 0.95, 2)
                c.execute(
                    "INSERT INTO satellite_positions"
                    "(code, name, buy_date, buy_price, buy_shares, buy_amount,"
                    " status, limit_price, tp_price, sl_price,"
                    " llm_conviction, llm_reason, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, "", today, cur, shares, cur * shares,
                     "pending" if "已报" in msg else "open", cur, tp, sl,
                     d.get("conviction", 0), d.get("reason", ""), now))
                n_order += 1

    return f"卫星轨 LLM：委托 {n_order} 笔（选中 {len(decisions)} 只，可用资金 {available_cash:.0f}元）"


def satellite_fill_check(today: str) -> str:
    """卫星轨盘中撮合：pending 限价单触及即成交。"""
    import broker as bk
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_hm = datetime.now().strftime("%H%M")

    with _sat_conn() as c:
        pend = pd.read_sql(
            "SELECT * FROM satellite_positions WHERE status='pending'", c)
    if pend.empty:
        return "卫星轨：无挂单"

    prices = bk._latest_prices(list(pend["code"]))
    n_fill = n_expire = 0
    with _sat_conn() as c:
        for _, p in pend.iterrows():
            if str(p["buy_date"]) < today or (str(p["buy_date"]) == today and now_hm >= "1500"):
                c.execute("UPDATE satellite_positions SET status='expired', closed_at=? WHERE id=?",
                          (now, int(p["id"])))
                n_expire += 1
                continue
            pr = prices.get(p["code"])
            cur = pr[0] if pr else None
            if cur is None or not p["limit_price"]:
                continue
            if cur <= p["limit_price"]:
                fill_price = min(cur, float(p["limit_price"]))
                c.execute(
                    "UPDATE satellite_positions SET status='open', buy_price=?, buy_amount=? WHERE id=?",
                    (fill_price, fill_price * int(p["buy_shares"]), int(p["id"])))
                n_fill += 1

    return f"卫星轨撮合：成交 {n_fill} 笔，过期 {n_expire} 笔" if (n_fill or n_expire) else "卫星轨：无变动"


def satellite_close_check(today: str) -> str:
    """卫星轨独立止损/止盈/到期（EVENT_RULES: -5%/+12%/15天）。"""
    import broker as bk

    with _sat_conn() as c:
        opens = pd.read_sql(
            "SELECT * FROM satellite_positions WHERE status='open'", c)
    if opens.empty:
        return "卫星轨：无持仓"

    prices = bk._latest_prices(list(opens["code"]))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_close = 0
    total_sell_amount = 0.0

    with _sat_conn() as c:
        for _, p in opens.iterrows():
            pr = prices.get(p["code"])
            cur = pr[0] if pr and pr[0] else None
            if cur is None:
                continue

            entry = p["buy_price"]
            tp = entry * 1.12   # EVENT_RULES
            sl = entry * 0.95
            hold = _count_trade_days(str(p["buy_date"])[:10], today)

            reason = None
            if cur <= sl:
                reason = "止损"
            elif cur >= tp:
                reason = "止盈"
            elif hold >= 15:
                reason = "到期"

            if reason:
                shares = int(p["buy_shares"])
                msg = bk.place_order(p["code"], "sell", None, shares, source="satellite")
                if "已报" in msg or "已成" in msg:
                    pnl = (cur - entry) * shares
                    pnl_pct = (cur / entry - 1) if entry else 0
                    c.execute(
                        "UPDATE satellite_positions SET status='closed', sell_date=?,"
                        " sell_price=?, sell_amount=?, pnl=?, pnl_pct=?, hold_days=?,"
                        " closed_at=? WHERE id=?",
                        (today, cur, cur * shares, pnl, pnl_pct, hold, now, int(p["id"])))
                    # 写战果
                    c.execute(
                        "INSERT INTO satellite_outcomes"
                        "(code, name, buy_date, buy_price, buy_shares, sell_date, sell_price,"
                        " pnl, pnl_pct, hold_days, exit_reason, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (p["code"], p["name"], str(p["buy_date"])[:10], entry, shares,
                         today, cur, pnl, pnl_pct, hold, reason, now))
                    n_close += 1
                    # 计算卖出金额（扣除手续费）
                    sell_fee = max(5.0, cur * shares * 0.00025)
                    sell_tax = cur * shares * 0.0005  # 印花税0.05%
                    sell_amount = cur * shares - sell_fee - sell_tax
                    total_sell_amount += sell_amount

    # 卖出回款已由 broker 按 source='satellite' 记入卫星现金池

    return f"卫星轨平仓：{n_close} 笔（止损/止盈/到期）" if n_close else "卫星轨：无触发"


_SATELLITE_INIT_CASH = 20000.0  # 卫星轨初始资金（用于收益率计算基准）


def satellite_nav_update(today: str) -> str:
    """更新卫星轨净值：持仓市值 + 卫星轨专属现金（从 broker 独立现金池读取）。"""
    import broker as bk

    with _sat_conn() as c:
        opens = pd.read_sql(
            "SELECT code, buy_shares, buy_price, buy_amount"
            " FROM satellite_positions WHERE status='open'", c)

    positions_value = 0.0
    if not opens.empty:
        prices = bk._latest_prices(list(opens["code"]))
        for _, p in opens.iterrows():
            pr = prices.get(p["code"])
            cur = pr[0] if pr and pr[0] else p["buy_price"]
            shares = int(p["buy_shares"])
            positions_value += cur * shares

    # 从 broker 独立现金池读取卫星轨现金
    cash = bk._get_satellite_cash()
    nav = cash + positions_value

    with _sat_conn() as c:
        prev = c.execute(
            "SELECT nav, cumulative_return, max_drawdown FROM satellite_nav"
            " ORDER BY date DESC LIMIT 1").fetchone()

    daily_pnl = 0.0
    daily_ret = 0.0
    cum_ret = 0.0
    mdd = 0.0
    if prev:
        daily_pnl = nav - prev[0]
        daily_ret = daily_pnl / prev[0] if prev[0] else 0
        cum_ret = nav / _SATELLITE_INIT_CASH - 1
        mdd = min(prev[2] or 0, nav / max(prev[0], 1) - 1)
    else:
        cum_ret = nav / _SATELLITE_INIT_CASH - 1

    with _sat_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO satellite_nav"
            "(date, nav, cash, positions_value, daily_pnl, daily_return,"
            " cumulative_return, max_drawdown) VALUES (?,?,?,?,?,?,?,?)",
            (today, nav, cash, positions_value, daily_pnl, daily_ret, cum_ret, mdd))

    return f"卫星轨净值：{nav:.0f}元（现金{cash:.0f}+持仓{positions_value:.0f}）"


def satellite_leaderboard() -> pd.DataFrame:
    """卫星轨战绩统计：按持有天数分组的胜率/收益。"""
    with _sat_conn() as c:
        df = pd.read_sql("SELECT * FROM satellite_outcomes ORDER BY id DESC", c)
    if df.empty:
        return pd.DataFrame()
    # 按 hold_days 分组
    stats = []
    for days, grp in df.groupby("hold_days"):
        stats.append({
            "持有天数": int(days),
            "交易笔数": len(grp),
            "平均收益": f"{grp['pnl_pct'].mean():+.2%}",
            "胜率": f"{(grp['pnl_pct'] > 0).mean():.0%}",
            "平均盈亏": f"{grp['pnl'].mean():+,.0f}元",
            "总盈亏": f"{grp['pnl'].sum():+,.0f}元",
        })
    return pd.DataFrame(stats)


def _count_trade_days(d1: str, d2: str) -> int:
    """计算两个日期间的交易日数（近似：自然日 × 5/7）。"""
    try:
        dt1 = pd.Timestamp(d1)
        dt2 = pd.Timestamp(d2)
        cal_days = (dt2 - dt1).days
        return max(1, int(cal_days * 5 / 7))
    except Exception:
        return 1


# ---------------------------------------------------------------- OOS-实盘偏差追踪
def track_oos_vs_live(window_days: int = 90) -> dict:
    """追踪回测 OOS 胜率 vs 实盘胜率的偏差。

    当偏差持续扩大时，说明回测模型失效或过拟合加重。
    返回: {avg_oos_winrate, avg_live_winrate, bias, status, n_samples, details}
    """
    with _conn() as c:
        # 最近 window_days 天的选股记录及其保存时的 OOS 胜率
        rows = c.execute(f"""
            SELECT p.id, p.pack_name, p.oos_winrate_at_save, p.trade_date,
                   o.fwd_days, o.hit
            FROM picks p
            JOIN outcomes o ON o.pick_id = p.id
            WHERE p.trade_date >= date('now', '-{window_days} days')
              AND p.oos_winrate_at_save IS NOT NULL
              AND o.fwd_days = 5
            ORDER BY p.trade_date
        """).fetchall()

    if not rows:
        return {"status": "数据不足", "avg_oos_winrate": None,
                "avg_live_winrate": None, "bias": None, "n_samples": 0, "details": []}

    df = pd.DataFrame(rows, columns=["pick_id", "pack_name", "oos_wr", "trade_date",
                                      "fwd_days", "hit"])

    # 按策略包分组统计
    pack_stats = []
    for pack, grp in df.groupby("pack_name"):
        oos_wr = float(grp["oos_wr"].mean()) if grp["oos_wr"].notna().any() else None
        live_wr = float(grp["hit"].mean()) if len(grp) > 0 else None
        if oos_wr is not None and live_wr is not None:
            pack_stats.append({
                "pack_name": pack,
                "oos_winrate": round(oos_wr, 3),
                "live_winrate": round(live_wr, 3),
                "bias": round(oos_wr - live_wr, 3),
                "n_trades": len(grp),
            })

    if not pack_stats:
        return {"status": "数据不足", "avg_oos_winrate": None,
                "avg_live_winrate": None, "bias": None, "n_samples": 0, "details": []}

    ps_df = pd.DataFrame(pack_stats)
    avg_oos = float(ps_df["oos_winrate"].mean())
    avg_live = float(ps_df["live_winrate"].mean())
    bias = avg_oos - avg_live

    if bias < 0.03:
        status = "正常"
    elif bias < 0.08:
        status = "注意"
    else:
        status = "⚠️ 过拟合加剧"

    return {
        "avg_oos_winrate": round(avg_oos, 3),
        "avg_live_winrate": round(avg_live, 3),
        "bias": round(bias, 3),
        "status": status,
        "n_samples": len(df),
        "n_packs": len(pack_stats),
        "details": pack_stats,
    }
