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
