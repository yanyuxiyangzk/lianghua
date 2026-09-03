"""QSYS (QuantSys) —— 量化可视化看板（左侧菜单导航壳）。

页面分区（st.navigation）：
  我的:    🎯 今日执行（每天只看这页）/ 📚 实战成绩 / 📋 选股列表
  行情:    📈 股票行情 / 🕯️ 自选K线 / 📉 专业K线
  📡 iFinD数据中心: 📋行情（A股市场/A股指数，落库读库）/ 🌐板块资金流
           / 📰舆情/新闻（iFinD公告 + DeepSeek 舆情摘要）
           / 📖接口文档（官方文档内嵌）/ 🗄数据仓库（所有落库表总览+浏览）
           （原 实时行情/历史行情/高频行情/日内快照/基本面数据/特色数据 已下线，文件保留）
  专业区:  🧩 选股组合 / 🔬 个股分析 / 🧮 因子策略库 / 🪄 选股工作台 / 📈 模拟交易
           / 🧬 进化看板 / 📊 回测浏览 / ⏰ 定时任务（调参研究用，平时不用看）
  系统:    ⚙️ 设置（数据源切换/缓存/状态/说明）

注意：页面文件放在 views/ 而非 pages/ —— pages/ 是 Streamlit 旧版自动发现
的保留目录，会把文件名（英文）也列进侧栏菜单，造成中英文两套菜单并存。
"""

import streamlit as st

st.set_page_config(page_title="QSYS · QuantSys 看板", layout="wide", page_icon="📈",
                   initial_sidebar_state="expanded")

# 调度器随服务启动即初始化（不再等访问定时任务页才拉起——否则容器重建后定时任务全部停摆）
from scheduler import get_scheduler as _get_scheduler

_get_scheduler()

# 全局：禁用 Streamlit 重跑时对"过期元素"的变暗遮罩（白纱层）。
# 老内容保持可见且可点击，直到新内容到达——局部无感刷新的关键补丁。
st.markdown(
    "<style>"
    "[data-stale='true']{opacity:1 !important; pointer-events:auto !important;}"
    ".stApp [data-testid='stStatusWidget']{opacity:1 !important;}"
    "</style>",
    unsafe_allow_html=True)

pages = {
    "我的": [
        st.Page("views/p_desk.py", title="今日执行", icon="🎯", url_path="today"),
        st.Page("views/p_broker.py", title="资金账号", icon="💹", url_path="broker"),
    ],
    "行情": [
        st.Page("views/p_quotes.py", title="股票行情", icon="📈", url_path="quotes"),
        st.Page("views/p_stocklist.py", title="股票/指数列表", icon="📋", url_path="stocklist"),
        st.Page("views/p_sector.py", title="板块行情", icon="🏛️", url_path="sector"),
        st.Page("views/p_kline.py", title="自选K线", icon="🕯️", url_path="kline"),
        st.Page("views/p_kpro.py", title="专业K线", icon="📉", url_path="kpro"),
        st.Page("views/p_sectorflow.py", title="板块资金流", icon="🌐", url_path="sectorflow"),
        st.Page("views/p_stock_fundflow.py", title="个股资金流", icon="💰", url_path="fundflow"),
    ],
    # iFinD 数据中心：独立菜单板块，每个子页只展示对应数据（逻辑在 ifind_hub.py）
    # （实时行情/历史行情/高频行情/日内快照/基本面数据/特色数据 已下线——被「行情」页覆盖或用不到，页面文件保留在 views/ 可随时恢复）
    "📡 iFinD数据中心": [
        st.Page("views/p_ifind_stocklist.py", title="行情", icon="📋", url_path="ifind-stocklist"),
        st.Page("views/p_ifind_kline.py", title="股价K线", icon="📈", url_path="ifind-kline"),
        st.Page("views/p_ifind_lhb.py", title="龙虎榜", icon="🐉", url_path="ifind-lhb"),
        st.Page("views/p_ifind_announce.py", title="公告信息", icon="📜", url_path="ifind-announce"),
        st.Page("views/p_ifind_fundflow.py", title="资金流向", icon="💰", url_path="ifind-fundflow"),

        st.Page("views/p_newsense.py", title="舆情/新闻", icon="📰", url_path="newsense"),
        st.Page("views/p_ifind_doc.py", title="接口文档", icon="📖", url_path="ifind-doc"),
        st.Page("views/p_warehouse.py", title="数据仓库", icon="🗄", url_path="ifind-warehouse"),
    ],
    "专业区（调参研究，平时不用看）": [
        st.Page("views/p_combo.py", title="选股组合", icon="🧩", url_path="combo"),
        st.Page("views/p_single.py", title="个股分析", icon="🔬", url_path="single"),
        st.Page("views/p_factorlib.py", title="因子策略库", icon="🧮", url_path="factorlib"),
        st.Page("views/p_picker.py", title="选股工作台", icon="🪄", url_path="picker"),
        st.Page("views/p_trades.py", title="模拟交易", icon="📈", url_path="trades"),
        st.Page("views/p_evo.py", title="进化看板", icon="🧬", url_path="evo"),
        st.Page("views/p_backtest.py", title="回测浏览", icon="📊", url_path="backtest"),
        st.Page("views/p_sched.py", title="定时任务", icon="⏰", url_path="sched"),
    ],
    "系统": [
        st.Page("views/p_settings.py", title="设置", icon="⚙️", url_path="settings"),
    ],
}

nav = st.navigation(pages, expanded=True)   # 菜单分区默认全部展开
with st.sidebar:
    st.caption("💡 每天只看「🎯 今日执行」；菜单分区可点击展开/收起。")
nav.run()
