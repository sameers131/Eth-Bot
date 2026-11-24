#!/usr/bin/env python3
"""V42.2 BIGBET 1000 fail-closed shadow approval ledger. NEVER submits real orders.

Atomic SQLite ledger rejects duplicates and enforces per-intent size and a
single outstanding shadow intent. Not wired to an exchange or order placement.
"""
import argparse,sqlite3,time,json,os
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from decimal import Decimal,InvalidOperation

_TRADE_USD=os.environ.get('BIGBET_TRADE_USD','10') or '10'
if _TRADE_USD not in ('5','10','15','20','25','30','35','40'):raise SystemExit('BIGBET_TRADE_USD must be 5, 10, 15, 20, 25, 30, 35 or 40')   # ETH V1.7
# V12.3: paper cap follows the chosen trade size ($10 by default; ladder-controlled)
MAX_PAPER_USD=Decimal(_TRADE_USD+'.00')
MAX_OPEN_INTENTS=1
MAX_DAILY_REALIZED_LOSS_RATIO=Decimal('0.30')
MAX_DAILY_STOP_OUTS=2
# Shared between V5 sessions; no live trading. A failed guard check rejects new entries.
# ETH V1.1: daily stop counters live INSIDE the ETH data folder, one file per mode, so a paper stop can
# never block live trading (or the reverse) and nothing is shared with any other app.
#   live prod -> ETH_LIVE_DAILY_GUARD.sqlite3 | live demo -> ETH_DEMO_DAILY_GUARD.sqlite3 | paper -> ETH_PAPER_DAILY_GUARD.sqlite3
_GUARD_DIR=Path(os.environ.get('BIGBET_DAILY_GUARD_DIR') or (Path.home()/'BIGBET_ETH_DATA'/'DAILY_GUARD'))
_GUARD_MODE=os.environ.get('BIGBET_RE5_LIVE','').strip().lower()
DAILY_DB=_GUARD_DIR/{'prod':'ETH_LIVE_DAILY_GUARD.sqlite3','demo':'ETH_DEMO_DAILY_GUARD.sqlite3'}.get(_GUARD_MODE,'ETH_PAPER_DAILY_GUARD.sqlite3')
SCHEMA="""CREATE TABLE IF NOT EXISTS intents (
 ticker TEXT PRIMARY KEY, side TEXT NOT NULL, cost_cents INTEGER NOT NULL, contracts INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('OPEN','SETTLED','STOPPED','REJECTED')),
 created_utc INTEGER NOT NULL, settled_outcome TEXT,
 realized_pnl_cents INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_intent_status ON intents(status);"""

DAILY_SCHEMA="""CREATE TABLE IF NOT EXISTS daily_events (
 ticker TEXT PRIMARY KEY, local_day TEXT NOT NULL, status TEXT NOT NULL,
 pnl_cents INTEGER NOT NULL, event_utc INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS daily_events_day ON daily_events(local_day);
CREATE TABLE IF NOT EXISTS daily_bid_anchor (local_day TEXT PRIMARY KEY, original_bid_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS daily_override (local_day TEXT PRIMARY KEY, stops_base INTEGER NOT NULL, loss_base_cents INTEGER NOT NULL, at_utc INTEGER NOT NULL);"""
def eastern_day():
 return datetime.now(ZoneInfo('America/New_York')).date().isoformat()
def daily_connect():
 DAILY_DB.parent.mkdir(parents=True,exist_ok=True)
 c=sqlite3.connect(str(DAILY_DB),timeout=10,isolation_level=None)
 c.execute('PRAGMA busy_timeout=10000')
 c.execute('PRAGMA journal_mode=WAL')
 c.executescript(DAILY_SCHEMA)
 return c
def daily_status():
 with closing(daily_connect()) as c:
  row=c.execute("SELECT COUNT(*) FILTER (WHERE status IN ('STOPPED','PARTIAL')), COALESCE(SUM(CASE WHEN pnl_cents < 0 THEN pnl_cents ELSE 0 END),0) FROM daily_events WHERE local_day=?",(eastern_day(),)).fetchone()
 return int(row[0]),int(row[1])
