"""数据源层：统一行情访问、来源标识、全局切换。

设计：
  - SOURCES 注册表：qlib_local（本地社区库）/ akshare（东财日线·前复权）
  - 全局当前源存 /data/settings.json，分析层各页面统一读取
  - akshare 数据"读穿缓存"进 market.db（market_daily 表带 source 字段），
    data_sources 表登记各源状态 —— 每条数据都有出处
  - 边界：RD-Agent 进化/回测固定用 qlib_local，本层只服务 QSYS 分析/展示
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.environ.get("QSYS_DATA_DIR", "/data"))
QLIB_DATA_DIR = Path(os.environ.get("QLIB_DATA_DIR", "/data/qlib/cn_data"))
MKT_DB = DATA_DIR / "market.db"
SETTINGS_FILE = DATA_DIR / "settings.json"

SOURCES = {
    "qlib_local": {"name": "Qlib 本地库（社区日线）", "minute": False, "note": "回测同源"},
    "akshare": {"name": "akshare·新浪日线（前复权）", "minute": True, "note": "网页接口，首次抓取较慢"},
    "easytdx": {"name": "easy-tdx·通达信（日线/分钟/逐笔竞价）", "minute": True,
                "note": "TDX TCP 行情，含集合竞价逐笔标记"},
}

_QLIB_READY = False


# ---------------------------------------------------------------- 设置
def get_source() -> str:
    try:
        return json.loads(SETTINGS_FILE.read_text()).get("data_source", "qlib_local")
    except Exception:
        return "qlib_local"


def set_source(source: str):
    if source not in SOURCES:
        raise ValueError(f"未知数据源: {source}")
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if SETTINGS_FILE.exists():
        try:
            cfg = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            cfg = {}
    cfg["data_source"] = source
    SETTINGS_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- market.db（来源标识）
def _conn():
    c = sqlite3.connect(MKT_DB)
    c.execute("PRAGMA journal_mode=WAL")  # 并发读写更稳（后台采集线程 + 页面读取）
    c.executescript("""
    CREATE TABLE IF NOT EXISTS market_daily(
        source TEXT NOT NULL, code TEXT NOT NULL, date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
        fetched_at TEXT, PRIMARY KEY(source, code, date));
    CREATE TABLE IF NOT EXISTS data_sources(
        source TEXT PRIMARY KEY, name TEXT, last_sync TEXT, rows INTEGER, note TEXT);
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(market_daily)")]
    if "outstanding_share" not in cols:
        c.execute("ALTER TABLE market_daily ADD COLUMN outstanding_share REAL")
    return c


def _touch_source(source: str):
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM market_daily WHERE source=?", (source,)).fetchone()[0]
        c.execute("INSERT OR REPLACE INTO data_sources (source, name, last_sync, rows, note)"
                  " VALUES (?,?,?,?,?)",
                  (source, SOURCES[source]["name"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   n, SOURCES[source]["note"]))


def source_status() -> list[dict]:
    out = []
    with _conn() as c:
        rows = {r[0]: r for r in c.execute("SELECT source, last_sync, rows FROM data_sources")}
    for key, meta in SOURCES.items():
        r = rows.get(key)
        out.append({"source": key, "name": meta["name"],
                    "last_sync": r[1] if r else "—", "rows": (r[2] if r else 0), "note": meta["note"]})
    return out


# ---------------------------------------------------------------- qlib 通道
def _ensure_qlib():
    global _QLIB_READY
    if not _QLIB_READY:
        import qlib

        # expression_cache/dataset_cache 置 None：禁用 qlib 磁盘缓存
        # （多进程共享 /root/.qlib 缓存会产生竞态，偶发返回空数据——已实测复现）
        qlib.init(provider_uri=str(QLIB_DATA_DIR), region="cn",
                  expression_cache=None, dataset_cache=None)
        _QLIB_READY = True


def _qlib_daily(codes: list[str], fields: list[str], start: str, end: str) -> pd.DataFrame:
    _ensure_qlib()
    from qlib.data import D

    df = D.features(codes, fields, start_time=start, end_time=end)
    return df if df is not None else pd.DataFrame()


# ---------------------------------------------------------------- akshare 通道（读穿缓存）
def _to_ak_symbol(code: str) -> str:
    """SH600519 → sh600519（新浪通道用小写交易所前缀）。"""
    m = re.match(r"^([A-Za-z]{2})(\d{6})$", code)
    return (m.group(1).lower() + m.group(2)) if m else code.lower()


def _ak_normalize(df: pd.DataFrame) -> pd.DataFrame:
    # 东财接口为中文列，新浪接口为英文列，统一兼容
    m = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
         "成交量": "volume", "成交额": "amount"}
    df = df.rename(columns=m)
    df["date"] = df["date"].astype(str)
    keep = ["date", "open", "high", "low", "close", "volume", "amount"]
    if "outstanding_share" in df.columns:
        keep.append("outstanding_share")
    return df[keep]


