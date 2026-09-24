"""Read-only workflow heartbeat snapshot; never starts a scheduler or mining job."""
import json
import math
import time
from pathlib import Path

MINING_JOBS = ("multitype_mine", "loopengine")


def read_live_status(data_dir, now=None):
    now = time.time() if now is None else now
    try:
        live = json.loads((Path(data_dir) / "scheduler_live.json").read_text())
        ts = float(live["ts"])
        if not math.isfinite(ts) or not 0 <= now - ts < 180:
            raise ValueError("stale heartbeat")
        running = live.get("running", {})
        if not isinstance(running, dict):
            raise ValueError("invalid running state")
        running = {k: float(v) for k, v in running.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)
                   and math.isfinite(v) and 0 < v <= now}
        return {"fresh": True, "running": running, "ts": ts,
                "next_mining": (live.get("next") or {}).get("multitype_mine")}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"fresh": False, "running": {}, "ts": None, "next_mining": None}


def read_mining_progress(data_dir, live, now=None):
    """Only attach progress to the currently running scheduler batch."""
    now = time.time() if now is None else now
    started = live.get("running", {}).get("multitype_mine")
    if not live.get("fresh") or not started:
        return None
    try:
        p = json.loads((Path(data_dir) / "mining_progress.json").read_text())
        if not started <= p["started_at"] <= p["updated_at"] <= now:
            return None
        if p.get("status") in ("queued", "complete", "failed") and isinstance(p.get("queue"), list):
            return p
        if p["status"] not in ("preparing", "running", "round_complete", "skipped"):
            return None
        if not isinstance(p["factor_type"], str):
            return None
        if not 1 <= p["type_index"] <= p["total_types"]:
            return None
        if p["status"] == "running" and not 1 <= p["rotation"] <= p["rotations"]:
            return None
        return p
    except (OSError, ValueError, KeyError, TypeError):
        return None
