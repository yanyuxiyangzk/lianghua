"""Bounded strategy computation. Only the parent publishes validation evidence."""
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


def compute_isolated(name, pack, day, timeout_seconds=180):
    from validation_trace import ValidationTimeout
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600:
        raise ValueError('重验进程超时必须在0至3600秒之间')
    with tempfile.TemporaryDirectory(prefix='strategy-validation-') as tmp:
        root = Path(tmp)
        request = root / 'request.json'
        output = root / 'result.json'
        request.write_text(json.dumps(dict(name=name, pack=pack, day=day), ensure_ascii=False))
        with (root / 'worker.log').open('w') as log:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(request), str(output)],
                                    stdout=log, stderr=log, start_new_session=True)
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                raise ValidationTimeout(f'独立计算进程超过{timeout_seconds}秒，已终止进程组')
            finally:
                # Also clean up factor-code descendants on failure or cancellation.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
        if proc.returncode != 0 or not output.exists():
            raise RuntimeError(f'独立重验进程异常退出：{proc.returncode}')
        result = json.loads(output.read_text())
        if not isinstance(result, dict):
            raise ValueError('独立重验返回格式无效')
        return result


def run_bounded(name, timeout_seconds=180):
    import fcntl
    import hashlib
    from common import DATA_DIR
    import scheduler
    locks = DATA_DIR / 'validation_locks'
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / (hashlib.sha256(name.encode()).hexdigest() + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(ok=False, name=name, assessment_status='busy', error='该策略已有重验运行，本次跳过')
        return scheduler.revalidate_strategy(name, timeout_seconds=timeout_seconds, isolated=True)


if __name__ == '__main__':
    from validation_trace import Trace, trace_run
    import scheduler
    request = json.loads(Path(sys.argv[1]).read_text())
    trace = Trace(request['name'])
    try:
        with trace_run(trace):
            result = scheduler._compute_strategy_validation(request['name'], request['pack'], request['day'])
    except Exception as exc:
        result = dict(ok=False, error=str(exc), error_type=type(exc).__name__)
    result['worker_run_id'] = trace.run_id
    result['worker_stage_events'] = trace.events
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False))
