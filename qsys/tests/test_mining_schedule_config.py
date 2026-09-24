"""Exercise scheduler methods without starting production scheduler."""
import ast, sys, unittest, copy
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mining_policy
TREE=ast.parse((Path(__file__).resolve().parents[1]/'scheduler.py').read_text())
CLS=next(n for n in TREE.body if isinstance(n,ast.ClassDef) and n.name=='SchedulerManager')
class Tests(unittest.TestCase):
    def methods(self):
        ns={'TZ':'Asia/Shanghai'}
        nodes=[n for n in CLS.body if isinstance(n,ast.FunctionDef) and n.name in ('set_mining_config','_apply_state')]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'scheduler_methods','exec'),ns)
        return ns
    def test_save_and_dynamic_reschedule(self):
        ns=self.methods()
        mgr=MagicMock()
        state={'loopengine':{'enabled':False},'multitype_mine':{'enabled':True,'hour':19,'minute':30,'params':{}}}
        mgr._state.side_effect=lambda:copy.deepcopy(state)
        mgr._save_state.side_effect=lambda new:state.update(new)
        ns['set_mining_config'](mgr,True,3,2,20,17,15,2,True)
        self.assertEqual(state['multitype_mine']['params']['daily_batches'],3)
        self.assertFalse(state['loopengine']['enabled'])
        mgr._owner=True
        mgr.sched.get_job.return_value=MagicMock()
        ns['_apply_state'](mgr)
        trigger=mgr.sched.reschedule_job.call_args.kwargs['trigger']
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now=datetime(2026,9,24,16,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.assertEqual(trigger.get_next_fire_time(None,now).hour,17)
        nxt=trigger.get_next_fire_time(None,now)
        self.assertEqual(trigger.get_next_fire_time(nxt,nxt).hour,19)
    def test_invalid_config_not_saved(self):
        mgr=MagicMock()
        with self.assertRaises(ValueError):self.methods()['set_mining_config'](mgr,True,4,1,15,23,0,1,True)
        mgr._save_state.assert_not_called()
if __name__=='__main__':unittest.main()
