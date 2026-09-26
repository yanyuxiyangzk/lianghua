import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import pandas as pd
import execution_constraints as ec
from historical_execution import simulate


class ConstraintTests(TestCase):
    def test_sync_exact_dates_unknown_status_and_no_forward_fill(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ec,'DB',Path(tmp)/'constraints.db'):
            def fetch(code,indicator,start,end,**kw):
                self.assertEqual(kw['fill'],'Original')
                value = '交易' if indicator == ec.INDICATORS['suspended'] else 11. if indicator == ec.INDICATORS['limit_up'] else 9.
                return pd.DataFrame({'time':['2026-01-05'],'thscode':['600664.SH'],indicator:[value]}),None,0
            with patch('datasource.ths_date_serial',side_effect=fetch):
                r=ec.sync(['SH600664'],'2026-01-05','2026-01-06')
            self.assertEqual(sum(x['known'] for x in r),3)
            idx=pd.MultiIndex.from_product([['2026-01-05','2026-01-06'],['SH600664']],names=['date','code'])
            p=pd.DataFrame({'open':[10.,10.],'close':[10.,10.]},index=idx)
            out=ec.attach(p,'ths_ifind')
            self.assertEqual(out.iloc[0].limit_up,11.)
            self.assertTrue(pd.isna(out.iloc[1].limit_up))
            self.assertEqual(out.iloc[0].suspended,0)
            self.assertIsNone(ec.normalize('suspended','未知'))
            self.assertEqual(ec.normalize('suspended','停牌'),1.)
            self.assertEqual(list(ec.attach(p,'other').columns),list(p.columns))

    def test_suspension_rejects_even_without_open_price(self):
        idx=pd.MultiIndex.from_product([['2026-01-02','2026-01-05'],['A']],names=['date','code'])
        p=pd.DataFrame({'open':[10.,float('nan')],'close':[10.,10.], 'suspended':[0,1],
                        'limit_up':[11.,11.],'limit_down':[9.,9.]},index=idx)
        s=pd.DataFrame([dict(date='2026-01-02',code='A',target_shares=100)])
        r=simulate(s,p)
        self.assertTrue(r['fills'].empty)
        self.assertEqual(r['orders'].iloc[0].reason,'suspended_or_zero_volume')

    def test_missing_constraints_reject_instead_of_assuming_tradable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ec,'DB',Path(tmp)/'none.db'):
            idx=pd.MultiIndex.from_product([['2026-01-02','2026-01-05'],['A']],names=['date','code'])
            p=pd.DataFrame({'open':[10.,10.],'close':[10.,10.]},index=idx)
            p=ec.attach(p,'ths_ifind')
            s=pd.DataFrame([dict(date='2026-01-02',code='A',target_shares=100)])
            r=simulate(s,p)
            self.assertEqual(r['data_quality_status'],'incomplete')
            self.assertEqual(r['orders'].iloc[0].reason,'unknown_execution_constraint')
            self.assertIn('limit_up',r['constraints_missing'])
