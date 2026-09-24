"""Risk calculation tests with isolated account and NAV fixtures."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
TMP = tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR'] = TMP.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import experience
import broker

class Tests(unittest.TestCase):
    def run_risk(self, history=None, baseline=None, valuation=True):
        if history is None:
            history = pd.DataFrame({'daily_ret': [.01, -.01, .02, -.02, .01], 'drawdown': [0]*5})
        if baseline is None:
            baseline = pd.DataFrame({'total_assets': [100.], 'nav': [1.]})
        with patch.object(experience, 'nav_stats', return_value={'净值天数': 5, '当前净值': 1., '最大回撤': 0.}), patch.object(experience, '_conn', return_value=MagicMock()), patch.object(broker, 'get_account', return_value={'总资产': 90., '估值有效': valuation}), patch.object(experience.pd, 'read_sql', side_effect=[history, baseline]):
            return experience.portfolio_risk(use_live=True)

    def test_snapshot_rejects_invalid_valuation_before_writing(self):
        with patch.object(broker, 'get_account', return_value={'估值有效': False, '总资产': 100.}), patch.object(experience, '_conn') as conn:
            with self.assertRaisesRegex(ValueError, '估值无效'):
                experience.snapshot_nav_today()
            conn.assert_not_called()

    def test_snapshot_rejects_invalid_totals_before_writing(self):
        for value in (float('nan'), float('inf'), 0., -1.):
            with patch.object(broker, 'get_account', return_value={'估值有效': True, '总资产': value}), patch.object(experience, '_conn') as conn:
                with self.assertRaises(ValueError):
                    experience.snapshot_nav_today()
                conn.assert_not_called()

    def test_unreliable_valuation_blocks_risk(self):
        for value in (False, None):
            self.assertFalse(self.run_risk(valuation=value)['ok'])

    def test_satellite_rechecks_changed_risk_state(self):
        import json
        flag = MagicMock()
        for state in ({'date': '2026-09-23', 'halt': False, 'level': 'normal'},
                      {'date': '2026-09-24', 'halt': True, 'level': 'normal'},
                      {'date': '2026-09-24', 'halt': 'false', 'level': 'normal'}):
            flag.read_text.return_value = json.dumps(state)
            with patch.object(experience, 'risk_halt_today', return_value=(False, '')), patch.object(experience, '_RISK_FLAG', flag):
                self.assertTrue(experience.satellite_halt_today('2026-09-24')[0])
        flag.read_text.return_value = json.dumps({'date': '2026-09-24', 'halt': False, 'level': 'normal'})
        with patch.object(experience, 'risk_halt_today', return_value=(False, '')), patch.object(experience, '_RISK_FLAG', flag):
            self.assertFalse(experience.satellite_halt_today('2026-09-24')[0])

    def test_valid_live_drawdown(self):
        result = self.run_risk()
        self.assertTrue(result['ok'])
        self.assertAlmostEqual(result['dd_now'], -.1)

    def test_missing_baseline_does_not_use_yesterday(self):
        self.assertFalse(self.run_risk(baseline=pd.DataFrame())['ok'])

    def test_baseline_query_failure_does_not_use_yesterday(self):
        self.assertFalse(self.run_risk(baseline=RuntimeError('database unavailable'))['ok'])

    def test_invalid_nav_rejected(self):
        for value in (float('nan'), float('inf'), 0., -1.):
            self.assertFalse(self.run_risk(baseline=pd.DataFrame({'total_assets': [100.], 'nav': [value]}))['ok'])

    def test_invalid_return_series_rejected(self):
        for value in (float('nan'), float('inf')):
            self.assertFalse(self.run_risk(history=pd.DataFrame({'daily_ret': [0., 0., 0., 0., value], 'drawdown': [0]*5}))['ok'])

if __name__ == '__main__': unittest.main()
