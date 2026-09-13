"""🧭 支撑阻力扫描（Density-SR 集成）：四信号融合的全市场支撑/阻力机会排名。

信号 = 成交量分布(VPVR) + ATR归一化 + 极端波动率 + 多周期共振
输出 = 触及概率 / 守住概率 / 机会分（规则化启发式评分，仅供参考）
数据 = 本地 iFinD 日线（market_daily），扫描由 ⏰每日任务 sr_scan 落库，也可手动立即扫描。
"""

import json

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import broker
import common
import density_sr as sr


# ---------------------------------------------------------------- 工具
def _pool_codes(label: str) -> set | None:
    if label == "自选股":
        return set(common.load_watchlist())
    if label == "当前持仓":
        pos = broker.get_positions()
        return set(pos["code"]) if pos is not None and not pos.empty else set()
    return None


def _zone_text(lo, hi) -> str:
    return f"{lo:.2f}~{hi:.2f}" if lo is not None and pd.notna(lo) else "—"


def _pct(v) -> str:
    return f"{v * 100:.0f}%" if v is not None and pd.notna(v) else "—"


# ---------------------------------------------------------------- 详情图
def _detail_fig(code: str, name: str, zones: dict) -> go.Figure:
    """蜡烛图(120日) + 支撑/阻力区间色带 + 右侧 VPVR 直方图（共享价格轴）。"""
    bars = sr.load_bars(code, days=400)
    df = bars.iloc[-120:]
    atr_val = float(sr.calc_atr(bars).iloc[-1])

    fig = make_subplots(1, 2, shared_yaxes=True, column_widths=[0.85, 0.15],
                        horizontal_spacing=0.01)
    fig.add_trace(go.Candlestick(
        x=df["date"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        increasing_line_color="#e54545", decreasing_line_color="#26a65b",
        increasing_fillcolor="#e54545", decreasing_fillcolor="#26a65b",
        name="K线", showlegend=False), row=1, col=1)

    x0, x1 = df["date"].iloc[0], df["date"].iloc[-1]
    for z in zones.get("support", []):
        alpha = min(0.10 + z["strength"] * 0.8, 0.45)
        fig.add_shape(type="rect", x0=x0, x1=x1, y0=z["lo"], y1=z["hi"],
                      fillcolor=f"rgba(38,166,91,{alpha:.2f})", line_width=0, row=1, col=1)
    for z in zones.get("resistance", []):
        alpha = min(0.10 + z["strength"] * 0.8, 0.45)
        fig.add_shape(type="rect", x0=x0, x1=x1, y0=z["lo"], y1=z["hi"],
                      fillcolor=f"rgba(229,69,69,{alpha:.2f})", line_width=0, row=1, col=1)

    # 现价水平线
    last_close = float(df["close"].iloc[-1])
    fig.add_hline(y=last_close, line_dash="dot", line_color="#888", row=1, col=1)

    # 右侧 VPVR（250 日窗口，与 K 线共享价格轴）
    centers, vols = sr.volume_profile(bars, 250, atr_val)
    if len(centers):
        fig.add_trace(go.Bar(x=vols, y=centers, orientation="h",
                             marker_color="rgba(90,120,200,0.45)", name="成交量分布",
                             showlegend=False), row=1, col=2)

    fig.update_layout(height=560, margin=dict(l=10, r=10, t=30, b=10),
                      title=f"{name} {code} · 近120日 + 支撑/阻力区间 + 250日成交量分布",
                      xaxis_rangeslider_visible=False, template="plotly_white")
    fig.update_xaxes(type="category", row=1, col=1)
    fig.update_xaxes(showticklabels=False, row=1, col=2)
    return fig


# ---------------------------------------------------------------- 页面
def render():
    st.title("🧭 支撑阻力扫描")
    st.caption("成交量分布 + ATR归一化 + 极端波动率 + 多周期共振 → 触及概率/守住概率 → 机会排名")

    dates = sr.list_scan_dates()
    if not dates:
        st.info("还没有扫描结果。点击右侧「立即扫描」生成（全市场约 1 分钟）。")

    # ---- 控制条 ----
    c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.2, 1])
    with c1:
        scan_date = st.selectbox("扫描日期", dates, index=0 if dates else None,
                                 key="sr_date") if dates else None
    with c2:
        pool_label = st.selectbox("范围", ["全市场", "自选股", "当前持仓"], key="sr_pool")
    with c3:
        min_touch = st.slider("触及概率 ≥", 0.0, 1.0, 0.0, 0.05, key="sr_min_touch")
    with c4:
        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        run = st.button("🔄 立即扫描", type="primary", use_container_width=True, key="sr_run")

    if run:
        codes = _pool_codes(pool_label)
        if codes is not None and not codes:
            st.warning(f"「{pool_label}」为空，先去维护池子")
            st.stop()
        prog = st.progress(0, text="扫描中…")
        with st.spinner("正在扫描（读全市场日线 + 四信号计算）…"):
            n = sr.scan_and_store(codes=sorted(codes) if codes else None,
                                  progress=lambda i, t: prog.progress(
                                      min(i / max(t, 1), 1.0), text=f"扫描中… {i}/{t}"))
        prog.empty()
        st.success(f"扫描完成：{n} 只")
        st.rerun()

    if not scan_date:
        st.stop()

    # ---- 结果表 ----
    df, _ = sr.load_scan(scan_date)
    if df.empty:
        st.info(f"{scan_date} 无扫描数据")
        st.stop()

    codes = _pool_codes(pool_label)
    if codes is not None:
        df = df[df["code"].isin(codes)]
    df = df[(df["p_touch"].fillna(0) >= min_touch)]
    if df.empty:
        st.warning("当前筛选条件下无结果")
        st.stop()

    st.caption(f"共 {len(df)} 只 · 按机会分排名 · 扫描日 {scan_date}")
    show = df.head(200).reset_index(drop=True)
    disp = pd.DataFrame({
        "代码": show["code"],
        "名称": show["name"].fillna(""),
        "现价": show["close"].map(lambda v: f"{v:.2f}"),
        "最近支撑": [_zone_text(lo, hi) for lo, hi in zip(show["sup_lo"], show["sup_hi"])],
        "距离(ATR)": show["sup_dist_atr"].map(lambda v: f"{v:.2f}" if pd.notna(v) else "—"),
        "触及概率": show["p_touch"].map(_pct),
        "守住概率": show["p_hold"].map(_pct),
        "共振": show["resonance"].map(lambda v: f"{int(v)}窗" if pd.notna(v) else "—"),
        "阻力区间": [_zone_text(lo, hi) for lo, hi in zip(show["res_lo"], show["res_hi"])],
        "机会分": show["score"].map(lambda v: f"{v:.3f}"),
    })
    event = st.dataframe(disp, width="stretch", hide_index=True, height=min(600, 40 + 35 * len(disp)),
                         on_select="rerun", selection_mode="single-row", key="sr_table")
    sel = event.selection.rows if event and event.selection else []
    if not sel:
        st.caption("👆 点击行查看 K线 + 区间 + 成交量分布详情")
    else:
        row = show.iloc[sel[0]]
        code, name = row["code"], row.get("name") or ""
        zones = json.loads(row["zones_json"]) if row.get("zones_json") else {}
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("现价", f"{row['close']:.2f}")
        m2.metric("触及概率(5日)", _pct(row["p_touch"]))
        m3.metric("守住概率", _pct(row["p_hold"]))
        m4.metric("历史触碰守住", f"{zones.get('touches', 0)} 次")
        st.plotly_chart(_detail_fig(code, name, zones), use_container_width=True)

    st.markdown("---")
    st.caption("⚠️ 触及/守住概率为规则化信号强度评分（0-1），由成交量分布密度、多周期共振与历史触碰统计合成，"
               "非真实概率。市场有风险，量化信号仅供参考，不构成买卖依据。")


render()
