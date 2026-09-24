"""One daily sequential mining batch; shared cross-process lock and data receipts."""
import json
import os
import time
import logging
import fcntl
import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
from common import DATA_DIR

DB = DATA_DIR / 'mining_runs.db'
LOCK = DATA_DIR / 'mining.lock'

def fingerprint(frames):
    h = hashlib.sha256()
    for name, frame in sorted(frames.items()):
        h.update(str(name).encode())
        h.update(str(list(frame.columns) if isinstance(frame, pd.DataFrame) else [frame.name]).encode())
        h.update(pd.util.hash_pandas_object(frame, index=True).values.tobytes())
    return h.hexdigest()

def validate_config(daily_batches=1, rotations=1, batch_per_type=15, hour=19, minute=30, interval_hours=1):
    values = dict(daily_batches=daily_batches, rotations=rotations, batch_per_type=batch_per_type,
                  hour=hour, minute=minute, interval_hours=interval_hours)
    limits = dict(daily_batches=(1,4), rotations=(1,3), batch_per_type=(1,50),
                  hour=(16,23), minute=(0,59), interval_hours=(1,7))
    for key, value in values.items():
        lo, hi = limits[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f'{key}必须为{lo}至{hi}的整数')
    if hour + (daily_batches-1)*interval_hours > 23:
        raise ValueError('批次启动时间不能跨过当天23:59，请减少次数或间隔')
    return values


def schedule_hours(cfg):
    v = validate_config(**cfg)
    return ','.join(str(v['hour']+i*v['interval_hours']) for i in range(v['daily_batches']))


