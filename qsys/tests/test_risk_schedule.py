"""Execute scheduler definitions in isolation: no live scheduler or production DB."""
import ast
import sys
import types
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / 'scheduler.py'
tree = ast.parse(source.read_text())

class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 24, 13, 30, tzinfo=tz)

class Tests(unittest.TestCase):
    def test_afternoon_schedule_is_registered_separately(self):
        node = next(n for n in tree.body if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == 'JOBS' for t in n.targets))
        jobs = {k.value:v for k,v in zip(node.value.keys,node.value.values)}
        afternoon = jobs['risk_guard_afternoon']
        defaults = next(v for k,v in zip(afternoon.keys,afternoon.values) if k.value=='default')
        cfg = ast.literal_eval(defaults)
        self.assertEqual((cfg['hour'],cfg['minute']), (13,30))
        self.assertTrue(cfg['enabled'])

    def test_evaluation_failure_closes_buy_gate(self):
        node = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='job_risk_guard_intraday')
        writes=[]
        exp=types.SimpleNamespace(portfolio_risk=lambda **kw: {'ok':False,'reason':'missing'},
            _write_risk_flag=lambda *a,**kw:writes.append((a,kw)))
        ns={'datetime':Clock,'TZ':'Asia/Shanghai'}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns)
        with patch.dict(sys.modules, {'experience':exp}):
            msg=ns['job_risk_guard_intraday']()
        self.assertIn('暂停买入',msg)
        self.assertTrue(writes[0][0][1])
        self.assertEqual(writes[0][1]['level'],'red')

    def test_application_failure_closes_buy_gate(self):
        for name in ('job_risk_guard', 'job_risk_guard_intraday'):
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            writes = []
            exp = types.SimpleNamespace(portfolio_risk=lambda **kw: {'ok': True},
                _write_risk_flag=lambda *a, **kw: writes.append((a, kw)))
            def fail(*args, **kwargs):
                raise KeyError('dd_now')
            ns = {'datetime': Clock, 'TZ': 'Asia/Shanghai', '_apply_account_risk': fail}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), ns)
            with patch.dict(sys.modules, {'experience': exp}):
                self.assertIn('暂停买入', ns[name]())
            self.assertTrue(writes[0][0][1])
            self.assertEqual(writes[0][1]['level'], 'red')

    def test_exception_closes_buy_gate(self):
        for name in ('job_risk_guard', 'job_risk_guard_intraday'):
            node = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
            writes=[]
            def fail(**kw):
                raise RuntimeError('db unavailable')
            exp=types.SimpleNamespace(portfolio_risk=fail,
                _write_risk_flag=lambda *a,**kw:writes.append((a,kw)))
            ns={'datetime':Clock,'TZ':'Asia/Shanghai'}
            exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns)
            with patch.dict(sys.modules, {'experience':exp}):
                self.assertIn('暂停买入',ns[name]())
            self.assertTrue(writes[0][0][1])

if __name__=='__main__': unittest.main()
