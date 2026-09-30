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
