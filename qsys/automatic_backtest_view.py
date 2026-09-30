"""Automatic history queue and immutable historical reports."""
import json
from datetime import datetime
import pandas as pd
import streamlit as st
import automatic_backtest as q


def render():
    st.subheader('自动历史周期回测')
    st.info('新因子与历史数据变更自动排队。研究计算完成不代表因子有效，也不授予自动交易资格。')
    cfg=q.config()
    with st.expander('自动运行设置',expanded=False):
        from common import all_pools
        choices=sorted(set(all_pools())|{'本地已采集股票'}|set(cfg['pools']))
        with st.form('automatic_history_settings'):
            enabled=st.checkbox('启用自动历史回测',value=cfg['enabled'])
            pools=st.multiselect('全库因子评估股票池',choices,default=cfg['pools'])
            single=st.checkbox('同时回放已保存单股候选的留出段',value=cfg['single_stock'])
            batch=st.number_input('每批任务数',1,10,int(cfg['batch']))
            timeout=st.number_input('单任务超时（秒）',10,300,int(cfg['timeout']))
            st.caption('独立工作线程每5分钟推进；覆盖扫描每30分钟补漏，数据入库事件提前唤醒。只读本地历史，不调用LLM、不自动补抓。')
            if st.form_submit_button('保存自动回测设置'):
                try:
                    q.configure(dict(enabled=enabled,pools=pools,single_stock=single,batch=int(batch),timeout=int(timeout)))
                    st.success('已保存，下一个调度周期生效')
                except ValueError as exc: st.error(str(exc))
    a,b=st.columns(2)
    if a.button('重新检查历史覆盖并排队'):
        q.notify('user_requested_scan');q.put('discovery',{})
        st.success('已请求覆盖扫描，后台将在下一调度周期处理；重复请求不会重复创建相同版本任务。')
    if b.button('刷新任务状态'): st.rerun()
    _status()


@st.fragment(run_every=10)
def _status():
    if q.setting('error'): st.error(q.setting('error'))
    discovery=q.setting('discovery',{})
    if discovery:
        st.caption(f"最近覆盖扫描：{datetime.fromtimestamp(discovery['at']):%Y-%m-%d %H:%M:%S} · 已收盘数据截止 {discovery['end']} · 股票池：{'、'.join(discovery['pools'])}")
    else: st.caption('等待首次覆盖扫描；开启页面不会同步执行重计算。')
    counts,rows=q.overview()
    total=sum(counts.values());done=total-counts.get('pending',0)-counts.get('running',0)
    good=counts.get('research_complete',0)+counts.get('execution_complete',0)
    c=st.columns(4)
    c[0].metric('当前版本任务',total);c[1].metric('已处理（含失败/不足）',done)
    c[2].metric('完整计算结果',good);c[3].metric('排队 / 运行',f"{counts.get('pending',0)} / {counts.get('running',0)}")
    if total: st.progress(done/total,text=f'处理覆盖 {done}/{total}；完整结果覆盖 {good}/{total}')
    st.write({q.LABELS.get(k,k):v for k,v in counts.items()})
    if not rows:
        st.info('尚未生成任务。后台首次扫描后，这里会显示覆盖情况和进度。');return
    query=st.text_input('筛选最近任务：因子名或股票代码',key='auto_history_search')
    visible=[]
    for row in rows:
        p=json.loads(row['payload'])
        if query and query.lower() not in (p['name']+' '+p['scope']).lower():continue
        visible.append((row,p))
    st.caption('下表展示最近200条当前版本任务；完整覆盖计数来自全部任务。')
    st.dataframe(pd.DataFrame([dict(因子=p['name'],范围=p['scope'],开始=p['start'],截止=p['end'],状态=q.LABELS[r['status']],阶段=r['stage'],说明=r['reason'],任务=r['id'][:12]) for r,p in visible]),hide_index=True,width='stretch')
    if not visible:return
    chosen=st.selectbox('查看任务及历史报告',[r['id'] for r,p in visible],format_func=lambda rid:next(f"{p['name']} · {p['scope']} · {q.LABELS[r['status']]}" for r,p in visible if r['id']==rid))
    row=next(r for r,p in visible if r['id']==chosen)
    with q.connect() as conn:
        history=[dict(r) for r in conn.execute('SELECT id,updated,status,report FROM tasks WHERE logical_key=? ORDER BY rowid DESC LIMIT 30',(row['logical_key'],))]
    report_id=st.selectbox('报告版本',[r['id'] for r in history],format_func=lambda rid:next(f"{datetime.fromtimestamp(r['updated']):%m-%d %H:%M} · {q.LABELS[r['status']]} · {rid[:10]}" for r in history if r['id']==rid))
    raw=next(r['report'] for r in history if r['id']==report_id)
    if not raw:st.caption('尚无报告，任务进度见上表。');return
    report=json.loads(raw)
    st.write(report.get('reason',''))
    for warning in report.get('limitations',[]): st.caption(warning)
    if report.get('summary'):
        st.dataframe(pd.DataFrame(report['summary']).T,width='stretch')
        st.caption('日频研究：IC和上下分组超额均为历史统计；不是账户净值或实盘收益。重叠标签不复利。')
    if report.get('periods'):
        st.dataframe(pd.DataFrame(report['periods']),hide_index=True,width='stretch')
    if report.get('metrics'): st.json(report['metrics'])
    if report.get('report_id'):st.info('单股成交明细已保存至该股票的「因子回测」报告：'+report['report_id'])
    with st.expander('数据覆盖、缺失与每日研究记录'):
        st.json(report.get('quality',{}))
        if report.get('daily'): st.dataframe(pd.DataFrame(report['daily']),hide_index=True,width='stretch')
    st.download_button('下载此版本报告',raw,file_name=f'history_{report_id[:12]}.json',mime='application/json')
