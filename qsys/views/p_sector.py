"""🏛️ 板块行情：指数 / ETF 基金行情（可编辑清单，本地落库）。

与股票行情同架构：后台采集线程写库，页面只读库渲染（无网络请求、无白屏）。
"""

import re

import pandas as pd
import streamlit as st

import datasource
import quote_table as qt
from common import DATA_DIR, load_json, save_json
from quotefeed import get_feed

POOLS_FILE = DATA_DIR / "sector_pools.json"

DEFAULT_SECTORS = {
    "主要指数": ["SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905",
               "SH000852", "SH000016", "SH000688", "SH000985", "SZ399303"],
    "ETF基金": ["SH510300", "SH510500", "SH588000", "SH512880", "SH512000",
              "SH512800", "SH512660", "SH512010", "SH512690", "SH512480",
              "SH515030", "SZ159915", "SZ159949", "SH513050", "SH513100",
              "SH513500", "SZ159920", "SH518880", "SH510880", "SH511880"],
}
DEFAULT_COLS = ["seq", "code", "name", "chg_pct", "price", "chg", "volume",
                "amount_yi", "speed", "turnover", "amplitude", "quantity_ratio"]


def _now_hm() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H%M")


def _load_pools() -> dict:
    return load_json(POOLS_FILE, {k: list(v) for k, v in DEFAULT_SECTORS.items()})


def render():
    st.title("🏛️ 板块行情（指数 / ETF）")
    pools = _load_pools()

    c1, c2, c3, c4, c5 = st.columns([2, 1.4, 1.1, 1.3, 1.1])
    with c1:
        pool_name = st.selectbox("清单", list(pools.keys()), index=0, key="ps_pool")
    with c2:
        sort_by = st.selectbox("排序", list(qt.SORTABLE.keys()), index=0, key="ps_sort")
    with c3:
        order = st.radio("方向", ["降序", "升序"], horizontal=True, key="ps_order")
    with c4:
        in_session = "0915" <= _now_hm() <= "1505"
        live = st.toggle("🔄 实时行情", value=in_session, key="ps_live")
    with c5:
        collect_sec = st.selectbox("采集间隔", [5, 10, 30], index=1, key="ps_sec",
                                   format_func=lambda x: f"{x} 秒")

    with st.expander("✏️ 编辑清单（添加/移除指数或ETF代码）"):
        ec1, ec2 = st.columns(2)
        with ec1:
            new_code = st.text_input("添加代码（如 SH510300 / SZ399006 / SH000688）", key="ps_new").strip().upper()
            if st.button("➕ 添加", key="ps_add"):
                if re.match(r"^(SH|SZ|BJ)\d{6}$", new_code):
                    if new_code not in pools[pool_name]:
                        pools[pool_name].append(new_code)
                        save_json(POOLS_FILE, pools)
                        st.success(f"已加入 {new_code}")
                        st.rerun()
                    else:
                        st.info("已在清单中")
                else:
                    st.error("代码格式应为 交易所前缀+6位数字，如 SH510300")
        with ec2:
            rm = st.multiselect("移除代码", pools[pool_name], key="ps_rm")
            if rm:
                pools[pool_name] = [c for c in pools[pool_name] if c not in rm]
                save_json(POOLS_FILE, pools)
                st.rerun()

    codes = pools.get(pool_name) or []
    if not codes:
        st.info("清单为空，请在上方添加代码。")
        return

    feed = get_feed()
    feed_key = f"sector:{pool_name}"
    b1, b3 = st.columns([1.2, 4.8])
    with b1:
        clicked = st.button("🔄 立即抓取一次", key="ps_once")
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
        fetch_status.markdown(f"✅ {pd.Timestamp.now().strftime('%H:%M:%S')} 抓取 {n} 条")

    with st.expander("⚙️ 显示列设置（点按增删）"):
        show_cols = st.pills("显示列", list(qt.COLUMNS.keys()), default=DEFAULT_COLS,
                             selection_mode="multi", format_func=lambda k: qt.COLUMNS[k][0],
                             key="ps_cols")
    interval = "5s" if live else None
    body = st.fragment(_sector_body, run_every=interval) if interval else _sector_body
    body(codes, pool_name, sort_by, order == "降序", show_cols, feed_key)


def _sector_body(codes: list[str], pool_name: str, sort_by: str, desc: bool,
                 show_cols: list[str], feed_key: str):
    feed = get_feed()
    stt = feed.status(feed_key)
    rows, max_ts = datasource.get_latest_snapshots(codes)
    if not rows:
        if stt:
            st.info("📡 后台采集中，首批数据即将到来…")
        else:
            st.info("点击「立即抓取一次」或开启「实时行情」开始采集。")
        return
    cap = f"数据源：腾讯行情快照 · 最新采集 **{max_ts}**"
    trade_hm = max((r["trade_time"][8:12] for r in rows if r["trade_time"]), default="")
    if trade_hm:
        cap += f" · 行情时间 **{trade_hm[:2]}:{trade_hm[2:]}**"
    hm = _now_hm()
    if not ("0925" <= hm <= "1130" or "1300" <= hm <= "1500"):
        cap += "（⏸ 非交易时段，价格静止为正常现象）"
    st.caption(cap + " · 本地库 quote_snapshots 表")

    # 同股票行情页：fragment 重跑必须每次完整渲染，不能跳过重绘
    df = qt.build_table(rows, max_ts)
    df = df.sort_values(qt.SORTABLE[sort_by], ascending=not desc, na_position="last").reset_index(drop=True)
    df.insert(0, "seq", df.index + 1)
    show_cols = [c for c in show_cols if c in df.columns]
    if not show_cols:
        return
    view = df[show_cols]
    st.markdown(qt.html_table(view, show_cols, height=560), unsafe_allow_html=True)
    st.caption("说明：指数为行情参考（无五档/换手语义时显示—）；ETF 与股票同规则。"
               "指数/ETF 的K线暂不支持（K线数据为股票日线），需要可加 akshare 基金通道。")


render()
