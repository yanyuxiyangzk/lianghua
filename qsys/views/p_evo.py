"""🧬 进化看板：RD-Agent 因子进化过程（假设/因子代码/指标/反馈/资金曲线）。"""

import pickle
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import LOG_DIR, list_traces, load_trace, metric_subset

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

HOST_PREFIX = "/home/zk/code/lianghua"
LOG_FILE = LOG_DIR / "factor_run.out"  # supervisor 控制台日志（700MB+，只 tail 不整读）


def _tail_log(path: Path, n_lines: int, only_err: bool = False) -> str:
    """读日志尾部 N 行：只从文件末尾 seek 64KB，绝不整读大文件。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", errors="replace")
        lines = [ln for ln in data.splitlines() if ln.strip()]
        lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines]  # 去 ANSI 颜色码
        if only_err:
            lines = [ln for ln in lines
                     if re.search(r"WARNING|ERROR|Error|Exception|Traceback|Killed", ln)]
        return "\n".join(lines[-n_lines:]) or "（无匹配行）"
    except FileNotFoundError:
        return f"（日志文件不存在: {path}）"
    except Exception as e:
        return f"（日志读取失败: {e}）"


def _latest_loop(trace_dir: Path):
    """最新 Loop_N 轮次与其 mtime（判断当前跑到第几轮）"""
    best = None
    try:
        for d in Path(trace_dir).iterdir():
            if not d.is_dir() or not d.name.startswith("Loop_"):
                continue
            try:
                n = int(d.name.split("_")[1])
            except (IndexError, ValueError):
                continue
            if best is None or n > best[0]:
                best = (n, d.stat().st_mtime)
    except OSError:
        pass
    return best


st.title("🧬 进化看板")
st.caption("RD-Agent + Qlib 因子进化闭环的过程回放 · 数据源固定为 qlib 本地库（RD-Agent 产物）")

traces = list_traces()
if not traces:
    st.info(f"未发现 RD-Agent 运行记录（{LOG_DIR} 下没有 trace 目录）。先跑 `rdagent fin_factor`。")
    st.stop()

# trace 目录名是会话启动日(长跑会话靠断点续跑,名字永远不变),
# 选择器同时展示最近活动时间,避免误以为数据陈旧
from common import trace_last_activity  # noqa: E402


def _trace_label(p) -> str:
    last = datetime.fromtimestamp(trace_last_activity(p))
    fresh = " 🟢" if (datetime.now() - last).total_seconds() < 1800 else ""
    return f"{p.name} · 最近活动 {last:%m-%d %H:%M}{fresh}"


# ---------------------------------------------------------------- 运行状态条
# traces 按最近活动倒序，traces[0] 即当前活跃会话
_last_ts = trace_last_activity(traces[0])
_last_dt = datetime.fromtimestamp(_last_ts)
_age_min = (datetime.now() - _last_dt).total_seconds() / 60
_loop = _latest_loop(traces[0])

c1, c2, c3 = st.columns([3, 1.2, 1])
with c1:
    if _age_min < 30:
        st.success(f"🟢 **正在运行** · 最近活动 {_last_dt:%m-%d %H:%M}（{_age_min:.0f} 分钟前）"
                   + (f" · 当前 Loop_{_loop[0]}" if _loop else ""))
    elif _age_min < 120:
        st.warning(f"🟡 **疑似停滞** · 最近活动 {_last_dt:%m-%d %H:%M}（{_age_min:.0f} 分钟前）"
                   + (f" · 当前 Loop_{_loop[0]}" if _loop else ""))
    else:
        st.error(f"🔴 **已停止** · 最近活动 {_last_dt:%m-%d %H:%M}（{_age_min/60:.1f} 小时前）"
                 + (f" · 最后 Loop_{_loop[0]}" if _loop else ""))
with c2:
    st.caption(f"🕐 现在时间\n{datetime.now():%Y-%m-%d %H:%M:%S}")
with c3:
    _auto = st.toggle("自动刷新(30s)", value=False, key="evo_auto",
                      help="开启后每 30 秒自动刷新本页，状态与日志保持最新")
if _auto:
    if st_autorefresh:
        st_autorefresh(interval=30_000, key="evo_autorefresh")
    else:
        st.warning("未安装 streamlit-autorefresh，无法自动刷新")

sel = st.selectbox("选择一次进化运行（trace）", traces, format_func=_trace_label)
data = load_trace(str(sel))
rounds = data["rounds"]
for err in data["errors"][:5]:
    st.warning(err)

# ---------------------------------------------------------------- 加载回测资金曲线
def _load_backtest_chart(trace_path: Path) -> dict[int, pd.DataFrame]:
    """从 trace 目录中加载所有 Loop 的回测资金曲线。"""
    charts = {}
    trace_str = str(trace_path)
    
    # 先试容器内路径（/work/log），找不到再转宿主机路径
    trace_dir = Path(trace_str)
    if not trace_dir.exists():
        if trace_str.startswith("/work/log"):
            trace_dir = Path(HOST_PREFIX + trace_str[len("/work/log"):])
        if not trace_dir.exists():
            return charts
    
    for loop_dir in trace_dir.iterdir():
        if not loop_dir.is_dir() or not loop_dir.name.startswith("Loop_"):
            continue
        try:
            loop_num = int(loop_dir.name.split("_")[1])
        except (IndexError, ValueError):
            continue
        chart_dir = loop_dir / "running" / "Quantitative Backtesting Chart"
        if not chart_dir.exists():
            continue
        for pkl_file in chart_dir.rglob("*.pkl"):
            try:
                with open(pkl_file, "rb") as f:
                    df = pickle.load(f)
                if isinstance(df, pd.DataFrame) and "account" in df.columns:
                    charts[loop_num] = df
                    break
            except Exception:
                continue
    return charts

backtest_charts = _load_backtest_chart(sel)

rows = []
for r in rounds:
    if r["metrics"] is not None:
        s = metric_subset(r["metrics"])
        s.name = "基线" if r["round"] == 0 else f"Round {r['round']}"
        rows.append(s)
if rows:
    df = pd.DataFrame(rows)
    df.index.name = "轮次"
    st.subheader("各轮回测指标")
    st.dataframe(df.style.format("{:.4f}"), width='stretch')

    ic_col = next((c for c in ["IC", "Rank IC"] if c in df.columns), None)
    ar_col = "1day.excess_return_with_cost.annualized_return"
    fig = go.Figure()
    if ic_col:
        fig.add_trace(go.Bar(x=df.index, y=df[ic_col], name=ic_col))
    if ar_col in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df[ar_col], name="年化超额(含费)", yaxis="y2"))
    fig.update_layout(yaxis2=dict(overlaying="y", side="right"), height=380,
                      legend=dict(orientation="h"), margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width='stretch')

    if ic_col:
        comp = df.drop(index="基线", errors="ignore")
        if not comp.empty:
            bi = comp[ic_col].astype(float).idxmax()
            st.success(f"当前最佳（按 {ic_col}，除基线）：**{bi}** · " +
                       " · ".join(f"{k}={v:.4f}" for k, v in comp.loc[bi].items()))

    # ---------------------------------------------------------------- 资金曲线
    if backtest_charts:
        st.subheader("📈 资金曲线")
        
        # 选择要展示的轮次
        available_loops = sorted(backtest_charts.keys())
        if len(available_loops) > 1:
            sel_loops = st.multiselect("选择要对比的轮次", available_loops,
                                       default=[available_loops[-1]],
                                       key="chart_loops")
        else:
            sel_loops = available_loops
        
        if sel_loops:
            fig = go.Figure()
            for loop_num in sel_loops:
                if loop_num in backtest_charts:
                    chart_df = backtest_charts[loop_num]
                    # 归一化账户价值为净值
                    nav = chart_df["account"] / chart_df["account"].iloc[0]
                    label = "基线" if loop_num == 0 else f"Round {loop_num}"
                    fig.add_trace(go.Scatter(
                        x=chart_df.index, y=nav, name=label,
                        mode="lines", line=dict(width=2)
                    ))
                    # 基准线
                    if "bench" in chart_df.columns:
                        bench_nav = (1 + chart_df["bench"]).cumprod()
                        fig.add_trace(go.Scatter(
                            x=chart_df.index, y=bench_nav, name=f"{label} 基准",
                            mode="lines", line=dict(width=1, dash="dot")
                        ))
            
            fig.update_layout(
                title="策略净值 vs 基准",
                xaxis_title="日期", yaxis_title="净值",
                height=400, hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                margin=dict(l=10, r=10, t=40, b=10)
            )
            st.plotly_chart(fig, width="stretch")
            
            # 绩效指标卡片
            st.subheader("📊 绩效指标")
            for loop_num in sel_loops:
                if loop_num not in backtest_charts:
                    continue
                chart_df = backtest_charts[loop_num]
                nav = chart_df["account"] / chart_df["account"].iloc[0]
                daily_ret = nav.pct_change().dropna()
                
                total_ret = nav.iloc[-1] - 1
                ann_ret = (nav.iloc[-1] ** (252 / len(nav))) - 1
                sharpe = daily_ret.mean() / (daily_ret.std() + 1e-12) * (252 ** 0.5)
                mdd = ((nav - nav.cummax()) / nav.cummax()).min()
                calmar = ann_ret / abs(mdd) if mdd != 0 else 0
                
                label = "基线" if loop_num == 0 else f"Round {loop_num}"
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric(f"{label} 累计收益", f"{total_ret:.2%}")
                c2.metric(f"{label} 年化收益", f"{ann_ret:.2%}")
                c3.metric(f"{label} 夏普比率", f"{sharpe:.2f}")
                c4.metric(f"{label} 最大回撤", f"{mdd:.2%}")
                c5.metric(f"{label} Calmar", f"{calmar:.2f}")
else:
    st.info("该 trace 尚无包含回测指标的轮次（可能还在提案/编码阶段）。")

# ---------------------------------------------------------------- 每日选股名单
if backtest_charts:
    st.subheader("📋 每日选股名单")
    st.caption("基于 TopkDropoutStrategy（topk=50, n_drop=5），展示每天持仓的股票")
    
    from common import load_positions
    
    # 选择要展示的轮次
    available_loops = sorted(backtest_charts.keys())
    if len(available_loops) > 1:
        sel_pos_loop = st.selectbox("选择轮次", available_loops,
                                    index=len(available_loops)-1,
                                    key="pos_loop")
    else:
        sel_pos_loop = available_loops[0] if available_loops else None
    
    if sel_pos_loop is not None:
        positions_df = load_positions(str(sel), sel_pos_loop)
        if positions_df is not None and not positions_df.empty:
            # 按日期分组展示
            dates = sorted(positions_df["datetime"].unique(), reverse=True)
            sel_date = st.selectbox("选择日期", dates, key="pos_date")
            
            day_pos = positions_df[positions_df["datetime"] == sel_date]
            if not day_pos.empty:
                # 展示持仓明细
                st.dataframe(
                    day_pos[["instrument", "amount", "cost"]].rename(columns={
                        "instrument": "股票代码",
                        "amount": "持仓数量",
                        "cost": "成本价"
                    }),
                    width='stretch', hide_index=True
                )
                st.caption(f"共 {len(day_pos)} 只股票 · 总持仓数量 {day_pos['amount'].sum():,.0f}")
            else:
                st.info("该日期无持仓数据")
        else:
            st.info("该轮次暂无持仓数据（需要新演化出的回测才会保存每日持仓）")

st.subheader("假设与因子明细")
for r in reversed(rounds):
    label = ("基线" if r["round"] == 0 else f"Round {r['round']}") + f" · {r.get('time','')}"
    if r["feedback"] and r["feedback"]["decision"] is not None:
        label += " ✅ 被接受" if r["feedback"]["decision"] else " ❌ 被拒绝"
    with st.expander(label, expanded=(r == rounds[-1])):
        st.markdown(f"**假设**：{r['hypothesis']}")
        if r["reason"]:
            st.markdown(f"**理由**：{r['reason'][:1000]}")
        if r["feedback"]:
            st.markdown(f"**反馈**：{'被接受' if r['feedback']['decision'] else '被拒绝'} — {r['feedback']['reason']}")
        for t in r["tasks"]:
            st.markdown(f"**因子 `{t['name']}`**：{t['description']}")
            if t.get("formulation"):
                st.caption(f"公式：{t['formulation']}")
            if t["code"]:
                st.code(t["code"], language="python")
            else:
                st.caption("（因子代码见对应 workspace 的 factor.py）")

# ---------------------------------------------------------------- 运行日志（尾部）
st.divider()
with st.expander("📜 进化运行日志（factor_run.out 尾部）", expanded=False):
    lc1, lc2, lc3 = st.columns([1, 1, 1])
    with lc1:
        _n_lines = st.selectbox("显示行数", [100, 300, 1000], index=0, key="evo_log_lines")
    with lc2:
        _only_err = st.checkbox("只看 WARNING/ERROR", value=False, key="evo_log_err")
    with lc3:
        if st.button("🔄 刷新日志", key="evo_log_refresh"):
            st.rerun()
    st.code(_tail_log(LOG_FILE, _n_lines, _only_err), language="text")
    st.caption("日志为 supervisor 控制台输出（只读尾部 64KB）；开启顶部「自动刷新」可实时跟踪")
