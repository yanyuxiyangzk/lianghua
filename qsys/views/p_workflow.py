"""全流程工作流可视化 — 单页纵向流程图 + KPI仪表盘 + 阶段展开日志。"""

import json

import pandas as pd
import streamlit as st

st.set_page_config(page_title="全流程工作流", layout="wide")

# ---------- CSS ----------
st.markdown("""
<style>
/* KPI 卡片 */
.kpi-row { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
.kpi-card {
    flex: 1; min-width: 140px; background: #fff; border-radius: 10px;
    padding: 14px; box-shadow: 0 1px 6px rgba(0,0,0,0.06); text-align: center;
    border-top: 3px solid #667eea;
}
.kpi-card.c1 { border-top-color: #11998e; }
.kpi-card.c2 { border-top-color: #667eea; }
.kpi-card.c3 { border-top-color: #f7971e; }
.kpi-card.c4 { border-top-color: #eb3349; }
.kpi-card.c5 { border-top-color: #764ba2; }
.kpi-card.c6 { border-top-color: #11998e; }
.kpi-value { font-size: 24px; font-weight: 700; color: #222; }
.kpi-label { font-size: 11px; color: #999; margin-top: 2px; }

/* 纵向流程容器 */
.vflow-wrap { position: relative; margin: 0 0 0 6px; }
.vflow-line {
    position: absolute; left: 22px; top: 0; bottom: 0; width: 3px;
    background: linear-gradient(180deg, #667eea 0%, #764ba2 50%, #11998e 100%);
    border-radius: 2px;
}

/* 阶段卡片 */
.vf-stage {
    position: relative; margin-bottom: 16px; margin-left: 48px;
    background: #fff; border-radius: 10px; padding: 16px 20px;
    box-shadow: 0 1px 6px rgba(0,0,0,0.05); transition: all 0.15s;
    text-decoration: none; display: block; color: inherit;
}
.vf-stage:hover { box-shadow: 0 3px 12px rgba(0,0,0,0.1); transform: translateX(3px); }
.vf-stage-clickable { cursor: pointer; border: 2px solid transparent; }
.vf-stage-clickable:hover { border-color: #667eea; }

/* 阶段圆点 */
.vf-dot {
    position: absolute; left: -34px; top: 18px; width: 14px; height: 14px;
    border-radius: 50%; border: 2px solid #fff; z-index: 1;
}
.vf-dot.done { background: #11998e; box-shadow: 0 0 0 2px #11998e; }
.vf-dot.running { background: #f5576c; box-shadow: 0 0 0 2px #f5576c; animation: pulse 1.5s infinite; }
.vf-dot.pending { background: #ccc; box-shadow: 0 0 0 2px #ccc; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

/* 阶段标题 */
.vf-header { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.vf-icon { font-size: 18px; }
.vf-title { font-size: 15px; font-weight: 700; color: #333; }
.vf-sub { font-size: 11px; color: #999; margin-bottom: 10px; }

/* 步骤节点 */
.vf-steps { display: flex; gap: 6px; flex-wrap: wrap; }
.vf-step {
    background: #f5f6fa; border-radius: 6px; padding: 5px 10px;
    font-size: 11px; color: #555; display: flex; align-items: center; gap: 4px;
    border: 1px solid #e8e8e8;
}
.vf-step .d { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.vf-step .d.g { background: #11998e; }
.vf-step .d.r { background: #eb3349; }
.vf-step .d.y { background: #f7971e; }
.vf-step .d.n { background: #ddd; }

/* 箭头连接 */
.vf-arrow {
    position: relative; height: 28px; margin-left: 48px;
}
.vf-arrow::before {
    content: ''; position: absolute; left: -12px; top: 0; bottom: 0; width: 3px;
    background: linear-gradient(180deg, #667eea, #764ba2);
}
.vf-arrow::after {
    content: '▼'; position: absolute; left: -18px; bottom: -4px; font-size: 10px; color: #764ba2;
}

/* 调度时间线 */
.tl-wrap { display: flex; gap: 2px; align-items: flex-end; overflow-x: auto; padding: 8px 0; background: #fafafa; border-radius: 8px; padding: 12px; }
.tl-item { text-align: center; flex-shrink: 0; min-width: 60px; }
.tl-bar { width: 36px; margin: 0 auto 3px; border-radius: 3px 3px 0 0; min-height: 8px; }
.tl-lbl { font-size: 9px; color: #666; line-height: 1.2; }
.tl-time { font-size: 8px; color: #999; }

/* 因子类型演化 */
.type-bar-wrap{background:#fff;border-radius:10px;padding:16px 20px;box-shadow:0 1px 6px rgba(0,0,0,.05);margin-bottom:16px}
.type-bar-title{font-size:14px;font-weight:700;color:#333;margin-bottom:12px}
.type-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.type-label{width:60px;font-size:11px;color:#666;text-align:right;flex-shrink:0}
.type-track{flex:1;height:22px;background:#f0f0f0;border-radius:4px;overflow:hidden;position:relative}
.type-fill{height:100%;border-radius:4px;transition:width .6s ease;display:flex;align-items:center;justify-content:flex-end;padding-right:6px;font-size:10px;font-weight:600;color:#fff;min-width:0}
.type-fillspan{position:absolute;right:6px;top:50%;transform:translateY(-50%);font-size:10px;font-weight:600;color:#333;white-space:nowrap}

/* 轮转指示器 */
.rot-wrap{display:flex;align-items:center;gap:6px;margin-bottom:16px;padding:10px 16px;background:#fff;border-radius:10px;box-shadow:0 1px 6px rgba(0,0,0,.05)}
.rot-label{font-size:12px;color:#999;margin-right:4px}
.rot-dot{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;color:#999;background:#f0f0f0;transition:all .3s}
.rot-dot.active{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;box-shadow:0 2px 8px rgba(102,126,234,.4);transform:scale(1.15)}
.rot-dot.done{background:#d4edda;color:#155724}
.rot-arrow{color:#ccc;font-size:10px}

/* 实时进度组件 */
.lp-wrap{background:#fff;border-radius:10px;padding:14px 18px;box-shadow:0 1px 6px rgba(0,0,0,.05);margin:-4px 0 16px 48px;border-left:3px solid #667eea}
.lp-header{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.lp-pulse{width:8px;height:8px;border-radius:50%;background:#11998e;flex-shrink:0}
.lp-pulse.running{animation:lpPulse 1.2s infinite}
@keyframes lpPulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(17,153,142,.6)}50%{opacity:.7;box-shadow:0 0 0 6px rgba(17,153,142,0)}}
.lp-round{font-size:13px;font-weight:700;color:#333}
.lp-type{font-size:11px;padding:2px 8px;border-radius:10px;color:#fff;font-weight:600}
.lp-time{font-size:11px;color:#999;margin-left:auto;font-family:monospace}
.lp-bar{display:flex;gap:3px;margin-bottom:6px;height:18px}
.lp-seg{flex:1;border-radius:3px;background:#f0f0f0;transition:background .3s;position:relative}
.lp-seg.done{background:#11998e}
.lp-seg.running{background:#f7971e;animation:segPulse 1s infinite}
.lp-seg.fail{background:#eb3349}
.lp-seg.dup{background:#adb5bd}
.lp-seg.frozen{background:#667eea}
@keyframes segPulse{0%,100%{opacity:1}50%{opacity:.5}}
.lp-step{font-size:11px;color:#666;display:flex;align-items:center;gap:6px}
.lp-step b{color:#333}
.lp-kpi{display:flex;gap:12px;margin-top:6px;font-size:10px;color:#999}
.lp-kpi span{display:flex;align-items:center;gap:3px}
.lp-idle{font-size:11px;color:#999;font-style:italic}

/* 多阶段状态面板 */
.lp-stages{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:-4px 0 16px 48px}
.lp-stage-card{background:#fff;border-radius:10px;padding:12px 14px;box-shadow:0 1px 6px rgba(0,0,0,.05);border-left:3px solid #ddd;transition:border-color .3s}
.lp-stage-card.running{border-left-color:#f7971e}
.lp-stage-card.done{border-left-color:#11998e}
.lp-stage-card.idle{border-left-color:#ddd}
.lp-stage-head{display:flex;align-items:center;gap:6px;margin-bottom:4px}
.lp-stage-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.lp-stage-dot.running{background:#f7971e;animation:lpPulse 1.2s infinite}
.lp-stage-dot.done{background:#11998e}
.lp-stage-dot.idle{background:#ddd}
.lp-stage-name{font-size:12px;font-weight:700;color:#333}
.lp-stage-job{font-size:10px;color:#999;font-family:monospace}
.lp-stage-msg{font-size:10px;color:#666;margin-top:3px;line-height:1.3;max-height:28px;overflow:hidden}
.lp-stage-next{font-size:9px;color:#aaa;margin-top:2px}
</style>
""", unsafe_allow_html=True)


