import ast
from pathlib import Path
import unittest
from streamlit.testing.v1 import AppTest

class RealtimeStepsTests(unittest.TestCase):
    def test_all_ten_steps_with_readable_logs(self):
        page=Path(__file__).resolve().parents[1]/'views/p_le_realtime.py'
        tree=ast.parse(page.read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='render_steps')
        code='import streamlit as st\nfrom mining_log_format import STEPS, log_line, round_sections\n'
        code+=ast.unparse(function)
        code+='\nrender_steps(round_sections(st.session_state.get("events", [])))'
        app=AppTest.from_string(code).run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(len(app.expander),10)
        self.assertEqual(app.expander[-1].label,'10. 入库')
        app.session_state['events']=[{'type':'round_start','batch':1},
            {'type':'step_update','step':1,'status':'done'},
            {'type':'step_update','step':4,'batch_left':1,'status':'done'},
            {'type':'step_update','step':5,'status':'pass'}]
        app.run()
        self.assertEqual(len(app.expander),10)
        self.assertEqual(len(app.json),0)
        text='\n'.join(t.value for t in app.text)
        self.assertIn('构建面板：完成',text)
        self.assertIn('规则审查：通过',text)
        self.assertIn('候选 1/1',text)
