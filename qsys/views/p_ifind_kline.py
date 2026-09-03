"""📈 股价K线：仿同花顺个股页（全部同花顺 iFinD 数据）。

- 头部：名称/代码 + 最新价/涨跌/涨跌幅 + 高低开/市值/流通/市盈/量比/换手/成交额
  （ifind_stocklist 档案 + ifind_realtime 最新快照覆盖）
- 周期：分时 / 日K / 周K / 月K / 季K / 年K / 120分 / 60分 / 30分 / 15分 / 1分
  （日/周/月/季/年K 读本地 market_daily·读穿缓存，缺数自动补抓落库；分时/分钟K 线上直取 THS_HF）
- 从「行情」页点击股票/指数行跳转（session_state["kline_code"] 带入代码）
"""

import re
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

import datasource

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

UP, DOWN = "#e54545", "#26a69a"  # 红涨绿跌（A股配色）
MA_WINDOWS = [(5, "#f5c542"), (10, "#4fc3f7"), (20, "#ba68c8"), (60, "#9e9e9e")]

PERIODS = ["分时", "日K", "周K", "月K", "季K", "年K", "120分", "60分", "30分", "15分", "1分"]


# ---------------------------------------------------------------- 代码格式
def to_ifind_code(raw: str) -> str:
    """600519 / SH600519 / 920188 / 920188.BJ → 600519.SH / 920188.BJ（iFinD 格式）"""
    raw = (raw or "").strip().upper()
    if re.match(r"^\d{6}\.(SH|SZ|BJ)$", raw):
        return raw
    m = re.match(r"^(SH|SZ|BJ)(\d{6})$", raw)
    if m:
        return f"{m.group(2)}.{m.group(1)}"
    if re.match(r"^\d{6}$", raw):
        if raw.startswith("6") or raw.startswith("000") or raw.startswith("001"):
            return raw + ".SH"  # 6 开头股票 + 000/001 开头上证指数
        if raw.startswith(("4", "8", "920")):
            return raw + ".BJ"
        return raw + ".SZ"
    return raw


def to_db_code(code: str) -> str:
    """600519.SH → SH600519（ifind_stocklist 的代码格式）"""
    m = re.match(r"^(\d{6})\.(SH|SZ|BJ)$", code)
    return f"{m.group(2)}{m.group(1)}" if m else code


# ---------------------------------------------------------------- 头部数据
def _header_info(code: str) -> dict:
    """股票查 ifind_stocklist（+实时覆盖）；指数查 ifind_indexlist。"""
    out = {}
    db_code = to_db_code(code)
    with datasource._qconn() as c:
        row = c.execute(
            "SELECT name, price, prev_close, open, high, low, change_pct, amount,"
            " turnover, quantity_ratio, pe_ttm, total_mv, float_mv, market"
            " FROM ifind_stocklist WHERE code=?", (db_code,)).fetchone()
        if row:
            keys = ["name", "price", "prev_close", "open", "high", "low", "change_pct",
                    "amount", "turnover", "quantity_ratio", "pe_ttm", "total_mv",
                    "float_mv", "market"]
            out = dict(zip(keys, row))
            rt = c.execute(
                "SELECT price, prev_close, open, high, low, change_pct, amount, turnover,"
                " quantity_ratio FROM ifind_realtime WHERE code=? AND datetime="
                " (SELECT MAX(datetime) FROM ifind_realtime)", (db_code,)).fetchone()
            if rt:
                for k, v in zip(["price", "prev_close", "open", "high", "low",
                                 "change_pct", "amount", "turnover", "quantity_ratio"], rt):
                    if v is not None:
                        out[k] = v
        else:
            r2 = c.execute(
                "SELECT name, price, prev_close, open, high, low, change_pct, amount, market"
                " FROM ifind_indexlist WHERE code=?", (code,)).fetchone()
            if r2:
                out = dict(zip(["name", "price", "prev_close", "open", "high", "low",
                                "change_pct", "amount", "market"], r2))
    return out


