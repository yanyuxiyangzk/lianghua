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


# ---------------------------------------------------------------- 搜索预算遥测（P2 顾问→硬闸的过渡）
def _record_budget_telemetry(pool_name: str, date: str, evaluated: int,
                             gate_passed: int, would_block: int, t_star: float) -> None:
    """每日落库：过闸因子中"会被搜索预算 Gate15 拦截"的数量——硬闸校准数据。"""
    from datetime import datetime
    with datasource._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS search_budget_telemetry(
            date TEXT NOT NULL, pool_name TEXT NOT NULL,
            evaluated INTEGER, gate_passed INTEGER, would_block INTEGER,
            t_star REAL, created_at TEXT, PRIMARY KEY(date, pool_name))""")
        c.execute("INSERT OR REPLACE INTO search_budget_telemetry VALUES(?,?,?,?,?,?,?)",
                  (date, pool_name, int(evaluated), int(gate_passed), int(would_block),
                   float(t_star), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def load_budget_telemetry(days: int = 30) -> pd.DataFrame:
    """近 N 日预算遥测（页面趋势展示）。"""
    with datasource._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS search_budget_telemetry(
            date TEXT NOT NULL, pool_name TEXT NOT NULL,
            evaluated INTEGER, gate_passed INTEGER, would_block INTEGER,
            t_star REAL, created_at TEXT, PRIMARY KEY(date, pool_name))""")
        return pd.read_sql_query(
            "SELECT date,pool_name,evaluated,gate_passed,would_block,t_star "
            "FROM search_budget_telemetry ORDER BY date DESC LIMIT ?",
            c, params=(int(days),))


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
    n_eval, n_pass, n_would_block = 0, 0, 0
    t_star = 0.0
    for _, row in registry.iterrows():
        name = row["name"]
        try:
            fac = {"name": name, "kind": row["kind"], "code": row.get("code")}
            vals = fe.get_factor_values(fac, codes, end)
            result = G.evaluate_gates(vals, panel, library_ics=passed_ics)
            ic_val = result["metrics"].get("IC", 0.0)
            # Gate 15 遥测：复用 evaluate_gates 的搜索预算顾问字段（零重算），
            # 统计"过了硬闸但会被搜索预算拦截"的因子——硬闸校准的拦击率。
            sb_pass = result["metrics"].get("搜索预算通过")
            if sb_pass is False:
                n_would_block += 1
            t_star = float(result["metrics"].get("搜索预算地板") or t_star)
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
    if n_eval:
        try:
            _record_budget_telemetry(pool_name, end_date, n_eval, n_pass,
                                     n_would_block, t_star)
        except Exception:
            pass
    return {"evaluated": n_eval, "passed": n_pass, "would_block": n_would_block,
            "t_star": t_star,
            "frozen": int(fsa["frozen"].sum()) if not fsa.empty else 0}
