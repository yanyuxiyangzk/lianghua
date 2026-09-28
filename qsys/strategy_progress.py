"""Bounded strategy review and read-only, versioned qualification summaries."""
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from execution_gate import strategy_version
from selection_policy import SELECTION_POLICY


def latest_reports():
    import datasource
    path = Path(datasource.MKT_DB)
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='strategy_validation_reports'").fetchone():
            return {}
        rows = c.execute('SELECT strategy_name,created_at,report_json FROM strategy_validation_reports '
                         'ORDER BY created_at DESC,rowid DESC').fetchall()
    out = {}
    for name, created, raw in rows:
        if name in out:
            continue
        try:
            report = json.loads(raw)
            out[name] = {**report, '_created_at': created}
        except (TypeError, ValueError):
            out[name] = {'error': '报告无法解析', '_created_at': created}
    return out


def research_reason(pack, report):
    if pack.get('status') in ('paused', 'retired', 'archived'):
        return '策略已暂停或归档'
    if not report:
        return '尚无验证报告'
    if report.get('strategy_version') != strategy_version(pack):
        return '报告对应旧策略版本，待重验'
    if report.get('selection_policy') != SELECTION_POLICY:
        return '尚无当前固定选股规则的验证报告'
    if not report.get('ok'):
        return str(report.get('error') or report.get('assessment_status') or '验证未完成')
    if report.get('weight_mode') != 'frozen_strategy_snapshot':
        return '验证权重与固定策略不一致'
    if report.get('research_passed') is not True:
        return '固定策略研究未达标'
    try:
        thresholds = {'oos_windows': (30, None), 'oos_winrate': (.55, 1),
                      'sharpe': (.5, None), 'avg_net_excess': (0, None), 'max_drawdown': (-.25, 0)}
        for key, (low, high) in thresholds.items():
            value = report.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    or value < low or (high is not None and value > high)):
                return f'研究指标缺失或未达标：{key}'
        if report['avg_net_excess'] <= 0:
            return '扣费超额未达标'
        if not all(report.get(k, {}).get('passed') is True for k in ('walk_forward', 'regime_validation')):
            return '交叉验证或市场分层验证未通过'
        age = (datetime.now().date() - datetime.fromisoformat(report['eval_date']).date()).days
        if not 0 <= age <= 14:
            return '研究报告日期无效或超过14天，待重验'
    except (KeyError, TypeError, ValueError, AttributeError):
        return '研究证据不完整'
    return ''


def review_candidates(packs, reports, day, limit=2):
    """Old-policy reports are reviewed once; same-version failures cool down seven days."""
    candidates = []
    for name, pack in packs.items():
        if pack.get('status') in ('paused', 'retired', 'archived'):
            continue
        report = reports.get(name, {})
        current = (report.get('strategy_version') == strategy_version(pack)
                   and report.get('selection_policy') == SELECTION_POLICY)
        if current:
            try:
                created = datetime.fromisoformat(report['_created_at']).date()
                if datetime.fromisoformat(day).date() < created + timedelta(days=7):
                    continue
            except (KeyError, ValueError, TypeError):
                pass
        candidates.append((current, pack.get('status') != 'active', report.get('_created_at', ''), name))
    return [x[-1] for x in sorted(candidates)[:limit]]


def review_batch(max_strategies=2, timeout_seconds=90):
    import fcntl
    import library
    import validation_worker
    from common import DATA_DIR
    from selection_policy import completed_signal_day
    if not 1 <= int(max_strategies) <= 3 or not 1 <= float(timeout_seconds) <= 180:
        raise ValueError('每批最多3个策略，单策略最长180秒')
    with (DATA_DIR / 'strategy_review_batch.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return '已有分批策略重验运行，本次跳过'
        packs = library.list_strategies()
        names = review_candidates(packs, latest_reports(), completed_signal_day(), int(max_strategies))
        results, failures = [], []
        for name in names:
            try:
                r = validation_worker.run_bounded(name, timeout_seconds=float(timeout_seconds))
            except Exception as exc:
                failures.append(f'{name}：执行异常 {type(exc).__name__}: {exc}')
                continue
            if r.get('ok'):
                results.append(f"{name}：{'研究达标' if r.get('research_passed') else '研究未达标'}，不代表交易资格")
            else:
                failures.append(f"{name}：{r.get('assessment_status', '异常')} {r.get('error', '')}")
        message = '；'.join(results + failures) or '暂无到期策略，未重复消耗评估资源'
        if failures:
            raise RuntimeError(message)
        return message


def progress_rows(packs, reports, approvals, regime, today):
    from execution_gate import check
    rows = []
    for name, pack in packs.items():
        if pack.get('status') in ('archived', 'retired'):
            continue
        report = reports.get(name, {})
        reason = research_reason(pack, report)
        execution = check(pack, approvals.get(name), regime, today)
        rows.append({'策略': name, '股票池': pack.get('pool_name'),
                     '研究状态': reason or '当前规则研究达标',
                     '自动买入': '未放行' if reason or execution else '资格通过（仍需账户及行情风控）',
                     '执行限制': reason or execution or '待下单前复核',
                     '评估日期': report.get('eval_date', ''), '报告编号': report.get('report_id', '')})
    return rows
