"""Versioned, fail-closed research evidence policy (not a trading authorization)."""
import math
import pandas as pd

POLICY_VERSION = '2026-09-23-v2'
MIN_IC_DAYS = {'量价': 250, '财务': 250, '板块轮动': 250, '指数': 250,
               '资金流': 250, '龙虎榜': 120, '盘口异动': 120,
               '爆量抢筹': 120, '事件记忆': 120, '支撑阻力': 250}


def sample_check(ic, factor_type):
    clean = ic.replace([float('inf'), -float('inf')], float('nan')).dropna()
    clean = clean[~clean.index.duplicated()].sort_index()
    minimum = MIN_IC_DAYS.get(factor_type, 250)
    reasons = []
    if len(clean) != len(ic):
        reasons.append('IC存在缺失/非有限值/重复日期')
    if len(clean) < minimum or len(clean.iloc[int(len(clean) * .8):]) < 24:
        reasons.append(f'有效IC样本不足：{len(clean)}/{minimum}，尾部至少24天')
    return reasons


def point_in_time_report(observations):
    """observations columns: available_at, decision_at, evidence_ref; timestamps in UTC/offset."""
    required = {'available_at', 'decision_at', 'evidence_ref'}
    if observations.empty or not required.issubset(observations.columns):
        return {'passed': False, 'reason': '缺少逐记录时点证据'}
    available = pd.to_datetime(observations.available_at, errors='coerce', utc=True)
    decision = pd.to_datetime(observations.decision_at, errors='coerce', utc=True)
    bad = available.isna() | decision.isna() | (available > decision)
    bad |= observations.evidence_ref.fillna('').astype(str).str.strip().eq('')
    return {'passed': not bool(bad.any()), 'observations': len(observations),
            'invalid_observations': int(bad.sum()), 'policy_version': POLICY_VERSION}


def regime_report(wf, regimes, scope):
    """Use historical date->regime labels, never today's regime for historical rows."""
    required = set(scope.split(',') if isinstance(scope, str) else scope or [])
    if 'all' in required:
        required = {'bull', 'bear', 'sideways', 'transition'}
    if not required or not required.issubset({'bull', 'bear', 'sideways', 'transition'}):
        return {'passed': False, 'reason': '未声明适用市场状态', 'regimes': {}}
    results = {}
    labels = wf['调仓日'].map(regimes)
    for regime in sorted(required):
        net = wf.loc[labels == regime, '优化组合扣费超额'].dropna()
        results[regime] = {'windows': len(net), 'mean_net_excess': float(net.mean()) if len(net) else None,
                           'passed': bool(len(net) >= 8 and net.mean() > 0)}
    return {'passed': bool(labels.notna().all() and all(x['passed'] for x in results.values())),
            'unknown_windows': int(labels.isna().sum()), 'regimes': results}


def _approval_evidence_rejection(approval, version):
    for stage in ('walk_forward', 'holdout', 'shadow', 'data_quality', 'regime_validation', 'search_budget'):
        r = approval.get(stage)
        if not isinstance(r, dict) or r.get('passed') is not True or not r.get('report_id'):
            return f'缺少{stage}验证报告'
        if r.get('strategy_version') != version or r.get('policy_version') != POLICY_VERSION:
            return f'{stage}报告版本不匹配'
    wf = approval['walk_forward']
    folds = wf.get('folds', [])
    if not isinstance(folds, list) or len(folds) < 3:
        return 'walk_forward至少需要3折'
    previous = None
    for f in folds:
        try:
            start, end = pd.Timestamp(f['start']), pd.Timestamp(f['end'])
            if pd.isna(start) or pd.isna(end) or start > end or (previous is not None and start <= previous):
                return 'walk_forward折日期重叠或无效'
            previous = end
            vals = [float(f[k]) for k in ('n_periods', 'mean_excess', 'sharpe', 'max_drawdown', 'turnover')]
            if not all(math.isfinite(x) for x in vals):
                return 'walk_forward指标无效'
            n, mean, sharpe, dd, turnover = vals
            if n < 8 or mean <= 0 or sharpe < .5 or dd < -.25 or not 0 <= turnover <= .8:
                return 'walk_forward折未达门槛'
        except (KeyError, TypeError, ValueError):
            return 'walk_forward折证据不完整'
    ho = approval['holdout']
    if ho.get('independent') is not True or not math.isfinite(float(ho.get('matured_windows', 0))) or ho.get('matured_windows', 0) < 8:
        return 'holdout缺少独立成熟样本'
    rg = approval['regime_validation'].get('regimes', {})
    if not rg or not all(math.isfinite(float(x.get('windows', 0))) and math.isfinite(float(x.get('mean_net_excess', 0))) and x.get('windows', 0) >= 8 and x.get('mean_net_excess', 0) > 0
                         for x in rg.values()):
        return '市场状态分层证据不足'
    sb = approval['search_budget']
    p = float(sb.get('adjusted_pvalue', float('nan')))
    if not math.isfinite(float(sb.get('n_trials', 0))) or sb.get('n_trials', 0) < 1 or not math.isfinite(p) or not 0 <= p <= .05:
        return '搜索校正证据不足'
    dq = approval['data_quality']
    if dq.get('point_in_time_passed') is not True:
        return '数据可用时点未验证'
    if not math.isfinite(float(approval['shadow'].get('matured_windows', 0))) or approval['shadow'].get('matured_windows', 0) < 20:
        return 'shadow成熟窗口不足20'
    return ''


def approval_evidence_rejection(approval, version):
    try:
        return _approval_evidence_rejection(approval, version)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return '审批证据格式无效'
