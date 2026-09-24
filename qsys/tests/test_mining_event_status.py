import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from mining_event_monitor import MiningEventStatus

class EventStatusTests(unittest.TestCase):
    def test_type_changes_and_clears(self):
        observer = MiningEventStatus()
        start = datetime(2026,9,24,11,11,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        live = {'fresh':True,'running':{'multitype_mine':start}}
        self.assertIsNone(observer.snapshot(live))
        for name in ['量价','资金流']:
            observer.accept({'type':'round_start','ts':'2026-09-24T11:12:00','factor_type':name,'iteration':1524})
            self.assertEqual(observer.snapshot(live)['factor_type'],name)
        self.assertIsNone(observer.snapshot({'fresh':False,'running':live['running']}))
        observer.accept({'type':'round_complete'})
        self.assertIsNone(observer.snapshot(live))

    def test_old_event_cannot_label_new_batch(self):
        observer = MiningEventStatus()
        observer.accept({'type':'round_start','ts':'2026-09-24T11:12:00','factor_type':'量价'})
        start = datetime(2026,9,24,12,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.assertIsNone(observer.snapshot({'fresh':True,'running':{'multitype_mine':start}}))

    def test_recent_logs_survive_snapshot_reads_and_are_bounded(self):
        observer = MiningEventStatus()
        for i in range(205):
            observer.accept({'type': 'step_update', 'step': 4, 'batch_left': i})
        events = observer.details()['events']
        self.assertEqual(len(events), 200)
        self.assertEqual(events[0]['batch_left'], 5)
        self.assertEqual(observer.details()['events'], events)
        observer.accept({'type': 'job_start', 'job_key': 'quote_collect'})
        self.assertEqual(len(observer.details()['events']), 200)
