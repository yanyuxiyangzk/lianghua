import sys, sqlite3, unittest, types
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import selection_gate as gate

class GateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript('''
        CREATE TABLE stock_industry(code TEXT, sector_label TEXT, sector_name TEXT, source TEXT, updated_at TEXT);
        CREATE TABLE sector_daily(date TEXT, sector_name TEXT);
        CREATE TABLE ifind_financial(code TEXT, report_date TEXT, indicator TEXT, value REAL, fetched_at TEXT);
        CREATE TABLE ifind_realtime(code TEXT, datetime TEXT, price REAL);
        ''')
        p = patch.dict(sys.modules, {'datasource':types.SimpleNamespace(_qconn=lambda:self.db)})
        p.start(); self.addCleanup(p.stop)
        self.now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
        self.day = self.now[:10]

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

    def test_unknown_market_blocks(self):
        self.assertEqual(gate.check_market_state({'regime_scope':'all'}, 'unknown').status, 'insufficient_data')

    def test_future_financial_not_visible(self):
        self.seed()
        self.db.execute("UPDATE ifind_financial SET fetched_at='2099-01-01'")
        self.assertEqual(gate.check_financial('SZ002709').status, 'insufficient_data')

    def test_invalid_price_and_stale_time(self):
        self.seed()
        self.db.execute('UPDATE ifind_realtime SET price=-1')
        self.assertEqual(gate.check_technical('SZ002709').status, 'reject')
        self.db.execute("UPDATE ifind_realtime SET price=10, datetime='2000-01-01'")
        self.assertEqual(gate.check_technical('SZ002709').status, 'reject')

if __name__ == '__main__': unittest.main()
