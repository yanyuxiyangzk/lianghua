"""Calendar, daily P&L and NAV regressions; isolated databases only."""
import hashlib
import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime as RealDatetime
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR'] = TMP.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import broker
import datasource
import experience
import trading_calendar as calendar


class Clock(RealDatetime):
    value = RealDatetime(2026, 9, 28, 15, 5)

    @classmethod
    def now(cls, tz=None):
        return cls.value


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, name, value in [(datasource, 'MKT_DB', Path(self.tmp.name)/'market.db'),
                                    (broker, 'DB_PATH', Path(self.tmp.name)/'account.db'),
                                    (experience, 'DB_PATH', Path(self.tmp.name)/'account.db'),
                                    (broker, 'datetime', Clock), (experience, 'datetime', Clock)]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        Clock.value = RealDatetime(2026, 9, 28, 15, 5)
        dates = ['2026-09-23', '2026-09-24', '2026-09-28', '2026-09-29']
        raw = json.dumps(dates)
        with datasource._conn() as c:
            c.executemany("INSERT INTO ifind_calendar VALUES ('SSE',?)", [(d,) for d in dates])
            c.execute('CREATE TABLE research_calendar_receipts(exchange,start,end,dates_json,digest)')
            c.execute('INSERT INTO research_calendar_receipts VALUES (?,?,?,?,?)',
                      ('SSE','2026-09-23','2026-09-30', raw, hashlib.sha256(raw.encode()).hexdigest()))
        self.positions = pd.DataFrame([dict(code='SZ002709', shares=4600)])
        self.empty = pd.DataFrame(columns=['code','side','shares','amount','fee','tax'])
        self.quote = (31.76,32.84,32.,36.,29.,'2026-09-28 15:01:00')

    def pnl(self, quote=None, fills=None):
        return broker._day_pnl_by_code(self.positions, self.empty if fills is None else fills,
                                      {'SZ002709': self.quote if quote is None else quote})['SZ002709']

    def test_calendar_outside_coverage_never_permits_orders(self):
        self.assertIsNone(calendar.day_status('2026-10-01'))
        Clock.value = RealDatetime(2026,10,1,10)
        self.assertFalse(broker._market_open())

    def test_holiday_and_lunch_blocked_session_allowed(self):
        self.assertIs(calendar.day_status('2026-09-25'), False)
        self.assertIs(calendar.day_status('2026-09-28'), True)
        for day, hour, minute, expected in [(25,10,0,False),(28,10,0,True),(28,12,30,False)]:
            Clock.value = RealDatetime(2026,9,day,hour,minute)
            self.assertEqual(broker._market_open(), expected)

    def test_corrupt_receipt_cannot_certify_holiday(self):
        with datasource._conn() as c:
            c.execute("UPDATE research_calendar_receipts SET digest='corrupt'")
        self.assertIsNone(calendar.day_status('2026-09-25'))
        self.assertIsNone(calendar.previous_session('2026-09-28'))

    def test_holiday_and_weekend_do_not_repeat_old_loss(self):
        for day in (25,26,27):
            Clock.value = RealDatetime(2026,9,day,15,5)
            self.assertEqual(self.pnl(), 0.)

    def test_holiday_fill_is_flagged_not_hidden(self):
        Clock.value = RealDatetime(2026,9,25,15,5)
        fills = pd.DataFrame([dict(code='SZ002709',side='sell',shares=100,amount=3176.,fee=5.,tax=1.59)])
        self.assertTrue(math.isnan(self.pnl(fills=fills)))

    def test_stale_missing_preopen_and_future_quotes_are_unknown(self):
        for stamp in ('2026-09-25 15:05:01','2026-09-28 09:05:00','2026-09-28 15:06:00',None):
            self.assertTrue(math.isnan(self.pnl(self.quote[:5]+(stamp,))))
        self.assertTrue(math.isnan(self.pnl(self.quote[:5])))

    def test_current_quote_retains_correct_market_loss(self):
        self.assertAlmostEqual(self.pnl(), -4968.)

    def test_latest_prices_preserves_timestamp(self):
        with datasource._conn() as c:
            c.execute('INSERT INTO ifind_realtime(code,datetime,price,prev_close) VALUES (?,?,?,?)',
                      ('SZ002709',self.quote[5],31.76,32.84))
        self.assertEqual(broker._latest_prices(['SZ002709'])['SZ002709'][5], self.quote[5])

    def test_nav_missing_previous_session_rejects_interval_return(self):
        with experience._conn() as c:
            c.executescript(experience._NAV_SCHEMA)
            c.execute("INSERT INTO account_nav_daily VALUES ('2026-09-23',100,100,0,1,0,0,'test')")
        account = {'估值有效':True,'总资产':90.,'可用资金':90.,'收盘估值有效':True}
        with patch.object(broker,'get_account',return_value=account):
            with self.assertRaisesRegex(ValueError,'前一交易日'):
                experience.snapshot_nav_today()
        with experience._conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM account_nav_daily').fetchone()[0],1)

    def test_nav_includes_weekend_deposit_and_is_idempotent(self):
        with experience._conn() as c:
            c.executescript(experience._NAV_SCHEMA)
            c.execute("INSERT INTO account_nav_daily VALUES ('2026-09-24',100,100,0,1,0,0,'test')")
        with broker._conn() as c:
            c.execute("INSERT INTO broker_cashflows(ts,type,amount) VALUES ('2026-09-26 10:00:00','入金',50)")
        account = {'估值有效':True,'总资产':140.,'可用资金':140.,'收盘估值有效':True}
        with patch.object(broker,'get_account',return_value=account):
            experience.snapshot_nav_today()
            experience.snapshot_nav_today()
        with experience._conn() as c:
            nav, ret = c.execute("SELECT nav,daily_ret FROM account_nav_daily WHERE date='2026-09-28'").fetchone()
        self.assertAlmostEqual(nav,.9)
        self.assertAlmostEqual(ret,-.1)

    def test_nav_rejects_old_or_intraday_valuation(self):
        account = {'估值有效':True,'总资产':100.,'可用资金':100.,'收盘估值有效':False}
        with patch.object(broker,'get_account',return_value=account):
            with self.assertRaisesRegex(ValueError,'收盘行情'):
                experience.snapshot_nav_today()


if __name__ == '__main__':
    unittest.main()
