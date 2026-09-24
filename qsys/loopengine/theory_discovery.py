"""理论发现引擎（Theory Discovery Engine）

自动发现市场模式 → 生成假说 → 形式化为因子 → 统计验证 → 命名入库。

设计目标：
1. 不依赖已有理论，从数据中发现未知规律
2. 自动组合现有理论元素创造新理论
3. 每次运行产生新创意，避免重复

核心循环：
  模式发现 → 假说生成 → S表达式形式化 → 统计验证 → 理论命名 → 知识图谱更新
     ↑                                                          ↓
     └──────────────── 反馈循环（已验证理论指导下一轮发现）──────────┘
"""

import json
import logging
import random
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("theory_discovery")

# ---------------------------------------------------------------- 理论知识图谱

# 已知理论库（用于组合创新）
KNOWN_THEORIES = {
    "动量": {
        "definition": "强者恒强，趋势延续",
        "math_core": "roc(close, N)",
        "variants": [
            "roc(close, N)",
            "mul(roc(close, N), div(volume, ma(volume, N)))",
            "ts_rank(close, N)",
            "delta(close, N)",
        ],
        "fields": ["close", "volume"],
        "windows": [5, 10, 20, 60],
    },
    "均值回归": {
        "definition": "偏离均值后回归",
        "math_core": "zscore(close, N)",
        "variants": [
            "zscore(close, N)",
            "div(sub(close, ma(close, N)), std(close, N))",
            "sub(0, roc(close, 5))",
        ],
        "fields": ["close", "volume"],
        "windows": [5, 10, 20],
    },
    "趋势": {
        "definition": "趋势一旦形成不会轻易改变",
        "math_core": "ma(close, N) > ma(close, M)",
        "variants": [
            "sub(ma(close, N), ma(close, M))",
            "ts_rank(div(delta(close, N), std(close, N)), M)",
        ],
        "fields": ["close"],
        "windows": [5, 10, 20, 60, 120],
    },
    "波动率": {
        "definition": "波动率聚集与均值回归",
        "math_core": "std(close, N)",
        "variants": [
            "std(close, N)",
            "div(std(close, N), std(close, M))",
            "skew(close, N)",
        ],
        "fields": ["close", "high", "low"],
        "windows": [10, 20, 60],
    },
    "流动性": {
        "definition": "流动性溢价与流动性风险",
        "math_core": "div(volume, turnover)",
        "variants": [
            "div(volume, ma(volume, N))",
            "ts_rank(volume, N)",
        ],
        "fields": ["volume", "turnover"],
        "windows": [5, 10, 20],
    },
    "量价背离": {
        "definition": "量价关系异常预示反转",
        "math_core": "corr(close, volume, N)",
        "variants": [
            "corr(close, volume, N)",
            "mul(roc(close, N), sign(delta(volume, N)))",
        ],
        "fields": ["close", "volume"],
        "windows": [10, 20],
    },
    "情绪": {
        "definition": "市场情绪周期影响收益",
        "math_core": "zscore(volume, 20) * sign(delta(close, 5))",
        "variants": [
            "mul(zscore(volume, 20), sign(delta(close, 5)))",
            "div(sub(volume, ma(volume, 20)), std(volume, 20))",
        ],
        "fields": ["close", "volume"],
        "windows": [5, 10, 20],
    },
    "支撑阻力": {
        "definition": "价格在支撑/阻力位反弹或突破",
        "math_core": "div(sub(close, ts_min(close, N)), sub(ts_max(close, N), ts_min(close, N)))",
        "variants": [
            "div(sub(close, ts_min(close, N)), sub(ts_max(close, N), ts_min(close, N)))",
            "sub(close, ts_min(close, N))",
        ],
        "fields": ["close", "high", "low"],
        "windows": [20, 60, 120],
    },
    "资金流": {
        "definition": "资金流向决定价格方向",
        "math_core": "mul(delta(close, N), sign(delta(volume, N)))",
        "variants": [
            "mul(delta(close, N), sign(delta(volume, N)))",
            "corr(close, volume, N)",
        ],
        "fields": ["close", "volume"],
        "windows": [5, 10, 20],
    },
    "龙虎榜": {
        "definition": "机构/游资席位资金流预示短期走势",
        "math_core": "需事件数据",
        "variants": [],
        "fields": [],
        "windows": [],
    },
    "缠论": {
        "definition": "笔/线段/中枢构成价格运动的基本单元",
        "math_core": "需K线形态识别",
        "variants": [],
        "fields": ["close", "high", "low", "open"],
        "windows": [],
    },
}

