"""Single-stock local research UI reached from history inventory row actions."""
from datetime import date,timedelta
import json
import pandas as pd
import streamlit as st
import stock_factor_workbench as work
import single_stock_jobs as jobs


@st.fragment(run_every=2)
def task_status(code):
    rows=jobs.jobs(code)
    # A fragment refresh alone cannot reload the result selectors below it.
    # Acknowledge terminal transitions before requesting one full rerun.
    terminal={r['id']:r['status'] for r in rows if r['status'] in ('completed','failed')}
    key=f'job_terminal_states_{code}'
    previous=st.session_state.get(key,{})
    changed=[r for r in rows if r['id'] in terminal and previous.get(r['id'])!=r['status']]
    st.session_state[key]=terminal
    if changed:
        for kind,selection in [('mine','research_batch'),('backtest','research_report')]:
            latest=next((r for r in changed if r['kind']==kind and r['status']=='completed' and r['result_id']),None)
            if latest:
                st.session_state[f'{selection}_{code}']=latest['result_id']
        st.rerun()
    if rows:
        st.subheader("单股任务状态（每2秒刷新）")
    kinds={'mine':'因子挖掘','backtest':'因子回测','constraints':'成交约束补抓'}
    states={'queued':'排队中，尚未开始','running':'执行中','completed':'已完成','failed':'失败'}
    for row in rows[:3]:
        label=f"{kinds.get(row['kind'],row['kind'])} · {states.get(row['status'],row['status'])}"
        if row['status']=='completed':
            if row['kind']=='mine' and row['result_id']:
                exp=work.get_result(row['result_id'],'experiments')
                count=len(exp['candidates'])
                st.success(f"{label}：已评估 {len(exp.get('trials',[]))} 个表达式，保存 {count} 个研究候选。")
                if not count:
                    st.warning('本次挖掘已执行，但没有候选通过筛选；可查看全部表达式及淘汰原因。')
                st.caption('研究候选不等于有效交易因子；请继续做留出回测。结果已自动加载到下方研究批次。')
            else:
                st.success(f"{label}：{row['message']}")
        elif row['status']=='failed':
            st.error(f"{label}：{row['message']}")
        else:
            st.info(f"{label}：{row['message']}")
        st.progress(int(row['progress']),text=f"{label} · {row['message']}")
        st.caption(f"任务 {row['id']} · 提交时间 {row['created']} · 结果 {row['result_id'] or '尚未保存'}")
    if rows and st.button("刷新已保存结果",key=f"refresh_jobs_{code}"):
        st.rerun()


def research_table(rows):
    # Nested ASTs contain both lists and numbers; Arrow needs display strings.
    return [{key:json.dumps(value,ensure_ascii=False) if isinstance(value,(list,dict)) else value
             for key,value in row.items()} for row in rows]


def result_selection(code, suffix, runs):
    key=f'{suffix}_{code}'
    ids=[r['id'] for r in runs]
    if st.session_state.get(key) not in ids:
        st.session_state[key]=ids[0]
    return key


