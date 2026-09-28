import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import single_stock_jobs as j


class SuspensionTests(unittest.TestCase):
    def setUp(self):
        self.days=pd.bdate_range('2025-01-01',periods=190).strftime('%Y-%m-%d').tolist()
        self.d=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,amount=1000.),index=self.days)
        self.d.index.name='date'

    def validate(self, d, states):
        def attach(frame,source):
            return frame.assign(suspended=[states.get(day,np.nan) for day,code in frame.index])
        with patch('execution_constraints.attach',side_effect=attach):
            return j.validated_daily('SH600825',d,self.days)

    def test_confirmed_suspension_keeps_calendar_and_raw_missing(self):
        self.d.loc[self.days[80:90],:]=np.nan
        out,quality=self.validate(self.d,{day:1 for day in self.days[80:90]})
        self.assertEqual(out.index.tolist(),self.days)
        self.assertTrue(out.loc[self.days[80:90]].isna().all().all())
        self.assertEqual(quality['valid_days'],180)
        self.assertEqual(quality['suspension_dates'],self.days[80:90])

    def test_missing_whole_bar_requires_confirmed_suspension(self):
        d=self.d.drop(self.days[85])
        with self.assertRaisesRegex(ValueError,self.days[85]):self.validate(d,{})
        out,quality=self.validate(d,{self.days[85]:1})
        self.assertTrue(out.loc[self.days[85]].isna().all())
        self.assertEqual(quality['missing_calendar_rows'],[self.days[85]])

    def test_unknown_and_trading_nan_still_fail_with_field(self):
        self.d.loc[self.days[80],'amount']=np.nan
        for state in ({},{self.days[80]:0}):
            with self.assertRaisesRegex(ValueError,'amount'):self.validate(self.d,state)

    def test_invalid_price_and_ohlc_are_not_waived(self):
        self.d.loc[self.days[80],'close']=-1
        with self.assertRaisesRegex(ValueError,'close'):self.validate(self.d,{})
        self.d.loc[self.days[80],'close']=12
        with self.assertRaisesRegex(ValueError,'OHLC'):self.validate(self.d,{})

    def test_vendor_filled_suspension_cannot_be_mined(self):
        original=self.d.copy()
        out,_=self.validate(self.d,{self.days[80]:1})
        self.assertTrue(out.loc[self.days[80]].isna().all())
        pd.testing.assert_frame_equal(self.d,original)

    def test_labels_and_features_never_bridge_suspension(self):
        close=pd.Series(np.arange(20,dtype=float)+10)
        close.iloc[8]=np.nan
        forward=j.uninterrupted_return(close,forward=True)
        backward=j.uninterrupted_return(close)
        self.assertTrue(forward.iloc[3:9].isna().all())
        self.assertTrue(backward.iloc[8:14].isna().all())
        self.assertTrue(np.isfinite(forward.iloc[9]))
        self.assertTrue(np.isfinite(backward.iloc[14]))


if __name__=='__main__':unittest.main()
