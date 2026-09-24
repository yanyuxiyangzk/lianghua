"""Isolated regression checks; no production data or external requests."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
TMP = tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR'] = TMP.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
import factor_eval as fe
import gates
import library
import validation_policy as policy
from loopengine.extra_frames import conservative_available_day
from test_factor_eval import _make_panel_and_vals

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.panel, self.vals = _make_panel_and_vals(n_days=600, n_stocks=35)

    def wf(self, **kw):
        return fe.walk_forward({'f': self.vals}, self.panel, '等权', 5,
                               min_factors=1, norms={'f':'zscore'}, **kw)

    def test_test_window_uses_prior_training(self):
        wf = self.wf(start_idx=500, end_idx=580)
        days = sorted(self.vals.index.get_level_values('datetime').unique())
        self.assertFalse(wf.empty)
        self.assertEqual(wf.iloc[0]['调仓日'], str(days[500])[:10])
        self.assertLess(pd.Timestamp(wf.iloc[-1]['调仓日']), days[575])

    def test_cv_has_three_nonoverlapping_folds(self):
        with patch('signals.scoring_norms', return_value={'f':'zscore'}):
            cv = fe.time_series_cv({'f':self.vals}, self.panel, '等权', 5, n_folds=3)
        self.assertEqual(len(cv['folds']), 3)
        for a,b in zip(cv['folds'], cv['folds'][1:]):
            self.assertLess(a['end'], b['start'])

    def test_future_values_do_not_change_prior_decisions(self):
        before = self.wf(end_idx=400)
        v = self.vals.copy()
        days = sorted(v.index.get_level_values('datetime').unique())
        v.loc[v.index.get_level_values('datetime') >= days[400]] *= -100
        with patch('signals.scoring_norms', return_value={'f':'zscore'}):
            after = fe.walk_forward({'f':v}, self.panel, '等权', 5, min_factors=1, end_idx=400)
        pd.testing.assert_frame_equal(before, after)

    def test_overlapping_holding_period_rejected(self):
        with self.assertRaises(ValueError): self.wf(step=1, fwd_days=5)

    def test_insufficient_cv_never_passes(self):
        panel, vals = _make_panel_and_vals(n_days=300)
        cv = fe.time_series_cv({'f':vals}, panel, '等权', 5, n_folds=3)
        self.assertFalse(cv.get('passed', False))

    def test_sample_and_pit_fail_closed(self):
        self.assertTrue(policy.sample_check(pd.Series([.1]*10), '龙虎榜'))
        df = pd.DataFrame([dict(available_at='2026-01-02',decision_at='2026-01-01',evidence_ref='x')])
        self.assertFalse(policy.point_in_time_report(df)['passed'])
        df.available_at = '2025-12-31'
        self.assertTrue(policy.point_in_time_report(df)['passed'])
        df.available_at = None
        self.assertFalse(policy.point_in_time_report(df)['passed'])

    def test_collection_time_not_report_date(self):
        days = conservative_available_day(pd.Series(['2026-09-23 18:00:00', None]))
        self.assertEqual(days.iloc[0], '2026-09-24')
        self.assertTrue(pd.isna(days.iloc[1]))

    def test_missing_regime_cannot_pass(self):
        wf = pd.DataFrame({'调仓日':[str(x) for x in range(30)], '优化组合扣费超额':[.01]*30})
        result = policy.regime_report(wf, {str(x):'bull' for x in range(30)}, 'all')
        self.assertFalse(result['passed'])
        self.assertTrue(policy.regime_report(wf, {str(x):'bull' for x in range(30)}, 'bull')['passed'])

    def test_search_budget_is_hard_in_both_gates(self):
        with patch.object(gates, 'global_trial_count', return_value={'total':100000}), \
             patch.object(fe, 'ic_pvalue_robust', return_value=.00001):
            for fn in (gates.evaluate_gates, gates.evaluate_gates_relaxed):
                result = fn(self.vals, self.panel)
                self.assertFalse(result['pass'])
                self.assertTrue(any('搜索预算' in x for x in result['reasons']))
                self.assertEqual(result['metrics']['搜索校正p值'], 1)

    def test_historical_weights_ignore_current_direction_state(self):
        with patch.object(fe, 'direction_map', side_effect=AssertionError('current state leak')):
            self.assertFalse(self.wf(end_idx=340).empty)

    def test_first_loss_counts_as_drawdown(self):
        self.assertAlmostEqual(gates._max_dd(pd.Series([.9, .95])), -.1)

    def test_five_day_returns_are_not_counted_daily(self):
        fwd = fe.forward_returns(self.panel, 5)
        returns = gates._daily_excess(self.vals, fwd)
        self.assertLessEqual(len(returns), 120)

    def test_both_years_required(self):
        dates = pd.bdate_range('2024-01-01', '2025-12-31', freq='5B')
        returns = pd.Series(np.where(dates.year == 2024, -.005, .01), index=dates)
        with patch.object(gates, '_daily_excess', return_value=returns):
            for fn in (gates.evaluate_gates, gates.evaluate_gates_relaxed):
                result = fn(self.vals, self.panel)
                self.assertTrue(any('2024年超额' in r for r in result['reasons']))

    def test_save_never_reactivates_paused(self):
        library.save_strategy('fixture', {}, status='paused')
        library.save_strategy('fixture', {})
        self.assertEqual(library.list_strategies()['fixture']['status'], 'paused')
        library.save_strategy('new', {})
        self.assertEqual(library.list_strategies()['new']['status'], 'shadow')

if __name__ == '__main__': unittest.main()
