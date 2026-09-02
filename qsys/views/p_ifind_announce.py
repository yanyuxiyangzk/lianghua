"""📜 公告信息：iFinD 公告落库展示（保存7天，超期自动清理）。

数据链路：⏰定时任务 job_ifind_announce 每日 16:30 抓取自选股近7天公告
（iFinD THS_ReportQuery / HTTP report_query）→ ifind_announcements 表 → 本页读库。
"""

import math

import pandas as pd
import streamlit as st

import datasource

PAGE_SIZE = 20


@st.cache_data(ttl=86400, show_spinner=False)
def _fetch_pdf_b64(url: str):
    """服务器端抓取 PDF 并 base64 编码（同花顺 PDF 是 force-download，浏览器无法直接内嵌，
    转成 data URI 才能在 iframe 里在线预览；按 URL 缓存一天）"""
    import base64
    import requests
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return base64.b64encode(r.content).decode()
    except Exception:
        pass
    return None


def render():
    st.title("📜 公告信息")
    st.caption("数据源：同花顺 iFinD（THS_ReportQuery）· 保存最近 7 天，超期自动清理"
               " · 每日 16:30 自动抓取**自选股**公告")

    df = datasource.get_announcements_from_db()

    # 数据库为空时提示
    if df.empty:
        st.info("数据库中暂无公告数据，请点击「🔄 同步公告」按钮拉取（自选股近7天）。")
        if st.button("🔄 同步公告", type="primary", key="ann_sync_first"):
            with st.spinner("正在同步公告…"):
                from scheduler import job_ifind_announce
                msg = job_ifind_announce()
            st.success(msg)
            st.rerun()
        return

    # 搜索 + 同步（搜索框默认空）
    c1, c2 = st.columns([3, 1])
    with c1:
        kw = st.text_input("搜索", "", key="ann_kw", label_visibility="collapsed",
                           placeholder="输入股票代码/名称/公告标题关键字…")
    with c2:
        if st.button("🔄 同步公告", key="ann_sync"):
            with st.spinner("正在同步公告…"):
                from scheduler import job_ifind_announce
                msg = job_ifind_announce()
            st.success(msg)
            st.rerun()

    # 应用筛选
    if kw:
        mask = (df["code"].str.contains(kw, case=False, na=False)
                | df["name"].str.contains(kw, case=False, na=False)
                | df["title"].str.contains(kw, case=False, na=False))
        df = df[mask]

    st.caption(f"共 {len(df)} 条公告（近7天）")

    # 分页
    total = len(df)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page_key = "page_ann"
    if page_key not in st.session_state:
        st.session_state[page_key] = 0
    page = st.session_state[page_key]
    if page >= total_pages:
        page = total_pages - 1
        st.session_state[page_key] = page

    start = page * PAGE_SIZE
    page_df = df.iloc[start:start + PAGE_SIZE]

    # 格式化显示
    display_df = pd.DataFrame()
    display_df["公告日期"] = page_df["report_date"].values
    display_df["股票代码"] = page_df["code"].values
    display_df["股票名称"] = page_df["name"].values
    display_df["公告标题"] = page_df["title"].values
    display_df["发布时间"] = page_df["ctime"].values
    display_df["PDF"] = page_df["pdf_url"].values
    display_df.insert(0, "序号", range(start + 1, start + 1 + len(display_df)))

    st.dataframe(
        display_df,
        column_config={
            "PDF": st.column_config.LinkColumn("PDF", display_text="查看"),
        },
        use_container_width=True, hide_index=True,
        height=35 * (len(display_df) + 1) + 3)

    # 分页导航
    nav1, nav2, nav3, nav4, nav5 = st.columns([1, 1, 2, 1, 1])
    with nav1:
        if st.button("◀ 上一页", key="prev_ann", disabled=(page <= 0)):
            st.session_state[page_key] = page - 1
            st.rerun()
    with nav2:
        if st.button("下一页 ▶", key="next_ann", disabled=(page >= total_pages - 1)):
            st.session_state[page_key] = page + 1
            st.rerun()
    with nav3:
        st.caption(f"共 {total} 条 · 第 {page + 1}/{total_pages} 页 · 每页 {PAGE_SIZE} 条")
    with nav4:
        jump = st.number_input("跳转", min_value=1, max_value=total_pages,
                               value=page + 1, key="jump_ann",
                               label_visibility="collapsed")
    with nav5:
        if st.button("跳转", key="go_ann"):
            st.session_state[page_key] = jump - 1
            st.rerun()

    # 导出当前筛选结果
    csv = df.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        label=f"📥 导出公告CSV（{total}条）",
        data=csv,
        file_name=f"公告信息_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key="dl_ann",
    )

    # ---------------------------------------------------------------- PDF 在线预览
    st.divider()
    st.subheader("📄 PDF 在线预览")
    opts = list(range(len(page_df)))
    sel = st.selectbox(
        "选择本页一条公告进行预览", opts, index=None,
        placeholder="点击选择公告…",
        format_func=lambda i: f"{start + i + 1}. {page_df.iloc[i]['name'] or page_df.iloc[i]['code']} "
                              f"{str(page_df.iloc[i]['title'])[:42]}",
        key="ann_pdf_sel")
    if sel is not None:
        row = page_df.iloc[sel]
        url = str(row["pdf_url"] or "")
        if not url.startswith("http"):
            st.warning("该公告没有有效的 PDF 链接")
        else:
            with st.spinner("加载 PDF…（首次约几秒，之后有缓存）"):
                b64 = _fetch_pdf_b64(url)
            if b64:
                import streamlit.components.v1 as components
                components.html(
                    f'<iframe src="data:application/pdf;base64,{b64}" '
                    f'width="100%" height="820px" style="border:1px solid #ddd;border-radius:6px;"></iframe>',
                    height=840)
                st.link_button("⬇️ 在新标签页打开/下载", url)
            else:
                st.error("PDF 加载失败（源站不可用或格式异常），可尝试新标签页打开")
                st.link_button("在新标签页打开", url)


render()
