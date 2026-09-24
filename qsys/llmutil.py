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


def _record_usage(label, model, cache_hit=False, input_tokens=None,
                  output_tokens=None, reserved_tokens=0,
                  cache_hit_tokens=None, cache_miss_tokens=None):
    try:
        _ensure_cache_table()
        with sqlite3.connect(str(_CACHE_DB), timeout=10) as c:
            c.execute("INSERT INTO llm_usage_log(created_at,day,label,model,cache_hit,input_tokens,output_tokens,reserved_tokens,prompt_cache_hit_tokens,prompt_cache_miss_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), time.strftime("%Y-%m-%d"), label, model, int(cache_hit),
                       input_tokens, output_tokens, reserved_tokens,
                       cache_hit_tokens, cache_miss_tokens))
    except Exception:
        pass


def _budget_reserve(max_tokens: int, label: str = "") -> bool:
    """Reserve a daily request/output budget. Cache hits never consume budget.
    保留轨道（_LABEL_RESERVED_CALLS 内的 label）：按自身当日非缓存调用数限流，
    绕过全局调用上限，但仍占用全局 token 预算。"""
    day = time.strftime("%Y-%m-%d")
    try:
        _ensure_cache_table()
        with sqlite3.connect(str(_CACHE_DB), timeout=10) as c:
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
                        f"今日 LLM 输出预算不足（已预留 {tokens}/{_DAILY_TOKEN_LIMIT} tokens，"
                        f"本次需要 {max_tokens}）")
                    return False
                c.execute("INSERT OR REPLACE INTO llm_usage(day,calls,reserved_tokens) VALUES(?,?,?)",
                          (day, calls, tokens + max_tokens))
                return True
            if calls >= _DAILY_CALL_LIMIT:
                _set_last_error(f"今日 LLM 调用次数已达上限（{calls}/{_DAILY_CALL_LIMIT}）")
                log.warning("LLM daily call limit exceeded: calls=%d/%d", calls, _DAILY_CALL_LIMIT)
                return False
            if tokens + max_tokens > _DAILY_TOKEN_LIMIT:
                _set_last_error(
                    f"今日 LLM 输出预算不足（已预留 {tokens}/{_DAILY_TOKEN_LIMIT} tokens，"
                    f"本次需要 {max_tokens}）")
                log.warning("LLM daily budget exceeded: calls=%d/%d tokens=%d/%d",
                            calls, _DAILY_CALL_LIMIT, tokens, _DAILY_TOKEN_LIMIT)
                return False
            c.execute("INSERT OR REPLACE INTO llm_usage(day,calls,reserved_tokens) VALUES(?,?,?)",
                      (day, calls + 1, tokens + max_tokens))
            return True
    except Exception:
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


def _get_cached(cache_key: str) -> str | None:
    """从缓存获取响应。"""
    try:
        _ensure_cache_table()
        with sqlite3.connect(str(_CACHE_DB), timeout=10) as c:
            row = c.execute(
                "SELECT response, created_at FROM llm_cache WHERE cache_key=?",
                (cache_key,)
            ).fetchone()
            if row and (time.time() - row[1]) < _CACHE_TTL:
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


def llm_chat(system: str, user: str, max_tokens: int = 4096, model: str | None = None,
             label: str = "", use_cache: bool = True, max_retries: int | None = None) -> str | None:
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
        cached = _get_cached(cache_key)
        if cached is not None:
            _record_usage(label, model, cache_hit=True)
            return cached
    
    try:
        from litellm import completion

        if not _budget_reserve(max_tokens, label):
            return None

        r = completion(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.2,
            **{**_completion_options(model), **({"num_retries": max_retries} if max_retries is not None else {})},
        )
        pre_hit, pre_miss = _log_cache_usage(r, label or "chat")
        response = (r.choices[0].message.content or "").strip()
        usage = getattr(r, "usage", None) or {}
        _record_usage(label, model, input_tokens=getattr(usage, "prompt_tokens", None),
                      output_tokens=getattr(usage, "completion_tokens", None),
                      reserved_tokens=max_tokens,
                      cache_hit_tokens=pre_hit, cache_miss_tokens=pre_miss)
        
        # 缓存响应
        if use_cache and response:
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
        from litellm import completion

        if not _budget_reserve(max_tokens, label):
            return None

        def _call(**extra):
            r = completion(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,
                **_completion_options(model),
                **extra)
            pre = _log_cache_usage(r, label or "chat_multi")
            ch = r.choices[0]
            return ((ch.message.content or "").strip(), getattr(ch, "finish_reason", None),
                    getattr(r, "usage", None) or {}, pre)

        content, finish, usage, pre = _call()
        if not content and finish == "length":
            content, _, usage, pre = _call(reasoning_effort="low")
        # 记录实际用量；缓存命中在入口处单独记录
        _record_usage(label, model, input_tokens=getattr(usage, "prompt_tokens", None),
                      output_tokens=getattr(usage, "completion_tokens", None),
                      reserved_tokens=max_tokens,
                      cache_hit_tokens=pre[0], cache_miss_tokens=pre[1])
        
        # 缓存响应
        if use_cache and content:
            _set_cached(cache_key, content, model, label)
        
        return content or None
    except Exception as exc:
        _set_last_error(f"LLM 接口调用失败：{type(exc).__name__}: {exc}")
        log.exception("LLM multi-chat failed: model=%s label=%s", model, label)
        return None