# ---------- 数据加载 ----------
@st.cache_data(ttl=30)
def _load_data():
    import library
    from common import DATA_DIR
    data = {}
    try:
        with library._lconn() as c:
            r1 = c.execute("""
                SELECT
                    (SELECT iteration FROM engine_state WHERE id='loopengine') as iter,
                    (SELECT accepted FROM engine_state WHERE id='loopengine') as acc,
                    (SELECT budget FROM engine_state WHERE id='loopengine') as budget,
                    (SELECT COUNT(*) FROM gate_detail_log) as gate_t,
                    (SELECT COUNT(*) FROM gate_detail_log WHERE passed=1) as gate_p,
                    (SELECT COUNT(*) FROM tested_hashes) as tested_t,
                    (SELECT COUNT(*) FROM tested_hashes WHERE passed=1) as tested_p,
                    (SELECT COUNT(*) FROM failure_patterns) as fail_count
            """).fetchone()
            if r1:
                try:
                    budget = json.loads(r1[2]) if r1[2] else {}
                except (json.JSONDecodeError, TypeError):
                    budget = {}
                data.update({"iteration": r1[0] or 0, "accepted": r1[1] or 0,
                    "budget": budget,
                    "gate_total": r1[3] or 0, "gate_passed": r1[4] or 0,
                    "tested_total": r1[5] or 0, "tested_passed": r1[6] or 0,
                    "failure_count": r1[7] or 0})

            r2 = c.execute("""
                SELECT
                    (SELECT COUNT(*) FROM factor_registry WHERE engine='loopengine') as reg_loop,
                    (SELECT COUNT(DISTINCT name) FROM factor_scorecards) as sc_count,
                    (SELECT COUNT(*) FROM strategies) as st_count,
                    (SELECT COUNT(*) FROM factor_registry) as reg_total
            """).fetchone()
            if r2:
                data.update({"registry_loop": r2[0] or 0, "scorecard_count": r2[1] or 0,
                    "strategy_count": r2[2] or 0, "registry_total": r2[3] or 0})

            data["gate_recent"] = [{"name": r[0], "passed": r[1],
                "metrics": json.loads(r[2]) if r[2] else {}, "reasons": json.loads(r[3]) if r[3] else [],
                "date": r[4]} for r in c.execute(
                "SELECT factor_name, passed, metrics, fail_reasons, gate_date FROM gate_detail_log ORDER BY id DESC LIMIT 5")]

            data["sched_recent"] = [{"key": r[0], "name": r[1], "time": r[2],
                "success": r[3], "dur": r[4], "msg": r[5]} for r in c.execute(
                "SELECT job_key, job_name, started_at, success, duration_ms, message FROM sched_exec_log ORDER BY id DESC LIMIT 10")]

            data["family_top"] = [{"family": r[0], "count": r[1]} for r in c.execute(
                "SELECT family, COUNT(*) FROM factor_registry WHERE engine='loopengine' AND family IS NOT NULL GROUP BY family ORDER BY 2 DESC LIMIT 5")]

            data["type_dist"] = [{"type": r[0] or "未分类", "total": r[1], "passed": r[2]}
                for r in c.execute(
                "SELECT factor_type, COUNT(*), SUM(CASE WHEN gate_status=1 THEN 1 ELSE 0 END) "
                "FROM factor_registry WHERE engine='loopengine' GROUP BY factor_type ORDER BY 2 DESC")]

            data["recent_mines"] = [{"msg": r[0], "time": r[1]} for r in c.execute(
                "SELECT message, started_at FROM sched_exec_log "
                "WHERE job_key='loopengine' ORDER BY id DESC LIMIT 12")]

        # picks/positions 在 experience.db
        exp_db = DATA_DIR / "experience.db"
        if exp_db.exists():
            import sqlite3
            with sqlite3.connect(str(exp_db)) as ec:
                r3 = ec.execute("""
                    SELECT
                        (SELECT COUNT(*) FROM picks) as pick_total,
                        (SELECT COUNT(*) FROM picks WHERE trade_date=(SELECT MAX(trade_date) FROM picks)) as pick_today,
                        (SELECT COUNT(*) FROM positions WHERE status='open') as pos_open,
                        (SELECT COUNT(*) FROM positions WHERE status='closed') as pos_closed,
                        (SELECT SUM(pnl_pct) FROM positions WHERE status='closed') as total_pnl,
                        (SELECT COUNT(*) FROM positions WHERE status='closed' AND pnl_pct > 0) as win_count
                """).fetchone()
                if r3:
                    data.update({"pick_total": r3[0] or 0, "pick_today": r3[1] or 0,
                        "pos_open": r3[2] or 0, "pos_closed": r3[3] or 0,
                        "total_pnl": r3[4] or 0, "win_count": r3[5] or 0})

                data["picks_recent"] = [{"date": r[0], "method": r[1], "codes": r[2]} for r in ec.execute(
                    "SELECT p.trade_date, p.method, GROUP_CONCAT(pi.code) FROM picks p JOIN pick_items pi ON p.id=pi.pick_id GROUP BY p.id ORDER BY p.trade_date DESC LIMIT 3")]
        else:
            data.update({"pick_total": 0, "pick_today": 0, "pos_open": 0,
                         "pos_closed": 0, "total_pnl": 0, "win_count": 0, "picks_recent": []})
    except Exception as e:
        data["error"] = str(e)
    return data


