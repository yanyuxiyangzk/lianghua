import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import mining_event_journal as journal

class JournalTests(unittest.TestCase):
    def test_persisted_log_read_and_stale_connection(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(journal,'DB',Path(tmp)/'events.db'):
            self.assertEqual(journal.read_details()['events'], [])
            with sqlite3.connect(journal.DB) as c:
                c.executescript('CREATE TABLE events(id INTEGER PRIMARY KEY,payload TEXT); CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT);')
                for step in (4,5,4):
                    c.execute('insert into events(payload) values (?)',(json.dumps({'type':'step_update','step':step}),))
                c.execute('insert into state values (?,?)',('connection',json.dumps({'connected':True,'ts':time.time()})))
            self.assertTrue(journal.read_details()['connected'])
            self.assertEqual([e['step'] for e in journal.read_details()['events']], [4,5,4])
            with sqlite3.connect(journal.DB) as c:
                c.execute('update state set value=?',(json.dumps({'connected':True,'ts':0}),))
            self.assertFalse(journal.read_details()['connected'])
            self.assertEqual(len(journal.read_details()['events']),3)

    def test_completed_round_is_not_reported_running(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        started = datetime(2026,9,24,11,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        live = {'fresh':True,'running':{'multitype_mine':started}}
        event = {'type':'round_complete','ts':'2026-09-24T12:00:00',
                 'iteration':1525,'stats':{'factor_type':'资金流'}}
        with patch.object(journal,'read_details',return_value={'current':None,'connected':True,'events':[event]}):
            self.assertEqual(journal.snapshot(live)['status'],'round_complete')
            self.assertEqual(journal.snapshot(live)['factor_type'],'资金流')
