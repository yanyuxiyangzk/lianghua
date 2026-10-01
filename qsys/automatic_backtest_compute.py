"""Deterministic local historical diagnostics, separate from trade approval."""
from contextlib import closing
import json
import math
import numpy as np
import pandas as pd
import automatic_backtest as queue


class WaitingData(ValueError):
    pass


def records(frame):
    return json.loads(frame.to_json(orient='records', date_format='iso'))


def summarize(daily):
    if daily.empty: return dict(days=0, mean_rank_ic=None, mean_top_excess=None)
    good = daily.dropna(subset=['rank_ic'])
    value = lambda x: float(x) if pd.notna(x) and math.isfinite(float(x)) else None
    return dict(days=len(good),mean_rank_ic=value(good.rank_ic.mean()),
                mean_top_excess=value(good.top_excess.mean()),
                mean_bottom_excess=value(good.bottom_excess.mean()),
                mean_coverage=value(daily.coverage.mean()))


def historical_stats(values, close, horizons=(1,5,10,20), minimum=30):
    """No filling, no net-value compounding of overlapping forward labels.

    Thresholds/directions are not selected from future returns. Both tails
    are retained; this report does not turn the profitable tail into a strategy.
    """
    values = values.reindex(index=close.index, columns=close.columns).replace([np.inf,-np.inf],np.nan)
    frames, summaries, periods = [], {}, []
    for horizon in horizons:
        complete = close.rolling(horizon+1).count().eq(horizon+1).shift(-horizon,fill_value=False)
        future = (close.shift(-horizon)/close-1).where(complete)
        rows = []
        for pos, day in enumerate(close.index):
            if pos+horizon >= len(close): continue  # immature outcomes remain pending
            x = values.loc[day].dropna()
            x = x[close.loc[day].reindex(x.index).gt(0)]
            row = dict(date=str(day.date()),horizon=horizon,signals=len(x),matched=0,
                       coverage=len(x)/len(close.columns) if len(close.columns) else 0.,
                       rank_ic=None,top_excess=None,bottom_excess=None,status='insufficient')
            if len(x) >= minimum and x.nunique() > 1:
                y = future.loc[day].reindex(x.index)
                row['matched'] = int(y.notna().sum())
                if y.notna().all():
                    rank = x.sort_index().sort_values(kind='stable')
                    k = max(1,len(rank)//5)
                    ic = x.corr(y,method='spearman')
                    row.update(rank_ic=float(ic) if pd.notna(ic) else None,
                               top_excess=float(y.loc[rank.index[-k:]].mean()-y.mean()),
                               bottom_excess=float(y.loc[rank.index[:k]].mean()-y.mean()),
                               status='valid' if pd.notna(ic) else 'constant_outcome')
                else:
                    # Do not replace the stocks whose forward outcomes are missing.
                    row['status']='missing_forward_prices'
            rows.append(row)
        daily = pd.DataFrame(rows,columns=['date','horizon','signals','matched','coverage','rank_ic','top_excess','bottom_excess','status'])
        frames.append(daily)
        summaries[str(horizon)] = {**summarize(daily), 'immature_dates':min(horizon,len(close)),
                                  'unusable_dates':int(daily.rank_ic.isna().sum())}
        if not daily.empty:
            dates=pd.to_datetime(daily.date)
            for label, groups in [('year',dates.dt.year.astype(str)),('quarter',dates.dt.to_period('Q').astype(str))]:
                for period, g in daily.groupby(groups):
                    periods.append(dict(period=period,kind=label,horizon=horizon,**summarize(g)))
    return dict(summary=summaries,periods=periods,daily=records(pd.concat(frames,ignore_index=True)))


def load_daily(task):
    import datasource
    from trading_calendar import calendar_data, covers_interval
    from single_stock_jobs import validated_daily
    codes = task['codes']
    if not codes: raise WaitingData('股票池为空，未改用其他股票池')
    with closing(queue.read_db(datasource.MKT_DB)) as c:
        parts=[]
        for pos in range(0,len(codes),300):
            chunk=codes[pos:pos+300]
            parts.append(pd.read_sql_query("SELECT code,date,open,high,low,close,volume,amount FROM market_daily WHERE source='ths_ifind' AND date BETWEEN ? AND ? AND code IN ("+','.join('?'*len(chunk))+") ORDER BY code,date",c,params=(task['start'],task['end'],*chunk)))
    raw=pd.concat(parts,ignore_index=True)
    if raw.empty: raise WaitingData('本地历史日线为空；没有发起网络补抓')
    panels, rejected, qualities = [], [], []
    calendars={x:calendar_data(x) for x in ('SSE','SZSE','BSE')}
    groups={code:g for code,g in raw.groupby('code')}
    for code in codes:
        d=groups.get(code)
        if d is None:
            rejected.append(dict(code=code,reason='本地日线缺失')); continue
        d=d.drop(columns='code').set_index('date').sort_index()
        start,end=d.index[0],task['end']
        exchange='SSE' if code.startswith('SH') else 'SZSE' if code.startswith('SZ') else 'BSE'
        days,receipts=calendars[exchange]
        expected=sorted(day for day in days if start<=day<=end)
        try:
            if not expected or not covers_interval(start,end,receipts):
                raise WaitingData(f'{start}～{end} 缺少完整可信交易日历')
            clean,quality=validated_daily(code,d,expected)
            clean.index=pd.to_datetime(clean.index)
            clean.index.name='datetime'
            clean['instrument']=code
            panels.append(clean.reset_index().set_index(['instrument','datetime']).rename(columns=lambda x:'$'+x))
            qualities.append(dict(code=code,**quality))
        except ValueError as exc:
            rejected.append(dict(code=code,reason=str(exc)))
    if not panels: raise WaitingData('全部股票数据预检未通过：'+queue.dumps(rejected[:8]))
    panel=pd.concat(panels).sort_index()
    return panel,dict(requested_stocks=len(codes),accepted_stocks=len(panels),rejected=rejected,
                      stocks=qualities,input_hash=queue.digest(records(raw)))


def factor_values(task,panel):
    import signals as sig
    from loopengine.tree import parse,evaluate_tree,required_observations,build_field_frames,Leaf,FIELDS
    f=task['factor']; code=f.get('code') or ''; name=f['name']
    if code.startswith('# sexpr: '):
        tree=parse(code.splitlines()[0][9:],'任意')
        if tree is None: raise ValueError('表达式解析失败')
        def fields(node):
            if isinstance(node,Leaf):
                try: float(node.field); return set()
                except ValueError: return {node.field}
            return set().union(*(fields(c) for c in node.children))
        leaves=fields(tree)
        base=build_field_frames(panel)
        # Per-stock shadows must use that stock's open/close, not a cross-stock max.
        base['upper_shadow']=(base['high']-base['open'].where(base['open']>=base['close'],base['close']))/base['close'].shift(1)
        base['lower_shadow']=(base['open'].where(base['open']<=base['close'],base['close'])-base['low'])/base['close'].shift(1)
        extras=leaves-set(FIELDS)
        if extras:
            from loopengine.extra_frames import frames_with_extras_for
            days=(pd.Timestamp(task['end'])-pd.Timestamp(task['start'])).days
            frames=frames_with_extras_for(code.splitlines()[0][9:],panel,task['codes'],task['end'],int(days/1.6)+2)
            base.update({k:v for k,v in frames.items() if k not in FIELDS})
            # Fund flow builders historically fill missing fields with zero. Load
            # actual fields below, preserving their missingness for this evaluator.
            from loopengine.tree import TYPE_FIELDS
            if extras & set(TYPE_FIELDS['资金流']):
                base.update(strict_fund_frames(task,panel))
            missing=[k for k in extras if k not in base]
            if missing: raise WaitingData('缺少该表达式所需历史字段：'+','.join(sorted(missing)))
        for k,v in list(base.items()):
            if isinstance(v,pd.DataFrame):
                v=v.copy();v.index=pd.to_datetime(v.index)
                base[k]=v.reindex(index=base['close'].index,columns=base['close'].columns)
        for field in leaves:
            if field in base and not np.isfinite(base[field].to_numpy()).any():
                raise WaitingData('历史字段无有效值：'+field)
        out=evaluate_tree(tree,base)
        if not isinstance(out,pd.DataFrame): raise ValueError('因子输出不是日期×股票矩阵')
        return out, required_observations(tree), sorted(leaves)
    if f.get('kind')=='builtin':
        s=sig.compute_builtin(panel,name)
        return s.unstack('instrument'),1,[name]
    if f.get('kind')=='tech':
        s=sig.compute_common(panel,name) if name in sig.CATALOG_NAMES else sig.compute_tech(panel,name)
        return s.unstack('instrument'),1,[name]
    raise NotImplementedError('当前自动历史引擎支持内置/技术/标准表达式；任意Python因子需完成本地数据与时点适配')


def strict_fund_frames(task,panel):
    import datasource
    with closing(queue.read_db(datasource.MKT_DB)) as c:
        try:
            raw=pd.read_sql_query('SELECT code,date,main_net,super_net,big_net,mid_net,small_net FROM stock_fundflow_daily WHERE date BETWEEN ? AND ?',c,params=(task['start'],task['end']))
        except Exception as exc: raise WaitingData('资金历史字段缺失：'+str(exc))
    if raw.empty: raise WaitingData('资金流历史为空')
    raw=raw[raw.code.isin(task['codes'])];raw['date']=pd.to_datetime(raw.date)
    def pivot(col): return raw.pivot(index='date',columns='code',values=col)
    main=pivot('main_net'); out={'main_net_inflow':main}
    out['net_inflow_ratio']=main.div(main.abs().sum(axis=1,min_count=1).replace(0,np.nan),axis=0)
    out['main_small_spread']=main-main.rolling(20,min_periods=5).mean()
    amount=panel['$amount'].unstack('instrument').replace(0,np.nan)
    for col,label in [('main_net','main_net_pct'),('super_net','super_net_pct'),('big_net','big_net_pct'),('mid_net','mid_net_pct'),('small_net','small_net_pct')]:
        out[label]=pivot(col).div(amount)*100
    return out


def compute(task):
    window=task.get('data_window',{})
    if window.get('error'):
        result=dict(status='waiting_data',reason=window['error'])
    else:
        result=_compute(task)
    if window:
        result['data_window']=window
        note=queue.window_note(window)
        if note and not window.get('error'):
            result['reason']=result.get('reason','')+'；'+note
    return result


def _compute(task):
    queue.stage(task['id'],'历史数据与交易日历预检')
    if task['kind']=='single':
        return compute_single(task)
    try:
        panel,quality=load_daily(task)
        queue.stage(task['id'],'计算冻结表达式的历史因子值')
        values,warmup,fields=factor_values(task,panel)
    except WaitingData as exc: return dict(status='waiting_data',reason=str(exc))
    except NotImplementedError as exc: return dict(status='unsupported',reason=str(exc))
    close=panel['$close'].unstack('instrument').sort_index()
    queue.stage(task['id'],'计算成熟收益标签、年度和季度报告')
    stats=historical_stats(values,close,task['horizons'])
    valid=stats['summary'][str(task['primary_horizon'])]['days']
    status='research_complete' if valid>=30 else 'insufficient'
    if quality['rejected'] and status=='research_complete': status='partial'
    output=dict(status=status,reason=f'主周期{task["primary_horizon"]}日：有效截面{valid}天；预检通过{quality["accepted_stocks"]}/{quality["requested_stocks"]}只',
                assessment_kind='historical_diagnostic_not_independent_oos',
                start=str(close.index[0].date()),end=str(close.index[-1].date()),
                factor=task['factor'],scope=task['scope'],quality=quality,warmup=warmup,fields=fields,
                planning_manifest=task['manifest'],**stats,
                execution_status='not_run',
                limitations=['未复权价格诊断，未处理公司行动，不能作为交易收益或准入证据',
                             '当前股票池历史复盘，非历史时点成分股，无幸存者偏差保证',
                             '重叠收益标签仅用于统计，不复利生成账户净值',
                             '使用原表达式，不按回测收益反选方向或阈值',
                             '板块映射和稀疏事件字段沿用已有历史帧，其时点与缺失覆盖仍须独立审核'])
    # Keep exact factor values for charts/review without keeping raw high frequency.
    flat=values.stack().rename('value').reset_index()
    flat.columns=['date','code','value']
    output['factor_values']=records(flat)
    return output


def compute_single(task):
    import stock_factor_workbench as work
    import research_retention as rr
    from single_stock_jobs import evaluate,uninterrupted_return
    exp=work.get_result(task['experiment_id'],'experiments')
    if rr._hash(exp['inputs'])!=task['input_hash']:
        return dict(status='failed',reason='保存输入快照摘要不一致，禁止回放')
    candidate=task['candidate']
    if not candidate.get('expression'):
        return dict(status='unsupported',reason='旧版固定特征候选不属于真实表达式快照，需单独适配')
    try:
        panel,quality=load_daily({**task,'codes':[task['code']],'start':exp['start']})
        d=panel.xs(task['code'],level='instrument').rename(columns=lambda x:x[1:])
        if exp['source']=='日线量价':
            d['return_5']=uninterrupted_return(d.close)
            d['volatility_20']=d.close.pct_change(fill_method=None).rolling(20).std()
            d['volume_ratio_20']=d.volume/d.volume.rolling(20).mean().replace(0,np.nan)-1
        else:
            import datasource
            source='ifind_minute' if exp['source']=='分钟日内特征' else 'ifind_realtime'
            with closing(queue.read_db(datasource.MKT_DB)) as c:
                rows=c.execute("SELECT day,features,revision,quality FROM research_day_archive WHERE code=? AND source=? AND version=? AND day BETWEEN ? AND ? ORDER BY day,revision",(task['code'],source,exp['feature_version'],exp['start'],task['end'])).fetchall()
                revisions=dict(c.execute('SELECT day,revision FROM research_raw_revision WHERE code=? AND source=?',(task['code'],source)))
            cleared={(day,revision) for day,features,revision,quality0 in rows if quality0=='cleared'}
            valid={day:json.loads(features) for day,features,revision,quality0 in rows if quality0=='ready' and (revisions.get(day)==revision or (day,revisions.get(day)) in cleared)}
            for field in work.SOURCES[exp['source']]:
                d[field]=pd.Series({pd.Timestamp(day):v.get(field) for day,v in valid.items()})
        values=evaluate(candidate['expression'],d).replace([np.inf,-np.inf],np.nan)
    except (ValueError,KeyError) as exc:
        return dict(status='waiting_data',reason=str(exc))
    # Reusing a tested holdout must not be described as a fresh independent test.
    threshold=candidate['threshold']; direction=candidate['direction']
    rows=[]
    for horizon in (1,5,10,20):
        future=uninterrupted_return(d.close,periods=horizon,forward=True)
        for day in d.index[d.index>=pd.Timestamp(exp['test_start'])]:
            value=values.loc[day];ret=future.loc[day]
            rows.append(dict(date=str(day.date()),horizon=horizon,
                             factor_value=float(value) if pd.notna(value) else None,
                             triggered=bool(value*direction>threshold) if pd.notna(value) else False,
                             forward_return=float(ret) if pd.notna(ret) else None,
                             phase='discovery_followup' if day>pd.Timestamp(exp['end']) else 'saved_holdout',
                             status='valid' if pd.notna(ret) and pd.notna(value) else 'unsettled_or_missing'))
    frame=pd.DataFrame(rows)
    stats={}
    for horizon,group in frame.groupby('horizon'):
        matched=group[group.triggered & group.forward_return.notna()]
        stats[str(horizon)]=dict(signals=int(group.triggered.sum()),settled_signals=len(matched),
                                mean_signal_return=float(matched.forward_return.mean()) if len(matched) else None,
                                win_rate=float((matched.forward_return>0).mean()) if len(matched) else None)
    queue.stage(task['id'],'复核单股快照留出段的日频成交回放')
    execution={}
    # Reuse an existing complete receipt for exactly the same saved input.
    previous=[r for r in work.list_results(task['code'],'reports') if r.get('experiment_id')==exp['id'] and r.get('candidate',{}).get('name')==candidate['name'] and r.get('inputs_used')==exp['input_hash'] and r.get('data_quality_status')!='incomplete']
    try:
        result=previous[0] if previous else work.backtest(exp['id'],candidate['name'])
        execution=dict(execution_status='execution_complete' if result['data_quality_status']!='incomplete' else 'partial',
                       report_id=result['id'],metrics=result['metrics'],execution_period=result['period'])
    except ValueError as exc:
        execution=dict(execution_status='waiting_data',execution_reason=str(exc))
    settled=stats['5']['settled_signals']
    return dict(status='research_complete' if settled>=30 else 'insufficient',
                reason=f'单股冻结表达式：5日已结算信号{settled}个；成交状态另列',
                summary=stats,daily=rows,quality=quality,threshold=threshold,direction=direction,
                start=exp['test_start'],end=task['end'],**execution,
                assessment_kind='single_frozen_expression_history_and_followup',
                limitations=['单股时序研究，不是全市场因子验证',
                             '收益标签未复权、未扣费，不复利构造净值',
                             '成交报告仅覆盖原始快照留出段；新增日期为信号跟踪',
                             '发现后跟踪按原研究截止日区分，不自动声称数据在真实发现时完全未见'])
