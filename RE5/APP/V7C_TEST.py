import unittest,tempfile,sys
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
from ETH_V42_Collector import settlement_stop_assessment
import ETH_V42_Risk as risk
class V7CTests(unittest.TestCase):
 def test_mismatch_does_not_trigger(self):
  h={'close':1060.0,'partial':[(1000+i*2,81100.0) for i in range(20)]}
  a=settlement_stop_assessment(h,'no',80500,80430,20,Decimal('12'))
  self.assertEqual(a['decision'],'INVALID')
 def test_coherent_mean(self):
  h={'close':1060.,'partial':[(1000+i*2,80420+i*.3) for i in range(20)]}
  a=settlement_stop_assessment(h,'no',80500,80430,20,Decimal('12'))
  self.assertEqual(a['decision'],'HOLD')
  self.assertTrue(80420<=a['mean']<=80432)
 def test_weighted_mean_no_first_tick_double_count(self):
  h={'close':1060.,'partial':[(1000+i*2,80400.0+i*1.0) for i in range(20)]}
  a=settlement_stop_assessment(h,'no',80500,80420,20,Decimal('12'))
  self.assertAlmostEqual(a['mean'],80409.5,places=5)
 def test_bid_sweep(self):
  from ETH_V42_Collector import displayed_exit_bids
  class Dummy:
   def request(self,*args):return {'orderbook_fp':{'no_dollars':[['0.86','250.00'],['0.88','143.00'],['0.87','300.00']]}}
  self.assertEqual(displayed_exit_bids(Dummy(),'MKT','no',1011),[(Decimal('0.88'),143),(Decimal('0.87'),300),(Decimal('0.86'),250)])
 def test_partial_then_remainder(self):
  with tempfile.TemporaryDirectory() as tmp:
   with patch.object(risk,'DAILY_DB',Path(tmp)/'daily.sqlite3'):
    c=risk.connect(Path(tmp)/'test.sqlite3')
    risk.approve(c,'X','no','9.70',data_age_seconds=1,market_seconds_left=90,contracts=1000)
    a=risk.partial_exit(c,'X',143,'0.95','0.0','0')
    self.assertEqual(a['remaining'],857)
    self.assertEqual(risk.outstanding(c)[0][3],857)
    with self.assertRaises(risk.Rejected):risk.partial_exit(c,'X',858,'0.95','0')
    b=risk.partial_exit(c,'X',857,'0.95','0.0','0')
    self.assertEqual(b['remaining'],0)
    self.assertFalse(risk.outstanding(c))
    self.assertEqual(c.execute('SELECT COUNT(*) FROM partial_exits').fetchone()[0],2)
    self.assertEqual(risk.daily_status()[0],1)
    c.close()
 def test_partial_then_settlement(self):
  with tempfile.TemporaryDirectory() as tmp:
   with patch.object(risk,'DAILY_DB',Path(tmp)/'daily.sqlite3'):
    c=risk.connect(Path(tmp)/'test.sqlite3')
    risk.approve(c,'Y','yes','9.70',data_age_seconds=1,market_seconds_left=90,contracts=1000)
    risk.partial_exit(c,'Y',143,'0.95','0','0')
    cost=c.execute('SELECT remaining_cost_cents FROM intents WHERE ticker="Y"').fetchone()[0]
    risk.settle(c,'Y','yes',str((Decimal(857)-Decimal(cost)/100).quantize(Decimal('0.01'))))
    self.assertEqual(c.execute('SELECT status FROM intents').fetchone()[0],'SETTLED')
    self.assertFalse(risk.outstanding(c));c.close()
if __name__=='__main__':unittest.main()