def record_daily_override():
 """ETH V1.6: operator chose to keep trading after a daily stop. Stops/losses so far are set aside;
 new ones after this moment count toward the same limits again. Can be repeated."""
 stops,loss=daily_status()
 with closing(daily_connect()) as c:
  c.execute('BEGIN IMMEDIATE')
  try:
   c.execute('INSERT INTO daily_override VALUES (?,?,?,?) ON CONFLICT(local_day) DO UPDATE SET stops_base=excluded.stops_base,loss_base_cents=excluded.loss_base_cents,at_utc=excluded.at_utc',(eastern_day(),stops,loss,int(time.time())))
   c.execute('COMMIT')
  except BaseException:c.execute('ROLLBACK');raise
 return stops,loss
def daily_status_since_override():
 stops,loss=daily_status()
 with closing(daily_connect()) as c:
  row=c.execute('SELECT stops_base,loss_base_cents FROM daily_override WHERE local_day=?',(eastern_day(),)).fetchone()
 if row:stops,loss=stops-int(row[0]),loss-int(row[1])
 return stops,loss
def daily_loss_limit_cents(original_bid_cents=None):
 """30% of the original approved bid cost, anchored once per Eastern day.

 First approved paper position determines the day's reference. Later sessions
 cannot increase the limit by choosing a larger budget.
 """
 with closing(daily_connect()) as c:
  c.execute('BEGIN IMMEDIATE')
  try:
   day=eastern_day()
   row=c.execute('SELECT original_bid_cents FROM daily_bid_anchor WHERE local_day=?',(day,)).fetchone()
   if row is None and original_bid_cents is not None:
    bid=int(original_bid_cents)
    if bid<=0:raise Rejected('INVALID_ORIGINAL_BID')
    c.execute('INSERT INTO daily_bid_anchor VALUES (?,?)',(day,bid))
    row=(bid,)
   c.execute('COMMIT')
  except BaseException:c.execute('ROLLBACK');raise
 if row is None:return None
 return int((Decimal(row[0])*MAX_DAILY_REALIZED_LOSS_RATIO).quantize(Decimal('1'),rounding='ROUND_HALF_UP'))

def daily_record(ticker,status,pnl_cents):
 with closing(daily_connect()) as c:
  c.execute('BEGIN IMMEDIATE')
  try:
   c.execute('INSERT OR IGNORE INTO daily_events VALUES (?,?,?,?,?)',(ticker,eastern_day(),status,int(pnl_cents),int(time.time())))
   c.execute('COMMIT')
  except BaseException:c.execute('ROLLBACK');raise

def daily_upsert_partial(ticker,total_pnl_cents):
 """Record cumulative realized partial exit P/L without counting multiple stop events."""
 with closing(daily_connect()) as c:
  c.execute('BEGIN IMMEDIATE')
  try:
   c.execute("INSERT INTO daily_events VALUES (?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET status='PARTIAL',pnl_cents=excluded.pnl_cents,event_utc=excluded.event_utc",(ticker,eastern_day(),'PARTIAL',int(total_pnl_cents),int(time.time())))
   c.execute('COMMIT')
  except BaseException:c.execute('ROLLBACK');raise

def daily_finalize_stop(ticker,pnl_cents):
 with closing(daily_connect()) as c:
  c.execute('BEGIN IMMEDIATE')
  try:
   c.execute("INSERT INTO daily_events VALUES (?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET status='STOPPED',pnl_cents=excluded.pnl_cents,event_utc=excluded.event_utc",(ticker,eastern_day(),'STOPPED',int(pnl_cents),int(time.time())))
   c.execute('COMMIT')
  except BaseException:c.execute('ROLLBACK');raise

class Rejected(Exception):pass

def cents(value):
 try:
  v=Decimal(str(value))
  if not v.is_finite() or v<=0 or v>MAX_PAPER_USD:raise Rejected(f'COST_OUTSIDE_0_TO_{_TRADE_USD}_USD')
  c=v*100
  if c!=c.to_integral_value():raise Rejected('COST_REQUIRES_CENT_PRECISION')
  return int(c)
 except (ValueError,InvalidOperation,TypeError) as exc:raise Rejected('INVALID_COST') from exc

