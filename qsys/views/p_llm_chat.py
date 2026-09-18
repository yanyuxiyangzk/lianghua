"""🧠 涨停复盘对话：选中涨停股 → 系统组装全景数据包 → DeepSeek 对话分析。

与普通问答的本质区别：LLM 的上下文是本系统实时组装的数据包
（行情快照/资金分量/支撑阻力/龙虎榜/板块/公告/连板 + 今日选股记录与因子评分卡），
因此能回答"我的系统为何选中/漏选它、因子和策略该怎么改进"。
"""

import json
from datetime import datetime

import pandas as pd
import streamlit as st

import broker
import common
import datasource
import density_sr
import experience
from llmutil import llm_chat_multi, llm_available


# ---------------------------------------------------------------- 涨停检测
def _limit_threshold(code: str, name: str = "") -> float:
    """涨停阈值%：北交所30 / 创业板·科创板20 / ST 5 / 主板10（留 0.2 裕度判贴板）。"""
    if "ST" in str(name).upper():
        return 4.8
    if code.startswith("BJ"):
        return 29.8
    if code.startswith(("SZ30", "SH688")):
        return 19.8
    return 9.8


def _today_limit_ups() -> pd.DataFrame:
    """从 stocklist（实时快照已覆盖）筛今日涨停，附连板数。"""
    df = datasource.get_stocklist_from_db()
    if df.empty:
        return pd.DataFrame()
    df = df.dropna(subset=["change_pct", "price"])
    df["_thr"] = [_limit_threshold(c, n) for c, n in zip(df["code"], df["name"])]
    ups = df[df["change_pct"] >= df["_thr"]].copy()
    if ups.empty:
        return ups
    ups["连板"] = [_limit_streak(c) for c in ups["code"]]
    return ups.sort_values(["连板", "amount"], ascending=[False, False])


def _limit_streak(code: str) -> int:
    """截至最新日的连续涨停天数（market_daily 日线口径）。"""
    with datasource._conn() as c:
        rows = c.execute(
            "SELECT date, close, open FROM market_daily WHERE source='ths_ifind' AND code=? "
            "ORDER BY date DESC LIMIT 12", (code,)).fetchall()
    if len(rows) < 2:
        return 1
    thr = _limit_threshold(code)
    streak = 0
    closes = [(r[0], r[1]) for r in rows]
    for i in range(len(closes) - 1):
        chg = (closes[i][1] / closes[i + 1][1] - 1) * 100 if closes[i + 1][1] else 0
        if chg >= thr - 0.15:
            streak += 1
        else:
            break
    return max(streak, 1)


