"""V7-D passive order-book telemetry. NO order submission or ledger mutations.

Bid books are displayed indications, NOT guaranteed executable liquidity.
Buy-side asks are inferred from the opposite side's bids (ask = 1 - opposite bid).
"""
import csv,queue,threading,time
from datetime import datetime,timezone
from decimal import Decimal as D, ROUND_CEILING
from pathlib import Path

CSV_NAME='eth_v7d_orderbook_liquidity.csv'
FIELDS=['favored_side','simulation_type','target_contracts','filled_contracts','gross_proceeds_usd','estimated_exit_fee_usd','net_proceeds_usd','fee_basis','observed_utc','ticker','seconds_left','held_side','open_contracts','open_cost_usd','snapshot_reason','book_side','level_type','price_dollars','displayed_contracts','cumulative_contracts','estimated_proceeds_usd','remaining_contracts','eth_price','strike_price','book_status','note']

class PassiveObserver:
 def __init__(self, root, client_factory):
  self.path=Path(root)/CSV_NAME
  self.client_factory=client_factory
  self.q=queue.Queue(maxsize=4)
  self.last_enqueue={}
  self.errors=0
  self.thread=threading.Thread(target=self._worker,daemon=True,name='V7D_PASSIVE_BOOK_OBSERVER')
  self.thread.start()
 def submit(self,ticker,seconds_left,open_positions,eth,strike,*,checkpoint=False):
  # Never hold up trade decisions; telemetry can be dropped if overloaded.
  if seconds_left > 180 or seconds_left < 0:return
  now=time.monotonic()
  key=ticker
  if not checkpoint and now-self.last_enqueue.get(key,0)<15:return
  self.last_enqueue[key]=now
  packet=(ticker,float(seconds_left),list(open_positions),str(eth),str(strike),'checkpoint' if checkpoint else 'periodic')
  try:self.q.put_nowait(packet)
  except queue.Full:self.errors+=1
 def _worker(self):
  client=None
  while True:
   packet=self.q.get()
   try:
    if client is None:client=self.client_factory()
    self._capture(client,*packet)
   except Exception as ex:
    client=None
    self.errors+=1
    self._write([dict(observed_utc=datetime.now(timezone.utc).isoformat(),ticker=packet[0],seconds_left=round(packet[1],2),book_status='ERROR',note=f'{type(ex).__name__}: {str(ex)[:130]}')])
   finally:self.q.task_done()
 def _write(self,rows):
  if not rows:return
  with self.path.open('a',newline='',encoding='utf8') as f:
   w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore')
   if f.tell()==0:w.writeheader()
   w.writerows(rows)
 def _capture(self,client,ticker,rem,positions,eth,strike,reason):
  # Observer-only: this method must never mutate the trading ledger or submit orders.
  ts=datetime.now(timezone.utc).isoformat(timespec='milliseconds')
  favorite='yes' if D(eth)>D(strike) else ('no' if D(eth)<D(strike) else 'tie')
  rows=[]
  def row(side,kind,p=0,q=0,cum=0,remaining=0,**extras):
   r=dict(observed_utc=ts,ticker=ticker,seconds_left=round(rem,2),held_side=side,
          favored_side=favorite,open_contracts=extras.pop('open_contracts',0),
          open_cost_usd=extras.pop('open_cost_usd',''),snapshot_reason=reason,
          book_side=side,level_type=kind,price_dollars=str(p),displayed_contracts=q,
          cumulative_contracts=cum,remaining_contracts=remaining,
          eth_price=eth,strike_price=strike,book_status='OBSERVED',**extras)
   rows.append(r)
  try:
   response=client.request('GET','/markets/'+ticker+'/orderbook')
   raw=response.get('orderbook_fp',{})
   if not isinstance(raw,dict):raise ValueError('orderbook_fp unavailable')
   parsed={}
   for side in ('yes','no'):
    levels=raw.get(side+'_dollars')
    if not isinstance(levels,list):raise ValueError('missing '+side+' bid levels')
    parsed[side]=sorted([(D(str(price)),int(D(str(qty)))) for price,qty in levels
                 if D('0')<D(str(price))<D('1') and int(D(str(qty)))>0],reverse=True)
   if favorite == 'tie':
    row('', 'NO_FAVORED_SIDE',book_status='TIE',note='ETH equals strike; no directional side')
   else:
    opposite='no' if favorite=='yes' else 'yes'
    # All displayed indicative asks, cheapest to 99.99c, with no fixed lower bound.
    asks=sorted([(D('1')-price,qty) for price,qty in parsed[opposite] if D('1')-price<D('1')])
    cum=0; cost=D(0)
    for price,qty in asks:
     cum+=qty;cost+=price*qty
     row(favorite,'FAVORED_ENTRY_ASK_LEVEL',price,qty,cum,
         estimated_proceeds_usd=str(cost),note='Indicative ask inferred from opposite bid; not guaranteed fill')
    row(favorite,'FAVORED_ENTRY_ASK_SUMMARY',0,cum,cum,
        estimated_proceeds_usd=str(cost),note='Full visible ask ladder through 99.99c; not a sizing decision')
    # Research-only counterfactual for ALL markets, not only held positions.
    positions_here=[pos for pos in positions if pos[0]==ticker]
    simulations=[('HYPOTHETICAL_1000',1000,'')]
    simulations += [('ACTUAL_OPEN',int(n),str(D(cost_cents)/100)) for _,side,cost_cents,n in positions_here if side==favorite and int(n)>0]
    # If actually held on now-unfavored side, log its liquidation separately.
    for _,side,cost_cents,n in positions_here:
     if side!=favorite and int(n)>0:
      simulations.append(('ACTUAL_OPEN_UNFAVORED',int(n),str(D(cost_cents)/100),side))
    for sim in simulations:
     tag,n,cost=sim[:3]
     side=sim[3] if len(sim)==4 else favorite
     remaining=n;gross=D(0);filled=0
     for rank,(price,available) in enumerate(parsed[side],1):
      if remaining==0:break
      take=min(remaining,available)
      filled+=take;remaining-=take;gross+=price*take
      row(side,'EXIT_BID_LEVEL',price,available,filled,remaining,
          simulation_type=tag,target_contracts=n,filled_contracts=take,
          open_contracts=n if tag.startswith('ACTUAL') else 0,open_cost_usd=cost,
          note=f'Level {rank}; hypothetical book sweep, no execution')
     # Standard default fee model used by the existing trading engine: 7% * n * p*(1-p),
     # ceiling to $0.01. With mixed prices, sum per-level raw fee before rounding.
     fee_raw=D(0);left=n
     for price,available in parsed[side]:
      take=min(left,available)
      if take<=0:break
      fee_raw+=D('0.07')*D(take)*price*(D('1')-price)
      left-=take
     fee=fee_raw.quantize(D('0.01'),rounding=ROUND_CEILING)
     net=gross-fee
     row(side,'EXIT_FULL_DEPTH_SUMMARY',0,filled,filled,remaining,
         simulation_type=tag,target_contracts=n,filled_contracts=filled,
         gross_proceeds_usd=str(gross),estimated_exit_fee_usd=str(fee),
         net_proceeds_usd=str(net),estimated_proceeds_usd=str(net),
         fee_basis='0.07*n*p*(1-p), ceiling $0.01; estimate; verify current market fee schedule',
         open_contracts=n if tag.startswith('ACTUAL') else 0,open_cost_usd=cost,
         note='PARTIAL' if remaining else 'DEPTH_SUFFICIENT; displayed not executable')
  except Exception as ex:
   rows.append(dict(observed_utc=ts,ticker=ticker,seconds_left=round(rem,2),favored_side=favorite,
                    book_status='ERROR',level_type='BOOK_UNAVAILABLE',note=f'{type(ex).__name__}: {str(ex)[:150]}'))
   self.errors+=1
  self._write(rows)
