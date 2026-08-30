"""🔍 搜索增强（舆情/新闻）页：iFinD 公告 + DeepSeek 舆情摘要/情绪标签。

交互：顶部搜索框输代码（逗号分隔，支持 600519 / 600519.SH / SH600519），
或勾选自选股一键批量；选时间窗后点「🚀 搜索增强」，先拉公告再交 LLM 出摘要与情绪。
"""

import io

import pandas as pd
import streamlit as st
from datetime import datetime

import newsense
from common import load_watchlist
from ifind_hub import _codes_input, header as ifind_header


def _norm_pdf_url(u: str) -> str | None:
    """规范化公告 PDF 链接：iFinD 偶尔把 pdfURL 返回成占位/相对主机（如 http://x/1.pdf，
    真实主机被替换但路径+query（含有效 token）尚在）。把这种链接重新挂到规范的
    ft.10jqka.com.cn 主机上，路径与 query 原样保留。

    若连 query（seq/token）都没有（纯占位路径 x/1.pdf），无法重建，返回 None。"""
    if not u or "://" not in u:
        return None
    from urllib.parse import urlparse
    p = urlparse(u)
    if not p.netloc or p.netloc in ("x", "X") or "." not in p.netloc:
        if p.query:  # 仅当路径后还带着 seq/token 等真实参数才可重建
            return "http://ft.10jqka.com.cn" + (p.path or "") + ("?" + p.query)
        return None
    return u


@st.cache_data(show_spinner=False, ttl=3600)
def _load_announce_pdf(url: str):
    """服务端尽力下载公告 PDF 并抽取正文；返回 (pdf_bytes|None, text, err)。
    仅作"加分项"（抽正文 / st.pdf），失败不影响浏览器内嵌预览。显式关闭代理以免误走代理。"""
    import io
    import time
    from urllib.parse import urlparse

    import requests
    url = _norm_pdf_url(url)
    if not url:
        return None, "", "数据源未返回可用 PDF 链接（pdfURL 为占位/无效地址，无法重建）"
    host = urlparse(url).netloc or url
    if not url.startswith(("http://", "https://")):
        return None, "", f"URL 非法：{url[:60]}"
    data = None
    last_err = ""
    for _ in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                             timeout=30, proxies={"http": None, "https": None})
            r.raise_for_status()
            if r.content[:4] == b"%PDF":
                data = r.content
                break
            last_err = "返回内容不是 PDF"
        except Exception as e:
            last_err = e
        time.sleep(1)
    if data is None:
        return None, "", f"服务端无法抓取（主机 {host} 不可达，可能需特定网络/浏览器访问）：{last_err}"

    text = ""
    try:
        import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        text = "\n".join(p.get_text() for p in doc)
    except Exception:
        try:
            from pypdf import PdfReader
            text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        except Exception:
            text = ""
    return data, text, ""


st.title("📰 舆情 / 新闻")

# 数据源/凭证状态条（复用 iFinD 通用自检）
ifind_header()

# 公告回填（⏰定时任务 ifind_announce）状态 + 手动触发
try:
    from scheduler import get_scheduler as _get_sched
    _ann = _get_sched().view().get("ifind_announce", {})
    if _ann.get("enabled"):
        _st = f"📜 公告自动回填：**已开启** · 每日 {_ann.get('hour', 15):02d}:{_ann.get('minute', 40):02d}" \
              f" · 范围 {_ann['params'].get('pool_name', '自选股')} · 近 {_ann['params'].get('days', 7)} 天"
        _last = _ann.get("last")
        if _last:
            _st += f"　·　上次：{'✅' if _last['ok'] else '❌'}{_last.get('at', '')[:16]}"
        st.success(_st)
        if st.button("⤴️ 立即回填公告库（后台）", help="手动触发一次 job_ifind_announce，把最新公告写回落库表"):
            _get_sched().run_now("ifind_announce")
            st.toast("已触发公告回填（后台执行，稍后刷新看结果）")
    else:
        st.caption("📜 公告自动回填：**未开启**（⏰定时任务 页可开启；也可勾选下方「实时拉取」直接现拉）")
except Exception:
    pass

st.caption("数据：同花顺 iFinD 公告（⏰定时任务每日回填，可强制实时）　·　增强：DeepSeek 舆情摘要 + 利好/中性/利空 标签")

# ---------------------------------------------------------------- 输入区
watch = load_watchlist()
sel = st.multiselect("📋 自选股批量（勾选后并入搜索范围）", watch,
                     help="留空则只用下方搜索框输入的代码")
