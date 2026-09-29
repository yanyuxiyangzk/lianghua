"""🎲 卫星轨 · 事件策略候选名单与真实战绩（统一资金账户口径）。

候选由每日 19:10 独立扫描产出；下单、持仓与风控全部由主轨资金账户统一管理。
战绩直接读主账户 positions 表中卫星来源记录——旧的 satellite_positions 独立台账
已废弃（无写入方），不再作为展示口径。
"""

import pandas as pd
import streamlit as st

import experience as exp
import broker as bk
import library
import scheduler

_SOURCE_LABEL = {"satellite_scan": "正式", "sched_satellite_scan": "影子"}


def _get_satellite_pack_info() -> dict | None:
    """获取卫星轨策略包信息。"""
    packs = library.list_strategies()
    name = scheduler._satellite_pack_name(packs)
    return {"name": name, **packs[name]} if name else None


def _render_pack_info(pack: dict):
    """渲染策略包信息卡片。"""
    st.caption(f"策略包: **{pack['name']}** | 股票池: {pack.get('pool_name', '沪深300')} | "
               f"Top-N: {pack.get('top_n', 5)} | 加权: {pack.get('method', 'ICIR加权')}")
    oos = pack.get("oos_winrate")
    if oos:
        st.caption(f"OOS胜率: {oos}")
    from strategy_progress import latest_reports, research_reason
    reason = research_reason(pack, latest_reports().get(pack['name']))
    if reason:
        st.warning(f"研究资格：{reason}。候选仅供观察，不代表可以自动买入。")
    else:
        st.info("当前版本研究已通过；执行仍须独立资格与账户风控校验。")
    factors = pack.get("factors", [])
    if factors:
        rows = []
        for f in factors:
            rows.append({
                "因子": f["name"],
                "权重": f"{f['weight']:.2%}",
                "方向": "正向" if f["direction"] == 1 else "反向",
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_observation_only():
    """当前候选必须匹配已收盘日期和策略版本，历史名单单独展示。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from selection_policy import completed_signal_day
    from satellite_candidates import latest_candidates
    today = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
    try:
        day = completed_signal_day()
    except ValueError as exc:
        st.error(str(exc))
        return
    st.caption(f"查看日期：{today} · 目标行情截止日期：{day}（仅使用已收盘数据）")
    pack = _get_satellite_pack_info()
    if not pack:
        st.warning("暂无可用卫星事件策略包。")
        return
    _render_pack_info(pack)
    status = scheduler.get_scheduler().view().get('satellite_scan', {})
    last = status.get('last') or {}
    if status.get('running_since'):
        st.info("卫星候选正在生成，请稍后刷新状态。")
    if last:
        st.caption(f"最近扫描：{last.get('time')} · {last.get('msg')}")
        if last.get('ok') is False:
            st.warning("最近扫描未完成，旧名单不会作为当前候选展示。")
    st.caption(f"下次自动扫描：{status.get('next') or '未排程'}；盘后扫描才使用当日收盘行情。")
    st.button("刷新候选与任务状态", key="refresh_satellite_candidates")
    stale_display = False
    try:
        picks = latest_candidates(pack['name'], pack, day)
        observation = picks.empty
        if observation:
            picks = latest_candidates(pack['name'], pack, day, observation=True)
        with st.expander("历史候选（不作为当前名单）", expanded=False):
            old = latest_candidates(pack['name'], pack, history=True)
            if old.empty:
                old = latest_candidates(pack['name'], pack, observation=True, history=True)
            if old.empty:
                st.caption("暂无历史候选")
            else:
                st.caption(f"历史行情日期：{old.iloc[0]['trade_date']} · 生成时间：{old.iloc[0]['created_at']}")
                st.dataframe(old, hide_index=True, width="stretch")
    except Exception as exc:
        st.error(f"读取卫星名单失败：{exc}")
        return
    if picks.empty:
        # 收盘后尚未到下一次扫描时，保留最近一批供查看，但明确标为旧批次。
        picks = latest_candidates(pack['name'], pack, history=True)
        observation = False
        stale_display = not picks.empty
        if not stale_display:
            st.warning(f"尚无行情截止 {day}、匹配当前策略版本的候选。请等待下一次扫描。")
            return
    if stale_display:
        st.warning(f"当前尚无行情截止 {day} 的新名单；以下为最近一批候选，仅供查看，不作为当前信号。")
    st.caption(f"生成时间：{picks.iloc[0]['created_at']} · 行情截止日期：{picks.iloc[0]['trade_date']} · {'历史候选' if stale_display else ('观察名单' if observation else '卫星扫描候选')}")
    try:
        prices = bk._latest_prices(picks["code"].tolist())
    except Exception:
        prices = {}
    rows = []
    for _, row in picks.iterrows():
        pr = prices.get(row["code"]) or ()
        cur = pr[0] if len(pr) > 0 else None
        prev = pr[1] if len(pr) > 1 else None
        chg = ((cur / prev - 1) * 100) if cur and prev else None
        rows.append({"行情截止日期": row["trade_date"], "股票代码": row["code"],
                     "策略评分": row["score"], "最新价": cur, "涨跌幅(%)": chg})
    st.subheader(f"候选股票（{len(rows)} 只）")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if stale_display:
        st.caption("该批次已过当前目标日期，待下一次扫描生成新信号。")
    elif observation:
        st.caption("当前仅有观察名单，待正式扫描确认后交给统一账户评估。")
    else:
        st.caption("正式建议通过策略资格、买点及账户风控后，由统一资金账户买入；委托、成交和持仓标记卫星来源。")


def _render_track_record():
    """卫星轨真实战绩：统一资金账户中卫星来源的持仓、挂单与平仓记录。"""
    st.divider()
    st.subheader("真实战绩（统一资金账户）")
    opens = exp.satellite_track_positions(("open", "closing"))
    pendings = exp.satellite_track_positions(("pending",))
    closed = exp.satellite_track_record(200)
    traded = closed[closed["pnl_pct"].notna()] if not closed.empty else closed
    housekeeping = closed[closed["pnl_pct"].isna()] if not closed.empty else closed

    n = len(traded)
    realized = float((traded["pnl_pct"] * traded["buy_amount"].fillna(0)).sum()) if n else 0.0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("已平仓", f"{n} 笔")
    c2.metric("胜率", f"{(traded['pnl_pct'] > 0).mean():.0%}" if n else "—")
    c3.metric("平均每笔", f"{traded['pnl_pct'].mean():+.2%}" if n else "—")
    c4.metric("累计已实现盈亏", f"{realized:+,.0f} 元")
    st.caption("口径：主账户 positions 表中卫星扫描来源的平仓记录（盈亏%已扣双边费用估算）；"
               "对账合并等非交易记录不计入统计。")

    if not opens.empty:
        st.markdown("**当前持仓**")
        try:
            prices = bk._latest_prices(opens["code"].tolist())
        except Exception:
            prices = {}
        rows = []
        for p in opens.itertuples():
            pr = prices.get(p.code) or ()
            cur = pr[0] if len(pr) > 0 else None
            pnl = (cur / p.buy_price - 1) if cur and p.buy_price else None
            rows.append({"代码": p.code, "名称": p.name, "买入日": p.buy_date,
                         "成本": p.buy_price, "股数": int(p.shares or 0), "最新价": cur,
                         "浮动盈亏": f"{pnl:+.2%}" if pnl is not None else "—",
                         "来源": _SOURCE_LABEL.get(p.source, p.source)})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    if not pendings.empty:
        st.markdown("**挂单中（限价买入，盘中触及成交）**")
        rows = [{"代码": p.code, "名称": p.name, "限价": p.limit_price,
                 "委托时间": p.created_at, "来源": _SOURCE_LABEL.get(p.source, p.source)}
                for p in pendings.itertuples()]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    if n:
        curve = (traded.sort_values(["sell_date", "id"])
                 .assign(盈亏额=lambda d: d["pnl_pct"] * d["buy_amount"].fillna(0)))
        curve["累计盈亏"] = curve["盈亏额"].cumsum()
        st.markdown("**累计已实现盈亏（元）**")
        st.line_chart(curve.set_index("sell_date")["累计盈亏"])

        st.markdown("**逐笔平仓明细**")
        show = curve[["code", "name", "buy_date", "buy_price", "sell_date", "sell_price",
                      "pnl_pct", "盈亏额", "hold_days", "sell_reason", "source"]].copy()
        show.columns = ["代码", "名称", "买入日", "买入价", "卖出日", "卖出价",
                        "盈亏%", "盈亏额(元)", "持有交易日", "卖出原因", "来源"]
        show["盈亏%"] = show["盈亏%"].map(lambda x: f"{x:+.2%}")
        show["盈亏额(元)"] = show["盈亏额(元)"].map(lambda x: f"{x:+,.0f}")
        show["来源"] = show["来源"].map(lambda s: _SOURCE_LABEL.get(s, s))
        st.dataframe(show, hide_index=True, width="stretch")
    elif opens.empty and pendings.empty:
        st.info("卫星轨尚无真实成交记录：候选名单每日照常生成，"
                "但需策略包恢复 active 且账户风控放行后才会开仓。")

    if not housekeeping.empty:
        with st.expander(f"非交易记录（对账/合并，{len(housekeeping)} 条）", expanded=False):
            show = housekeeping[["code", "name", "buy_date", "sell_date", "sell_reason"]].copy()
            show.columns = ["代码", "名称", "买入日", "关闭日", "原因"]
            st.dataframe(show, hide_index=True, width="stretch")


def render():
    st.title("🎲 卫星轨 · 事件策略选股")
    st.info("卫星轨只负责生成事件候选；下单、持仓与风控全部由主轨资金账户统一管理。")
    _render_observation_only()
    _render_track_record()


render()
