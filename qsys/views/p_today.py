"""🎯 今日选股 · 双轨制：一页看懂「今天买什么」。

  🛡 主轨 · 稳健名单：收益口径验证过的策略包（看胜率、看过拟合灯）——仓位大头
  🎲 卫星轨 · 涨停候选：事件口径（ev_ 因子）的包（看盈亏比、别苛求胜率）——仓位小头

每轨独立：三盏信号灯 / 名单（为什么选它/止盈止损）/ 一键生成 / 加自选。
名单落库后由经验库自动结算实战，两轨一个月后对比台见分晓。
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
from common import DATA_DIR, WATCHLIST_FILE, all_pools, get_evolved_factors, get_last_trade_day, load_json, save_json

st.title("🎯 今日选股")
st.caption("双轨制：**🛡 主轨求稳（胜率优先）· 🎲 卫星博弹性（盈亏比优先）** · "
           "调策略去 🧩选股组合 / 🪄选股工作台")

end = get_last_trade_day()
packs = library.list_strategies()
TRACK_FILE = DATA_DIR / "today_tracks.json"


def _pct(x):
    try:
        return float(str(x).replace("%", "").strip()) / 100
    except (TypeError, ValueError):
        return None


def _name_map(codes: list[str]) -> dict:
    try:
        with datasource._qconn() as conn:
            rows = conn.execute(
                f"SELECT code, name, MAX(ts) FROM quote_snapshots"
                f" WHERE code IN ({','.join('?' * len(codes))}) GROUP BY code",
                list(codes)).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


# ---------------------------------------------------------------- 轨道包指定（自动识别 + 手动覆盖持久化）
def _satellite_auto() -> str | None:
    """卫星包自动识别：ev_（事件口径）因子最多的包；没有则找名字含 涨停/事件 的。"""
    best, best_n = None, 0
    for n, pk in packs.items():
        k = sum(1 for f in pk.get("factors", []) if str(f["name"]).startswith("ev_"))
        if k > best_n:
            best, best_n = n, k
    if best:
        return best
    for n in packs:
        if "涨停" in n or "事件" in n:
            return n
    return None


cfg = load_json(TRACK_FILE, {})
sat_name = cfg.get("satellite") or _satellite_auto()
main_name = cfg.get("main")  # None → 自动（今日定时名单 / OOS 最高包）

if packs:
    with st.expander("⚙️ 轨道设置（每轨用哪个策略包）", expanded=False):
        names = list(packs.keys())
        msel = st.selectbox("🛡 主轨包", ["自动（OOS胜率最高）"] + names,
                            index=(names.index(cfg["main"]) + 1 if cfg.get("main") in names else 0))
        ssel = st.selectbox("🎲 卫星轨包", ["自动（ev_因子最多）"] + names,
                            index=(names.index(cfg["satellite"]) + 1 if cfg.get("satellite") in names else 0))
        if st.button("💾 保存轨道设置", key="td_track_save"):
            save_json(TRACK_FILE, {"main": None if msel.startswith("自动") else msel,
                                   "satellite": None if ssel.startswith("自动") else ssel})
            st.rerun()

# ---------------------------------------------------------------- 名单来源
dates = experience.list_pick_dates(limit=5)
picks = experience.picks_on_date(dates[0]) if dates else pd.DataFrame()
sel_date = dates[0] if dates else None

evo_map = {f["name"]: f["code"] for f in get_evolved_factors(only_accepted=False)}
try:
    _reg = library.get_factor_registry()
    le_map = {r["name"]: r["code"] for _, r in _reg[_reg["engine"] == "loopengine"].iterrows()}
except Exception:
    le_map = {}


def _load_pick_factors(pick) -> tuple[dict, dict]:
    """按落库时记录的因子配置重建因子值与权重（缓存命中则秒出）。"""
    try:
        facs_cfg = json.loads(pick["factors"]) if isinstance(pick.get("factors"), str) else []
    except (json.JSONDecodeError, TypeError, AttributeError):
        facs_cfg = []
    codes = all_pools().get(pick["pool_name"]) or []
    f_series, weights = {}, {}
    for f in facs_cfg:
        fac = fe.resolve_factor(f["name"], f.get("kind"), evo_map, le_map)
        if not fac:
            continue
        try:
            f_series[f["name"]] = fe.get_factor_values(fac, codes, pick.get("trade_date") or end)
            weights[f["name"]] = (float(f["weight"]), int(f["direction"]))
        except Exception:
            continue
    return f_series, weights


def _gen_track_pick(pack_name: str, source: str):
    """用指定策略包现场生成今日名单并落库（走经验库实战结算）。"""
    pk = packs[pack_name]
    codes = all_pools().get(pk["pool_name"]) or []
    sel, note, _w, _fs = scheduler.compute_pack_picks(pk, codes, end, pk["top_n"])
    experience.save_pick(source=source, pool_name=pk["pool_name"], top_n=len(sel),
                         method=pk.get("method"), filters=pk.get("filters", []),
                         factors=pk["factors"], final_scores=sel, pack_name=pack_name,
                         oos_winrate=_pct(pk.get("oos_winrate")), trade_date=end)
    return len(sel), note


def _lights(pick, pack_name):
    """三盏信号灯；返回 (head_icon, head_word, [三行灯文本])。"""
    oos = pick["oos_winrate_at_save"] if pd.notna(pick.get("oos_winrate_at_save")) else None
    if oos is None:
        bt = ("🟡", "回测未验证")
    elif oos >= 0.60:
        bt = ("🟢", f"样本外胜率 {oos:.0%}")
    elif oos >= 0.55:
        bt = ("🟡", f"样本外胜率 {oos:.0%}（刚过线）")
    else:
        bt = ("🔴", f"样本外胜率只有 {oos:.0%}")
    live = ("⚪", "实战样本太少（<3 期）")
    lb = experience.pack_leaderboard()
    if pack_name and not lb.empty:
        row = lb[lb["策略包"] == pack_name]
        if not row.empty:
            r = row.iloc[0]
            for c in ["5日胜率", "20日胜率", "1日胜率"]:
                if c in row.index and pd.notna(r[c]) and int(r["已回填战果"]) >= 3:
                    w = float(r[c])
                    live = ("🟢" if w >= 0.55 else "🔴",
                            f"实战{c.replace('胜率', '命中率')} {w:.0%}（{int(r['已回填战果'])} 期）")
                    break
    pk = packs.get(pack_name) if pack_name else None
    is_wr = _pct(pk.get("is_winrate")) if pk else None
    oos_pk = _pct(pk.get("oos_winrate")) if pk else None
    if is_wr is None or oos_pk is None:
        of = ("⚪", "没跑过 IS/OOS 双轨")
    elif is_wr - oos_pk <= 0.10:
        of = ("🟢", f"IS/OOS 差距 {is_wr - oos_pk:.0%}")
    else:
        of = ("🔴", f"样本内 {is_wr:.0%} 样本外 {oos_pk:.0%}，疑似过拟合")
    icons = [bt[0], live[0], of[0]]
    if "🔴" in icons:
        head = ("🔴", "谨慎（有红灯）")
    elif bt[0] == "🟢" and "⚪" not in icons:
        head = ("🟢", "可执行")
    else:
        head = ("🟡", "观察（证据不充分）")
    return head, [bt, live, of]


def _render_track(icon, title, pack_name, pick, kp: str,卫星: bool = False):
    """渲染一条轨道：标题 + 灯 + 名单表（为什么选它/止盈止损）+ 操作。"""
    st.markdown(f"## {icon} {title}")
    if not packs:
        st.info("还没有策略包——去 🧩选股组合 ① 自动组建一个，或 🪄选股工作台 手动搭。")
        return
    if not pack_name or pack_name not in packs:
        st.warning("这一轨还没指定策略包——点上面 ⚙️轨道设置 选一个。"
                   + ("（卫星轨建议用 ev_ 事件因子组的包；还没有就先去 🔬个股分析 定向挖）" if 卫星 else ""))
        return

    if pick is None:
        c1, _ = st.columns([1, 2])
        with c1:
            if st.button(f"🚀 生成今日名单（{pack_name}）", key=f"{kp}_gen", type="primary"):
                with st.spinner(f"用「{pack_name}」扫描中…"):
                    n, note = _gen_track_pick(pack_name, f"track_{kp}")
                st.success(f"已生成 {n} 只（{note}），并落库接受实战检验")
                st.rerun()
        st.caption(f"包「{pack_name}」：OOS {packs[pack_name].get('oos_winrate') or '未验证'} · "
                   f"{len(packs[pack_name]['factors'])} 因子 · Top-{packs[pack_name]['top_n']}")
        return

    items = experience.pick_items_detail(int(pick["id"]))
    if items.empty:
        st.warning("名单为空。")
        return
    head, (bt, live, of) = _lights(pick, pack_name)
    st.markdown(f"**{head[0]} {sel_date} · {len(items)} 只 · 信号：{head[1]}**"
                + ("　<span style='color:#888'>（卫星轨看盈亏比，胜率 45%+ 即可接受）</span>"
                   if 卫星 else ""), unsafe_allow_html=True)
    l1, l2, l3 = st.columns(3)
    l1.markdown(f"{bt[0]} **回测**　{bt[1]}")
    l2.markdown(f"{live[0]} **实战**　{live[1]}")
    l3.markdown(f"{of[0]} **过拟合**　{of[1]}")

    f_series, weights = _load_pick_factors(pick)
    nmap = _name_map(list(items["code"]))
    try:
        snaps, snap_ts = datasource.get_latest_snapshots(list(items["code"]))
        smap = {s["code"]: s for s in snaps}
    except Exception:
        smap, snap_ts = {}, None
    rules = experience.DEFAULT_RULES
    ref = [smap.get(c, {}).get("price") or smap.get(c, {}).get("prev_close") for c in items["code"]]

    def _reason(c):
        pos = [(n, v) for n, v in sig.factor_contributions(f_series, weights, c) if v > 0][:2]
        return " · ".join(f"{sig.plain_factor_name(n)}({v:+.2f})" for n, v in pos) or "—"

    st.dataframe(pd.DataFrame({
        "代码": list(items["code"]),
        "名称": [nmap.get(c, "") for c in items["code"]],
        "综合分": [round(float(s), 3) for s in items["score"]],
        "为什么选它": [_reason(c) for c in items["code"]],
        "最新价": [smap.get(c, {}).get("price") for c in items["code"]],
        "参考买入价": [round(p, 2) if p else None for p in ref],
        "止盈价": [round(p * (1 + rules["take_profit"]), 2) if p else None for p in ref],
        "止损价": [round(p * (1 + rules["stop_loss"]), 2) if p else None for p in ref],
    }), width='stretch', hide_index=True)
    plan = experience.trade_plan(None, sel_date)
    st.caption(f"📝 {plan['买入时间']}按开盘价买入；{plan['规则']}；最迟 {plan['最迟平仓']} 平仓"
               + (f" · 快照 {snap_ts}" if snap_ts else ""))
    if st.button(f"➕ 加入自选股", key=f"{kp}_wl"):
        wl = load_json(WATCHLIST_FILE, [])
        add = [c for c in items["code"] if c not in wl]
        save_json(WATCHLIST_FILE, wl + add)
        st.success(f"已加入 {len(add)} 只")


# ---------------------------------------------------------------- 双轨渲染
if sel_date is None:
    st.info("还没有任何选股记录——先在下面任意一轨点「生成今日名单」。")

if sel_date and sel_date < end:
    st.caption(f"⏰ 今日（{end}）名单还没出（定时任务每日 19:00），先看 {sel_date} 的；"
               "也可点各轨的生成按钮现场出今日名单。")


def _pick_for(pack_name):
    if picks.empty or not pack_name:
        return None
    rows = picks[picks["pack_name"] == pack_name]
    return rows.iloc[0] if len(rows) else None


# 主轨名单：指定包 > 今日定时扫描（非卫星包） > 第一条
main_pick = _pick_for(main_name)
if main_pick is None and not picks.empty:
    non_sat = picks[picks["pack_name"] != sat_name] if sat_name else picks
    sched = non_sat[non_sat["source"] == "sched_pool_scan"]
    main_pick = (sched.iloc[0] if not sched.empty else non_sat.iloc[0])
    if main_name is None and pd.notna(main_pick.get("pack_name")):
        main_name = main_pick["pack_name"]

_render_track("🛡", "主轨 · 稳健名单（收益验证口径）", main_name, main_pick, "main")
st.markdown("---")
_render_track("🎲", "卫星轨 · 涨停候选（事件口径）", sat_name, _pick_for(sat_name), "sat", 卫星=True)

with st.expander("📖 这些词什么意思？"):
    st.markdown("""
- **双轨制**：主轨求"胜率稳"（收益口径验证），卫星轨求"赔率大"（事件口径挖的涨停因子）——两条线的名单分开看、分开结算。
- **胜率**：名单平均收益跑赢"池子中位数"的比例。主轨要 ≥55%（最好 60%+）；卫星轨 45% 即可接受（赚一次够盖几次小亏）。
- **样本外（OOS）**：只用"当时已经知道"的数据定权重再看之后表现；样本内（IS）是事后看答案，好看不作数。两者差距 >10pp = 过拟合红灯。
- **为什么选它**：这只票综合分里贡献最大的因子（括号=贡献值）。
- **止盈/止损/最迟平仓**：统一执行规则（止盈15% / 止损-8% / 持有≤20交易日，双边成本已含）。
""")
