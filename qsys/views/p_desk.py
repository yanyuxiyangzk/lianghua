"""🎯 今日执行：行动卡 + 每日名单 + 经验库（三合一页面）。

每天只看这页：
  🎯 今日执行：主轨/卫星轨行动卡（买什么/止损/止盈/最迟卖出 + 实战红绿灯 + 竞价确认）
  📋 每日名单：按交易日回看全部来源名单（自动扫描/手动/组合）+ 批次结算明细 + 个股模拟盈亏
  📚 经验库：策略包/因子实战榜 + 每日选股情况 + 选股历史
"""

import streamlit as st

import tab_exp
import tab_picks
import tab_today

tab1, tab2, tab3 = st.tabs(["🎯 今日执行", "📋 每日名单", "📚 经验库"])

with tab1:
    tab_today.render()

with tab2:
    tab_picks.render()

with tab3:
    tab_exp.render()