def _ak_fetch_daily(code: str, start: str, end: str) -> int:
    """抓日线（前复权）写入 market.db，返回写入行数。

    主通道：新浪（东财接口在本机网络被重置，已实测）。"""
    import akshare as ak

    sym = _to_ak_symbol(code)
    df = ak.stock_zh_a_daily(symbol=sym, start_date=start.replace("-", ""),
                             end_date=end.replace("-", ""), adjust="qfq")
    if df is None or df.empty:
        return 0
    df = _ak_normalize(df)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        rows_vals = [(code, r.date, r.open, r.high, r.low, r.close, r.volume, r.amount,
                      getattr(r, "outstanding_share", None), now) for r in df.itertuples()]
        c.executemany(
            "INSERT OR REPLACE INTO market_daily (source, code, date, open, high, low, close, volume, amount, outstanding_share, fetched_at)"
            " VALUES ('akshare',?,?,?,?,?,?,?,?,?,?)",  # 1 literal + 10 占位 = 11 列
            rows_vals)
    return len(df)


def _ak_daily_cached(code: str, start: str, end: str) -> pd.DataFrame:
    """读穿缓存：库内覆盖不足则补抓（仅补缺段）。"""
    with _conn() as c:
        have = c.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM market_daily"
                         " WHERE source='akshare' AND code=?", (code,)).fetchone()
    need_fetch = True
    if have and have[2] > 0 and have[0] <= start and have[1] >= end:
        need_fetch = False
    elif have and have[2] > 0 and have[0] <= end and have[1] >= start:
        # 部分覆盖：补抓缺口（简化：直接重抓全段，INSERT OR REPLACE 幂等）
        need_fetch = True
    if need_fetch:
        try:
            _ak_fetch_daily(code, start, end)
            time.sleep(0.15)  # 礼貌限速
            _touch_source("akshare")
        except Exception as e:
            import logging
            logging.getLogger("datasource").warning(f"akshare 抓取失败 {code}: {e}")
    with _conn() as c:
        df = pd.read_sql("SELECT date, open, high, low, close, volume, amount FROM market_daily"
                         " WHERE source='akshare' AND code=? AND date BETWEEN ? AND ? ORDER BY date",
                         c, params=(code, start, end))
    return df


# ---------------------------------------------------------------- easy-tdx（通达信 TCP）通道
_TDX = {"client": None}
# 实测数据质量+速度双优的服务器（2026-08 验证；from_best_host 会选到返回空数据的坏节点，故钉死）
_TDX_HOSTS = ["180.153.18.170", "115.238.56.198", "115.238.90.165", "218.75.126.9",
              "175.178.128.227", "124.223.163.242"]


def _tdx_client():
    """TDX 连接单例：按优选列表逐个建连并做数据质量校验（防空响应服务器）。"""
    if _TDX["client"] is not None:
        return _TDX["client"]
    from easy_tdx import KlineCategory, Market, TdxClient

    for host in _TDX_HOSTS:
        try:
            c = TdxClient(host=host, port=7709, timeout=6)
            df = c.get_security_bars(Market.SH, "600519", KlineCategory.DAY, 0, 2)
            if df is not None and not df.empty and float(df["close"].iloc[-1]) > 100:
                _TDX["client"] = c
                return c
        except Exception:
            continue
    # 全部不可用时退回自动选路
    _TDX["client"] = TdxClient.from_best_host()
    return _TDX["client"]


