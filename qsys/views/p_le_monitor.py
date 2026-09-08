"""LoopEngine 演化监控 — 引擎状态 + 实时闸门日志 + 控制台日志 + 生成源分析。"""

import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="LoopEngine监控", layout="wide")

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

LOG_FILE = Path("/data/log/factor_run.out")

# ---------- CSS ----------
st.markdown("""
<style>
.log-box {
    background: #1e1e1e; color: #d4d4d4; font-family: 'Consolas', monospace;
    font-size: 12px; padding: 12px; border-radius: 8px; max-height: 500px;
    overflow-y: auto; white-space: pre-wrap; word-break: break-all; line-height: 1.5;
}
.log-box .time { color: #569cd6; }
.log-box .pass { color: #4ec9b0; }
.log-box .fail { color: #f44747; }
.log-box .warn { color: #ce9178; }
.log-box .dim { color: #888; }
.gate-card {
    background: #f8f9fa; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px;
    border-left: 3px solid #11998e; font-size: 13px;
}
.gate-card.rejected { border-left-color: #eb3349; }
.gate-card .name { font-weight: 600; color: #333; }
.gate-card .meta { font-size: 11px; color: #888; margin-top: 4px; }
.gate-card .reason { font-size: 11px; color: #eb3349; margin-top: 2px; }
</style>
""", unsafe_allow_html=True)


# ---------- 数据加载 ----------
@st.cache_data(ttl=30)
def _load_engine_state():
    import library
    try:
        with library._lconn() as c:
            row = c.execute("SELECT * FROM engine_state WHERE id='loopengine'").fetchone()
        if row:
            return {"iteration": row[1], "budget": json.loads(row[2]),
                    "momentum": json.loads(row[3] or "{}"),
                    "field_weights": json.loads(row[4] or "{}"),
                    "accepted": row[5], "updated_at": row[6]}
    except Exception:
        pass
    return None


@st.cache_data(ttl=15)
def _load_gate_log(limit=100):
    import library
    try:
        with library._lconn() as c:
            df = pd.read_sql(
                "SELECT factor_name, gate_date, passed, metrics, fail_reasons, created_at "
                "FROM gate_detail_log ORDER BY id DESC LIMIT ?", c, params=(limit,))
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def _load_registry_stats():
    import library
    try:
        with library._lconn() as c:
            df = pd.read_sql(
                "SELECT family, factor_type, gate_status, COUNT(*) as cnt "
                "FROM factor_registry WHERE engine='loopengine' "
                "GROUP BY family, factor_type, gate_status", c)
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def _load_mo_scores(limit=10):
    """多目标评分 Top 因子（含风险指标 + 衰减状态）。"""
    import library
    try:
        with library._lconn() as c:
            df = pd.read_sql(
                "SELECT name, family, multi_objective_score, max_drawdown, sharpe, sortino, calmar, "
                "       COALESCE(decay_status, '-') AS decay_status, decay_rate "
                "FROM factor_registry WHERE engine='loopengine' AND gate_status=1 "
                "AND multi_objective_score IS NOT NULL "
                "ORDER BY multi_objective_score DESC LIMIT ?", c, params=(limit,))
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def _load_decay_stats():
    """因子衰减状态分布。"""
    import library
    try:
        with library._lconn() as c:
            df = pd.read_sql(
                "SELECT decay_status, COUNT(*) AS cnt FROM factor_decay GROUP BY decay_status", c)
        return df
    except Exception:
        return pd.DataFrame()


def _load_console_log(n_lines=200, only_err=False):
    try:
        size = LOG_FILE.stat().st_size
        with open(LOG_FILE, "rb") as f:
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", errors="replace")
        lines = [ln for ln in data.splitlines() if ln.strip()]
        lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines]
        if only_err:
            lines = [ln for ln in lines
                     if re.search(r"WARNING|ERROR|Error|Exception|Traceback|Killed", ln, re.I)]
        return lines[-n_lines:]
    except FileNotFoundError:
        return [f"日志文件不存在: {LOG_FILE}"]
    except Exception as e:
        return [f"读取失败: {e}"]


