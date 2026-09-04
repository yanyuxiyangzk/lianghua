"""🚀 量化驾驶舱：一屏总览（深色主题，设计参照 docs/quant-dashboard-v2.svg）。

数据全部真实接线（每块数据独立 try/except，缺数据优雅降级，绝不白屏）：
  账户卡片  ← broker.get_account()（模拟盘资金账户）
  策略信号  ← experience 今日选股名单（较昨日增量）
  组合净值  ← experience 已平仓持仓累计净值曲线（无数据回退沪深300指数）
  实时信号  ← 今日选股名单明细（无名单则展示当前持仓提醒）
  市场概览  ← 腾讯快照 5 大指数实时行情
  市场宽度  ← sector_daily 全行业上涨/下跌家数（定时落库）
  底部状态  ← 行情快照时间 / iFinD 同步 / 账户状态
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import broker
import datasource
import experience
import sectorflow
from common import get_last_trade_day

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

# ---------------------------------------------------------------- 主题（跟随全局浅色/深色）
import theme as _app_theme

_PALETTES = {
    "dark": dict(
        blue="#2678ff", red="#f04444", green="#28d7a0",
        title="#f3f7fb", date="#73859a",
        card="linear-gradient(135deg, #172332 0%, #101923 100%)",
        card_border="#223243", kpi_label="#708195", kpi_value="#f3f7fb", kpi_sub="#65778a",
        flat="#708195",
        badge_bg="rgba(40,215,160,.12)", badge_border="rgba(40,215,160,.4)", badge_fg="#28d7a0",
        th="#65778a", th_border="#223243", td="#dbe5ef", td_border="#16232f",
        tag_buy="rgba(240,68,68,.15)", tag_hold="rgba(38,120,255,.15)",
        tag_watch="rgba(240,180,40,.15)", tag_sell="rgba(40,215,160,.15)",
        status="#65778a", status_b="#9aaabd",
        plotly="plotly_dark", axis_grid="rgba(34,50,67,.4)", bar_text="#9aaabd",
    ),
    "light": dict(
        blue="#1a6bff", red="#e13a3a", green="#17a673",
        title="#16202e", date="#6b7689",
        card="linear-gradient(135deg, #ffffff 0%, #f4f7fb 100%)",
        card_border="#e3e9f0", kpi_label="#8a94a6", kpi_value="#16202e", kpi_sub="#9aa5b5",
        flat="#8a94a6",
        badge_bg="rgba(23,166,115,.10)", badge_border="rgba(23,166,115,.45)", badge_fg="#0d8a5f",
        th="#8a94a6", th_border="#e3e9f0", td="#27303e", td_border="#f0f4f9",
        tag_buy="rgba(225,58,58,.12)", tag_hold="rgba(26,107,255,.12)",
        tag_watch="rgba(214,158,0,.14)", tag_sell="rgba(23,166,115,.14)",
        status="#8a94a6", status_b="#4b5768",
        plotly="plotly_white", axis_grid="rgba(15,23,42,.12)", bar_text="#5b6b80",
    ),
}


def _pal() -> dict:
    t = _app_theme.get_theme()
    return _PALETTES[t if t in _PALETTES else "dark"]


def _page_css(p: dict) -> str:
    return f"""
<style>
.dash-kpi {{
    background: {p['card']};
    border: 1px solid {p['card_border']}; border-radius: 14px;
    padding: 18px 20px; min-height: 132px;
}}
.dash-kpi .kpi-label {{ color: {p['kpi_label']}; font-size: 13px; letter-spacing: .5px; }}
.dash-kpi .kpi-value {{ color: {p['kpi_value']}; font-size: 30px; font-weight: 700;
                       margin: 6px 0 4px; font-variant-numeric: tabular-nums; }}
