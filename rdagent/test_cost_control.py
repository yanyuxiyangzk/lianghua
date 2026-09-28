import tempfile
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from rdagent.oai import cost_control as c

class BudgetTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.dict(os.environ,{'RDAGENT_LLM_USAGE_DB':str(Path(self.tmp.name)/'rd.db'),'RDAGENT_LLM_DAILY_CALL_LIMIT':'3','RDAGENT_LLM_DAILY_TOKEN_LIMIT':'10000'})
        p.start();self.addCleanup(p.stop)
        c.connect().close()
    def test_concurrent_attempt_limit(self):
        def attempt(_):
            try:return c.reserve('model',100,500)
            except c.CostLimitError:return None
        with ThreadPoolExecutor(max_workers=8) as pool:rows=list(pool.map(attempt,range(12)))
        self.assertEqual(sum(r is not None for r in rows),3)
    def test_input_plus_output_budget(self):
        with patch.dict(os.environ,{'RDAGENT_LLM_DAILY_TOKEN_LIMIT':'600'}):
            c.reserve('model',200,400)
            with self.assertRaises(c.CostLimitError):c.reserve('model',1,1)
    def test_permission_failure_circuit_and_failure_receipt(self):
        rid=c.reserve('model',100,500)
        self.assertTrue(c.finish(rid,'model',error=RuntimeError('team not allowed to access model')))
        with self.assertRaisesRegex(c.CostLimitError,'熔断'):c.reserve('model',100,500)
        with c.connect() as db:self.assertEqual(db.execute('SELECT status,error_type FROM attempts').fetchone(),('failed','RuntimeError'))
    def test_backend_checks_budget_before_network_and_no_auth_retry(self):
        from rdagent.oai.backend.litellm import LiteLLMAPIBackend
        obj=LiteLLMAPIBackend.__new__(LiteLLMAPIBackend)
        obj.retry_wait_seconds=0
        obj.use_chat_cache=False
        obj.dump_chat_cache=False
        with patch.object(obj,'get_complete_kwargs',return_value={'model':'model','max_tokens':500}),patch('rdagent.oai.backend.litellm.token_counter',return_value=100),patch.object(obj,'_create_chat_completion_guarded_body',side_effect=RuntimeError('team not allowed to access model')) as remote:
            with self.assertRaises(c.CostLimitError):obj._try_create_chat_completion_or_embedding(chat_completion=True,messages=[])
            with self.assertRaises(c.CostLimitError):obj._try_create_chat_completion_or_embedding(chat_completion=True,messages=[])
            self.assertEqual(remote.call_count,1)