# ---------- KPI 仪表盘 ----------
def _render_kpi(data):
    gt, gp = data.get("gate_total", 0), data.get("gate_passed", 0)
    po, pc = data.get("pos_open", 0), data.get("pos_closed", 0)
    win, pnl = data.get("win_count", 0), data.get("total_pnl", 0)
    rate = f"{gp/gt:.1%}" if gt > 0 else "-"
    wr = f"{win/pc:.0%}" if pc > 0 else "-"
    ps = f"{pnl:.1f}%" if pnl else "-"

    st.markdown(f"""<div class="kpi-row">
<div class="kpi-card c1"><div class="kpi-value">{data.get('iteration',0)}</div><div class="kpi-label">累计迭代</div></div>
<div class="kpi-card c2"><div class="kpi-value">{data.get('registry_loop',0)}</div><div class="kpi-label">入库因子</div></div>
<div class="kpi-card c3"><div class="kpi-value">{rate}</div><div class="kpi-label">闸门通过率</div></div>
<div class="kpi-card c4"><div class="kpi-value">{po}</div><div class="kpi-label">当前持仓</div></div>
<div class="kpi-card c5"><div class="kpi-value">{wr}</div><div class="kpi-label">持仓胜率</div></div>
<div class="kpi-card c6"><div class="kpi-value">{ps}</div><div class="kpi-label">累计盈亏</div></div>
</div>""", unsafe_allow_html=True)


# ---------- 纵向流程组件 ----------
def _vf_stage(icon, title, subtitle, steps, status="done", link_url=None):
    sh = "".join(f'<div class="vf-step"><span class="d {s}"></span>{n}</div>' for n, s in steps)
    cls = "vf-stage vf-stage-clickable" if link_url else "vf-stage"
    if link_url:
        return f"""<a href="{link_url}" class="{cls}"><div class="vf-dot {status}"></div>
<div class="vf-header"><span class="vf-icon">{icon}</span><span class="vf-title">{title}</span></div>
<div class="vf-sub">{subtitle}</div><div class="vf-steps">{sh}</div></a>"""
    return f"""<div class="{cls}"><div class="vf-dot {status}"></div>
<div class="vf-header"><span class="vf-icon">{icon}</span><span class="vf-title">{title}</span></div>
<div class="vf-sub">{subtitle}</div><div class="vf-steps">{sh}</div></div>"""


def _vf_arrow():
    return '<div class="vf-arrow"></div>'


# ---------- 阶段渲染 ----------
def _render_gen(data):
    it, acc = data.get("iteration", 0), data.get("accepted", 0)
    gt, gp = data.get("gate_total", 0), data.get("gate_passed", 0)
    tt, fail = data.get("tested_total", 0), data.get("failure_count", 0)
    rl = data.get("registry_loop", 0)
    h = it > 0
    rate = f"{gp/gt:.1%}" if gt > 0 else "-"
    steps = [("构建面板", "g" if h else "n"), ("机制族引导", "g" if h else "n"),
             ("FSA重算", "g" if h else "n"), ("生成候选", "g" if h else "n"),
             ("规则审查", "g" if fail > 0 or tt > 0 else "n"),
             ("LLM审查", "g" if tt > 0 else "n"), ("去重", "g" if tt > 0 else "n"),
             ("FSA拦截", "g" if tt > 0 else "n"),
             ("硬闸门", "g" if gt > 0 else ("y" if h else "n")), ("入库", "g" if rl > 0 else "n")]
    st.markdown(_vf_stage("🧬", "因子生成 LoopEngine", f"迭代 {it} 轮 · 入库 {acc} 个 · 通过率 {rate}", steps,
                          "done" if rl > 0 else ("running" if h else "pending"),
                          link_url="/le-realtime" if h else None), unsafe_allow_html=True)


