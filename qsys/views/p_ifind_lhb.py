"""🐉 龙虎榜 — 个股明细 / 营业部排名 / 最新动态。

数据链路：
  - 个股明细 / 营业部排名 / 个股明细页：同花顺数据中心龙虎榜公开网页 HTML 解析
  - 最新动态：同花顺数据中心龙虎榜页公开的「龙虎榜解析」资讯
  （iFinD 专题报表 p04669/p04674 通过 HTTP data_pool 通道返回 -4001 无权限，改用公开网页）
"""

import re
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st

import ifind_hub

LHB_URL = "https://data.10jqka.com.cn/market/longhu/"


def _ltd() -> datetime:
    """最近交易日（近似：盘中/盘前回退一天，周末回退到周五）。"""
    d = datetime.now()
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _get_html(url: str) -> str:
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.encoding = "gbk"
    return r.text


def _parse_amount(text: str) -> float:
    """'4.12亿' → 41200.0（万），'-7173.84万' → -7173.84（万），'26.51亿' → 265100.0（万）。"""
    text = text.strip().replace(",", "")
    m = re.match(r"(-?[\d.]+)\s*亿", text)
    if m:
        return float(m.group(1)) * 10000
    m = re.match(r"(-?[\d.]+)\s*万", text)
    if m:
        return float(m.group(1))
    try:
        return float(text)
    except Exception:
        return 0.0


def _market(code: str) -> str:
    if code.startswith("6") or code.startswith("9"):
        return "SH"
    if code.startswith("8") or code.startswith("4"):
        return "BJ"
    return "SZ"


def _parse_broker_table(tbl_html: str) -> list[dict]:
    """解析营业部买卖明细表格，返回 [{name, buy, sell, net}]。"""
    rows = []
    for tr_m in re.finditer(r"<tr>(.*?)</tr>", tbl_html, re.S):
        tr = tr_m.group(1)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 4:
            continue
        name = re.sub(r"<[^>]+>", "", tds[0]).strip()
        try:
            buy = float(re.sub(r"[^0-9.\-]", "", tds[1]))
            sell = float(re.sub(r"[^0-9.\-]", "", tds[2]))
            net = float(re.sub(r"[^0-9.\-]", "", tds[3]))
        except Exception:
            continue
        rows.append({"营业部": name, "买入额(万)": buy, "卖出额(万)": sell, "净额(万)": net})
    return rows


@st.cache_data(ttl=600, show_spinner=False)
def _scrape_all() -> tuple[pd.DataFrame, dict[str, dict]]:
    """一次性解析龙虎榜页面，返回 (个股列表, {code: {buy: [...], sell: [...], reason: str}})。"""
    html = _get_html(LHB_URL)

    # ---- 1. 个股明细表格 ----
    stocks = []
    for tr_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tr = tr_m.group(1)
        code_m = re.search(r'stockcode="(\d{6})"[^>]*class="stock"[^>]*>([^<]+)</a>', tr)
        if not code_m:
            continue
        code, name = code_m.group(1), code_m.group(2).strip()
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 6:
            continue
        texts = [re.sub(r"<[^>]+>", "", td).strip() for td in tds]
        try:
            close = float(texts[3])
        except Exception:
            continue
        stocks.append({
            "代码": f"{_market(code)}{code}",
            "名称": name,
            "收盘价": close,
            "涨跌幅(%)": texts[4],
            "成交金额(万)": _parse_amount(texts[5]),
            "净买入额(万)": _parse_amount(texts[6]) if len(texts) > 6 else 0.0,
        })

    # ---- 2. stockcont divs：每个个股的营业部买卖明细 ----
    details: dict[str, dict] = {}
    for sc_m in re.finditer(
        r'<div\s+class="stockcont"\s+stockcode="(\d{6})"[^>]*>(.*?)(?=<div\s+class="stockcont"|<div class="rightcol|$)',
        html, re.S):
        sc_code = sc_m.group(1)
        sc_html = sc_m.group(2)
        full_code = f"{_market(sc_code)}{sc_code}"
        d: dict = {}

        # 上榜原因
        reason_m = re.search(r'上榜类型[：:]\s*([^<\n]+)', sc_html)
        if reason_m:
            d["reason"] = reason_m.group(1).strip()

        # 买入前5
        buy_part = sc_html.split("买入金额最大的前5名营业部")
        if len(buy_part) > 1:
            tbl = buy_part[1].split("</table>")[0]
            d["buy"] = _parse_broker_table(tbl)

        # 卖出前5
        sell_part = sc_html.split("卖出金额最大的前5名营业部")
        if len(sell_part) > 1:
            tbl = sell_part[1].split("</table>")[0]
            d["sell"] = _parse_broker_table(tbl)

        if d.get("buy") or d.get("sell"):
            # 同一股票可能有多个stockcont（不同上榜原因），合并
            if full_code in details:
                existing = details[full_code]
                existing.setdefault("buy", []).extend(d.get("buy", []))
                existing.setdefault("sell", []).extend(d.get("sell", []))
            else:
                details[full_code] = d

    return pd.DataFrame(stocks), details


