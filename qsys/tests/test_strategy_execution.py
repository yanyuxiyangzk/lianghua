import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock
import pandas as pd
import strategy_backtest as sb
from test_factor_eval import _make_panel_and_vals


class IntegrationTests(unittest.TestCase):
    def run_fixture(self, reverse=False, missing=False):
        panel, vals = _make_panel_and_vals(n_days=40)
        codes = list(panel.index.get_level_values('instrument').unique())
        days = sorted(panel.index.get_level_values('datetime').unique())
        panel['$open'] = 10.; panel['$close'] = 10.
        if missing:
            panel.loc[(days[1], codes[0]), '$open'] = float('nan')
        if reverse: panel = panel.swaplevel()
        conn = MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value = (
            json.dumps([{'name':'a'}]), '等权', 'fixture')
        with ExitStack() as st:
            for obj, name, value in [(sb.library,'_lconn',conn), (sb,'all_pools',{'fixture':codes}),
                                     (sb,'get_last_trade_day',str(days[-1])[:10]), (sb,'trade_day_offset',str(days[0])[:10]),
                                     (sb.sig,'get_panel_cached',panel), (sb.sig,'scoring_norms',None),
                                     (sb.fe,'get_factor_values',vals),
                                     (sb.fe,'_score_at',pd.Series([2.,1.],index=codes[:2]))]:
                st.enter_context(patch.object(obj,name,return_value=value))
            st.enter_context(patch.object(sb.fe,'forward_returns',side_effect=AssertionError('execution must not use future returns')))
            return sb.backtest_strategy('fixture',top_n=2,mode='execution')

    def test_execution_uses_ledger_metrics_and_serializes(self):
        for reverse in [False,True]:
            r = self.run_fixture(reverse)
            self.assertTrue(r['ok'],r)
            self.assertEqual(r['execution']['fills'][0]['date'], '2023-01-03')
            self.assertAlmostEqual(r['total_return'],r['equity']/200000-1)
            self.assertAlmostEqual(r['nav'][-1],r['equity']/200000)
            self.assertLess(r['total_return'],0)
            self.assertNotIn('excess_ann_return',r)
            json.dumps(r,allow_nan=False)

    def test_missing_selected_open_invalidates_whole_report(self):
        r = self.run_fixture(missing=True)
        self.assertFalse(r['ok'])
        self.assertIn('成交',r['msg'])