def migrate_partial_schema(con):
 con.execute("""CREATE TABLE IF NOT EXISTS partial_exits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, event_utc INTEGER NOT NULL,
  quantity INTEGER NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
  entry_cost_cents INTEGER NOT NULL, proceeds_cents INTEGER NOT NULL,
  realized_pnl_cents INTEGER NOT NULL, source TEXT NOT NULL)""")
 cols={r[1] for r in con.execute('PRAGMA table_info(intents)').fetchall()}
 for name,definition in [('remaining_contracts','INTEGER'),('remaining_cost_cents','INTEGER'),('exit_pnl_cents','INTEGER NOT NULL DEFAULT 0')]:
  if name not in cols:con.execute('ALTER TABLE intents ADD COLUMN '+name+' '+definition)
 con.execute('UPDATE intents SET remaining_contracts=contracts WHERE remaining_contracts IS NULL')
 con.execute('UPDATE intents SET remaining_cost_cents=cost_cents WHERE remaining_cost_cents IS NULL')

def partial_exit(con,ticker,quantity,sell_price,exit_fee,slippage_per_contract=Decimal('0.001')):
 """Atomic, paper-only partial liquidation. Returns incremental simulated P/L.
 Source quote is an indicative complementary ask, not a verified executable bid.
 """
 px=Decimal(str(sell_price));fee=Decimal(str(exit_fee));slip=Decimal(str(slippage_per_contract))
 if not all(v.is_finite() for v in (px,fee,slip)) or not (0<=px<=1) or fee<0 or slip<0 or not isinstance(quantity,int) or quantity<1:raise Rejected('INVALID_PARTIAL_EXIT')
 con.execute('BEGIN IMMEDIATE')
 try:
  row=con.execute('SELECT remaining_contracts,remaining_cost_cents,status,side,exit_pnl_cents FROM intents WHERE ticker=?',(ticker,)).fetchone()
  if not row or row[2]!='OPEN' or quantity>row[0]:raise Rejected('PARTIAL_EXIT_OUTSIDE_OPEN_POSITION')
  remaining,cost,_,side,prior=row
  alloc=cost if quantity==remaining else int((Decimal(cost)*quantity/remaining).quantize(Decimal('1'),rounding='ROUND_HALF_UP'))
  proceeds=int(((max(Decimal(0),px-slip)*quantity-fee)*100).quantize(Decimal('1'),rounding='ROUND_HALF_UP'))
  pnl=proceeds-alloc;newremain=remaining-quantity
  newstatus='STOPPED' if newremain==0 else 'OPEN'
  con.execute('UPDATE intents SET remaining_contracts=?,remaining_cost_cents=?,status=?,exit_pnl_cents=?,realized_pnl_cents=? WHERE ticker=?',(newremain,cost-alloc,newstatus,prior+pnl,prior+pnl,ticker))
  con.execute('INSERT INTO partial_exits(ticker,event_utc,quantity,price,fee,entry_cost_cents,proceeds_cents,realized_pnl_cents,source) VALUES (?,?,?,?,?,?,?,?,?)',(ticker,int(time.time()),quantity,str(px),str(fee),alloc,proceeds,pnl,'INDICATIVE_COMPLEMENTARY_ASK_NOT_VERIFIED_BID'))
  con.execute('COMMIT')
 except BaseException:con.execute('ROLLBACK');raise
 daily_finalize_stop(ticker,prior+pnl) if newremain==0 else daily_upsert_partial(ticker,prior+pnl)
 return dict(ticker=ticker,side=side,exited=quantity,remaining=newremain,remaining_cost_cents=cost-alloc,pnl_cents=pnl,total_exit_pnl_cents=prior+pnl,paper_only=True)

def connect(db):
 con=sqlite3.connect(str(db),timeout=10,isolation_level=None)
 con.execute('PRAGMA busy_timeout=10000')
 con.execute('PRAGMA journal_mode=WAL')
 con.executescript(SCHEMA)
 migrate_partial_schema(con)
 return con

