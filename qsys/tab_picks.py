"""📋 选股列表：按日查询每日 Top-N 名单 + 实战成绩（到期结算的胜率/超额）。

数据链：每日 19:00 定时扫描自动落库（experience.db）→ 数据更新后 18:45 自动结算
5/10/20 日战果 → 本页按日回看。手动在 🪄选股组合 构建的名单也在此处。
"""

import pandas as pd
import streamlit as st

import datasource
import experience
from common import SIGNALS_DIR, trade_day_offset


def render():
    st.title("📋 选股列表")
    st.caption("每日名单自动落库 · 到期自动结算 5/10/20 日实战收益 · "
               "**胜率 = 名单平均收益跑赢池内中位数的比例**（对错的唯一标准看这里，回测胜率只是先验）")

    dates = experience.list_pick_dates()
    if not dates:
        st.info("还没有选股记录。开启 ⏰定时任务 的「板块扫描」后，每个交易日 19:00 自动出名单。")
        st.stop()

    # ================= 每日总览（按日分类） =================
    hist = experience.pick_history(300)
    daily = (
        hist.groupby("trade_date")
        .agg(名单数=("id", "count"), 已结算战果=("战果数", lambda s: int(s.fillna(0).sum())),
             命中率=("命中率", "mean"), 平均超额=("平均超额", "mean"))
        .reset_index()
        .sort_values("trade_date", ascending=False)
    )
    with st.expander(f"📅 每日一览（共 {len(daily)} 个交易日有记录）", expanded=False):
        show = daily.copy()
        show["命中率"] = show["命中率"].map(lambda x: f"{x:.0%}" if pd.notna(x) else "未到期")
        show["平均超额"] = show["平均超额"].map(lambda x: f"{x:+.2%}" if pd.notna(x) else "—")
        st.dataframe(show, width='stretch', height=240)

    # ================= 按日查询 =================
    c1, c2 = st.columns([1, 2])
    with c1:
        sel_date = st.selectbox("选择交易日", dates)

    picks = experience.picks_on_date(sel_date)
    if picks.empty:
        st.warning("该日无记录")
        st.stop()

    # 来源中文标签（一眼看出这份名单是哪个功能出的）
    SRC_LABEL = {"sched_pool_scan": "⏰定时扫描", "manual_picker": "🪄选股工作台",
                 "combo_vote": "🧩策略组合投票", "auto_combo": "🤖自动因子组合"}

    # 当日多条记录时先给总览表（否则组合/自动名单藏在选择器后面看不见）
    if len(picks) > 1:
        ov_rows = []
        for r in picks.itertuples():
            o = experience.pick_outcomes(int(r.id))
            hit = f"{(o['hit'] == 1).mean():.0%}（{len(o)}期）" if not o.empty else "未到期"
            ov_rows.append({"来源": SRC_LABEL.get(r.source, r.source),
                            "策略包/方法": r.pack_name or r.method,
                            "只数": int(r.top_n), "实战命中": hit})
        st.dataframe(pd.DataFrame(ov_rows), width='stretch', hide_index=True)

    with c2:
        if len(picks) > 1:
            labels = [f"{SRC_LABEL.get(r.source, r.source)} · {r.pack_name or r.method}"
                      f"（Top{r.top_n} · {r.data_source}）" for r in picks.itertuples()]
            sel_idx = st.selectbox("该日有多条记录，逐条查看", range(len(picks)),
                                   format_func=lambda i: labels[i])
        else:
            sel_idx = 0
    pick = picks.iloc[sel_idx]
    pick_id = int(pick["id"])

    # ---- 名单头信息：先验（回测）vs 实战（结算） ----
    outs = experience.pick_outcomes(pick_id)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("策略包", pick["pack_name"] or pick["method"])
    m2.metric("回测胜率(先验)", (f"{pick['oos_winrate_at_save']:.0%}"
                              if pd.notna(pick["oos_winrate_at_save"]) else "未验证"))
    if not outs.empty:
        m3.metric("实战命中率(已结算)", f"{(outs['hit'] == 1).mean():.0%}（{len(outs)} 期）")
        m4.metric("平均超额", f"{outs['excess'].mean():+.2%}")
    else:
        m3.metric("实战命中率", "未到期")
        m4.metric("平均超额", "—")

    # ---- 结算明细：5/10/20 日 ----
    exp_dates = experience.expected_eval_dates(sel_date, pick.get("data_source"))
    chips = []
    for fwd in experience.FWD_LIST:
        o = outs[outs["fwd_days"] == fwd]
        if not o.empty:
            r = o.iloc[0]
            mark = "✅" if r["hit"] == 1 else "❌"
            chips.append(f"{mark} **{fwd}日**（{r['eval_date']}）：超额 {r['excess']:+.2%}，名单均涨 {r['avg_ret']:+.2%}")
        else:
            eta = exp_dates.get(fwd)
            chips.append(f"⏳ **{fwd}日**：待结算" + (f"（预计 {eta}）" if eta else ""))
    st.info(" · ".join(chips))

    # ---- 名单明细（含个股模拟交易成绩） ----
    items = experience.pick_items_detail(pick_id)
    # 股票名称映射（快照库里最近一次采集的名字）
    try:
        with datasource._qconn() as conn:
            rows = conn.execute(
                f"SELECT code, name, MAX(ts) FROM quote_snapshots"
                f" WHERE code IN ({','.join('?' * len(items))}) GROUP BY code",
                list(items["code"])).fetchall()
        name_map = {r[0]: r[1] for r in rows}
    except Exception:
        name_map = {}
    items.insert(1, "名称", [name_map.get(c, "") for c in items["code"]])

    # 当天/最近的名单叠加实时行情（腾讯快照）
    try:
        snaps, snap_ts = datasource.get_latest_snapshots(list(items["code"]))
        smap = {s["code"]: s for s in snaps}
        items["最新价"] = [smap.get(c, {}).get("price") for c in items["code"]]
        items["较昨收%"] = [
            round((smap[c]["price"] / smap[c]["prev_close"] - 1) * 100, 2)
            if smap.get(c) and smap[c].get("price") and smap[c].get("prev_close") else None
            for c in items["code"]
        ]
        if snap_ts:
            st.caption(f"实时列为腾讯快照（最新采集 {snap_ts}）")
    except Exception:
        pass

    # 竞价确认：名单次一交易日 09:26 的竞价检查（低开>2%/无量承接 → 回避）
    try:
        def _next_day_approx(d: str) -> str:
            nd = pd.Timestamp(d) + pd.Timedelta(days=1)
            while nd.weekday() >= 5:
                nd += pd.Timedelta(days=1)
            return nd.strftime("%Y-%m-%d")

        _cal_end = (experience._calendar() or [sel_date])[-1]
        _aday = trade_day_offset(sel_date, 1) if sel_date < _cal_end else _next_day_approx(sel_date)
        _af = SIGNALS_DIR / f"auction_{_aday}.parquet"
        if _af.exists():
            adf = pd.read_parquet(_af)
            items = items.merge(adf, on="code", how="left")
            n_avoid = int((items["竞价结论"] == "回避").sum())
            st.caption(f"🔔 竞价确认（{_aday} 09:25 落锤）：{n_avoid} 只标记回避"
                       f"（低开≤-2% 或竞价量比<0.5%），其余确认")
    except Exception:
        pass

    # 止盈/止损参考价：已入场的按实际买入价，未入场的按最新快照价
    _rules = experience.DEFAULT_RULES
    _entry = items["entry_price"] if "entry_price" in items.columns else pd.Series([None] * len(items))
    _last = items["最新价"] if "最新价" in items.columns else pd.Series([None] * len(items))
    _ref = [(b if pd.notna(b) else l) for b, l in zip(_entry, _last)]
    items["止盈价"] = [round(p * (1 + _rules["take_profit"]), 2) if p else None for p in _ref]
    items["止损价"] = [round(p * (1 + _rules["stop_loss"]), 2) if p else None for p in _ref]
    _plan = experience.trade_plan(None, sel_date)
    st.caption(f"📝 计划口径：{_plan['规则']}；新名单 {_plan['买入时间']} 买入，最迟 {_plan['最迟平仓']} 平仓")

    rename = {"rank": "排名", "code": "代码", "score": "综合分", "entry_date": "买入日",
              "entry_price": "买入价", "exit_date": "卖出日", "exit_price": "卖出价",
              "exit_reason": "平仓原因", "pnl_pct": "盈亏%", "hold_days": "持有天数"}
    show = items.rename(columns=rename)
    for c in ["综合分", "买入价", "卖出价"]:
        if c in show:
            show[c] = show[c].map(lambda x: round(float(x), 3) if pd.notna(x) else None)
    if "盈亏%" in show:
        show["盈亏%"] = show["盈亏%"].map(lambda x: f"{x * 100:+.1f}" if pd.notna(x) else "持有中/未模拟")
    st.dataframe(show, width='stretch', hide_index=True)

    if "pnl_pct" in items.columns and items["pnl_pct"].notna().any():
        done = items["pnl_pct"].dropna()
        win = (done > 0).mean()
        st.markdown(f"**个股模拟交易成绩**：{len(done)} 笔已平仓 · 胜率 {win:.0%} · 平均盈亏 {done.mean():+.2%}"
                    f"（规则：止盈15% / 止损-8% / 持有≤20日，含双边成本）")

    st.caption("更多聚合视图：📚经验库（策略包/因子实战榜）· 📈模拟交易（逐笔流水）")

