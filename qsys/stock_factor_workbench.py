"""Local-only single-stock time-series diagnostics with retained input snapshots.

Not cross-sectional factor admission. No network calls or global registry writes.
"""
from contextlib import closing, contextmanager
from datetime import datetime, date
import fcntl
import hashlib
import json
import math
import re
import sqlite3
import uuid
import time

import numpy as np
import pandas as pd
from common import DATA_DIR
import datasource

DB = DATA_DIR / 'stock_factor_research.db'
SOURCES = {'日线量价': ['return_5', 'volatility_20', 'volume_ratio_20'],
           '分钟日内特征': ['tail_ret_30m', 'realized_vol', 'close_vwap_gap'],
           '盘口日内特征': ['ob_imbalance_mean', 'spread_median', 'seal_strength_close']}
from research_retention import RAW


def valid_code(code):
    if not re.fullmatch(r'(SH|SZ|BJ)\d{6}', code):
        raise ValueError('股票代码格式无效')


from research_retention import stock_lock


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''CREATE TABLE IF NOT EXISTS experiments(
       id TEXT PRIMARY KEY,code TEXT,source TEXT,created TEXT,payload TEXT);
       CREATE TABLE IF NOT EXISTS reports(
       id TEXT PRIMARY KEY,experiment_id TEXT,code TEXT,created TEXT,payload TEXT);
       CREATE TABLE IF NOT EXISTS cleanups(
       id TEXT PRIMARY KEY,code TEXT,created TEXT,payload TEXT);''')
    return c


def save(table, payload):
    with closing(connect()) as c, c:
        if table == 'experiments':
            c.execute('INSERT INTO experiments VALUES (?,?,?,?,?)', (payload['id'],payload['code'],payload['source'],payload['created'],json.dumps(payload,ensure_ascii=False,allow_nan=False)))
        elif table == 'reports':
            c.execute('INSERT INTO reports VALUES (?,?,?,?,?)', (payload['id'],payload['experiment_id'],payload['code'],payload['created'],json.dumps(payload,ensure_ascii=False,allow_nan=False)))
        else:
            raise ValueError('未知结果类型')


def list_results(code, table='experiments'):
    if table not in ('experiments','reports'): raise ValueError('未知结果类型')
    with closing(connect()) as c:
        return [json.loads(r[0]) for r in c.execute(f'SELECT payload FROM {table} WHERE code=? ORDER BY created DESC,rowid DESC LIMIT 30',(code,))]


def get_result(identifier, table):
    if table not in ('experiments','reports'): raise ValueError('未知结果类型')
    with closing(connect()) as c:
        row=c.execute(f'SELECT payload FROM {table} WHERE id=?',(identifier,)).fetchone()
    if row is None: raise ValueError('结果不存在')
    return json.loads(row[0])


def load_inputs(code, start, end, source, progress=None):
    emit = progress or (lambda percent, message: None)
    if source not in SOURCES: raise ValueError('不支持的数据类型')
    if start > end or end >= date.today().isoformat():
        raise ValueError('日期范围无效；只使用已结束的历史日期')
    if source != '日线量价':
        # Bounded memory: aggregate at most seven days of raw data per call.
        a=pd.Timestamp(start)
        total_batches=((pd.Timestamp(end)-a).days//7)+1
        batch=0
        while a <= pd.Timestamp(end):
            b=min(a+pd.Timedelta(days=6),pd.Timestamp(end))
            if source == '分钟日内特征':
                datasource.compute_intraday_features(code,str(a.date()),str(b.date()))
            else:
                datasource.compute_orderbook_features(code,str(a.date()),str(b.date()))
            batch+=1
            emit(5+int(40*batch/total_batches),f'特征汇总 {batch}/{total_batches} 批：{a.date()}～{b.date()}')
            a=b+pd.Timedelta(days=1)
    emit(45, '读取日线与历史特征')
    with datasource._conn() as c:
        d=pd.read_sql("SELECT date,open,high,low,close,volume,amount FROM market_daily WHERE source='ths_ifind' AND code=? AND date BETWEEN ? AND ? ORDER BY date",c,params=(code,start,end))
        if source != '日线量价':
            table='stock_intraday_features' if source=='分钟日内特征' else 'stock_orderbook_features'
            cols=SOURCES[source]
            features=pd.read_sql(f'SELECT trade_date AS date,{",".join(cols)} FROM {table} WHERE code=? AND trade_date BETWEEN ? AND ?',c,params=(code,start,end))
    if len(d)<100: raise ValueError('至少需要100个日线交易日；分钟/盘口特征也需足够的历史日期')
    if d.date.duplicated().any(): raise ValueError('日线日期重复')
    d=d.set_index('date')
    if source=='日线量价':
        d['return_5']=d.close.pct_change(5,fill_method=None)
        d['volatility_20']=d.close.pct_change(fill_method=None).rolling(20).std()
        d['volume_ratio_20']=d.volume/d.volume.rolling(20).mean().replace(0,np.nan)-1
    else:
        d=d.join(features.set_index('date'),how='left')
    emit(55, f'输入校验与特征计算完成：{len(d)} 个日线日期')
    # Snapshot exact history; missing values remain missing, never zero-filled.
    return d.replace([np.inf,-np.inf],np.nan)


def mine(code,start,end,source='日线量价',progress=None):
    started=time.monotonic()
    history=[]
    def emit(percent,message):
        history.append(dict(percent=percent,message=message,elapsed_seconds=round(time.monotonic()-started,3)))
        if progress: progress(percent,message)
    emit(0,'准备研究，检查股票任务锁')
    with stock_lock(code):
        emit(5,f'读取本地数据：{source}，{start}～{end}')
        d=load_inputs(code,start,end,source,progress=emit)
        split=int(len(d)*.7)
        if len(d)-split<30: raise ValueError('独立留出段不足30个交易日')
        # Five-day target must mature strictly before the split.
        forward=d.close.shift(-5)/d.close-1
        emit(60,f'时间切分完成：训练 {split} 日，留出 {len(d)-split} 日；留出起点 {d.index[split]}')
        candidates=[]
        for index,name in enumerate(SOURCES[source]):
            emit(60+index*10,f'评估候选 {index+1}/{len(SOURCES[source])}：{name}')
            train=pd.concat([d[name],forward.rename('future')],axis=1).iloc[:split-5].dropna()
            if len(train)<40 or train[name].nunique()<2:
                candidates.append(dict(name=name,status='sample_insufficient',samples=len(train)))
                continue
            ic=float(train[name].corr(train.future,method='spearman'))
            if not math.isfinite(ic):
                candidates.append(dict(name=name,status='sample_insufficient',samples=len(train)))
                continue
            direction=1 if ic>=0 else -1
            candidates.append(dict(name=name,status='research_candidate',samples=len(train),train_rank_ic=ic,
                                   direction=direction,threshold=float((train[name]*direction).median())))
        if not any(r['status']=='research_candidate' for r in candidates):
            raise ValueError('当前区间没有可用候选：特征至少需要40个有效训练日期；不以日线冒充盘口或分钟数据')
        emit(90,'候选评估完成，生成输入快照')
        snapshot=json.loads(d.reset_index().to_json(orient='records',double_precision=15))
        result=dict(id=uuid.uuid4().hex,code=code,source=source,created=datetime.now().isoformat(timespec='seconds'),
                    start=d.index[0],end=d.index[-1],split=split,test_start=d.index[split],
                    candidates=candidates,inputs=snapshot,
                    input_hash=hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest(),
                    contract='single-stock-timeseries-v1',
                    note='固定3个特征候选，训练段确定方向与中位数阈值；单股时序研究，不是全池IC验证或交易批准。反复查看留出结果会使其不再独立。')
        emit(95,'正在保存研究批次和输入快照')
        result['progress_log']=list(history)
        result['research_type']='fixed-feature-evaluation'
        save('experiments',result)
        emit(100,f'研究完成：候选 {len(candidates)} 个，批次 {result["id"]} 已保存')
        return result


def backtest(experiment_id,candidate_name):
    experiment=get_result(experiment_id,'experiments');code=experiment['code']
    with stock_lock(code):
        candidates=[c for c in experiment['candidates'] if c['name']==candidate_name and c['status']=='research_candidate']
        if not candidates: raise ValueError('该候选不可回测')
        candidate=candidates[0];d=pd.DataFrame(experiment['inputs']).set_index('date')
        test=d.iloc[experiment['split']:].copy()
        dates=test.index.tolist();signals=[]
        for day in dates[:-1:5]:
            value=test.loc[day,candidate_name]
            if value is None or not pd.notna(value) or not math.isfinite(float(value)):
                raise ValueError(f'{day} 因子值缺失，无法完成此区间回测；请补数据或选择其他区间')
            signals.append(dict(date=day,code=code,target_weight=1. if float(value)*candidate['direction']>candidate['threshold'] else 0.))
        from historical_execution import simulate,performance
        from execution_constraints import attach
        prices=test[['open','high','low','close','volume','amount']].copy()
        prices['code']=code;prices=prices.reset_index().set_index(['date','code'])
        prices=attach(prices,'ths_ifind')
        if experiment.get('research_type')=='expression-search':
            if prices[['suspended','limit_up','limit_down']].isna().any().any():
                raise ValueError('成交约束不完整：请在回测页面查看缺失日期并点击“从同花顺补齐本股票回测约束”后重试')
            from single_stock_jobs import evaluate
            rebuilt=evaluate(candidate['expression'],d).replace([np.inf,-np.inf],np.nan)
            stored=pd.to_numeric(d[candidate_name],errors='coerce')
            if not np.allclose(rebuilt.to_numpy(),stored.to_numpy(),equal_nan=True,rtol=1e-10,atol=1e-12):
                raise ValueError('因子表达式与保存值复算不一致，禁止回测')

        ledger=simulate(pd.DataFrame(signals),prices)
        metrics=performance(ledger)
        # Missing constraint coverage remains visible even if the strategy made no orders.
        complete=not ledger['data_issues'] and not ledger['constraints_missing']
        result=dict(id=uuid.uuid4().hex,experiment_id=experiment_id,code=code,created=datetime.now().isoformat(timespec='seconds'),
                    ok=True,mode='execution',strategy=f'{code} / {candidate_name}',pool='单股时序诊断',
                    source=experiment['source'],candidate=candidate,metrics=metrics,**metrics,
                    period=f'{dates[0]} ~ {dates[-1]}',trades=len(ledger['fills']),
                    data_quality_status='complete_for_supplied_fields' if complete else 'incomplete',
                    execution={k:v.to_dict('records') if isinstance(v,pd.DataFrame) else v for k,v in ledger.items()},
                    execution_limitations=ledger['limitations'],inputs_used=experiment['input_hash'],
                    prices_snapshot=json.loads(prices.reset_index().to_json(orient='records',double_precision=15)),
                    signals=signals,note=experiment['note'])
        save('reports',result)
        return result


def cleanup_preview(code,start,end,include_minutes=False):
    from research_retention import preview
    return preview(code,start,end)['eligible']


def cleanup(code,start,end,report_id=None,include_minutes=False):
    from research_retention import cleanup as guarded_cleanup
    if report_id:
        report=get_result(report_id,'reports')
        if report['code']!=code:
            raise ValueError('报告与股票不匹配')
    return guarded_cleanup(code,start,end,report_id=report_id)


def review_snapshot(experiment_id):
    """Replay a saved snapshot with calendar-verified labels; no raw writes."""
    from research_retention import setup, _hash
    setup()
    exp=get_result(experiment_id,'experiments')
    d=pd.DataFrame(exp['inputs']).set_index('date')
    exchange='SZSE' if exp['code'].startswith('SZ') else 'SSE' if exp['code'].startswith('SH') else 'BSE'
    with datasource._conn() as c:
        calendar=[r[0] for r in c.execute('SELECT date FROM ifind_calendar WHERE exchange=? ORDER BY date',(exchange,))]
        receipts=c.execute('SELECT start,end,dates_json,digest FROM research_calendar_receipts WHERE exchange=?',(exchange,)).fetchall()
    pos={day:i for i,day in enumerate(calendar)}
    labels={};ends={}
    for day in d.index:
        i=pos.get(day)
        if i is None or i+5>=len(calendar):continue
        stop=calendar[i+5]
        verified=any(a<=day and b>=stop and _hash(json.loads(raw))==digest and calendar[i:i+6]==[v for v in json.loads(raw) if day<=v<=stop] for a,b,raw,digest in receipts)
        if not verified or not all(t in d.index for t in calendar[i:i+6]):continue
        prices=pd.to_numeric(d.loc[calendar[i:i+6],'close'],errors='coerce')
        if not np.isfinite(prices).all() or (prices<=0).any():continue
        labels[day]=float(prices.iloc[-1]/prices.iloc[0]-1);ends[day]=stop
    rows=[]
    for candidate in exp['candidates']:
        name=candidate['name'];frame=pd.DataFrame({'factor':d[name],'future':pd.Series(labels),'end':pd.Series(ends)})
        train=frame[(frame.index<exp['test_start']) & (frame.end<exp['test_start'])].dropna()
        test=frame[frame.index>=exp['test_start']].dropna()
        def ic(part):
            if len(part)<30 or part.factor.nunique()<2 or part.future.nunique()<2:return None
            value=float(part.factor.corr(part.future,method='spearman'))
            return value if math.isfinite(value) else None
        rows.append(dict(name=name,train_samples=len(train),holdout_samples=len(test),train_rank_ic=ic(train),holdout_rank_ic=ic(test),direction=candidate.get('direction'),status='reviewed' if ic(train) is not None and ic(test) is not None else 'sample_insufficient'))
    report=dict(experiment_id=exp['id'],code=exp['code'],source=exp['source'],input_hash=exp['input_hash'],start=exp['start'],end=exp['end'],test_start=exp['test_start'],candidates=rows,contract='snapshot-review-v1',note='单股时序诊断；未复权收盘价5交易日收益；留出段已反复使用，不是独立盲测；非成交收益。分钟特征可能来自旧版汇总，未视为全部通过新版原始数据校验。')
    with closing(connect()) as c,c:
        c.execute('CREATE TABLE IF NOT EXISTS snapshot_reviews(experiment_id TEXT PRIMARY KEY,payload TEXT)')
        c.execute('INSERT OR REPLACE INTO snapshot_reviews VALUES(?,?)',(experiment_id,json.dumps(report,ensure_ascii=False,allow_nan=False)))
    return report
