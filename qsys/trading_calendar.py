"""Read-only exchange calendar. Missing coverage is unknown, never a trading permit."""
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path


def calendar_data(exchange="SSE"):
    import datasource
    dates, receipts = set(), []
    try:
        with sqlite3.connect(Path(datasource.MKT_DB).resolve().as_uri() + "?mode=ro", uri=True) as c:
            dates = {r[0] for r in c.execute(
                "SELECT date FROM ifind_calendar WHERE exchange=?", (exchange,))}
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='research_calendar_receipts'").fetchone():
                for start, end, raw, digest in c.execute(
                        "SELECT start,end,dates_json,digest FROM research_calendar_receipts WHERE exchange=?",
                        (exchange,)):
                    values = json.loads(raw)
                    actual = hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True,
                                                      default=str, allow_nan=False).encode()).hexdigest()
                    if (actual == digest and values == sorted(d for d in dates if start <= d <= end)):
                        receipts.append((start, end))
    except (sqlite3.Error, OSError, ValueError, TypeError):
        # Explicit stored sessions remain usable; corrupt receipts cannot certify closures.
        receipts = []
    return dates, receipts


def day_status(day, data=None):
    """True=open, False=confirmed closed, None=unknown coverage."""
    if date.fromisoformat(day).weekday() >= 5:
        return False
    dates, receipts = calendar_data() if data is None else data
    if day in dates:
        return True
    if any(start <= day <= end for start, end in receipts):
        return False
    return None


def previous_session(day):
    """Require a certified interval so a partial calendar cannot hide missing sessions."""
    dates, receipts = calendar_data()
    previous = max((d for d in dates if d < day), default=None)
    if previous and any(start <= previous and day <= end for start, end in receipts):
        return previous
    return None
