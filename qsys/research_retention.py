"""Versioned local feature archive and guarded raw-data retention. No network calls."""
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import json
import math
import re
import threading
import uuid
import numpy as np
import pandas as pd
import datasource

VERSION = 'daily-research-v2'
RAW = {'ifind_minute': 'datetime', 'ifind_realtime': 'datetime',
       'quote_snapshots': 'ts', 'tick_data': 'datetime'}
DEFAULT = dict(minute_days=60, micro_days=10, auto_cleanup=False)
_local = threading.local()


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai'))


@contextmanager
def stock_lock(code):
    if not re.fullmatch(r'(SH|SZ|BJ)\d{6}', code):
        raise ValueError('股票代码格式无效')
    path = Path(datasource.MKT_DB).parent / f'stock_research_{code}.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    held = getattr(_local, 'held', set())
    if str(path) in held:
        yield
        return
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('该股票正在同步、研究或清理，请稍后重试')
        _local.held = held | {str(path)}
        try:
            yield
        finally:
            _local.held = held


def guarded(func):
    @wraps(func)
    def call(code, *args, **kwargs):
        with stock_lock(code):
            return func(code, *args, **kwargs)
    return call


def setup():
    with datasource._qconn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS research_calendar_receipts(exchange TEXT,start TEXT,end TEXT,dates_json TEXT,digest TEXT,created TEXT,PRIMARY KEY(exchange,start,end));
        CREATE TABLE IF NOT EXISTS research_retry(code TEXT,day TEXT,source TEXT,next_try TEXT,PRIMARY KEY(code,day,source));
        CREATE TABLE IF NOT EXISTS research_policy(code TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS research_protection(id TEXT PRIMARY KEY,code TEXT,start TEXT,end TEXT,reason TEXT);
        CREATE TABLE IF NOT EXISTS research_raw_revision(code TEXT,day TEXT,source TEXT,revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(code,day,source));
        CREATE TABLE IF NOT EXISTS research_day_archive(code TEXT,day TEXT,source TEXT,version TEXT,revision INTEGER,input_hash TEXT,quality TEXT,reason TEXT,features TEXT,rows INTEGER,created TEXT,PRIMARY KEY(code,day,source,version,revision));
        CREATE TABLE IF NOT EXISTS research_cleanup_log(id TEXT PRIMARY KEY,code TEXT,created TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS research_labels(code TEXT,day TEXT,horizon INTEGER,end_day TEXT,value REAL,status TEXT,input_hash TEXT,updated TEXT,PRIMARY KEY(code,day,horizon));
        CREATE TABLE IF NOT EXISTS research_candidate_runs(id TEXT PRIMARY KEY,code TEXT,created TEXT,input_hash TEXT UNIQUE,payload TEXT);
        CREATE TABLE IF NOT EXISTS research_worker_status(code TEXT PRIMARY KEY,updated TEXT,payload TEXT);
        ''')
        for table, col in RAW.items():
            for action, ref in [('INSERT', 'NEW'), ('UPDATE', 'NEW'), ('DELETE', 'OLD')]:
                c.execute(f'''CREATE TRIGGER IF NOT EXISTS research_{table}_{action.lower()}
                 AFTER {action} ON {table} BEGIN
                 INSERT INTO research_raw_revision(code,day,source,revision)
                 VALUES({ref}.code,substr({ref}.{col},1,10),'{table}',1)
                 ON CONFLICT(code,day,source) DO UPDATE SET revision=revision+1; END''')
            marker = f'_seed_{table}'
            if not c.execute('SELECT 1 FROM research_policy WHERE code=?', (marker,)).fetchone():
                c.execute(f'''INSERT OR IGNORE INTO research_raw_revision(code,day,source,revision)
                  SELECT code,substr({col},1,10),?,1 FROM {table} GROUP BY code,substr({col},1,10)''', (table,))
                c.execute('INSERT INTO research_policy VALUES(?,?)', (marker, '{}'))


def policy(code, c=None):
    if c is None:
        setup()
        with datasource._conn() as conn:
            return policy(code, conn)
    row = c.execute('SELECT payload FROM research_policy WHERE code=?', (code,)).fetchone()
    return {**DEFAULT, **(json.loads(row[0]) if row else {})}


def set_policy(code, minute_days, micro_days, auto_cleanup):
    if not 1 <= int(minute_days) <= 2000 or not 1 <= int(micro_days) <= 2000:
        raise ValueError('保留交易日必须在1～2000之间')
    setup()
    with stock_lock(code), datasource._conn() as c:
        c.execute('INSERT OR REPLACE INTO research_policy VALUES(?,?)',
                  (code, json.dumps(dict(minute_days=int(minute_days), micro_days=int(micro_days),auto_cleanup=bool(auto_cleanup)))))


def protect(code, start, end, reason):
    if start > end or not reason.strip():
        raise ValueError('请输入有效日期及保留原因')
    setup()
    with stock_lock(code), datasource._conn() as c:
        c.execute('INSERT INTO research_protection VALUES(?,?,?,?,?)',(uuid.uuid4().hex, code, start, end, reason.strip()))


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,default=str, allow_nan=False).encode()).hexdigest()


def _features(table, df, day):
    ts = pd.to_datetime(df[RAW[table]], errors='coerce')
    if ts.isna().any() or ts.duplicated().any() or set(ts.dt.strftime('%Y-%m-%d')) != {day}:
        raise ValueError('时间无效、重复或日期不一致')
    names = ['open','high','low','close','volume','amount'] if table == 'ifind_minute' else ['price','volume']
    if table == 'ifind_realtime': names += ['bid1','ask1'] + [f'{s}_size{i}' for s in ['bid','ask'] for i in range(1,6)]
    if table == 'quote_snapshots': names += ['bid1','ask1','bid_vol_sum','ask_vol_sum']
    x = df[names].apply(pd.to_numeric, errors='coerce')
    if not np.isfinite(x.to_numpy(dtype=float)).all() or (x < 0).any().any():
        raise ValueError('关键字段缺失、非有限值或负数')
    if (x['close' if table == 'ifind_minute' else 'price'] <= 0).any(): raise ValueError('价格非正')
    if table == 'ifind_minute':
        required = set(pd.date_range(day+' 09:31', day+' 11:30', freq='min')) | set(pd.date_range(day+' 13:01', day+' 15:00', freq='min'))
        if not required.issubset(set(ts)): raise ValueError('缺少连续竞价分钟时间点，保留待补齐')
        if ((x.high < x[['open','close','low']].max(axis=1)) | (x.low > x[['open','close','high']].min(axis=1))).any(): raise ValueError('OHLC关系异常')
        if (x[['open','low']] <= 0).any().any() or x.volume.sum() <= 0: raise ValueError('价格或成交量无效')
        vwap = x.amount.sum()/x.volume.sum()
        if not x.low.min()*.95 <= vwap <= x.high.max()*1.05: raise ValueError('成交额/成交量单位或VWAP异常')
        ret = x.close.pct_change(fill_method=None).dropna()
        return dict(tail_ret_30m=float(x.close.iloc[-1]/x.close.iloc[-30]-1),realized_vol=float(np.sqrt((ret**2).sum())),close_vwap_gap=float(x.close.iloc[-1]/vwap-1))
    if len(df) < (500 if table == 'ifind_realtime' else 100): raise ValueError('日内样本不足')
    if ts.min() > pd.Timestamp(day+' 09:40') or ts.max() < pd.Timestamp(day+' 14:55'): raise ValueError('缺少开盘或收盘时段')
    for begin, end in [('09:30','11:30'), ('13:00','15:00')]:
        part = ts[(ts >= pd.Timestamp(day+' '+begin)) & (ts <= pd.Timestamp(day+' '+end))]
        if part.empty or part.diff().dropna().max() > pd.Timedelta(minutes=10): raise ValueError('日内覆盖存在超过10分钟缺口')
    if table == 'tick_data':
        return dict(tick_mean_price=float(x.price.mean()),tick_volume_sum=float(x.volume.sum()),tick_price_std=float(x.price.std(ddof=0)))
    if ((x.ask1 < x.bid1) | (x.ask1 <= 0) | (x.bid1 <= 0)).any(): raise ValueError('盘口买卖价无效，保留待核实')
    if table == 'ifind_realtime':
        bid=x[[f'bid_size{i}' for i in range(1,6)]].sum(axis=1);ask=x[[f'ask_size{i}' for i in range(1,6)]].sum(axis=1)
    else: bid,ask=x.bid_vol_sum,x.ask_vol_sum
    if ((bid+ask)<=0).any() or x.volume.iloc[-1]<=0: raise ValueError('盘口挂单量或成交量无效')
    out=dict(ob_imbalance_mean=float(((bid-ask)/(bid+ask)).mean()),spread_median=float(((x.ask1-x.bid1)/x.price).median()))
    if table == 'ifind_realtime': out['seal_strength_close']=float(x.bid_size1.iloc[-1]/x.volume.iloc[-1])
    return out


def process_day(code, day, table):
    with stock_lock(code):
        with datasource._conn() as c:
            rev=c.execute('SELECT revision FROM research_raw_revision WHERE code=? AND day=? AND source=?',(code,day,table)).fetchone()
            df=pd.read_sql_query(f'SELECT * FROM {table} WHERE code=? AND {RAW[table]}>=? AND {RAW[table]}<? ORDER BY {RAW[table]}',c,params=(code,day,str(pd.Timestamp(day).date()+timedelta(days=1))))
            daily=c.execute("SELECT open,high,low,close,volume,amount FROM market_daily WHERE code=? AND date=? AND source='ths_ifind'",(code,day)).fetchone()
        if df.empty or not rev: return 'empty'
        quality,reason,features='ready','',{}
        try:
            if not daily or any(v is None or not math.isfinite(float(v)) for v in daily): raise ValueError('缺少有效基础日线')
            features=_features(table,df,day)
        except (ValueError,KeyError,TypeError) as exc: quality,reason='blocked',str(exc)
        # Compatibility readers must be ready BEFORE publishing deletion permission.
        if quality == 'ready':
            try:
                if table == 'ifind_minute': datasource.compute_intraday_features(code,day,day)
                elif table == 'ifind_realtime': datasource.compute_orderbook_features(code,day,day)
                if table in ('ifind_minute','ifind_realtime'):
                    target='stock_intraday_features' if table=='ifind_minute' else 'stock_orderbook_features'
                    with datasource._conn() as check:
                        saved=check.execute(f'SELECT 1 FROM {target} WHERE code=? AND trade_date=?',(code,day)).fetchone()
                    if not saved: raise ValueError('必要特征未实际入库')
            except Exception as exc:
                quality,reason='blocked','特征保存失败：'+str(exc)[:300]
        digest=_hash(json.loads(df.to_json(orient='records',double_precision=15)))
        with datasource._conn() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT revision FROM research_raw_revision WHERE code=? AND day=? AND source=?',(code,day,table)).fetchone() != rev: return 'changed_during_calculation'
            c.execute('INSERT OR REPLACE INTO research_day_archive VALUES(?,?,?,?,?,?,?,?,?,?,?)',(code,day,table,VERSION,rev[0],digest,quality,reason,json.dumps(features,allow_nan=False),len(df),now().isoformat()))
            if quality=='blocked':
                c.execute('INSERT OR REPLACE INTO research_retry VALUES(?,?,?,?)',(code,day,table,(now()+timedelta(hours=6)).isoformat()))
            else:
                c.execute('DELETE FROM research_retry WHERE code=? AND day=? AND source=?',(code,day,table))
        return quality


def _plan(c,code,start,end):
    p=policy(code,c);today=now().date().isoformat()
    days=[r[0] for r in c.execute("SELECT date FROM market_daily WHERE code=? AND source='ths_ifind' AND date<? ORDER BY date DESC",(code,today))]
    protected=c.execute('SELECT start,end FROM research_protection WHERE code=?',(code,)).fetchall()
    details=[]
    for table,col in RAW.items():
        keep=p['minute_days'] if table=='ifind_minute' else p['micro_days']
        cutoff=days[keep-1] if len(days)>=keep else '0000-00-00'
        latest=c.execute(f'SELECT MAX({col}) FROM {table} WHERE code=?',(code,)).fetchone()[0] if table in ('ifind_realtime','quote_snapshots') else None
        for day,count in c.execute(f'SELECT substr({col},1,10),COUNT(*) FROM {table} WHERE code=? AND {col}>=? AND {col}<? GROUP BY 1',(code,start,str(pd.Timestamp(end).date()+timedelta(days=1)))):
            reason=''
            archive=c.execute('''SELECT a.quality,a.reason,a.revision FROM research_day_archive a JOIN research_raw_revision r ON r.code=a.code AND r.day=a.day AND r.source=a.source AND r.revision=a.revision WHERE a.code=? AND a.day=? AND a.source=? AND a.version=?''',(code,day,table,VERSION)).fetchone()
            if day>=today or day>=cutoff: reason='保留期内或历史不足'
            elif latest and day==latest[:10]: reason='保留最新报价所在日'
            elif any(a<=day<=b for a,b in protected): reason='人工保护区间'
            elif not c.execute("SELECT 1 FROM market_daily WHERE code=? AND date=? AND source='ths_ifind' AND close>0",(code,day)).fetchone(): reason='基础日线缺失'
            elif not archive: reason='尚未生成当前版本特征或原始数据已变化'
            elif archive[0]!='ready': reason=archive[1]
            details.append(dict(table=table,day=day,rows=count,eligible=not reason,reason=reason,revision=archive[2] if archive else None))
    return details


def preview(code,start,end):
    if start>end or end>=now().date().isoformat(): raise ValueError('仅可清理已结束日期')
    setup()
    with datasource._conn() as c: rows=_plan(c,code,start,end)
    return dict(eligible={t:sum(r['rows'] for r in rows if r['table']==t and r['eligible']) for t in RAW},blocked_rows=sum(r['rows'] for r in rows if not r['eligible']),details=rows)


def prepare_cleanup(code,start,end,limit=40):
    """Archive pending raw days in the selected range; never delete or change policy."""
    plan=preview(code,start,end)
    counts={}
    pending=[]
    with datasource._conn() as c:
        for row in plan['details']:
            archive=c.execute('SELECT quality FROM research_day_archive WHERE code=? AND day=? AND source=? AND version=? AND revision=?',
                              (code,row['day'],row['table'],VERSION,row['revision'])).fetchone()
            if not archive or archive[0]!='ready':
                pending.append(row)
    for row in pending[:limit]:
        state=process_day(code,row['day'],row['table'])
        counts[state]=counts.get(state,0)+1
    return dict(processed=counts,remaining=max(0,len(pending)-limit))


def archive_and_cleanup(code,start,end,emit):
    """Visit each selected raw day once, then revalidate deletion in one transaction."""
    with stock_lock(code):
        plan=preview(code,start,end)
        rows=plan['details']
        counts={}
        for index,row in enumerate(rows):
            with datasource._conn() as c:
                ready=c.execute("SELECT 1 FROM research_day_archive a JOIN research_raw_revision r ON r.code=a.code AND r.day=a.day AND r.source=a.source AND r.revision=a.revision WHERE a.code=? AND a.day=? AND a.source=? AND a.version=? AND a.quality='ready'",
                                (code,row['day'],row['table'],VERSION)).fetchone()
            state='ready' if ready else process_day(code,row['day'],row['table'])
            counts[state]=counts.get(state,0)+1
            emit(5+int(85*(index+1)/max(1,len(rows))),f"归档 {index+1}/{len(rows)}：{row['day']} {row['table']} · {state}")
        emit(92,'归档结束，重新检查保留期、人工保护和数据版本后删除')
        result=cleanup(code,start,end)
        remaining=preview(code,start,end)
        reasons={}
        for row in remaining['details']:
            reason=row['reason'] or '数据已变化，待重新检查'
            reasons[reason]=reasons.get(reason,0)+row['rows']
        result.update(archive_counts=counts,remaining_rows=sum(row['rows'] for row in remaining['details']),remaining_reasons=reasons)
        with datasource._conn() as c:
            c.execute('UPDATE research_cleanup_log SET payload=? WHERE id=?',(json.dumps(result,ensure_ascii=False),result['id']))
        return result


def cleanup(code,start,end,report_id=None,automatic=False):
    preview(code,start,end)
    with stock_lock(code), datasource._conn() as c:
        c.execute('BEGIN IMMEDIATE')
        if automatic and not policy(code,c)['auto_cleanup']: raise ValueError('未启用自动清理')
        task=c.execute('SELECT status FROM minute_sync_tasks WHERE code=?',(code,)).fetchone()
        if task and task[0]=='running': raise ValueError('分钟同步任务正在运行')
        rows=_plan(c,code,start,end);deleted={t:0 for t in RAW}
        for r in rows:
            if r['eligible']:
                table=r['table'];col=RAW[table]
                deleted[table]+=c.execute(f'DELETE FROM {table} WHERE code=? AND {col}>=? AND {col}<?',(code,r['day'],str(pd.Timestamp(r['day']).date()+timedelta(days=1)))).rowcount
                rev=c.execute('SELECT revision FROM research_raw_revision WHERE code=? AND day=? AND source=?',(code,r['day'],table)).fetchone()[0]
                c.execute('INSERT INTO research_day_archive VALUES(?,?,?,?,?,?,?,?,?,?,?)',(code,r['day'],table,VERSION,rev,'','cleared','已按保留策略清理','{}',0,now().isoformat()))
        record=dict(id=uuid.uuid4().hex,code=code,created=now().isoformat(),deleted=deleted,start=start,end=end,report_id=report_id,automatic=automatic,evidence=[r for r in rows if r['eligible']],policy=policy(code,c),note='保留历史日线、成交约束、每日特征版本、研究快照和报告；原始路径无法从汇总重建。')
        c.execute('INSERT INTO research_cleanup_log VALUES(?,?,?,?)',(record['id'],code,record['created'],json.dumps(record,ensure_ascii=False)))
        if deleted['ifind_minute']:
            c.execute("UPDATE stock_history_jobs_v2 SET status='partial',row_count=(SELECT COUNT(*) FROM ifind_minute WHERE code=?),complete_days=0,missing_days=0 WHERE code=? AND data_type='minute_1m'",(code,code))
        return record


def storage():
    with datasource._conn() as c:
        page=c.execute('PRAGMA page_size').fetchone()[0];total=c.execute('PRAGMA page_count').fetchone()[0]*page;reusable=c.execute('PRAGMA freelist_count').fetchone()[0]*page
    path=Path(datasource.MKT_DB)
    return dict(database_bytes=total,reusable_bytes=reusable,wal_bytes=Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0)


def closed_before():
    current=now()
    return str(current.date()+timedelta(days=1)) if current.hour>=17 else str(current.date())


def refresh_labels(code):
    """Five exchange sessions; raw close diagnostic, not adjusted total return."""
    today=closed_before()
    with datasource._conn() as c:
        prices=dict(c.execute("SELECT date,close FROM market_daily WHERE code=? AND source='ths_ifind' AND date<? ORDER BY date",(code,today)))
        exchange='BSE' if code.startswith('BJ') else 'SZSE' if code.startswith('SZ') else 'SSE'
        calendar=[r[0] for r in c.execute('SELECT date FROM ifind_calendar WHERE exchange=? AND date<? ORDER BY date',(exchange,today))]
        days=[r[0] for r in c.execute("SELECT DISTINCT day FROM research_day_archive WHERE code=? AND quality='ready'",(code,))]
        receipts=c.execute('SELECT start,end,dates_json,digest FROM research_calendar_receipts WHERE exchange=?',(exchange,)).fetchall()
        pos={d:i for i,d in enumerate(calendar)}
        for day in days:
            idx=pos.get(day);end=calendar[idx+5] if idx is not None and idx+5<len(calendar) else None
            a,b=prices.get(day),prices.get(end)
            covered=any(start<=day and end is not None and stop>=end and _hash(json.loads(raw))==digest and [d for d in calendar if day<=d<=end]==[d for d in json.loads(raw) if day<=d<=end] for start,stop,raw,digest in receipts)
            valid=bool(covered and end and all(d in prices for d in calendar[idx:idx+6]) and a and b and math.isfinite(float(a)) and math.isfinite(float(b)) and a>0 and b>0)
            value=float(b/a-1) if valid else None
            state='mature_raw_close' if valid else 'waiting_calendar' if idx is None or (end and not covered) else 'waiting_data' if end else 'waiting_maturity'
            c.execute('INSERT OR REPLACE INTO research_labels VALUES(?,?,?,?,?,?,?,?)',(code,day,5,end,value,state,_hash([day,end,a,b]),now().isoformat()))


def search_candidates(code, expand=True):
    """Bounded expression search, diagnostics only, never global admission."""
    with datasource._conn() as c:
        rows=c.execute('''SELECT a.day,a.source,a.features,a.input_hash FROM research_day_archive a
            WHERE a.code=? AND a.version=? AND a.quality='ready' AND a.revision=(
             SELECT MAX(b.revision) FROM research_day_archive b WHERE b.code=a.code AND b.day=a.day
             AND b.source=a.source AND b.version=a.version AND b.quality!='cleared')
            AND NOT EXISTS (SELECT 1 FROM research_raw_revision r WHERE r.code=a.code AND r.day=a.day AND r.source=a.source AND r.revision!=a.revision AND NOT EXISTS (SELECT 1 FROM research_day_archive z WHERE z.code=r.code AND z.day=r.day AND z.source=r.source AND z.version=a.version AND z.revision=r.revision AND z.quality='cleared')) ORDER BY a.day,a.source''',(code,VERSION)).fetchall()
        labels=pd.read_sql_query("SELECT day,value,end_day FROM research_labels WHERE code=? AND status='mature_raw_close' ORDER BY day",c,params=(code,)).set_index('day')
    if len(labels)<100: return dict(status='waiting_samples',mature_days=len(labels))
    series={}
    for day,source,features,digest in rows:
        for name,value in json.loads(features).items(): series.setdefault(source+':'+name,{})[day]=value
    results=[];calendar=sorted(labels.index);split=int(len(calendar)*.7);test_start=calendar[split]
    for name,values in series.items():
        x=pd.Series(values).reindex(calendar)
        variants=[(name,x)]
        if expand:
            variants += [(f'mean5({name})',x.rolling(5).mean()),(f'diff5({name})',x.diff(5))]
        for expr,v in variants:
            frame=pd.DataFrame({'x':v,'y':labels.value}).replace([np.inf,-np.inf],np.nan)
            # Purge by label end date, not merely by available row count.
            train=frame.loc[(frame.index<test_start)&(labels.end_day<test_start)].dropna();test=frame.loc[frame.index>=test_start].dropna()
            if len(train)<40 or len(test)<30 or train.x.nunique()<2 or test.x.nunique()<2: continue
            train_ic=train.x.corr(train.y,method='spearman');test_ic=test.x.corr(test.y,method='spearman')
            if not math.isfinite(train_ic) or not math.isfinite(test_ic): continue
            results.append(dict(expression=expr,train_samples=len(train),test_samples=len(test),train_rank_ic=float(train_ic),holdout_rank_ic=float(test_ic),direction=1 if train_ic>=0 else -1))
    digest=_hash([code,VERSION,expand,rows,labels.reset_index().to_dict('records')])
    payload=dict(status='completed' if results else 'waiting_samples',test_start=test_start,candidates=sorted(results,key=lambda x:abs(x['train_rank_ic']),reverse=True),note='单股时序探索；原始收盘价标签未复权；5日标签重叠，不计算显著性或交易批准；重复搜索后的留出段不是独立最终测试。mean5/diff5以可用标签日期序列计算。')
    with datasource._conn() as c:
        c.execute('INSERT OR IGNORE INTO research_candidate_runs VALUES(?,?,?,?,?)',(uuid.uuid4().hex,code,now().isoformat(),digest,json.dumps(payload,ensure_ascii=False,allow_nan=False)))
    return dict(status=payload['status'],candidates=len(results),test_start=test_start)


def run_incremental(code=None,limit=40,search=False):
    setup();today=closed_before()
    with datasource._conn() as c:
        codes=[code] if code else [r[0] for r in c.execute("SELECT DISTINCT r.code FROM research_raw_revision r WHERE r.code GLOB '??[0-9][0-9][0-9][0-9][0-9][0-9]' ORDER BY r.code")]
    outcomes={}
    for symbol in codes:
        try:
            with stock_lock(symbol):
                with datasource._conn() as c:
                    pending=c.execute('''SELECT r.day,r.source FROM research_raw_revision r
                       LEFT JOIN research_day_archive a ON a.code=r.code AND a.day=r.day AND a.source=r.source AND a.revision=r.revision AND a.version=?
                       LEFT JOIN research_retry t ON t.code=r.code AND t.day=r.day AND t.source=r.source
                       WHERE r.code=? AND r.day<? AND (a.code IS NULL OR (a.quality='blocked' AND COALESCE(t.next_try,'')<=?))
                       ORDER BY CASE WHEN a.code IS NULL THEN 0 ELSE 1 END,r.day ASC LIMIT ?''',(VERSION,symbol,today,now().isoformat(),int(limit))).fetchall()
                counts={}
                for day,table in pending:
                    state=process_day(symbol,day,table);counts[state]=counts.get(state,0)+1
                    if state=='empty':
                        # Cleared raw revisions need a terminal marker, preserving prior features.
                        with datasource._conn() as c:
                            rev=c.execute('SELECT revision FROM research_raw_revision WHERE code=? AND day=? AND source=?',(symbol,day,table)).fetchone()[0]
                            c.execute('INSERT OR IGNORE INTO research_day_archive VALUES(?,?,?,?,?,?,?,?,?,?,?)',(symbol,day,table,VERSION,rev,'','missing_raw','原始数据缺失且无受控清理凭据','{}',0,now().isoformat()))
                refresh_labels(symbol)
                result=dict(processed=counts,search=search_candidates(symbol,expand=search))
                if policy(symbol)['auto_cleanup']: result['cleanup']=cleanup(symbol,'1900-01-01',str(now().date()-timedelta(days=1)),automatic=True)['deleted']
                outcomes[symbol]=result
        except Exception as exc: outcomes[symbol]=dict(error=str(exc))
        with datasource._conn() as c:
            c.execute('INSERT OR REPLACE INTO research_worker_status VALUES(?,?,?)',(symbol,now().isoformat(),json.dumps(outcomes[symbol],ensure_ascii=False)))
    return outcomes


def sync_calendar(exchange,start,end):
    """Receipt only from an explicit successful provider range request. No BSE fallback."""
    if exchange not in ('SSE','SZSE'):
        raise ValueError('尚未配置该交易所可信日历接口，不使用其他交易所替代')
    setup()
    df,_,err=datasource.ths_trade_dates(exchange,start,end)
    if err not in (0,None) or df is None or df.empty:
        raise ValueError(f'{exchange} 日历接口失败或为空：{err}')
    col=next((k for k in df.columns if 'date' in k.lower() or 'time' in k.lower()),None)
    if col is None: raise ValueError('日历缺少日期字段')
    dates=sorted(set(pd.to_datetime(df[col],errors='raise').dt.strftime('%Y-%m-%d')))
    if not dates or any(d<start or d>end for d in dates): raise ValueError('日历返回日期超出请求区间')
    with datasource._conn() as c:
        c.execute('BEGIN IMMEDIATE')
        # An omitted known session is a conflict, not permission to remove it.
        existing={r[0] for r in c.execute('SELECT date FROM ifind_calendar WHERE exchange=? AND date BETWEEN ? AND ?',(exchange,start,end))}
        if existing-set(dates): raise ValueError('接口日历遗漏本地已知交易日，需核实')
        c.executemany('INSERT OR IGNORE INTO ifind_calendar VALUES(?,?)',[(exchange,d) for d in dates])
        c.execute('INSERT OR REPLACE INTO research_calendar_receipts VALUES(?,?,?,?,?,?)',(exchange,start,end,json.dumps(dates),_hash(dates),now().isoformat()))
    return len(dates)


def sync_research_calendars():
    result={}
    with datasource._conn() as c:
        ranges=c.execute("SELECT substr(code,1,2),MIN(date) FROM market_daily WHERE source='ths_ifind' GROUP BY substr(code,1,2)").fetchall()
    for prefix,start in ranges:
        exchange={'SH':'SSE','SZ':'SZSE','BJ':'BSE'}.get(prefix)
        if not exchange:continue
        try:result[exchange]=sync_calendar(exchange,start,now().date().isoformat())
        except Exception as exc:result[exchange]={'error':str(exc)}
    return result
