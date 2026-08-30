"""搜索增强（舆情/新闻）：iFinD 公告抓取 + DeepSeek 舆情摘要与情绪标签。

数据流：
  - 公告优先读 ifind_announcements 落库表（⏰定时任务 job_ifind_announce 每日回填，按 seq 去重）；
    落库为空或强制实时时，走 datasource.ths_announce 现拉（SDK/HTTP 双通道）。
  - 抓到公告标题后，交给 DeepSeek 生成：① 该股票近期舆情一句话摘要；
    ② 每条公告的情绪标签（利好 / 中性 / 利空）与一句话理由。
  - LLM 失败 / 无 key 时 fail-open：只展示原始公告，不挡数据。
"""

import json
import re
from datetime import datetime, timedelta

import pandas as pd

import datasource
from llmutil import llm_chat

_SENTIMENT = {"利好", "中性", "利空"}
_DIMENSION = {"业绩面", "资本运作", "分红回购", "监管", "人事", "行业", "其他"}


# ---------------------------------------------------------------- 抓取
def load_cached_announcements(codes: list[str], days: int = 7) -> pd.DataFrame:
    """从落库表读近 N 天公告（定时任务已回填的优先，省一次 iFinD 调用）。"""
    if not codes:
        return pd.DataFrame()
    # iFinD 落库 code 为 600519.SH 版式，输入可能是 SH600519 —— 统一转 thscode
    ths_codes = [datasource._to_ths_code(c) for c in codes]
    begin = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    placeholders = ",".join("?" * len(ths_codes))
    with datasource._conn() as c:
        df = pd.read_sql(
            f"SELECT seq, code, report_date, title, pdf_url, ctime FROM ifind_announcements"
            f" WHERE code IN ({placeholders}) AND report_date >= ?"
            f" ORDER BY report_date DESC, ctime DESC",
            c, params=(*ths_codes, begin))
    return df


def fetch_announcements(codes: list[str], days: int = 7, live: bool = False) -> pd.DataFrame:
    """公告聚合：落库优先；落库空或 live=True 时现拉 ths_announce 并回填。"""
    cached = load_cached_announcements(codes, days)
    if not live and not cached.empty:
        return cached
    try:
        df, _res, err = datasource.ths_announce(codes, days=int(days))
    except Exception as e:
        # 现拉失败但落库有货，退回落库
        if not cached.empty:
            return cached
        raise RuntimeError(f"iFinD 公告拉取失败：{e}")
    if err not in (0, None) or df is None or df.empty:
        if not cached.empty:
            return cached
        return pd.DataFrame(columns=["seq", "code", "report_date", "title", "pdf_url", "ctime"])
    df.columns = [str(c).lower() for c in df.columns]
    df = df.rename(columns={"reporttitle": "title", "reportdate": "report_date",
                            "pdfurl": "pdf_url", "thscode": "code", "ctime": "ctime"})
    df["seq"] = df.get("seq", pd.Series([], dtype=str)).astype(str)
    df = df[["seq", "code", "report_date", "title", "pdf_url", "ctime"]]
    _persist(df)
    return df


def _persist(df: pd.DataFrame) -> None:
    """把现拉公告增量写回落库表（按 seq 去重）。"""
    if df.empty:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [(str(r.get("seq", "")), str(r.get("code", "")), str(r.get("report_date", ""))[:10],
             str(r.get("title", "")), str(r.get("pdf_url", "")), str(r.get("ctime", "")), now)
            for _, r in df.iterrows() if str(r.get("seq", ""))]
    if not rows:
        return
    with datasource._conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO ifind_announcements"
            "(seq,code,report_date,title,pdf_url,ctime,fetched_at) VALUES (?,?,?,?,?,?,?)", rows)


# ---------------------------------------------------------------- LLM 舆情增强
def _build_prompt(items: list[dict], code: str | None = None) -> str:
    lines = []
    for i, it in enumerate(items):
        lines.append(f"{i + 1}. [{it.get('report_date', '')}] {it['title']}")
    text = "\n".join(lines)
    scope = f"股票 {code} " if code else "下列股票 "
    return (
        "你是 A 股舆情分析师。" + scope + "近期公告/资讯标题如下。\n"
        "请完成：\n"
        f"1) 给{'股票 ' + code if code else '整体'}写一句不超过 60 字的中文舆情摘要"
        "（概括近期主基调：偏利好/中性/偏利空）。\n"
        "2) 给出综合情绪分 score：整数，利好偏多为正、利空偏多为负，范围约 -5..+5。\n"
        "3) 给出关键主题词 themes（最多 5 个，如 业绩/回购/并购/减持/监管）。\n"
        "4) 为每条标注：情绪 sentiment(利好|中性|利空)、理由 reason(≤25字)、"
        "影响程度 impact(高|中|低)、主题 theme(单关键词)、"
        "维度 dimension(业绩面|资本运作|分红回购|监管|人事|行业|其他)。\n\n"
        "只输出如下 JSON（不要任何额外文字、不要 Markdown 代码块）：\n"
        '{"summary": "舆情摘要", "score": 2, "themes": ["业绩"], '
        '"items": [{"idx": 1, "sentiment": "利好|中性|利空", "reason": "理由", '
        '"impact": "高|中|低", "theme": "关键词", "dimension": "业绩面"}]}\n\n'
        "标题列表：\n" + text
    )


