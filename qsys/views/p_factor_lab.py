"""🧪 在线因子实验室：手写因子 → 即时验证 → 一键入库。

两种编写模式：
  ⚡ 表达式模式：LoopEngine 树直算（秒级）——字段/算子白名单，无代码注入面
  🐍 Python 模式：RD-Agent factor.py 契约（读 daily_pv.h5 写 result.h5），子进程隔离执行

入库（engine="manual"）后与 AI 因子同管线：每日体检评分卡、选股工作台可选、可打包进策略包。
"""

import json
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import common
import datasource
import factor_eval as fe
import library
import signals as sig

# ---------------------------------------------------------------- 速查表
EXPR_FIELDS = [
    ("open/high/low/close", "开/高/低/收"), ("volume", "成交量"), ("amount", "成交额"),
    ("vwap", "成交均价"), ("overnight", "隔夜涨幅"), ("amplitude", "振幅"),
    ("upper_shadow", "上影"), ("lower_shadow", "下影"), ("hl_ratio", "高低价比"),
    ("body_ratio", "实体占比"),
]
EXPR_OPS = [
    ("sub/mul/div(a,b)", "减/乘/除"), ("corr(a,b,n)", "n日相关(自动rank)"),
    ("ma(a,n)", "n日均线"), ("ema(a,n)", "指数均线"), ("delta(a,n)", "n日差分"),
    ("roc(a,n)", "n日变化率"), ("std(a,n)", "n日标准差"), ("skew(a,n)", "n日偏度"),
    ("ts_max/ts_min(a,n)", "n日最高/最低"), ("ts_rank(a,n)", "n日时序分位"),
    ("rank_cs(a)", "当日截面分位"), ("zscore(a,n)", "n日时序标准化"),
    ("decay_linear(a,n)", "线性衰减加权"), ("abs/sign(a)", "绝对值/符号"),
]
EXPR_EXAMPLES = {
    "均线差 5/20": "sub(ma(close,5),ma(close,20))",
    "5日反转(反向)": "sub(0,roc(close,5))",
    "量价相关 10日": "corr(rank_cs(close),rank_cs(volume),10)",
    "缩量反弹": "mul(sub(0,roc(close,5)),div(ma(volume,5),ma(volume,20)))",
    "振幅时序分位": "ts_rank(amplitude,20)",
}

PY_TEMPLATE = '''import pandas as pd
import numpy as np

# ===== 数据契约（与 RD-Agent 因子相同）=====
# daily_pv.h5：索引 (datetime, instrument)，
# 列 $open/$high/$low/$close/$volume/$amount/$factor
df = pd.read_hdf("daily_pv.h5", key="data")

# ==== 在这里写你的因子逻辑 ====
g = df.groupby(level="instrument")["$close"]
factor = -g.pct_change(5)          # 示例：5日反转（跌得多 → 得分高）
# ============================

# 出口契约：单列 DataFrame/Series 写 result.h5（key="data"），列名=因子名
result = factor.rename("{name}").dropna().to_frame()
result.to_hdf("result.h5", key="data")
'''


# ---------------------------------------------------------------- 执行
def _latest_trade_day() -> str:
    with datasource._conn() as c:
        r = c.execute("SELECT MAX(date) FROM market_daily WHERE source='ths_ifind'").fetchone()
    return r[0] if r and r[0] else datetime.now().strftime("%Y-%m-%d")


def _run_expression(expr: str, codes: list[str], end: str) -> pd.Series:
    """表达式 → 树直算 → 长表 Series[(datetime, instrument)]。"""
    from loopengine.tree import build_field_frames, evaluate_tree, parse

    tree = parse(expr)  # 语法错误在这里抛 ValueError
    panel = sig.get_panel_cached(codes, end, 800)
    frames = build_field_frames(panel)
    out = evaluate_tree(tree, frames)          # datetime × instrument
    s = out.stack().dropna()
    s.index = s.index.set_names(["datetime", "instrument"])
    return s


