import json
import tempfile
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace as NS, ModuleType
from unittest import TestCase
from unittest.mock import patch, Mock
from concurrent.futures import ThreadPoolExecutor
import llmutil as u
from loopengine import llm_review as review

class CostTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.object(u,'_CACHE_DB',Path(self.tmp.name)/'usage.db');p.start();self.addCleanup(p.stop)
        p=patch.dict('os.environ',{'LLM_REVIEW_DAILY_LIMIT':'5','LLM_GENERATE_DAILY_LIMIT':'5'});p.start();self.addCleanup(p.stop)
        u._ensure_cache_table()
    def test_atomic_label_budget_counts_failed_attempts(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(lambda _:u._budget_reserve(350,'loopengine_review',input_tokens=200),range(20)))
        self.assertEqual(sum(results),5)
        with sqlite3.connect(u._CACHE_DB) as c:
            self.assertEqual(c.execute('SELECT calls,reserved_tokens FROM llm_usage').fetchone(),(5,2750))
    def response(self,text):
        return NS(choices=[NS(message=NS(content=text))],usage=NS(prompt_tokens=100,completion_tokens=30))
    def test_review_cache_survives_days_but_versions_and_types_invalidate(self):
        completion=Mock(return_value=self.response('{"verdict":"pass","reason":"test"}'))
        fake=ModuleType('litellm');fake.completion=completion
        with patch.dict('sys.modules',{'litellm':fake}),patch.object(u,'llm_available',return_value=True):
            self.assertTrue(review.llm_review('ma(close,20)')[0])
            with sqlite3.connect(u._CACHE_DB) as c:c.execute('UPDATE llm_cache SET created_at=?',(time.time()-7*86400,))
            self.assertTrue(review.llm_review('ma( close , 20 )')[0])
            self.assertEqual(completion.call_count,1)
            review.llm_review('ma(close,20)',factor_type='资金流')
            self.assertEqual(completion.call_count,2)
            with patch.object(review,'_REVIEWER_SYS',review._REVIEWER_SYS+' revised'):
                review.llm_review('ma(close,20)')
            self.assertEqual(completion.call_count,3)
    def test_invalid_review_not_reused_and_does_not_become_pass(self):
        fake=ModuleType('litellm');fake.completion=Mock(return_value=self.response('{"reason":"missing verdict"}'))
        with patch.dict('sys.modules',{'litellm':fake}),patch.object(u,'llm_available',return_value=True):
            for _ in range(2):self.assertEqual(review.llm_review('ma(close,20)')[1],'llm-error-fallback')
        self.assertEqual(fake.completion.call_count,2)
        with sqlite3.connect(u._CACHE_DB) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM llm_cache').fetchone()[0],0)

class EngineCostTests(TestCase):
    def test_duplicate_frozen_and_failed_candidates_never_reach_llm(self):
        from contextlib import ExitStack
        import pandas as pd
        from unittest.mock import MagicMock
        import loopengine.engine as e
        eng=e.LoopEngine.__new__(e.LoopEngine)
        eng.pool_name='fixture'
        budget=MagicMock();budget.p={}
        eng.state={'iteration':0,'budget':budget,'field_weights':MagicMock(),'momentum':{},'accepted':0}
        eng._save_state=Mock();eng._signal_shadow_hook=Mock()
        eng._gen_candidate=Mock(return_value=('mutate',e.parse('ma(close,20)')))
        conn=MagicMock();conn.__enter__.return_value.execute.return_value.fetchall.return_value=[]
        with ExitStack() as st:
            for module,name,value in [(e,'bus',MagicMock()),(e.library,'get_factor_registry',Mock(return_value=pd.DataFrame())),(e.library,'family_live_stats',Mock(return_value={})),(e.library,'fsa_recompute',Mock()),(e.library,'record_tested',Mock()),(e.library,'record_failure',Mock()),(e.library,'sync_factor_registry',Mock()),(e.library,'_lconn',Mock(return_value=conn)),(e.structure,'family_coverage',Mock(return_value={})),(e.structure,'assign_family',Mock(return_value='fixture')),(e,'detect_regime',Mock(return_value={'regime':'sideways','confidence':1,'details':{}})),(e.G,'log_gate_detail',Mock()),(e,'evaluate_tree',Mock(return_value=pd.DataFrame([[1],[2]]))),(e.fe,'multi_objective_score',Mock(return_value={}))]:
                st.enter_context(patch.object(module,name,value))
            st.enter_context(patch('loopengine.decay.run_decay_detection',return_value={}))
            st.enter_context(patch.object(e.review,'review',return_value=(True,'')))
            st.enter_context(patch.object(e.library,'is_tested',side_effect=[True,False,False,False]))
            st.enter_context(patch.object(e.library,'is_frozen',side_effect=[True,False,False]))
            gates=st.enter_context(patch.object(e.G,'evaluate_gates',side_effect=[{'pass':False,'reasons':['IC too low'],'metrics':{}},{'pass':True,'reasons':[],'metrics':{}}]))
            semantic=st.enter_context(patch.object(review,'llm_review',return_value=(True,'ok')))
            out=eng.run_round(batch=4,include_events=False,prepared=(pd.DataFrame(),{},[],'2026-01-01'))
        self.assertEqual((out['dup'],out['frozen'],out['tested'],out['passed']),(1,1,2,1))
        self.assertEqual(semantic.call_count,1)
        self.assertEqual(gates.call_count,2)

    def test_generation_limit_falls_back_to_program_search(self):
        import loopengine.engine as e
        import random
        from unittest.mock import MagicMock
        eng=e.LoopEngine.__new__(e.LoopEngine)
        eng.state={'budget':MagicMock(),'field_weights':MagicMock()}
        eng.state['budget'].choose.return_value='llm';eng.state['field_weights'].w={}
        eng._llm_generate=Mock(return_value=e.parse('ma(close,20)'))
        eng._pick_parent=Mock(return_value=None)
        stats={}
        with patch.dict('os.environ',{'LLM_LOOPENGINE_GENERATE_LIMIT':'2'}),patch.object(e.genetics,'random_tree',return_value=e.parse('ma(close,20)')):
            sources=[eng._gen_candidate(random.Random(i),[],[],{},stats=stats)[0] for i in range(10)]
        self.assertEqual(eng._llm_generate.call_count,2)
        self.assertEqual(sources.count('mutate'),8)
