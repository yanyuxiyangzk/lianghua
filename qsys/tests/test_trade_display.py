"""Run standalone: python tests/test_trade_display.py."""
import sys
import unittest
from pathlib import Path

import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trade_display import with_signal_columns


class TradeDisplayTests(unittest.TestCase):
    def test_old_orders_and_fills_keep_records(self):
        for value_column in ("status", "amount"):
            old = pd.DataFrame({"code": ["SZ002709"], "source": ["ai"],
                                value_column: [1]}, index=[42])
            result = with_signal_columns(old)
            result[["code", "source", "signal_source", "strategy_name", value_column]]
            pd.testing.assert_frame_equal(result[old.columns], old)
            self.assertTrue(result["signal_source"].isna().all())
            self.assertTrue(result["strategy_name"].isna().all())
            self.assertNotIn("signal_source", old)
            labels = result["signal_source"].map({"satellite_scan": "卫星轨"}).fillna(
                result["signal_source"].fillna("历史未标记"))
            self.assertEqual(labels.iloc[0], "历史未标记")

    def test_new_sources_preserved(self):
        new = pd.DataFrame({"signal_source": ["satellite_scan", None],
                            "strategy_name": ["事件卫星", None]})
        pd.testing.assert_frame_equal(with_signal_columns(new), new)

    def test_partial_and_empty_formats(self):
        partial = pd.DataFrame({"signal_source": ["satellite_scan"]})
        result = with_signal_columns(partial)
        self.assertEqual(result.iloc[0]["signal_source"], "satellite_scan")
        self.assertTrue(pd.isna(result.iloc[0]["strategy_name"]))
        empty = with_signal_columns(pd.DataFrame())
        self.assertTrue(empty.empty)
        self.assertEqual(list(empty.columns), ["signal_source", "strategy_name"])


if __name__ == "__main__":
    unittest.main()
