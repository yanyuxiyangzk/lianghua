import os,sys,tempfile,unittest,types
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
TMP=tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR']=TMP.name
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
import mining_policy as mp
class Clock:
    day=24
    @classmethod
    def now(cls,tz=None): return datetime(2026,9,cls.day,19,30,tzinfo=tz)
class Engine:
    calls=[]
    def __init__(self,*a):pass
    def _frames(self,ft):
        f=pd.DataFrame({'a':[1.,2.]})
        self._last_extra_frames={'extra':f}
        return f,{'a':f},['x'],'2026-09-24'
    def run_round(self,**kw):
        self.calls.append(kw)
        return {'passed':0}
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        Clock.day=24;Engine.calls=[]
        for p in (patch.object(mp,'DB',Path(self.tmp.name)/'runs.db'),patch.object(mp,'LOCK',Path(self.tmp.name)/'lock'),patch.object(mp,'datetime',Clock),patch.dict(sys.modules,{'loopengine.engine':types.SimpleNamespace(LoopEngine=Engine,DEFAULT_FACTOR_TYPES=['量价','财务'])})):
            p.start();self.addCleanup(p.stop)
    def test_daily_limit_and_type_data_dedup(self):
        self.assertIn('完成',mp.run_daily('fixture',15))
        self.assertEqual([c['factor_type'] for c in Engine.calls],['量价','财务'])
        self.assertTrue(all(c['batch']==15 and not c['include_events'] for c in Engine.calls))
        self.assertIn('今日已',mp.run_daily('fixture'))
        Clock.day=25
        self.assertIn('数据未变',mp.run_daily('fixture'))
        self.assertEqual(len(Engine.calls),2)
    def test_configurable_counts_and_persistent_quota(self):
        mp.run_daily('fixture',7,daily_batches=2,rotations=2,skip_unchanged=False)
        self.assertEqual(len(Engine.calls),4)
        self.assertEqual([x['factor_type'] for x in Engine.calls], ['量价','财务','量价','财务'])
        self.assertTrue(all(x['batch']==7 for x in Engine.calls))
        mp.run_daily('fixture',7,daily_batches=2,rotations=2,skip_unchanged=False)
        self.assertEqual(len(Engine.calls),8)
        self.assertIn('今日已',mp.run_daily('fixture',7,daily_batches=2,skip_unchanged=False))
        self.assertIn('今日已',mp.run_daily('fixture',7,daily_batches=1))

    def test_second_rotation_not_blocked_by_first_rotation_receipt(self):
        mp.run_daily('fixture',7,rotations=2,skip_unchanged=True)
        self.assertEqual([x['factor_type'] for x in Engine.calls], ['量价','财务','量价','财务'])
        import json
        final = json.loads((mp.DB.parent/'mining_progress.json').read_text())
        self.assertEqual(final['completed'], {'量价':2, '财务':2})

    def test_config_bounds_and_schedule(self):
        self.assertEqual(mp.schedule_hours(dict(daily_batches=3,rotations=1,batch_per_type=15,hour=17,minute=30,interval_hours=2)),'17,19,21')
        for config in ({'daily_batches':0},{'rotations':4},{'batch_per_type':51},{'hour':15},{'hour':23,'daily_batches':2}):
            with self.assertRaises(ValueError): mp.validate_config(**config)

    def test_old_daily_quota_survives_migration(self):
        import sqlite3
        with sqlite3.connect(mp.DB) as c:
            c.execute('CREATE TABLE batches(day TEXT PRIMARY KEY,status TEXT)')
            c.execute("INSERT INTO batches VALUES ('2026-09-24','complete')")
        self.assertIn('今日已',mp.run_daily('fixture'))
        self.assertEqual(Engine.calls,[])

    def test_lock_prevents_overlap(self):
        import fcntl
        with mp.LOCK.open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertIn('已有',mp.run_daily('fixture'))
        self.assertEqual(Engine.calls,[])
    def test_manual_bypasses_time_only(self):
        Clock.day=26
        self.assertIn('完成',mp.run_daily('fixture',manual=True))
        self.assertIn('今日已',mp.run_daily('fixture',manual=True))

    def test_manual_queue_deduplicates(self):
        rid=mp.manual_request()
        self.assertEqual(mp.manual_request(),rid)
        self.assertEqual(mp.manual_request('claim'),rid)
        self.assertIsNone(mp.manual_request('claim'))
        mp.finish_manual_request(rid)
        self.assertNotEqual(mp.manual_request(),rid)

    def test_weekend_skips(self):
        Clock.day=26
        self.assertIn('跳过',mp.run_daily('fixture'))
        self.assertEqual(Engine.calls,[])
if __name__=='__main__':unittest.main()
