"""因子库 / 策略库 持久化层（SQLite，market.db）。

三张表：
  factor_registry   因子注册表（名称/来源/代码/出处轮次）
  factor_scorecards 因子体检表（按股票池×评估日批量记录指标）
  strategies        策略包表（组合配置）

从文件迁移：packs.json / factor_cards/*.parquet 首次读取时自动导入，之后只走库。
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from common import DATA_DIR, load_json
from datasource import _qconn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS factor_registry (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,           -- evolved / builtin
    code TEXT,                    -- 进化因子代码
    trace TEXT, round INTEGER, decision INTEGER,
    first_seen TEXT,
    factor_type TEXT DEFAULT '量价',  -- 量价/资金流/板块轮动/龙虎榜/盘口异动/指数
    multi_objective_score REAL,       -- 多目标综合评分
    max_drawdown REAL, sharpe REAL, sortino REAL, calmar REAL,
    decay_status TEXT, decay_rate REAL  -- 因子衰减状态/衰减率
);
CREATE TABLE IF NOT EXISTS factor_scorecards (
    name TEXT NOT NULL, pool_name TEXT NOT NULL, eval_date TEXT NOT NULL,
    kind TEXT, ic_mean REAL, icir REAL, ic_winrate REAL, top_winrate REAL,
    direction TEXT, days INTEGER, updated_at TEXT,
    PRIMARY KEY (name, pool_name, eval_date)
);
CREATE TABLE IF NOT EXISTS strategies (
    name TEXT PRIMARY KEY,
    pool_name TEXT, top_n INTEGER, method TEXT,
    filters TEXT,                 -- JSON array
    factors TEXT,                 -- JSON array [{name,kind,weight,direction}]
    oos_winrate TEXT,
    horizon TEXT,                 -- 决策持有期（1日/5日/20日），调度器共振用
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tested_hashes (
    hash TEXT PRIMARY KEY,
    name TEXT, kind TEXT, engine TEXT,
    eval_date TEXT, passed INTEGER, ic REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS fsa_status (
    skeleton TEXT PRIMARY KEY,
    count INTEGER, frozen INTEGER DEFAULT 0, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS failure_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_name TEXT, skeleton TEXT, family TEXT, reason TEXT,
    engine TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS combo_strategies (
    name TEXT PRIMARY KEY,
    pool_name TEXT, top_n INTEGER,
    rule TEXT,                    -- vote2 / intersect
    packs TEXT,                   -- JSON array：成员策略包名
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS factor_usage (
    factor_name TEXT PRIMARY KEY,
    pick_count INTEGER DEFAULT 0,     -- 被选股使用的次数
    trade_count INTEGER DEFAULT 0,    -- 被模拟交易使用的次数
    last_used TEXT,                   -- 最后使用时间
    last_pick_date TEXT,              -- 最后被选股日期
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sched_exec_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    job_name TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    success INTEGER,
    message TEXT,
    params TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sched_job_time ON sched_exec_log(job_key, started_at);
CREATE TABLE IF NOT EXISTS gate_detail_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_name TEXT NOT NULL,
    gate_date TEXT NOT NULL,
    pool_name TEXT DEFAULT '沪深300',
    metrics TEXT NOT NULL,
    passed INTEGER NOT NULL,
    fail_reasons TEXT,
    created_at TEXT,
    UNIQUE(factor_name, gate_date, pool_name)
);
CREATE INDEX IF NOT EXISTS idx_gate_factor_date ON gate_detail_log(factor_name, gate_date);
CREATE TABLE IF NOT EXISTS walk_forward_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    pool_name TEXT,
    method TEXT,
    top_n INTEGER,
    fwd_days INTEGER,
    cost REAL,
    opt_return REAL,
    opt_excess REAL,
    opt_turnover REAL,
    opt_net_excess REAL,
    eq_return REAL,
    eq_excess REAL,
    eq_turnover REAL,
    eq_net_excess REAL,
    pool_median REAL,
    active_factors TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wf_run ON walk_forward_log(run_id, trade_date);
"""