# 可组合的理论元素
COMBINABLE_ELEMENTS = {
    "时间框架": ["5日", "10日", "20日", "60日", "120日", "250日"],
    "数学操作": ["差分", "比率", "标准化", "排名", "相关性", "波动率"],
    "数据源": ["价格", "成交量", "换手率", "涨跌幅", "振幅"],
    "组合方式": ["乘积", "条件", "分层", "轮动"],
}

# ---------------------------------------------------------------- 模式发现器

class PatternDiscovery:
    """从数据中发现未知模式——无监督方法。"""
    
    @staticmethod
    def _col(panel: pd.DataFrame, name: str) -> str:
        """兼容 $close 和 close 两种列名。"""
        if name in panel.columns:
            return name
        if f"${name}" in panel.columns:
            return f"${name}"
        return name

    @staticmethod
    def discover_anomalies(panel: pd.DataFrame, codes: list[str], end: str) -> list[dict]:
        """发现异常模式：量价异动、波动率突变、相关性断裂等。"""
        patterns = []
        col = PatternDiscovery._col

        try:
            # 1. 量价异动：成交量突然放大但价格不动
            vol_col = col(panel, "volume")
            close_col = col(panel, "close")
            if vol_col in panel.columns and close_col in panel.columns:
                vol_ma = panel[vol_col].rolling(20).mean()
                vol_ratio = panel[vol_col] / (vol_ma + 1e-12)
                price_change = panel[close_col].pct_change(5).abs()
                
                # 量涨价不涨（可能吸筹）
                anomaly = (vol_ratio > 2.0) & (price_change < 0.02)
                if anomaly.any():
                    patterns.append({
                        "type": "量价异动",
                        "description": "成交量突然放大但价格未动，可能在吸筹",
                        "severity": float(vol_ratio[anomaly].mean()),
                        "candidates": list(panel.index[anomaly][:5]),
                    })
            
            # 2. 波动率突变：波动率突然放大
            if close_col in panel.columns:
                ret = panel[close_col].pct_change()
                vol_20 = ret.rolling(20).std()
                vol_5 = ret.rolling(5).std()
                vol_ratio = vol_5 / (vol_20 + 1e-12)
                
                spike = vol_ratio > 2.0
                if spike.any():
                    patterns.append({
                        "type": "波动率突变",
                        "description": "短期波动率突然放大，可能有事件驱动",
                        "severity": float(vol_ratio[spike].mean()),
                        "candidates": list(panel.index[spike][:5]),
                    })
            
            # 3. 趋势加速：价格偏离均线加速
            if close_col in panel.columns:
                ma20 = panel[close_col].rolling(20).mean()
                deviation = (panel[close_col] - ma20) / (ma20 + 1e-12)
                accel = deviation.diff(5)
                
                surge = accel.abs() > 0.05
                if surge.any():
                    patterns.append({
                        "type": "趋势加速",
                        "description": "价格偏离均线加速，趋势可能延续或反转",
                        "severity": float(accel[surge].abs().mean()),
                        "candidates": list(panel.index[surge][:5]),
                    })
            
            # 4. 流动性枯竭：成交量持续萎缩
            if vol_col in panel.columns:
                vol_ma = panel[vol_col].rolling(20).mean()
                vol_trend = vol_ma.pct_change(10)
                
                drought = vol_trend < -0.3
                if drought.any():
                    patterns.append({
                        "type": "流动性枯竭",
                        "description": "成交量持续萎缩，可能有大行情酝酿",
                        "severity": float(abs(vol_trend[drought].mean())),
                        "candidates": list(panel.index[drought][:5]),
                    })
            
            # 5. 相关性断裂：量价相关性突然改变
            if close_col in panel.columns and vol_col in panel.columns:
                corr_20 = panel[close_col].rolling(20).corr(panel[vol_col])
                corr_5 = panel[close_col].rolling(5).corr(panel[vol_col])
                corr_change = (corr_5 - corr_20).abs()
                
                break_ = corr_change > 0.5
                if break_.any():
                    severity = float(corr_change[break_].mean())
                    if not np.isfinite(severity):
                        severity = 0.5
                    patterns.append({
                        "type": "相关性断裂",
                        "description": "量价关系突然改变，市场结构可能变化",
                        "severity": severity,
                        "candidates": list(panel.index[break_][:5]),
                    })
                    
        except Exception as e:
            log.warning(f"模式发现失败: {e}")
        
        return patterns

    @staticmethod
    def discover_regimes(panel: pd.DataFrame, codes: list[str]) -> list[dict]:
        """发现市场状态转换模式。"""
        patterns = []
        col = PatternDiscovery._col
        close_col = col(panel, "close")
        
        try:
            if close_col not in panel.columns:
                return patterns
            
            # 计算市场整体指标（按日期聚合）
            market_ret = panel[close_col].groupby(level="datetime").mean().pct_change()
            market_vol = market_ret.rolling(20).std()
            
            # 状态转换检测
            bull = market_ret.rolling(5).mean() > 0
            transition = bull != bull.shift(1)
            
            if transition.any():
                trans_dates = list(bull.index[transition])
                patterns.append({
                    "type": "市场状态转换",
                    "description": f"检测到{len(trans_dates)}次牛熊转换",
                    "severity": len(trans_dates) / max(len(market_ret), 1),
                    "candidates": trans_dates[:5],
                })
            
            # 波动率状态
            high_vol = market_vol > market_vol.quantile(0.8)
            vol_regime_change = high_vol != high_vol.shift(1)
            if vol_regime_change.any():
                patterns.append({
                    "type": "波动率状态转换",
                    "description": "波动率从高到低或从低到高转换",
                    "severity": float(vol_regime_change.sum() / max(len(vol_regime_change), 1)),
                    "candidates": [],
                })
                
        except Exception as e:
            log.warning(f"状态转换发现失败: {e}")
        
        return patterns


