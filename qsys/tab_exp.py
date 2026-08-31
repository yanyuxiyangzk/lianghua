"""📚 经验库 tab：选股结果档案 + 到期战果 + 实战榜单（经验积累，反哺进化）。"""

import pandas as pd
import streamlit as st

import experience


def render():
    st.subheader("📚 选股经验库")
    st.caption("每次生成名单（手动选股/定时扫描）都会自动落库，不管对错；"
               "到期后由定时任务「战果回填」按交易日历结算 5/10/20 日战绩，形成可复用的实战经验。")

    hist = experience.pick_history()
    if hist.empty:
        st.info("经验库还是空的。去 🪄选股组合 构建一次组合（会自动落库），或开启定时扫描任务。")
        return

    # ---------------- 概览 ----------------
    total = len(hist)
    filled = int(hist["战果数"].fillna(0).sum())
    evaluated = hist.dropna(subset=["命中率"])
    c1, c2, c3 = st.columns(3)
    c1.metric("选股次数", total)
    c2.metric("已结算战果", filled)
    c3.metric("整体命中率", f"{(evaluated['命中率'] > 0.5).mean():.0%}" if len(evaluated) else "待结算")

    # ---------------- 榜单 ----------------
    st.markdown("**🏆 组合/策略包实战榜**（实战胜率 vs 保存时的回测胜率——两者差距大说明回测乐观）")
    pack_lb = experience.pack_leaderboard()
    if pack_lb.empty:
        st.caption("暂无")
    else:
        show = pack_lb.copy()
        for c in show.columns:
            if "胜率" in c and c != "回测OOS胜率":
                show[c] = show[c].map(lambda x: f"{x:.0%}" if pd.notna(x) else "—")
            if "均超额" in c:
                show[c] = show[c].map(lambda x: f"{x:.2%}" if pd.notna(x) else "—")
            if "收益率" in c:
                show[c] = show[c].map(lambda x: f"{x:.2%}" if pd.notna(x) else "—")
        st.dataframe(show, width='stretch')

    st.markdown("**🧬 因子实战榜（近似归因）**")
    st.caption("含有该因子的组合其后的 20 日战果均值——因子间有混杂，仅作方向参考，不作淘汰唯一依据")
    fac_lb = experience.factor_leaderboard()
    if fac_lb.empty:
        st.caption("暂无（需先有到期战果）")
    else:
        show = fac_lb.copy()
        show["20日胜率(近似)"] = show["20日胜率(近似)"].map(lambda x: f"{x:.0%}")
        show["20日均超额(近似)"] = show["20日均超额(近似)"].map(lambda x: f"{x:.2%}")
        st.dataframe(show, width='stretch')

    # ---------------- 历史明细 ----------------
    st.markdown("**🗂 选股历史**")
    show = hist.copy()
    show["命中率"] = show["命中率"].map(lambda x: f"{x:.0%}" if pd.notna(x) else "未到期")
    show["平均超额"] = show["平均超额"].map(lambda x: f"{x:.2%}" if pd.notna(x) else "—")
    st.dataframe(show, width='stretch', height=260)

    pid = st.selectbox("查看某次明细", hist["id"].tolist(),
                       format_func=lambda i: f"#{i} · {hist[hist['id']==i]['created_at'].iloc[0]} · "
                                             f"{hist[hist['id']==i]['pool_name'].iloc[0]}")
    if pid:
        import sqlite3
        with experience._conn() as conn:
            items = pd.read_sql("SELECT rank, code, score FROM pick_items WHERE pick_id=? ORDER BY rank",
                                conn, params=(pid,))
            outs = pd.read_sql("SELECT fwd_days, eval_date, avg_ret, pool_median, excess, hit"
                               " FROM outcomes WHERE pick_id=?", conn, params=(pid,))
            meta = pd.read_sql("SELECT * FROM picks WHERE id=?", conn, params=(pid,)).iloc[0]
        st.caption(f"方法 {meta['method']} · 过滤器 {meta['filters']} · 因子 {meta['factors'][:200]}…")
        c4, c5 = st.columns(2)
        with c4:
            st.markdown("入选名单")
            st.dataframe(items, width='stretch')
        with c5:
            st.markdown("战果结算")
            if outs.empty:
                st.caption("未到期或未回填（可在 ⏰定时任务 开启「战果回填」）")
            else:
                o = outs.copy()
                for c in ["avg_ret", "pool_median", "excess"]:
                    o[c] = o[c].map(lambda x: f"{x:.2%}")
                o["hit"] = o["hit"].map({1: "✅", 0: "❌"})
                st.dataframe(o, width='stretch')

    # ---------------- 操作 ----------------
    b1, b2 = st.columns(2)
    with b1:
        if st.button("🎯 立即回填战果"):
            with st.spinner("回填中…"):
                msg = experience.backfill_outcomes()
            st.success(msg)
            st.rerun()
    with b2:
        if st.button("📄 导出经验报告（可供 RD-Agent 读取）"):
            path = experience.export_experience_report()
            st.success(f"已生成 {path}")
            st.download_button("下载报告", path.read_text(), file_name="experience_report.md")
