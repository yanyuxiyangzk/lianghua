"""Selection contracts: complete factors, frozen definitions, dated signals."""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np
import pandas as pd

import scheduler
import selection_policy as policy
from research_backtest import benchmark_returns, BenchmarkDataError, suspension_valuations, research_close


class SelectionIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.day = '2026-09-24'
        self.codes = ['SH600001', 'SH600002', 'SH600003', 'SH600004']
        index = pd.MultiIndex.from_product([[pd.Timestamp(self.day)], self.codes],
                                          names=['datetime', 'instrument'])
        self.a = pd.Series([1., 2., 3., 4.], index=index)
        self.b = pd.Series([4., 3., 2., 1.], index=index)
        self.factors = [dict(name='a', kind='builtin', weight=.7, direction=1),
                        dict(name='b', kind='builtin', weight=.3, direction=-1)]
        self.pack = dict(pool_name='fixture', factors=self.factors, filters=[], horizon='5日')

    def compute(self, pack=None, series=None):
        import library
        import stock_probability
        with ExitStack() as stack:
            stack.enter_context(patch.object(scheduler.sig, 'get_panel_cached', return_value=self.a.to_frame('$close')))
            stack.enter_context(patch.object(scheduler, 'get_evolved_factors', return_value=[]))
            stack.enter_context(patch.object(library, 'get_factor_registry', return_value=pd.DataFrame()))
            stack.enter_context(patch.object(library, 'get_latest_scorecard', side_effect=AssertionError('no live reweight')))
            stack.enter_context(patch.object(library, 'record_factor_usage'))
            stack.enter_context(patch.object(scheduler.sig, 'compute_builtin', side_effect=series or [self.a, self.b]))
            stack.enter_context(patch.object(scheduler.sig, 'scoring_norms', return_value=None))
            stack.enter_context(patch.object(scheduler.sig, 'apply_filters', side_effect=lambda codes, *_: codes))
            stack.enter_context(patch.object(scheduler.sig, 'industry_cap_select', side_effect=lambda scores, **_: scores))
            stack.enter_context(patch.object(stock_probability, 'probability_overlay', return_value=(None, pd.DataFrame())))
            return scheduler.compute_pack_picks(pack or self.pack, self.codes, self.day, 3)

    def test_fixed_weights_preserve_ranking(self):
        picks, _, weights, _ = self.compute()
        self.assertEqual(weights, {'a': (.7, 1), 'b': (.3, -1)})
        self.assertEqual(list(picks.index), self.codes[::-1][:3])

    def test_missing_definition_never_returns_partial_strategy(self):
        pack = {**self.pack, 'factors': [self.factors[0], dict(name='missing', kind='evolved', weight=.3, direction=1)]}
        with self.assertRaisesRegex(ValueError, '残缺策略'):
            self.compute(pack)

    def test_partial_stock_values_excluded_before_ranking(self):
        b = self.b.copy(); b.iloc[-1] = np.nan
        picks, *_ = self.compute(series=[self.a, b])
        self.assertNotIn(self.codes[-1], picks.index)
        self.assertEqual(len(picks), 3)

    def test_stale_factor_cannot_use_its_own_latest_day(self):
        b = self.b.copy()
        b.index = pd.MultiIndex.from_product([[pd.Timestamp('2026-09-23')], self.codes], names=b.index.names)
        with self.assertRaisesRegex(ValueError, '无有效值'):
            self.compute(series=[self.a, b])

    def test_duplicate_and_invalid_definitions_rejected(self):
        for factors in [[self.factors[0]] * 2, [{**self.factors[0], 'weight': float('inf')}],
                        [{**self.factors[0], 'weight': 0}], [{**self.factors[0], 'direction': 0}]]:
            with self.subTest(factors=factors), self.assertRaises(ValueError):
                policy.validate_factors(factors)

    def test_holiday_uses_certified_previous_session(self):
        with patch('trading_calendar.day_status', return_value=True), \
             patch('trading_calendar.previous_session', return_value=self.day):
            self.assertEqual(policy.signal_date_rejection(self.day, '2026-09-28'), '')
            self.assertIn('禁止', policy.signal_date_rejection('2026-09-21', '2026-09-28'))
            self.assertIn('禁止', policy.signal_date_rejection('2026-09-28', '2026-09-28'))

    def test_unknown_calendar_blocks(self):
        with patch('trading_calendar.day_status', return_value=None):
            self.assertTrue(policy.signal_date_rejection(self.day, '2026-09-28'))

    def test_signal_day_never_uses_unfinished_intraday_bar(self):
        from datetime import datetime
        with patch('trading_calendar.day_status', return_value=True), \
             patch('trading_calendar.previous_session', return_value=self.day):
            self.assertEqual(policy.completed_signal_day(datetime(2026, 9, 28, 11)), self.day)
            self.assertEqual(policy.completed_signal_day(datetime(2026, 9, 28, 19)), '2026-09-28')

    def test_health_checks_actual_nonempty_persisted_output(self):
        import sqlite3
        import experience
        from contextlib import contextmanager
        c = sqlite3.connect(':memory:')
        self.addCleanup(c.close)
        c.executescript('CREATE TABLE picks(id INTEGER,trade_date TEXT,source TEXT,pool_name TEXT);'
                       'CREATE TABLE pick_items(pick_id INTEGER);')
        c.execute("INSERT INTO picks VALUES(1,'2026-09-24','sched_pool_scan','沪深300')")
        @contextmanager
        def conn():
            yield c
        config = {'pool_scan_zz500': {'enabled': False}, 'satellite_scan': {'enabled': False}}
        with patch.object(experience, '_conn', conn), patch.object(scheduler, 'load_json', return_value=config), \
             patch.object(policy, 'completed_signal_day', return_value=self.day):
            with self.assertRaisesRegex(RuntimeError, '产出未达标'):
                scheduler.job_selection_health()
            c.execute('INSERT INTO pick_items VALUES(1)')
            self.assertIn('核验通过', scheduler.job_selection_health())
        with patch('trading_calendar.day_status', return_value=True), patch('trading_calendar.previous_session', return_value=None):
            self.assertTrue(policy.signal_date_rejection(self.day, '2026-09-28'))

    def test_stale_signal_blocks_before_position_writes(self):
        import experience
        with patch.object(policy, 'signal_date_rejection', return_value='过期名单'), \
             patch.object(experience, '_conn', side_effect=AssertionError('must not write')):
            self.assertEqual(experience.position_open_from_picks('2026-09-21', '2026-09-28'), '过期名单')

    def test_benchmark_error_identifies_date_and_stock(self):
        close = pd.DataFrame([[10., 20.]], index=[self.day], columns=self.codes[:2])
        forward = pd.DataFrame([[.1, np.nan]], index=[self.day], columns=self.codes[:2])
        with self.assertRaises(BenchmarkDataError) as ctx:
            benchmark_returns(close, forward, self.day)
        self.assertIn(self.codes[1], str(ctx.exception))
        self.assertIn(self.day, str(ctx.exception))

    def test_scan_rejects_cross_pool_explicit_pack(self):
        import library
        with patch.object(library, 'list_strategies', return_value={'a': {'pool_name': 'other'}}), \
             patch.object(policy, 'completed_signal_day', return_value=self.day):
            with self.assertRaisesRegex(ValueError, '股票池'):
                scheduler.job_pool_scan('fixture', pack='a')

    def test_no_qualified_pack_produces_only_fixed_observation(self):
        import library
        import experience
        picks = pd.Series([3., 2., 1.], index=self.codes[:3])
        weights = {'mom_20d': (1., 1), 'vol_20d': (1., -1), 'volume_ratio_5_20': (1., -1)}
        with patch.object(policy, 'completed_signal_day', return_value=self.day), \
             patch.object(library, 'list_strategies', return_value={}), \
             patch.object(scheduler, 'all_pools', return_value={'fixture': self.codes}), \
             patch.object(scheduler, '_top_packs', return_value=[]), \
             patch.object(scheduler, '_best_pack', return_value=''), \
             patch.object(scheduler, 'compute_pack_picks', return_value=(picks, '完整', weights, {})), \
             patch.object(scheduler.sig, '_write_parquet_atomic'), \
             patch.object(scheduler.sig, 'scoring_norms', return_value=None), \
             patch.object(scheduler, '_selection_decision_evidence', return_value={}), \
             patch.object(experience, 'save_pick', return_value=1) as save:
            result = scheduler.job_pool_scan('fixture')
        self.assertIn('无自动买入资格', result)
        self.assertEqual(save.call_args.kwargs['pool_name'], 'fixture')
        self.assertIsNone(save.call_args.kwargs['pack_name'])

    def test_suspension_marks_preserve_raw_prices_and_stop_at_unknown_gap(self):
        days = pd.date_range('2026-09-14', periods=5)
        raw = pd.DataFrame({'SH600001': [10., np.nan, np.nan, np.nan, 12.]}, index=days)
        raw.index.name = 'datetime'; raw.columns.name = 'instrument'
        panel = raw.stack(dropna=False).to_frame('$close')
        obs = pd.DataFrame([(days[1], 'SH600001', 1), (days[3], 'SH600001', 1)],
                           columns=['date', 'code', 'value'])
        result = suspension_valuations(panel, 'ths_ifind', obs)
        pd.testing.assert_series_equal(result['$close'], panel['$close'])
        marks = research_close(result)['SH600001']
        self.assertEqual(marks.iloc[1], 10.)
        self.assertTrue(pd.isna(marks.iloc[2]))
        self.assertTrue(pd.isna(marks.iloc[3]))
        self.assertEqual(result.attrs['research_valuation']['marked_cells'], 1)
        self.assertNotIn('$valuation_close', panel)

    def test_confirmed_consecutive_suspensions_have_zero_mark_return(self):
        import factor_eval
        days = pd.date_range('2026-09-14', periods=3)
        index = pd.MultiIndex.from_product([days, ['SH600001']], names=['datetime','instrument'])
        panel = pd.DataFrame({'$close': [10., np.nan, np.nan]}, index=index)
        obs = pd.DataFrame([(d, 'SH600001', 1) for d in days[1:]], columns=['date','code','value'])
        result = suspension_valuations(panel, 'ths_ifind', obs)
        self.assertEqual(factor_eval.forward_returns(result, 2).iloc[0, 0], 0.)
        self.assertTrue(panel['$close'].iloc[1:].isna().all())

    def test_suspension_without_prior_price_never_backfills(self):
        days = pd.date_range('2026-09-14', periods=2)
        index = pd.MultiIndex.from_product([days, ['SH600001']], names=['datetime','instrument'])
        panel = pd.DataFrame({'$close': [np.nan, 10.]}, index=index)
        obs = pd.DataFrame([(days[0], 'SH600001', 1)], columns=['date','code','value'])
        result = suspension_valuations(panel, 'ths_ifind', obs)
        self.assertTrue(pd.isna(research_close(result).iloc[0, 0]))


if __name__ == '__main__':
    unittest.main()
