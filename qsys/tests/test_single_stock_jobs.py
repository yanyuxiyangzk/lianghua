from test_stock_factor_workbench import Tests as Fixtures
import single_stock_jobs as j
import stock_factor_workbench as w
import research_retention as rr
import datasource,json
from unittest import TestCase
from unittest.mock import patch

class JobsTests(TestCase):
    def setUp(self):
        self.fixture=Fixtures();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        rr.setup()
        with datasource._conn() as c:
            dates=[r[0] for r in c.execute("SELECT date FROM market_daily WHERE code='SH600664' ORDER BY date")]
            c.executemany("INSERT OR IGNORE INTO ifind_calendar VALUES('SSE',?)",[(d,) for d in dates])
            c.execute('INSERT INTO research_calendar_receipts VALUES(?,?,?,?,?,?)',('SSE',dates[0],dates[-1],json.dumps(dates),rr._hash(dates),'now'))
        self.p=dict(source='日线量价',start='2025-01-01',end='2025-12-01',budget=20,seed=42)
    def test_search_persisted_reproducible_and_backtest(self):
        rid=j.submit('SH600664','mine',self.p)
        self.assertEqual(j.submit('SH600664','mine',self.p),rid)
        self.assertTrue(j.run_once());self.assertEqual(j.jobs('SH600664')[0]['status'],'completed')
        exp=w.get_result(rid,'experiments');self.assertEqual(len(exp['trials']),20)
        again=j.mine('SH600664',self.p,lambda *a:None,'repeat')
        self.assertEqual(exp['trials'],again['trials']);self.assertEqual(exp['input_hash'],again['input_hash'])
        self.assertTrue(exp['candidates'])
        if exp['candidates']:
            with self.assertRaisesRegex(ValueError,'成交约束不完整'):
                w.backtest(rid,exp['candidates'][0]['name'])
            import execution_constraints as ec
            with ec.connect() as c:
                for day in [r['date'] for r in exp['inputs']][exp['split']:]:
                    for field,value in [('suspended',0),('limit_up',100),('limit_down',1)]:
                        c.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?)',('ths_ifind','SH600664',day,field,value,str(value),'now'))
            bid=j.submit('SH600664','backtest',dict(experiment_id=rid,candidate=exp['candidates'][0]['name']))
            j.run_once();row=next(x for x in j.jobs('SH600664') if x['id']==bid)
            self.assertEqual(row['status'],'completed')
            self.assertEqual(w.get_result(row['result_id'],'reports')['experiment_id'],rid)
    def test_missing_calendar_fails_without_substitution(self):
        with datasource._conn() as c:c.execute('DELETE FROM research_calendar_receipts')
        j.submit('SH600664','mine',self.p);j.run_once()
        self.assertEqual(j.jobs('SH600664')[0]['status'],'failed')
    def test_high_frequency_requires_verified_features(self):
        self.p['source']='盘口日内特征'
        j.submit('SH600664','mine',self.p);j.run_once()
        self.assertIn('不足150日',j.jobs('SH600664')[0]['message'])
    def test_page_submits_without_inline_execution(self):
        from streamlit.testing.v1 import AppTest
        from datetime import date
        app=AppTest.from_string("from stock_factor_view import render\nrender('SH600664','mine')").run()
        app.date_input[0].set_value(date(2025,1,1));app.date_input[1].set_value(date(2025,12,1))
        next(b for b in app.button if b.label=='开始单股真实挖掘').click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any('排队中，尚未开始' in x.value for x in app.info))
        self.assertEqual(j.jobs('SH600664')[0]['status'],'queued')
    def test_page_refreshes_completed_result_and_failure_without_resubmitting(self):
        from streamlit.testing.v1 import AppTest
        rid=j.submit('SH600664','mine',self.p)
        app=AppTest.from_string("from stock_factor_view import render\nrender('SH600664','mine')").run()
        self.assertFalse(app.exception)
        self.assertTrue(any('排队中，尚未开始' in x.value for x in app.info))
        j.run_once()
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any('已评估 20 个表达式' in x.value for x in app.success))
        self.assertEqual(next(x for x in app.selectbox if x.label=='研究批次').value,rid)
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(len(j.jobs('SH600664')),1)
        self.assertFalse(any('任务已提交' in x.value for x in app.success))
        self.p['source']='盘口日内特征'
        j.submit('SH600664','mine',self.p)
        app.run()
        j.run_once()
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any('不足150日' in x.value for x in app.error))
        self.assertEqual(len(j.jobs('SH600664')),2)

    def test_constraint_repair_job_records_unresolved_result(self):
        exp=self.fixture.exp
        rid=j.submit('SH600664','constraints',dict(experiment_id=exp['id']))
        import execution_constraints as ec
        with patch.object(ec,'repair_missing',return_value={'before':{},'after':{'complete':False,'missing_fields':2},'requests':[]}):
            j.run_once()
        row=next(r for r in j.jobs('SH600664') if r['id']==rid)
        self.assertEqual(row['status'],'failed')
        self.assertIn('仍缺 2',row['message'])
        with j.connect() as c:self.assertIsNotNone(c.execute('SELECT payload FROM single_constraint_repairs WHERE id=?',(rid,)).fetchone())

    def test_constraint_coverage_exact_dates(self):
        import execution_constraints as ec
        report=ec.coverage('SH600664',['2025-01-02','2025-01-03'])
        self.assertEqual(report['missing_fields'],6)
        with ec.connect() as c:
            for field,value in [('suspended',0),('limit_up',12),('limit_down',8)]:
                c.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?)',('ths_ifind','SH600664','2025-01-02',field,value,str(value),'now'))
        self.assertTrue(ec.coverage('SH600664',['2025-01-02'])['complete'])
        self.assertEqual(ec.coverage('SH600664',['2025-01-03'])['missing_fields'],3)
