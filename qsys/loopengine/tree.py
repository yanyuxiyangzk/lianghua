"""LoopEngine 表达式树：词汇、求值（向量化）、代码发射（h5 兼容）。

设计：
  - 树以 S 表达式为规范形式：sub(ma(overnight,20),delta(ma(overnight,20),5))
  - 求值用 datetime×instrument 帧（列向量化，单因子 <100ms）
  - 通过因子发射与 daily_pv.h5 兼容的 python 代码 → 直接进既有因子体系
  - 支持多类型因子：量价/资金流/板块轮动/龙虎榜/盘口异动/指数
"""

import re

# ---------------------------------------------------------------- 基础量价字段（原有）
FIELDS = ["open", "high", "low", "close", "volume", "amount", "vwap",
          "overnight", "amplitude", "upper_shadow", "lower_shadow", "hl_ratio", "body_ratio"]
WINDOWS = [3, 5, 10, 15, 20, 30, 40, 60, 90, 120, 150, 200]
MAX_DEPTH = 6

# ---------------------------------------------------------------- 多类型因子字段定义
# 每种 factor_type 对应的字段列表（与 FIELDS 合并使用）
TYPE_FIELDS = {
    "资金流": ["main_net_pct", "super_net_pct", "big_net_pct", "mid_net_pct", "small_net_pct",
              "net_inflow_ratio", "main_small_spread"],
    "板块轮动": ["sector_momentum", "sector_net_flow", "sector_breadth",
                "sector_rank", "sector_amount_ratio", "sector_excess_ret"],
    "龙虎榜": ["lhb_net_buy", "lhb_inst_ratio", "lhb_hot_count",
              "lhb_win_rate", "lhb_consecutive"],
    "盘口异动": ["bid_ask_ratio", "outer_inner_ratio", "quantity_ratio_dev",
                "tick_vol_ratio", "bid_ask_spread"],
    "指数": ["idx_beta", "idx_rs", "idx_vol_ratio", "idx_corr", "idx_alpha"],
    "爆量抢筹": ["vol_spike", "bid_pressure", "outer_dominance",
                "accumulation_composite"],
    "财务": ["fin_np", "fin_or", "fin_gp", "fin_ncf",
            "fin_np_yoy", "fin_or_yoy", "fin_nm"],
    "支撑阻力": ["sr_dist_atr", "sr_res_dist", "sr_p_touch", "sr_p_hold",
                "sr_strength", "sr_resonance", "sr_vol_extr", "sr_score"],
    "事件记忆": ["days_since_limit", "limit_streak", "announce_7d"],
}

# 因子类型 → 默认机制族映射（用于新类型因子的族分类）
TYPE_FAMILY_MAP = {
    "资金流": "资金流",
    "板块轮动": "板块轮动",
    "龙虎榜": "龙虎榜",
    "盘口异动": "盘口异动",
    "指数": "指数",
    "财务": "财务",
    "支撑阻力": "支撑阻力",
    "事件记忆": "事件记忆",
}

# 所有可用字段（基础 + 当前类型）
def all_fields(factor_type: str | None = "量价") -> list[str]:
    """返回指定因子类型的完整字段列表。factor_type=None/"任意" → 全部类型的并集
    （评估/回测场景用——类型限制只约束生成端，评估端按 sexpr 实际引用放行）。"""
    if factor_type in (None, "任意"):
        return FIELDS + [f for fs in TYPE_FIELDS.values() for f in fs]
    extra = TYPE_FIELDS.get(factor_type, [])
    return FIELDS + extra