def _run_python(code: str, name: str, codes: list[str], end: str) -> pd.Series:
    """Python 代码 → 子进程执行（run_factor_code 既有隔离通道）。"""
    df = sig.run_factor_code(code, name, codes, end)
    s = df.iloc[:, 0].dropna()
    s.index = s.index.set_names(["datetime", "instrument"])
    return s


def _expr_to_factor_code(expr: str, name: str) -> str:
    """表达式入库用的 code：首行 # sexpr: 供树直算 fast path，下面是子进程兜底。"""
    return f'''# sexpr: {expr}
# 由在线因子实验室表达式模式自动生成（首行 sexpr 供树直算；以下为子进程兜底执行代码）
import pandas as pd
from loopengine.tree import parse, build_field_frames, evaluate_tree

df = pd.read_hdf("daily_pv.h5", key="data")
panel = df.swaplevel().sort_index()          # (instrument, datetime)
frames = build_field_frames(panel)
out = evaluate_tree(parse({expr!r}), frames)
result = out.stack().rename({name!r}).dropna().to_frame()
result.to_hdf("result.h5", key="data")
'''


# ---------------------------------------------------------------- 评估与绘图
def _evaluate(vals: pd.Series, panel: pd.DataFrame) -> dict:
    fwd5 = fe.forward_returns(panel, 5)
    ic = fe.ic_series(vals, fwd5)
    bt = fe.factor_group_backtest(vals, panel, n_groups=10)
    last_day = vals.index.get_level_values("datetime").max()
    cross = vals[vals.index.get_level_values("datetime") == last_day]
    return {"ic": ic, "bt": bt, "cross": cross, "last_day": str(last_day)[:10]}


def _fig_ic(ic: pd.Series) -> go.Figure:
    roll = ic.rolling(20, min_periods=5).mean()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ic.index, y=ic.values, name="日IC",
                             mode="lines", opacity=0.3, line=dict(color="gray")))
    fig.add_trace(go.Scatter(x=roll.index, y=roll.values, name="20日滚动IC",
                             mode="lines", line=dict(color="#1f6feb", width=2)))
    fig.add_hline(y=0, line_dash="dash", line_color="#e54545")
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10),
                      title="IC 时序（5日前向）", template="plotly_white")
    return fig


def _fig_groups(group_mean: dict) -> go.Figure:
    ks = [k for k, v in group_mean.items() if v is not None]
    vs = [group_mean[k] * 100 for k in ks]
    colors = ["#26a65b" if v >= 0 else "#e54545" for v in vs]
    fig = go.Figure(go.Bar(x=ks, y=vs, marker_color=colors))
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10),
                      title="十分层平均收益(%)", template="plotly_white")
    return fig


def _fig_nav(nav: pd.Series) -> go.Figure:
    fig = go.Figure(go.Scatter(x=nav.index, y=nav.values, mode="lines",
                               line=dict(color="#7c3aed", width=2), name="多空净值"))
    fig.add_hline(y=1, line_dash="dash", line_color="#999")
    fig.update_layout(height=300, margin=dict(l=10, r=10, t=30, b=10),
                      title="顶组-底组 多空净值", template="plotly_white")
    return fig