def _render_stage_status_bar(data):
    """紧凑状态条：4个阶段一行显示 + 下一轮任务预告，SSE 实时更新。"""
    sse_port = st.session_state.get("sse_port", 8502)
    sse_url = f"http://localhost:{sse_port}"
    ft_colors = json.dumps(FACTOR_TYPE_COLORS)

    last_runs = {}
    for r in data.get("sched_recent", []):
        k = r["key"]
        if k not in last_runs:
            last_runs[k] = r

    # 计算下一轮任务信息
    iteration = data.get("iteration", 0)
    factor_types = ["量价", "资金流", "板块轮动", "指数", "盘口异动", "龙虎榜", "爆量抢筹"]
    next_type = factor_types[iteration % len(factor_types)]
    next_iteration = iteration + 1

    # 类型描述
    type_desc = {
        "量价": "K线形态/量价关系",
        "资金流": "主力资金流向",
        "板块轮动": "行业板块轮动",
        "指数": "指数相关性",
        "盘口异动": "盘口买卖盘变化",
        "龙虎榜": "龙虎榜数据",
        "爆量抢筹": "盘口吸筹信号",
    }
    next_desc = type_desc.get(next_type, next_type)

    # 类型颜色
    type_colors = {
        "量价": "#667eea", "资金流": "#11998e", "板块轮动": "#f7971e",
        "指数": "#eb3349", "盘口异动": "#764ba2", "龙虎榜": "#e91e63",
        "爆量抢筹": "#ff5722",
    }
    next_color = type_colors.get(next_type, "#999")

    # 轮转进度条
    rotation_html = ""
    for i, ft in enumerate(factor_types):
        is_current = (i == (iteration - 1) % len(factor_types)) and iteration > 0
        is_next = i == (iteration % len(factor_types))
        bg = type_colors.get(ft, "#ddd")
        opacity = "1" if is_current or is_next else "0.3"
        border = f"2px solid {bg}" if is_next else "1px solid #ddd"
        rotation_html += f'<div style="width:18px;height:18px;border-radius:50%;background:{bg};opacity:{opacity};border:{border};display:flex;align-items:center;justify-content:center;font-size:7px;color:#fff;font-weight:700" title="{ft}">{ft[0]}</div>'
        if i < len(factor_types) - 1:
            rotation_html += '<div style="width:12px;height:2px;background:#ddd;align-self:center"></div>'

    stages_json = json.dumps([
        {"id": "gen", "label": "因子生成", "icon": "🧬",
         "jobs": ["loopengine", "event_mine", "fundflow_sync", "lhb_sync"]},
        {"id": "eval", "label": "因子回测", "icon": "📊",
         "jobs": ["le_factor_eval", "gate_check"]},
        {"id": "pick", "label": "自动选股", "icon": "🎯",
         "jobs": ["pool_scan", "auto_scan", "watchlist_signals", "auction_confirm"]},
        {"id": "pos", "label": "持仓管理", "icon": "💼",
         "jobs": ["position_track", "trade_simulate", "minute_sync"]},
    ])

    last_runs_json = json.dumps({k: {"time": v["time"], "ok": v.get("ok", v.get("success", True)),
                                      "msg": (v.get("msg") or "")[:60]}
                                 for k, v in last_runs.items()})

    html = f"""<div id="ssb-bar" style="display:flex;gap:8px;margin-bottom:14px">
<div id="ssb-gen" class="ssb-item" style="flex:1;display:flex;align-items:center;gap:6px;padding:8px 12px;background:#f8f9fa;border-radius:8px;border-left:3px solid #ddd">
  <span class="ssb-dot" id="sb-dot-gen" style="width:8px;height:8px;border-radius:50%;background:#ddd;flex-shrink:0"></span>
  <span style="font-size:12px;font-weight:700;color:#333">🧬 因子生成</span>
  <span id="sb-info-gen" style="font-size:10px;color:#999;margin-left:auto">等待中</span>
</div>
<div id="ssb-eval" class="ssb-item" style="flex:1;display:flex;align-items:center;gap:6px;padding:8px 12px;background:#f8f9fa;border-radius:8px;border-left:3px solid #ddd">
  <span class="ssb-dot" id="sb-dot-eval" style="width:8px;height:8px;border-radius:50%;background:#ddd;flex-shrink:0"></span>
  <span style="font-size:12px;font-weight:700;color:#333">📊 因子回测</span>
  <span id="sb-info-eval" style="font-size:10px;color:#999;margin-left:auto">等待中</span>
</div>
<div id="ssb-pick" class="ssb-item" style="flex:1;display:flex;align-items:center;gap:6px;padding:8px 12px;background:#f8f9fa;border-radius:8px;border-left:3px solid #ddd">
  <span class="ssb-dot" id="sb-dot-pick" style="width:8px;height:8px;border-radius:50%;background:#ddd;flex-shrink:0"></span>
  <span style="font-size:12px;font-weight:700;color:#333">🎯 自动选股</span>
  <span id="sb-info-pick" style="font-size:10px;color:#999;margin-left:auto">等待中</span>
</div>
<div id="ssb-pos" class="ssb-item" style="flex:1;display:flex;align-items:center;gap:6px;padding:8px 12px;background:#f8f9fa;border-radius:8px;border-left:3px solid #ddd">
  <span class="ssb-dot" id="sb-dot-pos" style="width:8px;height:8px;border-radius:50%;background:#ddd;flex-shrink:0"></span>
  <span style="font-size:12px;font-weight:700;color:#333">💼 持仓管理</span>
  <span id="sb-info-pos" style="font-size:10px;color:#999;margin-left:auto">等待中</span>
</div>
</div>
<!-- LoopEngine 运行进度（仅运行时显示） -->
<div id="lp-wrap" style="display:none;background:#fff;border-radius:8px;padding:10px 14px;box-shadow:0 1px 4px rgba(0,0,0,.05);margin-bottom:12px">
<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
  <div id="lp-pulse" style="width:8px;height:8px;border-radius:50%;background:#11998e"></div>
  <span id="lp-round" style="font-size:12px;font-weight:700;color:#333">第 {data.get("iteration",0)} 轮</span>
  <span id="lp-type" style="font-size:10px;padding:1px 6px;border-radius:8px;color:#fff;font-weight:600;background:#ddd">-</span>
  <span id="lp-time" style="font-size:10px;color:#999;margin-left:auto;font-family:monospace">--:--:--</span>
</div>
<div id="lp-bar" style="display:flex;gap:2px;margin-bottom:4px">
  <div class="lp-seg" data-step="1"></div><div class="lp-seg" data-step="2"></div>
  <div class="lp-seg" data-step="3"></div><div class="lp-seg" data-step="4"></div>
  <div class="lp-seg" data-step="5"></div><div class="lp-seg" data-step="6"></div>
  <div class="lp-seg" data-step="7"></div><div class="lp-seg" data-step="8"></div>
  <div class="lp-seg" data-step="9"></div><div class="lp-seg" data-step="10"></div>
</div>
<div style="display:flex;align-items:center;gap:8px">
  <span id="lp-step-name" style="font-size:10px;color:#666">等待启动...</span>
  <span id="lp-kpi" style="font-size:9px;color:#999;margin-left:auto;display:none">
    测试 <b id="lp-tested">0</b> · 过审 <b id="lp-passed" style="color:#11998e">0</b>
    · 重复 <b id="lp-dup" style="color:#adb5bd">0</b> · FSA <b id="lp-frozen" style="color:#667eea">0</b>
  </span>
</div>
</div>
<!-- 下一轮任务预告 -->
<div style="background:#f8f9fa;border-radius:8px;padding:10px 14px;margin-bottom:12px;border:1px dashed #dee2e6">
<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
  <span style="font-size:11px;color:#666">⏭️ 下一轮</span>
  <span style="font-size:12px;font-weight:700;color:#333">第{next_iteration}轮</span>
  <span style="font-size:10px;padding:2px 8px;border-radius:10px;color:#fff;font-weight:600;background:{next_color}">{next_type}</span>
  <span style="font-size:10px;color:#999;margin-left:auto">{next_desc}</span>
</div>
<div style="display:flex;align-items:center;gap:4px">
  <span style="font-size:9px;color:#999;margin-right:4px">轮转:</span>
  {rotation_html}
</div>
</div>

<style>
@keyframes sbPulse{{0%,100%{{opacity:1}}50%{{opacity:.5}}}}
@keyframes lpPulse{{0%,100%{{opacity:1;box-shadow:0 0 0 0 rgba(17,153,142,.5)}}50%{{opacity:.7;box-shadow:0 0 0 4px rgba(17,153,142,0)}}}}
.ssb-item{{transition:border-color .3s,background .3s}}
.ssb-item.running{{border-left-color:#f7971e;background:#fff8f0}}
.ssb-item.done{{border-left-color:#11998e}}
.ssb-item.fail{{border-left-color:#eb3349}}
.lp-seg{{flex:1;border-radius:2px;height:14px;background:#f0f0f0;transition:background .3s}}
.lp-seg.done{{background:#11998e}}
.lp-seg.running{{background:#f7971e;animation:lpPulse 1s infinite}}
.lp-seg.fail{{background:#eb3349}}
.lp-seg.dup{{background:#adb5bd}}
.lp-seg.frozen{{background:#667eea}}
</style>

<script>
(function(){{
  const STAGES = {stages_json};
  const LAST_RUNS = {last_runs_json};
  const COLORS = {ft_colors};
  const STEP_NAMES = ['构建面板','机制族引导','FSA重算','生成候选','规则审查','LLM审查','去重','FSA拦截','硬闸门','入库'];
  const JOB_TO_STAGE = {{}};
  STAGES.forEach(s => s.jobs.forEach(j => {{ JOB_TO_STAGE[j] = s.id; }}));

  function setStage(sid, state, text) {{
    const el = document.getElementById('ssb-' + sid);
    const dot = document.getElementById('sb-dot-' + sid);
    const info = document.getElementById('sb-info-' + sid);
    if (!el) return;
    el.className = 'ssb-item ' + state;
    if (dot) dot.style.background = state === 'running' ? '#f7971e' : state === 'done' ? '#11998e' : state === 'fail' ? '#eb3349' : '#ddd';
    if (info) info.textContent = text;
  }}

  // 初始化
  STAGES.forEach(s => {{
    let latest = null;
    for (const jk of s.jobs) {{ if (LAST_RUNS[jk] && (!latest || LAST_RUNS[jk].time > latest.time)) latest = LAST_RUNS[jk]; }}
    if (latest) {{
      const t = latest.time.substring(11, 16);
      setStage(s.id, latest.ok ? 'done' : 'fail', (latest.ok ? '✅ ' : '❌ ') + t + ' ' + (latest.msg || ''));
    }}
  }});

  const segs = document.querySelectorAll('.lp-seg');
  const wrapEl = document.getElementById('lp-wrap');
  const pulseEl = document.getElementById('lp-pulse');
  const roundEl = document.getElementById('lp-round');
  const typeEl = document.getElementById('lp-type');
  const timeEl = document.getElementById('lp-time');
  const stepEl = document.getElementById('lp-step-name');
  const kpiEl = document.getElementById('lp-kpi');

  function resetBar() {{ segs.forEach(s => {{ s.className = 'lp-seg'; }}); }}
  function updateBar(step, status) {{
    if (step > 0) {{
      for (let i = 1; i < step; i++) segs[i-1].className = 'lp-seg done';
      const cls = status === 'done' ? 'lp-seg done' : status === 'fail' ? 'lp-seg fail' :
                  status === 'dup' ? 'lp-seg dup' : status === 'frozen' ? 'lp-seg frozen' : 'lp-seg running';
      segs[step-1].className = cls;
    }}
  }}
  setInterval(function() {{ timeEl.textContent = new Date().toLocaleTimeString('zh-CN',{{hour12:false}}); }}, 1000);
  timeEl.textContent = new Date().toLocaleTimeString('zh-CN',{{hour12:false}});

  const es = new EventSource('{sse_url}/events');
  es.addEventListener('job_start', function(e) {{
    try {{ const d = JSON.parse(e.data); const sid = JOB_TO_STAGE[d.job_key]; if (sid) setStage(sid, 'running', '🔄 ' + d.job_name); }} catch(err) {{}}
  }});
  es.addEventListener('job_end', function(e) {{
    try {{
      const d = JSON.parse(e.data); const sid = JOB_TO_STAGE[d.job_key]; const t = new Date().toLocaleTimeString('zh-CN',{{hour12:false}}).substring(0,5);
      if (sid) setStage(sid, d.success ? 'done' : 'fail', (d.success ? '✅ ' : '❌ ') + t + ' ' + (d.message || ''));
    }} catch(err) {{}}
  }});
  es.addEventListener('round_start', function(e) {{
    try {{
      const d = JSON.parse(e.data); wrapEl.style.display = 'block'; resetBar();
      roundEl.textContent = '第 ' + (d.iteration || '?') + ' 轮';
      const ft = d.factor_type || '?'; typeEl.textContent = ft; typeEl.style.background = COLORS[ft] || '#999';
      pulseEl.style.animation = 'lpPulse 1.2s infinite';
      stepEl.innerHTML = '<b>启动中...</b>'; kpiEl.style.display = 'inline';
      ['lp-tested','lp-passed','lp-dup','lp-frozen'].forEach(id => {{ document.getElementById(id).textContent = '0'; }});
    }} catch(err) {{}}
  }});
  es.addEventListener('step_update', function(e) {{
    try {{
      const d = JSON.parse(e.data);
      if (d.step > 0) {{ updateBar(d.step, d.status); stepEl.innerHTML = '<b>' + STEP_NAMES[d.step-1] + '</b>' + (d.status === 'done' ? ' ✅' : d.status === 'fail' ? ' ❌' : d.status === 'dup' ? ' ⏭' : d.status === 'frozen' ? ' 🔒' : ' ⏳'); }}
      if (d.stats) {{ document.getElementById('lp-tested').textContent = d.stats.tested||0; document.getElementById('lp-passed').textContent = d.stats.passed||0; document.getElementById('lp-dup').textContent = d.stats.dup||0; document.getElementById('lp-frozen').textContent = d.stats.frozen||0; }}
    }} catch(err) {{}}
  }});
  es.addEventListener('gate_eval', function(e) {{
    try {{ const d = JSON.parse(e.data); if(d.stats){{ document.getElementById('lp-tested').textContent=d.stats.tested||0; document.getElementById('lp-passed').textContent=d.stats.passed||0; document.getElementById('lp-dup').textContent=d.stats.dup||0; document.getElementById('lp-frozen').textContent=d.stats.frozen||0; }} }} catch(err) {{}}
  }});
  es.addEventListener('round_complete', function(e) {{
    try {{
      const d = JSON.parse(e.data); segs.forEach(s => {{ s.className = 'lp-seg done'; }}); pulseEl.style.animation = 'none';
      const s = d.stats || {{}}; stepEl.innerHTML = '<b>完成</b> · 测试 ' + (s.tested||0) + ' · 入库 ' + (s.passed||0);
      if (d.new_factors && d.new_factors.length) stepEl.innerHTML += ' · ' + d.new_factors.slice(0,3).join(', ');
      setTimeout(function() {{ wrapEl.style.display = 'none'; }}, 5000);
    }} catch(err) {{}}
  }});
  es.onerror = function() {{ stepEl.textContent = 'SSE 未连接'; pulseEl.style.animation = 'none'; }};
}})();
</script>"""
    st.components.v1.html(html, height=90, scrolling=False)


