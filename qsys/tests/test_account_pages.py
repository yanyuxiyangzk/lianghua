"""Render account pages against isolated stale-quote fixtures, without API calls."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime

TMP = tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR'] = TMP.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import broker
import datasource
import experience
from streamlit.testing.v1 import AppTest


class Clock(datetime):
    @classmethod
    def now(cls, tz=None): return cls(2026,9,28,10)


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Environment variables do not relocate modules imported by another
        # test first. Bind every fixture database explicitly before any write.
        import common
        for target, name, path in [(broker,'DB_PATH',Path(TMP.name)/'experience.db'),
                                   (experience,'DB_PATH',Path(TMP.name)/'experience.db'),
                                   (datasource,'MKT_DB',Path(TMP.name)/'market.db'),
                                   (common,'DATA_DIR',Path(TMP.name))]:
            cls.enterClassContext(patch.object(target,name,path))
        broker._init_account()
        with broker._conn() as c:
            c.execute("UPDATE broker_cashflows SET ts='2026-09-23 09:00:00'")
            c.execute("INSERT INTO broker_positions(code,name,source,shares,sellable,cost,last_buy_date) VALUES ('SZ002709','天赐材料','manual',100,100,10,'2026-09-23')")
            c.execute("UPDATE broker_account SET value='199000' WHERE key='cash'")
        with experience._conn() as c:
            c.executescript(experience._NAV_SCHEMA)
            c.execute("INSERT INTO account_nav_daily VALUES ('2026-09-24',200000,199000,1000,1,0,0,'test')")
        with datasource._qconn() as c:
            c.execute("INSERT INTO ifind_realtime(code,datetime,price,prev_close) VALUES ('SZ002709','2026-09-25 15:05:01',9,10)")

    def test_pages_render_unknown_daily_pnl_without_nan_or_fake_zero(self):
        calendar = ({'2026-09-23','2026-09-24','2026-09-28'}, [('2026-09-01','2026-09-30')])
        for page in ('p_broker.py','p_dash.py'):
            with self.subTest(page=page), patch('trading_calendar.calendar_data',return_value=calendar), \
                    patch.object(broker,'datetime',Clock), \
                    patch.object(datasource,'get_daily',side_effect=AssertionError('No network fallback')):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1]/'views'/page),default_timeout=30).run()
                self.assertFalse(app.exception, [e.message for e in app.exception])
                self.assertFalse(app.error, [e.value for e in app.error])
                if page == 'p_broker.py':
                    value = next(m.value for m in app.metric if m.label=='今日盈亏')
                    self.assertEqual(value,'待更新')
                else:
                    self.assertTrue(any('待更新' in m.value and '今日盈亏' in m.value for m in app.markdown))

    def test_auto_mode_has_no_review_checkbox_and_can_pause(self):
        import account_controls as controls
        calendar = ({'2026-09-23','2026-09-24','2026-09-28'}, [('2026-09-01','2026-09-30')])
        try:
            with patch('trading_calendar.calendar_data',return_value=calendar),patch.object(broker,'datetime',Clock):
                controls.set_automatic(True)
                app=AppTest.from_string('from account_controls_view import render\nrender()',default_timeout=30).run()
                self.assertFalse(app.exception)
                self.assertTrue(any('无需逐笔审核' in s.value for s in app.success))
                self.assertEqual(len(app.checkbox),0)
                self.assertFalse(any(b.label=='确认提交模拟降仓' for b in app.button))
                next(b for b in app.button if b.label=='暂停自动降仓（撤销未成交自动卖单）').click().run()
                self.assertFalse(controls.automatic_state()['enabled'])
                self.assertFalse(app.exception)
        finally:
            controls.set_automatic(False)

    def test_controls_generate_but_stale_quotes_cannot_submit(self):
        calendar = ({'2026-09-23','2026-09-24','2026-09-28'}, [('2026-09-01','2026-09-30')])
        with broker._conn() as c:c.execute('UPDATE broker_positions SET shares=10000,sellable=10000')
        try:
            with patch('trading_calendar.calendar_data',return_value=calendar),patch.object(broker,'datetime',Clock):
                app=AppTest.from_string('from account_controls_view import render\nrender()',default_timeout=30).run()
                next(b for b in app.button if b.label=='生成降仓方案（不下单）').click().run()
                self.assertFalse(app.exception)
                self.assertFalse(app.error)
                self.assertTrue(any('待审核' in m.value for m in app.markdown))
                self.assertTrue(next(b for b in app.button if b.label=='确认提交模拟降仓').disabled)
                app.checkbox[0].check().run()
                self.assertTrue(next(b for b in app.button if b.label=='确认提交模拟降仓').disabled)
            with broker._conn() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM broker_fills').fetchone()[0],0)
        finally:
            with broker._conn() as c:c.execute('UPDATE broker_positions SET shares=100,sellable=100')


if __name__ == '__main__': unittest.main()
