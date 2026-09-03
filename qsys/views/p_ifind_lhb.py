"""🐉 龙虎榜（同花顺 iFinD 专题报表）——个股明细 / 营业部排名 / 最新动态。

数据链路：
  - 个股明细：THS_DR 报表 p04669「每日交易龙虎榜数据」+ 行下钻 p04679「每日交易龙虎榜数据-营业部明细」
  - 营业部排名：THS_DR 报表 p04674「证券营业部交易龙虎榜统计」+ 行下钻 p04680「证券营业部交易龙虎榜统计-营业部明细」
  - 最新动态：同花顺数据中心龙虎榜页公开的「龙虎榜解析」资讯（10jqka）
报表号/参数以「📖 接口文档」官方超级命令-专题报表为准；参数不对时可展开「⚙️ 报表参数」修改。
"""

import re
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

import datasource
import ifind_hub

# 报表预设：(报表号, 参数模板, 输出字段)。{d}=YYYYMMDD 日期
P_DETAIL = "p04669"
P_DETAIL_DRILL = "p04679"
P_YYB = "p04674"
P_YYB_DRILL = "p04680"

# 上榜原因枚举（同花顺数据中心常用项；「全部」= 不传该参数）
REASONS = [
    "全部", "日涨幅偏离值达7%的前5只证券", "日跌幅偏离值达7%的前5只证券",
    "日换手率达20%的前5只证券", "日振幅值达15%的前5只证券",
    "连续三个交易日内涨幅偏离值累计达20%的证券", "连续三个交易日内跌幅偏离值累计达20%的证券",
    "无价格涨跌幅限制的证券", "日收盘价涨幅达15%的前五只证券", "日收盘价跌幅达15%的前五只证券",
    "日收盘价涨幅达20%的前五只证券", "日收盘价跌幅达20%的前五只证券",
    "日收盘价涨幅达30%的前五只证券", "日收盘价跌幅达30%的前五只证券",
    "日价格振幅达30%的前五只证券", "退市整理期",
]

# 字段中文名兜底（HTTP 接口会自带 outParams 中文名；SDK 兜底时用此表，未收录保留原代码）
FIELD_CN = {
    "p04669_f001": "代码", "p04669_f002": "名称", "p04669_f003": "收盘价",
    "p04669_f004": "涨跌幅(%)", "p04669_f005": "龙虎榜成交额(万)",
    "p04669_f006": "买入额(万)", "p04669_f007": "卖出额(万)",
    "p04669_f008": "净额(万)", "p04669_f009": "总成交额(万)",
    "p04669_f010": "占龙虎榜成交额比例(%)", "p04669_f011": "上榜原因",
    "p04669_f012": "上榜原因", "p04669_f013": "数据日期", "p04669_f014": "市场",
    "p04674_f001": "营业部名称", "p04674_f002": "上榜次数", "p04674_f003": "合计动用资金(万)",
    "p04674_f004": "年内上榜次数", "p04674_f005": "年内买入股票只数",
    "p04674_f006": "年内3日跟买成功率(%)", "p04674_f007": "排名", "p04674_f008": "数据日期",
    "p04679_f001": "排名", "p04679_f002": "营业部名称", "p04679_f003": "买入额(万)",
    "p04679_f004": "卖出额(万)", "p04679_f005": "净额(万)", "p04679_f006": "占总成交比例(%)",
    "p04680_f001": "营业部名称", "p04680_f002": "股票代码", "p04680_f003": "股票名称",
    "p04680_f004": "买入额(万)", "p04680_f005": "卖出额(万)", "p04680_f006": "净买入额(万)",
    "p04680_f007": "上榜原因", "p04680_f008": "数据日期",
}


