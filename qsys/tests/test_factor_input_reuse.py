import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import signals
from test_factor_eval import _make_panel_and_vals


class InputTests(unittest.TestCase):
    def test_python_factor_reuses_panel_and_separates_lookbacks(self):
        panel, _ = _make_panel_and_vals(n_days=12)
        code = "import pandas as pd\ndf=pd.read_hdf('daily_pv.h5',key='data')\nassert df.index.names==['datetime','instrument']\nassert (df['$factor']==1).all()\ndf[['$close']].to_hdf('result.h5',key='data')\n"
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(signals, 'CACHE_DIR', Path(tmp)), \
             patch.object(signals, 'get_panel_cached', return_value=panel) as cached, \
             patch.object(signals, 'build_daily_pv_h5', side_effect=AssertionError('duplicate fetch')):
            first = signals.run_factor_code(code, 'fixture', ['x'], '2026-09-24', 20, source='ths_ifind')
            second = signals.run_factor_code(code, 'fixture', ['x'], '2026-09-24', 30, source='ths_ifind')
            self.assertEqual(cached.call_count, 2)
            self.assertEqual(len(list(Path(tmp).glob('evo_*.parquet'))), 2)
            self.assertEqual(len(first), len(panel))
            self.assertEqual(len(second), len(panel))
