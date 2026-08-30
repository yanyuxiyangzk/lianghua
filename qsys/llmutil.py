"""QSYS 共享 LLM 通道：用看板已配置的 DeepSeek（或 .env 的 CHAT_MODEL）做文本增强。

复用 .env 里的 CHAT_MODEL / DEEPSEEK_API_KEY（与 RD-Agent 同源），
不引入新依赖——litellm 已随 pydantic-ai-slim 进入 qsys 镜像。
失败时 fail-open：返回 None，由调用方决定兜底展示。
"""

import os

_DEFAULT_MODEL = os.environ.get("CHAT_MODEL") or "deepseek/deepseek-chat"


def llm_available() -> bool:
    return bool(
        os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("LITELLM_PROXY_API_KEY")
    )


def llm_chat(system: str, user: str, max_tokens: int = 1024, model: str | None = None) -> str | None:
    """调用一次 chat completion，返回纯文本；无 key / 调用异常返回 None。"""
    if not llm_available():
        return None
    try:
        from litellm import completion

        r = completion(
            model=model or _DEFAULT_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception:
        return None
