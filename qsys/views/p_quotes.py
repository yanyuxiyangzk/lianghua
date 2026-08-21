"""📈 股票行情：腾讯快照批量行情表（本地落库，列可配，红涨绿跌）。

防白屏架构（真 AJAX 模式）：
  - quotefeed 后台线程按间隔抓快照写本地库（网络延迟与页面无关）
  - 页面 fragment 只读库渲染（<100ms），无任何网络请求，无过渡态
"""

import pandas as pd
import streamlit as st

import datasource
import quote_table as qt
from common import all_pools, load_watchlist, save_json, WATCHLIST_FILE
from quotefeed import get_feed

DEFAULT_COLS = ["seq", "code", "name", "chg_pct", "price", "chg", "bid1", "ask1",
                "volume", "amount_yi", "last_vol", "speed", "body_pct",
                "price_avg_diff", "turnover", "weibi"]


def _now_hm() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H%M")


def render():
    st.title("📈 股票行情")
    pools = all_pools()
    c1, c2, c3, c4, c5 = st.columns([2, 1.4, 1.1, 1.3, 1.1])
    with c1:
        pool_name = st.selectbox("股票池", list(pools.keys()), index=0, key="pq_pool")
    with c2:
        sort_by = st.selectbox("排序", list(qt.SORTABLE.keys()), index=0, key="pq_sort")
    with c3:
        order = st.radio("方向", ["降序", "升序"], horizontal=True, key="pq_order")
    with c4:
        in_session = "0915" <= _now_hm() <= "1505"
        live = st.toggle("🔄 实时行情", value=in_session, key="pq_live")
    with c5:
        collect_sec = st.selectbox("采集间隔", [5, 10, 30], index=1, key="pq_sec",
                                   format_func=lambda x: f"{x} 秒")

    with st.expander("⚙️ 显示列设置（点按增删，共 %d 列可选）" % len(qt.COLUMNS)):
        show_cols = st.pills("显示列", list(qt.COLUMNS.keys()), default=DEFAULT_COLS,
                             selection_mode="multi", format_func=lambda k: qt.COLUMNS[k][0],
                             key="pq_cols")

    codes = pools.get(pool_name) or []
    if len(codes) > 1000:
        st.warning(f"{pool_name} 共 {len(codes)} 只：批量抓取约 1-2 分钟。")

    feed = get_feed()
    feed_key = f"stocks:{pool_name}"
    b1, b3 = st.columns([1.2, 4.8])
    with b1:
        clicked = st.button("🔄 立即抓取一次", key="pq_once")
    fetch_status = b3.empty()  # 常驻占位：提示在按钮右侧同行出现/消失，页面不跳动

    if live:
        feed.stop_all_except(feed_key)
        feed.ensure(feed_key, codes, interval=collect_sec)
    else:
        feed.stop(feed_key)

    if clicked:
        fetch_status.markdown("⏳ 抓取中…")
        rows = datasource.get_batch_snapshots(codes)
        n = datasource.save_snapshots(rows)
        fetch_status.markdown(f"✅ {pd.Timestamp.now().strftime('%H:%M:%S')} 抓取 {n} 只")

    interval = "5s" if live else None
    body = st.fragment(_quotes_body, run_every=interval) if interval else _quotes_body
    body(codes, pool_name, sort_by, order == "降序", show_cols, feed_key)

    # ---- 行操作（在刷新区之外，不随刷新遮罩/重建）----
    options = st.session_state.get("pq_options", [])
    act = st.selectbox("选择股票操作（前50行）", options, index=None,
                       placeholder="选择一只…", key="pq_act")
    if act:
        code = act.split()[0]
        name = act.split(maxsplit=1)[1]
        b1, b2 = st.columns(2)
        with b1:
            if st.button("➕ 加入自选股", key="pq_add"):
                wl = load_watchlist()
                if code not in wl:
                    save_json(WATCHLIST_FILE, wl + [code])
                st.success(f"已加入自选股：{name}")
        with b2:
            if st.button("📉 打开专业K线", key="pq_open"):
                wl = load_watchlist()
                if code not in wl:
                    save_json(WATCHLIST_FILE, wl + [code])
                st.session_state["kp_preselect"] = code
                st.switch_page("views/p_kpro.py")


def _quotes_body(codes: list[str], pool_name: str, sort_by: str, desc: bool,
                 show_cols: list[str], feed_key: str):
    feed = get_feed()
    stt = feed.status(feed_key)
    rows, max_ts = datasource.get_latest_snapshots(codes)
    if not rows:
        if stt and stt.get("err"):
            st.error(f"后台采集出错：{stt['err']}")
        elif stt:
            st.info("📡 后台采集中，首批数据即将到来…（约 3-5 秒）")
        else:
            st.info("点击「立即抓取一次」或开启「实时行情」开始采集。")
        return

    with datasource._qconn() as c:
        total = c.execute("SELECT COUNT(*) FROM quote_snapshots").fetchone()[0]
    cap = f"数据源：腾讯行情快照 · 最新采集 **{max_ts}**"
    if stt:
        cap += f" · 📡 后台每{stt.get('interval', 10)}秒采集"
    trade_hm = max((r["trade_time"][8:12] for r in rows if r["trade_time"]), default="")
    if trade_hm:
        cap += f" · 行情时间 **{trade_hm[:2]}:{trade_hm[2:]}**"
    hm = _now_hm()
    if not ("0925" <= hm <= "1130" or "1300" <= hm <= "1500"):
        cap += "（⏸ 非交易时段，价格静止为正常现象）"
    st.caption(cap + f" · 本地库累计 {total} 行（quote_snapshots 表）")

    # 注意：fragment 每次重跑都是整体重执行，未重新输出的元素会被删除——
    # 所以这里【必须】每次都完整渲染表格（读库+生成 ~100ms，本就无感），
    # 不能做"数据没变就跳过重绘"的优化（会导致表格消失）。
    df = qt.build_table(rows, max_ts)
    df = df.sort_values(qt.SORTABLE[sort_by], ascending=not desc, na_position="last").reset_index(drop=True)
    df.insert(0, "seq", df.index + 1)
    show_cols = [c for c in show_cols if c in df.columns]
    if not show_cols:
        st.info("至少选择一列。")
        return
    view = df[show_cols]
    st.markdown(qt.html_table(view, show_cols, height=620), unsafe_allow_html=True)
    # 把前50行选项写到 session，供刷新区外的操作控件使用
    st.session_state["pq_options"] = [f"{r['code']} {r['name']}" for _, r in view.head(50).iterrows()]


render()