# ---------- 引擎状态概览 ----------
def _render_header(state):
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("累计迭代", state["iteration"])
    with c2:
        st.metric("累计入库", state["accepted"])
    with c3:
        bp = state["budget"].get("p", {})
        st.metric("LLM概率", f"{bp.get('llm', 0):.0%}")
    with c4:
        st.metric("更新时间", (state.get("updated_at") or "-")[:16])


# ---------- 闸门实时日志 ----------
def _render_gate_log():
    st.markdown("### 🚪 闸门评估日志（实时）")

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        n = st.selectbox("显示条数", [20, 50, 100, 200], index=1, key="gate_n")
    with col2:
        only_fail = st.checkbox("仅显示失败", key="gate_fail_only")

    df = _load_gate_log(n)
    if df.empty:
        st.info("暂无闸门评估数据")
        return

    if only_fail:
        df = df[df["passed"] == 0]

    # 统计条
    total = len(df)
    passed = int(df["passed"].sum())
    failed = total - passed
    rate = f"{passed/total:.1%}" if total > 0 else "-"

    mc1, mc2, mc3, mc4 = st.columns(4)
    mc1.metric("评估总数", total)
    mc2.metric("通过", passed)
    mc3.metric("失败", failed)
    mc4.metric("通过率", rate)

    st.markdown("---")

    # 逐条展示
    for _, row in df.iterrows():
        import html as _html
        name = _html.escape(str(row["factor_name"] or "-"))
        is_passed = row["passed"]
        date = _html.escape(str(row["gate_date"] or "-"))
        created = _html.escape(str(row["created_at"][:19] if row["created_at"] else "-"))
        try:
            metrics = json.loads(row["metrics"]) if row["metrics"] else {}
        except (json.JSONDecodeError, TypeError):
            metrics = {}
        try:
            reasons = json.loads(row["fail_reasons"]) if row["fail_reasons"] else []
        except (json.JSONDecodeError, TypeError):
            reasons = []

        cls = "gate-card" if is_passed else "gate-card rejected"
        icon = "✅" if is_passed else "❌"

        # 指标摘要
        ic = metrics.get("IC", metrics.get("|IC|", "-"))
        sharpe = metrics.get("夏普2025", metrics.get("夏普2026", "-"))
        exc = metrics.get("超额2025", metrics.get("超额2026", "-"))
        ic_s = f"{ic:.3f}" if isinstance(ic, (int, float)) else str(ic)
        sh_s = f"{sharpe:.2f}" if isinstance(sharpe, (int, float)) else str(sharpe)
        ex_s = f"{exc:.1%}" if isinstance(exc, (int, float)) else str(exc)

        html = f"""<div class="{cls}">
<div>{icon} <span class="name">{name[:60]}</span></div>
<div class="meta">📅 {date} | IC: {ic_s} | 夏普: {sh_s} | 超额: {ex_s} | {created}</div>"""
        if reasons:
            safe_reasons = _html.escape(" | ".join(str(r)[:40] for r in reasons[:3]))
            html += f'<div class="reason">❌ {safe_reasons}</div>'
        html += "</div>"
        st.markdown(html, unsafe_allow_html=True)


# ---------- 控制台日志 ----------
def _render_console_log():
    st.markdown("### 📟 控制台日志（factor_run.out）")

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        n = st.selectbox("行数", [100, 200, 500, 1000], index=1, key="console_n")
    with col2:
        only_err = st.checkbox("仅错误/警告", key="console_err")

    lines = _load_console_log(n, only_err)

    if not lines or lines[0].startswith("日志文件不存在"):
        st.warning(lines[0] if lines else "无日志")
        return

    # 高亮显示
    colored = []
    for ln in lines[-n:]:
        ln = ln.replace("<", "&lt;").replace(">", "&gt;")
        if re.search(r"ERROR|Exception|Traceback|Killed", ln, re.I):
            ln = f'<span class="fail">{ln}</span>'
        elif re.search(r"WARNING|WARN", ln, re.I):
            ln = f'<span class="warn">{ln}</span>'
        elif re.search(r"passed|accepted|入库|通过", ln, re.I):
            ln = f'<span class="pass">{ln}</span>'
        else:
            ln = f'<span class="dim">{ln}</span>'
        colored.append(ln)

    st.markdown(f'<div class="log-box">{"<br>".join(colored)}</div>', unsafe_allow_html=True)


