"""板块市场：行业分类 + 板块资金流向。

数据通道：
  - 行业分类：新浪行业体系（84 板块）→ stock_industry 表（每周可重同步）
  - 板块级行情：akshare stock_sector_spot("行业") → sector_flow_snapshots 表（时序）
  - 资金净流入代理：quote_snapshots 的 外盘-内盘 按板块聚合（统计口径=已采集宇宙）

口径说明（页面也会标注）：真实"主力资金流向"需 L2 逐笔，免费通道不可得；
本模块用 成交额变化 + 内外盘净额 作为资金进出的代理指标。
"""

import threading
import time
from datetime import datetime

import numpy as np
import pandas as pd

from common import get_instruments
from datasource import _qconn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_industry (
    code TEXT PRIMARY KEY, sector_label TEXT, sector_name TEXT,
    source TEXT DEFAULT 'sina', updated_at TEXT);
CREATE TABLE IF NOT EXISTS sector_flow_snapshots (
    ts TEXT NOT NULL, sector_label TEXT NOT NULL, sector_name TEXT,
    members INTEGER, avg_chg_pct REAL, total_volume REAL, total_amount REAL,
    leader_code TEXT, leader_name TEXT, leader_chg_pct REAL,
    PRIMARY KEY (ts, sector_label));
CREATE TABLE IF NOT EXISTS sector_daily (
    date TEXT NOT NULL, sector_name TEXT NOT NULL,
    avg_chg_pct REAL, total_amount REAL, up_count INTEGER, down_count INTEGER, members INTEGER,
    flow_net REAL,
    PRIMARY KEY (date, sector_name));
CREATE TABLE IF NOT EXISTS sector_inflow_snapshots (
    ts TEXT NOT NULL, sector_name TEXT NOT NULL, net_amt REAL, cover INTEGER,
    PRIMARY KEY (ts, sector_name));