def _parse_llm(out: str | None, n: int) -> dict:
    """解析 LLM 输出为 {summary, score, themes, sentiments:{idx: {label,reason,impact,theme,dimension}}}；
    解析失败全部置中性、score=0、impact=中、dimension=其他。"""
    fb_sent = {i + 1: {"label": "中性", "reason": "", "impact": "中",
                       "theme": "", "dimension": "其他"} for i in range(n)}
    fallback = {"summary": "", "score": 0, "themes": [], "sentiments": fb_sent}
    if not out:
        return fallback
    out = out.strip()
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if not m:
        return fallback
    try:
        d = json.loads(m.group(0))
    except Exception:
        return fallback
    sentiments = {}
    for it in d.get("items", []) or []:
        try:
            idx = int(it.get("idx"))
        except Exception:
            continue
        label = str(it.get("sentiment", "中性")).strip()
        if label not in _SENTIMENT:
            label = "中性"
        dim = str(it.get("dimension", "其他")).strip()
        if dim not in _DIMENSION:
            dim = "其他"
        sentiments[idx] = {
            "label": label,
            "reason": str(it.get("reason", ""))[:60],
            "impact": (str(it.get("impact", "中"))[:1] or "中"),
            "theme": str(it.get("theme", ""))[:20],
            "dimension": dim,
        }
    for i in range(1, n + 1):
        sentiments.setdefault(i, fb_sent[i])
    try:
        score = max(-5, min(5, int(d.get("score", 0))))
    except Exception:
        score = 0
    themes = [str(t)[:20] for t in (d.get("themes") or []) if t][:5]
    return {"summary": str(d.get("summary", ""))[:200], "score": score,
            "themes": themes, "sentiments": sentiments}


_MAX_ITEMS_PER_CALL = 40


def llm_enhance(df: pd.DataFrame, max_items: int = _MAX_ITEMS_PER_CALL) -> dict:
    """对公告 DataFrame 做舆情增强。

    按代码分块调用 LLM：避免多股票公告混在一起出笼统结论，同时单块不超过
    max_items 条以防提示超长被截断。返回：
        {summary, score, themes,
         by_code: {code: {summary, score, themes}},
         sentiments: {全局行号(1-based): {label, reason, impact, theme}}}
    LLM 失败 / 无 key 时该块 fail-open 全置中性、score=0，不影响其它块。
    """
    if df is None or df.empty:
        return {"summary": "", "score": 0, "themes": [], "by_code": {}, "sentiments": {}}
    from collections import defaultdict
    rows = [{"gi": i + 1, "code": str(r.get("code", "")), "report_date": str(r.get("report_date", "")),
             "title": str(r.get("title", ""))} for i, (_, r) in enumerate(df.iterrows())]
    by_code_items: dict[str, list[dict]] = defaultdict(list)
    for it in rows:
        by_code_items[it["code"]].append(it)
    sentiments: dict[int, dict] = {}
    by_code: dict[str, dict] = {}
    for code, items in by_code_items.items():
        agg_score = 0
        agg_themes: list[str] = []
        summaries: list[str] = []
        for start in range(0, len(items), max_items):
            chunk = items[start:start + max_items]
            out = llm_chat(
                system="你是严谨的 A 股舆情分析师，输出严格 JSON。",
                user=_build_prompt(chunk, code=code),
                max_tokens=1500)
            parsed = _parse_llm(out, len(chunk))
            for local_i, sd in parsed["sentiments"].items():
                sentiments[chunk[local_i - 1]["gi"]] = sd
            agg_score += parsed["score"]
            agg_themes += parsed["themes"]
            if parsed["summary"]:
                summaries.append(parsed["summary"])
        by_code[code] = {
            "summary": " / ".join(summaries),
            "score": max(-10, min(10, agg_score)),
            "themes": list(dict.fromkeys(agg_themes))[:8],
        }
    overall_score = sum(c["score"] for c in by_code.values())
    overall_themes = list(dict.fromkeys([t for c in by_code.values() for t in c["themes"]]))[:10]
    overall = "　".join(f"{c}：{v['summary']}" for c, v in by_code.items() if v["summary"])
    return {"summary": overall, "score": overall_score, "themes": overall_themes,
            "by_code": by_code, "sentiments": sentiments}
