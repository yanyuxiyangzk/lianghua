"""QSYS 共享 LLM 通道：用看板已配置的 DeepSeek（或 .env 的 CHAT_MODEL）做文本增强。

复用 .env 里的 CHAT_MODEL / DEEPSEEK_API_KEY（与 RD-Agent 同源），
不引入新依赖——litellm 已随 pydantic-ai-slim 进入 qsys 镜像。
失败时 fail-open：返回 None，由调用方决定兜底展示。

DeepSeek 前缀缓存监控：
  响应中 prompt_cache_hit_tokens / prompt_cache_miss_tokens 反映缓存命中情况。
  命中率 = hit / (hit + miss)，目标 > 80%（稳定前缀 > 1024 tokens 时可达 95%+）。

LLM 响应缓存：
  相同 prompt 的响应缓存到 SQLite，避免重复调用。
  缓存 key = SHA256(model + system + user + max_tokens + temperature)
  缓存 TTL = 24 小时（可配置）
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid

from common import DATA_DIR

log = logging.getLogger("llmutil")

_DEFAULT_MODEL = os.environ.get("CHAT_MODEL") or "deepseek/deepseek-chat"

# 模型名映射：某些环境变量中的模型名不是有效的API模型名
_MODEL_ALIAS = {
    # 兼容旧配置/旧调用名，统一路由到当前 CHAT_MODEL。
    "deepseek/deepseek-v4.1-flash": _DEFAULT_MODEL,
    "deepseek-v4.1-flash": _DEFAULT_MODEL,
    "deepseek/deepseek-flash": _DEFAULT_MODEL,
    "deepseek-flash": _DEFAULT_MODEL,
    "deepseek/deepseek-chat": _DEFAULT_MODEL,
    "deepseek/deepseek-v4-pro": _DEFAULT_MODEL,
    "deepseek-v4-pro": _DEFAULT_MODEL,
}


def _resolve_model(model: str) -> str:
    """解析模型名，处理别名映射。"""
    return _MODEL_ALIAS.get(model, model)


def _completion_options(model: str) -> dict:
    """当前 Flash 模型默认会消耗输出额度进行内部推理；普通文本任务关闭推理。"""
    if "flash" in model.lower():
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}

# LLM 响应缓存
_CACHE_DB = DATA_DIR / "experience.db"
_CACHE_TTL = 86400  # 24小时
_DAILY_CALL_LIMIT = int(os.environ.get("LLM_DAILY_CALL_LIMIT", "30"))
_DAILY_TOKEN_LIMIT = int(os.environ.get("LLM_DAILY_TOKEN_LIMIT", "30000"))
_MAX_INPUT_TOKENS = int(os.environ.get("LLM_MAX_INPUT_TOKENS", "12000"))
# 低频高价值任务的保留日额度（调用次数）：绕过全局调用上限，不与高频任务
# （如演化评审）竞争——2026-09-22 概率画像被 loopengine_review 打满全局额度而饿死。
# 保留轨道仍占用全局 token 预算，且全部调用照常落 llm_usage_log 可审计。
_LABEL_RESERVED_CALLS = {"stock_probability_profile_v1": 5,
                         "theory_hypothesis": 3, "theory_name": 6}
_last_error = ""


def _set_last_error(message: str) -> None:
    global _last_error
    _last_error = str(message or "")[:500]


def llm_failure_reason() -> str:
    """返回最近一次 LLM 失败的用户可读原因，避免把所有故障误报为缺少 Key。"""
    if not llm_available():
        return "未配置全局 LLM API Key"
    return _last_error or "LLM 接口调用失败，请检查服务状态或稍后重试"


def _ensure_cache_table():
    """确保缓存表存在。"""
    with sqlite3.connect(str(_CACHE_DB), timeout=10) as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key TEXT PRIMARY KEY,
                response TEXT,
                created_at REAL,
                model TEXT,
                label TEXT
            )
        """)
        c.execute("""CREATE TABLE IF NOT EXISTS llm_usage (
            day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0,
            reserved_tokens INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS llm_usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
            day TEXT NOT NULL, label TEXT, model TEXT, cache_hit INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER, output_tokens INTEGER, reserved_tokens INTEGER)""")
        # 前缀缓存遥测补列（升级兼容）：DeepSeek prompt_cache_hit/miss_tokens 落表，
        # 前缀命中率从此可审计，不再只写日志。
        log_cols = [r[1] for r in c.execute("PRAGMA table_info(llm_usage_log)")]
        for col in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            if col not in log_cols:
                c.execute(f"ALTER TABLE llm_usage_log ADD COLUMN {col} INTEGER")

        for col,kind in {'request_id':'TEXT','attempt':'INTEGER','status':'TEXT',
                         'input_estimate':'INTEGER','output_limit':'INTEGER',
                         'error_type':'TEXT','finish_reason':'TEXT'}.items():
            if col not in log_cols:
                c.execute(f'ALTER TABLE llm_usage_log ADD COLUMN {col} {kind}')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS llm_request_id ON llm_usage_log(request_id)')


