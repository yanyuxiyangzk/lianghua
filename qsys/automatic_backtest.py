"""Local historical research queue; never grants trading eligibility."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from common import DATA_DIR

DB = DATA_DIR / 'automatic_backtest.db'
POLICY = 'automatic-history-v1'
DEFAULT = dict(enabled=True, pools=['沪深300'], single_stock=True, batch=3,
               timeout=120, discover_seconds=1800)
TABLES = ('market_daily', 'ifind_calendar', 'research_calendar_receipts',
          'stock_fundflow_daily', 'sector_inflow_snapshots',
          'ifind_financial', 'lhb_daily', 'research_day_archive',
          'research_raw_revision', 'sr_scan_daily', 'ifind_announcements', 'quote_snapshots')
LABELS = {'pending':'排队', 'running':'计算中', 'research_complete':'研究计算完成',
          'execution_complete':'日频成交回放完成', 'waiting_data':'等待数据',
          'insufficient':'有效样本不足', 'failed':'计算失败',
          'unsupported':'待适配', 'superseded':'旧版本', 'partial':'部分结果'}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS tasks(
      id TEXT PRIMARY KEY,logical_key TEXT,payload TEXT,status TEXT,stage TEXT,
      reason TEXT,created REAL,updated REAL,attempts INTEGER DEFAULT 0,report TEXT);
    CREATE INDEX IF NOT EXISTS auto_tasks_status ON tasks(status,created);
    CREATE INDEX IF NOT EXISTS auto_tasks_logical ON tasks(logical_key);
    CREATE TABLE IF NOT EXISTS manifests(id TEXT PRIMARY KEY,payload TEXT,created REAL);
    ''')
    return c


def setting(key, default=None):
    with closing(connect()) as c:
        row = c.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(key, value):
    with closing(connect()) as c, c:
        c.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, dumps(value)))


def config():
    return {**DEFAULT, **setting('config', {})}


def configure(values):
    cfg = {**config(), **values}
    if not 1 <= int(cfg['batch']) <= 10 or not 10 <= int(cfg['timeout']) <= 300:
        raise ValueError('每批1～10个，单任务超时10～300秒')
    if not isinstance(cfg['pools'], list) or not cfg['pools']:
        raise ValueError('至少选择一个股票池')
    put('config', cfg)
    notify('configuration')


def notify(reason='data_changed'):
    put('dirty', dict(at=time.time(), reason=reason))


def read_db(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=15)


def table_digest(c, table, end):
    columns = [r[1] for r in c.execute(f'PRAGMA table_info("{table}")')]
    if not columns:
        return dict(hash='missing', rows=0)
    used = [x for x in columns if x not in ('stock_id',) and
            (x != 'fetched_at' or table == 'ifind_financial')]
    quoted = ','.join('"'+x+'"' for x in used)
    clauses, params = [], []
    date_col = next((x for x in ('date', 'day', 'datetime', 'ts') if x in columns), None)
    if date_col:
        clauses.append(f'substr("{date_col}",1,10)<=?'); params.append(end)
    if table == 'market_daily':
        clauses.append("source='ths_ifind'")
    sql = f'SELECT {quoted} FROM "{table}"' + (' WHERE '+' AND '.join(clauses) if clauses else '')
    primary=[r[1] for r in c.execute(f'PRAGMA table_info("{table}")') if r[5]]
    sql += ' ORDER BY '+','.join('"'+x+'"' for x in (primary or used))
    h, count, per_code = hashlib.sha256(), 0, {}
    code_index=used.index('code') if 'code' in used else None
    for row in c.execute(sql, params):
        encoded=dumps(list(row)).encode()+b'\n'
        h.update(encoded); count += 1
        if code_index is not None:
            per_code.setdefault(str(row[code_index]),hashlib.sha256()).update(encoded)
    return dict(hash=h.hexdigest(), rows=count, codes={k:v.hexdigest() for k,v in per_code.items()})


def manifest(end):
    import datasource
    import execution_constraints as ec
    hashes = {}
    with closing(read_db(datasource.MKT_DB)) as c:
        deadline = time.monotonic() + 180
        c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        for table in TABLES:
            hashes[table] = table_digest(c, table, end)
        coverage = [dict(code=r[0], start=r[1], end=r[2], rows=r[3]) for r in c.execute(
            "SELECT code,MIN(date),MAX(date),COUNT(*) FROM market_daily WHERE source='ths_ifind' AND date<=? GROUP BY code", (end,))]
    if ec.DB.exists():
        with closing(read_db(ec.DB)) as c:
            hashes['execution_constraints'] = table_digest(c, 'observations', end)
    result = dict(end=end, tables=hashes, coverage=coverage)
    result['id'] = digest(result)
    with closing(connect()) as c, c:
        c.execute('INSERT OR IGNORE INTO manifests VALUES (?,?,?)', (result['id'], dumps(result), time.time()))
    return result


def dependency_revision(manifest, codes, factor):
    """Only referenced data invalidates a factor, not live quotes or other stocks."""
    from loopengine.tree import TYPE_FIELDS
    text=factor.get('code') or ''
    tables=['market_daily','ifind_calendar','research_calendar_receipts']
    dependencies={'资金流':['stock_fundflow_daily'], '板块轮动':['sector_inflow_snapshots'],
                  '财务':['ifind_financial'], '龙虎榜':['lhb_daily'],
                  '盘口异动':['quote_snapshots'], '爆量抢筹':['quote_snapshots'],
                  '支撑阻力':['sr_scan_daily'], '事件记忆':['ifind_announcements']}
    for kind,fields in TYPE_FIELDS.items():
        if any(field in text for field in fields): tables+=dependencies.get(kind,[])
    pieces={}
    for table in tables:
        data=manifest['tables'].get(table,{'hash':'missing'})
        # Sector/marketwide context must retain its full cross-section.
        selected=codes if table not in ('sector_inflow_snapshots',) else []
        pieces[table]={code:data['codes'].get(code,'missing') for code in selected} if data.get('codes') and selected else data['hash']
    # Independent suspension flags affect price validity for every expression.
    states=manifest['tables'].get('execution_constraints',{})
    pieces['suspensions']={code:states.get('codes',{}).get(code,'missing') for code in codes}
    return digest(pieces)


def enqueue(payload):
    payload = {**payload, 'policy':POLICY}
    identity = digest(payload)
    logical = digest([payload['kind'], payload['name'], payload['scope']])
    now = time.time()
    with closing(connect()) as c, c:
        previous = c.execute("SELECT MIN(created) FROM tasks WHERE logical_key=? AND status IN ('pending','running')", (logical,)).fetchone()[0]
        c.execute("UPDATE tasks SET status='superseded',updated=? WHERE logical_key=? AND id<>? AND status='pending'", (now, logical, identity))
        c.execute("INSERT OR IGNORE INTO tasks(id,logical_key,payload,status,stage,reason,created,updated) VALUES (?,?,?,'pending','等待独立进程','',?,?)",
                  (identity,logical,dumps(payload),previous or now,now))
    return identity


def discover(force=False):
    cfg = config()
    dirty, last, now = setting('dirty', {}), setting('discovery', {}), time.time()
    if not force and now-last.get('at',0) < cfg['discover_seconds'] and dirty.get('at',0) <= last.get('started',0):
        return last
    if not force and dirty and now-dirty.get('at',0) < 30:
        return last
    from selection_policy import completed_signal_day
    import library
    from common import all_pools
    import stock_factor_workbench as work
    end = completed_signal_day()
    m = manifest(end)
    coverage = {r['code']:r for r in m['coverage']}
    pools = all_pools()
    pools['本地已采集股票'] = sorted(coverage)
    reg = library.get_factor_registry()
    factors = json.loads(reg.to_json(orient='records')) if not reg.empty else []
    count = 0
    # Keep task identity independent from changing scorecard/health metadata.
    from factor_evaluation_queue import factor_version
    factor_keys=('name','kind','code','factor_type','first_seen','version_seen_at','norm','regime_scope')
    factors=[{k:f.get(k) for k in factor_keys} for f in factors]
    factors.sort(key=lambda f: (f.get('kind') not in ('builtin','tech'),f['name']))
    for pool in cfg['pools']:
        codes = sorted(set(pools.get(pool) or []))
        starts = [coverage[c]['start'] for c in codes if c in coverage]
        for f in factors:
            enqueue(dict(kind='registry', name=f['name'], factor=f, scope=pool,
                         codes=codes, start=min(starts) if starts else end, end=end,
                         manifest=dependency_revision(m,codes,f), horizons=[1,5,10,20], primary_horizon=5))
            count += 1
    if cfg['single_stock'] and work.DB.exists():
        with closing(read_db(work.DB)) as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='experiments'").fetchone()
            rows = c.execute('SELECT payload FROM experiments ORDER BY created').fetchall() if exists else []
        for row in rows:
            exp = json.loads(row[0])
            for candidate in exp.get('candidates', []):
                if candidate.get('status') != 'research_candidate': continue
                enqueue(dict(kind='single',name=candidate['name'],scope=exp['code']+'/'+exp['id'],
                             code=exp['code'],experiment_id=exp['id'],candidate=candidate,
                             input_hash=exp['input_hash'],start=exp['test_start'],end=end,
                             manifest=dependency_revision(m,[exp['code']],{'code':' '.join(['盘口异动'] if exp['source']!='日线量价' else [])})+digest([m['tables'].get('research_day_archive',{}).get('codes',{}).get(exp['code']),m['tables'].get('research_raw_revision',{}).get('codes',{}).get(exp['code'])])))
                count += 1
    result = dict(at=time.time(), started=now, end=end, manifest=m['id'],
                  tasks_seen=count, stocks=len(coverage), pools=cfg['pools'])
    put('discovery', result)
    return result


def stage(identifier, message):
    with closing(connect()) as c, c:
        c.execute('UPDATE tasks SET stage=?,updated=? WHERE id=?', (message,time.time(),identifier))


def isolated(task, timeout):
    with tempfile.TemporaryDirectory(prefix='auto-history-') as tmp:
        request, output = Path(tmp)/'in.json', Path(tmp)/'out.json'
        request.write_text(dumps(task))
        env = {**os.environ, 'QSYS_RESEARCH_LOCAL_ONLY':'1', 'OPENBLAS_NUM_THREADS':'1', 'OMP_NUM_THREADS':'1'}
        with (Path(tmp)/'worker.log').open('w') as log:
            p = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),str(request),str(output)],
                                 stdout=log,stderr=log,env=env,start_new_session=True)
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return dict(status='failed',reason=f'单任务超过{timeout}秒，进程组已终止')
            finally:
                try: os.killpg(p.pid,signal.SIGKILL)
                except ProcessLookupError: pass
                p.wait()
        if not output.exists():
            return dict(status='failed',reason=f'计算进程退出{p.returncode}，未生成报告')
        return json.loads(output.read_text())


def run_batch(force=False):
    cfg = config()
    if not cfg['enabled']: return '自动历史回测已暂停'
    with (DATA_DIR/'automatic_backtest.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return '已有历史回测运行，跳过重入'
        with closing(connect()) as c, c:
            c.execute("UPDATE tasks SET status='pending',stage='进程中断，恢复排队' WHERE status='running'")
        try:
            discover(force)
            put('error', '')
        except Exception as exc:
            put('error', f'覆盖扫描失败：{exc}')
            raise
        counts = {}
        for _ in range(int(cfg['batch'])):
            if not config()['enabled']: break
            with closing(connect()) as c, c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY created,id LIMIT 1").fetchone()
                if not row: break
                c.execute("UPDATE tasks SET status='running',stage='本地数据预检',attempts=attempts+1,updated=? WHERE id=?",(time.time(),row['id']))
            task = dict(id=row['id'], **json.loads(row['payload']))
            try: result = isolated(task,int(cfg['timeout']))
            except Exception as exc: result = dict(status='failed',reason=str(exc))
            result.update(policy=POLICY,task_id=row['id'],trading_eligible=False,created=datetime.now().isoformat())
            status = result['status']
            if status not in LABELS: status='failed'
            with closing(connect()) as c, c:
                c.execute('UPDATE tasks SET status=?,stage=?,reason=?,report=?,updated=? WHERE id=?',
                          (status,LABELS[status],result.get('reason',''),dumps(result),time.time(),row['id']))
            counts[status] = counts.get(status,0)+1
        put('last_run',dict(at=time.time(),counts=counts))
        return '自动历史回测：'+dumps(counts)


def overview(limit=200):
    with closing(connect()) as c:
        latest = '''SELECT t.* FROM tasks t WHERE t.rowid=(SELECT MAX(s.rowid) FROM tasks s WHERE s.logical_key=t.logical_key)'''
        counts = dict(c.execute('SELECT status,COUNT(*) FROM ('+latest+') GROUP BY status').fetchall())
        rows = [dict(r) for r in c.execute(latest+' ORDER BY updated DESC LIMIT ?', (int(limit),))]
    return counts, rows


if __name__ == '__main__':
    task = json.loads(Path(sys.argv[1]).read_text())
    try:
        from automatic_backtest_compute import compute
        result = compute(task)
    except Exception as exc:
        result = dict(status='failed',reason=f'{type(exc).__name__}: {exc}')
    Path(sys.argv[2]).write_text(dumps(result))