def _ltd() -> datetime:
    """最近交易日（近似：盘中/盘前回退一天，周末回退到周五）。"""
    d = datetime.now()
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _run(key: str, report_id: str, params: str, cols: str) -> pd.DataFrame | None:
    """查询专题报表 → session_state 缓存；返回 DataFrame 或 None（失败已在页面上提示）。"""
    try:
        with st.spinner(f"查询 {report_id} …"):
            df, res, err = datasource.ths_dr_report(report_id, params, cols)
    except Exception as e:
        st.error(f"查询失败：{e}")
        return None
    if err not in (0, None):
        msg = str(getattr(res, "errmsg", None) or (res.get("errmsg", "") if hasattr(res, "get") else ""))
        hint = ""
        if err in (-4001,):
            hint = ("（报表无数据或该报表未开通数据权限——参数不对时展开「⚙️ 报表参数」核对；"
                    "报表号/指标以「📖 接口文档」超级命令为准）")
        elif err == -209:
            hint = "（参数格式错误——展开「⚙️ 报表参数」核对键值对格式）"
        st.error(f"{report_id} 返回 errorcode={err}：{msg}{hint}")
        return None
    if df is None or df.empty:
        st.warning(f"{report_id} 调用成功但返回为空（该日无上榜记录或参数需调整）")
        return None
    st.session_state[key] = (df, report_id, params)
    return df


