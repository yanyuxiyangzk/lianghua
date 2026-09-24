import unittest
import numpy as np
import pandas as pd
from research_backtest import group_research, period_metrics


def fixture(n=11):
    days = pd.bdate_range('2025-12-22', periods=n)
    codes = [f's{i:02}' for i in range(10)]
    close = pd.DataFrame(100., index=days, columns=codes)
    vals = pd.DataFrame([list(range(10))] * n, index=days, columns=codes)
    close.index.name = vals.index.name = 'datetime'
    close.columns.name = vals.columns.name = 'instrument'
    return close, vals.stack()


class ResearchTests(unittest.TestCase):
    def test_hand_computed_two_periods_and_initial_loss(self):
        close, vals = fixture()
        close.iloc[5, 5:] = 90
        close.iloc[10, 5:] = 108
        result = group_research(vals, close.stack().to_frame('$close'), 2, 5, 5)
        np.testing.assert_allclose(result['ls_ret'], [-.1, .2])
        np.testing.assert_allclose(result['ls_nav'], [1, .9, 1.08])
        self.assertEqual(list(result['ls_ret'].index), [close.index[5], close.index[10]])
        self.assertAlmostEqual(result['metrics']['max_drawdown'], -.1)
        self.assertAlmostEqual(result['metrics']['ann_return'], 1.08 ** (252/10) - 1)

    def test_flat_prices_and_round_trip_cost_on_both_legs(self):
        close, vals = fixture()
        result = group_research(vals, close.stack().to_frame('$close'), 2, 5, 5, .001)
        np.testing.assert_allclose(result['ls_ret'], [-.002, -.002])
        self.assertAlmostEqual(result['ls_nav'].iloc[-1], .998**2)
        self.assertIsNone(result['metrics']['sharpe'])

    def test_overlap_rejected(self):
        close, vals = fixture(41)
        with self.assertRaises(ValueError):
            group_research(vals, close.stack().to_frame('$close'), 2, 20, 1)
        result = group_research(vals, close.stack().to_frame('$close'), 2, 20, 20)
        self.assertEqual(len(result['ls_ret']), 2)

    def test_missing_selected_price_invalidates_curve(self):
        close, vals = fixture()
        close.iloc[5, -1] = np.nan
        result = group_research(vals, close.stack().to_frame('$close'), 2, 5, 5)
        self.assertEqual(result['status'], 'incomplete')
        self.assertTrue(result['ls_nav'].empty)
        self.assertIn('禁止事后换股', result['reasons'][0])

    def test_missing_signal_cannot_compress_calendar(self):
        close, vals = fixture(16)
        vals = vals[vals.index.get_level_values('datetime') != close.index[5]]
        result = group_research(vals, close.stack().to_frame('$close'), 2, 5, 5)
        self.assertTrue(result['ls_nav'].empty)
        self.assertIn('缺少因子截面', result['reasons'][0])

    def test_group_returns_are_equal_weight_means_not_medians(self):
        close, vals = fixture(6)
        close.iloc[5, -1] = 150
        result = group_research(vals, close.stack().to_frame('$close'), 2, 5, 5)
        self.assertAlmostEqual(result['group_mean']['G2'], .1)

    def test_unsorted_input_and_ties_are_deterministic(self):
        close, vals = fixture()
        vals[:] = 1
        a = group_research(vals, close.stack().to_frame('$close'), 2, 5, 5)
        b = group_research(vals.iloc[::-1], close.stack().iloc[::-1].to_frame('$close'), 2, 5, 5)
        pd.testing.assert_series_equal(a['ls_nav'], b['ls_nav'])

    def test_later_prices_do_not_change_earlier_period(self):
        close, vals = fixture()
        a = group_research(vals, close.stack().to_frame('$close'), 2, 5, 5)
        close.iloc[10, -1] = 1000
        b = group_research(vals, close.stack().to_frame('$close'), 2, 5, 5)
        self.assertEqual(a['ls_ret'].iloc[0], b['ls_ret'].iloc[0])

    def test_ruin_cannot_continue_compounding(self):
        with self.assertRaises(ValueError):
            period_metrics([-.5, -1.1, .5], 15)

    def test_sampling_gap_uses_elapsed_time_and_no_sharpe(self):
        close, vals = fixture(16)
        close.iloc[5, 5:] = 110
        close.iloc[15, 5:] = 110
        result = group_research(vals, close.stack().to_frame('$close'), 2, 5, 10)
        self.assertAlmostEqual(result['metrics']['ann_return'], 1.21 ** (252/15) - 1)
        self.assertIsNone(result['metrics']['sharpe'])


if __name__ == '__main__':
    unittest.main()
