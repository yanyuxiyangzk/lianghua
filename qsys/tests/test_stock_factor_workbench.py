import tempfile,sqlite3
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from contextlib import ExitStack
import numpy as np
import pandas as pd
import datasource
import stock_factor_workbench as w
import execution_constraints as ec

class Tests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.s=ExitStack();self.addCleanup(self.s.close)
        for obj,key,v in [(w,'DATA_DIR',root),(w,'DB',root/'research.db'),(datasource,'MKT_DB',root/'market.db'),(ec,'DB',root/'constraints.db')]:
            self.s.enter_context(patch.object(obj,key,v))
        days=pd.bdate_range('2025-01-01',periods=180)
        with datasource._conn() as c:
            for i,day in enumerate(days):
                price=10+np.sin(i/7)+i*.01
                c.execute('INSERT INTO market_daily(source,code,date,open,high,low,close,volume,amount) VALUES(?,?,?,?,?,?,?,?,?)',('ths_ifind','SH600664',str(day.date()),price,price+1,price-1,price,1000+i,price*(1000+i)))
            for code in ('SH600664','SH603893'):
                c.execute('INSERT INTO ifind_minute(code,datetime,open,high,low,close,volume,amount) VALUES(?,?,?,?,?,?,?,?)',(code,'2025-05-01 10:00:00',10,10,10,10,100,1000))
        self.exp=w.mine('SH600664','2025-01-01','2025-12-01')
    def test_snapshot_replay_and_targeted_cleanup(self):
        candidate=next(c['name'] for c in self.exp['candidates'] if c['status']=='research_candidate')
        r=w.backtest(self.exp['id'],candidate)
        self.assertEqual(r['data_quality_status'],'incomplete')
        self.assertEqual(r['trades'],0)
        original=w.get_result(r['id'],'reports')
        w.cleanup('SH600664','2025-01-01',self.exp['end'],r['id'],True)
        with datasource._conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM market_daily WHERE code='SH600664'").fetchone()[0],180)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ifind_minute WHERE code='SH600664'").fetchone()[0],1)  # incomplete, unarchived raw data is protected
            self.assertEqual(c.execute("SELECT COUNT(*) FROM ifind_minute WHERE code='SH603893'").fetchone()[0],1)
        self.assertEqual(w.get_result(r['id'],'reports'),original)
        self.assertEqual(w.backtest(self.exp['id'],candidate)['nav'],r['nav'])
        with self.assertRaises(ValueError):w.cleanup('SH603893','2025-01-01',self.exp['end'],r['id'])
    def test_missing_orderbook_not_substituted_with_daily(self):
        with self.assertRaisesRegex(ValueError,'没有可用候选'):
            w.mine('SH600664','2025-01-01','2025-12-01','盘口日内特征')
    def test_view_and_buttons(self):
        from streamlit.testing.v1 import AppTest
        a=AppTest.from_string("from stock_factor_view import render\nrender('SH600664','backtest')").run()
        self.assertFalse(a.exception)
        next(b for b in a.button if b.label=='运行单股因子回测').click().run()
        self.assertFalse(a.exception)
        self.assertTrue(any('排队中，尚未开始' in x.value for x in a.info))
    def test_inventory_row_actions_navigate_to_workbench(self):
        from streamlit.testing.v1 import AppTest
        import ast
        source=Path('/app/views/p_stock_history.py').read_text()
        module=ast.parse(source)
        functions='\n\n'.join(ast.get_source_segment(source,n) for n in module.body if isinstance(n,ast.FunctionDef) and n.name in ('_go','_render_stock_rows'))
        app_source='import streamlit as st\nimport pandas as pd\n'+functions+'''
if st.session_state.get('stock_history_view','list')=='list':
    _render_stock_rows(pd.DataFrame([dict(code='SH600664',name='fixture',daily_days=180,daily_start='2025-01-01',daily_end='2025-09-01',minute_days=1,minute_rows=200,minute_start='2025-05-01',minute_end='2025-05-01')]))
else:
    st.write(st.session_state['stock_history_view'])
'''
        for label,view in [('因子挖掘','factor_mine'),('因子回测','factor_backtest'),('清理高频','factor_cleanup')]:
            app=AppTest.from_string(app_source).run()
            next(b for b in app.button if b.label==label).click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['stock_history_view'],view)
            self.assertEqual(app.session_state['stock_history_code'],'SH600664')
    def test_mining_progress_finishes_only_after_save(self):
        events=[]
        result=w.mine('SH600664','2025-01-01','2025-12-01',progress=lambda p,m:events.append((p,m)))
        self.assertEqual(events[-1][0],100)
        self.assertEqual([p for p,m in events],sorted(p for p,m in events))
        self.assertEqual(w.get_result(result['id'],'experiments')['research_type'],'fixed-feature-evaluation')
        events=[]
        with patch.object(w,'save',side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError):
                w.mine('SH600664','2025-01-01','2025-12-01',progress=lambda p,m:events.append((p,m)))
        self.assertNotIn(100,[p for p,m in events])

    def test_snapshot_review_blocks_unverified_calendar(self):
        import research_retention as rr
        rr.setup()
        r=w.review_snapshot(self.exp['id'])
        self.assertTrue(all(c['status']=='sample_insufficient' for c in r['candidates']))
        with datasource._conn() as c:
            dates=[r[0] for r in c.execute("SELECT date FROM market_daily WHERE code='SH600664' ORDER BY date")]
            import json
            c.executemany("INSERT OR IGNORE INTO ifind_calendar VALUES('SSE',?)",[(d,) for d in dates])
            c.execute('INSERT INTO research_calendar_receipts VALUES(?,?,?,?,?,?)',('SSE',dates[0],dates[-1],json.dumps(dates),rr._hash(dates),'2026-09-25'))
        a=w.review_snapshot(self.exp['id']);b=w.review_snapshot(self.exp['id'])
        self.assertEqual(a,b)
        self.assertTrue(all(c['holdout_samples']==50 for c in a['candidates']))
    def test_local_panel_never_fetches_network(self):
        import signals,os
        root=Path(self.tmp.name)
        with patch.dict(os.environ,{'QSYS_RESEARCH_LOCAL_ONLY':'1'}),patch.object(signals,'CACHE_DIR',root/'cache'),patch.object(signals,'fetch_panel',side_effect=AssertionError('network path')):
            panel=signals.get_panel_cached(['SH600664'],'2025-12-01',800,source='ths_ifind')
        self.assertEqual(len(panel),180)
        self.assertEqual(panel.index.names,['instrument','datetime'])
