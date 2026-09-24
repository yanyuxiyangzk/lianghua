import unittest
from unittest.mock import patch
from coverage_report import estimate_days, build, configured_windows


class CoverageTests(unittest.TestCase):
    def test_saved_windows_respect_pool_disabled_and_worker_cap(self):
        state = {'le_factor_eval': {'enabled': True, 'params': {'batch': 1000, 'pool_name': 'p'}},
                 'le_factor_eval_pm': {'enabled': False, 'params': {'batch': 30, 'pool_name': 'p'}},
                 'le_factor_eval_noon': {'enabled': True, 'params': {'batch': 20, 'pool_name': 'other'}}}
        self.assertEqual([w['batch'] for w in configured_windows('p', state)], [100])
        self.assertEqual(configured_windows('none', state), [])

    def test_estimate_is_conservative_and_invalid_capacity(self):
        self.assertEqual(estimate_days(39867, 70), 39867 / 70)
        self.assertIsNone(estimate_days(10, 0))
        self.assertEqual(estimate_days(0, 70), 0.0)

    def test_report_labels_terminal_outcomes_as_done_not_approved(self):
        with patch('factor_evaluation_queue.coverage', return_value={'total': 10, 'done': 4, 'pending': 6,
            'running': 0, 'valid': 3, 'sample_insufficient': 1, 'data_error': 0, 'compute_failed': 0, 'blocked': 0, 'coverage': .4}):
            r = build(batches=(20,20,30))
        self.assertEqual(r['daily_capacity_estimate'], 70)
        self.assertIn('不代表交易资格', r['interpretation'])
