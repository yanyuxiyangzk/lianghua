"""✅ 实战验证：因子与策略包"是否真生效"的硬证据页。

数据链：每日名单（picks）→ 到期结算（outcomes：1/5/20/60/120 日超额 vs 池内中位数）。
三个视角：
  1. 策略包实战累计超额净值曲线（复利的最终裁决，区分回测吹牛与实盘生效）
  2. 校准对照：回测 OOS 胜率 vs 实战胜率（回测说的算不算数）
  3. 因子 OOS IC 动势（评分卡 icir_oos 逐日轨迹——引擎是否在持续产出真 alpha）
另附每日名单/结算覆盖日历，断更一眼可见。
"""

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

UP, DOWN = "#e54545", "#26a69a"
EXP_DB = Path("/data/experience.db")
MKT_DB = Path("/data/market.db")
MAIN_FWD = 5  # 主口径与策略包 horizon 一致（5日）


def _conn(path: Path):
    c = sqlite3.connect(str(path), timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    return c


@st.cache_data(ttl=300, show_spinner=False)
def _load_outcomes() -> tuple[pd.DataFrame, pd.DataFrame]:
    """picks + outcomes 关联宽表。"""
    if not EXP_DB.exists():
        return pd.DataFrame(), pd.DataFrame()
    with _conn(EXP_DB) as c:
        picks = pd.read_sql(
            "SELECT id, trade_date, source, pool_name, pack_name, oos_winrate_at_save"
            " FROM picks", c)
        outs = pd.read_sql("SELECT pick_id, fwd_days, eval_date, avg_ret, excess, hit"
                           " FROM outcomes", c)
    return picks, outs


@st.cache_data(ttl=300, show_spinner=False)
def _load_scorecard_oos() -> pd.DataFrame:
    """评分卡 OOS 轨迹（icir_oos 是 2026-09-11 新增列，历史行可能为空）。"""
    if not MKT_DB.exists():
        return pd.DataFrame()
    try:
        with _conn(MKT_DB) as c:
            return pd.read_sql(
                "SELECT name, kind, eval_date, ic_mean, icir, ic_oos, icir_oos, oos_days"
                " FROM factor_scorecards WHERE pool_name='沪深300'", c)
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------- 区块 1：实战净值曲线
def _render_equity(picks: pd.DataFrame, outs: pd.DataFrame):
    st.markdown("### 📈 策略包实战累计超额（最终裁决）")
    if outs.empty or picks.empty:
        st.info("暂无已结算战果——名单落库后到期自动结算（1/5/20 日起）")
        return

    df = outs.merge(picks, left_on="pick_id", right_on="id")
    df["pack_name"] = df["pack_name"].fillna("(未存包)")
    main = df[df["fwd_days"] == MAIN_FWD]
    if main.empty:
        st.info(f"暂无 {MAIN_FWD} 日口径战果")
        return

    # 每包每日均值超额 → 复利累计
    daily = main.groupby(["pack_name", "eval_date"])["excess"].mean().reset_index()
    packs = daily.groupby("pack_name").size().sort_values(ascending=False)
    top_packs = packs.index[:6].tolist()

    fig = go.Figure()
    for pack in top_packs:
        s = daily[daily["pack_name"] == pack].sort_values("eval_date")
        cum = (1 + s["excess"]).cumprod() - 1
        fig.add_trace(go.Scatter(x=s["eval_date"], y=cum, mode="lines+markers",
                                 name=f"{pack}（{len(s)}期）"))
    # 全部名单混合基准线
    blend = main.groupby("eval_date")["excess"].mean().sort_index()
    fig.add_trace(go.Scatter(x=blend.index, y=(1 + blend).cumprod() - 1,
                             mode="lines", name="全部名单混合",
                             line=dict(dash="dash", color="gray")))
    fig.add_hline(y=0, line_color="gray", line_width=1)
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10),
                      yaxis_tickformat=".1%", hovermode="x unified",
                      legend=dict(orientation="h", y=1.12))
    st.plotly_chart(fig, use_container_width=True)

    # 汇总表：实战胜率/均超额/期数/最近结算日
    g = main.groupby("pack_name")
    summ = g.agg(期数=("excess", "size"), 实战胜率=("hit", lambda s: (s == 1).mean()),
                 期均超额=("excess", "mean"), 最近结算=("eval_date", "max"))
    summ["累计超额"] = (1 + daily.pivot(index="eval_date", columns="pack_name",
                                        values="excess")).prod() - 1
    summ = summ.reset_index().sort_values("累计超额", ascending=False)
    show = pd.DataFrame({
        "策略包": summ["pack_name"], "期数": summ["期数"],
        "实战胜率": summ["实战胜率"].map(lambda x: f"{x:.0%}"),
        "期均超额": summ["期均超额"].map(lambda x: f"{x:+.2%}"),
        "累计超额": summ["累计超额"].map(lambda x: f"{x:+.2%}"),
        "最近结算": summ["最近结算"],
    })
    st.dataframe(show, hide_index=True, width="stretch")


