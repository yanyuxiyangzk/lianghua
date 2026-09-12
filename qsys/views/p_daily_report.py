"""📊 每日量化战报：点击生成 AI 深度分析报告。"""

from datetime import datetime
import streamlit as st

import broker
import experience
import datasource
from llmutil import llm_chat


# ---------------------------------------------------------------- 数据采集
def _collect_all_data() -> dict:
    """采集所有报告数据"""
    # 1. 账户概况
    account = broker.get_account()

    # 2. 持仓明细
    positions = broker.get_positions()

    # 3. 今日成交
    fills = broker.list_fills(today_only=False)  # 最近成交

    # 4. 因子表现
    factors = experience.factor_leaderboard(fwd=5)

    # 5. 策略表现
    strategies = experience.pack_leaderboard()

    # 6. 市场环境（5大指数，同花顺快照——热码集已含指数，盘后回落到日更值）
    idx_codes = ["SH000001", "SZ399001", "SZ399006", "SH000300", "SH000905"]
    snaps = datasource.get_ifind_latest(idx_codes)
    indices = [{"code": c, "price": s.get("price"), "prev_close": s.get("prev_close"),
                "amount": None}
               for c in idx_codes if (s := snaps.get(c))]

    # 7. 交易统计
    stats = experience.position_stats()

    return {
        "account": account,
        "positions": positions,
        "fills": fills,
        "factors": factors,
        "strategies": strategies,
        "indices": indices,
        "stats": stats,
        "date": datetime.now().strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------- 格式化函数
def _fmt_money(v) -> str:
    if v is None or v != v:
        return "—"
    return f"{v:,.2f}"


def _fmt_pct(v) -> str:
    if v is None or v != v:
        return "—"
    return f"{v:.2f}%"


def _format_account(acc: dict) -> str:
    pnl = acc.get("持仓盈亏", 0) or 0
    day_pnl = acc.get("今日盈亏", 0) or 0
    total = acc.get("总资产", 0) or 0
    pnl_pct = (pnl / (total - pnl) * 100) if (total - pnl) > 0 else 0
    day_pct = (day_pnl / (total - day_pnl) * 100) if (total - day_pnl) > 0 else 0
    return f"""- 总资产：{_fmt_money(acc.get('总资产'))} 元
- 可用资金：{_fmt_money(acc.get('可用资金'))} 元
- 持仓市值：{_fmt_money(acc.get('持仓市值'))} 元
- 持仓盈亏：{_fmt_money(pnl)} 元 ({_fmt_pct(pnl_pct)})
- 今日盈亏：{_fmt_money(day_pnl)} 元 ({_fmt_pct(day_pct)})"""


def _format_positions(df) -> str:
    if df is None or df.empty:
        return "无持仓"
    lines = ["| 股票 | 代码 | 成本 | 现价 | 持仓 | 盈亏 | 盈亏% |",
             "|------|------|------|------|------|------|-------|"]
    for _, r in df.iterrows():
        name = r.get("name", "")
        code = r.get("code", "")
        cost = r.get("cost", 0)
        price = r.get("最新价", cost)
        shares = r.get("shares", 0)
        pnl = r.get("持仓盈亏", 0) or 0
        pnl_pct = r.get("盈亏%", 0) or 0
        lines.append(f"| {name} | {code} | {cost:.2f} | {price:.2f} | {shares} | {pnl:+,.0f} | {pnl_pct:+.1f}% |")
    return "\n".join(lines)


def _format_fills(df) -> str:
    if df is None or df.empty:
        return "无成交记录"
    lines = ["| 时间 | 股票 | 方向 | 价格 | 数量 | 金额 |",
             "|------|------|------|------|------|------|"]
    for _, r in df.head(20).iterrows():
        ts = r.get("ts", "")[:16]
        name = r.get("name", "")
        side = "买入" if r.get("side") == "buy" else "卖出"
        price = r.get("price", 0)
        shares = r.get("shares", 0)
        amount = r.get("amount", 0) or 0
        lines.append(f"| {ts} | {name} | {side} | {price:.2f} | {shares} | {amount:,.0f} |")
    return "\n".join(lines)


def _format_factors(df) -> str:
    if df is None or df.empty:
        return "无因子数据"
    lines = ["| 因子 | 参与次数 | 5日胜率 | 5日均超额 |",
             "|------|----------|---------|-----------|"]
    for _, r in df.head(10).iterrows():
        name = r.get("因子", "")
        cnt = r.get("参与且有战果的次数", 0)
        wr = r.get("5日胜率(近似)", 0) or 0
        ex = r.get("5日均超额(近似)", 0) or 0
        lines.append(f"| {name} | {cnt} | {wr:.1%} | {ex:+.2f}% |")
    return "\n".join(lines)


def _format_strategies(df) -> str:
    if df is None or df.empty:
        return "无策略数据"
    lines = ["| 策略包 | 股票池 | 选股次数 | 5日胜率 | 回测OOS |",
             "|--------|--------|----------|---------|---------|"]
    for _, r in df.head(10).iterrows():
        pack = r.get("策略包", "")
        pool = r.get("股票池", "")
        cnt = r.get("选股次数", 0)
        wr = r.get("5日胜率", None)
        oos = r.get("回测OOS胜率", "—")
        wr_s = f"{wr:.1%}" if wr is not None else "—"
        lines.append(f"| {pack} | {pool} | {cnt} | {wr_s} | {oos} |")
    return "\n".join(lines)


def _format_indices(data) -> str:
    if not data:
        return "无指数数据"
    names = {"SH000001": "上证指数", "SZ399001": "深证成指",
             "SZ399006": "创业板指", "SH000300": "沪深300", "SH000905": "中证500"}
    lines = ["| 指数 | 最新价 | 涨跌% |",
             "|------|--------|-------|"]
    for r in data:
        code = r.get("code", "")
        name = names.get(code, code)
        price = r.get("price", 0) or 0
        chg = r.get("change_pct", 0) or 0
        lines.append(f"| {name} | {price:.2f} | {chg:+.2f}% |")
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    if not stats:
        return "无统计数据"
    return f"""- 总交易笔数：{stats.get('总交易笔数', 0)}
- 胜率：{_fmt_pct(stats.get('胜率', 0) * 100)}
- 平均盈亏：{_fmt_pct(stats.get('平均盈亏', 0) * 100)}
- 盈亏比：{stats.get('盈亏比', 0):.2f}
- 最大回撤：{_fmt_pct(stats.get('最大回撤', 0) * 100)}"""


# ---------------------------------------------------------------- LLM 分析
SYSTEM_PROMPT = """你是一位专业的量化投资分析师，负责每日战果汇报。
请深入分析以下数据，重点回答：
1. 今天为什么赚钱/亏钱？根本原因是什么？
2. 哪些方面存在不足？（选股/择时/仓位/风控/因子/策略）
3. 具体如何改进？（可执行的行动项）

用 Markdown 格式，包含标题、列表、表格。语言简洁专业。"""


def _build_prompt(data: dict) -> str:
    return f"""
# 日期：{data['date']}

## 一、账户概况
{_format_account(data['account'])}

## 二、持仓明细
{_format_positions(data['positions'])}

## 三、今日成交记录
{_format_fills(data['fills'])}

## 四、因子表现
{_format_factors(data['factors'])}

## 五、策略表现
{_format_strategies(data['strategies'])}

## 六、市场环境
{_format_indices(data['indices'])}

## 七、历史统计
{_format_stats(data['stats'])}

---

请从以下维度深入分析：

### 1. 盈亏原因分析
- **技术面**：买入/卖出时机是否合理？是否追高/抄底？
- **因子面**：使用的因子今日表现如何？哪些因子有效/失效？
- **策略面**：选股策略是否适应今日市场？持仓周期是否合理？
- **仓位面**：仓位是否过重/过轻？集中度是否合理？

### 2. 不足之处诊断
- **选股问题**：是否选到了弱势股？是否错过强势股？
- **择时问题**：是否买在高点/卖在低点？
- **风控问题**：止损是否及时？止盈是否过早？
- **因子问题**：因子是否失效？是否需要调整权重？
- **策略问题**：策略是否不适应当前市场环境？

### 3. 优化改进建议
- **短期调整**：今日持仓如何处理？是否需要调仓？
- **因子优化**：哪些因子需要降权/增权？是否需要新增因子？
- **策略优化**：是否需要切换策略？参数是否需要调整？
- **风控优化**：止损/止盈点位是否需要调整？
- **仓位优化**：仓位如何分配更合理？

### 4. 明日操作建议
- **持仓处理**：哪些股票继续持有？哪些需要卖出？
- **关注方向**：明日关注哪些板块/因子？
- **风险提示**：需要注意哪些风险？
"""


def _generate_report(data: dict) -> str:
    """调用 LLM 生成分析报告"""
    prompt = _build_prompt(data)
    result = llm_chat(SYSTEM_PROMPT, prompt, max_tokens=2048)
    if result:
        return result
    return "⚠️ LLM 服务不可用，请检查 DEEPSEEK_API_KEY 配置。"


# ---------------------------------------------------------------- 页面渲染
def page_daily_report():
    st.title("📊 每日量化战报")
    st.caption("点击按钮，AI 实时分析今日盈亏原因、不足之处和改进建议")

    # 历史战报查看
    history_df = experience.list_daily_reports(limit=30)
    if not history_df.empty:
        with st.expander("📜 历史战报", expanded=False):
            for _, row in history_df.iterrows():
                date = row["date"]
                pnl = row.get("pnl_today", 0) or 0
                gen = row.get("generated_at", "")[:16]
                icon = "🟢" if pnl >= 0 else "🔴"
                if st.button(f"{icon} {date}  盈亏: {pnl:+,.0f}元  ({gen})", key=f"hist_{date}"):
                    report_data = experience.get_daily_report(date)
                    if report_data:
                        st.session_state["daily_report"] = report_data.get("content", "")
                        st.session_state["daily_report_data"] = {
                            "account": report_data.get("account_json", {}),
                            "positions": report_data.get("positions_json", []),
                            "factors": report_data.get("factors_json", []),
                            "strategies": report_data.get("strategies_json", []),
                            "indices": report_data.get("market_json", []),
                            "stats": report_data.get("stats_json", {}),
                            "date": date,
                        }
                        st.rerun()

    # 一键生成按钮
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        generate = st.button("🤖 生成今日战报", type="primary", use_container_width=True)

    if generate:
        with st.spinner("📊 正在采集数据..."):
            data = _collect_all_data()
        with st.spinner("🤖 AI 分析中（约10秒）..."):
            report = _generate_report(data)
        st.session_state["daily_report"] = report
        st.session_state["daily_report_data"] = data
        # 保存战报到 DB
        try:
            experience.save_daily_report(data["date"], report, data)
        except Exception as e:
            st.warning(f"战报保存失败: {e}")

    # 显示报告
    if "daily_report" in st.session_state:
        report = st.session_state["daily_report"]
        data = st.session_state.get("daily_report_data", {})

        # 顶部概览卡片
        if data:
            acc = data.get("account", {})
            st.markdown("---")
            c1, c2, c3, c4 = st.columns(4)
            total = acc.get("总资产", 0) or 0
            pnl = acc.get("持仓盈亏", 0) or 0
            day_pnl = acc.get("今日盈亏", 0) or 0
            cash = acc.get("可用资金", 0) or 0
            pnl_pct = (pnl / (total - pnl) * 100) if (total - pnl) > 0 else 0
            day_pct = (day_pnl / (total - day_pnl) * 100) if (total - day_pnl) > 0 else 0

            c1.metric("总资产", f"{total:,.0f} 元")
            c2.metric("持仓盈亏", f"{pnl:+,.0f} 元", f"{pnl_pct:+.1f}%")
            c3.metric("今日盈亏", f"{day_pnl:+,.0f} 元", f"{day_pct:+.1f}%")
            c4.metric("可用资金", f"{cash:,.0f} 元")

        # AI 分析报告
        st.markdown("---")
        st.markdown(report)

        # 底部时间戳
        st.markdown("---")
        st.caption(f"⏰ 报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    elif not generate:
        st.info("👆 点击上方按钮，AI 将实时采集数据并生成分析报告")


# 入口
page_daily_report()
