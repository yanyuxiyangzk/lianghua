import unittest
import json
from contextlib import ExitStack
from unittest.mock import MagicMock
from unittest.mock import patch
import numpy as np
import pandas as pd
import factor_eval as fe
from research_backtest import window_metrics
from test_factor_eval import _make_panel_and_vals


class CalendarTests(unittest.TestCase):
    def test_maturity_months_use_compounding(self):
        days = pd.bdate_range('2025-12-01', periods=50)
        m = window_metrics([.1, -.1, -.1], days[[0, 5, 25]], days[[5, 10, 30]], days)
        self.assertEqual(m['monthly_winrate'], 0)
        self.assertEqual(m['max_consec_loss_months'], 2)
        self.assertAlmostEqual(m['ann_return'], (.9 * .9 * 1.1)**(252/30)-1)
        self.assertIsNone(m['sharpe'])

    def test_static_overlap_and_invalid_parameters(self):
        for kwargs in ({'step': 1}, {'step': 0}, {'fwd_days': True}, {'cost': -1}):
            with self.assertRaises(ValueError):
                fe.static_backtest({}, pd.DataFrame(), {}, 5, **kwargs)

    def test_static_missing_date_cannot_shift_schedule(self):
        panel, vals = _make_panel_and_vals(n_days=30)
        days = sorted(panel.index.get_level_values('datetime').unique())
        vals = vals[vals.index.get_level_values('datetime') != days[5]]
        with self.assertRaisesRegex(ValueError, '不能压缩'):
            fe.static_backtest({'a': vals}, panel, {'a': (1, 1)}, 5, norms={})

    def test_static_gap_requires_new_roundtrip(self):
        panel, vals = _make_panel_and_vals(n_days=26)
        scores = pd.Series(range(50), index=panel.index.get_level_values('instrument').unique())
        with patch.object(fe, '_score_at', return_value=scores):
            result = fe.static_backtest({'a': vals}, panel, {'a': (1, 1)}, 5, step=10, cost=.0025, norms={})
        np.testing.assert_allclose(result['组合换手率'], 1)
        np.testing.assert_allclose(result['组合超额'] - result['组合扣费超额'], .0025)
        self.assertEqual(result['收益到期日'].iloc[0], str(sorted(panel.index.get_level_values('datetime').unique())[5])[:10])

    def test_walk_forward_missing_day_fails_before_scoring(self):
        panel, vals = _make_panel_and_vals(n_days=120)
        dates = pd.DatetimeIndex(sorted(panel.index.get_level_values('datetime').unique()))
        missing = vals[vals.index.get_level_values('datetime') != dates[80]]
        ic = pd.Series(.02, index=dates)
        with self.assertRaisesRegex(ValueError, '不能压缩'):
            fe.walk_forward({'a': missing}, panel, '等权', 5, est=80, min_factors=1,
                            ic_full={'a': ic}, norms={})

    def test_forward_return_order_invariant(self):
        panel, _ = _make_panel_and_vals(n_days=30)
        pd.testing.assert_frame_equal(fe.forward_returns(panel, 5), fe.forward_returns(panel.iloc[::-1], 5))

    def test_strategy_maturity_dates_full_logs_and_shared_metrics(self):
        import strategy_backtest as sb
        panel, vals = _make_panel_and_vals(n_days=41)
        days = sorted(panel.index.get_level_values('datetime').unique())
        conn = MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value = (
            json.dumps([{'name': 'a'}]), '等权', 'fixture')
        with ExitStack() as st:
            st.enter_context(patch.object(sb.library, '_lconn', return_value=conn))
            st.enter_context(patch.object(sb, 'all_pools', return_value={'fixture': list(range(50))}))
            st.enter_context(patch.object(sb, 'get_last_trade_day', return_value=str(days[-1])[:10]))
            st.enter_context(patch.object(sb, 'trade_day_offset', return_value=str(days[0])[:10]))
            st.enter_context(patch.object(sb.sig, 'get_panel_cached', return_value=panel))
            st.enter_context(patch.object(sb.fe, 'get_factor_values', return_value=vals))
            st.enter_context(patch.object(sb.sig, 'scoring_norms', return_value={}))
            result = sb.backtest_strategy('fixture', top_n=10)
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['picks']), 8)
        self.assertEqual(len(result['picks'][0]['picks']), 10)
        self.assertEqual(result['nav_dates'][1], str(days[5])[:10])
        self.assertEqual(result['nav_dates'][-1], str(days[40])[:10])
        expected = window_metrics(pd.Series(result['nav']).pct_change().dropna(),
                                  [x['date'] for x in result['picks']],
                                  [x['end_date'] for x in result['picks']], days)
        self.assertEqual(result['ann_return'], round(expected['ann_return'], 4))
        with patch('builtins.print'):
            sb.print_result(result)