def _record_usage(label, model, cache_hit=False, input_tokens=None,
                  output_tokens=None, reserved_tokens=0,
                  cache_hit_tokens=None, cache_miss_tokens=None, request_id=None,
                  status='completed', error_type=None, finish_reason=None):
    try:
        _ensure_cache_table()
        with sqlite3.connect(str(_CACHE_DB), timeout=10) as c:
            if request_id:
                c.execute('UPDATE llm_usage_log SET input_tokens=?,output_tokens=?,prompt_cache_hit_tokens=?,prompt_cache_miss_tokens=?,status=?,error_type=?,finish_reason=? WHERE request_id=?',
                          (input_tokens,output_tokens,cache_hit_tokens,cache_miss_tokens,status,error_type,finish_reason,request_id))
            else:
                c.execute("INSERT INTO llm_usage_log(created_at,day,label,model,cache_hit,input_tokens,output_tokens,reserved_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (time.time(), time.strftime("%Y-%m-%d"), label, model, int(cache_hit),
                           input_tokens, output_tokens, reserved_tokens,
                           cache_hit_tokens, cache_miss_tokens,'cache_hit' if cache_hit else status))
    except Exception:
        log.error('LLM usage receipt update failed; reserved budget remains charged',exc_info=True)


def _budget_reserve(max_tokens: int, label: str = "", input_tokens: int = 0,
                    model=None, request_id=None, attempt=1) -> bool:
    """Reserve estimated input + output and journal each attempt atomically. Cache hits never consume budget.
    保留轨道（_LABEL_RESERVED_CALLS 内的 label）：按自身当日非缓存调用数限流，
    绕过全局调用上限，但仍占用全局 token 预算。"""
    day = time.strftime("%Y-%m-%d")
    output_limit=max_tokens
    max_tokens += input_tokens
    try:
        _ensure_cache_table()
        with sqlite3.connect(str(_CACHE_DB), timeout=10) as c:
            c.execute("CREATE TABLE IF NOT EXISTS llm_label_budget(day TEXT,label TEXT,calls INTEGER NOT NULL,PRIMARY KEY(day,label))")
            c.execute('BEGIN IMMEDIATE')
            label_row=c.execute('SELECT calls FROM llm_label_budget WHERE day=? AND label=?',(day,label)).fetchone()
            used_attempts=label_row[0] if label_row else c.execute('SELECT COUNT(*) FROM llm_usage_log WHERE day=? AND label=? AND cache_hit=0',(day,label)).fetchone()[0]
            limits={'loopengine_generate':int(os.environ.get('LLM_GENERATE_DAILY_LIMIT','5')), 'loopengine_review':int(os.environ.get('LLM_REVIEW_DAILY_LIMIT','5'))}
            cap=limits.get(label,_LABEL_RESERVED_CALLS.get(label))
            if cap is not None and used_attempts>=cap:
                _set_last_error(f'{label} 今日调用额度已用完（{used_attempts}/{cap}）')
                return False
            row = c.execute("SELECT calls, reserved_tokens FROM llm_usage WHERE day=?", (day,)).fetchone()
            calls, tokens = row if row else (0, 0)
            quota = _LABEL_RESERVED_CALLS.get(label)
            if quota:
                used = c.execute(
                    "SELECT COUNT(*) FROM llm_usage_log WHERE day=? AND label=? AND cache_hit=0",
                    (day, label)).fetchone()[0]
                if used >= quota:
                    _set_last_error(f"{label} 今日保留额度已用完（{used}/{quota}）")
                    return False
                if tokens + max_tokens > _DAILY_TOKEN_LIMIT:
                    _set_last_error(
                        f"今日 LLM 输入及输出预算不足（已预留 {tokens}/{_DAILY_TOKEN_LIMIT} tokens，"
                        f"本次需要 {max_tokens}）")
                    return False
                c.execute("INSERT OR REPLACE INTO llm_usage(day,calls,reserved_tokens) VALUES(?,?,?)",
                          (day, calls, tokens + max_tokens))
                c.execute('INSERT OR REPLACE INTO llm_label_budget VALUES(?,?,?)',(day,label,used_attempts+1))
                if request_id:
                    c.execute('INSERT INTO llm_usage_log(created_at,day,label,model,cache_hit,reserved_tokens,request_id,attempt,status,input_estimate,output_limit) VALUES(?,?,?,?,0,?,?,?,?,?,?)',
                              (time.time(),day,label,model,max_tokens,request_id,attempt,'pending',input_tokens,output_limit))
                return True
            if calls >= _DAILY_CALL_LIMIT:
                _set_last_error(f"今日 LLM 调用次数已达上限（{calls}/{_DAILY_CALL_LIMIT}）")
                log.warning("LLM daily call limit exceeded: calls=%d/%d", calls, _DAILY_CALL_LIMIT)
                return False
            if tokens + max_tokens > _DAILY_TOKEN_LIMIT:
                _set_last_error(
                    f"今日 LLM 输入及输出预算不足（已预留 {tokens}/{_DAILY_TOKEN_LIMIT} tokens，"
                    f"本次需要 {max_tokens}）")
                log.warning("LLM daily budget exceeded: calls=%d/%d tokens=%d/%d",
                            calls, _DAILY_CALL_LIMIT, tokens, _DAILY_TOKEN_LIMIT)
                return False
            c.execute("INSERT OR REPLACE INTO llm_usage(day,calls,reserved_tokens) VALUES(?,?,?)",
                      (day, calls + 1, tokens + max_tokens))
            c.execute('INSERT OR REPLACE INTO llm_label_budget VALUES(?,?,?)',(day,label,used_attempts+1))
            if request_id:
                c.execute('INSERT INTO llm_usage_log(created_at,day,label,model,cache_hit,reserved_tokens,request_id,attempt,status,input_estimate,output_limit) VALUES(?,?,?,?,0,?,?,?,?,?,?)',
                          (time.time(),day,label,model,max_tokens,request_id,attempt,'pending',input_tokens,output_limit))
            return True
    except Exception:
        _set_last_error('LLM 预算记录不可用，本次未请求模型')
        log.error('LLM budget reservation failed',exc_info=True)
        return False


