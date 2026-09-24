import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import library
import scheduler


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'reports.db'
        with sqlite3.connect(self.path) as c:
            c.executescript(library._SCHEMA)
            c.execute("ALTER TABLE strategies ADD COLUMN status TEXT")
            c.execute("ALTER TABLE strategies ADD COLUMN is_winrate TEXT")
            c.execute("INSERT INTO strategies(name,status,oos_winrate) VALUES ('s','active','80%')")
        self.conn_patch = patch.object(library, '_lconn', side_effect=lambda: sqlite3.connect(self.path))
        self.conn_patch.start()
        with sqlite3.connect(self.path) as c:
            self.pack = library._strategies_on_connection(c)["s"]

    def tearDown(self):
        self.conn_patch.stop()
        self.tmp.cleanup()

    def test_repeated_day_preserves_reports_and_failure_clears_old_score(self):
        first = library.save_strategy_validation('s', {'eval_date': '2026-09-24', 'status': 'active', 'oos_winrate': .8})
        second = library.save_strategy_validation('s', {'eval_date': '2026-09-24', 'status': 'degraded', 'ok': False,
                                                      'sharpe': float('nan')}, update_status=True)
        self.assertNotEqual(first['report_id'], second['report_id'])
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM strategy_validation_reports').fetchone()[0], 2)
            self.assertEqual(c.execute('SELECT status,oos_winrate FROM strategies').fetchone(), ('degraded', None))
            doc = json.loads(c.execute('SELECT metrics_json FROM strategy_validation').fetchone()[0])
            self.assertIsNone(doc['sharpe'])

    def test_exception_is_persisted(self):
        with patch.object(library, 'list_strategies', return_value={'s': self.pack}), \
             patch.object(scheduler, 'get_last_trade_day', return_value='2026-09-24'), \
             patch.object(scheduler, '_compute_strategy_validation', side_effect=ValueError('missing price')):
            result = scheduler.revalidate_strategy('s')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'ValueError')
        self.assertIn('report_id', result)
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT status FROM strategies').fetchone()[0], 'degraded')

    def test_version_drift_archives_without_publishing(self):
        changed = {**self.pack, 'pool_name': 'other'}
        with patch.object(library, 'list_strategies', side_effect=[{'s': self.pack}, {'s': changed}]), \
             patch.object(scheduler, 'get_last_trade_day', return_value='2026-09-24'), \
             patch.object(scheduler, '_compute_strategy_validation', side_effect=ValueError('missing')):
            result = scheduler.revalidate_strategy('s')
        self.assertEqual(result['assessment_status'], 'stale_version')
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM strategy_validation').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT status FROM strategies').fetchone()[0], 'active')
            self.assertEqual(c.execute('SELECT count(*) FROM strategy_validation_reports').fetchone()[0], 1)

    def test_write_failure_rolls_back_evidence_and_status(self):
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TRIGGER reject_validation BEFORE INSERT ON strategy_validation BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            library.save_strategy_validation('s', {'eval_date': '2026-09-24', 'status': 'degraded'}, update_status=True)
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM strategy_validation_reports').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT status FROM strategies').fetchone()[0], 'active')

    def test_paused_strategy_never_computes(self):
        with patch.object(library, 'list_strategies', return_value={'s': {**self.pack, 'status': 'paused'}}), \
             patch.object(scheduler, '_compute_strategy_validation') as compute:
            scheduler.revalidate_strategy('s')
        compute.assert_not_called()

    def test_legacy_daily_record_is_archived_before_replacement(self):
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO strategy_validation(strategy_name,eval_date,metrics_json,created_at) VALUES (?,?,?,?)",
                      ('s', '2026-09-24', '{"ok":true,"oos_winrate":0.8}', 'old'))
        library.save_strategy_validation('s', {'eval_date': '2026-09-24', 'ok': False, 'status': 'degraded'})
        with sqlite3.connect(self.path) as c:
            rows = c.execute('SELECT report_id,report_json FROM strategy_validation_reports').fetchall()
            self.assertEqual(len(rows), 2)
            legacy = next(json.loads(raw) for key, raw in rows if key.startswith('legacy-'))
            self.assertEqual(legacy['oos_winrate'], .8)

    def test_pool_missing_does_not_fall_back(self):
        with patch.object(scheduler, 'all_pools', return_value={'沪深300': ['x']}), \
             patch.object(scheduler.sig, 'get_panel_cached') as read:
            with self.assertRaisesRegex(ValueError, '禁止回退'):
                scheduler._compute_strategy_validation('s', {**self.pack, 'pool_name': 'missing'}, '2026-09-24')
        read.assert_not_called()

    def test_transaction_rechecks_version_after_external_edit(self):
        from execution_gate import strategy_version
        expected = strategy_version(self.pack)
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE strategies SET top_n=99 WHERE name='s'")
        report = library.save_strategy_validation('s', {'eval_date': '2026-09-24', 'status': 'degraded'},
                                                   update_status=True, expected_version=expected)
        self.assertFalse(report['published'])
        self.assertEqual(report['assessment_status'], 'stale_version')
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM strategy_validation').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT status FROM strategies').fetchone()[0], 'active')

    def test_pause_before_publication_does_not_publish(self):
        from execution_gate import strategy_version
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE strategies SET status='paused' WHERE name='s'")
        result = library.save_strategy_validation('s', {'eval_date': '2026-09-24', 'status': 'active'},
                                                 update_status=True, expected_version=strategy_version(self.pack))
        self.assertFalse(result['published'])

    def test_reader_does_not_create_schema_and_returns_failures(self):
        from validation_report_reader import read_reports
        self.assertEqual(read_reports(self.path), [])
        with sqlite3.connect(self.path) as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='strategy_validation_reports'").fetchone())
        result = library.save_strategy_validation('s', {'eval_date': '2026-09-24', 'ok': False,
                                                       'status': 'degraded', 'error': 'missing data'})
        entries = read_reports(self.path)
        self.assertEqual(entries[0]['report_id'], result['report_id'])
        self.assertEqual(entries[0]['report']['error'], 'missing data')

    def test_deadline_archives_and_preserves_old_status(self):
        import time
        with patch.object(library, 'list_strategies', return_value={'s': self.pack}), \
             patch.object(scheduler, 'get_last_trade_day', return_value='2026-09-24'), \
             patch.object(scheduler, '_compute_strategy_validation', side_effect=lambda *args: time.sleep(.2)):
            result = scheduler.revalidate_strategy('s', timeout_seconds=.02)
        self.assertEqual(result['assessment_status'], 'timeout')
        self.assertFalse(result['published'])
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('SELECT status,oos_winrate FROM strategies').fetchone(), ('active', '80%'))
            self.assertEqual(c.execute('SELECT count(*) FROM strategy_validation').fetchone()[0], 0)
