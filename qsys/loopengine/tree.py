"""LoopEngine 表达式树：词汇、求值（向量化）、代码发射（h5 兼容）。

设计：
  - 树以 S 表达式为规范形式：sub(ma(overnight,20),delta(ma(overnight,20),5))
  - 求值用 datetime×instrument 帧（列向量化，单因子 <100ms）
  - 通过因子发射与 daily_pv.h5 兼容的 python 代码 → 直接进既有因子体系
"""

import re

# ---------------------------------------------------------------- 字段（由面板派生）
FIELDS = ["open", "high", "low", "close", "volume", "amount", "vwap",
          "overnight", "amplitude", "upper_shadow", "lower_shadow", "hl_ratio", "body_ratio"]
WINDOWS = [3, 5, 10, 15, 20, 30, 40, 60, 90, 120, 150, 200]
MAX_DEPTH = 6

# 算子表：name: (arity, windowed, dim_out)
OPS = {
    "sub": (2, False, "same"), "mul": (2, False, "same"), "div": (2, False, "same"),
    "corr": (2, True, "rank"),
    "abs": (1, False, "keep"), "sign": (1, False, "keep"),
    "rank_cs": (1, False, "rank"),
    "ma": (1, True, "keep"), "ts_min": (1, True, "keep"), "ts_max": (1, True, "keep"),
    "ts_rank": (1, True, "rank"), "decay_linear": (1, True, "keep"),
    "std": (1, True, "keep"), "skew": (1, True, "keep"),
    "delta": (1, True, "keep"), "roc": (1, True, "keep"),
}


# ---------------------------------------------------------------- 树结构
class Leaf:
    def __init__(self, field):
        self.field = field

    def sexpr(self):
        return self.field

    def depth(self):
        return 1

    def dim(self):
        return "val"


class Node:
    def __init__(self, op, children, window=None):
        self.op = op
        self.children = children
        self.window = window

    def sexpr(self):
        w = f",{self.window}" if self.window is not None else ""
        return f"{self.op}({','.join(ch.sexpr() for ch in self.children)}{w})"

    def depth(self):
        return 1 + max(ch.depth() for ch in self.children)

    def dim(self):
        kind = OPS[self.op][2]
        if kind == "rank":
            return "rank"
        if kind == "keep":
            return self.children[0].dim()
        return "val"  # same → 调用方需自行校验子维一致


# ---------------------------------------------------------------- S 表达式解析
def parse(s: str):
    """解析 S 表达式为树；失败返回 None。"""
    s = s.strip()
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[(),]", s)
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def eat(t=None):
        tok = peek()
        if t and tok != t:
            raise ValueError(f"期望 {t} 得 {tok}")
        pos[0] += 1
        return tok

    def parse_node():
        tok = eat()
        if tok in FIELDS:
            return Leaf(tok)
        if tok not in OPS:
            raise ValueError(f"未知符号 {tok}")
        op = tok
        eat("(")
        arity = OPS[op][0]
        children = []
        for i in range(arity):
            if i > 0:
                eat(",")          # 兄弟参数间的逗号分隔符
            children.append(parse_node())
        window = None
        if OPS[op][1]:
            eat(",")
            window = int(eat())
        eat(")")
        return Node(op, children, window)

    try:
        tree = parse_node()
        if pos[0] != len(toks):
            return None
        return tree
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------- 求值（datetime×instrument 帧，列向量化）
def build_field_frames(panel):
    """面板 → 字段帧 dict。panel 为 (instrument, datetime) 索引。"""
    p = panel.unstack("instrument")
    pc = p["$close"].shift(1)
    oc_max = p[["$open", "$close"]].max(axis=1)
    oc_min = p[["$open", "$close"]].min(axis=1)
    return {
        "open": p["$open"], "high": p["$high"], "low": p["$low"], "close": p["$close"],
        "volume": p["$volume"], "amount": p["$amount"],
        "vwap": p["$amount"] / (p["$volume"] + 1e-12),
        "overnight": p["$open"] / (pc + 1e-12) - 1,
        "amplitude": (p["$high"] - p["$low"]) / (pc + 1e-12),
        "upper_shadow": (p["$high"] - oc_max) / (pc + 1e-12),
        "lower_shadow": (oc_min - p["$low"]) / (pc + 1e-12),
        "hl_ratio": p["$high"] / (p["$low"] + 1e-12),
        "body_ratio": (p["$close"] - p["$open"]) / (p["$high"] - p["$low"] + 1e-12),
    }


