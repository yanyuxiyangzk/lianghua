"""⚙️ 设置：数据源切换 / 缓存管理 / 系统状态 / 使用说明。"""

import shutil

import pandas as pd
import streamlit as st

import datasource
import experience
from common import DATA_DIR, get_last_trade_day, init_qlib

st.title("⚙️ 设置")

# ---------------------------------------------------------------- 数据源
st.subheader("全局数据源")
cur = datasource.get_source()
opts = list(datasource.SOURCES.keys())
sel = st.selectbox("分析层数据源（K线 / 信号 / 选股 / 经验结算）", opts,
                   index=opts.index(cur),
                   format_func=lambda k: datasource.SOURCES[k]["name"])
if sel != cur:
    datasource.set_source(sel)
    st.cache_data.clear()
    st.success(f"已切换到 {datasource.SOURCES[sel]['name']}")
    st.rerun()

st.dataframe(pd.DataFrame(datasource.source_status()).rename(
    columns={"source": "源标识", "name": "名称", "last_sync": "最近同步", "rows": "缓存行数", "note": "备注"}),
    width='stretch')

# loop 因子分析（LoopEngine 演化/体检/闸门/合成）专用数据源——可切同花顺 iFinD
st.subheader("loop 因子分析数据源")
loop_cur = datasource.get_loop_source()
loop_sel = st.selectbox("LoopEngine 演化/体检/闸门/Top5合成的面板数据源", opts,
                        index=opts.index(loop_cur) if loop_cur in opts else 0,
                        format_func=lambda k: datasource.SOURCES[k]["name"])
if loop_sel != loop_cur:
    datasource.set_loop_source(loop_sel)
    st.success(f"loop 因子分析数据源已切换到 {datasource.SOURCES[loop_sel]['name']}（首次切换需补抓日线，之后定时任务每日维护）")
    st.rerun()
st.caption("💡 切到 ths_ifind 后，演化因子全部基于同花顺数据（与展示端口径一致）；"
           "首次会按需补抓池内股票日K（THS_HQ，几分钟），之后由 ifind_daily_sync 每日维护。")
st.caption("⚠️ 进化闭环与回测（🧬/📊）固定使用 qlib 本地库，切换只影响分析展示层；"
           "akshare 为前复权口径，与 qlib 价格基准不同（水平差异属正常，形态一致）。")

# ---------------------------------------------------------------- 持仓风控参数
st.subheader("持仓风控参数")
st.caption("卫星轨只负责选股，成交后进入主轨账户统一管理；卫星来源仍使用独立的小仓位风控参数。")
main_rules = experience.get_risk_rules("main")
event_rules = experience.get_risk_rules("event")
with st.form("risk_rules_form"):
    st.markdown("##### 主轨稳健策略")
    m1, m2, m3 = st.columns(3)
    main_tp = m1.number_input("主轨止盈（%）", min_value=1.0, max_value=100.0,
                              value=float(main_rules["take_profit"] * 100), step=0.5)
    main_sl = m2.number_input("主轨止损（%）", min_value=0.5, max_value=50.0,
                              value=float(abs(main_rules["stop_loss"]) * 100), step=0.5)
    main_days = m3.number_input("主轨最长持有（交易日）", min_value=1, max_value=250,
                                value=int(main_rules["hold_days"]), step=1)

    st.markdown("##### 卫星来源事件策略")
    e1, e2, e3 = st.columns(3)
    event_tp = e1.number_input("卫星止盈（%）", min_value=1.0, max_value=100.0,
                               value=float(event_rules["take_profit"] * 100), step=0.5)
    event_sl = e2.number_input("卫星止损（%）", min_value=0.5, max_value=50.0,
                               value=float(abs(event_rules["stop_loss"]) * 100), step=0.5)
    event_days = e3.number_input("卫星最长持有（交易日）", min_value=1, max_value=250,
                                 value=int(event_rules["hold_days"]), step=1)
    submitted = st.form_submit_button("保存持仓风控参数", type="primary")

