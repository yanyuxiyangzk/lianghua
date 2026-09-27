"""Controls and evidence for local incremental research and raw retention."""
from datetime import date,timedelta
import json
import streamlit as st
import datasource
import research_retention as rr


def render(code):
    rr.setup()
    st.subheader('增量研究与原始数据保留')
    st.info('长期保留历史日线、成交约束、每日特征版本和研究报告。只有过保留期且校验通过的数据可清理；新数据先计算特征，5交易日标签成熟后再评估。')
    p=rr.policy(code)
    with st.form(f'retention_policy_{code}'):
        minute=st.number_input('分钟原始数据保留交易日',1,2000,int(p['minute_days']))
        micro=st.number_input('盘口、快照和逐笔保留交易日',1,2000,int(p['micro_days']))
        auto=st.checkbox('授权该股票按上述规则自动删除过期原始数据',value=p['auto_cleanup'])
        if st.form_submit_button('保存保留策略'):
            rr.set_policy(code,minute,micro,auto)
            st.success('策略已保存；校验不通过或人工保护的数据不会自动删除。')
    st.caption('保留期按本股已保存的历史日线交易日期计数；历史不足时不删除。分钟60天、其他10天为默认值。')
    if st.button('更新增量特征并评估候选',key=f'incremental_{code}'):
        with st.spinner('校验并归档原始数据、更新成熟标签、评估候选（每次最多40个股票日/来源）…'):
            st.write(rr.run_incremental(code,limit=40,search=True))
    with datasource._conn() as c:
        state=c.execute('SELECT updated,payload FROM research_worker_status WHERE code=?',(code,)).fetchone()
        quality=c.execute('''SELECT quality,COUNT(*) FROM research_day_archive a WHERE code=? AND version=?
          AND revision=(SELECT MAX(b.revision) FROM research_day_archive b WHERE b.code=a.code AND b.day=a.day AND b.source=a.source AND b.version=a.version) GROUP BY quality''',(code,rr.VERSION)).fetchall()
        labels=c.execute('SELECT status,COUNT(*) FROM research_labels WHERE code=? GROUP BY status',(code,)).fetchall()
        runs=c.execute('SELECT created,payload FROM research_candidate_runs WHERE code=? ORDER BY created DESC LIMIT 1',(code,)).fetchone()
        protection=c.execute('SELECT id,start,end,reason FROM research_protection WHERE code=?',(code,)).fetchall()
        audit=c.execute('SELECT created,payload FROM research_cleanup_log WHERE code=? ORDER BY created DESC LIMIT 5',(code,)).fetchall()
    if state:
        st.caption('最近增量检查：'+state[0]);st.json(json.loads(state[1]),expanded=False)
    st.write({'归档状态（股票日/来源）':dict(quality),'5交易日标签状态':dict(labels)})
    st.caption('ready=通过归档校验；blocked=保留待核实；cleared=原始记录已清理。标签待成熟/待日历/待行情分别记录。旧版汇总特征不会自动获得删除许可。')
    if runs:
        result=json.loads(runs[1]);st.caption('最近候选评估：'+runs[0]);st.caption(result['note'])
        st.dataframe(result['candidates'],hide_index=True,width='stretch')
    st.warning('候选搜索仅为预设特征及其5期均值/差分的单股探索，不自动进入全局因子库或交易。标签采用未复权收盘价，不作为实盘收益认证。')
    with st.expander('人工保护研究区间'):
        with st.form(f'protect_{code}'):
            pa=st.date_input('保护开始',date.today()-timedelta(days=30))
            pb=st.date_input('保护结束',date.today())
            reason=st.text_input('保留原因')
            if st.form_submit_button('添加保护'):
                try:
                    rr.protect(code,pa.isoformat(),pb.isoformat(),reason);st.rerun()
                except ValueError as exc: st.error(str(exc))
        if protection: st.dataframe([dict(id=r[0],开始=r[1],结束=r[2],原因=r[3]) for r in protection],hide_index=True)
    size=rr.storage()
    st.write({'数据库大小 MiB':round(size['database_bytes']/1048576,2),'库内可复用 MiB':round(size['reusable_bytes']/1048576,2),'WAL大小 MiB':round(size['wal_bytes']/1048576,2)})
    st.caption('这些是整个行情库的实际空间；预览行数不等于可回收字节。DELETE不保证文件缩小，不自动执行阻塞数据库的VACUUM。')
    with datasource._conn() as c:
        first=c.execute("SELECT MIN(date) FROM market_daily WHERE code=? AND source='ths_ifind'",(code,)).fetchone()[0]
    a,b=st.columns(2)
    start=a.date_input('清理开始日期',date.fromisoformat(first) if first else date.today()-timedelta(days=730))
    end=b.date_input('清理结束日期',date.today()-timedelta(days=1),max_value=date.today()-timedelta(days=1))
    st.caption('启用自动清理只影响后续后台执行，不会立即删除，也不会缩短当前保留期。')
    if st.button('校验并归档所选范围（不删除）',key=f'prepare_cleanup_{code}'):
        try:
            with st.spinner('保存分钟、盘口等特征并校验，每次最多40个日期/来源…'):
                prepared=rr.prepare_cleanup(code,start.isoformat(),end.isoformat())
            st.info(f"归档检查结果：{prepared['processed']}；本轮未处理 {prepared['remaining']} 个日期/来源。下方预览已更新，仍受保留期和校验结果约束。")
        except Exception as exc:
            st.error(f'归档未完成：{exc}')
    receipt=st.session_state.pop(f'cleanup_receipt_{code}',None)
    if receipt:
        st.success(receipt)
    try: preview=rr.preview(code,start.isoformat(),end.isoformat())
    except ValueError as exc: st.error(str(exc));return
    st.write({'符合删除条件的行数':preview['eligible'],'被保护或待核实行数':preview['blocked_rows']})
    with st.expander('按日期查看可删除范围与阻止原因'):
        st.dataframe(preview['details'],hide_index=True,width='stretch')
    eligible_rows=sum(preview['eligible'].values())
    if not eligible_rows:
        if not preview['details']:
            st.info('当前日期范围内没有原始高频数据，无需清理；可调整清理日期范围。')
        else:
            st.warning('当前符合删除条件的记录为 0 行；可使用“一键归档并清理”先自动处理归档。')
            reasons={}
            for row in preview['details']:
                if not row['eligible']:
                    reason=row['reason'] or '归档校验未通过'
                    reasons[reason]=reasons.get(reason,0)+row['rows']
            st.dataframe([{'阻止原因':reason,'原始记录行数':count} for reason,count in reasons.items()],hide_index=True,width='stretch')
            if '保留期内或历史不足' in reasons:
                st.caption('处理办法：等待数据超过保留期，或在上方调整并保存保留策略。缩短保留期后仍须通过归档校验；最新报价所在日继续保留。')
            if any('特征' in reason or '归档' in reason for reason in reasons):
                st.caption('处理办法：点击“校验并归档所选范围（不删除）”生成并校验归档，再检查预览；校验失败的具体原因见日期明细。')
            st.caption('日线因子挖掘完成不代表分钟、盘口等原始数据已完成特征归档。')
    st.caption('一键归档并清理会在后台处理所选范围全部日期/来源，无需反复点击40日归档；只删除归档通过且超过保留期的数据。关闭页面不影响后台任务。')
    confirm=st.checkbox('确认仅删除所选范围内归档合格且超过保留期的原始分钟、盘口、快照和逐笔记录，保留历史日线与全部研究特征')
    if st.button('一键归档并清理',disabled=not confirm or not preview['details'],type='primary'):
        try:
            import single_stock_jobs as jobs
            rid=jobs.submit(code,'cleanup',dict(start=start.isoformat(),end=end.isoformat()))
            st.session_state[f'cleanup_receipt_{code}']=f'任务 {rid} 已进入后台队列，进度见上方任务状态；若已有任务，将先显示已有任务。'
            st.rerun()
        except Exception as exc:
            st.error(f'任务提交失败：{exc}')
    if eligible_rows and not confirm:
        st.info(f'可清理 {eligible_rows:,} 行：请先勾选上方确认框，再点击“确认清理原始高频”。')
    if st.button('确认清理原始高频',disabled=not confirm or not any(preview['eligible'].values()),type='primary'):
        try:
            result=rr.cleanup(code,start.isoformat(),end.isoformat())
            deleted=sum(result['deleted'].values())
            st.session_state[f'cleanup_receipt_{code}']=f"本次实际删除 {deleted} 行：{result['deleted']}；审计编号：{result['id']}"
            st.rerun()
        except Exception as exc: st.error(f'清理未完成：{exc}')
    with st.expander('最近清理记录'):
        for created,payload in audit:
            st.caption(created);st.json(json.loads(payload),expanded=False)
