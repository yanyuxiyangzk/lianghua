"""Provider failures stay visible without fabricating account or chart data."""
import ast
import os
import sqlite3
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

_ROOT = tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR'] = _ROOT.name

import datasource
import ifind_client as client
import broker


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026,9,30,20,0)


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path(__file__).resolve().parents[1]/'views/p_ifind_kline.py'
        tree=ast.parse(path.read_text())
        assert isinstance(tree.body[-1],ast.Expr) and tree.body[-1].value.func.id=='render'
        tree.body.pop()  # Import pure chart helpers without executing the page.
        cls.page=types.ModuleType('kline_test_helpers')
        exec(compile(tree,str(path),'exec'),cls.page.__dict__)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        for target,name,value in [(datasource,'MKT_DB',Path(self.tmp.name)/'market.db'),
                                  (broker,'DB_PATH',Path(self.tmp.name)/'account.db'),
                                  (self.page,'datetime',Clock),(broker,'datetime',Clock)]:
            p=patch.object(target,name,value);p.start();self.addCleanup(p.stop)
        with datasource._conn():pass

    def test_quota_error_is_preserved_without_sdk_retry(self):
        response=dict(errorcode=-4302,errmsg='quote quota exceeded')
        sdk=Mock(side_effect=AssertionError('Do not retry quota rejection via SDK'))
        result=client._sdk_or_http(sdk,lambda:(None,response,-4302))
        self.assertEqual(result[1],response);sdk.assert_not_called()

    def test_quota_status_is_shared_and_cleared_after_success(self):
        stored={}
        response=Mock()
        response.json.return_value=dict(errorcode=-4302,errmsg='quota exceeded')
        with patch.object(client,'_ths_access_token',return_value='test'), \
             patch.object(client,'_config_get',side_effect=stored.get), \
             patch.object(client,'_config_set',side_effect=lambda k,v:stored.update({k:v})), \
             patch('requests.post',return_value=response):
            client._ths_http('real_time_quotation',{})
            self.assertEqual(client.quote_service_status()['error'],-4302)
            self.assertNotIn('test',stored['quote_service_status'])
            response.json.return_value=dict(errorcode=0,tables=[])
            client._ths_http('real_time_quotation',{})
            self.assertEqual(client.quote_service_status(),{})

    def test_bj_daily_error_keeps_reason_without_second_online_request(self):
        response=dict(errorcode=-4302,errmsg='quota exceeded')
        with patch.object(datasource,'ths_history',return_value=(None,response,-4302)) as fetch:
            with self.assertRaisesRegex(RuntimeError,'额度已超限.*本地尚无'):
                self.page._load_kline('920202.BJ','日K')
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(fetch.call_args.args[0],['BJ920202'])
        self.assertEqual(client._to_ths_code('BJ920202'),'920202.BJ')

    def test_existing_chart_cache_remains_visible_with_stale_warning(self):
        with datasource._conn() as c:
            c.execute("INSERT INTO market_daily(source,code,date,open,high,low,close,volume,amount) VALUES ('ths_ifind','BJ920202','2026-09-28',10,11,9,10,100,1000)")
        with patch.object(datasource,'_ths_fetch_daily',side_effect=RuntimeError('额度已超限 -4302')):
            for period in ['日K','周K']:
                frame=self.page._load_kline('920202.BJ',period)
                self.assertFalse(frame.empty)
                self.assertIn('本地缓存',frame.attrs['data_warning'])
        with datasource._conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM market_daily').fetchone()[0],1)

    def test_empty_success_fallback_preserves_error_code(self):
        with patch.object(self.page,'_local_daily',return_value=pd.DataFrame()), \
             patch.object(datasource,'ths_history',return_value=(None,{'errmsg':'explicit provider error'},-123)):
            with self.assertRaisesRegex(RuntimeError,'-123.*explicit provider error'):
                self.page._load_kline('920202.BJ','日K')

    def test_account_identifies_stale_stock_and_keeps_daily_pnl_unknown(self):
        broker._init_account()
        with broker._conn() as c:
            c.execute("INSERT INTO broker_positions(code,shares,cost) VALUES ('SH601658',300,5)")
        quote=(5.41,5.32,5.33,5.85,4.79,'2026-09-29 15:05:50')
        with patch.object(broker,'day_status',return_value=True), \
             patch.object(broker,'_latest_prices',return_value={'SH601658':quote}):
            account=broker.get_account()
        self.assertFalse(account['日盈亏有效'])
        self.assertTrue(pd.isna(account['今日盈亏']))
        self.assertEqual(account['日盈亏缺失行情'],[dict(code='SH601658',latest_quote_at=quote[5])])


if __name__=='__main__':unittest.main()