# ---------- 生成源 + 漏斗 + 族覆盖 + 字段权重 ----------
def _render_budget_chart(state, key_prefix=""):
    st.markdown("### 生成方式概率分布")
    bp = state["budget"].get("p", state["budget"])
    if isinstance(bp, dict):
        valid = [(k, v) for k, v in bp.items() if isinstance(v, (int, float))]
        if valid:
            bdf = pd.DataFrame({"方式": [x[0] for x in valid], "概率": [x[1] for x in valid]})
            bdf = bdf.sort_values("概率", ascending=False)
            fig = px.bar(bdf, x="方式", y="概率", color="方式",
                         color_discrete_sequence=px.colors.qualitative.Set2)
            fig.update_layout(height=300, yaxis_tickformat=".0%",
                              margin=dict(l=20, r=20, t=30, b=20))
            st.plotly_chart(fig, width="stretch", key=f"budget_{key_prefix}")


def _render_funnel_chart(key_prefix=""):
    st.markdown("### 闸门淘汰漏斗")
    gate_df = _load_gate_log(3000)
    if not gate_df.empty:
        reasons_all = []
        for reasons_str in gate_df["fail_reasons"].dropna():
            try:
                reasons_all.extend(json.loads(reasons_str))
            except Exception:
                continue
        gate_keys = ["IC", "超额2025", "夏普2025", "超额2026", "夏普2026",
                     "Calmar", "近9月", "近12月", "最大IC相关"]
        gate_labels = ["|IC|", "2025超额", "2025夏普", "2026超额", "2026夏普",
                       "Calmar", "近9月超额", "近12月超额", "IC相关"]
        counts = [sum(1 for r in reasons_all if gk in r) for gk in gate_keys]
        fd = pd.DataFrame({"闸门": gate_labels, "淘汰数": counts})
        fd = fd[fd["淘汰数"] > 0].sort_values("淘汰数", ascending=False)
        if not fd.empty:
            fig = px.funnel(fd, x="淘汰数", y="闸门")
            fig.update_layout(height=400, margin=dict(l=20, r=20, t=30, b=20))
            st.plotly_chart(fig, width="stretch", key=f"funnel_{key_prefix}")
        else:
            st.info("无淘汰数据")


def _render_family_chart(key_prefix=""):
    st.markdown("### 机制族覆盖度")
    reg_df = _load_registry_stats()
    if not reg_df.empty:
        fam_df = reg_df.groupby("family")["cnt"].sum().reset_index()
        fam_df = fam_df.sort_values("cnt", ascending=False).head(20)
        fig = px.bar(fam_df, x="family", y="cnt", color="cnt",
                     color_continuous_scale="Viridis")
        fig.update_layout(height=350, margin=dict(l=20, r=20, t=30, b=20),
                          xaxis_title="机制族", yaxis_title="因子数")
        st.plotly_chart(fig, width="stretch", key=f"family_{key_prefix}")


def _render_field_chart(state, key_prefix=""):
    st.markdown("### 字段权重 TOP15")
    fw = state.get("field_weights", {})
    fw_data = fw.get("w", fw) if isinstance(fw, dict) else {}
    if fw_data:
        fw_df = pd.DataFrame({"字段": list(fw_data.keys()), "权重": list(fw_data.values())})
        fw_df = fw_df.sort_values("权重", ascending=True).tail(15)
        fig = px.bar(fw_df, x="权重", y="字段", orientation="h",
                     color="权重", color_continuous_scale="Greens")
        fig.update_layout(height=400, margin=dict(l=20, r=20, t=30, b=20),
                          coloraxis_showscale=False)
        st.plotly_chart(fig, width="stretch", key=f"field_{key_prefix}")