"""


def _sconn():
    c = _qconn()
    c.executescript(_SCHEMA)
    cols = [r[1] for r in c.execute("PRAGMA table_info(sector_daily)")]
    if "flow_net" not in cols:
        c.execute("ALTER TABLE sector_daily ADD COLUMN flow_net REAL")
    return c


# ---------------------------------------------------------------- 行业分类
def industry_map() -> pd.DataFrame:
    with _sconn() as c:
        return pd.read_sql("SELECT * FROM stock_industry", c)


def industry_status() -> dict:
    with _sconn() as c:
        n = c.execute("SELECT COUNT(*) FROM stock_industry").fetchone()[0]
        s = c.execute("SELECT COUNT(DISTINCT sector_label), MAX(updated_at) FROM stock_industry").fetchone()
    return {"stocks": n, "sectors": (s[0] or 0), "updated_at": (s[1] or "—")}


_sync_state = {"running": False, "done": 0, "total": 0, "err": None}


def sync_industry_map(background: bool = True):
    """从新浪同步行业分类（84 板块成分 → stock_industry 表）。幂等。"""
    if _sync_state["running"]:
        return

    def _run():
        import akshare as ak

        _sync_state.update(running=True, done=0, total=0, err=None)
        try:
            spot = ak.stock_sector_spot(indicator="行业")
            if spot is None or spot.empty:
                raise RuntimeError("现货接口返回空（可能限流，稍后重试）")
            labels = spot["label"].tolist()
            names = dict(zip(spot["label"], spot["板块"]))
            _sync_state["total"] = len(labels)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with _sconn() as c:
                for i, label in enumerate(labels):
                    try:
                        detail = ak.stock_sector_detail(sector=label)
                        if detail is not None and not detail.empty:
                            c.executemany(
                                "INSERT OR REPLACE INTO stock_industry (code, sector_label, sector_name, source, updated_at)"
                                " VALUES (?,?,?,'sina',?)",
                                [(str(r.symbol).upper(), label, names.get(label, ""), now)
                                 for r in detail.itertuples()])
                    except Exception:
                        pass
                    _sync_state["done"] = i + 1
                    time.sleep(0.2)
        except Exception as e:
            _sync_state["err"] = str(e)[:120]
        finally:
            _sync_state["running"] = False

    if background:
        threading.Thread(target=_run, daemon=True, name="industry-sync").start()
    else:
        _run()


def sync_progress() -> dict:
    return dict(_sync_state)


# ---------------------------------------------------------------- 板块级行情（新浪）
def fetch_sector_spot() -> pd.DataFrame:
    import akshare as ak

    df = ak.stock_sector_spot(indicator="行业")
    return df.rename(columns={"label": "sector_label", "板块": "sector_name", "公司家数": "members",
                              "涨跌幅": "avg_chg_pct", "总成交量": "total_volume", "总成交额": "total_amount",
                              "股票代码": "leader_code", "股票名称": "leader_name", "个股-涨跌幅": "leader_chg_pct"})


def save_sector_spot(df: pd.DataFrame, ts: str | None = None) -> int:
    ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    keep = ["sector_label", "sector_name", "members", "avg_chg_pct", "total_volume", "total_amount",
            "leader_code", "leader_name", "leader_chg_pct"]
    with _sconn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO sector_flow_snapshots (ts, sector_label, sector_name, members,"
            " avg_chg_pct, total_volume, total_amount, leader_code, leader_name, leader_chg_pct)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(ts, *[getattr(r, k) for k in keep]) for r in df[keep].itertuples()])
    return len(df)


def sector_amount_baseline(days: int = 5) -> dict:
    """各板块近 N 天日均成交额（环比基准）。"""
    cutoff = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    with _sconn() as c:
        rows = c.execute(
            "SELECT sector_label, AVG(total_amount) FROM sector_flow_snapshots"
            " WHERE ts < date('now') AND ts > ? GROUP BY sector_label", (cutoff,)).fetchall()
    return {k: v for k, v in rows if v}


def sector_trend(sector_label: str, date: str | None = None) -> pd.DataFrame:
    date = date or datetime.now().strftime("%Y-%m-%d")
    with _sconn() as c:
        return pd.read_sql(
            "SELECT ts, avg_chg_pct, total_amount FROM sector_flow_snapshots"
            " WHERE sector_label=? AND ts LIKE ? ORDER BY ts",
            c, params=(sector_label, f"{date}%"))


# ---------------------------------------------------------------- 板块日线（轮动走势的历史基础）
_backfill_state = {"running": False, "done": 0, "total": 0, "err": None}


def sector_daily_status() -> dict:
    with _sconn() as c:
        r = c.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM sector_daily").fetchone()
    return {"rows": r[0], "min_date": r[1], "max_date": r[2]}


def backfill_sector_daily(days: int = 260, background: bool = True):
    """用 stock_industry 映射 × 股票日线回填板块日线（跟随全局数据源，qlib 兜底；等权涨跌幅/成交额/涨跌家数）。"""
    if _backfill_state["running"]:
        return

    def _run():
        import datasource as ds

        _backfill_state.update(running=True, done=0, total=1, err=None)
        try:
            imap = industry_map()
            if imap.empty:
                raise RuntimeError("行业分类为空，先同步行业分类")
            codes = imap["code"].tolist()
            code2sector = dict(zip(imap["code"], imap["sector_name"]))
            src = ds.get_source()
            if src == "qlib_local":
                cal = ds.QLIB_DATA_DIR / "calendars" / "day.txt"
                end = cal.read_text().splitlines()[-1].strip()
            else:
                # 在线源（easytdx/ths_ifind/akshare）数据到今天，不套 qlib 日历的截止日期
                end = datetime.now().strftime("%Y-%m-%d")
            start = (pd.Timestamp(end) - pd.Timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d")
            # 跟随全局数据源；在线源首次逐股回填较慢（读穿缓存，逐日增量后很快）
            used_qlib = src == "qlib_local"
            panel = ds._qlib_daily(codes, ["$close", "$amount"], start=start, end=end) \
                if used_qlib else ds.get_panel(codes, start, end, ["$close", "$amount"], source=src)
            if panel.empty and not used_qlib:
                # 在线源不可用时回退本地库，保证页面有数据
                panel = ds._qlib_daily(codes, ["$close", "$amount"], start=start, end=end)
                used_qlib = True
            if panel.empty:
                raise RuntimeError("qlib 取数为空")
            close = panel["$close"].unstack("instrument").sort_index().tail(days + 1)
            amount = panel["$amount"].unstack("instrument").sort_index().tail(days + 1)
            chg = close.pct_change(fill_method=None) * 100
            sector_series = pd.Series(code2sector)
            # 单位对齐：qlib $amount 为千元，market.db 各在线源为元
            amount_scale = 1000 if used_qlib else 1
            rows = []
            dates = chg.index[1:]
            for i, dt in enumerate(dates):
                c_row = chg.iloc[i + 1]
                a_row = amount.iloc[i + 1] * amount_scale
                tmp = pd.DataFrame({"chg": c_row, "amt": a_row})
                tmp["sector"] = sector_series.reindex(tmp.index)
                # 量价净流：个股涨记 +成交额、跌记 -成交额（日线级资金流代理，可回填全历史）
                tmp["flow"] = np.sign(tmp["chg"]) * tmp["amt"]
                g = tmp.dropna(subset=["sector"]).groupby("sector")
                d = str(dt)[:10]
                for sec, grp in g:
                    rows.append((d, sec, float(grp["chg"].mean()) if grp["chg"].notna().any() else None,
                                 float(grp["amt"].sum()), int((grp["chg"] > 0).sum()),
                                 int((grp["chg"] < 0).sum()), int(grp["chg"].notna().sum()),
                                 float(grp["flow"].sum())))
                _backfill_state["done"] = i + 1
                _backfill_state["total"] = len(dates)
            with _sconn() as c:
                c.executemany(
                    "INSERT OR REPLACE INTO sector_daily (date, sector_name, avg_chg_pct, total_amount,"
                    " up_count, down_count, members, flow_net) VALUES (?,?,?,?,?,?,?,?)", rows)
        except Exception as e:
            _backfill_state["err"] = str(e)[:150]
        finally:
            _backfill_state["running"] = False

    if background:
        threading.Thread(target=_run, daemon=True, name="sector-daily-backfill").start()
    else:
        _run()


def backfill_progress() -> dict:
    return dict(_backfill_state)


def sector_daily_range(days: int = 20, end: str | None = None) -> pd.DataFrame:
    """近 N 个交易日的板块日线（轮动图数据）。"""
    with _sconn() as c:
        if end is None:
            end = c.execute("SELECT MAX(date) FROM sector_daily").fetchone()[0]
        if not end:
            return pd.DataFrame()
        dates = [r[0] for r in c.execute(
            "SELECT DISTINCT date FROM sector_daily WHERE date<=? ORDER BY date DESC LIMIT ?",
            (end, days)).fetchall()]
        if not dates:
            return pd.DataFrame()
        marks = ",".join("?" * len(dates))
        return pd.read_sql(
            f"SELECT * FROM sector_daily WHERE date IN ({marks}) ORDER BY date",
            c, params=dates)


def save_sector_inflow_snapshot() -> int:
    """把宇宙快照的内外盘净额按板块聚合入库（板块净主动金额时序）。"""
    agg = sector_net_inflow()
    if agg.empty:
        return 0
    ts = agg.attrs.get("ts") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _sconn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO sector_inflow_snapshots (ts, sector_name, net_amt, cover)"
            " VALUES (?,?,?,?)",
            [(ts, r.sector, float(r.净主动金额亿), int(r.覆盖家数)) for r in agg.itertuples()])
    return len(agg)


def sector_inflow_range(days: int = 5) -> pd.DataFrame:
    cutoff = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    with _sconn() as c:
        return pd.read_sql(
            "SELECT * FROM sector_inflow_snapshots WHERE ts>=? ORDER BY ts", c, params=(cutoff,))
def sector_net_inflow(universe: list[str] | None = None) -> pd.DataFrame:
    """外盘-内盘 金额按板块聚合（净流入代理）。universe 默认沪深300+中证500。"""
    import datasource as ds

    if universe is None:
        universe = get_instruments("csi300") + get_instruments("csi500")
    rows, max_ts = ds.get_latest_snapshots(universe)
    if not rows:
        return pd.DataFrame()
    snap = pd.DataFrame(rows)
    snap["net_amt"] = (snap["outer_vol"].fillna(0) - snap["inner_vol"].fillna(0)) * snap["price"].fillna(0) * 100
    imap = industry_map()
    if imap.empty:
        return pd.DataFrame()
    code2sector = dict(zip(imap["code"], imap["sector_name"]))
    snap["sector"] = snap["code"].map(code2sector)
    agg = (snap.dropna(subset=["sector"]).groupby("sector")
           .agg(净主动金额亿=("net_amt", lambda s: s.sum() / 1e8),
                覆盖家数=("code", "count"))
           .reset_index())
    agg.attrs["ts"] = max_ts
    return agg
