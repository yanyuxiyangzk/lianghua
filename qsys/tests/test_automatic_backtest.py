import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
import automatic_backtest as q
from automatic_backtest_compute import historical_stats


class AutomaticQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.object(q,'DB',Path(self.tmp.name)/'queue.db');p.start();self.addCleanup(p.stop)
        p=patch.object(q,'DATA_DIR',Path(self.tmp.name));p.start();self.addCleanup(p.stop)
        self.payload=dict(kind='registry',name='factor',scope='pool',manifest='v1',start='2024-01-01',end='2026-09-29')

    def test_idempotency_and_changed_data_wakes_failed_preserving_report(self):
        a=q.enqueue(self.payload)
        self.assertEqual(a,q.enqueue(self.payload))
        with q.connect() as c:
            c.execute("UPDATE tasks SET status='waiting_data',report=? WHERE id=?",(json.dumps({'old':True}),a))
        b=q.enqueue({**self.payload,'manifest':'v2'})
        self.assertNotEqual(a,b)
        counts,rows=q.overview()
        self.assertEqual(counts,{'pending':1})
        with q.connect() as c:
            old=c.execute('SELECT report FROM tasks WHERE id=?',(a,)).fetchone()[0]
        self.assertEqual(json.loads(old),{'old':True})

    def test_latest_pending_supersedes_without_starvation(self):
        a=q.enqueue(self.payload)
        with q.connect() as c:c.execute('UPDATE tasks SET created=1 WHERE id=?',(a,))
        b=q.enqueue({**self.payload,'end':'2026-09-30'})
        with q.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM tasks WHERE id=?',(a,)).fetchone()[0],'superseded')
            self.assertEqual(c.execute('SELECT created FROM tasks WHERE id=?',(b,)).fetchone()[0],1)

    def test_batch_enqueue_keeps_versioning_and_idempotency(self):
        items=[{**self.payload,'name':str(n)} for n in range(205)]
        self.assertEqual(q.enqueue_many(items),205)
        q.enqueue_many(items)
        self.assertEqual(q.overview()[0],{'pending':205})
        q.enqueue_many({**p,'manifest':'v2'} for p in items)
        self.assertEqual(q.overview()[0],{'pending':205})
        with q.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks WHERE status='superseded'").fetchone()[0],205)

    def test_failure_is_not_completed_and_lock_excludes_another_worker(self):
        q.enqueue(self.payload)
        with patch.object(q,'discover'),patch.object(q,'isolated',return_value={'status':'waiting_data','reason':'gap'}):
            q.run_batch()
        self.assertEqual(q.overview()[0],{'waiting_data':1})
        import fcntl
        with (Path(self.tmp.name)/'automatic_backtest.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertIn('跳过重入',q.run_batch())

    def test_running_recovers_and_paused_does_not_run(self):
        a=q.enqueue(self.payload)
        with q.connect() as c:c.execute("UPDATE tasks SET status='running' WHERE id=?",(a,))
        with patch.object(q,'discover'),patch.object(q,'isolated',return_value={'status':'research_complete'}):q.run_batch()
        self.assertEqual(q.overview()[0],{'research_complete':1})
        q.configure({'enabled':False})
        with patch.object(q,'discover') as d:
            self.assertIn('暂停',q.run_batch());d.assert_not_called()

    def test_discovery_failure_does_not_block_existing_queue_or_hide_error(self):
        q.enqueue(self.payload)
        with patch.object(q,'discover',side_effect=sqlite3.OperationalError('interrupted')), \
             patch.object(q,'isolated',return_value={'status':'research_complete'}):
            q.run_batch()
        self.assertEqual(q.overview()[0],{'research_complete':1})
        self.assertIn('interrupted',q.setting('error'))
        with patch.object(q,'discover',return_value={}):
            q.run_batch()
        self.assertIn('interrupted',q.setting('error'))

    def test_verified_calendar_union_covers_overlap_and_adjacent_only(self):
        from trading_calendar import covers_interval
        receipts=[('2019-01-01','2026-09-25'),('2026-01-01','2026-12-31')]
        self.assertTrue(covers_interval('2019-09-09','2026-09-30',receipts))
        self.assertTrue(covers_interval('2026-09-24','2026-09-30',
            [('2026-09-24','2026-09-25'),('2026-09-26','2026-09-30')]))
        self.assertFalse(covers_interval('2026-09-24','2026-09-30',
            [('2026-09-24','2026-09-25'),('2026-09-27','2026-09-30')]))
        self.assertFalse(covers_interval('2018-01-01','2026-09-30',receipts))
        self.assertFalse(covers_interval('2019-09-09','2027-01-01',receipts))

    def test_daily_precheck_accepts_verified_union_but_rejects_real_price_gap(self):
        import datasource
        import execution_constraints as ec
        from automatic_backtest_compute import load_daily, WaitingData
        db=Path(self.tmp.name)/'market.db'
        dates=['2026-09-24','2026-09-25','2026-09-28','2026-09-29','2026-09-30']
        with sqlite3.connect(db) as c:
            c.execute('CREATE TABLE market_daily(source,code,date,open,high,low,close,volume,amount)')
            c.executemany('INSERT INTO market_daily VALUES (?,?,?,?,?,?,?,?,?)',
                [('ths_ifind','SH600000',d,10,11,9,10,100,1000) for d in dates])
            c.execute('CREATE TABLE ifind_calendar(exchange,date)')
            c.executemany('INSERT INTO ifind_calendar VALUES (?,?)',[('SSE',d) for d in dates])
            c.execute('CREATE TABLE research_calendar_receipts(exchange,start,end,dates_json,digest)')
            for start,end in [('2026-09-24','2026-09-28'),('2026-09-28','2026-09-30')]:
                days=[d for d in dates if start<=d<=end]
                c.execute('INSERT INTO research_calendar_receipts VALUES (?,?,?,?,?)',
                    ('SSE',start,end,json.dumps(days),q.digest(days)))
        task=dict(codes=['SH600000'],start=dates[0],end=dates[-1])
        with patch.object(datasource,'MKT_DB',db),patch.object(ec,'DB',Path(self.tmp.name)/'missing.db'):
            panel,quality=load_daily(task)
            self.assertEqual(len(panel),5)
            self.assertEqual(quality['accepted_stocks'],1)
            with sqlite3.connect(db) as c:
                c.execute("UPDATE research_calendar_receipts SET digest='corrupt' WHERE start='2026-09-28'")
            with self.assertRaisesRegex(WaitingData,'可信交易日历'):load_daily(task)
            with sqlite3.connect(db) as c:
                c.execute("UPDATE research_calendar_receipts SET digest=? WHERE start='2026-09-28'",(q.digest(dates[2:]),))
                c.execute("DELETE FROM market_daily WHERE date='2026-09-29'")
            with self.assertRaisesRegex(WaitingData,'2026-09-29'):load_daily(task)

    def test_digest_catches_in_place_correction_but_ignores_redownload_timestamp(self):
        c=sqlite3.connect(':memory:')
        c.execute('CREATE TABLE market_daily(source,code,date,close,fetched_at)')
        c.execute("INSERT INTO market_daily VALUES ('ths_ifind','SH600000','2026-09-29',10,'a')")
        a=q.table_digest(c,'market_daily','2026-09-29')
        c.execute("UPDATE market_daily SET fetched_at='b'")
        self.assertEqual(a,q.table_digest(c,'market_daily','2026-09-29'))
        c.execute('UPDATE market_daily SET close=11')
        self.assertNotEqual(a,q.table_digest(c,'market_daily','2026-09-29'))
        c.execute("INSERT INTO market_daily VALUES ('ths_ifind','SH600000','2026-09-30',12,'c')")
        b=q.table_digest(c,'market_daily','2026-09-29')
        self.assertEqual(b['rows'],1)

    def test_dependency_isolation(self):
        m={'tables':{'market_daily':{'hash':'all','codes':{'a':'1','b':'2'}},'ifind_calendar':{'hash':'cal'},'research_calendar_receipts':{'hash':'receipt'}}}
        first=q.dependency_revision(m,['a'],{'code':'# sexpr: close'})
        m['tables']['market_daily']['codes']['b']='3'
        self.assertEqual(first,q.dependency_revision(m,['a'],{'code':'# sexpr: close'}))
        m['tables']['market_daily']['codes']['a']='4'
        self.assertNotEqual(first,q.dependency_revision(m,['a'],{'code':'# sexpr: close'}))

    def test_dependency_cache_is_scoped_to_manifest_pool_and_fields(self):
        m={'id':'v1','tables':{'market_daily':{'hash':'all','codes':{'a':'1','b':'2'}},
            'stock_fundflow_daily':{'hash':'fund'}}}
        cache={}
        with patch.object(q,'digest',wraps=q.digest) as hashed:
            first=q.dependency_revision(m,['a'],{'code':'# sexpr: close'},cache)
            self.assertEqual(first,q.dependency_revision(m,['a'],{'code':'# sexpr: volume'},cache))
            self.assertEqual(hashed.call_count,1)
            q.dependency_revision(m,['b'],{'code':'# sexpr: close'},cache)
            q.dependency_revision(m,['a'],{'code':'# sexpr: main_net_pct'},cache)
            q.dependency_revision({**m,'id':'v2'},['a'],{'code':'# sexpr: close'},cache)
            self.assertEqual(hashed.call_count,4)


class LocalWindowTests(unittest.TestCase):
    setUp = AutomaticQueueTests.setUp

    def coverage(self):
        return {'SH600000':dict(start='2026-01-01',end='2026-09-28'),
                'SH600009':dict(start='2026-02-01',end='2026-09-29')}

    def test_common_cutoff_advances_only_when_pool_tail_is_collected(self):
        coverage=self.coverage();codes=list(coverage)
        first=q.local_window(codes,coverage,'2026-09-30')
        self.assertEqual(first['effective_end'],'2026-09-28')
        self.assertTrue(first['tail_pending'])
        self.assertFalse(first['error'])
        coverage[codes[1]]['end']='2026-09-30'
        self.assertEqual(first,q.local_window(codes,coverage,'2026-09-30'))
        coverage[codes[0]]['end']='2026-09-30'
        final=q.local_window(codes,coverage,'2026-09-30')
        self.assertEqual(final['effective_end'],'2026-09-30')
        self.assertFalse(final['tail_pending'])
        self.assertEqual(q.local_window(codes,coverage,'2026-09-25')['effective_end'],'2026-09-25')

    def test_missing_stock_and_nonoverlap_do_not_shrink_pool(self):
        coverage=self.coverage()
        self.assertIn('缺少本地日线',q.local_window(['SH600000','missing'],coverage,'2026-09-30')['error'])
        coverage['SH600009']['start']='2026-09-29'
        self.assertIn('无共同研究区间',q.local_window(list(coverage),coverage,'2026-09-30')['error'])
        self.assertTrue(q.local_window(['SH600000'],coverage,'2026-09-30','2026-09-29')['error'])

    def test_discovery_freezes_same_cutoff_for_all_factors_and_keeps_pool(self):
        import library,common
        coverage=self.coverage()
        m=dict(id='snapshot',tables={},coverage=[dict(code=k,**v) for k,v in coverage.items()])
        q.configure(dict(pools=['test'],single_stock=False))
        with patch.object(q,'manifest',return_value=m), \
             patch('selection_policy.completed_signal_day',return_value='2026-09-30'), \
             patch.object(common,'all_pools',return_value={'test':list(coverage)}), \
             patch.object(library,'get_factor_registry',return_value=pd.DataFrame([
                 dict(name='one',kind='builtin'),dict(name='two',kind='builtin')])):
            result=q.discover(force=True)
        self.assertEqual(result['windows']['test']['effective_end'],'2026-09-28')
        counts,rows=q.overview()
        self.assertEqual(counts,{'pending':2})
        for row in rows:
            payload=json.loads(row['payload'])
            self.assertEqual(payload['end'],'2026-09-28')
            self.assertEqual(payload['codes'],sorted(coverage))
            self.assertEqual(payload['data_window']['requested_end'],'2026-09-30')

    def test_only_absent_tail_is_excluded_not_invalid_bars_or_interior_gaps(self):
        import datasource,execution_constraints as ec
        from automatic_backtest_compute import load_daily,WaitingData
        db=Path(self.tmp.name)/'window_market.db'
        dates=['2026-09-24','2026-09-25','2026-09-28','2026-09-29','2026-09-30']
        codes=['SH600000','SH600009']
        with sqlite3.connect(db) as c:
            c.execute('CREATE TABLE market_daily(source,code,date,open,high,low,close,volume,amount)')
            c.executemany('INSERT INTO market_daily VALUES (?,?,?,?,?,?,?,?,?)',
                [('ths_ifind',code,d,10,11,9,10,100,1000) for code in codes for d in dates[:3]])
            c.execute('INSERT INTO market_daily VALUES (?,?,?,?,?,?,?,?,?)',
                ('ths_ifind',codes[1],dates[3],10,11,9,10,100,1000))
        window=q.local_window(codes,self.coverage(),dates[-1])
        task=dict(codes=codes,start=dates[0],end=window['effective_end'],data_window=window)
        with patch.object(datasource,'MKT_DB',db),patch.object(ec,'DB',Path(self.tmp.name)/'missing.db'), \
             patch('trading_calendar.calendar_data',return_value=(set(dates),[(dates[0],dates[-1])])):
            panel,quality=load_daily(task)
            self.assertEqual(quality['accepted_stocks'],2)
            self.assertEqual(str(panel.index.get_level_values('datetime').max().date()),'2026-09-28')
            with sqlite3.connect(db) as c:
                c.execute("DELETE FROM market_daily WHERE date='2026-09-25'")
            with self.assertRaisesRegex(WaitingData,'2026-09-25'):load_daily(task)
            with sqlite3.connect(db) as c:
                c.executemany('INSERT INTO market_daily VALUES (?,?,?,?,?,?,?,?,?)',
                    [('ths_ifind',code,dates[1],10,11,9,10,100,1000) for code in codes])
                c.execute("UPDATE market_daily SET close=NULL WHERE date='2026-09-28'")
            with self.assertRaisesRegex(WaitingData,'2026-09-28'):load_daily(task)

    def test_report_retains_cutoff_even_for_insufficient_or_failed_precheck(self):
        import automatic_backtest_compute as compute
        window=q.local_window(list(self.coverage()),self.coverage(),'2026-09-30')
        for status in ['research_complete','insufficient','waiting_data']:
            with patch.object(compute,'_compute',return_value=dict(status=status,reason='fixture')):
                report=compute.compute(dict(data_window=window))
            self.assertEqual(report['data_window'],window)
            self.assertEqual(report['status'],status)
            self.assertIn('2026-09-28',report['reason'])
        bad=q.local_window(['missing'],{},'2026-09-30')
        with patch.object(compute,'_compute',side_effect=AssertionError('must not drop missing stock')):
            self.assertEqual(compute.compute(dict(data_window=bad))['status'],'waiting_data')

    def test_page_displays_market_date_and_frozen_local_cutoff(self):
        from streamlit.testing.v1 import AppTest
        window=q.local_window(list(self.coverage()),self.coverage(),'2026-09-30')
        q.enqueue({**self.payload,'end':window['effective_end'],'data_window':window})
        q.put('discovery',dict(at=1,end='2026-09-30',pools=['pool'],windows={'pool':window}))
        app=AppTest.from_string('import automatic_backtest_view as view\nview._status()',default_timeout=20).run()
        self.assertFalse(app.exception,[e.message for e in app.exception])
        frame=app.dataframe[0].value
        self.assertEqual(frame.iloc[0]['回测截止'],'2026-09-28')
        self.assertEqual(frame.iloc[0]['市场截止'],'2026-09-30')
        self.assertTrue(any('待同步' in x.value for x in app.info))


class HistoricalStatisticTests(unittest.TestCase):
    def fixture(self):
        dates=pd.bdate_range('2025-01-01',periods=65)
        close=pd.DataFrame({f'S{i}':10*(1+.001*i)**np.arange(65) for i in range(35)},index=dates)
        x=pd.DataFrame(np.tile(np.arange(35),(65,1)),index=dates,columns=close.columns)
        return x,close

    def test_mature_labels_and_periods_no_nav(self):
        x,close=self.fixture();r=historical_stats(x,close,[5])
        self.assertEqual(r['summary']['5']['days'],60)
        self.assertAlmostEqual(r['summary']['5']['mean_rank_ic'],1)
        self.assertEqual(r['summary']['5']['immature_dates'],5)
        self.assertNotIn('nav',r)
        self.assertTrue(r['periods'])

    def test_gap_invalidates_window_instead_of_replacing_stock(self):
        x,close=self.fixture();close.iloc[20,5]=np.nan
        r=historical_stats(x,close,[5])
        self.assertEqual(r['summary']['5']['days'],55)
        self.assertEqual(sum(d['status']=='missing_forward_prices' for d in r['daily']),5)

    def test_constant_factor_and_small_cross_section_are_not_success(self):
        x,close=self.fixture();x[:]=1
        self.assertEqual(historical_stats(x,close,[5])['summary']['5']['days'],0)
        self.assertEqual(historical_stats(x.iloc[:,:10],close.iloc[:,:10],[5])['summary']['5']['days'],0)


if __name__=='__main__':unittest.main()
