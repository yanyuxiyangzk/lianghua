"""统一选股硬闸：缺数据不放行，结果可审计。"""
from dataclasses import dataclass, field
from datetime import datetime
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

@dataclass
class GateResult:
    status: str
    reason: str = ""
    evidence: dict = field(default_factory=dict)

def _r(status, reason="", **e):
    return GateResult(status, reason, e)

def check_market_state(pack=None, regime="unknown"):
    scope = (pack or {}).get("regime_scope") or (pack or {}).get("regime")
    if regime not in ("bull", "bear", "sideways", "transition") or not scope:
        return _r("insufficient_data", "市场状态或策略适用范围缺失")
    if scope == "all":
        return _r("pass", regime=regime)
    allowed = scope if isinstance(scope, (list, tuple, set)) else [x.strip() for x in str(scope).split(",")]
    return _r("pass", regime=regime) if "all" in allowed or regime in allowed else _r("reject", f"市场状态{regime}不匹配策略范围{scope}", regime=regime)

def _query(sql, params=()):
    import datasource
    try:
        with closing(sqlite3.connect(
                Path(datasource.MKT_DB).resolve().as_uri() + "?mode=ro",
                uri=True, timeout=2)) as c:
            return c.execute(sql, params).fetchone()
    except Exception:
        return None

def check_sector(code, asof=None):
    asof = asof or datetime.now().strftime("%Y-%m-%d")
    row = _query("SELECT sector_name, updated_at FROM stock_industry WHERE code=? AND substr(updated_at,1,10)<=?", (code, asof))
    if not row or not row[0]:
        return _r("insufficient_data", "缺少股票板块归属")
    try:
        updated = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        if updated > datetime.now(ZoneInfo("Asia/Shanghai")):
            return _r("insufficient_data", "板块归属更新时间在未来")
    except (TypeError, ValueError):
        return _r("insufficient_data", "板块归属更新时间无效")
    daily = _query("SELECT date FROM sector_daily WHERE sector_name=? AND date<=? ORDER BY date DESC LIMIT 1", (row[0], asof))
    if not daily:
        return _r("insufficient_data", "缺少板块日线", sector=row[0])
    return _r("pass", sector=str(row[0]), data_date=daily[0])

def check_financial(code, asof=None, required=True):
    asof = asof or datetime.now().strftime("%Y-%m-%d")
    row = _query("SELECT report_date, fetched_at, value FROM ifind_financial "
                 "WHERE code IN (?,?) AND report_date<=? AND substr(fetched_at,1,10)<=? "
                 "AND value IS NOT NULL ORDER BY report_date DESC LIMIT 1",
                 (code, code[-6:], asof, asof))
    if row is None:
        return _r("insufficient_data", "缺少可用财务数据") if required else _r("pass", "财务数据非必需")
    try:
        if not math.isfinite(float(row[2])):
            return _r("insufficient_data", "财务指标值无效")
    except (TypeError, ValueError, OverflowError):
        return _r("insufficient_data", "财务指标值无效")
    try:
        fetched = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        if fetched > datetime.now(ZoneInfo("Asia/Shanghai")):
            return _r("insufficient_data", "财务采集时间在未来")
    except (TypeError, ValueError):
        return _r("insufficient_data", "财务采集时间无效")
    return _r("pass", "财务数据存在（不代表财务质量达标）", report_date=row[0], fetched_at=row[1])

def check_technical(code, max_minutes=10):
    row = _query("SELECT datetime,price FROM ifind_realtime WHERE code=? ORDER BY datetime DESC LIMIT 1", (code,))
    if not row or not row[0] or row[1] is None:
        return _r("insufficient_data", "缺少实时行情")
    try:
        ts = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        age = (datetime.now(ZoneInfo("Asia/Shanghai")) - ts).total_seconds()/60
        if not math.isfinite(float(row[1])) or float(row[1]) <= 0:
            return _r("reject", "行情价格无效")
        if age < -1 or age > max_minutes:
            return _r("reject", "行情过期", age_minutes=age)
    except Exception:
        return _r("insufficient_data", "行情时间无效")
    return _r("pass", price=row[1], age_minutes=age)

def evaluate_candidate(code, pack=None, regime="unknown", financial_required=True):
    checks = {
        "market": check_market_state(pack, regime),
        "sector": check_sector(code),
        "financial": check_financial(code, required=financial_required),
        "technical": check_technical(code),
    }
    bad = [f"{k}:{v.reason}" for k,v in checks.items() if v.status == "reject"]
    missing = [f"{k}:{v.reason}" for k,v in checks.items() if v.status == "insufficient_data"]
    status = "reject" if bad else ("insufficient_data" if missing else "pass")
    return GateResult(status, "; ".join(bad or missing), {k: v.__dict__ for k,v in checks.items()})
