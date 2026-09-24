"""Per-run durable stage timings and optional main-thread deadline."""
import contextvars
import json
import signal
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from common import DATA_DIR

_active = contextvars.ContextVar('validation_trace', default=None)


class ValidationTimeout(BaseException):
    """Cancellation must escape datasource's recoverable Exception handlers."""
    pass


class Trace:
    def __init__(self, name):
        self.run_id = uuid.uuid4().hex
        self.name = name
        self.started = time.monotonic()
        self.events = []
        self.path = DATA_DIR / 'validation_runs' / (self.run_id + '.jsonl')

    def emit(self, stage, status, **details):
        event = dict(run_id=self.run_id, strategy=self.name, stage=stage, status=status,
                     ts=datetime.now().isoformat(), elapsed=round(time.monotonic()-self.started, 3), **details)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
        self.events.append(event)


@contextmanager
def stage(name):
    trace = _active.get()
    if trace:
        trace.emit(name, 'started')
    start = time.monotonic()
    try:
        yield
    except (Exception, ValidationTimeout) as exc:
        if trace:
            trace.emit(name, 'failed', seconds=round(time.monotonic()-start, 3), error_type=type(exc).__name__)
        raise
    else:
        if trace:
            trace.emit(name, 'completed', seconds=round(time.monotonic()-start, 3))


@contextmanager
def trace_run(trace, timeout_seconds=None):
    if timeout_seconds is not None:
        if threading.current_thread() is not threading.main_thread():
            raise ValueError('限时重验必须在独立进程主线程运行')
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 3600:
            raise ValueError('超时必须在0至3600秒之间')
        if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
            raise ValueError('进程已有计时器，不能覆盖')
    token = _active.set(trace)
    old = None
    try:
        trace.emit('run', 'started')
        if timeout_seconds is not None:
            old = signal.getsignal(signal.SIGALRM)
            def expired(*_):
                raise ValidationTimeout(f'策略重验超过{timeout_seconds}秒')
            signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        yield
    finally:
        if old is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
        _active.reset(token)
