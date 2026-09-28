"""Immutable order evidence; never infer historical strategy identity from today's pack."""
import json


def capture(c, position_id, side, source):
    empty = dict(strategy_version=None, signal_batch_id=None, factor_snapshot=None,
                 attribution_status='manual' if source == 'manual' else 'historical_unknown')
    if position_id is None:
        return empty
    if side == 'sell':
        rows = c.execute("SELECT strategy_version,signal_batch_id,factor_snapshot,attribution_status "
                         "FROM broker_orders WHERE position_id=? AND side='buy' AND status='已成'",
                         (position_id,)).fetchall()
        if len(rows) == 1 and rows[0][0] and rows[0][3] == 'verified_snapshot':
            return dict(zip(empty, rows[0]))
        return empty
    row = c.execute('SELECT pack_name,pick_id,source,buy_date FROM positions WHERE id=?',
                    (position_id,)).fetchone()
    if not row or not row[0] or row[2] == 'reconcile_fix':
        raise ValueError('自动开仓缺少可保存的策略归因')
    import library
    from execution_gate import strategy_version
    pack = library.list_strategies().get(row[0])
    if not pack or not pack.get('factors'):
        raise ValueError('策略版本或因子快照不可用，暂停开仓')
    return dict(strategy_version=strategy_version(pack),
                signal_batch_id=f'pick:{row[1]}' if row[1] is not None else f'position:{position_id}',
                factor_snapshot=json.dumps(pack['factors'], ensure_ascii=False, sort_keys=True, allow_nan=False),
                attribution_status='verified_snapshot')