codes = _codes_input("🔎 搜索代码（逗号分隔）", "600519,300750", "ns_codes")

# 合并：手动输入 ∪ 自选勾选
all_codes = list(dict.fromkeys([*codes, *sel]))
days = st.selectbox("时间窗（近 N 天）", [3, 7, 15, 30], index=1)
live = st.checkbox("实时拉取（绕过落库缓存，重新请求 iFinD）", value=False)

c1, c2 = st.columns([1, 3])
with c1:
    go = st.button("🚀 搜索增强", type="primary", disabled=not all_codes)
with c2:
    if st.button("🧹 清空结果"):
        for k in list(st.session_state):
            if k.startswith("ns_"):
                del st.session_state[k]

if go and all_codes:
    with st.spinner(f"拉取 {len(all_codes)} 只公告并做舆情增强…"):
        try:
            df = newsense.fetch_announcements(all_codes, days=days, live=live)
        except Exception as e:
            st.error(str(e))
            df = None
        if df is not None:
            st.session_state["ns_df"] = df
            st.session_state["ns_searched"] = all_codes
            st.session_state["ns_days"] = days
            st.session_state["ns_enh"] = newsense.llm_enhance(df)

# ---------------------------------------------------------------- 结果区
df = st.session_state.get("ns_df")
if df is None:
    st.info("输入代码或勾选自选股，点「🚀 搜索增强」开始。")
    st.stop()

enh = st.session_state.get("ns_enh", {"summary": "", "sentiments": {}, "by_code": {}})
n = len(df) if df is not None else 0
st.caption(           f"命中 **{n}** 条公告　·　范围 {', '.join(st.session_state.get('ns_searched', []))}"
           f"　·　近 {st.session_state.get('ns_days', days)} 天"
           + ("　·　🤖 DeepSeek 舆情增强已开启" if enh.get("by_code") else "　·　⚠️ 未启用 LLM（无 key / 调用失败，仅展示原始公告）"))

