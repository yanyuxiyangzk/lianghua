"""行情表共享组件：列定义、建表、着色（股票行情/板块行情 共用）。"""

import pandas as pd

import datasource

RED_T, GREEN_T = "#e54545", "#2ca02c"

COLUMNS = {
    "seq": ("序号", "{}", False),
    "code": ("代码", "{}", False),
    "name": ("名称", "{}", False),
    "chg_pct": ("涨幅%", "{:+.2f}", True),
    "price": ("现价", "{:.2f}", False),
    "chg": ("涨跌", "{:+.2f}", True),
    "open": ("今开", "{:.2f}", False),
    "high": ("最高", "{:.2f}", False),
    "low": ("最低", "{:.2f}", False),
    "prev_close": ("昨收", "{:.2f}", False),
    "bid1": ("买价", "{:.2f}", False),
    "ask1": ("卖价", "{:.2f}", False),
    "volume": ("总手", "{:.0f}", False),
    "amount_yi": ("总金额(亿)", "{:.2f}", False),
    "last_vol": ("现手", "{:.0f}", False),
    "speed": ("1分钟涨速%", "{:+.2f}", True),
    "body_pct": ("实体涨幅%", "{:+.2f}", True),
    "price_avg_diff": ("现均差", "{:+.2f}", True),
    "turnover": ("换手%", "{:.2f}", False),
    "weibi": ("委比%", "{:+.1f}", True),
    "amplitude": ("振幅%", "{:.2f}", False),
    "outer_vol": ("外盘", "{:.0f}", False),
    "inner_vol": ("内盘", "{:.0f}", False),
    "avg_price": ("均价", "{:.2f}", False),
    "quantity_ratio": ("量比", "{:.2f}", False),
    "limit_up": ("涨停价", "{:.2f}", False),
    "limit_down": ("跌停价", "{:.2f}", False),
    "trade_time": ("行情时间", "{}", False),
}
SORTABLE = {"涨幅%": "chg_pct", "总金额": "amount_yi", "总手": "volume", "换手%": "turnover",
            "1分钟涨速%": "speed", "量比": "quantity_ratio", "委比%": "weibi", "振幅%": "amplitude"}


def build_table(rows: list[dict], ref_ts: str) -> pd.DataFrame:
    codes = [r["code"] for r in rows]
    speed_base = datasource.get_speed_1min(codes, ref_ts)
    prev_vols = datasource.get_prev_snapshot_volumes(codes, ref_ts)
    recs = []
    for r in rows:
        price, prev = r["price"], r["prev_close"]
        chg = (price - prev) if (price and prev) else None
        bid_sum, ask_sum = r.get("bid_vol_sum") or 0, r.get("ask_vol_sum") or 0
        last_vol = r.get("last_tick_vol")
        if last_vol is None and r["volume"] is not None and r["code"] in prev_vols:
            last_vol = max(r["volume"] - prev_vols[r["code"]], 0)
        recs.append({
            "code": r["code"], "name": r["name"], "price": price,
            "chg_pct": chg / prev * 100 if chg is not None else None,
            "chg": chg,
            "open": r["open"], "high": r["high"], "low": r["low"], "prev_close": prev,
            "bid1": r["bid1"], "ask1": r["ask1"],
            "volume": r["volume"],
            "amount_yi": (r["amount"] or 0) / 1e8,
            "last_vol": last_vol,
            "speed": ((price / speed_base[r["code"]] - 1) * 100
                      if price and r["code"] in speed_base else None),
            "body_pct": ((price - r["open"]) / prev * 100) if (price and r["open"] and prev) else None,
            "price_avg_diff": (price - r["avg_price"]) if (price and r["avg_price"]) else None,
            "turnover": r["turnover"],
            "weibi": ((bid_sum - ask_sum) / (bid_sum + ask_sum) * 100) if (bid_sum + ask_sum) > 0 else None,
            "amplitude": ((r["high"] - r["low"]) / prev * 100) if (r["high"] and r["low"] and prev) else None,
            "outer_vol": r["outer_vol"], "inner_vol": r["inner_vol"],
            "avg_price": r["avg_price"], "quantity_ratio": r["quantity_ratio"],
            "limit_up": r["limit_up"], "limit_down": r["limit_down"],
            "trade_time": (r["trade_time"][8:12] if r["trade_time"] else ""),
        })
    return pd.DataFrame(recs)