@st.cache_data(ttl=600, show_spinner=False)
def _scrape_yyb_ranking() -> pd.DataFrame:
    """解析营业部排名表格。"""
    html = _get_html(LHB_URL)
    parts = html.split("营业部排名")
    if len(parts) < 2:
        return pd.DataFrame()
    section = parts[-1]
    rows = []
    for tr_m in re.finditer(r"<tr[^>]*>(.*?)</tr>", section, re.S):
        tr = tr_m.group(1)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 7:
            continue
        texts = [re.sub(r"<[^>]+>", "", td).strip() for td in tds]
        try:
            rank = int(texts[0])
        except Exception:
            continue
        name = re.sub(r"\s*(一线游资|知名游资|游资|敢死队|跟风高手)\s*$", "", texts[1].strip())
        rows.append({
            "排名": rank,
            "营业部名称": name,
            "上榜次数": texts[2],
            "合计动用资金": texts[3],
            "年内上榜次数": texts[4],
            "年内买入股票只数": texts[5],
            "年内3日跟买成功率": texts[6],
        })
    return pd.DataFrame(rows)


def _tab_detail():
    """📋 个股明细 + 可展开营业部买卖明细。"""
    st.info("数据来自同花顺数据中心公开页面，每日收盘后更新")
    df, details = _scrape_all()
    if df.empty:
        st.warning("暂未获取到龙虎榜个股数据，请稍后重试")
        return

    # 标记有明细的个股
    df["有明细"] = df["代码"].apply(lambda c: "🔍" if c in details else "")
    st.dataframe(df[["代码", "名称", "收盘价", "涨跌幅(%)", "成交金额(万)", "净买入额(万)", "有明细"]],
                 width="stretch", hide_index=True,
                 height=min(35 * (len(df) + 1) + 3, 560))
    st.download_button(f"📥 导出CSV（{len(df)}行）",
                       df.drop(columns=["有明细"]).to_csv(index=False, encoding="utf-8-sig"),
                       file_name=f"lhb_detail_{datetime.now():%Y%m%d_%H%M}.csv",
                       key="lhb_d_dl")

    # 个股营业部明细
    codes_with_detail = [c for c in df["代码"] if c in details]
    if codes_with_detail:
        st.markdown("---")
        st.markdown("**🔍 个股营业部明细**")
        options = [f"{c} {df[df['代码']==c].iloc[0]['名称']}" for c in codes_with_detail]
        sel = st.selectbox("选择个股查看买卖前5营业部", options, index=None,
                           placeholder="点击选择…", key="lhb_d_sel")
        if sel is not None:
            code = options[options.index(sel)].split()[0]
            d = details[code]
            if d.get("reason"):
                st.caption(f"上榜原因：{d['reason']}")
            for side, label in [("buy", "买入金额最大前5名营业部"), ("sell", "卖出金额最大前5名营业部")]:
                rows = d.get(side, [])
                if rows:
                    st.markdown(f"**{label}**")
                    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _tab_yyb():
    """🏛️ 营业部排名。"""
    st.info("数据来自同花顺数据中心公开页面，每日收盘后更新")
    df = _scrape_yyb_ranking()
    if df.empty:
        st.warning("暂未获取到营业部排名数据，请稍后重试")
        return
    st.dataframe(df, width="stretch", hide_index=True,
                 height=min(35 * (len(df) + 1) + 3, 560))
    st.download_button(f"📥 导出CSV（{len(df)}行）",
                       df.to_csv(index=False, encoding="utf-8-sig"),
                       file_name=f"lhb_yyb_{datetime.now():%Y%m%d_%H%M}.csv",
                       key="lhb_y_dl")


@st.cache_data(ttl=600, show_spinner=False)
def _latest_news() -> pd.DataFrame:
    """同花顺数据中心龙虎榜页「最新动态」资讯。"""
    try:
        r = requests.get(LHB_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "gbk"
        html = r.text
    except Exception:
        return pd.DataFrame()
    items = re.findall(
        r'<a[^>]*href="(http://(?:yuanchuang|t)\.10jqka\.com\.cn/[^"]+)"[^>]*>([^<]{6,100})</a>',
        html)
    rows = []
    for u, title in items:
        title = re.sub(r"\s+", " ", title).strip()
        if "龙虎" not in title:
            continue
        m = re.search(r"/(\d{8})/", u)
        rows.append({"标题": title, "日期": m.group(1) if m else "", "链接": u})
    return pd.DataFrame(rows)


def _tab_news():
    """📰 最新动态：龙虎榜解析资讯流。"""
    df = _latest_news()
    if df.empty:
        st.warning("暂未拉到最新动态，请稍后重试")
    else:
        show = df[["日期", "标题", "链接"]].rename(columns={"链接": "原文"})
        st.dataframe(show, width="stretch", hide_index=True,
                     height=min(35 * (len(show) + 1) + 3, 620),
                     column_config={"原文": st.column_config.LinkColumn("原文", display_text="查看")})
    st.link_button("↗ 打开同花顺数据中心·龙虎榜（原页）", LHB_URL)


def render():
    st.title("🐉 龙虎榜")
    st.caption("数据源：同花顺数据中心公开页面（每日收盘后更新）")
    ifind_hub.header()
    t1, t2, t3 = st.tabs(["📋 个股明细", "🏛️ 营业部排名", "📰 最新动态"])
    with t1:
        _tab_detail()
    with t2:
        _tab_yyb()
    with t3:
        _tab_news()


render()