def _render_mo_and_decay(key_prefix=""):
    """多目标评分 Top 因子 + 衰减状态分布。"""
    mo = _load_mo_scores()
    st.markdown("### 多目标评分 Top 因子（IC+风险+收益平衡）")
    if mo.empty:
        st.info("暂无多目标评分数据（等待因子入库时计算）")
    else:
        show = mo.rename(columns={
            "name": "因子", "family": "族", "multi_objective_score": "综合评分",
            "max_drawdown": "最大回撤", "sharpe": "夏普", "sortino": "索提诺",
            "calmar": "卡玛", "decay_status": "衰减状态", "decay_rate": "衰减率"})
        st.dataframe(show, width="stretch", hide_index=True,
                     column_config={
                         "综合评分": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.3f"),
                         "最大回撤": st.column_config.NumberColumn(format="%.1%%"),
                         "夏普": st.column_config.NumberColumn(format="%.2f"),
                         "索提诺": st.column_config.NumberColumn(format="%.2f"),
                         "卡玛": st.column_config.NumberColumn(format="%.2f"),
                         "衰减率": st.column_config.NumberColumn(format="%.2f")},
                     key=f"mo_table_{key_prefix}")

    dec = _load_decay_stats()
    st.markdown("### 因子衰减状态分布")
    if dec.empty:
        st.info("暂无衰减检测数据")
    else:
        label_map = {"normal": "正常", "mild": "轻度", "moderate": "中度",
                     "severe": "重度", "insufficient_data": "数据不足", "error": "错误"}
        dec["状态"] = dec["decay_status"].map(label_map).fillna(dec["decay_status"])
        fig = px.pie(dec, names="状态", values="cnt", hole=0.45,
                     color_discrete_sequence=px.colors.qualitative.Set2)
        fig.update_layout(height=320, margin=dict(l=20, r=20, t=30, b=20))
        st.plotly_chart(fig, width="stretch", key=f"decay_{key_prefix}")


# ---------- 主页面 ----------
def render():
    st.markdown("## 🧬 LoopEngine 演化监控")

    # 自动刷新
    if st_autorefresh:
        auto = st.toggle("30秒自动刷新", value=False, key="auto_refresh")
        if auto:
            st_autorefresh(interval=30000, key="le_autorefresh")
    else:
        st.caption("安装 streamlit-autorefresh 可启用自动刷新: `pip install streamlit-autorefresh`")

    state = _load_engine_state()
    if not state:
        st.info("引擎未初始化，请先运行 LoopEngine。")
        return

    _render_header(state)
    st.divider()

    # 主tab: 实时日志 + 图表分析
    tab_live, tab_log, tab_gate, tab_console, tab_chart = st.tabs(
        ["⚡ 实时事件流", "📊 概览分析", "🚪 闸门日志", "📟 控制台日志", "📈 详细图表"])

    with tab_live:
        _render_sse_events()

    with tab_log:
        _render_budget_chart(state, "tab1")
        _render_funnel_chart("tab1")
        _render_family_chart("tab1")
        _render_field_chart(state, "tab1")
        _render_mo_and_decay("tab1")

    with tab_gate:
        _render_gate_log()

    with tab_console:
        _render_console_log()

    with tab_chart:
        st.markdown("### 详细图表分析")
        _render_budget_chart(state, "tab4")
        _render_funnel_chart("tab4")
        _render_family_chart("tab4")
        _render_field_chart(state, "tab4")


