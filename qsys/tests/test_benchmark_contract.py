import unittest
import numpy as np
import pandas as pd
from research_backtest import benchmark_returns, window_contract, WINDOW_POLICY
from test_factor_eval import _make_panel_and_vals
import factor_eval as fe


class BenchmarkTests(unittest.TestCase):
    def test_future_missing_member_is_not_removed(self):
        close = pd.DataFrame([[100, 100], [110, np.nan]], columns=['a', 'b'])
        forward = close.shift(-1) / close - 1
        with self.assertRaisesRegex(ValueError, '禁止事后剔除'):
            benchmark_returns(close, forward, 0)

    def test_membership_uses_start_price_only(self):
        close = pd.DataFrame([[100, np.nan], [110, 50]], columns=['a', 'b'])
        result = benchmark_returns(close, close.shift(-1) / close - 1, 0)
        self.assertEqual(result.index.tolist(), ['a'])
        self.assertAlmostEqual(result.mean(), .1)

    def test_static_benchmark_failure_even_if_stock_not_selected(self):
        panel, vals = _make_panel_and_vals(n_days=12)
        days = sorted(panel.index.get_level_values('datetime').unique())
        stock = panel.index.get_level_values('instrument').unique()[0]
        panel.loc[(days[5], stock), '$close'] = np.nan
        with self.assertRaisesRegex(ValueError, '对照组合'):
            fe.static_backtest({'a': vals}, panel, {'a': (1, 1)}, 5, norms={})

    def test_static_report_records_calculation_contract(self):
        panel, vals = _make_panel_and_vals(n_days=12)
        result = fe.static_backtest({'a': vals}, panel, {'a': (1, 1)}, 5, norms={})
        self.assertEqual(result.attrs['calculation_version'], WINDOW_POLICY)
        self.assertFalse(result.attrs['trading_eligible'])
        self.assertEqual(result.attrs['monthly_basis'], window_contract()['monthly_basis'])

    def test_walk_forward_report_preserves_contract(self):
        panel, vals = _make_panel_and_vals(n_days=100)
        dates = sorted(panel.index.get_level_values('datetime').unique())
        result = fe.walk_forward({'a': vals}, panel, '等权', 5, est=80,
                                 min_factors=1, ic_full={'a': pd.Series(.02, index=dates)}, norms={})
        self.assertFalse(result.empty)
        self.assertEqual(result.attrs['calculation_version'], WINDOW_POLICY)
        self.assertIn('not_verified', result.attrs['universe_basis'])

    def test_walk_forward_missing_benchmark_maturity_fails(self):
        panel, vals = _make_panel_and_vals(n_days=100)
        dates = sorted(panel.index.get_level_values('datetime').unique())
        stock = panel.index.get_level_values('instrument').unique()[0]
        panel.loc[(dates[85], stock), '$close'] = np.nan
        with self.assertRaisesRegex(ValueError, '对照组合'):
            fe.walk_forward({'a': vals}, panel, '等权', 5, est=80,
                            min_factors=1, ic_full={'a': pd.Series(.02, index=dates)}, norms={})

    def test_missing_sharpe_is_not_described_as_negative(self):
        result = pd.DataFrame({'x': [1]})
        result.attrs['sharpe'] = None
        report = fe.backtest_credibility_score(result)
        self.assertTrue(any('夏普不可用' in x for x in report['warnings']))
