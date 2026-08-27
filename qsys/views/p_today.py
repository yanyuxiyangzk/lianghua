"""🎯 今日选股：一页看懂「今天买什么 · 为什么 · 靠不靠谱」。

面向执行的决策页（调策略去 🪄选股工作台）：
  ① 结论卡：名单 + 三盏信号灯（回测/实战/过拟合）
  ② 名单表：每股「为什么选它」= 综合分的因子贡献拆解，白话展示
  ③ 操作与词典：加自选、止盈止损计划、名词解释

名单来源：⏰定时任务「板块扫描」每日落库（experience.db）；
没有时本页可一键用 OOS 胜率最高的策略包现场生成（同样落库，接受实战检验）。
"""

import json

import pandas as pd
import streamlit as st

import datasource
import experience
import factor_eval as fe
import library
import scheduler
import signals as sig
from common import WATCHLIST_FILE, all_pools, get_evolved_factors, get_last_trade_day, load_json, save_json

st.title("🎯 今日选股")
st.caption("每天只看这一页：**买什么 → 为什么 → 靠不靠谱**。想自己调因子组合去 🪄选股工作台。")

end = get_last_trade_day()


def _pct(x) -> float | None:
    try:
        return float(str(x).replace("%", "").strip()) / 100
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 名单来源
dates = experience.list_pick_dates(limit=5)
if not dates:
    st.info("还没有任何选股记录。点下面按钮，用当前 OOS 胜率最高的策略包立即生成一份。")
    if st.button("🚀 立即生成今日名单", type="primary"):
        with st.spinner("正在扫描（最优策略包）…"):
            msg = scheduler.job_pool_scan(top_n=10)
        st.success(msg)
        st.rerun()
    st.stop()

sel_date = dates[0]
if sel_date < end:
    st.caption(f"⏰ 今日（{end}）名单还没生成（定时任务每日 19:00 出），下面先看 {sel_date} 的。")
    if st.button("🚀 立即生成今日名单", type="primary"):
        with st.spinner("正在扫描（最优策略包）…"):
            msg = scheduler.job_pool_scan(top_n=10)
        st.success(msg)
        st.rerun()

picks = experience.picks_on_date(sel_date)
pack_picks = picks[picks["pack_name"].notna()]
pick = (pack_picks.iloc[0] if not pack_picks.empty else picks.iloc[0])  # 优先策略包名单
pick_id = int(pick["id"])
items = experience.pick_items_detail(pick_id)
if items.empty:
    st.warning("名单为空。")
    st.stop()

packs = library.list_strategies()
pk = packs.get(pick["pack_name"]) if pick["pack_name"] else None
src_label = f"策略包「{pick['pack_name']}」" if pick["pack_name"] else f"{pick['method']}"

# ---------------------------------------------------------------- 三盏信号灯
# 回测灯：保存名单时记录的 OOS 胜率
oos = pick["oos_winrate_at_save"] if pd.notna(pick["oos_winrate_at_save"]) else None
if oos is None:
    bt_light, bt_note = "🟡", "回测未验证"
elif oos >= 0.60:
    bt_light, bt_note = "🟢", f"样本外胜率 {oos:.0%}"
elif oos >= 0.55:
    bt_light, bt_note = "🟡", f"样本外胜率 {oos:.0%}（刚过线）"
else:
    bt_light, bt_note = "🔴", f"样本外胜率只有 {oos:.0%}"

# 实战灯：经验库里这个包的真实命中率（≥3 期结算才算数）
live_light, live_note = "⚪", "实战样本太少（<3 期）"
lb = experience.pack_leaderboard()
if pick["pack_name"] and not lb.empty:
    row = lb[lb["策略包"] == pick["pack_name"]]
    if not row.empty:
        r = row.iloc[0]
        for c in ["5日胜率", "20日胜率", "1日胜率"]:
            if c in row.index and pd.notna(r[c]) and int(r["已回填战果"]) >= 3:
                live_win = float(r[c])
                live_light = "🟢" if live_win >= 0.55 else "🔴"
                live_note = f"实战 {c.replace('胜率', '命中率')} {live_win:.0%}（{int(r['已回填战果'])} 期）"
                break

# 过拟合灯：保存策略包时记录的 IS 胜率 vs OOS 胜率
is_wr = _pct(pk.get("is_winrate")) if pk else None
oos_pk = _pct(pk.get("oos_winrate")) if pk else None
if is_wr is None or oos_pk is None:
    of_light, of_note = "⚪", "没跑过 IS/OOS 双轨"
elif is_wr - oos_pk <= 0.10:
    of_light, of_note = "🟢", f"IS/OOS 差距 {is_wr - oos_pk:.0%}（正常）"
else:
    of_light, of_note = "🔴", f"样本内 {is_wr:.0%} 但样本外只有 {oos_pk:.0%}，疑似过拟合"

lights = [bt_light, live_light, of_light]
if "🔴" in lights:
    head, head_word = "🔴", "谨慎（有红灯）"
elif bt_light == "🟢" and "🔴" not in lights and lights.count("⚪") == 0:
    head, head_word = "🟢", "可执行"
else:
    head, head_word = "🟡", "观察（证据还不充分）"

st.markdown(f"### {head} {sel_date} 名单：{len(items)} 只 · 来自 {src_label} · 信号：**{head_word}**")
l1, l2, l3 = st.columns(3)
l1.markdown(f"{bt_light} **回测**　{bt_note}")
l2.markdown(f"{live_light} **实战**　{live_note}")
l3.markdown(f"{of_light} **过拟合**　{of_note}")

