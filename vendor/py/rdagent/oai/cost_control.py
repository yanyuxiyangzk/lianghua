"""Local, process-safe RD-Agent chat limits. No remote calls; fail closed."""
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class CostLimitError(RuntimeError):
    pass


def connect():
    default=Path(__file__).resolve().parents[4]/'qsys'/'data'/'rdagent_llm_usage.db'
    path=Path(os.environ.get('RDAGENT_LLM_USAGE_DB',str(default)))
    path.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(path,timeout=10)
    c.executescript('''CREATE TABLE IF NOT EXISTS attempts(
        id TEXT PRIMARY KEY, day TEXT, model TEXT, created REAL, input_estimate INTEGER,
        output_limit INTEGER, status TEXT, output_estimate INTEGER, error_type TEXT);
        CREATE TABLE IF NOT EXISTS circuits(model TEXT PRIMARY KEY,until REAL);''')
    return c


def reserve(model,input_tokens,output_limit):
    day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    with closing(connect()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        circuit=c.execute('SELECT until FROM circuits WHERE model=?',(model,)).fetchone()
        if circuit and circuit[0]>time.time():
            raise CostLimitError('RD-Agent 模型权限错误熔断中，24小时内不再请求；修复配置后可人工解除')
        calls,tokens=c.execute('SELECT COUNT(*),COALESCE(SUM(input_estimate+output_limit),0) FROM attempts WHERE day=?',(day,)).fetchone()
        if calls>=int(os.environ.get('RDAGENT_LLM_DAILY_CALL_LIMIT','20')):
            raise CostLimitError('RD-Agent 今日请求次数预算已用完')
        if tokens+input_tokens+output_limit>int(os.environ.get('RDAGENT_LLM_DAILY_TOKEN_LIMIT','60000')):
            raise CostLimitError('RD-Agent 今日预估输入及输出预算不足')
        identifier=uuid.uuid4().hex
        c.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?)',(identifier,day,model,time.time(),input_tokens,output_limit,'reserved',None,None))
        return identifier


def finish(identifier,model,output_tokens=None,error=None):
    permanent=error is not None and (getattr(error,'status_code',None) in (401,403) or
        any(x in str(error).lower() for x in ('team not allowed','invalid api key','invalid_api_key','permission denied','not allowed to access model')))
    with closing(connect()) as c,c:
        c.execute('UPDATE attempts SET status=?,output_estimate=?,error_type=? WHERE id=?',
                  ('failed' if error else 'completed',output_tokens,type(error).__name__ if error else None,identifier))
        if permanent:
            c.execute('INSERT OR REPLACE INTO circuits VALUES(?,?)',(model,time.time()+86400))
    return permanent