def _render_eval(data):
    sc, stc = data.get("scorecard_count", 0), data.get("strategy_count", 0)
    tp = data.get("tested_passed", 0)
    steps = [("因子体检", "g" if sc > 0 else "n"), ("多周期胜率", "g" if sc > 0 else "n"),
             ("去冗余", "g" if sc > 0 else "n"), ("组合搜索", "y"),
             ("Walk-Forward", "y"), ("策略固化", "g" if stc > 0 else "n")]
    st.markdown(_vf_stage("📊", "因子回测", f"已体检 {sc} 个 · 策略包 {stc} 个 · 通过闸门 {tp} 个", steps,
                          "done" if stc > 0 else ("running" if sc > 0 else "pending"),
                          link_url="/factor-stats" if sc > 0 else None), unsafe_allow_html=True)


def _render_pick(data):
    pt, ptd = data.get("pick_total", 0), data.get("pick_today", 0)
    po, pc = data.get("pos_open", 0), data.get("pos_closed", 0)
    steps = [("板块扫描", "g" if pt > 0 else "n"), ("因子打分", "g" if pt > 0 else "n"),
             ("Top-N选股", "g" if ptd > 0 else "n"), ("竞价确认", "g" if ptd > 0 else "n"),
             ("开仓", "g" if po > 0 or pc > 0 else "n"), ("止盈止损", "g" if pc > 0 else "n"),
             ("战果回填", "g" if pc > 0 else "n")]
    st.markdown(_vf_stage("🎯", "自动选股", f"今日 {ptd} 只 · 持仓 {po} 只 · 已平 {pc} 只", steps,
                          "done" if pc > 0 else ("running" if po > 0 else "pending"),
                          link_url="/picker" if pt > 0 else None), unsafe_allow_html=True)