# ---------------------------------------------------------------- 「为什么选它」：因子贡献拆解
reasons = {}
try:
    facs_cfg = json.loads(pick["factors"]) if isinstance(pick["factors"], str) else []
except (json.JSONDecodeError, TypeError):
    facs_cfg = []
codes = all_pools().get(pick["pool_name"]) or []
if facs_cfg and len(codes) >= 30:
    evo_code = {f["name"]: f["code"] for f in get_evolved_factors(only_accepted=False)}
    try:
        reg = library.get_factor_registry()
        le_code = {r["name"]: r["code"]
                   for _, r in reg[reg["engine"] == "loopengine"].iterrows()}
    except Exception:
        le_code = {}
    f_series, weights = {}, {}
    for f in facs_cfg:
        name = f["name"]
        if name in sig.BUILTIN_FACTORS:
            fac = {"name": name, "kind": "builtin", "code": None}
        elif name in sig.CATALOG_NAMES or name in sig.TECH_INDICATORS:
            fac = {"name": name, "kind": "tech", "code": None}
        else:
            code = evo_code.get(name) or le_code.get(name)
            if not code:
                continue
            fac = {"name": name, "kind": "evolved", "code": code}
        try:
            f_series[name] = fe.get_factor_values(fac, codes, sel_date)
            weights[name] = (float(f["weight"]), int(f["direction"]))
        except Exception:
            continue

    def _reason(code: str) -> str:
        pos = [(n, v) for n, v in sig.factor_contributions(f_series, weights, code) if v > 0][:3]
        return " · ".join(f"{sig.plain_factor_name(n)}({v:+.2f})" for n, v in pos) or "—"

    reasons = {c: _reason(c) for c in items["code"]}

# ---------------------------------------------------------------- 名单表
try:
    with datasource._qconn() as conn:
        rows = conn.execute(
            f"SELECT code, name, MAX(ts) FROM quote_snapshots"
            f" WHERE code IN ({','.join('?' * len(items))}) GROUP BY code",
            list(items["code"])).fetchall()
    name_map = {r[0]: r[1] for r in rows}
except Exception:
    name_map = {}
items.insert(1, "名称", [name_map.get(c, "") for c in items["code"]])

try:
    snaps, snap_ts = datasource.get_latest_snapshots(list(items["code"]))
    smap = {s["code"]: s for s in snaps}
    items["最新价"] = [smap.get(c, {}).get("price") for c in items["code"]]
    items["较昨收%"] = [
        round((smap[c]["price"] / smap[c]["prev_close"] - 1) * 100, 2)
        if smap.get(c) and smap[c].get("price") and smap[c].get("prev_close") else None
        for c in items["code"]]
except Exception:
    smap, snap_ts = {}, None

items["为什么选它"] = [reasons.get(c, "—") for c in items["code"]]

rules = experience.DEFAULT_RULES
ref = [smap.get(c, {}).get("price") or smap.get(c, {}).get("prev_close") for c in items["code"]]
items["参考买入价"] = [round(p, 2) if p else None for p in ref]
items["止盈价"] = [round(p * (1 + rules["take_profit"]), 2) if p else None for p in ref]
items["止损价"] = [round(p * (1 + rules["stop_loss"]), 2) if p else None for p in ref]
plan = experience.trade_plan(None, sel_date)

show = items.rename(columns={"rank": "排名", "code": "代码", "score": "综合分"})
show["综合分"] = show["综合分"].map(lambda x: round(float(x), 3) if pd.notna(x) else None)
cols = [c for c in ["排名", "代码", "名称", "最新价", "较昨收%", "综合分",
                    "为什么选它", "参考买入价", "止盈价", "止损价"] if c in show.columns]
st.dataframe(show[cols], width='stretch', hide_index=True)
st.caption(f"📝 执行计划：{plan['买入时间']}按开盘价买入（参考价=最近可得价"
           + (f"，快照 {snap_ts}" if snap_ts else "")
           + f"）；{plan['规则']}；最迟 {plan['最迟平仓']} 平仓。名单方向均为**看涨**。")

# ---------------------------------------------------------------- 操作
b1, b2 = st.columns([1, 3])
with b1:
    if st.button("➕ 名单全部加入自选股"):
        wl = load_json(WATCHLIST_FILE, [])
        add = [c for c in items["代码" if "代码" in items.columns else "code"] if c not in wl]
        save_json(WATCHLIST_FILE, wl + add)
        st.success(f"已加入 {len(add)} 只")
with b2:
    st.caption("进阶：📋选股列表（逐日回看+结算） · 📈模拟交易（逐笔盈亏） · "
               "📚经验库（策略包实战榜） · 🪄选股工作台（自己调组合）")

with st.expander("📖 这些词什么意思？"):
    st.markdown("""
- **胜率**：名单平均收益跑赢"池子中位数"的比例。55% 以上才算有本事，60%+ 算优秀。
- **超额**：名单平均涨幅 − 池内中位涨幅。+2% ≈ 比闭眼随便买一只多赚 2 个点。
- **样本外（OOS）**：只用"当时已经知道"的数据定权重，再看之后的表现——模拟真实使用。
  样本内（IS）是事后看答案，分数好看但不作数。
- **过拟合**：样本内 80%、样本外 50% = 背答案型策略。两胜率差距 >10pp 亮红灯。
- **扣费**：买卖双边成本 0.25%，数字里已经扣掉了。
- **为什么选它**：这只票的综合分里贡献最大的 3 个因子（括号=贡献值，越大越是它上榜的原因）。
""")
