"""📊 回测浏览：RD-Agent 各轮实验的 Qlib 回测产物（净值/绩效/产物清单）。"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import LOG_DIR, WORKSPACE_ROOT, quick_stats

st.title("📊 回测浏览")
st.caption("RD-Agent 每轮实验的 Qlib 回测产物（mlruns） · 数据源固定为 qlib 本地库")


def find_mlruns():
    found = []
    for root in [LOG_DIR, WORKSPACE_ROOT / "git_ignore_folder"]:
        if root.exists():
            found.extend(root.rglob("mlruns"))
    return sorted(set(found))


mlruns_dirs = find_mlruns()
if not mlruns_dirs:
    st.info("未发现 Qlib 回测产物（mlruns）。RD-Agent 跑过至少一轮后会自动生成。")
    st.stop()

rec_options = []
for mdir in mlruns_dirs:
    for exp_dir in sorted(mdir.iterdir()):
        if not exp_dir.is_dir():
            continue
        for rec_dir in sorted(exp_dir.iterdir()):
            art = rec_dir / "artifacts"
            if art.is_dir():
                try:
                    rel = rec_dir.relative_to(WORKSPACE_ROOT)
                except ValueError:
                    rel = rec_dir
                rec_options.append((str(rel), rec_dir))
if not rec_options:
    st.info("mlruns 已生成但还没有 recorder 产物。")
    st.stop()

sel = st.selectbox("选择回测记录（recorder）", rec_options, format_func=lambda x: x[0])
art = sel[1] / "artifacts"
report_pkl = art / "portfolio_analysis" / "report_normal_1day.pkl"

if report_pkl.exists():
    report = pd.read_pickle(report_pkl)
    report = report.sort_index()
    st.subheader("净值曲线（组合 / 基准 / 超额）")
    fig = go.Figure()
    if "return" in report.columns:
        net_ret = report["return"] - report.get("cost", 0)
        fig.add_trace(go.Scatter(x=report.index, y=(1 + net_ret).cumprod(), name="组合(含费)"))
    if "bench" in report.columns:
        fig.add_trace(go.Scatter(x=report.index, y=(1 + report["bench"]).cumprod(), name="基准"))
    if "return" in report.columns and "bench" in report.columns:
        excess = report["return"] - report["bench"] - report.get("cost", 0)
        fig.add_trace(go.Scatter(x=report.index, y=(1 + excess).cumprod(), name="超额(含费)"))
    fig.update_layout(height=420, legend=dict(orientation="h"),
                      margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width='stretch')

    if "return" in report.columns:
        stats = quick_stats(report["return"] - report.get("cost", 0))
        if stats:
            st.subheader("绩效概览（含交易成本）")
            st.table(pd.DataFrame(stats, index=["值"]).T)
else:
    st.caption("该 recorder 无 portfolio_analysis 产物（可能是纯因子/信号实验）。")

st.subheader("产物清单")
for p in sorted(p for p in art.rglob("*") if p.is_file())[:50]:
    st.caption(f"`{p.relative_to(art)}` ({p.stat().st_size/1024:.1f} KB)")