# ---------------------------------------------------------------- 数据包组装（本页的灵魂）
def _build_context(code: str) -> tuple[str, dict]:
    """组装该票的全景数据包（本地库只读，亚秒级）。返回 (文本, 摘要dict)。"""
    meta = {"code": code}
    S = []

    # 1) 行情快照
    sl = datasource.get_stocklist_from_db()
    row = sl[sl["code"] == code]
    if not row.empty:
        r = row.iloc[0]
        meta["name"] = r.get("name", "")
        meta["price"] = r.get("price")
        meta["chg"] = r.get("change_pct")
        S.append(f"【行情】{r.get('name','')}({code}) 现价{r.get('price')}元 涨跌幅{r.get('change_pct'):+.2f}% "
                 f"成交额{(r.get('amount') or 0)/1e8:.1f}亿 换手{r.get('turnover')}% 量比{r.get('quantity_ratio')} "
                 f"PE{r.get('pe_ttm')} 流通市值{(r.get('float_mv') or 0)/1e8:.0f}亿")

    # 2) 连板与K线近况
    with datasource._conn() as c:
        bars = pd.read_sql(
            "SELECT date, open, high, low, close, volume, amount FROM market_daily "
            "WHERE source='ths_ifind' AND code=? ORDER BY date DESC LIMIT 12", c, params=(code,))
    if not bars.empty:
        bars = bars.iloc[::-1]
        streak = _limit_streak(code)
        meta["streak"] = streak
        last = bars.iloc[-1]
        # 开板判定看最低价是否跌破当日最高（涨停价）：close==high 但 low 远低于 high
        # = 盘中开过板后回封（通鼎互联 09-15 正是如此，原来只看 close<high 误判成一字）
        open_board = "否（一字/封死）" if last["low"] >= last["high"] * 0.9999 else "是（盘中开过板）"
        chg5 = (bars["close"].iloc[-1] / bars["close"].iloc[-6] - 1) * 100 if len(bars) >= 6 else None
        S.append(f"【连板】近10日第{streak}板 · 今日是否开板：{open_board}"
                 + (f" · 近5日涨幅 {chg5:+.1f}%" if chg5 is not None else "（本地日线数据不足5日）"))

    # 3) 资金流分量 + 近5日主力
    with datasource._conn() as c:
        ff = pd.read_sql(
            "SELECT date, main_net, super_net, big_net, mid_net, small_net FROM stock_fundflow_daily "
            "WHERE code=? ORDER BY date DESC LIMIT 5", c, params=(code,))
    if not ff.empty:
        t = ff.iloc[0]
        meta["main_net"] = t["main_net"]
        flow_days = "/".join(str(d)[5:] for d in ff["date"].iloc[::-1])  # 标注时间轴（LLM 提示过没标）
        S.append(f"【资金流·最新{t['date']}】主力净额{(t['main_net'] or 0)/1e8:+.2f}亿 "
                 f"（超大{(t['super_net'] or 0)/1e8:+.2f} 大{(t['big_net'] or 0)/1e8:+.2f} "
                 f"中{(t['mid_net'] or 0)/1e8:+.2f} 小{(t['small_net'] or 0)/1e8:+.2f}）；"
                 f"近5日主力净额（{flow_days}） {[round(x/1e8,2) for x in ff['main_net'].iloc[::-1]]} 亿")

    # 4) 支撑阻力（Density-SR）
    sr_df, sr_date = density_sr.load_scan()
    if not sr_df.empty and code in set(sr_df["code"]):
        r = sr_df[sr_df["code"] == code].iloc[0]
        meta["sr"] = f"支撑{r['sup_lo']}~{r['sup_hi']}"
        S.append(f"【支撑阻力·{sr_date}】支撑区 {r['sup_lo']}~{r['sup_hi']}（距离{r['sup_dist_atr']}ATR，"
                 f"触及概率{r['p_touch']:.0%}，守住概率{r['p_hold']:.0%}，{r['resonance']}窗共振）；"
                 f"阻力区 {r['res_lo']}~{r['res_hi']}")

    # 5) 龙虎榜
    with datasource._conn() as c:
        lhb_cnt = c.execute(
            "SELECT COUNT(*) FROM lhb_daily WHERE code=? AND date>=date('now','-30 day')",
            (code,)).fetchone()[0]
        lhb = pd.read_sql("SELECT date, net_buy FROM lhb_daily WHERE code=? ORDER BY date DESC LIMIT 3",
                          c, params=(code,))
    if not lhb.empty:
        S.append(f"【龙虎榜】近30天上榜{lhb_cnt}次，最近 {lhb.iloc[0]['date']} 净买额"
                 f" {(lhb.iloc[0]['net_buy'] or 0)/1e8:+.2f}亿")
    else:
        S.append("【龙虎榜】近30天未上榜（未覆盖=无数据，与未上榜是两回事，此处为后者）")

    # 6) 板块
    with datasource._conn() as c:
        ind = c.execute("SELECT sector_name FROM stock_industry WHERE code=? LIMIT 1", (code,)).fetchone()
    if ind:
        with datasource._conn() as c:
            sec = pd.read_sql(
                "SELECT sector_name, flow_net, avg_chg_pct, up_count, down_count FROM sector_daily "
                "WHERE date=(SELECT MAX(date) FROM sector_daily) AND sector_name=?", c, params=(ind[0],))
        if not sec.empty:
            s0 = sec.iloc[0]
            S.append(f"【板块】{ind[0]}：今日净流入{(s0['flow_net'] or 0)/1e8:+.2f}亿 "
                     f"均涨幅{s0['avg_chg_pct']:.2f}% 涨/跌家数 {s0['up_count']}/{s0['down_count']}")
        else:
            S.append(f"【板块】{ind[0]}")

    # 7) 公告
    with datasource._conn() as c:
        ann = pd.read_sql("SELECT report_date, title FROM ifind_announcements WHERE code=? "
                          "ORDER BY ctime DESC LIMIT 5", c, params=(code,))
    if not ann.empty:
        titles = "；".join(f"{str(r['report_date'])[:10]}《{r['title']}》" for _, r in ann.iterrows())
        S.append(f"【公告】{titles}")

    # 8) 系统侧：今日选股/持仓/因子冷热（picks 在 experience.db，别用 datasource._conn）
    latest_day = None
    try:
        with experience._conn() as c:
            latest_day = c.execute("SELECT MAX(trade_date) FROM picks").fetchone()[0]
    except Exception:
        pass
    picked_by, pick_scores = [], []
    if latest_day:
        for p in experience.picks_on_date(latest_day).itertuples():
            items = experience.pick_items_detail(int(p.id))
            hit = items[items["code"] == code] if not items.empty else pd.DataFrame()
            if not hit.empty:
                picked_by.append(f"{p.pack_name or p.source}(#{int(hit.iloc[0]['rank'])})")
                pick_scores.append(float(hit.iloc[0]["score"]))
    pos = broker.get_positions()
    held = not pos.empty and code in set(pos["code"])
    meta["picked_by"] = picked_by
    meta["held"] = held
    S.append(f"【我的系统·{latest_day}】今日选股{'选中了它：' + '、'.join(picked_by) if picked_by else '未选它'}"
             f"；持仓状态：{'持有中' if held else '未持仓'}")

    try:
        with datasource._conn() as c:
            sc = pd.read_sql(
                "SELECT name, ic_mean, ic_winrate, eval_date FROM factor_scorecards "
                "WHERE eval_date=(SELECT MAX(eval_date) FROM factor_scorecards)", c)
        if not sc.empty:
            sc_date = sc["eval_date"].iloc[0]
            top = sc.nlargest(3, "ic_mean")
            bot = sc.nsmallest(3, "ic_mean")
            S.append(f"【因子冷热·体检日 {sc_date}】最热：" +
                     "、".join(f"{r['name']}({r['ic_mean']:+.3f})" for _, r in top.iterrows()) +
                     "；最冷：" + "、".join(f"{r['name']}({r['ic_mean']:+.3f})" for _, r in bot.iterrows()))
    except Exception:
        pass

    return "\n".join(S), meta