if df is not None and not df.empty:
    # 导出 CSV（含情绪/理由/影响/主题，便于归档与二次分析）
    try:
        _out = io.StringIO()
        _exp = df.copy()
        sentiments_map = enh.get("sentiments", {})
        _exp["sentiment"] = [sentiments_map.get(i, {}).get("label", "中性") for i in range(1, len(_exp) + 1)]
        _exp["reason"] = [sentiments_map.get(i, {}).get("reason", "") for i in range(1, len(_exp) + 1)]
        _exp["impact"] = [sentiments_map.get(i, {}).get("impact", "中") for i in range(1, len(_exp) + 1)]
        _exp["theme"] = [sentiments_map.get(i, {}).get("theme", "") for i in range(1, len(_exp) + 1)]
        _exp["dimension"] = [sentiments_map.get(i, {}).get("dimension", "其他") for i in range(1, len(_exp) + 1)]
        _exp.to_csv(_out, index=False)
        st.download_button("⬇️ 导出 CSV", _out.getvalue(),
                           file_name=f"newsense_{datetime.now():%Y%m%d_%H%M}.csv", mime="text/csv")
    except Exception:
        pass

    # ---------------------------------------------------------------- 舆情概览
    sentiments_map = enh.get("sentiments", {})
    counts = {"利好": 0, "中性": 0, "利空": 0}
    for i in range(1, n + 1):
        lab = sentiments_map.get(i, {}).get("label", "中性")
        if lab in counts:
            counts[lab] += 1
    score = enh.get("score", 0)
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("📋 公告总数", n)
    m2.metric("🟢 利好", counts["利好"])
    m3.metric("⚪ 中性", counts["中性"])
    m4.metric("🔴 利空", counts["利空"])
    m5.metric("📊 综合情绪分", f"{score:+d}", help="利好偏多为正、利空偏多为负（每票累加，范围约 -10..+10）")
    themes = enh.get("themes", [])
    if themes:
        st.markdown("**🏷️ 关键主题：** " + "　".join(f"`{t}`" for t in themes))
    # 情绪分布条
    if n:
        _dist = pd.DataFrame({"情绪": ["利好", "中性", "利空"], "条数": [counts["利好"], counts["中性"], counts["利空"]]})
        st.bar_chart(_dist.set_index("情绪"), use_container_width=True)

    # 按代码汇总表
    from collections import defaultdict
    per = defaultdict(lambda: {"n": 0, "pos": 0, "neg": 0})
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        c = str(r.get("code", ""))
        lab = sentiments_map.get(i, {}).get("label", "中性")
        per[c]["n"] += 1
        if lab == "利好":
            per[c]["pos"] += 1
        elif lab == "利空":
            per[c]["neg"] += 1
    rows = []
    for c, d in per.items():
        基调 = "利好" if d["pos"] > d["neg"] else ("利空" if d["neg"] > d["pos"] else "中性")
        bc = enh.get("by_code", {}).get(c, {})
        rows.append({"代码": c, "条数": d["n"], "利好": d["pos"], "利空": d["neg"],
                     "主基调": 基调, "情绪分": f"{bc.get('score', 0):+d}",
                     "舆情摘要": (bc.get("summary") or "")[:80]})
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    # 公告情绪时间线（可选 周/月/日 粒度）
    try:
        import altair as alt
        _gran = (st.segmented_control("时间粒度", ["日", "周", "月", "季度"], default="日", key="ns_gran")
                  if hasattr(st, "segmented_control")
                  else st.selectbox("时间粒度", ["日", "周", "月", "季度"], key="ns_gran"))
        _freq = {"日": "D", "周": "W", "月": "M", "季度": "Q"}[_gran]
        _per = pd.to_datetime(df["report_date"], errors="coerce").dt.to_period(_freq)
        _tl = pd.DataFrame({
            "period": _per.astype(str),
            "label": [sentiments_map.get(i, {}).get("label", "中性") for i in range(1, n + 1)],
            "cnt": 1,
        })
        _piv = _tl.pivot_table(index="period", columns="label", values="cnt", aggfunc="sum", fill_value=0)
        for _c in ["利好", "中性", "利空"]:
            if _c not in _piv:
                _piv[_c] = 0
        _piv = _piv[["利好", "中性", "利空"]]
        _melt = _piv.reset_index().melt(id_vars="period", var_name="情绪", value_name="条数")
        _chart = (
            alt.Chart(_melt).mark_bar()
            .encode(x=alt.X("period:N", title="时间", sort=None),
                    y=alt.Y("条数:Q", title="公告数"),
                    color=alt.Color("情绪:N", scale=alt.Scale(
                        domain=["利好", "中性", "利空"],
                        range=["#2ca02c", "#bbbbbb", "#d62728"])),
                    tooltip=["period", "情绪", "条数"])
            .properties(height=260, title=f"📈 公告情绪时间线（按{_gran}）")
        )
        st.altair_chart(_chart, use_container_width=True)
    except Exception:
        pass

    # 维度细分（业绩面 / 资本运作 / 分红回购 / 监管 / 人事 / 行业 / 其他）
    try:
        _dd = pd.DataFrame({
            "dim": [sentiments_map.get(i, {}).get("dimension", "其他") for i in range(1, n + 1)],
            "label": [sentiments_map.get(i, {}).get("label", "中性") for i in range(1, n + 1)],
            "cnt": 1,
        })
        _dp = _dd.pivot_table(index="dim", columns="label", values="cnt", aggfunc="sum", fill_value=0)
        for _c in ["利好", "中性", "利空"]:
            if _c not in _dp:
                _dp[_c] = 0
        _dp = _dp[["利好", "中性", "利空"]]
        _dp["总"] = _dp.sum(axis=1)
        _dp = _dp.sort_values("总", ascending=False)
        st.markdown("**🧩 舆情维度细分**")
        _order = _dp.index.tolist()
        _m2 = _dp.reset_index().melt(id_vars="dim", var_name="情绪", value_name="条数")
        _m2["dim"] = pd.Categorical(_m2["dim"], categories=_order, ordered=True)
        _dchart = (
            alt.Chart(_m2).mark_bar()
            .encode(x=alt.X("dim:N", title="维度", sort=None),
                    y=alt.Y("条数:Q", title="公告数"),
                    color=alt.Color("情绪:N", scale=alt.Scale(
                        domain=["利好", "中性", "利空"],
                        range=["#2ca02c", "#bbbbbb", "#d62728"])),
                    tooltip=["dim", "情绪", "条数"])
            .properties(height=260, title="舆情维度分布（按情绪）")
        )
        st.altair_chart(_dchart, use_container_width=True)
        st.dataframe(_dp.reset_index().rename(columns={"dim": "维度", "利好": "🟢利好",
                     "中性": "⚪中性", "利空": "🔴利空"}), width="stretch", hide_index=True)
    except Exception:
        pass

    # 情绪筛选
    if hasattr(st, "segmented_control"):
        filt = st.segmented_control("情绪筛选", ["全部", "利好", "中性", "利空"], default="全部")
    else:
        filt = st.selectbox("情绪筛选", ["全部", "利好", "中性", "利空"])
    keep = None if filt == "全部" else filt

    # ---------------------------------------------------------------- 按代码分组展示
    groups: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        groups[str(r.get("code", ""))].append((i, r.to_dict()))

    SENT_COLOR = {"利好": "🟢", "中性": "⚪", "利空": "🔴"}
    IMPACT_ICON = {"高": "🔥", "中": "·", "低": "·"}
    for code, items in groups.items():
        csum = enh.get("by_code", {}).get(code)
        visible = [(i, r) for (i, r) in items
                   if keep is None or sentiments_map.get(i, {}).get("label", "中性") == keep]
        if not visible:
            continue
        if csum:
            bc = enh.get("by_code", {}).get(code, {})
            st.info(f"📊 **{code}** 舆情摘要：{bc.get('summary', '')}"
                     f"　｜　情绪分 **{bc.get('score', 0):+d}**"
                     + (f"　｜　主题 {'、'.join(bc.get('themes', []))}" if bc.get("themes") else ""))
        for i, r in visible:
            sd = enh.get("sentiments", {}).get(i, {"label": "中性", "reason": "", "impact": "中", "theme": "", "dimension": "其他"})
            label, reason, impact, theme, dim = (sd.get("label", "中性"), sd.get("reason", ""),
                                                sd.get("impact", "中"), sd.get("theme", ""), sd.get("dimension", "其他"))
            icon = SENT_COLOR.get(label, "⚪")
            title = str(r.get("title", "")) or "(无标题)"
            date = str(r.get("report_date", ""))[:10]
            pdf = str(r.get("pdf_url", "") or "")
            key = str(r.get("seq") or f"{code}_{title}")
            with st.container(border=True):
                col1, col2 = st.columns([1, 5])
                col1.markdown(f"**{icon} {label}**")
                col2.markdown(f"**{title}**", help=reason)
                sub = f"`{code}`　·　📅 {date}"
                if impact:
                    sub += f"　·　{IMPACT_ICON.get(impact, '·')}影响{impact}"
                if dim and dim != "其他":
                    sub += f"　·　🧩 {dim}"
                if theme:
                    sub += f"　·　🏷️ {theme}"
                if reason:
                    sub += f"　·　💡 {reason}"
                col2.caption(sub)
                if pdf.startswith("http"):
                    if st.button("📄 查看公告正文 / PDF", key=f"view_{key}"):
                        st.session_state[f"show_{key}"] = not st.session_state.get(f"show_{key}", False)
                    if st.session_state.get(f"show_{key}"):
                        pdf_view = _norm_pdf_url(pdf)
                        if not pdf_view:
                            st.caption("⚠️ 该公告未返回可用 PDF 链接（数据源 pdfURL 为占位/无效地址，"
                                       "无法预览或下载，仅展示标题与舆情标签）。")
                        else:
                            # 浏览器侧内嵌预览：由用户浏览器直接拉 PDF，绕开服务端网络限制
                            st.markdown(
                                f'<iframe src="{pdf_view}" width="100%" height="600" '
                                f'style="border:1px solid #444;border-radius:6px"></iframe>',
                                unsafe_allow_html=True)
                            st.markdown(
                                f'<a href="{pdf_view}" target="_blank" rel="noopener">⬇️ 在新标签打开 / 下载 PDF</a>',
                                unsafe_allow_html=True)
                            # 服务端尽力抓取：抽正文 + st.pdf（失败不阻断，已用上方内嵌兜底）
                            with st.spinner("服务端尝试抓取公告（抽正文）…"):
                                b, text, err = _load_announce_pdf(pdf)
                            if err:
                                st.caption(f"⚠️ {err}")
                            else:
                                if text.strip():
                                    st.text_area("公告正文", text, height=320, key=f"txt_{key}")
                                else:
                                    st.caption("（服务端未能从 PDF 抽取文本，可用上方内嵌/新标签查看）")
                                try:
                                    st.pdf(b)
                                except Exception:
                                    pass
else:
    st.warning("该范围内近 N 天无公告（或 iFinD 未返回）。")
