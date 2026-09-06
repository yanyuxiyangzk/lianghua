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


# ---------- 详细日志 ----------
def _render_details(data):
    st.markdown("---")
    st.markdown("### 📋 各阶段详细日志")
    t1, t2, t3, t4 = st.tabs(["因子生成", "因子回测", "选股持仓", "调度日志"])

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


# ---------- 主页面 ----------
def render():
    st.markdown("## 🔗 全流程工作流")
    data = _load_data()
    if "error" in data:
        st.error(f"数据加载失败: {data['error']}")
        return

    _render_kpi(data)

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

    _render_details(data)


if __name__ == "__main__":
    render()
