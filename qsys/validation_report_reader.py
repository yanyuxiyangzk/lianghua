"""Read-only access to stored validation evidence; no implicit migrations."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def read_reports(path, limit=200):
    path = Path(path)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'strategy_validation_reports' not in tables:
            return []
        rows = c.execute('SELECT report_id,strategy_name,eval_date,created_at,report_json '
                         'FROM strategy_validation_reports ORDER BY created_at DESC,rowid DESC LIMIT ?',
                         (max(1, min(int(limit), 1000)),)).fetchall()
    result = []
    for report_id, name, day, created, payload in rows:
        try:
            report = json.loads(payload)
            if not isinstance(report, dict):
                raise ValueError('报告内容不是对象')
        except (ValueError, TypeError):
            report = {'ok': False, 'error': '报告JSON损坏', 'assessment_status': 'corrupt'}
        result.append(dict(report_id=report_id, strategy_name=name, eval_date=day,
                           created_at=created, report=report))
    return result


def render_reports():
    import pandas as pd
    import streamlit as st
    from datasource import MKT_DB
    from research_backtest import WINDOW_POLICY
    st.markdown('### 版本化策略重验报告')
    try:
        reports = read_reports(MKT_DB)
    except sqlite3.Error as exc:
        st.error(f'读取报告失败：{exc}')
        return
    if not reports:
        st.info('暂无版本化报告。新版策略重验完成或失败后将保存记录；历史日志不会自动转成新版证据。')
        return
    selected = st.selectbox('报告（最近200份，含失败与归档）', range(len(reports)),
                            format_func=lambda i: f"{reports[i]['created_at']} · {reports[i]['strategy_name']} · {reports[i]['report_id'][:12]}",
                            key='validation_report_id')
    entry = reports[selected]
    report = entry['report']
    st.caption(f"报告编号：{entry['report_id']} · 评估日：{entry['eval_date']} · 计算版本：{report.get('calculation_version', '历史未验证')}")
    st.write({'结果状态': report.get('assessment_status', '历史未验证'),
              '策略状态': report.get('status'), '曾发布摘要': report.get('published'),
              '策略版本': report.get('strategy_version')})
    if report.get('error'):
        st.warning(report['error'])
    if report.get('calculation_version') != WINDOW_POLICY:
        st.info('此报告不是当前计算口径，只作为历史记录查看。')
    st.caption('报告仅为研究证据；曾发布不代表仍是当前策略版本，也不代表交易批准。')
    metrics = {k: report.get(k) for k in ('oos_windows', 'oos_winrate', 'avg_net_excess', 'max_drawdown', 'sharpe')}
    st.dataframe(pd.DataFrame([metrics]), hide_index=True)
    if report.get('windows'):
        st.markdown('**保存的逐窗口结果**')
        st.dataframe(pd.DataFrame(report['windows']), hide_index=True)
    with st.expander('计算口径和原始报告'):
        st.json(report)
    st.download_button('导出报告 JSON', json.dumps(entry, ensure_ascii=False, indent=2),
                       file_name=f"validation-{entry['report_id']}.json", mime='application/json')
