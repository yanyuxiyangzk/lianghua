"""QSYS (QuantSys) —— 量化可视化看板（左侧菜单导航壳）。

页面拆分（st.navigation，侧栏可折叠）：
  进化工厂: 🧬 进化看板 / 📊 回测浏览
  行情:     📈 股票行情 / 🏛️ 板块行情 / 🕯️ 自选K线 / 📉 专业K线
  选股:     🎯 今日选股 / 🧩 选股组合 / 📋 选股列表 / 📈 模拟交易 / 📚 经验库 / 🧮 因子策略库 / 🪄 选股工作台 / ⏰ 定时任务
  系统:     ⚙️ 设置（数据源切换/缓存/状态/说明）

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
    "进化工厂": [
        st.Page("views/p_evo.py", title="进化看板", icon="🧬", url_path="evo"),
        st.Page("views/p_backtest.py", title="回测浏览", icon="📊", url_path="backtest"),
    ],
    "行情": [
        st.Page("views/p_quotes.py", title="股票行情", icon="📈", url_path="quotes"),
        st.Page("views/p_sector.py", title="板块行情", icon="🏛️", url_path="sector"),
        st.Page("views/p_sectorflow.py", title="资金趋势", icon="🌐", url_path="sectorflow"),
        st.Page("views/p_kline.py", title="自选K线", icon="🕯️", url_path="kline"),
        st.Page("views/p_kpro.py", title="专业K线", icon="📉", url_path="kpro"),
    ],
       "选股": [
        st.Page("views/p_today.py", title="今日选股", icon="🎯", url_path="today"),
        st.Page("views/p_combo.py", title="选股组合", icon="🧩", url_path="combo"),
        st.Page("views/p_picks.py", title="选股列表", icon="📋", url_path="picks"),
        st.Page("views/p_trades.py", title="模拟交易", icon="📈", url_path="trades"),
        st.Page("views/p_exp.py", title="经验库", icon="📚", url_path="exp"),
        st.Page("views/p_factorlib.py", title="因子策略库", icon="🧮", url_path="factorlib"),
        st.Page("views/p_picker.py", title="选股工作台", icon="🪄", url_path="picker"),
        st.Page("views/p_sched.py", title="定时任务", icon="⏰", url_path="sched"),
    ],
    "系统": [
        st.Page("views/p_settings.py", title="设置", icon="⚙️", url_path="settings"),
    ],
}

nav = st.navigation(pages, expanded=True)   # 菜单分区默认全部展开
with st.sidebar:
    st.caption("💡 菜单分区可点击展开/收起；找不到侧栏时点页面左上角 `>` 展开。")
nav.run()
