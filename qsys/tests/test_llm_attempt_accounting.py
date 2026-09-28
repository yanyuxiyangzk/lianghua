import json
import os
import sqlite3
import tempfile
from pathlib import Path
from types import ModuleType,SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock,patch
import llmutil as u

class AttemptTests(TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.remote=Mock();fake=ModuleType('litellm');fake.completion=self.remote
        for p in [patch.object(u,'_CACHE_DB',Path(self.tmp.name)/'llm.db'),patch.object(u,'llm_available',return_value=True),patch.object(u,'_DAILY_TOKEN_LIMIT',30000),patch.object(u,'_DAILY_CALL_LIMIT',30),patch.object(u,'_MAX_INPUT_TOKENS',12000),patch.dict('sys.modules',{'litellm':fake})]:
            p.start();self.addCleanup(p.stop)
        u._ensure_cache_table()
        self.messages=[{'role':'user','content':'测试输入'}]
    def response(self,text='',finish='length',input_tokens=12,output_tokens=30):
        return NS(choices=[NS(message=NS(content=text),finish_reason=finish)],usage=NS(prompt_tokens=input_tokens,completion_tokens=output_tokens))
    def rows(self):
        with sqlite3.connect(u._CACHE_DB) as c:
            c.row_factory=sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM llm_usage_log ORDER BY id')]
    def budget(self):
        with sqlite3.connect(u._CACHE_DB) as c:return c.execute('SELECT calls,reserved_tokens FROM llm_usage').fetchone()
    def test_each_retry_reserves_input_and_records_both_usage(self):
        self.remote.side_effect=[self.response(),self.response('答案','stop',15,20)]
        self.assertEqual(u.llm_chat_multi(self.messages,100,label='chat_stock'),'答案')
        rows=self.rows()
        self.assertEqual([r['attempt'] for r in rows],[1,2])
        self.assertEqual([r['status'] for r in rows],['empty','completed'])
        self.assertEqual(sum(r['input_tokens']+r['output_tokens'] for r in rows),77)
        self.assertEqual(self.budget(),(2,2*(100+u._input_budget(self.messages))))
        self.assertTrue(all(c.kwargs['num_retries']==0 and c.kwargs['max_retries']==0 for c in self.remote.call_args_list))
    def test_retry_blocked_when_remaining_budget_insufficient(self):
        self.remote.return_value=self.response()
        with patch.object(u,'_DAILY_TOKEN_LIMIT',100+u._input_budget(self.messages)):
            self.assertIsNone(u.llm_chat_multi(self.messages,100))
        self.assertEqual(self.remote.call_count,1)
        self.assertEqual(len(self.rows()),1)
        self.assertIn('输入及输出预算不足',u.llm_failure_reason())
    def test_oversize_input_blocked_before_request(self):
        with patch.object(u,'_MAX_INPUT_TOKENS',300):
            self.assertIsNone(u.llm_chat_multi([{'role':'user','content':'中'*100}],100))
        self.remote.assert_not_called();self.assertEqual(self.rows(),[])
    def test_failure_recorded_without_inventing_usage(self):
        self.remote.side_effect=TimeoutError('fixture timeout')
        self.assertIsNone(u.llm_chat_multi(self.messages,100))
        row=self.rows()[0]
        self.assertEqual(row['status'],'failed');self.assertEqual(row['error_type'],'TimeoutError')
        self.assertIsNone(row['input_tokens']);self.assertGreater(row['reserved_tokens'],100)
    def test_cache_hit_does_not_consume_budget(self):
        self.remote.return_value=self.response('答案','stop')
        self.assertEqual(u.llm_chat_multi(self.messages,100),'答案')
        before=self.budget()
        self.assertEqual(u.llm_chat_multi(self.messages,100),'答案')
        self.assertEqual(self.remote.call_count,1);self.assertEqual(self.budget(),before)
        self.assertEqual(self.rows()[-1]['status'],'cache_hit')
    def test_single_chat_explicit_retry_accounted(self):
        self.remote.side_effect=[TimeoutError('fixture timeout'),self.response('答案','stop')]
        self.assertEqual(u.llm_chat('system','问题',100,max_retries=1),'答案')
        self.assertEqual([r['status'] for r in self.rows()],['failed','completed'])
        self.assertEqual(self.budget()[0],2)
    def test_pending_attempt_exists_before_network_call(self):
        def remote(**kwargs):
            self.assertEqual(self.rows()[0]['status'],'pending')
            self.assertEqual(self.budget()[0],1)
            return self.response('答案','stop')
        self.remote.side_effect=remote
        self.assertEqual(u.llm_chat_multi(self.messages,100),'答案')
    def test_usage_missing_remains_unknown(self):
        self.remote.return_value=NS(choices=[NS(message=NS(content='答案'),finish_reason='stop')])
        self.assertEqual(u.llm_chat_multi(self.messages,100),'答案')
        self.assertIsNone(self.rows()[0]['input_tokens'])
    def test_midnight_receipt_updates_original_reservation_day(self):
        def remote(**kwargs):
            with patch.object(u.time,'strftime',return_value='2099-01-01'):
                # Update must not move the existing request to another day's budget.
                row=self.rows()[0]
                u._record_usage('','model',request_id=row['request_id'],input_tokens=1,output_tokens=1)
                self.assertEqual(self.rows()[0]['day'],row['day'])
            return self.response('答案','stop')
        self.remote.side_effect=remote
        self.assertEqual(u.llm_chat_multi(self.messages,100),'答案')
    def test_journal_failure_rolls_back_budget_and_prevents_request(self):
        with sqlite3.connect(u._CACHE_DB) as c:
            c.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON llm_usage_log BEGIN SELECT RAISE(ABORT,'test receipt failure'); END")
        self.assertIsNone(u.llm_chat_multi(self.messages,100))
        self.remote.assert_not_called()
        self.assertIsNone(self.budget())
    def test_retry_call_count_limit_also_blocks_network(self):
        self.remote.return_value=self.response()
        with patch.object(u,'_DAILY_CALL_LIMIT',1):
            self.assertIsNone(u.llm_chat_multi(self.messages,100))
        self.assertEqual(self.remote.call_count,1)
        self.assertIn('调用次数已达上限',u.llm_failure_reason())