_PACKS_JSON = DATA_DIR / "packs.json"
_CARD_DIR = DATA_DIR / "factor_cards"


def _lconn():
    c = _qconn()
    c.executescript(_SCHEMA)
    # 迁移：factor_registry 加骨架/机制族/闸门列
    cols = [r[1] for r in c.execute("PRAGMA table_info(factor_registry)")]
    for col, ddl in [("skeleton", "TEXT"), ("family", "TEXT"), ("gate_status", "INTEGER"),
                     ("engine", "TEXT DEFAULT 'rdagent'"), ("factor_type", "TEXT DEFAULT '量价'")]:
        if col not in cols:
            c.execute(f"ALTER TABLE factor_registry ADD COLUMN {col} {ddl}")
    # 迁移：factor_registry 加多目标评分/风险指标/衰减状态列
    for col, ddl in [("multi_objective_score", "REAL"), ("max_drawdown", "REAL"),
                     ("sharpe", "REAL"), ("sortino", "REAL"), ("calmar", "REAL"),
                     ("decay_status", "TEXT"), ("decay_rate", "REAL")]:
        if col not in cols:
            c.execute(f"ALTER TABLE factor_registry ADD COLUMN {col} {ddl}")
    # 迁移：factor_scorecards 加多周期胜率 JSON（1/5/20/60/120 日）
    sc_cols = [r[1] for r in c.execute("PRAGMA table_info(factor_scorecards)")]
    if "winrates" not in sc_cols:
        c.execute("ALTER TABLE factor_scorecards ADD COLUMN winrates TEXT")
    # 迁移：strategies 加持有期（多周期共振用）
    st_cols = [r[1] for r in c.execute("PRAGMA table_info(strategies)")]
    if "horizon" not in st_cols:
        c.execute("ALTER TABLE strategies ADD COLUMN horizon TEXT")
    # 迁移：strategies 加样本内胜率（🎯今日选股的过拟合信号灯用）
    if "is_winrate" not in st_cols:
        c.execute("ALTER TABLE strategies ADD COLUMN is_winrate TEXT")
    # 迁移：存量因子 factor_type 回填（NULL → 基于名称/family 推断）
    _backfill_factor_type(c)
    # 迁移：sched_exec_log 从 JSONL 导入历史数据
    _migrate_sched_history(c)
    _migrate(c)
    return c


_migrated = False


def _backfill_factor_type(c):
    """存量因子 factor_type 回填：NULL → 基于名称/family 推断类型。"""
    rows = c.execute("SELECT name, family FROM factor_registry WHERE factor_type IS NULL").fetchall()
    if not rows:
        return
    for name, fam in rows:
        ft = "量价"  # 默认
        low = (name or "").lower()
        fam_low = (fam or "").lower()
        if any(k in low or k in fam_low for k in ["lhb", "dragon", "龙虎榜"]):
            ft = "龙虎榜"
        elif any(k in low or k in fam_low for k in ["sector", "板块", "breadth", "rotation"]):
            ft = "板块轮动"
        elif any(k in low or k in fam_low for k in ["idx", "benchmark", "beta", "alpha", "relative_strength"]):
            ft = "指数"
        elif any(k in low or k in fam_low for k in ["bid_ask", "tick", "orderbook", "quantity_ratio", "outer_inner", "盘口"]):
            ft = "盘口异动"
        elif any(k in low or k in fam_low for k in ["main_net", "super_net", "big_net", "small_net",
                                                      "fundflow", "inflow", "资金流",
                                                      "cmf", "mfi", "obv", "adosc", "bop"]):
            ft = "资金流"
        elif fam and fam in ("资金流", "板块轮动", "龙虎榜", "盘口异动", "指数"):
            ft = fam
        c.execute("UPDATE factor_registry SET factor_type=? WHERE name=? AND factor_type IS NULL",
                  (ft, name))