def _render_pos(data):
    po, pc = data.get("pos_open", 0), data.get("pos_closed", 0)
    win, pnl = data.get("win_count", 0), data.get("total_pnl", 0)
    wr = f"{win/pc:.0%}" if pc > 0 else "-"
    ps = f"{pnl:.1f}%" if pnl else "-"
    steps = [("限价委托", "g" if po > 0 or pc > 0 else "n"), ("成交确认", "g" if po > 0 or pc > 0 else "n"),
             ("止盈+15%", "g" if pc > 0 else "n"), ("止损-8%", "g" if pc > 0 else "n"),
             ("到期平仓", "g" if pc > 0 else "n"), ("战果记录", "g" if pc > 0 else "n")]
    st.markdown(_vf_stage("💼", "持仓管理", f"持仓 {po} 只 · 胜率 {wr} · 盈亏 {ps}", steps,
                          "done" if pc > 0 else ("running" if po > 0 else "pending"),
                          link_url="/trades" if po > 0 or pc > 0 else None), unsafe_allow_html=True)


# ---------- 调度时间线 ----------
def _render_timeline(data):
    recent = data.get("sched_recent", [])
    if not recent:
        return
    job_latest = {}
    for r in recent:
        if r["key"] not in job_latest:
            job_latest[r["key"]] = r

    jobs = [("update_data", "数据更新"), ("ifind_daily_sync", "iFinD入库"),
            ("gate_check", "硬闸门"), ("top5_composite", "Top5"),
            ("pool_scan", "选股"), ("le_factor_eval", "体检"), ("event_mine", "事件")]

    items = ""
    for key, label in jobs:
        r = job_latest.get(key)
        if r:
            c = "#11998e" if r["success"] else "#eb3349"
            m = "✅" if r["success"] else "❌"
            t = r["time"][11:16] if r["time"] else "-"
            h = max(12, min(60, (r["dur"] or 0) / 20))
            items += f'<div class="tl-item"><div class="tl-bar" style="height:{h}px;background:{c}"></div><div class="tl-lbl">{m}{label}</div><div class="tl-time">{t}</div></div>'
        else:
            items += f'<div class="tl-item"><div class="tl-bar" style="height:8px;background:#ddd"></div><div class="tl-lbl">⏳{label}</div><div class="tl-time">-</div></div>'
    st.markdown(f'<div class="tl-wrap">{items}</div>', unsafe_allow_html=True)


