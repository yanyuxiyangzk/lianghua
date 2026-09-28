"""Invalid replay data must preserve existing NAV records."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
TMP = tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR'] = TMP.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
import experience
import broker
import datasource
import trading_calendar

class Tests(unittest.TestCase):
    def setUp(self):
        p = patch.object(trading_calendar, 'calendar_data', return_value=(
            {'2026-09-23'}, [('2026-09-01','2026-09-30')]))
        p.start()
        self.addCleanup(p.stop)
        with experience._conn() as c:
            c.executescript(experience._NAV_SCHEMA)
            c.execute('DELETE FROM account_nav_daily')
            c.execute("INSERT INTO account_nav_daily VALUES ('2026-09-23',100,100,0,1,0,0,'fixture')")
        self.fills = pd.DataFrame([dict(date='2026-09-23', ts='2026-09-23 10:00:00',
            code='SZ002709', side='buy', amount=10., fee=0., tax=0., shares=1)])
        self.flows = pd.DataFrame([dict(ts='2026-09-23 09:00:00', type='初始入金', amount=100.)])

    def test_invalid_market_data_preserves_nav(self):
        frames = [pd.DataFrame(columns=['code','date','close'])]
        frames += [pd.DataFrame([dict(code=code, date='2026-09-23', close=value)])
                   for code, value in [('SH600000',10.), ('SZ002709',float('nan')),
                                       ('SZ002709',float('inf')), ('SZ002709',0.)]]
        for frame in frames:
            with self.subTest(frame=frame), patch.object(broker, '_conn', return_value=MagicMock()), patch.object(datasource, '_conn', return_value=MagicMock()), patch.object(experience.pd, 'read_sql', side_effect=[self.fills, self.flows.copy(), frame]):
                with self.assertRaises(ValueError):
                    experience.rebuild_nav_history()
            with experience._conn() as c:
                self.assertEqual(c.execute('SELECT date,total_assets,nav FROM account_nav_daily').fetchall(),
                                 [('2026-09-23',100.,1.)])

    def test_valid_replay_writes_nav(self):
        frame = pd.DataFrame([dict(code='SZ002709',date='2026-09-23',close=9.)])
        with patch.object(broker, '_conn', return_value=MagicMock()), patch.object(datasource, '_conn', return_value=MagicMock()), patch.object(experience.pd, 'read_sql', side_effect=[self.fills,self.flows,frame]):
            self.assertEqual(experience.rebuild_nav_history(), 1)
        with experience._conn() as c:
            self.assertEqual(c.execute('SELECT total_assets,cash,position_mv FROM account_nav_daily').fetchone(), (99.,90.,9.))

    def test_non_session_deposit_and_historical_fill_are_not_lost(self):
        fills = pd.concat([self.fills, pd.DataFrame([dict(date='2026-09-25',ts='2026-09-25 10:00:00',
            code='SZ002709',side='sell',amount=9.,fee=1.,tax=0.,shares=1)])], ignore_index=True)
        flows = pd.concat([self.flows, pd.DataFrame([dict(ts='2026-09-25 09:00:00',type='入金',amount=50.)])],ignore_index=True)
        prices = pd.DataFrame([dict(code='SZ002709',date=d,close=9.) for d in ['2026-09-23','2026-09-28']])
        from datetime import datetime
        class ClosedTime(datetime):
            @classmethod
            def now(cls): return cls(2026,9,28,16)
        with patch.object(experience,'datetime',ClosedTime), patch.object(trading_calendar,'calendar_data',return_value=(
                {'2026-09-23','2026-09-28'},[('2026-09-01','2026-09-30')])), \
                patch.object(broker,'_conn',return_value=MagicMock()), patch.object(datasource,'_conn',return_value=MagicMock()), \
                patch.object(experience.pd,'read_sql',side_effect=[fills,flows,prices]):
            self.assertEqual(experience.rebuild_nav_history(),2)
        with experience._conn() as c:
            rows = c.execute('SELECT date,total_assets,cash,position_mv,daily_ret FROM account_nav_daily ORDER BY date').fetchall()
        self.assertEqual(rows[-1][:4],('2026-09-28',148.,148.,0.))
        self.assertAlmostEqual(rows[-1][4],-1/99,places=6)

    def test_missing_held_price_on_calendar_session_preserves_original(self):
        prices = pd.DataFrame([dict(code='SZ002709',date=d,close=9.) for d in ['2026-09-23','2026-09-25']])
        with patch.object(trading_calendar,'calendar_data',return_value=(
                {'2026-09-23','2026-09-24'},[('2026-09-01','2026-09-30')])), \
                patch.object(broker,'_conn',return_value=MagicMock()), patch.object(datasource,'_conn',return_value=MagicMock()), \
                patch.object(experience.pd,'read_sql',side_effect=[self.fills,self.flows,prices]):
            with self.assertRaisesRegex(ValueError,'缺少有效价格'):
                experience.rebuild_nav_history()
        with experience._conn() as c:
            self.assertEqual(c.execute('SELECT date,total_assets FROM account_nav_daily').fetchall(),[('2026-09-23',100.)])

if __name__ == '__main__': unittest.main()