if submitted:
    experience.save_risk_rules(
        {"take_profit": main_tp / 100, "stop_loss": -main_sl / 100,
         "hold_days": int(main_days)},
        {"take_profit": event_tp / 100, "stop_loss": -event_sl / 100,
         "hold_days": int(event_days)},
    )
    st.success("持仓风控参数已保存，将从下一次持仓检查开始生效。")
    st.rerun()

# ---------------------------------------------------------------- 账户级分级风控
st.subheader("账户级分级风控")
st.caption("黄色仅限制卫星来源开仓；橙色/红色停止全部开仓并撤销待成交买单。减仓计划目前仅供观察，不会自动卖出。")
account_risk = experience.get_account_risk_config()
with st.form("account_risk_form"):
    a1, a2, a3 = st.columns(3)
    yellow_dd = a1.number_input("黄色回撤阈值（%）", 0.5, 50.0,
                                float(account_risk["yellow_drawdown"] * 100), 0.5)
    orange_dd = a2.number_input("橙色回撤阈值（%）", 0.5, 50.0,
                                float(account_risk["orange_drawdown"] * 100), 0.5)
    red_dd = a3.number_input("红色回撤阈值（%）", 0.5, 50.0,
                             float(account_risk["red_drawdown"] * 100), 0.5)
    t1, t2, t3, t4 = st.columns(4)
    normal_target = t1.number_input("正常目标仓位（%）", 0.0, 100.0,
                                    float(account_risk["normal_target"] * 100), 5.0)
    yellow_target = t2.number_input("黄色目标仓位（%）", 0.0, 100.0,
                                    float(account_risk["yellow_target"] * 100), 5.0)
    orange_target = t3.number_input("橙色目标仓位（%）", 0.0, 100.0,
                                    float(account_risk["orange_target"] * 100), 5.0)
    red_target = t4.number_input("红色目标仓位（%）", 0.0, 100.0,
                                 float(account_risk["red_target"] * 100), 5.0)
    account_submitted = st.form_submit_button("保存账户级风控参数", type="primary")

if account_submitted:
    if not yellow_dd < orange_dd < red_dd:
        st.error("回撤阈值必须满足：黄色 < 橙色 < 红色。")
    elif not red_target <= orange_target <= yellow_target <= normal_target:
        st.error("目标仓位必须随风险升级而不增加：红色 ≤ 橙色 ≤ 黄色 ≤ 正常。")
    else:
        experience.save_account_risk_config({
            "yellow_drawdown": yellow_dd / 100,
            "orange_drawdown": orange_dd / 100,
            "red_drawdown": red_dd / 100,
            "normal_target": normal_target / 100,
            "yellow_target": yellow_target / 100,
            "orange_target": orange_target / 100,
            "red_target": red_target / 100,
        })
        st.success("账户级风控参数已保存。")
        st.rerun()

plan = experience.latest_risk_plan()
if plan:
    st.markdown("##### 最新影子减仓计划")
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("风险等级", str(plan.get("level", "-")).upper())
    p2.metric("当前回撤", f"{float(plan.get('drawdown') or 0) * 100:.2f}%")
    p3.metric("当前 / 目标仓位",
              f"{float(plan.get('current_position_ratio') or 0) * 100:.1f}% / {float(plan.get('target_position_ratio') or 0) * 100:.1f}%")
    p4.metric("建议释放金额", f"¥{float(plan.get('required_release') or 0):,.2f}")
    rows = pd.DataFrame(plan.get("positions") or [])
    if not rows.empty:
        st.dataframe(rows, hide_index=True, width="stretch")
    st.warning("这是影子计划，仅用于验证账户级风控，不会自动提交卖单。")

    advice = experience.latest_risk_llm_advice()
    st.markdown("##### LLM 风控参考")
    if advice.get("date") != plan.get("date"):
        st.info("当前计划尚无 LLM 意见；正常风险等级不会调用 LLM。")
    elif advice.get("status") == "ok":
        l1, l2, l3 = st.columns(3)
        l1.metric("建议倾向", advice.get("stance", "-"))
        l2.metric("置信度", f"{float(advice.get('confidence') or 0):.0%}")
        l3.metric("数据缓存", "命中" if advice.get("cache_hit") else "新分析")
        st.write(advice.get("summary") or "无摘要")
        st.metric("LLM 建议目标仓位",
                  f"{float(advice.get('recommended_target_position') or 0) * 100:.1f}%")
        priority = pd.DataFrame(advice.get("priority_positions") or [])
        if not priority.empty:
            st.markdown("**持仓处理优先级**")
            st.dataframe(priority, hide_index=True, width="stretch")
        if advice.get("account_actions"):
            st.markdown("**账户级参考动作**\n\n" + "\n".join(
                f"- {x}" for x in advice["account_actions"]))
        disagreement = advice.get("rule_disagreement") or {}
        if disagreement.get("has_disagreement"):
            st.warning("与规则计划的分歧：" + str(disagreement.get("reason") or "未说明"))
        if advice.get("missing_evidence"):
            st.markdown("**缺失证据**\n\n" + "\n".join(
                f"- {x}" for x in advice["missing_evidence"]))
        st.caption("LLM 意见仅供人工参考，不会修改风险等级、撤单或提交买卖委托。")
    else:
        st.info(advice.get("reason") or "LLM 风控意见暂不可用，规则引擎继续独立运行。")

