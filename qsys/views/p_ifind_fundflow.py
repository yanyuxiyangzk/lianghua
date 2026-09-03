"""📡 资金流向（同花顺） — 全市场个股资金流排名 / 板块资金流汇总。

数据源：
  - 同花顺 10jqka 公开页面 HTML 解析
  - 资金流排名：data.10jqka.com.cn/funds/ggzjl/
  - 板块资金流：data.10jqka.com.cn/funds/hyb/（行业）/ data.10jqka.com.cn/funds/zjlx/（概念）
"""

import re
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import ifind_hub


def _parse_10jqka_table(url: str) -> pd.DataFrame:
    """解析同花顺 10jqka 页面 HTML 表格。"""
    import requests
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "gbk"
        html = r.text
        table_start = html.find("<table")
        if table_start < 0:
            return pd.DataFrame()
        table_end = html.find("</table>", table_start) + 8
        table_html = html[table_start:table_end]
        ths = re.findall(r"<th[^>]*>(.*?)</th>", table_html, re.S)
        headers = [re.sub(r"<[^>]+>", "", th).strip() for th in ths]
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S)
        data = []
        for row in rows:
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            texts = [re.sub(r"<[^>]+>", "", td).strip() for td in tds]
            if texts and len(texts) >= len(headers):
                data.append(texts[: len(headers)])
        if not data:
            return pd.DataFrame()
        return pd.DataFrame(data, columns=headers)
    except Exception:
        return pd.DataFrame()


def _parse_money(s: str) -> float:
    """解析金额字符串（如 '1.23亿' / '4567.89万'）为浮点数（元）。"""
    s = str(s).strip().replace(",", "")
    if not s or s == "--" or s == "-":
        return 0.0
    if "亿" in s:
        try:
            return float(s.replace("亿", "")) * 1e8
        except ValueError:
            return 0.0
    if "万" in s:
        try:
            return float(s.replace("万", "")) * 1e4
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


@st.cache_data(ttl=600, show_spinner="加载同花顺资金流排名…")
def _fetch_fundflow_ranking() -> pd.DataFrame:
    """同花顺个股资金流排名（当日，约50只）。"""
    url = "https://data.10jqka.com.cn/funds/ggzjl/field/zdf/order/desc/page/1/ajax/1/free/1/"
    df = _parse_10jqka_table(url)
    if df.empty:
        return df
    # 统一列名
    rename = {"涨跌幅": "涨跌幅%"}
    df.rename(columns=rename, inplace=True)
    # 数值化
    for col in ["最新价", "涨跌幅%", "换手率"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace("%", ""), errors="coerce")
    for col in ["流入资金(元)", "流出资金(元)", "净额(元)", "成交额(元)", "大单流入(元)"]:
        if col in df.columns:
            df[col] = df[col].apply(_parse_money)
    return df


@st.cache_data(ttl=600, show_spinner="加载同花顺行业资金流…")
def _fetch_industry_fundflow() -> pd.DataFrame:
    """同花顺行业板块资金流排名。"""
    url = "https://data.10jqka.com.cn/funds/hyb/field/lxjr/order/desc/page/1/ajax/1/free/1/"
    df = _parse_10jqka_table(url)
    if df.empty:
        return df
    for col in ["涨跌幅", "净流入", "成交额"]:
        if col in df.columns:
            df[col] = df[col].apply(_parse_money)
    return df


def render():
    st.title("📡 资金流向（同花顺）")
    st.caption("数据源：同花顺 10jqka 公开页面 · 受反爬限制可能偶尔加载失败")
    ifind_hub.header()

    t1, t2 = st.tabs(["🏆 个股资金流排名", "🏭 行业板块资金流"])

    with t1:
        st.markdown(f"**同花顺个股资金流排名**（{datetime.now():%Y-%m-%d %H:%M}）")
        with st.spinner("加载数据…"):
            df = _fetch_fundflow_ranking()
        if df.empty:
            st.warning("同花顺排名数据加载失败（反爬拦截），请稍后重试")
        else:
            sort_options = ["净额(元)", "流入资金(元)", "大单流入(元)", "成交额(元)"]
            sort_col = st.radio("排序", sort_options, horizontal=True, key="ths_ff_sort")
            d = df.sort_values(sort_col, ascending=False).reset_index(drop=True)
            d.insert(0, "排名", range(1, len(d) + 1))

            # 格式化金额列
            show = d.copy()
            for col in ["流入资金(元)", "流出资金(元)", "净额(元)", "成交额(元)", "大单流入(元)"]:
                if col in show.columns:
                    show[col] = show[col].apply(lambda x: f"{x / 1e8:.2f}亿" if abs(x) >= 1e8
                                                else (f"{x / 1e4:.1f}万" if abs(x) >= 1e4 else f"{x:.0f}"))
            st.dataframe(show, width="stretch", hide_index=True, height=min(32 * (len(show) + 1) + 3, 620))

            st.download_button(f"📥 导出CSV（{len(d)}行）",
                               d.to_csv(index=False, encoding="utf-8-sig"),
                               file_name=f"ths_fundflow_rank_{datetime.now():%Y%m%d}.csv",
                               key="ths_ff_dl")

            # 净流入 TOP15 / 净流出 TOP15
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**净流入 TOP15**")
                top_in = d.nlargest(15, "净额(元)")
                colors = ["#e54545" if v > 0 else "#2ca02c" for v in top_in["净额(元)"]]
                fig = go.Figure(go.Bar(
                    x=top_in["净额(元)"], y=top_in["名称"], orientation="h",
                    marker_color=colors,
                    text=[f"{v / 1e8:.2f}亿" for v in top_in["净额(元)"]],
                    textposition="outside", textfont=dict(size=9)))
                fig.update_layout(height=max(300, 22 * len(top_in) + 80),
                                  margin=dict(l=10, r=10, t=10, b=10),
                                  template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                                  xaxis=dict(title="净流入(元)", gridcolor="#333"),
                                  yaxis=dict(gridcolor="#333", automargin=True))
                st.plotly_chart(fig, width="stretch")
            with c2:
                st.markdown("**净流出 TOP15**")
                top_out = d.nsmallest(15, "净额(元)").sort_values("净额(元)", ascending=False)
                colors = ["#e54545" if v > 0 else "#2ca02c" for v in top_out["净额(元)"]]
                fig = go.Figure(go.Bar(
                    x=top_out["净额(元)"], y=top_out["名称"], orientation="h",
                    marker_color=colors,
                    text=[f"{v / 1e8:.2f}亿" for v in top_out["净额(元)"]],
                    textposition="outside", textfont=dict(size=9)))
                fig.update_layout(height=max(300, 22 * len(top_out) + 80),
                                  margin=dict(l=10, r=10, t=10, b=10),
                                  template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                                  xaxis=dict(title="净流入(元)", gridcolor="#333"),
                                  yaxis=dict(gridcolor="#333", automargin=True))
                st.plotly_chart(fig, width="stretch")

    with t2:
        st.markdown(f"**同花顺行业板块资金流**（{datetime.now():%Y-%m-%d %H:%M}）")
        with st.spinner("加载行业数据…"):
            df_ind = _fetch_industry_fundflow()
        if df_ind.empty:
            st.warning("行业板块数据加载失败，请稍后重试")
        else:
            st.dataframe(df_ind, width="stretch", hide_index=True, height=min(32 * (len(df_ind) + 1) + 3, 500))


render()