# ---------------------------------------------------------------- 对话
_SYSTEM = """你是量化复盘分析师，服务于一位有自己的量化系统的用户。系统会给你注入一只股票的实时全景数据包（本地量化系统组装），以及用户的问题。

回答要求：
1. 归因要有证据链：引用数据包里的具体数字（资金分量/连板/板块/龙虎榜/公告/支撑阻力）
2. 区分事实与推测；数据包没有的信息明说"数据包未覆盖"，不要编造
3. 回答"系统改进"类问题时，落到可执行项：哪类因子该加权/降权、建议新建什么因子（给出 LoopEngine 表达式示例）、过滤器/闸门规则怎么调
4. 简洁专业，用 Markdown；风险提示收尾
5. 量化信号仅供参考，不构成投资建议"""

_SYSTEM_GENERAL = """你是量化投资助手。用户暂时没有选定具体股票，回答一般性量化/市场问题即可。
如果用户的问题明显针对某只股票但没报代码/名称，提示用户报出代码或名称（支持 600519 / SH600519 / 600519.SH / 股票全称，系统会自动加载该票的数据包）。
简洁专业，量化信号仅供参考，不构成投资建议。"""


def _extract_code_from_text(text: str) -> str | None:
    """从自然语言问题里识别股票：6 位代码（任意写法）或股票全称（长名优先防误配）。"""
    import re
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if m:
        d = m.group(1)
        code = ("SH" if d.startswith("6") else "SZ" if d.startswith(("0", "3")) else "BJ") + d
        with datasource._conn() as c:
            hit = c.execute("SELECT code FROM ifind_stocklist WHERE code=?", (code,)).fetchone()
        if hit:
            return code
    with datasource._conn() as c:
        names = c.execute("SELECT code, name FROM ifind_stocklist WHERE name IS NOT NULL").fetchall()
    for code, name in sorted(names, key=lambda x: -len(x[1] or "")):
        if name and len(name) >= 2 and name in text:
            return code
    return None


# ---------------------------------------------------------------- 对话持久化（落 experience.db）
_CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,           -- 股票代码 或 general
    role TEXT NOT NULL,
    content TEXT,
    created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_chat_channel ON chat_history(channel, id);
CREATE TABLE IF NOT EXISTS chat_contexts(
    channel TEXT NOT NULL,           -- 当日注入的数据包留档（回答可复现的依据）
    date TEXT NOT NULL,
    ctx TEXT,
    created_at TEXT,
    PRIMARY KEY(channel, date));