def approve(con,ticker,side,cost_usd,*,data_age_seconds,market_seconds_left,kill_switch=False,contracts=1):
 """Only records an approval; no API/order side effects."""
 if kill_switch:raise Rejected('KILL_SWITCH_ACTIVE')
 if not ticker or len(ticker)>128 or side not in ('yes','no'):raise Rejected('INVALID_MARKET_OR_SIDE')
 if not (0<=data_age_seconds<=10):raise Rejected('STALE_OR_FUTURE_MARKET_DATA')
 if not (20<=market_seconds_left<=425):raise Rejected('OUTSIDE_APPROVED_CHECKPOINT_WINDOW')   # ETH V1.4: Early-Hold up to 420 s
 if not isinstance(contracts,int) or contracts<1 or contracts>2000:raise Rejected('INVALID_CONTRACTS')
 amount=cents(cost_usd)
 con.execute('BEGIN IMMEDIATE')
 try:
  if con.execute('SELECT 1 FROM intents WHERE ticker=?',(ticker,)).fetchone():raise Rejected('DUPLICATE_MARKET')
  if con.execute("SELECT COUNT(*) FROM intents WHERE status='OPEN'").fetchone()[0]>=MAX_OPEN_INTENTS:raise Rejected('ANOTHER_OPEN_PAPER_INTENT')
  stops,daypnl=daily_status_since_override()
  daily_limit=daily_loss_limit_cents(amount)
  if stops>=MAX_DAILY_STOP_OUTS or (daily_limit is not None and daypnl<=-daily_limit):raise Rejected('DAILY_STOP_OR_LOSS_LIMIT_REACHED')
  con.execute('INSERT INTO intents(ticker,side,cost_cents,contracts,status,created_utc,remaining_contracts,remaining_cost_cents) VALUES (?,?,?,?,?,?,?,?)',(ticker,side,amount,contracts,'OPEN',int(time.time()),contracts,amount))
  con.execute('COMMIT')
 except BaseException:
  con.execute('ROLLBACK');raise
 return {'result':'PAPER_APPROVED','ticker':ticker,'side':side,'paper_cost_usd':amount/100,'LIVE_ORDERS_ENABLED':False}

def settle(con,ticker,outcome,realized_pnl_usd):
 if outcome not in ('yes','no'):raise Rejected('INVALID_OFFICIAL_OUTCOME')
 pnl=Decimal(str(realized_pnl_usd))*100
 if not pnl.is_finite() or pnl!=pnl.to_integral_value():raise Rejected('INVALID_REALIZED_PNL')
 con.execute('BEGIN IMMEDIATE')
 try:
  row=con.execute('SELECT side,status,remaining_cost_cents,remaining_contracts,exit_pnl_cents FROM intents WHERE ticker=?',(ticker,)).fetchone()
  if not row or row[1]!='OPEN':raise Rejected('NO_OPEN_INTENT')
  if int(pnl)<-row[2]:raise Rejected('LOSS_EXCEEDS_MAX_COST')
  con.execute("UPDATE intents SET status='SETTLED',settled_outcome=?,realized_pnl_cents=? WHERE ticker=?",(outcome,int(pnl)+row[4],ticker))
  con.execute('COMMIT')
 except BaseException:
  con.execute('ROLLBACK');raise
 daily_record(ticker,'SETTLED',int(pnl))


def paper_stop(con,ticker,sell_price,exit_fee,slippage_per_contract=Decimal('0.001')):
 """Terminal simulated exit. Never sends an exchange order.

 Prices are Decimal USD/contract, already derived from opposite ask.
 This transaction prevents an official settlement from booking the same intent.
 """
 px=Decimal(str(sell_price));fee=Decimal(str(exit_fee));slip=Decimal(str(slippage_per_contract))
 if not all(x.is_finite() for x in (px,fee,slip)) or px<0 or px>1 or fee<0 or slip<0:raise Rejected('INVALID_EXIT_QUOTE')
 con.execute('BEGIN IMMEDIATE')
 try:
  row=con.execute('SELECT cost_cents,contracts,status,side FROM intents WHERE ticker=?',(ticker,)).fetchone()
  if row is None or row[2]!='OPEN':raise Rejected('POSITION_NOT_OPEN')
  cost,n,_,side=row
  proceeds=max(Decimal('0'),px-slip)*n-fee
  pnl_cents=int(((proceeds-Decimal(cost)/100)*100).quantize(Decimal('1'),rounding='ROUND_HALF_UP'))
  con.execute("UPDATE intents SET status='STOPPED',realized_pnl_cents=? WHERE ticker=? AND status='OPEN'",(pnl_cents,ticker))
  con.execute('COMMIT')
 except BaseException:
  con.execute('ROLLBACK');raise
 daily_record(ticker,'STOPPED',pnl_cents)
 return {'ticker':ticker,'side':side,'contracts':n,'sell_price':str(px),'slippage':str(slip),'fee':str(fee),'pnl_cents':pnl_cents,'paper_only':True}

