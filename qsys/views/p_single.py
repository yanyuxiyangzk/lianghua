"""🔬 个股分析 · 事件研究：从「涨停/大涨/跌停/创新高」事件反推有效因子。

方法：找出范围内全部事件点 → 取事件前 lag 日的因子分位 → 与基准 0.5 对比，
分位显著偏高/偏低的因子就是该事件的"前兆因子"。
  - 池模式（横截面分位）：事件样本多、统计可靠，回答"什么因子预示涨停"
  - 单票模式（时序分位）：个股画像，回答"这只票涨停前长什么样"（单票无截面，
    截面算子退化，故只用内置+技术指标类时序因子）
挖到前兆因子后可一键样本外快验（预测力最终仍以收益口径的 walk-forward 裁决）。
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import datasource
import factor_eval as fe
import library
import signals as sig
from common import all_pools, get_evolved_factors, get_last_trade_day, load_watchlist

st.title("🔬 个股分析 · 事件研究")
st.caption("从事件反推因子：**什么因子在涨停/大涨/跌停/创新高之前就已经就位** —— 前兆因子榜即因子挖掘的方向盘")

end = get_last_trade_day()


def _norm_code(text: str) -> str | None:
    """600721 → SH600721；接受带前缀或纯数字两种写法。"""
    t = text.strip().upper().replace(".", "").replace(" ", "")
    if t[:2] in ("SH", "SZ", "BJ") and len(t) == 8:
        return t
    if len(t) == 6 and t.isdigit():
        if t.startswith("6"):
            return "SH" + t
        if t.startswith(("0", "3")):
            return "SZ" + t
        if t.startswith(("4", "8")):
            return "BJ" + t
    return None


# ---------------------------------------------------------------- 参数区
c1, c2, c3 = st.columns([2, 2, 1])
with c1:
    mode = st.radio("研究范围", ["整个股票池（统计更可靠）", "单票历史（个股画像）"],
                    horizontal=True, key="ps_mode")
with c2:
    kind = st.radio("事件类型", fe.EVENT_KINDS, horizontal=True, key="ps_kind")
with c3:
    lag = st.radio("看事件前", ["前1日", "前5日"], horizontal=True, key="ps_lag")
lag_n = 1 if lag == "前1日" else 5

pool_mode = mode.startswith("整个")
if pool_mode:
    pool_name = st.selectbox("股票池", list(all_pools().keys()), key="ps_pool")
    codes = all_pools().get(pool_name) or []
    scope_note = "内置+技术指标全量 + 本池评分卡 |ICIR| Top20 进化因子"
else:
    default_code = (load_watchlist() or ["SH600519"])[0]
    code_in = st.text_input("个股代码（如 600721 或 SH600721）", value=default_code, key="ps_code")
    code = _norm_code(code_in)
    if not code:
        st.error("代码格式不对——输入 6 位数字或带交易所前缀（SH/SZ/BJ）")
        st.stop()
    pool_name, codes = f"个股 {code}", [code]
    if st.button("🔍 该票舆情 / 新闻搜索增强", key="ps_newsense",
                 help="跳到「搜索增强」页，已预填本票代码"):
        st.session_state["ns_codes"] = code
        st.switch_page("views/p_newsense.py")
    scope_note = "内置+技术指标（单票无横截面，截面排名类进化因子退化不适用）"
st.caption(f"因子范围：{scope_note} · 数据截至 **{end}**")

if st.button("🔬 开始研究", type="primary", key="ps_run"):
    st.session_state.pop("ps_wf", None)  # 新研究作废旧验证结果
    with st.status("研究中…", expanded=True) as bar:
        bar.write("① 加载面板并找事件点…")
        panel = sig.get_panel_cached(codes, end, 800, source="qlib_local")
        events = fe.find_events(panel, kind)
        if events.empty:
            st.session_state["ps_res"] = {"empty": True, "kind": kind}
        else:
            bar.write(f"② 找到 {len(events)} 个事件点，加载因子值（缓存命中则秒出）…")
            facs = ([{"name": n, "kind": "builtin", "code": None} for n in sig.BUILTIN_FACTORS]
                    + [{"name": n, "kind": "tech", "code": None} for n in sig.TECH_INDICATORS]
                    + [{"name": n, "kind": "tech", "code": None} for n in sig.CATALOG_NAMES])
            if pool_mode:  # 池模式加评分卡 Top20 进化因子（已评估过 → 有缓存）
                sc = library.get_latest_scorecard(pool_name)
                if sc is not None and not sc.empty:
                    valid = sc.dropna(subset=["ICIR"]).assign(
                        _abs=lambda d: pd.to_numeric(d["ICIR"], errors="coerce").abs())
                    top_ev = valid.sort_values("_abs", ascending=False).head(20)["因子"].tolist()
                    base = {f["name"] for f in facs}
                    evo_map = {f["name"]: f["code"] for f in get_evolved_factors(only_accepted=False)}
                    reg = library.get_factor_registry()
                    le_map = {r["name"]: r["code"]
                              for _, r in reg[reg["engine"] == "loopengine"].iterrows()}
                    for n in top_ev:
                        if n in base:
                            continue
                        fac = fe.resolve_factor(n, evo_map=evo_map, le_map=le_map)
                        if fac:
                            facs.append(fac)
            fvals, failed = {}, 0
            for fac in facs:
                try:
                    fvals[fac["name"]] = fe.get_factor_values(fac, codes, end)
                except Exception:
                    failed += 1
            bar.write(f"③ 对 {len(fvals)} 个因子做事件前兆分析…")
            tbl = fe.event_premonition(fvals, events, panel, lag=lag_n,
                                       mode=("cs" if pool_mode else "ts"))
            st.session_state["ps_res"] = {
                "tbl": tbl, "events": events, "fvals": fvals, "codes": codes,
                "n_factors": len(fvals), "failed": failed, "kind": kind, "lag": lag_n,
                "pool_mode": pool_mode, "pool_name": pool_name}
        bar.update(label="研究完成", state="complete")

res = st.session_state.get("ps_res")
if res:
    if res.get("empty"):
        st.warning(f"范围内近 3 年没有「{res['kind']}」事件——换个事件类型或范围试试。")
    else:
        events, tbl = res["events"], res["tbl"]
        st.markdown(f"**找到 {len(events)} 个「{res['kind']}」事件点"
                    f"（{res['pool_name']} · 近3年 · 前{res['lag']}日）** · "
                    f"分析因子 {res['n_factors']} 个" +
                    (f"（{res['failed']} 个计算失败已跳过）" if res["failed"] else ""))
        if tbl.empty:
            st.warning("事件点太少（<5 个有效样本），统计没有意义——换范围或事件类型。")
        else:
            show = tbl.copy()
            show.insert(1, "白话", [sig.plain_factor_name(n) for n in show["因子"]])
            st.markdown("**🧭 事件前兆因子榜**（|差值| 越大越是前兆；|t|>2 统计显著）")
            st.dataframe(show, width='stretch', hide_index=True, height=320)
            top15 = tbl.head(15)
            fig = go.Figure(go.Bar(
                y=[sig.plain_factor_name(n) for n in top15["因子"]][::-1],
                x=top15["差值"][::-1], orientation="h",
                marker_color=["#e54545" if v > 0 else "#2ca02c" for v in top15["差值"][::-1]]))
            fig.update_layout(height=420, margin=dict(l=10, r=10, t=10, b=10),
                              xaxis_title="事件前平均分位 − 0.5（红=偏高，绿=偏低）")
            st.plotly_chart(fig, width='stretch')

            if not res["pool_mode"]:
                with st.expander(f"📅 事件明细（{len(events)} 次）"):
                    ev = events.reset_index()[["datetime", "instrument", "ret"]]
                    ev["datetime"] = ev["datetime"].astype(str).str[:10]
                    ev["ret"] = ev["ret"].map(lambda x: f"{x:+.1%}")
                    st.dataframe(ev.rename(columns={"datetime": "日期", "instrument": "代码",
                                                    "ret": "当日涨幅"}),
                                 width='stretch', hide_index=True)
            else:
                st.markdown("**🧪 前兆因子组合快验**（Top5 前兆因子等权，方向按偏高/偏低自动定；"
                            "事件前兆 → 收益预测是代理检验，最终以样本外表现为准）")
                if st.button("🧪 跑一次样本外验证", key="ps_validate"):
                    top5 = tbl.head(5)
                    fvals, weights = {}, {}
                    for _, r in top5.iterrows():
                        fvals[r["因子"]] = res["fvals"][r["因子"]]
                        weights[r["因子"]] = (1.0 / 5, 1 if r["差值"] >= 0 else -1)
                    with st.spinner("walk-forward 验证中…"):
                        panel = sig.get_panel_cached(res["codes"], end, 800, source="qlib_local")
                        wf = fe.walk_forward(fvals, panel, "等权", 10,
                                             fwd_days=5, step=5, min_factors=1)
                    st.session_state["ps_wf"] = wf
                wf = st.session_state.get("ps_wf")
                if wf is not None and not wf.empty and "优化组合扣费超额" in wf:
                    win = (wf["优化组合扣费超额"] > 0).mean()
                    verdict = ("——✅ 可去 🧩选股组合/🪄选股工作台 固化" if win >= 0.55
                               else "——⚠️ 不足 55%，前兆≠能赚钱，别固化")
                    st.markdown(f"**样本外扣费胜率 {win:.0%}**（{len(wf)} 个应用点 · 5日口径）" + verdict)
                    cum = wf.set_index("调仓日")["优化组合扣费超额"].add(1).cumprod()
                    st.line_chart(cum, height=220)

# ---------------------------------------------------------------- 🧬 定向挖因子
st.markdown("---")
st.header("🧬 定向挖因子（事件驱动演化）")
st.caption("上面的前兆榜是**用现成因子**对照事件；这里是**围绕事件生产新因子**——"
           "LoopEngine 把演化目标从「预测收益」换成「预测事件」，"
           "过事件版硬闸门（事件IC + 十分位提升≥2x + 前后两半稳定 + 库内去重）才入库，前缀 `ev_`。")

try:
    reg = library.get_factor_registry()
    n_ev = int(reg["name"].str.startswith("ev_").sum()) if not reg.empty else 0
except Exception:
    n_ev = 0
# 挖掘范围：池模式跟随所选股票池；单票事件样本太少（统计上不可行），回落沪深300 并说明
mine_pool = pool_name if pool_mode else "沪深300"
if not pool_mode:
    st.caption(f"⚠️ 单票的事件样本太少（个位数），无法支撑定向演化——挖掘在 **{mine_pool}** 池上进行，"
               f"挖出的因子再去个股上对照（上面的单票画像）")
m1, m2 = st.columns([1, 3])
with m1:
    rounds = st.selectbox("跑多少轮", [10, 30, 50], index=1, key="ps_mine_batch",
                          help="一轮≈30 个候选；事件闸门很严，一轮入库 0~2 个是常态")
with m2:
    st.caption(f"当前事件：**{kind}**（未来5日内发生） · 挖掘池：**{mine_pool}** · "
               f"库内已有 ev_ 因子 **{n_ev}** 个 · "
               "入库后可在 🪄选股工作台 体检（收益口径）与 🧩选股组合 中使用")
if st.button("🧬 围绕「{}」定向挖因子".format(kind), key="ps_mine"):
    from loopengine.engine import LoopEngine

    with st.spinner(f"定向演化中（{rounds} 轮 × ~30 候选，{mine_pool} 池，事件闸门把关）…"):
        r = LoopEngine(mine_pool).run_event_round(kind, batch=rounds)
    st.success(f"完成：测试 {r['tested']} · 重复 {r['dup']} · FSA拦截 {r['frozen']} · "
               f"**入库 {r['passed']} 个** {r['new'][:5]}")
    if r["passed"] == 0:
        st.caption("一轮没挖到很正常（事件闸门宁缺毋滥）——挂 ⏰定时任务「事件定向挖因子」每晚自动挖。")
