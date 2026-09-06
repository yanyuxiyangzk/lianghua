"""LoopEngine 实时事件流 — iframe 嵌入 SSE server 页面。"""

import streamlit as st

st.set_page_config(page_title="LoopEngine 实时", layout="wide")


def render():
    st.page_link("views/p_workflow.py", label="返回全流程工作流", icon="🔙")
    st.markdown("## ⚡ LoopEngine 实时事件流")
    st.caption("SSE 推送 · 10 步生成过程 · 点击展开查看每步实时日志")

    sse_port = st.session_state.get("sse_port", 8502)
    st.components.v1.iframe(
        f"http://localhost:{sse_port}/realtime",
        height=750,
        width=None,
    )


if __name__ == "__main__":
    render()
