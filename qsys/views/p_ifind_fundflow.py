"""💰 资金流向（同花顺） — 个股资金流排名 + 板块资金流（行业/概念/地域/证监会）。

数据链路：
  1. 个股：优先读 stock_fundflow_daily 表（定时任务同步），DB 为空时 fallback 到同花顺 10jqka 公开页面
  2. 板块：优先读 sector_daily 表（定时任务同步），DB 为空时 fallback 到同花顺 10jqka 公开页面
"""

import re
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import ifind_hub


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


# ---------------------------------------------------------------- DB 读取 ----------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def _load_fundflow_from_db() -> pd.DataFrame:
    """从 stock_fundflow_daily 表读取个股资金流排名。"""
    try:
        from datasource import _conn
        with _conn() as c:
            df = pd.read_sql(
                """SELECT code, date, main_net, small_net, mid_net, big_net, super_net
                   FROM stock_fundflow_daily
                   WHERE date = (SELECT MAX(date) FROM stock_fundflow_daily)
                   ORDER BY main_net DESC""",
                c)
        if df.empty:
            return df
        # 先并名称再改列名（原顺序相反：rename 后 on="code" 必抛 KeyError，
        # 一直走 except 兜底 → 名称列显示成代码）
        try:
            names = pd.read_sql("SELECT code, name FROM ifind_stocklist", c)
            df = df.merge(names, on="code", how="left")
            df.rename(columns={"name": "名称"}, inplace=True)
        except Exception:
            df["名称"] = df["code"]
        df.rename(columns={
            "code": "代码", "date": "日期",
            "main_net": "净额(元)", "super_net": "超大单流入(元)",
            "big_net": "大单流入(元)", "mid_net": "中单流入(元)",
            "small_net": "小单流入(元)",
        }, inplace=True)
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300, show_spinner=False)
def _load_sector_fundflow_from_db(category: str = "同花顺行业") -> pd.DataFrame:
    """从 sector_daily 表读取板块资金流数据。

    Args:
        category: 板块分类（同花顺行业/概念板块/地域板块/证监会板块）
    """
    try:
        from datasource import _conn
        with _conn() as c:
            # 先尝试从 sector_daily 读取
            df = pd.read_sql(
                """SELECT sector_name, avg_chg_pct, total_amount, up_count, down_count, members, flow_net
                   FROM sector_daily
                   WHERE date = (SELECT MAX(date) FROM sector_daily)
                   ORDER BY flow_net DESC""",
                c)
            if not df.empty:
                df.rename(columns={
                    "sector_name": "板块", "avg_chg_pct": "涨跌幅",
                    "total_amount": "总成交额", "up_count": "上涨家数",
                    "down_count": "下跌家数", "members": "成分股数",
                    "flow_net": "净流入",
                }, inplace=True)
                return df
    except Exception:
        pass
    return pd.DataFrame()


# ---------------------------------------------------------------- 问财聚合（主通道） ----------------------------------------------------------------

# 概念标签里的泛化标签（非主题概念，聚合时剔除）
_SECTOR_TAG_STOP = {"融资融券", "转融券标的", "深股通", "沪股通", "MSCI概念",
                    "富时罗素", "富时罗素概念", "标普道琼斯A股", "同花顺漂亮100"}


def _agg_sector_flow(df: pd.DataFrame, tag_col: str, flow_col: str,
                     chg_col: str, amt_col: str, split: bool = False) -> pd.DataFrame:
    """个股级资金流向按归属标签聚合为板块资金流。"""
    d = df.copy()
    if split:
        d[tag_col] = d[tag_col].astype(str).str.split(r"[;；]")  # 问财标签混用中英文分号
        d = d.explode(tag_col)
        d[tag_col] = d[tag_col].str.strip()
        d = d[~d[tag_col].isin(_SECTOR_TAG_STOP)]
    d = d[d[tag_col].notna() & (d[tag_col].astype(str).str.len() > 0)
          & (d[tag_col] != "不详")]  # 剔除未分类（证监会"不详"等）
    if d.empty:
        return pd.DataFrame()
    d = d.assign(_up=d[chg_col] > 0, _down=d[chg_col] < 0)
    out = (d.groupby(tag_col)
            .agg(净流入=(flow_col, "sum"), 涨跌幅=(chg_col, "mean"), 成交额=(amt_col, "sum"),
                 上涨家数=("_up", "sum"), 下跌家数=("_down", "sum"), 成分股数=(flow_col, "size"))
            .reset_index().rename(columns={tag_col: "板块"})
            .sort_values("净流入", ascending=False))
    return out