# ---------- 实时事件流（SSE） ----------
def _render_sse_events():
    st.markdown("### ⚡ LoopEngine 实时事件流")

    # SSE 端口配置
    sse_port = st.number_input("SSE端口", value=8502, key="sse_port_input")
    sse_url = f"http://localhost:{sse_port}/events"

    # 连接状态指示
    status_placeholder = st.empty()
    status_placeholder.info("⏳ 等待连接 SSE 服务...")

    # 事件列表容器
    events_container = st.container()

    # JS EventSource 代码
    js_code = f"""
    <div id="sse-status" style="padding:8px;border-radius:6px;margin-bottom:12px;font-size:13px;">
        ⏳ 正在连接 SSE...
    </div>
    <div id="sse-log" style="
        background:#1e1e1e;color:#d4d4d4;font-family:Consolas,monospace;
        font-size:11px;padding:12px;border-radius:8px;max-height:450px;
        overflow-y:auto;line-height:1.5;white-space:pre-wrap;
    ">
    </div>
    <div id="sse-stats" style="margin-top:8px;font-size:12px;color:#888;"></div>

    <script>
    (function() {{
        const logEl = document.getElementById('sse-log');
        const statusEl = document.getElementById('sse-status');
        const statsEl = document.getElementById('sse-stats');
        let count = 0, passCount = 0, failCount = 0;
        let retryCount = 0;
        const maxRetry = 5;

        function addLine(text, cls) {{
            const line = document.createElement('div');
            line.style.color = cls || '#d4d4d4';
            line.textContent = text;
            logEl.appendChild(line);
            logEl.scrollTop = logEl.scrollHeight;
            count++;
            statsEl.textContent = `共 ${{count}} 条 | ✅ ${{passCount}} 通过 | ❌ ${{failCount}} 失败 | 重连 ${{retryCount}} 次`;
        }}

        function connect() {{
            const es = new EventSource('{sse_url}');

            es.onopen = function() {{
                retryCount = 0;
                statusEl.innerHTML = '🟢 <b>已连接</b> — SSE 实时事件流';
                statusEl.style.background = '#1a3a1a';
                statusEl.style.color = '#4ec9b0';
            }};

            es.addEventListener('round_start', function(e) {{
                const d = JSON.parse(e.data);
                addLine(`\\n━━━ 第${{d.iteration}}轮开始 | 类型=${{d.factor_type}} | 批次=${{d.batch}} ━━━`, '#667eea');
            }});

            es.addEventListener('gate_eval', function(e) {{
                const d = JSON.parse(e.data);
                const icon = d.passed ? '✅' : '❌';
                const cls = d.passed ? '#4ec9b0' : '#f44747';
                const ic = d.metrics?.IC?.toFixed?.(3) || '-';
                const sh = d.metrics?.夏普2025?.toFixed?.(2) || d.metrics?.夏普2026?.toFixed?.(2) || '-';
                const st = d.stats_snapshot || {{}};
                addLine(`${{icon}} ${{d.factor_name}} | IC=${{ic}} 夏普=${{sh}} | 测试${{st.tested||0}} 入库${{st.passed||0}}`, cls);
            }});

            es.addEventListener('gate_pass', function(e) {{
                const d = JSON.parse(e.data);
                addLine(`🎉 入库! ${{d.factor_name}} | 族=${{d.family}}`, '#4ec9b0');
                passCount++;
            }});

            es.addEventListener('review_result', function(e) {{
                const d = JSON.parse(e.data);
                addLine(`⚠️ 规则拒绝: ${{d.reason}}`, '#ce9178');
            }});

            es.addEventListener('llm_result', function(e) {{
                const d = JSON.parse(e.data);
                addLine(`🤖 LLM否决: ${{d.reason}}`, '#ce9178');
            }});

            es.addEventListener('round_complete', function(e) {{
                const d = JSON.parse(e.data);
                const s = d.stats || {{}};
                addLine(`\\n━━━ 轮次结束 | 测试=${{s.tested}} 入库=${{s.passed}} 重复=${{s.dup}} FSA=${{s.frozen}} ━━━`, '#764ba2');
                failCount = (s.tested || 0) - (s.passed || 0);
            }});

            es.onerror = function() {{
                retryCount++;
                if (retryCount > maxRetry) {{
                    statusEl.innerHTML = '🔴 <b>连接失败</b> — SSE 服务未启动';
                    statusEl.style.background = '#3a1a1a';
                    statusEl.style.color = '#f44747';
                    es.close();
                    return;
                }}
                statusEl.innerHTML = `🔄 重连中 (${{retryCount}}/${{maxRetry}})...`;
                statusEl.style.background = '#3a3a1a';
                statusEl.style.color = '#f7971e';
            }};
        }}

        connect();
    }})();
    </script>
    """

    st.components.v1.html(js_code, height=520, scrolling=True)


if __name__ == "__main__":
    render()
