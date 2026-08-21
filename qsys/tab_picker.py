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
                    get_last_trade_day, load_groups, load_json, load_watchlist, save_json)

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
    st.subheader("🪄 选股神奇组合")
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
    st.caption("评估窗口约 3 年 · 主指标为 20 日 RankIC · 首次评估每因子需执行一次（之后走缓存）")
    if st.button("🔬 开始/刷新体检", type="primary"):
        bar = st.status("评估中…（进化因子首次需逐个执行，请耐心）", expanded=True)
        card_rows = []
        for fac in facs:
            bar.write(f"评估 `{fac['name']}`（{'进化' if fac['kind']=='evolved' else '内置'}）…")
            card = fe.build_scorecard([fac], codes, end)
            card_rows.append(card)
        st.session_state["pe_card"] = pd.concat(card_rows, ignore_index=True)
        # 体检结果 + 因子注册 → 本地库（library 层，market.db）
        library.save_scorecard(st.session_state["pe_card"], pool_name, end)
        library.sync_factor_registry(facs)
        bar.update(label="体检完成", state="complete")

    card = st.session_state.get("pe_card")
    if card is None:
        st.info("点「开始/刷新体检」生成因子体检表。")
        return
    show = card.copy()
    for c in ["IC均值", "ICIR"]:
        show[c] = show[c].map(lambda x: f"{x:.4f}" if pd.notna(x) else "—")
    for c in ["IC胜率", "Top组胜率"]:
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
    kept, dropped = fe.dedup_factors(corr, valid)
    if dropped:
        st.info("🧹 去冗余建议：已剔除 " + "；".join(f"`{k}`（{v}）" for k, v in dropped.items()))
    c1, c2 = st.columns([2, 3])
    with c1:
        method = st.radio("加权方法", WEIGHT_METHODS, index=1, horizontal=True)
        chosen = st.multiselect("参与组合的因子（已按去冗余过滤，可再调）", kept, default=kept[:6])
        filters = st.multiselect("策略过滤器", list(sig.STRATEGY_FILTERS.keys()), default=["tradable"],
                                 format_func=lambda k: sig.STRATEGY_FILTERS[k])
        top_n = st.slider("Top-N", 5, 50, 20)
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
        weights = fe.compute_weights(valid, method, chosen) if chosen else {}
    with c2:
        if weights:
            wt = pd.DataFrame([{"因子": n, "权重": f"{w:.1%}", "方向": "正向" if d > 0 else "负向"}
                               for n, (w, d) in weights.items()])
            st.dataframe(wt, width='stretch', height=200)

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
                                "p": pool_name, "e": end}, sort_keys=True)
        if st.session_state.get("pe_combo_key") != combo_key:
            with st.spinner("组合已构建，正在生成今日名单…"):
                try:
                    fvals = {n: fe.get_factor_values(next(f for f in facs if f["name"] == n), codes, end)
                             for n in chosen}
                    score = sig.composite_score(fvals, weights)
                    panel_now = sig.get_panel_cached(codes, end, 800)
                    survived = sig.apply_filters(score.index.tolist(), panel_now, filters)
                    st.session_state["pe_final"] = score[score.index.isin(survived)].head(top_n)
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
            st.dataframe(pd.DataFrame({"综合分": final.round(3)}), width='stretch')
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
                wf = fe.walk_forward(fvals, panel, method, top_n)
                st.session_state["pe_wf"] = wf
    wf = st.session_state.get("pe_wf")
    if wf is not None and not wf.empty:
        oos_win = (wf["优化组合超额"] > 0).mean()
        eq_win = (wf["等权组合超额"] > 0).mean() if "等权组合超额" in wf else float("nan")
        st.markdown(
            f"**OOS 胜率：优化组合 {oos_win:.0%}（平均超额 {wf['优化组合超额'].mean():.2%}/期）"
            f" vs 等权 {eq_win:.0%}** · 共 {len(wf)} 个应用点")
        cum = wf.set_index("调仓日")[[c for c in ["优化组合超额", "等权组合超额"] if c in wf]].add(1).cumprod()
        st.line_chart(cum)
        with st.expander("逐点明细"):
            st.dataframe(wf, width='stretch')
        if oos_win < 0.55:
            st.warning("OOS 胜率 < 55%：该组合方向有效性不足，建议换因子/换池/降 Top-N，不要固化。")

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
            library.save_strategy(pname, {
                "pool_name": pool_name, "top_n": top_n, "method": method, "filters": filters,
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
