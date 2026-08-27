"""🧩 选股组合：因子组合自动组建 + 策略组合多包投票 + 对比台 + 组合包跟踪。

与 🪄选股工作台 的分工：工作台是"手动挡"调参研究；本页是"自动挡"——
  ① 一键组建因子组合（族配额候选 → 去冗余 → 贪心推荐 → IS/OOS 双轨验证 → 存策略包）
  ② 策略组合：多个策略包投票合成名单（票数=信心），组合 K 线回测，
     落库后由经验库自动结算实战战果（对错的最终裁决）
"""

import json

import pandas as pd
import streamlit as st

import datasource
import experience
import factor_eval as fe
import library
import scheduler
import signals as sig
from common import WATCHLIST_FILE, all_pools, get_evolved_factors, get_last_trade_day, load_json, save_json

st.title("🧩 选股组合")
st.caption("自动挡建站：① 一键组建因子组合 → ② 多包投票合成名单 → ③ 对比台汰弱留强 · "
           "名单落库后经验库自动结算实战（1/5/20/60/120 日）")

end = get_last_trade_day()
pools = all_pools()
packs = library.list_strategies()


def _pct(x):
    try:
        return float(str(x).replace("%", "").strip()) / 100
    except (TypeError, ValueError):
        return None


def _name_map(codes: list[str]) -> dict:
    try:
        with datasource._qconn() as conn:
            rows = conn.execute(
                f"SELECT code, name, MAX(ts) FROM quote_snapshots"
                f" WHERE code IN ({','.join('?' * len(codes))}) GROUP BY code",
                list(codes)).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


# ---------------------------------------------------------------- ① 自动组建因子组合
st.header("① 自动组建因子组合")
st.caption("只选三样东西，剩下交给 OOS 贪心搜索（族配额取候选 → 去冗余 → 逐个试加，"
           "样本外扣费胜率不提升就停）")

c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
with c1:
    pool_name = st.selectbox("股票池", list(pools.keys()), key="pc_pool")
with c2:
    hold_h = st.radio("持有期", ["1日", "5日", "20日"], index=1, horizontal=True, key="pc_hold",
                      help="默认 5 日。**1 日口径每天全换仓，双边 0.25% 成本日扣，"
                           "实测沪深300 上绝大多数组合扣费后净超额为负**——短线口径慎选")
with c3:
    top_n = st.slider("Top-N", 5, 30, 10, key="pc_topn")
with c4:
    method = st.selectbox("加权方法", ["ICIR加权", "等权", "胜率加权", "均值方差"], key="pc_method")

codes = pools.get(pool_name) or []
if len(codes) < 30:
    st.warning("股票池太小（<30 只），截面统计没有意义。")
    st.stop()

