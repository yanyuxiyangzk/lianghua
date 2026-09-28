import unittest
from datetime import datetime
from unittest.mock import patch
import pandas as pd
import factor_eval as fe
import strategy_progress as progress
from selection_policy import SELECTION_POLICY, rank_snapshot
from execution_gate import strategy_version
from test_factor_eval import _make_panel_and_vals


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.pack = dict(status='active', pool_name='p', factors=[dict(name='a', weight=1., direction=-1)])
        self.report = dict(ok=True, strategy_version=strategy_version(self.pack),
                           selection_policy=SELECTION_POLICY, weight_mode='frozen_strategy_snapshot',
                           research_passed=True, _created_at='2026-09-28 10:00:00',
                           eval_date=datetime.now().date().isoformat(), oos_windows=40,
                           oos_winrate=.6, sharpe=.8, avg_net_excess=.01, max_drawdown=-.1,
                           walk_forward={'passed': True}, regime_validation={'passed': True})

    def test_old_policy_or_definition_cannot_qualify(self):
        self.assertEqual(progress.research_reason(self.pack, self.report), '')
        self.assertTrue(progress.research_reason(self.pack, {**self.report, 'selection_policy': 'old'}))
        self.assertTrue(progress.research_reason({**self.pack, 'top_n': 5}, self.report))

    def test_calculation_success_is_not_research_pass(self):
        self.assertIn('未达标', progress.research_reason(self.pack, {**self.report, 'research_passed': False}))
        self.assertTrue(progress.research_reason(self.pack, {**self.report, 'weight_mode': 'adaptive'}))
        self.assertTrue(progress.research_reason(self.pack, {**self.report, 'sharpe': float('nan')}))
        self.assertTrue(progress.research_reason(self.pack, {**self.report, 'eval_date': '2000-01-01'}))

    def test_research_pass_does_not_grant_execution(self):
        rows = progress.progress_rows({'p': self.pack}, {'p': self.report}, {}, 'bull', '2026-09-28')
        self.assertEqual(rows[0]['自动买入'], '未放行')

    def test_bounded_review_skips_paused_and_cools_down_current_attempt(self):
        packs = {'a': self.pack, 'b': {**self.pack, 'status': 'paused'}, 'c': {**self.pack, 'status': 'degraded'}}
        self.assertEqual(progress.review_candidates(packs, {'a': self.report}, '2026-09-28', 2), ['c'])
        self.assertEqual(progress.review_candidates(packs, {}, '2026-09-28', 1), ['a'])
        self.assertIn('a', progress.review_candidates(packs, {'a': self.report}, '2026-10-06', 2))

    def test_version_change_bypasses_cooldown(self):
        self.assertEqual(progress.review_candidates({'a': {**self.pack, 'top_n': 5}},
                                                    {'a': self.report}, '2026-09-28'), ['a'])

    def test_fixed_research_never_reestimates_weights(self):
        panel, vals = _make_panel_and_vals(n_days=320, n_stocks=10)
        seen = []
        def ranker(weights, day):
            seen.append(dict(weights))
            codes = panel.index.get_level_values('instrument').unique()
            return pd.Series(range(len(codes)), index=codes, dtype=float).sort_values(ascending=False)
        with patch.object(fe, 'compute_weights', side_effect=AssertionError('must not reweight')):
            wf = fe.walk_forward({'a': vals}, panel, 'ICIR加权', 3, min_factors=1,
                                 fixed_weights={'a': (2., -1)}, ranker=ranker)
        self.assertFalse(wf.empty)
        self.assertEqual(seen[0], {'a': (2., -1)})
        self.assertTrue(all(x['a'][1] == -1 for x in seen))

    def test_shared_selector_filters_cannot_see_future_bars(self):
        panel, vals = _make_panel_and_vals(n_days=320, n_stocks=10)
        day = sorted(panel.index.get_level_values('datetime').unique())[270]
        def filters(codes, history, filters):
            self.assertLessEqual(history.index.get_level_values('datetime').max(), day)
            return codes
        codes = panel.index.get_level_values('instrument').unique()
        with patch('signals.apply_filters', side_effect=filters), \
             patch('signals.industry_cap_select', side_effect=lambda s, **kw: s), \
             patch('signals.scoring_norms', return_value={'a': 'zscore'}):
            ranking, count = rank_snapshot({'a': vals}, self.pack['factors'], panel, codes, day)
        self.assertFalse(ranking.empty)

    def test_volume_only_filter_does_not_compute_unrequested_indicators(self):
        import signals
        index = pd.MultiIndex.from_product([[pd.Timestamp('2026-09-24')], ['a','b','c','d']],
                                          names=['datetime', 'instrument'])
        panel = pd.DataFrame({'$volume': [10., 0., float('nan'), -1.]}, index=index)
        with patch.object(signals, 'compute_tech', side_effect=AssertionError('unrequested computation')):
            self.assertEqual(signals.apply_filters(['d','a','c','b','missing'], panel, ['tradable']), ['a'])

    def test_observation_task_persists_failure_reason_and_end_event(self):
        import json, tempfile, threading, scheduler, library
        from pathlib import Path
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'last.json'
            manager = scheduler.SchedulerManager.__new__(scheduler.SchedulerManager)
            manager._owner = False; manager._running = {}; manager._last_lock = threading.Lock()
            jobs = {'pool_scan': {'name': 'fixture', 'default': {'params': {}},
                                 'func': lambda: '仅研究、无自动买入资格'}}
            with patch.object(scheduler, 'JOBS', jobs), patch.object(scheduler, 'SCHED_LAST_FILE', path), \
                 patch.object(library, 'write_runtime_log') as log, patch.object(library, '_lconn', MagicMock()):
                manager._run('pool_scan', manual=True)
            self.assertFalse(json.loads(path.read_text())['pool_scan']['ok'])
            self.assertEqual(log.call_args.kwargs['error_type'], 'SelectionNotReady')
            self.assertFalse(manager._running)


if __name__ == '__main__':
    unittest.main()