"""


def _chat_save(channel: str, role: str, content: str):
    """写一条对话；持久化失败不影响对话本身（fail-open）。"""
    try:
        with experience._conn() as c:
            c.executescript(_CHAT_SCHEMA)
            c.execute("INSERT INTO chat_history (channel, role, content, created_at) VALUES (?,?,?,?)",
                      (channel, role, content, datetime.now().strftime("%F %T")))
    except Exception:
        pass


def _chat_ctx_save(code: str, ctx: str):
    """数据包按（票, 日）留档——之后复查能知道 AI 当时看到了什么。"""
    try:
        with experience._conn() as c:
            c.executescript(_CHAT_SCHEMA)
            c.execute("INSERT OR REPLACE INTO chat_contexts (channel, date, ctx, created_at) "
                      "VALUES (?,?,?,?)",
                      (code, datetime.now().strftime("%Y-%m-%d"), ctx,
                       datetime.now().strftime("%F %T")))
    except Exception:
        pass


def _chat_load(channel: str, limit: int = 200) -> list[dict]:
    """读历史对话（按时间正序）。"""
    try:
        with experience._conn() as c:
            c.executescript(_CHAT_SCHEMA)
            rows = c.execute(
                "SELECT role, content, created_at FROM chat_history WHERE channel=? "
                "ORDER BY id DESC LIMIT ?", (channel, limit)).fetchall()
        return [{"role": r, "content": ct, "ts": ts} for r, ct, ts in reversed(rows)]
    except Exception:
        return []


def _chat_channels() -> list:
    """历史会话清单（频道, 消息数, 最近时间）。"""
    try:
        with experience._conn() as c:
            c.executescript(_CHAT_SCHEMA)
            return c.execute(
                "SELECT channel, COUNT(*), MAX(created_at) FROM chat_history "
                "GROUP BY channel ORDER BY MAX(id) DESC").fetchall()
    except Exception:
        return []


def _chat_clear(channel: str):
    with experience._conn() as c:
        c.executescript(_CHAT_SCHEMA)
        c.execute("DELETE FROM chat_history WHERE channel=?", (channel,))


def _send(question: str, code: str | None):
    """发送一轮对话：组装消息 → LLM → 落历史。code=None 走普通问答（无数据包）。"""
    if not code:
        hist_key = "chat_hist_general"
        hist = st.session_state.get(hist_key, [])
        msgs = [{"role": "system", "content": _SYSTEM_GENERAL}]
        msgs += hist[-8:]
        msgs.append({"role": "user", "content": question})
        with st.spinner("DeepSeek v4-pro 思考中…"):
            reply = llm_chat_multi(msgs)
        if not reply:
            reply = "⚠️ LLM 暂不可用或思考超长，请稍后重试。"
        hist.append({"role": "user", "content": question})
        hist.append({"role": "assistant", "content": reply})
        st.session_state[hist_key] = hist
        _chat_save("general", "user", question)
        _chat_save("general", "assistant", reply)
        return
    ctx_key = f"chat_ctx_{code}"
    hist_key = f"chat_hist_{code}"
    if ctx_key not in st.session_state:
        with st.spinner("组装数据包…"):
            ctx, meta = _build_context(code)
        st.session_state[ctx_key] = ctx
        st.session_state[f"chat_meta_{code}"] = meta
        st.session_state[hist_key] = _chat_load(code)  # 从库里恢复该股历史对话
        _chat_ctx_save(code, ctx)                      # 数据包留档（回答可复现）
    ctx = st.session_state[ctx_key]
    hist = st.session_state[hist_key]

    msgs = [{"role": "system", "content": _SYSTEM + "\n\n# 数据包\n" + ctx}]
    msgs += hist[-8:]  # 最近 4 轮（user+assistant 各 4 条）
    msgs.append({"role": "user", "content": question})
    with st.spinner("DeepSeek v4-pro 思考中…（推理模型，约 5-15 秒）"):
        reply = llm_chat_multi(msgs)
    if not reply:  # None（异常）或空串（推理把配额想完了）都按失败提示
        reply = "⚠️ LLM 暂不可用或思考超长（检查 DEEPSEEK_API_KEY / 网络），请稍后重试。"
    hist.append({"role": "user", "content": question})
    hist.append({"role": "assistant", "content": reply})
    st.session_state[hist_key] = hist
    _chat_save(code, "user", question)
    _chat_save(code, "assistant", reply)


_QUICK = [
    ("为什么涨停", "分析这只股票今天涨停的原因：资金结构（各档分量）、板块联动、公告/消息催化、龙虎榜、技术位置。给出证据链。"),
    ("能持续吗", "评估这个涨停的持续性：连板高度、是否炸板、主力vs散户资金结构、支撑阻力距离与守住概率、风险点。给出明日观察要点。"),
    ("系统为何选/漏选", "对照数据包里的「我的系统」部分：分析我的系统今天为什么选中了它（或没选它），命中/被拦的具体因子和过滤器是什么，这个位置买入/错过的历史相似案例表现如何。"),
    ("因子策略怎么改进", "基于这个涨停案例和我的系统现状（因子冷热/选股记录），回答：①涨停归因 ②系统为何选中或漏选 ③哪类因子该加权/降权 ④建议新建什么因子（给 LoopEngine 表达式示例，字段：open/high/low/close/volume/amount/vwap/overnight/amplitude 及资金流 main_net_pct 等，算子 ma/ema/std/ts_rank/rank_cs/corr/delta/roc/sub/mul/div 等）⑤过滤器/闸门规则调整 ⑥可执行行动项清单。"),
]


# ---------------------------------------------------------------- 页面
def render():
    st.title("🧠 涨停复盘 · 对话分析")
    st.caption("DeepSeek v4-pro · 上下文由本系统实时组装（行情/资金/支撑阻力/龙虎榜/公告/选股记录）"
               " · 对话里提到股票名/代码即自动加载该票数据包")

    if not llm_available():
        st.error("未配置 LLM Key（DEEPSEEK_API_KEY），对话不可用")
        st.stop()

    # ---- 股票选择 ----
    ups = _today_limit_ups()
    qcode = st.query_params.get("code", "")
    c1, c2 = st.columns([2.4, 1.2])
    with c1:
        if not ups.empty:
            opts = [f"{r['code']} {r['name']}（{r['change_pct']:+.1f}% · {r['连板']}板）"
                    for _, r in ups.iterrows()]
            default_i = next((i for i, o in enumerate(opts) if o.startswith(qcode)), None)
            pick = st.selectbox("今日涨停（按连板/成交额排序）", opts, index=default_i,
                                placeholder="选择涨停股…", key="chat_pick")
            sel_code = pick.split()[0] if pick else ""
        else:
            # 非交易日/无涨停：回退到最近观察清单（盘后任务 limit_up_watch 落库）
            watch = pd.DataFrame()
            try:
                with datasource._conn() as c:
                    wd = c.execute("SELECT MAX(date) FROM limit_up_watch").fetchone()[0]
                    if wd:
                        watch = pd.read_sql(
                            "SELECT code, name, chg_pct, quantity_ratio, kind FROM limit_up_watch "
                            "WHERE date=? ORDER BY kind, chg_pct DESC", c, params=(wd,))
            except Exception:
                pass
            if not watch.empty:
                opts = [f"{r['code']} {r['name']}（{r['chg_pct']:+.1f}% · {r['kind']}）"
                        for _, r in watch.iterrows()]
                default_i = next((i for i, o in enumerate(opts) if o.startswith(qcode)), None)
                pick = st.selectbox(f"最近观察清单（{wd} 涨停+放量异动）", opts, index=default_i,
                                    placeholder="选择股票…", key="chat_pick_watch")
                sel_code = pick.split()[0] if pick else ""
            else:
                st.info("今日无涨停（或非交易日）——可直接输代码分析任意股票")
                sel_code = ""
    with c2:
        manual = st.text_input("或输入代码", value=qcode if qcode else "",
                               placeholder="如 600519 / SH600519", key="chat_manual")
    if manual.strip():
        import re
        m = re.match(r"(?i)^\s*(?:SH|SZ|BJ)?(\d{6})(?:\.(?:SH|SZ|BJ))?\s*$", manual.strip())
        if m:
            d = m.group(1)
            sel_code = ("SH" if d.startswith("6") else "SZ" if d.startswith(("0", "3")) else "BJ") + d \
                if not manual.strip().upper().startswith(("SH", "SZ", "BJ")) else manual.strip().upper()
        else:
            st.warning("代码格式不对")
            sel_code = ""

    # 活跃分析对象：显式选择优先，否则沿用上次（对话中识别到新股会切换）
    if sel_code:
        st.session_state["chat_active_code"] = sel_code
    active = st.session_state.get("chat_active_code", "")

    if active:
        # ---- 数据包摘要卡 ----
        ctx_key = f"chat_ctx_{active}"
        if ctx_key not in st.session_state:
            with st.spinner("组装数据包…"):
                ctx, meta = _build_context(active)
            st.session_state[ctx_key] = ctx
            st.session_state[f"chat_meta_{active}"] = meta
            st.session_state[f"chat_hist_{active}"] = []
        meta = st.session_state.get(f"chat_meta_{active}", {})

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric(meta.get("name", active), f"{meta.get('price')} 元",
                  f"{meta.get('chg'):+.2f}%" if meta.get("chg") is not None else None)
        m2.metric("连板", f"{meta.get('streak', '—')} 板")
        m3.metric("主力净额", f"{(meta.get('main_net') or 0)/1e8:+.2f} 亿")
        m4.metric("支撑区", meta.get("sr", "—"))
        m5.metric("系统状态", ("持有" if meta.get("held") else "") +
                  ("已选:" + "、".join(meta.get("picked_by", [])) if meta.get("picked_by") else "未选未持"))
        with st.expander("📦 查看注入 LLM 的数据包原文", expanded=False):
            st.text(st.session_state[ctx_key])

        # ---- 快捷提问 ----
        cols = st.columns(len(_QUICK))
        for col, (label, q) in zip(cols, _QUICK):
            with col:
                if st.button(label, key=f"q_{label}", use_container_width=True):
                    _send(q, active)
                    st.rerun()

    # ---- 历史会话（持久化在 experience.db，刷新/重启不丢）----
    channels = _chat_channels()
    if channels:
        with st.expander(f"📜 历史会话（{len(channels)} 个频道，点击切换）", expanded=False):
            codes_in = [ch for ch, _, _ in channels if ch != "general"]
            name_map = {}
            if codes_in:
                try:
                    with datasource._conn() as c:
                        name_map = dict(c.execute(
                            f"SELECT code, name FROM ifind_stocklist WHERE code IN "
                            f"({','.join('?' * len(codes_in))})", codes_in).fetchall())
                except Exception:
                    pass
            for ch, cnt, last in channels[:20]:
                label = "💬 普通问答" if ch == "general" else f"{name_map.get(ch, '') or ch}（{ch}）"
                cc1, cc2 = st.columns([5, 1])
                with cc1:
                    if st.button(f"{label} · {cnt}条 · {last[:16]}", key=f"histch_{ch}",
                                 use_container_width=True):
                        if ch != "general":
                            st.session_state["chat_active_code"] = ch
                            st.session_state[f"chat_hist_{ch}"] = _chat_load(ch)
                        st.rerun()
                with cc2:
                    if st.button("🗑", key=f"histdel_{ch}", help="删除该频道全部对话"):
                        _chat_clear(ch)
                        st.session_state.pop(f"chat_hist_{ch}", None)
                        st.rerun()

    # ---- 对话区（始终渲染；无活跃票=普通问答）----
    if active:
        hist = st.session_state.get(f"chat_hist_{active}", [])
    else:
        hist = st.session_state.get("chat_hist_general", [])
        if not hist:
            st.info("💬 直接提问即可。提到股票名或代码（如“分析一下汉王科技”/“600519 怎么样”）"
                    "会自动加载该票数据包做深度分析；不提股票就是普通问答。")
    last_day = None
    for msg in hist:
        day = (msg.get("ts") or "")[:10]  # 库里的历史带时间戳，按日分隔
        if day and day != last_day:
            st.caption(f"—— {day} ——")
            last_day = day
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    user_q = st.chat_input("提问：可提到任意股票（自动加载其数据包）…", key="chat_input")
    if user_q:
        detected = _extract_code_from_text(user_q)
        if detected and detected != active:
            st.session_state["chat_active_code"] = detected
            hk = f"chat_hist_{detected}"
            st.session_state.setdefault(hk, [])
            nm = ""
            try:
                with datasource._conn() as c:
                    row = c.execute("SELECT name FROM ifind_stocklist WHERE code=?", (detected,)).fetchone()
                    nm = row[0] if row else ""
            except Exception:
                pass
            st.session_state[hk].append(
                {"role": "assistant", "content": f"🔄 已切换分析对象到 {nm}（{detected}），数据包已就绪。"})
            _send(user_q, detected)
        elif active:
            _send(user_q, active)
        else:
            _send(user_q, None)  # 普通问答（无数据包）
        st.rerun()

    st.caption("⚠️ AI 分析基于本地数据包与模型推理，仅供参考，不构成投资建议。市场有风险。")


render()
