import unittest
from mining_queue_view import queue_rows

class QueueTests(unittest.TestCase):
    def test_recorded_queue_and_next(self):
        p={'queue':[{'rotation':1,'factor_type':'量价','status':'round_complete'},
                    {'rotation':1,'factor_type':'资金流','status':'preparing'},
                    {'rotation':2,'factor_type':'量价','status':'queued'}]}
        rows, next_label=queue_rows(p,[])
        self.assertEqual([r['状态'] for r in rows],['本轮完成','准备中','排队中'])
        self.assertIn('第 2 轮 · 量价',next_label)
        p['queue'][1]['status']='running'
        self.assertEqual(queue_rows(p,[])[0][1]['状态'],'运行中')

    def test_no_queue_does_not_invent_order(self):
        rows,next_label=queue_rows({'factor_type':'资金流','status':'running'},['量价','资金流'])
        self.assertEqual([r['状态'] for r in rows],['待确认','运行中'])
        self.assertIn('待确认',next_label)
