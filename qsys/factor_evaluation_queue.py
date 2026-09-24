"""Versioned evaluation receipts and a bounded, fair research queue.

No result in this module grants trading eligibility. Date-based retries never
reuse an earlier factor version or treat an empty scorecard as valid evidence.
"""
import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from common import DATA_DIR

DB = DATA_DIR / 'factor_evaluation.db'
POLICY = 'factor-eval-v2'
MIN_TRAIN_DAYS = 250
REVIEW_DAYS = 7
MAX_FAILURES = 3


def factor_version(factor):
    keys = ('name', 'kind', 'code', 'factor_type', 'theory_id', 'hypothesis_id', 'first_seen', 'version_seen_at', 'norm', 'regime_scope')
    def canonical(value):
        if value is None or (isinstance(value, float) and not math.isfinite(value)):
            return ''
        return str(value)
    return hashlib.sha256(json.dumps({**{k: canonical(factor.get(k)) for k in keys},'policy':POLICY},
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def connect():
    c = sqlite3.connect(DB, timeout=10)
    c.execute('PRAGMA busy_timeout=10000')
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS queue(
      name TEXT,pool TEXT,version TEXT,payload TEXT,status TEXT DEFAULT 'pending',
      priority INTEGER DEFAULT 0,created REAL,updated REAL,last_day TEXT,
      due_day TEXT,failures INTEGER DEFAULT 0,reason TEXT,
      PRIMARY KEY(name,pool,version));
    CREATE TABLE IF NOT EXISTS receipts(
      name TEXT,pool TEXT,version TEXT,day TEXT,policy TEXT,status TEXT,reason TEXT,
      report TEXT,created REAL,PRIMARY KEY(name,pool,version,day,policy));
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,payload TEXT);
    CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
    ''')
    return c


def classify(row, factor_type=None):
    reason = str(row.get('评估原因') or row.get('建议方向') or '')
    explicit = row.get('评估状态')
    if explicit in ('sample_insufficient', 'data_error', 'compute_failed'):
        return explicit, reason
    try:
        days = int(row.get('天数') or 0)
        finite = all(math.isfinite(float(row.get(k))) for k in ('IC均值', 'ICIR'))
    except (ValueError, TypeError, OverflowError):
        days, finite = 0, False
    from validation_policy import MIN_IC_DAYS
    minimum = MIN_IC_DAYS.get(factor_type or row.get('factor_type'), MIN_TRAIN_DAYS)
    if finite and days >= minimum:
        return 'valid', ''
    if finite:
        return 'sample_insufficient', f'训练有效天数不足 {days}/{minimum}'
    if any(t in reason for t in ('预选窗', '样本', 'IC 序列为空')):
        return 'sample_insufficient', reason or f'训练有效天数不足 {days}/{minimum}'
    if '因子值为空' in reason or '字段' in reason:
        return 'data_error', reason
    return 'compute_failed', reason or '指标无效或计算失败'


def enqueue(factors, pool, used_names=()):
    now = time.time()
    with closing(connect()) as c, c:
        for raw in factors:
            f = {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k,v in raw.items()}
            v = factor_version(f)
            c.execute("UPDATE queue SET status='superseded' WHERE name=? AND pool=? AND version<>? AND status<>'running'",(f['name'],pool,v))
            c.execute('''INSERT INTO queue(name,pool,version,payload,priority,created,updated)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(name,pool,version) DO UPDATE SET
                priority=excluded.priority,payload=excluded.payload''',
                (f['name'],pool,v,json.dumps(f,ensure_ascii=False,default=str),int(f['name'] in used_names),now,now))


def select(pool, day, limit):
    """One candidate per type per pass, used-strategy factors first within a type."""
    with closing(connect()) as c:
        rows = c.execute('''SELECT name,version,payload,priority,created FROM queue
          WHERE pool=? AND status NOT IN ('superseded','running','blocked')
          AND (last_day IS NULL OR last_day<?) AND (due_day IS NULL OR due_day<=?)
          ORDER BY priority DESC,COALESCE(due_day,substr(datetime(created,'unixepoch'),1,10)),updated,created,name''',(pool,day,day)).fetchall()
    groups = {}
    for name,v,payload,priority,created in rows:
        f = json.loads(payload)
        f['_version'] = v
        groups.setdefault(f.get('factor_type') or '量价', []).append(f)
    result = []
    while groups and len(result) < limit:
        for kind in list(groups):
            result.append(groups[kind].pop(0))
            if not groups[kind]: del groups[kind]
            if len(result) >= limit: break
    return result


def coverage(pool):
    """Queue coverage and a conservative completion estimate for operations UI."""
    with closing(connect()) as c:
        rows = c.execute("SELECT status,COUNT(*) FROM queue WHERE pool=? GROUP BY status", (pool,)).fetchall()
    counts = {k: int(v) for k, v in rows}
    superseded = counts.pop('superseded', 0)
    total = sum(counts.values())
    done = total - counts.get('pending', 0) - counts.get('running', 0)
    return dict(pool=pool, total=total, done=done, superseded=superseded, pending=counts.get('pending', 0),
                running=counts.get('running', 0), valid=counts.get('valid', 0),
                sample_insufficient=counts.get('sample_insufficient', 0),
                data_error=counts.get('data_error', 0), compute_failed=counts.get('compute_failed', 0),
                blocked=counts.get('blocked', 0), coverage=(done / total if total else 0.0))


def emit(status, **values):
    event = dict(status=status, ts=datetime.now().isoformat(timespec='seconds'), **values)
    with closing(connect()) as c, c:
        c.execute('INSERT INTO events(payload) VALUES (?)',(json.dumps(event,ensure_ascii=False,default=str),))
        c.execute('DELETE FROM events WHERE id <= (SELECT max(id)-1000 FROM events)')
        c.execute("INSERT OR REPLACE INTO state VALUES ('current',?)",(json.dumps(event,ensure_ascii=False,default=str),))
    try:
        from event_bus import bus, EventType
        bus.push(EventType.FACTOR_EVAL_PROGRESS, **event)
    except Exception:
        pass


def record(factor, pool, day, row, train_end, source, universe_hash):
    status, reason = classify(row, factor.get('factor_type'))
    v = factor['_version']
    report = dict(assessment_kind='research_scorecard_not_validation_approval',factor_version=v,policy=POLICY,eval_date=day,train_end=train_end,
                  first_seen=factor.get('first_seen'),version_seen_at=factor.get('version_seen_at'),source=source,universe_hash=universe_hash,
                  minimum_train_days=__import__('validation_policy').MIN_IC_DAYS.get(factor.get('factor_type'),MIN_TRAIN_DAYS),
                  horizon_days=5,cost=0.0025,trading_eligible=False,
                  metrics={k: (None if isinstance(x,float) and not math.isfinite(x) else x)
                           for k,x in row.items()})
    with closing(connect()) as c, c:
        old = c.execute('SELECT failures FROM queue WHERE name=? AND pool=? AND version=?',
                        (factor['name'],pool,v)).fetchone()
        failures = (old[0] if old else 0) + 1 if status in ('data_error','compute_failed') else 0
        queue_status = 'blocked' if failures >= MAX_FAILURES else status
        delay = REVIEW_DAYS if status in ('valid','sample_insufficient') else min(2**failures,7)
        due = (datetime.fromisoformat(day)+timedelta(days=delay)).date().isoformat()
        c.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?,?,?)',
                  (factor['name'],pool,v,day,POLICY,status,reason,json.dumps(report,ensure_ascii=False,default=str),time.time()))
        c.execute('''UPDATE queue SET status=?,last_day=?,due_day=?,failures=?,reason=?,updated=?
                     WHERE name=? AND pool=? AND version=?''',
                  (queue_status,day,due,failures,reason,time.time(),factor['name'],pool,v))
    return status


def read_events(after=0):
    if not DB.exists(): return []
    with closing(sqlite3.connect(DB.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as c:
        return [(r[0],json.loads(r[1])) for r in c.execute('SELECT id,payload FROM events WHERE id>? ORDER BY id LIMIT 1000',(after,))]


def run(pool, batch):
    import fcntl
    import library
    import factor_eval as fe
    import datasource
    from common import all_pools, get_last_trade_day, trade_day_offset
    with (DATA_DIR/'factor_evaluation.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return '已有因子体检运行，本次合并等待'
        # Exclusive lock proves any old running claims have no live worker.
        with closing(connect()) as c,c:
            c.execute("UPDATE queue SET status='pending' WHERE status='running'")
        reg = library.get_factor_registry()
        if reg.empty:
            emit('idle', pool=pool, reason='无因子可评估')
            return '无因子可评估'
        used = set()
        for pack in library.list_strategies().values():
            if pack.get('status') in ('active','shadow'):
                used.update(f['name'] for f in pack.get('factors',[]))
        factors = reg[reg['engine'].eq('loopengine') | reg['kind'].isin(['tech','builtin','manual','evolved','rdagent'])].to_dict('records')
        enqueue(factors,pool,used)
        day = get_last_trade_day()
        selected = select(pool,day,max(1,min(int(batch),100)))
        if not selected:
            emit('idle', pool=pool, reason='无到期或新增因子')
            return '无到期或新增因子，跳过体检'
        codes = all_pools().get(pool) or []
        if len(codes)<30:
            emit('deferred', pool=pool, reason='股票池有效股票不足30只，未消耗评估名额')
            return '股票池有效股票不足30只，未消耗评估名额'
        train_end = trade_day_offset(day,-250)
        source = datasource.get_loop_source()
        universe_hash = hashlib.sha256('|'.join(sorted(codes)).encode()).hexdigest()
        counts = dict(valid=0,sample_insufficient=0,data_error=0,compute_failed=0)
        emit('running',pool=pool,total=len(selected),processed=0,counts=counts)
        begun = time.monotonic()
        try:
            # bounded chunks share panels/cache and isolate a bad factor from the whole batch
            for offset in range(0,len(selected),5):
                chunk = selected[offset:offset+5]
                with closing(connect()) as c,c:
                    for f in chunk:
                        c.execute("UPDATE queue SET status='running' WHERE name=? AND pool=? AND version=?",(f['name'],pool,f['_version']))
                try:
                    card = fe.build_scorecard_batch(chunk,codes,day,source=source,train_end=train_end)
                    mapped = {r['因子']:r.to_dict() for _,r in card.iterrows()}
                except Exception as exc:
                    mapped = {f['name']:{'因子':f['name'],'评估状态':'compute_failed','评估原因':str(exc)} for f in chunk}
                # Recheck factor versions before publishing research metrics.
                latest = library.get_factor_registry()
                versions = {r['name']:factor_version(r) for r in latest.to_dict('records')}
                valid_rows = []
                results = []
                for f in chunk:
                    row = mapped.get(f['name'],{'因子':f['name'],'评估状态':'compute_failed','评估原因':'评估没有返回结果'})
                    if versions.get(f['name']) != f['_version']:
                        row = {'因子':f['name'],'评估状态':'compute_failed','评估原因':'因子版本已变更，本次结果作废'}
                    row['factor_type'] = f.get('factor_type')
                    status, reason = classify(row, f.get('factor_type'))
                    results.append((f,row,status))
                    if status=='valid': valid_rows.append({**row,'factor_version':f['_version']})
                # Write usable scorecards before the success receipt. A failed write leaves
                # claims recoverable instead of suppressing retry with a false success.
                if valid_rows:
                    import pandas as pd
                    library.save_scorecard(pd.DataFrame(valid_rows),pool,day)
                for f,row,status in results:
                    record(f,pool,day,row,train_end,source,universe_hash)
                    counts[status]+=1
                    emit('running',pool=pool,total=len(selected),processed=sum(counts.values()),
                         counts=counts,factor=f['name'],result=status,reason=row.get('评估原因') or row.get('建议方向'))
                # Yield between chunks; never kill a running numerical calculation.
                if time.monotonic()-begun > 600 and sum(counts.values()) < len(selected):
                    emit('deferred',pool=pool,total=len(selected),processed=sum(counts.values()),counts=counts,
                         reason='本批计算时间已达预算，剩余因子留待下个处理窗口')
                    return '体检时间预算已用尽，剩余因子仍在队列'
            emit('complete',pool=pool,total=len(selected),processed=sum(counts.values()),counts=counts)
        except Exception as exc:
            emit('failed',pool=pool,reason=str(exc),counts=counts)
            raise
        return f"体检完成 {len(selected)} 个：有效 {counts['valid']} · 样本不足 {counts['sample_insufficient']} · 数据异常 {counts['data_error']} · 计算失败 {counts['compute_failed']}"


def request_drain(pool):
    with closing(connect()) as c,c:
        c.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('drain:'+pool,json.dumps({'requested':time.time()})))


def claim_drain():
    with closing(connect()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT key FROM state WHERE key LIKE 'drain:%' LIMIT 1").fetchone()
        if not row: return None
        c.execute('DELETE FROM state WHERE key=?',(row[0],))
        return row[0][6:]


def retry_blocked(name, pool, version):
    """Explicit retry after repairing data/code; preserves all previous receipts."""
    with closing(connect()) as c,c:
        result = c.execute("""UPDATE queue SET status='pending',failures=0,due_day=NULL,
            last_day=NULL,updated=? WHERE name=? AND pool=? AND version=? AND status='blocked'""",
            (time.time(),name,pool,version))
        return result.rowcount == 1
