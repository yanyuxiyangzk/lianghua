"""进化信号（战报蒸馏）共享模块：模式开关 / 嵌套 JSON 平衡抽取 / schema 校验 /
蒸馏 prompt 构建 / 信号渲染与新鲜度判定（交易日历）。

设计：docs/report-distill-evolution-plan.md v2（专家评审修订）。
铁律：信号只影响出题分布，永不改闸门；缺失/过期/低置信 = 照常跑（无信号不是错误）。
"""

import json
from datetime import datetime

MODES = ("off", "shadow", "prompt", "weights")
DEFAULT_MODE = "shadow"  # 缺省 shadow：蒸馏照常落库，引擎不改行为（rollout 第 1 周）

SIGNAL_MAX_BOOST = 0.5   # 单路 boost 上限 ±50%（全局乘数 cap×2 在引擎合成处）
TEXT_AGE_TD = 3          # 文本层有效期：3 个交易日
STRUCT_AGE_TD = 2        # 结构层有效期：2 个交易日（更新鲜才许动权重）
SUPPORT_KINDS = ("data", "narrative", "hypothesis")
REGIMES = ("bull", "bear", "sideways", "transition")


def get_mode() -> str:
    """DATA_DIR/evolution_signals.json 的 {"mode": ...}；缺省 shadow。
    每次调用重读（长驻调度器切换即时生效，与 norm_scheme 同款纪律）。"""
    try:
        from common import DATA_DIR, load_json
        m = (load_json(DATA_DIR / "evolution_signals.json", {}) or {}).get("mode", DEFAULT_MODE)
        return m if m in MODES else DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE


def extract_json_obj(text: str) -> dict | None:
    """从 LLM 输出抽取第一个括号平衡的 JSON 对象——支持嵌套（steer 段两层）、
    容忍代码围栏与推理模型前置思考文本；字符串内的花括号/转义正确处理。
    （llm_review._extract_json 的扁平正则抓不了嵌套——评审必修 2）"""
    t = text or ""
    i = t.find("{")
    if i < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(t)):
        ch = t[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[i:j + 1])
                except Exception:
                    return None
    return None


def is_degenerate_report(content: str) -> bool:
    """战报退化检测：LLM 不可用时落库的是错误提示文本，蒸馏它会产出垃圾信号
    （评审补充 5——"⚠️ LLM 服务不可用…"照样被 save_daily_report 落库）。"""
    c = (content or "").strip()
    return (not c) or c.startswith("⚠️") or ("LLM 服务不可用" in c) or len(c) < 200


def validate_signals(d: dict) -> tuple[bool, str, dict | None]:
    """schema 校验 + 清洗（boost clamp ±0.5，枚举剔除，长度截断）。
    返回 (ok, 原因, 清洗后信号)。宁空勿编：全空且无 regime_hint = 不通过。"""
    if not isinstance(d, dict):
        return False, "非 JSON 对象", None
    out = {
        "regime_hint": d.get("regime_hint") if d.get("regime_hint") in REGIMES else None,
        "insufficient_evidence": bool(d.get("insufficient_evidence")),
        "effective": [], "decaying": [], "hypotheses": [],
        "steer": {"families_boost": {}, "fields_boost": {}, "types_boost": {}},
        "confidence": 0.0,
    }
    try:
        out["confidence"] = max(0.0, min(1.0, float(d.get("confidence", 0.0))))
    except (TypeError, ValueError):
        pass
    if out["insufficient_evidence"]:
        # 显式"证据不足"是合法信号：方向段清空，只留 regime_hint/confidence
        return True, "", out
    for sect in ("effective", "decaying"):
        for item in d.get(sect) or []:
            if not isinstance(item, dict) or not item.get("target"):
                continue
            out[sect].append({
                "target": str(item["target"])[:40],
                "evidence": str(item.get("evidence", ""))[:120],
                "support": item["support"] if item.get("support") in SUPPORT_KINDS else "narrative",
            })
    out["hypotheses"] = [str(h)[:120] for h in (d.get("hypotheses") or []) if str(h).strip()][:3]
    for axis in ("families_boost", "fields_boost", "types_boost"):
        src = (d.get("steer") or {}).get(axis)
        if isinstance(src, dict):
            for k, v in src.items():
                try:
                    out["steer"][axis][str(k)[:30]] = max(-SIGNAL_MAX_BOOST,
                                                          min(SIGNAL_MAX_BOOST, float(v)))
                except (TypeError, ValueError):
                    continue
    if not (out["effective"] or out["decaying"] or out["hypotheses"] or out["regime_hint"]):
        return False, "空信号（无有效条目且无 regime_hint）", None
    return True, "", out