# ---------- 因子类型演化 ----------
FACTOR_TYPE_COLORS = {
    "量价": "#667eea", "资金流": "#11998e", "板块轮动": "#f7971e",
    "指数": "#eb3349", "盘口异动": "#764ba2", "龙虎榜": "#569cd6", "未分类": "#adb5bd"
}
FACTOR_TYPES_ORDER = ["量价", "资金流", "板块轮动", "指数", "盘口异动", "龙虎榜"]

def _render_rotation(data):
    iteration = data.get("iteration", 0)
    current_idx = iteration % 6 if iteration > 0 else -1
    current_type = FACTOR_TYPES_ORDER[current_idx] if current_idx >= 0 else "-"

    dots = ""
    for i, ft in enumerate(FACTOR_TYPES_ORDER):
        cls = "active" if i == current_idx else ("done" if i < current_idx else "")
        short = ft[:2]
        dots += f'<div class="rot-dot {cls}">{short}</div>'
        if i < 5:
            dots += '<span class="rot-arrow">→</span>'
    st.markdown(f"""<div class="rot-wrap">
        <span class="rot-label">当前轮转:</span>{dots}
    </div>""", unsafe_allow_html=True)


def _render_type_chart(data):
    type_dist = data.get("type_dist", [])
    if not type_dist:
        return

    max_val = max((d["total"] for d in type_dist), default=1) or 1
    rows_html = ""
    for d in type_dist:
        ft = d["type"]
        color = FACTOR_TYPE_COLORS.get(ft, "#adb5bd")
        pct = d["total"] / max_val * 100
        passed = d["passed"]
        total = d["total"]
        rate = f"{passed/total:.0%}" if total > 0 else "-"
        rows_html += f"""<div class="type-row">
            <span class="type-label">{ft}</span>
            <div class="type-track">
                <div class="type-fill" style="width:{pct:.1f}%;background:{color}">{total}</div>
            </div>
            <span style="font-size:10px;color:#999;width:50px;text-align:right">{rate} 通过</span>
        </div>"""
    st.markdown(f"""<div class="type-bar-wrap">
        <div class="type-bar-title">📊 因子类型分布</div>
        {rows_html}
    </div>""", unsafe_allow_html=True)


