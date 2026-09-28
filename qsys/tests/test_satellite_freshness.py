import contextlib
import json
import sqlite3
import unittest
from unittest.mock import patch

import satellite_candidates as candidates
from loopengine.tree import parse, required_observations
from selection_policy import SELECTION_POLICY
from execution_gate import strategy_version


class SatelliteFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''
        CREATE TABLE picks(id INTEGER PRIMARY KEY,trade_date TEXT,created_at TEXT,source TEXT,pack_name TEXT);
        CREATE TABLE pick_items(pick_id INTEGER,code TEXT,score REAL,rank INTEGER);
        CREATE TABLE pick_decision_evidence(pick_id INTEGER,risk_json TEXT);
        ''')
        self.pack = {'factors': [{'name': 'a', 'weight': 1, 'direction': 1}]}
        self.mock = patch.object(candidates.exp, '_conn', lambda: contextlib.nullcontext(self.db))
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.addCleanup(self.db.close)

    def add(self, ident, day, source='satellite_scan', name='sat', valid=True):
        self.db.execute('INSERT INTO picks VALUES(?,?,?,?,?)', (ident, day, f'2026-09-28 13:{ident:02d}:00', source, name))
        self.db.execute('INSERT INTO pick_items VALUES(?,?,?,?)', (ident, f'SH{ident:06d}', .8, 1))
        evidence = {'strategy_version': strategy_version(self.pack), 'selection_policy': SELECTION_POLICY} if valid else {}
        self.db.execute('INSERT INTO pick_decision_evidence VALUES(?,?)', (ident, json.dumps(evidence)))

    def test_stale_formal_does_not_hide_current_observation(self):
        self.add(1, '2026-09-21')
        self.add(2, '2026-09-24', source='sched_satellite_scan')
        self.assertTrue(candidates.latest_candidates('sat', self.pack, '2026-09-24').empty)
        self.assertEqual(candidates.latest_candidates('sat', self.pack, '2026-09-24', True).id.tolist(), [2])
        self.assertEqual(candidates.latest_candidates('sat', self.pack, history=True).id.tolist(), [1])

    def test_latest_single_batch_and_selected_strategy(self):
        self.add(1, '2026-09-24')
        self.add(2, '2026-09-24')
        self.add(3, '2026-09-24', name='other')
        self.assertEqual(candidates.latest_candidates('sat', self.pack, '2026-09-24').id.tolist(), [2])

    def test_latest_invalid_version_does_not_fall_back(self):
        self.add(1, '2026-09-24')
        self.add(2, '2026-09-24', valid=False)
        self.assertTrue(candidates.latest_candidates('sat', self.pack, '2026-09-24').empty)

    def test_nested_warmup(self):
        tree = parse('decay_linear(ma(sub(ts_min(corr(hl_ratio,open,3),200),ts_max(corr(amount,volume,150),60)),40),200)')
        self.assertEqual(required_observations(tree), 447)
        self.assertEqual(required_observations(parse('delta(ma(amplitude,20),5)')), 26)

    def test_scan_failure_is_not_success_string(self):
        import scheduler
        with patch.object(scheduler, '_satellite_trading_day', return_value=True), patch('selection_policy.completed_signal_day', return_value='2026-09-24'), patch('library.list_strategies', return_value={'sat': {**self.pack, 'pool_name': 'pool'}}), patch.object(scheduler, '_satellite_pack_name', return_value='sat'), patch.object(scheduler, 'all_pools', return_value={'pool':['SH600001']}), patch.object(scheduler, 'compute_pack_picks', side_effect=ValueError('缺少有效值')):
            with self.assertRaisesRegex(RuntimeError, '行情截止2026-09-24'):
                scheduler.job_satellite_scan()


if __name__ == '__main__':
    unittest.main()
