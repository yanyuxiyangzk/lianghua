"""Read-only filtering of current, versioned research evidence."""
import pandas as pd
from factor_evaluation_queue import POLICY, factor_version


def current_valid_mask(card, registry):
    versions = {r['name']: factor_version(r) for r in registry.to_dict('records')}
    def column(name):
        return card.get(name, pd.Series('', index=card.index))
    return (column('evaluation_status').eq('valid')
            & column('policy_version').eq(POLICY)
            & column('因子').map(versions).notna()
            & column('factor_version').eq(column('因子').map(versions)))
