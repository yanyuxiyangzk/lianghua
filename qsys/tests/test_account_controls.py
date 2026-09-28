"""Isolated exposure, explicit submission, retry and immutable attribution tests."""
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

TMP=tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR']=TMP.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import broker
import experience
import account_controls as controls
import execution_gate
import library
import trade_attribution


class Clock(datetime):
    @classmethod
    def now(cls,tz=None): return cls(2026,9,28,10)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.pack={'factors':[{'name':'momentum','expr':'$close/Ref($close,5)'}]}
        patches=[patch.object(broker,'DB_PATH',root/'account.db'),patch.object(experience,'DB_PATH',root/'account.db'),
                 patch.object(experience,'_RISK_FLAG',root/'risk.json'),patch.object(broker,'datetime',Clock),
                 patch.object(broker,'day_status',return_value=True),patch.object(broker,'_quote_fresh',return_value=True),
                 patch.object(broker,'get_name',side_effect=lambda x:x),
                 patch.object(broker,'_latest_prices',side_effect=lambda codes:{cd:(10.,10.,10.,11.,9.,'2026-09-28 10:00:00') for cd in codes}),
                 patch.object(execution_gate,'position_rejection',return_value=''),
                 patch.object(library,'list_strategies',side_effect=lambda:{'fixture':self.pack}),
                 patch.object(experience,'risk_halt_today',return_value=(False,'')),
                 patch.object(experience,'get_account_risk_config',return_value=dict(experience.ACCOUNT_RISK_DEFAULTS))]
        for p in patches:p.start();self.addCleanup(p.stop)
        experience._RISK_FLAG.write_text(json.dumps(dict(date='2026-09-28',target_position_ratio=.8,level='normal',halt=False)))
        with experience._conn(): pass
        broker._init_account();broker._settle_today()
        with broker._conn() as c:
            c.execute("UPDATE broker_account SET value='100000' WHERE key='cash'")
            c.execute("INSERT INTO broker_positions(code,name,source,shares,sellable,cost,last_buy_date) "
                      "VALUES ('SZ002709','测试','manual',10000,10000,10,'2026-09-23')")

    def count_fills(self):
        with broker._conn() as c:return c.execute('SELECT COUNT(*) FROM broker_fills').fetchone()[0]

    def test_other_stock_buy_blocked_by_existing_excess(self):
        message=broker.place_order('SH600000','buy',None,100)
        self.assertIn('存量单票敞口超限',message)
        self.assertEqual(self.count_fills(),0)

    def test_pending_buy_rechecked_after_other_holding_becomes_excess(self):
        with broker._conn() as c:
            c.execute("UPDATE broker_positions SET shares=1000,sellable=1000")
        self.assertIn('已挂单',broker.place_order('SH600000','buy',9.5,100))
        with broker._conn() as c:c.execute('UPDATE broker_positions SET shares=10000,sellable=10000')
        with patch.object(broker,'_latest_prices',side_effect=lambda codes:{cd:(9.,10.,10.,11.,8.,'2026-09-28 10:00:00') for cd in codes}):
            self.assertEqual(broker.fill_pending_orders(),0)
        self.assertEqual(self.count_fills(),0)

    def test_generation_does_not_trade_and_confirmation_required(self):
        identifier=controls.generate_plan()
        plan=controls.latest_plan()
        self.assertEqual(plan['id'],identifier)
        self.assertGreater(plan['items'][0]['shares'],0)
        self.assertEqual(self.count_fills(),0)
        with self.assertRaisesRegex(ValueError,'必须确认'):controls.submit_plan(identifier)
        self.assertEqual(self.count_fills(),0)

    def test_confirmed_plan_fills_once_and_reports_actual_fills(self):
        identifier=controls.generate_plan()
        controls.submit_plan(identifier,confirmed=True)
        first=controls.latest_plan()
        self.assertEqual(first['items'][0]['status'],'已成交')
        self.assertEqual(first['items'][0]['filled_shares'],first['items'][0]['shares'])
        self.assertIn('已提交',controls.submit_plan(identifier,confirmed=True))
        self.assertEqual(self.count_fills(),1)
        after=controls.snapshot()
        self.assertLessEqual(after['rows'][0]['weight'],.15)
        with broker._conn() as c:
            row=c.execute('SELECT risk_plan_id,decision_reason,attribution_status FROM broker_orders').fetchone()
        self.assertEqual(row[0],identifier);self.assertIn('账户降仓',row[1]);self.assertEqual(row[2],'manual')

    def test_concurrent_submission_does_not_duplicate(self):
        identifier=controls.generate_plan()
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _:controls.submit_plan(identifier,confirmed=True),range(2)))
        self.assertEqual(self.count_fills(),1)

    def test_changed_cash_or_quotes_reject_stale_review(self):
        identifier=controls.generate_plan()
        with broker._conn() as c:c.execute("UPDATE broker_account SET value='100001' WHERE key='cash'")
        with self.assertRaisesRegex(ValueError,'已变化'):controls.submit_plan(identifier,confirmed=True)
        identifier=controls.generate_plan()
        with patch.object(broker,'_latest_prices',side_effect=lambda codes:{cd:(11.,10.,10.,12.,9.,'2026-09-28 10:00:00') for cd in codes}):
            with self.assertRaisesRegex(ValueError,'超过2%'):controls.submit_plan(identifier,confirmed=True)
        self.assertEqual(self.count_fills(),0)

    def test_stale_quote_or_missing_risk_target_blocks_submission(self):
        identifier=controls.generate_plan()
        with patch.object(broker,'_quote_fresh',return_value=False):
            with self.assertRaisesRegex(ValueError,'未就绪'):controls.submit_plan(identifier,confirmed=True)
        experience._RISK_FLAG.write_text('{}')
        with self.assertRaisesRegex(ValueError,'未就绪'):controls.submit_plan(identifier,confirmed=True)
        self.assertEqual(self.count_fills(),0)

    def test_expired_or_superseded_plan_cannot_submit(self):
        identifier=controls.generate_plan()
        controls.generate_plan()
        with self.assertRaisesRegex(ValueError,'失效'):controls.submit_plan(identifier,confirmed=True)
        identifier=controls.generate_plan()
        with controls._connection() as c:c.execute("UPDATE account_reduction_plans SET day='2026-09-25' WHERE id=?",(identifier,))
        with self.assertRaisesRegex(ValueError,'失效'):controls.submit_plan(identifier,confirmed=True)

    def test_limit_down_waits_then_cancel_remains_unfinished(self):
        quote=lambda codes:{cd:(10.,10.,10.,11.,10.,'2026-09-28 10:00:00') for cd in codes}
        with patch.object(broker,'_latest_prices',side_effect=quote):
            identifier=controls.generate_plan();controls.submit_plan(identifier,confirmed=True)
        plan=controls.latest_plan();self.assertEqual(plan['items'][0]['status'],'等待成交')
        broker.cancel_order(plan['items'][0]['order_id'])
        self.assertEqual(controls.latest_plan()['items'][0]['status'],'已撤销')
        self.assertEqual(self.count_fills(),0)

    def test_t_plus_one_or_mismatched_ai_lots_are_blocked(self):
        with broker._conn() as c:c.execute('UPDATE broker_positions SET sellable=0')
        controls.generate_plan();self.assertEqual(controls.latest_plan()['items'][0]['status'],'受限，未提交')
        with broker._conn() as c:c.execute("UPDATE broker_positions SET source='ai',sellable=10000")
        controls.generate_plan();self.assertIn('对账',controls.latest_plan()['items'][0]['message'])
        self.assertEqual(self.count_fills(),0)

    def test_partly_sellable_position_keeps_unsold_quantity_visible(self):
        with broker._conn() as c:c.execute('UPDATE broker_positions SET sellable=1000')
        identifier=controls.generate_plan()
        plan=controls.latest_plan()
        self.assertEqual(len(plan['items']),2)
        self.assertEqual(plan['items'][0]['shares'],1000)
        controls.submit_plan(identifier,confirmed=True)
        plan=controls.latest_plan()
        self.assertEqual(plan['status'],'部分完成，仍有未完成项')
        self.assertEqual(plan['items'][0]['filled_shares'],1000)
        self.assertEqual(plan['items'][1]['filled_shares'],0)

    def test_advisory_includes_manual_concentration_even_below_total_cap(self):
        with patch.object(experience,'RISK_PLAN_FILE',Path(self.tmp.name)/'plan.json'):
            plan=experience.build_risk_reduction_plan({'dd_now':0},'2026-09-28')
        self.assertEqual(plan['current_position_ratio'],.5)
        self.assertGreater(plan['required_release'],70000)
        self.assertEqual(plan['positions'][0]['reasons'],['单股超限'])
        self.assertEqual(self.count_fills(),0)

    def test_scheduler_excess_cancels_buys_even_at_normal_drawdown(self):
        import scheduler
        with patch.object(experience,'_write_risk_flag') as flag,patch.object(broker,'cancel_pending_buys',return_value=1) as cancel, \
                patch.object(experience,'build_risk_reduction_plan',return_value={'required_release':1}), \
                patch.object(experience,'risk_llm_advice',return_value={'status':'skipped'}):
            scheduler._apply_account_risk('2026-09-28',{'dd_now':0,'circuit':False})
        self.assertTrue(flag.call_args.args[1])
        self.assertIn('单股超限',flag.call_args.args[2])
        cancel.assert_called_once()

    def test_plan_write_failure_rolls_back_order_fill_and_cash(self):
        identifier=controls.generate_plan()
        with controls._connection() as c:
            c.execute("CREATE TRIGGER fail_plan BEFORE UPDATE ON account_reduction_items BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(Exception):controls.submit_plan(identifier,confirmed=True)
        self.assertEqual(self.count_fills(),0)
        self.assertEqual(broker._get_cash(),100000.)
        self.assertEqual(controls.latest_plan()['state'],'review')

    def test_partial_fill_tracking_is_not_completion(self):
        quote=lambda codes:{cd:(10.,10.,10.,11.,10.,'2026-09-28 10:00:00') for cd in codes}
        with patch.object(broker,'_latest_prices',side_effect=quote):
            identifier=controls.generate_plan();controls.submit_plan(identifier,confirmed=True)
        oid=controls.latest_plan()['items'][0]['order_id']
        with broker._conn() as c:c.execute('INSERT INTO broker_fills(order_id,shares) VALUES (?,100)',(oid,))
        plan=controls.latest_plan();self.assertEqual(plan['items'][0]['status'],'部分成交')
        self.assertEqual(plan['status'],'执行中')

    def _buy_ai(self):
        with broker._conn() as c:
            c.execute('DELETE FROM broker_positions')
            c.execute("UPDATE broker_account SET value='200000' WHERE key='cash'")
            pid=c.execute("INSERT INTO positions(code,buy_date,source,pack_name,status,limit_price,pick_id) "
                          "VALUES ('SZ002709','2026-09-28','sched_pool_scan','fixture','pending',11,77)").lastrowid
        self.assertIn('已成交',broker.buy_position(pid,100))
        return pid

    def test_sell_keeps_entry_version_even_after_strategy_changes(self):
        pid=self._buy_ai()
        with broker._conn() as c:
            before=c.execute('SELECT strategy_version,factor_snapshot FROM broker_orders').fetchone()
            c.execute("UPDATE positions SET buy_date='2026-09-23' WHERE id=?",(pid,))
            c.execute('UPDATE broker_positions SET sellable=shares')
        self.pack={'factors':[{'name':'different','expr':'$open'}]}
        self.assertIn('已成交',broker.sell_position(pid,100,reason='止损'))
        fills=broker.list_fills(False).sort_values('id')
        self.assertEqual(list(fills['strategy_version']),[before[0],before[0]])
        self.assertEqual(list(fills['factor_snapshot']),[before[1],before[1]])
        self.assertEqual(list(fills['signal_batch_id']),['pick:77','pick:77'])
        self.assertEqual(fills.iloc[-1]['decision_reason'],'止损')

    def test_ai_reduction_updates_both_ledgers_and_marks_legacy_unknown(self):
        with broker._conn() as c:
            c.execute("UPDATE broker_positions SET source='ai'")
            c.execute("INSERT INTO positions(code,buy_date,buy_price,shares,buy_amount,source,pack_name,status) "
                      "VALUES ('SZ002709','2026-09-23',10,10000,100000,'reconcile_fix','对账补记','open')")
        identifier=controls.generate_plan();controls.submit_plan(identifier,confirmed=True)
        with broker._conn() as c:
            self.assertEqual(c.execute('SELECT shares FROM positions').fetchone()[0],c.execute('SELECT shares FROM broker_positions').fetchone()[0])
            self.assertEqual(c.execute('SELECT attribution_status,strategy_version FROM broker_orders').fetchone(),('historical_unknown',None))

    def test_missing_strategy_snapshot_blocks_new_ai_buy(self):
        with broker._conn() as c:
            c.execute('DELETE FROM broker_positions')
            pid=c.execute("INSERT INTO positions(code,buy_date,source,pack_name,status,limit_price) "
                          "VALUES ('SZ002709','2026-09-28','sched_pool_scan','fixture','pending',11)").lastrowid
        with patch.object(library,'list_strategies',return_value={}):
            self.assertIn('归因不可用',broker.buy_position(pid,100))
        self.assertEqual(self.count_fills(),0)

    def test_manual_risk_refresh_does_not_call_llm_or_sell(self):
        with patch.object(experience,'portfolio_risk',return_value={'ok':True,'dd_now':0,'circuit':False}), \
                patch.object(experience,'RISK_PLAN_FILE',Path(self.tmp.name)/'plan.json'), \
                patch.object(experience,'risk_llm_advice') as llm:
            controls.refresh_risk()
        llm.assert_not_called()
        self.assertTrue(json.loads(experience._RISK_FLAG.read_text())['halt'])
        self.assertEqual(self.count_fills(),0)

    def test_manual_risk_refresh_retains_missing_data_reason(self):
        with patch.object(experience,'portfolio_risk',return_value={'ok':False,'reason':'测试行情过期'}):
            with self.assertRaisesRegex(ValueError,'测试行情过期'):controls.refresh_risk()
        state=json.loads(experience._RISK_FLAG.read_text())
        self.assertTrue(state['halt'])
        self.assertEqual(state['reason'],'测试行情过期')

    def test_llm_advice_failure_does_not_erase_risk_target(self):
        import scheduler
        with patch.object(experience,'RISK_PLAN_FILE',Path(self.tmp.name)/'plan.json'), \
                patch.object(experience,'risk_llm_advice',side_effect=RuntimeError('model unavailable')):
            scheduler._apply_account_risk('2026-09-28',{'dd_now':0,'circuit':False})
        state=json.loads(experience._RISK_FLAG.read_text())
        self.assertTrue(state['halt'])
        self.assertEqual(state['target_position_ratio'],.8)
        self.assertEqual(self.count_fills(),0)

    def test_changed_strategy_version_rejects_pending_fill(self):
        with broker._conn() as c:
            c.execute('DELETE FROM broker_positions')
            pid=c.execute("INSERT INTO positions(code,buy_date,source,pack_name,status,limit_price) "
                          "VALUES ('SZ002709','2026-09-28','sched_pool_scan','fixture','pending',11)").lastrowid
        self.assertIn('已挂单',broker.place_order('SZ002709','buy',9.5,100,source='ai',_position_id=pid))
        self.pack={'factors':[{'name':'changed','expr':'$open'}]}
        with patch.object(broker,'_latest_prices',side_effect=lambda codes:{cd:(9.,10.,10.,11.,8.) for cd in codes}):
            self.assertEqual(broker.fill_pending_orders(),0)
        self.assertEqual(self.count_fills(),0)


if __name__=='__main__':unittest.main()
