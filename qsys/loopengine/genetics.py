"""LoopEngine 遗传操作：随机树、变异、交叉、参数扰动、字段权重、自适应预算。"""

import random
from collections import deque

from loopengine.tree import FIELDS, OPS, WINDOWS, Leaf, Node

MAX_DEPTH = 6


# ---------------------------------------------------------------- 随机生成
def random_tree(rng: random.Random, depth: int, field_weights: dict | None = None):
    if depth <= 1 or (depth >= 3 and rng.random() < 0.3):
        return Leaf(_pick_field(rng, field_weights))
    op = rng.choice(list(OPS.keys()))
    arity, windowed, kind = OPS[op]
    if kind == "rank":
        child_dim = "val"
        children = [_gen_with_dim(rng, depth - 1, field_weights, child_dim) for _ in range(arity)]
    elif kind == "same":
        d = rng.choice(["val", "rank"])
        children = [_gen_with_dim(rng, depth - 1, field_weights, d) for _ in range(arity)]
    else:  # keep
        children = [_gen_with_dim(rng, depth - 1, field_weights, None)]
    window = _pick_window(rng) if windowed else None
    return Node(op, children, window)


def _gen_with_dim(rng, depth, field_weights, dim):
    t = random_tree(rng, depth, field_weights)
    tries = 0
    while dim and t.dim() != dim and tries < 6:
        t = random_tree(rng, depth, field_weights)
        tries += 1
    return t


def _pick_field(rng, field_weights):
    if field_weights:
        fields = list(field_weights.keys())
        weights = [field_weights[f] for f in fields]
        return rng.choices(fields, weights=weights, k=1)[0]
    return rng.choice(FIELDS)


def _pick_window(rng):
    # 文章发现中长窗口更富矿：50-100 加权
    weights = [1 if w < 50 else (4 if 50 <= w <= 100 else (2 if w <= 200 else 1)) for w in WINDOWS]
    return rng.choices(WINDOWS, weights=weights, k=1)[0]


# ---------------------------------------------------------------- 子树操作
def _paths(tree, prefix=()):
    yield prefix, tree
    if isinstance(tree, Node):
        for i, ch in enumerate(tree.children):
            yield from _paths(ch, prefix + (i,))


def _get(tree, path):
    node = tree
    for i in path:
        node = node.children[i]
    return node


def _set(tree, path, new_node):
    if not path:
        return new_node
    root = _clone(tree)
    parent = _get(root, path[:-1]) if len(path) > 1 else root
    if len(path) == 1:
        return new_node if path[0] == -1 else _replace_root(root, path[0], new_node)
    parent.children[path[-1]] = new_node
    return root


def _replace_root(root, idx, new_node):
    root.children[idx] = new_node
    return root


def _clone(t):
    if isinstance(t, Leaf):
        return Leaf(t.field)
    return Node(t.op, [_clone(ch) for ch in t.children], t.window)


def mutate(tree, rng, field_weights):
    paths = [(p, n) for p, n in _paths(tree) if p]
    if not paths:
        return random_tree(rng, 3, field_weights)
    path, node = rng.choice(paths)
    new_sub = random_tree(rng, min(node.depth() + 1, MAX_DEPTH - len(path)), field_weights)
    tries = 0
    while new_sub.dim() != node.dim() and tries < 6:
        new_sub = random_tree(rng, min(node.depth() + 1, MAX_DEPTH - len(path)), field_weights)
        tries += 1
    return _set(tree, path, new_sub)


def crossover(t1, t2, rng):
    p1 = [(p, n) for p, n in _paths(t1) if p]
    p2 = [(p, n) for p, n in _paths(t2)]
    if not p1:
        return _clone(t1)
    rng.shuffle(p2)
    path1, node1 = rng.choice(p1)
    for path2, node2 in p2:
        if node2.dim() == node1.dim():
            return _set(t1, path1, _clone(node2))
    return _clone(t1)


def perturb(tree, rng, momentum: dict):
    """窗口参数微调：momentum = {骨架签名: 方向±1}（由入库历史引导）。"""
    t = _clone(tree)
    win_paths = [(p, n) for p, n in _paths(t) if isinstance(n, Node) and n.window is not None]
    if not win_paths:
        return t
    path, node = rng.choice(win_paths)
    sk = node.sexpr().split("(")[0]
    direction = momentum.get(sk, rng.choice([-1, 1]))
    idx = WINDOWS.index(node.window) if node.window in WINDOWS else 3
    idx = max(0, min(len(WINDOWS) - 1, idx + direction))
    node.window = WINDOWS[idx]
    return t


