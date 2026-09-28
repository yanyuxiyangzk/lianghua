"""Autonomous reduction regression tests, always on isolated simulated accounts."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import patch

import test_account_controls as base
broker, experience, controls = base.broker, base.experience, base.controls


class Tests(unittest.TestCase):
    count_fills = base.Tests.count_fills

    def setUp(self):
        base.Tests.setUp(self)
        p = patch.object(controls,'refresh_risk',return_value='fixture')
        self.refresh = p.start()
        self.addCleanup(p.stop)
        controls.set_automatic(True,'test fixture authority')

    def test_automatic_sells_without_manual_confirmation_and_stops_at_limit(self):
        result=controls.run_automatic()
        self.assertEqual(result['status'],'processed')
        self.assertEqual(self.count_fills(),1)
        self.assertEqual(controls.latest_plan()['snapshot']['execution_mode'],'automatic')
        self.assertIn('自动账户降仓',broker.list_fills().iloc[0]['decision_reason'])
        self.assertEqual(controls.run_automatic()['status'],'within_limits')
        self.assertEqual(self.count_fills(),1)
        self.refresh.assert_called_with(prefix='自动核验')

    def test_old_review_plan_is_recalculated_not_blindly_approved(self):
        old=controls.generate_plan()
        with self.assertRaisesRegex(ValueError,'重新生成'):controls.submit_plan(old,automatic=True)
        controls.run_automatic()
        self.assertNotEqual(controls.latest_plan()['id'],old)
        self.assertEqual(self.count_fills(),1)

    def test_disabled_mode_cannot_submit_automatic_plan(self):
        identifier=controls.generate_plan(automatic=True)
        controls.set_automatic(False)
        self.assertEqual(controls.run_automatic()['status'],'paused')
        with self.assertRaisesRegex(ValueError,'已暂停'):controls.submit_plan(identifier,automatic=True)
        self.assertEqual(self.count_fills(),0)

    def test_outside_market_never_refreshes_or_trades(self):
        with patch.object(broker,'_market_open',return_value=False):
            self.assertEqual(controls.run_automatic()['status'],'waiting_market')
        self.refresh.assert_not_called()
        self.assertEqual(self.count_fills(),0)

    def test_stale_data_or_missing_risk_target_waits_without_orders(self):
        with patch.object(broker,'_quote_fresh',return_value=False):
            self.assertEqual(controls.run_automatic()['status'],'waiting_data')
        experience._RISK_FLAG.write_text('{}')
        self.assertEqual(controls.run_automatic()['status'],'waiting_data')
        self.assertEqual(self.count_fills(),0)

    def test_refresh_failure_persists_reason_and_retries_next_cycle(self):
        self.refresh.side_effect=ValueError('行情暂不可用')
        result=controls.run_automatic()
        self.assertEqual(result['status'],'retry')
        self.assertIn('行情暂不可用',controls.automatic_state()['message'])
        self.refresh.side_effect=None
        self.assertEqual(controls.run_automatic()['status'],'processed')
        self.assertEqual(self.count_fills(),1)

    def test_pending_limit_down_order_is_not_duplicated_then_fills(self):
        quotes=lambda codes:{cd:(10.,10.,10.,11.,10.,'2026-09-28 10:00:00') for cd in codes}
        with patch.object(broker,'_latest_prices',side_effect=quotes):
            controls.run_automatic()
            self.assertEqual(controls.latest_plan()['items'][0]['status'],'等待成交')
            self.assertEqual(controls.run_automatic()['status'],'waiting_orders')
            self.assertEqual(len(broker.list_orders()),1)
            self.assertEqual(self.count_fills(),0)
        self.assertEqual(controls.run_automatic()['status'],'within_limits')
        self.assertEqual(self.count_fills(),1)
        self.assertEqual(len(broker.list_orders()),1)

    def test_pause_cancels_only_unfilled_automatic_sells(self):
        quotes=lambda codes:{cd:(10.,10.,10.,11.,10.,'2026-09-28 10:00:00') for cd in codes}
        with patch.object(broker,'_latest_prices',side_effect=quotes):controls.run_automatic()
        self.assertIn('已挂单',broker.place_order('SZ002709','sell',10.5,100))
        controls.set_automatic(False)
        orders=broker.list_orders().sort_values('id')
        self.assertEqual(list(orders['status']),['已撤','已报'])
        self.assertEqual(controls.run_automatic()['status'],'paused')
        self.assertEqual(self.count_fills(),0)

    def test_concurrent_workers_do_not_duplicate_sales(self):
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:controls.run_automatic(),range(2)))
        self.assertTrue(all(r['status'] in ('busy','processed','within_limits') for r in results),results)
        self.assertEqual(self.count_fills(),1)

    def test_failure_cooldown_then_automatic_retry(self):
        with patch.object(broker,'place_order',side_effect=RuntimeError('temporary failure')):
            self.assertEqual(controls.run_automatic()['status'],'retry')
        self.assertEqual(controls.run_automatic()['status'],'waiting_retry')
        self.assertEqual(self.count_fills(),0)
        class Later(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,9,28,10,6)
        with patch.object(broker,'datetime',Later):
            self.assertEqual(controls.run_automatic()['status'],'processed')
        self.assertEqual(self.count_fills(),1)

    def test_t_plus_one_waits_without_churning_plans_and_resumes_next_day(self):
        with broker._conn() as c:c.execute('UPDATE broker_positions SET sellable=0')
        self.assertEqual(controls.run_automatic()['status'],'blocked')
        identifier=controls.latest_plan()['id']
        self.assertEqual(controls.run_automatic()['status'],'waiting_retry')
        self.assertEqual(controls.latest_plan()['id'],identifier)
        class Tomorrow(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,9,29,10)
        with patch.object(broker,'datetime',Tomorrow):
            experience._RISK_FLAG.write_text(json.dumps(dict(date='2026-09-29',halt=False,level='normal',target_position_ratio=.8)))
            self.assertEqual(controls.run_automatic()['status'],'processed')
        self.assertEqual(self.count_fills(),1)

    def test_latest_risk_target_is_used_each_cycle(self):
        def refresh(prefix):
            experience._RISK_FLAG.write_text(json.dumps(dict(date=broker._today(),halt=True,level='red',target_position_ratio=.3)))
        self.refresh.side_effect=refresh
        controls.run_automatic()
        self.assertEqual(controls.latest_plan()['snapshot']['target'],.3)

    def test_scoped_matching_does_not_fill_unrelated_order(self):
        self.assertIn('已挂单',broker.place_order('SZ002709','sell',10.5,100))
        with patch.object(broker,'_latest_prices',side_effect=lambda codes:{cd:(10.6,10.,10.,11.,9.) for cd in codes}):
            self.assertEqual(broker.fill_pending_orders(order_ids=[]),0)
        self.assertEqual(self.count_fills(),0)


if __name__=='__main__':unittest.main()