# ---------------------------------------------------------------- 假说生成器

class HypothesisGenerator:
    """将模式转化为可检验假说——LLM + 理论模板。"""
    
    SYSTEM_PROMPT = """你是量化研究员，负责将市场模式转化为可检验的假说。

你需要：
1. 分析给定的市场模式
2. 结合金融理论提出假说
3. 将假说形式化为S表达式

输出格式（JSON）：
{
  "hypotheses": [
    {
      "name": "假说名称（简短）",
      "theory": "所属理论（动量/均值回归/趋势/波动率/流动性/情绪/支撑阻力/资金流/其他）",
      "description": "假说描述",
      "math": "数学表达式（可选）",
      "sexpr": "S表达式",
      "confidence": 0.0-1.0,
      "evidence": "支持证据"
    }
  ]
}

S表达式格式要求（严格遵守）：
- 使用函数调用风格，不是Lisp风格
- 正确示例: mul(zscore(close, 20), sign(delta(close, 5)))
- 错误示例: (mul (zscore close 20) (sign (delta close 5)))
- 窗口参数紧跟在逗号后面: ma(close, 20) 不是 ma(close 20)
- 没有add算子，用sub代替加法: sub(a, sub(0, b)) 等价于 a+b

可用算子（只能用这些）：
一元: abs, sign, rank_cs, ma, ts_min, ts_max, ts_rank, decay_linear, std, skew, delta, roc, ema, zscore, log1p
二元: sub, mul, div, corr
字段: close, open, high, low, volume, turnover, vwap, overnight, prev_close

注意：
- 每个假说必须有明确的经济逻辑
- S表达式必须语法正确，只能使用上述算子
- 避免过拟合（窗口参数不要太大）
- 鼓励创新组合"""
    
    @staticmethod
    def generate(patterns: list[dict], theories: dict = None,
                 track_record: dict = None, budget=None) -> list[dict]:
        """从模式生成假说。track_record：理论战绩（有效/已证伪），注入 user 段
        明令禁止重提已证伪方向——前缀纪律：system 常量不变，战绩只在 user。"""
        if not patterns:
            return []

        try:
            from llmutil import llm_chat

            # 前缀缓存纪律：system 为常量（稳定前缀），变动内容全部在 user 且以固定
            # 引导句开头；max_tokens/temperature 固定，模型别名统一由 llmutil 路由。

            # 构建prompt
            pattern_text = "\n".join([
                f"- {p['type']}: {p['description']}（严重度: {p['severity']:.2f}）"
                for p in patterns[:5]
            ])

            theory_text = ""
            if theories:
                theory_text = "\n已知理论参考：\n" + "\n".join([
                    f"- {name}: {t['definition']}（核心: {t['math_core']}）"
                    for name, t in list(theories.items())[:10]
                ])

            record_text = ""
            if track_record:
                eff = track_record.get("effective") or []
                fals = track_record.get("falsified") or []
                if eff:
                    record_text += "\n已验证有效的理论方向（可借鉴但不要照抄）：\n" + "\n".join(
                        f"- {e['name']}（{e['family']}）IC={e['ic']:.3f}" for e in eff)
                if fals:
                    record_text += ("\n已被证伪的方向（不要重复提出，证伪的是具体模式而非"
                                    "整个机制族）：\n" + "\n".join(
                        f"- {f['name']}（{f['family']}）：{f['reason']}" for f in fals))

            user_prompt = f"""发现的市场模式：
{pattern_text}
{theory_text}
{record_text}

请为每个模式提出1-2个可检验的假说，并形式化为S表达式。"""
            
            from theory_policy import Budget
            budget = budget or Budget("unscoped-theory-hypothesis", calls=0)
            text = budget.chat(HypothesisGenerator.SYSTEM_PROMPT, user_prompt,
                               max_tokens=1500, label="theory_hypothesis") or ""
            return _extract_hypotheses(text)

        except Exception as e:
            from theory_policy import BudgetExceeded
            if isinstance(e, BudgetExceeded):
                raise
            log.warning(f"假说生成失败: {e}")

        return []


