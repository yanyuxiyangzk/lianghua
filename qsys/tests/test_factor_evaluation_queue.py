import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QSYS_DATA_DIR', tempfile.mkdtemp(prefix='eval-tests-'))
import factor_evaluation_queue as q

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.object(q,'DB',Path(self.tmp.name)/'queue.db');p.start();self.addCleanup(p.stop)
        self.factors=[dict(name=n,kind='loopengine',code=n,factor_type=t) for n,t in [('a','资金流'),('b','资金流'),('c','财务'),('d','量价')]]

    def test_invalid_not_success_and_pool_version_isolation(self):
        self.assertEqual(q.classify({'天数':0,'建议方向':'评估失败: 预选窗内无数据'})[0],'sample_insufficient')
        self.assertEqual(q.classify({'天数':300,'IC均值':float('nan'),'ICIR':.5})[0],'compute_failed')
        self.assertEqual(q.classify({'天数':130,'IC均值':.1,'ICIR':.5},'资金流')[0],'sample_insufficient')
        self.assertEqual(q.classify({'天数':130,'IC均值':.1,'ICIR':.5},'龙虎榜')[0],'valid')
        q.enqueue(self.factors,'p')
        self.assertEqual([f['name'] for f in q.select('p','2026-09-24',3)],['a','c','d'])
        self.assertEqual(q.select('other','2026-09-24',5),[])
        changed={**self.factors[0],'code':'changed'}
        q.enqueue([changed],'p')
        self.assertEqual(sum(f['name']=='a' for f in q.select('p','2026-09-24',10)),1)
        self.assertNotEqual(q.factor_version(changed),q.factor_version(self.factors[0]))

    def test_receipt_backoff_reassessment_and_no_same_day_repeat(self):
        q.enqueue(self.factors[:1],'p')
        f=q.select('p','2026-09-24',1)[0]
        # record reads the cost constant only, no computation or live data.
        status=q.record(f,'p','2026-09-24',{'天数':300,'IC均值':.02,'ICIR':.3},'2025-09-24','fixture','hash')
        self.assertEqual(status,'valid')
        self.assertEqual(q.select('p','2026-09-24',1),[])
        self.assertEqual(q.select('p','2026-09-25',1),[])
        self.assertEqual(len(q.select('p','2026-10-01',1)),1)
        for day in ['2026-10-01','2026-10-03','2026-10-07']:
            q.record(f,'p',day,{'评估状态':'compute_failed','评估原因':'boom'},'2025-09-24','fixture','hash')
        self.assertEqual(q.select('p','2026-11-01',1),[])

    def test_coverage_excludes_superseded_versions(self):
        q.enqueue(self.factors[:1], 'p')
        q.enqueue([{**self.factors[0], 'code': 'new version'}], 'p')
        r = q.coverage('p')
        self.assertEqual(r['total'], 1)
        self.assertEqual(r['pending'], 1)
        self.assertEqual(r['superseded'], 1)
        self.assertEqual(r['coverage'], 0)

    def test_drain_deduplicated(self):
        q.request_drain('p');q.request_drain('p')
        self.assertEqual(q.claim_drain(),'p')
        self.assertIsNone(q.claim_drain())

    def test_null_versions_and_precise_sample_reason(self):
        self.assertEqual(q.factor_version({'name':'a','norm':None}),
                         q.factor_version({'name':'a','norm':float('nan')}))
        self.assertIn('130/250',q.classify({'天数':130,'IC均值':.02,'ICIR':.3,'建议方向':'正向'},'资金流')[1])

    def test_lock_prevents_duplicate_worker(self):
        import fcntl
        with patch.object(q,'DATA_DIR',Path(self.tmp.name)):
            with (Path(self.tmp.name)/'factor_evaluation.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertIn('已有因子体检运行',q.run('p',1))

    def test_durable_replay_after_cursor(self):
        q.emit('running',processed=0,total=1)
        first=q.read_events()[-1][0]
        q.emit('complete',processed=1,total=1)
        replay=q.read_events(first)
        self.assertEqual(len(replay),1)
        self.assertEqual(replay[0][1]['status'],'complete')
        self.assertEqual(q.read_events(replay[0][0]),[])

    def test_series_fingerprint(self):
        import pandas as pd
        from mining_policy import fingerprint
        s=pd.Series([1.,2.],name='flow')
        self.assertEqual(fingerprint({'flow':s}),fingerprint({'flow':s.copy()}))
        self.assertNotEqual(fingerprint({'flow':s}),fingerprint({'flow':s+1}))

    def test_run_records_failures_but_only_publishes_valid_scores(self):
        import pandas as pd
        import library, factor_eval, datasource, common
        from contextlib import ExitStack
        reg=pd.DataFrame([{**f,'engine':'loopengine','first_seen':'2025-01-01'} for f in self.factors])
        def evaluate(fs,*args,**kw):
            return pd.DataFrame([{'因子':f['name'],'来源':'演化引擎','天数':300 if f['name']=='a' else 0,
                       'IC均值':.03 if f['name']=='a' else float('nan'),'ICIR':.5 if f['name']=='a' else float('nan'),
                       '建议方向':'正向' if f['name']=='a' else '预选窗内无数据'} for f in fs])
        with ExitStack() as stack:
            stack.enter_context(patch.object(q,'DATA_DIR',Path(self.tmp.name)))
            stack.enter_context(patch.object(library,'get_factor_registry',return_value=reg))
            stack.enter_context(patch.object(library,'list_strategies',return_value={}))
            save=stack.enter_context(patch.object(library,'save_scorecard'))
            stack.enter_context(patch.object(factor_eval,'build_scorecard_batch',side_effect=evaluate))
            stack.enter_context(patch.object(datasource,'get_loop_source',return_value='fixture'))
            stack.enter_context(patch.object(common,'all_pools',return_value={'p':[str(i) for i in range(30)]}))
            stack.enter_context(patch.object(common,'get_last_trade_day',return_value='2026-09-24'))
            stack.enter_context(patch.object(common,'trade_day_offset',return_value='2025-09-24'))
            result=q.run('p',4)
            self.assertIn('有效 1',result)
            self.assertIn('样本不足 3',result)
            self.assertEqual(save.call_args.args[0]['因子'].tolist(),['a'])
            self.assertEqual(q.select('p','2026-09-24',10),[])
            last=q.read_events()[-1][1]
            self.assertEqual(last['processed'],4)
            self.assertEqual(last['status'],'complete')
            self.assertIn('无到期',q.run('p',4))
