"""Selection evidence persistence tests."""
import os,sys,tempfile,unittest
from pathlib import Path
import pandas as pd
os.environ['QSYS_DATA_DIR']=tempfile.mkdtemp();sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import experience
class EvidenceTests(unittest.TestCase):
 def test_pick_keeps_layered_evidence(self):
  pid=experience.save_pick('test','p',1,'m',[],[{'name':'f','theory_family':'质量'}],pd.Series({'SZ000001':1}),trade_date='2026-09-23',data_source='test',theory_family='质量',regime_scope='bull',evidence_type='基本面,技术',risk_class='stable',decision_evidence={'market_state':'bull','sector':{'status':'advisory'},'financial':{'status':'advisory'},'technical':{'status':'applied'},'risk':{'status':'checked'}})
  with experience._conn() as c:
   row=c.execute('select market_state,sector_json,financial_json,technical_json,risk_json from pick_decision_evidence where pick_id=?',(pid,)).fetchone()
  self.assertEqual(row[0],'bull');self.assertIn('advisory',row[1]);self.assertIn('applied',row[3])
if __name__=='__main__':unittest.main()
