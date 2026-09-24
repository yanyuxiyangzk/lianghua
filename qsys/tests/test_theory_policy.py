import os, sys, tempfile, unittest, json
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
TMP = tempfile.TemporaryDirectory()
os.environ['QSYS_DATA_DIR'] = TMP.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import theory_policy as policy
import llmutil
from loopengine.theory_discovery import TheoryNamer, HypothesisGenerator

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(policy, 'DB', Path(self.tmp.name)/'research.db')
        p.start(); self.addCleanup(p.stop)

    def test_registry_reuses_and_reclassifies(self):
        import library, datasource
        with patch.object(datasource, 'MKT_DB', Path(self.tmp.name)/'market.db'), patch.object(llmutil, 'llm_chat', return_value='{"theory_id":"value"}') as llm:
            f = {'name':'fixture','kind':'manual','code':'x = 1'}
            library.sync_factor_registry([f])
            library.sync_factor_registry([f])
            self.assertEqual(llm.call_count,1)
            library.sync_factor_registry([{**f,'code':'x = 2'}])
            self.assertEqual(llm.call_count,2)
            with library._lconn() as c:
                row = c.execute("SELECT theory_id,validation_status,gate_status FROM factor_registry WHERE name='fixture'").fetchone()
            self.assertEqual(row[0], 'value')
            self.assertEqual(row[1], 'shadow_only')
            self.assertIsNone(row[2])

    def test_engine_weekly_guard_and_programmatic_naming(self):
        import pandas as pd
        import signals, common
        from contextlib import ExitStack
        from loopengine import theory_discovery as td
        panel = pd.DataFrame({'close':[10.,11.]})
        patterns = [{'type':'fixture','description':'fixture','severity':1.}]
        with ExitStack() as st:
            st.enter_context(patch.object(td, 'KnowledgeGraph'))
            st.enter_context(patch.object(common, 'all_pools', return_value={'沪深300':['x']}))
            st.enter_context(patch.object(common, 'get_last_trade_day', return_value='2026-09-24'))
            st.enter_context(patch.object(signals, 'get_panel_cached', return_value=panel))
            st.enter_context(patch.object(td.PatternDiscovery, 'discover_anomalies', return_value=patterns))
            st.enter_context(patch.object(td.PatternDiscovery, 'discover_regimes', return_value=[]))
            st.enter_context(patch.object(td, 'load_theory_track_record', return_value={'effective':[],'falsified':[]}))
            st.enter_context(patch.object(td.Formalizer, 'generate_variants', return_value=['roc(close,5)']))
            st.enter_context(patch.object(td.Formalizer, 'validate_sexpr', return_value=(True,'')))
            st.enter_context(patch.object(td.TheoryValidator, 'validate_factor', return_value={'valid':True}))
            registered = st.enter_context(patch.object(td, 'register_theory_factor', return_value=True))
            llm = st.enter_context(patch.object(llmutil, 'llm_chat', return_value='{"hypotheses":[{"sexpr":"roc(close,5)","description":"fixture"}]}'))
            engine = td.TheoryDiscoveryEngine()
            self.assertEqual(engine.run()['registered'],1)
            self.assertIn('skipped',engine.run())
            self.assertEqual(llm.call_count,1)
            self.assertTrue(registered.call_args.args[0].startswith('候选_'))

    def test_strategy_inherits_factor_snapshots_and_scope_intersection(self):
        registry = {'a': {'theory_id':'momentum','hypothesis_id':'h1','theory_family':'动量','regime_scope':'bull,sideways','factor_type':'盘口'},
                    'b': {'theory_id':'momentum','hypothesis_id':'h2','theory_family':'动量','regime_scope':['bull'],'factor_type':'量价'}}
        pack = policy.inherit_strategy_theory({'factors':[{'name':'a','weight':.6},{'name':'b','weight':.4}]},registry)
        self.assertEqual(pack['theory_id'],'momentum')
        self.assertEqual(pack['theory_name'],'动量')
        self.assertEqual(pack['regime_scope'],'bull')
        self.assertEqual(pack['factors'][0]['hypothesis_id'],'h1')
        self.assertEqual(pack['factors'][0]['weight'],.6)
        self.assertEqual(pack['factors'][0]['factor_type'],'盘口')
        registry['a']['hypothesis_id']='changed'
        self.assertEqual(pack['factors'][0]['hypothesis_id'],'h1')

    def test_mixed_missing_and_disjoint_theories_fail_closed(self):
        for other in ({'theory_id':'value','regime_scope':'bull'},
                      {'regime_scope':'bull'}, {'theory_id':'momentum','regime_scope':'bear'}, {}):
            pack = policy.inherit_strategy_theory({'factors':[{'name':'a'},{'name':'b'}]},
                {'a':{'theory_id':'momentum','regime_scope':'bull'}, 'b':other})
            self.assertIsNone(pack['theory_id'])
            self.assertEqual(pack['account_scope'],'none')

    def test_discovery_factor_strategy_roundtrip(self):
        import library, datasource
        from loopengine.theory_discovery import register_theory_factor
        with patch.object(datasource,'MKT_DB',Path(self.tmp.name)/'market.db'), patch.object(llmutil,'llm_chat') as llm:
            self.assertTrue(register_theory_factor('fixture','roc(close,5)','趋势',{}))
            with library._lconn() as c:
                c.row_factory = __import__('sqlite3').Row
                factor = dict(c.execute("SELECT * FROM factor_registry WHERE name='theory_fixture'").fetchone())
            self.assertEqual(factor['theory_id'],policy.research_theory_id('roc(close,5)'))
            library.sync_factor_registry([{'name':factor['name'],'kind':factor['kind'],'code':factor['code'],'factor_type':factor['factor_type']}])
            with library._lconn() as c:
                row=c.execute("SELECT theory_id,hypothesis_id,source_theory_sexpr FROM factor_registry WHERE name='theory_fixture'").fetchone()
            self.assertEqual(row,(factor['theory_id'],'fixture','roc(close,5)'))
            pack=policy.inherit_strategy_theory({'pool_name':'fixture','top_n':5,'method':'等权','factors':[{'name':factor['name'],'weight':1,'direction':1}]},{factor['name']:factor})
            library.save_strategy('fixture_pack',pack)
            loaded=library.list_strategies()['fixture_pack']
            self.assertEqual(loaded['theory_id'],factor['theory_id'])
            self.assertEqual(loaded['factors'][0]['hypothesis_id'],'fixture')
            self.assertEqual(loaded['status'],'shadow')
            llm.assert_not_called()

    def test_rules_do_not_call_llm(self):
        with patch.object(llmutil, 'llm_chat') as llm:
            out = policy.associate({'name':'x','family':'动量'})
        self.assertEqual(out['theory_id'], 'momentum')
        llm.assert_not_called()

    def test_unchanged_reused_changed_reclassified(self):
        with patch.object(llmutil, 'llm_chat', return_value='{"theory_id":"value"}') as llm:
            a = policy.associate({'name':'x','code':'a'})
            self.assertEqual(a, policy.associate({'name':'x','code':'a'}))
            policy.associate({'name':'x','code':'b'})
            self.assertEqual(llm.call_count, 2)
            self.assertEqual(llm.call_args.kwargs['max_retries'], 0)

    def test_unavailable_stays_unclassified_cached(self):
        with patch.object(llmutil, 'llm_chat', return_value=None) as llm:
            self.assertIsNone(policy.associate({'name':'x'})['theory_id'])
            policy.associate({'name':'x'})
            self.assertEqual(llm.call_count, 1)

    def test_unknown_model_classification_rejected(self):
        with patch.object(llmutil, 'llm_chat', return_value='{"theory_id":"invented"}'):
            self.assertIsNone(policy.associate({'name':'x'})['theory_id'])

    def test_budget_limits_before_network(self):
        with patch.object(llmutil, 'llm_chat', return_value='ok') as llm:
            for budget in (policy.Budget('chars',input_chars=1), policy.Budget('tokens',tokens=1)):
                with self.assertRaises(policy.BudgetExceeded):
                    budget.chat('system','user',10,'test')
            llm.assert_not_called()
            b = policy.Budget('calls')
            b.chat('s','u',10,'test')
            with self.assertRaises(policy.BudgetExceeded): b.chat('s','u',10,'test')
            with self.assertRaises(policy.BudgetExceeded): policy.Budget('calls').chat('s','u',10,'test')
            self.assertEqual(llm.call_count,1)

    def test_weekly_and_duplicate_limits(self):
        day = datetime(2026,9,24)
        self.assertTrue(policy.claim_discovery('a','b',day)[0])
        self.assertIsNone(policy.claim_discovery('c','d',day)[0])
        later = datetime(2026,10,1)
        self.assertIsNone(policy.claim_discovery('a','d',later)[0])
        self.assertIsNone(policy.claim_discovery('c','b',later)[0])
        self.assertTrue(policy.claim_discovery('c','d',later)[0])

    def test_concurrent_discovery_only_one_claim(self):
        # Initialize schema before contention test.
        policy.connect().close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _:policy.claim_discovery('a','b',datetime(2026,9,24)),range(4)))
        self.assertEqual(sum(bool(r[0]) for r in results),1)

    def test_programmatic_name_stable_without_llm(self):
        with patch.object(llmutil,'llm_chat') as llm:
            a = TheoryNamer.name_theory('roc(close,5)',{}, {})
            self.assertEqual(a, TheoryNamer.name_theory('roc(close,5)',{},{}))
            self.assertNotEqual(a['name'], TheoryNamer.name_theory('roc(close,10)',{}, {})['name'])
            llm.assert_not_called()

    def test_unbudgeted_hypothesis_does_not_call_llm(self):
        with patch.object(llmutil,'llm_chat') as llm:
            with self.assertRaises(policy.BudgetExceeded):
                HypothesisGenerator.generate([{'type':'x','description':'x','severity':1}])
            llm.assert_not_called()

if __name__ == '__main__': unittest.main()