def _migrate_sched_history(c):
    """一次性把 scheduler_history.jsonl 导入 sched_exec_log 表。"""
    hist_file = DATA_DIR / "scheduler_history.jsonl"
    if not hist_file.exists():
        return
    n = c.execute("SELECT COUNT(*) FROM sched_exec_log").fetchone()[0]
    if n > 0:
        return  # 已导入过
    JOBS_NAME = {
        "update_data": "每日数据更新", "ifind_daily_sync": "iFinD日线入库",
        "ifind_calendar": "iFinD交易日历", "ifind_basic_daily": "iFinD基本面",
        "ifind_announce": "iFinD公告", "ifind_stocklist_sync": "A股列表同步",
        "ifind_indexlist_sync": "指数列表同步", "ifind_realtime_sync": "实时快照",
        "ifind_cleanup": "过期数据清理", "watchlist_signals": "个股信号",
        "pool_scan": "板块/池扫描Top-N", "outcome_backfill": "战果回填",
        "gate_check": "硬闸门筛查", "quote_collect": "行情快照采集",
        "sector_flow_collect": "板块资金流采集", "loopengine": "LoopEngine演化",
        "multitype_mine": "多类型因子挖掘", "top5_composite": "Top5复合因子",
        "trade_simulate": "模拟交易回填", "position_track": "持仓跟踪",
        "minute_sync": "分钟线同步", "auction_confirm": "竞价确认",
        "le_factor_eval": "LoopEngine因子体检", "event_mine": "事件定向挖掘",
        "fundflow_sync": "个股资金流入库",
    }
    rows = []
    with hist_file.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                job = item.get("job", "")
                time_str = item.get("time", "")
                ok = item.get("ok", False)
                msg = item.get("msg", "")
                rows.append((job, JOBS_NAME.get(job, job), time_str, time_str, 0,
                             1 if ok else 0, msg if isinstance(msg, str) else str(msg), None, time_str))
            except Exception:
                continue
    if rows:
        try:
            c.execute("BEGIN")
            c.executemany(
                "INSERT INTO sched_exec_log (job_key,job_name,started_at,finished_at,"
                "duration_ms,success,message,params,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                rows)
            c.execute("COMMIT")
            print(f"[sched_exec_log] migrated {len(rows)} rows from history")
        except Exception as e:
            c.execute("ROLLBACK")
            logging.warning("[library] _migrate_sched_history failed: %s", e)


def _migrate(c):
    """一次性把 packs.json / factor_cards parquet 导入库。"""
    global _migrated
    if _migrated:
        return
    try:
        n = c.execute("SELECT COUNT(*) FROM strategies").fetchone()[0]
        if n == 0 and _PACKS_JSON.exists():
            packs = load_json(_PACKS_JSON, {})
            for name, pk in packs.items():
                c.execute(
                    "INSERT OR REPLACE INTO strategies (name, pool_name, top_n, method, filters, factors, oos_winrate, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (name, pk.get("pool_name"), pk.get("top_n"), pk.get("method"),
                     json.dumps(pk.get("filters", []), ensure_ascii=False),
                     json.dumps(pk.get("factors", []), ensure_ascii=False),
                     pk.get("oos_winrate"), pk.get("updated")))
        # factor_cards/*.parquet → factor_scorecards（文件名：<pool>_<eval_date>.parquet）
        n2 = c.execute("SELECT COUNT(*) FROM factor_scorecards").fetchone()[0]
        if n2 == 0 and _CARD_DIR.exists():
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for f in _CARD_DIR.glob("*.parquet"):
                try:
                    pool, eval_date = f.stem.rsplit("_", 1)
                    card = pd.read_parquet(f)
                    rows = []
                    for _, r in card.iterrows():
                        rows.append((r["因子"], pool, eval_date, r.get("来源"),
                                     _f(r.get("IC均值")), _f(r.get("ICIR")), _f(r.get("IC胜率")),
                                     _f(r.get("Top组胜率")), str(r.get("建议方向", "")),
                                     int(r.get("天数", 0) or 0), now))
                    c.executemany(
                        "INSERT OR REPLACE INTO factor_scorecards (name, pool_name, eval_date, kind,"
                        " ic_mean, icir, ic_winrate, top_winrate, direction, days, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
                except Exception:
                    continue
        _migrated = True
    except Exception as e:
        logging.warning("[library] _migrate failed (will retry): %s", e)


# ---------------------------------------------------------------- 因子注册表
def sync_factor_registry(factors: list[dict]):
    """同步因子注册表（自动提取骨架/机制族）。factors: [{name, kind, code?, trace?, round?, decision?, factor_type?}]"""
    import structure

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lconn() as c:
        for f in factors:
            sk = structure.extract_skeleton(f["name"], f.get("code"))
            fam = structure.assign_family(f["name"], sk)
            ft = f.get("factor_type", "量价")
            c.execute(
                "INSERT INTO factor_registry (name, kind, code, trace, round, decision, first_seen,"
                " skeleton, family, engine, factor_type)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET code=excluded.code, trace=excluded.trace,"
                "   round=excluded.round, decision=excluded.decision,"
                "   skeleton=excluded.skeleton, family=excluded.family,"
                "   factor_type=excluded.factor_type",
                (f["name"], f["kind"], f.get("code"), f.get("trace"),
                 f.get("round"), int(f["decision"]) if f.get("decision") is not None else None, now,
                 sk, fam, f.get("engine", "rdagent"), ft))


def get_factor_registry() -> pd.DataFrame:
    with _lconn() as c:
        return pd.read_sql("SELECT * FROM factor_registry", c)


# ---------------------------------------------------------------- 因子体检表
def save_scorecard(card: pd.DataFrame, pool_name: str, eval_date: str):
    """保存一批体检结果（build_scorecard 的输出 DataFrame，含多周期胜率列）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    win_cols = [c for c in card.columns if c.endswith("日胜率")]
    rows = []
    for _, r in card.iterrows():
        winrates = {c: _f(r.get(c)) for c in win_cols} if win_cols else {}
        rows.append((r["因子"], pool_name, eval_date, r.get("来源"),
                     _f(r.get("IC均值")), _f(r.get("ICIR")), _f(r.get("IC胜率")),
                     _f(r.get("Top组胜率")), str(r.get("建议方向", "")),
                     int(r.get("天数", 0) or 0),
                     json.dumps(winrates, ensure_ascii=False), now))
    with _lconn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO factor_scorecards (name, pool_name, eval_date, kind,"
            " ic_mean, icir, ic_winrate, top_winrate, direction, days, winrates, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def _f(v):
    try:
        return float(v) if pd.notna(v) else None
    except (TypeError, ValueError):
        return None


def get_latest_scorecard(pool_name: str) -> pd.DataFrame:
    """某池最新一批体检（兼容页面原 DataFrame 列名）。"""
    with _lconn() as c:
        d = c.execute("SELECT MAX(eval_date) FROM factor_scorecards WHERE pool_name=?",
                      (pool_name,)).fetchone()
        if not d or not d[0]:
            return pd.DataFrame()
        df = pd.read_sql("SELECT * FROM factor_scorecards WHERE pool_name=? AND eval_date=?",
                         c, params=(pool_name, d[0]))
    df = df.rename(columns={"name": "因子", "kind": "来源", "ic_mean": "IC均值", "icir": "ICIR",
                            "ic_winrate": "IC胜率", "top_winrate": "Top组胜率",
                            "direction": "建议方向", "days": "天数"})
    # 多周期胜率 JSON 展开回列（1日/5日/20日/60日/120日胜率）
    if "winrates" in df.columns:
        wr = df["winrates"].map(lambda s: json.loads(s) if isinstance(s, str) and s else {})
        wr_df = pd.DataFrame(list(wr), index=df.index)
        if not wr_df.empty:
            df = pd.concat([df.drop(columns=["winrates"]), wr_df], axis=1)
        else:
            df = df.drop(columns=["winrates"])
    return df


def list_scorecard_pools() -> list[str]:
    with _lconn() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT pool_name FROM factor_scorecards")]


# ---------------------------------------------------------------- 策略包
def save_strategy(name: str, pack: dict):
    with _lconn() as c:
        c.execute(
            "INSERT OR REPLACE INTO strategies (name, pool_name, top_n, method, filters, factors,"
            " oos_winrate, horizon, is_winrate, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, pack.get("pool_name"), pack.get("top_n"), pack.get("method"),
             json.dumps(pack.get("filters", []), ensure_ascii=False),
             json.dumps(pack.get("factors", []), ensure_ascii=False),
             pack.get("oos_winrate"), pack.get("horizon"), pack.get("is_winrate"),
             pack.get("updated") or datetime.now().strftime("%Y-%m-%d %H:%M")))


def list_strategies() -> dict:
    """返回与 packs.json 相同的结构 {name: pack_dict}，便于各处平滑切换。"""
    with _lconn() as c:
        rows = c.execute("SELECT name, pool_name, top_n, method, filters, factors, oos_winrate,"
                         " horizon, is_winrate, updated_at FROM strategies").fetchall()
    out = {}
    for (name, pool, top_n, method, filters, factors, oos, horizon, is_wr, updated) in rows:
        out[name] = {"pool_name": pool, "top_n": top_n, "method": method,
                     "filters": json.loads(filters or "[]"), "factors": json.loads(factors or "[]"),
                     "oos_winrate": oos, "horizon": horizon, "is_winrate": is_wr, "updated": updated}
    return out


def delete_strategy(name: str):
    with _lconn() as c:
        c.execute("DELETE FROM strategies WHERE name=?", (name,))


# ---------------------------------------------------------------- 策略组合（多包投票）
def save_combo(name: str, cfg: dict):
    """保存策略组合：{pool_name, top_n, rule, packs[包名...]}。
    独立于 strategies 表——组合包不是因子包，调度器 _best_pack 不会误选。"""
    with _lconn() as c:
        c.execute(
            "INSERT OR REPLACE INTO combo_strategies (name, pool_name, top_n, rule, packs, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (name, cfg.get("pool_name"), cfg.get("top_n"), cfg.get("rule"),
             json.dumps(cfg.get("packs", []), ensure_ascii=False),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def list_combos() -> dict:
    with _lconn() as c:
        rows = c.execute(
            "SELECT name, pool_name, top_n, rule, packs, created_at FROM combo_strategies").fetchall()
    return {n: {"pool_name": pool, "top_n": top_n, "rule": rule,
                "packs": json.loads(packs or "[]"), "created_at": created}
            for n, pool, top_n, rule, packs, created in rows}


def delete_combo(name: str):
    with _lconn() as c:
        c.execute("DELETE FROM combo_strategies WHERE name=?", (name,))


# ---------------------------------------------------------------- P1：哈希检查点
def record_tested(hash_: str, name: str, kind: str, engine: str,
                  eval_date: str, passed: bool, ic: float | None):
    """记录一个已测因子哈希（原子 upsert）。"""
    with _lconn() as c:
        c.execute(
            "INSERT INTO tested_hashes (hash, name, kind, engine, eval_date, passed, ic, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(hash) DO UPDATE SET eval_date=excluded.eval_date,"
            " passed=excluded.passed, ic=excluded.ic",
            (hash_, name, kind, engine, eval_date, int(passed), ic,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def is_tested(hash_: str) -> bool:
    with _lconn() as c:
        return c.execute("SELECT 1 FROM tested_hashes WHERE hash=?", (hash_,)).fetchone() is not None


def tested_stats() -> dict:
    with _lconn() as c:
        total, passed = c.execute("SELECT COUNT(*), COALESCE(SUM(passed),0) FROM tested_hashes").fetchone()
    return {"tested": total, "passed": int(passed)}


# ---------------------------------------------------------------- P2：FSA 与失败模式
def fsa_recompute(threshold: float = 0.15, variant_cap: int = 3) -> pd.DataFrame:
    """按入库因子骨架频次重算 FSA 冻结名单。
    
    冻结规则：
      1. 变体数 > variant_cap (默认3)
      2. 占比 > threshold (默认15%)
      3. 失败次数 > 30 (新增：高频失败骨架冻结)
    """
    with _lconn() as c:
        rows = c.execute(
            "SELECT skeleton, COUNT(*) AS n FROM factor_registry"
            " WHERE skeleton IS NOT NULL AND skeleton != '' GROUP BY skeleton").fetchall()
        total = sum(r[1] for r in rows) or 1
        
        # 获取失败次数（包括未注册的骨架）
        fail_counts = {}
        for sk, cnt in c.execute(
            "SELECT skeleton, COUNT(*) FROM failure_patterns GROUP BY skeleton"
        ).fetchall():
            fail_counts[sk] = cnt
        
        # 收集所有需要冻结的骨架
        all_skeletons = set()
        for sk, n in rows:
            all_skeletons.add(sk)
        for sk in fail_counts:
            all_skeletons.add(sk)
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for sk in all_skeletons:
            # 获取注册表中的数量
            reg_count = 0
            for r_sk, r_n in rows:
                if r_sk == sk:
                    reg_count = r_n
                    break
            
            fail_cnt = fail_counts.get(sk, 0)
            frozen = int((reg_count / total) > threshold or reg_count > variant_cap or fail_cnt > 30)
            c.execute("INSERT OR REPLACE INTO fsa_status (skeleton, count, frozen, updated_at)"
                      " VALUES (?,?,?,?)", (sk, reg_count, frozen, now))
        return pd.read_sql("SELECT * FROM fsa_status ORDER BY count DESC", c)


def is_frozen(skeleton: str) -> bool:
    if not skeleton:
        return False
    with _lconn() as c:
        r = c.execute("SELECT frozen FROM fsa_status WHERE skeleton=?", (skeleton,)).fetchone()
    return bool(r and r[0])


def record_failure(name: str, skeleton: str, family: str, reason: str, engine: str):
    with _lconn() as c:
        c.execute("INSERT INTO failure_patterns (factor_name, skeleton, family, reason, engine, created_at)"
                  " VALUES (?,?,?,?,?,?)",
                  (name, skeleton, family, reason, engine,
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def failure_stats(limit: int = 20) -> pd.DataFrame:
    with _lconn() as c:
        return pd.read_sql(
            "SELECT skeleton, family, COUNT(*) AS n, MAX(created_at) AS last_fail"
            " FROM failure_patterns GROUP BY skeleton ORDER BY n DESC LIMIT ?",
            c, params=(limit,))


# ---------------------------------------------------------------- 族实战统计（回喂 LoopEngine 生成预算）
def family_live_stats(min_n: int = 3) -> dict:
    """{机制族: 实战胜率} —— 经验库因子近似归因 × 注册表族标签。
    结算周期按 20/5/1 日逐级回退（20 日战果积累慢，新库先用短周期让回喂尽快生效）；
    只统计有 ≥min_n 次实战结算的因子；无数据返回 {}（调用方按无偏置处理）。"""
    try:
        import experience
        flb = pd.DataFrame()
        win_col = None
        for fwd in (20, 5, 1):
            flb = experience.factor_leaderboard(fwd=fwd)
            if not flb.empty:
                win_col = f"{fwd}日胜率(近似)"
                break
        if flb.empty or not win_col:
            return {}
        reg = get_factor_registry()
        fam_map = dict(zip(reg["name"], reg["family"].fillna("其他"))) if not reg.empty else {}
        acc = {}
        for _, r in flb.iterrows():
            n_ = int(r["参与且有战果的次数"])
            if n_ < min_n:
                continue
            fam = fam_map.get(r["因子"])
            if not fam:
                continue
            acc.setdefault(fam, []).extend([float(r[win_col])] * n_)
        return {f: sum(v) / len(v) for f, v in acc.items()}
    except Exception:
        return {}


# ---------------------------------------------------------------- 因子使用追踪
def record_factor_usage(factor_names: list[str], usage_type: str = "pick",
                        pick_date: str = None) -> None:
    """记录因子被使用的次数。usage_type: pick=选股, trade=模拟交易"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lconn() as c:
        for name in factor_names:
            c.execute("""
                INSERT INTO factor_usage (factor_name, pick_count, trade_count, last_used, last_pick_date, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(factor_name) DO UPDATE SET
                    pick_count = pick_count + ?,
                    trade_count = trade_count + ?,
                    last_used = ?,
                    last_pick_date = CASE WHEN ? > last_pick_date THEN ? ELSE last_pick_date END,
                    updated_at = ?
            """, (name,
                  1 if usage_type == "pick" else 0,
                  1 if usage_type == "trade" else 0,
                  now, pick_date or now, now,
                  1 if usage_type == "pick" else 0,
                  1 if usage_type == "trade" else 0,
                  now, pick_date or now, pick_date or now, now))


def get_factor_usage(factor_names: list[str] = None) -> pd.DataFrame:
    """获取因子使用统计。"""
    with _lconn() as c:
        if factor_names:
            placeholders = ",".join("?" * len(factor_names))
            return pd.read_sql(
                f"SELECT * FROM factor_usage WHERE factor_name IN ({placeholders})",
                c, params=factor_names)
        return pd.read_sql("SELECT * FROM factor_usage", c)


def factor_value_scores(factor_type: str = None, min_days: int = 5) -> pd.DataFrame:
    """因子价值5维评分：IC质量(30%) + IC稳定性(25%) + 一致性(20%) + 使用率(15%) + 新鲜度(10%)
    返回 DataFrame: name, factor_type, ic_score, stability_score, consistency_score,
                     usage_score, freshness_score, total_score
    """
    with _lconn() as c:
        # 获取所有活跃因子
        where = "WHERE gate_status=1"
        params = []
        if factor_type:
            where += " AND factor_type=?"
            params.append(factor_type)
        reg = pd.read_sql(
            f"SELECT name, factor_type, first_seen FROM factor_registry {where}",
            c, params=params)
        if reg.empty:
            return pd.DataFrame()

        # 获取scorecards
        sc = pd.read_sql(
            "SELECT name, ic_mean, ic_winrate, top_winrate, days, eval_date "
            "FROM factor_scorecards WHERE days >= ? ORDER BY eval_date DESC",
            c, params=(min_days,))

        # 获取使用统计
        usage = pd.read_sql("SELECT factor_name, pick_count, trade_count FROM factor_usage", c)
        usage_map = dict(zip(usage["factor_name"], usage["pick_count"] + usage["trade_count"])) if not usage.empty else {}

        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        results = []

        for _, row in reg.iterrows():
            name = row["name"]
            ftype = row["factor_type"] or "量价"
            first_seen = row["first_seen"]

            # 取该因子最新的scorecard
            fsc = sc[sc["name"] == name].head(1)
            if fsc.empty:
                # 无scorecard，给基础分
                ic_score = 0.3
                stability_score = 0.3
                consistency_score = 0.3
            else:
                ic_mean = abs(fsc.iloc[0]["ic_mean"] or 0)
                ic_winrate = fsc.iloc[0]["ic_winrate"] or 0.5
                top_winrate = fsc.iloc[0]["top_winrate"] or 0.5

                # IC质量：|IC| * IC胜率
                ic_score = min(1.0, ic_mean * 10 * 0.5 + ic_winrate * 0.5)

                # IC稳定性：IC胜率接近1越稳定
                stability_score = ic_winrate

                # 一致性：Top组胜率
                consistency_score = top_winrate

            # 使用率：被选股/交易使用的次数，sigmoid归一化
            total_usage = usage_map.get(name, 0)
            usage_score = min(1.0, total_usage / 10)  # 10次以上满分

            # 新鲜度：越近期入库的因子越新鲜
            if first_seen:
                days_old = (pd.Timestamp(today) - pd.Timestamp(first_seen)).days
                freshness_score = max(0.1, 1.0 - days_old / 180)  # 180天后降到0.1
            else:
                freshness_score = 0.5

            # 综合评分
            total_score = (
                ic_score * 0.30 +
                stability_score * 0.25 +
                consistency_score * 0.20 +
                usage_score * 0.15 +
                freshness_score * 0.10
            )

            results.append({
                "name": name,
                "factor_type": ftype,
                "ic_score": round(ic_score, 4),
                "stability_score": round(stability_score, 4),
                "consistency_score": round(consistency_score, 4),
                "usage_score": round(usage_score, 4),
                "freshness_score": round(freshness_score, 4),
                "total_score": round(total_score, 4),
            })

    return pd.DataFrame(results)
