"""P4：Top5 复合因子（方向修正 z-score 等权合成，对标文章 Sharpe 3.14）。

从过硬闸门的因子中按夏普取 Top5，方向按 IC 符号修正后等权合成，
跟踪复合 IC / 夏普 / 年化超额，并固化为特殊策略包（可挂定时任务出名单）。
"""

import numpy as np
import pandas as pd

import factor_eval as fe
import gates as G
import library
import signals as sig
from common import all_pools, get_last_trade_day

PACK_NAME = "Top5复合因子"


def _overall_sharpe(vals: pd.Series, panel: pd.DataFrame) -> tuple[float, pd.Series]:
    """整体夏普（日度超额序列年化）+ 返回超额序列。"""
    fwd = fe.forward_returns(panel, G.GATE["FWD_DAYS"])
    x = G._daily_excess(vals, fwd)
    shp = float(x.mean() / (x.std() + 1e-12) * np.sqrt(252)) if len(x) > 5 else 0.0
    return shp, x


def build_top5_composite(pool_name: str = "沪深300", top_n: int = 5) -> dict:
    registry = library.get_factor_registry()
    passed = registry[registry["gate_status"] == 1]
    if len(passed) < 3:
        return {"ok": False, "msg": f"过闸因子不足（{len(passed)}<3），暂无法合成"}

    codes = all_pools()[pool_name]
    end = get_last_trade_day()
    panel = sig.get_panel_cached(codes, end, 800, source="qlib_local")

    # 预筛：用检查点入库时的 IC 取 Top25，避免对全部过闸因子逐个重算（规模上来后的必需优化）
    try:
        with library._lconn() as c:
            ic_map = dict(c.execute(
                "SELECT code, MAX(ABS(ic)) FROM tested_hashes th JOIN factor_registry fr"
                " ON th.hash IS NOT NULL GROUP BY th.name").fetchall()) or {}
    except Exception:
        ic_map = {}
    cand = passed.copy()
    if ic_map:
        cand["_pre_ic"] = cand["code"].map(lambda x: ic_map.get((x or "").split("\n")[0].replace("# sexpr: ", "")[:60], 0))
        cand = cand.sort_values("_pre_ic", ascending=False).head(25)

    # 逐因子算整体夏普，取 Top5
    rows = []
    for _, r in cand.iterrows():
        try:
            fac = {"name": r["name"], "kind": r["kind"], "code": r.get("code")}
            vals = fe.get_factor_values(fac, codes, end)
            shp, _ = _overall_sharpe(vals, panel)
            ic = fe.get_ic_series(fac, codes, end, fwd_days=G.GATE["FWD_DAYS"])
            rows.append({"name": r["name"], "kind": r["kind"], "sharpe": shp,
                         "ic_mean": float(ic.mean()) if len(ic) else 0.0, "vals": vals})
        except Exception:
            continue
    rows = [r for r in rows if np.isfinite(r["sharpe"])]
    rows.sort(key=lambda r: -r["sharpe"])
    top = rows[:top_n]
    if not top:
        return {"ok": False, "msg": "无有效因子"}

    # 方向修正 z-score 等权合成
    comp = None
    members = []
    for r in top:
        direction = 1 if r["ic_mean"] >= 0 else -1
        v = fe._norm(r["vals"].dropna())
        dt_level = "datetime"
        day = v.index.get_level_values(dt_level).max()
        cross = v[v.index.get_level_values(dt_level) == day]
        z = (v - v.mean()) / (v.std() + 1e-12) * direction
        comp = z if comp is None else comp.add(z, fill_value=0)
        members.append({"name": r["name"], "kind": r["kind"], "weight": 1.0 / len(top),
                        "direction": direction, "sharpe": round(r["sharpe"], 2)})
    comp = comp / len(top)

    # 复合指标
    ic = fe.ic_series(comp.dropna(), fe.forward_returns(panel, G.GATE["FWD_DAYS"]))
    shp, x = _overall_sharpe(comp.dropna(), panel)
    ann = float(x.mean() * 252) if len(x) else 0.0
    by_year = {str(y): round(float(x[x.index.year == y].mean() * 252), 4)
               for y in sorted(set(x.index.year)) if (x.index.year == y).sum() > 20}

    factors = [{"name": m["name"], "kind": m["kind"], "weight": m["weight"],
                "direction": m["direction"]} for m in members]
    library.save_strategy(PACK_NAME, {
        "pool_name": pool_name, "top_n": 20, "method": "等权复合(方向修正)",
        "filters": ["tradable"], "factors": factors,
        "oos_winrate": f"{shp:.2f}夏普",
        "updated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")})
    return {"ok": True, "members": members, "IC": round(float(ic.mean()), 4) if len(ic) else 0.0,
            "sharpe": round(shp, 2), "年化超额": round(ann, 4), "分年超额": by_year}