def _renamed(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [FIELD_CN.get(str(c), str(c)) for c in out.columns]
    return out


def _render_result(key: str) -> pd.DataFrame | None:
    """渲染 session_state 里的查询结果 + 导出。"""
    hit = st.session_state.get(key)
    if not hit:
        return None
    df, report_id, params = hit
    show = _renamed(df)
    st.caption(f"{report_id} · {len(df)} 行 · 参数 `{params}` · 数据源：同花顺 iFinD（专题报表）")
    st.dataframe(show, width="stretch", hide_index=True,
                 height=min(35 * (len(df) + 1) + 3, 560))
    st.download_button(f"📥 导出CSV（{len(df)}行）",
                       show.to_csv(index=False, encoding="utf-8-sig"),
                       file_name=f"{report_id}_{datetime.now():%Y%m%d_%H%M}.csv",
                       key=f"dl_{key}")
    return df


def _param_box(key: str, report_id: str, params: str, cols: str) -> tuple[str, str, str]:
    """折叠面板：报表号/参数/字段可改（官方文档为准）。"""
    with st.expander("⚙️ 报表参数（报表号/参数/字段，对照「📖 接口文档」超级命令-专题报表）",
                     expanded=False):
        c1, c2 = st.columns([1, 2])
        rid = c1.text_input("报表号", report_id, key=f"{key}_rid")
        p = c2.text_input("参数（键值对，分号分隔，{d} 已替换为日期）", params, key=f"{key}_p")
        c = st.text_input("输出字段（逗号分隔）", cols, key=f"{key}_c")
    return rid, p, c


def _tab_detail():
    """📋 个股明细：每日交易龙虎榜数据（p04669）+ 行下钻个股营业部明细（p04679）。"""
    d = st.date_input("日期", value=_ltd(), key="lhb_d")
    ds = d.strftime("%Y%m%d")
    c1, c2 = st.columns([2, 1])
    reason = c1.selectbox("上榜原因（全部=不限）", REASONS, key="lhb_sbyy")
    go = c2.button("🔍 查询", type="primary", key="lhb_d_go")

    params = f"edate={ds}"
    if reason != "全部":
        params += f";sbyy={reason}"
    cols = ",".join(f"{P_DETAIL}_f{i:03d}" for i in range(1, 17))
    rid, params, cols = _param_box("lhb_d", P_DETAIL, params, cols)
    if go:
        _run("lhb_detail", rid, params, cols)
    df = _render_result("lhb_detail")
    if df is None or df.empty:
        return

    st.markdown("**🔍 个股营业部明细（先选一只个股再查询）**")
    sel = st.selectbox("选择个股", list(range(len(df))), index=None, placeholder="点击选择…",
                       key="lhb_d_sel",
                       format_func=lambda i: " ".join(
                           str(df.iloc[i].get(c) or "") for c in df.columns[:2]))
    if sel is not None:
        row = df.iloc[sel]
        jydm = row.get("jydm") or row.get("交易所代码") or ""
        sbyy = row.get(P_DETAIL + "_f012") or row.get("上榜原因") or ""
        pmlx = st.radio("排名榜单", ["买入金额最大前五名", "卖出金额最大前五名"],
                        horizontal=True, key="lhb_pmlx")
        params = f"edate={ds};resouce={jydm};sbyy={sbyy};pmlx={pmlx}"
        cols = ",".join(f"{P_DETAIL_DRILL}_f{i:03d}" for i in range(1, 9))
        rid2, p2, c2 = _param_box("lhb_dd", P_DETAIL_DRILL, params, cols)
        if st.button("🔍 查询营业部明细", key="lhb_dd_go"):
            _run("lhb_detail_drill", rid2, p2, c2)
        _render_result("lhb_detail_drill")


def _tab_yyb():
    """🏛️ 营业部排名：证券营业部交易龙虎榜统计（p04674）+ 行下钻营业部上榜明细（p04680）。"""
    d = st.date_input("报表日期", value=_ltd(), key="lhb_y_d")
    ds = d.strftime("%Y%m%d")
    go = st.button("🔍 查询", type="primary", key="lhb_y_go")
    params = f"period={ds}"
    cols = ",".join(f"{P_YYB}_f{i:03d}" for i in range(1, 9))
    rid, params, cols = _param_box("lhb_y", P_YYB, params, cols)
    if go:
        _run("lhb_yyb", rid, params, cols)
    df = _render_result("lhb_yyb")
    if df is None or df.empty:
        return

    st.markdown("**🔍 营业部上榜明细（先选一家营业部再查询）**")
    sel = st.selectbox("选择营业部", list(range(len(df))), index=None, placeholder="点击选择…",
                       key="lhb_y_sel",
                       format_func=lambda i: str(df.iloc[i].get(P_YYB + "_f001")
                                                 or df.iloc[i].iloc[0] or ""))
    if sel is not None:
        yybmc = df.iloc[sel].get(P_YYB + "_f001") or ""
        params = f"period={ds};yybmc={yybmc}"
        cols = ",".join(f"{P_YYB_DRILL}_f{i:03d}" for i in range(1, 9))
        rid2, p2, c2 = _param_box("lhb_yd", P_YYB_DRILL, params, cols)
        if st.button("🔍 查询上榜明细", key="lhb_yd_go"):
            _run("lhb_yyb_drill", rid2, p2, c2)
        _render_result("lhb_yyb_drill")


@st.cache_data(ttl=600, show_spinner=False)
def _latest_news() -> pd.DataFrame:
    """同花顺数据中心龙虎榜页「最新动态」资讯（龙虎榜解析，公开网页）。"""
    import requests
    url = "https://data.10jqka.com.cn/market/longhu/"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
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
        st.warning("暂未拉到最新动态（同花顺数据中心龙虎榜页），请稍后重试")
    else:
        show = df[["日期", "标题", "链接"]].rename(columns={"链接": "原文"})
        st.dataframe(show, width="stretch", hide_index=True,
                     height=min(35 * (len(show) + 1) + 3, 620),
                     column_config={"原文": st.column_config.LinkColumn("原文", display_text="查看")})
    st.link_button("↗ 打开同花顺数据中心·龙虎榜（原页）",
                   "https://data.10jqka.com.cn/market/longhu/")


def render():
    st.title("🐉 龙虎榜")
    st.caption("数据源：同花顺 iFinD 专题报表（THS_DR）+ 同花顺数据中心公开资讯 · 每日收盘后更新")
    ifind_hub.header()
    t1, t2, t3 = st.tabs(["📋 个股明细", "🏛️ 营业部排名", "📰 最新动态"])
    with t1:
        _tab_detail()
    with t2:
        _tab_yyb()
    with t3:
        _tab_news()


render()
