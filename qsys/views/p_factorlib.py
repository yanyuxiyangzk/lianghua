"""🧮 因子策略库：因子/策略列表 + 点击进详情 + 单因子/单策略回测。

因子详情：类别/来源/代码/体检指标 + 单因子回测（IC累计、十分层、多空对冲净值）
策略详情：完整配置 + 策略回测（walk-forward 累计超额）+ 今日实盘名单
持久化：factor_registry / factor_scorecards / strategies 三张表（library 层，market.db）。
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import experience
import factor_eval as fe
import library
import signals as sig
from common import all_pools, get_evolved_factors, get_last_trade_day


def _fac_of(name: str, reg_map: dict) -> dict:
    reg = reg_map.get(name, {})
    return {"name": name, "kind": reg.get("kind", "builtin"), "code": reg.get("code")}


def _metrics_html(items: dict) -> str:
    spans = "".join(f"<span style='color:#999'>{k}</span> <b style='color:#ddd'>{v}</b>　" for k, v in items.items())
    return f"<div style='background:#161618;padding:8px 12px;border-radius:6px'>{spans}</div>"


# ---------------------------------------------------------------- 因子详情
def _render_factor_detail(pick: str, pool_name: str, row: pd.Series, reg_map: dict):
    cat = row.get("类别", "")
    st.markdown(f"#### 🔬 {pick}")
    st.markdown(_metrics_html({
        "类别": cat, "来源": row["来源"], "建议方向": row["建议方向"],
        "IC均值": f"{row['IC均值']:.4f}" if pd.notna(row["IC均值"]) else "—",
        "ICIR": f"{row['ICIR']:.3f}" if pd.notna(row["ICIR"]) else "—",
        "IC胜率": f"{row['IC胜率']:.1%}" if pd.notna(row["IC胜率"]) else "—",
        "Top组胜率": f"{row['Top组胜率']:.1%}" if pd.notna(row["Top组胜率"]) else "—",
    }), unsafe_allow_html=True)
    reg = reg_map.get(pick, {})
    if reg.get("trace"):
        st.caption(f"出自 RD-Agent trace `{reg['trace']}` Round {reg.get('round')} · "
                   f"{'✅ 被接受' if reg.get('decision') else '❌ 被拒绝/待定'}")
    if reg.get("code"):
        with st.expander("因子代码（RD-Agent 产出）"):
            st.code(reg["code"], language="python")
    elif pick in sig.TECH_INDICATORS:
        st.caption(f"技术指标：{sig.TECH_INDICATORS[pick]}")
    elif pick in sig.CATALOG_NAMES:
        st.caption(f"常见因子 · 类别 {sig.NAME2CAT.get(pick)}")
    else:
        st.caption(f"内置因子：{sig.BUILTIN_FACTORS.get(pick, '')}")

    if st.button(f"🔬 运行单因子回测（{pool_name}）", key=f"fl_bt_{pick}"):
        codes = all_pools()[pool_name]
        end = get_last_trade_day()
        with st.spinner("回测中（因子值缓存命中则秒出）…"):
            fac = _fac_of(pick, reg_map)
            vals = fe.get_factor_values(fac, codes, end)
            panel = sig.get_panel_cached(codes, end, 800, source="qlib_local")
            st.session_state[f"fl_bt_{pool_name}_{pick}"] = fe.factor_group_backtest(vals, panel)
    bt = st.session_state.get(f"fl_bt_{pool_name}_{pick}")
    if bt:
        c1, c2 = st.columns([2, 1])
        with c1:
            st.markdown("**累计 IC 曲线**（因子预测力的稳定性）")
            st.line_chart(bt["ic"].cumsum(), height=220)
        with c2:
            st.markdown("**多空对冲绩效**（顶组多-底组空）")
            st.table(pd.DataFrame(bt["ls_stats"], index=["值"]).T if bt["ls_stats"] else pd.DataFrame({"提示": ["数据不足"]}, index=[0]))
        c3, c4 = st.columns(2)
        with c3:
            st.markdown("**十分层平均 20 日收益**（单调性检验：应沿 G1→G10 单调）")
            gm = pd.Series(bt["group_mean"]).dropna()
            fig = go.Figure(go.Bar(x=gm.index, y=gm.values,
                                   marker_color=["#2ca02c" if v < 0 else "#e54545" for v in gm.values]))
            fig.update_layout(height=260, margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig, width='stretch')
        with c4:
            st.markdown("**多空对冲净值曲线**")
            st.line_chart(bt["ls_nav"], height=260)


# ---------------------------------------------------------------- 策略详情
def _render_strategy_detail(name: str, pk: dict, reg_map: dict):
    st.markdown(f"#### 💼 {name}")
    st.markdown(_metrics_html({
        "股票池": pk["pool_name"], "Top-N": pk["top_n"], "加权方法": pk.get("method"),
        "过滤器": "、".join(pk.get("filters", [])) or "无",
        "回测OOS胜率": pk.get("oos_winrate") or "未验证", "更新": pk.get("updated"),
    }), unsafe_allow_html=True)
    ft = pd.DataFrame([
        {"因子": f["name"], "类别": sig.NAME2CAT.get(f["name"], "进化/内置"),
         "权重": f"{f['weight']:.1%}", "方向": "正向" if f["direction"] > 0 else "负向"}
        for f in pk.get("factors", [])])
    st.dataframe(ft, width='stretch', hide_index=True)

    codes = all_pools()[pk["pool_name"]]
    end = get_last_trade_day()

    def _fvals():
        out = {}
        for f in pk["factors"]:
            out[f["name"]] = fe.get_factor_values(_fac_of(f["name"], reg_map), codes, end)
        return out

    b1, b2 = st.columns(2)
    with b1:
        run_bt = st.button("🧪 运行策略回测（walk-forward）", key=f"st_bt_{name}")
    with b2:
        run_picks = st.button("📋 生成今日实盘名单", key=f"st_pk_{name}")

    if run_bt:
        with st.spinner("滚动回测中…"):
            panel = sig.get_panel_cached(codes, end, 800, source="qlib_local")
            wf = fe.walk_forward(_fvals(), panel, pk.get("method", "ICIR加权"), pk["top_n"])
            st.session_state[f"st_wf_{name}"] = wf
    wf = st.session_state.get(f"st_wf_{name}")
    if wf is not None and not wf.empty:
        oos_win = (wf["优化组合超额"] > 0).mean()
        eq_win = (wf["等权组合超额"] > 0).mean() if "等权组合超额" in wf else None
        st.markdown(_metrics_html({
            "OOS胜率(优化)": f"{oos_win:.0%}", "OOS胜率(等权)": f"{eq_win:.0%}" if eq_win is not None else "—",
            "平均每期超额": f"{wf['优化组合超额'].mean():.2%}", "应用点数": len(wf),
        }), unsafe_allow_html=True)
        cum = wf.set_index("调仓日")[[c for c in ["优化组合超额", "等权组合超额"] if c in wf]].add(1).cumprod()
        st.line_chart(cum, height=260)
        with st.expander("逐点明细"):
            st.dataframe(wf, width='stretch')

    if run_picks:
        with st.spinner("计算今日名单…"):
            weights = {f["name"]: (f["weight"], f["direction"]) for f in pk["factors"]}
            score = sig.composite_score(_fvals(), weights)
            panel = sig.get_panel_cached(codes, end, 800, source="qlib_local")
            survived = sig.apply_filters(score.index.tolist(), panel, pk.get("filters", []))
            final = score[score.index.isin(survived)].head(pk["top_n"])
            st.session_state[f"st_picks_{name}"] = final
    picks = st.session_state.get(f"st_picks_{name}")
    if picks is not None and not picks.empty:
        st.markdown(f"**今日 Top-{len(picks)} 实盘名单（{end}）**")
        st.dataframe(pd.DataFrame({"综合分": picks.round(3)}), width='stretch', height=240)


# ---------------------------------------------------------------- 页面
def render():
    st.title("🧮 因子策略库")
    st.caption("点击表格行进入详情 · 因子/策略均支持回测 · 持久化于本地库")

    facs_all = [{"name": n, "kind": "builtin", "code": None} for n in sig.BUILTIN_FACTORS]
    facs_all += [{"name": n, "kind": "tech", "code": None} for n in sig.TECH_INDICATORS]
    facs_all += [{"name": n, "kind": "tech", "code": None} for n in sig.CATALOG_NAMES]
    for f in get_evolved_factors(only_accepted=False):
        facs_all.append({"name": f["name"], "kind": "evolved", "code": f["code"],
                         "trace": f.get("trace"), "round": f.get("round"), "decision": f.get("decision")})
    library.sync_factor_registry(facs_all)
    registry = library.get_factor_registry()
    reg_map = registry.set_index("name").to_dict("index") if not registry.empty else {}

    # ================================ ① 因子库 ================================
    st.markdown("## ① 因子库")
    pools_db = library.list_scorecard_pools()
    pools = list(dict.fromkeys(pools_db + list(all_pools().keys())))
    c1, _ = st.columns([1, 3])
    with c1:
        pool_name = st.selectbox("体检口径（股票池）", pools, index=0, key="fl_pool")
    card = library.get_latest_scorecard(pool_name)

    # 概览：注册总量 vs 本池已体检（之前只显示已体检的，看起来像"库不全"）
    n_reg = len(registry)
    n_ev = int(registry["name"].str.startswith("ev_").sum()) if not registry.empty else 0
    n_gate1 = int((registry.get("gate_status") == 1).sum()) if not registry.empty else 0
    st.caption(f"库内注册因子 **{n_reg}** 个（收益闸门通过 {n_gate1} · 事件口径 ev_ {n_ev}）· "
               f"本池（{pool_name}）已体检 **{len(card)}** 个 —— 体检每晚自动扩 60 个，"
               f"全量覆盖需要数月，属正常")

    view_all = st.toggle("📚 显示全部注册因子（含未体检）", value=False, key="fl_all")
    if view_all:
        f1, f2, f3 = st.columns([1, 1, 2])
        with f1:
            fams = ["全部"] + sorted(registry["family"].dropna().unique().tolist()) if not registry.empty else ["全部"]
            fam_sel = st.selectbox("机制族", fams, key="fl_fam")
        with f2:
            srcs = ["全部", "loopengine", "rdagent", "builtin", "tech"]
            src_sel = st.selectbox("来源引擎", srcs, key="fl_src")
        with f3:
            kw = st.text_input("搜索因子名", "", key="fl_kw")
        reg_show = registry.copy()
        if fam_sel != "全部":
            reg_show = reg_show[reg_show["family"] == fam_sel]
        if src_sel != "全部":
            reg_show = reg_show[reg_show["engine"] == src_sel]
        if kw.strip():
            reg_show = reg_show[reg_show["name"].str.contains(kw.strip(), case=False)]
        reg_show["闸门"] = reg_show["gate_status"].map({1: "收益✅", 0: "❌", 2: "事件✅"}).fillna("未测")
        disp_all = reg_show[["name", "family", "engine", "闸门", "first_seen"]].rename(
            columns={"name": "因子", "family": "机制族", "engine": "来源", "first_seen": "入库时间"})
        st.caption(f"命中 {len(disp_all)} 个" + ("（仅显示前 2000 个，用筛选/搜索收敛）" if len(disp_all) > 2000 else ""))
        st.dataframe(disp_all.head(2000), width='stretch', height=380, hide_index=True)
        st.markdown("---")

    fac_live = experience.factor_leaderboard()
    packs = library.list_strategies()
    usage = {}
    for pname, pk in packs.items():
        for f in pk.get("factors", []):
            usage.setdefault(f["name"], []).append(pname)

    if card.empty:
        st.info(f"暂无「{pool_name}」的因子体检数据。到 🪄选股组合 页点「开始/刷新体检」生成。")
    else:
        show = card.copy()
        if not fac_live.empty:
            live_map = fac_live.set_index("因子")["20日胜率(近似)"].to_dict()
            show["实战胜率"] = show["因子"].map(lambda n: live_map.get(n))
        else:
            show["实战胜率"] = None
        show["用于策略"] = show["因子"].map(lambda n: "、".join(usage.get(n, [])) or "—")
        _src_map = {"evolved": "进化", "builtin": "内置", "tech": "技术指标", "loopengine": "演化引擎"}
        show["来源"] = show["来源"].map(lambda k: _src_map.get(k, k))
        show["类别"] = show.apply(
            lambda r: sig.NAME2CAT.get(r["因子"], "RD-Agent进化" if r["来源"] == "进化"
                                       else ("经典量价" if r["来源"] == "内置" else "摆动指标")), axis=1)
        reg_gate = registry.set_index("name")["gate_status"].to_dict() if "gate_status" in registry.columns else {}
        show["硬闸门"] = show["因子"].map(lambda n: {1: "✅", 0: "❌"}.get(reg_gate.get(n), "未测"))
        disp = show[["因子", "类别", "来源", "IC均值", "ICIR", "IC胜率", "Top组胜率", "硬闸门", "实战胜率", "建议方向", "用于策略"]].copy()
        for c in ["IC均值", "ICIR"]:
            disp[c] = disp[c].map(lambda x: f"{x:.4f}" if pd.notna(x) else "—")
        for c in ["IC胜率", "Top组胜率", "实战胜率"]:
            disp[c] = disp[c].map(lambda x: f"{x:.0%}" if pd.notna(x) else "—")
        event = st.dataframe(disp, width='stretch', height=320, hide_index=True,
                             on_select="rerun", selection_mode="single-row", key="fl_table")
        # ---- P1：硬闸门 ----
        g1, g2 = st.columns([1.6, 4.4])
        with g1:
            if st.button("🛡 运行11项硬闸门（5日换仓口径）", key="fl_gates"):
                import gaterun

                with st.spinner("逐因子评估硬闸门…"):
                    res = gaterun.run_gates_for_pool(pool_name, only_pending=True)
                st.success(f"评估 {res['evaluated']} · 通过 {res['passed']} · FSA冻结 {res['frozen']}")
                st.rerun()
        with g2:
            ts = library.tested_stats()
            st.caption(f"哈希检查点：已测 {ts['tested']} · 通过 {ts['passed']} · "
                       f"通过率 {ts['passed']/max(ts['tested'],1):.1%}（对标文章 0.41%）")
        sel_rows = event.selection.rows if event and event.selection else []
        if sel_rows:
            pick = show.iloc[sel_rows[0]]["因子"]
            _render_factor_detail(pick, pool_name, show.iloc[sel_rows[0]], reg_map)
        else:
            st.caption("👆 点击表格中的因子行查看详情与回测")

    # ================================ ①.5 机制族覆盖 / FSA / 失败模式 ================================
    with st.expander("🧬 机制族覆盖 · FSA 反同质化 · 失败模式库（P2）"):
        import structure

        cov = structure.family_coverage(registry)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**机制族覆盖**（0 = 待开垦方向，LLM引导应优先补）")
            cov_df = pd.DataFrame({"机制族": list(cov.keys()), "因子数": list(cov.values())})
            st.bar_chart(cov_df.set_index("机制族"))
        with c2:
            fsa = library.fsa_recompute()
            frozen = fsa[fsa["frozen"] == 1] if not fsa.empty else fsa
            st.markdown("**FSA 冻结名单**（骨架占比>15% 或同构变体>3）")
            if frozen is not None and not frozen.empty:
                st.dataframe(frozen[["skeleton", "count"]], width='stretch', height=180, hide_index=True)
            else:
                st.caption("暂无冻结骨架")
        st.markdown("**高频失败骨架 TOP10**（生成阶段自动排除）")
        fs = library.failure_stats(10)
        st.dataframe(fs, width='stretch', hide_index=True) if not fs.empty else st.caption("暂无记录")

    # ================================ ② 策略库 ================================
    st.markdown("## ② 策略库")
    if not packs:
        st.info("还没有策略包。到 🪄选股组合 完成「样本外验证」后保存当前组合为策略包。")
        return
    pack_lb = experience.pack_leaderboard()
    live_map = {}
    if not pack_lb.empty:
        for _, r in pack_lb.iterrows():
            live_map[r["策略包"]] = r.get("20日胜率")

    rows = []
    for name, pk in packs.items():
        live = live_map.get(name)
        rows.append({
            "策略包": name, "股票池": pk["pool_name"], "Top-N": pk["top_n"],
            "加权方法": pk.get("method"), "因子数": len(pk.get("factors", [])),
            "过滤器": "、".join(pk.get("filters", [])) or "—",
            "回测OOS胜率": pk.get("oos_winrate") or "未验证",
            "实战胜率(20日)": f"{live:.0%}" if pd.notna(live) else "—",
            "更新": pk.get("updated"),
        })
    dfp = pd.DataFrame(rows)
    event2 = st.dataframe(dfp, width='stretch', hide_index=True,
                          on_select="rerun", selection_mode="single-row", key="st_table")
    sel2 = event2.selection.rows if event2 and event2.selection else []
    if sel2:
        name = dfp.iloc[sel2[0]]["策略包"]
        _render_strategy_detail(name, packs[name], reg_map)
        if st.button("🗑 删除该策略包", key="fl_del"):
            library.delete_strategy(name)
            st.warning(f"已删除「{name}」")
            st.rerun()
    else:
        st.caption("👆 点击表格中的策略行查看详情、运行回测或生成实盘名单")

    st.caption("策略包用法：到 ⏰定时任务 的「板块/股票池扫描」卡片里选择对应策略包，每个交易日自动出名单并计入经验库。")


render()
