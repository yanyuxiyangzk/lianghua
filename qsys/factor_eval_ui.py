"""One initial read plus SSE progress; no Streamlit periodic rerun."""
import json
import streamlit as st
from factor_evaluation_queue import read_events


def render_evaluation_status():
    from common import all_pools
    from coverage_report import build
    with st.expander('全库评估覆盖与调度容量', expanded=False):
        pools = list(all_pools())
        if pools:
            pool = st.selectbox('覆盖统计股票池', pools, key='factor_coverage_pool')
            report = build(pool)
            st.caption(f"队列快照时间：{report['as_of']}（重新打开页面刷新）")
            st.progress(report['coverage'], text=f"已处理 {report['done']:,} / {report['total']:,} 个队列版本")
            st.write({k: report[k] for k in ('pending', 'running', 'valid', 'sample_insufficient', 'data_error', 'compute_failed', 'blocked', 'superseded')})
            st.caption(f"已启用定时窗口的配置容量：{report['daily_capacity_estimate']} 个/调度日")
            if report['schedule_windows']:
                st.dataframe(report['schedule_windows'], hide_index=True)
            if report['estimated_days'] is not None:
                st.caption(f"仅按待处理数量/配置容量估算：{report['estimated_days']:.1f} 个调度日；非完工承诺。")
            st.caption(report['interpretation'])
    events = read_events()
    payload = json.dumps([e for _,e in events][-30:],ensure_ascii=False).replace('<','\\u003c')
    cursor = events[-1][0] if events else 0
    html = '''<style>body{font:14px system-ui;color:#31333f;margin:4px}#summary{padding:10px;background:#f0f2f6;border-radius:7px}pre{white-space:pre-wrap;font:12px/1.6 system-ui;height:150px;overflow:auto}small{color:#666}</style>
<div id="summary">尚无新版体检执行记录</div><small id="connection">连接中</small><details><summary>回测中文日志</summary><pre id="logs"></pre></details>
<script>
const statuses={idle:'等待下次体检',running:'运行中',complete:'已完成',failed:'失败',deferred:'等待下一窗口'},results={valid:'有效完成',sample_insufficient:'样本不足',data_error:'数据异常',compute_failed:'计算失败'};
const box=document.getElementById('summary'),logs=document.getElementById('logs');
function show(e){const c=e.counts||{};box.textContent=(statuses[e.status]||e.status)+' · 已处理 '+(e.processed||0)+'/'+(e.total||0)+' · 有效 '+(c.valid||0)+' · 样本不足 '+(c.sample_insufficient||0)+' · 异常 '+((c.data_error||0)+(c.compute_failed||0));
const follow=logs.scrollHeight-logs.scrollTop-logs.clientHeight<30;
logs.textContent+=(e.ts||'')+' '+(e.factor||'批次')+' '+(results[e.result]||statuses[e.status]||'')+(e.reason?' · '+e.reason:'')+'\\n';
if(logs.textContent.length>30000)logs.textContent=logs.textContent.slice(-25000);if(follow)logs.scrollTop=logs.scrollHeight;}
__INITIAL__.forEach(show);
const url=new URL(window.parent.location.href);url.port='8502';url.pathname='/factor-eval/events';url.search='?after=__CURSOR__';url.hash='';
const es=new EventSource(url);es.addEventListener('factor_eval_progress',e=>show(JSON.parse(e.data)));
es.onopen=()=>document.getElementById('connection').textContent='SSE 已连接 · 评分有效不等于验证通过或交易获批';
es.onerror=()=>document.getElementById('connection').textContent='进度连接中断，自动重连中';window.addEventListener('pagehide',()=>es.close());
</script>'''.replace('__INITIAL__',payload).replace('__CURSOR__',str(cursor))
    st.components.v1.html(html,height=245,scrolling=True)