def evaluate_tree(tree, frames):
    """返回因子值 DataFrame（datetime×instrument）。"""
    def ev(t):
        if isinstance(t, Leaf):
            return frames[t.field]
        op = t.op
        args = [ev(ch) for ch in t.children]
        w = t.window
        if op == "sub":
            return args[0] - args[1]
        if op == "mul":
            return args[0] * args[1]
        if op == "div":
            return args[0] / (args[1] + 1e-12)
        if op == "abs":
            return args[0].abs()
        if op == "sign":
            return args[0].apply(lambda x: 0) if False else args[0].applymap(lambda x: (x > 0) - (x < 0))
        if op == "rank_cs":
            return args[0].rank(axis=1, pct=True)
        if op == "ma":
            return args[0].rolling(w).mean()
        if op == "ts_min":
            return args[0].rolling(w).min()
        if op == "ts_max":
            return args[0].rolling(w).max()
        if op == "ts_rank":
            return args[0].rolling(w).apply(lambda x: x.rank(pct=True).iloc[-1] if len(x) == w else float("nan"))
        if op == "decay_linear":
            weights = list(range(1, w + 1))
            return args[0].rolling(w).apply(lambda x: float((x * weights).sum() / sum(weights)) if len(x) == w else float("nan"))
        if op == "std":
            return args[0].rolling(w).std()
        if op == "skew":
            return args[0].rolling(w).skew()
        if op == "delta":
            return args[0].diff(w)
        if op == "roc":
            return args[0].pct_change(w)
        if op == "corr":
            return args[0].rolling(w).corr(args[1])
        raise ValueError(f"未知算子 {op}")

    return ev(tree)


# ---------------------------------------------------------------- 代码发射（daily_pv.h5 兼容）
def emit_code(sexpr: str, factor_name: str) -> str:
    """生成与 RD-Agent 因子同构的 python 代码（读 daily_pv.h5 写 result.h5）。
    首行注释携带 S 表达式（供演化引擎父本选择时解析，且不影响执行）。"""
    return f'''# sexpr: {sexpr}
import pandas as pd
import numpy as np

df = pd.read_hdf('daily_pv.h5', key='data').sort_index()
p = df.unstack('instrument')
_pc = p['$close'].shift(1)
_oc_max = p[['$open', '$close']].max(axis=1)
_oc_min = p[['$open', '$close']].min(axis=1)
FRAMES = {{
    'open': p['$open'], 'high': p['$high'], 'low': p['$low'], 'close': p['$close'],
    'volume': p['$volume'], 'amount': p['$amount'],
    'vwap': p['$amount'] / (p['$volume'] + 1e-12),
    'overnight': p['$open'] / (_pc + 1e-12) - 1,
    'amplitude': (p['$high'] - p['$low']) / (_pc + 1e-12),
    'upper_shadow': (p['$high'] - _oc_max) / (_pc + 1e-12),
    'lower_shadow': (_oc_min - p['$low']) / (_pc + 1e-12),
    'hl_ratio': p['$high'] / (p['$low'] + 1e-12),
    'body_ratio': (p['$close'] - p['$open']) / (p['$high'] - p['$low'] + 1e-12),
}}

def _ev(t):
    if isinstance(t, str):
        return FRAMES[t]
    op, args, w = t
    a = [_ev(x) for x in args]
    if op == 'sub': return a[0] - a[1]
    if op == 'mul': return a[0] * a[1]
    if op == 'div': return a[0] / (a[1] + 1e-12)
    if op == 'abs': return a[0].abs()
    if op == 'sign': return a[0].applymap(lambda x: (x > 0) - (x < 0))
    if op == 'rank_cs': return a[0].rank(axis=1, pct=True)
    if op == 'ma': return a[0].rolling(w).mean()
    if op == 'ts_min': return a[0].rolling(w).min()
    if op == 'ts_max': return a[0].rolling(w).max()
    if op == 'ts_rank': return a[0].rolling(w).apply(lambda x: x.rank(pct=True).iloc[-1] if len(x) == w else np.nan)
    if op == 'decay_linear':
        _w = list(range(1, w + 1))
        return a[0].rolling(w).apply(lambda x: float((x * _w).sum() / sum(_w)) if len(x) == w else np.nan)
    if op == 'std': return a[0].rolling(w).std()
    if op == 'skew': return a[0].rolling(w).skew()
    if op == 'delta': return a[0].diff(w)
    if op == 'roc': return a[0].pct_change(w)
    if op == 'corr': return a[0].rolling(w).corr(a[1])
    raise ValueError(op)

TREE = {sexpr!r}
import re as _re
def _parse(s):
    toks = _re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\\d+|[(),]", s)
    pos = [0]
    def eat():
        t = toks[pos[0]]; pos[0] += 1; return t
    def node():
        t = eat()
        if t.isdigit():
            return int(t)
        if t in FRAMES:
            return t
        eat0 = eat()
        assert eat0 == '(', eat0
        args = []
        while toks[pos[0]] != ')':
            if toks[pos[0]] == ',':
                eat()
            args.append(node())
        eat()
        w = None
        if args and isinstance(args[-1], int):
            w = args.pop()
        return (t, args, w)
    return node()

tree = _parse(TREE)
X = _ev(tree)
result = X.stack().rename('{factor_name}').dropna().to_frame()
result = result[['{factor_name}']].astype(float)
result.to_hdf('result.h5', key='data', mode='w')
'''
