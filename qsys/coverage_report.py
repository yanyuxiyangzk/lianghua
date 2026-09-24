"""Queue snapshot and configured capacity; neither proves trading eligibility."""
import math
from datetime import datetime

EVALUATION_JOBS = ('le_factor_eval_noon', 'le_factor_eval_pm', 'le_factor_eval')


def estimate_days(pending, daily_capacity):
    if pending <= 0:
        return 0.0
    if not isinstance(daily_capacity, (int, float)) or not math.isfinite(daily_capacity) or daily_capacity <= 0:
        return None
    return pending / daily_capacity


def configured_windows(pool, state=None):
    # Read saved settings merged exactly as scheduler._state; never instantiate scheduler.
    if state is None:
        from scheduler import JOBS
        from common import load_json, SCHED_STATE_FILE
        saved = load_json(SCHED_STATE_FILE, {})
        state = {k: {**JOBS[k]['default'], **saved.get(k, {})} for k in EVALUATION_JOBS}
    windows = []
    for key in EVALUATION_JOBS:
        cfg = state.get(key, {})
        params = cfg.get('params', {})
        if not cfg.get('enabled') or params.get('pool_name', '沪深300') != pool:
            continue
        windows.append(dict(job=key, hour=cfg.get('hour'), minute=cfg.get('minute'),
                            batch=max(1, min(int(params.get('batch', 500)), 100))))
    return windows


def build(pool='沪深300', batches=None, running_days=1, state=None):
    from factor_evaluation_queue import coverage
    c = coverage(pool)
    windows = configured_windows(pool, state) if batches is None else []
    batches = [w['batch'] for w in windows] if batches is None else list(batches)
    daily = sum(batches) * max(1, running_days)
    return {**c, 'schedule_batches': batches, 'schedule_windows': windows,
            'daily_capacity_estimate': daily, 'estimated_days': estimate_days(c['pending'], daily),
            'as_of': datetime.now().isoformat(timespec='seconds'),
            'interpretation': '当前队列版本覆盖，排除superseded；done包含失败、阻塞和样本不足，不代表交易资格。'
            '容量为配置上限，不是实测吞吐；重评、锁等待、失败退避及新增任务会影响完成时间。'}