def _tdx_market(code: str):
    from easy_tdx import Market

    prefix = code[:2].upper()
    return {"SH": Market.SH, "SZ": Market.SZ, "BJ": Market.BJ}.get(prefix, Market.SH), code[2:]


def _tdx_fetch_daily(code: str, start: str, end: str) -> int:
    """easy-tdx 日线分页抓取 → market.db（source='easytdx'，单位：股/元）。"""
    from easy_tdx import KlineCategory

    market, sym = _tdx_market(code)
    frames = []
    start_idx = 0
    while True:
        df = _tdx_client().get_security_bars(market, sym, KlineCategory.DAY, start_idx, 800)
        if df is None or df.empty:
            break
        frames.append(df)
        if len(df) < 800:
            break
        start_idx += 800
        if len(frames) > 40:  # 安全上限
            break
    if not frames:
        return 0
    big = pd.concat(frames).drop_duplicates(subset=["date"]).sort_values("date")
    big["date"] = pd.to_datetime(big["date"]).dt.strftime("%Y-%m-%d")
    big = big[(big["date"] >= start) & (big["date"] <= end)]
    if big.empty:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO market_daily (source, code, date, open, high, low, close, volume, amount, fetched_at)"
            " VALUES ('easytdx',?,?,?,?,?,?,?,?,?)",
            [(code, r.date, r.open, r.high, r.low, r.close, r.vol, r.amount, now)
             for r in big.itertuples()])
    return len(big)


def _cached_daily(code: str, start: str, end: str, source: str) -> pd.DataFrame:
    """通用读穿缓存（akshare/easytdx 共用）。"""
    fetcher = {"akshare": _ak_fetch_daily, "easytdx": _tdx_fetch_daily}[source]
    with _conn() as c:
        have = c.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM market_daily"
                         " WHERE source=? AND code=?", (source, code)).fetchone()
    if not (have and have[2] > 0 and have[0] <= start and have[1] >= end):
        try:
            fetcher(code, start, end)
            time.sleep(0.1)
            _touch_source(source)
        except Exception as e:
            import logging
            logging.getLogger("datasource").warning(f"{source} 抓取失败 {code}: {e}")
    with _conn() as c:
        return pd.read_sql(
            "SELECT date, open, high, low, close, volume, amount FROM market_daily"
            " WHERE source=? AND code=? AND date BETWEEN ? AND ? ORDER BY date",
            c, params=(source, code, start, end))


# ---------------------------------------------------------------- 统一接口
def get_daily(code: str, start: str, end: str, source: str | None = None) -> pd.DataFrame:
    """单票日线 → ($open..$amount, datetime 索引)，与 qlib 版式一致。"""
    source = source or get_source()
    if source == "qlib_local":
        df = _qlib_daily([code], ["$open", "$high", "$low", "$close", "$volume", "$amount"], start=start, end=end)
        if df.empty:
            return df
        return df.droplevel("instrument").sort_index()
    df = _cached_daily(code, start, end, source)
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df.index.name = "datetime"
    return df.rename(columns={c: f"${c}" for c in ["open", "high", "low", "close", "volume", "amount"]})


def get_panel(codes: list[str], start: str, end: str, fields: list[str],
              source: str | None = None, progress=None) -> pd.DataFrame:
    """多票面板 → (instrument, datetime) MultiIndex，列 $open...（与 qlib 版式一致）。"""
    source = source or get_source()
    if source == "qlib_local":
        df = _qlib_daily(codes, fields, start=start, end=end)
        return df.sort_index() if not df.empty else df

    frames = []
    for i, code in enumerate(codes):
        if progress:
            progress(f"{source} 抓取 {i + 1}/{len(codes)} {code}")
        d = _cached_daily(code, start, end, source)
        if d.empty:
            continue
        d["instrument"] = code
        d["date"] = pd.to_datetime(d["date"])
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    big = pd.concat(frames)
    big = big.rename(columns={c: f"${c}" for c in ["open", "high", "low", "close", "volume", "amount"]})
    keep = [f for f in fields if f in big.columns]
    panel = big.set_index(["instrument", "date"])[keep].sort_index()
    # 与 qlib 通道保持同名索引层（下游统一按 datetime 取层）
    panel.index = panel.index.set_names(["instrument", "datetime"])
    return panel


