"""Durable read-only SSE collector, independent of Streamlit reloads."""
import json
import sqlite3
import time
from pathlib import Path
from contextlib import closing
from common import DATA_DIR

DB = DATA_DIR / 'mining_events.db'
KINDS = {'round_start','step_update','candidate_gen','review_result','llm_result',
         'gate_eval','gate_pass','round_complete'}


def read_details():
    try:
        with closing(sqlite3.connect(DB.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)) as c:
            row = c.execute('select value from state where key="connection"').fetchone()
            state = json.loads(row[0]) if row else {}
            rows = c.execute('select payload from events order by id desc limit 200').fetchall()
            current = c.execute('select value from state where key="round"').fetchone()
        return dict(connected=state.get('connected', False) and time.time()-state.get('ts',0)<45,
                    last_received=state.get('ts'), events=[json.loads(r[0]) for r in reversed(rows)],
                    current=json.loads(current[0]) if current else None)
    except (OSError, sqlite3.Error, ValueError):
        return dict(connected=False, last_received=None, events=[], current=None)


def snapshot(live):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    details = read_details()
    p = details['current']
    # A completed round remains visible until the next round starts.
    lifecycle = [e for e in details['events'] if e.get('type') in ('round_start', 'round_complete')]
    if lifecycle:
        last = lifecycle[-1]
        if last.get('type') == 'round_complete':
            p = dict(last, factor_type=(last.get('stats') or {}).get('factor_type'), status='round_complete')
    start = live.get('running', {}).get('multitype_mine')
    if p and p.get('factor_type') and start and live.get('fresh') and details['connected']:
        ts = datetime.fromisoformat(p['ts']).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        if ts >= int(start):
            return dict(factor_type=p['factor_type'], iteration=p.get('iteration'),source='events',
                        status=p.get('status', 'running'))
    return None


def collect():
    import requests
    import fcntl
    lock = (DATA_DIR / 'mining_events.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    with closing(sqlite3.connect(DB, timeout=5)) as c:
        c.executescript('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,payload TEXT); CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);')
        def state(key, value):
            c.execute('insert or replace into state values (?,?)',(key,json.dumps(value,ensure_ascii=False)))
        while True:
            try:
                with requests.get('http://127.0.0.1:8502/events',stream=True,timeout=(3,30)) as r:
                    r.raise_for_status()
                    with c:
                        state('round',None)
                        state('connection',dict(connected=True,ts=time.time()))
                    for line in r.iter_lines(chunk_size=1):
                        with c:
                            state('connection',dict(connected=True,ts=time.time()))
                            if not line.startswith(b'data:'):
                                continue
                            e=json.loads(line[5:])
                            if e.get('type') in KINDS:
                                c.execute('insert into events(payload) values (?)',(json.dumps(e,ensure_ascii=False),))
                                c.execute('delete from events where id <= (select max(id)-2000 from events)')
                            if e.get('type')=='round_start':
                                state('round',e)
                            elif e.get('type')=='round_complete' or (e.get('type')=='job_end' and e.get('job_key') in ('multitype_mine','loopengine')):
                                state('round',None)
            except (requests.RequestException, ValueError, sqlite3.Error):
                with c:
                    state('connection',dict(connected=False,ts=time.time()))
                    state('round',None)
                time.sleep(3)


if __name__ == '__main__':
    collect()
