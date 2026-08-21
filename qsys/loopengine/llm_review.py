"""P4：双 LLM 分离 —— 独立审查 sub-agent（与生成端隔离，防单一模型自我说服）。

生成端（engine._llm_generate）：创造性 prompt，按机制族出题。
审查端（本模块）：严格审查 prompt，随机抽样候选做边界精判，可一票否决。
"""

import json
import os

_REVIEWER_SYS = (
    "你是量化表达式审查员，职责是【挑剔地】审查量化因子 S 表达式的工程与经济合理性。"
    "只输出 JSON：{\"verdict\": \"pass\" 或 \"reject\", \"reason\": \"一句话\"}。"
)

_REVIEWER_USER = """审查以下 A 股日频因子表达式：
{sexpr}

检查项（任一不满足即 reject）：
1. 量纲：mul/div/sub 两端维度是否一致（价格型不得与排名型混算）
2. 除零/爆炸风险：分母是否可能恒近零
3. 窗口合理性：窗口参数与字段语义是否匹配（如 ts_rank 窗口≤5 意义有限）
4. 经济含义：表达式是否有可解释的因果/行为金融逻辑，还是纯噪声拼凑
5. 结构冗余：是否存在可化简的重复结构（如 ma(ma(x,N),N) 嵌套无增量）

只输出 JSON。"""


def llm_review(sexpr: str) -> tuple[bool, str]:
    """独立审查 sub-agent。无 key/调用失败时放行（fail-open，由硬闸门兜底）。"""
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return True, "no-llm"
    try:
        from litellm import completion

        r = completion(
            model="deepseek/deepseek-chat",
            messages=[{"role": "system", "content": _REVIEWER_SYS},
                      {"role": "user", "content": _REVIEWER_USER.format(sexpr=sexpr)}],
            max_tokens=150)
        text = r.choices[0].message.content.strip().strip("`")
        if text.startswith("json"):
            text = text[4:]
        d = json.loads(text)
        verdict = str(d.get("verdict", "pass")).lower()
        return verdict == "pass", str(d.get("reason", ""))[:120]
    except Exception:
        return True, "llm-error"