# ---------------------------------------------------------------- 区块 2：校准对照
def _render_calibration(picks: pd.DataFrame, outs: pd.DataFrame):
    st.markdown("### 🎯 校准：回测 OOS 胜率 vs 实战胜率")
    st.caption("点越靠近对角线，说明回测越可信；长期低于对角线 = 回测系统性吹牛")
    import experience
    lb = experience.pack_leaderboard()
    if lb.empty:
        st.info("暂无数据")
        return
    col = f"{MAIN_FWD}日胜率"
    if col not in lb.columns:
        st.info(f"暂无 {MAIN_FWD} 日实战胜率数据")
        return
    d = lb.dropna(subset=[col]).copy()
    d["回测OOS胜率"] = pd.to_numeric(
        d["回测OOS胜率"].astype(str).str.rstrip("%"), errors="coerce") / 100
    d = d.dropna(subset=["回测OOS胜率"])
    if d.empty:
        st.info("暂无同时具备回测与实战成绩的策略包")
        return

    fig = go.Figure()
    lim = [0, 1]
    fig.add_trace(go.Scatter(x=lim, y=lim, mode="lines", name="完美校准",
                             line=dict(dash="dash", color="gray")))
    fig.add_trace(go.Scatter(
        x=d["回测OOS胜率"], y=d[col], mode="markers+text",
        text=d["策略包"], textposition="top center",
        marker=dict(size=d["已回填战果"].clip(3, 40), color=d[col] - d["回测OOS胜率"],
                    colorscale=[[0, DOWN], [0.5, "#f0ad4e"], [1, UP]],
                    cmin=-0.3, cmax=0.3, showscale=True,
                    colorbar=dict(title="实战-回测")),
        name="策略包"))
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis_title="回测 OOS 胜率（吹牛值）", yaxis_title="实战胜率（真实值）",
                      xaxis_tickformat=".0%", yaxis_tickformat=".0%")
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------- 区块 3：因子 OOS IC 动势
def _render_factor_oos():
    st.markdown("### 🧬 因子 OOS IC 动势（引擎产出是否真实）")
    sc = _load_scorecard_oos()
    if sc.empty:
        st.info("暂无评分卡数据")
        return
    oos = sc.dropna(subset=["icir_oos"])
    if oos.empty:
        st.info("OOS 评分列今日（2026-09-11）刚上线，尚无轨迹——随每日体检累积")
        return
    oos["date"] = pd.to_datetime(oos["eval_date"])
    # 汇总视角：每日各来源的 OOS ICIR 均值/中位
    g = oos.groupby(["date", "kind"])["icir_oos"].agg(["mean", "median", "count"])
    g = g.reset_index()
    fig = go.Figure()
    for kind in g["kind"].unique():
        s = g[g["kind"] == kind].sort_values("date")
        fig.add_trace(go.Scatter(x=s["date"], y=s["median"], mode="lines+markers",
                                 name=f"{kind}（{int(s['count'].iloc[-1])}个）"))
    fig.add_hline(y=0, line_color="gray", line_width=1)
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=30, b=10),
                      yaxis_title="ICIR_OOS 中位数", hovermode="x unified",
                      legend=dict(orientation="h", y=1.12))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("曲线在 0 上方且上行 = 引擎在产出真 alpha；围绕 0 波动 = 产出噪声。"
               "新因子的 OOS 窗口随日子累积变长，统计噪声会自然收敛。")


# ---------------------------------------------------------------- 区块 4：名单覆盖日历
def _render_coverage(picks: pd.DataFrame, outs: pd.DataFrame):
    st.markdown("### 📅 名单覆盖（断更检查）")
    if picks.empty:
        st.info("暂无名单")
        return
    daily = picks.groupby("trade_date").size().rename("名单数").reset_index()
    settled = outs.groupby("eval_date").size().rename("结算数").reset_index() \
        if not outs.empty else pd.DataFrame(columns=["eval_date", "结算数"])

    fig = go.Figure()
    fig.add_trace(go.Bar(x=daily["trade_date"], y=daily["名单数"], name="每日名单",
                         marker_color="#5B8FF9"))
    if not settled.empty:
        fig.add_trace(go.Bar(x=settled["eval_date"], y=settled["结算数"],
                             name="每日结算", marker_color="#61DDAA"))
    fig.update_layout(height=260, barmode="group",
                      margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h", y=1.15), hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)
    gaps = ""
    if len(daily) >= 2:
        d = pd.to_datetime(daily["trade_date"]).sort_values()
        hole = d.diff().dt.days > 4  # 超过4天（含周末）视为断更
        if hole.any():
            gaps = " · ⚠️ 断更点：" + "、".join(d[hole].dt.strftime("%m-%d"))
    st.caption(f"共 {len(daily)} 个交易日有名单，最近：{daily['trade_date'].max()}{gaps}")


def render():
    st.title("✅ 实战验证")
    st.caption("回答一个问题：**因子和策略包到底生不生效？** 全部基于真实到期结算，"
               "不是回测。主口径 = 5 日超额（vs 池内中位数，与策略包持有期一致）")

    picks, outs = _load_outcomes()
    _render_equity(picks, outs)
    st.divider()
    _render_calibration(picks, outs)
    st.divider()
    _render_factor_oos()
    st.divider()
    _render_coverage(picks, outs)


render()
