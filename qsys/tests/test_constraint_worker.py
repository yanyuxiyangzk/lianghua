import sqlite3,tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from contextlib import ExitStack
import execution_constraints as ec
import constraint_worker as w

class WorkerTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.stack=ExitStack();self.addCleanup(self.stack.close)
        for obj,key,val in [(ec,'DB',self.root/'constraints.db'),(w,'MARKET',self.root/'market.db'),(w,'LIVE',self.root/'status.json')]:
            self.stack.enter_context(patch.object(obj,key,val))
        with sqlite3.connect(w.MARKET) as c:
            c.executescript('CREATE TABLE market_daily(source,code,date); CREATE TABLE ifind_stocklist(code); CREATE TABLE ifind_indexlist(code);')
            c.executemany('INSERT INTO ifind_stocklist VALUES (?)',[('A',),('I',)])
            c.execute("INSERT INTO ifind_indexlist VALUES ('I')")
            c.executemany('INSERT INTO market_daily VALUES (?,?,?)',[('ths_ifind','A','2026-01-02'),('ths_ifind','I','2026-01-02')])
    def fetch(self,codes,start,end,fields):
        with ec.connect() as c:
            c.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?)',('ths_ifind',codes[0],start,fields[0],1.,'fixture','now'))
        return [dict(rows=1,known=1)]
    def test_plan_resume_and_increment(self):
        self.assertEqual(w.plan(),3)
        for _ in range(3): self.assertTrue(w.step(self.fetch))
        self.assertFalse(w.step(self.fetch));w.plan()
        self.assertFalse(w.step(self.fetch))
        with sqlite3.connect(w.MARKET) as c: c.execute("INSERT INTO market_daily VALUES ('ths_ifind','A','2026-01-05')")
        w.plan()
        for _ in range(3): self.assertTrue(w.step(self.fetch))
        self.assertFalse(w.step(self.fetch))
        self.assertEqual(w.snapshot()['stocks'],1)
    def test_failure_not_done_and_blocks_after_three(self):
        w.plan()
        for _ in range(9):
            w.step(lambda *a,**k:[{'error':'fixture missing'}])
            with w.connection() as c: c.execute('UPDATE backfill_tasks SET next_try=0')
        self.assertEqual(w.snapshot()['tasks'],{'blocked':3})
        self.assertFalse(w.step(self.fetch))