def build_distill_prompt(payload: dict) -> tuple[str, str]:
    """蒸馏 prompt（纯函数）——返回 (system, user) 元组，优化 DeepSeek 前缀缓存命中。

    System: 稳定内容（角色、schema、要求），跨调用不变，可被缓存。
    User: 变化内容（战报、排行榜、观察清单等），每次不同。

    payload 键：report_date/report_text/leaderboard_txt/watch_txt/sr_txt/sector_txt/chat_index_txt。"""
    # System: 稳定内容（~400 tokens），跨调用不变，可被 DeepSeek 前缀缓存
    system = (
        "你是量化研究助理。把「每日量化战报 + 结构化原料」蒸馏成明日因子进化的结构化信号。\n"
        "只输出一个 JSON 对象，不要任何解释。\n\n"
        "schema：\n"
        '{"regime_hint": "bull|bear|sideways|transition|null",'
        ' "insufficient_evidence": false,'
        ' "effective": [{"target": "机制族/因子类型/字段名", "evidence": "一句话",'
        ' "support": "data|narrative|hypothesis"}],'
        ' "decaying": [同 effective 结构],'
        ' "hypotheses": ["1~3 条前瞻机制假设"],'
        ' "steer": {"families_boost": {"族名": -0.5~0.5}, "fields_boost": {"字段名": ...},'
        ' "types_boost": {"因子类型名": ...}},'
        ' "confidence": 0.0~1.0}\n\n'
        "要求：\n"
        "1. 宁空勿编：原料里没有依据的方向不要写；证据不足就设 insufficient_evidence=true；\n"
        "2. support 标注来源：data=能对上排行榜/清单数字，narrative=战报叙事，hypothesis=推测；\n"
        "3. steer 的 key 必须是机制族名（动量/反转/资金流/爆量抢筹/支撑阻力…）、字段名或因子类型名，"
        "数值为明日出题加/减权强度（-0.5~0.5，不偏置可省略）；\n"
        '4. hypotheses 要具体到可检验的机制（如"开板回封次日缩量企稳的溢价"），不要泛泛而谈。'
    )

    # User: 变化内容（~1000-1500 tokens），每次不同
    user = (
        f"=== 战报（{payload.get('report_date', '')}）===\n{payload.get('report_text', '')}\n\n"
        f"=== 因子实战榜（重取自 DB，近5日口径）===\n{payload.get('leaderboard_txt', '')}\n\n"
        f"=== 涨停/异动观察清单（当日）===\n{payload.get('watch_txt', '')}\n\n"
        f"=== 支撑阻力共振 Top ===\n{payload.get('sr_txt', '')}\n\n"
        f"=== 板块资金流 Top/Bottom ===\n{payload.get('sector_txt', '')}\n\n"
        f"=== 复盘数据包索引（当日）===\n{payload.get('chat_index_txt', '')}\n"
    )

    return system, user


def render_for_prompt(sig: dict) -> str:
    """蒸馏信号的紧凑文本渲染——LLM 出题 prompt 证据段（替换原文 900 字截断）。"""
    lines = []
    if sig.get("regime_hint"):
        lines.append(f"市场状态提示: {sig['regime_hint']}")
    for title, key in (("验证有效方向", "effective"), ("失效规避方向", "decaying")):
        items = sig.get(key) or []
        if items:
            lines.append(title + ": " + "; ".join(
                f"{i['target']}（{i.get('support', '?')}: {i.get('evidence', '')}）"
                for i in items[:4]))
    return "\n".join(lines)


def hypotheses_of(sig: dict) -> list[str]:
    """前瞻假设（与 _family_fewshots 分槽渲染——它是待验证想法，不是已验证口味）。"""
    return list(sig.get("hypotheses") or [])[:3]


def signal_age_td(report_date: str, today: str) -> int | None:
    """report_date 到 today 相隔的交易日数（交易日历驱动；日历缺失回退自然日）。
    None=无法计算。report_date==today → 0。"""
    if not report_date or not today:
        return None
    try:
        from common import trade_day_offset
        for n in range(0, 10):
            if trade_day_offset(report_date, n) >= today:
                return n
        return None
    except Exception:
        try:
            return (datetime.strptime(today, "%Y-%m-%d")
                    - datetime.strptime(report_date, "%Y-%m-%d")).days
        except Exception:
            return None


def usable_layer(sig: dict, today: str, report_date: str | None = None) -> str:
    """信号今日可用的最深层级："struct"（结构层亦可）/ "text"（仅文本层）/ "none"（过期）。
    report_date 为 signals 行级列（不在 signals JSON 内），调用方应显式传入；
    缺省回退 sig["report_date"]（兼容内嵌形态）。
    护栏：confidence<0.4 或 insufficient_evidence → 最多 text（评审：低置信不动权重）。"""
    rd = report_date or sig.get("report_date") or ""
    age = signal_age_td(rd, today)
    if age is None or age > TEXT_AGE_TD:
        return "none"
    if sig.get("insufficient_evidence") or float(sig.get("confidence") or 0) < 0.4:
        return "text"
    return "struct" if age <= STRUCT_AGE_TD else "text"