# ---------------------------------------------------------------- 腾讯展示通道（分时/竞价）
def _to_tx_symbol(code: str) -> str:
    """SH600519 → sh600519（腾讯行情通道）。"""
    return _to_ak_symbol(code)


def get_minute_today(code: str) -> dict:
    """腾讯当日分钟线 + 快照。返回 {"date", "prev_close", "minutes": DataFrame, "name"}。

    minutes 列: time(str 'HHMM'), price, volume(手), cum_amount(元)。
    腾讯语义：volume / cum_amount 均为**当日累计**值（每分钟递增）。
    首行 09:30 = 集合竞价成交（price=竞价, volume=竞价量, cum_amount=竞价金额）。
    """
    import requests

    sym = _to_tx_symbol(code)
    r = requests.get(f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}",
                     timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    d = r.json()["data"][sym]
    rows = d["data"]["data"]
    date = d["data"]["date"]
    qt = d.get("qt", {}).get(sym, [])
    name = qt[1] if len(qt) > 1 else code
    prev_close = float(qt[4]) if len(qt) > 4 and qt[4] else None
    m = pd.DataFrame([str(r).split()[:4] for r in rows], columns=["time", "price", "volume", "cum_amount"])
    for c in ["price", "volume", "cum_amount"]:
        m[c] = pd.to_numeric(m[c], errors="coerce")
    m["time"] = m["time"].astype(str).str.zfill(4)
    m = m.dropna()
    # 竞价/盘前时段腾讯会返回 0930 占位行（volume=0），剔除避免误判为竞价成交
    m = m[(m["volume"] > 0) | (m["time"] > "0930")]
    if m.empty:
        return {"date": date, "name": name, "prev_close": prev_close, "minutes": m}
    # 累计量 → 每分量（首行即竞价量，保持原值）
    m["minute_vol"] = m["volume"].diff().fillna(m["volume"])
    return {"date": date, "name": name, "prev_close": prev_close, "minutes": m}


def get_latest_outstanding(code: str) -> float | None:
    """最近一次 akshare 抓取缓存的流通股本（股）。"""
    with _conn() as c:
        r = c.execute("SELECT outstanding_share FROM market_daily WHERE source='akshare'"
                      " AND code=? AND outstanding_share IS NOT NULL ORDER BY date DESC LIMIT 1",
                      (code,)).fetchone()
    return float(r[0]) if r and r[0] else None


# ---------------------------------------------------------------- easy-tdx 分钟线/逐笔（竞价）
def get_minute_tdx(code: str, count: int = 240) -> pd.DataFrame:
    """当日 1 分钟线（通达信），返回 datetime/price/vol(股)/amount 帧。"""
    from easy_tdx import KlineCategory

    market, sym = _tdx_market(code)
    df = _tdx_client().get_security_bars(market, sym, KlineCategory.MIN_1, 0, count)
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("datetime").reset_index(drop=True)


def get_ticks_tdx(code: str, max_pages: int = 20) -> pd.DataFrame:
    """当日逐笔成交（含集合竞价标记 buyorsell=8）。分页回溯到 09:25 竞价为止。"""
    market, sym = _tdx_market(code)
    frames = []
    for page in range(max_pages):
        df = _tdx_client().get_transaction_data(market, sym, page * 800, 800)
        if df is None or df.empty:
            break
        frames.append(df)
        earliest = df["datetime"].astype(str).min()
        if earliest <= "09:30" or len(df) < 800:
            break
    if not frames:
        return pd.DataFrame()
    big = pd.concat(frames).drop_duplicates().sort_values("datetime").reset_index(drop=True)
    return big


# ---------------------------------------------------------------- 行情快照（腾讯批量）与本地持久化
_QUOTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS quote_snapshots (
    ts TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    price REAL, prev_close REAL, open REAL, high REAL, low REAL,
    bid1 REAL, ask1 REAL, volume REAL, amount REAL,
    bid_vol_sum REAL, ask_vol_sum REAL, last_tick_vol REAL,
    turnover REAL, limit_up REAL, limit_down REAL, avg_price REAL,
    outer_vol REAL, inner_vol REAL, quantity_ratio REAL,
    trade_time TEXT, source TEXT DEFAULT 'tencent',
    PRIMARY KEY (ts, code));