def _render_mine_timeline(data):
    mines = data.get("recent_mines", [])
    if not mines:
        return
    import re
    st.markdown("#### ⛏ 最近因子挖掘")
    items_html = ""
    for m in mines[:8]:
        msg = m["msg"]
        t = m["time"][11:16] if m["time"] else "-"
        match = re.search(r"第(\d+)轮\[([^\]]+)\]", msg)
        if match:
            iteration = match.group(1)
            ftype = match.group(2)
            color = FACTOR_TYPE_COLORS.get(ftype, "#999")
            items_html += f"""<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
                <span style="width:36px;font-size:10px;color:#999">{t}</span>
                <span style="width:8px;height:8px;border-radius:50%;background:{color};flex-shrink:0"></span>
                <span style="font-size:11px;color:#333">第{iteration}轮 <b style="color:{color}">[{ftype}]</b></span>
                <span style="font-size:10px;color:#999">{msg.split('·',1)[-1].strip()[:60]}</span>
            </div>"""
        else:
            items_html += f"""<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
                <span style="width:36px;font-size:10px;color:#999">{t}</span>
                <span style="width:8px;height:8px;border-radius:50%;background:#ddd;flex-shrink:0"></span>
                <span style="font-size:11px;color:#666">{msg[:80]}</span>
            </div>"""
    st.markdown(items_html, unsafe_allow_html=True)


# ---------- 详细日志 ----------
def _render_details(data):
    st.markdown("---")
    st.markdown("### 📋 各阶段详细日志")
    t1, t2, t3, t4, t5 = st.tabs(["因子生成", "因子回测", "选股持仓", "调度日志", "类型分布"])

    with t1:
        c1, c2 = st.columns(2)
        with c1:
            with st.expander("引擎状态", expanded=False):
                bp = data.get("budget", {}).get("p", {})
                bs = "\n".join(f"  - {k}: {v:.1%}" for k, v in bp.items()) if bp else "  - 暂无"
                ft = "\n".join(f"  - {f['family']}: {f['count']}个" for f in data.get("family_top", []))
                st.markdown(f"- 迭代: 第{data.get('iteration',0)}轮\n- 入库: {data.get('accepted',0)}个\n- 生成概率:\n{bs}\n- 机制族TOP5:\n{ft or '  - 暂无'}")
        with c2:
            with st.expander("闸门结果", expanded=False):
                for g in data.get("gate_recent", [])[:3]:
                    import html as _html
                    m = "✅" if g["passed"] else "❌"
                    safe_name = _html.escape(str(g['name'][:35]))
                    safe_date = _html.escape(str(g['date']))
                    st.markdown(f"**{m} {safe_name}** ({safe_date})")
                    if not g["passed"] and g["reasons"]:
                        safe_reason = _html.escape(str(g['reasons'][0][:50]))
                        st.caption(f"  {safe_reason}")

    with t2:
        with st.expander("评估指标", expanded=False):
            st.markdown(f"- 已测试: {data.get('tested_total',0)} 个\n- 通过闸门: {data.get('tested_passed',0)} 个\n- 失败记录: {data.get('failure_count',0)} 个\n- 体检因子: {data.get('scorecard_count',0)} 个\n- 策略包: {data.get('strategy_count',0)} 个")

    with t3:
        c1, c2 = st.columns(2)
        with c1:
            with st.expander("持仓明细", expanded=False):
                po, pc = data.get("pos_open", 0), data.get("pos_closed", 0)
                win = data.get("win_count", 0)
                wr = f"{win/pc:.0%}" if pc > 0 else "-"
                st.markdown(f"- 持仓: {po} 只\n- 已平: {pc} 只\n- 胜率: {wr}\n- 盈亏: {data.get('total_pnl',0):.1f}%")
        with c2:
            with st.expander("最近选股", expanded=False):
                for p in data.get("picks_recent", []):
                    codes = (p["codes"][:25] + "...") if p["codes"] and len(p["codes"]) > 25 else (p["codes"] or "-")
                    st.markdown(f"- {p['date']} | {codes}")

    with t4:
        for r in data.get("sched_recent", [])[:5]:
            m = "✅" if r["success"] else "❌"
            d = f"{r['dur']}ms" if r["dur"] else "-"
            with st.expander(f"{m} {r['name']} — {r['time']} ({d})", expanded=False):
                st.markdown(f"- 任务: `{r['key']}`\n- 状态: {'成功' if r['success'] else '失败'}\n- 消息: {(r['msg'] or '-')[:150]}")

    with t5:
        type_dist = data.get("type_dist", [])
        if type_dist:
            import pandas as _pd
            df = _pd.DataFrame(type_dist)
            df["通过率"] = df.apply(lambda r: f"{r['passed']/r['total']:.1%}" if r["total"] > 0 else "-", axis=1)
            df.columns = ["因子类型", "总数", "通过闸门", "通过率"]
            st.dataframe(df, use_container_width=True, hide_index=True)

            total_all = sum(d["total"] for d in type_dist)
            passed_all = sum(d["passed"] for d in type_dist)
            st.metric("总因子数", f"{total_all:,}", help="factor_registry 中 engine=loopengine 的全部因子")
            st.metric("闸门通过率", f"{passed_all/total_all:.1%}" if total_all > 0 else "-")
        else:
            st.info("暂无因子类型数据")


# ---------- 主页面 ----------
def render():
    st.markdown("## 🔗 全流程工作流")
    data = _load_data()
    if "error" in data:
        st.error(f"数据加载失败: {data['error']}")
        return

    _render_kpi(data)

    _render_stage_status_bar(data)

    _render_rotation(data)
    _render_type_chart(data)

    st.markdown('<div class="vflow-wrap"><div class="vflow-line"></div>', unsafe_allow_html=True)
    _render_gen(data)
    st.markdown(_vf_arrow(), unsafe_allow_html=True)
    _render_eval(data)
    st.markdown(_vf_arrow(), unsafe_allow_html=True)
    _render_pick(data)
    st.markdown(_vf_arrow(), unsafe_allow_html=True)
    _render_pos(data)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### ⏰ 调度时间线")
    _render_timeline(data)

    _render_mine_timeline(data)

    _render_details(data)


if __name__ == "__main__":
    render()