def mutate_operator(tree, rng, field_weights):
    """算子变异：保持结构，改变算子类型。

    例如：ma(close,20) → ema(close,20)
         sub(x,y) → add(x,y)
    """
    t = _clone(tree)
    node_paths = [(p, n) for p, n in _paths(t) if isinstance(n, Node)]

    if not node_paths:
        return random_tree(rng, 3, field_weights)

    # 随机选择一个节点
    path, node = rng.choice(node_paths)

    # 找到兼容的算子（相同维度和arity）
    current_op = node.op
    compatible_ops = []
    for op_name, (arity, windowed, kind) in OPS.items():
        if op_name == current_op:
            continue
        if arity == node.arity:
            # 检查维度兼容性
            if kind == "rank" and node.dim() == "val":
                continue
            compatible_ops.append(op_name)

    if not compatible_ops:
        return t

    # 选择一个兼容的算子
    new_op = rng.choice(compatible_ops)
    node.op = new_op

    # 如果新算子需要窗口，而原算子没有，添加窗口
    new_arity, new_windowed, new_kind = OPS[new_op]
    if new_windowed and node.window is None:
        node.window = _pick_window(rng)
    elif not new_windowed:
        node.window = None

    return t


def perturb_field(tree, rng, field_weights):
    """字段扰动：切换到相近字段。

    例如：close → open, volume → amount
    """
    t = _clone(tree)
    leaf_paths = [(p, n) for p, n in _paths(t) if isinstance(n, Leaf)]

    if not leaf_paths:
        return t

    # 随机选择一个叶子节点
    path, leaf = rng.choice(leaf_paths)

    # 定义相近字段组
    related_fields = {
        "close": ["open", "high", "low"],
        "open": ["close", "high", "low"],
        "high": ["close", "open", "low"],
        "low": ["close", "open", "high"],
        "volume": ["amount"],
        "amount": ["volume"],
    }

    current_field = leaf.field
    if current_field in related_fields:
        new_field = rng.choice(related_fields[current_field])
        leaf.field = new_field

    return t


def hill_climb(tree, rng, field_weights, fitness_fn, steps=5):
    """局部搜索：对优质因子做邻域优化。

    尝试小的扰动，如果适应度提升则保留。
    """
    best = tree
    best_fitness = fitness_fn(tree)

    for _ in range(steps):
        # 随机选择一种扰动方式
        perturbation_type = rng.choice(["window", "operator", "field"])

        if perturbation_type == "window":
            neighbor = perturb(best, rng, {})
        elif perturbation_type == "operator":
            neighbor = mutate_operator(best, rng, field_weights)
        else:
            neighbor = perturb_field(best, rng, field_weights)

        # 检查维度一致性
        if neighbor.dim() != tree.dim():
            continue

        neighbor_fitness = fitness_fn(neighbor)
        if neighbor_fitness > best_fitness:
            best = neighbor
            best_fitness = neighbor_fitness

    return best


# ---------------------------------------------------------------- 自适应预算
class Budget:
    SOURCES = ["mutate", "crossover", "perturb", "random", "llm", "mutate_op", "perturb_field"]

    def __init__(self):
        self.p = {
            "mutate": 0.20, "crossover": 0.20, "perturb": 0.12,
            "random": 0.12, "llm": 0.16,
            "mutate_op": 0.10, "perturb_field": 0.10,
        }
        self.history = deque(maxlen=50)

    def choose(self, rng) -> str:
        r = rng.random()
        acc = 0.0
        for s in self.SOURCES:
            acc += self.p[s]
            if r <= acc:
                return s
        return "random"

    def record(self, source: str, accepted: bool):
        self.history.append((source, accepted))
        self._adjust()

    def _adjust(self):
        if len(self.history) < 20:
            return
        rates = {}
        for s in self.SOURCES:
            recs = [a for src, a in self.history if src == s]
            rates[s] = (sum(recs) / len(recs)) if recs else 0.0
        best = max(rates, key=rates.get)
        worst = min(rates, key=rates.get)
        if rates[best] - rates[worst] > 0.05:
            self.p[best] = min(0.45, self.p[best] + 0.02)
            self.p[worst] = max(0.05, self.p[worst] - 0.02)
            total = sum(self.p.values())
            self.p = {s: v / total for s, v in self.p.items()}

    def to_json(self):
        return {"p": self.p, "history": list(self.history)}

    @classmethod
    def from_json(cls, d):
        b = cls()
        b.p = d.get("p", b.p)
        b.history = deque(d.get("history", []), maxlen=50)
        return b


# ---------------------------------------------------------------- 字段权重（数据驱动）
class FieldWeights:
    def __init__(self):
        self.w = {f: 1.0 for f in FIELDS}

    def boost_from_factors(self, sexprs: list[str]):
        """按入库因子的字段频次倾斜（富矿字段 ×1.5，封顶 ×4，对标文章 overnight 4x）。"""
        import re
        cnt = {f: 0 for f in FIELDS}
        for s in sexprs:
            for f in set(re.findall(r"[a-z_]+", s)):
                if f in cnt:
                    cnt[f] += 1
        if not any(cnt.values()):
            return
        top = max(cnt.values())
        for f in FIELDS:
            self.w[f] = min(4.0, 1.0 + 3.0 * cnt[f] / top) if cnt[f] else 0.5

    def to_json(self):
        return self.w

    @classmethod
    def from_json(cls, d):
        fw = cls()
        fw.w.update(d or {})
        return fw
