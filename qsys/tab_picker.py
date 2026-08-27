"""🪄 选股神奇组合 tab：胜率体检 → 组合构建 → 样本外验证 → 名单应用/策略包。"""

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import factor_eval as fe
import experience
import library
import signals as sig
from common import (DATA_DIR, GROUPS_FILE, WATCHLIST_FILE, all_pools, get_evolved_factors,
                    get_last_trade_day, load_groups, load_json, load_watchlist, save_json,
                    trade_day_offset)

PACKS_FILE = DATA_DIR / "packs.json"
WEIGHT_METHODS = ["等权", "ICIR加权", "胜率加权", "均值方差"]


def _factor_universe():
    """全部候选因子：内置经典量价 + 技术指标 + RD-Agent进化 + LoopEngine演化引擎。"""
    facs = [{"name": n, "kind": "builtin", "code": None} for n in sig.BUILTIN_FACTORS]
    facs += [{"name": n, "kind": "tech", "code": None} for n in sig.TECH_INDICATORS]
    facs += [{"name": n, "kind": "tech", "code": None} for n in sig.CATALOG_NAMES]
    for f in get_evolved_factors(only_accepted=False):
        facs.append({"name": f["name"], "kind": "evolved", "code": f["code"]})
    # LoopEngine 因子（注册表 engine='loopengine'，选股可用）
    try:
        import library

        reg = library.get_factor_registry()
        for _, r in reg[reg["engine"] == "loopengine"].iterrows():
            facs.append({"name": r["name"], "kind": "loopengine", "code": r["code"]})
    except Exception:
        pass
    return facs