# ---------------------------------------------------------------- 缓存管理
st.subheader("缓存管理")
c1, c2 = st.columns(2)
with c1:
    if st.button("🧹 清空页面内存缓存"):
        st.cache_data.clear()
        st.cache_resource.clear()
        st.success("已清空（页面将重新计算）")
with c2:
    if st.button("🗑 清空磁盘缓存（因子值/IC/面板，下次需重算）"):
        for d in [DATA_DIR / "cache"]:
            if d.exists():
                shutil.rmtree(d)
        st.success("磁盘缓存已清空")

# ---------------------------------------------------------------- 系统状态
st.subheader("系统状态")
try:
    init_qlib()
    qlib_ok = True
except Exception:
    qlib_ok = False
st.markdown(f"""
| 项 | 状态 |
|---|---|
| qlib 数据（回测同源） | {'✅ 可用，截至 ' + get_last_trade_day() if qlib_ok else '❌ 初始化失败'} |
| 当前数据源 | {datasource.SOURCES[cur]['name']} |
| 调度器 | 见 ⏰定时任务 页（进程内调度，容器停即停） |
""")

# ---------------------------------------------------------------- iFinD 连通性
st.subheader("iFinD 连通性测试")
acc, pwd, token = datasource._ths_credentials()
st.markdown(f"""
| 通道 | 状态 |
|---|---|
| SDK（账号密码） | {'✅ 已配置' if acc else '❌ 未配置'} |
| HTTP（refresh_token） | {'✅ 已配置' if token else '❌ 未配置'} |
""")
if st.button("🔌 iFinD 连通性自检", type="primary"):
    with st.spinner("登录并拉取测试数据…"):
        st.info(datasource.ths_selftest())

# ---------------------------------------------------------------- 使用说明
st.subheader("使用说明")
st.markdown(
    """
**架构分工**：RD-Agent + Qlib（`lh-rdagent` 容器）负责因子假设 → 编码 → 回测 → 反馈进化的完整闭环；
QSYS 本看板只做只读展示与执行层消费（信号/选股/经验），不含任何因子生成逻辑。

**页面导览**
- 🧬 进化看板：每轮假设、因子代码、IC/年化对比、SOTA 轨迹
- 📊 回测浏览：每轮 qlib 回测的净值曲线与绩效
- 🕯️ 自选K线 / 📉 专业K线：日K / 同花顺风格终端（含分时·竞价视图）
- 🪄 选股组合：胜率体检 → 去冗余 → 加权 → 样本外验证 → 策略包
- 📚 经验库：选股结果落库，到期自动结算战果
- ⏰ 定时任务：个股信号 / 板块扫描 / 数据更新 / 战果回填（进程内调度，手动启停）

**常用命令（宿主机）**
```bash
./scripts/health.sh        # RD-Agent 环境自检
./scripts/factor.sh        # 启动因子进化闭环
./scripts/ui.sh            # RD-Agent 官方监控 UI (:19899)
./scripts/update_data.sh   # 手动更新行情（或用定时任务）
```
"""
)
