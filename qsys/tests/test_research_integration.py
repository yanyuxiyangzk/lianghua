import unittest
import pandas as pd
from test_research_backtest import fixture
from research_backtest import group_research
from scorecard_evidence import current_valid_mask
from factor_evaluation_queue import factor_version, POLICY
import factor_eval as fe


class IntegrationTests(unittest.TestCase):
    def test_public_factor_entry_uses_shared_results(self):
        close, vals = fixture()
        panel = close.stack().to_frame('$close')
        shared = group_research(vals, panel, 2, 5, 5)
        public = fe.factor_group_backtest(vals, panel, 2, 5, 5)
        pd.testing.assert_series_equal(shared['ls_nav'], public['ls_nav'])
        self.assertEqual(shared['ls_stats'], public['ls_stats'])

    def test_only_current_valid_evidence_survives(self):
        registry = pd.DataFrame([{'name': 'a', 'code': 'x'}])
        v = factor_version(registry.iloc[0].to_dict())
        good = {'因子': 'a', 'factor_version': v, 'policy_version': POLICY, 'evaluation_status': 'valid'}
        rows = [good, {**good, 'factor_version': 'old'},
                {**good, 'evaluation_status': 'legacy_unverified'},
                {**good, 'evaluation_status': 'sample_insufficient'},
                {**good, 'policy_version': 'old'}, {**good, '因子': 'deleted'}]
        self.assertEqual(current_valid_mask(pd.DataFrame(rows), registry).tolist(),
                         [True, False, False, False, False, False])

    def test_legacy_columns_fail_closed(self):
        mask = current_valid_mask(pd.DataFrame([{'因子': 'a'}]), pd.DataFrame([{'name': 'a'}]))
        self.assertFalse(mask.any())
