"""涨停事件与打板情绪加工（盘后）。

limit_up_events：逐票逐日涨停质量档案——触板/封板/一字板、首次封板时间、开板
次数、封单金额（快照五档）、连板数、流通市值、所属板块。分钟线/盘口快照被保留
策略清理后事件档案仍然完整，是打板策略的样本地基。

limit_up_sentiment：打板情绪周期——收盘封板家数、炸板率、最高连板、连板家数、
打板指数（昨日封板股今日平均收益）。全部基于日线，历史可全量回溯。

连板口径：按交易日连续（跨周末/节假日不断链）；盘中触板未封记 touched=1,
sealed_close=0，不计板数。
"""
from datetime import datetime, timedelta

import pandas as pd

import datasource

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS limit_up_events (
    date TEXT NOT NULL, code TEXT NOT NULL,
    name TEXT,
    touched INTEGER NOT NULL,        -- 盘中触及涨停价
    sealed_close INTEGER NOT NULL,   -- 收盘封住涨停
    one_word INTEGER,                -- 一字板（开盘即封且全天未开）
    first_seal_ts TEXT,              -- 首次封板分钟（分钟线 close 达到涨停价）
    open_count INTEGER,              -- 封板后开板次数（分钟线口径）
    seal_amount_avg REAL,            -- 封板快照平均封单金额（元）
    seal_amount_close REAL,          -- 收盘封单金额（元，未封=0）
    board_count INTEGER,             -- 连板数（首板=1，未封=0）
    limit_price REAL,                -- 涨停价（快照口径，缺失时为空）
    float_mv REAL,                   -- 流通市值（快照口径，缺失时为空）
    industry TEXT,                   -- 所属板块（stock_industry 最新映射）
    intraday_source TEXT,            -- 日内字段来源：realtime+minute / minute / daily_only
    updated_at TEXT,
    PRIMARY KEY (date, code)
);
CREATE INDEX IF NOT EXISTS idx_lue_date ON limit_up_events(date, sealed_close);
"""

_SENTIMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS limit_up_sentiment (
    date TEXT PRIMARY KEY,
    limit_count INTEGER,             -- 收盘封板家数
    touch_count INTEGER,             -- 盘中触板家数
    broken_ratio REAL,               -- 炸板率 = (触板-封板)/触板
    max_board INTEGER,               -- 最高连板高度
    board2_count INTEGER,            -- 连板（>=2板）家数
    yday_limit_today_ret REAL,       -- 打板指数：昨日封板股今日收盘平均收益（含买不进的一字板，偏乐观）
    honest_ret REAL,                 -- 可成交口径：昨日非一字封板股今日开盘买（开盘即板剔除）收盘卖，扣千1滑点
    updated_at TEXT
);
"""