def render():
    st.subheader("🪄 选股工作台")
    st.caption("💡 只想知道**今天买什么、为什么、靠不靠谱**？去左侧 **🎯 今日选股**。本页是调策略的专业工作台。")
    st.caption(f"胜率驱动的因子组合：体检 → 去冗余 → 加权 → 样本外验证 → 固化执行 · 数据截至 **{get_last_trade_day()}**")

    pools = all_pools()
    c0, _ = st.columns([1, 2])
    with c0:
        pool_name = st.selectbox("股票池/板块（全 tab 生效）", list(pools.keys()), index=0)
    codes = pools.get(pool_name) or []
    if len(codes) < 30:
        st.warning("股票池太小（<30 只），截面统计没有意义，请换大一点的池子。")
        return
    end = get_last_trade_day()
    facs = _factor_universe()

    # ---- 体检范围收敛：全宇宙 8000+ 因子全评不现实，按来源圈定范围 ----
    SCOPE_KINDS = {"内置经典": ["builtin"], "技术指标": ["tech"], "RD-Agent进化": ["evolved"]}
    scope = st.multiselect(
        "参与体检的因子范围", list(SCOPE_KINDS) + ["LoopEngine精选(族配额Top50)"],
        default=["内置经典", "技术指标", "RD-Agent进化"],
        help="LoopEngine 已自动生成数千因子，全量体检需按个执行代码、耗时以天计；"
             "精选模式按机制族配额取每族 |ICIR| 最高的若干（共约 50 个）——"
             "全局 Top50 会被同质化的波动族克隆占满，族配额保证候选池多样性")
    kinds = sum((SCOPE_KINDS[s] for s in scope if s in SCOPE_KINDS), [])
    eval_facs = [f for f in facs if f["kind"] in kinds]
    if "LoopEngine精选(族配额Top50)" in scope:
        try:
            sc = library.get_latest_scorecard(pool_name)
            reg = library.get_factor_registry()
            fam_map = dict(zip(reg["name"], reg["family"].fillna("其他")))
            if sc is not None and not sc.empty:
                # get_latest_scorecard 返回中列已汉化（因子/ICIR）
                sc = sc.assign(_abs=pd.to_numeric(sc["ICIR"], errors="coerce").abs(),
                               _fam=sc["因子"].map(lambda n: fam_map.get(n, "其他")))
                sc = sc.dropna(subset=["_abs"])
                n_fams = max(1, sc["_fam"].nunique())
                k = max(3, -(-50 // n_fams))  # 每族配额，共约 50 个
                top_names = (sc.sort_values("_abs", ascending=False)
                               .groupby("_fam").head(k)["因子"].tolist())
                rank = {n: i for i, n in enumerate(top_names)}
                le = [f for f in facs if f["kind"] == "loopengine" and f["name"] in rank]
                le.sort(key=lambda f: rank[f["name"]])
                eval_facs += le
        except Exception:
            pass

    # ================= 🏆 策略包速用（加载高胜率包直接出名单） =================
    packs = library.list_strategies()
    if packs:
        with st.container(border=True):
            def _oos_key(item):
                v = str(item[1].get("oos_winrate") or "")
                try:
                    return float(v.replace("%", "").replace("夏普", "")) or 0.0
                except ValueError:
                    return 0.0

            sorted_packs = sorted(packs.items(), key=_oos_key, reverse=True)
            labels = [f"{n}（OOS {pk.get('oos_winrate') or '未验证'}）" for n, pk in sorted_packs]
            pc1, pc2, pc3 = st.columns([2.6, 1, 1])
            with pc1:
                sel_label = st.selectbox("🏆 策略包速用（按回测胜率排序）", labels, key="pe_pack_sel")
            with pc2:
                st.markdown("<div style='height:1.65rem'></div>", unsafe_allow_html=True)
                if st.button("📥 加载", key="pe_pack_load", type="primary"):
                    _name = sel_label.split("（")[0]
                    st.session_state["loaded_pack"] = {"name": _name, **packs[_name]}
                    st.session_state["pe_combo_key"] = None  # 强制重算名单
            with pc3:
                st.markdown("<div style='height:1.65rem'></div>", unsafe_allow_html=True)
                if st.session_state.get("loaded_pack") and st.button("✖ 取消加载", key="pe_pack_clear"):
                    st.session_state.pop("loaded_pack", None)
                    st.session_state["pe_combo_key"] = None
            lp0 = st.session_state.get("loaded_pack")
            if lp0:
                st.success(f"已加载「{lp0['name']}」：{len(lp0['factors'])} 因子 · 池 {lp0['pool_name']} · "
                           f"Top-{lp0['top_n']} · {lp0.get('method')} · OOS {lp0.get('oos_winrate') or '未验证'}（名单按包配置计算）")

    # ================= ① 因子体检 =================
    st.markdown("### ① 因子体检（每个因子的胜率）")
    st.caption(f"评估窗口约 3 年 · 主指标为 20 日 RankIC · 首次评估每因子需执行一次（之后走缓存）· "
               f"当前范围 **{len(eval_facs)}** 个因子")
    if not eval_facs:
        st.warning("当前范围内没有因子——至少勾一类因子范围。")
        return
    pre_oos = st.checkbox("预选防未来函数（体检统计只用到 250 个交易日之前，"
                          "最近一年留给 ③ 样本外验证做裁决）", value=True)
    train_end = trade_day_offset(end, -250) if pre_oos else None
    if pre_oos:
        st.caption(f"当前体检统计窗口截止 **{train_end}**（≈1 年前）")
    if st.button("🔬 开始/刷新体检", type="primary"):
        bar = st.status("评估中…（进化因子首次需逐个执行，请耐心）", expanded=True)
        card_rows = []
        for fac in eval_facs:
            bar.write(f"评估 `{fac['name']}`（{'进化' if fac['kind']=='evolved' else '内置'}）…")
            card = fe.build_scorecard([fac], codes, end, train_end=train_end)
            card_rows.append(card)
        st.session_state["pe_card"] = pd.concat(card_rows, ignore_index=True)
        # 体检结果 + 因子注册 → 本地库（library 层，market.db）
        library.save_scorecard(st.session_state["pe_card"], pool_name, end)
        library.sync_factor_registry(facs)
        bar.update(label="体检完成", state="complete")

    card = st.session_state.get("pe_card")
    if card is None:
        # 会话内没有体检结果时,先加载库内最近一批(避免每次进页面都要重跑)
        persisted = library.get_latest_scorecard(pool_name)
        if persisted is not None and not persisted.empty:
            card = persisted
            st.session_state["pe_card"] = card
            st.caption(f"已载入库内最新一批体检（评估日 {persisted['eval_date'].iloc[0]}），点上方按钮可重跑")
    if card is None:
        st.info("点「开始/刷新体检」生成因子体检表。")
        return
    show = card.copy()
    for c in ["IC均值", "ICIR"]:
        if c in show:
            show[c] = show[c].map(lambda x: f"{x:.4f}" if pd.notna(x) else "—")
    for c in [c for c in show.columns if "胜率" in c]:  # IC胜率/Top组胜率/1日~120日胜率
        show[c] = show[c].map(lambda x: f"{x:.1%}" if pd.notna(x) else "—")
    st.dataframe(show, width='stretch', height=280)

    valid = card.dropna(subset=["ICIR"])
    if valid.empty:
        st.error("没有评估成功的因子。")
        return

    # 衰减曲线（选一个因子看）
    pick = st.selectbox("查看衰减曲线（判断短炒/长持属性）", valid["因子"].tolist())
    if pick:
        fac = next(f for f in facs if f["name"] == pick)
        with st.spinner("计算衰减…"):
            dec = fe.decay_curve(fac, codes, end)
        st.bar_chart(pd.Series(dec, name="平均RankIC").rename_axis("forward交易日"))

    # ================= ② 组合构建 =================
    st.markdown("### ② 组合构建")
    corr = fe.ic_corr_matrix([f for f in facs if f["name"] in set(valid["因子"])], codes, end)
    try:
        _fam_map = dict(zip(library.get_factor_registry()["name"],
                            library.get_factor_registry()["family"].fillna("其他")))
    except Exception:
        _fam_map = None
    kept, dropped = fe.dedup_factors(corr, valid, family_map=_fam_map)
    if dropped:
        st.info("🧹 去冗余建议：已剔除 " + "；".join(f"`{k}`（{v}）" for k, v in dropped.items()))
    c1, c2 = st.columns([2, 3])
    with c1:
        method = st.radio("加权方法", WEIGHT_METHODS, index=1, horizontal=True)
        hold_h = st.radio("决策持有期（选股锚定的预测窗口）", ["1日", "5日", "20日"],
                          index=0, horizontal=True,
                          help="默认 1 日：胜率加权与样本外验证都按该窗口的远期收益评估")
        # 推荐回填守卫：pe_chosen 可能含已被去冗余剔除的因子，创建控件前过滤（允许空选择）
        # 贪心推荐的回填走 _pe_chosen_next 中转：widget 实例化后禁止直写其 key，
        # 必须在下一轮 rerun、控件创建之前落位（否则 StreamlitAPIException）
        if "_pe_chosen_next" in st.session_state:
            st.session_state["pe_chosen"] = st.session_state.pop("_pe_chosen_next")
        if "pe_chosen" in st.session_state:
            st.session_state["pe_chosen"] = [n for n in st.session_state["pe_chosen"] if n in kept]
        chosen = st.multiselect("参与组合的因子（已按去冗余过滤，可再调）", kept,
                                default=kept[:6], key="pe_chosen")
        filters = st.multiselect("策略过滤器", list(sig.STRATEGY_FILTERS.keys()), default=["tradable"],
                                 format_func=lambda k: sig.STRATEGY_FILTERS[k])
        top_n = st.slider("Top-N", 5, 50, 10)
        ind_cap = st.checkbox("行业分散（每行业≤2只，防单一赛道扎堆）", value=True)
        resonance = st.checkbox("多周期共振（1日+5日 双口径交集，信号更少更稳）", value=False)
    lp = st.session_state.get("loaded_pack")
    if lp:  # 加载策略包：池/因子/权重/过滤器/TopN 全部按包配置
        pool_name = lp["pool_name"] if lp["pool_name"] in pools else pool_name
        codes = pools.get(pool_name) or codes
        chosen = [f["name"] for f in lp["factors"]]
        method = lp.get("method", method)
        filters = lp.get("filters", filters)
        top_n = lp["top_n"]
        weights = {f["name"]: (f["weight"], f["direction"]) for f in lp["factors"]}
    else:
        win_col = f"{hold_h}胜率"
        weights = fe.compute_weights(valid, method, chosen, win_col=win_col) if chosen else {}
    with c2:
        if weights:
            wt = pd.DataFrame([{"因子": n, "权重": f"{w:.1%}", "方向": "正向" if d > 0 else "负向"}
                               for n, (w, d) in weights.items()])
            st.dataframe(wt, width='stretch', height=200)

    # ---- 🤖 一键贪心组合推荐：以 OOS 扣费胜率为目标做前向选择 ----
    if kept and not lp:
        if st.button("🤖 一键推荐组合（OOS贪心）",
                     help="在去冗余后的因子里做前向选择：每轮加入使样本外扣费胜率提升最大的因子，"
                          "直到无提升或满 8 个。候选按 |ICIR| 截前 12，IC 序列预计算后单轮亚秒级。"):
            cands = kept[:12]
            with st.spinner(f"贪心搜索中（{len(cands)} 候选 × walk-forward 滚动验证）…"):
                fvals = {}
                for n in cands:
                    fac = next(f for f in facs if f["name"] == n)
                    fvals[n] = fe.get_factor_values(fac, codes, end)
                panel = sig.get_panel_cached(codes, end, 800)
                hdays = fe.WIN_HORIZONS.get(hold_h, fe.MAIN_FWD)
                reco = fe.greedy_combo(fvals, panel, method, top_n, cands,
                                       fwd_days=hdays, step=max(1, min(fe.STEP_DAYS, hdays)))
            st.session_state["pe_reco"] = {"selected": reco["selected"], "history": reco["history"]}
            if reco["selected"]:
                st.session_state["_pe_chosen_next"] = reco["selected"]
                st.rerun()
            else:
                st.warning("贪心搜索没有找到可用的因子组合（候选因子 OOS 数据不足）。")
        reco = st.session_state.get("pe_reco")
        if reco and reco.get("selected"):
            st.caption("🤖 推荐路径（每步加入使 OOS 扣费胜率提升最大的因子）："
                       + " → ".join(f"`{n}`" for n in reco["selected"]))
            h = reco.get("history")
            if h is not None and not h.empty:
                st.dataframe(h, width='stretch', hide_index=True, height=200)

    # 相关矩阵：全宽 + 斜排标签 + 高度随因子数自适应（防挤压截断）
    if not corr.empty and len(corr) > 1:
        n_fac = len(corr)
        fig = go.Figure(go.Heatmap(z=corr.values, x=corr.columns, y=corr.index,
                                   zmin=-1, zmax=1, colorscale="RdBu_r",
                                   colorbar=dict(title="ρ")))
        fig.update_layout(
            height=max(420, 26 * n_fac + 220),
            margin=dict(l=10, r=10, t=30, b=10),
            title="IC 相关矩阵（悬停看具体数值）",
            xaxis=dict(tickangle=-45, tickfont=dict(size=10), side="bottom"),
            yaxis=dict(tickfont=dict(size=10), autorange="reversed"),
        )
        st.plotly_chart(fig, width='stretch')

    # ---- 构建即出名单：输入（方法/因子/过滤器/池/日期）任一变化自动重算 ----
    final = None
    if weights:
        combo_key = json.dumps({"m": method, "c": chosen, "f": filters, "n": top_n,
                                "p": pool_name, "e": end, "h": hold_h, "ic": ind_cap,
                                "res": resonance}, sort_keys=True)
        if st.session_state.get("pe_combo_key") != combo_key:
            with st.spinner("组合已构建，正在生成今日名单…"):
                try:
                    fvals = {n: fe.get_factor_values(next(f for f in facs if f["name"] == n), codes, end)
                             for n in chosen}
                    score = sig.composite_score(fvals, weights)
                    panel_now = sig.get_panel_cached(codes, end, 800)
                    survived = sig.apply_filters(score.index.tolist(), panel_now, filters)
                    if resonance:
                        # 双口径共振：当前持有期 + 另一短线口径各打一次分，取交集
                        other_h = "5日" if hold_h == "1日" else "1日"
                        w2 = fe.compute_weights(valid, method, chosen, win_col=f"{other_h}胜率")
                        sel = sig.resonance_select(fvals, weights, w2, top_n, k=top_n * 3)
                        sel = sel[sel.index.isin(survived)]
                    else:
                        sel = score[score.index.isin(survived)]
                    if ind_cap:
                        sel = sig.industry_cap_select(sel, cap=2)
                    st.session_state["pe_final"] = sel.head(top_n)
                    # 经验库落库（不管对错，到期自动回填战果）
                    fcfg = [{"name": n, "kind": next(f for f in facs if f["name"] == n)["kind"],
                             "weight": float(w), "direction": int(d)} for n, (w, d) in weights.items()]
                    experience.save_pick(source="manual_picker", pool_name=pool_name, top_n=top_n,
                                         method=method, filters=filters, factors=fcfg,
                                         final_scores=st.session_state["pe_final"], trade_date=end)
                except Exception as e:
                    st.session_state["pe_final"] = None
                    st.error(f"名单生成失败: {e}")
                st.session_state["pe_combo_key"] = combo_key
        final = st.session_state.get("pe_final")
        if final is not None and not final.empty:
            st.markdown(f"**📋 今日 Top-{len(final)}（按当前组合自动计算，{end}）**")
            show_final = pd.DataFrame({"综合分": final.round(3)})
            try:
                import datasource as _ds
                import experience as _exp

                snaps, snap_ts = _ds.get_latest_snapshots(list(final.index))
                smap = {s["code"]: s for s in snaps}
                show_final["最新价"] = [smap.get(c, {}).get("price") for c in show_final.index]
                show_final["较昨收%"] = [
                    round((smap[c]["price"] / smap[c]["prev_close"] - 1) * 100, 2)
                    if smap.get(c) and smap[c].get("price") and smap[c].get("prev_close") else None
                    for c in show_final.index
                ]
                # 交易计划：参考买入价(快照最新价,缺省用昨收) + 止盈/止损价
                rules = _exp.DEFAULT_RULES
                ref = [smap.get(c, {}).get("price") or smap.get(c, {}).get("prev_close")
                       for c in show_final.index]
                show_final["参考买入价"] = [round(p, 2) if p else None for p in ref]
                show_final["止盈价"] = [round(p * (1 + rules["take_profit"]), 2) if p else None for p in ref]
                show_final["止损价"] = [round(p * (1 + rules["stop_loss"]), 2) if p else None for p in ref]
                plan = _exp.trade_plan(None, end)
                st.caption(f"📝 交易计划：{plan['买入时间']}按开盘价买入（参考价=最近可得价）；{plan['规则']}；"
                           f"最迟 {plan['最迟平仓']}平仓。名单方向均为**看涨**（持有窗口 ≤{rules['hold_days']} 交易日）；"
                           f"实时快照 {snap_ts or '暂无'}")
            except Exception:
                pass
            st.dataframe(show_final, width='stretch')
        elif final is not None:
            st.warning("当前过滤器下没有股票通过——放宽过滤器或换大一点的池子试试。")

    # ================= ③ 样本外验证 =================
    st.markdown("### ③ 样本外验证（walk-forward，决策只看这里）")
    st.caption(f"估计窗 {fe.EST_WINDOW} 交易日 → 应用窗 {fe.STEP_DAYS} 交易日滚动 · IC 观测端点回退防未来函数 · "
               "历史统计基于当前成分股（含轻微幸存者偏差，解读时留有余量）")
    if st.button("🧪 运行样本外验证"):
        if len(chosen) < 2:
            st.warning("至少选 2 个因子")
        else:
            with st.spinner("滚动回测中…"):
                fvals = {}
                for n in chosen:
                    fac = next(f for f in facs if f["name"] == n)
                    fvals[n] = fe.get_factor_values(fac, codes, end)
                panel = sig.get_panel_cached(codes, end, 800)
                hdays = fe.WIN_HORIZONS.get(hold_h, fe.MAIN_FWD)
                wf = fe.walk_forward(fvals, panel, method, top_n,
                                     fwd_days=hdays, step=max(1, min(fe.STEP_DAYS, hdays)))
                st.session_state["pe_wf"] = wf
                # IS 对照：② 的固定权重（不滚动重估）在样本内区间的表现
                if weights:
                    upto = train_end if (pre_oos and train_end) else None
                    st.session_state["pe_is"] = fe.static_backtest(
                        fvals, panel, weights, top_n, upto=upto,
                        fwd_days=hdays, step=max(1, min(fe.STEP_DAYS, hdays)))
    wf = st.session_state.get("pe_wf")
    if wf is not None and not wf.empty:
        oos_win = (wf["优化组合超额"] > 0).mean()
        eq_win = (wf["等权组合超额"] > 0).mean() if "等权组合超额" in wf else float("nan")
        st.markdown(
            f"**OOS 胜率：优化组合 {oos_win:.0%}（平均超额 {wf['优化组合超额'].mean():.2%}/期）"
            f" vs 等权 {eq_win:.0%}** · 共 {len(wf)} 个应用点")
        if "优化组合扣费超额" in wf:
            net = wf["优化组合扣费超额"]
            net_win = (net > 0).mean()
            tvr = wf["优化组合换手率"].mean() if "优化组合换手率" in wf else float("nan")
            st.markdown(f"**扣费后（双边 0.25%）：胜率 {net_win:.0%} · 平均净超额 {net.mean():+.2%}/期 · "
                        f"平均换手 {tvr:.0%}/期** —— 决策以扣费后为准")
        cum_cols = [c for c in ["优化组合超额", "优化组合扣费超额", "等权组合超额"] if c in wf]
        cum = wf.set_index("调仓日")[cum_cols].add(1).cumprod()
        st.line_chart(cum)
        with st.expander("逐点明细"):
            st.dataframe(wf, width='stretch')
        if oos_win < 0.55:
            st.warning("OOS 胜率 < 55%：该组合方向有效性不足，建议换因子/换池/降 Top-N，不要固化。")
        elif "优化组合扣费超额" in wf and net.mean() <= 0:
            st.warning("毛胜率合格但扣费后净超额 ≤ 0：换手吃掉利润——考虑延长持有期或降 Top-N 波动。")

        # ---- IS/OOS 双轨对比（设计文档：IS 仅参考，OOS 才用于决策；差距大 = 过拟合警报） ----
        is_df = st.session_state.get("pe_is")
        if is_df is not None and not is_df.empty and "组合扣费超额" in is_df:
            oos_col = "优化组合扣费超额" if "优化组合扣费超额" in wf else "优化组合超额"
            is_win = (is_df["组合扣费超额"] > 0).mean()
            oos_win_net = (wf[oos_col] > 0).mean()
            st.markdown(
                f"**IS/OOS 双轨**：样本内（② 固定权重）扣费胜率 **{is_win:.0%}**（{len(is_df)} 点）"
                f" vs 样本外（滚动重估）**{oos_win_net:.0%}**（{len(wf)} 点）"
                f" · 平均净超额 IS {is_df['组合扣费超额'].mean():+.2%} / OOS {wf[oos_col].mean():+.2%}")
            cum_is = is_df.set_index("调仓日")["组合扣费超额"].add(1).cumprod().rename("样本内(固定权重)")
            cum_oos = wf.set_index("调仓日")[oos_col].add(1).cumprod().rename("样本外(walk-forward)")
            st.line_chart(pd.concat([cum_is, cum_oos], axis=1), height=260)
            if is_win - oos_win_net > 0.10:
                st.warning(f"⚠️ 过拟合警报：IS 胜率比 OOS 高 {is_win - oos_win_net:.0%}（>10pp）——"
                           "权重/因子选择过度拟合了样本内。建议：减因子数量、换低相关因子、"
                           "拉长持有期，或退守等权。")
            st.caption("多重检验提示：组合是试出来的（含 🤖 贪心推荐），其 OOS 胜率仍偏乐观——"
                       "固化策略包后以经验库的实战命中做最终裁决。")

    # ================= ④ 名单应用 & 策略包 =================
    st.markdown("### ④ 名单应用 & 策略包")
    if final is not None and not final.empty:
        b1, b2 = st.columns(2)
        with b1:
            if st.button("➕ 名单全部加入自选股"):
                wl = load_watchlist()
                add = [c for c in final.index if c not in wl]
                save_json(WATCHLIST_FILE, wl + add)
                st.success(f"已加入 {len(add)} 只")
        with b2:
            gname = st.text_input("存为板块组名", value=f"神奇组合_{pd.Timestamp.now():%m%d}")
            if st.button("💾 存为板块组"):
                groups = load_groups()
                groups[gname] = list(final.index)
                save_json(GROUPS_FILE, groups)
                st.success(f"已存板块组「{gname}」，定时任务的板块扫描可选它")
    else:
        st.caption("完成②后此处可对今日名单操作。")

    st.markdown("**策略包**（固化组合配置，存本地库 strategies 表，定时任务「板块扫描」可按包每日出名单）")
    packs = library.list_strategies()
    pname = st.text_input("策略包名", value="我的组合_v1")
    if st.button("💼 保存当前组合为策略包"):
        if not weights:
            st.warning("先完成②的组合构建")
        else:
            oos = None
            if wf is not None and not wf.empty:
                oos = f"{(wf['优化组合超额'] > 0).mean():.0%}"
            is_wr = None
            is_df = st.session_state.get("pe_is")
            if is_df is not None and not is_df.empty and "组合扣费超额" in is_df:
                is_wr = f"{(is_df['组合扣费超额'] > 0).mean():.0%}"
            library.save_strategy(pname, {
                "pool_name": pool_name, "top_n": top_n, "method": method, "filters": filters,
                "horizon": hold_h, "is_winrate": is_wr,
                "factors": [{"name": n, "kind": next(f for f in facs if f['name'] == n)["kind"],
                             "weight": w, "direction": d} for n, (w, d) in weights.items()],
                "oos_winrate": oos, "updated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
            })
            st.success(f"策略包「{pname}」已存入库（OOS胜率 {oos or '未验证'}），可在 ⏰定时任务 的板块扫描中选用")
            st.rerun()
    if packs:
        st.dataframe(pd.DataFrame([
            {"策略包": k, "股票池": v["pool_name"], "Top-N": v["top_n"], "方法": v.get("method"),
             "因子数": len(v["factors"]), "OOS胜率": v.get("oos_winrate") or "未验证", "更新": v.get("updated")}
            for k, v in packs.items()]), width='stretch')