def _extract_hypotheses(text: str) -> list[dict]:
    """从（可能被 max_tokens 截断的）LLM 输出提取假说。

    先整包解析；失败则逐对象挽救——截断的 JSON 数组里完整对象仍然可用，
    不让一次截断浪费整次 LLM 调用（假说对象无嵌套花括号，可安全逐块提取）。
    """
    import re
    if not text:
        return []
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            return data.get("hypotheses", [])
        except json.JSONDecodeError:
            pass
    out = []
    for m in re.finditer(r"\{[^{}]*\"sexpr\"[^{}]*\}", text, re.S):
        try:
            item = json.loads(m.group(0))
            if isinstance(item, dict) and item.get("sexpr"):
                out.append(item)
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------- 形式化器

class Formalizer:
    """将假说转化为S表达式——语法约束。"""
    
    @staticmethod
    def validate_sexpr(sexpr: str) -> tuple[bool, str]:
        """验证S表达式语法。"""
        try:
            from loopengine.tree import parse
            tree = parse(sexpr)
            if tree is None:
                return False, "解析失败"
            return True, "OK"
        except Exception as e:
            return False, str(e)
    
    @staticmethod
    def repair_sexpr(sexpr: str) -> str:
        """修复常见语法错误。"""
        # 移除多余空格
        sexpr = " ".join(sexpr.split())
        # 确保括号匹配
        depth = 0
        result = []
        for ch in sexpr:
            if ch == "(":
                depth += 1
                result.append(ch)
            elif ch == ")":
                depth -= 1
                if depth >= 0:
                    result.append(ch)
            else:
                result.append(ch)
        # 补齐缺失括号
        while depth > 0:
            result.append(")")
            depth -= 1
        return "".join(result)
    
    @staticmethod
    def generate_variants(base_sexpr: str, max_variants: int = 5) -> list[str]:
        """从基础表达式生成变体。"""
        variants = [base_sexpr]
        
        try:
            from loopengine.tree import parse
            tree = parse(base_sexpr)
            if tree is None:
                return variants
            
            # 变体策略
            strategies = [
                # 1. 窗口参数扰动
                lambda t: _perturb_windows(t),
                # 2. 嵌套一层ma
                lambda t: f"ma({t.sexpr()}, 10)",
                # 3. zscore标准化
                lambda t: f"zscore({t.sexpr()}, 20)",
                # 4. rank归一化
                lambda t: f"rank_cs({t.sexpr()})",
            ]
            
            rng = random.Random()
            for _ in range(max_variants - 1):
                strategy = rng.choice(strategies)
                try:
                    new_sexpr = strategy(tree)
                    valid, _ = Formalizer.validate_sexpr(new_sexpr)
                    if valid and new_sexpr not in variants:
                        variants.append(new_sexpr)
                except Exception:
                    continue
                    
        except Exception as e:
            log.debug(f"变体生成失败: {e}")
        
        return variants[:max_variants]


def _perturb_windows(tree) -> str:
    """扰动窗口参数。"""
    import re
    sexpr = tree.sexpr()
    # 找到所有窗口参数并随机调整
    def replace_window(m):
        op = m.group(1)
        n = int(m.group(2))
        # 在合理范围内扰动
        new_n = max(3, min(120, n + random.choice([-5, -2, 0, 2, 5])))
        return f"{op}({m.group(3)}, {new_n})"
    
    return re.sub(r"(ma|std|delta|roc|ts_rank|zscore|ts_min|ts_max|decay_linear|corr)\(([^,]+),\s*(\d+)\)", 
                  replace_window, sexpr)


# ---------------------------------------------------------------- 验证器

