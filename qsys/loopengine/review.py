"""LoopEngine 审查规则：维度一致性、复杂度、深度（生成后置过滤）。"""

from loopengine.tree import MAX_DEPTH, Node


def review(tree) -> tuple[bool, str]:
    """返回 (通过, 原因)。"""
    if tree is None:
        return False, "解析失败"
    if tree.depth() > MAX_DEPTH:
        return False, f"深度 {tree.depth()} > {MAX_DEPTH}"
    if isinstance(tree, Node) is False:
        return False, "单叶子无意义"

    def _check(t, path=""):
        if isinstance(t, Node):
            op = t.op
            dims = [ch.dim() for ch in t.children]
            if op in ("mul", "div", "sub") and dims[0] != dims[1]:
                return f"跨量纲运算 {op}({dims[0]},{dims[1]})"
            if op == "corr" and t.children[0].dim() != t.children[1].dim():
                return "corr 子维不一致"
            if op == "div" and isinstance(t.children[1], Node) is False and t.children[1].dim() == "rank":
                return "除以排名型无量纲分母（易近零爆炸）"
            for i, ch in enumerate(t.children):
                r = _check(ch, f"{path}/{op}[{i}]")
                if r:
                    return r
        return None

    err = _check(tree)
    if err:
        return False, err

    # 最小复杂度：至少含一个窗口算子
    def _has_window(t):
        if isinstance(t, Node):
            if t.window is not None:
                return True
            return any(_has_window(ch) for ch in t.children)
        return False

    if not _has_window(tree):
        return False, "缺少时序算子（最小复杂度）"
    return True, ""


def skeleton_of(tree) -> str:
    """结构骨架签名：算子序列@字段集合（用于 FSA）。"""
    ops, fields = [], set()

    def _walk(t):
        if isinstance(t, Node):
            ops.append(t.op)
            for ch in t.children:
                _walk(ch)
        else:
            fields.add(t.field)

    _walk(tree)
    return f"{'-'.join(ops[:6])}@{','.join(sorted(fields))}"
