"""⚙️ 设置：数据源切换 / 缓存管理 / 系统状态 / 使用说明。"""

import shutil

import pandas as pd
import streamlit as st

import datasource
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