if st.button("🤖 自动组建因子组合", type="primary", key="pc_build"):
    sc = library.get_latest_scorecard(pool_name)
    if sc is None or sc.empty:
        st.error("本池还没有体检数据——先去 🪄选股工作台 跑一次「因子体检」，"
                 "或开启 ⏰定时任务 的 LoopEngine 滚动体检积累评分卡。")
        st.stop()
    with st.status("组建中…", expanded=True) as bar:
        bar.write("① 族配额取候选（每个机制族取族内 |ICIR| 最强，保证多样性）…")
        reg = library.get_factor_registry()
        fam_map = dict(zip(reg["name"], reg["family"].fillna("其他")))
        valid = sc.dropna(subset=["ICIR"]).copy()
        valid["_abs"] = pd.to_numeric(valid["ICIR"], errors="coerce").abs()
        valid["_fam"] = valid["因子"].map(lambda n: fam_map.get(n, "其他"))
        n_fams = max(1, valid["_fam"].nunique())
        k = max(2, -(-24 // n_fams))
        cand_names = (valid.sort_values("_abs", ascending=False)
                      .groupby("_fam").head(k)["因子"].tolist())
        evo_map = {f["name"]: f["code"] for f in get_evolved_factors(only_accepted=False)}
        le_map = {r["name"]: r["code"]
                  for _, r in reg[reg["engine"] == "loopengine"].iterrows()}
        facs = {n: fe.resolve_factor(n, evo_map=evo_map, le_map=le_map) for n in cand_names}
        facs = {n: f for n, f in facs.items() if f}
        bar.write(f"② 去冗余（候选 {len(facs)} 个，|IC 相关|>0.7 剔除）…")
        corr = fe.ic_corr_matrix(list(facs.values()), codes, end)
        kept, dropped = fe.dedup_factors(corr, valid, family_map=fam_map)
        kept = [n for n in kept if n in facs][:12]
        bar.write(f"③ 贪心搜索（{len(kept)} 候选 × walk-forward）…")
        fvals = {n: fe.get_factor_values(facs[n], codes, end) for n in kept}
        panel = sig.get_panel_cached(codes, end, 800, source="qlib_local")
        hdays = fe.WIN_HORIZONS.get(hold_h, fe.MAIN_FWD)
        reco = fe.greedy_combo(fvals, panel, method, top_n, kept,
                               fwd_days=hdays, step=max(1, min(fe.STEP_DAYS, hdays)),
                               buffer_n=(top_n if hold_h == "1日" else 0))
        out = {"selected": reco["selected"], "history": reco["history"], "wf": reco["wf"],
               "hold_h": hold_h, "top_n": top_n, "method": method, "pool": pool_name}
        if reco["selected"]:
            sel_fvals = {n: fvals[n] for n in reco["selected"]}
            weights = fe.compute_weights(valid, method, reco["selected"],
                                         win_col=f"{hold_h}胜率")
            is_bt = fe.static_backtest(sel_fvals, panel, weights, top_n, upto=None,
                                       fwd_days=hdays, step=max(1, min(fe.STEP_DAYS, hdays)))
            out["weights"] = weights
            out["is_bt"] = is_bt
            out["facs"] = {n: facs[n] for n in reco["selected"]}
            out["fvals"] = sel_fvals
            # 组建的最终目的是出股——顺手用这个组合算出今日 Top-N 名单
            score = sig.composite_score(sel_fvals, weights)
            survived = sig.apply_filters(score.index.tolist(), panel, ["tradable"])
            out["today_picks"] = sig.industry_cap_select(
                score[score.index.isin(survived)], cap=2).head(top_n)
        bar.update(label="组建完成", state="complete")
    st.session_state["pc_auto"] = out

auto = st.session_state.get("pc_auto")
if auto and auto.get("selected"):
    if not auto.get("selected"):
        st.warning("贪心搜索没有找到可用组合（候选因子样本外数据不足）。")
    sel = auto["selected"]
    st.markdown(f"**推荐因子组合（{len(sel)} 个）**："
                + " → ".join(f"`{sig.plain_factor_name(n)}`" for n in sel))
    h = auto.get("history")
    if h is not None and not h.empty:
        st.dataframe(h, width='stretch', hide_index=True, height=180)
    if len(sel) <= 1:
        st.info(f"💡 只选出 1 个因子**不是 bug**：在 {auto.get('hold_h')} 口径下，往 `"
            f"{sig.plain_factor_name(sel[0])}` 里加任何候选因子都会拉低样本外扣费胜率，"
            "贪心就停在了这里。1 日口径换手成本极高，单一最强因子常常就是最优解；"
            "**想要多因子组合，换 5日/20日 持有期**（实测本池能组出 3 因子、胜率 63%~86% 的组合）。")

    wf = auto.get("wf")
    if wf is not None and not wf.empty and "优化组合扣费超额" in wf:
        oos_win = (wf["优化组合扣费超额"] > 0).mean()
        oos_avg = wf["优化组合扣费超额"].mean()
        cum_oos = wf.set_index("调仓日")["优化组合扣费超额"].add(1).cumprod().rename("样本外(walk-forward)")
        is_bt = auto.get("is_bt")
        gap_note, gap_bad = "", False
        if is_bt is not None and not is_bt.empty:
            is_win = (is_bt["组合扣费超额"] > 0).mean()
            gap_bad = is_win - oos_win > 0.10
            gap_note = f" · IS/OOS 差距 {is_win - oos_win:.0%}" + ("（>10pp 过拟合！）" if gap_bad else "")
            cum_is = is_bt.set_index("调仓日")["组合扣费超额"].add(1).cumprod().rename("样本内(固定权重)")
            st.line_chart(pd.concat([cum_is, cum_oos], axis=1), height=260)
        else:
            st.line_chart(cum_oos.to_frame(), height=260)
        light = "🟢" if (oos_win >= 0.60 and not gap_bad) else ("🔴" if (oos_win < 0.55 or gap_bad) else "🟡")
        st.markdown(f"{light} **组合净值（扣费）**：OOS 胜率 **{oos_win:.0%}** · 平均净超额 {oos_avg:+.2%}/期{gap_note}")
        ok_save = oos_win >= 0.55 and not gap_bad
        pn = st.text_input("存为策略包名", value=f"自动组合_{pool_name}_{hold_h}", key="pc_pname")
        if st.button("💾 保存为策略包", disabled=not ok_save, key="pc_save",
                     help=None if ok_save else "OOS 胜率 <55% 或过拟合的组合不允许固化"):
            library.save_strategy(pn, {
                "pool_name": auto["pool"], "top_n": auto["top_n"], "method": auto["method"],
                "filters": ["tradable"], "horizon": auto["hold_h"],
                "factors": [{"name": n, "kind": auto["facs"][n]["kind"],
                             "weight": float(w), "direction": int(d)}
                            for n, (w, d) in auto["weights"].items()],
                "oos_winrate": f"{oos_win:.0%}",
                "is_winrate": (f"{(auto['is_bt']['组合扣费超额'] > 0).mean():.0%}"
                               if auto.get("is_bt") is not None and not auto["is_bt"].empty else None),
                "updated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")})
            st.success(f"策略包「{pn}」已入库——🎯今日选股 和定时任务都能用它")
            st.rerun()

    # ---- 组建出的今日股票名单（因子组合 → 股票，闭环的最后一步） ----
    tp = auto.get("today_picks")
    if tp is not None and not tp.empty:
        st.markdown(f"**📋 用这个组合选出的今日 Top-{len(tp)}（{end}）**")
        nmap = _name_map(list(tp.index))
        try:
            snaps, _ts = datasource.get_latest_snapshots(list(tp.index))
            smap = {s["code"]: s for s in snaps}
        except Exception:
            smap = {}
        rules = experience.DEFAULT_RULES
        ref = [smap.get(c, {}).get("price") or smap.get(c, {}).get("prev_close") for c in tp.index]

        def _reason(c):
            pos = [(n, v) for n, v in
                   sig.factor_contributions(auto["fvals"], auto["weights"], c) if v > 0][:2]
            return " · ".join(f"{sig.plain_factor_name(n)}({v:+.2f})" for n, v in pos) or "—"

        st.dataframe(pd.DataFrame({
            "代码": list(tp.index),
            "名称": [nmap.get(c, "") for c in tp.index],
            "综合分": [round(float(s), 3) for s in tp.values],
            "为什么选它": [_reason(c) for c in tp.index],
            "最新价": [smap.get(c, {}).get("price") for c in tp.index],
            "参考买入价": [round(p, 2) if p else None for p in ref],
            "止盈价": [round(p * (1 + rules["take_profit"]), 2) if p else None for p in ref],
            "止损价": [round(p * (1 + rules["stop_loss"]), 2) if p else None for p in ref],
        }), width='stretch', hide_index=True)
        plan = experience.trade_plan(None, end)
        st.caption(f"📝 {plan['买入时间']}按开盘价买入；{plan['规则']}；最迟 {plan['最迟平仓']} 平仓")
        b1, b2 = st.columns(2)
        with b1:
            if st.button("➕ 加入自选股", key="pc_auto_wl"):
                wl = load_json(WATCHLIST_FILE, [])
                add = [c for c in tp.index if c not in wl]
                save_json(WATCHLIST_FILE, wl + add)
                st.success(f"已加入 {len(add)} 只")
        with b2:
            if st.button("📥 名单落库（开始实战跟踪）", key="pc_auto_log"):
                saved = library.list_strategies()
                experience.save_pick(
                    source="auto_combo", pool_name=auto["pool"], top_n=len(tp),
                    method=auto["method"], filters=["tradable"],
                    factors=[{"name": n, "kind": auto["facs"][n]["kind"],
                              "weight": float(w), "direction": int(d)}
                             for n, (w, d) in auto["weights"].items()],
                    final_scores=tp,
                    pack_name=(st.session_state.get("pc_pname")
                               if st.session_state.get("pc_pname") in saved else None),
                    trade_date=end)
                st.success(f"已落库（{end}，{len(tp)} 只）→ 📋选股列表 / 📚经验库 可追踪实战")
elif auto is not None:
    st.warning("贪心搜索没有找到可用组合（候选因子样本外数据不足）——先扩大体检覆盖再试。")

# ---------------------------------------------------------------- ② 策略组合（多包投票）
st.header("② 策略组合（多包投票）")
st.caption("多个策略包各自出名单，**票数 = 信心**：被越多独立策略同时选中的票，踩雷概率越低")
if len(packs) < 2:
    st.info("策略包不足 2 个——先在 ① 自动组建或去 🪄选股工作台 保存几个包再回来组合。")
else:
    labels = {n: f"{n}（OOS {pk.get('oos_winrate') or '未验证'}）" for n, pk in packs.items()}
    sel_packs = st.multiselect("选择 2~4 个策略包（须同一股票池）", list(packs.keys()),
                               format_func=lambda n: labels[n], key="pc_packs")
    rule = st.radio("合成规则", ["投票(≥2包选中)", "交集(全部选中)"], horizontal=True, key="pc_rule")
    min_votes = len(sel_packs) if rule.startswith("交集") else 2

    if sel_packs:
        pools_sel = {packs[p]["pool_name"] for p in sel_packs}
        if len(pools_sel) > 1:
            st.error(f"所选包的股票池不一致：{'、'.join(pools_sel)}——请选同池的包。")
            sel_packs = []

    can_vote = len(sel_packs) >= 2
    if not can_vote:
        st.info("👆 先在上面**勾选至少 2 个策略包**，再点下面按钮生成组合名单")
    if st.button("🗳 生成策略组合名单", type="primary", key="pc_vote", disabled=not can_vote):
        pool0 = packs[sel_packs[0]]["pool_name"]
        codes0 = pools.get(pool0) or []
        with st.spinner("各包分别打分中…"):
            per_pack, votes, rank_in = {}, {}, {}
            for p in sel_packs:
                try:
                    picks, note, _w, _fs = scheduler.compute_pack_picks(
                        packs[p], codes0, end, packs[p]["top_n"])
                    per_pack[p] = list(picks.index)
                    for i, c in enumerate(picks.index):
                        votes[c] = votes.get(c, 0) + 1
                        rank_in.setdefault(c, {})[p] = i + 1
                except Exception as e:
                    st.warning(f"包「{p}」生成失败：{e}")
        merged = [c for c, v in votes.items() if v >= min_votes]
        if len(merged) < 3 and votes:
            merged = sorted(votes, key=lambda c: (-votes[c], c))[:max(3, min(8, len(votes)))]
            st.caption("严格规则下不足 3 只，已放宽为票数最高的前若干只")
        merged.sort(key=lambda c: -votes[c])
        st.session_state["pc_vote_res"] = {"merged": merged, "votes": votes, "rank_in": rank_in,
                                       "per_pack": per_pack, "pool": pool0,
                                       "rule": rule, "packs": sel_packs}

    vote = st.session_state.get("pc_vote_res")
    if vote and vote.get("merged"):
        merged, votes, rank_in = vote["merged"], vote["votes"], vote["rank_in"]
        nmap = _name_map(merged)
        try:
            snaps, snap_ts = datasource.get_latest_snapshots(merged)
            smap = {s["code"]: s for s in snaps}
        except Exception:
            smap, snap_ts = {}, None
        rules = experience.DEFAULT_RULES
        ref = [smap.get(c, {}).get("price") or smap.get(c, {}).get("prev_close") for c in merged]
        tbl = pd.DataFrame({
            "代码": merged,
            "名称": [nmap.get(c, "") for c in merged],
            "票数": [votes[c] for c in merged],
            "各包内排名": [" · ".join(f"{p}#{r}" for p, r in rank_in[c].items()) for c in merged],
            "最新价": [smap.get(c, {}).get("price") for c in merged],
            "参考买入价": [round(p, 2) if p else None for p in ref],
            "止盈价": [round(p * (1 + rules["take_profit"]), 2) if p else None for p in ref],
            "止损价": [round(p * (1 + rules["stop_loss"]), 2) if p else None for p in ref],
        })
        pack_oos = [_pct(packs[p].get("oos_winrate")) for p in vote["packs"]]
        pack_oos = [x for x in pack_oos if x is not None]
        oos_hint = (f"成员包 OOS 胜率 {min(pack_oos):.0%}~{max(pack_oos):.0%}"
                    if pack_oos else "成员包未验证")
        plan = experience.trade_plan(None, end)
        st.markdown(f"**🗳 组合名单（{end}，{len(merged)} 只 · {oos_hint}）**")
        st.dataframe(tbl, width='stretch', hide_index=True)
        st.caption(f"📝 执行计划：{plan['买入时间']}按开盘价买入；{plan['规则']}；最迟 {plan['最迟平仓']} 平仓")

        # ---- 策略组合 K 线：组合 vs 各单包 ----
        if st.button("📈 回测这个策略组合（组合K线 vs 各单包）", key="pc_vote_bt"):
            with st.spinner("回测中（因子缓存命中则较快）…"):
                evo_map = {f["name"]: f["code"] for f in get_evolved_factors(only_accepted=False)}
                reg = library.get_factor_registry()
                le_map = {r["name"]: r["code"]
                          for _, r in reg[reg["engine"] == "loopengine"].iterrows()}
                pool0 = vote["pool"]
                codes0 = pools.get(pool0) or []
                panel = sig.get_panel_cached(codes0, end, 800, source="qlib_local")
                pack_defs = []
                for p in vote["packs"]:
                    pk = packs[p]
                    fvals, w = {}, {}
                    for f in pk["factors"]:
                        fac = fe.resolve_factor(f["name"], f.get("kind"), evo_map, le_map)
                        if not fac:
                            continue
                        try:
                            fvals[f["name"]] = fe.get_factor_values(fac, codes0, end)
                            w[f["name"]] = (float(f["weight"]), int(f["direction"]))
                        except Exception:
                            continue
                    if w:
                        pack_defs.append({"name": p, "weights": w, "fvals": fvals,
                                          "top_n": pk["top_n"]})
                bt = fe.combo_backtest(pack_defs, panel, min_votes=min_votes)
            st.session_state["pc_vote_bt_res"] = bt
        bt = st.session_state.get("pc_vote_bt_res")
        if bt is not None and not bt.empty:
            win = (bt["组合扣费超额"] > 0).mean()
            st.markdown(f"**组合回测（固定权重 · 近3年 · 扣费）**：胜率 **{win:.0%}** · "
                        f"平均净超额 {bt['组合扣费超额'].mean():+.2%}/期 · 平均入选 {bt['入选只数'].mean():.0f} 只")
            cum = bt.set_index("调仓日")[[c for c in bt.columns if c.endswith("超额") and "扣费" not in c and c != "池内中位收益"]]
            cum = cum.add(1).cumprod()
            cum["组合(扣费)"] = bt.set_index("调仓日")["组合扣费超额"].add(1).cumprod()
            st.line_chart(cum, height=280)
            st.caption("各单包为毛超额曲线，组合为扣费净超额——组合赢在稳，不一定赢在猛")

        # ---- 落库 + 存组合包 ----
        b1, b2 = st.columns(2)
        with b1:
            cname = st.text_input("组合包名", value="我的策略组合_v1", key="pc_cname")
            if st.button("💾 保存组合包", key="pc_csave"):
                library.save_combo(cname, {"pool_name": vote["pool"], "top_n": len(merged),
                                           "rule": vote["rule"], "packs": vote["packs"]})
                st.success(f"组合包「{cname}」已保存")
        with b2:
            if st.button("📥 名单落库（开始实战跟踪）", key="pc_log",
                         help="写入经验库，每日自动结算 1/5/20/60/120 日实战胜率"):
                scores = pd.Series({c: float(votes[c]) for c in merged})
                experience.save_pick(
                    source="combo_vote", pool_name=vote["pool"], top_n=len(merged),
                    method=f"组合投票({vote['rule']})", filters=[],
                    factors=[{"name": p, "kind": "pack", "weight": 1.0, "direction": 1}
                             for p in vote["packs"]],
                    final_scores=scores, pack_name=cname or None, trade_date=end)
                st.success(f"已落库（{end}，{len(merged)} 只）→ 📋选股列表 / 📚经验库 可追踪实战")
        if st.button("➕ 名单全部加入自选股", key="pc_wl"):
            wl = load_json(WATCHLIST_FILE, [])
            add = [c for c in merged if c not in wl]
            save_json(WATCHLIST_FILE, wl + add)
            st.success(f"已加入 {len(add)} 只")

# ---------------------------------------------------------------- ③ 策略包对比台
st.header("③ 策略包对比台")
if not packs:
    st.info("还没有策略包。")
else:
    lb = experience.pack_leaderboard()
    live = {}
    if not lb.empty:
        for _, r in lb.iterrows():
            for c in ["5日胜率", "20日胜率", "1日胜率"]:
                if c in r.index and pd.notna(r[c]) and int(r["已回填战果"]) >= 3:
                    live[r["策略包"]] = (float(r[c]), int(r["已回填战果"]))
                    break
    rows = []
    for n, pk in packs.items():
        oos = _pct(pk.get("oos_winrate"))
        is_wr = _pct(pk.get("is_winrate"))
        row = {"策略包": n, "股票池": pk["pool_name"], "Top-N": pk["top_n"],
               "持有期": pk.get("horizon") or "—", "因子数": len(pk.get("factors", [])),
               "回测OOS胜率": oos, "IS胜率": is_wr,
               "过拟合差距": (is_wr - oos) if (oos is not None and is_wr is not None) else None,
               "实战命中率": live.get(n, (None,))[0], "实战期数": live.get(n, (None, 0))[1],
               "更新": pk.get("updated")}
        rows.append(row)
    df = pd.DataFrame(rows).sort_values("回测OOS胜率", ascending=False, na_position="last")
    show = df.copy()
    for c in ["回测OOS胜率", "IS胜率", "实战命中率"]:
        show[c] = show[c].map(lambda x: f"{x:.0%}" if pd.notna(x) else "未验证")
    show["过拟合差距"] = show["过拟合差距"].map(
        lambda x: (f"{x:+.0%} ⚠️" if x > 0.10 else f"{x:+.0%}") if pd.notna(x) else "—")
    st.dataframe(show, width='stretch', hide_index=True)
    st.caption("过拟合差距 = IS胜率 − OOS胜率，>+10pp 标 ⚠️（没跑过双轨的包显示 —，去 🪄选股工作台 ③ 验证后重存）")

# ---------------------------------------------------------------- ④ 我的组合包
combos = library.list_combos()
if combos:
    st.header("④ 我的组合包")
    lb2 = experience.pack_leaderboard()
    for n, cfg in combos.items():
        live_note = ""
        if not lb2.empty:
            r = lb2[lb2["策略包"] == n]
            if not r.empty and int(r.iloc[0]["已回填战果"]) >= 1:
                r = r.iloc[0]
                wins = [f"{c.replace('胜率', '')} {r[c]:.0%}" for c in r.index
                        if c.endswith("日胜率") and pd.notna(r[c]) and isinstance(r[c], float)]
                live_note = f" · 实战 {' / '.join(wins)}" if wins else ""
        c1, c2 = st.columns([5, 1])
        c1.markdown(f"**{n}** — {' ＋ '.join(cfg['packs'])}（{cfg['rule']} · 池 {cfg['pool_name']}）"
                    f" · 建于 {cfg.get('created_at', '')[:16]}{live_note}")
        if c2.button("🗑 删除", key=f"pc_del_{n}"):
            library.delete_combo(n)
            st.rerun()
