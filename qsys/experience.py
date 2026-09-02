"""经验库：选股结果落库 → 到期回填战果 → 实战榜单（经验积累，供组合进化使用）。

设计：
  - SQLite（/data/experience.db），三表：picks / pick_items / outcomes
  - 每次生成名单自动落库（combo_hash + trade_date 去重，同组合同日覆盖）
  - 定时任务 outcome_backfill 按交易日历回填 5/10/20 日远期战果（不管对错都记）
  - 榜单：策略包实战胜率（可与回测 OOS 胜率对照校准）、因子实战近似归因
"""

import json
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


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.executescript(_SCHEMA)
    c.executescript(_TRADES_SCHEMA)
    # 迁移：picks 增加数据来源字段（老库无此列则补上）
    cols = [r[1] for r in c.execute("PRAGMA table_info(picks)")]
    if "data_source" not in cols:
        c.execute("ALTER TABLE picks ADD COLUMN data_source TEXT DEFAULT 'qlib_local'")
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
    combo_key = json.dumps({"s": source, "p": pool_name, "n": top_n, "m": method,
                            "f": filters, "fac": factors, "pk": pack_name, "ds": data_source},
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
DEFAULT_RULES = {"take_profit": 0.15, "stop_loss": -0.08, "hold_days": 20, "cost": 0.0025}
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
