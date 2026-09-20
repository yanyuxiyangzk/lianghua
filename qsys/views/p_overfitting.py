"""🛡️ 过拟合诊断：回测可信度的系统性检验。

四视角：
  1. IS/OOS三段验证：样本内/验证段/测试段净值曲线 + 逐期超额 + 组合FDR
  2. 回测可信度：回测-实盘偏差演化 + 复杂度惩罚哑铃图
  3. 统计显著性：简单 vs Newey-West p-value + FDR校正 + 假阳性翻转
  4. 选股公式：策略包因子权重 + 质量指标 + 过拟合风险
"""

import logging

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import experience as exp
import factor_eval as fe
import library
from common import all_pools, get_last_trade_day

st.set_page_config(page_title="过拟合诊断", layout="wide")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 数据加载
def _pct_val(v):
    """将 '63%' / 0.63 / NaN 统一为 float or None。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, str):
        v = v.replace("%", "").strip()
        try:
            return float(v) / 100
        except ValueError:
            return None
    return float(v)


@st.cache_data(ttl=300, show_spinner=False)
def _load_bias_history() -> pd.DataFrame:
    """合并两个数据源：track_oos_vs_live (picks级) + pack_leaderboard (策略包级)。"""
    # 数据源1: picks级别 —— 有 oos_winrate_at_save 的具体交易记录
    info = exp.track_oos_vs_live(180)
    details = info.get("details", [])
    by_pack = {}
    for d in details:
        by_pack[d["pack_name"]] = {
            "pack_name": d["pack_name"],
            "oos_winrate": d["oos_winrate"],
            "live_winrate": d["live_winrate"],
            "bias": d["bias"],
            "n_trades": d["n_trades"],
            "source": "picks",
        }

    # 数据源2: 策略包级别 —— pack_leaderboard 的回测/实战胜率
    lb = exp.pack_leaderboard()
    if not lb.empty:
        for _, r in lb.iterrows():
            name = r.get("策略包", "")
            if not name or name == "(未存包)":
                continue
            oos = _pct_val(r.get("回测OOS胜率"))
            live5 = _pct_val(r.get("5日胜率"))
            live1 = _pct_val(r.get("1日胜率"))
            live_wr = live5 if live5 is not None else live1
            n_backfill = int(r.get("已回填战果", 0) or 0)
            if name in by_pack:
                # picks级数据已存在，补充 n_trades
                by_pack[name]["n_trades"] = max(by_pack[name]["n_trades"], n_backfill)
            elif oos is not None and live_wr is not None:
                by_pack[name] = {
                    "pack_name": name,
                    "oos_winrate": round(oos, 3),
                    "live_winrate": round(live_wr, 3),
                    "bias": round(oos - live_wr, 3),
                    "n_trades": n_backfill,
                    "source": "leaderboard",
                }

    if not by_pack:
        return pd.DataFrame()
    return pd.DataFrame(by_pack.values())


@st.cache_data(ttl=300, show_spinner=False)
def _load_all_packs_status() -> pd.DataFrame:
    """全量策略包状态表：回测/实战/样本数一目了然。"""
    packs = library.list_strategies()
    lb = exp.pack_leaderboard()
    lb_map = {}
    if not lb.empty:
        for _, r in lb.iterrows():
            lb_map[r.get("策略包", "")] = r

    rows = []
    for name, pk in packs.items():
        lr = lb_map.get(name, {})
        oos = _pct_val(pk.get("oos_winrate"))
        live5 = _pct_val(lr.get("5日胜率")) if isinstance(lr, dict) else _pct_val(lr.get("5日胜率"))
        live1 = _pct_val(lr.get("1日胜率")) if isinstance(lr, dict) else _pct_val(lr.get("1日胜率"))
        n_backfill = int(lr.get("已回填战果", 0) or 0) if isinstance(lr, dict) else 0

        # 状态判定
        if oos is not None and (live5 is not None or live1 is not None) and n_backfill >= 3:
            status = "✅ 完整"
        elif oos is None and (live5 is not None or live1 is not None):
            status = "🟡 缺回测"
        elif oos is not None and (live5 is None and live1 is None):
            status = "🟡 缺实战"
        else:
            status = "⚠️ 数据不足"

        rows.append({
            "策略包": name,
            "股票池": pk.get("pool_name", "—"),
            "因子数": len(pk.get("factors", [])),
            "回测OOS胜率": oos,
            "实战5日胜率": live5,
            "实战1日胜率": live1,
            "回填笔数": n_backfill,
            "状态": status,
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=300, show_spinner=False)
def _load_gate_stats() -> dict:
    """加载闸门通过率统计。"""
    try:
        with library._lconn() as c:
            df = pd.read_sql(
                "SELECT passed, COUNT(*) as cnt FROM gate_detail_log"
                " GROUP BY passed", c)
        total = int(df["cnt"].sum())
        passed = int(df[df["passed"] == 1]["cnt"].sum()) if 1 in df["passed"].values else 0
        return {"total": total, "passed": passed, "rate": passed / total if total else 0}
    except Exception:
        return {"total": 0, "passed": 0, "rate": 0}


# ---------------------------------------------------------------- 顶部健康仪表盘
def _render_health_dashboard():
    st.markdown("### 🛡️ 过拟合健康指标")
    c1, c2, c3, c4 = st.columns(4)

    # IS/OOS差距（从经验库最近记录）
    with c1:
        bias = _load_bias_history()
        if not bias.empty and "bias" in bias.columns:
            avg_bias = float(bias["bias"].mean())
            color = "normal" if avg_bias < 0.03 else ("warning" if avg_bias < 0.08 else "inverse")
            st.metric("回测-实盘偏差", f"{avg_bias:+.1%}",
                      help="回测OOS胜率 − 实盘胜率，<3%正常，>8%过拟合")
        else:
            st.metric("回测-实盘偏差", "—", help="数据不足")

    # 闸门通过率
    with c2:
        gs = _load_gate_stats()
        if gs["total"] > 0:
            rate = gs["rate"]
            st.metric("闸门通过率", f"{rate:.0%}",
                      help=f"历史 {gs['passed']}/{gs['total']} 次通过")
        else:
            st.metric("闸门通过率", "—")

    # 组合FDR（从pc_auto session state）
    with c3:
        fdr = st.session_state.get("pc_auto", {}).get("combo_fdr")
        if fdr is not None:
            label = "✅ 显著" if fdr < 0.05 else ("🟡 边缘" if fdr < 0.20 else "⚠️ 不显著")
            st.metric("组合 p-value", f"{fdr:.3f}", help="组合级多重检验显著性")
            st.caption(label)
        else:
            st.metric("组合 p-value", "—", help="需先运行 🪄选股工作台 ③")

    # 平均IC p值
    with c4:
        avg_p = st.session_state.get("overfit_avg_ic_p")
        if avg_p is not None:
            st.metric("平均IC p值", f"{avg_p:.4f}", help="Newey-West HAC 校正后")
        else:
            st.metric("平均IC p值", "—", help="需先运行因子体检")


# ---------------------------------------------------------------- Tab 1: IS/OOS三段验证
def _render_tab_is_oos():
    st.markdown("#### IS / OOS验证段 / OOS测试段")

    # 尝试从 session state 获取数据
    wf = st.session_state.get("pc_auto", {}).get("wf")
    wf_test = st.session_state.get("pc_auto", {}).get("wf_test")
    is_bt = st.session_state.get("pc_auto", {}).get("is_bt")

    if wf is None or (isinstance(wf, pd.DataFrame) and wf.empty):
        st.info("💡 请先到 **🪄选股工作台** 或 **🧩选股组合** 运行组合搜索，结果会自动显示在此。")
        return

    # --- 三段净值曲线 ---
    fig = go.Figure()

    if is_bt is not None and not is_bt.empty and "组合扣费超额" in is_bt.columns:
        cum_is = is_bt.set_index("调仓日")["组合扣费超额"].add(1).cumprod()
        fig.add_trace(go.Scatter(
            x=cum_is.index, y=cum_is.values,
            name="IS（固定权重）", line=dict(color="#636EFA", width=2)))

    if not wf.empty and "优化组合扣费超额" in wf.columns:
        cum_oos = wf.set_index("调仓日")["优化组合扣费超额"].add(1).cumprod()
        fig.add_trace(go.Scatter(
            x=cum_oos.index, y=cum_oos.values,
            name="OOS验证段（walk-forward）", line=dict(color="#EF553B", width=2)))

    if wf_test is not None and not wf_test.empty and "优化组合扣费超额" in wf_test.columns:
        cum_test = wf_test.set_index("调仓日")["优化组合扣费超额"].add(1).cumprod()
        fig.add_trace(go.Scatter(
            x=cum_test.index, y=cum_test.values,
            name="OOS测试段（独立留出）", line=dict(color="#00CC96", width=2,
                                                      dash="dash")))

    fig.add_hline(y=1.0, line_dash="dot", line_color="gray", line_width=1)
    fig.update_layout(
        height=380, margin=dict(l=20, r=20, t=40, b=20),
        title="三段净值对比（差距越大 = 过拟合越严重）",
        yaxis_title="累计净值", xaxis_title="调仓日",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
    st.plotly_chart(fig, use_container_width=True)

    # --- 指标行 ---
    c1, c2, c3, c4 = st.columns(4)
    if not wf.empty and "优化组合扣费超额" in wf.columns:
        net = wf["优化组合扣费超额"]
        with c1:
            st.metric("验证段胜率", f"{(net > 0).mean():.0%}")
        with c2:
            st.metric("验证段平均净超额", f"{net.mean():+.2%}/期")

    if is_bt is not None and not is_bt.empty and "组合扣费超额" in is_bt.columns:
        is_wr = (is_bt["组合扣费超额"] > 0).mean()
        oos_wr = (net > 0).mean() if not wf.empty else 0
        with c3:
            gap = is_wr - oos_wr
            st.metric("IS/OOS差距", f"{gap:+.0%}",
                      delta="⚠️ 过拟合" if gap > 0.10 else "正常",
                      delta_color="inverse" if gap > 0.10 else "normal")

    oos_test = st.session_state.get("pc_auto", {}).get("oos_winrate_test")
    with c4:
        if oos_test is not None:
            st.metric("测试段胜率", f"{oos_test:.0%}")
        else:
            st.metric("测试段胜率", "—")

    # --- 逐期超额柱状图 ---
    if not wf.empty and "优化组合扣费超额" in wf.columns:
        st.markdown("#### 逐期超额收益")
        net = wf["优化组合扣费超额"]
        colors = ["#26a74a" if v > 0 else "#e54545" for v in net.values]
        fig2 = go.Figure(go.Bar(
            x=net.index, y=net.values, marker_color=colors,
            name="扣费超额"))
        fig2.add_hline(y=0, line_dash="solid", line_color="gray", line_width=1)
        fig2.add_hline(y=net.mean(), line_dash="dot", line_color="blue",
                       line_width=1, annotation_text=f"均值 {net.mean():+.2%}")
        fig2.update_layout(
            height=260, margin=dict(l=20, r=20, t=30, b=20),
            yaxis_title="超额收益", xaxis_title="调仓日")
        st.plotly_chart(fig2, use_container_width=True)


# ---------------------------------------------------------------- Tab 2: 回测可信度
def _render_tab_credibility():
    st.markdown("#### 回测可信度分析")

    # --- 回测-实盘偏差散点 ---
    st.markdown("##### 回测 vs 实盘校准")
    bias_df = _load_bias_history()
    if not bias_df.empty and "oos_winrate" in bias_df.columns and "live_winrate" in bias_df.columns:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=bias_df["oos_winrate"], y=bias_df["live_winrate"],
            mode="markers+text",
            text=bias_df["pack_name"].str[:15],
            textposition="top center", textfont=dict(size=9),
            marker=dict(size=bias_df.get("n_trades", pd.Series([10] * len(bias_df))) * 3,
                        color=bias_df["bias"], colorscale="RdYlGn_r",
                        colorbar=dict(title="偏差")),
            hovertemplate="回测: %{x:.0%}<br>实盘: %{y:.0%}<br>偏差: %{marker.color:.1%}"
                          "<br>样本: %{marker.size}<extra></extra>"))
        # 45°对角线
        mn = min(bias_df["oos_winrate"].min(), bias_df["live_winrate"].min()) - 0.05
        mx = max(bias_df["oos_winrate"].max(), bias_df["live_winrate"].max()) + 0.05
        fig.add_trace(go.Scatter(
            x=[mn, mx], y=[mn, mx], mode="lines",
            line=dict(dash="dash", color="gray", width=1),
            name="完美校准 (y=x)", showlegend=True))
        fig.update_layout(
            height=400, margin=dict(l=20, r=20, t=30, b=20),
            xaxis_title="回测OOS胜率", yaxis_title="实盘胜率",
            xaxis=dict(tickformat=".0%"), yaxis=dict(tickformat=".0%"))
        st.plotly_chart(fig, use_container_width=True)
        n = len(bias_df)
        st.caption(f"共 {n} 个策略包 · 点越偏离对角线 = 过拟合越严重 · 颜色越红 = 偏差越大 · 点越大 = 样本越多")
    else:
        st.info("暂无回测-实盘校准数据（需有策略包被固化且产生实盘记录）。")

    # --- 策略包全量状态表 ---
    st.markdown("##### 📋 策略包数据状态")
    status_df = _load_all_packs_status()
    if not status_df.empty:
        show = status_df.copy()
        for c in ["回测OOS胜率", "实战5日胜率", "实战1日胜率"]:
            if c in show.columns:
                show[c] = show[c].map(lambda x: f"{x:.0%}" if pd.notna(x) else "—")
        st.dataframe(show, use_container_width=True, hide_index=True,
                     height=min(400, len(show) * 35 + 40))
        n_ok = (status_df["状态"] == "✅ 完整").sum()
        n_no_oos = (status_df["状态"] == "🟡 缺回测").sum()
        n_no_live = (status_df["状态"] == "🟡 缺实战").sum()
        n_no_data = (status_df["状态"] == "⚠️ 数据不足").sum()
        st.caption(f"✅ 完整 {n_ok} · 🟡 缺回测 {n_no_oos} · 🟡 缺实战 {n_no_live} · "
                   f"⚠️ 数据不足 {n_no_data} · 共 {len(status_df)} 个策略包")
    else:
        st.info("暂无策略包数据。")

    st.divider()

    # --- 复杂度惩罚哑铃图 ---
    st.markdown("##### 因子复杂度惩罚")
    try:
        from loopengine.tree import parse
        # 从因子库取已入库因子
        with library._lconn() as c:
            rows = c.execute(
                "SELECT fr.name, fr.code, fs.ic_mean FROM factor_registry fr"
                " LEFT JOIN factor_scorecards fs ON fs.name = fr.name"
                " WHERE fr.engine='loopengine' AND fr.gate_status IN (1, 3)"
                " AND fr.code IS NOT NULL AND fr.code != ''"
                " ORDER BY ABS(COALESCE(fs.ic_mean, 0)) DESC LIMIT 30"
            ).fetchall()

        if rows:
            factors_data = []
            for name, code, ic_mean in rows:
                if not code or "# sexpr:" not in code.split("\n", 1)[0]:
                    continue
                raw_score = abs(ic_mean) if ic_mean else 0.0
                pen = fe.complexity_penalty(code)
                factors_data.append({
                    "name": name[:25], "raw": raw_score,
                    "penalized": raw_score * pen, "penalty_pct": (1 - pen) * 100})

            if factors_data:
                fdf = pd.DataFrame(factors_data).sort_values("penalty_pct", ascending=True)

                fig = go.Figure()
                # 惩罚前分数（左侧圆点）
                fig.add_trace(go.Scatter(
                    x=fdf["raw"], y=fdf["name"], mode="markers",
                    marker=dict(size=10, color="#636EFA"), name="惩罚前"))
                # 惩罚后分数（右侧圆点）
                fig.add_trace(go.Scatter(
                    x=fdf["penalized"], y=fdf["name"], mode="markers",
                    marker=dict(size=10, color="#EF553B"), name="惩罚后"))
                # 连接线
                for _, row in fdf.iterrows():
                    color = "#26a74a" if row["penalty_pct"] < 5 else (
                        "#FFA15A" if row["penalty_pct"] < 15 else "#e54545")
                    fig.add_trace(go.Scatter(
                        x=[row["raw"], row["penalized"]], y=[row["name"], row["name"]],
                        mode="lines", line=dict(color=color, width=2),
                        showlegend=False, hoverinfo="skip"))

                fig.update_layout(
                    height=max(350, len(fdf) * 28 + 80),
                    margin=dict(l=10, r=10, t=30, b=20),
                    xaxis_title="因子评分", yaxis=dict(autorange="reversed"),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02))
                st.plotly_chart(fig, use_container_width=True)
                st.caption("蓝点=原始评分，红点=复杂度惩罚后。连线越长=惩罚越重（表达式越复杂）。")
            else:
                st.info("暂无进化因子的表达式数据。")
        import factor_eval as _fe  # noqa: F811
    except ImportError:
        st.info("loopengine.tree 不可用，无法评估复杂度。")

    # --- 因子回测 K 线反馈 ---
    st.divider()
    st.markdown("##### 📈 因子回测 K 线反馈")
    st.caption("选择已回测因子和股票，查看因子值、价格走势及未来收益；因子值不直接当作买卖信号。")
    try:
        with library._lconn() as c:
            factor_rows = c.execute("SELECT name, kind, code FROM factor_registry "
                                    "WHERE name IS NOT NULL ORDER BY name").fetchall()
        factor_map = {n: {"name": n, "kind": k or "builtin", "code": code}
                      for n, k, code in factor_rows}
        names = list(factor_map)
        if names:
            c1, c2, c3 = st.columns([2, 1, 1])
            with c1:
                factor_name = st.selectbox("因子", names, key="of_factor")
            with c2:
                pool_name = st.selectbox("股票池", list(all_pools()), key="of_pool")
            with c3:
                code = st.selectbox("股票", all_pools()[pool_name], key="of_code")
            if st.button("加载因子 K 线反馈", key="of_load", type="primary"):
                st.session_state["of_request"] = (factor_name, code)
            if st.session_state.get("of_request") == (factor_name, code):
                import datasource
                import factor_eval as _fe
                end = get_last_trade_day()
                source = datasource.get_loop_source()
                vals = _fe.get_factor_values(factor_map[factor_name], [code], end,
                                               lookback_days=180, source=source)
                panel = datasource.get_panel([code],
                    (pd.Timestamp(end) - pd.Timedelta(days=300)).strftime("%Y-%m-%d"),
                    end, ["$open", "$high", "$low", "$close", "$volume"], source=source)
                if vals.empty or panel.empty:
                    st.warning("该因子或股票暂无可用回测数据。")
                else:
                    v = vals.xs(code, level="instrument", drop_level=True).rename("因子值")
                    p = panel.xs(code, level="instrument", drop_level=True).join(v, how="inner").dropna(subset=["$close", "因子值"])
                    if p.empty:
                        st.warning("因子值与 K 线没有重叠日期。")
                    else:
                        p["因子分位"] = p["因子值"].rolling(20, min_periods=5).rank(pct=True)
                        fig = go.Figure(go.Candlestick(x=p.index, open=p["$open"], high=p["$high"],
                                                       low=p["$low"], close=p["$close"], name="K线"))
                        hi = p[p["因子分位"] >= .8]
                        lo = p[p["因子分位"] <= .2]
                        fig.add_trace(go.Scatter(x=hi.index, y=hi["$high"] * 1.01, mode="markers",
                                                 marker=dict(color="#e54545", size=7), name="因子高分位"))
                        fig.add_trace(go.Scatter(x=lo.index, y=lo["$low"] * .99, mode="markers",
                                                 marker=dict(color="#2ca02c", size=7), name="因子低分位"))
                        fig.update_layout(height=480, xaxis_rangeslider_visible=False,
                                          yaxis_title="价格", margin=dict(l=10, r=10, t=25, b=10))
                        st.plotly_chart(fig, use_container_width=True)
                        st.line_chart(p[["因子值"]], height=180)
                        st.caption(f"样本 {len(p)} 天 · 因子均值 {p['因子值'].mean():.4g} · "
                                   f"高分位日占比 {(p['因子分位'] >= .8).mean():.1%}")
    except Exception as exc:
        st.error(f"因子 K 线反馈加载失败：{exc}")


# ---------------------------------------------------------------- Tab 3: 统计显著性
def _render_tab_significance():
    st.markdown("#### 统计显著性诊断")

    # 加载因子评分卡
    try:
        with library._lconn() as c:
            sc = pd.read_sql(
                "SELECT fs.name, fs.ic_mean, fs.icir, fs.ic_winrate, fs.kind,"
                " fs.days"
                " FROM factor_scorecards fs"
                " WHERE fs.pool_name='沪深300'"
                " AND fs.eval_date = (SELECT MAX(eval_date) FROM factor_scorecards"
                "   WHERE pool_name='沪深300')"
                " AND fs.ic_mean IS NOT NULL"
                " ORDER BY ABS(fs.ic_mean) DESC LIMIT 80", c)
    except Exception:
        sc = pd.DataFrame()

    if sc.empty:
        st.info("暂无因子评分卡数据（需先运行体检）。")
        return

    # --- 简单 vs Newey-West p-value 散点 ---
    st.markdown("##### IC p-value：简单 vs Newey-West HAC")
    st.caption("点在对角线上方 = Newey-West 更保守（IC存在自相关），越远离对角线说明简单p-value越不可信。")

    # 计算每个因子的p-value
    results = []
    for _, row in sc.iterrows():
        ic_mean = row.get("ic_mean", 0) or 0
        ic_std_val = abs(ic_mean / row["icir"]) if pd.notna(row.get("icir")) and abs(row.get("icir", 0)) > 1e-12 else 1.0
        n_days = row.get("days", 200) or 200
        p_simple = fe.ic_pvalue(ic_mean, ic_std_val, n_days) if n_days >= 10 else 1.0
        results.append({
            "name": row["name"], "kind": row.get("kind", ""),
            "p_simple": p_simple, "ic_mean": ic_mean
        })

    if results:
        rdf = pd.DataFrame(results)

        # 散点图：p-value vs IC均值
        kind_colors = {"内置": "#636EFA", "技术指标": "#AB63FA", "进化": "#EF553B",
                       "演化引擎": "#FF97FF", "loopengine": "#EF553B"}
        fig = go.Figure()
        for kind in rdf["kind"].unique():
            sub = rdf[rdf["kind"] == kind]
            fig.add_trace(go.Scatter(
                x=sub["ic_mean"], y=sub["p_simple"],
                mode="markers", name=kind,
                marker=dict(color=kind_colors.get(kind, "#999"), size=8),
                text=sub["name"], hovertemplate="%{text}<br>IC: %{x:.4f}<br>p值: %{y:.4f}<extra></extra>"))

        fig.add_hline(y=0.05, line_dash="dot", line_color="red", line_width=1,
                      annotation_text="p=0.05 显著性阈值")
        fig.update_layout(
            height=420, margin=dict(l=20, r=20, t=30, b=20),
            xaxis_title="IC均值", yaxis_title="p值",
            yaxis=dict(type="log", dtick=1))
        st.plotly_chart(fig, use_container_width=True)

        # 显著性统计
        sig_05 = (rdf["p_simple"] < 0.05).sum()
        sig_01 = (rdf["p_simple"] < 0.01).sum()
        st.markdown(f"**显著性分布**：p<0.01 有 {sig_01} 个，p<0.05 有 {sig_05} 个，共 {len(rdf)} 个因子")

        # 显著因子列表
        sig_factors = rdf[rdf["p_simple"] < 0.05].sort_values("p_simple")
        if not sig_factors.empty:
            st.markdown("##### ✅ 显著因子（p<0.05）")
            display = sig_factors[["name", "kind", "ic_mean", "p_simple"]].copy()
            display.columns = ["因子", "类型", "IC均值", "p值"]
            st.dataframe(display, use_container_width=True, hide_index=True, height=min(350, len(display) * 35 + 40))

        # FDR校正q值表
        st.markdown("##### FDR校正后q值（Benjamini-Hochberg）")
        display = rdf[["name", "kind", "ic_mean", "p_simple"]].copy()
        # 计算BH FDR
        p_series = pd.Series(rdf["p_simple"].values, index=rdf["name"])
        q_vals = fe.bh_fdr(p_series, alpha=0.05)
        display["q值"] = [q_vals.get(n, 1.0) for n in rdf["name"]]
        display["FDR显著"] = display["q值"] < 0.05
        display = display.sort_values("q值")
        display.columns = ["因子", "类型", "IC均值", "p值", "q值", "FDR显著"]
        st.dataframe(display, use_container_width=True, hide_index=True, height=min(400, len(display) * 35 + 40))

        # 存到session state供顶部仪表盘用
        st.session_state["overfit_avg_ic_p"] = float(rdf["p_simple"].mean())
    else:
        st.info("无法计算p-value（因子数据不足）。")


# ---------------------------------------------------------------- Tab 4: 选股公式
def _render_tab_formula():
    st.markdown("#### 选股算法公式")

    packs = library.list_strategies()
    if not packs:
        st.info("暂无策略包。请先到 🪄选股工作台 或 🧩选股组合 固化组合。")
        return

    # --- 策略包选择 ---
    pack_names = list(packs.keys())
    sel = st.selectbox("选择策略包", pack_names, key="of_formula_pack")
    pk = packs[sel]

    # --- 基本信息 ---
    factors = pk.get("factors", [])
    method = pk.get("method", "ICIR加权")
    horizon = pk.get("horizon") or "—"
    top_n = pk.get("top_n", 10)
    pool = pk.get("pool_name", "—")
    oos_wr = pk.get("oos_winrate")
    is_wr = pk.get("is_winrate")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("股票池", pool)
    with c2:
        st.metric("持有期", horizon)
    with c3:
        st.metric("Top-N", top_n)
    with c4:
        st.metric("加权方法", method)

    st.divider()

    # --- 公式展示 ---
    st.markdown("##### 📐 综合评分公式")
    st.code(
        "Score(c) = (1/N) × Σ [ norm(z_i(c)) × w_i × d_i ] / w_total\n"
        "\n"
        "  z_i(c)   = 因子i对股票c的截面标准化值（zscore 或 rank）\n"
        "  w_i      = 因子i的原始权重（由加权方法决定）\n"
        "  d_i      = 因子i的方向（+1正向 / -1负向）\n"
        "  norm     = zscore / rank（取决于标准化方案）\n"
        "  w_total  = Σ w_i（所有正权重之和，归一化分母）\n"
        "  N        = 有效因子数（NaN因子不计入）",
        language=None)

    # 加权方法说明
    method_docs = {
        "等权": "w_i = 1/N",
        "ICIR加权": "w_i = |ICIR_i| / Σ|ICIR_j|",
        "胜率加权": "w_i = max(winrate_i − 0.5, 0) / Σ...",
        "均值方差": "w_i = ICIR_i²（因 σ²=(μ/ICIR)², 故 μ/σ² = ICIR²/μ → 归一化后 ∝ ICIR²）",
    }
    st.caption(f"加权方法：{method} → {method_docs.get(method, '—')}")

    st.divider()

    # --- 因子权重条形图 ---
    if factors:
        st.markdown("##### 📊 因子权重分布")
        fdf = pd.DataFrame(factors)
        fdf["label"] = fdf.apply(
            lambda r: f"{'↑' if r.get('direction', 1) > 0 else '↓'} {r['name']}", axis=1)
        fdf["color"] = fdf["direction"].apply(
            lambda d: "#26a74a" if d > 0 else "#e54545")

        fig = go.Figure(go.Bar(
            x=fdf["weight"], y=fdf["label"],
            orientation="h", marker_color=fdf["color"],
            text=fdf["weight"].map(lambda w: f"{w:.1%}"),
            textposition="outside"))
        fig.update_layout(
            height=max(200, len(fdf) * 32 + 60),
            margin=dict(l=10, r=60, t=10, b=20),
            xaxis_title="权重", yaxis=dict(autorange="reversed"),
            showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
        st.caption("绿↑=正向（值越大越好），红↓=负向（值越小越好）。")
    else:
        st.info("该策略包无因子配置信息。")

    st.divider()

    # --- 因子质量表格 ---
    if factors:
        st.markdown("##### 🔬 因子质量指标")
        fnames = [f["name"] for f in factors]
        if not fnames:
            st.info("该策略包无因子配置。")
        else:
            try:
                with library._lconn() as c:
                    placeholders = ",".join("?" * len(fnames))
                    sc = pd.read_sql(
                        f"SELECT name, ic_mean, icir, ic_winrate, kind"
                        f" FROM factor_scorecards"
                        f" WHERE name IN ({placeholders})"
                        f" AND pool_name=?"
                        f" AND eval_date = (SELECT MAX(eval_date) FROM factor_scorecards"
                        f"   WHERE pool_name=?)",
                        c, params=fnames + [pool, pool])

                if not sc.empty:
                    # 合并权重信息 + 标准化方法
                    wmap = {f["name"]: f for f in factors}
                    try:
                        import signals as _sig
                        norms = _sig.scoring_norms(fnames, factors) or {}
                    except Exception:
                        norms = {}
                    rows = []
                    for _, r in sc.iterrows():
                        fcfg = wmap.get(r["name"], {})
                        rows.append({
                            "因子": r["name"],
                            "类型": r.get("kind", ""),
                            "IC均值": r.get("ic_mean"),
                            "ICIR": r.get("icir"),
                            "IC胜率": r.get("ic_winrate"),
                            "权重": fcfg.get("weight"),
                            "方向": "正向" if fcfg.get("direction", 1) > 0 else "负向",
                            "标准化": norms.get(r["name"], "zscore"),
                        })
                    qdf = pd.DataFrame(rows)
                    for col in ["IC均值", "ICIR", "IC胜率"]:
                        if col in qdf.columns:
                            qdf[col] = qdf[col].map(
                                lambda x: f"{x:.4f}" if pd.notna(x) else "—")
                    if "权重" in qdf.columns:
                        qdf["权重"] = qdf["权重"].map(
                            lambda x: f"{x:.1%}" if pd.notna(x) else "—")
                    st.dataframe(qdf, use_container_width=True, hide_index=True,
                                 height=min(350, len(qdf) * 35 + 40))
                else:
                    st.info("因子评分卡中无该包因子数据（需先运行体检）。")
            except Exception as e:
                logger.exception("读取因子评分卡失败")
                st.info(f"无法读取因子评分卡：{e}")

    # --- LoopEngine因子表达式 & 复杂度惩罚 ---
    le_factors = [f for f in factors if f.get("kind") in ("loopengine", "evolved")]
    if le_factors:
        st.markdown("##### 🧬 进化因子表达式 & 复杂度惩罚")
        try:
            with library._lconn() as c:
                le_names = [f["name"] for f in le_factors]
                ph = ",".join("?" * len(le_names))
                reg_rows = c.execute(
                    f"SELECT name, code FROM factor_registry"
                    f" WHERE name IN ({ph}) AND code IS NOT NULL AND code != ''",
                    le_names).fetchall()
            code_map = {r[0]: r[1] for r in reg_rows}

            pen_rows = []
            for f in le_factors:
                name = f["name"]
                code = code_map.get(name, "")
                sexpr = ""
                penalty = None
                if code and "# sexpr:" in code.split("\n", 1)[0]:
                    sexpr = code.split("\n", 1)[0].replace("# sexpr: ", "")
                    penalty = fe.complexity_penalty(code)
                pen_rows.append({
                    "因子": name,
                    "表达式": sexpr[:60] + ("…" if len(sexpr) > 60 else ""),
                    "复杂度惩罚": f"{penalty:.2f}" if penalty is not None else "—",
                    "权重": f"{f.get('weight', 0):.1%}",
                })
            if pen_rows:
                pdf = pd.DataFrame(pen_rows)
                st.dataframe(pdf, use_container_width=True, hide_index=True,
                             height=min(300, len(pdf) * 35 + 40))
                st.caption("复杂度惩罚：1.0=无惩罚，越小惩罚越重（表达式越复杂，过拟合风险越高）。")
        except Exception as e:
            logger.warning("读取因子表达式失败: %s", e)

    st.divider()

    # --- Walk-Forward 参数 ---
    st.markdown("##### ⚙️ Walk-Forward 验证参数")
    st.markdown(
        "| 参数 | 值 | 说明 |\n|------|----|------|\n"
        "| 估计窗 | 250 交易日 | 计算IC/ICIR的滚动窗口 |\n"
        "| 应用窗 | 5 交易日 | 每5日重新估计权重 |\n"
        "| 交易成本 | 0.25% 双边 | 超额收益已扣除 |\n"
        "| 防前视 | IC端点回退 fwd_days | 确保无未来函数 |")

    st.divider()

    # --- 过拟合风险指标 ---
    st.markdown("##### ⚠️ 过拟合风险")
    c1, c2, c3 = st.columns(3)

    with c1:
        if oos_wr and is_wr:
            oos_v = float(oos_wr.replace("%", "")) / 100 if isinstance(oos_wr, str) else oos_wr
            is_v = float(is_wr.replace("%", "")) / 100 if isinstance(is_wr, str) else is_wr
            gap = is_v - oos_v
            st.metric("IS/OOS差距", f"{gap:+.0%}",
                      delta="⚠️ 过拟合" if gap > 0.10 else "正常",
                      delta_color="inverse" if gap > 0.10 else "normal")
        else:
            st.metric("IS/OOS差距", "—", help="需在选股工作台③运行双轨验证后重存")

    with c2:
        lb = exp.pack_leaderboard()
        if not lb.empty:
            row = lb[lb["策略包"] == sel]
            if not row.empty:
                row = row.iloc[0]
                n_backfill = int(row.get("已回填战果", 0))
                live_wr = None
                for col in ["5日胜率", "1日胜率", "20日胜率"]:
                    if col in row.index and pd.notna(row[col]):
                        live_wr = float(row[col])
                        break
                if live_wr is not None and n_backfill >= 3:
                    st.metric("实战胜率", f"{live_wr:.0%}",
                              help=f"基于 {n_backfill} 笔回填战果")
                else:
                    st.metric("实战胜率", "—",
                              help=f"样本不足（仅 {n_backfill} 笔，需≥3笔）")
            else:
                st.metric("实战胜率", "—", help="该包尚无实战记录")
        else:
            st.metric("实战胜率", "—")

    with c3:
        fdr = st.session_state.get("pc_auto", {}).get("combo_fdr")
        if fdr is not None:
            st.metric("组合 p-value", f"{fdr:.3f}",
                      help="组合级多重检验显著性")
        else:
            st.metric("组合 p-value", "—", help="需先运行选股工作台③")


# ---------------------------------------------------------------- Tab 5: 数学公式
def _render_tab_math_formulas():
    st.markdown("#### LoopEngine 因子生成算法数学公式")
    st.caption("将因子演化引擎形式化为严格数学表达 · 适用于论文引用与算法审计")

    # --- 1. 问题定义 ---
    st.markdown("##### 1. 问题定义")
    st.code(
        "目标：找到 S-表达式树 f*(x)，使得\n"
        "\n"
        "    f* = argmax  Score(f) = argmax  IC(f) + λ₁·ICIR(f) + λ₂·WinRate(f)\n"
        "          f∈F          f∈F\n"
        "\n"
        "约束：\n"
        "    depth(f) ≤ 6\n"
        "    type(f) 匹配\n"
        "    无除零风险\n"
        "    骨架(f) ∉ FSA_frozen",
        language=None)
    st.caption("其中 F 是所有合法 S-表达式树的集合，IC 为信息系数，ICIR 为IC比率")

    st.divider()

    # --- 2. 搜索空间 ---
    st.markdown("##### 2. 搜索空间")
    st.code(
        "F = { f : f 由以下递归定义 }\n"
        "    f  → op(f₁, f₂)        # 二元算子\n"
        "        → unary_op(f₁)     # 一元算子\n"
        "        → op(f₁, w)        # 算子+窗口参数\n"
        "        → field(w)         # 字段+窗口\n"
        "\n"
        "字段集:   V = {v₁, v₂, ..., vₙ}  (n ≈ 50+)\n"
        "算子集:   O = {o₁, o₂, ..., oₘ}  (m = 18)\n"
        "窗口集:   W = {3, 5, 10, 15, 20, 30, 40, 60, 90, 120, 150, 200}",
        language=None)
    st.caption("字段涵盖量价、资金流、板块轮动、龙虎榜、盘口、指数、爆量抢筹7大类")

    st.divider()

    # --- 3. 核心公式 ---
    st.markdown("##### 3. 核心公式")

    # 3.1 适应度评分
    with st.expander("3.1 适应度评分（Fitness）", expanded=False):
        st.code(
            "Score(f) = LiveBoost(f) × ValueScore(f) × DecayWeight(f)"
            " × RegimeWeight(f) × ComplexityPenalty(f)\n"
            "\n"
            "其中:\n"
            "  LiveBoost(family(f)) ∈ [0, 1]         # 实战加权\n"
            "  ValueScore(f) = Σᵢ wᵢ·xᵢ               # 5维价值评分\n"
            "  DecayWeight(f) ∈ {1.0, 0.7, 0.4, 0.2}  # 衰减惩罚\n"
            "  RegimeWeight(f) ∈ [0.3, 1.5]           # 市场环境加权\n"
            "  ComplexityPenalty(f) ∈ [0.5, 1.0]      # 复杂度惩罚",
            language=None)
        st.caption("每个因子的最终得分由5个乘性因子决定，任一为0则整体为0")

    # 3.2 自适应预算
    with st.expander("3.2 自适应预算（Adaptive Budget）", expanded=False):
        st.code(
            "pₛ(t+1) = pₛ(t) + Δ(t)\n"
            "\n"
            "其中:\n"
            "  Δ(t) = { +0.02,  如果 s = argmax{accept_rate}\n"
            "           -0.02,  如果 s = argmin{accept_rate}\n"
            "            0,     其他 }\n"
            "\n"
            "约束: Σₛ pₛ = 1,  0.05 ≤ pₛ ≤ 0.45",
            language=None)
        st.caption("7种生成源的概率根据历史采纳率动态调整，最优源+2%，最差源-2%")

    # 3.3 衰减检测
    with st.expander("3.3 衰减检测（Decay Detection）", expanded=False):
        st.code(
            "IC_long  = mean(IC[t₀-500 : t₀-60])\n"
            "IC_short = mean(IC[t₀-60 : t₀])\n"
            "d        = (IC_short - IC_long) / |IC_long|\n"
            "\n"
            "统计检验: H₀: μ_short = μ_long  (Welch's t-test)\n"
            "显著性:   p < 0.05\n"
            "\n"
            "衰减分类:\n"
            "  w(d) = { 1.0,  if d ≥ -0.30 ∨ p ≥ 0.05\n"
            "           0.7,  if d < -0.30 ∧ p < 0.05\n"
            "           0.4,  if d < -0.50 ∧ p < 0.05\n"
            "           0.2,  if d < -0.70 ∧ p < 0.05 }",
            language=None)
        st.caption("通过比较近期IC与历史IC判断因子是否衰减，重度衰减因子权重降至0.2")

    # 3.4 市场环境
    with st.expander("3.4 市场环境（Regime Detection）", expanded=False):
        st.code(
            "S_trend    = f(均线斜率, 趋势强度)\n"
            "S_momentum = f(动量指标, 涨跌比)\n"
            "S_vol      = f(波动率, 成交额)\n"
            "\n"
            "combined = 0.6 × S_trend + 0.4 × S_momentum × 10\n"
            "\n"
            "regime(combined, S_vol) = { bull,       if combined > 0.3\n"
            "                           bear,       if combined < -0.3\n"
            "                           transition, if |combined| ≤ 0.3 ∧ S_vol > μ+σ\n"
            "                           sideways }  # 其他",
            language=None)
        st.caption("基于沪深300/深证成指/创业板指的4维度综合判断")

    # 3.5 多目标评分
    with st.expander("3.5 多目标评分（Multi-Objective）", expanded=False):
        st.code(
            "Score(f) = Σᵢ wᵢ · norm(xᵢ(f))\n"
            "\n"
            "其中:\n"
            "  x₁ = IC均值 (IC)\n"
            "  x₂ = ICIR (IC / std(IC))\n"
            "  x₃ = IC胜率 (IC > 0 的比例)\n"
            "  x₄ = Top组胜率\n"
            "  x₅ = 因子相关性惩罚\n"
            "\n"
            "  norm(x) = (x - min) / (max - min)  # Min-Max归一化\n"
            "  w = [0.80, 0.10, 0.10, 0, 0]       # 权重",
            language=None)
        st.caption("IC主导(80%)，风险/夏普只保留灾难阈值惩罚（回撤>70%、夏普<0.5才扣分）")

    # 3.6 遗传操作
    with st.expander("3.6 遗传操作（Genetic Operations）", expanded=False):
        st.code(
            "突变:    mutate(f) = replace_subtree(f, random_node, random_tree(depth≤6))\n"
            "交叉:    crossover(f₁, f₂) = swap_subtree(f₁, f₂, compatible_nodes)\n"
            "扰动:    perturb(f) = adjust_param(f, ±Δw, momentum)\n"
            "随机:    random(f) = build_tree(random_field(), random_op(), depth≤6)",
            language=None)
        st.caption("7种生成源通过自适应预算动态选择，确保探索与利用的平衡")

    st.divider()

    # --- 4. 算法流程 ---
    st.markdown("##### 4. 算法流程")
    st.code(
        "初始化: F₀ = {f₁, f₂, ..., fₙ}  (随机生成)\n"
        "For t = 1, 2, ..., T:\n"
        "    1. 评估: Score(f) = eval(f), ∀f ∈ F_{t-1}\n"
        "    2. 选择: F'_t = select(F_{t-1}, p)  # 基于Score的轮盘赌\n"
        "    3. 演化: F''_t = evolve(F'_t)       # 突变/交叉/LLM\n"
        "    4. 过滤: F_t = gate(F''_t)          # 12道硬闸门\n"
        "    5. 更新: p = update_budget(p, accept_rate)\n"
        "    6. 衰减: decay = detect_decay(IC_series)\n"
        "    7. F_t = F_t × decay                # 降权衰减因子",
        language=None)
    st.caption("每轮演化批量处理30个候选因子，持续迭代优化")

    st.divider()

    # --- 5. 强化学习框架 ---
    st.markdown("##### 5. 强化学习框架")
    st.code(
        "整个 LoopEngine 可以用强化学习框架表达：\n"
        "\n"
        "状态:    s = (IC_series, decay_status, budget, regime)\n"
        "动作:    a = (operation, tree)\n"
        "奖励:    r = Score(f) - ComplexityPenalty(f)\n"
        "策略:    π(a|s) = p(operation) × p(tree)\n"
        "更新:    π ← π + α·(r - b)·∇π\n"
        "\n"
        "不可公式化的部分：\n"
        "- LLM 引导生成：神经网络，只能优化输入输出\n"
        "- LLM 审查：神经网络，只能优化输入输出\n"
        "- 本质是探索策略的参数化近似",
        language=None)
    st.caption("除LLM部分外，整个系统可用强化学习框架表达")


# ---------------------------------------------------------------- 主渲染
def render():
    st.markdown("## 🛡️ 过拟合诊断")
    st.caption("回测可信度的系统性检验 · 数据来自因子评分卡 / 经验库 / walk-forward结果")

    _render_health_dashboard()
    st.divider()

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["📈 IS/OOS三段验证", "🎯 回测可信度", "📐 统计显著性",
         "📊 选股公式", "🧮 数学公式"])

    with tab1:
        _render_tab_is_oos()
    with tab2:
        _render_tab_credibility()
    with tab3:
        _render_tab_significance()
    with tab4:
        _render_tab_formula()
    with tab5:
        _render_tab_math_formulas()


if __name__ == "__main__":
    render()
