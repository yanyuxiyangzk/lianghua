"""因子库 / 策略库 持久化层（SQLite，market.db）。

三张表：
  factor_registry   因子注册表（名称/来源/代码/出处轮次）
  factor_scorecards 因子体检表（按股票池×评估日批量记录指标）
  strategies        策略包表（组合配置）

从文件迁移：packs.json / factor_cards/*.parquet 首次读取时自动导入，之后只走库。
"""

import json
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
    first_seen TEXT
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
"""

_PACKS_JSON = DATA_DIR / "packs.json"
_CARD_DIR = DATA_DIR / "factor_cards"


def _lconn():
    c = _qconn()
    c.executescript(_SCHEMA)
    # 迁移：factor_registry 加骨架/机制族/闸门列
    cols = [r[1] for r in c.execute("PRAGMA table_info(factor_registry)")]
    for col, ddl in [("skeleton", "TEXT"), ("family", "TEXT"), ("gate_status", "INTEGER"),
                     ("engine", "TEXT DEFAULT 'rdagent'")]:
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
    _migrate(c)
    return c


_migrated = False


def _migrate(c):
    """一次性把 packs.json / factor_cards parquet 导入库。"""
    global _migrated
    if _migrated:
        return
    _migrated = True
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


# ---------------------------------------------------------------- 因子注册表
def sync_factor_registry(factors: list[dict]):
    """同步因子注册表（自动提取骨架/机制族）。factors: [{name, kind, code?, trace?, round?, decision?}]"""
    import structure

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lconn() as c:
        for f in factors:
            sk = structure.extract_skeleton(f["name"], f.get("code"))
            fam = structure.assign_family(f["name"], sk)
            c.execute(
                "INSERT INTO factor_registry (name, kind, code, trace, round, decision, first_seen,"
                " skeleton, family, engine)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET code=excluded.code, trace=excluded.trace,"
                "   round=excluded.round, decision=excluded.decision,"
                "   skeleton=excluded.skeleton, family=excluded.family",
                (f["name"], f["kind"], f.get("code"), f.get("trace"),
                 f.get("round"), int(f["decision"]) if f.get("decision") is not None else None, now,
                 sk, fam, f.get("engine", "rdagent")))


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
            "INSERT OR REPLACE INTO strategies (name, pool_name, top_n, method, filters, factors, oos_winrate, horizon, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (name, pack.get("pool_name"), pack.get("top_n"), pack.get("method"),
             json.dumps(pack.get("filters", []), ensure_ascii=False),
             json.dumps(pack.get("factors", []), ensure_ascii=False),
             pack.get("oos_winrate"), pack.get("horizon"),
             pack.get("updated") or datetime.now().strftime("%Y-%m-%d %H:%M")))


def list_strategies() -> dict:
    """返回与 packs.json 相同的结构 {name: pack_dict}，便于各处平滑切换。"""
    with _lconn() as c:
        rows = c.execute("SELECT name, pool_name, top_n, method, filters, factors, oos_winrate,"
                         " horizon, updated_at FROM strategies").fetchall()
    out = {}
    for (name, pool, top_n, method, filters, factors, oos, horizon, updated) in rows:
        out[name] = {"pool_name": pool, "top_n": top_n, "method": method,
                     "filters": json.loads(filters or "[]"), "factors": json.loads(factors or "[]"),
                     "oos_winrate": oos, "horizon": horizon, "updated": updated}
    return out


def delete_strategy(name: str):
    with _lconn() as c:
        c.execute("DELETE FROM strategies WHERE name=?", (name,))


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
    """按入库因子骨架频次重算 FSA 冻结名单。"""
    with _lconn() as c:
        rows = c.execute(
            "SELECT skeleton, COUNT(*) AS n FROM factor_registry"
            " WHERE skeleton IS NOT NULL AND skeleton != '' GROUP BY skeleton").fetchall()
        total = sum(r[1] for r in rows) or 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for sk, n in rows:
            frozen = int((n / total) > threshold or n > variant_cap)
            c.execute("INSERT OR REPLACE INTO fsa_status (skeleton, count, frozen, updated_at)"
                      " VALUES (?,?,?,?)", (sk, n, frozen, now))
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