def _fmt_yi(v) -> str:
    return f"{v / 1e8:.2f}亿" if v is not None and pd.notna(v) else "-"


def _render_header(code: str, info: dict):
    name = info.get("name") or code
    price, pc = info.get("price"), info.get("prev_close")
    chg = (price - pc) if (price is not None and pc) else None
    pct = info.get("change_pct")
    color = UP if (pct or 0) >= 0 else DOWN
    price_txt = f"{price:.2f}" if price is not None else "-"
    chg_txt = f"{chg:+.2f}" if chg is not None else ""
    pct_txt = f"{pct:+.2f}%" if pct is not None else ""
    f = lambda v: f"{v:.2f}" if v is not None and pd.notna(v) else "-"
    pe = info.get("pe_ttm")
    pe_txt = f"{pe:.2f}" if pe is not None and pd.notna(pe) else "-"
    st.markdown(
        f"""<div style="display:flex;gap:36px;align-items:flex-end;flex-wrap:wrap">
        <div>
          <span style="font-size:18px;font-weight:600">{name}</span>
          <span style="color:#888;margin-left:6px">{code}</span><br>
          <span style="font-size:26px;font-weight:700;color:{color}">{price_txt}</span>
          <span style="color:{color};margin-left:8px">{chg_txt} {pct_txt}</span>
        </div>
        <div style="font-size:13px;line-height:1.9;color:#ccc">
          高 <b style="color:{UP}">{f(info.get('high'))}</b>
          低 <b style="color:{DOWN}">{f(info.get('low'))}</b>
          开 {f(info.get('open'))}<br>
          市值 {_fmt_yi(info.get('total_mv'))}
          流通 {_fmt_yi(info.get('float_mv'))}
          市盈 {pe_txt}
        </div>
        <div style="font-size:13px;line-height:1.9;color:#ccc">
          量比 {f(info.get('quantity_ratio'))}<br>
          换手 {f(info.get('turnover'))}%
          额 {_fmt_yi(info.get('amount'))}
        </div>
        </div>""",
        unsafe_allow_html=True)


# ---------------------------------------------------------------- K线数据
def _local_daily(code: str, start: str, end: str) -> pd.DataFrame:
    """本地 market_daily（source='ths_ifind'）读穿缓存：区间缺数时自动爬取落库，
    之后都从本地库展示（不重复打 iFinD）。返回 datetime 索引 ohlcv 帧。"""
    db_code = to_db_code(code)
    with datasource._conn() as c:
        have = c.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM market_daily"
                         " WHERE source='ths_ifind' AND code=?", (db_code,)).fetchone()
    if not (have and have[2] > 0 and have[0] <= start and have[1] >= end):
        try:
            datasource._ths_fetch_daily(db_code, start, end)  # 抓取并落库（INSERT OR REPLACE）
        except Exception:
            pass  # 抓取失败 → 返回空，由调用方回退线上直取
    with datasource._conn() as c:
        df = pd.read_sql(
            "SELECT date, open, high, low, close, volume, amount FROM market_daily"
            " WHERE source='ths_ifind' AND code=? AND date BETWEEN ? AND ? ORDER BY date",
            c, params=(db_code, start, end))
    if df.empty:
        return pd.DataFrame()
    df["datetime"] = pd.to_datetime(df["date"])
    return df.drop(columns=["date"]).set_index("datetime")


def _resample_period(df: pd.DataFrame, iv: str) -> pd.DataFrame:
    """日线 → 周/月/季/年K（A股周K以周五收盘为界）。"""
    rules = {"W": "W-FRI", "M": "ME", "Q": "QE", "Y": "YE"}
    g = df.resample(rules[iv])
    return pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(),
        "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "amount": g["amount"].sum(),
    }).dropna(subset=["open", "close"])