@st.cache_data(ttl=600, show_spinner="加载板块资金流（问财聚合）…")
def _fetch_wencai_sector_flow() -> dict:
    """问财个股资金流向 + 归属标签（概念/省份/证监会行业）→ 三类板块资金流聚合。

    替代 10jqka HTML 爬取（2026-09 起 401 反爬盾不可用）。问财 token 通道、不落库。
    """
    from datasource import _conn, _ths_http
    # 用库内最新交易日，保证周末/假期也能拿到最近有效数据
    try:
        with _conn() as c:
            latest = c.execute("SELECT MAX(date) FROM stock_fundflow_daily").fetchone()[0]
    except Exception:
        latest = None
    day = (latest or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    q = f"{day} 资金流向 所属概念 省份 所属证监会行业 涨跌幅 成交额"
    try:
        df, _res, err = _ths_http("smart_stock_picking", {"searchstring": q, "searchtype": "block"})
    except Exception:
        return {}
    if err not in (0, None) or df is None or df.empty:
        return {}

    def _col(sub):
        return next((c for c in df.columns if sub in str(c)), None)

    flow_col, chg_col, amt_col = _col("资金流向"), _col("涨跌幅"), _col("成交额")
    con_col, reg_col, csrc_col = _col("所属概念"), _col("省份"), _col("证监会行业")
    if not all([flow_col, chg_col, amt_col]):
        return {}
    for c in (flow_col, chg_col, amt_col):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    out = {}
    if con_col:
        out["概念板块"] = _agg_sector_flow(df, con_col, flow_col, chg_col, amt_col, split=True).head(100)
    if reg_col:
        out["地域板块"] = _agg_sector_flow(df, reg_col, flow_col, chg_col, amt_col)
    if csrc_col:
        out["证监会板块"] = _agg_sector_flow(df, csrc_col, flow_col, chg_col, amt_col)
    return out


# ---------------------------------------------------------------- HTML 爬取（fallback） ----------------------------------------------------------------

def _parse_10jqka_table(url: str) -> pd.DataFrame:
    """通用：解析同花顺 10jqka 页面 HTML 表格。"""
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


@st.cache_data(ttl=600, show_spinner="加载同花顺资金流排名…")
def _fetch_fundflow_ranking() -> pd.DataFrame:
    """同花顺个股资金流排名（当日，约50只）— HTML fallback。"""
    url = "https://data.10jqka.com.cn/funds/ggzjl/field/zdf/order/desc/page/1/ajax/1/free/1/"
    df = _parse_10jqka_table(url)
    if df.empty:
        return df
    rename = {"涨跌幅": "涨跌幅%", "股票名称": "名称", "股票代码": "代码",
              "净流入": "净额(元)", "主力净流入": "净额(元)"}
    df.rename(columns=rename, inplace=True)
    if "名称" not in df.columns:
        df["名称"] = df.get("代码", pd.Series(df.index, index=df.index)).astype(str)
    for col in ["最新价", "涨跌幅%", "换手率"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace("%", ""), errors="coerce")
    for col in ["流入资金(元)", "流出资金(元)", "净额(元)", "成交额(元)", "大单流入(元)"]:
        if col in df.columns:
            df[col] = df[col].apply(_parse_money)
        else:
            df[col] = 0.0
    return df


@st.cache_data(ttl=600, show_spinner="加载同花顺行业资金流…")
def _fetch_industry_fundflow() -> pd.DataFrame:
    """同花顺行业板块资金流排名 — HTML fallback。"""
    url = "https://data.10jqka.com.cn/funds/hyb/field/lxjr/order/desc/page/1/ajax/1/free/1/"
    df = _parse_10jqka_table(url)
    if df.empty:
        return df
    for col in ["涨跌幅", "净流入", "成交额"]:
        if col in df.columns:
            df[col] = df[col].apply(_parse_money)
    return df


@st.cache_data(ttl=600, show_spinner="加载概念板块资金流…")
def _fetch_concept_fundflow() -> pd.DataFrame:
    """同花顺概念板块资金流排名 — HTML fallback。"""
    url = "https://data.10jqka.com.cn/funds/gnjl/field/lxjr/order/desc/page/1/ajax/1/free/1/"
    df = _parse_10jqka_table(url)
    if df.empty:
        return df
    for col in ["涨跌幅", "净流入", "成交额"]:
        if col in df.columns:
            df[col] = df[col].apply(_parse_money)
    return df


@st.cache_data(ttl=600, show_spinner="加载地域板块资金流…")
def _fetch_regional_fundflow() -> pd.DataFrame:
    """同花顺地域板块资金流排名 — HTML fallback。"""
    url = "https://data.10jqka.com.cn/funds/dyb/field/lxjr/order/desc/page/1/ajax/1/free/1/"
    df = _parse_10jqka_table(url)
    if df.empty:
        return df
    for col in ["涨跌幅", "净流入", "成交额"]:
        if col in df.columns:
            df[col] = df[col].apply(_parse_money)
    return df


@st.cache_data(ttl=600, show_spinner="加载证监会板块资金流…")
def _fetch_csrc_fundflow() -> pd.DataFrame:
    """同花顺证监会板块资金流排名 — HTML fallback。"""
    url = "https://data.10jqka.com.cn/funds/zjhjg/field/lxjr/order/desc/page/1/ajax/1/free/1/"
    df = _parse_10jqka_table(url)
    if df.empty:
        return df
    for col in ["涨跌幅", "净流入", "成交额"]:
        if col in df.columns:
            df[col] = df[col].apply(_parse_money)
    return df


# ---------------------------------------------------------------- 渲染 ----------------------------------------------------------------

def _render_sector_tab(df: pd.DataFrame, title: str, key_prefix: str):
    """通用板块资金流渲染。"""
    if df.empty:
        st.warning(f"{title}数据加载失败，请稍后重试")
        return

    st.markdown(f"**{title}**（{datetime.now():%Y-%m-%d %H:%M}）")

    # 找到可用的排序列
    sort_options = [c for c in ["净流入", "净额(元)", "涨跌幅", "成交额", "总成交额"]
                    if c in df.columns]
    if not sort_options:
        st.dataframe(df, width="stretch", hide_index=True)
        return

    sort_col = st.radio("排序", sort_options, horizontal=True, key=f"{key_prefix}_sort")
    d = df.sort_values(sort_col, ascending=False).reset_index(drop=True)
    d.insert(0, "排名", range(1, len(d) + 1))

    # 格式化金额列
    show = d.copy()
    for col in ["净流入", "净额(元)", "成交额", "总成交额"]:
        if col in show.columns:
            show[col] = show[col].apply(lambda x: f"{x / 1e8:.2f}亿" if x and abs(x) >= 1e8
                                        else (f"{x / 1e4:.1f}万" if x and abs(x) >= 1e4 else f"{(x or 0):.0f}"))
    for col in ["涨跌幅"]:
        if col in show.columns:
            show[col] = show[col].apply(lambda x: f"{x:.2f}%" if x else "0.00%")

    st.dataframe(show, width="stretch", hide_index=True, height=min(32 * (len(show) + 1) + 3, 620))

    # TOP15 流入/流出
    flow_col = "净流入" if "净流入" in d.columns else "净额(元)"
    if flow_col in d.columns:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**净流入 TOP15**")
            top_in = d.nlargest(15, flow_col)
            if not top_in.empty:
                colors = ["#e54545" if v > 0 else "#2ca02c" for v in top_in[flow_col]]
                fig = go.Figure(go.Bar(
                    x=top_in[flow_col], y=top_in["板块"] if "板块" in top_in.columns else top_in.index, orientation="h",
                    marker_color=colors,
                    text=[f"{v / 1e8:.2f}亿" for v in top_in[flow_col]],
                    textposition="outside", textfont=dict(size=9)))
                fig.update_layout(height=max(300, 22 * len(top_in) + 80),
                                  margin=dict(l=10, r=10, t=10, b=10),
                                  template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                                  xaxis=dict(title="净流入(元)", gridcolor="#333"),
                                  yaxis=dict(gridcolor="#333", automargin=True))
                st.plotly_chart(fig, width="stretch", key=f"sector_flow_in_{key}")
        with c2:
            st.markdown("**净流出 TOP15**")
            top_out = d.nsmallest(15, flow_col).sort_values(flow_col, ascending=False)
            if not top_out.empty:
                colors = ["#e54545" if v > 0 else "#2ca02c" for v in top_out[flow_col]]
                fig = go.Figure(go.Bar(
                    x=top_out[flow_col], y=top_out["板块"] if "板块" in top_out.columns else top_out.index, orientation="h",
                    marker_color=colors,
                    text=[f"{v / 1e8:.2f}亿" for v in top_out[flow_col]],
                    textposition="outside", textfont=dict(size=9)))
                fig.update_layout(height=max(300, 22 * len(top_out) + 80),
                                  margin=dict(l=10, r=10, t=10, b=10),
                                  template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                                  xaxis=dict(title="净流入(元)", gridcolor="#333"),
                                  yaxis=dict(gridcolor="#333", automargin=True))
                st.plotly_chart(fig, width="stretch", key=f"sector_flow_out_{key}")


def render():
    st.title("💰 资金流向")
    st.caption("数据源：stock_fundflow_daily / sector_daily 表（定时同步）或同花顺 10jqka 公开页面")
    ifind_hub.header()

    t1, t2 = st.tabs(["🏆 个股资金流排名", "🏭 板块资金流"])

    with t1:
        st.markdown(f"**个股资金流排名**（{datetime.now():%Y-%m-%d %H:%M}）")

        # 优先 DB
        db_df = _load_fundflow_from_db()
        use_db = not db_df.empty

        if use_db:
            st.info(f"数据来自 stock_fundflow_daily 表（{db_df['日期'].iloc[0]}）")
            df = db_df
        else:
            st.info("stock_fundflow_daily 表为空，从同花顺公开页面实时爬取")
            with st.spinner("加载数据…"):
                df = _fetch_fundflow_ranking()
            if df.empty:
                st.warning("同花顺排名数据加载失败（反爬拦截），请稍后重试")
                return

        sort_options = [c for c in ["净额(元)", "流入资金(元)", "大单流入(元)", "成交额(元)",
                                     "超大单流入(元)", "大单流入(元)"] if c in df.columns]
        if not sort_options:
            st.warning(f"资金流数据表头无法识别，实际列：{', '.join(map(str, df.columns))}")
            st.dataframe(df, width="stretch", hide_index=True)
            st.stop()
        sort_col = st.radio("排序", sort_options, horizontal=True, key="ths_ff_sort")
        d = df.sort_values(sort_col, ascending=False).reset_index(drop=True)
        d.insert(0, "排名", range(1, len(d) + 1))

        # 格式化金额列
        show = d.copy()
        for col in ["流入资金(元)", "流出资金(元)", "净额(元)", "成交额(元)",
                     "大单流入(元)", "超大单流入(元)", "小单流入(元)", "中单流入(元)"]:
            if col in show.columns:
                show[col] = show[col].apply(lambda x: f"{x / 1e8:.2f}亿" if x and abs(x) >= 1e8
                                            else (f"{x / 1e4:.1f}万" if x and abs(x) >= 1e4 else f"{(x or 0):.0f}"))
        st.dataframe(show, width="stretch", hide_index=True, height=min(32 * (len(show) + 1) + 3, 620))

        st.download_button(f"📥 导出CSV（{len(d)}行）",
                           d.to_csv(index=False, encoding="utf-8-sig"),
                           file_name=f"ths_fundflow_rank_{datetime.now():%Y%m%d}.csv",
                           key="ths_ff_dl")

        # 净流入 TOP15 / 净流出 TOP15
        flow_col = "净额(元)" if "净额(元)" in d.columns else sort_col
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**净流入 TOP15**")
            top_in = d.nlargest(15, flow_col)
            colors = ["#e54545" if v > 0 else "#2ca02c" for v in top_in[flow_col]]
            fig = go.Figure(go.Bar(
                x=top_in[flow_col], y=top_in["名称"], orientation="h",
                marker_color=colors,
                text=[f"{v / 1e8:.2f}亿" for v in top_in[flow_col]],
                textposition="outside", textfont=dict(size=9)))
            fig.update_layout(height=max(300, 22 * len(top_in) + 80),
                              margin=dict(l=10, r=10, t=10, b=10),
                              template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                              xaxis=dict(title="净流入(元)", gridcolor="#333"),
                              yaxis=dict(gridcolor="#333", automargin=True))
            st.plotly_chart(fig, width="stretch", key="stock_flow_in")
        with c2:
            st.markdown("**净流出 TOP15**")
            top_out = d.nsmallest(15, flow_col).sort_values(flow_col, ascending=False)
            colors = ["#e54545" if v > 0 else "#2ca02c" for v in top_out[flow_col]]
            fig = go.Figure(go.Bar(
                x=top_out[flow_col], y=top_out["名称"], orientation="h",
                marker_color=colors,
                text=[f"{v / 1e8:.2f}亿" for v in top_out[flow_col]],
                textposition="outside", textfont=dict(size=9)))
            fig.update_layout(height=max(300, 22 * len(top_out) + 80),
                              margin=dict(l=10, r=10, t=10, b=10),
                              template="plotly_dark", paper_bgcolor="#101010", plot_bgcolor="#101010",
                              xaxis=dict(title="净流入(元)", gridcolor="#333"),
                              yaxis=dict(gridcolor="#333", automargin=True))
            st.plotly_chart(fig, width="stretch", key="stock_flow_out")

    with t2:
        st.markdown(f"**板块资金流**（{datetime.now():%Y-%m-%d %H:%M}）")
        st.caption("4类板块：同花顺行业 / 概念板块 / 地域板块 / 证监会板块")

        # 4类板块 tabs
        t_ind, t_con, t_reg, t_csrc = st.tabs(["📊 同花顺行业", "💡 概念板块", "🗺️ 地域板块", "🏛️ 证监会板块"])

        with t_ind:
            # 优先从数据库读取
            db_df = _load_sector_fundflow_from_db("同花顺行业")
            if not db_df.empty:
                st.info("数据来自 sector_daily 表（定时同步）")
                _render_sector_tab(db_df, "同花顺行业板块资金流", "ind_db")
            else:
                st.info("sector_daily 表为空，从同花顺公开页面实时爬取")
                with st.spinner("加载同花顺行业数据…"):
                    df_ind = _fetch_industry_fundflow()
                _render_sector_tab(df_ind, "同花顺行业板块资金流", "ind_html")

        with t_con:
            df_con = _fetch_wencai_sector_flow().get("概念板块", pd.DataFrame())
            if df_con.empty:
                st.info("问财聚合不可用，尝试同花顺公开页面…")
                with st.spinner("加载概念板块数据…"):
                    df_con = _fetch_concept_fundflow()
            else:
                st.caption("数据源：问财个股资金流向按所属概念聚合")
            _render_sector_tab(df_con, "同花顺概念板块资金流", "con")

        with t_reg:
            df_reg = _fetch_wencai_sector_flow().get("地域板块", pd.DataFrame())
            if df_reg.empty:
                st.info("问财聚合不可用，尝试同花顺公开页面…")
                with st.spinner("加载地域板块数据…"):
                    df_reg = _fetch_regional_fundflow()
            else:
                st.caption("数据源：问财个股资金流向按所属省份聚合")
            _render_sector_tab(df_reg, "同花顺地域板块资金流", "reg")

        with t_csrc:
            df_csrc = _fetch_wencai_sector_flow().get("证监会板块", pd.DataFrame())
            if df_csrc.empty:
                st.info("问财聚合不可用，尝试同花顺公开页面…")
                with st.spinner("加载证监会板块数据…"):
                    df_csrc = _fetch_csrc_fundflow()
            else:
                st.caption("数据源：问财个股资金流向按证监会行业聚合")
            _render_sector_tab(df_csrc, "同花顺证监会板块资金流", "csrc")


render()
