"""📖 iFinD 接口文档（内嵌官方超级命令网页版）。

指标代码、参数格式、报表名全部在这个官方文档里查；
内嵌加载失败时用按钮新标签打开（需能访问 quantapi.51ifind.com）。
"""

import streamlit as st

DOC_URL = "https://quantapi.51ifind.com/gwstatic/static/ds_web/super-command-web/index.html#/BasicData"

st.title("📖 iFinD 接口文档（官方超级命令）")
c1, c2 = st.columns([1, 4])
with c1:
    st.link_button("↗ 新标签打开官方文档", DOC_URL)
with c2:
    st.caption("查指标代码（如市盈率 ths_pe_ttm_stock）、报表名、参数格式都在这里；"
               "查好后到「基本面数据/特色数据」页直接填代码查询。")
st.iframe(DOC_URL, height=1300)