def signed_color(v):
    if pd.isna(v):
        return ""
    return f"color: {RED_T}" if v > 0 else (f"color: {GREEN_T}" if v < 0 else "color: #999")


def styled_view(df: pd.DataFrame, show_cols: list[str]):
    """列名映射为中文标签 + 格式化 + 红涨绿跌着色。"""
    view = df[show_cols].rename(columns={k: COLUMNS[k][0] for k in show_cols})
    fmt = {COLUMNS[k][0]: v[1] for k, v in COLUMNS.items() if k in show_cols and v[1] != "{}"}
    signed = [COLUMNS[k][0] for k in show_cols if COLUMNS[k][2]]
    return view.style.format(fmt, na_rep="—").map(signed_color, subset=signed)


# ---------------------------------------------------------------- 自绘 HTML 表格（防 dataframe 重绘闪屏）
def _fmt_cell(col_key: str, v) -> tuple[str, str]:
    """返回 (文本, 内联样式)。"""
    label_fmt = COLUMNS[col_key][1]
    if pd.isna(v):
        return "—", "color:#666"
    if label_fmt == "{}":
        return str(v), ""
    text = label_fmt.format(v)
    if COLUMNS[col_key][2]:
        return text, signed_color(v)
    return text, ""


def html_table(df: pd.DataFrame, show_cols: list[str], height: int = 620) -> str:
    """自绘 HTML 表格 v2：粘性表头 + 中文标签 + 斑马纹 + 涨跌热力底色 + 前三名徽章。
    单元素原子替换刷新，避免 st.dataframe 组件级重绘闪屏。"""
    import html as _html

    show_cols = [c for c in show_cols if c in df.columns]
    signed_keys = {k for k in show_cols if COLUMNS[k][2]}

    ths = "".join(
        f"<th style='padding:8px 10px;text-align:{'left' if k in ('code','name') else 'right'};"
        f"color:#e8e8e8;font-size:12.5px;letter-spacing:0.3px;border-bottom:2px solid #3a3f4b;"
        f"white-space:nowrap;background:#1c1f26'>{COLUMNS[k][0]}</th>"
        for k in show_cols)

    def _heat(key, v):
        """涨跌列按幅度给底色（热力效果）。"""
        try:
            a = abs(float(v))
        except (TypeError, ValueError):
            return ""
        if key in ("chg_pct", "speed", "body_pct"):
            if a >= 5:
                return "background:rgba(229,69,80,0.30)" if v > 0 else "background:rgba(38,166,154,0.30)"
            if a >= 2:
                return "background:rgba(229,69,80,0.18)" if v > 0 else "background:rgba(38,166,154,0.18)"
            if a >= 0.8:
                return "background:rgba(229,69,80,0.08)" if v > 0 else "background:rgba(38,166,154,0.08)"
        return ""

    trs = []
    for idx, (_, r) in enumerate(df.iterrows()):
        tds = []
        for k in show_cols:
            text, style = _fmt_cell(k, r[k])
            align = "left" if k in ("code", "name") else "right"
            bold = "font-weight:600;" if k in ("price", "chg_pct") else ""
            heat = _heat(k, r[k]) if k in signed_keys else ""
            if k == "seq" and idx < 3:
                text = f"{['🥇', '🥈', '🥉'][idx]} {text}"
            tds.append(f"<td style='padding:6px 10px;text-align:{align};white-space:nowrap;{bold}{style};{heat}'>"
                       f"{_html.escape(text)}</td>")
        zebra = "#14161b" if idx % 2 == 0 else "#101010"
        trs.append(f"<tr style='background:{zebra};border-bottom:1px solid #20232a' "
                   "onmouseover=\"this.style.background='#232733'\" "
                   f"onmouseout=\"this.style.background='{zebra}'\">" + "".join(tds) + "</tr>")
    return (
        f"<div style='height:{height}px;overflow-y:auto;border:1px solid #2a2f3a;border-radius:8px;"
        f"background:#101010;box-shadow:0 1px 4px rgba(0,0,0,0.4)'>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px;"
        f"font-family:ui-monospace,SFMono-Regular,monospace;color:#ddd'>"
        f"<thead style='position:sticky;top:0;z-index:2'><tr>{ths}</tr></thead>"
        f"<tbody>{''.join(trs)}</tbody></table></div>")
