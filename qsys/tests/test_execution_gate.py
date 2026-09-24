import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution_gate import check, strategy_version


class GateTests(unittest.TestCase):
    def setUp(self):
        self.pack = dict(status='active', theory_id='momentum', theory_family='趋势',
                         regime_scope=['bull'], evidence_type=['技术'], risk_class='event',
                         account_scope='satellite', factors=[dict(name='f', theory_id='momentum')])
        self.approval = dict(approved=True, version=strategy_version(self.pack),
                             valid_from='2026-09-01', valid_until='2026-09-30',
                             **{s:dict(passed=True, report_id='test-report') for s in ('holdout','shadow','data_quality')})

        from validation_policy import POLICY_VERSION
        for stage in ('holdout', 'shadow', 'data_quality', 'walk_forward', 'regime_validation', 'search_budget'):
            self.approval[stage] = dict(passed=True, report_id='fixture',
                                       strategy_version=strategy_version(self.pack), policy_version=POLICY_VERSION)
        self.approval['walk_forward']['folds'] = [dict(start=f'2025-0{i+1}-01', end=f'2025-0{i+1}-28',
             n_periods=10, mean_excess=.01, sharpe=1, max_drawdown=-.1, turnover=.3) for i in range(3)]
        self.approval['holdout'].update(independent=True, matured_windows=8)
        self.approval['regime_validation']['regimes'] = {'bull':dict(windows=10, mean_net_excess=.01)}
        self.approval['search_budget'].update(n_trials=100, adjusted_pvalue=.01)
        self.approval['shadow']['matured_windows'] = 20
        self.approval['data_quality']['point_in_time_passed'] = True

    def test_verified_version(self):
        self.assertEqual(check(self.pack,self.approval,'bull','2026-09-23'),'')

    def test_no_implicit_approval(self):
        self.assertTrue(check(self.pack,None,'bull','2026-09-23'))

    def test_mutation_revokes_approval(self):
        self.pack['factors'][0]['weight']=2
        self.assertIn('版本',check(self.pack,self.approval,'bull','2026-09-23'))

    def test_regime_and_expiry(self):
        for regime,date in [('bear','2026-09-23'),('unknown','2026-09-23'),('bull','2026-10-01')]:
            self.assertTrue(check(self.pack,self.approval,regime,date))

    def test_required_reports(self):
        self.approval['holdout']['report_id']=''
        self.assertIn('holdout',check(self.pack,self.approval,'bull','2026-09-23'))

    def test_evidence_failures(self):
        import copy
        for stage, key, value in [('walk_forward', 'folds', []), ('shadow', 'matured_windows', 0),
                ('data_quality', 'point_in_time_passed', False), ('holdout', 'independent', False),
                ('search_budget', 'adjusted_pvalue', float('nan')), ('regime_validation', 'regimes', {})]:
            approval = copy.deepcopy(self.approval)
            approval[stage][key] = value
            self.assertTrue(check(self.pack, approval, 'bull', '2026-09-23'))

    def test_nonfinite_samples_rejected(self):
        for stage, key in [('holdout','matured_windows'), ('shadow','matured_windows'), ('search_budget','n_trials')]:
            import copy
            approval = copy.deepcopy(self.approval)
            approval[stage][key] = float('nan')
            self.assertTrue(check(self.pack, approval, 'bull', '2026-09-23'))

    def test_wrong_regime_evidence_rejected(self):
        self.approval['regime_validation']['regimes'] = {'bear':dict(windows=10, mean_net_excess=.01)}
        self.assertIn('市场', check(self.pack, self.approval, 'bull', '2026-09-23'))

    def test_missing_factor_theory(self):
        del self.pack['factors'][0]['theory_id']
        self.assertIn('因子',check(self.pack,self.approval,'bull','2026-09-23'))

if __name__=='__main__': unittest.main()