# ---------------------------------------------------------------- 字段语义/值域表
# LLM 出题 prompt 注入用：裸字段名会让模型对新类型（财务/事件记忆等）的量纲瞎猜，
# 产出"语法对但经济学无意义"的表达式，全靠下游闸门兜底（2026-09-16 prompt 审查发现 ①）。
FIELD_INFO = {
    # 基础量价
    "open": ("开盘价", "价格/元"), "high": ("最高价", "价格/元"),
    "low": ("最低价", "价格/元"), "close": ("收盘价", "价格/元"),
    "volume": ("成交量", "股数，重尾"), "amount": ("成交额", "元，重尾"),
    "vwap": ("成交均价", "价格/元"),
    "overnight": ("隔夜跳空收益（今开/昨收-1）", "比率，±0.1 内"),
    "amplitude": ("振幅（(高-低)/昨收）", "比率，0~0.2"),
    "upper_shadow": ("上影线比率", "比率，≥0"), "lower_shadow": ("下影线比率", "比率，≥0"),
    "hl_ratio": ("最高/最低价之比", "比率，≥1"),
    "body_ratio": ("实体比率（收-开）/（高-低）", "比率，-1~1"),
    # 资金流（占比类已归一；净额类重尾）
    "main_net_pct": ("主力净流入占比", "百分比，-100~100"),
    "super_net_pct": ("超大单净流入占比", "百分比，-100~100"),
    "big_net_pct": ("大单净流入占比", "百分比"), "mid_net_pct": ("中单净流入占比", "百分比"),
    "small_net_pct": ("小单（散户）净流入占比", "百分比"),
    "net_inflow_ratio": ("主力净流入/当日总成交", "比率，-1~1"),
    "main_small_spread": ("主力-散户净流差（20 日偏离）", "元，重尾"),
    # 板块轮动
    "sector_momentum": ("所属板块动量", "比率"), "sector_net_flow": ("板块净流入额", "元，重尾"),
    "sector_breadth": ("板块上涨家数占比", "0~1"), "sector_rank": ("板块全市场排名分位", "0~1"),
    "sector_amount_ratio": ("板块成交额占全市场比", "比率"),
    "sector_excess_ret": ("个股相对板块超额收益", "比率"),
    # 龙虎榜（计数/金额类：稀疏、多数票多数日为 0）
    "lhb_net_buy": ("龙虎榜净买入额", "元，重尾，多数为 0"),
    "lhb_inst_ratio": ("龙虎榜机构买入占比", "0~1，稀疏"),
    "lhb_hot_count": ("近 20 日上榜次数", "计数，多数为 0"),
    "lhb_win_rate": ("近 20 日上榜后净买为正占比", "0~1"),
    "lhb_consecutive": ("近 5 日连续上榜计数", "计数，多数为 0"),
    # 盘口异动
    "bid_ask_ratio": ("买盘/卖盘金额比", "比率>0，1 为均衡"),
    "outer_inner_ratio": ("外盘/内盘比", "比率>0"),
    "quantity_ratio_dev": ("量比相对其 20 日均值偏离", "比率，0 居中"),
    "tick_vol_ratio": ("逐笔量比（/20 日均值）", "比率，1 居中"),
    "bid_ask_spread": ("买卖价差", "元，重尾"),
    # 指数
    "idx_beta": ("对沪深300的β", "无量纲，1 居中"),
    "idx_rs": ("相对强弱（个股/基准累计收益比）", "比率，1 居中"),
    "idx_vol_ratio": ("个股成交量/基准量比", "比率，1 居中"),
    "idx_corr": ("与基准日收益 60 日相关", "-1~1"),
    "idx_alpha": ("年化α（剔β后）", "比率"),
    # 爆量抢筹
    "vol_spike": ("爆量倍数（当日量/均量）", "倍数，1 居中"),
    "bid_pressure": ("买盘压力", "比率"), "outer_dominance": ("外盘主导度", "比率"),
    "accumulation_composite": ("抢筹合成强度", "综合分"),
    # 财务（季度前值填充；货币类极重尾）
    "fin_np": ("净利润", "元，极重尾"), "fin_or": ("营业收入", "元，极重尾"),
    "fin_gp": ("毛利", "元，极重尾"), "fin_ncf": ("经营现金流", "元，可负，极重尾"),
    "fin_np_yoy": ("净利润同比", "百分比，可负"), "fin_or_yoy": ("营收同比", "百分比，可负"),
    "fin_nm": ("净利率（净利/营收）", "百分比"),
    # 支撑阻力（density_sr 产出，多数已归一）
    "sr_dist_atr": ("距最近支撑位", "ATR 倍数，≥0"), "sr_res_dist": ("距最近阻力位", "ATR 倍数，≥0"),
    "sr_p_touch": ("触及支撑概率", "0~1"), "sr_p_hold": ("支撑守住概率", "0~1"),
    "sr_strength": ("区间强度", "综合分"), "sr_resonance": ("多周期共振度", "综合分"),
    "sr_vol_extr": ("极端波动率标记", "比率"), "sr_score": ("支撑阻力机会分", "综合分"),
    # 事件记忆
    "days_since_limit": ("距上次涨停天数", "计数，≥1"),
    "limit_streak": ("当前连板高度", "计数，≥0，多数为 0"),
    "announce_7d": ("近 7 日公告数", "计数，≥0"),
}