# ---------------------------------------------------------------- 页面
def render():
    st.title("🧪 在线因子实验室")
    st.caption("手写因子 → 即时验证 → 一键入库（入库后与 AI 因子同管线：每日体检/选股/打包）")

    # ---- 控制条 ----
    c1, c2, c3 = st.columns([2, 1.5, 1.5])
    with c1:
        fac_name = st.text_input("因子名称（入库标识，英文/下划线）", key="lab_name",
                                 placeholder="如 my_reversal_5d")
    with c2:
        pools = common.all_pools()
        pool_name = st.selectbox("验证股票池", list(pools.keys()),
                                 index=list(pools.keys()).index("沪深300") if "沪深300" in pools else 0,
                                 key="lab_pool")
    with c3:
        end = st.text_input("评估截止日", value=_latest_trade_day(), key="lab_end")
    codes = pools[pool_name]

    # ---- 编写区 ----
    def _insert_example():  # on_change 在组件创建前运行，此时写 widget key 合法
        pick = st.session_state.get("lab_ex_pick")
        if pick in EXPR_EXAMPLES:
            st.session_state["lab_expr"] = EXPR_EXAMPLES[pick]

    tab_expr, tab_py = st.tabs(["⚡ 表达式模式（秒级）", "🐍 Python 模式（分钟级）"])
    with tab_expr:
        ex_col1, ex_col2 = st.columns([3, 1])
        with ex_col2:
            st.selectbox("插入示例", ["（选择示例）"] + list(EXPR_EXAMPLES.keys()),
                         key="lab_ex_pick", on_change=_insert_example)
        with ex_col1:
            expr = st.text_area("因子表达式", height=90, key="lab_expr",
                                placeholder="例：sub(ma(close,5),ma(close,20))")
        with st.expander("📖 字段 / 算子速查"):
            st.markdown("**字段**：" + " · ".join(f"`{f}`{d}" for f, d in EXPR_FIELDS))
            st.markdown("**算子**：" + " · ".join(f"`{o}`{d}" for o, d in EXPR_OPS))
    with tab_py:
        py_code = st.text_area("因子 Python 代码", height=300, key="lab_py",
                               value=PY_TEMPLATE.replace("{name}", fac_name or "my_factor"))
        with st.expander("📜 数据契约说明"):
            st.markdown("""- 输入 `daily_pv.h5`（key=`data`）：索引 `(datetime, instrument)`，
  列 `$open/$high/$low/$close/$volume/$amount/$factor`（因子生成在同花顺 iFinD 口径数据上）
- 输出 `result.h5`（key=`data`）：单列 DataFrame，列名=因子名，索引同上
- 子进程隔离执行，超时 300 秒；结果按 (数据源×代码×池×截止日) 缓存
- ⚠️ 代码在本地子进程执行，风险自担（勿写文件/网络操作）""")

    run = st.button("🚀 运行并评估", type="primary", key="lab_run")

    # ---- 执行 ----
    if run:
        if not fac_name.strip():
            st.error("先填因子名称")
            st.stop()
        with st.spinner("计算因子值…"):
            try:
                if expr.strip():
                    vals = _run_expression(expr.strip(), codes, end)
                    mode_used = "expr"
                elif py_code.strip():
                    vals = _run_python(py_code, fac_name.strip(), codes, end)
                    mode_used = "py"
                else:
                    st.warning("两种模式至少填一个")
                    st.stop()
            except Exception as e:
                st.error(f"因子计算失败：{e}")
                st.stop()
        if vals.empty:
            st.warning("因子值全为空——检查表达式/代码逻辑")
            st.stop()
        with st.spinner("评估中（IC/分层回测）…"):
            panel = sig.get_panel_cached(codes, end, 800)
            result = _evaluate(vals, panel)
        st.session_state["lab_eval"] = {"vals": vals, "result": result,
                                        "mode": mode_used, "expr": expr.strip(),
                                        "code": py_code, "name": fac_name.strip(),
                                        "pool": pool_name, "end": end}

    # ---- 结果区 ----
    ev = st.session_state.get("lab_eval")
    if ev:
        vals, res = ev["vals"], ev["result"]
        ic = res["ic"]
        st.markdown("---")
        st.markdown(f"#### 评估结果：`{ev['name']}` · {ev['pool']} · 截至 {res['last_day']}")
        m1, m2, m3, m4 = st.columns(4)
        if not ic.empty:
            m1.metric("IC均值(5日)", f"{ic.mean():+.4f}")
            m2.metric("ICIR", f"{ic.mean() / (ic.std() + 1e-12):.3f}")
            m3.metric("IC胜率", f"{(ic > 0).mean():.0%}")
        cov = res["cross"].notna().mean() if len(res["cross"]) else 0
        m4.metric("最新截面覆盖率", f"{cov:.0%}（{len(res['cross'])} 只）")

        if not ic.empty:
            g1, g2 = st.columns(2)
            with g1:
                st.plotly_chart(_fig_ic(ic), use_container_width=True)
            with g2:
                gm = res["bt"].get("group_mean") if res.get("bt") else None
                if gm:
                    st.plotly_chart(_fig_groups(gm), use_container_width=True)
            g3, g4 = st.columns(2)
            with g3:
                nav = res["bt"].get("ls_nav") if res.get("bt") else None
                if nav is not None and len(nav):
                    st.plotly_chart(_fig_nav(nav), use_container_width=True)
                    stats = res["bt"].get("ls_stats") or {}
                    if stats:
                        st.caption("多空：" + " · ".join(f"{k} {v}" for k, v in stats.items()))
            with g4:
                cross = res["cross"].dropna().sort_values(ascending=False)
                t1, t2 = st.columns(2)
                with t1:
                    st.markdown("**Top 10**")
                    st.dataframe(cross.head(10).round(4).rename("因子值"), width="stretch")
                with t2:
                    st.markdown("**Bottom 10**")
                    st.dataframe(cross.tail(10).round(4).rename("因子值"), width="stretch")

        # ---- 入库 ----
        st.markdown("---")
        reg = library.get_factor_registry()
        exists = not reg.empty and ev["name"] in set(reg["name"])
        b1, b2 = st.columns([1.2, 4.8])
        with b1:
            if st.button("📥 注册进因子库", key="lab_register"):
                code = (ev["code"] if ev["mode"] == "py"
                        else _expr_to_factor_code(ev["expr"], ev["name"]))
                library.sync_factor_registry([{
                    "name": ev["name"], "kind": "manual", "code": code,
                    "engine": "manual", "factor_type": "量价"}])
                st.success(f"已入库：{ev['name']}（engine=manual）——今晚体检起自动评分，选股工作台可选用")
        with b2:
            if exists:
                st.warning(f"⚠️ 库中已有同名因子 `{ev['name']}`，注册将覆盖其代码")
            else:
                st.caption("入库后：每日体检自动评分 · 选股工作台可选 · 可打包进策略包")

    # ---- 我的手工因子 ----
    reg = library.get_factor_registry()
    mine = reg[reg["engine"] == "manual"] if not reg.empty else pd.DataFrame()
    if not mine.empty:
        st.markdown("---")
        st.markdown(f"#### 📦 我的手工因子（{len(mine)} 个）")
        # 最新评分卡
        try:
            with datasource._conn() as c:
                sc = pd.read_sql(
                    "SELECT name, ic_mean, icir, ic_winrate, eval_date FROM factor_scorecards "
                    "WHERE name IN (%s) ORDER BY eval_date DESC"
                    % ",".join("?" * len(mine)), c, params=tuple(mine["name"].tolist()))
            sc = sc.drop_duplicates("name")
        except Exception:
            sc = pd.DataFrame()
        show = mine[["name", "first_seen"]].rename(
            columns={"name": "因子", "first_seen": "注册时间"})
        if not sc.empty:
            show = show.merge(sc.rename(columns={"name": "因子", "ic_mean": "IC均值",
                                                 "icir": "ICIR", "ic_winrate": "IC胜率",
                                                 "eval_date": "最近体检"}),
                              on="因子", how="left")
        st.dataframe(show, width="stretch", hide_index=True)
        d1, d2 = st.columns([2, 1])
        with d1:
            del_name = st.selectbox("删除因子", ["（不删除）"] + mine["name"].tolist(),
                                    key="lab_del_name")
        with d2:
            if del_name != "（不删除）":
                if st.session_state.get("lab_del_pending") == del_name:
                    if st.button(f"确认删除 {del_name}", type="primary", key="lab_del2"):
                        library.delete_factor(del_name)
                        st.session_state.pop("lab_del_pending", None)
                        st.success(f"已删除 {del_name}")
                        st.rerun()
                elif st.button("🗑 删除", key="lab_del1"):
                    st.session_state["lab_del_pending"] = del_name
                    st.rerun()


render()
