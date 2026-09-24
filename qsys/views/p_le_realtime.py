"""LoopEngine details: server-side event subscription survives page navigation."""

import streamlit as st
from common import DATA_DIR
from mining_event_journal import read_details, snapshot
from mining_log_format import log_line, round_sections, STEPS
from workflow_status import read_live_status, read_mining_progress

st.set_page_config(page_title="LoopEngine 实时", layout="wide")
def render_steps(sections):
    event_steps = {'candidate_gen': 4, 'review_result': 5, 'llm_result': 6,
                   'gate_eval': 9, 'gate_pass': 10}
    for step, name in enumerate(STEPS, 1):
        lines = []
        if step <= 3:
            lines = [log_line(e) for e in sections['preparation'] if e.get('step') == step]
        else:
            for index, candidate in enumerate(sections['candidates']):
                logs = [e for e in candidate['events']
                        if e.get('step', event_steps.get(e.get('type'))) == step]
                if not logs:
                    continue
                number = candidate['number']
                label = (f"候选 {number}/{sections['batch']}" if number is not None
                         else f"候选记录 {index+1}（按接收顺序）")
                lines.append('—— ' + label + ' ——')
                lines.extend(log_line(e) for e in logs)
        with st.expander(f'{step}. {name}', expanded=False):
            if lines:
                st.text('\n'.join(lines))
            else:
                st.caption('暂无该步骤记录（可能未采集到，或该候选未进入此步骤）。')


def render_events():
    from mining_realtime_html import build_html
    details = read_details()
    live = read_live_status(DATA_DIR)
    running = live['fresh'] and any(k in live['running'] for k in ('multitype_mine', 'loopengine'))
    st.components.v1.html(build_html(details['events'], running,
                                    st.session_state.get('sse_port', 8502)),
                          height=1050, scrolling=True)


def render():
    st.page_link('views/p_workflow.py', label='返回全流程工作流', icon='🔙')
    st.markdown('## ⚡ LoopEngine 实时事件流')
    st.caption('服务端持续接收事件；切换页面或刷新浏览器后复用已接收日志。日志落盘保存，服务重启后仍可查看历史记录。')
    render_events()


if __name__ == '__main__':
    render()
