import sys, sqlite3, unittest, types, tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import selection_gate as gate

class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'market.db'
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.executescript('''
        CREATE TABLE stock_industry(code TEXT, sector_label TEXT, sector_name TEXT, source TEXT, updated_at TEXT);
        CREATE TABLE sector_daily(date TEXT, sector_name TEXT);
        CREATE TABLE ifind_financial(code TEXT, report_date TEXT, indicator TEXT, value REAL, fetched_at TEXT);
        CREATE TABLE ifind_realtime(code TEXT, datetime TEXT, price REAL);
        ''')
        p = patch.dict(sys.modules, {'datasource':types.SimpleNamespace(MKT_DB=self.path)})
        p.start(); self.addCleanup(p.stop)
        self.now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
        self.day = self.now[:10]

    def test_missing_database_not_created(self):
        self.db.close()
        self.path.unlink()
        self.assertIsNone(gate._query('SELECT 1'))
        self.assertFalse(self.path.exists())

    def test_query_cannot_write(self):
        self.assertIsNone(gate._query("CREATE TABLE unexpected(value TEXT)"))
        self.assertIsNone(self.db.execute(
            "SELECT name FROM sqlite_master WHERE name='unexpected'").fetchone())

    def test_query_closes_connection(self):
        opened = []
        connect = sqlite3.connect
        def track(*args, **kwargs):
            c = connect(*args, **kwargs)
            opened.append(c)
            return c
        with patch.object(gate.sqlite3, 'connect', side_effect=track):
            self.assertEqual(gate._query('SELECT 1'), (1,))
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')

    def seed(self):
        self.db.execute('INSERT INTO stock_industry VALUES (?,?,?,?,?)', ('SZ002709','x','化工','fixture',self.now))
        self.db.execute('INSERT INTO sector_daily VALUES (?,?)', (self.day,'化工'))
        self.db.execute('INSERT INTO ifind_financial VALUES (?,?,?,?,?)', ('002709',self.day,'净利润',10,self.now))
        self.db.execute('INSERT INTO ifind_realtime VALUES (?,?,?)', ('SZ002709',self.now,10))

    def test_real_schema_and_code_format_pass(self):
        self.seed()
        self.assertEqual(gate.evaluate_candidate('SZ002709', {'regime_scope':'all'}, 'bull').status, 'pass')

    def test_missing_data_blocks(self):
        self.assertEqual(gate.evaluate_candidate('SZ002709', {'regime_scope':'all'}, 'bull').status, 'insufficient_data')

    def test_all_scope_list(self):
        self.assertEqual(gate.check_market_state({'regime_scope':['all']}, 'bull').status, 'pass')

    def test_unknown_market_blocks(self):
        self.assertEqual(gate.check_market_state({'regime_scope':'all'}, 'unknown').status, 'insufficient_data')

    def test_future_financial_not_visible(self):
        self.seed()
        self.db.execute("UPDATE ifind_financial SET fetched_at='2099-01-01'")
        self.assertEqual(gate.check_financial('SZ002709').status, 'insufficient_data')

    def test_future_intraday_financial_rejected(self):
        self.seed()
        from datetime import timedelta
        future=(datetime.now(ZoneInfo('Asia/Shanghai'))+timedelta(seconds=30)).isoformat()
        self.db.execute('UPDATE ifind_financial SET fetched_at=?',(future,))
        self.assertEqual(gate.check_financial('SZ002709').status,'insufficient_data')

    def test_future_intraday_sector_rejected(self):
        self.seed()
        from datetime import timedelta
        future = (datetime.now(ZoneInfo('Asia/Shanghai')) + timedelta(seconds=30)).isoformat()
        self.db.execute('UPDATE stock_industry SET updated_at=?', (future,))
        self.assertEqual(gate.check_sector('SZ002709').status, 'insufficient_data')

    def test_historical_asof_uses_shanghai_date(self):
        self.seed()
        self.db.execute("UPDATE ifind_financial SET report_date='2020-01-01', fetched_at='2020-01-02T20:00:00+00:00'")
        self.db.execute("UPDATE stock_industry SET updated_at='2020-01-02T20:00:00+00:00'")
        self.db.execute("UPDATE sector_daily SET date='2020-01-02'")
        self.assertEqual(gate.check_financial('SZ002709', asof='2020-01-02').status, 'insufficient_data')
        self.assertEqual(gate.check_sector('SZ002709', asof='2020-01-02').status, 'insufficient_data')
        self.assertEqual(gate.check_financial('SZ002709', asof='2020-01-03').status, 'pass')
        self.assertEqual(gate.check_sector('SZ002709', asof='2020-01-03').status, 'pass')

    def test_nonfinite_financial_rejected(self):
        self.seed()
        for value in (float('inf'), -float('inf'), 'invalid'):
            self.db.execute('UPDATE ifind_financial SET value=?', (value,))
            self.assertEqual(gate.check_financial('SZ002709').status, 'insufficient_data')

    def test_invalid_price_and_stale_time(self):
        self.seed()
        self.db.execute('UPDATE ifind_realtime SET price=-1')
        self.assertEqual(gate.check_technical('SZ002709').status, 'reject')
        self.db.execute("UPDATE ifind_realtime SET price=10, datetime='2000-01-01'")
        self.assertEqual(gate.check_technical('SZ002709').status, 'reject')

if __name__ == '__main__': unittest.main()