class TheoryValidator:
    """统计检验假说——IC/ICIR/IC胜率 + Walk-forward。"""
    
    @staticmethod
    def validate_factor(sexpr: str, panel: pd.DataFrame, codes: list[str], 
                       end: str, fwd_days: int = 5) -> dict:
        """验证单个因子。"""
        result = {
            "sexpr": sexpr,
            "valid": False,
            "ic_mean": None,
            "icir": None,
            "ic_winrate": None,
            "sharpe": None,
            "max_drawdown": None,
            "error": None,
        }
        
        try:
            import signals as sig
            import factor_eval as fe
            
            # 计算因子值
            from loopengine.tree import build_field_frames, evaluate_tree, parse
            tree = parse(sexpr)
            if tree is None:
                result["error"] = "解析失败"
                return result
            
            s = evaluate_tree(tree, build_field_frames(panel)).stack()
            s.index = s.index.set_names(["datetime", "instrument"])
            
            if s.dropna().empty:
                result["error"] = "因子值全为NaN"
                return result
            
            # IC分析（使用 factor_eval.ic_series）
            fwd = fe.forward_returns(panel, fwd_days)
            ic_series = fe.ic_series(s, fwd)
            if ic_series is None or ic_series.empty:
                result["error"] = "IC计算失败"
                return result
            
            ic_mean = float(ic_series.mean())
            ic_std = float(ic_series.std())
            icir = ic_mean / (ic_std + 1e-12)
            ic_winrate = float((ic_series > 0).mean())
            
            result["ic_mean"] = ic_mean
            result["icir"] = icir
            result["ic_winrate"] = ic_winrate
            
            # Walk-forward验证
            factor_vals = {f"factor_{hash(sexpr) % 10000}": s}
            wf = fe.walk_forward(factor_vals, panel, "ICIR加权", 10, 
                                fwd_days=fwd_days, step=10, min_factors=1)
            
            if not wf.empty and "优化组合扣费超额" in wf.columns:
                net = wf["优化组合扣费超额"]
                result["sharpe"] = float(net.mean() / (net.std() + 1e-12) * (252/fwd_days)**0.5)
                result["max_drawdown"] = float(net.cumsum().diff().min())
            
            # 判断是否通过（放宽门槛，先让引擎能产出结果）
            result["valid"] = (
                abs(ic_mean) > 0.01 and
                abs(icir) > 0.1 and
                ic_winrate > 0.42
            )
            
        except Exception as e:
            result["error"] = str(e)
            log.warning(f"因子验证失败: {e}")
        
        return result


# ---------------------------------------------------------------- 理论命名器

class TheoryNamer:
    """给验证通过的假说命名——LLM + 自动分类。"""
    
    SYSTEM_PROMPT = """你是量化理论命名专家。给以下验证通过的因子/假说起一个简洁的名字。

命名规则：
1. 名字应反映因子的经济逻辑
2. 名字应简洁（2-4个汉字）
3. 避免使用已有理论的名字
4. 可以组合多个概念

输出格式：{"name": "理论名", "family": "机制族", "description": "一句话描述"}"""
    
    @staticmethod
    def name_theory(sexpr: str, validation_result: dict, pattern: dict) -> dict:
        """稳定程序命名，不调用LLM。"""
        from theory_policy import digest
        return {"name": "候选_" + digest(sexpr)[:16], "family": "其他",
                "description": "待独立验证的研究假设"}


# ---------------------------------------------------------------- 知识图谱

