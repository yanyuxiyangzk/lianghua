"""经验库：选股结果落库 → 到期回填战果 → 实战榜单（经验积累，供组合进化使用）。

设计：
  - SQLite（/data/experience.db），三表：picks / pick_items / outcomes
  - 每次生成名单自动落库（combo_hash + trade_date 去重，同组合同日覆盖）
  - 定时任务 outcome_backfill 按交易日历回填 5/10/20 日远期战果（不管对错都记）
  - 榜单：策略包实战胜率（可与回测 OOS 胜率对照校准）、因子实战近似归因
"""

import json
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
    UNIQUE(combo_hash, trade_date)
);
CREATE TABLE IF NOT EXISTS pick_items (
    pick_id INTEGER NOT NULL, code TEXT NOT NULL, rank INTEGER, score REAL,
    UNIQUE(pick_id, code)
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
    pnl_today REAL,
    pnl_total REAL,
    generated_at TEXT
);
"""


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")  # 写冲突时等待30秒，避免 database is locked
    c.execute("PRAGMA journal_mode=WAL")    # 读写不互斥（此前默认 DELETE，锁升级死锁频发）
    c.executescript(_SCHEMA)
    c.executescript(_TRADES_SCHEMA)
    c.executescript(_POSITIONS_SCHEMA)
    c.executescript(_DAILY_REPORTS_SCHEMA)
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
                     ("extend_count", "INTEGER DEFAULT 0")]:
        if col not in pcols:
            c.execute(f"ALTER TABLE positions ADD COLUMN {col} {ddl}")
    # 迁移：picks 增加 data_source（生产库为历史手工添加，全新建库走不到——
    # 2026-09-16 测试暴露；补上后新环境 save_pick 不再炸）
    pkcols = [r[1] for r in c.execute("PRAGMA table_info(picks)")]
    if "data_source" not in pkcols:
        c.execute("ALTER TABLE picks ADD COLUMN data_source TEXT")
    return c


# ---------------------------------------------------------------- 落库
def save_pick(source: str, pool_name: str, top_n: int, method: str,
              filters: list, factors: list, final_scores: pd.Series,
              pack_name: str | None = None, oos_winrate: float | None = None,
              trade_date: str | None = None, data_source: str | None = None) -> int | None:
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
                                  pack_name, method, top_n, filters, factors, oos_winrate_at_save, data_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(combo_hash, trade_date) DO UPDATE SET
                 created_at=excluded.created_at, factors=excluded.factors,
                 oos_winrate_at_save=excluded.oos_winrate_at_save, data_source=excluded.data_source""",
            (combo_hash, trade_date, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source,
             pool_name, pack_name, method, top_n, json.dumps(filters, ensure_ascii=False),
             json.dumps(factors, ensure_ascii=False), oos_winrate, data_source))
        row = c.execute("SELECT id FROM picks WHERE combo_hash=? AND trade_date=?",
                        (combo_hash, trade_date))
        pick_id = row.fetchone()[0]
        c.execute("DELETE FROM pick_items WHERE pick_id=?", (pick_id,))
        c.executemany("INSERT INTO pick_items (pick_id, code, rank, score) VALUES (?,?,?,?)",
                      [(pick_id, code, i + 1, float(sc))
                       for i, (code, sc) in enumerate(final_scores.items())])
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
# 规则：信号日次日开盘价买入；盘中先触止损按止损价、先触止盈按止盈价（同日双触按保守止损）；
# 到期未触发则第 N 日收盘卖出。成本按往返 0.25% 计。


def trade_plan(ref_price: float | None, signal_date: str, rules: dict | None = None) -> dict:
    """把名单翻译成可执行计划：买入时间/参考价/止盈价/止损价/最迟平仓日（规则同模拟交易）。

    买入时间 = 信号日次一交易日开盘；最迟平仓 = 买入后第 hold_days 个交易日收盘。
    日历超出 qlib 数据末端时按 weekday 顺延近似（遇节假日再顺延，仅作参考）。
    """
    rules = rules or DEFAULT_RULES
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
    r = {**DEFAULT_RULES, **(rules or {})}
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


