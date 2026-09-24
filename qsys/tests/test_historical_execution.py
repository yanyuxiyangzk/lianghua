import unittest
import pandas as pd
from historical_execution import simulate, performance


class ExecutionTests(unittest.TestCase):
    def prices(self):
        idx = pd.MultiIndex.from_product([['2026-01-02','2026-01-05','2026-01-06'],['A']], names=['date','code'])
        return pd.DataFrame({'open':[10.,11.,12.], 'close':[11.,12.,11.]}, index=idx)

    def signal(self, target=100, code='A', day='2026-01-02'):
        return pd.DataFrame([dict(date=day, code=code, target_shares=target)])

    def test_next_open_cash_and_daily_metrics(self):
        s = pd.concat([self.signal(), self.signal(0, day='2026-01-05')])
        r = simulate(s, self.prices(), initial_cash=2000)
        self.assertEqual(r['fills'].date.tolist(), ['2026-01-05','2026-01-06'])
        self.assertEqual(r['equity'].cash.tolist(), [2000., 895., 2089.4])
        self.assertAlmostEqual(r['cash'], 2000 + r['cashflows'].amount.sum())
        self.assertAlmostEqual(performance(r)['total_return'], 2089.4 / 2000 - 1)
        self.assertEqual(performance(r)['elapsed_trading_days'], 2)

    def test_missing_quote_date_and_last_signal_fail(self):
        for signal in [self.signal(code='B'), self.signal(day='2026-01-03'), self.signal(day='2026-01-06')]:
            with self.assertRaises(ValueError):
                simulate(signal, self.prices())
        with self.assertRaisesRegex(ValueError, '估值'):
            p = self.prices(); p.loc[('2026-01-06','A'), 'close'] = float('nan')
            simulate(self.signal(), p)

    def test_non_integer_negative_and_nonfinite_rejected(self):
        for n in [-100, 50, 100.5, float('nan'), float('inf')]:
            with self.assertRaises(ValueError): simulate(self.signal(n), self.prices())

    def test_cash_shortage_is_visible_rejection(self):
        r = simulate(self.signal(), self.prices(), initial_cash=500)
        self.assertEqual(r['orders'].iloc[0].reason, 'insufficient_cash')
        self.assertEqual(r['cash'], 500)
        self.assertTrue(r['fills'].empty)

    def test_limits_suspension_and_slippage(self):
        for col, val, reason in [('limit_up',11.,'limit_up'), ('volume',0.,'suspended_or_zero_volume'), ('suspended',1,'suspended_or_zero_volume')]:
            p = self.prices(); p[col] = val
            r = simulate(self.signal(), p)
            self.assertEqual(r['orders'].iloc[0].reason, reason)
        p = self.prices(); p['limit_down'] = 12.
        s = pd.concat([self.signal(), self.signal(0, day='2026-01-05')])
        r = simulate(s, p)
        self.assertEqual(r['orders'].iloc[-1].reason, 'limit_down')
        self.assertEqual(r['positions'], {'A': 100})
        r = simulate(self.signal(), self.prices(), slippage=.01)
        self.assertAlmostEqual(r['fills'].iloc[0].price, 11.11)

    def test_weight_rebalance_sells_first_and_reserves_fees(self):
        a = self.prices(); b = a.copy()
        b.index = pd.MultiIndex.from_product([a.index.get_level_values('date'), ['B']], names=['date','code'])
        p = pd.concat([a,b]); p['open'] = 10.; p['close'] = 10.
        signals = pd.DataFrame([dict(date='2026-01-02',code='A',target_weight=1.), dict(date='2026-01-05',code='B',target_weight=1.)])
        r = simulate(signals, p, initial_cash=2000)
        self.assertEqual(r['fills'].side.tolist(), ['buy','sell','buy'])
        self.assertEqual(r['positions'], {'B':100})
        self.assertGreaterEqual(r['cash'], 0)
        self.assertTrue((r['fills'].date > r['fills'].signal_date).all())

    def test_weight_quantities_do_not_use_future_open(self):
        s = pd.DataFrame([dict(date='2026-01-02',code='A',target_weight=1.)])
        p = self.prices(); p['close'] = 10.
        r = simulate(s, p, initial_cash=2000)
        p.loc[('2026-01-05','A'),'open'] = 50.
        gap = simulate(s, p, initial_cash=2000)
        self.assertEqual(r['orders'].shares.tolist(), gap['orders'].shares.tolist())
        self.assertEqual(gap['orders'].iloc[0].reason, 'insufficient_cash')

    def test_unknown_volume_rejects_order_and_marks_report_incomplete(self):
        p = self.prices()
        p['volume'] = 1000.
        p.loc[('2026-01-05', 'A'), 'volume'] = float('nan')
        r = simulate(self.signal(), p, initial_cash=2000)
        self.assertTrue(r['fills'].empty)
        self.assertEqual(r['cash'], 2000)
        self.assertEqual(r['orders'].iloc[0].reason, 'unknown_volume')
        self.assertEqual(r['data_quality_status'], 'incomplete')
        self.assertEqual(r['data_issues'][0]['date'], '2026-01-05')

        # Unknown volume must also prevent sells and preserve the holding.
        p['volume'] = 1000.
        p.loc[('2026-01-06', 'A'), 'volume'] = float('nan')
        s = pd.concat([self.signal(), self.signal(0, day='2026-01-05')])
        r = simulate(s, p, initial_cash=2000)
        self.assertEqual(r['positions'], {'A': 100})
        self.assertEqual(len(r['fills']), 1)
        self.assertEqual(r['orders'].iloc[-1].reason, 'unknown_volume')

    def test_named_index_order_and_bad_metadata(self):
        r = simulate(self.signal(), self.prices().swaplevel())
        self.assertEqual(len(r['fills']), 1)
        p = self.prices(); p['limit_up'] = float('nan')
        with self.assertRaises(ValueError): simulate(self.signal(), p)
        with self.assertRaises(ValueError): simulate(self.signal(), pd.concat([p,p]))

if __name__ == '__main__': unittest.main()
