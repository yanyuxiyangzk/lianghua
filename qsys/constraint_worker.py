"""Resumable full-history and daily incremental execution-constraint backfill.

Only stocks already present in the local iFinD daily store are scheduled.
Coverage is checked against actual dated daily rows, not API success codes.
"""
import argparse
from contextlib import closing
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from zoneinfo import ZoneInfo
import execution_constraints as ec
from common import DATA_DIR

MARKET = DATA_DIR / 'market.db'
LIVE = DATA_DIR / 'constraint_worker_status.json'
LOCK = DATA_DIR / 'constraint_worker.lock'


def connection():
    c = ec.connect()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS backfill_tasks(
      code TEXT,year TEXT,field TEXT,start TEXT,end TEXT,status TEXT DEFAULT 'pending',
      attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,missing INTEGER,error TEXT,updated REAL,
      PRIMARY KEY(code,year,field));
    CREATE TABLE IF NOT EXISTS backfill_state(key TEXT PRIMARY KEY,value TEXT);
    CREATE INDEX IF NOT EXISTS backfill_due ON backfill_tasks(status,next_try);
    ''')
    return c


def market_connection():
    return sqlite3.connect(MARKET.resolve().as_uri()+'?mode=ro',uri=True,timeout=30)


def plan():
    # Match stock master and explicitly exclude index master, including misleading codes.
    with closing(market_connection()) as m:
        rows = m.execute('''SELECT d.code,substr(d.date,1,4),MIN(d.date),MAX(d.date)
          FROM market_daily d WHERE d.source='ths_ifind'
          AND d.code IN (SELECT code FROM ifind_stocklist)
          AND upper(d.code) NOT IN (SELECT upper(code) FROM ifind_indexlist)
          GROUP BY d.code,substr(d.date,1,4)''').fetchall()
    with closing(connection()) as c, c:
        for code, year, start, end in rows:
            c.execute('''INSERT INTO backfill_tasks(code,year,field,start,end,updated) VALUES(?,?,?,?,?,?)
              ON CONFLICT(code,year,field) DO UPDATE SET
              status=CASE WHEN excluded.start<start OR excluded.end>end THEN 'pending' ELSE status END,
              next_try=CASE WHEN excluded.start<start OR excluded.end>end THEN 0 ELSE next_try END,
              attempts=CASE WHEN excluded.start<start OR excluded.end>end THEN 0 ELSE attempts END,
              start=MIN(start,excluded.start),end=MAX(end,excluded.end)''',
              (code,year,'suspended',start,end,time.time()))
            for field in ('limit_up','limit_down'):
                c.execute('''INSERT INTO backfill_tasks(code,year,field,start,end,updated) VALUES(?,?,?,?,?,?)
                  ON CONFLICT(code,year,field) DO UPDATE SET
                  status=CASE WHEN excluded.start<start OR excluded.end>end THEN 'pending' ELSE status END,
                  next_try=CASE WHEN excluded.start<start OR excluded.end>end THEN 0 ELSE next_try END,
                  attempts=CASE WHEN excluded.start<start OR excluded.end>end THEN 0 ELSE attempts END,
                  start=MIN(start,excluded.start),end=MAX(end,excluded.end)''',
                  (code,year,field,start,end,time.time()))
        c.execute("INSERT OR REPLACE INTO backfill_state VALUES('last_plan',?)",(str(time.time()),))
    return len(rows)*3


def missing_dates(task):
    code,year,field,start,end = task[:5]
    with closing(market_connection()) as m:
        expected = {r[0] for r in m.execute("SELECT date FROM market_daily WHERE source='ths_ifind' AND code=? AND date BETWEEN ? AND ?",(code,start,end))}
    with closing(connection()) as c:
        known = {r[0] for r in c.execute("SELECT date FROM observations WHERE source='ths_ifind' AND code=? AND field=? AND date BETWEEN ? AND ? AND value IS NOT NULL",(code,field,start,end))}
    return sorted(expected-known)


def snapshot(state='running', **extra):
    with closing(connection()) as c:
        counts = dict(c.execute('SELECT status,COUNT(*) FROM backfill_tasks GROUP BY status'))
        stocks = c.execute('SELECT COUNT(DISTINCT code) FROM backfill_tasks').fetchone()[0]
        samples = [dict(code=a,field=b,missing=n,error=e) for a,b,n,e in c.execute("SELECT code,field,missing,error FROM backfill_tasks WHERE status IN ('retry','blocked') ORDER BY updated DESC LIMIT 10")]
    result = dict(state=state,pid=os.getpid(),updated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                  stocks=stocks,tasks=counts,failures=samples,**extra)
    tmp=LIVE.with_suffix('.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2));tmp.replace(LIVE)
    return result


def step(fetch=None):
    with closing(connection()) as c:
        task=c.execute("SELECT code,year,field,start,end,attempts FROM backfill_tasks WHERE status IN ('pending','retry') AND next_try<=? ORDER BY end DESC,attempts,code,field LIMIT 1",(time.time(),)).fetchone()
    if not task: return False
    key=task[:3]; missing=missing_dates(task)
    error=''
    if missing:
        with closing(connection()) as c,c:
            c.execute("UPDATE backfill_tasks SET status='running',updated=? WHERE code=? AND year=? AND field=?",(time.time(),*key))
        snapshot(current=dict(code=task[0],field=task[2],start=missing[0],end=missing[-1]))
        try:
            if fetch is not None:
                result=fetch([task[0]],missing[0],missing[-1],fields=[task[2]])
            else:
                # Hard timeout isolates native SDK stalls; no shell expansion.
                script='import execution_constraints as e,json,sys; print(json.dumps(e.sync([sys.argv[1]],sys.argv[2],sys.argv[3],fields=[sys.argv[4]]),ensure_ascii=False))'
                completed=subprocess.run([sys.executable,'-c',script,task[0],missing[0],missing[-1],task[2]],capture_output=True,text=True,timeout=90)
                if completed.returncode: raise RuntimeError('采集子进程退出码 '+str(completed.returncode))
                result=json.loads(completed.stdout.strip().splitlines()[-1])
            error='; '.join(r.get('error','') for r in result if r.get('error'))[:500]
        except Exception as exc:
            error=type(exc).__name__+': '+str(exc)[:400]
        missing=missing_dates(task)
    attempts=task[5]+int(bool(missing))
    status='done' if not missing else ('blocked' if attempts>=3 else 'retry')
    with closing(connection()) as c,c:
        c.execute('''UPDATE backfill_tasks SET status=?,attempts=?,next_try=?,missing=?,error=?,updated=?
          WHERE code=? AND year=? AND field=?''',
          (status,attempts,time.time()+min(86400,3600*2**attempts) if missing else 0,len(missing),
           error or (f'仍缺{len(missing)}个行情日期的有效字段' if missing else ''),time.time(),*key))
    snapshot()
    return True


def run(once=False):
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    with LOCK.open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
        with closing(connection()) as c,c:
            c.execute("UPDATE backfill_tasks SET status='pending' WHERE status='running'")
        # Each restart also reconciles newly available daily data.
        plan()
        while True:
            try:
                now=datetime.now(ZoneInfo('Asia/Shanghai'))
                with closing(connection()) as c:
                    last=float(c.execute("SELECT value FROM backfill_state WHERE key='last_plan'").fetchone()[0])
                # During evening ingestion rescan hourly; otherwise every 6h.
                if time.time()-last > (3600 if now.hour>=16 else 21600): plan()
                if shutil.disk_usage(DATA_DIR).free < 3*1024**3:
                    snapshot('waiting_disk',reason='可用空间低于3GiB，暂停写入')
                    time.sleep(30)
                    continue
                worked=step()
                if not worked: snapshot('idle')
                if once: return
                time.sleep(1 if worked else 30)
            except Exception as exc:
                snapshot('error',reason=str(exc)[:500])
                if once: raise
                time.sleep(30)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');p.add_argument('--plan',action='store_true')
    a=p.parse_args()
    if a.plan: print(plan());print(json.dumps(snapshot('planned'),ensure_ascii=False))
    else: run(a.once)