def _get_cache_key(model: str, messages: list[dict], max_tokens: int, temperature: float) -> str:
    """生成缓存 key。"""
    content = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()[:32]


def _get_cached(cache_key: str, ttl: int = _CACHE_TTL) -> str | None:
    """从缓存获取响应。"""
    try:
        _ensure_cache_table()
        with sqlite3.connect(str(_CACHE_DB), timeout=10) as c:
            row = c.execute(
                "SELECT response, created_at FROM llm_cache WHERE cache_key=?",
                (cache_key,)
            ).fetchone()
            if row and (time.time() - row[1]) < ttl:
                log.debug("LLM cache hit: %s", cache_key[:12])
                return row[0]
    except Exception:
        pass
    return None


def _set_cached(cache_key: str, response: str, model: str, label: str):
    """缓存响应。"""
    try:
        _ensure_cache_table()
        with sqlite3.connect(str(_CACHE_DB), timeout=10) as c:
            c.execute(
                "INSERT OR REPLACE INTO llm_cache (cache_key, response, created_at, model, label) VALUES (?,?,?,?,?)",
                (cache_key, response, time.time(), model, label)
            )
    except Exception:
        pass


def _log_cache_usage(r, label: str = "") -> tuple[int, int]:
    """从 LLM 响应中提取前缀缓存命中 tokens，写日志并返回 (hit, miss) 供落表。"""
    try:
        usage = getattr(r, "usage", None) or {}
        hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
        # litellm 可能映射到 cached_tokens
        if hit == 0 and miss == 0:
            details = getattr(usage, "prompt_tokens_details", None)
            if details:
                hit = getattr(details, "cached_tokens", 0) or 0
                miss = (getattr(usage, "prompt_tokens", 0) or 0) - hit
        if hit + miss > 0:
            rate = hit / (hit + miss)
            log.debug("LLM cache %s: hit=%d miss=%d rate=%.0f%% total=%d",
                      label, hit, miss, rate * 100, hit + miss)
        return int(hit), int(miss)
    except Exception:
        return 0, 0