CREATE INDEX IF NOT EXISTS idx_quote_code_ts ON quote_snapshots(code, ts);
"""


def _qconn():
    c = _conn()
    c.executescript(_QUOTE_SCHEMA)
    return c


def _parse_tx_line(line: str) -> dict | None:
    """解析腾讯快照单行 v_sh600519="1~贵州茅台~..."。字段索引按公开映射，越界防御。"""
    m = re.match(r'v_([a-z]{2}\d{6})="(.*)"', line.strip().rstrip(";"))
    if not m:
        return None
    sym, body = m.group(1), m.group(2)
    p = body.split("~")

    def _f(i):
        try:
            return float(p[i]) if p[i] not in ("", None) else None
        except (IndexError, ValueError):
            return None

    def _vols(start):  # 五档量求和：价/量成对，量在后一位
        total = 0.0
        for i in range(start, start + 10, 2):
            v = _f(i)
            if v:
                total += v
        return total

    code = sym.upper()
    return {
        "code": code,
        "name": p[1] if len(p) > 1 else code,
        "price": _f(3), "prev_close": _f(4), "open": _f(5),
        "volume": _f(6),                          # 总手
        "outer_vol": _f(7), "inner_vol": _f(8),
        "bid1": _f(9), "ask1": _f(19),
        "bid_vol_sum": _vols(10), "ask_vol_sum": _vols(20),
        "last_tick_vol": _f(29),                  # 现手（盘中有效，盘后常为空）
        "trade_time": p[30] if len(p) > 30 else "",
        "high": _f(33), "low": _f(34),
        "amount": (_f(37) or 0) * 1e4,            # 成交额（万→元）
        "turnover": _f(38),
        "limit_up": _f(47), "limit_down": _f(48),
        "quantity_ratio": _f(49),                 # 量比（部分档位提供）
        "avg_price": _f(51),
    }


def get_batch_snapshots(codes: list[str], chunk: int = 50) -> list[dict]:
    """腾讯批量快照：qt.gtimg.cn/q=code1,code2,...（每批约50只）。"""
    import requests

    out = []
    for i in range(0, len(codes), chunk):
        syms = [_to_tx_symbol(c) for c in codes[i:i + chunk]]
        try:
            r = requests.get("https://qt.gtimg.cn/q=" + ",".join(syms), timeout=12,
                             headers={"User-Agent": "Mozilla/5.0"})
            for line in r.text.strip().split(";"):
                row = _parse_tx_line(line)
                if row:
                    out.append(row)
        except Exception:
            continue
        time.sleep(0.2)
    return out


def save_snapshots(rows: list[dict], ts: str | None = None) -> int:
    """快照批次落库（quote_snapshots，保留3天）。"""
    if not rows:
        return 0
    ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _qconn() as c:
        vals = [(ts, r["code"], r["name"], r["price"], r["prev_close"], r["open"], r["high"], r["low"],
                 r["bid1"], r["ask1"], r["volume"], r["amount"], r["bid_vol_sum"], r["ask_vol_sum"],
                 r["last_tick_vol"], r["turnover"], r["limit_up"], r["limit_down"], r["avg_price"],
                 r["outer_vol"], r["inner_vol"], r["quantity_ratio"], r["trade_time"]) for r in rows]
        assert all(len(v) == 23 for v in vals), "字段数不匹配"
        c.executemany(
            "INSERT OR REPLACE INTO quote_snapshots (ts, code, name, price, prev_close, open, high, low,"
            " bid1, ask1, volume, amount, bid_vol_sum, ask_vol_sum, last_tick_vol, turnover,"
            " limit_up, limit_down, avg_price, outer_vol, inner_vol, quantity_ratio, trade_time, source)"
            " VALUES (" + ",".join(["?"] * 23) + ",'tencent')", vals)
        # 保留最近3天（每批一次廉价清理）
        cutoff = (datetime.now() - pd.Timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("DELETE FROM quote_snapshots WHERE ts < ?", (cutoff,))
    return len(rows)


def get_speed_1min(codes: list[str], ref_ts: str) -> dict:
    """1分钟涨速基准价：每只股票在 ref_ts 45秒**之前**的最新一条快照价。

    （原实现取 45~90 秒窗口，按 10 秒采集节奏永远匹配不到 → 恒为空；
    改为"45 秒前最近一条"，任意采集节奏都能正确取到约 1 分钟前的基准。）
    """
    out = {}
    if not codes:
        return out
    cutoff = (pd.Timestamp(ref_ts) - pd.Timedelta(seconds=45)).strftime("%Y-%m-%d %H:%M:%S")
    with _qconn() as c:
        marks = ",".join("?" * len(codes))
        rows = c.execute(
            f"SELECT code, MAX(ts) FROM quote_snapshots WHERE code IN ({marks}) AND ts <= ?"
            f" GROUP BY code", (*codes, cutoff)).fetchall()
        for code, mts in rows:
            v = c.execute("SELECT price FROM quote_snapshots WHERE code=? AND ts=?",
                          (code, mts)).fetchone()
            if v and v[0]:
                out[code] = v[0]
    return out


def get_prev_snapshot_volumes(codes: list[str], ref_ts: str) -> dict:
    """最近一条早于 ref_ts 的快照总手（用于估算现手=两次总手差）。"""
    out = {}
    with _qconn() as c:
        marks = ",".join("?" * len(codes))
        rows = c.execute(
            f"SELECT code, MAX(ts) FROM quote_snapshots WHERE code IN ({marks}) AND ts < ?"
            f" GROUP BY code", (*codes, ref_ts)).fetchall()
        for code, mts in rows:
            v = c.execute("SELECT volume FROM quote_snapshots WHERE code=? AND ts=?",
                          (code, mts)).fetchone()
            if v and v[0] is not None:
                out[code] = v[0]
    return out


def get_latest_snapshots(codes: list[str]) -> tuple[list[dict], str | None]:
    """每只股票最新一条快照（读库，供页面无网络渲染）。返回 (rows, 最新采集时间)。"""
    if not codes:
        return [], None
    with _qconn() as c:
        marks = ",".join("?" * len(codes))
        latest = c.execute(
            f"SELECT code, MAX(ts) AS mts FROM quote_snapshots WHERE code IN ({marks})"
            f" GROUP BY code", codes).fetchall()
        if not latest:
            return [], None
        ts_map = {code: mts for code, mts in latest}
        rows = []
        cols = ["ts", "code", "name", "price", "prev_close", "open", "high", "low",
                "bid1", "ask1", "volume", "amount", "bid_vol_sum", "ask_vol_sum",
                "last_tick_vol", "turnover", "limit_up", "limit_down", "avg_price",
                "outer_vol", "inner_vol", "quantity_ratio", "trade_time"]
        for code, mts in ts_map.items():
            r = c.execute(f"SELECT {','.join(cols)} FROM quote_snapshots WHERE code=? AND ts=?",
                          (code, mts)).fetchone()
            if r:
                rows.append(dict(zip(cols, r)))
    max_ts = max(ts_map.values()) if ts_map else None
    return rows, max_ts


def get_realtime_snapshot(code: str) -> dict:
    """腾讯实时快照（qt.gtimg.cn，约 3 秒级更新）。

    竞价时段（09:15-09:25）：price/volume 字段为**虚拟匹配价/匹配量**。
    返回: {name, price, prev_close, open, volume(手), amount(万), high, low,
           turnover, limit_up, limit_down, avg_price, time}
    """
    import requests

    sym = _to_tx_symbol(code)
    r = requests.get(f"https://qt.gtimg.cn/q={sym}", timeout=8,
                     headers={"User-Agent": "Mozilla/5.0"})
    parts = r.text.split("~")

    def _f(i):
        try:
            return float(parts[i]) if parts[i] not in ("", None) else None
        except (IndexError, ValueError):
            return None

    return {
        "name": parts[1] if len(parts) > 1 else code,
        "price": _f(3), "prev_close": _f(4), "open": _f(5),
        "volume": _f(6),                      # 手（竞价时段=匹配量）
        "amount_wan": _f(37),                 # 成交额（万）
        "time": parts[30] if len(parts) > 30 else "",
        "high": _f(33), "low": _f(34),
        "turnover": _f(38),
        "limit_up": _f(47), "limit_down": _f(48),
        "avg_price": _f(51),
    }