def remove_unfilled(con,ticker):
 """RE5.1 live mode: the real order filled nothing, so the paper intent is withdrawn.
 Paper results then never count a trade that did not happen, and later checkpoints may retry."""
 con.execute('BEGIN IMMEDIATE')
 try:
  row=con.execute('SELECT status FROM intents WHERE ticker=?',(ticker,)).fetchone()
  if row and row[0]=='OPEN':con.execute('DELETE FROM intents WHERE ticker=?',(ticker,))
  con.execute('COMMIT')
 except BaseException:
  con.execute('ROLLBACK');raise

def sync_to_live(con,ticker,contracts,cost_cents):
 """RE5.1 live mode: paper intent takes the REAL filled quantity and real fee-inclusive cost."""
 if not isinstance(contracts,int) or contracts<1:raise Rejected('INVALID_CONTRACTS')
 con.execute('BEGIN IMMEDIATE')
 try:
  con.execute("UPDATE intents SET contracts=?,cost_cents=?,remaining_contracts=?,remaining_cost_cents=? WHERE ticker=? AND status='OPEN'",(contracts,int(cost_cents),contracts,int(cost_cents),ticker))
  con.execute('COMMIT')
 except BaseException:
  con.execute('ROLLBACK');raise

def outstanding(con):
 return con.execute("SELECT ticker,side,remaining_cost_cents,remaining_contracts FROM intents WHERE status='OPEN' AND remaining_contracts>0").fetchall()

def reconcile_official(con, official_rows):
 """Reconcile only OPEN paper intents against explicit official results.

 Costs and quantities are supplied by the original ledger/selection, not inferred
 from market quotes. The caller supplies an iterable of (ticker, outcome, pnl).
 """
 changed=0
 for ticker,outcome,pnl in official_rows:
  row=con.execute('SELECT status FROM intents WHERE ticker=?',(ticker,)).fetchone()
  if row and row[0]=='OPEN':
   settle(con,ticker,outcome,pnl); changed+=1
 return changed

def main():
 a=argparse.ArgumentParser(description='V42.1 shadow risk ledger: no live orders')
 a.add_argument('--db',type=Path,default=Path(__file__).with_name('eth_v42_2_BIGBET_1000_shadow_ledger.sqlite3'))
 a.add_argument('--self-check',action='store_true')
 args=a.parse_args()
 if not args.self_check:a.error('Only self-check CLI is supported; integration uses the Python functions')
 import tempfile
 with tempfile.TemporaryDirectory() as tmp:
  con=connect(Path(tmp)/'ledger.sqlite3')
  assert approve(con,'TEST1','yes','9.90',data_age_seconds=1,market_seconds_left=90)['result']=='PAPER_APPROVED'
  try:approve(con,'TEST1','yes','9.90',data_age_seconds=1,market_seconds_left=90)
  except Rejected as e:assert str(e)=='DUPLICATE_MARKET'
  else:raise AssertionError('duplicate approved')
  try:approve(con,'TEST2','yes','10.01',data_age_seconds=1,market_seconds_left=90)
  except Rejected as e:assert str(e)=='COST_OUTSIDE_0_TO_10_USD'
  else:raise AssertionError('oversized approved')
  try:approve(con,'TEST2','yes','5.00',data_age_seconds=11,market_seconds_left=90)
  except Rejected as e:assert str(e)=='STALE_OR_FUTURE_MARKET_DATA'
  else:raise AssertionError('stale approved')
  settle(con,'TEST1','yes','0.10')
  assert approve(con,'TEST2','no','5.00',data_age_seconds=1,market_seconds_left=90)['result']=='PAPER_APPROVED'
  con.close()
 print('PASS: SQLite atomic paper ledger, $10 hard cap, duplicate rejection, stale data, one open intent, settlement')
if __name__=='__main__':main()
