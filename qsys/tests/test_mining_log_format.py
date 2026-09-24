import unittest
from mining_log_format import log_line, round_sections

class LogTests(unittest.TestCase):
    def test_serial_candidates_and_readable_preparation(self):
        events=[{'type':'round_start','batch':2,'iteration':4},
                {'type':'step_update','step':1,'status':'done'},
                {'type':'step_update','step':2,'status':'done','gaps':['动量']},
                {'type':'step_update','step':4,'batch_left':2},
                {'type':'step_update','step':5,'status':'fail','reason':'表达式过深'},
                {'type':'step_update','step':4,'batch_left':1},
                {'type':'round_complete','iteration':4,'stats':{'factor_type':'资金流'}}]
        s=round_sections(events)
        self.assertEqual([c['number'] for c in s['candidates']],[1,2])
        self.assertTrue(s['ended'])
        self.assertIn('构建面板：完成',log_line(events[1]))
        self.assertIn('优先探索：动量',log_line(events[2]))
        self.assertIn('原因：表达式过深',log_line(events[4]))
        self.assertNotIn('{',log_line(events[-1]))

    def test_partial_round_does_not_invent_number(self):
        s=round_sections([{'type':'step_update','step':4,'batch_left':7},
                          {'type':'round_complete','stats':{}}])
        self.assertTrue(s['partial'])
        self.assertTrue(s['ended'])
        self.assertIsNone(s['candidates'][0]['number'])
