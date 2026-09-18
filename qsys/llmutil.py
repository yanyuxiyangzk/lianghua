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

# LLM 响应缓存
_CACHE_DB = DATA_DIR / "experience.db"
_CACHE_TTL = 86400  # 24小时


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


def _log_cache_usage(r, label: str = "") -> None:
    """从 LLM 响应中提取缓存命中信息并记录日志。"""
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
    except Exception:
        pass


def llm_available() -> bool:
    return bool(
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("LITELLM_PROXY_API_KEY")
    )


def llm_chat(system: str, user: str, max_tokens: int = 4096, model: str | None = None,
             label: str = "", use_cache: bool = True) -> str | None:
    """调用一次 chat completion，返回纯文本；无 key / 调用异常返回 None。
    use_cache: 是否使用响应缓存（默认启用，相同prompt直接返回缓存）。"""
    if not llm_available():
        return None
    
    model = model or _DEFAULT_MODEL
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    
    # 检查缓存
    if use_cache:
        cache_key = _get_cache_key(model, messages, max_tokens, 0.2)
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached
    
    try:
        from litellm import completion

        r = completion(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.2,
        )
        _log_cache_usage(r, label or "chat")
        response = (r.choices[0].message.content or "").strip()
        
        # 缓存响应
        if use_cache and response:
            _set_cached(cache_key, response, model, label)
        
        return response
    except Exception:
        return None


def llm_chat_multi(messages: list[dict], max_tokens: int = 4000, model: str | None = None,
                   label: str = "", use_cache: bool = True) -> str | None:
    """多轮对话版：[{"role": "system"|"user"|"assistant", "content": ...}] → 纯文本。
    fail-open 返回 None。推理模型（v4-pro）思维链与正文共享 max_tokens 配额：
    预算给足 4000；若思维链烧光配额导致正文为空（finish_reason=length），
    自动降思考强度（reasoning_effort=low）重试一次。
    use_cache: 是否使用响应缓存（默认启用）。"""
    if not llm_available():
        return None
    
    model = model or _DEFAULT_MODEL
    
    # 检查缓存
    if use_cache:
        cache_key = _get_cache_key(model, messages, max_tokens, 0.3)
        cached = _get_cached(cache_key)
        if cached is not None:
            return cached
    
    try:
        from litellm import completion

        def _call(**extra):
            r = completion(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,
                **extra)
            _log_cache_usage(r, label or "chat_multi")
            ch = r.choices[0]
            return (ch.message.content or "").strip(), getattr(ch, "finish_reason", None)

        content, finish = _call()
        if not content and finish == "length":
            content, _ = _call(reasoning_effort="low")
        
        # 缓存响应
        if use_cache and content:
            _set_cached(cache_key, content, model, label)
        
        return content or None
    except Exception:
        return None
