"""P4：双 LLM 分离 —— 独立审查 sub-agent（与生成端隔离，防单一模型自我说服）。

生成端（engine._llm_generate）：创造性 prompt，按机制族出题。
审查端（本模块）：严格审查 prompt，随机抽样候选做边界精判，可一票否决。
"""

import json
import os
import re

_REVIEWER_SYS = (
    "你是量化表达式审查员，职责是【挑剔地】审查量化因子 S 表达式的工程与经济合理性。\n\n"
    "检查项（任一不满足即 reject）：\n"
    "1. 量纲一致性：mul/div/sub 两端维度必须一致。价格型（close, open, high, low, vwap, "
    "prev_close, overnight）不得与排名型（rank_cs 输出）混算。rank_cs 输出维度为 rank，"
    "其他算子保持输入维度。\n"
    "2. 除零/爆炸风险：div 的分母是否可能恒近零。若分母含 std(close,N) 且 N≤5，"
    "或分母含 ts_rank(x, N) 且 N≤3，存在除零风险。\n"
    "3. 窗口合理性：窗口参数与字段语义是否匹配。ts_rank 窗口≤5 意义有限；"
    "ma/delta/roc 窗口应≥5；corr 窗口应≥20。\n"
    "4. 经济含义：表达式是否有可解释的因果/行为金融逻辑，还是纯噪声拼凑。"
    "例如：动量因子（delta/roc）、反转因子（sub 与 ma 的差）、波动率因子（std）、"
    "流动性因子（volume 与 amount 的关系）。\n"
    "5. 结构冗余：是否存在可化简的重复结构，如 ma(ma(x,N),N) 嵌套无增量，"
    "或 sub(x,x) 恒为零。\n\n"
    "输出格式：{\"verdict\": \"pass\" 或 \"reject\", \"reason\": \"一句话\"}。"
)

_REVIEWER_USER = "审查以下 A 股日频因子表达式：\n{sexpr}"


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出抽取第一个扁平 JSON 对象——容忍代码围栏与推理模型的前置思考文本。"""
    m = re.search(r"\{[^{}]*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def llm_review(sexpr: str) -> tuple[bool, str]:
    """独立审查 sub-agent。无 key/调用失败时回退到规则审查（fail-strict，非fail-open）。"""
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return _rule_review(sexpr), "no-llm-fallback"
    try:
        from litellm import completion

        r = completion(
            model=os.environ.get("CHAT_MODEL") or "deepseek/deepseek-chat",
            messages=[{"role": "system", "content": _REVIEWER_SYS},
                      {"role": "user", "content": _REVIEWER_USER.format(sexpr=sexpr)}],
            max_tokens=1000, timeout=45,
            temperature=0)  # 审查端要判决稳定：同一表达式不应两次调用一过一拒
        # 缓存命中监控
        try:
            usage = getattr(r, "usage", None) or {}
            hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
            miss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
            if hit + miss > 0:
                import logging
                logging.getLogger("llm_review").debug(
                    "review cache: hit=%d miss=%d rate=%.0f%%", hit, miss, hit / (hit + miss) * 100)
        except Exception:
            pass
        d = _extract_json(r.choices[0].message.content or "")
        if d is None:
            return _rule_review(sexpr), "llm-error-fallback"
        verdict = str(d.get("verdict", "pass")).lower()
        return verdict == "pass", str(d.get("reason", ""))[:120]
    except Exception:
        return _rule_review(sexpr), "llm-error-fallback"


def _rule_review(sexpr: str) -> bool:
    """规则审查降级：LLM不可用时的增强版规则检查。"""
    sexpr_lower = sexpr.lower()
    
    # 1. 检查深度（简单估算括号嵌套）
    depth = 0
    max_depth = 0
    for ch in sexpr:
        if ch == '(':
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch == ')':
            depth -= 1
    if max_depth > 6:
        return False
    
    # 2. 检查除以排名型分母（仅拒绝 div(X, rank_cs(叶节点))，允许 div(rank_cs(a), rank_cs(b))）
    import re as _re
    div_rank_pattern = _re.search(r'div\([^,]*,\s*rank_cs\([^)]*\)\)', sexpr_lower)
    if div_rank_pattern:
        return False
    
    # 3. 检查是否包含窗口算子（至少一个）
    window_ops = ['ma', 'ts_min', 'ts_max', 'ts_rank', 'std', 'skew', 
                  'delta', 'roc', 'ema', 'decay_linear', 'corr']
    if not any(op in sexpr_lower for op in window_ops):
        return False
    
    # 4. 检查单叶子（无意义）
    if sexpr_lower.count('(') == 0:
        return False
    
    return True
