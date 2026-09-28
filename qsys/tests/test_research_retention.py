import json
import tempfile
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import pandas as pd
import datasource
import research_retention as rr


class RetentionTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.patch=patch.object(datasource,'MKT_DB',Path(self.tmp.name)/'market.db');self.patch.start();self.addCleanup(self.patch.stop)
        rr.setup();self.code='SH600664';self.day='2025-01-02'
        with datasource._conn() as c:
            for day in pd.bdate_range('2025-01-02',periods=100):
                d=str(day.date())
                c.execute("INSERT INTO market_daily(source,code,date,open,high,low,close,volume,amount) VALUES('ths_ifind',?,?,10,11,9,10,24000,240000)",(self.code,d))
                c.execute("INSERT INTO ifind_calendar VALUES('SSE',?)",(d,))
        with datasource._conn() as c:
            dates=[r[0] for r in c.execute("SELECT date FROM ifind_calendar WHERE exchange='SSE' ORDER BY date")]
            c.execute('INSERT INTO research_calendar_receipts VALUES(?,?,?,?,?,?)',('SSE',dates[0],dates[-1],json.dumps(dates),rr._hash(dates),'2025-01-01'))
        self.insert_minute(self.day)

    def insert_minute(self,day,code=None):
        times=list(pd.date_range(day+' 09:31',day+' 11:30',freq='min'))+list(pd.date_range(day+' 13:01',day+' 15:00',freq='min'))
        with datasource._conn() as c:
            c.executemany('INSERT OR REPLACE INTO ifind_minute(code,datetime,open,high,low,close,volume,amount) VALUES(?,?,10,11,9,10,100,1000)',[(code or self.code,str(t)) for t in times])

    def test_cleanup_preserves_features_daily_and_other_stock(self):
        self.insert_minute(self.day,'SH603893')
        self.assertEqual(rr.process_day(self.code,self.day,'ifind_minute'),'ready')
        out=rr.cleanup(self.code,'2025-01-01','2025-12-31')
        self.assertEqual(out['deleted']['ifind_minute'],240)
        with datasource._conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM market_daily').fetchone()[0],100)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ifind_minute WHERE code='SH603893'").fetchone()[0],240)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM research_day_archive WHERE quality='ready'").fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM research_cleanup_log').fetchone()[0],1)
        self.assertEqual(rr.cleanup(self.code,'2025-01-01','2025-12-31')['deleted']['ifind_minute'],0)

    def test_missing_minute_blocks_even_if_count_threshold_passed(self):
        with datasource._conn() as c:c.execute("DELETE FROM ifind_minute WHERE datetime=?",(self.day+' 10:00:00',))
        self.assertEqual(rr.process_day(self.code,self.day,'ifind_minute'),'blocked')
        self.assertEqual(rr.preview(self.code,'2025-01-01','2025-12-31')['eligible']['ifind_minute'],0)

    def test_late_same_count_update_invalidates_archive(self):
        rr.process_day(self.code,self.day,'ifind_minute')
        with datasource._conn() as c:c.execute('UPDATE ifind_minute SET close=10.01 WHERE code=? AND datetime=?',(self.code,self.day+' 10:00:00'))
        self.assertEqual(rr.preview(self.code,'2025-01-01','2025-12-31')['eligible']['ifind_minute'],0)

    def test_protection_and_retention_and_auto_opt_in(self):
        rr.process_day(self.code,self.day,'ifind_minute')
        rr.set_policy(self.code,100,10,False)
        self.assertEqual(rr.preview(self.code,'2025-01-01','2025-12-31')['eligible']['ifind_minute'],0)
        rr.set_policy(self.code,60,10,False);rr.protect(self.code,self.day,self.day,'研究归档')
        self.assertEqual(rr.preview(self.code,'2025-01-01','2025-12-31')['eligible']['ifind_minute'],0)
        with self.assertRaisesRegex(ValueError,'未启用'):rr.cleanup(self.code,'2025-01-01','2025-12-31',automatic=True)

    def test_five_session_label_waits_and_rejects_missing_daily(self):
        rr.process_day(self.code,self.day,'ifind_minute');rr.refresh_labels(self.code)
        with datasource._conn() as c:
            row=c.execute('SELECT status,end_day,value FROM research_labels').fetchone()
            self.assertEqual(row,('mature_raw_close','2025-01-09',0.0))
            c.execute("DELETE FROM market_daily WHERE date='2025-01-06'")
        rr.refresh_labels(self.code)
        with datasource._conn() as c:self.assertEqual(c.execute('SELECT status,value FROM research_labels').fetchone(),('waiting_data',None))

    def test_worker_is_idempotent_and_does_not_delete_by_default(self):
        rr.run_incremental(self.code,search=True);rr.run_incremental(self.code,search=True)
        with datasource._conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM research_day_archive').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ifind_minute').fetchone()[0],240)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM research_labels').fetchone()[0],1)

    def test_cross_thread_lock(self):
        errors=[]
        def attempt():
            try:
                with rr.stock_lock(self.code):pass
            except ValueError as exc:errors.append(str(exc))
        with rr.stock_lock(self.code):
            with rr.stock_lock(self.code):pass
            t=threading.Thread(target=attempt);t.start();t.join(3)
        self.assertEqual(len(errors),1)

    def test_cleanup_page(self):
        from streamlit.testing.v1 import AppTest
        app=AppTest.from_string("from research_retention_view import render\nrender('SH600664')").run()
        self.assertFalse(app.exception)
        self.assertTrue(next(b for b in app.button if b.label=='确认清理原始高频').disabled)

    def test_cleanup_button_requires_archive_and_confirmation(self):
        from streamlit.testing.v1 import AppTest
        app=AppTest.from_string("from research_retention_view import render\nrender('SH600664')").run()
        self.assertTrue(any('符合删除条件的记录为 0' in x.value for x in app.warning))
        next(x for x in app.checkbox if x.label.startswith('确认仅删除')).check().run()
        self.assertTrue(next(b for b in app.button if b.label=='确认清理原始高频').disabled)
        self.assertEqual(rr.process_day(self.code,self.day,'ifind_minute'),'ready')
        app.run()
        self.assertFalse(app.exception)
        self.assertFalse(next(b for b in app.button if b.label=='确认清理原始高频').disabled)
        next(x for x in app.checkbox if x.label.startswith('确认仅删除')).uncheck().run()
        self.assertTrue(next(b for b in app.button if b.label=='确认清理原始高频').disabled)
        self.assertTrue(any('请先勾选上方确认框' in x.value for x in app.info))

    def test_prepare_cleanup_never_deletes_even_with_auto_policy(self):
        rr.set_policy(self.code,1,1,True)
        result=rr.prepare_cleanup(self.code,'2025-01-01','2025-12-31')
        self.assertEqual(result['processed'],{'ready':1})
        with datasource._conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ifind_minute').fetchone()[0],240)
        self.assertEqual(rr.prepare_cleanup(self.code,'2025-01-01','2025-12-31')['processed'],{})

    def test_page_prepares_then_cleans_and_refreshes_preview(self):
        from streamlit.testing.v1 import AppTest
        app=AppTest.from_string("from research_retention_view import render\nrender('SH600664')").run()
        next(b for b in app.button if b.label=='校验并归档所选范围（不删除）').click().run()
        self.assertFalse(app.exception)
        next(x for x in app.checkbox if x.label.startswith('确认仅删除')).check().run()
        next(b for b in app.button if b.label=='确认清理原始高频').click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any('本次实际删除 240 行' in x.value for x in app.get('toast')))
        self.assertTrue(next(b for b in app.button if b.label=='确认清理原始高频').disabled)
        with datasource._conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM ifind_minute').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM market_daily').fetchone()[0],100)

    def test_archive_cleanup_covers_more_than_one_batch_and_keeps_blocked(self):
        days=[str(x.date()) for x in pd.bdate_range('2025-01-02',periods=43)]
        for day in days[1:]:
            self.insert_minute(day)
        rr.set_policy(self.code,1,1,False)
        with datasource._conn() as c:
            c.execute('UPDATE ifind_minute SET volume=-1 WHERE code=? AND substr(datetime,1,10)=?',(self.code,days[-1]))
        events=[]
        result=rr.archive_and_cleanup(self.code,'2025-01-01','2025-12-31',lambda p,m:events.append((p,m)))
        self.assertEqual(result['deleted']['ifind_minute'],42*240)
        self.assertEqual(result['remaining_rows'],240)
        self.assertEqual(result['archive_counts'],{'ready':42,'blocked':1})
        self.assertTrue(result['remaining_reasons'])
        self.assertTrue(any('43/43' in m for _,m in events))
        with datasource._conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM market_daily').fetchone()[0],100)
            saved=json.loads(c.execute('SELECT payload FROM research_cleanup_log WHERE id=?',(result['id'],)).fetchone()[0])
        self.assertEqual(saved['remaining_rows'],240)

    def test_legacy_cleanup_does_not_delete_unarchived_raw(self):
        datasource.cleanup_old_data()
        with datasource._conn() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM ifind_minute').fetchone()[0],240)

    def test_candidate_search_survives_guarded_cleanup_and_deduplicates(self):
        import math
        with datasource._conn() as c:
            for i,day in enumerate(pd.bdate_range('2024-01-02',periods=180)):
                ds=str(day.date());f=json.dumps({'return_feature':math.sin(i/7)})
                c.execute('INSERT INTO research_day_archive VALUES(?,?,?,?,?,?,?,?,?,?,?)',(self.code,ds,'tick_data',rr.VERSION,1,str(i),'ready','',f,100,'2025-01-01'))
                c.execute('INSERT INTO research_labels VALUES(?,?,?,?,?,?,?,?)',(self.code,ds,5,str((day+pd.offsets.BDay(5)).date()),math.sin(i/7+.5),'mature_raw_close',str(i),'2025-01-01'))
        first=rr.search_candidates(self.code);self.assertEqual(first['status'],'completed');self.assertGreater(first['candidates'],0)
        rr.search_candidates(self.code)
        with datasource._conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM research_candidate_runs').fetchone()[0],1)
            c.execute('INSERT INTO research_raw_revision VALUES(?,?,?,?)',(self.code,'2024-01-02','tick_data',101))
            c.execute('INSERT INTO research_day_archive VALUES(?,?,?,?,?,?,?,?,?,?,?)',(self.code,'2024-01-02','tick_data',rr.VERSION,101,'','cleared','','{}',0,'2025-01-01'))
        self.assertEqual(rr.search_candidates(self.code),first)
        with datasource._conn() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM research_candidate_runs').fetchone()[0],1)

    def test_audit_failure_rolls_back_deletion(self):
        rr.process_day(self.code,self.day,'ifind_minute')
        with datasource._conn() as c:
            c.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON research_cleanup_log BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")
        with self.assertRaises(Exception):rr.cleanup(self.code,'2025-01-01','2025-12-31')
        with datasource._conn() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM ifind_minute').fetchone()[0],240)

    def test_calendar_hole_never_matures(self):
        rr.process_day(self.code,self.day,'ifind_minute')
        with datasource._conn() as c:c.execute("DELETE FROM ifind_calendar WHERE date='2025-01-06'")
        rr.refresh_labels(self.code)
        with datasource._conn() as c:self.assertEqual(c.execute('SELECT status,value FROM research_labels').fetchone(),('waiting_calendar',None))

    def test_feature_write_failure_blocks_cleanup_and_retries(self):
        with patch.object(datasource,'compute_intraday_features',side_effect=RuntimeError('disk full')):
            self.assertEqual(rr.process_day(self.code,self.day,'ifind_minute'),'blocked')
        self.assertEqual(rr.preview(self.code,'2025-01-01','2025-12-31')['eligible']['ifind_minute'],0)
        with datasource._conn() as c:c.execute("UPDATE research_retry SET next_try='2000-01-01'")
        self.assertEqual(rr.run_incremental(self.code)[self.code]['processed'],{'ready':1})

    def test_missing_daily_does_not_starve_other_dates(self):
        self.insert_minute('2025-01-03')
        with datasource._conn() as c:c.execute('DELETE FROM market_daily WHERE date=?',(self.day,))
        self.assertEqual(rr.run_incremental(self.code,limit=1)[self.code]['processed'],{'blocked':1})
        self.assertEqual(rr.run_incremental(self.code,limit=1)[self.code]['processed'],{'ready':1})

    def test_unregistered_stock_is_included_without_deletion(self):
        result=rr.run_incremental(limit=1)
        self.assertIn(self.code,result)
        with datasource._conn() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM ifind_minute').fetchone()[0],240)

    def test_calendar_receipt_rejects_provider_omissions(self):
        days=pd.DataFrame({'date':['2025-01-02','2025-01-03']})
        with patch.object(datasource,'ths_trade_dates',return_value=(days,None,0)):
            with self.assertRaisesRegex(ValueError,'遗漏'):
                rr.sync_calendar('SSE','2025-01-02','2025-01-10')
        with self.assertRaisesRegex(ValueError,'尚未配置'):
            rr.sync_calendar('BSE','2025-01-02','2025-01-10')

    def test_noop_feature_writer_cannot_authorize_cleanup(self):
        with patch.object(datasource,'compute_intraday_features',return_value={}):
            self.assertEqual(rr.process_day(self.code,self.day,'ifind_minute'),'blocked')
        self.assertEqual(rr.preview(self.code,'2025-01-01','2025-12-31')['eligible']['ifind_minute'],0)
