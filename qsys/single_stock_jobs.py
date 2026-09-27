"""Persistent manually requested single-stock expression search. No global admission."""
import json,uuid,hashlib,random,math,time,fcntl
from contextlib import closing
from datetime import datetime
import pandas as pd
import numpy as np
import stock_factor_workbench as w
import datasource
import research_retention as rr
VERSION='single-expression-v1'


def connect():
    c=w.connect()
    c.executescript('''CREATE TABLE IF NOT EXISTS single_jobs(id TEXT PRIMARY KEY,code TEXT,kind TEXT,status TEXT,params TEXT,progress INTEGER,message TEXT,result_id TEXT,created TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS single_active_stock ON single_jobs(code) WHERE status IN ('queued','running');''')
    return c


def submit(code,kind,params):
    w.valid_code(code)
    if kind not in ('mine','backtest','constraints','cleanup'):raise ValueError('未知任务')
    if kind=='mine':
        if params.get('source') not in w.SOURCES:raise ValueError('未知来源')
        if not 10<=int(params.get('budget',60))<=300:raise ValueError('候选预算须为10～300')
        if params['start']>params['end'] or params['end']>=rr.now().date().isoformat():raise ValueError('仅使用已结束日期')
    elif kind=='cleanup':
        rr.preview(code,params['start'],params['end'])
    else:
        exp=w.get_result(params['experiment_id'],'experiments')
        if exp['code']!=code:raise ValueError('股票与研究批次不一致')
        if kind=='backtest' and params['candidate'] not in [c['name'] for c in exp['candidates'] if c['status']=='research_candidate']:raise ValueError('候选不可回测')
    with closing(connect()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        existing=c.execute("SELECT id FROM single_jobs WHERE code=? AND status IN ('queued','running')",(code,)).fetchone()
        if existing:return existing[0]
        rid=uuid.uuid4().hex
        c.execute('INSERT INTO single_jobs VALUES(?,?,?,?,?,?,?,?,?)',(rid,code,kind,'queued',json.dumps(params),'0','已排队，等待独立工作进程',None,datetime.now().isoformat()))
    return rid


def jobs(code):
    with closing(connect()) as c:
        c.row_factory=__import__('sqlite3').Row
        return [dict(r) for r in c.execute('SELECT * FROM single_jobs WHERE code=? ORDER BY created DESC LIMIT 10',(code,))]


def evaluate(expr,d):
    op=expr[0]
    if op=='field':return pd.to_numeric(d[expr[1]],errors='coerce')
    x=evaluate(expr[1],d)
    if op=='mean':return x.rolling(expr[2],min_periods=expr[2]).mean()
    if op=='delta':return x.diff(expr[2])
    y=evaluate(expr[2],d)
    if op=='add':return x+y
    if op=='sub':return x-y
    if op=='mul':return x*y
    if op=='div':return x/y.where(y.abs()>1e-12)
    raise ValueError('未知运算')


def mine(code,p,emit,identifier):
    rr.setup()
    with rr.stock_lock(code):
        emit(5,'预检：读取日线与所选来源；不自动补抓')
        source=p['source'];start=p['start'];end=p['end']
        with datasource._conn() as c:
            d=pd.read_sql_query("SELECT date,open,high,low,close,volume,amount FROM market_daily WHERE code=? AND source='ths_ifind' AND date BETWEEN ? AND ? ORDER BY date",c,params=(code,start,end)).set_index('date')
            exchange='SSE' if code.startswith('SH') else 'SZSE' if code.startswith('SZ') else 'BSE'
            cal=[r[0] for r in c.execute('SELECT date FROM ifind_calendar WHERE exchange=? AND date BETWEEN ? AND ? ORDER BY date',(exchange,start,end))]
            receipts=c.execute('SELECT start,end,dates_json,digest FROM research_calendar_receipts WHERE exchange=?',(exchange,)).fetchall()
        if len(d)<180:raise ValueError('至少需要180个有效日线日期用于训练、验证和最终留出')
        a,b=d.index[0],d.index[-1]
        expected=[day for day in cal if a<=day<=b]
        if not any(lo<=a and hi>=b and rr._hash(json.loads(raw))==digest and expected==[t for t in json.loads(raw) if a<=t<=b] for lo,hi,raw,digest in receipts):raise ValueError('研究区间缺少完整可信交易日历，请先补齐日历')
        if list(d.index)!=expected:raise ValueError('日线存在缺失交易日；需补齐或选择连续区间')
        numeric=d.apply(pd.to_numeric,errors='coerce')
        if not np.isfinite(numeric.to_numpy()).all() or (numeric[['open','high','low','close']]<=0).any().any() or (numeric[['volume','amount']]<0).any().any():raise ValueError('日线存在无效价格或成交量额')
        if ((numeric.high<numeric[['open','close','low']].max(axis=1)) | (numeric.low>numeric[['open','close','high']].min(axis=1))).any():raise ValueError('日线OHLC关系异常')
        d=numeric
        if source=='日线量价':
            d['return_5']=d.close.pct_change(5,fill_method=None)
            d['volatility_20']=d.close.pct_change(fill_method=None).rolling(20).std()
            d['volume_ratio_20']=d.volume/d.volume.rolling(20).mean().replace(0,np.nan)-1
            provenance='daily-snapshot'
        else:
            table='ifind_minute' if source=='分钟日内特征' else 'ifind_realtime'
            with datasource._conn() as c:
                rows=c.execute('''SELECT a.day,a.features,a.input_hash,a.revision FROM research_day_archive a WHERE a.code=? AND a.source=? AND a.version=? AND a.quality='ready' AND a.day BETWEEN ? AND ? ORDER BY a.day,a.revision''',(code,table,rr.VERSION,start,end)).fetchall()
                revisions=dict(c.execute('SELECT day,revision FROM research_raw_revision WHERE code=? AND source=?',(code,table)))
                cleared={(r[0],r[1]) for r in c.execute("SELECT day,revision FROM research_day_archive WHERE code=? AND source=? AND version=? AND quality='cleared'",(code,table,rr.VERSION))}
            valid={}
            for day,features,digest,rev in rows:
                current=revisions.get(day)
                if current==rev or (day,current) in cleared:valid[day]=(json.loads(features),digest,rev)
            for field in w.SOURCES[source]:d[field]=pd.Series({day:r[0].get(field) for day,r in valid.items()})
            provenance={day:dict(hash=r[1],revision=r[2],version=rr.VERSION) for day,r in valid.items()}
            if d[w.SOURCES[source]].notna().all(axis=1).sum()<150:raise ValueError('所选来源通过版本化归档的特征不足150日；请先校验归档原始数据，不使用未认证旧汇总代替')
        fields=w.SOURCES[source];n=len(d);train_stop=int(n*.5);test_start=int(n*.75)
        future=d.close.shift(-5)/d.close-1
        rng=random.Random(int(p.get('seed',42)));seen=set();trials=[];values={};budget=int(p.get('budget',60))
        def leaf():return ['field',rng.choice(fields)]
        def tree(depth=2):
            if depth==0:return leaf()
            op=rng.choice(['mean','delta','add','sub','mul','div'])
            return [op,tree(depth-1),rng.choice([3,5,10,20])] if op in ('mean','delta') else [op,tree(depth-1),leaf()]
        emit(15,f'预检通过：{n}日；开始搜索{budget}个不同表达式')
        for attempt in range(budget*20):
            if len(trials)>=budget:break
            expr=tree();key=json.dumps(expr,separators=(',',':'))
            if key in seen:continue
            seen.add(key);name='ss_'+hashlib.sha256((VERSION+key).encode()).hexdigest()[:16]
            row=dict(name=name,expression=expr,status='rejected')
            try:
                x=evaluate(expr,d).replace([np.inf,-np.inf],np.nan)
                frame=pd.DataFrame({'x':x,'y':future})
                train=frame.iloc[:train_stop-5].dropna();valid=frame.iloc[train_stop:test_start-5].dropna()
                row.update(train_samples=len(train),validation_samples=len(valid))
                if len(train)<40 or len(valid)<30 or train.x.nunique()<2 or valid.x.nunique()<2:raise ValueError('训练或验证样本不足/常数因子')
                ic=float(train.x.corr(train.y,method='spearman'));vic=float(valid.x.corr(valid.y,method='spearman'))
                if not math.isfinite(ic) or not math.isfinite(vic):raise ValueError('相关性无效')
                direction=1 if ic>=0 else -1
                row.update(train_rank_ic=ic,validation_rank_ic=vic,direction=direction,threshold=float((train.x*direction).median()))
                if abs(ic)<.05 or direction*vic<.02:raise ValueError('未通过训练/验证探索门槛')
                row['status']='research_candidate';values[name]=x
            except ValueError as exc:row['reason']=str(exc)
            trials.append(row);emit(15+int(70*len(trials)/budget),f"已评估 {len(trials)}/{budget}；通过 {len(values)}；当前 {name}")
        candidates=sorted([t for t in trials if t['status']=='research_candidate'],key=lambda t:abs(t['validation_rank_ic']),reverse=True)[:5]
        for row in candidates:d[row['name']]=values[row['name']]
        snapshot=json.loads(d.reset_index().to_json(orient='records',double_precision=15))
        result=dict(id=identifier,code=code,source=source,created=datetime.now().isoformat(),start=a,end=b,split=test_start,test_start=d.index[test_start],candidates=candidates,inputs=snapshot,feature_version=rr.VERSION if source!='日线量价' else VERSION,calendar_receipts=[dict(start=lo,end=hi,digest=digest) for lo,hi,raw,digest in receipts if lo<=a and hi>=b],input_hash=rr._hash(snapshot),contract=VERSION,research_type='expression-search',trials=trials,seed=int(p.get('seed',42)),budget=budget,feature_provenance=provenance,train_end=d.index[train_stop-1],validation_end=d.index[test_start-1],note='单股日频表达式探索；50%训练/25%验证/25%最终留出，5日标签边界净化；最终留出未参与选优。未复权收益、重复搜索存在选优偏差；仅供研究，不是交易批准。保存快照不自动授权清理。')
        emit(90,'保存表达式、全部搜索尝试、特征版本及因子值快照')
        w.save('experiments',result)
        return result


def run_once():
    with closing(connect()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT id,code,kind,params FROM single_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:return False
        rid,code,kind,raw=row
        c.execute("UPDATE single_jobs SET status='running',message='开始预检' WHERE id=?",(rid,))
    def emit(percent,message):
        with closing(connect()) as c,c:c.execute('UPDATE single_jobs SET progress=?,message=? WHERE id=?',(percent,message,rid))
    try:
        p=json.loads(raw)
        if kind=='mine':result=mine(code,p,emit,rid)
        elif kind=='cleanup':
            result=rr.archive_and_cleanup(code,p['start'],p['end'],emit)
        elif kind=='constraints':
            import execution_constraints as ec
            exp=w.get_result(p['experiment_id'],'experiments')
            if exp['code']!=code:raise ValueError('股票不一致')
            dates=[r['date'] for r in exp['inputs']][exp['split']:]
            repaired=ec.repair_missing(code,dates,emit)
            result=dict(id=rid)
            with closing(connect()) as c,c:
                c.execute('CREATE TABLE IF NOT EXISTS single_constraint_repairs(id TEXT PRIMARY KEY,payload TEXT)')
                c.execute('INSERT INTO single_constraint_repairs VALUES(?,?)',(rid,json.dumps(repaired,ensure_ascii=False)))
            if not repaired['after']['complete']:
                raise ValueError(f"补抓结束但仍缺 {repaired['after']['missing_fields']} 个日期/字段；接口空值或异常仍保留，未伪造；明细已保存 {rid}")
        else:
            emit(10,'预检保存的因子版本与输入快照')
            exp=w.get_result(p['experiment_id'],'experiments')
            if exp['code']!=code:raise ValueError('股票不一致')
            emit(30,'按固定方向与阈值执行留出段回测')
            result=w.backtest(p['experiment_id'],p['candidate'])
        message=(f"归档清理完成：实际删除 {sum(result['deleted'].values())} 行；保留 {result['remaining_rows']} 行；原因：{result['remaining_reasons']}" if kind=='cleanup' else '已完成；请检查结果与数据质量')
        with closing(connect()) as c,c:c.execute("UPDATE single_jobs SET status='completed',progress=100,message=?,result_id=? WHERE id=?",(message,result['id'],rid))
    except Exception as exc:
        with closing(connect()) as c,c:c.execute("UPDATE single_jobs SET status='failed',message=? WHERE id=?",(str(exc),rid))
    return True


def worker():
    w.DATA_DIR.mkdir(parents=True,exist_ok=True)
    with (w.DATA_DIR/'single_stock_worker.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX)
            # wait for any previous worker to release the shared lock
        except BlockingIOError:return
        # An exclusive worker lock proves previous in-flight jobs were interrupted.
        with closing(connect()) as c,c:
            c.execute("UPDATE single_jobs SET status='failed',message='工作进程中断，请检查已保存结果后重新提交' WHERE status='running'")
        while True:
            try:
                if not run_once():time.sleep(2)
            except Exception:time.sleep(5)

if __name__=='__main__':worker()
