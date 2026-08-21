"""LoopEngine 主引擎：状态持久化 + 每轮 生成→审查→验证→入库 + 自适应预算。

对标文章 Loop 框架：每轮批量候选，硬闸门把关，检查点原子写入，任意中断可续。
与 RD-Agent 双引擎共存：产物统一进 factor_registry（engine='loopengine'）。
"""

import json
import random
from datetime import datetime

import pandas as pd

import gates as G
import library
import structure
from loopengine import genetics, review
from loopengine.tree import build_field_frames, emit_code, evaluate_tree, parse

STATE_KEY = "loopengine"


class LoopEngine:
    def __init__(self, pool_name: str = "沪深300"):
        self.pool_name = pool_name
        self.state = self._load_state()

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
    def _frames(self):
        import signals as sig
        from common import all_pools, get_last_trade_day

        codes = all_pools()[self.pool_name]
        end = get_last_trade_day()
        panel = sig.get_panel_cached(codes, end, 800, source="qlib_local")
        return panel, build_field_frames(panel), end

    # ---------------- 生成 ----------------
    def _gen_candidate(self, rng, families_low):
        src = self.state["budget"].choose(rng)
        fw = self.state["field_weights"].w
        if src == "llm":
            tree = self._llm_generate(families_low) or genetics.random_tree(rng, 4, fw)
        elif src == "mutate":
            parent = self._pick_parent(rng)
            tree = genetics.mutate(parent, rng, fw) if parent else genetics.random_tree(rng, 4, fw)
        elif src == "crossover":
            p1, p2 = self._pick_parent(rng), self._pick_parent(rng)
            tree = genetics.crossover(p1, p2, rng) if p1 and p2 else genetics.random_tree(rng, 4, fw)
        elif src == "perturb":
            parent = self._pick_parent(rng)
            tree = genetics.perturb(parent, rng, self.state["momentum"]) if parent else genetics.random_tree(rng, 4, fw)
        else:
            tree = genetics.random_tree(rng, 4, fw)
        return src, tree

    def _pick_parent(self, rng):
        """从已通过硬闸门的 loopengine 因子中选取父本（从代码首行注释解析 S 表达式）。"""
        with library._lconn() as c:
            row = c.execute(
                "SELECT code FROM factor_registry WHERE engine='loopengine' AND gate_status=1"
                " ORDER BY RANDOM() LIMIT 1").fetchone()
        if not row or not row[0]:
            return None
        first = row[0].split("\n", 1)[0]
        if first.startswith("# sexpr: "):
            return parse(first[len("# sexpr: "):])
        return None

    def _llm_generate(self, families_low):
        """LLM 机制引导：补最空缺机制族（无 key/失败则回退 None）。"""
        import os

        if not os.environ.get("DEEPSEEK_API_KEY") or not families_low:
            return None
        fam = families_low[0]
        try:
            from litellm import completion

            fields = "open,high,low,close,volume,amount,vwap,overnight,amplitude,upper_shadow,lower_shadow,hl_ratio,body_ratio"
            ops = "sub,mul,div,abs,sign,rank_cs,ma,ts_min,ts_max,ts_rank,decay_linear,std,skew,delta,roc,corr"
            prompt = (f"你是量化因子工程师。用以下 S 表达式语法写一个属于「{fam}」机制族的 A 股日频量价因子。\n"
                      f"字段: {fields}\n算子: {ops}（窗口算子需带整数窗口，如 ma(close,20)）\n"
                      "规则: 深度≤6，corr/mul/div/sub 两端维度一致，至少含一个窗口算子。\n"
                      "只输出一个 S 表达式，如 sub(ma(overnight,20),delta(ma(overnight,20),5))，不要任何解释。")
            r = completion(model="deepseek/deepseek-chat",
                           messages=[{"role": "user", "content": prompt}], max_tokens=120)
            text = r.choices[0].message.content.strip().strip("`").split("\n")[0]
            return parse(text)
        except Exception:
            return None

    # ---------------- 单轮 ----------------
    def run_round(self, batch: int = 30) -> dict:
        panel, frames, end = self._frames()
        s = self.state
        rng = random.Random(s["iteration"] * 7919 + 13)
        s["iteration"] += 1

        # 机制族空缺指引
        registry = library.get_factor_registry()
        cov = structure.family_coverage(registry[registry["engine"] == "loopengine"] if not registry.empty else registry)
        families_low = sorted(cov, key=cov.get)[:3]

        library.fsa_recompute()
        stats = {"tested": 0, "rejected_review": 0, "llm_rejected": 0, "dup": 0, "frozen": 0, "passed": 0, "new": []}
        llm_review_budget = 5  # P4：每轮随机抽样 5 个候选交独立审查 sub-agent 精判
        for _ in range(batch):
            src, tree = self._gen_candidate(rng, families_low)
            ok, why = review.review(tree)
            if not ok:
                stats["rejected_review"] += 1
                s["budget"].record(src, False)
                continue
            sexpr = tree.sexpr()
            # P4 双LLM分离：抽样送独立审查员（与生成端不同的 system prompt）
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
            h = G.factor_hash(sexpr)
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
                result = G.evaluate_gates(vals, panel)
            except Exception:
                result = {"pass": False, "reasons": ["eval error"], "metrics": {}}

            library.record_tested(h, sexpr[:60], "loopengine", "loopengine", end, result["pass"],
                                  result["metrics"].get("IC"))
            s["budget"].record(src, result["pass"])
            if result["pass"]:
                fam = structure.assign_family(sexpr, sk)
                name = f"le_{fam}_{h[:6]}"
                library.sync_factor_registry([{
                    "name": name, "kind": "loopengine",
                    "code": emit_code(sexpr, name),
                    "engine": "loopengine"}])
                with library._lconn() as c:
                    c.execute("UPDATE factor_registry SET gate_status=1, skeleton=?, family=? WHERE name=?",
                              (sk, fam, name))
                stats["passed"] += 1
                stats["new"].append(name)
                s["accepted"] += 1
                s["momentum"][sk.split("@")[0].split("-")[0]] = 1
            else:
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
        return {"iteration": s["iteration"], **stats,
            "budget": {k: round(v, 2) for k, v in s["budget"].p.items()}}