def render(code, action):
    if st.button('← 返回已抓取股票列表'):
        st.session_state['stock_history_view']='list'
        st.rerun()
    st.title(f'{code} · 单股因子研究')
    st.info('这是单股时序诊断，不是全股票池的因子有效性验证；不降低30/50只股票的正式评估门槛。只读取本地数据，不自动补抓。')
    mode=st.radio('操作',['因子挖掘','因子回测','清理高频'],index={'mine':0,'backtest':1,'cleanup':2}.get(action,0),key=f'stock_research_mode_{code}_{action}')
    task_status(code)
    if mode=='因子挖掘':
        st.info('手动启动单股真实表达式搜索：50%训练、25%验证、25%最终留出；不会写入全市场因子库。分钟/盘口必须已有足够的新版校验特征。')
        with st.form(f'stock_mine_{code}'):
            source=st.selectbox('数据来源',list(work.SOURCES))
            a,b=st.columns(2)
            start=a.date_input('开始日期',date.today()-timedelta(days=730))
            end=b.date_input('结束日期',date.today()-timedelta(days=1),max_value=date.today()-timedelta(days=1))
            budget=st.number_input('不同表达式搜索预算',10,300,60)
            seed=st.number_input('随机种子（用于复现）',0,2147483647,42)
            clicked=st.form_submit_button('开始单股真实挖掘',type='primary')
        if clicked:
            try:
                rid=jobs.submit(code,'mine',dict(source=source,start=start.isoformat(),end=end.isoformat(),budget=int(budget),seed=int(seed)))
                st.rerun()
            except Exception as exc:st.error(str(exc))
        runs=work.list_results(code)
        if runs:
            st.subheader('已保存候选研究结果')
            chosen=st.selectbox('研究批次',[r['id'] for r in runs],key=result_selection(code,'research_batch',runs),format_func=lambda x: next(f"{r['created']} · {r['source']} · {r['start']}～{r['end']}" for r in runs if r['id']==x))
            exp=next(r for r in runs if r['id']==chosen)
            st.metric('候选数量', len(exp['candidates']))
            st.dataframe(research_table(exp['candidates']),hide_index=True,width='stretch')
            st.caption(exp['note'])
            if exp.get('trials'):
                with st.expander('全部表达式及淘汰原因'):
                    st.dataframe(research_table(exp['trials']),hide_index=True,width='stretch')
            if st.button('复核快照的训练与留出结果',key=f"review_{chosen}"):
                try:
                    reviewed=work.review_snapshot(chosen)
                    st.dataframe(research_table(reviewed['candidates']),hide_index=True,width='stretch')
                    st.caption(reviewed['note'])
                    st.download_button('下载快照复核报告',json.dumps(reviewed,ensure_ascii=False,allow_nan=False),file_name=f'{code}_snapshot_review.json')
                except Exception as exc:
                    st.error(f'复核未完成：{exc}')

            if exp.get('progress_log'):
                with st.expander('查看已保存批次的执行记录（数据库已存在此批次）'):
                    st.dataframe(exp['progress_log'],hide_index=True,width='stretch')
            st.download_button('下载因子和输入快照',json.dumps(exp,ensure_ascii=False,allow_nan=False),file_name=f'{code}_factor_inputs.json',mime='application/json')
    elif mode=='因子回测':
        runs=work.list_results(code)
        if not runs:
            st.info('请先点击“因子挖掘”，生成并保存该股票的候选。')
            return
        chosen=st.selectbox('选择已保存研究批次',[r['id'] for r in runs],format_func=lambda x: next(f"{r['created']} · {r['source']}" for r in runs if r['id']==x))
        exp=next(r for r in runs if r['id']==chosen)
        import execution_constraints as ec
        constraint_dates=[r['date'] for r in exp['inputs']][exp['split']:]
        coverage=ec.coverage(code,constraint_dates)
        if coverage['complete']:
            st.caption(f"成交约束预检通过：留出段 {coverage['expected_days']} 日的历史停牌、涨停价、跌停价均已保存。")
        else:
            st.warning(f"成交约束不足：{coverage['missing_fields']} 个日期/字段缺失或无效。")
            st.write({field:len(days) for field,days in coverage['missing'].items()})
            with st.expander('缺失日期明细'):
                st.json(coverage['missing'])
            if st.button('从同花顺补齐本股票回测约束',key=f'constraints_{chosen}'):
                try:
                    rid=jobs.submit(code,'constraints',dict(experiment_id=chosen))
                    st.rerun()
                except Exception as exc:st.error(str(exc))
        names=[c['name'] for c in exp['candidates'] if c['status']=='research_candidate']
        if not names:
            st.warning('此批次没有通过筛选的候选，不能回测；请查看搜索尝试记录。')
        else:
            candidate=st.selectbox('选择因子候选',names)
            st.caption(f"留出回测：{exp['test_start']}～{exp['end']}；固定训练方向与阈值，每5日产生信号、次日开盘模拟成交。")
            if st.button('运行单股因子回测',type='primary'):
                try:
                    rid=jobs.submit(code,'backtest',dict(experiment_id=chosen,candidate=candidate))
                    st.rerun()
                except Exception as exc:st.error(str(exc))
        reports=work.list_results(code,'reports')
        if reports:
            report_id=st.selectbox('历史回测报告',[r['id'] for r in reports],key=result_selection(code,'research_report',reports),format_func=lambda x: next(f"{r['created']} · {r['candidate']['name']} · {r['source']}" for r in reports if r['id']==x))
            report=next(r for r in reports if r['id']==report_id)
            st.caption(f"报告 {report_id} · {report['period']} · {report['note']}")
            if report['data_quality_status']=='incomplete':
                st.warning('成交约束或行情数据不完整：此结果只能用于排查，不算完整回测验收通过。')
            st.caption(report['execution_limitations'])
            st.write({'成交笔数':report['trades'],'数据状态':report['data_quality_status'],
                      '缺少约束':report['execution']['constraints_missing']})
            from execution_detail_view import daily_table,detail_table
            st.dataframe(daily_table(report),hide_index=True,width='stretch')
            with st.expander('委托、成交与异常'):
                for field in ('orders','fills','data_issues'):
                    st.caption(field)
                    st.dataframe(detail_table(report['execution'][field]),hide_index=True,width='stretch')
            st.download_button('下载完整回测报告',json.dumps(report,ensure_ascii=False,allow_nan=False),file_name=f'{code}_factor_backtest.json',mime='application/json')
    else:
        from research_retention_view import render as render_retention
        render_retention(code)
