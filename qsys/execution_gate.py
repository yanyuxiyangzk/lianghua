"""自动策略开仓资格。批准绑定内容版本，不以 active 标签替代审批。"""
import hashlib
import json
from common import DATA_DIR

APPROVAL_FILE = DATA_DIR / 'strategy_execution_approvals.json'


def strategy_version(pack):
    keys = ('factors', 'filters', 'pool_name', 'top_n', 'method', 'horizon',
            'theory_id', 'theory_family', 'regime_scope', 'evidence_type', 'risk_class', 'account_scope')
    payload = {k: pack.get(k) for k in keys}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def tokens(value):
    if isinstance(value, str):
        return {s.strip() for s in value.split(',') if s.strip()}
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return set(value)
    return set()


def check(pack, approval, regime, today):
    if pack.get('status') != 'active':
        return '策略未处于active状态'
    for key in ('theory_id', 'theory_family', 'regime_scope', 'evidence_type', 'risk_class'):
        if not pack.get(key) or pack[key] == 'unclassified':
            return f'理论属性不完整：{key}'
    if pack.get('account_scope') not in ('main', 'satellite'):
        return '策略账户范围未确认'
    if not pack.get('factors') or any(not f.get('theory_id') for f in pack['factors']):
        return '因子理论来源未确认'
    if any(f.get('theory_id') != pack.get('theory_id') for f in pack['factors']):
        return '策略与因子理论归属不一致'
    if regime not in ('bull', 'bear', 'sideways', 'transition'):
        return '市场状态未知'
    scopes = tokens(pack['regime_scope'])
    if 'all' not in scopes and regime not in scopes:
        return '策略不适用当前市场状态'
    if not isinstance(approval, dict) or approval.get('approved') is not True:
        return '策略尚无版本执行批准，仅允许影子研究'
    if approval.get('version') != strategy_version(pack):
        return '策略版本已变更，需重新验证'
    if not approval.get('valid_from', '') <= today <= approval.get('valid_until', ''):
        return '执行批准不在有效期'
    for stage in ('holdout', 'shadow', 'data_quality'):
        evidence = approval.get(stage, {})
        if not isinstance(evidence, dict) or evidence.get('passed') is not True or not evidence.get('report_id'):
            return f'缺少{stage}验证报告'
    from validation_policy import approval_evidence_rejection
    rejection = approval_evidence_rejection(approval, strategy_version(pack))
    if rejection:
        return rejection
    evidence_scopes = set(approval['regime_validation']['regimes'])
    required_scopes = {'bull', 'bear', 'sideways', 'transition'} if 'all' in scopes else scopes
    if not required_scopes.issubset(evidence_scopes) or regime not in evidence_scopes:
        return '市场分层报告未覆盖策略适用范围'
    return ''


def position_rejection(c, position_id, today):
    """只读资格检查；任一依赖异常关闭买入闸，不影响卖出。"""
    try:
        row = c.execute('SELECT pack_name,source,code,status,buy_date FROM positions WHERE id=?', (position_id,)).fetchone()
        if not row or not row[0] or row[1] in ('le_shadow', 'sched_satellite_scan'):
            return '未关联正式策略，只有影子资格'
        if row[3] != 'pending' or row[4] != today:
            return '持仓任务非当日待买入状态'
        import library
        from loopengine.regime import detect_regime
        pack = library.list_strategies().get(row[0], {})
        approvals = json.loads(APPROVAL_FILE.read_text())
        regime = (detect_regime() or {}).get('regime', 'unknown')
        rejection = check(pack, approvals.get(row[0]), regime, today)
        if rejection:
            return rejection
        import selection_gate
        result = selection_gate.evaluate_candidate(row[2], pack, regime)
        if result.status != 'pass':
            return '成交前选股复核未通过：' + result.reason
        return ''
    except Exception:
        return '执行资格或验证证据不可用，暂停自动买入'
