"""LoopEngine 主引擎：状态持久化 + 每轮 生成→审查→验证→入库 + 自适应预算。

支持多类型因子挖掘：量价/资金流/板块轮动/龙虎榜/盘口异动/指数。
每种类型使用独立的字段帧，通过遗传算法自动搜索最优因子表达式。
"""

import json
import logging
import random
import re
from datetime import datetime

import pandas as pd

import gates as G
import factor_eval as fe
import library
import structure
import datasource
from event_bus import EventType, bus
from loopengine import genetics, review
from loopengine.tree import TYPE_FIELDS, all_fields, build_field_frames, emit_code, evaluate_tree, field_table, parse
from loopengine.regime import detect_regime, get_regime_factor_weight, detect_regime_from_reports

log = logging.getLogger("loopengine")

STATE_KEY = "loopengine"


# ---------------------------------------------------------------- LLM 出题辅助（纯函数，可单测）
def _extract_sexpr(text: str) -> str | None:
    """从 LLM 输出鲁棒抽取第一个 S 表达式——容忍代码围栏、"答案是："前缀、行内注释、
    多余解释行。抽取失败返回 None（调用方回退随机生成并计数）。"""
    text = (text or "").strip()
    for line in text.split("\n"):
        line = line.strip().strip("`").strip()
        m = re.search(r"[a-z_][a-z0-9_]*\(", line)
        if not m:
            continue
        depth = 0
        for j in range(m.start(), len(line)):
            ch = line[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return line[m.start():j + 1]
    return None


def _build_llm_prompt(fam: str, why: str, factor_type: str, evidence: str,
                      fewshots: list[str], hypotheses: list[str] | None = None,
                      theories: list[dict] | None = None
                      ) -> tuple[str, str]:
    """LLM 出题 prompt（纯函数）——返回 (system, user) 元组，优化 DeepSeek 前缀缓存命中。

    System: 稳定内容（角色、字段表、算子、规则、输出格式），跨调用不变，可被缓存。
    User: 变化内容（机制族、证据、few-shot、hypotheses、theories），每次不同。

    theories: 已发现的理论知识图谱条目，包含 name, family, sexpr, validation 等。
              用于指导 LLM 生成符合已验证理论模式的新因子。"""
    ops = "sub,mul,div,abs,sign,rank_cs,ma,ts_min,ts_max,ts_rank,decay_linear,std,skew,delta,roc,corr,ema,zscore"
    type_hint = f"（因子类型：{factor_type}）" if factor_type != "量价" else ""

    # System: 稳定内容（~600-800 tokens），跨调用不变，可被 DeepSeek 前缀缓存
    system = (
        "你是量化因子工程师。用以下 S 表达式语法写 A 股日频因子。\n"
        f"{type_hint}\n"
        f"字段（含含义与量纲）:\n{field_table(factor_type)}\n"
        f"算子: {ops}（窗口算子需带整数窗口，如 ma(close,20)）\n"
        "规则: 深度≤6，corr/mul/div/sub 两端维度一致，至少含一个窗口算子。\n"
        "只输出一个 S 表达式，如 sub(ma(overnight,20),delta(ma(overnight,20),5))，不要任何解释。"
    )

    # User: 变化内容（~100-300 tokens），每次不同
    user_parts = [f"写一个属于「{fam}」机制族的因子。（{why}）"]
    if evidence:
        user_parts.append(evidence)
    if fewshots:
        user_parts.append(
            "以下为该族已入库并通过统计闸门的真实因子（参考其结构与口味，不要照抄）：\n"
            + "\n".join(f"  {s}" for s in fewshots))
    if hypotheses:
        user_parts.append(
            "待验证机制假设（来自最新复盘蒸馏——是想法、不是已验证口味，可择优落地）：\n"
            + "\n".join(f"  - {h}" for h in hypotheses))
    if theories:
        theory_lines = []
        for t in theories[:3]:  # 最多3条理论指导
            name = t.get("name", "未知")
            sexpr = t.get("sexpr", "")
            family = t.get("family", "")
            theory_lines.append(f"  - {name}（{family}）: {sexpr}")
        user_parts.append(
            "已发现的市场理论（来自知识图谱，可借鉴其数学核心但不要直接复制）：\n"
            + "\n".join(theory_lines))

    return system, "\n\n".join(user_parts)

# 默认挖掘顺序：量价（主力）→ 资金流 → 板块轮动 → 指数 → 盘口异动 → 龙虎榜
DEFAULT_FACTOR_TYPES = ["量价", "资金流", "板块轮动", "指数", "盘口异动", "龙虎榜", "爆量抢筹", "财务",
                        "支撑阻力", "事件记忆"]


class LoopEngine:
    def __init__(self, pool_name: str = "沪深300"):
        self.pool_name = pool_name
        self.state = self._load_state()
        self._last_extra_frames = True
        self._signal_id_in_prompt = None  # 进化信号打标（record_tested 用），每候选重置

    # ---------------- 状态 ----------------
    def _load_state(self) -> dict:
        with library._lconn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS engine_state (
                id TEXT PRIMARY KEY, iteration INTEGER, budget TEXT,
                momentum TEXT, field_weights TEXT, accepted INTEGER, updated_at TEXT)""")
            row = c.execute("SELECT * FROM engine_state WHERE id=?", (STATE_KEY,)).fetchone()
        if row:
            return {"iteration": row[1], "budget": genetics.Budget.from_json(json.loads(row[2])),
                    "momentum": json.loads(row[3] or "{}"),
                    "field_weights": genetics.FieldWeights.from_json(json.loads(row[4] or "{}")),
                    "accepted": row[5] or 0}
        return {"iteration": 0, "budget": genetics.Budget(), "momentum": {},
                "field_weights": genetics.FieldWeights(), "accepted": 0}

    def _save_state(self):
        s = self.state
        with library._lconn() as c:
            c.execute(
                "INSERT OR REPLACE INTO engine_state (id, iteration, budget, momentum,"
                " field_weights, accepted, updated_at) VALUES (?,?,?,?,?,?,?)",
                (STATE_KEY, s["iteration"], json.dumps(s["budget"].to_json()),
                 json.dumps(s["momentum"]), json.dumps(s["field_weights"].to_json()),
                 s["accepted"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    # ---------------- 面板 ----------------
    def _frames(self, factor_type: str = "量价"):
        import signals as sig
        from common import all_pools, get_last_trade_day, trade_day_offset

        codes = all_pools()[self.pool_name]
        end = get_last_trade_day()
        panel = sig.get_panel_cached(codes, end, 1600, source=datasource.get_loop_source())
        if factor_type == "量价":
            # 防泄漏（2026-09-11 评审）：选拔/评估截到 end-250 交易日，
            # 最近一年对生成端不可见，留给评分卡 OOS 层做盲测。
            train_end = trade_day_offset(end, -250)
            panel = panel[panel.index.get_level_values("datetime") <= train_end]
        # 非量价类型（资金流/龙虎榜等）数据源历史仅 ~45 天，无 250 天盲测段可留——
        # 不截断，其验证完全依赖"发现后 OOS"（评分卡 ic_oos 逐日累积）
        extra = None
        if factor_type != "量价":
            from loopengine.extra_frames import build_extra_frames
            extra = build_extra_frames(factor_type, codes, end, lookback=1600)
        self._last_extra_frames = extra if factor_type != "量价" else True
        return panel, build_field_frames(panel, extra), codes, end

    # ---------------- 生成 ----------------
    def _gen_candidate(self, rng, gaps, proven, live_boost, factor_type: str = "量价",
                       regime: str | None = None, stats: dict | None = None):
        src = self.state["budget"].choose(rng)
        # 每轮独立生成预算，避免 batch 增大导致 LLM 调用线性膨胀
        import os
        gen_limit = int(os.environ.get("LLM_LOOPENGINE_GENERATE_LIMIT", "5"))
        if src == "llm" and stats is not None and stats.get("llm_gen_used", 0) >= gen_limit:
            src = "mutate"
        self._signal_id_in_prompt = None  # 每候选重置；仅 LLM 真正产出且 prompt 含信号时挂标
        fw = self.state["field_weights"].w
        if factor_type != "量价":
            # 随机/变异/交叉路径的字段采样池需带上该类型的专属字段
            # （此前 fw 只有基础量价字段，资金流/财务等类型字段永远采不到，
            #   等于非量价类型的随机生成名存实亡）
            fw = {**fw, **{f: 1.0 for f in TYPE_FIELDS.get(factor_type, [])}}
        if src == "llm":
            if stats is not None:
                stats["llm_gen_used"] = stats.get("llm_gen_used", 0) + 1
            tree = self._llm_generate(rng, gaps, proven, factor_type, stats=stats)
            if tree is None:  # LLM 失败回退随机树——随机产物不挂信号标
                tree = genetics.random_tree(rng, 4, fw)
                self._signal_id_in_prompt = None
        elif src == "mutate":
            parent = self._pick_parent(rng, live_boost, factor_type, regime)
            tree = genetics.mutate(parent, rng, fw) if parent else genetics.random_tree(rng, 4, fw)
        elif src == "crossover":
            p1 = self._pick_parent(rng, live_boost, factor_type, regime)
            p2 = self._pick_parent(rng, live_boost, factor_type, regime)
            tree = genetics.crossover(p1, p2, rng) if p1 and p2 else genetics.random_tree(rng, 4, fw)
        elif src == "perturb":
            parent = self._pick_parent(rng, live_boost, factor_type, regime)
            tree = genetics.perturb(parent, rng, self.state["momentum"]) if parent else genetics.random_tree(rng, 4, fw)
        else:
            tree = genetics.random_tree(rng, 4, fw)
        return src, tree

    def _pick_parent(self, rng, live_boost: dict | None = None, factor_type: str = "量价",
                     regime: str | None = None):
        """从已通过硬闸门的 loopengine 因子中选取父本。

        升级版选择策略：
        1. 精英保留：Top3 因子直接保留，确保优质基因传递
        2. 锦标赛选择：随机选k个，取最优，平衡探索与利用
        3. 小生境机制：同骨架最多选2个，保持多样性
        4. regime加权：当前市场环境下有效的因子获得更高权重
        """
        # 获取因子价值评分
        value_scores = {}
        try:
            vs_df = library.factor_value_scores(factor_type=factor_type)
            if not vs_df.empty:
                value_scores = dict(zip(vs_df["name"], vs_df["total_score"]))
        except Exception:
            pass

        # 获取衰减状态
        decay_status = {}
        try:
            with library._lconn() as c:
                c.execute("""CREATE TABLE IF NOT EXISTS factor_decay (
                    factor_name TEXT PRIMARY KEY, decay_status TEXT, decay_rate REAL,
                    ic_long REAL, ic_short REAL, ic_std REAL, check_time TEXT, updated_at TEXT
                )""")
                rows_decay = c.execute("SELECT factor_name, decay_status, decay_rate FROM factor_decay").fetchall()
                for r in rows_decay:
                    decay_status[r[0]] = {'status': r[1], 'rate': r[2]}
        except Exception:
            pass

        # 获取所有通过门控的因子
        with library._lconn() as c:
            rows = c.execute(
                "SELECT code, family, name FROM factor_registry WHERE engine='loopengine'"
                " AND gate_status IN (1, 3) AND (factor_type=? OR factor_type IS NULL)",
                (factor_type,)).fetchall()
        if not rows:
            # 回退到任意类型
            with library._lconn() as c:
                rows = c.execute(
                    "SELECT code, family, name FROM factor_registry WHERE engine='loopengine'"
                    " AND gate_status IN (1, 3)").fetchall()
        if not rows:
            return None

        # 计算适应度分数
        from loopengine.decay import adjust_factor_weight
        from loopengine.review import skeleton_of

        candidates = []
        for r in rows:
            score = 1.0
            # 族实战加权
            if live_boost:
                score += live_boost.get(r[1] or "", 0.0)
            # 因子价值加权
            if value_scores:
                score += value_scores.get(r[2], 0.3) * 1.5

            # 衰减状态调整
            decay_weight = 1.0
            if r[2] in decay_status:
                decay_weight = adjust_factor_weight(r[2], decay_status[r[2]]['status'])

            # regime 加权
            regime_weight = 1.0
            if regime:
                regime_weight = get_regime_factor_weight(regime, r[2])

            final_score = max(0.1, score) * decay_weight * regime_weight

            # 复杂度惩罚（奥卡姆剃刀：表达式越复杂越容易过拟合）
            try:
                code_text = r[0] or ""
                if "# sexpr: " in code_text.split("\n", 1)[0]:
                    from factor_eval import complexity_penalty
                    final_score *= complexity_penalty(code_text)
            except Exception:
                pass

            # 提取骨架用于小生境
            try:
                first = r[0].split("\n", 1)[0] if r[0] else ""
                if first.startswith("# sexpr: "):
                    tree = parse(first[len("# sexpr: "):], factor_type)
                    sk = skeleton_of(tree)
                else:
                    sk = "unknown"
            except Exception:
                sk = "unknown"

            candidates.append({
                "row": r,
                "score": final_score,
                "skeleton": sk,
            })

        # 按适应度排序
        candidates.sort(key=lambda x: x["score"], reverse=True)

        # 精英保留：Top3 直接保留
        elite_n = min(3, len(candidates))
        elites = candidates[:elite_n]

        # 锦标赛选择（带小生境）
        tournament_k = 3
        max_per_skeleton = 2  # 同骨架最多选2个
        skeleton_count = {}

        # 从非精英中选择
        pool = candidates[elite_n:]
        selected = []

        for _ in range(min(9, len(pool))):  # 最多再选9个，总共12个候选
            if not pool:
                break

            # 锦标赛：随机选k个
            k = min(tournament_k, len(pool))
            contestants = rng.sample(pool, k)
            winner = max(contestants, key=lambda x: x["score"])

            # 小生境检查：同骨架最多max_per_skeleton个
            sk = winner["skeleton"]
            if skeleton_count.get(sk, 0) >= max_per_skeleton:
                # 跳过，从池中移除并重试
                pool = [c for c in pool if c["row"] != winner["row"]]
                continue

            selected.append(winner)
            skeleton_count[sk] = skeleton_count.get(sk, 0) + 1
            pool = [c for c in pool if c["row"] != winner["row"]]

        # 合并精英和锦标赛选择的结果
        all_selected = elites + selected

        if not all_selected:
            return None

        # 从选中的候选中按适应度加权选择
        weights = [c["score"] for c in all_selected]
        chosen = rng.choices(all_selected, weights=weights, k=1)[0]

        row = chosen["row"]
        if not row[0]:
            return None
        first = row[0].split("\n", 1)[0]
        if first.startswith("# sexpr: "):
            return parse(first[len("# sexpr: "):], factor_type)
        return None

    def _llm_generate(self, rng, gaps, proven, factor_type: str = "量价", stats: dict | None = None):
        """LLM 机制引导，双目标轮转（无 key/失败则回退 None）：
        探索——补最空缺机制族；开采——深挖实战验证过的强族（经验库回喂）。
        stats 传入时计数 llm_gen_fail（抽取/解析失败，prompt 质量的核心度量）。"""
        import os

        if not os.environ.get("DEEPSEEK_API_KEY"):
            return None
        targets = []  # [(族, 引导语)]
        if gaps:
            targets.append((rng.choice(gaps), "该机制族在因子库中覆盖极少，探索这个方向的新机制"))
        if proven:
            targets.append((rng.choice(proven), "该机制族实盘命中表现最好，在它基础上深挖变体"))
        if not targets:
            return None
        fam, why = rng.choice(targets)
        # 实盘证据注入（两档，进化信号优先）：
        # - mode=prompt/weights 且有未过期蒸馏信号 → 结构化信号 + hypotheses 种子
        #   （战报蒸馏方案阶段 1，docs/report-distill-evolution-plan.md v2）；
        # - 否则回退到昨日战报原文 900 字截断（每日 18:35 战报，复盘→改进闭环）。
        evidence = ""
        hypotheses: list[str] = []
        sig_used = None
        try:
            from loopengine import evolution_signals as es
            if es.get_mode() in ("prompt", "weights"):
                import experience
                from common import get_last_trade_day
                row = experience.get_latest_evolution_signal()
                if row and es.usable_layer(row["signals"], get_last_trade_day(),
                                           report_date=row["report_date"]) != "none":
                    rendered = es.render_for_prompt(row["signals"])
                    if rendered:
                        evidence = (f"\n进化信号（{row['report_date']} 战报蒸馏，结构化）：\n{rendered}\n"
                                    "请让新因子与这些方向一致：强化验证有效方向，规避失效方向。\n")
                        hypotheses = es.hypotheses_of(row["signals"])
                        sig_used = row["date"]
        except Exception:
            pass
        if not evidence:
            try:
                import experience
                with experience._conn() as c:
                    row = c.execute(
                        "SELECT date, content FROM daily_reports ORDER BY date DESC LIMIT 1").fetchone()
                if row and row[1]:
                    excerpt = " ".join(str(row[1]).split())[:900]
                    evidence = (f"\n昨日（{row[0]}）实盘复盘证据（自动战报摘要）：\n{excerpt}\n"
                                "请让新因子与该证据一致：强化其中验证有效的方向，规避失效方向。\n")
            except Exception:
                pass
        
        # 知识图谱理论指导注入
        theories: list[dict] = []
        try:
            from loopengine.theory_discovery import KnowledgeGraph
            kg = KnowledgeGraph()
            theories = kg.get_theories()[:5]  # 最多5条理论
        except Exception:
            pass
        
        try:
            from llmutil import llm_chat

            system_prompt, user_prompt = _build_llm_prompt(
                fam, why, factor_type, evidence,
                self._family_fewshots(fam, factor_type),
                hypotheses=hypotheses,
                theories=theories)
            text = llm_chat(system_prompt, user_prompt, max_tokens=500,
                            label="loopengine_generate") or ""
            cand = _extract_sexpr(text)
            if cand is None:
                if stats is not None:
                    stats["llm_gen_fail"] = stats.get("llm_gen_fail", 0) + 1
                log.info(f"LLM 出题抽取失败: {text[:100]!r}")
                return None
            tree = parse(cand, factor_type)
            if tree is None:
                if stats is not None:
                    stats["llm_gen_fail"] = stats.get("llm_gen_fail", 0) + 1
                log.info(f"LLM 出题解析失败: {cand[:100]!r}")
            else:
                self._signal_id_in_prompt = sig_used  # 仅 LLM 成功产出时挂信号标（lift 度量）
            return tree
        except Exception:
            return None

    def _family_fewshots(self, fam: str, factor_type: str, limit: int = 3) -> list[str]:
        """捞同族已入库且过统计闸门的真实因子 sexpr 作 few-shot（把闸门口味前置到生成端）。
        为打破 LLM 反馈循环，随机替换 30% 为 IC>0.015 的未过闸"潜力因子"。"""
        try:
            with library._lconn() as c:
                rows = c.execute(
                    "SELECT code FROM factor_registry WHERE engine='loopengine' AND family=?"
                    " AND gate_status IN (1, 3) AND factor_type=?"
                    " ORDER BY multi_objective_score DESC LIMIT ?",
                    (fam, factor_type, limit)).fetchall()
            out = []
            for (code,) in rows:
                if code and code.startswith("# sexpr:"):
                    sx = code.split("\n", 1)[0][len("# sexpr: "):].strip()
                    if sx:
                        out.append(sx)
            # 30% 替换为未过闸但 IC > 0.015 的潜力因子
            if out:
                import random as _rng
                with library._lconn() as c:
                    potential = c.execute(
                        "SELECT fs.name, fr.code FROM factor_scorecards fs"
                        " JOIN factor_registry fr ON fr.name = fs.name"
                        " WHERE fr.engine='loopengine' AND fr.family=?"
                        " AND fr.gate_status NOT IN (1, 3)"
                        " AND fs.pool_name='沪深300'"
                        " AND ABS(fs.ic_mean) > 0.015"
                        " AND fr.code LIKE '# sexpr:%'"
                        " ORDER BY ABS(fs.ic_mean) DESC LIMIT 10",
                        (fam,)).fetchall()
                for name, code in potential:
                    if code and code.startswith("# sexpr:"):
                        sx = code.split("\n", 1)[0][len("# sexpr: "):].strip()
                        if sx and sx not in out and _rng.random() < 0.3:
                            out.append(sx)
            return out[:limit]
        except Exception:
            return []

    def _signal_shadow_hook(self, gaps: list, proven: list) -> None:
        """进化信号 shadow 挂钩（战报蒸馏方案 rollout 第 1 周）：读最新信号，计算
        "若启用会怎么偏置"的快照回写 evolution_signals.shadow_bias_json——
        供 shadow_eval 量化验收（偏置方向 vs 当日实际过闸族分布的 rank 相关）。
        绝不改变任何行为：不碰 gaps/proven/字段权重/预算。"""
        from loopengine import evolution_signals as es
        if es.get_mode() == "off":
            return
        import experience
        row = experience.get_latest_evolution_signal()
        if not row:
            return
        from common import get_last_trade_day
        steer = (row["signals"].get("steer") or {})
        snapshot = {
            "mode": es.get_mode(), "signal_date": row["date"], "report_date": row["report_date"],
            "usable_layer": es.usable_layer(row["signals"], get_last_trade_day(),
                                            report_date=row["report_date"]),
            # usable_layer=none 时偏置本就为零，照样记录（验收分母）
            "would_boost_families": steer.get("families_boost") or {},
            "would_boost_fields": steer.get("fields_boost") or {},
            "would_boost_types": steer.get("types_boost") or {},
            "gaps_now": list(gaps), "proven_now": list(proven),
            "round": self.state["iteration"],
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        experience.update_signal_shadow_bias(row["date"], snapshot)

    # ---------------- 单轮 ----------------
    def run_round(self, batch: int = 30, factor_type: str = "量价", include_events: bool = True, prepared=None) -> dict:
        """单轮挖掘：factor_type 指定因子类型（量价/资金流/板块轮动/龙虎榜/盘口异动/指数）。"""
        s = self.state
        import time as _time
        rng = random.Random(s["iteration"] * 7919 + 13 + int(_time.time_ns() % 10000))
        s["iteration"] += 1

        stats = {"tested": 0, "rejected_review": 0, "llm_rejected": 0, "dup": 0, "frozen": 0,
                 "passed": 0, "new": [], "llm_gen_fail": 0, "llm_review_fallback": 0,
                 "factor_type": factor_type}
        bus.push(EventType.ROUND_START, iteration=s["iteration"], batch=batch,
                 factor_type=factor_type)

        # Step 1: 构建面板
        bus.push(EventType.STEP_UPDATE, step=1, name="构建面板", status="running")
        panel, frames, codes, end = prepared if prepared is not None else self._frames(factor_type)
        bus.push(EventType.STEP_UPDATE, step=1, name="构建面板", status="done")

        # 非量价类型：检查额外帧是否为空，为空则跳过本轮
        if factor_type != "量价" and not self._last_extra_frames:
            bus.push(EventType.ROUND_COMPLETE, iteration=s["iteration"],
                     stats={**stats, "tested": 0, "passed": 0, "dup": 0, "frozen": 0},
                     new_factors=[], skip_reason=f"{factor_type}数据源为空")
            self._save_state()
            return {"iteration": s["iteration"], "tested": 0, "rejected_review": 0, "llm_rejected": 0,
                    "passed": 0, "dup": 0, "frozen": 0,
                    "new": [], "gaps": [], "proven": [], "budget": {}, "skip_reason": f"{factor_type}数据源为空"}

        # Step 1.5: 市场环境识别（regime detection）
        bus.push(EventType.STEP_UPDATE, step=1, name="市场环境识别", status="running")
        try:
            regime_info = detect_regime(codes=codes, end=end)
            regime = regime_info["regime"]
            bus.push(EventType.STEP_UPDATE, step=1, name="市场环境识别", status="done",
                     regime=regime, confidence=regime_info["confidence"],
                     details=regime_info["details"])
        except Exception as e:
            log.warning(f"市场环境识别失败: {e}")
            regime = "sideways"
            regime_info = {"regime": "sideways", "confidence": 0.3, "details": {}, "regime_weights": {}}
            bus.push(EventType.STEP_UPDATE, step=1, name="市场环境识别", status="error", error=str(e))

        # Step 2: 机制族引导
        bus.push(EventType.STEP_UPDATE, step=2, name="机制族引导", status="running")
        registry = library.get_factor_registry()
        loop_reg = registry[registry["engine"] == "loopengine"] if not registry.empty else registry
        cov = structure.family_coverage(loop_reg)
        gaps = sorted(cov, key=cov.get)[:3]
        live = library.family_live_stats()
        proven = sorted(live, key=lambda f: -live[f])[:3]
        live_boost = {f: min(1.0, max(0.0, (w - 0.5) * 4)) for f, w in live.items()}
        bus.push(EventType.STEP_UPDATE, step=2, name="机制族引导", status="done",
                 gaps=gaps, proven=proven)

        # Step 2.5: 进化信号 shadow 挂钩——只记"若启用会怎么偏置"的快照回写
        # （shadow 期量化验收数据积累），绝不改变任何行为
        try:
            self._signal_shadow_hook(gaps, proven)
        except Exception as e:
            log.debug(f"进化信号 shadow 挂钩失败（不影响本轮）: {e}")

        # Step 3: FSA重算
        bus.push(EventType.STEP_UPDATE, step=3, name="FSA重算", status="running")
        library.fsa_recompute()
        bus.push(EventType.STEP_UPDATE, step=3, name="FSA重算", status="done")

        # Step 3.5: 因子衰减检测
        bus.push(EventType.STEP_UPDATE, step=3, name="衰减检测", status="running")
        try:
            from loopengine.decay import run_decay_detection
            decay_stats = run_decay_detection(codes, end)
            bus.push(EventType.STEP_UPDATE, step=3, name="衰减检测", status="done",
                     decay_stats=decay_stats)
        except Exception as e:
            log.warning(f"衰减检测失败: {e}")
            decay_stats = {'total': 0, 'normal': 0, 'mild': 0, 'moderate': 0, 'severe': 0}
            bus.push(EventType.STEP_UPDATE, step=3, name="衰减检测", status="error",
                     error=str(e))

        # 成本闸门：每轮最多 3 次 LLM 审查；规则审查仍覆盖全部候选
        llm_review_budget = 3
        for _ in range(batch):
            # Step 4: 生成候选
            src, tree = self._gen_candidate(rng, gaps, proven, live_boost, factor_type, regime,
                                            stats=stats)
            bus.push(EventType.STEP_UPDATE, step=4, name="生成候选", status="done",
                     source=src, batch_left=batch - _)

            # Step 5: 规则审查
            bus.push(EventType.STEP_UPDATE, step=5, name="规则审查", status="running")
            ok, why = review.review(tree, factor_type)
            bus.push(EventType.STEP_UPDATE, step=5, name="规则审查",
                     status="pass" if ok else "fail", source=src, reason=why if not ok else None)
            if not ok:
                stats["rejected_review"] += 1
                s["budget"].record(src, False)
                bus.push(EventType.REVIEW_RESULT, iteration=s["iteration"],
                         source=src, passed=False, reason=why)
                continue
            sexpr = tree.sexpr()

            # Step 6: LLM语义审查（仅作风险标注，不覆盖硬规则/统计闸门）
            # 优先审查候选，预算用尽后跳过；LLM 不再直接淘汰因子。
            do_llm = llm_review_budget > 0 and rng.random() < 0.5
            bus.push(EventType.STEP_UPDATE, step=6, name="LLM审查", status="running",
                     source=src, sampled=do_llm)
            if do_llm:
                from loopengine.llm_review import llm_review
                llm_review_budget -= 1
                passed_review, reason = llm_review(sexpr)
                if reason.endswith("-fallback"):  # LLM 不可用/JSON 解析失败的回退率（可观测性）
                    stats["llm_review_fallback"] = stats.get("llm_review_fallback", 0) + 1
                bus.push(EventType.STEP_UPDATE, step=6, name="LLM审查",
                         status="pass" if passed_review else "fail", source=src, reason=reason if not passed_review else None)
                if not passed_review:
                    stats["llm_rejected"] += 1  # 兼容旧统计：表示风险标记，不是硬拒绝
                stats.setdefault("llm_flags", []).append({"sexpr": sexpr[:120],
                                                            "passed": passed_review,
                                                            "reason": reason})
                bus.push(EventType.LLM_RESULT, iteration=s["iteration"],
                         source=src, passed=passed_review, reason=reason)
            else:
                bus.push(EventType.STEP_UPDATE, step=6, name="LLM审查",
                         status="skip", source=src)

            # Step 7: 去重
            h = G.factor_hash(sexpr)
            bus.push(EventType.STEP_UPDATE, step=7, name="去重", status="running")
            if library.is_tested(h):
                stats["dup"] += 1
                bus.push(EventType.STEP_UPDATE, step=7, name="去重", status="dup", source=src)
                continue
            bus.push(EventType.STEP_UPDATE, step=7, name="去重", status="pass", source=src)

            # Step 8: FSA拦截
            sk = review.skeleton_of(tree)
            bus.push(EventType.STEP_UPDATE, step=8, name="FSA拦截", status="running")
            if library.is_frozen(sk):
                stats["frozen"] += 1
                s["budget"].record(src, False)
                bus.push(EventType.STEP_UPDATE, step=8, name="FSA拦截", status="frozen", source=src)
                continue
            bus.push(EventType.STEP_UPDATE, step=8, name="FSA拦截", status="pass", source=src)

            # Step 9: 硬闸门
            stats["tested"] += 1
            bus.push(EventType.STEP_UPDATE, step=9, name="硬闸门", status="running")
            try:
                X = evaluate_tree(tree, frames)
                vals = X.stack().rename("f").dropna()
                vals.index = vals.index.set_names(["datetime", "instrument"])
                result = G.evaluate_gates(vals, panel, factor_type=factor_type)
            except Exception as e:
                result = {"pass": False, "reasons": [f"eval error: {e}"], "metrics": {}}

            fam = structure.assign_family(sexpr, sk) if sk else "unknown"
            fname = f"le_{fam}_{h[:6]}"
            G.log_gate_detail(fname, str(end), result, self.pool_name)
            bus.push(EventType.STEP_UPDATE, step=9, name="硬闸门",
                     status="pass" if result["pass"] else "fail",
                     source=src, factor_name=fname,
                     metrics=result.get("metrics", {}),
                     reasons=result.get("reasons", []))

            bus.push(EventType.GATE_EVAL, iteration=s["iteration"],
                     source=src, factor_name=fname, passed=result["pass"],
                     metrics=result.get("metrics", {}),
                     reasons=result.get("reasons", []),
                     stats_snapshot={k: v for k, v in stats.items() if k != "new"})

            library.record_tested(h, sexpr[:60], "loopengine", "loopengine", end, result["pass"],
                                   result["metrics"].get("IC"),
                                   signal_id=getattr(self, "_signal_id_in_prompt", None))
            s["budget"].record(src, result["pass"])

            # Step 10: 入库（含多目标评分）
            bus.push(EventType.STEP_UPDATE, step=10, name="入库", status="running")
            if result["pass"]:
                name = fname
                emit = emit_code(sexpr, name)

                # 多目标评分
                try:
                    from factor_eval import multi_objective_score
                    mo_score = multi_objective_score(name, codes, end, code=emit)
                    result["multi_objective_score"] = mo_score.get("score", 0.0)
                    result["risk_metrics"] = {
                        "max_drawdown": mo_score.get("max_drawdown", 0.0),
                        "sharpe": mo_score.get("sharpe", 0.0),
                        "sortino": mo_score.get("sortino", 0.0),
                        "calmar": mo_score.get("calmar", 0.0),
                    }
                except Exception as e:
                    log.warning(f"多目标评分失败: {e}")
                    result["multi_objective_score"] = 0.0
                    result["risk_metrics"] = {}

                library.sync_factor_registry([{
                    "name": name, "kind": "loopengine",
                    "code": emit,
                    "engine": "loopengine", "factor_type": factor_type,
                    "generation_mode": "theory_guided" if getattr(self, "_theory_context", None) else src,
                    "theory_id": (self._theory_context or {}).get("theory_id") if getattr(self, "_theory_context", None) else None,
                    "hypothesis_id": (self._theory_context or {}).get("hypothesis_id") if getattr(self, "_theory_context", None) else None,
                    "source_theory_sexpr": (self._theory_context or {}).get("sexpr") if getattr(self, "_theory_context", None) else None,
                    "multi_objective_score": result.get("multi_objective_score", 0.0)}])
                with library._lconn() as c:
                    c.execute("""UPDATE factor_registry 
                        SET gate_status=1, skeleton=?, family=?, 
                            multi_objective_score=?, max_drawdown=?, sharpe=?, sortino=?, calmar=?
                        WHERE name=?""",
                        (sk, fam, result.get("multi_objective_score", 0.0),
                         result.get("risk_metrics", {}).get("max_drawdown", 0.0),
                         result.get("risk_metrics", {}).get("sharpe", 0.0),
                         result.get("risk_metrics", {}).get("sortino", 0.0),
                         result.get("risk_metrics", {}).get("calmar", 0.0),
                         name))
                stats["passed"] += 1
                stats["new"].append(name)
                s["accepted"] += 1
                s["momentum"][sk.split("@")[0].split("-")[0]] = 1
                bus.push(EventType.STEP_UPDATE, step=10, name="入库", status="pass",
                         source=src, factor_name=name, family=fam)
                bus.push(EventType.GATE_PASS, iteration=s["iteration"],
                         factor_name=name, family=fam, skeleton=sk)
            else:
                bus.push(EventType.STEP_UPDATE, step=10, name="入库", status="skip",
                         source=src, factor_name=fname)
                library.record_failure(sexpr[:60], sk, structure.assign_family(sexpr, sk),
                                       "; ".join(result["reasons"])[:200], "loopengine")

        # 数据驱动字段权重
        with library._lconn() as c:
            sexprs = []
            for r in c.execute(
                    "SELECT code FROM factor_registry WHERE engine='loopengine' AND gate_status IN (1, 3)").fetchall():
                if r[0] and r[0].startswith("# sexpr: "):
                    sexprs.append(r[0].split("\n", 1)[0][len("# sexpr: "):])
        s["field_weights"].boost_from_factors(sexprs)

        # ---- 事件定向挖掘（每 4 轮插入 1 轮，轮转 3 种事件）----
        _EVENT_KINDS_ROTATION = ["涨停", "大涨>=7%", "跌停"]
        _EVENT_MINE_BATCH = 15
        _EVENT_MINE_HORIZON = 5
        ev_result = None
        if include_events and s["iteration"] % 4 == 0:
            ev_kind = _EVENT_KINDS_ROTATION[s["iteration"] % len(_EVENT_KINDS_ROTATION)]
            try:
                ev_result = self.run_event_round(ev_kind, batch=_EVENT_MINE_BATCH,
                                                 horizon=_EVENT_MINE_HORIZON,
                                                 factor_type=factor_type)
            except Exception as e:
                log.warning(f"事件定向挖掘失败[{ev_kind}]: {e}")

        self._save_state()

        # 策略包生成已剥离到独立任务 job_strategy_gen（scheduler.py）
        # 不再嵌入每5分钟的演化流水线，避免单次20+分钟的浪费

        result = {"iteration": s["iteration"], **stats, "gaps": gaps, "proven": proven,
            "budget": {k: round(v, 2) for k, v in s["budget"].p.items()}}
        if ev_result:
            result["event_round"] = ev_result
        bus.push(EventType.ROUND_COMPLETE, iteration=s["iteration"],
                 stats={k: v for k, v in stats.items() if k != "new"},
                 new_factors=stats["new"][:5])
        return result

    def _try_generate_pack(self) -> str | None:
        """尝试生成策略包：从已通过闸门的因子中选Top因子，构建组合并验证。

        每日最多生成一次（包名带日期 LE_池_MMDD）：打分用每日三班体检后的评分卡，
        盘后 21:30 体检的分数会进入次日早盘的包。此前每轮（5分钟）都重算
        walk-forward + 逐因子子进程求值，单次可达 20+ 分钟，纯属浪费。"""
        import logging
        import signals as sig
        from common import all_pools, get_last_trade_day

        logger = logging.getLogger("pack_gen")

        try:
            # 每日一次闸：今日包已存在则跳过
            today_pack = f"LE_{self.pool_name}_{datetime.now().strftime('%m%d')}"
            try:
                with library._lconn() as c:
                    if c.execute("SELECT 1 FROM strategies WHERE name=?",
                                 (today_pack,)).fetchone():
                        return None
            except Exception:
                pass

            # 检查是否有足够高质量因子—— builtin + evolved 同台竞争。
            # 排名分：OOS 可信（被发现后 ≥20 交易日）用 |ICIR_OOS|，否则样本内 |ICIR| 打五折——
            # 样本内分数被引擎选择过程污染（实测 ICIR 3.7 的组合 WF OOS 仅 49.4%）。
            # 方向同理优先取 OOS 符号。每因子只取最新一次评分。
            with library._lconn() as c:
                rows = c.execute('''
                    SELECT * FROM (
                        SELECT fs.name, fs.kind, fr.code,
                               CASE WHEN fs.oos_days >= 20 AND fs.ic_oos IS NOT NULL
                                    THEN CASE WHEN fs.ic_oos < 0 THEN '负向' ELSE '正向' END
                                    ELSE fs.direction END AS direction,
                               CASE WHEN fs.oos_days >= 20 AND fs.icir_oos IS NOT NULL
                                    THEN ABS(fs.icir_oos) ELSE ABS(fs.icir) * 0.5 END AS score
                        FROM factor_scorecards fs
                        LEFT JOIN factor_registry fr ON fs.name = fr.name
                        JOIN (SELECT name, MAX(eval_date) md FROM factor_scorecards
                              WHERE pool_name = ? GROUP BY name) latest
                          ON fs.name = latest.name AND fs.eval_date = latest.md
                        WHERE fs.pool_name = ?
                          AND fs.eval_date >= date('now', '-30 days')
                          AND fs.kind IN ('内置', '技术指标', '进化', '演化引擎')
                    )
                    WHERE score > 0.2
                    ORDER BY score DESC
                ''', (self.pool_name, self.pool_name)).fetchall()
                
                if len(rows) < 3:
                    logger.debug(f"高质量因子不足: {len(rows)} < 3")
                    return None  # 高质量因子不足
                
                factor_info = {r[0]: {"kind": r[1], "code": r[2],
                                      "direction": -1 if r[3] == "负向" else 1}
                               for r in rows[:8]}
                score_map = {r[0]: float(r[4] or 0.5) for r in rows}  # OOS 感知分数
                logger.debug(f"因子信息: {factor_info}")
            
            # 获取因子值（builtin + evolved）
            codes = all_pools().get(self.pool_name) or all_pools().get("沪深300")
            end = get_last_trade_day()
            # 面板深度：walk-forward 需要 250 交易日估计窗 + 因子自身长窗预热
            # （进化因子常见 400+ 日窗），800 日面板下有效交集 <265 天会让 WF 直接返回空；
            # 1600 日（约 1100+ 交易日）才有足够样本（2026-09-11 实测 8 因子 WF 全空）
            panel = sig.get_panel_cached(codes, end, 1600, source=datasource.get_loop_source())

            factor_vals = {}
            _frames = None  # 树直算帧（有进化因子时才构建，构建一次复用）
            for name, info in factor_info.items():
                kind = info["kind"]
                code = info.get("code", "")
                try:
                    if kind in ("内置", "builtin"):
                        vals = sig.compute_builtin(panel, name)
                    elif name in sig.CATALOG_NAMES:
                        vals = sig.compute_common(panel, name)
                    elif name in sig.TECH_INDICATORS:
                        vals = sig.compute_tech(panel, name)
                    elif code and "# sexpr:" in code:
                        # 树直算快速路径（~0.02s/因子），失败回退子进程执行（分钟级）
                        try:
                            if _frames is None:
                                from loopengine.tree import build_field_frames
                                _frames = build_field_frames(panel)
                            from loopengine.tree import evaluate_tree, parse
                            sexpr = code.split("\n", 1)[0][len("# sexpr: "):]
                            vals = evaluate_tree(parse(sexpr), _frames).stack().rename(name).dropna()
                            vals.index = vals.index.set_names(["datetime", "instrument"])
                        except Exception:
                            vals = sig.run_factor_code(code, name, codes, end)
                    else:
                        logger.debug(f"跳过因子 {name}: 不在任何列表中")
                        continue
                    
                    if not vals.dropna().empty:
                        factor_vals[name] = vals
                        logger.debug(f"因子 {name}: 成功 ({vals.dropna().shape[0]}行)")
                    else:
                        logger.debug(f"因子 {name}: 空值")
                except Exception as e:
                    logger.debug(f"因子 {name}: 失败 - {str(e)[:50]}")
            
            logger.debug(f"有效因子: {len(factor_vals)}个")
            if len(factor_vals) < 3:
                logger.debug(f"有效因子不足: {len(factor_vals)} < 3")
                return None

            # ① 先相关性精简出最终组合（按 OOS 感知分数贪心去冗余）
            selected = list(factor_vals.keys())[:8]  # 最多8个候选
            if len(selected) > 3:
                # 计算因子值相关性矩阵
                import pandas as pd
                factor_df = pd.DataFrame({k: v for k, v in factor_vals.items() if k in selected})
                if not factor_df.empty and factor_df.shape[1] > 3:
                    corr_matrix = factor_df.corr().abs()
                    # 按分数降序贪心选择，剔除相关系数>0.7的因子
                    selected.sort(key=lambda x: score_map.get(x, 0.5), reverse=True)
                    filtered = [selected[0]]
                    for name in selected[1:]:
                        if all(corr_matrix.loc[name, s] < 0.7 for s in filtered):
                            filtered.append(name)
                        if len(filtered) >= 5:
                            break
                    selected = filtered

            if len(selected) < 3:
                logger.debug(f"相关性精简后因子不足: {len(selected)} < 3")
                return None

            # ② 再 Walk-forward 验证最终组合——验证什么就上线什么
            #    （此前顺序相反：WF 验证的是未精简的全量组合，出包组合从未被整体验证过）
            import factor_eval as fe
            final_vals = {n: factor_vals[n] for n in selected}
            wf = fe.walk_forward(
                final_vals, panel, "等权", 10, fwd_days=5, step=10, min_factors=2
            )

            if wf.empty or "优化组合扣费超额" not in wf:
                logger.debug("walk-forward无结果")
                return None

            library.record_research_trial("loop_strategy")
            cv = fe.time_series_cv(final_vals, panel, "等权", 10, n_folds=3, fwd_days=5, step=5)
            if not cv.get("passed"):
                return None
            net = wf["优化组合扣费超额"]
            oos_wr = float((net > 0).mean())
            logger.debug(f"OOS胜率: {oos_wr:.1%}")

            # 质量门槛
            if oos_wr < 0.50:
                logger.debug(f"OOS胜率不足: {oos_wr:.1%} < 50%")
                return None
            # ICIR加权（而非等权）：权重用候选查询的 OOS 感知分数，与排名口径一致
            icir_weights = {n: score_map.get(n, 0.5) for n in selected}
            total_icir = sum(icir_weights.values())
            
            # 归一化权重（方向取评分卡建议方向：负 IC 因子反向使用，
            # 2026-09-11 踩坑：方向曾硬编码 1，alpha041/mom_60d 实际 IC 为负）
            if total_icir > 0:
                weights = {n: (icir_weights[n] / total_icir,
                               factor_info.get(n, {}).get("direction", 1)) for n in selected}
            else:
                weights = {n: (1.0 / len(selected),
                               factor_info.get(n, {}).get("direction", 1)) for n in selected}
            # kind 映射（评分卡存中文标签：演化引擎/进化 都归 evolved）
            kind_map = {"内置": "builtin", "技术指标": "tech", "loopengine": "evolved",
                        "演化引擎": "evolved", "进化": "evolved"}
            factor_kind = {n: kind_map.get(factor_info.get(n, {}).get("kind", ""), "builtin") for n in selected}
            
            pack_name = f"LE_{self.pool_name}_{datetime.now().strftime('%m%d')}"

            # 归一化口径快照：typed_v2 下按因子类型解析并随包落盘（回放复现以快照为准）；
            # legacy 下不写 norm 键——避免存量包被 "zscore" 快照钉死、切换后享受不到分派。
            import signals as sig
            _norms = sig.scoring_norms(selected) or {}

            payload = {
                "pool_name": self.pool_name,
                "top_n": 10,
                "method": "等权",
                "factors": [{"name": n, "kind": factor_kind.get(n, "builtin"), "weight": w,
                             "direction": d, **({"norm": _norms[n]} if _norms else {})}
                           for n, (w, d) in weights.items()],
                "filters": ["tradable"],
                "oos_winrate": f"{oos_wr:.0%}",
                "horizon": "5日",
                "norm_scheme": sig.current_norm_scheme(),
            }
            # 日期版仅归档留档；固定名 current 版参与每日选股竞争——
            # 实战归因按包名累积，每天换名字的包永远是"零实战"、进不了 Top3
            library.save_strategy(pack_name, payload, status="archived")
            current = f"LE_{self.pool_name}_current"
            library.save_strategy(current, payload, status="shadow")

            logger.info(f"策略包已保存: {pack_name}(OOS={oos_wr:.0%}) + {current}(固定名)")
            return f"{pack_name}(OOS={oos_wr:.0%})"
        except Exception as e:
            logger.warning(f"策略包生成异常: {e}")
            return None

    # ---------------- 多类型批量挖掘 ----------------
    def run_multi_type_round(self, batch_per_type: int = 15,
                             factor_types: list[str] | None = None) -> dict:
        """批量挖掘多种因子类型。每种类型分配 batch_per_type 个候选。"""
        types = factor_types or DEFAULT_FACTOR_TYPES
        all_stats = {}
        for ft in types:
            result = self.run_round(batch=batch_per_type, factor_type=ft)
            all_stats[ft] = result
        return {"rounds": all_stats, "types_mined": types}

    # ---------------- 定向挖因子（事件目标） ----------------
    def run_event_round(self, kind: str, batch: int = 30, horizon: int = 5,
                        factor_type: str = "量价") -> dict:
        """围绕事件（涨停/大涨/跌停/创新高）定向演化：生成管线与 run_round 相同，
        但适应度换成事件版硬闸门（事件IC + 十分位提升 + 两半稳定 + 库内相关）。

        入库因子名前缀 ev_、gate_status=2（事件闸门通过——区别于收益闸门的 1，
        不会被 Top5 复合等收益管线误用）；哈希按事件命名空间去重（同结构在
        收益口径测过仍可在事件口径测）。
        """
        panel, frames, codes, end = self._frames(factor_type)
        s = self.state
        rng = random.Random(s["iteration"] * 7919 + 17 + hash(kind) % 1000)
        s["iteration"] += 1

        registry = library.get_factor_registry()
        loop_reg = registry[registry["engine"] == "loopengine"] if not registry.empty else registry
        cov = structure.family_coverage(loop_reg)
        gaps = sorted(cov, key=cov.get)[:3]
        live = library.family_live_stats()
        proven = sorted(live, key=lambda f: -live[f])[:3]
        live_boost = {f: min(1.0, max(0.0, (w - 0.5) * 4)) for f, w in live.items()}

        # 已通过事件闸门的因子的 IC 序列（相关性闸门基准）
        lab = G.event_labels(panel, kind, horizon)
        passed_ics = {}

        stats = {"tested": 0, "rejected_review": 0, "llm_rejected": 0, "dup": 0,
                 "frozen": 0, "passed": 0, "new": [], "factor_type": factor_type}
        llm_review_budget = 3
        for _ in range(batch):
            src, tree = self._gen_candidate(rng, gaps, proven, live_boost, factor_type)
            ok, _why = review.review(tree, factor_type)
            if not ok:
                stats["rejected_review"] += 1
                s["budget"].record(src, False)
                continue
            sexpr = tree.sexpr()
            if llm_review_budget > 0 and rng.random() < 0.3:
                from loopengine.llm_review import llm_review

                llm_review_budget -= 1
                passed_review, reason = llm_review(sexpr)
                if reason.endswith("-fallback"):  # LLM 不可用/JSON 解析失败的回退率
                    stats["llm_review_fallback"] = stats.get("llm_review_fallback", 0) + 1
                if not passed_review:
                    stats["llm_rejected"] += 1  # 兼容旧统计：表示风险标记，不是硬拒绝
                stats.setdefault("llm_flags", []).append({"sexpr": sexpr[:120],
                                                            "passed": passed_review,
                                                            "reason": reason})
            h = f"ev:{kind}:" + G.factor_hash(sexpr)
            if library.is_tested(h):
                stats["dup"] += 1
                continue
            sk = review.skeleton_of(tree)
            if library.is_frozen(sk):
                stats["frozen"] += 1
                s["budget"].record(src, False)
                continue

            stats["tested"] += 1
            try:
                X = evaluate_tree(tree, frames)
                vals = X.stack().rename("f").dropna()
                vals.index = vals.index.set_names(["datetime", "instrument"])
                result = G.evaluate_event_gates(vals, panel, kind, horizon, library_ics=passed_ics)
            except Exception:
                result = {"pass": False, "reasons": ["eval error"], "metrics": {}}

            library.record_tested(h, sexpr[:60], "loopengine", "loopengine", end,
                                  result["pass"], result["metrics"].get("事件IC"),
                                  signal_id=getattr(self, "_signal_id_in_prompt", None))
            s["budget"].record(src, result["pass"])
            if result["pass"]:
                fam = structure.assign_family(sexpr, sk)
                name = f"ev_{fam}_{G.factor_hash(sexpr)[:6]}"
                library.sync_factor_registry([{
                    "name": name, "kind": "loopengine",
                    "code": emit_code(sexpr, name), "engine": "loopengine",
                    "factor_type": factor_type}])
                with library._lconn() as c:
                    c.execute("UPDATE factor_registry SET gate_status=2, skeleton=?, family=? WHERE name=?",
                              (sk, fam, name))
                ic_s = fe._norm(vals).rename("f").to_frame().join(lab.rename("y"), how="inner").dropna()
                passed_ics[name] = ic_s.groupby(level="datetime").apply(
                    lambda g: g["f"].corr(g["y"], method="spearman") if len(g) >= 30 else float("nan")).dropna()
                stats["passed"] += 1
                stats["new"].append(name)
                s["accepted"] += 1
            else:
                library.record_failure(sexpr[:60], sk, structure.assign_family(sexpr, sk),
                                       "; ".join(result["reasons"])[:200], "loopengine")

        self._save_state()
        return {"iteration": s["iteration"], "kind": kind, "horizon": horizon, **stats}
