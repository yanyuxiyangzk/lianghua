import sqlite3,tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import daily_integrity as d

class IntegrityTests(TestCase):
    def test_internal_hole_invalid_bar_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(d,'MARKET',Path(tmp)/'market.db'),patch.object(d,'DB',Path(tmp)/'repair.db'):
            with sqlite3.connect(d.MARKET) as c:
                c.executescript('CREATE TABLE ifind_calendar(exchange,date); CREATE TABLE market_daily(source,code,date,open,high,low,close,volume,amount);')
                c.executemany('INSERT INTO ifind_calendar VALUES (?,?)',[('SSE',x) for x in ['2026-01-02','2026-01-05','2026-01-06']])
                c.executemany('INSERT INTO market_daily VALUES (?,?,?,?,?,?,?,?,?)',[
                    ('ths_ifind','A','2026-01-02',10,11,9,10,100,1000),
                    ('ths_ifind','A','2026-01-06',10,11,9,10,None,1000)])
            self.assertEqual(d.missing_dates('A','2026-01-02','2026-01-06'),['2026-01-05','2026-01-06'])
            old=d.revision(['A'],'ths_ifind');d.bump_revision('A')
            self.assertNotEqual(old,d.revision(['A'],'ths_ifind'))
            self.assertEqual(d.revision(['A'],'other'),'')

    def test_confirmed_suspension_is_explained_not_filled(self):
        import execution_constraints as ec
        with tempfile.TemporaryDirectory() as tmp, patch.object(ec,'DB',Path(tmp)/'constraints.db'), patch.object(d,'raw_missing_dates',return_value=['2025-03-21','2025-03-24']):
            with ec.connect() as c:
                c.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?)',('ths_ifind','A','2025-03-21','suspended',1.,'确认停牌','now'))
                c.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?)',('ths_ifind','A','2025-03-24','suspended',None,'未知','now'))
            result=d.classify_gaps('A','2025-03-21','2025-03-24')
            self.assertEqual(result['unresolved'],['2025-03-24'])
            self.assertEqual(len(result['raw_missing']),2)
            self.assertEqual(result['confirmed_suspensions'][0]['date'],'2025-03-21')

    def test_provider_status_whitelist(self):
        from execution_constraints import normalize
        for text in ['停牌','重要公告，停牌自2025-03-19起连续停牌','重大不确定性的事项，停牌1天']:
            self.assertEqual(normalize('suspended',text),1.)
        for text in ['临时停牌10分钟','复牌','未知','不停牌','盘中停牌']:
            self.assertIsNone(normalize('suspended',text))