def llm_cache_stats(day: str | None = None) -> dict:
    """缓存命中审计：应用层响应缓存命中率 + DeepSeek 前缀缓存命中率（按 label 分组）。"""
    day = day or time.strftime("%Y-%m-%d")
    try:
        _ensure_cache_table()
        with sqlite3.connect(str(_CACHE_DB), timeout=10) as c:
            rows = c.execute(
                "SELECT label, COUNT(*), SUM(cache_hit), "
                "SUM(COALESCE(prompt_cache_hit_tokens,0)), "
                "SUM(COALESCE(prompt_cache_miss_tokens,0)) "
                "FROM llm_usage_log WHERE day=? GROUP BY label", (day,)).fetchall()
    except Exception:
        return {"day": day, "labels": []}
    out = []
    for label, calls, hits, pre_hit, pre_miss in rows:
        total_pre = (pre_hit or 0) + (pre_miss or 0)
        out.append({"label": label, "calls": calls, "cache_hits": hits or 0,
                    "app_hit_rate": (hits or 0) / calls if calls else None,
                    "prefix_hit_tokens": pre_hit or 0,
                    "prefix_hit_rate": (pre_hit or 0) / total_pre if total_pre else None})
    return {"day": day, "labels": out}


def llm_available() -> bool:
    return bool(
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("LITELLM_PROXY_API_KEY")
    )


def _input_budget(messages):
    """Conservative text estimate; no tokenizer download or provider request."""
    if not isinstance(messages,list) or not messages or any(not isinstance(m,dict) or not isinstance(m.get('content'),str) for m in messages):
        raise ValueError('LLM 当前仅支持非空文本消息列表')
    estimate=len(json.dumps(messages,ensure_ascii=False,separators=(',',':')).encode('utf-8'))+256
    if estimate>_MAX_INPUT_TOKENS:
        raise ValueError(f'输入预估 {estimate} tokens 超过单次预算 {_MAX_INPUT_TOKENS}，请缩短问题或数据范围')
    return estimate


class _BudgetDenied(Exception):
    pass


def _call_once(messages,model,max_tokens,temperature,label,attempt=1,**extra):
    if isinstance(max_tokens,bool) or not isinstance(max_tokens,int) or max_tokens<=0:
        raise ValueError('输出预算须为正整数')
    estimate=_input_budget(messages)
    from litellm import completion
    request_id=uuid.uuid4().hex
    if not _budget_reserve(max_tokens,label,input_tokens=estimate,model=model,request_id=request_id,attempt=attempt):
        raise _BudgetDenied()
    response=None
    try:
        response=completion(model=model,messages=messages,max_tokens=max_tokens,temperature=temperature,
                            num_retries=0,max_retries=0,**_completion_options(model),**extra)
        ch=response.choices[0]
        content=(ch.message.content or '').strip()
        finish=getattr(ch,'finish_reason',None)
        usage=getattr(response,'usage',None) or {}
        get=lambda key:usage.get(key) if isinstance(usage,dict) else getattr(usage,key,None)
        hit,miss=_log_cache_usage(response,label)
        _record_usage(label,model,input_tokens=get('prompt_tokens'),output_tokens=get('completion_tokens'),
                      request_id=request_id,cache_hit_tokens=hit,cache_miss_tokens=miss,
                      status='completed' if content else 'empty',finish_reason=finish)
        return content,finish
    except Exception as exc:
        usage=getattr(response,'usage',None) or getattr(exc,'usage',None) or {}
        get=lambda key:usage.get(key) if isinstance(usage,dict) else getattr(usage,key,None)
        _record_usage(label,model,input_tokens=get('prompt_tokens'),output_tokens=get('completion_tokens'),
                      request_id=request_id,status='failed',error_type=type(exc).__name__)
        raise


