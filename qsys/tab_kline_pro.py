"""📉 专业K线 tab：同花顺风格技术分析终端（日线/周线/月线）。

版式参考同花顺分时页：深色底、红涨绿跌、MA 均线组、量能副图、指标副图、
顶部行情信息栏（含涨停/跌停价、量比、成交对比）。
数据边界：当前仅日线数据（qlib cn_data），周/月线由日线重采样。
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import datasource
from common import get_instruments, get_last_trade_day, get_ohlcv, init_qlib, load_watchlist, save_json, WATCHLIST_FILE

RED, GREEN = "#ef5350", "#26a69a"      # 红涨 / 绿跌
MA_COLORS = {5: "#ffffff", 10: "#ffd54f", 20: "#ba68c8", 60: "#4db6ac"}
BG, GRID = "#101010", "#2a2a2a"


# ---------------------------------------------------------------- 指标计算
def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = df.resample(rule).agg({"$open": "first", "$high": "max", "$low": "min",
                                 "$close": "last", "$volume": "sum", "$amount": "sum"})
    return agg.dropna(subset=["$close"])


def add_ma(df: pd.DataFrame, windows=(5, 10, 20, 60)) -> pd.DataFrame:
    for w in windows:
        df[f"MA{w}"] = df["$close"].rolling(w).mean()
    return df


def calc_macd(df: pd.DataFrame):
    ema12 = df["$close"].ewm(span=12, adjust=False).mean()
    ema26 = df["$close"].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea, 2 * (dif - dea)


def calc_kdj(df: pd.DataFrame, n: int = 9):
    low_n = df["$low"].rolling(n).min()
    high_n = df["$high"].rolling(n).max()
    rsv = (df["$close"] - low_n) / (high_n - low_n + 1e-12) * 100
    k = rsv.ewm(com=2, adjust=False).mean()   # SMA(3,1) 等价 ewm(com=2)
    d = k.ewm(com=2, adjust=False).mean()
    return k, d, 3 * k - 2 * d


def calc_rsi(df: pd.DataFrame, windows=(6, 12, 24)):
    delta = df["$close"].diff()
    out = {}
    for w in windows:
        up = delta.clip(lower=0).ewm(alpha=1 / w, adjust=False).mean()
        dn = (-delta.clip(upper=0)).ewm(alpha=1 / w, adjust=False).mean()
        out[w] = 100 * up / (up + dn + 1e-12)
    return out


def limit_prices(prev_close: float, code: str) -> tuple[float, float]:
    """按板块规则估算涨/跌停价（ST 标记数据缺失，±5% 情形不覆盖）。"""
    if code.startswith(("SH68", "SZ30")):      # 科创板/创业板
        pct = 0.20
    elif code.startswith(("BJ", "SH43", "SH83")):  # 北交所等
        pct = 0.30
    else:
        pct = 0.10
    return round(prev_close * (1 + pct), 2), round(prev_close * (1 - pct), 2)


# ---------------------------------------------------------------- 分时·竞价视图（腾讯展示通道）
@st.cache_data(ttl=20)  # 短缓存：盘中刷新频率高，腾讯推送本身约3秒级
def _minute_cached(code: str) -> dict:
    return datasource.get_minute_today(code)


def _now_sh():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _auction_card_html(code: str, name: str, date_str: str, price: float, prev: float,
                       vol_shou: float | None, amount: float | None, turnover, ratio,
                       live_note: str) -> str:
    a_pct = (price / prev - 1) * 100 if prev else 0
    ac = RED if a_pct >= 0 else GREEN

    def _fmt(v, unit="", nd=2):
        return "—" if v is None else f"{v:.{nd}f}{unit}"

    return (
        f"<div style='background:{BG};padding:10px 14px;border-radius:6px;font-family:monospace'>"
        f"<span style='color:#ccc;font-size:15px'><b>{name} {code}</b> 集合竞价 · {date_str}</span>　"
        f"<span style='color:#ffd54f;font-size:12px'>{live_note}</span><br>"
        f"<span style='color:#999'>匹配价</span> <span style='color:{ac};font-size:18px'><b>{price:.2f}</b></span>　"
        f"<span style='color:#999'>竞价涨幅</span> <span style='color:{ac}'>{a_pct:+.2f}%</span>　"
        f"<span style='color:#999'>匹配量</span> <span style='color:#ffd54f'>{_fmt(vol_shou, ' 手', 0)}</span><br>"
        f"<span style='color:#999'>匹配金额</span> <span style='color:#ffd54f'>{_fmt(amount / 1e4 if amount else None, ' 万')}</span>　"
        f"<span style='color:#999'>竞价换手</span> <span style='color:#ccc'>{_fmt(turnover, '%', 3)}</span>　"
        f"<span style='color:#999'>竞昨比</span> <span style='color:#ccc'>{_fmt(ratio, '%')}</span>　"
        f"<span style='color:#999'>未匹配量</span> <span style='color:#666'>—（L2）</span>"
        f"</div>")


def _prev_day_volume_shou(code: str, ref_date: str) -> float | None:
    """昨日成交量（手），用于竞昨比。"""
    try:
        dstart = (pd.Timestamp(ref_date) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        dend = (pd.Timestamp(ref_date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        dprev = datasource.get_daily(code, dstart, dend)
        if dprev.empty:
            return None
        v = dprev["$volume"].iloc[-1]
        return v / 100 if datasource.get_source() == "akshare" else v  # akshare=股，qlib=手
    except Exception:
        return None


def _render_auction_live(code: str, hm: str):
    """竞价时段直播卡：腾讯快照 3 秒级轮询。

    实测结论（2026-08-14 竞价时段验证）：腾讯/新浪免费通道在 9:15-9:25 不提供
    虚拟匹配价/量（price=昨收、volume=0）——虚拟匹配属 L2 增强行情。
    因此竞价时段展示"等待撮合"卡并保持 3 秒轮询，9:25:01 成交点一出现即自动呈现。
    """
    try:
        snap = ds.get_realtime_snapshot(code)
    except Exception as e:
        st.warning(f"竞价快照获取失败：{e}")
        return
    prev = snap["prev_close"]
    is_virtual = snap["price"] and prev and snap["price"] != prev and (snap["volume"] or 0) > 0

    if is_virtual:
        # 通道若有虚拟匹配数据（部分标的/行情档位可能提供），按直播卡展示
        outstanding = ds.get_latest_outstanding(code)
        vol_shou = snap["volume"]
        amount = vol_shou * 100 * snap["price"] if vol_shou else None
        turnover = vol_shou * 100 / outstanding * 100 if (outstanding and vol_shou) else None
        pv = _prev_day_volume_shou(code, _now_sh().strftime("%Y%m%d"))
        ratio = vol_shou / pv * 100 if (vol_shou and pv) else None
        rule_note = "9:20前可撤单，匹配量可能失真" if hm < "0920" else "9:20后不可撤单，匹配量真实"
        st.markdown(_auction_card_html(code, snap["name"], _now_sh().strftime("%Y-%m-%d"),
                                       snap["price"], prev, vol_shou, amount, turnover, ratio,
                                       f"🔴 竞价直播（每3秒刷新）· {rule_note} · {snap['time'][8:]}"),
                    unsafe_allow_html=True)
    else:
        st.markdown(
            f"<div style='background:{BG};padding:12px 14px;border-radius:6px;font-family:monospace'>"
            f"<span style='color:#ccc;font-size:15px'><b>{snap['name']} {code}</b> 集合竞价进行中…</span>　"
            f"<span style='color:#ffd54f'>⏳ 每3秒轮询，9:25 撮合后即刻呈现成交信息</span><br>"
            f"<span style='color:#999'>昨收</span> <span style='color:#ccc'>{prev or '—'}</span>　"
            f"<span style='color:#999'>当前时间</span> <span style='color:#ccc'>{_now_sh().strftime('%H:%M:%S')}</span><br>"
            f"<span style='color:#666;font-size:12px'>说明：9:15–9:25 的虚拟匹配价/量为交易所增强行情（L2），"
            f"免费通道不推送；9:20 前委托可撤、之后不可撤。</span></div>",
            unsafe_allow_html=True)


def _render_fenshi(code: str):
    """分时·竞价视图入口：st.fragment 局部刷新（页面其余部分不重跑，防闪屏）。"""
    now = _now_sh()
    hm = now.strftime("%H%M")
    is_weekday = now.weekday() < 5
    in_auction = is_weekday and "0915" <= hm < "0925"
    in_session = is_weekday and "0915" <= hm <= "1500"

    refresh_on = st.toggle("🔄 实时刷新", value=in_session, key="kp_live",
                           help="竞价时段每3秒、盘中每30秒局部刷新；非交易时段默认关闭")
    interval = None
    if refresh_on:
        interval = "3s" if in_auction else ("30s" if in_session else None)

    # 用 fragment 包住动态区：只有这块按 interval 重跑，整页不闪
    body = st.fragment(_fenshi_body, run_every=interval) if interval else _fenshi_body
    body(code, hm, in_auction, in_session, refresh_on)


def _fenshi_body(code: str, hm: str, in_auction: bool, in_session: bool, refresh_on: bool):
    if in_auction:
        _render_auction_live(code, hm)
        return

    try:
        data = _minute_cached(code)
    except Exception as e:
        st.warning(f"分时数据获取失败：{e}")
        return
    m, prev = data["minutes"], data["prev_close"]
    if m.empty or prev is None:
        st.info("无当日分时数据（非交易日或停牌）。")
        return
    m = m.reset_index(drop=True)
    vwap = m["cum_amount"] / (m["volume"] * 100).replace(0, np.nan)  # 累计额/(累计手×100)
    pct = (m["price"] / prev - 1) * 100

    # ---- 集合竞价信息卡（首行 09:30 = 竞价成交） ----
    a = m.iloc[0]
    outstanding = datasource.get_latest_outstanding(code)
    a_turnover = a["volume"] * 100 / outstanding * 100 if outstanding else None
    prev_vol_shou = _prev_day_volume_shou(code, data["date"])
    a_ratio = a["volume"] / prev_vol_shou * 100 if prev_vol_shou else None
    st.markdown(_auction_card_html(code, data["name"], data["date"], a["price"], prev,
                                   a["volume"], a["cum_amount"], a_turnover, a_ratio,
                                   "竞价已成交 · 09:25:01" + (" · 实时刷新中" if refresh_on and in_session else "")),
                unsafe_allow_html=True)

    # ---- 分时主图（白=价格 黄=均价，右轴涨跌幅） ----
    x = m["time"].str[:2] + ":" + m["time"].str[2:]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25],
                        vertical_spacing=0.02, specs=[[{"secondary_y": True}], [{}]])
    fig.add_trace(go.Scatter(x=x, y=m["price"], line=dict(color="#ffffff", width=1.2),
                             name="价格"), row=1, col=1, secondary_y=False)
    fig.add_trace(go.Scatter(x=x, y=vwap, line=dict(color="#ffd54f", width=1.2),
                             name="均价"), row=1, col=1, secondary_y=False)
    fig.add_hline(y=prev, line=dict(color="#888", width=0.8, dash="dot"), row=1, col=1, secondary_y=False)
    # 右轴百分比（与左轴价格同步映射）
    pmin, pmax = float(m["price"].min()), float(m["price"].max())
    pad = (pmax - pmin) * 0.05 + 1e-9
    fig.update_yaxes(range=[pmin - pad, pmax + pad], row=1, col=1, secondary_y=False,
                     gridcolor=GRID, side="left")
    fig.update_yaxes(range=[(pmin - pad) / prev * 100 - 100, (pmax + pad) / prev * 100 - 100],
                     row=1, col=1, secondary_y=True, ticksuffix="%", gridcolor=GRID, side="right")
    vol_colors = [RED if p >= (m["price"].iloc[i - 1] if i else prev) else GREEN
                  for i, p in enumerate(m["price"])]
    fig.add_trace(go.Bar(x=x, y=m["minute_vol"], marker_color=vol_colors, name="量(手)",
                         showlegend=False), row=2, col=1)
    fig.update_layout(template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG, height=640,
                      margin=dict(l=8, r=8, t=8, b=8), hovermode="x unified",
                      legend=dict(orientation="h", y=1.02),
                      uirevision="kp_fenshi")  # 局部刷新时保留缩放/平移，防跳变
    fig.update_xaxes(gridcolor=GRID)
    fig.update_yaxes(gridcolor=GRID, row=2, col=1)
    st.plotly_chart(fig, width='stretch')
    st.caption("说明：分钟线首点（09:30）即集合竞价成交；未匹配量等盘口明细属 Level-2 数据，免费通道不提供。")



# ---------------------------------------------------------------- 竞价撮合视图（easy-tdx 逐笔）
@st.cache_data(ttl=120)
def _ticks_cached(code: str):
    return datasource.get_ticks_tdx(code)


def _render_auction_match(code: str):
    """竞价撮合图：9:15-9:25 虚拟匹配价轨迹 + 撮合价/量 + 开盘后逐笔方向。
    数据源：easy-tdx 逐笔成交（buyorsell=8 为竞价段）。"""
    try:
        tk = _ticks_cached(code)
    except Exception as e:
        st.warning(f"逐笔数据获取失败：{e}")
        return
    if tk.empty:
        st.info("无当日逐笔数据（非交易日或标的未交易）。")
        return
    tk = tk.copy()
    tk["hhmm"] = tk["datetime"].astype(str).str[11:16]
    auction = tk[tk["buyorsell"] == 8].copy()
    match = tk[(tk["hhmm"] == "09:25") & (tk["buyorsell"].isin([8, 2]))]
    open_ticks = tk[(tk["hhmm"] > "09:25") & (tk["buyorsell"] != 8)].copy()

    if auction.empty:
        st.info("今日无竞价段数据。")
        return
    trail = auction.groupby("hhmm").last().reset_index()
    m_price = float(match["price"].iloc[-1]) if not match.empty else float(trail["price"].iloc[-1])
    m_vol = float(match["vol"].iloc[-1]) if not match.empty else 0.0
    pmin, pmax = float(trail["price"].min()), float(trail["price"].max())
    up_color = RED if (trail["price"].iloc[-1] >= trail["price"].iloc[0]) else GREEN

    st.markdown(
        f"<div style='background:{BG};padding:10px 14px;border-radius:6px;font-family:monospace'>"
        f"<span style='color:#ccc;font-size:15px'><b>{code}</b> 竞价撮合 · {str(tk['datetime'].iloc[0])[:10]}</span>"
        f"<span style='color:#888;font-size:12px'>（数据源：easy-tdx 逐笔）</span><br>"
        f"<span style='color:#999'>撮合价</span> <span style='color:{up_color};font-size:18px'><b>{m_price:.2f}</b></span>　"
        f"<span style='color:#999'>撮合量</span> <span style='color:#ffd54f'>{m_vol:.0f} 手</span>　"
        f"<span style='color:#999'>竞价区间</span> <span style='color:#ccc'>{pmin:.2f} ~ {pmax:.2f}</span>　"
        f"<span style='color:#999'>轨迹点数</span> <span style='color:#ccc'>{len(trail)}</span>"
        f"</div>", unsafe_allow_html=True)

    fig = go.Figure()
    x = trail["hhmm"]
    fig.add_trace(go.Scatter(x=x, y=trail["price"], mode="lines+markers", name="虚拟匹配价",
                             line=dict(color="#ffffff", width=1.4), marker=dict(size=4)))
    fig.add_trace(go.Scatter(x=[x.iloc[0], "09:25"], y=[m_price, m_price], mode="lines",
                             name="撮合价", line=dict(color="#ffd54f", dash="dash", width=1)))
    if not open_ticks.empty:
        ot = open_ticks.head(40)
        colors = ot["buyorsell"].map({0: RED, 1: GREEN, 2: "#888888"}).tolist()
        fig.add_trace(go.Scatter(x=ot["hhmm"], y=ot["price"], mode="markers", name="开盘后逐笔",
                                 marker=dict(color=colors, size=5, opacity=0.7)))
    fig.update_layout(template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG, height=460,
                      margin=dict(l=8, r=8, t=30, b=8), hovermode="x unified",
                      title="竞价撮合轨迹（9:15-9:25 虚拟匹配价 → 9:25 撮合 → 9:30 逐笔）",
                      legend=dict(orientation="h"))
    fig.update_xaxes(gridcolor=GRID)
    fig.update_yaxes(gridcolor=GRID, side="right")
    st.plotly_chart(fig, width='stretch')

    st.markdown("**竞价段逐笔明细**")
    disp = trail[["hhmm", "price"]].rename(columns={"hhmm": "时间", "price": "匹配价"})
    st.dataframe(disp, width='stretch', height=200, hide_index=True)
    st.caption("口径：easy-tdx 逐笔 buyorsell=8 为竞价段；9:25 撮合一笔；9:30 起为正常逐笔（红买绿卖灰中性）。")


def render():
    st.subheader("📉 专业K线")
    try:
        init_qlib()
    except Exception as e:
        st.error(f"Qlib 初始化失败：{e}")
        return

    watchlist = load_watchlist()
    instruments = get_instruments()

    # ---- 选股 + 参数行 ----
    c1, c2, c3, c4, c5 = st.columns([2, 1.2, 1.2, 1.2, 1.4])
    with c1:
        src = st.radio("来源", ["自选股", "全市场搜索"], horizontal=True, label_visibility="collapsed", key="kp_src")
        if src == "自选股":
            if not watchlist:
                st.warning("自选股为空，请用全市场搜索添加")
                return
            pre = st.session_state.pop("kp_preselect", None)  # 板块行情页联动跳转
            code = st.selectbox("自选股", watchlist, index=watchlist.index(pre) if pre in watchlist else 0,
                                label_visibility="collapsed", key="kp_wl")
        else:
            code = st.selectbox("全市场", instruments, index=None,
                                placeholder="输入代码搜索…", label_visibility="collapsed", key="kp_all")
            if code and code not in watchlist and st.button("➕ 加自选", key="kp_add"):
                save_json(WATCHLIST_FILE, watchlist + [code])
                st.rerun()
    with c2:
        period = st.radio("周期", ["日", "周", "月"], horizontal=True, key="kp_period")
    with c3:
        indicator = st.selectbox("副图指标", ["MACD", "KDJ", "RSI"], key="kp_ind")
    with c4:
        ma_on = st.multiselect("均线", [5, 10, 20, 60], default=[5, 10, 20, 60],
                               format_func=lambda x: f"MA{x}", key="kp_ma")
    with c5:
        span = st.select_slider("显示根数", [120, 250, 500, 750], value=250, key="kp_span")

    if not code:
        st.info("选择一只标的开始。")
        return

    view = st.radio("视图", ["K线", "分时·竞价", "竞价撮合"], horizontal=True, key="kp_view")
    if view == "分时·竞价":
        _render_fenshi(code)
        return
    if view == "竞价撮合":
        _render_auction_match(code)
        return

    # ---- 数据 ----
    end = get_last_trade_day()
    need = int(span * ({"日": 1, "周": 5, "月": 22}[period]) * 1.6) + 120
    start = (pd.Timestamp(end) - pd.Timedelta(days=need)).strftime("%Y-%m-%d")
    raw = get_ohlcv(code, start, end)
    if raw.empty:
        st.warning("该标的选择区间内无数据。")
        return
    df = resample_ohlcv(raw, {"日": "D", "周": "W", "月": "M"}[period]) if period != "日" else raw
    if period == "日":
        df = df.resample("D").agg({"$open": "first", "$high": "max", "$low": "min",
                                   "$close": "last", "$volume": "sum", "$amount": "sum"}).dropna(subset=["$close"])
    df = add_ma(df).dropna(subset=["$open", "$high", "$low", "$close"])
    if len(df) < 30:
        st.warning("K线根数不足。")
        return
    prev_close = df["$close"].iloc[-2] if period == "日" and len(df) >= 2 else df["$close"].iloc[-1]
    view = df.tail(span)

    # ---- 顶部信息栏（仿终端） ----
    last = view.iloc[-1]
    chg = last["$close"] - prev_close
    chg_pct = chg / prev_close * 100
    up_limit, dn_limit = limit_prices(prev_close, code)
    vol5 = df["$volume"].tail(6).iloc[:-1].mean() if len(df) > 6 else df["$volume"].mean()
    liangbi = last["$volume"] / (vol5 + 1e-12)
    amp = (last["$high"] - last["$low"]) / prev_close * 100
    amt_yi = last["$amount"] / 1e8 if last.get("$amount") else 0
    color = RED if chg >= 0 else GREEN
    st.markdown(
        f"<div style='background:{BG};padding:10px 14px;border-radius:6px;font-family:monospace'>"
        f"<span style='color:#ccc;font-size:16px'><b>{code}</b>（{period}线）</span>&nbsp;&nbsp;"
        f"<span style='color:{color};font-size:20px'><b>{last['$close']:.2f}</b></span>&nbsp;"
        f"<span style='color:{color}'>{chg:+.2f} {chg_pct:+.2f}%</span><br>"
        f"<span style='color:#999'>今开</span> <span style='color:{RED if last['$open']>=prev_close else GREEN}'>{last['$open']:.2f}</span>&nbsp;"
        f"<span style='color:#999'>最高</span> <span style='color:{RED}'>{last['$high']:.2f}</span>&nbsp;"
        f"<span style='color:#999'>最低</span> <span style='color:{GREEN}'>{last['$low']:.2f}</span>&nbsp;"
        f"<span style='color:#999'>成交额</span> <span style='color:#ffd54f'>{amt_yi:.2f}亿</span>&nbsp;"
        f"<span style='color:#999'>振幅</span> <span style='color:#ccc'>{amp:.2f}%</span>&nbsp;"
        f"<span style='color:#999'>量比</span> <span style='color:#ccc'>{liangbi:.2f}</span>&nbsp;"
        f"<span style='color:#f44'>涨停 {up_limit}</span>&nbsp;<span style='color:#4c4'>跌停 {dn_limit}</span>"
        f"</div>",
        unsafe_allow_html=True)

    # ---- 主图 + 量能 + 指标 三联副图 ----
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.58, 0.20, 0.22],
                        vertical_spacing=0.015)
    x = view.index
    fig.add_trace(go.Candlestick(
        x=x, open=view["$open"], high=view["$high"], low=view["$low"], close=view["$close"],
        increasing_line_color=RED, increasing_fillcolor=RED,
        decreasing_line_color=GREEN, decreasing_fillcolor=GREEN,
        name="K线", showlegend=False), row=1, col=1)
    for w in ma_on:
        fig.add_trace(go.Scatter(x=x, y=view[f"MA{w}"], name=f"MA{w}",
                                 line=dict(color=MA_COLORS[w], width=1)), row=1, col=1)
    # 前收参考线
    fig.add_hline(y=prev_close, line=dict(color="#888", width=0.8, dash="dot"), row=1, col=1)

    vol_colors = [RED if c >= o else GREEN for c, o in zip(view["$close"], view["$open"])]
    fig.add_trace(go.Bar(x=x, y=view["$volume"] / 1e4, marker_color=vol_colors,
                         name="成交量(万)", showlegend=False), row=2, col=1)
    if len(view) >= 10:
        mv5 = view["$volume"].rolling(5).mean() / 1e4
        mv10 = view["$volume"].rolling(10).mean() / 1e4
        fig.add_trace(go.Scatter(x=x, y=mv5, line=dict(color="#ffffff", width=0.8),
                                 name="MV5", showlegend=False), row=2, col=1)
        fig.add_trace(go.Scatter(x=x, y=mv10, line=dict(color="#ffd54f", width=0.8),
                                 name="MV10", showlegend=False), row=2, col=1)

    if indicator == "MACD":
        dif, dea, macd = calc_macd(view)
        bar_colors = [RED if v >= 0 else GREEN for v in macd]
        fig.add_trace(go.Bar(x=x, y=macd, marker_color=bar_colors, showlegend=False), row=3, col=1)
        fig.add_trace(go.Scatter(x=x, y=dif, line=dict(color="#ffffff", width=1), name="DIF"), row=3, col=1)
        fig.add_trace(go.Scatter(x=x, y=dea, line=dict(color="#ffd54f", width=1), name="DEA"), row=3, col=1)
    elif indicator == "KDJ":
        k, d, j = calc_kdj(view)
        fig.add_trace(go.Scatter(x=x, y=k, line=dict(color="#ffffff", width=1), name="K"), row=3, col=1)
        fig.add_trace(go.Scatter(x=x, y=d, line=dict(color="#ffd54f", width=1), name="D"), row=3, col=1)
        fig.add_trace(go.Scatter(x=x, y=j, line=dict(color="#ba68c8", width=1), name="J"), row=3, col=1)
    else:
        for w, c in zip((6, 12, 24), ("#ffffff", "#ffd54f", "#ba68c8")):
            fig.add_trace(go.Scatter(x=x, y=calc_rsi(view)[w], line=dict(color=c, width=1),
                                     name=f"RSI{w}"), row=3, col=1)

    fig.update_layout(
        template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG, height=760,
        margin=dict(l=8, r=8, t=8, b=8), xaxis_rangeslider_visible=False,
        hovermode="x unified", legend=dict(orientation="h", y=1.02, font=dict(size=10)),
        uirevision="kp_kline",
    )
    fig.update_xaxes(gridcolor=GRID, showgrid=True)
    fig.update_yaxes(gridcolor=GRID, showgrid=True, side="right")
    st.plotly_chart(fig, width='stretch')

    # ---- 成交对比（仿图底部） ----
    if len(view) >= 2 and view["$amount"].iloc[-1] > 0:
        today_amt = view["$amount"].iloc[-1] / 1e8
        yest_amt = view["$amount"].iloc[-2] / 1e8
        rate = (today_amt - yest_amt) / (yest_amt + 1e-12) * 100
        rc = RED if rate >= 0 else GREEN
        st.markdown(
            f"<div style='background:{BG};padding:6px 14px;font-family:monospace'>"
            f"<span style='color:#999'>成交对比：</span>"
            f"<span style='color:#ffd54f'>本期总额 {today_amt:.2f}亿</span> ｜ "
            f"<span style='color:#ccc'>上期总额 {yest_amt:.2f}亿</span> ｜ "
            f"<span style='color:{rc}'>变化率 {rate:+.2f}%</span></div>",
            unsafe_allow_html=True)

    st.caption("说明：当前为日线级数据（周/月线由日线重采样）；图中「分时/竞价」类信息需分钟级数据源，"
               "如需真实分时图可后续接入 akshare/tushare 分钟数据。涨跌停价按板块规则估算，未覆盖 ST ±5% 情形。")
