import unittest
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from test_strategy_execution import IntegrationTests
from execution_detail_view import daily_table


class DetailTests(unittest.TestCase):
    def test_daily_rows_reconcile(self):
        report = IntegrationTests().run_fixture()
        frame = daily_table(report)
        self.assertEqual(len(frame), len(report['nav']))
        self.assertAlmostEqual(frame['当日盈亏'].sum(), report['equity'] - 200000)
        self.assertAlmostEqual(frame['当日费用'].sum(), sum(r['fee']+r['tax'] for r in report['execution']['fills']))
        self.assertEqual(frame['成交笔数'].sum(), report['trades'])
        self.assertEqual(frame['拒单笔数'].sum(), report['rejected_orders'])

    def test_page_run_filter_and_details(self):
        report = IntegrationTests().run_fixture()
        with patch('library.list_strategies', return_value={'fixture': {'factors': [{'name':'a'}]}}), patch('strategy_backtest.backtest_strategy', return_value=report):
            app = AppTest.from_string('from execution_detail_view import render\nrender()').run()
            self.assertFalse(app.exception)
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertGreater(len(app.dataframe), 0)
            dates = report['nav_dates']
            app.select_slider[0].set_value((dates[1], dates[-1])).run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.dataframe[0].value), len(dates)-1)
            self.assertGreater(len(app.expander), 0)

    def test_failure_clears_previous_report(self):
        report = IntegrationTests().run_fixture()
        with patch('library.list_strategies', return_value={'fixture': {'factors':[{'name':'a'}]}}), patch('strategy_backtest.backtest_strategy', return_value={'ok':False,'msg':'缺少行情'}):
            app = AppTest.from_string('from execution_detail_view import render\nrender()')
            app.session_state['execution_detail_report'] = report
            app.run().button[0].click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.dataframe), 0)
            self.assertIn('缺少行情', app.error[0].value)