def field_table(factor_type: str | None = "量价") -> str:
    """当前类型可用字段的"名称: 含义（量纲/值域）"清单文本，供 LLM 出题 prompt 注入。"""
    lines = []
    for f in all_fields(factor_type):
        info = FIELD_INFO.get(f)
        lines.append(f"  {f}: {info[0]}（{info[1]}）" if info else f"  {f}")
    return "\n".join(lines)

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
    # 新增算子：EMA、Z-score 标准化、资金流加速度
    "ema": (1, True, "keep"),
    "zscore": (1, True, "rank"),
    # P2: 非线性算子（log1p: 对称对数变换，处理重尾分布）
    "log1p": (1, False, "keep"),
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
        if kind == "same":
            return self.children[0].dim()  # 返回实际子维度（review已校验子维一致）
        return "val"


# ---------------------------------------------------------------- S 表达式解析
def parse(s: str, factor_type: str | None = "量价"):
    """解析 S 表达式为树；失败返回 None。factor_type 决定哪些字段名合法；
    传 None/"任意" 时不限类型（评估端用——生成端才需要类型约束）。"""
    s = s.strip()
    valid_fields = all_fields(factor_type)
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
        if tok in valid_fields:
            return Leaf(tok)
        if tok.isdigit():
            return Leaf(tok)  # 数值常量叶（取负习语 sub(0,x)；emit 运行时按 int 求值）
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
def build_field_frames(panel, extra_frames: dict | None = None):
    """面板 → 字段帧 dict。panel 为 (instrument, datetime) 索引。
    extra_frames: 可选的额外字段帧 dict（资金流/板块/龙虎榜/盘口/指数等），
                  与基础量价帧合并。"""
    p = panel.unstack("instrument")
    pc = p["$close"].shift(1)
    oc_max = p[["$open", "$close"]].max(axis=1)
    oc_min = p[["$open", "$close"]].min(axis=1)
    frames = {
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
    if extra_frames:
        frames.update(extra_frames)
    return frames


def evaluate_tree(tree, frames):
    """返回因子值 DataFrame（datetime×instrument）。"""
    def ev(t):
        if isinstance(t, Leaf):
            if t.field in frames:
                return frames[t.field]
            return float(t.field)  # 数值常量叶（sub(0,x) 的取负习语等）
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
            return args[0].map(lambda x: (x > 0) - (x < 0))
        if op == "rank_cs":
            return args[0].rank(axis=1, pct=True)
        if op == "ma":
            return args[0].rolling(w).mean()
        if op == "ts_min":
            return args[0].rolling(w).min()
        if op == "ts_max":
            return args[0].rolling(w).max()
        if op == "ts_rank":
            from scipy.stats import rankdata as _rankdata
            return args[0].rolling(w).apply(lambda x: float(_rankdata(x)[-1] / len(x)) if len(x) == w else float("nan"), raw=True)
        if op == "decay_linear":
            _w = w
            _wsum = sum(range(1, _w + 1))
            _weights = list(range(1, _w + 1))
            return args[0].rolling(_w).apply(lambda x: float(sum(x[i] * _weights[i] for i in range(len(x))) / _wsum) if len(x) == _w else float("nan"), raw=True)
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
        if op == "ema":
            return args[0].ewm(span=w, adjust=False).mean()
        if op == "zscore":
            m = args[0].rolling(w).mean()
            s = args[0].rolling(w).std()
            return (args[0] - m) / (s + 1e-12)
        if op == "log1p":
            return args[0].abs().log1p() * args[0].map(lambda x: (x > 0) - (x < 0))
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
    if op == 'sign': return a[0].map(lambda x: (x > 0) - (x < 0))
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
    if op == 'ema': return a[0].ewm(span=w, adjust=False).mean()
    if op == 'zscore':
        _m = a[0].rolling(w).mean()
        _s = a[0].rolling(w).std()
        return (a[0] - _m) / (_s + 1e-12)
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
