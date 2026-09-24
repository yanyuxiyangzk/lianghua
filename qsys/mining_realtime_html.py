"""Push-only browser view: one initial snapshot, subsequent SSE DOM updates."""
import json


def build_html(events, running, port=8502):
    payload = json.dumps(events, ensure_ascii=False).replace('<', '\\u003c')
    return TEMPLATE.replace('__EVENTS__', payload).replace('__RUNNING__', json.dumps(running)).replace('__PORT__', str(int(port)))


TEMPLATE = r'''<!doctype html><html><head><meta charset="utf-8"><style>
body{font-family:system-ui,sans-serif;color:#31333f;margin:8px}details{border:1px solid #ddd;border-radius:7px;margin:6px 0;padding:10px}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.7 system-ui,sans-serif;max-height:240px;overflow:auto}.muted{font-size:12px;color:#666}#logs{height:300px;overflow:auto;border:1px solid #ddd;padding:10px}#status{padding:10px;background:#f0f2f6;border-radius:7px}
</style></head><body><div id="status"></div><p id="connection" class="muted">正在连接事件流…</p><div id="steps"></div><h3>完整时间日志</h3><p class="muted">按时间从上到下显示；在底部时自动跟随，向上滚动可暂停跟随。</p><pre id="logs"></pre>
<script>
const initial=__EVENTS__, names=['构建面板','机制族引导','FSA重算','生成候选','规则审查','LLM审查','去重','FSA拦截','硬闸门','入库'];
const status=document.getElementById('status'),conn=document.getElementById('connection'),logs=document.getElementById('logs');
status.textContent=__RUNNING__?'批次运行中，等待当前阶段事件':'当前无挖掘任务运行，下方为历史日志';
const statuses={running:'开始执行',done:'完成',pass:'通过',fail:'未通过',skip:'跳过',dup:'重复，跳过本候选',frozen:'拦截，跳过本候选',error:'异常'};
const sources={mutate:'变异生成',crossover:'交叉组合',perturb:'扰动生成',random:'随机生成',llm:'模型生成'};
let batch=null,candidate=null; const panes=[];
names.forEach((name,i)=>{const d=document.createElement('details'),s=document.createElement('summary'),p=document.createElement('pre');s.textContent=(i+1)+'. '+name;p.textContent='尚无该步骤记录';d.append(s,p);document.getElementById('steps').append(d);panes.push(p)});
function append(el,line){const follow=el.scrollHeight-el.scrollTop-el.clientHeight<30; if(el.textContent==='尚无该步骤记录')el.textContent=''; el.textContent+=(el.textContent?'\n':'')+line;const lines=el.textContent.split('\n');if(lines.length>800)el.textContent=lines.slice(-800).join('\n');if(follow)el.scrollTop=el.scrollHeight;}
function receive(e,historical=false){
 let step=e.step||({candidate_gen:4,review_result:5,llm_result:6,gate_eval:9,gate_pass:10}[e.type]),msg='';
 if(e.type==='round_start'){batch=e.batch;candidate=null;panes.forEach(p=>p.textContent='尚无该步骤记录');msg='第 '+e.iteration+' 轮开始 · '+e.factor_type+' · '+e.batch+' 个候选';if(!historical)status.textContent=msg;}
 else if(e.type==='round_complete'){const s=e.stats||{};msg='第 '+e.iteration+' 轮完成 · '+(s.factor_type||'')+' · 测试 '+(s.tested||0)+' 个，入库 '+(s.passed||0)+' 个';if(!historical)status.textContent=msg;}
 else if(e.type==='step_update'){
 if(step===4){candidate=Number.isInteger(batch)&&Number.isInteger(e.batch_left)?batch-e.batch_left+1:null;const label=candidate?'候选 '+candidate+'/'+batch:'下一候选（编号未记录）';for(let i=3;i<10;i++)append(panes[i],'—— '+label+' ——');}
 msg=step+'. '+(e.name||names[step-1])+'：'+(statuses[e.status]||e.status||'更新');
 if(e.source)msg+=' · '+(sources[e.source]||e.source);if(e.gaps)msg+=' · 优先探索：'+e.gaps.join('、');if(e.proven)msg+=' · 实战机制族：'+e.proven.join('、');
 if(!historical)status.textContent=(candidate?'候选 '+candidate+' · ':'')+msg;
 }else if(e.type==='gate_eval')msg='硬闸门评估：'+e.factor_name+' · '+(e.passed?'通过':'未通过');
 else if(e.type==='gate_pass')msg='因子入库：'+e.factor_name;
 else if(e.type==='review_result'||e.type==='llm_result')msg=(e.type==='llm_result'?'LLM 风险审查':'规则审查')+'：'+(e.passed?'通过':'未通过／风险标记');
 else if(e.type==='candidate_gen')msg='候选已生成';else return;
 const reason=e.reason||e.error||e.skip_reason;if(reason==='llm-error-fallback')msg='LLM 调用失败，已回退处理（不代表模型审查通过）';else if(reason)msg+=' · 原因：'+reason;
 const line=(e.ts||'').replace('T',' ')+'  '+msg;append(logs,line);if(step>=1&&step<=10)append(panes[step-1],line);
}
initial.forEach(e=>receive(e,true));logs.scrollTop=logs.scrollHeight;
// Resolve browser host instead of hardcoding localhost for remote users.
const url=new URL(window.parent.location.href);url.port='__PORT__';url.pathname='/events';url.search='';url.hash='';
const es=new EventSource(url.toString());
es.onopen=()=>conn.textContent='SSE 已连接 · 新日志即时追加，不定时刷新页面';
es.onerror=()=>conn.textContent='SSE 连接中断，正在自动重连；断线期间的日志可刷新页面读取已保存记录';
['round_start','step_update','candidate_gen','review_result','llm_result','gate_eval','gate_pass','round_complete'].forEach(kind=>es.addEventListener(kind,event=>{try{receive(JSON.parse(event.data))}catch(error){conn.textContent='收到无法解析的事件'}}));
es.addEventListener('job_end',event=>{const e=JSON.parse(event.data);if(['multitype_mine','loopengine'].includes(e.job_key))status.textContent=e.success?'挖掘批次已结束':'挖掘批次失败：'+(e.message||'查看运行日志');});
window.addEventListener('pagehide',()=>es.close());
</script></body></html>'''
