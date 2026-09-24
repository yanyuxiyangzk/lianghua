"""Run standalone: python tests/test_buy_atomicity.py (temporary databases only)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime as RealDatetime
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

_TMP = tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR'] = _TMP.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import broker
import experience
import execution_gate
_REAL_RISK_HALT = experience.risk_halt_today


class TradingTime(RealDatetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 23, 10, 0)


class AtomicBuyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'experience.db'
        patches = [patch.object(broker, 'DB_PATH', path),
                   patch.object(execution_gate, 'position_rejection', lambda *args: ''),
                   patch.object(experience, 'DB_PATH', path),
                   patch.object(broker, 'datetime', TradingTime),
                   patch.object(broker, '_quote_fresh', lambda _: True),
                   patch.object(experience, 'satellite_halt_today', lambda _: (False, '')),
                   patch.object(experience, '_RISK_FLAG', Path(self.tmp.name) / 'risk.json'),
                   patch.object(broker, 'get_name', lambda code: code),
                   patch.object(broker, '_latest_prices', lambda codes: {c: (10., 10., 10., 11., 9.) for c in codes}),
                   patch.object(experience, 'risk_halt_today', lambda _: (False, ''))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with experience._conn():
            pass
        broker._init_account()
        broker._settle_today()
        self.pid = self.pending('SZ002709')

    def pending(self, code, source='sched_pool_scan'):
        with experience._conn() as c:
            return c.execute("INSERT INTO positions(code,buy_date,source,status,limit_price) VALUES (?, '2026-09-23',?,'pending',11)", (code, source)).lastrowid

    def counts(self):
        with broker._conn() as c:
            return tuple(c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                         for t in ['broker_orders', 'broker_fills', 'broker_positions'])

    def test_repeat_and_cross_source(self):
        self.assertIn('已成交', broker.buy_position(self.pid, 100))
        self.assertIn('已处理', broker.buy_position(self.pid, 100))
        other = self.pending('SZ002709', 'satellite_scan')
        self.assertNotIn('已成交', broker.buy_position(other, 100))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_bookkeeping_failure_rolls_back_money_and_fill(self):
        with broker._conn() as c:
            c.execute("CREATE TRIGGER fail_open BEFORE UPDATE OF status ON positions WHEN NEW.status='open' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(Exception):
            broker.buy_position(self.pid, 100)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(broker._get_cash(), broker.INIT_CASH)
        with broker._conn() as c:
            self.assertEqual(c.execute('SELECT status FROM positions WHERE id=?', (self.pid,)).fetchone()[0], 'pending')
            c.execute('DROP TRIGGER fail_open')
        self.assertIn('已成交', broker.buy_position(self.pid, 100))

    def test_later_failure_does_not_undo_first_position(self):
        self.assertIn('已成交', broker.buy_position(self.pid, 100))
        other = self.pending('SH600000')
        with broker._conn() as c:
            c.execute(f"CREATE TRIGGER fail_second BEFORE UPDATE ON positions WHEN NEW.id={other} BEGIN SELECT RAISE(ABORT,'second failed'); END")
        with self.assertRaises(Exception):
            broker.buy_position(other, 100)
        with broker._conn() as c:
            self.assertEqual(c.execute('SELECT status FROM positions WHERE id=?', (self.pid,)).fetchone()[0], 'open')
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_concurrent_retry(self):
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: broker.buy_position(self.pid, 100), range(2)))
        self.assertEqual(sum('已成交' in r for r in results), 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_concentration_limit(self):
        self.assertIn('15%', broker.buy_position(self.pid, 3100))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_eight_positions_limit(self):
        with broker._conn() as c:
            for i in range(8):
                c.execute("INSERT INTO broker_positions(code,source,shares,cost) VALUES (?,'manual',100,10)", (f'SH{i:06d}',))
        self.assertIn('8只', broker.buy_position(self.pid, 100))

    def test_halt_blocks_existing_pending(self):
        with patch.object(experience, 'risk_halt_today', lambda _: (True, 'halt')):
            self.assertIn('风控拦截', broker.buy_position(self.pid, 100))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_fill_is_idempotent(self):
        broker.buy_position(self.pid, 100)
        cash = broker._get_cash()
        with broker._conn() as c:
            oid = c.execute('SELECT id FROM broker_orders').fetchone()[0]
            broker._fill(c, oid, 10)
        self.assertEqual(broker._get_cash(), cash)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_midday_does_not_buy(self):
        class Lunch(TradingTime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 23, 12, 30)
        with patch.object(broker, 'datetime', Lunch):
            self.assertIn('非连续交易时段', broker.buy_position(self.pid, 100))
        self.assertEqual(self.counts(), (0, 0, 0))

    def make_sellable(self, shares=200):
        self.assertIn('已成交', broker.buy_position(self.pid, shares))
        with broker._conn() as c:
            c.execute("UPDATE positions SET buy_date='2026-09-22' WHERE id=?", (self.pid,))
            c.execute('UPDATE broker_positions SET sellable=shares,today_bought=0')

    def test_partial_sale_retains_cost_basis(self):
        self.make_sellable()
        with patch.object(broker, '_latest_prices', lambda codes: {c: (9.,10.,10.,11.,8.) for c in codes}):
            self.assertIn('已成交', experience.manual_sell(self.pid, 100))
        with broker._conn() as c:
            self.assertEqual(c.execute('SELECT status,shares,buy_amount FROM positions WHERE id=?', (self.pid,)).fetchone(), ('open',100,1000.))
            self.assertEqual(c.execute('SELECT shares FROM broker_positions').fetchone()[0],100)

    def test_sale_failure_rolls_back(self):
        self.make_sellable()
        cash = broker._get_cash()
        with broker._conn() as c:
            c.execute("CREATE TRIGGER fail_sale BEFORE UPDATE ON positions WHEN NEW.status='closed' BEGIN SELECT RAISE(ABORT,'failed'); END")
        with self.assertRaises(Exception):
            broker.sell_position(self.pid,200)
        self.assertEqual(broker._get_cash(),cash)
        self.assertEqual(self.counts(), (1,1,1))

    def test_pending_partial_sale_settles_atomically(self):
        self.make_sellable()
        self.assertIn('已挂单',broker.sell_position(self.pid,100,10.5))
        self.assertNotIn('已成交',broker.sell_position(self.pid,100))
        with patch.object(broker, '_latest_prices', lambda codes: {c: (10.6,10.,10.,11.,9.) for c in codes}):
            self.assertEqual(broker.fill_pending_orders(),1)
            self.assertEqual(broker.fill_pending_orders(),0)
        with broker._conn() as c:
            self.assertEqual(c.execute('SELECT status,shares FROM positions WHERE id=?',(self.pid,)).fetchone(),('open',100))

    def test_sell_reservations_prevent_oversell(self):
        with broker._conn() as c:
            c.execute("INSERT INTO broker_positions(code,source,shares,sellable,today_bought,cost) VALUES ('SH600000','manual',100,100,0,10)")
        self.assertIn('已挂单',broker.place_order('SH600000','sell',10.5,100))
        self.assertIn('不足',broker.place_order('SH600000','sell',None,100))
        with patch.object(broker, '_latest_prices', lambda codes: {c: (10.6,10.,10.,11.,9.) for c in codes}):
            with ThreadPoolExecutor(2) as pool:
                self.assertEqual(sum(pool.map(lambda _: broker.fill_pending_orders(),range(2))),1)
        self.assertEqual(self.counts(),(1,1,0))

    def test_cash_reservation_and_release(self):
        # Bypass only exposure limits to isolate the shared manual/AI cash reservation.
        with broker._conn() as c:
            c.execute("UPDATE broker_account SET value='2000' WHERE key='cash'")
        with patch.object(broker,'_buy_rejection',lambda *a,**kw:''):
            self.assertIn('已挂单',broker.place_order('SH600000','buy',9.5,100))
            self.assertIn('不足',broker.place_order('SH600001','buy',9.5,200))
            self.assertEqual(broker.get_account()['可用资金'],1045)
            with broker._conn() as c:
                oid=c.execute('SELECT id FROM broker_orders').fetchone()[0]
            broker.cancel_order(oid)
            self.assertEqual(broker.get_account()['可用资金'],2000)

    def test_halt_rechecked_when_pending_fills(self):
        self.assertIn('已挂单',broker.place_order('SH600000','buy',9.5,100))
        with patch.object(broker,'_latest_prices',lambda codes:{c:(9.,10.,10.,11.,8.) for c in codes}), patch.object(experience,'risk_halt_today',lambda _:(True,'halt')):
            self.assertEqual(broker.fill_pending_orders(),0)
        self.assertEqual(self.counts(),(1,0,0))

    def test_expired_orders_never_fill_next_day(self):
        broker.place_order('SH600000','buy',9.5,100)
        with broker._conn() as c:
            c.execute("UPDATE broker_orders SET date='2026-09-22'")
        with patch.object(broker,'_latest_prices',lambda codes:{c:(9.,10.,10.,11.,8.) for c in codes}):
            self.assertEqual(broker.fill_pending_orders(),0)
        with broker._conn() as c:
            self.assertEqual(c.execute('SELECT status FROM broker_orders').fetchone()[0],'已撤')

    def test_bad_inputs_and_stale_quotes(self):
        for args in [('buy',float('nan'),100),('buy',10,100.5),('oops',10,100)]:
            self.assertNotIn('已成交',broker.place_order('SH600000',*args))
        with patch.object(broker,'_quote_fresh',lambda _:False):
            self.assertIn('过期',broker.place_order('SH600000','buy',None,100))
        self.assertEqual(self.counts(),(0,0,0))

    def test_reconcile_partial_mismatch_reports_error(self):
        self.make_sellable()
        with broker._conn() as c:
            c.execute('UPDATE broker_positions SET shares=100,sellable=100')
        self.assertIn('账本差异',experience.position_reconcile('2026-09-23'))

    def test_cancelled_partial_sale_reopens_without_reducing_shares(self):
        self.make_sellable()
        broker.sell_position(self.pid,100,10.5)
        with broker._conn() as c:
            oid=c.execute("SELECT id FROM broker_orders WHERE side='sell'").fetchone()[0]
        broker.cancel_order(oid)
        # 必须立即恢复，不依赖后续对账。
        with broker._conn() as c:
            self.assertEqual(c.execute('SELECT status,shares FROM positions WHERE id=?',(self.pid,)).fetchone(),('open',200))

    def test_expired_sell_reopens_immediately(self):
        self.make_sellable()
        broker.sell_position(self.pid, 100, 10.5)
        with broker._conn() as c:
            c.execute("UPDATE broker_orders SET date='2026-09-22' WHERE side='sell'")
        self.assertEqual(broker.expire_day_orders(), 1)
        with broker._conn() as c:
            self.assertEqual(c.execute('SELECT status,shares,sell_order_id FROM positions WHERE id=?',
                                      (self.pid,)).fetchone(), ('open', 200, None))

    def test_stale_existing_holding_blocks_new_buy(self):
        self.make_sellable()
        pid = self.pending('SH600000')
        with patch.object(broker, '_quote_fresh', lambda code: code != 'SZ002709'):
            self.assertIn('持仓行情过期', broker.buy_position(pid,100))
        self.assertEqual(self.counts(), (1,1,1))

    def test_limit_down_sell_uses_valid_limit_price(self):
        self.make_sellable()
        with patch.object(broker, '_latest_prices', lambda codes:{c:(9.,10.,10.,11.,9.) for c in codes}):
            self.assertIn('已挂单',broker.sell_position(self.pid,100))
        with broker._conn() as c:
            self.assertEqual(c.execute("SELECT price FROM broker_orders WHERE side='sell'").fetchone()[0],9.)

    def test_order_cannot_borrow_other_stock_task(self):
        result = broker.place_order('SH600000','buy',None,100,source='ai',_position_id=self.pid)
        self.assertIn('不一致',result)
        self.assertEqual(self.counts(),(0,0,0))

    def test_manual_buy_cannot_bypass_concentration(self):
        self.assertIn('15%',broker.place_order('SH600000','buy',None,3100))

    def test_old_satellite_entry_is_closed(self):
        self.assertIn('停用',broker.place_order('SH600000','buy',None,100,source='satellite'))

    def test_satellite_rules_checked_inside_transaction(self):
        with broker._conn() as c:
            c.execute("UPDATE positions SET source='satellite_scan' WHERE id=?",(self.pid,))
        self.assertIn('5%',broker.buy_position(self.pid,1100))

    def test_sell_still_works_during_halt(self):
        self.make_sellable()
        with patch.object(experience,'risk_halt_today',lambda _:(True,'halt')):
            self.assertIn('已成交',broker.sell_position(self.pid,200))

    def test_stop_loss_driver_uses_atomic_sale(self):
        self.make_sellable()
        quotes=lambda codes:{c:(8.,10.,10.,11.,7.) for c in codes}
        with patch.object(experience,'_latest_prices',quotes), patch.object(broker,'_latest_prices',quotes), patch.object(experience,'_latest_price_times',lambda codes:{c:'2026-09-23 10:00:00' for c in codes}), patch.object(experience,'_quote_is_fresh',lambda _:True), patch.object(experience.sig,'get_panel_cached',side_effect=RuntimeError('unavailable')):
            self.assertIn('平仓 1 笔',experience.position_close_check('2026-09-23'))
        with broker._conn() as c:
            self.assertEqual(c.execute('SELECT status FROM positions WHERE id=?',(self.pid,)).fetchone()[0],'closed')


    def test_risk_file_missing_stale_corrupt_fails_closed(self):
        import json
        path = experience._RISK_FLAG
        self.assertTrue(_REAL_RISK_HALT('2026-09-23')[0])
        for content in ['bad json', json.dumps({'date':'2026-09-22','halt':False,'level':'normal'}), json.dumps({'date':'2026-09-23','halt':'false','level':'normal'})]:
            path.write_text(content)
            self.assertTrue(_REAL_RISK_HALT('2026-09-23')[0])
        path.write_text(json.dumps({'date':'2026-09-23','halt':False,'level':'normal'}))
        self.assertFalse(_REAL_RISK_HALT('2026-09-23')[0])

    def test_quote_timestamp_validation(self):
        # Call the real timestamp validator, which the fixture normally mocks.
        import importlib.util
        spec = importlib.util.spec_from_file_location('broker_time_test', broker.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module,'datetime',TradingTime):
            for ts,expected in [('2026-09-23 09:59:30',True),('2026-09-22 10:00:00',False),('2026-09-23 09:00:00',False),('2026-09-23 10:10:00',False)]:
                with patch.object(experience,'_latest_price_times',lambda codes:{c:ts for c in codes}):
                    self.assertEqual(module._quote_fresh('SZ002709'),expected)

    def test_reconcile_missing_lot_preserves_aggregate_cost(self):
        self.make_sellable()
        with broker._conn() as c:
            c.execute("UPDATE broker_positions SET shares=300,cost=11,last_buy_date='2026-09-23'")
        self.assertIn('补记',experience.position_reconcile('2026-09-23'))
        with broker._conn() as c:
            self.assertAlmostEqual(c.execute("SELECT SUM(shares*buy_price) FROM positions WHERE status='open'").fetchone()[0],3300.)
        self.assertIn('对账一致',experience.position_reconcile('2026-09-23'))

    def test_account_target_counts_pending_orders(self):
        with patch.object(experience,'get_account_risk_config',lambda:{'normal_target':.02}):
            self.assertIn('已挂单',broker.place_order('SH600000','buy',9.5,300))
            self.assertIn('总仓位',broker.place_order('SH600001','buy',9.5,200))

    def test_concurrent_sales_only_execute_once(self):
        self.make_sellable()
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:broker.sell_position(self.pid,200),range(2)))
        self.assertEqual(sum('已成交' in x for x in results),1)
        self.assertEqual(self.counts(),(2,2,0))


    def test_legacy_partial_sale_reconciliation(self):
        self.make_sellable()
        broker.sell_position(self.pid,100,10.5)
        with broker._conn() as c:
            c.execute("UPDATE broker_orders SET position_id=NULL WHERE side='sell'")
        with patch.object(broker,'_latest_prices',lambda codes:{c:(10.6,10.,10.,11.,9.) for c in codes}):
            self.assertEqual(broker.fill_pending_orders(),1)
        experience.position_reconcile('2026-09-23')
        with broker._conn() as c:
            self.assertEqual(c.execute('SELECT status,shares,buy_amount FROM positions WHERE id=?',(self.pid,)).fetchone(),('open',100,1000.))


    def test_satellite_advice_uses_shared_cash_and_retains_source(self):
        with broker._conn() as c:
            c.execute("UPDATE positions SET source='satellite_scan',pack_name='事件卫星测试' WHERE id=?",(self.pid,))
        self.assertIn('已成交',broker.buy_position(self.pid,100))
        with broker._conn() as c:
            self.assertEqual(float(c.execute("SELECT value FROM broker_account WHERE key='cash'").fetchone()[0]),199000-5)
            self.assertIsNone(c.execute("SELECT value FROM broker_account WHERE key='cash_satellite'").fetchone())
            self.assertEqual(c.execute('SELECT source FROM broker_positions').fetchone()[0],'ai')
            self.assertEqual(c.execute('SELECT source,status FROM positions WHERE id=?',(self.pid,)).fetchone(),('satellite_scan','open'))
            c.execute("UPDATE positions SET buy_date='2026-09-22' WHERE id=?",(self.pid,))
            c.execute('UPDATE broker_positions SET sellable=shares,today_bought=0')
        self.assertIn('已成交',broker.sell_position(self.pid,100))
        for df in [broker.list_orders(),broker.list_fills()]:
            self.assertEqual(len(df),2)
            self.assertEqual(set(df['signal_source']),{'satellite_scan'})
            self.assertEqual(set(df['strategy_name']),{'事件卫星测试'})
        self.assertTrue(all('卫星轨/事件卫星测试' in note for note in broker.list_cashflows().query("type in ['买入', '卖出']")['note']))


    def test_daily_pnl_uses_actual_intraday_buy_price_and_fee(self):
        self.assertIn('已成交',broker.buy_position(self.pid,100))
        with patch.object(broker,'_latest_prices',lambda codes:{c:(9.,12.,10.,13.,8.) for c in codes}):
            self.assertAlmostEqual(broker.get_account()['今日盈亏'],-105.)
            self.assertAlmostEqual(broker.get_positions().iloc[0]['今日盈亏'],-105.)

    def test_daily_pnl_includes_sold_out_position(self):
        self.make_sellable()
        with broker._conn() as c:
            c.execute("UPDATE broker_fills SET date='2026-09-22'")
        self.assertIn('已成交',broker.sell_position(self.pid,200))
        self.assertAlmostEqual(broker.get_account()['今日盈亏'],-6.)

    def test_daily_pnl_partial_sale_and_remaining_mark(self):
        self.make_sellable()
        with broker._conn() as c:
            c.execute("UPDATE broker_fills SET date='2026-09-22'")
        with patch.object(broker,'_latest_prices',lambda codes:{c:(9.,10.,10.,11.,8.) for c in codes}):
            self.assertIn('已成交',broker.sell_position(self.pid,100))
            self.assertAlmostEqual(broker.get_account()['今日盈亏'],-205.45)


    def test_execution_qualification_blocks_money_movement(self):
        with patch.object(execution_gate, 'position_rejection', lambda *args: '缺少验证批准'):
            self.assertIn('执行资格拦截', broker.buy_position(self.pid,100))
        self.assertEqual(self.counts(), (0,0,0))
        self.assertEqual(broker._get_cash(), broker.INIT_CASH)


if __name__ == '__main__':
    unittest.main()
