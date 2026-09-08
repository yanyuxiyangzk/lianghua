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
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;padding:12px;color:#333}

/* ── Top bar: status + trigger ── */
.top-bar{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.conn{display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:16px;font-size:11px;font-weight:600;white-space:nowrap}
.conn.ok{background:#d4edda;color:#155724}.conn.warn{background:#fff3cd;color:#856404}.conn.err{background:#f8d7da;color:#721c24}
.conn .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.conn.ok .dot{background:#28a745}.conn.warn .dot{background:#ffc107;animation:blink 1s infinite}.conn.err .dot{background:#dc3545}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.trigger-btn{margin-left:auto;padding:5px 14px;border:none;border-radius:14px;font-size:11px;font-weight:600;cursor:pointer;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;transition:all .2s}
.trigger-btn:hover:not(:disabled){box-shadow:0 2px 8px rgba(102,126,234,.4);transform:translateY(-1px)}
.trigger-btn:disabled{opacity:.4;cursor:not-allowed;transform:none}

/* ── Progress bar ── */
.progress-wrap{display:flex;gap:2px;margin-bottom:10px;align-items:center}
.prog-seg{flex:1;height:6px;border-radius:3px;background:#dee2e6;transition:background .4s,box-shadow .4s}
.prog-seg.running{background:#ffc107;box-shadow:0 0 6px rgba(255,193,7,.5)}
.prog-seg.done{background:#28a745}.prog-seg.pass{background:#28a745}
.prog-seg.fail{background:#dc3545}.prog-seg.dup{background:#6c757d}
.prog-seg.frozen{background:#17a2b8}.prog-seg.skip{background:#adb5bd}
.prog-label{font-size:10px;color:#999;margin-left:6px;white-space:nowrap;min-width:50px;text-align:right}

/* ── KPI row ── */
.kpi-row{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.kpi{flex:1;min-width:70px;background:#fff;border-radius:8px;padding:8px 6px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.kpi .v{font-size:22px;font-weight:700;color:#222;transition:color .3s}
.kpi .v.pop{color:#667eea}
.kpi .l{font-size:10px;color:#999;margin-top:1px}
.kpi.k1{border-top:3px solid #11998e}.kpi.k2{border-top:3px solid #667eea}
.kpi.k3{border-top:3px solid #f7971e}.kpi.k4{border-top:3px solid #eb3349}

/* ── Steps ── */
.step{margin-bottom:3px;border-radius:6px;border:1px solid #e0e0e0;overflow:hidden;transition:border-color .3s}
.step.running{border-left:3px solid #ffc107}
.step.done,.step.pass{border-left:3px solid #28a745}
.step.fail{border-left:3px solid #dc3545}
.step.dup,.step.frozen,.step.skip{border-left:3px solid #adb5bd}
@keyframes pulse-left{0%,100%{border-left-color:#ffc107}50%{border-left-color:#ff9800}}
.step.running{animation:pulse-left 1.5s ease-in-out infinite}
.sh{display:flex;align-items:center;gap:8px;padding:7px 10px;cursor:pointer;user-select:none;background:#f8f9fa;transition:background .15s}
.sh:hover{background:#e9ecef}
.sh .ico{font-size:13px;width:18px;text-align:center;flex-shrink:0}
.sh .num{background:#6c757d;color:#fff;border-radius:50%;width:18px;height:18px;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;flex-shrink:0}
.sh .nm{font-size:12px;font-weight:600;color:#333;flex:1}
.sh .badge{font-size:10px;padding:1px 6px;border-radius:8px;background:#e0e0e0;color:#666;white-space:nowrap;transition:all .3s}
.sh .arr{font-size:9px;color:#999;transition:transform .2s;flex-shrink:0}
.sh.open .arr{transform:rotate(90deg)}
.sb{background:#1a1a1a;max-height:0;overflow:hidden;transition:max-height .3s ease}
.sb.open{max-height:350px;overflow-y:auto}
.sb pre{margin:0;padding:8px 10px;font-family:Consolas,'Courier New',monospace;font-size:10px;color:#d4d4d4;white-space:pre-wrap;word-break:break-all;line-height:1.5}

/* ── Round log ── */
.rlog{background:#1a1a1a;color:#d4d4d4;font-family:Consolas,'Courier New',monospace;font-size:10px;padding:8px 10px;border-radius:6px;height:130px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.5;margin-top:8px}
.ok{color:#4ec9b0}.err{color:#f44747}.warn{color:#f7971e}.info{color:#569cd6}
</style></head><body>

<div class="top-bar">
  <div id="conn" class="conn warn"><span class="dot"></span>连接中...</div>
  <button id="trigger" class="trigger-btn" onclick="doTrigger()">触发一轮</button>
</div>
<div class="progress-wrap" id="prog"></div>
<div class="kpi-row">
  <div class="kpi k1"><div class="v" id="kv-t">0</div><div class="l">测试</div></div>
  <div class="kpi k2"><div class="v" id="kv-p">0</div><div class="l">入库</div></div>
  <div class="kpi k3"><div class="v" id="kv-d">0</div><div class="l">重复</div></div>
  <div class="kpi k4"><div class="v" id="kv-f">0</div><div class="l">FSA拦截</div></div>
</div>
<div id="steps"></div>
<div class="rlog" id="rlog"></div>

<script>
(function(){
var SN=""" + json.dumps(STEP_NAMES, ensure_ascii=False) + """;
var N=SN.length;
var stepsEl=document.getElementById('steps'),rlogEl=document.getElementById('rlog');
var connEl=document.getElementById('conn'),btnEl=document.getElementById('trigger');
var kvT=document.getElementById('kv-t'),kvP=document.getElementById('kv-p'),
    kvD=document.getElementById('kv-d'),kvF=document.getElementById('kv-f');
var progEl=document.getElementById('prog');
var S=[],retryCount=0,rng=0;
var ICONS={pending:'○',running:'◉',done:'✓',pass:'✓',fail:'✗',skip:'–',dup:'≡',frozen:'◆'};
var LABELS={pending:'等待',running:'执行中',done:'完成',pass:'通过',fail:'失败',skip:'跳过',dup:'重复',frozen:'拦截'};
var BADGE_BG={done:'#d4edda',pass:'#d4edda',running:'#fff3cd',fail:'#f8d7da',dup:'#e2e3e5',frozen:'#d1ecf1',skip:'#e2e3e5',pending:'#e0e0e0'};
var BADGE_FG={done:'#155724',pass:'#155724',running:'#856404',fail:'#721c24',dup:'#383d41',frozen:'#0c5460',skip:'#383d41',pending:'#666'};

function init(){
  S=[];stepsEl.innerHTML='';progEl.innerHTML='';rng=0;
  for(var i=0;i<N;i++){
    S.push({status:'pending',logs:[],el:null,bodyEl:null,preEl:null,badgeEl:null});
    var div=document.createElement('div');div.className='step';div.id='s'+i;
    div.innerHTML='<div class="sh" onclick="tog('+i+')">'
      +'<span class="ico">'+ICONS.pending+'</span>'
      +'<span class="num">'+(i+1)+'</span>'
      +'<span class="nm">'+SN[i]+'</span>'
      +'<span class="badge" style="background:#e0e0e0;color:#666">等待</span>'
      +'<span class="arr">▶</span></div>'
      +'<div class="sb" id="b'+i+'"><pre></pre></div>';
    stepsEl.appendChild(div);
    S[i].el=div; S[i].bodyEl=div.querySelector('.sb'); S[i].preEl=div.querySelector('pre'); S[i].badgeEl=div.querySelector('.badge');
    var seg=document.createElement('div');seg.className='prog-seg';seg.id='pg'+i;
    progEl.appendChild(seg);
  }
  var lbl=document.createElement('div');lbl.className='prog-label';lbl.id='pg-lbl';lbl.textContent='0 / '+N;
  progEl.appendChild(lbl);
}
init();

function resetRound(){
  for(var i=0;i<N;i++){
    if(S[i].status==='done'||S[i].status==='pass')continue;
    S[i].status='pending';S[i].logs=[];
    S[i].preEl.innerHTML='';
    S[i].bodyEl.classList.remove('open');
    S[i].el.querySelector('.sh').classList.remove('open');
    patchStep(i,'pending');
  }
  rlogEl.innerHTML='';
  kvT.textContent='0';kvP.textContent='0';kvD.textContent='0';kvF.textContent='0';
}

window.tog=function(n){
  var sh=S[n].el.querySelector('.sh'),bd=S[n].bodyEl;
  sh.classList.toggle('open');bd.classList.toggle('open');
};

function patchStep(n,status){
  if(n<0||n>=N)return;
  var d=S[n];
  var changed=d.status!==status;
  d.status=status;
  var el=d.el,badge=d.badgeEl,ico=el.querySelector('.ico');
  // class on wrapper
  el.className='step '+status;
  // icon
  ico.textContent=ICONS[status]||'○';
  // badge
  badge.textContent=LABELS[status]||status;
  badge.style.background=BADGE_BG[status]||'#e0e0e0';
  badge.style.color=BADGE_FG[status]||'#666';
  // progress segment
  var seg=document.getElementById('pg'+n);
  if(seg)seg.className='prog-seg '+status;
  // count done segments
  var done=0;for(var i=0;i<N;i++){if(S[i].status!=='pending'&&S[i].status!=='running')done++;}
  var lbl=document.getElementById('pg-lbl');if(lbl)lbl.textContent=done+' / '+N;
}

function openStep(n){
  var sh=S[n].el.querySelector('.sh'),bd=S[n].bodyEl;
  if(!sh.classList.contains('open')){sh.classList.add('open');bd.classList.add('open');}
  bd.scrollTop=bd.scrollHeight;
}
function closeStep(n){
  var sh=S[n].el.querySelector('.sh'),bd=S[n].bodyEl;
  sh.classList.remove('open');bd.classList.remove('open');
}

function addLog(n,text,cls){
  S[n].logs.push('<div class="'+(cls||'')+'">'+text.replace(/</g,'&lt;')+'</div>');
  S[n].preEl.innerHTML=S[n].logs.join('');
  if(S[n].bodyEl.classList.contains('open'))S[n].bodyEl.scrollTop=S[n].bodyEl.scrollHeight;
}
function addRound(text,cls){
  var el=document.createElement('div');el.className=cls||'';el.textContent=text;
  rlogEl.appendChild(el);rlogEl.scrollTop=rlogEl.scrollHeight;
}
function animKPI(el,val){
  el.textContent=val;el.classList.add('pop');
  setTimeout(function(){el.classList.remove('pop');},600);
}
function setKPI(s){
  if(!s)return;
  if(s.tested!==undefined)animKPI(kvT,s.tested);
  if(s.passed!==undefined)animKPI(kvP,s.passed);
  if(s.dup!==undefined)animKPI(kvD,s.dup);
  if(s.frozen!==undefined)animKPI(kvF,s.frozen);
}

var collapseTimers={};
function autoCollapse(n){
  if(collapseTimers[n])clearTimeout(collapseTimers[n]);
  collapseTimers[n]=setTimeout(function(){closeStep(n);},2000);
}

function connect(){
  var es=new EventSource('/events');
  es.onopen=function(){
    retryCount=0;
    connEl.className='conn ok';connEl.innerHTML='<span class="dot"></span>已连接';
  };
  es.addEventListener('step_update',function(e){
    var d=JSON.parse(e.data),n=d.step-1;
    if(n<0||n>=N)return;
    patchStep(n,d.status);
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
    addLog(n,msg,cls);
    if(d.status==='running'){openStep(n);btnEl.disabled=true;}
    else if(d.status!=='pending'){autoCollapse(n);}
    if(n===1&&d.status==='done'&&d.gaps){
      addRound('  稀缺族:'+(d.gaps||[]).join(', ')+' 实战强族:'+(d.proven||[]).join(', '),'info');
      addRound('━'.repeat(50),'info');
    }
  });
  es.addEventListener('round_start',function(e){
    var d=JSON.parse(e.data);resetRound();
    btnEl.disabled=true;
    addRound('━'.repeat(50),'info');
    addRound('  第 '+d.iteration+' 轮 | '+d.factor_type+' | 批次:'+d.batch,'info');
  });
  es.addEventListener('gate_eval',function(e){
    var d=JSON.parse(e.data),icon=d.passed?'✅':'❌';
    var ic=d.metrics&&d.metrics.IC!==undefined?d.metrics.IC.toFixed(4):'-';
    var sh=d.metrics&&d.metrics['夏普2025']!==undefined?d.metrics['夏普2025'].toFixed(2):'-';
    var exc=d.metrics&&d.metrics['超额2025']!==undefined?(d.metrics['超额2025']*100).toFixed(1)+'%':'-';
    setKPI(d.stats_snapshot);
    addRound('  '+icon+' '+d.factor_name+' IC='+ic+' 夏普='+sh+' 超额='+exc,d.passed?'ok':'err');
  });
  es.addEventListener('gate_pass',function(e){
    var d=JSON.parse(e.data);
    addRound('  🎉 入库 → '+d.factor_name+' [族: '+d.family+']','ok');
  });
  es.addEventListener('round_complete',function(e){
    var d=JSON.parse(e.data),s=d.stats||{};
    addRound('━'.repeat(50),'info');
    addRound('  完成 | 测试:'+s.tested+' 入库:'+s.passed+' 重复:'+s.dup+' FSA:'+s.frozen,'info');
    if(d.new_factors&&d.new_factors.length>0)addRound('  新因子: '+d.new_factors.join('  '),'ok');
    addRound('━'.repeat(50),'info');
    setKPI(s);
    for(var i=0;i<N;i++){
      var st=S[i].status;
      if(st==='running'||st==='pending'||st==='done')patchStep(i,'done');
    }
    btnEl.disabled=false;
  });
  es.onerror=function(){
    retryCount++;
    if(retryCount>10){connEl.className='conn err';connEl.innerHTML='<span class="dot"></span>断开';es.close();btnEl.disabled=true;return;}
    connEl.className='conn warn';connEl.innerHTML='<span class="dot"></span>重连('+retryCount+'/10)';
  };
}

window.doTrigger=function(){
  btnEl.disabled=true;btnEl.textContent='触发中...';
  fetch('/trigger/loopengine',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
    if(d.status!=='started'){btnEl.disabled=false;btnEl.textContent='触发一轮';}
  }).catch(function(){btnEl.disabled=false;btnEl.textContent='触发一轮';});
};

connect();
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


@app.get("/market/events")
async def market_events(code: str = ""):
    """实时行情 SSE 端点：推送分时数据更新。"""
    if not code:
        return EventSourceResponse(generate_empty(), ping=15)

    async def generate():
        import time
        from datetime import datetime
        from zoneinfo import ZoneInfo

        while True:
            try:
                now = datetime.now(ZoneInfo("Asia/Shanghai"))
                hm = now.strftime("%H%M")
                is_weekday = now.weekday() < 5
                in_session = is_weekday and "0915" <= hm <= "1500"

                if in_session:
                    # 获取最新分钟数据
                    import datasource
                    data = datasource.get_minute_today(code)
                    if data and not data["minutes"].empty:
                        m = data["minutes"]
                        last = m.iloc[-1]
                        yield {
                            "event": "tick",
                            "data": json.dumps({
                                "code": code,
                                "time": last["time"],
                                "price": float(last["price"]),
                                "volume": float(last["volume"]),
                                "minute_vol": float(last["minute_vol"]),
                                "cum_amount": float(last["cum_amount"]),
                                "prev_close": data["prev_close"],
                                "name": data["name"],
                                "timestamp": now.isoformat(),
                            }, ensure_ascii=False),
                        }

                await asyncio.sleep(3 if "0915" <= hm < "0925" else 30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"market_events error: {e}")
                await asyncio.sleep(5)

    return EventSourceResponse(generate(), ping=15)


async def generate_empty():
    """空事件流，用于无代码参数时。"""
    while True:
        await asyncio.sleep(1)


@app.get("/realtime", response_class=HTMLResponse)
async def realtime():
    """完整实时页面 HTML，供 Streamlit iframe 嵌入。"""
    return REALTIME_HTML


@app.get("/health")
async def health():
    return {"status": "ok", **bus.stats}


@app.post("/trigger/loopengine")
async def trigger_loopengine():
    """触发一轮 LoopEngine 因子生成（同进程，共享 event_bus），按 iteration 轮转因子类型。"""
    import threading

    def _run():
        try:
            from loopengine.engine import LoopEngine, DEFAULT_FACTOR_TYPES
            eng = LoopEngine()
            factor_type = DEFAULT_FACTOR_TYPES[eng.state["iteration"] % len(DEFAULT_FACTOR_TYPES)]
            result = eng.run_round(batch=15, factor_type=factor_type)
            logger.info(f"trigger loopengine done: iter={result.get('iteration')} type={factor_type}")
        except Exception as e:
            logger.error(f"trigger loopengine failed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"status": "started"}


@app.get("/stats")
async def stats():
    return bus.stats