def _norm_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """iFinD 返回帧统一成 datetime 索引 + open/high/low/close/volume/amount 列。"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    tcol = next((c for c in ("time", "date", "trade_date", "datetime") if c in df.columns), None)
    if tcol is None:
        return pd.DataFrame()
    df[tcol] = pd.to_datetime(df[tcol])
    df = df.rename(columns={tcol: "datetime"}).set_index("datetime").sort_index()
    keep = [c for c in ["open", "high", "low", "close", "volume", "amount"] if c in df.columns]
    return df[keep].dropna(subset=["close"])


def _load_kline(code: str, period: str) -> pd.DataFrame:
    today = datetime.now()
    db_code = to_db_code(code)
    if period == "分时":
        # 当日 1 分钟：本地库读穿（缺数才线上补抓落库），非交易日回退 3 天
        for back in range(4):
            d = today - timedelta(days=back)
            day = f"{d:%Y-%m-%d}"
            df = datasource.get_minute_from_db(db_code, day)
            if df.empty:
                try:
                    datasource.fetch_minute_to_db(db_code, day)  # 线上补抓落库
                    df = datasource.get_minute_from_db(db_code, day)
                except Exception:
                    pass
            if not df.empty:
                return df
        return pd.DataFrame()
    if period.endswith("K"):
        # 日/周/月/季/年K：优先本地库（读穿缓存），本地缺失才回退线上前复权直取
        iv = {"日K": "D", "周K": "W", "月K": "M", "季K": "Q", "年K": "Y"}[period]
        days = {"日K": 400, "周K": 365 * 3, "月K": 365 * 10,
                "季K": 365 * 20, "年K": 365 * 40}[period]
        start = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        local = _local_daily(code, start, today.strftime("%Y-%m-%d"))
        if not local.empty:
            return local if iv == "D" else _resample_period(local, iv)
        df, _, err = datasource.ths_history(
            [code], "open,high,low,close,volume,amount",
            start, today.strftime("%Y-%m-%d"),
            params=f"Interval:{iv},CPS:2,Fill:Omit")  # CPS:2 前复权
        return _norm_ohlcv(df)
    # 分钟K：本地 1 分钟线（读穿缓存）聚合
    # 只回抓最近 3 天（防首次打开就补抓十天打满 iFinD）；更早的由每日盘中 minute_sync 逐步积累
    n = period.replace("分", "")
    span = 2 if n == "1" else (3 if n == "5" else 10)
    frames = []
    for back in range(span + 2):  # 多回看2天兜底周末/缺数
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        day = f"{d:%Y-%m-%d}"
        df = datasource.get_minute_from_db(db_code, day)
        if df.empty and back <= 2:  # 只有近3天缺数才线上补抓
            try:
                datasource.fetch_minute_to_db(db_code, day)
                df = datasource.get_minute_from_db(db_code, day)
            except Exception:
                pass
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    big = pd.concat(frames).sort_index()
    if n == "1":
        return big
    g = big.resample(f"{n}min")
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
        "amount": g["amount"].sum(),
    }).dropna(subset=["open", "close"])
    return out


# ---------------------------------------------------------------- 演化因子（loopengine 树因子求值叠加）
@st.cache_data(ttl=300, show_spinner=False)
def _load_factor_registry() -> pd.DataFrame:
    """loopengine 演化因子注册表 + 最新评分卡胜率（默认 5日胜率 降序，无评分卡排最后）。"""
    import json
    import library
    reg = library.get_factor_registry()
    le = reg[reg["engine"] == "loopengine"][["name", "family", "code"]].copy()
    try:
        with datasource._qconn() as c:
            sc = pd.read_sql_query(
                "SELECT name, winrates, icir FROM factor_scorecards sc1"
                " WHERE updated_at = (SELECT MAX(updated_at) FROM factor_scorecards sc2"
                "                     WHERE sc2.name = sc1.name)", c)
        def _wr5(j):
            try:
                return float(json.loads(j).get("5日胜率"))
            except Exception:
                return None
        sc["wr5"] = sc["winrates"].map(_wr5)
        le = le.merge(sc[["name", "wr5", "icir"]], on="name", how="left")
    except Exception:
        le["wr5"] = None
    return le.sort_values("wr5", ascending=False, na_position="last").reset_index(drop=True)


def _factor_choices(kw: str, limit: int = 80) -> list[str]:
    facs = _load_factor_registry()
    if kw.strip():
        facs = facs[facs["name"].str.contains(kw.strip(), na=False)]
    return facs["name"].head(limit).tolist()


@st.cache_data(ttl=300, show_spinner=False)
def _factor_label_map() -> dict:
    """因子名 → 显示标签（含胜率），一次构建，format_func 里 O(1) 查询——
    否则下拉每个选项都全表过滤一次（201 选项 × 2.8万行扫描，实测拖慢渲染）。"""
    facs = _load_factor_registry()
    out = {}
    for r in facs.itertuples():
        wr = getattr(r, "wr5", None)
        out[r.name] = f"{r.name}（5日胜率 {wr:.0%}）" if pd.notna(wr) else f"{r.name}（无评分）"
    return out


def _factor_label(name: str) -> str:
    return _factor_label_map().get(name, name)


def _factor_code_by_name(name: str) -> str:
    facs = _load_factor_registry()
    row = facs[facs["name"] == name]
    return str(row.iloc[0]["code"]) if not row.empty else ""


def _daily_panel(code_ifind: str, df_daily: pd.DataFrame) -> pd.DataFrame:
    """日K帧 → qlib 版式单票面板（instrument, datetime 多级索引，$ 前缀列）。"""
    p = df_daily.rename(columns={"open": "$open", "high": "$high", "low": "$low",
                                 "close": "$close", "volume": "$volume",
                                 "amount": "$amount"}).reset_index()
    p["instrument"] = to_db_code(code_ifind)
    return p.set_index(["instrument", "datetime"])


def _eval_factor_daily(code_ifind: str, factor_code: str, df_daily: pd.DataFrame) -> pd.Series:
    """在日K数据上求 loopengine 树因子值（单票），返回 datetime 索引 Series。"""
    from loopengine.tree import build_field_frames, evaluate_tree, parse
    panel = _daily_panel(code_ifind, df_daily)
    sexpr = factor_code.split("\n", 1)[0][len("# sexpr: "):]
    vals = evaluate_tree(parse(sexpr), build_field_frames(panel))
    return vals.iloc[:, 0].rename("factor")


# ---------------------------------------------------------------- 策略包（综合分时序叠加）
@st.cache_data(ttl=300, show_spinner=False)
def _load_packs() -> dict:
    """系统策略包（因子+权重+方向）。"""
    import library
    return library.list_strategies()


def _eval_pack_daily(code_ifind: str, pack: dict, df_daily: pd.DataFrame) -> pd.Series:
    """策略包综合分（单票时序）：各因子时序 z-score 后按 权重×方向 合成。
    树因子进程内向量求值；非树因子回退 signals.run_factor_code。"""
    import signals as sig
    panel = _daily_panel(code_ifind, df_daily)
    db_code = to_db_code(code_ifind)
    comp = None
    for f in pack.get("factors", []):
        kind, fname = f.get("kind"), f.get("name")
        w, direction = f.get("weight", 1.0), f.get("direction", 1)
        try:
            if kind == "builtin":
                s = sig.compute_builtin(panel, fname)
                s = s.xs(db_code, level="instrument")
            else:
                code = _factor_code_by_name(fname)
                if code.startswith("# sexpr:"):
                    s = _eval_factor_daily(code_ifind, code, df_daily)
                elif code:
                    df_f = sig.run_factor_code(code, fname, [db_code],
                                               df_daily.index[-1].strftime("%Y-%m-%d"))
                    s = df_f.iloc[:, 0]
                    if isinstance(s.index, pd.MultiIndex):
                        s = s.xs(db_code, level="instrument")
                else:
                    continue
            if s is None or s.dropna().empty:
                continue
            z = (s - s.mean()) / (s.std() + 1e-12)  # 时序标准化
            comp = z * (w * direction) if comp is None else comp + z * (w * direction)
        except Exception:
            continue
    return comp.dropna() if comp is not None else pd.Series(dtype=float)


# ---------------------------------------------------------------- 图表
def _calc_boll(df: pd.DataFrame, n: int = 20, k: float = 2.0):
    mid = df["close"].rolling(n).mean()
    std = df["close"].rolling(n).std()
    return mid, mid + k * std, mid - k * std


def _indicator_frame(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """复用 tab_kline_pro 的指标计算（它们吃 $close/$high/$low 列名）。"""
    from tab_kline_pro import calc_kdj, calc_macd, calc_rsi
    d = df.rename(columns={"open": "$open", "high": "$high", "low": "$low",
                           "close": "$close", "volume": "$volume"})
    if name == "MACD":
        dif, dea, hist = calc_macd(d)
        return pd.DataFrame({"DIF": dif, "DEA": dea, "MACD": hist})
    if name == "KDJ":
        k, dd, j = calc_kdj(d)
        return pd.DataFrame({"K": k, "D": dd, "J": j})
    if name == "RSI":
        r = calc_rsi(d)
        return pd.DataFrame({f"RSI{w}": s for w, s in r.items()})
    return pd.DataFrame()


_IND_COLORS = ["#f5c542", "#4fc3f7", "#ba68c8"]


def _kline_fig(df: pd.DataFrame, title: str, indicators: list[str] | None = None,
               show_boll: bool = False,
               factor_series: pd.Series | None = None, factor_name: str = "") -> go.Figure:
    d = df.copy()
    for w, _c in MA_WINDOWS:
        d[f"ma{w}"] = d["close"].rolling(w).mean()

    # 副图指标可多选：每个选中的指标占独立一行（BOLL 叠加在主图）
    ind_frames: list[tuple[str, pd.DataFrame]] = []
    for name in (indicators or []):
        f = _indicator_frame(df, name)
        if not f.empty:
            ind_frames.append((name, f))
    has_fac = factor_series is not None and not factor_series.empty
    nrows = 2 + len(ind_frames) + int(has_fac)
    heights = {2: [0.78, 0.22], 3: [0.60, 0.20, 0.20],
               4: [0.52, 0.16, 0.16, 0.16],
               5: [0.46, 0.135, 0.135, 0.135, 0.135],
               6: [0.40, 0.12, 0.12, 0.12, 0.12, 0.12]}[nrows]
    fig = make_subplots(rows=nrows, cols=1, shared_xaxes=True,
                        row_heights=heights, vertical_spacing=0.02)

    fig.add_trace(go.Candlestick(
        x=d.index, open=d["open"], high=d["high"], low=d["low"], close=d["close"],
        increasing_line_color=UP, increasing_fillcolor=UP,
        decreasing_line_color=DOWN, decreasing_fillcolor=DOWN,
        name="K线"), row=1, col=1)
    for w, color in MA_WINDOWS:
        fig.add_trace(go.Scatter(x=d.index, y=d[f"ma{w}"], name=f"MA{w}",
                                 line=dict(width=1.1, color=color), opacity=0.9),
                      row=1, col=1)
    if show_boll:
        mid, up_b, lo_b = _calc_boll(d)
        for s, nm, dash in [(mid, "BOLL中轨", "solid"), (up_b, "BOLL上轨", "dot"),
                            (lo_b, "BOLL下轨", "dot")]:
            fig.add_trace(go.Scatter(x=d.index, y=s, name=nm,
                                     line=dict(width=1, color="#ff9800", dash=dash)),
                          row=1, col=1)

    colors = [UP if c >= o else DOWN for o, c in zip(d["open"], d["close"])]
    fig.add_trace(go.Bar(x=d.index, y=d["volume"], name="VOL",
                         marker_color=colors, opacity=0.85), row=2, col=1)

    for r_idx, (ind_name, ind_df) in enumerate(ind_frames):
        row = 3 + r_idx
        if ind_name == "MACD":
            hist_colors = [UP if v >= 0 else DOWN for v in ind_df["MACD"].fillna(0)]
            fig.add_trace(go.Bar(x=d.index, y=ind_df["MACD"], name="MACD",
                                 marker_color=hist_colors), row=row, col=1)
            for ci, col in enumerate(["DIF", "DEA"]):
                fig.add_trace(go.Scatter(x=d.index, y=ind_df[col], name=col,
                                         line=dict(width=1.1, color=_IND_COLORS[ci])),
                              row=row, col=1)
        else:
            for ci, col in enumerate(ind_df.columns):
                fig.add_trace(go.Scatter(x=d.index, y=ind_df[col], name=col,
                                         line=dict(width=1.1, color=_IND_COLORS[ci % 3])),
                              row=row, col=1)
        fig.update_yaxes(title_text=ind_name, row=row, col=1)

    if has_fac:
        fac_row = nrows
        fs = factor_series.reindex(d.index)
        fig.add_trace(go.Scatter(x=d.index, y=fs, name=f"因子:{factor_name}",
                                 line=dict(width=1.3, color="#00bcd4")), row=fac_row, col=1)
        fig.update_yaxes(title_text="因子", row=fac_row, col=1)

    fig.update_layout(
        title=dict(text=title, font=dict(size=13)),
        height={2: 680, 3: 760, 4: 840, 5: 920, 6: 1000}[nrows], hovermode="x unified",
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, font=dict(size=11)),
        margin=dict(l=10, r=10, t=40, b=10),
        yaxis2=dict(title="VOL"))
    return fig


def _fenshi_fig(df: pd.DataFrame, title: str, prev_close: float | None) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.78, 0.22], vertical_spacing=0.02)
    fig.add_trace(go.Scatter(x=df.index, y=df["close"], name="价格",
                             line=dict(width=1.4, color="#4fc3f7")), row=1, col=1)
    if prev_close:
        fig.add_hline(y=prev_close, line_dash="dot", line_color="#888",
                      annotation_text=f"昨收 {prev_close:.2f}", row=1, col=1)
    fig.add_trace(go.Bar(x=df.index, y=df["volume"], name="VOL",
                         marker_color="#8a8a8a"), row=2, col=1)
    fig.update_layout(
        title=dict(text=title, font=dict(size=13)),
        height=680, hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, font=dict(size=11)),
        margin=dict(l=10, r=10, t=40, b=10), yaxis2=dict(title="VOL"))
    return fig


# ---------------------------------------------------------------- 页面
def render():
    # 代码输入（优先级：URL 参数 ?code=（超链接跳入）> session_state（双击跳入）> 默认）
    default_code = (st.query_params.get("code")
                    or st.session_state.get("kline_code", "000001.SH"))
    c1, c2 = st.columns([3, 1])
    with c1:
        raw = st.text_input("股票/指数代码", value=default_code,
                            placeholder="如 600519 / SH600519 / 600519.SH / 920188.BJ / 000300.SH",
                            label_visibility="collapsed")
    with c2:
        go_btn = st.button("🔍 查看", type="primary", use_container_width=True)
    code = to_ifind_code(raw)

    info = _header_info(code)
    if not info:
        st.warning(f"未找到 {code} 的档案数据（先在「行情」页同步股票/指数列表）")
        return
    _render_header(code, info)

    period = st.radio("周期", PERIODS, index=1, horizontal=True,
                      label_visibility="collapsed")

    # 右上功能区：副图指标 / BOLL / 自动刷新
    _ar1, _ar2, _ar3 = st.columns([2, 1, 1])
    with _ar1:
        indicators = st.multiselect("副图指标（可多选，选几个出几行）",
                                    ["MACD", "KDJ", "RSI"], default=[], key="kline_ind")
    with _ar2:
        show_boll = st.checkbox("BOLL 布林带", value=False, key="kline_boll")
    with _ar3:
        _auto = st.toggle("自动刷新(30s)", value=False, key="kline_auto")

    # 演化因子/策略叠加（仅日K）：搜索 + 因子（高胜率优先）+ 策略包
    _f1, _f2, _f3 = st.columns([1, 2, 1.5])
    with _f1:
        fac_kw = st.text_input("演化因子搜索", key="kline_fac_kw",
                               placeholder="关键字筛选（如 跳空/动量）")
    with _f2:
        fac_opts = ["（不叠加）"] + _factor_choices(fac_kw)
        fac_sel = st.selectbox("演化因子（高胜率优先）", fac_opts,
                               format_func=_factor_label, key="kline_fac")
    with _f3:
        pack_opts = ["（不选策略）"] + list(_load_packs().keys())
        pack_sel = st.selectbox("策略包叠加（综合分）", pack_opts, key="kline_pack")

    # 自动刷新开关：开启后每30秒重新调 iFinD 取数（K线/分时图表实时更新）
    if _auto:
        if st_autorefresh:
            st_autorefresh(interval=30_000, key="kline_autorefresh")
            st.caption(f"⏱ 每 30 秒自动刷新中 · 数据取数于 {datetime.now():%H:%M:%S}")
        else:
            st.warning("未安装 streamlit-autorefresh，无法自动刷新")

    with st.spinner(f"加载 {code} {period}K线…"):
        try:
            df = _load_kline(code, period)
        except Exception as e:
            st.warning(f"{code} {period} 数据获取失败：{e}（可稍后重试或换周期）")
            return
    if df.empty:
        st.warning(f"{code} {period} 数据获取失败（非交易时段/接口限流/代码不支持）")
        return

    # 演化因子/策略求值（仅日K；策略包优先于单因子）
    factor_series, factor_name = None, ""
    if pack_sel != "（不选策略）":
        if period == "日K":
            try:
                factor_series = _eval_pack_daily(code, _load_packs()[pack_sel], df)
                factor_name = f"策略:{pack_sel}"
                if factor_series.empty:
                    st.caption(f"⚠️ 策略包 {pack_sel} 的因子均无法求值")
            except Exception as e:
                st.caption(f"⚠️ 策略包 {pack_sel} 求值失败：{e}")
        else:
            st.caption("💡 策略包叠加仅支持日K周期")
    elif fac_sel != "（不叠加）":
        if period == "日K":
            try:
                factor_series = _eval_factor_daily(code, _factor_code_by_name(fac_sel), df)
                factor_name = fac_sel
            except Exception as e:
                st.caption(f"⚠️ 因子 {fac_sel} 求值失败：{e}")
        else:
            st.caption("💡 演化因子叠加仅支持日K周期")

    name = info.get("name") or code
    if period == "分时":
        st.plotly_chart(_fenshi_fig(df, f"{name} {code} 分时", info.get("prev_close")),
                        width="stretch")
        src_txt = "THS_HF 高频"
    elif period.endswith("K"):
        st.plotly_chart(_kline_fig(df, f"{name} {code} {period}（本地库·不复权）",
                                   indicators=indicators, show_boll=show_boll,
                                   factor_series=factor_series, factor_name=factor_name),
                        width="stretch")
        src_txt = "本地 market_daily（ths_ifind 每日盘后入库，缺数自动补抓）· 不复权"
    else:
        st.plotly_chart(_kline_fig(df, f"{name} {code} {period}",
                                   indicators=indicators, show_boll=show_boll,
                                   factor_series=factor_series, factor_name=factor_name),
                        width="stretch")
        src_txt = "THS_HF 高频"
    st.caption(f"数据范围: {df.index[0]:%Y-%m-%d %H:%M} ~ {df.index[-1]:%Y-%m-%d %H:%M}，"
               f"共 {len(df)} 根 · 数据源：同花顺 iFinD（{src_txt}）")


render()
