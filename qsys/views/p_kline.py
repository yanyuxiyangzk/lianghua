"""🕯️ 自选K线：自选股日K与成交量。"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import (QLIB_DATA_DIR, WATCHLIST_FILE, get_instruments, get_ohlcv, init_qlib,
                    load_watchlist, save_json, get_data_source)
import datasource

st.title("🕯️ 自选K线")

try:
    init_qlib()
    instruments = get_instruments()
except Exception as e:
    instruments = []
    st.error(f"数据初始化失败：{e}")

if instruments:
    watchlist = load_watchlist()
    col_sel, col_add = st.columns([3, 1])
    with col_sel:
        picked = st.selectbox("自选股", watchlist, index=0)
    with col_add:
        add = st.selectbox("添加自选", instruments, index=None, placeholder="输入代码搜索…")
        if add and add not in watchlist:
            save_json(WATCHLIST_FILE, watchlist + [add])
            st.rerun()
    rm = st.multiselect("移除自选", watchlist)
    if rm:
        save_json(WATCHLIST_FILE, [c for c in watchlist if c not in rm])
        st.rerun()

    cal_path = QLIB_DATA_DIR / "calendars" / "day.txt"
    d0, d1 = "2018-01-01", "2026-12-31"
    if cal_path.exists():
        lines = cal_path.read_text().splitlines()
        d0, d1 = lines[0], lines[-1].strip()
    rng = st.date_input("区间", value=(pd.Timestamp(d1) - pd.Timedelta(days=365), pd.Timestamp(d1)),
                        min_value=pd.Timestamp(d0), max_value=pd.Timestamp(d1))
    if picked and isinstance(rng, tuple) and len(rng) == 2:
        src = get_data_source()
        df = get_ohlcv(picked, str(rng[0]), str(rng[1]), source=src)
        if df.empty:
            st.warning("该区间无数据（停牌或未上市）。")
        else:
            st.caption(f"数据源：{datasource.SOURCES[src]['name']}")
            fig = go.Figure(data=[go.Candlestick(
                x=df.index, open=df["$open"], high=df["$high"], low=df["$low"], close=df["$close"],
                name=picked, increasing_line_color="#e54545", decreasing_line_color="#2ca02c")])
            fig.update_layout(height=520, xaxis_rangeslider_visible=False,
                              margin=dict(l=10, r=10, t=30, b=10),
                              title=f"{picked} 日K（{df.index[0].date()} ~ {df.index[-1].date()}）",
                              uirevision="pk_kline")
            st.plotly_chart(fig, width='stretch')

            vfig = go.Figure(go.Bar(x=df.index, y=df["$volume"] / 1e4, name="成交量(万)"))
            vfig.update_layout(height=160, margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(vfig, width='stretch')

            st.dataframe(df.sort_index(ascending=False).head(20).style.format("{:.2f}"), width='stretch')
