"""Daily OHLCV missing-session detection and cache revisions; no synthetic bars."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
from common import DATA_DIR

DB=DATA_DIR/'daily_repair.db'
MARKET=DATA_DIR/'market.db'


def connect():
    c=sqlite3.connect(DB,timeout=15)
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''CREATE TABLE IF NOT EXISTS revisions(code TEXT PRIMARY KEY,revision TEXT);
    CREATE TABLE IF NOT EXISTS tasks(code TEXT PRIMARY KEY,start TEXT,end TEXT,status TEXT,
    attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,missing INTEGER,error TEXT);''')
    return c


def bump_revision(code):
    with closing(connect()) as c,c:
        c.execute('INSERT OR REPLACE INTO revisions VALUES (?,?)',(code,str(time.time_ns())))


def revision(codes,source):
    if source!='ths_ifind' or not DB.exists(): return ''
    with closing(sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True)) as c:
        rows=dict(c.execute('SELECT code,revision FROM revisions'))
    return hashlib.sha256(json.dumps([(x,rows.get(x,'')) for x in sorted(codes)]).encode()).hexdigest()


def raw_missing_dates(code,start,end):
    if not MARKET.exists(): return []
    # Do not request an unfinished trading session. Calendar rows are authoritative dates.
    today=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
    with closing(sqlite3.connect(MARKET.resolve().as_uri()+'?mode=ro',uri=True)) as c:
        has=c.execute("SELECT 1 FROM sqlite_master WHERE name='ifind_calendar'").fetchone()
        if not has: return []
        # Local provider currently persists SSE calendar; used as A-share calendar proxy.
        dates={r[0] for r in c.execute("SELECT date FROM ifind_calendar WHERE exchange='SSE' AND date BETWEEN ? AND ? AND date<?",(start,end,today))}
        rows=c.execute("SELECT date,open,high,low,close,volume,amount FROM market_daily WHERE source='ths_ifind' AND code=? AND date BETWEEN ? AND ?",(code,start,end)).fetchall()
    import math
    good=set()
    for row in rows:
        vals=row[1:]
        if all(v is not None and math.isfinite(v) and (v>0 if i<4 else v>=0) for i,v in enumerate(vals)):
            op,hi,lo,cl,_,_=vals
            if lo<=min(op,cl)<=max(op,cl)<=hi: good.add(row[0])
    # Validate observed rows even when the local calendar covers a shorter history.
    # Missing sessions outside the calendar cannot be inferred and remain an audit limitation.
    dates.update(row[0] for row in rows if row[0] < today)
    return sorted(dates-good)


def classify_gaps(code, start, end):
    """Keep raw gaps distinct from vendor-confirmed full-session suspensions."""
    gaps = raw_missing_dates(code, start, end)
    import execution_constraints as ec
    states = {}
    if gaps and ec.DB.exists():
        with closing(sqlite3.connect(ec.DB.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            states = {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT date,value,raw FROM observations WHERE source='ths_ifind' AND code=? AND field='suspended' AND date BETWEEN ? AND ?",
                (code,start,end))}
    confirmed = [dict(date=d, raw=states[d][1]) for d in gaps if d in states and states[d][0] == 1]
    return {'raw_missing': gaps, 'confirmed_suspensions': confirmed,
            'unresolved': [d for d in gaps if d not in states or states[d][0] != 1]}


def missing_dates(code, start, end):
    return classify_gaps(code,start,end)['unresolved']


def repair_interval(code, start, end):
    """Query exact historical states first; never fabricate bars for suspended dates."""
    import execution_constraints as ec
    import datasource
    before = raw_missing_dates(code,start,end)
    status_result = ec.sync([code],start,end,fields=['suspended']) if before else []
    remaining = missing_dates(code,start,end)
    written = datasource._ths_fetch_daily(code,remaining[0],remaining[-1]) if remaining else 0
    return dict(written=written,status_fetch=status_result,**classify_gaps(code,start,end))
