"""P1+P2 执行器：对因子库批量跑 11 项硬闸门，写回 gate_status、
记录测试哈希、失败模式入库、重算 FSA 冻结名单。"""

import pandas as pd

import datasource
import factor_eval as fe
import gates as G
import library
import signals as sig
import structure
from common import all_pools, get_last_trade_day


def run_gates_for_pool(pool_name: str = "沪深300", only_pending: bool = True) -> dict:
    """对注册表因子逐个评估硬闸门。only_pending=True 时只跑未评估过的。"""
    registry = library.get_factor_registry()
    if registry.empty:
        return {"evaluated": 0, "passed": 0}
    if only_pending:
        registry = registry[registry["gate_status"].isna() | (registry["gate_status"] == "")]

    codes = all_pools()[pool_name]
    end = get_last_trade_day()
    panel = sig.get_panel_cached(codes, end, 800, source=datasource.get_loop_source())
    end_date = panel.index.get_level_values("datetime").max().strftime("%Y-%m-%d")

    # 已通过因子的 IC 序列用于相关性闸门
    passed_ics = {}
    n_eval, n_pass = 0, 0
    for _, row in registry.iterrows():
        name = row["name"]
        try:
            fac = {"name": name, "kind": row["kind"], "code": row.get("code")}
            vals = fe.get_factor_values(fac, codes, end)
            result = G.evaluate_gates(vals, panel, library_ics=passed_ics)
            ic_val = result["metrics"].get("IC", 0.0)
            library.record_tested(G.factor_hash(row.get("code") or name), name, row["kind"],
                                  row.get("engine", "rdagent"), end_date, result["pass"], ic_val)
            with library._lconn() as c:
                c.execute("UPDATE factor_registry SET gate_status=? WHERE name=?",
                          (int(result["pass"]), name))
            if result["pass"]:
                passed_ics[name] = fe.get_ic_series(fac, codes, end)
                n_pass += 1
            else:
                sk = row.get("skeleton") or structure.extract_skeleton(name, row.get("code"))
                library.record_failure(name, sk, row.get("family") or structure.assign_family(name, sk),
                                       "; ".join(result["reasons"])[:300], row.get("engine", "rdagent"))
            n_eval += 1
        except Exception as e:
            library.record_tested(G.factor_hash(row.get("code") or name), name, row["kind"],
                                  row.get("engine", "rdagent"), end_date, False, None)
            with library._lconn() as c:
                c.execute("UPDATE factor_registry SET gate_status=0 WHERE name=?", (name,))
    fsa = library.fsa_recompute()
    return {"evaluated": n_eval, "passed": n_pass, "frozen": int(fsa["frozen"].sum()) if not fsa.empty else 0}
