"""Render the actual directory component with Streamlit's headless page runner."""
import ast
from pathlib import Path
import unittest
from streamlit.testing.v1 import AppTest

PAGE = Path(__file__).resolve().parents[1] / 'views' / 'p_workflow.py'


def component_script():
    tree = ast.parse(PAGE.read_text())
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ('_render_rotation', '_progress_phase'):
            selected.append(node)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'FACTOR_TYPES_ORDER' for t in node.targets):
            selected.append(node)
    return ('import streamlit as st\nfrom html import escape\n' +
            ast.unparse(ast.Module(body=selected, type_ignores=[])) +
            '\n_render_rotation(st.session_state.get("snapshot", {}))\n')


class RotationPageTests(unittest.TestCase):
    def test_directory_visible_without_progress(self):
        app = AppTest.from_string(component_script()).run()
        self.assertEqual(len(app.exception), 0)
        content = '\n'.join(x.value for x in app.markdown)
        for name in ['量价', '资金流', '板块轮动', '指数', '盘口异动', '龙虎榜',
                     '爆量抢筹', '财务', '支撑阻力', '事件记忆']:
            self.assertIn(name, content)
        self.assertNotIn('data-active="true"', content)

    def test_actual_type_switch_and_stale_reset(self):
        app = AppTest.from_string(component_script())
        for name in ['量价', '资金流']:
            app.session_state['snapshot'] = {'live': {'fresh': True, 'progress': {
                'factor_type': name, 'status': 'running', 'rotation': 1, 'rotations': 2}}}
            app.run()
            self.assertEqual(len(app.exception), 0)
            content = '\n'.join(x.value for x in app.markdown)
            self.assertIn(f'data-factor-type="{name}" data-active="true"', content)
            self.assertEqual(content.count('data-active="true"'), 1)
        app.session_state['snapshot'] = {'live': {'fresh': False, 'progress': {
            'factor_type': '资金流'}}}
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn('data-active="true"', '\n'.join(x.value for x in app.markdown))

    def test_compact_next_label_without_queue_table(self):
        app = AppTest.from_string(component_script())
        app.session_state['snapshot'] = {'live': {'fresh': True, 'progress': {
            'factor_type': '量价', 'status': 'running', 'queue': [
                {'factor_type':'量价','status':'running'},
                {'factor_type':'资金流','status':'queued'}]}}}
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.dataframe), 0)
        self.assertEqual(len(app.caption), 0)
        content = '\n'.join(x.value for x in app.markdown)
        self.assertNotIn('轮动执行队列', content)
        self.assertIn('data-factor-type="量价" data-active="true" data-waiting="false"',content)
        self.assertIn('data-factor-type="资金流" data-active="false" data-waiting="true"',content)
        self.assertIn('background:#15803d', content)
        self.assertIn('background:#facc15', content)
        app.session_state['snapshot'] = {'live': {'fresh': True, 'progress': {
            'factor_type':'资金流', 'status':'preparing', 'queue': [
                {'factor_type':'板块轮动','status':'queued'}]}}}
        app.run()
        content = '\n'.join(x.value for x in app.markdown)
        self.assertEqual(content.count('data-waiting="true"'), 1)
        self.assertNotIn('data-active="true"', content)
        self.assertIn('data-factor-type="资金流" data-active="false" data-waiting="true"',content)


if __name__ == '__main__':
    unittest.main()
