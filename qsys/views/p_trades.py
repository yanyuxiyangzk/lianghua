"""📈 模拟交易：买入价 → 卖出价 → 平仓 → 实战盈亏。

两个模式：
  ① 组合模拟：策略包每日名单 → 次日开盘价买入 → 止盈/止损/到期平仓 → 组合净值与台账
  ② 手动模拟：指定代码 + 买入价 + 卖出规则（止盈/止损/持有天数）→ 单笔盈亏评估
"""

import pandas as pd
import streamlit as st

import experience
import library
import signals as sig
from common import get_last_trade_day


def _metrics(stats: dict) -> str:
    if not stats:
        return ""
    items = [("交易笔数", stats["交易笔数"]), ("胜率", f"{stats['胜率']:.0%}"),
             ("平均盈亏", f"{stats['平均盈亏']:+.2%}"), ("盈亏比", f"{stats['盈亏比']:.2f}" if stats['盈亏比'] else "—"),
             ("利润因子", f"{stats['利润因子']:.2f}" if stats['利润因子'] else "—"),
             ("净值", f"{stats['净值']:.3f}"), ("最大回撤", f"{stats['最大回撤']:.1%}")]
    spans = "".join(f"<span style='color:#999'>{k}</span> <b style='color:#ddd'>{v}</b>　" for k, v in items)
    return f"<div style='background:#161618;padding:8px 12px;border-radius:6px'>{spans}</div>"


def render():
    st.title("📈 模拟交易")
    st.caption("信号买入 → 规则平仓 → 实战盈亏。规则：次日开盘价买入；盘中先触止损按止损、先触止盈按止盈（同日双触保守止损）；到期收盘卖；往返成本 0.25%")

    tab1, tab2 = st.tabs(["① 组合模拟（策略包实战）", "② 手动模拟（自定买卖价）"])

    # ================= 组合模拟 =================
    with tab1:
        packs = library.list_strategies()
        pack_opts = ["（全部名单）"] + list(packs.keys())
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            pack = st.selectbox("策略包", pack_opts, index=0, key="tr_pack")
        with c2:
            tp = st.number_input("止盈 %", value=15.0, step=1.0, key="tr_tp") / 100
        with c3:
            sl = st.number_input("止损 %", value=-8.0, step=1.0, key="tr_sl") / 100
        with c4:
            hold = st.number_input("持有天数", value=20, step=5, key="tr_hold")
        rules = {"take_profit": tp, "stop_loss": sl, "hold_days": int(hold)}

        if st.button("🔄 回填/更新模拟（按当前规则重算全部名单）", key="tr_run"):
            with experience._conn() as c:
                c.execute("DELETE FROM trades")
            with st.spinner("逐笔模拟中…"):
                msg = experience.backfill_trades(rules)
            st.success(msg)
            st.rerun()

        stats = experience.trade_stats(None if pack.startswith("（") else pack)
        if stats:
            st.markdown(_metrics(stats), unsafe_allow_html=True)
            nav = stats.pop("nav")
            st.markdown("**组合净值曲线（逐笔平仓净值累计）**")
            st.line_chart(nav, height=260)

            st.markdown("**交易台账**")
            df = experience.trade_ledger(None if pack.startswith("（") else pack)
            if not df.empty:
                show = df[["exit_date", "code", "entry_date", "entry_price", "exit_price",
                           "exit_reason", "pnl_pct", "hold_days", "pack_name"]].copy()
                show.columns = ["平仓日", "代码", "买入日", "买入价", "卖出价", "平仓原因", "盈亏%", "持有天数", "策略包"]
                show["盈亏%"] = show["盈亏%"].map(lambda x: f"{x:+.2%}")
                st.dataframe(show, width='stretch', height=380, hide_index=True)
        else:
            st.info("暂无模拟交易。先点「回填/更新模拟」。")

    # ================= 手动模拟 =================
    with tab2:
        st.markdown("**自定买入价 + 卖出规则，评估单笔交易**")
        codes_all = sig.get_panel_cached.__wrapped__ if False else None
        from common import get_instruments
        c1, c2 = st.columns(2)
        with c1:
            code = st.selectbox("标的代码", get_instruments("csi300"), key="tr_code")
            buy_date = st.date_input("买入日", value=pd.Timestamp(get_last_trade_day()) - pd.Timedelta(days=10), key="tr_bdate")
            buy_price = st.number_input("买入价（0=用当日开盘价）", value=0.0, step=0.01, key="tr_bprice")
        with c2:
            tp2 = st.number_input("止盈 %", value=15.0, step=1.0, key="tr_tp2") / 100
            sl2 = st.number_input("止损 %", value=-8.0, step=1.0, key="tr_sl2") / 100
            hold2 = st.number_input("持有天数", value=20, step=5, key="tr_hold2")

        if st.button("▶️ 模拟这笔交易", key="tr_go", type="primary"):
            r2 = {"take_profit": tp2, "stop_loss": sl2, "hold_days": int(hold2)}
            t = experience.simulate_trade(
                code, str(buy_date), rules=r2,
                entry_price_override=(buy_price if buy_price > 0 else None),
                entry_date_override=str(buy_date))
            if t is None:
                st.error("数据不足，无法模拟（买入日可能非交易日或标的停牌）。")
            else:
                pnl_color = "#e54545" if t["pnl_pct"] > 0 else "#2ca02c"
                st.markdown(
                    f"<div style='background:#161618;padding:12px 14px;border-radius:6px;font-family:monospace'>"
                    f"<b style='color:#ccc'>{t['code']} 交易结果</b><br>"
                    f"买入 {t['entry_date']} @ <b>{t['entry_price']}</b> → "
                    f"卖出 {t['exit_date']} @ <b>{t['exit_price']}</b>（{t['exit_reason']}）<br>"
                    f"盈亏 <b style='color:{pnl_color};font-size:18px'>{t['pnl_pct']:+.2%}</b> · 持有 {t['hold_days']} 天"
                    f"</div>", unsafe_allow_html=True)
                st.caption(f"规则：止盈 {r2['take_profit']:+.0%} / 止损 {r2['stop_loss']:+.0%} / 持有 {r2['hold_days']} 天 / 成本 0.25%")


render()
