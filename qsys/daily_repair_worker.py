"""Persistent stock daily-bar repairs, following local history and closed calendar sessions."""
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
import fcntl,json,sqlite3,subprocess,sys,time,shutil
from daily_integrity import connect,missing_dates,classify_gaps,MARKET
from common import DATA_DIR


def plan():
    today=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
    with closing(sqlite3.connect(MARKET.resolve().as_uri()+'?mode=ro',uri=True)) as m:
        end=m.execute("SELECT MAX(date) FROM ifind_calendar WHERE exchange='SSE' AND date<?",(today,)).fetchone()[0]
        rows=m.execute("SELECT code,MIN(date) FROM market_daily WHERE source='ths_ifind' AND code IN (SELECT code FROM ifind_stocklist) AND upper(code) NOT IN (SELECT upper(code) FROM ifind_indexlist) GROUP BY code").fetchall()
    if not end: return
    with closing(connect()) as c,c:
        for code,start in rows:
            c.execute('''INSERT INTO tasks(code,start,end,status) VALUES(?,?,?,'pending')
             ON CONFLICT(code) DO UPDATE SET status=CASE WHEN excluded.end>end OR excluded.start<start THEN 'pending' ELSE status END,
             attempts=CASE WHEN excluded.end>end THEN 0 ELSE attempts END,
             next_try=CASE WHEN excluded.end>end THEN 0 ELSE next_try END,start=MIN(start,excluded.start),end=MAX(end,excluded.end)''',(code,start,end))


def status(**extra):
    with closing(connect()) as c:
        counts=dict(c.execute('SELECT status,COUNT(*) FROM tasks GROUP BY status'))
        failures=c.execute("SELECT code,missing,error FROM tasks WHERE status IN ('retry','blocked') LIMIT 10").fetchall()
    p=DATA_DIR/'daily_repair_status.json';tmp=p.with_suffix('.tmp')
    tmp.write_text(json.dumps(dict(updated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),counts=counts,failures=failures,**extra),ensure_ascii=False));tmp.replace(p)


def run():
    with (DATA_DIR/'daily_repair.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return
        with closing(connect()) as c,c: c.execute("UPDATE tasks SET status='pending' WHERE status='running'")
        last=0
        while True:
            try:
                if time.time()-last>3600: plan();last=time.time()
                if shutil.disk_usage(DATA_DIR).free<3*1024**3:
                    status(state='waiting_disk');time.sleep(30);continue
                with closing(connect()) as c:
                    task=c.execute("SELECT code,start,end,attempts FROM tasks WHERE status IN ('pending','retry') AND next_try<=? ORDER BY attempts,code LIMIT 1",(time.time(),)).fetchone()
                if not task: status(state='idle');time.sleep(30);continue
                code,start,end,attempts=task;gaps=missing_dates(code,start,end);error=''
                if gaps:
                    with closing(connect()) as c,c: c.execute("UPDATE tasks SET status='running' WHERE code=?",(code,))
                    status(state='running',code=code,missing_before=len(gaps))
                    # Request only a bounded missing interval per turn (up to 366 calendar days).
                    from datetime import timedelta,date
                    stop=min(gaps[-1],(date.fromisoformat(gaps[0])+timedelta(days=365)).isoformat())
                    script='from daily_integrity import repair_interval; import sys,json; print(json.dumps(repair_interval(*sys.argv[1:4]),ensure_ascii=False))'
                    try:
                        r=subprocess.run([sys.executable,'-c',script,code,gaps[0],stop],capture_output=True,text=True,timeout=90)
                        if r.returncode: error='接口子进程失败: '+r.stderr[-400:]
                    except Exception as exc: error=str(exc)[:400]
                    after=missing_dates(code,start,end)
                    progress=len(after)<len(gaps)
                    attempts=0 if progress else attempts+1
                    state='pending' if progress and after else 'done' if not after else 'blocked' if attempts>=3 else 'retry'
                    gaps=after
                else: state='done'
                evidence=classify_gaps(code,start,end)
                if state=='done' and evidence['confirmed_suspensions']:
                    state='done_with_suspensions'
                    error=f"已确认{len(evidence['confirmed_suspensions'])}个整日停牌日期，不伪造日线"
                with closing(connect()) as c,c:
                    c.execute('UPDATE tasks SET status=?,attempts=?,next_try=?,missing=?,error=? WHERE code=?',
                              (state,attempts,time.time()+3600*2**attempts if state=='retry' else 0,len(gaps),error or (f'仍缺{len(gaps)}日，可能停牌或源数据空值，未伪造日线' if gaps else ''),code))
                status(state='running');time.sleep(2)
            except Exception as exc: status(state='error',error=str(exc)[:400]);time.sleep(30)

if __name__=='__main__': run()
