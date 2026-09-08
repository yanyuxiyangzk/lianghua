"""LoopEngine 主引擎：状态持久化 + 每轮 生成→审查→验证→入库 + 自适应预算。

支持多类型因子挖掘：量价/资金流/板块轮动/龙虎榜/盘口异动/指数。
每种类型使用独立的字段帧，通过遗传算法自动搜索最优因子表达式。
"""

import json
import random
from datetime import datetime

import pandas as pd

import gates as G
import factor_eval as fe
import library
import structure
from event_bus import EventType, bus
from loopengine import genetics, review
from loopengine.tree import all_fields, build_field_frames, emit_code, evaluate_tree, parse

STATE_KEY = "loopengine"

# 默认挖掘顺序：量价（主力）→ 资金流 → 板块轮动 → 指数 → 盘口异动 → 龙虎榜
DEFAULT_FACTOR_TYPES = ["量价", "资金流", "板块轮动", "指数", "盘口异动", "龙虎榜", "爆量抢筹"]


class LoopEngine:
    def __init__(self, pool_name: str = "沪深300"):
        self.pool_name = pool_name
        self.state = self._load_state()
        self._last_extra_frames = True

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
        from common import all_pools, get_last_trade_day

        codes = all_pools()[self.pool_name]
        end = get_last_trade_day()
        panel = sig.get_panel_cached(codes, end, 800, source="qlib_local")
        # 构建额外帧（非量价类型）
        extra = None
        if factor_type != "量价":
            from loopengine.extra_frames import build_extra_frames
            extra = build_extra_frames(factor_type, codes, end, lookback=800)
        self._last_extra_frames = extra if factor_type != "量价" else True
        return panel, build_field_frames(panel, extra), codes, end

    # ---------------- 生成 ----------------
    def _gen_candidate(self, rng, gaps, proven, live_boost, factor_type: str = "量价"):
        src = self.state["budget"].choose(rng)
        fw = self.state["field_weights"].w
        if src == "llm":
            tree = self._llm_generate(rng, gaps, proven, factor_type) or genetics.random_tree(rng, 4, fw)
        elif src == "mutate":
            parent = self._pick_parent(rng, live_boost, factor_type)
            tree = genetics.mutate(parent, rng, fw) if parent else genetics.random_tree(rng, 4, fw)
        elif src == "crossover":
            p1 = self._pick_parent(rng, live_boost, factor_type)
            p2 = self._pick_parent(rng, live_boost, factor_type)
            tree = genetics.crossover(p1, p2, rng) if p1 and p2 else genetics.random_tree(rng, 4, fw)
        elif src == "perturb":
            parent = self._pick_parent(rng, live_boost, factor_type)
            tree = genetics.perturb(parent, rng, self.state["momentum"]) if parent else genetics.random_tree(rng, 4, fw)
        else:
            tree = genetics.random_tree(rng, 4, fw)
        return src, tree

    def _pick_parent(self, rng, live_boost: dict | None = None, factor_type: str = "量价"):
        """从已通过硬闸门的 loopengine 因子中选取父本。
        live_boost 非空时按族实战胜率加权；factor_type 过滤同类型因子。
        优先按因子价值评分加权选择。"""
        # 获取因子价值评分
        value_scores = {}
        try:
            vs_df = library.factor_value_scores(factor_type=factor_type)
            if not vs_df.empty:
                value_scores = dict(zip(vs_df["name"], vs_df["total_score"]))
        except Exception:
            pass

        with library._lconn() as c:
            rows = c.execute(
                "SELECT code, family, name FROM factor_registry WHERE engine='loopengine'"
                " AND gate_status=1 AND (factor_type=? OR factor_type IS NULL)"
                " ORDER BY RANDOM() LIMIT 12", (factor_type,)).fetchall()
        if not rows:
            # 回退到任意类型
            with library._lconn() as c:
                rows = c.execute(
                    "SELECT code, family, name FROM factor_registry WHERE engine='loopengine'"
                    " AND gate_status=1 ORDER BY RANDOM() LIMIT 12").fetchall()
        if not rows:
            return None
        if live_boost or value_scores:
            w = []
            for r in rows:
                base = 1.0
                # 族实战加权
                if live_boost:
                    base += live_boost.get(r[1] or "", 0.0)
                # 因子价值加权（0~1 → 0~1.5倍额外权重）
                if value_scores:
                    base += value_scores.get(r[2], 0.3) * 1.5
                w.append(max(0.1, base))
            row = rng.choices(rows, weights=w, k=1)[0]
        else:
            row = rows[0]
        if not row[0]:
            return None
        first = row[0].split("\n", 1)[0]
        if first.startswith("# sexpr: "):
            return parse(first[len("# sexpr: "):], factor_type)
        return None

    def _llm_generate(self, rng, gaps, proven, factor_type: str = "量价"):
        """LLM 机制引导，双目标轮转（无 key/失败则回退 None）：
        探索——补最空缺机制族；开采——深挖实战验证过的强族（经验库回喂）。"""
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
        try:
            from litellm import completion

            fields = ",".join(all_fields(factor_type))
            ops = "sub,mul,div,abs,sign,rank_cs,ma,ts_min,ts_max,ts_rank,decay_linear,std,skew,delta,roc,corr,ema,zscore"
            type_hint = f"（因子类型：{factor_type}）" if factor_type != "量价" else ""
            prompt = (f"你是量化因子工程师。用以下 S 表达式语法写一个属于「{fam}」机制族的 A 股日频{factor_type}因子。"
                      f"（{why}）{type_hint}\n"
                      f"字段: {fields}\n算子: {ops}（窗口算子需带整数窗口，如 ma(close,20)）\n"
                      "规则: 深度≤6，corr/mul/div/sub 两端维度一致，至少含一个窗口算子。\n"
                      "只输出一个 S 表达式，如 sub(ma(overnight,20),delta(ma(overnight,20),5))，不要任何解释。")
            r = completion(model="deepseek/deepseek-chat",
                           messages=[{"role": "user", "content": prompt}], max_tokens=120)
            text = r.choices[0].message.content.strip().strip("`").split("\n")[0]
            return parse(text, factor_type)
        except Exception:
            return None

    # ---------------- 单轮 ----------------
    def run_round(self, batch: int = 30, factor_type: str = "量价") -> dict:
        """单轮挖掘：factor_type 指定因子类型（量价/资金流/板块轮动/龙虎榜/盘口异动/指数）。"""
        s = self.state
        rng = random.Random(s["iteration"] * 7919 + 13)
        s["iteration"] += 1

        stats = {"tested": 0, "rejected_review": 0, "llm_rejected": 0, "dup": 0, "frozen": 0, "passed": 0, "new": [],
                 "factor_type": factor_type}
        bus.push(EventType.ROUND_START, iteration=s["iteration"], batch=batch,
                 factor_type=factor_type)

        # Step 1: 构建面板
        bus.push(EventType.STEP_UPDATE, step=1, name="构建面板", status="running")
        panel, frames, codes, end = self._frames(factor_type)
        bus.push(EventType.STEP_UPDATE, step=1, name="构建面板", status="done")

        # 非量价类型：检查额外帧是否为空，为空则跳过本轮
        if factor_type != "量价" and not self._last_extra_frames:
            bus.push(EventType.ROUND_COMPLETE, iteration=s["iteration"],
                     stats={**stats, "tested": 0, "passed": 0, "dup": 0, "frozen": 0},
                     new_factors=[], skip_reason=f"{factor_type}数据源为空")
            self._save_state()
            return {"iteration": s["iteration"], "tested": 0, "passed": 0, "dup": 0, "frozen": 0,
                    "new": [], "gaps": [], "proven": [], "budget": {}, "skip_reason": f"{factor_type}数据源为空"}

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

        # Step 3: FSA重算
        bus.push(EventType.STEP_UPDATE, step=3, name="FSA重算", status="running")
        library.fsa_recompute()
        bus.push(EventType.STEP_UPDATE, step=3, name="FSA重算", status="done")

        llm_review_budget = 5
        for _ in range(batch):
            # Step 4: 生成候选
            src, tree = self._gen_candidate(rng, gaps, proven, live_boost, factor_type)
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

            # Step 6: LLM审查（抽样）
            do_llm = llm_review_budget > 0 and rng.random() < 0.3
            bus.push(EventType.STEP_UPDATE, step=6, name="LLM审查", status="running",
                     source=src, sampled=do_llm)
            if do_llm:
                from loopengine.llm_review import llm_review
                llm_review_budget -= 1
                passed_review, reason = llm_review(sexpr)
                bus.push(EventType.STEP_UPDATE, step=6, name="LLM审查",
                         status="pass" if passed_review else "fail", source=src, reason=reason if not passed_review else None)
                if not passed_review:
                    stats["llm_rejected"] += 1
                    sk0 = review.skeleton_of(tree)
                    library.record_failure(sexpr[:60], sk0, structure.assign_family(sexpr, sk0),
                                           f"llm_review: {reason}", "loopengine")
                    s["budget"].record(src, False)
                    bus.push(EventType.LLM_RESULT, iteration=s["iteration"],
                             source=src, passed=False, reason=reason)
                    continue
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
                result = G.evaluate_gates(vals, panel)
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
                                  result["metrics"].get("IC"))
            s["budget"].record(src, result["pass"])

            # Step 10: 入库
            bus.push(EventType.STEP_UPDATE, step=10, name="入库", status="running")
            if result["pass"]:
                name = fname
                library.sync_factor_registry([{
                    "name": name, "kind": "loopengine",
                    "code": emit_code(sexpr, name),
                    "engine": "loopengine", "factor_type": factor_type}])
                with library._lconn() as c:
                    c.execute("UPDATE factor_registry SET gate_status=1, skeleton=?, family=? WHERE name=?",
                              (sk, fam, name))
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
                    "SELECT code FROM factor_registry WHERE engine='loopengine' AND gate_status=1").fetchall():
                if r[0] and r[0].startswith("# sexpr: "):
                    sexprs.append(r[0].split("\n", 1)[0][len("# sexpr: "):])
        s["field_weights"].boost_from_factors(sexprs)
        self._save_state()
        result = {"iteration": s["iteration"], **stats, "gaps": gaps, "proven": proven,
            "budget": {k: round(v, 2) for k, v in s["budget"].p.items()}}
        bus.push(EventType.ROUND_COMPLETE, iteration=s["iteration"],
                 stats={k: v for k, v in stats.items() if k != "new"},
                 new_factors=stats["new"][:5])
        return result

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
                if not passed_review:
                    stats["llm_rejected"] += 1
                    sk0 = review.skeleton_of(tree)
                    library.record_failure(sexpr[:60], sk0, structure.assign_family(sexpr, sk0),
                                           f"llm_review: {reason}", "loopengine")
                    s["budget"].record(src, False)
                    continue
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
                                  result["pass"], result["metrics"].get("事件IC"))
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