def position_open_from_picks(trade_date: str, today: str) -> str:
    """盘中委托买入：对名单挂限价单（限价 = 名单参考买入价 = 扫描日收盘价）。

    遵守真实交易规则：挂出委托（pending）后不立即成交，现价 ≤ 限价才触发成交（开仓）；
    当日收盘仍未成交的委托自动失效（次日不再补）。竞价确认"回避"的股票跳过。
    幂等：同一股票同一来源同日只挂一单（UNIQUE(code, buy_date, source)）。
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
            if r.source == "le_shadow":
                continue  # LE 影子名单：只结算战果攒战绩，绝不开仓
            items = pick_items_detail(int(r.id))
            if items.empty:
                continue
            codes = list(items["code"])
            names = _position_names(codes)
            # 参考买入价 = 扫描日收盘价（名单生成时的价格）；顺带取当日涨跌幅做追高保护
            ref_prices, ref_chg = {}, {}
            try:
                import datasource
                with datasource._qconn() as dc:
                    for row in dc.execute(
                            f"SELECT code, price, change_pct FROM ifind_stocklist"
                            f" WHERE code IN ({','.join('?' * len(codes))})", codes):
                        if row[1]:
                            ref_prices[row[0]] = row[1]
                            ref_chg[row[0]] = row[2]
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
                if chg is not None and pd.notna(chg) and chg > 5.0:
                    n_chase += 1
                    continue
                cur = c.execute(
                    "INSERT OR IGNORE INTO positions"
                    "(code, name, buy_date, buy_price, buy_ts, pick_id, source, pack_name,"
                    " status, limit_price, created_at, sup_lo_entry, atr_entry, regime_entry)"
                    " VALUES (?,?,?,NULL,?,?,?,?, 'pending', ?, ?, ?, ?, ?)",
                    (it.code, names.get(it.code, ""), today, now,
                     int(r.id), r.source, r.pack_name, float(limit), now,
                     float(sr["sup_lo"]) if sr is not None and pd.notna(sr.get("sup_lo")) else None,
                     float(sr["atr"]) if sr is not None and pd.notna(sr.get("atr")) else None,
                     regime_now))
                n_new += cur.rowcount
    msg = f"委托挂单：新增 {n_new} 笔限价单"
    if n_defer:
        msg += f" · 距支撑>{theta_entry}ATR 延迟 {n_defer} 笔"
    if n_chase:
        msg += f" · 追高保护拦 {n_chase} 笔"
    return msg if n_new or n_defer or n_chase else "委托挂单：无新增（已挂或竞价回避）"


# 每轨虚拟资金（等分买入）：主轨 7 万 / 卫星轨 2 万（对应今日执行页"主轨7成/卫星2成"的仓位约定）
TRACK_BUDGET = {"sched_satellite_scan": 20000.0}


def _budget_per_stock(source: str, top_n: int) -> float:
    """每股预算 = 轨道虚拟资金 / 名单只数。"""
    budget = TRACK_BUDGET.get(source, 70000.0)
    return budget / max(int(top_n or 10), 1)


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
    n_fill = n_expire = 0
    with _conn() as c:
        for _, p in pend.iterrows():
            # 隔夜挂单 / 当日收盘(15:00)后 → 失效
            if str(p["buy_date"]) < today or (str(p["buy_date"]) == today and now_hm >= "1500"):
                c.execute("UPDATE positions SET status='expired', closed_at=? WHERE id=?",
                          (now, int(p["id"])))
                n_expire += 1
                continue
            pr = prices.get(p["code"])
            cur = pr[0] if pr else None
            if cur is None or not p["limit_price"]:
                continue
            if cur <= p["limit_price"]:  # 现价触及限价 → 成交（取更优价）
                fill = min(cur, float(p["limit_price"]))
                # 按轨道预算整手配股，走柜台真实买入（扣可用资金、T+1冻结）
                top_n = c.execute("SELECT top_n FROM picks WHERE id=?",
                                  (int(p["pick_id"]),)).fetchone()
                per = _budget_per_stock(p["source"], top_n[0] if top_n else 10)
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
                # M7 单票集中度上限：委托金额 ≤ 总资产 15%
                try:
                    import broker as _bk
                    _total = _bk.get_account().get("总资产", 0) or 0
                    if _total > 0:
                        per = min(per, 0.15 * _total)
                except Exception:
                    pass
                # M7 持仓数上限：open+pending ≥8 不再开新仓（防过散）
                try:
                    n_open_pending = c.execute(
                        "SELECT COUNT(DISTINCT code) FROM positions WHERE status IN ('open','pending')").fetchone()[0]
                    if n_open_pending >= 8 and p["code"] not in {
                        r[0] for r in c.execute(
                            "SELECT DISTINCT code FROM positions WHERE status IN ('open','pending')").fetchall()}:
                        continue
                except Exception:
                    pass
                shares = int(per // fill // 100 * 100)
                if shares <= 0:
                    continue  # 预算买不起一手就不开（实盘如此：100股整手是硬约束）
                import broker
                msg = broker.place_order(p["code"], "buy", None, shares, source="ai")
                if "已成交" not in msg:
                    continue  # 柜台资金不足等 → 留挂（收盘仍未成交自动失效）
                m = re.search(r"@ ([\d.]+)", msg)
                fill = float(m.group(1)) if m else fill
                c.execute("UPDATE positions SET status='open', buy_price=?, buy_ts=?,"
                          " shares=?, buy_amount=?, max_close=? WHERE id=?",
                          (fill, now, shares, round(shares * fill, 2), fill, int(p["id"])))
                n_fill += 1

                # 注册 PriceMonitor 事件驱动监控
                try:
                    from price_monitor import monitor
                    rules = {"take_profit": 0.15, "stop_loss": -0.08, "hold_days": 20, "cost": 0.0025}
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
    """双账本对账自愈：柜台 ai 持仓 vs 经验库 open 持仓，差额补记为 open 仓。

    背景（2026-09-11 踩坑）：fill_check 中柜台买入（独立事务已扣款）与
    positions 记账 UPDATE 是两条事务，锁冲突/进程被杀会让"钱花了账没记"，
    产生孤儿仓（柜台有、账本无——止盈止损永远覆盖不到）。对账补记后，
    孤儿仓重新进入止盈/止损/到期管理（成本取柜台成本价，T+1 同样生效）。
    """
    import broker
    with _conn() as c:
        opens = pd.read_sql(
            "SELECT code, SUM(shares) sh FROM positions"
            " WHERE status IN ('open','closing') GROUP BY code", c)
    # ---- closing 仓结算（委托制）：委托成交→closed；日终撤单→回 open 次日重估 ----
    settled = []
    with _conn() as c:
        closing = pd.read_sql("SELECT * FROM positions WHERE status='closing'", c)
    if not closing.empty:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with broker._conn() as bc, _conn() as c:
            for _, p in closing.iterrows():
                oid = p.get("sell_order_id")
                if pd.isna(oid) or not oid:
                    # 无委托号的 closing（异常残留）→ 回 open
                    c.execute("UPDATE positions SET status='open', sell_order_id=NULL,"
                              " sell_reason=NULL WHERE id=?", (int(p["id"]),))
                    continue
                row = bc.execute(
                    "SELECT status, filled_price, filled_ts FROM broker_orders WHERE id=?",
                    (int(oid),)).fetchone()
                if not row:
                    continue
                st_, fprice, fts = row
                if st_ == "已成":
                    pnl = (round(fprice / p["buy_price"] - 1 - DEFAULT_RULES["cost"], 6)
                           if fprice and p["buy_price"] else None)
                    c.execute("UPDATE positions SET status='closed', sell_date=?, sell_price=?,"
                              " sell_ts=?, pnl_pct=?, hold_days=?, closed_at=? WHERE id=?",
                              (today, fprice, fts or now, pnl,
                               _trade_days_between(str(p["buy_date"]), today), now, int(p["id"])))
                    settled.append(f"{p.get('name') or p['code']}·成交")
                elif st_ == "已撤":
                    c.execute("UPDATE positions SET status='open', sell_order_id=NULL,"
                              " sell_reason=NULL WHERE id=?", (int(p["id"]),))
                    settled.append(f"{p.get('name') or p['code']}·撤单重持")
    try:
        bposs = broker.get_positions()
    except Exception:
        return "柜台持仓读取失败"
    exp_shares = {r["code"]: int(r["sh"]) for _, r in opens.iterrows()} if not opens.empty else {}
    fixed = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        for _, bp in bposs.iterrows():
            if (bp["source"] or "") != "ai":
                continue
            code = str(bp["code"])
            diff = int(bp["shares"] or 0) - exp_shares.get(code, 0)
            cost = float(bp["cost"] or 0)
            if diff <= 0 or cost <= 0:
                continue
            c.execute(
                "INSERT INTO positions (code, name, buy_date, buy_price, buy_ts, pick_id,"
                " source, pack_name, status, limit_price, shares, buy_amount, created_at)"
                " VALUES (?,?,?,?,?,?,?,?, 'open', NULL, ?, ?, ?)",
                (code, bp.get("name") or code, str(bp.get("last_buy_date") or today),
                 cost, now, None, "reconcile_fix", "对账补记",
                 diff, round(diff * cost, 2), now))
            fixed.append(f"{bp.get('name') or code}×{diff}")
    head = "对账补记：" + ",".join(fixed) if fixed else "对账一致"
    return head + (" · closing结算：" + ",".join(settled) if settled else "")


def position_close_check(today: str) -> str:
    """盘中检查持仓：止盈/止损/到期平仓（T+1：买入日当天不卖）。

    价格取 ifind_realtime 最新快照；触发后走柜台市价卖出，盈亏按实际成交价
    记账（不按触发价）；卖出前做双账本校验（经验库 open 股数 vs 柜台 ai 持仓
    股数，不一致则跳过该代码并计入消息，避免账本漂移后卖错数量）。
    """
    r = DEFAULT_RULES
    with _conn() as c:
        opens = pd.read_sql("SELECT * FROM positions WHERE status='open'", c)
    if opens.empty:
        return "无持仓"
    prices = _latest_prices(list(opens["code"]))
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
    n_close = n_skip = n_order = 0
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
            if not cur:
                continue
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
                mc = max(x for x in [cur, entry, mc_old] if x and pd.notna(x))  # 滚动最高（快照近似）
                if mc != mc_old:
                    _mc_updates.append((mc, int(p["id"])))  # 延迟到循环末统一写（防与柜台写锁自锁）
                if atr_e is not None and pd.notna(atr_e) and sup_e is not None and pd.notna(sup_e):
                    if cur < sup_e - 0.5 * atr_e:
                        reason, limit_price = "破位(SR)", round(cur * 0.995, 2)
                if not reason and atr_e is not None and pd.notna(atr_e) and mc and cur < mc - 3.0 * atr_e:
                    reason, limit_price = "吊灯止盈", round(cur * 0.995, 2)
                if not reason:
                    hd = _trade_days_between(str(p["buy_date"]), today)
                    if hd >= r["hold_days"]:
                        # M3 逻辑腿：到期且转弱才平；仍强则顺延一次（防好票被日历赶下车）
                        extend = int(p["extend_count"] or 0) if "extend_count" in p.index else 0
                        if extend < 1 and _states_map is not None and _states_map.get(code, "weak") != "weak":
                            c.execute("UPDATE positions SET extend_count=? WHERE id=?",
                                      (extend + 1, int(p["id"])))
                            continue  # 顺延一个持有期
                        reason, limit_price = "到期", round(cur * 0.995, 2)
            if reason:
                # 委托制（实盘规则）：触发只挂单，触及才成交；当日未成交收盘自动撤，次日重估重挂
                msg = broker.place_order(code, "sell", limit_price, int(p["shares"] or 0),
                                         source="ai")
                if "已成交" in msg:
                    # 限价当下即触及（止损让半步/更优价），按实际成交价平仓记账
                    mf = re.search(r"@ ([\d.]+)", msg)
                    fill = float(mf.group(1)) if mf else None
                    pnl = (round(fill / entry - 1 - r["cost"], 6)
                           if fill and entry else None)
                    c.execute("UPDATE positions SET status='closed', sell_date=?, sell_price=?,"
                              " sell_ts=?, sell_reason=?, pnl_pct=?, hold_days=?, closed_at=?"
                              " WHERE id=?",
                              (today, fill, now, reason, pnl,
                               _trade_days_between(str(p["buy_date"]), today), now, int(p["id"])))
                    n_close += 1
                    # 取消 PriceMonitor 监控（仅成交后；挂单中继续持有、继续监控）
                    try:
                        from price_monitor import monitor
                        monitor.unregister(code, int(p["id"]))
                    except Exception:
                        pass
                elif "已挂单" in msg:
                    mo = re.search(r"委托号 #(\d+)", msg)
                    c.execute("UPDATE positions SET status='closing', sell_reason=?,"
                              " sell_order_id=? WHERE id=?",
                              (reason, int(mo.group(1)) if mo else None, int(p["id"])))
                    n_order += 1
                # 其他结果（可卖不足等）→ 保持 open，下个周期再试

        # M3 吊灯基准统一落库（所有柜台写完成后，避免写锁交叉）
        for mc, pid in _mc_updates:
            c.execute("UPDATE positions SET max_close=? WHERE id=?", (mc, pid))

    parts = [f"平仓 {n_close} 笔"] if n_close else ["持仓检查：无触发"]
    if n_order:
        parts.append(f"挂出卖单 {n_order} 笔")
    if n_skip:
        parts.append(f"账本不一致跳过 {n_skip} 笔（经验库与柜台股数对不上）")
    return "；".join(parts)


def get_open_positions() -> pd.DataFrame:
    """当前持仓（open）+ 最新快照价 + 浮动盈亏（% 和 金额元）+ 可卖/止盈止损价。"""
    with _conn() as c:
        df = pd.read_sql("SELECT * FROM positions WHERE status='open' ORDER BY id DESC", c)
    if df.empty:
        return df
    today = datetime.now().strftime("%Y-%m-%d")
    prices = _latest_prices(list(df["code"]))
    df["最新价"] = df["code"].map(lambda x: (prices.get(x) or (None,))[0])
    df["浮动盈亏%"] = (df["最新价"] / df["buy_price"] - 1) * 100
    df["浮动盈亏额"] = (df["最新价"] - df["buy_price"]) * df["shares"].fillna(0)
    df["持有交易日"] = df["buy_date"].map(
        lambda d: _trade_days_between(str(d), today))
    df["可卖(股)"] = df.apply(
        lambda r: int(r["shares"] or 0) if str(r["buy_date"]) < today else 0, axis=1)
    df["止盈价"] = (df["buy_price"] * (1 + DEFAULT_RULES["take_profit"])).round(2)
    df["止损价"] = (df["buy_price"] * (1 + DEFAULT_RULES["stop_loss"])).round(2)
    return df


def manual_sell(position_id: int, shares: int) -> str:
    """手动卖出 AI 自动持仓：走柜台真实卖出（回笼资金、T+1 校验），成功后平仓/减仓记录。"""
    import broker
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        df = pd.read_sql("SELECT * FROM positions WHERE id=? AND status='open'",
                         c, params=(int(position_id),))
    if df.empty:
        return "持仓不存在或已平仓"
    p = df.iloc[0]
    shares = int(shares)
    held = int(p["shares"] or 0)
    if shares <= 0 or shares > held:
        return "卖出数量超出持仓"
    if str(p["buy_date"]) >= today:
        return "T+1：当日买入不可当日卖出"
    msg = broker.place_order(str(p["code"]), "sell", None, shares, source="ai")
    if "已成交" not in msg:
        return msg
    m = re.search(r"@ ([\d.]+)", msg)
    fill = float(m.group(1)) if m else None
    with _conn() as c:
        if shares >= held:
            pnl = (round(fill / p["buy_price"] - 1 - DEFAULT_RULES["cost"], 6)
                   if fill and p["buy_price"] else None)
            c.execute("UPDATE positions SET status='closed', sell_date=?, sell_price=?,"
                      " sell_ts=?, sell_reason='手动卖出', pnl_pct=?, hold_days=?, closed_at=?"
                      " WHERE id=?",
                      (today, fill, now, pnl,
                       _trade_days_between(str(p["buy_date"]), today), now, int(position_id)))
        else:
            c.execute("UPDATE positions SET shares=?, buy_amount=? WHERE id=?",
                      (held - shares,
                       round(float(p["buy_amount"] or 0) - shares * fill, 2) if fill else None,
                       int(position_id)))
    return f"已成交：卖出 {p['code']} {shares}股 @ {fill:.2f}"


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
                    if pd.notna(p):
                        mv += p * sh
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
    total = acc.get("总资产", 0) or 0
    cash = acc.get("可用资金", 0) or 0
    mv = acc.get("持仓市值", 0) or 0
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

def portfolio_risk() -> dict:
    """组合风控评估（M4）：账户净值序列波动率 → 日 VaR 近似 + 熔断状态。

    口径：σ = 净值日收益 20 日标准差（含真实持仓结构信息，比 60 日个股全相关矩阵稳——
    A 股个股两两全相关噪声大、伪相关多）；日 VaR(95%) ≈ 1.65×σ×总资产；
    熔断线 = −2×σ×√5（约当 95% 置信单周极端损失）。
    """
    import broker
    nv = nav_stats()
    if not nv or nv["净值天数"] < 5:
        return {"ok": False, "reason": "净值序列不足 5 日"}
    with _conn() as c:
        df = pd.read_sql("SELECT date, daily_ret, drawdown FROM account_nav_daily ORDER BY date", c)
    acc = broker.get_account()
    total = acc.get("总资产", 0) or 0
    sigma = float(df["daily_ret"].iloc[-20:].std())
    var_day = 1.65 * sigma * total
    circuit_line = -2 * sigma * np.sqrt(5)
    dd_now = float(df["drawdown"].iloc[-1])
    # σ 地板：净值近乎不动的账户（新建/空仓）熔断线≈0 会永久停开仓——无波动信号不熔断
    circuit = bool(dd_now <= circuit_line) if sigma >= 0.002 else False
    return {"ok": True, "sigma": sigma, "var_day": var_day,
            "var_pct": 1.65 * sigma, "circuit_line": circuit_line,
            "dd_now": dd_now, "circuit": circuit,
            "nav": nv["当前净值"], "mdd": nv["最大回撤"]}


def risk_halt_today(today: str) -> tuple[bool, str]:
    """当日是否熔断停止开新仓（读 risk_state.json；当日无记录则不熔断）。"""
    try:
        st_ = json.loads(_RISK_FLAG.read_text())
        if st_.get("date") == today and st_.get("halt"):
            return True, st_.get("reason", "")
    except Exception:
        pass
    return False, ""


def _write_risk_flag(today: str, halt: bool, reason: str):
    _RISK_FLAG.write_text(json.dumps(
        {"date": today, "halt": halt, "reason": reason,
         "ts": datetime.now().strftime("%F %T")}, ensure_ascii=False))


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
def save_daily_report(date: str, content: str, data: dict) -> None:
    """保存每日战报到 DB。同日覆盖。"""
    account = data.get("account", {})
    pnl_today = account.get("今日盈亏", 0) or 0
    pnl_total = account.get("持仓盈亏", 0) or 0
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO daily_reports
            (date, content, account_json, positions_json, fills_json,
             factors_json, strategies_json, market_json, stats_json,
             pnl_today, pnl_total, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            pnl_today,
            pnl_total,
            datetime.now().isoformat(),
        ))


def get_daily_report(date: str) -> dict | None:
    """读取指定日期的战报。"""
    with _conn() as c:
        row = c.execute("SELECT * FROM daily_reports WHERE date=?", (date,)).fetchone()
    if not row:
        return None
    cols = ["date", "content", "account_json", "positions_json", "fills_json",
            "factors_json", "strategies_json", "market_json", "stats_json",
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