.dash-kpi .kpi-delta {{ font-size: 14px; font-weight: 600; }}
.dash-kpi .kpi-sub {{ color: {p['kpi_sub']}; font-size: 12px; margin-top: 4px; }}
.up {{ color: {p['red']} !important; }}
.down {{ color: {p['green']} !important; }}
.flat {{ color: {p['flat']} !important; }}

.live-badge {{
    display: inline-flex; align-items: center; gap: 8px;
    background: {p['badge_bg']}; border: 1px solid {p['badge_border']};
    color: {p['badge_fg']}; font-weight: 700; font-size: 13px;
    border-radius: 999px; padding: 5px 14px;
}}
.live-dot {{ width: 8px; height: 8px; border-radius: 50%; background: {p['badge_fg']};
            animation: dash-pulse 1.6s infinite; }}
@keyframes dash-pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: .25; }} }}

.dash-table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
.dash-table th {{ color: {p['th']}; font-size: 12px; font-weight: 600; text-align: left;
                 padding: 8px 10px; border-bottom: 1px solid {p['th_border']}; }}
.dash-table td {{ padding: 9px 10px; border-bottom: 1px solid {p['td_border']};
                 color: {p['td']}; font-variant-numeric: tabular-nums; }}
.dash-table tr:last-child td {{ border-bottom: none; }}
.tag {{ border-radius: 6px; padding: 3px 10px; font-size: 12px; font-weight: 700; }}
.tag-buy {{ background: {p['tag_buy']}; color: {p['red']}; }}
.tag-hold {{ background: {p['tag_hold']}; color: {p['blue']}; }}
.tag-watch {{ background: {p['tag_watch']}; color: {p['bar_text']}; }}
.tag-sell {{ background: {p['tag_sell']}; color: {p['green']}; }}
.dash-status {{ color: {p['status']}; font-size: 12.5px; }}
.dash-status b {{ color: {p['status_b']}; font-weight: 600; }}
.dot-ok {{ color: {p['green']}; }}
.dot-bad {{ color: {p['red']}; }}
</style>
"""


st.markdown(_page_css(_pal()), unsafe_allow_html=True)


# ---------------------------------------------------------------- 基础工具
def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _cls(v) -> str:
    return "up" if v > 0 else ("down" if v < 0 else "flat")


def _fmt_money(v) -> str:
    if v is None:
        return "-"
    return f"¥{v:,.0f}"


def _kpi(label: str, value: str, delta: str, delta_cls: str, sub: str):
    st.markdown(
        f'<div class="dash-kpi"><div class="kpi-label">{label}</div>'
        f'<div class="kpi-value">{value}</div>'
        f'<div class="kpi-delta {delta_cls}">{delta}</div>'
        f'<div class="kpi-sub">{sub}</div></div>',
        unsafe_allow_html=True)


# ---------------------------------------------------------------- 数据层（全缓存 + 降级）
@st.cache_data(ttl=60, show_spinner=False)
def _index_quotes() -> dict[str, dict]:
    """5 大指数腾讯快照。返回 {code: {name, price, chg_pct, amount_yi}}，失败为空。"""
    codes = ["SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905"]
    out = {}
    try:
        rows = datasource.get_batch_snapshots(codes)
    except Exception:
        rows = []
    for r in rows:
        price = r.get("price")
        prev = r.get("prev_close")
        chg = ((price - prev) / prev * 100) if price and prev else None
        out[r["code"]] = {
            "name": r.get("name") or r["code"],
            "price": price,
            "chg_pct": chg,
            "amount_yi": (r.get("amount") or 0) / 1e8,
        }
    return out


@st.cache_data(ttl=300, show_spinner=False)
def _breadth() -> dict:
    """全行业涨跌家数（sector_daily 落库数据聚合）：最新日 + 近20日走势。"""
    try:
        df = sectorflow.sector_daily_range(days=20)
    except Exception:
        df = pd.DataFrame()
    if df.empty or "up_count" not in df.columns:
        return {}
    agg = (df.groupby("date")[["up_count", "down_count"]].sum()
           .rename(columns={"up_count": "上涨", "down_count": "下跌"}))
    last = agg.iloc[-1]
    return {
        "date": str(agg.index[-1]),
        "up": int(last["上涨"]), "down": int(last["下跌"]),
        "ratio": round(last["上涨"] / (last["上涨"] + last["下跌"]) * 100, 1),
        "history": agg.reset_index(),
    }


@st.cache_data(ttl=300, show_spinner=False)
def _nav_curve(days: int = 60) -> tuple[pd.Series, str]:
    """组合净值：已平仓持仓按卖出日累计（复利）；数据不足回退沪深300指数。"""
    try:
        hist = experience.get_position_history(limit=300)
        if hist is not None and len(hist) >= 2:
            hist = hist.sort_values("sell_date")
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
            hist = hist[pd.to_datetime(hist["sell_date"], errors="coerce") >= cutoff]
            if len(hist) >= 2:
                nav = (1 + hist["pnl_pct"] / 100).cumprod()
                return pd.Series(nav.values, index=pd.to_datetime(hist["sell_date"])), "组合（已平仓）"
    except Exception:
        pass
    try:
        end = _now().strftime("%Y-%m-%d")
        d = datasource.get_daily("sh000300", "2020-01-01", end)
        if d is not None and len(d) >= 2:
            d = d.tail(days)
            nav = d["close"] / d["close"].iloc[0]
            return pd.Series(nav.values, index=pd.to_datetime(d["date"])), "沪深300（回退基准）"
    except Exception:
        pass
    return pd.Series(dtype=float), ""


@st.cache_data(ttl=120, show_spinner=False)
def _pick_counts() -> dict:
    """最近两个交易日的选股名单条数（用于「较昨日」）。"""
    try:
        with experience._conn() as c:
            rows = c.execute(
                "SELECT trade_date, COUNT(*) n FROM picks GROUP BY trade_date "
                "ORDER BY trade_date DESC LIMIT 2").fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def _signals() -> list[dict]:
    """今日实时信号：选股名单明细（名称/信号类型/置信度/动作）。"""
    try:
        today = get_last_trade_day()
        picks = experience.picks_on_date(today)
        if not picks.empty:
            pick = picks.iloc[0]
            items = experience.pick_items_detail(int(pick["id"]))
            if not items.empty:
                scores = items["score"].astype(float)
                lo, hi = scores.min(), scores.max()
                conf = ((scores - lo) / (hi - lo) * 40 + 60).round(0) if hi > lo else 80
                sig_type = (pick["pack_name"] or pick["method"] or "策略选股")
                out = []
                for i, (_, row) in enumerate(items.iterrows()):
                    code = row["code"]
                    out.append({
                        "code": code,
                        "name": broker.get_name(code),
                        "type": str(sig_type),
                        "conf": int(conf.iloc[i]),
                        "score": row["score"],
                        "action": "买入" if i < 5 else "关注",
                    })
                return out
    except Exception:
        pass
    try:
        poss = experience.get_open_positions()
        if not poss.empty:
            return [{
                "code": str(r["code"]), "name": broker.get_name(r["code"]),
                "type": "持仓", "conf": None, "score": r.get("浮动盈亏%"),
                "action": "持有" if (r.get("浮动盈亏%") or 0) > 0 else "止盈/止损",
            } for _, r in poss.head(8).iterrows()]
    except Exception:
        pass
    return []


def _account() -> dict:
    try:
        return broker.get_account()
    except Exception:
        return {}


# ---------------------------------------------------------------- 头部
_p = _pal()
st.markdown('<div style="height:6px"></div>', unsafe_allow_html=True)
h1, h2 = st.columns([6.5, 1.5])
with h1:
    st.markdown(f'<span style="font-size:30px;font-weight:800;color:{_p["title"]};">🚀 量化驾驶舱</span>',
                unsafe_allow_html=True)
    now = _now()
    week = "一二三四五六日"[now.weekday()]
    st.markdown(f'<span style="color:{_p["date"]};font-size:13.5px;">周{week} · {now:%Y/%m/%d} · '
                f'A 股交易时段 · 最后更新 {now:%H:%M:%S}</span>', unsafe_allow_html=True)
with h2:
    st.markdown('<div style="height:10px"></div>', unsafe_allow_html=True)
    live = st.toggle("LIVE 自动刷新", value=True, key="dash_live_toggle",
                     help="每 60 秒自动刷新数据")
    if live and st_autorefresh:
        st_autorefresh(interval=60_000, key="dash_autorefresh")
if st.button("🔄 刷新数据", key="dash_refresh"):
    st.cache_data.clear()
    st.rerun()
st.markdown(f'<div class="live-badge"><span class="live-dot"></span>LIVE · 数据 60s 自动刷新</div>',
            unsafe_allow_html=True)
st.markdown("<hr>", unsafe_allow_html=True)

# ---------------------------------------------------------------- KPI 卡片
acct = _account()
total_asset = acct.get("总资产")
day_pnl = acct.get("今日盈亏", 0) or 0
pos_pnl = acct.get("持仓盈亏") or 0
cash = acct.get("可用资金") or 0
mv = acct.get("持仓市值") or 0
prev_total = (total_asset - day_pnl) if total_asset is not None else None
asset_pct = (day_pnl / prev_total * 100) if prev_total else None

# 沪深300 当日涨跌（跑赢基准对比用）
idx = _index_quotes().get("SH000300", {})
hs300_chg = idx.get("chg_pct")

# 策略信号（今日 / 昨日）
counts = _pick_counts()
dates = sorted(counts)
today_n = counts.get(dates[-1], 0) if dates else 0
yest_n = counts.get(dates[-2], 0) if len(dates) > 1 else 0
sig_delta = today_n - yest_n

# 风险评分：仓位暴露 + 当日波动
if total_asset and total_asset > 0:
    exposure = mv / total_asset
    day_ret = (day_pnl / total_asset * 100)
    risk = max(1, min(99, round(30 + 60 * exposure + abs(day_ret) * 1.2)))
    risk_lv = "低风险" if risk < 40 else ("中等风险" if risk < 70 else "高风险")
else:
    exposure, risk, risk_lv = None, None, "-"

nav_s, nav_label = _nav_curve(60)
max_dd = None
if len(nav_s) >= 2:
    dd = (nav_s / nav_s.cummax() - 1).min()
    max_dd = abs(dd) * 100

k1, k2, k3, k4 = st.columns(4)
with k1:
    delta_html = (f'<span class="{_cls(day_pnl)}">{"+" if day_pnl > 0 else ""}'
                  f'{day_pnl:+,.0f}</span>' if day_pnl else '<span class="flat">±0</span>')
    _kpi("账户总资产", _fmt_money(total_asset), delta_html, _cls(day_pnl or 0),
         f"持仓市值 {_fmt_money(mv)} · 现金 {_fmt_money(cash)}")
with k2:
    pct_html = f'{asset_pct:+.2f}%' if asset_pct is not None else "-"
    beat = ""
    if hs300_chg is not None and asset_pct is not None:
        beat_v = asset_pct - hs300_chg
        beat = f"　{('跑赢' if beat_v > 0 else '跑输')}沪深300 {abs(beat_v):.2f}%"
    _kpi("今日盈亏", f'{day_pnl:+,.0f}' if day_pnl else "¥0",
         pct_html, _cls(day_pnl), f"持仓盈亏 {pos_pnl:+,.0f}{beat}")
with k3:
    _kpi("策略信号", f"{today_n:02d}",
         f"{'↑' if sig_delta > 0 else ('↓' if sig_delta < 0 else '—')} "
         f"{abs(sig_delta)} 较昨日", "flat",
         f"今日选股名单 {today_n} 条 · 昨日 {yest_n} 条")
with k4:
    dd_html = f"回撤 {max_dd:.1f}%" if max_dd is not None else "暂无回撤数据"
    _kpi("风险评分", f"{risk}" if risk is not None else "--",
         risk_lv, "flat",
         f"仓位 {exposure * 100:.0f}% · {dd_html}" if exposure is not None else dd_html)

st.markdown('<div style="height:10px"></div>', unsafe_allow_html=True)

# ---------------------------------------------------------------- 组合净值
with st.container(border=True):
    nav_title, nav_tabs = st.columns([2, 1.4])
    with nav_title:
        st.markdown("### 📈 组合净值")
        st.caption(f"基准：{nav_label or '暂无数据'}")
    with nav_tabs:
        win = st.segmented_control("回看窗口", ["20D", "60D", "1Y"],
                                   default="60D", key="dash_nav_win")
    win_days = {"20D": 20, "60D": 60, "1Y": 250}[win or "60D"]
    nav_s2, _ = _nav_curve(win_days) if win_days != 60 else (nav_s, nav_label)
    if len(nav_s2) >= 2:
        ret = (nav_s2.iloc[-1] - 1) * 100
        fig = go.Figure(go.Scatter(
            x=nav_s2.index, y=nav_s2.values, mode="lines",
            line={"color": _p["blue"], "width": 2.5},
            fill="tozeroy", fillcolor="rgba(38,120,255,.15)",
            hovertemplate="%{x|%Y-%m-%d} · %{y:.3f}<extra></extra>"))
        fig.update_layout(
            template=_p["plotly"], height=300, margin={"l": 10, "r": 10, "t": 25, "b": 10},
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            xaxis={"gridcolor": _p["axis_grid"], "zeroline": False},
            yaxis={"gridcolor": _p["axis_grid"], "zeroline": False},
            annotations=[{"x": 1, "y": 1, "xref": "paper", "yref": "paper",
                          "xanchor": "right", "yanchor": "bottom",
                          "text": f"<b>{ret:+.2f}%</b>",
                          "showarrow": False,
                          "font": {"size": 26, "color": _p["red"] if ret >= 0 else _p["green"]}}])
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    else:
        st.info("暂无组合净值数据：在「今日执行」生成选股名单并实盘模拟后，这里会展示累计净值曲线。")

# ---------------------------------------------------------------- 实时信号 + 市场概览
s1, s2 = st.columns([1.15, 1])
with s1, st.container(border=True):
    st.markdown("### ⚡ 实时信号")
    sigs = _signals()
    if not sigs:
        st.info("今日暂无选股信号。")
    else:
        tag_map = {"买入": "buy", "持有": "hold", "关注": "watch", "止盈/止损": "sell"}
        rows = ""
        for s in sigs:
            conf_txt = f'{s["conf"]:.0f}%' if s["conf"] is not None else "-"
            act = s["action"]
            rows += (f'<tr><td><b>{s["name"]}</b><br>'
                     f'<span style="color:{_p["th"]};font-size:12px;">{s["code"]} · {s["type"]}</span></td>'
                     f'<td>{conf_txt}</td>'
                     f'<td>{s["score"]:.2f}</td>'
                     f'<td><span class="tag tag-{tag_map.get(act, "hold")}">{act}</span></td></tr>')
        st.markdown(
            '<table class="dash-table"><thead><tr><th>标的 / 信号类型</th><th>置信度</th>'
            '<th>评分</th><th>动作</th></tr></thead><tbody>' + rows + '</tbody></table>',
            unsafe_allow_html=True)

with s2, st.container(border=True):
    st.markdown("### 🗺 市场概览")
    quotes = _index_quotes()
    if not quotes:
        st.info("指数行情暂不可用（网络或非交易时段）。")
    else:
        rows = ""
        for code in ["SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905"]:
            q = quotes.get(code)
            if not q:
                continue
            cls = _cls(q["chg_pct"] or 0)
            rows += (f'<tr><td>{q["name"]}</td><td>{q["price"]:,.2f}</td>'
                     f'<td class="{cls}">{q["chg_pct"]:+.2f}%</td>'
                     f'<td>{q["amount_yi"]:,.0f} 亿</td></tr>')
        st.markdown(
            '<table class="dash-table"><thead><tr><th>指数</th><th>最新点位</th>'
            '<th>涨跌幅</th><th>成交额</th></tr></thead><tbody>' + rows + '</tbody></table>',
            unsafe_allow_html=True)

# ---------------------------------------------------------------- 市场宽度
b1, b2 = st.columns([1, 1.15])
br = _breadth()
with b1, st.container(border=True):
    st.markdown("### 🎚 市场宽度")
    if not br:
        st.info("暂无行业宽度数据（板块日线定时任务落库后展示）。")
    else:
        up, down, ratio = br["up"], br["down"], br["ratio"]
        fig = go.Figure(go.Bar(
            x=["上涨家数", "下跌家数"], y=[up, down],
            marker={"color": [_p["red"], _p["green"]]}, width=0.45,
            text=[f"{up}", f"{down}"], textposition="outside",
            textfont={"color": _p["bar_text"], "size": 14}))
        fig.update_layout(
            template=_p["plotly"], height=290, margin={"l": 10, "r": 10, "t": 15, "b": 10},
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            showlegend=False, yaxis={"gridcolor": _p["axis_grid"]},
            xaxis={"tickfont": {"color": _p["td"]}},
            annotations=[{"x": .5, "y": 1.05, "xref": "paper", "yref": "paper",
                          "text": f"上涨占比 <b>{ratio}%</b>（{br['date']}，全行业合计）",
                          "showarrow": False, "font": {"size": 13, "color": _p["bar_text"]}}])
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

with b2, st.container(border=True):
    st.markdown("### 🧭 宽度趋势（近 20 交易日）")
    if not br:
        st.info("暂无趋势数据。")
    else:
        hist = br["history"].copy()
        hist["净宽度"] = hist["上涨"] - hist["下跌"]
        fig = go.Figure()
        fig.add_bar(x=hist["date"], y=hist["净宽度"], marker={"color": _p["blue"]},
                    name="上涨-下跌", width=0.6)
        fig.update_layout(
            template=_p["plotly"], height=290, margin={"l": 10, "r": 10, "t": 15, "b": 10},
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            showlegend=False, yaxis={"gridcolor": _p["axis_grid"]},
            xaxis={"tickfont": {"color": _p["kpi_label"]}})
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

# ---------------------------------------------------------------- 底部状态栏
try:
    srcs = datasource.source_status()
    cur_src = datasource.get_source()
    sync = next((s for s in srcs if s["source"] == cur_src), None)
    ifind_ok = bool(sync and sync.get("rows"))
    sync_txt = f"iFinD 同步正常 · {sync['last_sync']}" if ifind_ok else "iFinD 同步待建立"
except Exception:
    ifind_ok, sync_txt = False, "iFinD 状态未知"

quotes_ok = bool(_index_quotes())
broker_ok = bool(acct)
pos_cnt = len(experience.get_open_positions()) if broker_ok else 0

st.markdown(
    f'<div class="dash-status">'
    f'<span class="dot-{"ok" if quotes_ok else "bad"}">●</span> 行情连接{"正常" if quotes_ok else "异常"}　·　'
    f'<span class="dot-{"ok" if ifind_ok else "bad"}">●</span> {sync_txt}　·　'
    f'<span class="dot-{"ok" if broker_ok else "bad"}">●</span> 资金账户{"正常" if broker_ok else "未初始化"}'
    f'（当前持仓 {pos_cnt} 只）　·　最后更新 {_now():%H:%M:%S}'
    f'</div>', unsafe_allow_html=True)
