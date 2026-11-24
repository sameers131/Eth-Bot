from pathlib import Path
from decimal import Decimal
import tempfile
import ETH_V42_Collector as c
import ETH_V42_Risk as r
assert c.BUDGET == Decimal('10')
assert c.P_MIN == Decimal('0.90') and c.P_MAX == Decimal('0.99')
assert c.CHECKPOINTS == (180,150,120,105,90,60,45,30)
assert c.EARLY_CHECKPOINTS == ()
assert c.MIN_CUSHION_PCT == Decimal('0.03') and c.MIN_SCALED_GAP == Decimal('1.6')
assert c.scaled_gate_for_checkpoint(180) == Decimal('2.0')
assert c.scaled_gate_for_checkpoint(150) == Decimal('2.0')
assert all(c.scaled_gate_for_checkpoint(v) == Decimal('1.6') for v in (120,105,90,60,45,30))
assert r.MAX_PAPER_USD == Decimal('10')
assert r.MAX_OPEN_INTENTS == 1
assert r.MAX_DAILY_REALIZED_LOSS_RATIO == Decimal('0.30')
assert r.MAX_DAILY_STOP_OUTS == 2
assert c.STOP_TRIGGER == Decimal("0.88")
assert c.EXIT_SLIPPAGE_PER_CONTRACT == Decimal("0.001")
with tempfile.TemporaryDirectory() as t:
 db=r.connect(Path(t)/'paper.sqlite3')
 rec=r.approve(db,'TEST-BIGBET','yes','9.00',data_age_seconds=0,market_seconds_left=180,contracts=1010)
 assert rec['paper_cost_usd'] == 9.00
 for ticker,cost in [('TEST-OVER','10.01'),('TEST-BIGBET','9.00')]:
  try:r.approve(db,ticker,'yes',cost,data_age_seconds=0,market_seconds_left=60,contracts=1010)
  except r.Rejected:pass
  else:raise AssertionError('unsafe approval')
 r.settle(db,'TEST-BIGBET','yes','0.01')
 assert db.execute("select count(*) from intents where status='SETTLED'").fetchone()[0]==1
 db.close()
print('PASS: V7-D paper-only, 90-99c, 0.03pct, scaled 2.0 at 180/150s and 1.6 at 120/105/90/60/45/30s, persistent risk ledger')

# Confirm the historical losing NO setup is rejected at 180s but may qualify on scaled gate at 60s.
raw={'ticker':'TEST-LOSS','checkpoint_s':180,'time_utc':'2026-10-27T14:27:00Z','seconds_left':180,'side':'no','eth':'79930.80','strike':'80026.28','gap_abs':'95.48','gap_signed':'-95.48','sigma_min':'35','scaled_gap':'1.573','minute_pairs':'8','ask':'0.968','quoted_depth':'2000'}
assert 'SCALED_CUSHION_BELOW_CHECKPOINT_MINIMUM' in c.checkpoint_candidate(raw,180)['reason']
raw.update(side='yes',gap_signed='117.63',gap_abs='117.63',scaled_gap='3.307',ask='0.989',seconds_left=60)
assert 'SCALED_CUSHION_BELOW_CHECKPOINT_MINIMUM' not in c.checkpoint_candidate(raw,60)['reason']
print('PASS: checkpoint-dependent gate regression')