def setup():
    with datasource._conn() as c:
        c.executescript(_EVENTS_SCHEMA)
        c.executescript(_SENTIMENT_SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(limit_up_sentiment)")}
        if "honest_ret" not in cols:
            c.execute("ALTER TABLE limit_up_sentiment ADD COLUMN honest_ret REAL")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def limit_threshold(code: str, name: str = "") -> float:
    """涨停幅度（与 limit_up_watch 口径一致）：ST 5% / 北交所 30% / 创业科创 20% / 主板 10%。"""
    if "ST" in str(name).upper():
        return 0.048
    if code.startswith("BJ"):
        return 0.298
    if code.startswith(("SZ30", "SH688")):
        return 0.198
    return 0.098


def _names(codes: list[str]) -> dict:
    if not codes:
        return {}
    with datasource._conn() as c:
        rows = c.execute(
            f"SELECT code,name FROM stock_master WHERE code IN ({','.join('?' * len(codes))})",
            codes).fetchall()
    return {r[0]: r[1] for r in rows}


def _industries(codes: list[str]) -> dict:
    if not codes:
        return {}
    with datasource._conn() as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_industry'")}
        if not tables:
            return {}
        rows = c.execute(
            f"SELECT code,sector_name FROM stock_industry WHERE code IN ({','.join('?' * len(codes))})"
            " ORDER BY updated_at DESC", codes).fetchall()
    out = {}
    for code, sector in rows:
        out.setdefault(code, sector)
    return out


def _daily_panel(start: str, end: str, codes: list[str] | None = None) -> pd.DataFrame:
    sql = ("SELECT code,date,open,high,low,close FROM market_daily "
           "WHERE source='ths_ifind' AND date BETWEEN ? AND ?")
    params: list = [start, end]
    if codes:
        sql += f" AND code IN ({','.join('?' * len(codes))})"
        params += codes
    sql += " ORDER BY code,date"
    with datasource._conn() as c:
        return pd.read_sql_query(sql, c, params=params)


def _touched_flags(panel: pd.DataFrame, names: dict) -> pd.DataFrame:
    """向量化标记触板/封板/一字板（阈值口径，±0.2% 与 limit_up_watch 一致）。"""
    p = panel.sort_values(["code", "date"]).copy()
    p["prev_close"] = p.groupby("code")["close"].shift(1)
    p["thr"] = [limit_threshold(code, names.get(code, "")) for code in p["code"]]
    limit = p["prev_close"] * (1 + p["thr"])
    p["touched"] = (p["high"] >= limit).astype(int)
    p["sealed_close"] = (p["close"] >= limit).astype(int)
    p["one_word"] = ((p["open"] >= limit * 0.999) & (p["low"] >= limit * 0.999)).astype(int)
    p.loc[p["prev_close"].isna(), ["touched", "sealed_close", "one_word"]] = 0
    return p


def _board_counts(events: pd.DataFrame, trade_days: list[str]) -> pd.Series:
    """连板数：sealed_close=1 且前一交易日也封板则 +1，跨周末/节假日不断链。"""
    day_idx = {d: i for i, d in enumerate(sorted(trade_days))}
    counts = {}
    for code, g in events[events["sealed_close"] == 1].groupby("code"):
        streak = 0
        prev_i = None
        for day in g.sort_values("date")["date"]:
            i = day_idx.get(day)
            streak = streak + 1 if (prev_i is not None and i is not None and i == prev_i + 1) else 1
            counts[(day, code)] = streak
            prev_i = i
    return pd.Series(counts, dtype="int64")


def _minute_enrich(day: str, codes: list[str], panel: pd.DataFrame) -> pd.DataFrame:
    """分钟线提取首次封板时间与开板次数（阈值口径）。"""
    if not codes:
        return pd.DataFrame()
    prev = panel[panel["date"] < day].groupby("code")["close"].last()
    out = []
    with datasource._conn() as c:
        for chunk in (codes[i:i + 200] for i in range(0, len(codes), 200)):
            df = pd.read_sql_query(
                f"SELECT code,datetime,close FROM ifind_minute WHERE datetime BETWEEN ? AND ?"
                f" AND code IN ({','.join('?' * len(chunk))}) ORDER BY code,datetime",
                c, params=[day, day + " 23:59:59", *chunk])
            if df.empty:
                continue
            for code, g in df.groupby("code"):
                pc = prev.get(code)
                if not pc or pd.isna(pc):
                    continue
                limit = pc * (1 + limit_threshold(code))
                sealed = (g["close"] >= limit).to_numpy()
                if not sealed.any():
                    out.append((code, None, 0))
                    continue
                first_ts = g["datetime"].to_numpy()[sealed.argmax()]
                # 开板次数：封板后 True→False 的跳变次数（离开涨停即一次开板）
                after = sealed[sealed.argmax():]
                opens = int(((~after[1:]) & after[:-1]).sum()) if len(after) > 1 else 0
                out.append((code, str(first_ts), opens))
    return pd.DataFrame(out, columns=["code", "first_seal_ts", "open_count"])


def _realtime_enrich(day: str, codes: list[str], panel: pd.DataFrame) -> pd.DataFrame:
    """盘口快照提取封单金额与涨停价/流通市值（保留期内才有数据）。

    两条写入链路口径不同：轮询行有 float_mv 无盘口字段，THS_SS 盘口行反之；
    涨停价优先取 limit_up 字段，缺失时按昨收×幅度推算。
    """
    if not codes:
        return pd.DataFrame()
    prev = panel[panel["date"] < day].groupby("code")["close"].last()
    out = []
    with datasource._conn() as c:
        for chunk in (codes[i:i + 200] for i in range(0, len(codes), 200)):
            df = pd.read_sql_query(
                f"SELECT code,datetime,limit_up,float_mv,bid1,bid_size1 FROM ifind_realtime"
                f" WHERE datetime BETWEEN ? AND ? AND code IN ({','.join('?' * len(chunk))})"
                f" ORDER BY code,datetime",
                c, params=[day, day + " 23:59:59", *chunk])
            if df.empty:
                continue
            for code, g in df.groupby("code"):
                limit_price = g["limit_up"].dropna()
                if not limit_price.empty:
                    limit = float(limit_price.iloc[-1])
                else:
                    pc = prev.get(code)
                    if not pc or pd.isna(pc):
                        continue
                    limit = pc * (1 + limit_threshold(code))
                mv = g["float_mv"].dropna()
                sealed = g[g["bid1"] >= limit * 0.999]
                amounts = sealed["bid1"] * sealed["bid_size1"] * 100  # bid_size 单位为手
                close_amt = float(amounts.iloc[-1]) if (not sealed.empty and sealed.index[-1] == g.index[-1]) else 0.0
                if sealed.empty and mv.empty:
                    continue
                out.append((code, limit,
                            float(mv.iloc[-1]) if not mv.empty else None,
                            float(amounts.mean()) if len(amounts) else None, close_amt))
    return pd.DataFrame(out, columns=["code", "limit_price", "float_mv", "seal_amount_avg", "seal_amount_close"])


def prepare_orderbook(day: str, codes: list[str] | None = None, limit: int = 150) -> dict:
    """对当日触板股补采盘口快照（THS_SS 逐只），供封单金额提取。

    全市场轮询不写盘口字段，封单数据靠本函数按需补采（触板股每天约几十至百余只，
    成本可控）。已有盘口数据的票跳过；逐只容错不中断整批。
    """
    setup()
    if codes is None:
        lead = (pd.Timestamp(day) - timedelta(days=10)).strftime("%Y-%m-%d")
        panel = _daily_panel(lead, day)
        flags = _touched_flags(panel, _names(panel["code"].unique().tolist()))
        codes = flags[(flags["date"] == day) & (flags["touched"] == 1)]["code"].tolist()
    if not codes:
        return {"day": day, "fetched": 0, "skipped": 0, "failed": 0, "errors": []}
    with datasource._conn() as c:
        have = {r[0] for r in c.execute(
            f"SELECT DISTINCT code FROM ifind_realtime WHERE datetime BETWEEN ? AND ?"
            f" AND bid1 IS NOT NULL AND code IN ({','.join('?' * len(codes))})",
            [day, day + " 23:59:59", *codes])}
    fetched, failed, errors = 0, 0, []
    for code in codes[:limit]:
        if code in have:
            continue
        try:
            datasource.fetch_orderbook_day_to_db(code, day)
            fetched += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{code}:{type(exc).__name__}")
    return {"day": day, "fetched": fetched, "skipped": len(have), "failed": failed,
            "errors": errors[:5]}


def build_events(day: str, panel: pd.DataFrame | None = None,
                 names: dict | None = None) -> dict:
    """加工单日涨停事件；幂等（INSERT OR REPLACE）。panel/names 可复用以避免重复读库。"""
    setup()
    lead = (pd.Timestamp(day) - timedelta(days=10)).strftime("%Y-%m-%d")
    if panel is None:
        panel = _daily_panel(lead, day)
    if names is None:
        names = _names(panel["code"].unique().tolist())
    flags = _touched_flags(panel, names)
    today = flags[flags["date"] == day]
    touched = today[today["touched"] == 1]
    if touched.empty:
        return {"day": day, "events": 0, "sealed": 0}
    codes = touched["code"].tolist()

    minute = _minute_enrich(day, codes, flags[flags["date"] <= day])
    realtime = _realtime_enrich(day, codes, flags[flags["date"] <= day])
    industry = _industries(codes)

    # 连板数：优先查事件表昨日记录（增量口径）；表内无记录时退回面板内连板推断
    trade_days = sorted(flags["date"].unique())
    day_idx = {d: i for i, d in enumerate(trade_days)}
    prev_day = trade_days[day_idx[day] - 1] if day in day_idx and day_idx[day] > 0 else None
    with datasource._conn() as c:
        prev_rows = c.execute(
            "SELECT e.code,e.board_count FROM limit_up_events e "
            "JOIN (SELECT code,MAX(date) md FROM limit_up_events WHERE date<? AND sealed_close=1 GROUP BY code) l"
            " ON l.code=e.code AND l.md=e.date WHERE e.sealed_close=1 AND l.md=?", (day, prev_day)).fetchall()
    prev_board = dict(prev_rows)
    in_panel_board = _board_counts(flags[flags["date"] <= day], trade_days)

    minute_map = minute.set_index("code") if not minute.empty else None
    rt_map = realtime.set_index("code") if not realtime.empty else None
    rows = []
    for r in touched.itertuples():
        code = r.code
        sealed = int(r.sealed_close)
        if sealed:
            if prev_day and code in prev_board:
                board = int(prev_board[code]) + 1
            else:
                board = int(in_panel_board.get((day, code), 1))
        else:
            board = 0
        m = minute_map.loc[code] if minute_map is not None and code in minute_map.index else None
        rt = rt_map.loc[code] if rt_map is not None and code in rt_map.index else None
        src = ("realtime+minute" if rt is not None and m is not None else
               "minute" if m is not None else "daily_only")
        rows.append((day, code, names.get(code, ""), 1, sealed, int(r.one_word),
                     str(m["first_seal_ts"]) if m is not None and pd.notna(m["first_seal_ts"]) else None,
                     int(m["open_count"]) if m is not None else None,
                     float(rt["seal_amount_avg"]) if rt is not None and pd.notna(rt["seal_amount_avg"]) else None,
                     float(rt["seal_amount_close"]) if rt is not None and pd.notna(rt["seal_amount_close"]) else None,
                     board,
                     float(rt["limit_price"]) if rt is not None and pd.notna(rt["limit_price"]) else None,
                     float(rt["float_mv"]) if rt is not None and pd.notna(rt["float_mv"]) else None,
                     industry.get(code), src, _now()))
    with datasource._conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO limit_up_events"
            "(date,code,name,touched,sealed_close,one_word,first_seal_ts,open_count,"
            "seal_amount_avg,seal_amount_close,board_count,limit_price,float_mv,industry,"
            "intraday_source,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return {"day": day, "events": len(rows), "sealed": sum(r[4] for r in rows)}


def build_sentiment(day: str) -> dict:
    """由事件表汇总单日情绪指标；打板指数用昨日封板股的今日收盘收益。"""
    setup()
    with datasource._conn() as c:
        today = pd.read_sql_query(
            "SELECT code,sealed_close,board_count FROM limit_up_events WHERE date=?", c, params=(day,))
        prev_days = [r[0] for r in c.execute(
            "SELECT DISTINCT date FROM limit_up_events WHERE date<? ORDER BY date DESC LIMIT 1", (day,))]
    touch = int(len(today))
    sealed = int(today["sealed_close"].sum()) if touch else 0
    broken = (touch - sealed) / touch if touch else None
    max_board = int(today["board_count"].max()) if touch else 0
    board2 = int((today["board_count"] >= 2).sum()) if touch else 0

    yday_ret = None
    honest = None
    if prev_days:
        yday = prev_days[0]
        with datasource._conn() as c:
            ysealed = pd.read_sql_query(
                "SELECT code,one_word FROM limit_up_events WHERE date=? AND sealed_close=1", c, params=(yday,))
            if not ysealed.empty:
                codes = ysealed["code"].tolist()
                names = _names(codes)
                df = pd.read_sql_query(
                    f"SELECT code,date,open,close FROM market_daily WHERE source='ths_ifind'"
                    f" AND date IN (?,?) AND code IN ({','.join('?' * len(codes))}) ORDER BY code,date",
                    c, params=[yday, day, *codes])
                rets = []
                honest_rets = []
                tradable = set(ysealed.loc[ysealed["one_word"] == 0, "code"])
                for code, g in df.groupby("code"):
                    if len(g) != 2 or g.iloc[0]["close"] <= 0:
                        continue
                    yc, today = g.iloc[0], g.iloc[1]
                    rets.append(today["close"] / yc["close"] - 1)
                    if code in tradable and today["open"] and today["open"] > 0:
                        # 今日开盘即涨停（一字/秒板）→ 买不进，剔除（ST 5% 口径要带名称）
                        if today["open"] < yc["close"] * (1 + limit_threshold(code, names.get(code, ""))) * 0.995:
                            honest_rets.append(today["close"] / today["open"] - 1 - 0.001)
                if rets:
                    yday_ret = float(pd.Series(rets).mean())
                if honest_rets:
                    honest = float(pd.Series(honest_rets).mean())
    with datasource._conn() as c:
        c.execute("INSERT OR REPLACE INTO limit_up_sentiment"
                  "(date,limit_count,touch_count,broken_ratio,max_board,board2_count,"
                  "yday_limit_today_ret,honest_ret,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (day, sealed, touch, broken, max_board, board2, yday_ret, honest, _now()))
    return {"day": day, "封板": sealed, "触板": touch,
            "炸板率": round(broken, 4) if broken is not None else None,
            "最高板": max_board, "连板家数": board2,
            "打板指数": round(yday_ret, 4) if yday_ret is not None else None,
            "可成交接力": round(honest, 4) if honest is not None else None}


def backfill(start: str, end: str) -> dict:
    """历史回填：日线口径事件（无日内字段处标 daily_only）+ 情绪。分块读库控内存。"""
    setup()
    days = datasource.expected_trade_days(start, end)
    if not days:
        return {"days": 0}
    lead = (pd.Timestamp(days[0]) - timedelta(days=10)).strftime("%Y-%m-%d")
    built = 0
    names = None
    # 按 ~90 自然日分块，块间通过事件表 prev_board 衔接连板
    chunk = None
    chunk_end = None
    for day in days:
        if chunk is None or day > chunk_end:
            lo = (pd.Timestamp(day) - timedelta(days=10)).strftime("%Y-%m-%d")
            hi = (pd.Timestamp(day) + timedelta(days=90)).strftime("%Y-%m-%d")
            chunk = _daily_panel(min(lead, lo), hi)
            chunk_end = hi
            names = _names(chunk["code"].unique().tolist())
        build_events(day, chunk[chunk["date"] <= day], names)
        built += 1
    for day in days:
        build_sentiment(day)
    return {"days": built, "sentiment": len(days)}


def sentiment_series(limit: int = 60) -> pd.DataFrame:
    setup()
    with datasource._conn() as c:
        return pd.read_sql_query(
            "SELECT * FROM limit_up_sentiment ORDER BY date DESC LIMIT ?", c, params=(limit,))
