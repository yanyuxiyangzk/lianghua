import os,tempfile,unittest
os.environ.setdefault('QSYS_DATA_DIR',tempfile.mkdtemp(prefix='eval-integrity-'))
from unittest.mock import patch
import pandas as pd
import numpy as np
import factor_eval as fe

class IntegrityTests(unittest.TestCase):
    def fixture(self):
        dates=pd.bdate_range('2025-01-01',periods=400)
        idx=pd.MultiIndex.from_product([dates,['a','b']],names=['datetime','instrument'])
        panel=pd.DataFrame({'$close':np.arange(len(idx))+100.},index=idx)
        vals=pd.Series(np.arange(len(idx)),index=idx)
        return dates,panel,vals

    def test_empty_training_retains_oos_and_reason(self):
        dates,panel,vals=self.fixture()
        factor={'name':'test','kind':'builtin'}
        recent=pd.Series(.03,index=dates[-30:])
        with patch.object(fe.sig,'get_panel_cached',return_value=panel), patch.object(fe,'get_factor_values',return_value=vals), patch.object(fe,'ic_series',return_value=recent), patch('loopengine.tree.build_field_frames',return_value={}):
            result=fe.build_scorecard_batch([factor],['a','b'],'2026-09-24',source='fixture',train_end='2024-01-01')
        row=result.iloc[0]
        self.assertEqual(row['评估状态'],'sample_insufficient')
        self.assertEqual(row['OOS天数'],30)
        self.assertIn('预选窗内无数据',row['评估原因'])

    def test_training_returns_cannot_use_holdout_prices(self):
        dates,panel,vals=self.fixture()
        boundary=dates[300].strftime('%Y-%m-%d')
        seen=[]
        def top(v,p,**kw):
            seen.append(p.index.get_level_values('datetime').max())
            return .5
        with patch.object(fe.sig,'get_panel_cached',return_value=panel), patch.object(fe,'get_factor_values',return_value=vals), patch.object(fe,'ic_series',return_value=pd.Series(np.linspace(.01,.05,400),index=dates)), patch('loopengine.tree.build_field_frames',return_value={}), patch('common.trade_day_offset',return_value=dates[295].strftime('%Y-%m-%d')), patch.object(fe,'top_group_winrate',side_effect=top):
            result=fe.build_scorecard_batch([{'name':'test','kind':'builtin'}],['a','b'],'2026-09-24',source='fixture',train_end=boundary)
        self.assertGreater(len(seen),0)
        self.assertTrue(all(d<=pd.Timestamp(boundary) for d in seen))
        self.assertEqual(result.iloc[0]['天数'],296)

    def test_scorecard_persistence_status_and_version(self):
        import library
        import sqlite3
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'market.db'
            def conn():
                c=sqlite3.connect(db)
                c.execute('''CREATE TABLE IF NOT EXISTS factor_scorecards(
                name TEXT,pool_name TEXT,eval_date TEXT,kind TEXT,ic_mean REAL,icir REAL,
                ic_winrate REAL,top_winrate REAL,direction TEXT,days INTEGER,winrates TEXT,
                updated_at TEXT,ic_oos REAL,icir_oos REAL,oos_days INTEGER,
                factor_version TEXT,evaluation_status TEXT,evaluation_reason TEXT,policy_version TEXT,
                PRIMARY KEY(name,pool_name,eval_date))''')
                return c
            with patch.object(library,'_lconn',side_effect=conn):
                library.save_scorecard(pd.DataFrame([{'因子':'good','IC均值':.03,'ICIR':.4,'天数':300,'factor_version':'v1'},
                    {'因子':'empty','天数':0,'IC均值':np.nan,'ICIR':np.nan,'建议方向':'预选窗内无数据'}]),'p','2026-09-24')
            with sqlite3.connect(db) as c:
                rows=dict(c.execute('select name,evaluation_status from factor_scorecards'))
                self.assertEqual(rows,{'good':'valid','empty':'sample_insufficient'})
                self.assertEqual(c.execute("select factor_version from factor_scorecards where name='good'").fetchone()[0],'v1')

    def test_modified_factor_oos_starts_after_version_date(self):
        ic=pd.Series(.02,index=pd.bdate_range('2026-01-01',periods=100))
        result=fe._oos_stats(ic,'2027-01-01','2025-09-01',engine_selected=True)
        self.assertEqual(result['OOS天数'],0)

    def test_parallel_worker_preserves_sample_diagnosis(self):
        dates,panel,vals=self.fixture()
        with patch.object(fe,'ic_series',return_value=pd.Series(.02,index=dates[-30:])):
            result=fe._eval_single_factor(({'name':'test','kind':'builtin'},vals,panel,
                {fe.MAIN_FWD:vals},'2024-01-01'))
        self.assertEqual(result['评估状态'],'sample_insufficient')
        self.assertEqual(result['OOS天数'],30)

    def test_definition_change_archives_and_invalidates_previous_score(self):
        import library,datasource,factor_evaluation_queue as q
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp, patch.object(datasource,'MKT_DB',Path(tmp)/'market.db'), patch.object(q,'DB',Path(tmp)/'queue.db'), patch('theory_policy.associate',return_value={'theory_id':'momentum'}):
            f={'name':'fixture','kind':'manual','code':'x=1'}
            library.sync_factor_registry([f])
            library.save_scorecard(pd.DataFrame([{'因子':'fixture','IC均值':.03,'ICIR':.4,'天数':300,'factor_version':'old'}]),'p','2026-09-24')
            library.sync_factor_registry([{**f,'kind':'evolved','code':'x=2'}])
            with library._lconn() as c:
                self.assertEqual(c.execute("SELECT evaluation_status,icir FROM factor_scorecards").fetchone(),('stale_version',None))
                self.assertEqual(c.execute("SELECT icir FROM factor_scorecard_archive").fetchone()[0],.4)
                self.assertEqual(c.execute("SELECT kind,validation_status FROM factor_registry WHERE name='fixture'").fetchone(),('evolved','shadow_only'))
