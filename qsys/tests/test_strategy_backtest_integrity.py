"""Independent strategy replay must not silently drop failed factors or selected stocks."""
import os, sys, tempfile, unittest, json
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch, MagicMock
TMP=tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR']=TMP.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
import strategy_backtest as sb
from test_factor_eval import _make_panel_and_vals

class Tests(unittest.TestCase):
    def test_invalid_parameters_rejected_before_data_access(self):
        with patch.object(sb.library, '_lconn', side_effect=AssertionError('must not query')):
            for value in (-1, 0, 1.5, True, float('nan'), '5'):
                self.assertFalse(sb.backtest_strategy('fixture', top_n=value)['ok'])
                self.assertFalse(sb.backtest_strategy('fixture', hold_days=value)['ok'])

    def test_invalid_factor_values_rejected(self):
        panel, vals = _make_panel_and_vals(n_days=40)
        for invalid in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(invalid=invalid):
                bad = vals.copy()
                bad[:] = invalid
                conn = MagicMock()
                conn.__enter__.return_value.execute.return_value.fetchone.return_value = (
                    json.dumps([{'name': 'a'}, {'name': 'b'}]), '等权', 'fixture')
                with ExitStack() as st:
                    st.enter_context(patch.object(sb.library, '_lconn', return_value=conn))
                    st.enter_context(patch.object(sb, 'all_pools', return_value={'fixture': list(range(35))}))
                    st.enter_context(patch.object(sb, 'get_last_trade_day', return_value='2023-03-01'))
                    st.enter_context(patch.object(sb, 'trade_day_offset', return_value='2023-01-01'))
                    st.enter_context(patch.object(sb.sig, 'get_panel_cached', return_value=panel))
                    st.enter_context(patch.object(sb.fe, 'get_factor_values', side_effect=[vals, bad]))
                    result = sb.backtest_strategy('fixture')
                self.assertFalse(result['ok'])
                self.assertIn('有限值', result['msg'])

    def test_missing_rebalance_scores_cannot_shift_calendar(self):
        panel, vals = _make_panel_and_vals(n_days=40)
        days = sorted(vals.index.get_level_values('datetime').unique())
        vals = vals[vals.index.get_level_values('datetime') != days[5]]
        conn = MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value = (
            json.dumps([{'name': 'a'}, {'name': 'b'}]), '等权', 'fixture')
        with ExitStack() as st:
            st.enter_context(patch.object(sb.library, '_lconn', return_value=conn))
            st.enter_context(patch.object(sb, 'all_pools', return_value={'fixture': list(range(35))}))
            st.enter_context(patch.object(sb, 'get_last_trade_day', return_value='2023-03-01'))
            st.enter_context(patch.object(sb, 'trade_day_offset', return_value='2023-01-01'))
            st.enter_context(patch.object(sb.sig, 'get_panel_cached', return_value=panel))
            st.enter_context(patch.object(sb.sig, 'scoring_norms', return_value=None))
            st.enter_context(patch.object(sb.fe, 'get_factor_values', side_effect=[vals, _make_panel_and_vals(n_days=40)[1]]))
            result = sb.backtest_strategy('fixture')
        self.assertFalse(result['ok'])
        self.assertIn('调仓日', result['msg'])

    def test_invalid_weights_and_directions_rejected(self):
        panel, vals = _make_panel_and_vals(n_days=40)
        configs = [{'weight': v} for v in (float('nan'), float('inf'), -1, None, 0)]
        configs += [{'direction': v} for v in (0, 2, float('nan'), None)]
        for cfg in configs:
            with self.subTest(cfg=cfg), ExitStack() as st:
                conn = MagicMock()
                conn.__enter__.return_value.execute.return_value.fetchone.return_value = (
                    json.dumps([{'name': 'a', **cfg}]), '等权', 'fixture')
                st.enter_context(patch.object(sb.library, '_lconn', return_value=conn))
                st.enter_context(patch.object(sb, 'all_pools', return_value={'fixture': list(range(35))}))
                st.enter_context(patch.object(sb, 'get_last_trade_day', return_value='2023-03-01'))
                st.enter_context(patch.object(sb, 'trade_day_offset', return_value='2023-01-01'))
                st.enter_context(patch.object(sb.sig, 'get_panel_cached', return_value=panel))
                st.enter_context(patch.object(sb.fe, 'get_factor_values', return_value=vals))
                result = sb.backtest_strategy('fixture')
                self.assertFalse(result['ok'])
                self.assertIn('权重', result['msg'])

    def test_failed_factor_invalidates_report(self):
        panel, vals = _make_panel_and_vals(n_days=40)
        conn=MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value=(
            json.dumps([{'name':'a'},{'name':'b'}]),'等权','fixture')
        with ExitStack() as st:
            st.enter_context(patch.object(sb.library,'_lconn',return_value=conn))
            st.enter_context(patch.object(sb,'all_pools',return_value={'fixture':list(range(35))}))
            st.enter_context(patch.object(sb,'get_last_trade_day',return_value='2023-03-01'))
            st.enter_context(patch.object(sb,'trade_day_offset',return_value='2023-01-01'))
            st.enter_context(patch.object(sb.sig,'get_panel_cached',return_value=panel))
            st.enter_context(patch.object(sb.fe,'get_factor_values',side_effect=[vals,RuntimeError('failed')]))
            result=sb.backtest_strategy('fixture')
        self.assertFalse(result['ok'])
        self.assertIn('部分因子',result['msg'])

if __name__=='__main__': unittest.main()