def llm_chat(system: str, user: str, max_tokens: int = 4096, model: str | None = None,
             label: str = "", use_cache: bool = True, max_retries: int | None = None, cache_ttl: int = _CACHE_TTL,
             cache_validator=None) -> str | None:
    """调用一次 chat completion，返回纯文本；无 key / 调用异常返回 None。
    use_cache: 是否使用响应缓存（默认启用，相同prompt直接返回缓存）。"""
    if not llm_available():
        _set_last_error("未配置全局 LLM API Key")
        return None
    
    model = _resolve_model(model or _DEFAULT_MODEL)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    
    # 检查缓存
    if use_cache:
        cache_key = _get_cache_key(model, messages, max_tokens, 0.2)
        cached = _get_cached(cache_key, cache_ttl)
        if cached is not None and (cache_validator is None or cache_validator(cached)):
            _record_usage(label, model, cache_hit=True)
            return cached
    
    try:
        # Explicit bounded retries; every attempt reserves and logs separately.
        retries=min(max(0,int(max_retries or 0)),1)
        for attempt in range(1,retries+2):
            try:
                response,_=_call_once(messages,model,max_tokens,0.2,label,attempt=attempt)
                break
            except _BudgetDenied:
                return None
            except Exception as exc:
                if attempt>retries or getattr(exc,'status_code',None) in (400,401,403,404) or isinstance(exc,ValueError):
                    raise
        # 缓存响应
        if use_cache and response and (cache_validator is None or cache_validator(response)):
            _set_cached(cache_key, response, model, label)
        
        return response
    except Exception as exc:
        _set_last_error(f"LLM 接口调用失败：{type(exc).__name__}: {exc}")
        log.exception("LLM chat failed: model=%s label=%s", model, label)
        return None


def llm_chat_multi(messages: list[dict], max_tokens: int = 4000, model: str | None = None,
                   label: str = "", use_cache: bool = True) -> str | None:
    """多轮对话版：[{"role": "system"|"user"|"assistant", "content": ...}] → 纯文本。
    fail-open 返回 None。推理模型（v4-pro）思维链与正文共享 max_tokens 配额：
    预算给足 4000；若思维链烧光配额导致正文为空（finish_reason=length），
    自动降思考强度（reasoning_effort=low）重试一次。
    use_cache: 是否使用响应缓存（默认启用）。"""
    if not llm_available():
        _set_last_error("未配置全局 LLM API Key")
        return None
    
    model = _resolve_model(model or _DEFAULT_MODEL)
    
    # 检查缓存
    if use_cache:
        cache_key = _get_cache_key(model, messages, max_tokens, 0.3)
        cached = _get_cached(cache_key)
        if cached is not None:
            _record_usage(label, model, cache_hit=True)
            return cached
    
    try:
        try:
            content,finish=_call_once(messages,model,max_tokens,0.3,label,attempt=1)
            if not content and finish=='length':
                content,_=_call_once(messages,model,max_tokens,0.3,label,attempt=2,reasoning_effort='low')
        except _BudgetDenied:
            return None
        if not content:
            _set_last_error('LLM 返回空正文，请缩短输入后重试')
        # 缓存响应
        if use_cache and content:
            _set_cached(cache_key, content, model, label)
        
        return content or None
    except Exception as exc:
        _set_last_error(f"LLM 接口调用失败：{type(exc).__name__}: {exc}")
        log.exception("LLM multi-chat failed: model=%s label=%s", model, label)
        return None
