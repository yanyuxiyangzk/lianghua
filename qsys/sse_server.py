"""FastAPI SSE 服务器：实时推送 LoopEngine 事件到前端。

端点：
  GET /events       SSE 事件流（EventSource 消费）
  GET /health       健康检查
  GET /stats        事件总线统计
  GET /realtime     完整实时页面 HTML（供 Streamlit iframe 嵌入）
"""

import asyncio
import json
import logging

from event_bus import bus
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse

app = FastAPI(title="QSYS SSE Server", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

logger = logging.getLogger("sse_server")

STEP_NAMES = ["构建面板", "机制族引导", "FSA重算", "生成候选", "规则审查",
              "LLM审查", "去重", "FSA拦截", "硬闸门", "入库"]

REALTIME_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LoopEngine 实时</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;padding:12px}
.kpi-row{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.kpi-card{flex:1;min-width:100px;background:#fff;border-radius:8px;padding:10px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.06);border-top:3px solid #667eea}
.kpi-card.c1{border-top-color:#11998e}.kpi-card.c2{border-top-color:#667eea}
.kpi-card.c3{border-top-color:#f7971e}.kpi-card.c4{border-top-color:#eb3349}
.kpi-value{font-size:20px;font-weight:700;color:#222}
.kpi-label{font-size:10px;color:#999;margin-top:2px}
.status-bar{padding:6px 10px;border-radius:5px;margin-bottom:10px;font-size:12px;font-weight:600}
.status-bar.connected{background:#1a3a1a;color:#4ec9b0}
.status-bar.connecting{background:#3a3a1a;color:#f7971e}
.status-bar.error{background:#3a1a1a;color:#f44747}
.step-acc{margin-bottom:4px;border-radius:6px;overflow:hidden;border:1px solid #e0e0e0}
.step-header{display:flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer;user-select:none;background:#f8f9fa;transition:background .2s}
.step-header:hover{background:#e9ecef}
.sh-icon{font-size:14px;width:20px;text-align:center}
.sh-num{background:#6c757d;color:#fff;border-radius:50%;width:20px;height:20px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;flex-shrink:0}
.sh-name{font-size:12px;font-weight:600;color:#333;flex:1}
.sh-badge{font-size:10px;padding:1px 6px;border-radius:8px;background:#e0e0e0;color:#666;white-space:nowrap}
.sh-arrow{font-size:10px;color:#999;transition:transform .2s}
.step-header.open .sh-arrow{transform:rotate(90deg)}
.step-body{display:none;background:#1a1a1a;max-height:0;overflow:hidden;transition:max-height .3s}
.step-body.open{display:block;max-height:300px;overflow-y:auto}
.step-body pre{margin:0;padding:8px 12px;font-family:Consolas,monospace;font-size:10px;color:#d4d4d4;white-space:pre-wrap;word-break:break-all;line-height:1.5}
.ok{color:#4ec9b0}.err{color:#f44747}.warn{color:#f7971e}.info{color:#569cd6}
.round-log{background:#1a1a1a;color:#d4d4d4;font-family:Consolas,monospace;font-size:10px;padding:8px 12px;border-radius:6px;height:160px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.5;margin-top:8px}
</style></head><body>
<div class="kpi-row">
<div class="kpi-card c1"><div class="kpi-value" id="kpi-tested">-</div><div class="kpi-label">测试</div></div>
<div class="kpi-card c2"><div class="kpi-value" id="kpi-passed">-</div><div class="kpi-label">入库</div></div>
<div class="kpi-card c3"><div class="kpi-value" id="kpi-dup">-</div><div class="kpi-label">重复</div></div>
<div class="kpi-card c4"><div class="kpi-value" id="kpi-frozen">-</div><div class="kpi-label">FSA拦截</div></div>
</div>
<div id="steps-container"></div>
<div class="round-log" id="round-log"></div>
<div id="sse-status" class="status-bar connecting">⏳ 连接中...</div>
<script>
(function(){
var STEPS=""" + json.dumps(STEP_NAMES, ensure_ascii=False) + """;
var container=document.getElementById('steps-container');
var logEl=document.getElementById('round-log');
var statusEl=document.getElementById('sse-status');
var kpiT=document.getElementById('kpi-tested'),kpiP=document.getElementById('kpi-passed'),
    kpiD=document.getElementById('kpi-dup'),kpiF=document.getElementById('kpi-frozen');
var stepData={},retryCount=0;
STEPS.forEach(function(n,i){stepData[i]={status:'pending',logs:[]};});

function buildSteps(){
  container.innerHTML='';
  STEPS.forEach(function(n,i){
    var d=stepData[i];
    var acc=document.createElement('div');
    acc.className='step-acc '+d.status;
    acc.id='acc-'+i;
    var icons={pending:'○',running:'🔄',done:'✅',pass:'✅',fail:'❌',skip:'⏭️',dup:'📋',frozen:'🧊'};
    var labels={pending:'等待',running:'执行中',done:'完成',pass:'通过',fail:'失败',skip:'跳过',dup:'重复',frozen:'拦截'};
    var badgeBg=d.status==='done'||d.status==='pass'?'#d4edda':d.status==='running'?'#fff3cd':d.status==='fail'?'#f8d7da':'#e0e0e0';
    var badgeFg=d.status==='done'||d.status==='pass'?'#155724':d.status==='running'?'#856404':d.status==='fail'?'#721c24':'#666';
    acc.innerHTML='<div class="step-header" onclick="toggleStep('+i+')">'
      +'<span class="sh-icon">'+(icons[d.status]||'○')+'</span>'
      +'<span class="sh-num">'+(i+1)+'</span>'
      +'<span class="sh-name">'+n+'</span>'
      +'<span class="sh-badge" style="background:'+badgeBg+';color:'+badgeFg+'">'+(labels[d.status]||d.status)+'</span>'
      +'<span class="sh-arrow">▶</span></div>'
      +'<div class="step-body" id="body-'+i+'"><pre>'+d.logs.join('')+'</pre></div>';
    container.appendChild(acc);
  });
}
window.toggleStep=function(n){
  var hdr=document.getElementById('acc-'+n).querySelector('.step-header');
  var body=document.getElementById('body-'+n);
  hdr.classList.toggle('open');
  body.classList.toggle('open');
};
function addStepLog(n,text,cls){
  stepData[n].logs.push('<div class="'+(cls||'')+'">'+text.replace(/</g,'&lt;')+'</div>');
  var body=document.getElementById('body-'+n);
  if(body)body.querySelector('pre').innerHTML=stepData[n].logs.join('');
}
function addRoundLog(text,cls){
  var line=document.createElement('div');
  line.className=cls||'';line.textContent=text;
  logEl.appendChild(line);logEl.scrollTop=logEl.scrollHeight;
}
function updateKPI(s){
  if(!s)return;
  if(s.tested!==undefined)kpiT.textContent=s.tested;
  if(s.passed!==undefined)kpiP.textContent=s.passed;
  if(s.dup!==undefined)kpiD.textContent=s.dup;
  if(s.frozen!==undefined)kpiF.textContent=s.frozen;
}
function resetAll(){
  STEPS.forEach(function(_,i){stepData[i]={status:'pending',logs:[]};});
  logEl.innerHTML='';kpiT.textContent='0';kpiP.textContent='0';kpiD.textContent='0';kpiF.textContent='0';
}
function connect(){
  var es=new EventSource('/events');
  es.onopen=function(){retryCount=0;statusEl.innerHTML='🟢 已连接';statusEl.className='status-bar connected';};
  es.addEventListener('step_update',function(e){
    var d=JSON.parse(e.data),n=d.step-1;
    if(n>=0&&n<10){stepData[n].status=d.status;buildSteps();}
    var msg='['+d.name+']';
    if(d.source)msg+=' '+d.source;
    if(d.batch_left!==undefined)msg+=' (剩'+d.batch_left+')';
    if(d.sampled!==undefined)msg+=d.sampled?' [抽样]':' [跳过]';
    if(d.reason)msg+=' — '+d.reason;
    if(d.metrics){
      var ic=d.metrics.IC!==undefined?d.metrics.IC.toFixed(4):'';
      var sh=d.metrics['夏普2025']!==undefined?d.metrics['夏普2025'].toFixed(2):'';
      if(ic||sh)msg+=' IC='+ic+' 夏普='+sh;
    }
    var cls=(d.status==='done'||d.status==='pass')?'ok':d.status==='fail'?'err':d.status==='running'?'warn':'info';
    addStepLog(n,msg,cls);
    if(d.status==='running'){var b=document.getElementById('body-'+n);if(b&&!b.classList.contains('open'))window.toggleStep(n);}
  });
  es.addEventListener('round_start',function(e){
    var d=JSON.parse(e.data);resetAll();
    addRoundLog('━'.repeat(50),'info');
    addRoundLog('  第 '+d.iteration+' 轮 | '+d.factor_type+' | 批次:'+d.batch,'info');
    addRoundLog('  稀缺族:'+(d.gaps||[]).join(', ')+' 实战强族:'+(d.proven||[]).join(', '),'info');
    addRoundLog('━'.repeat(50),'info');
  });
  es.addEventListener('gate_eval',function(e){
    var d=JSON.parse(e.data),icon=d.passed?'✅':'❌';
    var ic=d.metrics&&d.metrics.IC!==undefined?d.metrics.IC.toFixed(4):'-';
    var sh=d.metrics&&d.metrics['夏普2025']!==undefined?d.metrics['夏普2025'].toFixed(2):'-';
    var exc=d.metrics&&d.metrics['超额2025']!==undefined?(d.metrics['超额2025']*100).toFixed(1)+'%':'-';
    var s=d.stats_snapshot||{};updateKPI(s);
    addRoundLog('  '+icon+' '+d.factor_name+' IC='+ic+' 夏普='+sh+' 超额='+exc,d.passed?'ok':'err');
  });
  es.addEventListener('gate_pass',function(e){
    var d=JSON.parse(e.data);
    addRoundLog('  🎉 入库 → '+d.factor_name+' [族: '+d.family+']','ok');
  });
  es.addEventListener('round_complete',function(e){
    var d=JSON.parse(e.data),s=d.stats||{};
    addRoundLog('━'.repeat(50),'info');
    addRoundLog('  完成 | 测试:'+s.tested+' 入库:'+s.passed+' 重复:'+s.dup+' FSA:'+s.frozen,'info');
    if(d.new_factors&&d.new_factors.length>0)addRoundLog('  新因子: '+d.new_factors.join('  '),'ok');
    addRoundLog('━'.repeat(50),'info');
    updateKPI(s);
    STEPS.forEach(function(_,i){stepData[i].status='done';});buildSteps();
  });
  es.onerror=function(){retryCount++;if(retryCount>10){statusEl.innerHTML='🔴 断开';statusEl.className='status-bar error';es.close();return;}
    statusEl.innerHTML='🔄 重连 ('+retryCount+'/10)';statusEl.className='status-bar connecting';};
}
buildSteps();connect();
})();
</script></body></html>"""


@app.get("/events")
async def events():
    """SSE 事件流：客户端连接后持续推送事件。"""
    queue = bus.subscribe()

    async def generate():
        try:
            while True:
                try:
                    event = queue.get_nowait()
                    yield {
                        "event": event.get("type", "message"),
                        "data": json.dumps(event, ensure_ascii=False),
                    }
                except Exception:
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(queue)

    return EventSourceResponse(
        generate(),
        ping=15,
    )


@app.get("/realtime", response_class=HTMLResponse)
async def realtime():
    """完整实时页面 HTML，供 Streamlit iframe 嵌入。"""
    return REALTIME_HTML


@app.get("/health")
async def health():
    return {"status": "ok", **bus.stats}


@app.get("/stats")
async def stats():
    return bus.stats