class KnowledgeGraph:
    """存储已发现理论及其关系。"""
    
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or Path("/data/market.db")
        self._ensure_table()
    
    def _ensure_table(self):
        """确保 theory_graph 表存在。"""
        import sqlite3
        with sqlite3.connect(str(self.db_path), timeout=30) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS theory_graph (
                name TEXT PRIMARY KEY,
                theory TEXT,
                family TEXT,
                sexpr TEXT,
                validation TEXT,
                pattern TEXT,
                created_at TEXT
            )""")
    
    def save_theory(self, name: str, theory: dict):
        """保存理论到知识图谱。"""
        import sqlite3
        
        def _json_safe(obj):
            """递归转换为JSON可序列化对象。"""
            if hasattr(obj, 'isoformat'):
                return str(obj)
            elif isinstance(obj, dict):
                return {k: _json_safe(v) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return [_json_safe(x) for x in obj]
            elif isinstance(obj, (int, float, str, bool, type(None))):
                return obj
            else:
                return str(obj)
        
        with sqlite3.connect(str(self.db_path), timeout=30) as c:
            c.execute(
                "INSERT OR REPLACE INTO theory_graph VALUES (?,?,?,?,?,?,?)",
                (name,
                 json.dumps(_json_safe(theory.get("theory", {})), ensure_ascii=False),
                 theory.get("family", "其他"),
                 theory.get("sexpr", ""),
                 json.dumps(_json_safe(theory.get("validation", {})), ensure_ascii=False),
                 json.dumps(_json_safe(theory.get("pattern", {})), ensure_ascii=False),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
    
    def get_theories(self, family: str = None) -> list[dict]:
        """获取已发现理论。"""
        import sqlite3
        with sqlite3.connect(str(self.db_path), timeout=30) as c:
            if family:
                rows = c.execute(
                    "SELECT name, theory, family, sexpr, validation, pattern FROM theory_graph WHERE family=?",
                    (family,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT name, theory, family, sexpr, validation, pattern FROM theory_graph"
                ).fetchall()
            
            return [
                {
                    "name": r[0],
                    "theory": json.loads(r[1] or "{}"),
                    "family": r[2],
                    "sexpr": r[3],
                    "validation": json.loads(r[4] or "{}"),
                    "pattern": json.loads(r[5] or "{}"),
                }
                for r in rows
            ]
    
    def get_family_coverage(self) -> dict:
        """获取理论族覆盖情况。"""
        theories = self.get_theories()
        coverage = {}
        for t in theories:
            family = t.get("family", "其他")
            if family not in coverage:
                coverage[family] = {"count": 0, "avg_ic": 0, "avg_icir": 0}
            coverage[family]["count"] += 1
            v = t.get("validation", {})
            if v.get("ic_mean"):
                coverage[family]["avg_ic"] += abs(v["ic_mean"])
            if v.get("icir"):
                coverage[family]["avg_icir"] += abs(v["icir"])
        
        # 计算平均值
        for family in coverage:
            n = coverage[family]["count"]
            if n > 0:
                coverage[family]["avg_ic"] /= n
                coverage[family]["avg_icir"] /= n
        
        return coverage


# ---------------------------------------------------------------- 理论战绩回喂

def load_theory_track_record(limit_effective: int = 8,
                             limit_falsified: int = 20) -> dict:
    """理论战绩：已注册理论因子的闸门结果与现状，回喂下一轮假说生成。

    effective = 过闸（gate_status=1）的理论方向（供借鉴）；
    falsified = 被拒（gate_status=0）的方向 + 最近拒绝原因（gate_detail_log），
    下一轮 prompt 明令禁止重提——理论引擎不再每周从零开始。
    """
    record = {"effective": [], "falsified": []}
    try:
        import library
        reg = library.get_factor_registry()
        if reg.empty or "engine" not in reg.columns:
            return record
        theory = reg[reg["engine"] == "theory"]
        if theory.empty:
            return record
        names = [str(n) for n in theory["name"]]
        marks = ",".join("?" * len(names))
        logs, ic_map = {}, {}
        with library._lconn() as c:
            try:
                for name, reasons in c.execute(
                        f"SELECT factor_name, fail_reasons FROM gate_detail_log "
                        f"WHERE passed=0 AND factor_name IN ({marks}) "
                        f"ORDER BY gate_date DESC", names).fetchall():
                    logs.setdefault(name, str(reasons or ""))
            except Exception:
                pass
            try:
                for name, ic in c.execute(
                        f"SELECT name, AVG(ic) FROM factor_ic_daily "
                        f"WHERE name IN ({marks}) GROUP BY name", names).fetchall():
                    ic_map[name] = ic
            except Exception:
                pass
        for _, row in theory.iterrows():
            name = str(row.get("name", ""))
            family = str(row.get("family") or "其他")
            gs = row.get("gate_status")
            try:
                gs = int(gs) if gs is not None and gs == gs else None
            except (TypeError, ValueError):
                gs = None
            if gs == 1:
                record["effective"].append({
                    "name": name, "family": family,
                    "ic": float(ic_map.get(name) or 0.0)})
            elif gs == 0:
                reason = logs.get(name, "")
                record["falsified"].append({
                    "name": name, "family": family,
                    "skeleton": _skeleton_of_name_code(name, _code_of(row)),
                    "reason": reason[:120] or "未过硬闸门"})
        # 压缩规模：有效按 |IC| 排序截取，证伪按机制族去重后截取
        record["effective"].sort(key=lambda e: -abs(e["ic"]))
        seen_fam, falsified = set(), []
        for f in record["falsified"]:
            if f["family"] in seen_fam:
                continue
            seen_fam.add(f["family"])
            falsified.append(f)
        record["effective"] = record["effective"][:limit_effective]
        record["falsified"] = falsified[:limit_falsified]
    except Exception as e:
        log.warning(f"理论战绩加载失败（按无历史处理）: {e}")
    return record


def _code_of(row) -> str:
    for key in ("code", "sexpr"):
        v = row.get(key)
        if v:
            return str(v)
    return ""


def _skeleton_of_name_code(name: str, code: str) -> str:
    """由任意 sexpr/代码构造骨架签名（结构同构即命中，窗口参数天然归一）。"""
    if not code:
        return ""
    wrapped = code if code.startswith("# sexpr: ") else f"# sexpr: {code}"
    try:
        import structure
        return structure.extract_skeleton(name, wrapped)
    except Exception:
        return ""


def _filter_falsified(hypotheses: list[dict], track_record: dict) -> list[dict]:
    """丢弃与已证伪方向结构同构的假说（防止 LLM 换皮重提同一想法）。"""
    skels = {f["skeleton"] for f in (track_record or {}).get("falsified", [])
             if f.get("skeleton")}
    if not skels:
        return hypotheses
    out = []
    for h in hypotheses:
        sk = _skeleton_of_name_code(str(h.get("name", "")), str(h.get("sexpr", "")))
        if sk and sk in skels:
            log.info(f"丢弃与已证伪方向同构的假说: {h.get('name')}")
            continue
        out.append(h)
    return out


# ---------------------------------------------------------------- 主引擎

class TheoryDiscoveryEngine:
    """理论发现引擎主入口。"""
    
    def __init__(self, pool_name: str = "沪深300"):
        self.pool_name = pool_name
        self.kg = KnowledgeGraph()
    
    def run(self, max_patterns: int = 5, max_hypotheses: int = 10, 
            max_validate: int = 5) -> dict:
        """运行一轮理论发现。"""
        import signals as sig
        from common import all_pools, get_last_trade_day
        
        log.info("=== 理论发现引擎启动 ===")
        
        # 1. 获取数据
        codes = all_pools().get(self.pool_name) or all_pools().get("沪深300")
        end = get_last_trade_day()
        panel = sig.get_panel_cached(codes, end)
        
        if panel is None or panel.empty:
            return {"error": "数据获取失败"}
        
        # 2. 模式发现
        log.info("Step 1: 模式发现")
        pd_ = PatternDiscovery()
        patterns = pd_.discover_anomalies(panel, codes, end)
        patterns += pd_.discover_regimes(panel, codes)
        patterns = patterns[:max_patterns]
        log.info(f"发现 {len(patterns)} 个模式")
        
        from theory_policy import Budget, BudgetExceeded, claim_discovery, digest
        if not patterns or not np.isfinite(panel.select_dtypes(include="number").to_numpy()).any():
            return {"skipped": "无新增有效数据或模式"}
        data_hash = digest([list(map(str, panel.columns)), pd.util.hash_pandas_object(panel, index=True).tolist()])
        week, reason = claim_discovery(data_hash, digest(patterns))
        if not week:
            return {"skipped": reason}
        budget = Budget("discovery:" + week, calls=1, tokens=16000, input_chars=6000)

        # 3. 假说生成（回喂理论战绩：有效方向借鉴、已证伪方向禁提+同构丢弃）
        log.info("Step 2: 假说生成")
        hg = HypothesisGenerator()
        track_record = load_theory_track_record()
        try:
            hypotheses = hg.generate(patterns, KNOWN_THEORIES, track_record, budget=budget)
        except BudgetExceeded as exc:
            return {"skipped": str(exc)}
        hypotheses = _filter_falsified(hypotheses, track_record)
        hypotheses = hypotheses[:max_hypotheses]
        log.info(f"生成 {len(hypotheses)} 个假说"
                 f"（战绩: 有效{len(track_record['effective'])} 证伪{len(track_record['falsified'])}）")
        
        # 4. 形式化与验证
        log.info("Step 3: 形式化与验证")
        formalizer = Formalizer()
        validator = TheoryValidator()
        validated = []
        
        for h in hypotheses:
            sexpr = h.get("sexpr", "")
            if not sexpr:
                continue
            
            # 生成变体
            variants = formalizer.generate_variants(sexpr, max_variants=3)
            
            for v in variants:
                valid, msg = formalizer.validate_sexpr(v)
                if not valid:
                    continue
                
                result = validator.validate_factor(v, panel, codes, end)
                if result["valid"]:
                    validated.append({
                        **h,
                        "sexpr": v,
                        "validation": result,
                    })
                    break
        
        validated = validated[:max_validate]
        log.info(f"验证通过 {len(validated)} 个假说")
        
        # 5. 命名与入库
        log.info("Step 4: 命名与入库")
        namer = TheoryNamer()
        discovered = []
        registered = 0
        
        for v in validated:
            # 匹配原始模式
            pattern = next((p for p in patterns if p["type"] in v.get("description", "")), patterns[0] if patterns else {})
            
            # 转换pattern为JSON可序列化格式
            pattern_clean = {}
            for k, val in pattern.items():
                if hasattr(val, 'isoformat'):
                    pattern_clean[k] = str(val)
                elif isinstance(val, list):
                    pattern_clean[k] = [str(x) if hasattr(x, 'isoformat') else x for x in val]
                else:
                    pattern_clean[k] = val
            
            name_result = namer.name_theory(v["sexpr"], v.get("validation", {}), pattern_clean)
            
            from theory_policy import research_theory_id
            theory_data = {
                "theory": {**v, "theory_id": research_theory_id(v["sexpr"]),
                           "hypothesis_id": name_result["name"]},
                "family": name_result.get("family", "其他"),
                "sexpr": v["sexpr"],
                "validation": v.get("validation", {}),
                "pattern": pattern_clean,
            }
            
            self.kg.save_theory(name_result["name"], theory_data)
            # 闭环：注册为因子（engine='theory'），当晚 gate_check 按正式硬闸门准入
            if register_theory_factor(name_result["name"], v["sexpr"],
                                      name_result.get("family", "其他"),
                                      v.get("validation", {})):
                registered += 1
            discovered.append({
                "name": name_result["name"],
                "family": name_result.get("family", "其他"),
                "sexpr": v["sexpr"],
                "ic_mean": v.get("validation", {}).get("ic_mean"),
                "icir": v.get("validation", {}).get("icir"),
                "description": name_result.get("description", ""),
            })
        
        log.info(f"=== 理论发现完成: {len(discovered)} 个新理论 ===")
        
        return {
            "patterns": len(patterns),
            "hypotheses": len(hypotheses),
            "validated": len(validated),
            "registered": registered,
            "discovered": discovered,
            "coverage": self.kg.get_family_coverage(),
        }


# ---------------------------------------------------------------- CLI入口

def register_theory_factor(name: str, sexpr: str, family: str, validation: dict) -> bool:
    """把验证通过的理论因子注册进 factor_registry（engine='theory'），闭合
    理论发现 → 因子生产 的环路。

    gate_status 保持 NULL：当晚 job_gate_check 会按正式硬闸门评估——发现阶段的
    宽松门槛（|IC|>0.01 等）只负责产出候选，生产准入由硬闸门决定。
    """
    try:
        import library
        from theory_policy import research_theory_id
        code = f"# sexpr: {sexpr}\n# theory: {name}（{family}）"
        library.sync_factor_registry([{
            "name": f"theory_{name}", "kind": "loopengine", "code": code,
            "engine": "theory", "generation_mode": "theory_discovery",
            "factor_type": "量价", "theory_id": research_theory_id(sexpr),
            "theory_family": family, "hypothesis_id": name,
            "source_theory_sexpr": sexpr, "validation_status": "shadow_only",
        }])
        return True
    except Exception as e:
        log.warning(f"理论因子注册失败 {name}: {e}")
        return False


def run_theory_discovery(pool_name: str = "沪深300", **kwargs) -> str:
    """CLI入口：运行理论发现引擎。"""
    engine = TheoryDiscoveryEngine(pool_name)
    result = engine.run(**kwargs)
    
    if "skipped" in result:
        return "理论发现跳过：" + result["skipped"]
    if "error" in result:
        return f"理论发现失败: {result['error']}"
    
    lines = [f"理论发现完成:"]
    lines.append(f"  模式: {result['patterns']}个")
    lines.append(f"  假说: {result['hypotheses']}个")
    lines.append(f"  验证通过: {result['validated']}个")
    lines.append(f"  入库: {len(result['discovered'])}个 · 注册因子: {result.get('registered', 0)}个")
    
    for d in result["discovered"]:
        ic = d.get('ic_mean')
        icir = d.get('icir')
        ic_str = f"{ic:.4f}" if ic is not None else "N/A"
        icir_str = f"{icir:.3f}" if icir is not None else "N/A"
        lines.append(f"  - {d['name']} ({d['family']}): IC={ic_str} ICIR={icir_str}")
        lines.append(f"    {d['sexpr']}")
    
    return "\n".join(lines)


if __name__ == "__main__":
    print(run_theory_discovery())
