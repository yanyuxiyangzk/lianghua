"""Date- and version-scoped satellite candidate reads (never relabel history)."""
import json
import pandas as pd
import experience as exp
from execution_gate import strategy_version
from selection_policy import SELECTION_POLICY


def latest_candidates(pack_name, pack, signal_day=None, observation=False, history=False):
    source = 'sched_satellite_scan' if observation else 'satellite_scan'
    where = 'p.source=? AND p.pack_name=?'
    params = [source, pack_name]
    if not history:
        if not signal_day:
            raise ValueError('缺少目标行情日期')
        where += ' AND p.trade_date=?'
        params.append(signal_day)
    with exp._conn() as c:
        batch = c.execute(
            'SELECT p.id,e.risk_json FROM picks p LEFT JOIN pick_decision_evidence e ON e.pick_id=p.id '
            'WHERE ' + where + ' ORDER BY p.trade_date DESC,p.created_at DESC,p.id DESC LIMIT 1', params).fetchone()
        if not batch:
            return pd.DataFrame()
        if not history:
            try:
                evidence = json.loads(batch[1] or '{}')
            except (ValueError, TypeError):
                return pd.DataFrame()
            if (evidence.get('strategy_version') != strategy_version(pack)
                    or evidence.get('selection_policy') != SELECTION_POLICY):
                return pd.DataFrame()
        return pd.read_sql_query(
            'SELECT p.id,p.trade_date,p.created_at,p.source,p.pack_name,pi.code,pi.score '
            'FROM picks p JOIN pick_items pi ON pi.pick_id=p.id WHERE p.id=? ORDER BY pi.rank',
            c, params=(batch[0],))
