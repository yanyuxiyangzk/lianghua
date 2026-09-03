"""🎯 今日执行卡：每天打开只看这一页。

不问原理，只给行动：
  🛡 主轨（求稳）+ 🎲 卫星轨（博弹性），各 10 只，等分买入
  每只票只给四个数：参考买入价 / 止损价 / 止盈价 / 最迟卖出日
  红绿灯只看一个东西：这个策略**最近实战赚没赚钱**（经验库结算）
  策略失效或数据停更时，页面顶部横幅主动提醒——平时不用管任何后台。

所有因子生产/体检/演化都在后台全托管（⏰定时任务），专业页面收在"专业区"。
"""

import json

import pandas as pd
import streamlit as st

import datasource
import experience
import library
import scheduler
from common import DATA_DIR, all_pools, get_last_trade_day, load_json, save_json
from quotefeed import get_feed


def render():
    st.title("🎯 今日执行")

    end = get_last_trade_day()
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    packs = library.list_strategies()
    TRACK_FILE = DATA_DIR / "today_tracks.json"
    RULES = experience.DEFAULT_RULES  # 止盈15% / 止损-8% / 持有≤20交易日


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


    # ---------------------------------------------------------------- 轨道包
    def _satellite_auto():
        best, best_n = None, 0
        for n, pk in packs.items():
            k = sum(1 for f in pk.get("factors", []) if str(f["name"]).startswith("ev_"))
            if k > best_n:
                best, best_n = n, k
        if best:
            return best
        return next((n for n in packs if "涨停" in n or "事件" in n), None)


    cfg = load_json(TRACK_FILE, {})
    sat_name = cfg.get("satellite") or _satellite_auto()
    main_name = cfg.get("main")

    # ---------------------------------------------------------------- 需要干预的横幅（全托管的"出事才说话"）
    alerts = []
    if end < today and pd.Timestamp.now().weekday() < 5:
        alerts.append(f"⚠️ 行情数据停在 {end}（今天 {today}）——数据更新任务可能没跑，去 ⏰定时任务 看「每日数据更新」")
    if not packs:
        alerts.append("⚠️ 还没有任何策略包——去专业区「🧩选股组合」点一次 ① 自动组建")
    lb = experience.pack_leaderboard()
    for track_name in {n for n in [main_name, sat_name] if n}:
        if lb.empty:
            break
        row = lb[lb["策略包"] == track_name]
        if not row.empty:
            r = row.iloc[0]
            if int(r["已回填战果"]) >= 5:
                wins = [float(r[c]) for c in r.index if c.endswith("日胜率") and pd.notna(r[c]) and isinstance(r[c], float)]
                if wins and sum(wins) / len(wins) < 0.5:
                    alerts.append(f"🔴 策略包「{track_name}」实战命中率连续低于 50%（{int(r['已回填战果'])} 期）——"
                                  "建议换代：去专业区「🧩选股组合」重新组建")
    for a in alerts:
        st.warning(a)

    # ---------------------------------------------------------------- 名单
    dates = experience.list_pick_dates(limit=5)
    sel_date = dates[0] if dates else None
    picks = experience.picks_on_date(sel_date) if sel_date else pd.DataFrame()


    def _pick_for(pack_name):
        if picks.empty or not pack_name:
            return None
        rows = picks[picks["pack_name"] == pack_name]
        return rows.iloc[0] if len(rows) else None


    def _gen(pack_name, kp):
        pk = packs[pack_name]
        codes = all_pools().get(pk["pool_name"]) or []
        sel, note, _w, _fs = scheduler.compute_pack_picks(pk, codes, end, pk["top_n"])
        experience.save_pick(source=f"track_{kp}", pool_name=pk["pool_name"], top_n=len(sel),
                             method=pk.get("method"), filters=pk.get("filters", []),
                             factors=pk["factors"], final_scores=sel, pack_name=pack_name,
                             oos_winrate=_pct(pk.get("oos_winrate")), trade_date=end)
        return len(sel)


    def _live_badge(pack_name):
        """实战红绿灯：只看最近实战胜率。"""
        if not pack_name or lb.empty:
            return "⚪ 还没实战记录"
        row = lb[lb["策略包"] == pack_name]
        if row.empty:
            return "⚪ 还没实战记录"
        r = row.iloc[0]
        if int(r["已回填战果"]) < 3:
            return f"⚪ 实战积累中（{int(r['已回填战果'])} 期，≥3 期才亮灯）"
        wins = [float(r[c]) for c in r.index if c.endswith("日胜率") and pd.notna(r[c]) and isinstance(r[c], float)]
        if not wins:
            return "⚪ 还没实战记录"
        w = sum(wins) / len(wins)
        return (f"🟢 最近实战赚钱（命中率 {w:.0%}）" if w >= 0.55
                else f"🔴 最近实战不赚钱（命中率 {w:.0%}）——考虑换代")


    def _render_track(icon, title, pack_name, pick, kp, budget_pct):
        st.markdown(f"### {icon} {title}")
        if not pack_name or pack_name not in packs:
            st.caption("这一轨还没配置策略包。"
                       + ("（先攒 ev_ 因子再组涨停包：专业区「🔬个股分析」底部定向挖）" if kp == "sat" else ""))
            return
        st.markdown(f"**{_live_badge(pack_name)}**　·　策略包「{pack_name}」")
        if pick is None:
            if st.button(f"🚀 生成今日名单", key=f"{kp}_gen", type="primary"):
                with st.spinner("扫描中…"):
                    n = _gen(pack_name, kp)
                st.success(f"已出 {n} 只")
                st.rerun()
            if sel_date:
                st.caption(f"（{sel_date} 的名单里没有这一轨，点上面按钮现场生成）")
            return

        items = experience.pick_items_detail(int(pick["id"]))
        if items.empty:
            st.warning("名单为空。")
            return
        n = len(items)
        plan = experience.trade_plan(None, sel_date)
        st.markdown(f"**📌 {sel_date} 名单：{n} 只，等分买入（每只 = {budget_pct} 的 1/{n}）· "
                    f"{plan['买入时间']}开盘买 · 跌 {RULES['stop_loss']:.0%} 无条件卖 · "
                    f"涨 {RULES['take_profit']:.0%} 落袋 · 最迟 {plan['最迟平仓']} 必须卖**")

        nmap = _name_map(list(items["code"]))
        codes = list(items["code"])

        # 启动后台采集，确保快照数据可用
        feed = get_feed()
        feed_key = f"track:{kp}:{pack_name}"
        feed.ensure(feed_key, codes, interval=10)

        # 尝试从数据库读取快照；若无数据则立即抓取一次
        try:
            snaps, _ = datasource.get_latest_snapshots(codes)
            smap = {s["code"]: s for s in snaps}
        except Exception:
            smap = {}
        if not smap:
            if st.button("🔄 立即抓取行情", key=f"{kp}_fetch"):
                with st.spinner("抓取中…"):
                    rows = datasource.get_batch_snapshots(codes)
                    datasource.save_snapshots(rows)
                st.rerun()
            st.info("首次访问需要抓取行情数据，点击上方按钮或等待 3-5 秒自动采集。")
        ref = [smap.get(c, {}).get("price") or smap.get(c, {}).get("prev_close") for c in items["code"]]
        st.dataframe(pd.DataFrame({
            "代码": list(items["code"]),
            "名称": [nmap.get(c, "") for c in items["code"]],
            "最新价": [smap.get(c, {}).get("price") for c in items["code"]],
            "参考买入价": [round(p, 2) if p else None for p in ref],
            "止损价（无条件卖）": [round(p * (1 + RULES["stop_loss"]), 2) if p else None for p in ref],
            "止盈价（落袋）": [round(p * (1 + RULES["take_profit"]), 2) if p else None for p in ref],
        }), width='stretch', hide_index=True)
        # 选股方式与因子组合（可追溯：这批名单是怎么选出来的）
        pk_info = packs.get(pack_name) or {}
        facs = pk_info.get("factors", [])
        with st.expander(f"🧬 选股方式：策略包「{pack_name}」（{pk_info.get('method', '-')}）"
                         f" · {len(facs)} 因子组合 · 池 {pk_info.get('pool_name', '-')}"
                         f" · 买入时间：次日开盘"):
            if facs:
                st.dataframe(pd.DataFrame(
                    [{"因子": f.get("name"), "类型": f.get("kind"),
                      "权重": round(float(f.get("weight", 1)), 3),
                      "方向": ("正向" if f.get("direction", 1) > 0 else "负向")} for f in facs]),
                    hide_index=True, width='stretch')
        if st.button("➕ 加入自选股", key=f"{kp}_wl"):
            wl = load_json(WATCHLIST_FILE, [])
            add = [c for c in items["code"] if c not in wl]
            save_json(WATCHLIST_FILE, wl + add)
            st.success(f"已加入 {len(add)} 只")


    main_pick = _pick_for(main_name)
    if main_pick is None and not picks.empty:
        non_sat = picks[picks["pack_name"] != sat_name] if sat_name else picks
        sched = non_sat[non_sat["source"] == "sched_pool_scan"]
        main_pick = (sched.iloc[0] if not sched.empty else non_sat.iloc[0])
        if main_name is None and pd.notna(main_pick.get("pack_name")):
            main_name = main_pick["pack_name"]

    _render_track("🛡", "主轨 · 稳健（仓位大头，建议 7 成）", main_name, main_pick, "main", "主轨资金")
    st.markdown("---")
    _render_track("🎲", "卫星轨 · 博涨停（仓位小头，建议 ≤2 成，亏了不伤筋骨）",
                  sat_name, _pick_for(sat_name), "sat", "卫星轨资金")

    # ---------------------------------------------------------------- 持仓（盘中触发开平仓，T+1）
    def _render_positions():
        st.markdown("---")
        st.markdown("### 📦 当前持仓")
        st.caption("盘中自动开仓（名单次日 9:30 起按快照价触发，竞价回避自动跳过）"
                   " · 止盈+15% / 止损-8% / 满20交易日自动平仓 · T+1（买入日当天不卖）")
        try:
            opens = experience.get_open_positions()
        except Exception:
            opens = pd.DataFrame()
        if opens.empty:
            st.caption("暂无持仓——盘中每 5 分钟检查名单触发开仓")
        else:
            show = pd.DataFrame({
                "代码": opens["code"], "名称": opens["name"],
                "买入日期": opens["buy_date"],
                "买入时间": opens["buy_ts"],
                "买入价": opens["buy_price"].round(2),
                "最新价": opens["最新价"].round(2),
                "浮动盈亏": opens["浮动盈亏%"].map(lambda x: f"{x:+.2f}%" if pd.notna(x) else "-"),
                "持有(交易日)": opens["持有交易日"],
                "来源": opens["pack_name"].fillna(opens["source"]),
            })
            st.dataframe(show, hide_index=True, width='stretch')

        stats = experience.position_stats()
        hist = experience.get_position_history(50)
        if not hist.empty:
            st.markdown("### 📜 持仓历史（已平仓）")
            win = f"{stats['胜率']:.0%}" if stats.get("胜率") is not None else "-"
            avg = f"{stats['平均收益率']:+.2%}" if stats.get("平均收益率") is not None else "-"
            total = f"{stats['累计收益率']:+.2%}" if stats.get("累计收益率") is not None else "-"
            st.markdown(f"**已平仓 {stats['已平仓']} 笔 · 胜率 {win} · 平均收益率 {avg} · 累计收益率 {total}**")
            show_h = pd.DataFrame({
                "代码": hist["code"], "名称": hist["name"],
                "买入日": hist["buy_date"], "买入价": hist["buy_price"].round(2),
                "卖出日": hist["sell_date"], "卖出价": hist["sell_price"].round(2),
                "收益率": hist["pnl_pct"].map(lambda x: f"{x:+.2%}" if pd.notna(x) else "-"),
                "平仓原因": hist["sell_reason"], "持有(交易日)": hist["hold_days"],
            })
            st.dataframe(show_h, hide_index=True, width='stretch')

    _render_positions()

    # ---------------------------------------------------------------- 昨日执行回顾（T+1：昨日名单今开买入，逐股收益率+胜率）
    def _render_yesterday_review():
        dates = experience.list_pick_dates(limit=5)
        if len(dates) < 2:
            return
        prev_date = dates[1]  # dates[0]=今天用的名单（昨晚扫描），dates[1]=昨天名单（今开买入）
        ypicks = experience.picks_on_date(prev_date)
        if ypicks.empty:
            return
        st.markdown("---")
        st.markdown(f"### 📊 昨日执行回顾（{prev_date} 名单 · 今日开盘买入）")
        for r in ypicks.itertuples():
            items = experience.pick_items_detail(int(r.id))
            if items.empty:
                continue
            codes = list(items["code"])
            # 最新价 + 今开（ifind_realtime 5分钟快照）
            try:
                with datasource._qconn() as conn:
                    rt = pd.read_sql(
                        f"SELECT code, price, open FROM ifind_realtime"
                        f" WHERE datetime=(SELECT MAX(datetime) FROM ifind_realtime)"
                        f" AND code IN ({','.join('?' * len(codes))})",
                        conn, params=codes)
                rmap = {x.code: (x.price, x.open) for x in rt.itertuples()}
            except Exception:
                rmap = {}
            rows = []
            for it in items.itertuples():
                entry = getattr(it, "entry_price", None)  # 模拟成交的买入价（今晚 20:05 回填）
                price, open_p = rmap.get(it.code, (None, None))
                entry = entry if pd.notna(entry) else open_p  # 未回填时用今开
                pnl = getattr(it, "pnl_pct", None)
                if pd.notna(pnl):
                    ret, status = float(pnl), getattr(it, "exit_reason", "已平仓")
                elif entry and price:
                    ret, status = price / entry - 1, "持有中"
                else:
                    ret, status = None, "待数据"
                rows.append({"代码": it.code, "买入价": round(entry, 2) if entry else None,
                             "最新价": round(price, 2) if price else None,
                             "收益率": f"{ret:+.2%}" if ret is not None else "-",
                             "状态": status})
            df_r = pd.DataFrame(rows)
            done = [r for r in rows if isinstance(r["收益率"], str) and r["收益率"] != "-"]
            if done:
                rets = [float(r["收益率"].strip("%")) / 100 for r in done]
                win = sum(1 for x in rets if x > 0) / len(rets)
                st.markdown(f"**{r.pack_name or r.method}**（{r.source}）：{len(done)}/{len(rows)} 只有数"
                            f" · 当日胜率 **{win:.0%}** · 平均收益率 **{sum(rets)/len(rets):+.2%}**")
                st.dataframe(df_r, hide_index=True, width='stretch')
            else:
                st.markdown(f"**{r.pack_name or r.method}**（{r.source}）：盘中快照未就绪，稍后再看")

    _render_yesterday_review()

    with st.expander("⚙️ 轨道设置（每条轨用哪个策略包，平时不用动）"):
        names = list(packs.keys())
        msel = st.selectbox("🛡 主轨包", ["自动（今日定时扫描所用）"] + names,
                            index=(names.index(cfg["main"]) + 1 if cfg.get("main") in names else 0))
        ssel = st.selectbox("🎲 卫星轨包", ["自动（ev_ 事件因子最多）"] + names,
                            index=(names.index(cfg["satellite"]) + 1 if cfg.get("satellite") in names else 0))
        if st.button("💾 保存", key="td_track_save"):
            save_json(TRACK_FILE, {"main": None if msel.startswith("自动") else msel,
                                   "satellite": None if ssel.startswith("自动") else ssel})
            st.rerun()

    st.caption("📚 各策略包最近实战赚没赚：左侧「📚 实战成绩」页 · 想自己调策略：专业区「🧩选股组合」")