def run_daily(pool_name, batch_per_type=15, factor_types=None, daily_batches=1, rotations=1, skip_unchanged=True, manual=False):
    validate_config(daily_batches=daily_batches, rotations=rotations, batch_per_type=batch_per_type,
                    hour=16)
    if not isinstance(skip_unchanged, bool):
        raise ValueError('skip_unchanged必须为布尔值')
    from loopengine.engine import LoopEngine, DEFAULT_FACTOR_TYPES
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    if not isinstance(manual, bool):
        raise ValueError('manual必须为布尔值')
    if not manual and (now.weekday() >= 5 or now.hour < 16):
        return '非工作日盘后时段，跳过挖掘'
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return '已有因子挖掘批次运行，跳过'
        with closing(sqlite3.connect(DB, timeout=5)) as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS batches(day TEXT PRIMARY KEY,status TEXT);
            CREATE TABLE IF NOT EXISTS inputs(pool TEXT,type TEXT,hash TEXT,PRIMARY KEY(pool,type));''')
            c.execute("CREATE TABLE IF NOT EXISTS batch_attempts(day TEXT,attempt INTEGER,status TEXT,PRIMARY KEY(day,attempt))")
            # Preserve consumed quota from the former one-row-per-day ledger.
            c.execute("INSERT OR IGNORE INTO batch_attempts SELECT day,1,status FROM batches")
            c.commit()
            day = now.date().isoformat()
            with c:
                c.execute('BEGIN IMMEDIATE')
                used = c.execute('SELECT COUNT(*) FROM batch_attempts WHERE day=?',(day,)).fetchone()[0]
                if used >= daily_batches:
                    return '今日已尝试盘后挖掘，跳过'
                attempt = used + 1
                c.execute('INSERT INTO batch_attempts VALUES (?,?,?)',(day,attempt,'running'))
            batch_started = time.time()
            progress_path = DB.parent / 'mining_progress.json'
            queue = []
            def progress(status, **details):
                for item in queue:
                    if (item['factor_type'] == details.get('factor_type')
                            and item['rotation'] == details.get('rotation')):
                        item['status'] = status
                        item['reason'] = details.get('reason', '')
                if status == 'failed':
                    for item in queue:
                        if item['status'] in ('running', 'preparing'):
                            item['status'] = 'failed'
                        elif item['status'] == 'queued':
                            item['status'] = 'not_run'

                payload = dict(status=status, started_at=batch_started, updated_at=time.time(),
                               day=day, attempt=attempt, rotations=rotations, queue=queue, **details)
                # Observability failure must not interrupt mining or invalidate quota.
                try:
                    temp = progress_path.with_suffix(f'.{os.getpid()}.tmp')
                    temp.write_text(json.dumps(payload, ensure_ascii=False))
                    temp.replace(progress_path)
                except OSError:
                    logging.getLogger(__name__).exception('Cannot persist mining progress')
            types = factor_types or DEFAULT_FACTOR_TYPES
            try:
                if any(ft not in DEFAULT_FACTOR_TYPES for ft in types):
                    raise ValueError('未知因子类型')
                ordered_types = list(dict.fromkeys(types))
                queue.extend(dict(factor_type=ft, rotation=r, status='queued')
                             for r in range(1, rotations+1) for ft in ordered_types)
                progress('queued')
                eng = LoopEngine(pool_name)
                parts = []
                eligible = set()
                completed = {}
                for rotation in range(1, rotations + 1):
                    for type_index, ft in enumerate(ordered_types, 1):
                        if rotation > 1 and ft not in eligible:
                            progress('skipped', factor_type=ft, rotation=rotation,
                                     type_index=type_index, total_types=len(ordered_types),
                                     completed=dict(completed), reason='首轮无有效新增数据')
                            continue
                        position = dict(factor_type=ft, type_index=type_index,
                                        total_types=len(ordered_types), rotation=rotation,
                                        completed=dict(completed))
                        progress('preparing', **position)
                        panel, frames, codes, end = eng._frames(ft)
                        data = frames if ft == '量价' else eng._last_extra_frames
                        if panel is None or panel.empty or not data or not any(
                                not f.empty and f.notna().to_numpy().any() for f in data.values()):
                            parts.append(f'{ft}第{rotation}轮:无有效数据跳过')
                            progress('skipped', reason='无有效数据', **position)
                            continue
                        if rotation == 1:
                            stamp = fingerprint(data)
                            row = c.execute('SELECT hash FROM inputs WHERE pool=? AND type=?',(pool_name,ft)).fetchone()
                            if skip_unchanged and row and row[0] == stamp:
                                parts.append(ft+':数据未变跳过')
                                progress('skipped', reason='数据未变', **position)
                                continue
                            # Reserve once per type and batch; later rotations reuse eligibility.
                            with c:
                                c.execute('INSERT OR REPLACE INTO inputs VALUES (?,?,?)',(pool_name,ft,stamp))
                            eligible.add(ft)
                        progress('running', **position)
                        result = eng.run_round(batch=batch_per_type, factor_type=ft,
                                               include_events=False, prepared=(panel,frames,codes,end))
                        parts.append(f"{ft}第{rotation}轮:{result['passed']}个")
                        completed[ft] = rotation
                        position['completed'] = dict(completed)
                        progress('round_complete', **position)
                with c:
                    c.execute('UPDATE batch_attempts SET status=? WHERE day=? AND attempt=?',('complete',day,attempt))
                progress('complete', completed=completed)
                # Request a bounded evaluation drain; scheduler consumes after mining releases its lock.
                from factor_evaluation_queue import request_drain
                request_drain(pool_name)
                return '盘后挖掘完成 · '+' · '.join(parts)
            except Exception:
                progress('failed')
                with c:
                    c.execute('UPDATE batch_attempts SET status=? WHERE day=? AND attempt=?',('failed',day,attempt))
                raise


def manual_request(action='enqueue'):
    """Durable one-shot request, consumed only by scheduler owner."""
    import uuid
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB, timeout=5)) as c, c:
        c.execute('CREATE TABLE IF NOT EXISTS manual_requests(id TEXT PRIMARY KEY,status TEXT,created_at TEXT)')
        c.execute('BEGIN IMMEDIATE')
        row = c.execute("SELECT id,status FROM manual_requests WHERE status IN ('pending','running') ORDER BY created_at LIMIT 1").fetchone()
        if action == 'enqueue':
            if row:
                return row[0]
            rid = uuid.uuid4().hex
            c.execute('INSERT INTO manual_requests VALUES (?,?,?)',(rid,'pending',datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()))
            return rid
        if action == 'claim':
            if not row or row[1] != 'pending':
                return None
            c.execute("UPDATE manual_requests SET status='running' WHERE id=?",(row[0],))
            return row[0]
        raise ValueError('unknown action')


def finish_manual_request(rid):
    with closing(sqlite3.connect(DB, timeout=5)) as c, c:
        c.execute("UPDATE manual_requests SET status='finished' WHERE id=?",(rid,))
