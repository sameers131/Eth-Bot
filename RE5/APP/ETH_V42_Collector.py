#!/usr/bin/env python3
"""ETH Kalshi Market Microstructure Logger v41 — PAPER/SHADOW ONLY.

Collect simultaneous ETHRTI and priced YES/NO quotes across each full 15-minute market.
Model *market pricing*, not a validated probability of winning.
Evaluate 120/90/60/45/30s checkpoint alternatives using a $1000 capped shadow fill,
then reconcile with explicit official market results. Never submits orders.
Requires unchanged kalshi_eth_bot_v37.py in the same directory.
V41: reject checkpoint paper entries with ETH-side strike cushion below 0.03%
or volatility adjusted cushion below 1.6 (2.0 at 180/150s). Report one selected entry per market.
"""
import argparse
import csv
import importlib.util
import math
import os
import statistics
import time
from ETH_V42_Risk import remove_unfilled as risk_remove_unfilled, sync_to_live as risk_sync_to_live
from ETH_V42_Risk import connect as risk_connect, approve as risk_approve, reconcile_official as risk_reconcile, outstanding as risk_outstanding, partial_exit as risk_partial_exit, daily_status as risk_daily_status, Rejected as RiskRejected
from collections import Counter
from V7D_PassiveBook import PassiveObserver
from V12_MarketLog import MarketLog, heartbeat
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

ROOT=Path(__file__).resolve().parent
BASE=ROOT/'kalshi_eth_bot_v37.py'
if not BASE.is_file():
    raise SystemExit('Missing kalshi_eth_bot_v37.py in same folder; no trading performed')
spec=importlib.util.spec_from_file_location('v37_eth_readonly',BASE)
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
D=base.D
ASSET=base.ASSET   # ETH V1: series / index / dollar-scaled constants (RE5/asset.py)
# Operator-specified hard cap: never allow a higher value via environment.
_TRADE_USD=os.environ.get('BIGBET_TRADE_USD','10') or '10'
if _TRADE_USD not in ('5','10','15','20','25','30','35','40'):raise SystemExit('BIGBET_TRADE_USD must be 5, 10, 15, 20, 25, 30, 35 or 40')   # ETH V1.7
# V12.3: the ladder-controlled trade size; $10 unless the operator unlocked a bigger step
BUDGET=D(_TRADE_USD)
# ETH V1.4: check more often while a trade is open near the close.
FAST_POLL_S=0.5
NORMAL_POLL_S=2
def poll_pause(holding,rem):
    return FAST_POLL_S if holding and 0<rem<=180 else NORMAL_POLL_S
P_MIN=D('0.90'); P_MAX=D('0.99')   # ETH V1.3 Rule B: entries 90-99c (was 95-99c)
# Default 0.03 percent = 0.0003 fraction = 3 basis points.
# Change only by explicit operator configuration, e.g. 0.04 percent after review.
MIN_CUSHION_PCT=D(os.environ.get('ETH_V42_MIN_CUSHION_PCT','0.03'))
if not D('0') < MIN_CUSHION_PCT < D('1'):
    raise SystemExit('ETH_V42_MIN_CUSHION_PCT must be expressed as percent (e.g. 0.03 or 0.04)')
MIN_SCALED_GAP=D('1.6')   # ETH V1.3 Rule B: 1.6x at 120/105/90/60/45/30s (was 1.4)
CHECKPOINTS=(180,150,120,105,90,60,45,30)
EARLY_MIN_SCALED_GAP=D('2.0')   # ETH V1.3 Rule B: 2.0x at 180/150s (was 1.8)
EARLY_STRICT_CHECKPOINTS=(180,150)
# ETH V1.4 Rule C = Rule B + EARLY-HOLD: an extra entry chance between 7:00 and 4:00 left, only in a steady market.
EARLY_HOLD_CP=420            # label used for Early-Hold rows (checkpoint_s=420)
EARLY_HOLD_FROM=420          # seconds left: window opens
EARLY_HOLD_TO=240            # seconds left: window closes
# ETH V1.9.2 (option B, chosen Nov 14): Early-Hold now needs a lead of 2.0x (was 1.5x).
# Nov 14 7:15 PM: Early-Hold entered at lead 1.81x while ETH trended up; stop sold at 47c (-$5.66 at $10).
# Simulation on 420 ETH markets: real +$35.01 -> +$24.88 (fewer early trades); stress test losing trades 9 -> 5.
EARLY_HOLD_MIN_SCALED_GAP=D('2.0')
EARLY_HOLD_BID=D('0.90')     # favored side's bid must have stayed >= 90c ...
EARLY_HOLD_SECONDS=120       # ... for the whole previous 2 minutes
# ETH V1.9.4 (option B, chosen Nov 18): rules depend on the PREVIOUS market's mood (see ETH_V194_Regime.py).
#   CALM  -> Early-Hold lead >= 1.5x, steady filter 85c      HIGH -> no new entries      UNKNOWN -> V1.9.2 rules
CALM_EARLY_HOLD_MIN_SCALED_GAP=D('1.5')
CALM_STEADY_ASK=D('0.85')
def regime_rules(regime):
    """(trade_allowed, early_hold_lead, steady_ask) for 'CALM' / 'HIGH' / 'UNKNOWN'."""
    if regime=='HIGH':return False,EARLY_HOLD_MIN_SCALED_GAP,STEADY_ASK
    if regime=='CALM':return True,CALM_EARLY_HOLD_MIN_SCALED_GAP,CALM_STEADY_ASK
    return True,EARLY_HOLD_MIN_SCALED_GAP,STEADY_ASK
def scaled_gate_for_checkpoint(checkpoint,early_hold_gap=None):
    if int(checkpoint)==EARLY_HOLD_CP:return EARLY_HOLD_MIN_SCALED_GAP if early_hold_gap is None else early_hold_gap
    return EARLY_MIN_SCALED_GAP if int(checkpoint) in EARLY_STRICT_CHECKPOINTS else MIN_SCALED_GAP
def early_hold_ok(samples,now):
    """samples: [(time, bid)] for ONE side of ONE market. True only when we watched the full previous
    2 minutes and every bid in that window was >= 90c."""
    if not samples or samples[0][0]>now-EARLY_HOLD_SECONDS+5:return False      # not watched long enough
    window=[b for t,b in samples if t>=now-EARLY_HOLD_SECONDS]
    return len(window)>=5 and all(b is not None and b>=EARLY_HOLD_BID for b in window)
# ETH V1.9 "steady 60 s" entry filter (simulation on 364 ETH markets: losing trades 3 -> 1, profit -22%).
# A normal checkpoint entry (180 s ... 30 s) is allowed only if OUR side's ask stayed >= 90c for the previous 60 s.
# Early-Hold entries already need >= 90c for 2 minutes, so they are unchanged.
STEADY_ASK=D('0.90')
STEADY_SECONDS=60
def steady_ok(samples,now,min_ask=None):
    """samples: [(time, ask)] for ONE side of ONE market. True only if at least one ask was seen in the previous
    60 s and every ask seen in that window was >= 90c. A missing ask in the window counts as NOT steady."""
    window=[a for t,a in samples if now-STEADY_SECONDS<=t<now]
    need=STEADY_ASK if min_ask is None else min_ask
    return bool(window) and all(a is not None and a>=need for a in window)
RAW=ROOT/'eth_v42_market_quotes.csv'
CAND=ROOT/'eth_v42_shadow_candidates.csv'
EARLY=ROOT/'eth_v42_early_shadow_120_105.csv'
EARLY_FIELDS=['ticker','checkpoint_s','time_utc','side','eth','strike','ask','quote_depth','minute_pairs','readiness','status','reason','hypothetical_contracts','hypothetical_cost','quote_age_seconds','fill_verified']
DISAPPEAR=ROOT/'eth_v42_quote_disappearance.csv'
DISAPPEAR_FIELDS=['ticker','side','early_time_utc','early_remaining_s','early_ask','early_depth','late_time_utc','late_remaining_s','late_ask','late_depth','disappeared','potentially_affordable','hypothetical_fee','fill_verified','note']
HISTORY_AUDIT=ROOT/'eth_v42_history_readiness.csv'
HISTORY_FIELDS=['time_utc','ticker','readiness','minute_pairs','source','latest_history_utc','age_seconds']
PRELOAD=ROOT/'eth_v42_preload_history.csv'
PRELOAD_FIELDS=['time_utc','eth','source']
EARLY_CHECKPOINTS=()  # all these checkpoints are now active

SUMMARY=ROOT/'eth_v42_market_outcomes.csv'
RAW_FIELDS=['time_utc','ticker','seconds_left','side','eth','strike','gap_abs','gap_signed','sigma_min','scaled_gap','minute_pairs','momentum_30s','momentum_60s','ask','quoted_depth','quote_source','observed_partial_lastminute_mean','partial_observation_count','bid','spread']
CAND_FIELDS=['exit_time_utc','exit_sell_price','exit_fee','exit_slippage','exit_pnl','ticker','checkpoint_s','time_utc','actual_seconds_left','side','eth','strike','gap_abs','sigma_min','scaled_gap','cushion_pct','minimum_cushion_pct','ask','quote_depth','contracts','estimated_fee','total_cost','profit_if_win','loss_if_lose','reason','status','settlement_side','estimated_pnl']
ERRORS=ROOT/'eth_v42_errors.csv'
ERROR_FIELDS=['time_utc','stage','ticker','error_type','message']
SETTLE_ALL=ROOT/'eth_v42_all_market_settlements.csv'
SETTLE_FIELDS=['ticker','close_utc','official_side','official_status','last_checked_utc','note']
RISK_DB=ROOT/'eth_v7c_paper_ledger.sqlite3'
STOP_LOG=ROOT/'eth_v7c_paper_stop_exits.csv'
STOP_FIELDS=['time_utc','ticker','side','seconds_left','opposite_ask','implied_sell_price','modeled_sell_price_after_slippage','contracts','entry_total_cost','exit_fee','modeled_pnl','status','execution_verified','note','opposite_ask_depth','attempt','trigger_sell_price','retry_reason','remaining_contracts','quote_is_executable_bid']
STOP_TRIGGER=D('0.88')
ETH_STOP_MULTIPLE=D('0.5')        # V1.7 rule, kept ONLY for side-by-side comparison logging
# ETH V1.8 stop (simulation on 337 recorded ETH markets, Nov 5-12: the V1.7 stop fired 9 times, 7 on trades that settled as winners)
ETH_CROSS_HOLD_S=10               # sell only if ETH stays on the WRONG side of the target for 10 s in a row
ETH_EMERGENCY_PRICE=D('0.50')     # ...or, when the ETH price feed is stale/missing, if our side's price is 50c or less
_WRONG_SINCE={}                   # (ticker, side) -> first time ETH was seen on the wrong side
_OLD_RULE_LOGGED=set()
STOP_COMPARE=ROOT/'eth_v18_stop_compare.csv'
COMPARE_FIELDS=['time_utc','ticker','side','seconds_left','old_v17_reason','directional_multiple','held_price','note']
ETH_MAX_AGE_SECONDS=10
MOMENTUM_RESEARCH_USD=D(ASSET.P['momentum_research_usd'])   # research log only; ETH 15, ETH 0.5
EXIT_SLIPPAGE_PER_CONTRACT=D('0.001')
COMPARISON=ROOT/'eth_v42_policy_comparison.csv'
COMPARISON_FIELDS=['ticker','official_side','v41_entry_checkpoint','v41_side','v41_status','v41_net_pnl','baseline_entry_checkpoint','baseline_side','baseline_status','baseline_net_pnl','baseline_only_rejection_gates']
OUT_FIELDS=['ticker','settlement_side','last_checked_utc','checkpoints_considered','candidates_with_quote','shadow_positions','settled_positions','shadow_pnl']

def nowutc():return datetime.now(timezone.utc).isoformat(timespec='seconds')

def init_csv(path,fields):
    if not path.exists() or path.stat().st_size==0:
        with path.open('w',newline='',encoding='utf-8') as f:csv.DictWriter(f,fieldnames=fields).writeheader()

def add(path,fields,row):
    with path.open('a',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writerow(row)

def read(path):
    with path.open(newline='',encoding='utf-8') as f:return list(csv.DictReader(f))

def save(path,fields,rows):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
        f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def size_paper(ask,depth):
    if ask is None or depth is None or depth<1 or not (P_MIN<=ask<=P_MAX):return 0,D(0)
    n=min(int(depth),int((BUDGET/ask).to_integral_value(rounding=ROUND_FLOOR)))
    while n>0:
        fee=base.estimate_fee(ask,n)
        if ask*n+fee<=BUDGET:return n,fee
        n-=1
    return 0,D(0)

def load_verified_history(now=None):
    """Load recent, ordered ETHRTI history from a preseeded file or prior session records.

    No interpolation: require real ETHRTI samples with an honest time axis. The
    launcher can preseed using a previous session CSV; all inputs remain auditable.
    """
    now=time.time() if now is None else now
    candidates=[PRELOAD, ROOT/'kalshi_ethrti_ticks.csv']
    samples=[]
    for file in candidates:
        if not file.is_file():continue
        try:
            with file.open(newline='',encoding='utf-8-sig') as fh:
                for row in csv.DictReader(fh):
                    try:
                        stamp=row.get('time_utc') or row.get('ethrti_utc')
                        ts=datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
                        price=D(row['eth'])
                        if now-1200<=ts<=now+1 and 1000<price<1000000:
                            samples.append((ts,price,str(file.name)))
                    except (ValueError,TypeError,KeyError,ArithmeticError,AttributeError):continue
        except (OSError,UnicodeError):continue
    samples.sort(key=lambda x:x[0])
    dedup=[]
    for item in samples:
        if not dedup or item[0]>dedup[-1][0]:dedup.append(item)
    # A single stale history tail must NOT falsely authorize trading.
    if not dedup or now-dedup[-1][0]>30:return []
    return dedup

def preload_market_history(close, timestamped):
    beginning=close-900
    minutes={}
    for ts,price,_ in timestamped:
        slot=int((ts-beginning)//60)
        if 0<=slot<=13:minutes.setdefault(slot,price)
    return minutes

def record_readiness(ticker,raw,source,history):
    count=int(raw.get('minute_pairs') or 0)
    latest=history['ticks'][-1][0] if history['ticks'] else 0
    add(HISTORY_AUDIT,HISTORY_FIELDS,dict(time_utc=raw['time_utc'],ticker=ticker,readiness='READY' if count>=8 else 'NOT READY',minute_pairs=count,source=source,latest_history_utc=datetime.fromtimestamp(latest,timezone.utc).isoformat() if latest else '',age_seconds=f'{time.time()-latest:.1f}' if latest else ''))

def research_early(raw,cp):
    ask=D(raw['ask']) if raw['ask'] else None
    depth=D(raw['quoted_depth']) if raw['quoted_depth'] else None
    count=int(raw.get('minute_pairs') or 0)
    n,fee=size_paper(ask,depth)
    candidate=checkpoint_candidate(raw,cp)
    return dict(ticker=raw['ticker'],checkpoint_s=cp,time_utc=raw['time_utc'],side=raw['side'],eth=raw['eth'],strike=raw['strike'],ask=raw['ask'],quote_depth=raw['quoted_depth'],minute_pairs=count,readiness='READY' if count>=8 else 'NOT READY',status='EARLY_SHADOW_ONLY_'+candidate['status'],reason=candidate['reason'],hypothetical_contracts=n,hypothetical_cost=str(ask*n+fee) if n else '',quote_age_seconds='',fill_verified='NO')

def track_early_disappearance(early,raw):
    if not 88<=float(raw['seconds_left'])<=92:return
    if early is None:return
    eask=D(early['ask']) if early.get('ask') else None
    later=D(raw['ask']) if raw.get('ask') else None
    edepth=D(early['quote_depth']) if early.get('quote_depth') else None
    n,fee=size_paper(eask,edepth)
    vanished=eask is not None and P_MIN<=eask<=P_MAX and (later is None or later>P_MAX or later<P_MIN)
    add(DISAPPEAR,DISAPPEAR_FIELDS,dict(ticker=raw['ticker'],side=raw['side'],early_time_utc=early['time_utc'],early_remaining_s=early['checkpoint_s'],early_ask=early['ask'],early_depth=early['quote_depth'],late_time_utc=raw['time_utc'],late_remaining_s=raw['seconds_left'],late_ask=raw['ask'],late_depth=raw['quoted_depth'],disappeared='YES' if vanished else 'NO',potentially_affordable='YES' if n>0 else 'NO',hypothetical_fee=str(fee),fill_verified='NO',note='Snapshots cannot establish actual fills; depth is indicative and can cancel'))

def features(history,ts,price,strike,remaining):
    minute=int((ts-(history['close']-900))//60)
    if 0<=minute<=13:history['minutes'].setdefault(minute,price)
    history['ticks'].append((ts,price))
    history['ticks']=[(t,p) for t,p in history['ticks'] if t>=ts-180]
    pairs=[]
    j=min(13,minute)
    while j>=0 and j in history['minutes']:
        pairs.append(history['minutes'][j]);j-=1
    pairs.reverse()
    diffs=[float(pairs[i]-pairs[i-1]) for i in range(1,len(pairs))]
    sigma=(sum(map(abs,diffs))/len(diffs))*math.sqrt(math.pi/2) if diffs else None
    def move(horizon):
        eligible=[(t,p) for t,p in history['ticks'] if t<=ts-horizon]
        return float(price-eligible[-1][1]) if eligible else None
    absolute=abs(float(price-strike))
    scaled=absolute/(sigma*math.sqrt(max(remaining,0.1)/60)) if sigma and sigma>0 else None
    if history['close']-60<=ts<history['close']:history['partial'].append((ts,price))
    partial=(sum(p for t,p in history['partial'])/len(history['partial'])) if history['partial'] else None
    return dict(sigma_min=f'{sigma:.5f}' if sigma is not None else '',scaled_gap=f'{scaled:.5f}' if scaled is not None else '',minute_pairs=len(diffs),
                momentum_30s=f'{move(30):.4f}' if move(30) is not None else '',momentum_60s=f'{move(60):.4f}' if move(60) is not None else '',
                observed_partial_lastminute_mean=str(partial) if partial is not None else '',partial_observation_count=len(history['partial']))

def checkpoint_candidate(raw,checkpoint,early_hold_gap=None):
    ask=D(raw['ask']) if raw['ask'] else None
    depth=D(raw['quoted_depth']) if raw['quoted_depth'] else None
    # V12.2: the quote shows only the FRONT price level, which can be a crumb (0.40 contracts).
    # Size the intent on the trade-size budget; the live engine then buys only what the real book offers
    # within its limit (best ask + 1c, never above 99c).
    n,fee=size_paper(ask,D(1000) if depth is not None and depth>0 else depth)
    cost=ask*n+fee if n else D(0)
    if cost > BUDGET: raise AssertionError("PAPER_COST_CAP_BREACH")
    issues=[]
    # Safety gates are evaluated on each side's correct directional cushion.
    # gap_signed = ETH - strike: YES needs positive; NO needs negative.
    gap=D(raw['gap_signed'])
    strike=D(raw['strike'])
    direction_ok=(gap>0 if raw['side']=='yes' else gap<0)
    cushion_pct=(abs(gap)/strike*D(100)) if strike>0 else D(0)
    scaled=D(raw['scaled_gap']) if raw.get('scaled_gap') else None
    if not direction_ok:issues.append('SIDE_NOT_FAVORED_BY_ETH_STRIKE')
    if cushion_pct<MIN_CUSHION_PCT:issues.append('STRIKE_CUSHION_BELOW_0_03_PERCENT' if MIN_CUSHION_PCT==D('0.03') else 'STRIKE_CUSHION_BELOW_CONFIGURED_MINIMUM')
    if scaled is None:issues.append('MISSING_VOLATILITY_ADJUSTED_CUSHION')
    elif scaled<scaled_gate_for_checkpoint(checkpoint,early_hold_gap):issues.append('SCALED_CUSHION_BELOW_CHECKPOINT_MINIMUM')
    if raw['minute_pairs']=='' or int(raw['minute_pairs'])<8:issues.append('LESS_THAN_8_MINUTE_PAIRS')
    if ask is None:issues.append('NO_SIZED_EXECUTABLE_QUOTE')
    elif not P_MIN<=ask<=P_MAX:issues.append('ASK_OUTSIDE_90_TO_99_CENTS')
    if ask is not None and depth is not None and depth<=0:issues.append('INSUFFICIENT_QUOTED_DEPTH')
    if ask is not None and P_MIN<=ask<=P_MAX and n==0:issues.append('CANNOT_SIZE_WITHIN_BUDGET')
    if n>0 and (D(n)-cost)<=0:issues.append('NO_POSITIVE_WIN_PROFIT_AFTER_ESTIMATED_FEE')
    # This is market-implied research, NOT a calibrated safety rule. Candidates do
    # not claim probabilities of winning, and should never be executed live.
    return dict(ticker=raw['ticker'],checkpoint_s=checkpoint,time_utc=raw['time_utc'],actual_seconds_left=raw['seconds_left'],side=raw['side'],eth=raw['eth'],strike=raw['strike'],gap_abs=raw['gap_abs'],sigma_min=raw['sigma_min'],scaled_gap=raw['scaled_gap'],cushion_pct=str(cushion_pct),minimum_cushion_pct=str(MIN_CUSHION_PCT),ask=raw['ask'],quote_depth=raw['quoted_depth'],contracts=n,estimated_fee=fee,total_cost=cost,profit_if_win=D(n)-cost if n else 0,loss_if_lose=-cost if n else 0,reason=';'.join(issues),status='SHADOW_CANDIDATE' if not issues else 'REJECT',settlement_side='',estimated_pnl='')

def _known_market_closes():
    """Derive market close from quote timestamp plus remaining seconds; no inferred result."""
    closes={}
    for r in read(RAW):
        try:
            stamp=datetime.fromisoformat(r['time_utc'].replace('Z','+00:00')).timestamp()
            closing=stamp+float(r['seconds_left'])
            if r['ticker'] not in closes:closes[r['ticker']]=closing
        except (ValueError,TypeError,KeyError):
            continue
    return closes

def reconcile(client, *, limit=12, retry_seconds=300):
    """Fetch official results for ALL finished observed markets, even with no candidates.

    Never infer wins from sampled ETHRTI or a market's status. Keep unresolved rows.
    Use bounded API requests to avoid blocking ongoing quote collection excessively.
    """
    closes=_known_market_closes(); ts=time.time()
    known={r['ticker']:r for r in read(SETTLE_ALL)}
    pending=[]
    for ticker,close in sorted(closes.items(),key=lambda x:x[1]):
        if close>ts-5:continue
        row=known.get(ticker,{})
        if row.get('official_side') in ('yes','no'):continue
        last=row.get('last_checked_utc','')
        if last:
            try:
                if ts-datetime.fromisoformat(last.replace('Z','+00:00')).timestamp()<retry_seconds:continue
            except ValueError:pass
        pending.append((ticker,close))
    checks=0;newly=0
    for ticker,close in pending[:max(0,limit)]:
        checks+=1
        row=dict(ticker=ticker,close_utc=datetime.fromtimestamp(close,timezone.utc).isoformat(timespec='seconds'),official_side='',official_status='',last_checked_utc=nowutc(),note='')
        try:
            market=client.request('GET','/markets/'+ticker)['market']
            row['official_status']=str(market.get('status') or '')
            result=base.settlement_side(market)
            if result in ('yes','no'):
                row['official_side']=result;newly+=1
            else:row['note']='Awaiting explicit YES/NO official result'
        except Exception as ex:
            row['note']=f'{type(ex).__name__}: {str(ex)[:150]}'
            add(ERRORS,ERROR_FIELDS,dict(time_utc=nowutc(),stage='settlement_reconciliation',ticker=ticker,error_type=type(ex).__name__,message=str(ex)[:300]))
        known[ticker]=row
    if checks:
        save(SETTLE_ALL,SETTLE_FIELDS,[known[k] for k in sorted(known)])
        print(f'RECONCILIATION checked={checks} new_official_results={newly} completed_markets_with_official_results={sum(r["official_side"] in ("yes","no") for r in known.values())}',flush=True)
    return known

# Attempts are logged separately from successful paper stops. A trigger is not a fill.
BOOK_AUDIT=ROOT/'eth_v7c_orderbook_exit_audit.csv'
BOOK_FIELDS=['time_utc','ticker','side','bid_levels','best_bid','displayed_depth','vwap','remaining_contracts','execution_verified']
STOP_ATTEMPTS=ROOT/'eth_v7c_stop_attempts.csv'
ATTEMPT_FIELDS=['eth_price','strike_price','directional_multiple','eth_data_age_s','stop_reason','momentum_toward_60s','momentum_15usd_research','time_utc','ticker','side','seconds_left','attempt','trigger_price','opposite_ask','opposite_ask_depth','implied_sell_price','contracts','status','reason','execution_verified','remaining_contracts']
_PENDING_STOPS={}

SETTLEMENT_AUDIT=ROOT/'eth_v7c_settlement_stop_audit.csv'
SETTLEMENT_FIELDS=['time_utc','ticker','side','seconds_left','strike','eth','observed_partial_mean','partial_count','seconds_covered','largest_sample_gap_s','required_remainder_mean','signed_distance_to_boundary','threshold_dollars','data_quality','decision','stop_reason','method']

def settlement_stop_assessment(history, side, strike, eth, remaining, sigma):
    """Conservative *proxy* for final-minute arithmetic average, never official ETHRTI.
    Time weights are piecewise-constant between captured ETHRTI observations.
    Returns UNAVAILABLE when readings cannot reliably cover the elapsed window.
    """
    if remaining > 60 or remaining <= 0 or not history or eth is None or sigma is None or sigma <= 0:
        return dict(quality='UNAVAILABLE',decision='LEGACY')
    elapsed=60-float(remaining)
    start=history['close']-60
    ticks=sorted((float(t)-start,float(p)) for t,p in history.get('partial',[]) if start<=float(t)<history['close'])
    ticks=[(t,p) for t,p in ticks if t<=elapsed+0.25]
    if len(ticks)<5 or elapsed<10 or ticks[0][0]>4.0:
        return dict(quality='INSUFFICIENT_COVERAGE',decision='LEGACY',count=len(ticks))
    bounds=[0.0]+[t for t,p in ticks]+[elapsed]
    max_gap=max(bounds[i+1]-bounds[i] for i in range(len(bounds)-1))
    if max_gap>6.0 or ticks[-1][0]<elapsed-6.0:
        return dict(quality='GAPPED',decision='LEGACY',count=len(ticks),max_gap=max_gap)
    # Internal coherence guard: wrong-market/preload/source/clock contamination.
    # This is a *proxy*, not an assertion that benchmark and spot must match.
    latest=ticks[-1][1]
    max_plausible=max(ASSET.P['coherence_floor_usd'],6.0*float(sigma))   # ETH 100, ETH 3
    if abs(latest-float(eth))>max_plausible:
        return dict(quality='SOURCE_PRICE_MISMATCH',decision='INVALID',count=len(ticks),max_gap=max_gap)
    # A time-weighted mean must lie inside the observed price range.
    # Assume first observation held since start, each further observation until next.
    total=0.0
    covered=0.0
    for i,(t,price) in enumerate(ticks):
        left=0.0 if i==0 else t
        right=ticks[i+1][0] if i+1<len(ticks) else elapsed
        dt=max(0.0,right-left)
        total+=price*dt
        covered+=dt
    if covered<=0 or abs(covered-elapsed)>0.25:
        return dict(quality='INVALID_DURATION',decision='INVALID',count=len(ticks))
    avg=total/covered
    if not min(p for _,p in ticks)-0.01<=avg<=max(p for _,p in ticks)+0.01:
        return dict(quality='INVALID_AVERAGE',decision='INVALID',count=len(ticks),mean=avg)
    if abs(avg-float(eth))>max(ASSET.P['coherence_floor_usd'],6.0*float(sigma)):
        return dict(quality='PARTIAL_AVERAGE_MISMATCH',decision='INVALID',count=len(ticks),mean=avg)
    required=(60*float(strike)-elapsed*avg)/float(remaining)
    # Positive distance implies favorable current spot relative to required remainder.
    signed=(float(eth)-required) if side=='yes' else (required-float(eth))
    threshold=0.5*float(sigma)*math.sqrt(max(float(remaining),0.1)/60)
    return dict(quality='SAMPLED_PROXY',decision='TRIGGER' if signed<=threshold else 'HOLD',count=len(ticks),max_gap=max_gap,elapsed=elapsed,mean=avg,required=required,signed=signed,threshold=threshold)

def displayed_exit_bids(client,ticker,side,limit):
    """Read public Kalshi bids on the held side. Observed liquidity is NOT a fill."""
    data=client.request('GET','/markets/'+ticker+'/orderbook')
    book=data.get('orderbook_fp',{})
    levels=book.get(side+'_dollars')
    if not isinstance(levels,list):
        raise ValueError('orderbook_fp missing side bids')
    remaining=int(limit);fills=[]
    for price,count in sorted(levels,key=lambda x:D(str(x[0])),reverse=True):
        p=D(str(price));qty=int(D(str(count)))
        if not (D(0)<p<D(1)) or qty<1:continue
        take=min(remaining,qty)
        if take: fills.append((p,take));remaining-=take
        if not remaining:break
    return fills

def _v17_stop_reason(side,strike,eth_price,eth_ts,remaining,features_now,history_now,implied):
    """Shared V7-C stop rule (paper AND live). Returns (reason_or_None, details).

    reason is None when no exit is warranted. details['settlement'] carries the proxy result.
    """
    age=max(0,time.time()-float(eth_ts)) if eth_ts is not None else None
    fresh=eth_price is not None and age is not None and 0<=age<=ETH_MAX_AGE_SECONDS
    strike=D(str(strike))
    signed=(D(str(eth_price))-strike)*(D(1) if side=='yes' else D(-1)) if fresh else None
    sigma=D(str(features_now['sigma_min'])) if fresh and features_now and features_now.get('sigma_min') else None
    denom=sigma*D(str(math.sqrt(max(float(remaining),0.1)/60))) if sigma is not None and sigma>0 else None
    multiple=signed/denom if denom else None
    settlement=settlement_stop_assessment(history_now,side,strike,eth_price,remaining,sigma) if fresh else dict(quality='STALE',decision='LEGACY')
    details=dict(age=age,fresh=fresh,signed=signed,sigma=sigma,multiple=multiple,settlement=settlement)
    if settlement['decision']=='HOLD':
        return None,details
    reason=None
    if settlement['decision']=='TRIGGER': reason='V7C_SETTLEMENT_BOUNDARY_THREATENED'
    elif fresh and signed is not None and signed<=0:reason='ETH_CROSSED_STRIKE'
    elif fresh and multiple is not None and multiple<=ETH_STOP_MULTIPLE:reason='ETH_DISTANCE_MULTIPLE_LE_0P5'
    elif (settlement['decision']!='INVALID' and fresh and multiple is not None
          and multiple<D('1.0') and implied is not None and implied<=STOP_TRIGGER):
        reason='PRICE_BACKUP_88C_ETH_LT_1X'
    return reason,details


def v7c_stop_reason(side,strike,eth_price,eth_ts,remaining,features_now,history_now,implied,ticker=None,now=None):
    """ETH V1.8 stop rule (paper AND live). Returns (reason_or_None, details).

    1. ETH_WRONG_SIDE_10S: ETH has been on the wrong side of the target for 10 seconds in a row.
       In the final minute 'wrong side' uses the settlement-average proxy when it is valid
       (projected 60-second average on the wrong side), otherwise the live ETH price.
    2. PRICE_EMERGENCY_50C: no fresh ETH price AND our side's price is 50c or lower (blind fallback).
    The V1.7 reason is still computed and returned in details['old_reason'] for comparison only.
    """
    old_reason,details=_v17_stop_reason(side,strike,eth_price,eth_ts,remaining,features_now,history_now,implied)
    details['old_reason']=old_reason
    now=time.time() if now is None else float(now)
    key=(ticker or str(strike),side)
    if implied is not None and D(str(implied))<=ETH_EMERGENCY_PRICE and not details['fresh']:
        return 'PRICE_EMERGENCY_50C',details     # flying blind (no fresh ETH price) and our side is at 50c or less
    st=details['settlement']
    if st.get('quality')=='SAMPLED_PROXY' and st.get('signed') is not None:
        wrong=float(st['signed'])<=0
    elif details['fresh'] and details['signed'] is not None:
        wrong=details['signed']<=0
    else:
        wrong=None                      # no trustworthy ETH price: keep the timer, decide nothing
    if wrong is False:
        _WRONG_SINCE.pop(key,None)
        return None,details
    if wrong is None:
        return None,details
    since=_WRONG_SINCE.setdefault(key,now)
    details['wrong_for_s']=now-since
    if now-since>=ETH_CROSS_HOLD_S:
        return 'ETH_WRONG_SIDE_10S',details
    return None,details

def process_paper_stops(client,db,market,ticker,remaining,observed,*,eth_price=None,eth_ts=None,features_now=None,history_now=None):
    """Paper-only; ETH directional stop or independent opposite-ask price stop.

    Stop is a trigger, not a fill. Only the first qualifying observed proxy exit
    with sufficient indicative depth is booked. No actual bid/fill is asserted.
    """
    if remaining<=5:return
    active=[r for r in risk_outstanding(db) if r[0]==ticker]
    if not active:return
    for tick,side,cost_cents,n in active:
        other='no' if side=='yes' else 'yes'
        quote=observed.get(other)
        pending=_PENDING_STOPS.get(tick)
        strike=D(str(market['floor_strike']))
        move_60=D(str(features_now['momentum_60s'])) if features_now and features_now.get('momentum_60s') else None
        toward=(-move_60 if side=='yes' else move_60) if move_60 is not None else None
        momentum_hit=toward is not None and toward>=MOMENTUM_RESEARCH_USD
        other_ask=D(str(quote[0])) if quote and quote[0] is not None else None
        if other_ask is not None and not D(0)<=other_ask<=D(1):other_ask=None
        implied=D(1)-other_ask if other_ask is not None else None
        depth=D(str(quote[1])) if quote and len(quote)>1 and quote[1] is not None else None
        reason,_det=v7c_stop_reason(side,strike,eth_price,eth_ts,remaining,features_now,history_now,implied,ticker=tick)
        if _det.get('old_reason') and tick not in _OLD_RULE_LOGGED:
            _OLD_RULE_LOGGED.add(tick)
            add(STOP_COMPARE,COMPARE_FIELDS,dict(time_utc=nowutc(),ticker=tick,side=side,seconds_left=f'{remaining:.2f}',old_v17_reason=_det['old_reason'],directional_multiple=str(_det.get('multiple')),held_price=str(implied) if implied is not None else '',note='V1.7 stop WOULD have sold here; V1.8 decides separately'))
            print(f'V17_STOP_WOULD_FIRE ticker={tick} reason={_det["old_reason"]} implied={implied} (comparison only)',flush=True)
        age=_det['age'];fresh=_det['fresh'];signed=_det['signed'];sigma=_det['sigma'];multiple=_det['multiple'];settlement=_det['settlement']
        add(SETTLEMENT_AUDIT,SETTLEMENT_FIELDS,dict(time_utc=nowutc(),ticker=tick,side=side,seconds_left=f'{remaining:.2f}',strike=str(strike),eth=str(eth_price) if fresh else '',observed_partial_mean=str(settlement.get('mean','')),partial_count=settlement.get('count',''),seconds_covered=settlement.get('elapsed',''),largest_sample_gap_s=settlement.get('max_gap',''),required_remainder_mean=str(settlement.get('required','')),signed_distance_to_boundary=str(settlement.get('signed','')),threshold_dollars=str(settlement.get('threshold','')),data_quality=settlement['quality'],decision=settlement['decision'],stop_reason='',method='sampled time-weighted proxy; NOT official benchmark; no fill verification'))
        if settlement['decision']=='INVALID':
            _PENDING_STOPS.pop(tick,None)
            print(f'V7C_INVALID_SETTLEMENT_DATA ticker={tick} quality={settlement["quality"]}; price-backup suppressed until data valid',flush=True)
        if settlement['decision']=='HOLD' and reason is None:
            _PENDING_STOPS.pop(tick,None)
            continue
        if reason is not None:
            pass
        elif implied is not None and implied<=STOP_TRIGGER and (not fresh or multiple is None or settlement['decision']=='INVALID'):
            print(f'V7C_DATA_QUALITY_WARNING ticker={tick} price_backup_ignored quality={settlement["quality"]}',flush=True)
        if settlement['decision']=='INVALID' and reason is None:continue
        if reason is None:
            _PENDING_STOPS.pop(tick,None)
            continue
        if pending is None:
            pending={'attempts':0,'reason':reason,'trigger_implied':str(implied) if implied is not None else ''}
            _PENDING_STOPS[tick]=pending
            print(f'V7C_PAPER_STOP_TRIGGER ticker={tick} reason={reason} directional_multiple={multiple} implied={implied} (NO FILL YET)',flush=True)
        pending['attempts']+=1
        base_row=dict(time_utc=nowutc(),ticker=tick,side=side,seconds_left=f'{remaining:.2f}',attempt=pending['attempts'],trigger_price=str(STOP_TRIGGER),opposite_ask=str(other_ask) if other_ask is not None else '',opposite_ask_depth=str(depth) if depth is not None else '',implied_sell_price=str(implied) if implied is not None else '',contracts=n,execution_verified='NO',eth_price=str(eth_price) if fresh else '',strike_price=str(strike),directional_multiple=str(multiple) if multiple is not None else '',eth_data_age_s=str(age) if age is not None else '',stop_reason=pending['reason'],momentum_toward_60s=str(toward) if toward is not None else '',momentum_15usd_research='YES' if momentum_hit else 'NO')
        # Validate actual displayed bids; never assume complementary ask is executable.
        try:
            fills=displayed_exit_bids(client,tick,side,n)
        except Exception as ex:
            add(STOP_ATTEMPTS,ATTEMPT_FIELDS,dict(base_row,status='RETRY',reason='ORDERBOOK_UNAVAILABLE_'+type(ex).__name__,remaining_contracts=n))
            continue
        if not fills:
            add(STOP_ATTEMPTS,ATTEMPT_FIELDS,dict(base_row,status='RETRY',reason='NO_DISPLAYED_SIDE_BIDS',remaining_contracts=n))
            continue
        qty=sum(q for p,q in fills)
        proceeds=sum(p*q for p,q in fills)
        bid_vwap=proceeds/D(qty)
        exit_price=max(D(0),bid_vwap-EXIT_SLIPPAGE_PER_CONTRACT)
        exit_fee=base.estimate_fee(exit_price,qty)
        result=risk_partial_exit(db,tick,qty,bid_vwap,exit_fee,EXIT_SLIPPAGE_PER_CONTRACT)
        r=dict(time_utc=base_row['time_utc'],ticker=tick,side=side,seconds_left=base_row['seconds_left'],opposite_ask=str(other_ask),implied_sell_price=str(bid_vwap),modeled_sell_price_after_slippage=str(exit_price),contracts=qty,entry_total_cost=str(D(cost_cents)*qty/D(n)/100),exit_fee=str(exit_fee),modeled_pnl=str(D(result['pnl_cents'])/100),status='PARTIAL_EXIT' if result['remaining'] else 'STOPPED',execution_verified='NO',opposite_ask_depth=str(sum(q for p,q in fills)),attempt=pending['attempts'],trigger_sell_price=str(STOP_TRIGGER),retry_reason='',remaining_contracts=result['remaining'],quote_is_executable_bid='DISPLAYED_BID_NOT_FILL',note='V7C observed bid levels; paper sweep; NO real fill verification')
        add(BOOK_AUDIT,BOOK_FIELDS,dict(time_utc=base_row['time_utc'],ticker=tick,side=side,bid_levels=str([(str(p),q) for p,q in fills]),best_bid=str(fills[0][0]),displayed_depth=qty,vwap=str(bid_vwap),remaining_contracts=result['remaining'],execution_verified='NO'))
        add(STOP_LOG,STOP_FIELDS,r)
        add(STOP_ATTEMPTS,ATTEMPT_FIELDS,dict(base_row,status='PARTIAL_SIMULATED' if result['remaining'] else 'STOPPED_SIMULATED',reason='DISPLAYED_BIDS_NOT_VERIFIED_FILLS',contracts=qty,remaining_contracts=result['remaining']))
        if not result['remaining']:_PENDING_STOPS.pop(tick,None)
        rows=read(CAND)
        for cand in rows:
            if cand['ticker']==tick and cand['side']==side and cand['status'] in ('PAPER_APPROVED','PARTIALLY_EXITED'):
                cand.update(status='PARTIALLY_EXITED' if result['remaining'] else 'STOPPED',exit_time_utc=r['time_utc'],exit_sell_price=str(exit_price),exit_fee=str(exit_fee),exit_slippage=str(EXIT_SLIPPAGE_PER_CONTRACT),exit_pnl=str(D(result['total_exit_pnl_cents'])/100),estimated_pnl='')
                break
        save(CAND,CAND_FIELDS,rows)
        stops,pnl=risk_daily_status()
        print(f'V7C_PAPER_PARTIAL_EXIT ticker={tick} qty={qty} remaining={result["remaining"]} pnl=${D(result["pnl_cents"])/100:+.2f} DAILY_STOPS={stops} EXECUTION_UNVERIFIED',flush=True)


def settle(client, risk_db=None):
    official=reconcile(client,limit=12)
    # Ledger reconciliation is independent of session CSVs: the supervisor
    # archives CSVs between sessions, but open paper intent state persists.
    if risk_db is not None:
        for ticker,side,cost_cents,contracts in risk_outstanding(risk_db):
            outcome=official.get(ticker,{}).get('official_side','')
            if outcome not in ('yes','no'):
                try:
                    outcome=base.settlement_side(client.request('GET','/markets/'+ticker)['market'])
                except Exception as ex:
                    add(ERRORS,ERROR_FIELDS,dict(time_utc=nowutc(),stage='ledger_reconcile',ticker=ticker,error_type=type(ex).__name__,message=str(ex)[:250]))
            if outcome in ('yes','no'):
                pnl=(D(contracts)-D(cost_cents)/100 if outcome==side else -D(cost_cents)/100)
                risk_reconcile(risk_db,[(ticker,outcome,str(pnl.quantize(D('0.01'))))])
    rows=read(CAND);changed=False
    for r in rows:
        if r['status'] not in ('PAPER_APPROVED','PARTIALLY_EXITED'):continue
        # Only approved intents can become selected settled positions.
        ledger_row=risk_db.execute('SELECT status,side,remaining_contracts,remaining_cost_cents,exit_pnl_cents FROM intents WHERE ticker=?',(r['ticker'],)).fetchone() if risk_db is not None else None
        if not ledger_row or ledger_row[0]!='SETTLED' or ledger_row[1]!=r['side']:continue
        outcome=official.get(r['ticker'],{}).get('official_side','')
        if outcome not in ('yes','no'):continue
        won=r['side']==outcome
        r.update(status='SETTLED_WIN' if won else 'SETTLED_LOSS',settlement_side=outcome,estimated_pnl=str((D(ledger_row[4])+ (D(ledger_row[2])*100-D(ledger_row[3]) if won else -D(ledger_row[3])))/100))
        changed=True
        print(f"SETTLEMENT {r['ticker']} {r['checkpoint_s']}s {r['side'].upper()} {r['status']} P/L ${D(r['estimated_pnl']):+.2f}",flush=True)
    if changed:save(CAND,CAND_FIELDS,rows)
    if risk_db is not None:
        selected={}
        for r in sorted(rows,key=lambda x:(x['ticker'],-int(x['checkpoint_s']),x['time_utc'])):
            if r['status'] in ('SETTLED_WIN','SETTLED_LOSS'):
                selected.setdefault(r['ticker'],r)
        ready=[]
        for ticker,r in selected.items():
            outcome=official.get(ticker,{}).get('official_side','')
            if outcome in ('yes','no'):
                pnl=D(r['contracts'])-D(r['total_cost']) if r['side']==outcome else -D(r['total_cost'])
                ready.append((ticker,outcome,str(pnl.quantize(D('0.01')))))
        if ready:risk_reconcile(risk_db,ready)
    summarize(silent=True)

def _pnl(entry,result):
    if entry is None or result not in ('yes','no'):return ''
    return str(D(entry['contracts'])-D(entry['total_cost']) if entry['side']==result else -D(entry['total_cost']))

def compare_policies():
    """Offline counterfactual on IDENTICAL checkpoint records, not an executable fill replay.

    Baseline here is V41 WITHOUT the 0.03% and 1.4 scaled gates; not an
    assertion of precise V40 equivalence. Only existing sampled checkpoints.
    """
    candidates=read(CAND)
    official={r['ticker']:r['official_side'] for r in read(SETTLE_ALL) if r['official_side'] in ('yes','no')}
    # Preserve explicit, previously verified settlement outcomes in older V41 candidate CSVs.
    for r in candidates:
        if r['status'] in ('SETTLED_WIN','SETTLED_LOSS') and r.get('settlement_side') in ('yes','no'):
            official.setdefault(r['ticker'],r['settlement_side'])
    grouped={}
    for r in candidates:grouped.setdefault(r['ticker'],[]).append(r)
    rows=[]
    relaxed={'STRIKE_CUSHION_BELOW_0_03_PERCENT','STRIKE_CUSHION_BELOW_CONFIGURED_MINIMUM','SCALED_CUSHION_BELOW_CHECKPOINT_MINIMUM'}
    for ticker,records in sorted(grouped.items()):
        records=sorted(records,key=lambda r:(-int(r['checkpoint_s']),r['time_utc']))
        safe=next((r for r in records if r['status'] in ('PAPER_APPROVED','SETTLED_WIN','SETTLED_LOSS')),None)
        baseline=next((r for r in records if r.get('contracts') and int(r['contracts'])>0 and set(filter(None,r['reason'].split(';'))).issubset(relaxed)),None)
        outcome=official.get(ticker,'')
        def side_status(r):
            if r is None:return 'NO_ENTRY'
            if outcome not in ('yes','no'):return 'UNVERIFIED'
            return 'WIN' if r['side']==outcome else 'LOSS'
        rows.append(dict(ticker=ticker,official_side=outcome or 'UNVERIFIED',v41_entry_checkpoint=safe['checkpoint_s'] if safe else '',v41_side=safe['side'] if safe else '',v41_status=side_status(safe),v41_net_pnl=_pnl(safe,outcome),baseline_entry_checkpoint=baseline['checkpoint_s'] if baseline else '',baseline_side=baseline['side'] if baseline else '',baseline_status=side_status(baseline),baseline_net_pnl=_pnl(baseline,outcome),baseline_only_rejection_gates=baseline['reason'] if baseline else ''))
    save(COMPARISON,COMPARISON_FIELDS,rows)
    for label,prefix in [('V42.1 safe filters','v41'),('Ablation: remove both new gates','baseline')]:
        selected=[r for r in rows if r[prefix+'_entry_checkpoint']]
        settled=[r for r in selected if r[prefix+'_status'] in ('WIN','LOSS')]
        pnl=sum((D(r[prefix+'_net_pnl']) for r in settled),D(0))
        print(f'{label}: selected={len(selected)}, settled={len(settled)}, wins={sum(r[prefix+"_status"]=="WIN" for r in settled)}, losses={sum(r[prefix+"_status"]=="LOSS" for r in settled)}, modeled_pnl=${pnl:+.2f}')
    print('Ablation is not exact V40 replay. No retrospective selection of alternative checkpoint after a failed earlier choice. Official outcomes only; missing outcomes not counted.')
    print(f'Comparison file: {COMPARISON}')

def summarize(silent=False):
    raw=read(RAW);cand=read(CAND)
    all_ids=sorted({r['ticker'] for r in raw}|{r['ticker'] for r in cand})
    rows=[]
    official_map={o['ticker']:o['official_side'] for o in read(SETTLE_ALL) if o['official_side'] in ('yes','no')}
    for c in cand:
        if c['status'] in ('SETTLED_WIN','SETTLED_LOSS') and c.get('settlement_side') in ('yes','no'):
            official_map.setdefault(c['ticker'],c['settlement_side'])
    for ticker in all_ids:
        c=[r for r in cand if r['ticker']==ticker]
        settled=[r for r in c if r['status'].startswith('SETTLED_')]
        rows.append(dict(ticker=ticker,settlement_side=official_map.get(ticker) if official_map.get(ticker) in ('yes','no') else 'UNVERIFIED',last_checked_utc=nowutc(),checkpoints_considered=len(c),candidates_with_quote=sum(bool(r['ask']) for r in c),shadow_positions=sum(r['status'] in ('PAPER_APPROVED','SETTLED_WIN','SETTLED_LOSS') for r in c),settled_positions=len(settled),shadow_pnl=str(sum((D(r['estimated_pnl']) for r in settled),D(0)))))
    save(SUMMARY,OUT_FIELDS,rows)
    if not silent:
        settled=[r for r in cand if r['status'].startswith('SETTLED_')]
        wins=sum(r['status']=='SETTLED_WIN' for r in settled)
        pnl=sum((D(r['estimated_pnl']) for r in settled),D(0))
        print(f'Markets: {len(all_ids)} | checkpoints: {len(cand)} | shadow candidates: {sum(r["status"] in ("SHADOW_CANDIDATE","SETTLED_WIN","SETTLED_LOSS") for r in cand)} | settled: {len(settled)} | wins: {wins} | estimated P/L ${pnl:+.2f}')
        chosen=[]
        for ticker in all_ids:
            eligible=[r for r in cand if r['ticker']==ticker and r['status'] in ('PAPER_APPROVED','SETTLED_WIN','SETTLED_LOSS')]
            if eligible:
                chosen.append(sorted(eligible,key=lambda r:(-int(r['checkpoint_s']),r['time_utc']))[0])
        accounted=[r for r in chosen if r['status'].startswith('SETTLED_')]
        total=sum((D(r['estimated_pnl']) for r in accounted),D(0))
        print(f'ONE-ENTRY-PER-MARKET simulation: selected {len(chosen)}, settled {len(accounted)}, wins {sum(r["status"]=="SETTLED_WIN" for r in accounted)}, losses {sum(r["status"]=="SETTLED_LOSS" for r in accounted)}, hypothetical P/L ${total:+.2f}')
        print(f'Gate: ETH strike cushion >= {MIN_CUSHION_PCT}% AND volatility-adjusted cushion >= {MIN_SCALED_GAP}. Do not use for live trades.')
        print('Observations are correlated within markets. Quoted depth and fills are hypothetical; no winning probability is guaranteed.')

def replay():
    # Only uses previously recorded v38 fields; reconstructs historical snapshot
    # prices but does not fabricate missed per-second orderbook or settlement data.
    source=ROOT/'eth_v38_final60_decisions.csv'
    if not source.exists():raise SystemExit(f'Not found: {source}')
    observations=read(source);closest={};count=0
    for r in observations:
        if not r.get('remaining_s') or not r.get('side'):continue
        rem=float(r['remaining_s'])
        if rem<=0 or rem>180:continue
        cp=min(CHECKPOINTS,key=lambda t:abs(rem-t))
        if abs(rem-cp)>10:continue
        key=(r['ticker'],cp)
        if key not in closest or abs(rem-cp)<abs(float(closest[key]['remaining_s'])-cp):closest[key]=r
    existing={(r['ticker'],int(r['checkpoint_s']),r['side']) for r in read(CAND)}
    for (ticker,slot),r in sorted(closest.items()):
        key=(ticker,slot,r['side'])
        if key in existing:continue
        remaining=float(r['remaining_s']);gap=D(r['gap'])
        sigma=float(r['sigma_min']) if r.get('sigma_min') else 0
        normalized=dict(time_utc=r['time_utc'],ticker=ticker,seconds_left=r['remaining_s'],side=r['side'],eth=r['eth'],strike=r['strike'],gap_abs=str(abs(gap)),gap_signed=r['gap'],sigma_min=r.get('sigma_min',''),scaled_gap=(f'{abs(float(gap))/(sigma*math.sqrt(remaining/60)):.5f}' if sigma>0 else ''),minute_pairs='8' if sigma>0 else '0',momentum_30s='',momentum_60s='',ask=r.get('ask',''),quoted_depth=r.get('quote_depth',''),quote_source='historic_v38_snapshot',observed_partial_lastminute_mean=r.get('observed_partial_mean',''),partial_observation_count=r.get('observed_final_minute_points',''))
        add(RAW,RAW_FIELDS,normalized)
        add(CAND,CAND_FIELDS,checkpoint_candidate(normalized,slot));count+=1
    summarize()
    print(f'Imported {count} checkpoint snapshots; no official settlements were inferred and no order-book changes reconstructed.')

def current_eth_market(client, now=None):
    """Discover currently active 15-minute market, independent of status=open filter.

    The list endpoint can return zero for status=open while the unfiltered
    series listing still contains initialized future markets and active ones.
    Scan bounded pages and select based on actual market status/close time.
    Fail closed: never use a future initialized market to make a candidate.
    """
    if now is None:
        now=time.time()
    cursor=''
    seen_cursors=set()
    candidates=[]
    for _ in range(10):
        params={'series_ticker':ASSET.SERIES,'limit':200}
        if cursor:
            params['cursor']=cursor
        data=client.request('GET','/markets',params)
        markets=data.get('markets') or []
        for market in markets:
            if market.get('status')!='active':
                continue
            close_time=market.get('close_time')
            if not close_time:
                continue
            try:
                remain=base.iso(close_time)-now
            except (ValueError,TypeError,KeyError):
                continue
            if 0 < remain <= 900:
                candidates.append((remain,market))
        if candidates:
            break
        cursor=data.get('cursor') or ''
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
    if not candidates:
        return None
    _, selected=min(candidates,key=lambda item:item[0])
    detail=client.request('GET','/markets/'+selected['ticker'])['market']
    if detail.get('status')!='active':
        return None
    if not 0 < base.iso(detail['close_time'])-now <= 900:
        return None
    return detail

MLOG=None
def run():
    """Revision 3 wrapper: price logging (markets.csv / market_tape.csv) is flushed on every exit path."""
    global MLOG
    MLOG=MarketLog(Path(os.environ.get('BIGBET_LOG_DIR') or ROOT),SETTLE_ALL,os.environ.get('BIGBET_RE5_STATE_FILE'))
    try:
        return _run()
    finally:
        try:MLOG.maybe_flush(base.Client(auth=True),force=True)
        except Exception:pass

def _run():
    risk_db=risk_connect(RISK_DB)
    hb_file=os.environ.get('BIGBET_HEARTBEAT_FILE')
    init_csv(STOP_LOG,STOP_FIELDS)
    init_csv(STOP_ATTEMPTS,ATTEMPT_FIELDS)
    init_csv(SETTLEMENT_AUDIT,SETTLEMENT_FIELDS)
    init_csv(BOOK_AUDIT,BOOK_FIELDS)
    client=base.Client(auth=True)
    observer=PassiveObserver(ROOT,lambda: base.Client(auth=True))
    missing_strike_logged=set();history={};preloaded=load_verified_history();early_seen=set();early_quotes={};disappearance_seen=set();readiness_logged=set();seen=set((r['ticker'],int(r['checkpoint_s']),r['side']) for r in read(CAND));bid_hist={};ask_hist={};regime_logged=set();last_market=None;last_settle=0;last_line=0;last_discovery_msg=0;cached_market=None;cached_market_at=0
    live=None;live_dir=None;live_errors=0;live_status_at=0;outage_start=None
    # V12.7: times of Kalshi connection glitches. Any 15-minute market whose window contains one gets NO new entry.
    glitch_at=[float(os.environ['BIGBET_GLITCH_AT'])] if os.environ.get('BIGBET_GLITCH_AT') else []
    from ETH_V194_Regime import Regime
    regime=Regime(os.environ.get('BIGBET_MOOD_SAMPLES'))
    for _t,_p,_ in preloaded:regime.add(_t,_p)
    if os.environ.get('BIGBET_RE5_LIVE') in ('demo','prod'):
        import json as _json
        from RE5.exchange import ExchangeClient
        from RE5.controller import Controller as LiveController
        mode=os.environ['BIGBET_RE5_LIVE']
        if os.environ.get('KALSHI_ENV','demo')!=mode:
            raise SystemExit('RE5: KALSHI_ENV must equal BIGBET_RE5_LIVE')
        live_dir=Path(os.environ['BIGBET_RE5_STATE_FILE']).parent
        events=live_dir/'re5_live_events.jsonl'
        def _event_sink(row,_p=events):
            with _p.open('a',encoding='utf-8') as fh:fh.write(_json.dumps(row)+'\n')
        live=LiveController(ExchangeClient(env=mode,write_mode=mode),os.environ['BIGBET_RE5_STATE_FILE'],event_log=_event_sink)
        live.recover()
        print(f'RE5.5 {ASSET.SYMBOL} LIVE EXECUTION ACTIVE on {mode.upper()} | ${BUDGET} cap | IOC only | journal {os.environ["BIGBET_RE5_STATE_FILE"]}',flush=True)
    print(f'{ASSET.SYMBOL} KALSHI MARKET MICROSTRUCTURE LOGGER v42.2 | V7-D MODEL + RULE B | RE5.5 (ETH V1.3) | series {ASSET.SERIES} | index {ASSET.INDEX_ID}',flush=True)
    print(f'YES + NO quotes | FULL 15min market recorded | checkpoints 180,150,120,105,90,60,45,30 | 0.03% default strike safety gate (active {MIN_CUSHION_PCT}%) | entry {P_MIN}-{P_MAX} | scaled gap >= {EARLY_MIN_SCALED_GAP} at 180/150s; >= {MIN_SCALED_GAP} at 120/105/90/60/45/30s | ${BUDGET} maximum paper cost',flush=True)
    print('V7C PAPER STOP: directional ETH <= 0.5x or wrong-side cross; backup proxy <= 88c; first observed exit / retry. 2 stops or 30% of original paper bid cost as daily realized-loss cap. Fills unverified.',flush=True)
    print('Not calibrated; snapshots do not guarantee fills or 99% winning probability.',flush=True)
    print(f'PRELOAD: {len(preloaded)} real ETHRTI samples available, latest age: {(time.time()-preloaded[-1][0]):.1f}s' if preloaded else 'PRELOAD: no sufficiently recent verified ETHRTI history. NOT READY until 8 consecutive minute pairs.',flush=True)
    while True:
        try:
            now=time.time()
            heartbeat(hb_file)
            MLOG.maybe_flush(client,now)
            if live is None and os.environ.get('BIGBET_RE5_END_TIME') and now>=float(os.environ['BIGBET_RE5_END_TIME']):
                summarize(silent=True);print('PAPER SESSION TIME COMPLETE',flush=True);return
            if live is not None:
                _ls=live.j.read()
                if (live_dir/'KILL_SWITCH').exists() and 'OPERATOR_KILL_SWITCH' not in _ls['reason']:
                    live.j.halt('OPERATOR_KILL_SWITCH: no new entries; open position still managed')
                    print('RE5 KILL SWITCH: entries blocked; existing position protected until flat/settled',flush=True)
                if (live_dir/'KILL_SWITCH').exists() and not _ls['position'] and not _ls['pending']:
                    print('RE5 KILL SWITCH: flat, exiting',flush=True);return
                if os.environ.get('BIGBET_RE5_END_TIME') and now>=float(os.environ['BIGBET_RE5_END_TIME']):
                    if not _ls['position'] and not _ls['pending']:
                        print('RE5 SESSION TIME COMPLETE — FLAT',flush=True);return
                    if 'SESSION_END' not in _ls['reason']:
                        live.j.halt('SESSION_END: entries stopped; waiting for open position to close')
                live.poll_settlement()
                if now-live_status_at>=300:
                    print(live.status_line(),flush=True);live_status_at=now
            if now-last_settle>45:settle(client,risk_db);last_settle=now
            if cached_market is None or now-cached_market_at>=20 or base.iso(cached_market['close_time'])<=now:
                cached_market=current_eth_market(client,now)
                cached_market_at=now
            market=cached_market
            if live is not None and market:
                # Live mode: fresh market quotes every poll (paper caches up to 20 s).
                market=client.request('GET','/markets/'+market['ticker'])['market']
                if market.get('status')!='active':market=None
            if not market:
                if now-last_discovery_msg>=30:
                    print(f'WAITING_FOR_ACTIVE_ETH_MARKET utc={nowutc()} (unfiltered discovery; future initialized markets ignored)',flush=True)
                    last_discovery_msg=now
                time.sleep(3);continue
            ticker=market['ticker'];end=base.iso(market['close_time']);rem=end-now
            if ticker!=last_market:
                print(f'\nMARKET {ticker} | strike ${market.get("floor_strike","not yet published")}',flush=True)
                last_market=ticker
            if market.get('floor_strike') is None:
                if ticker not in missing_strike_logged:
                    print(f'WAITING_FOR_STRIKE ticker={ticker} (metadata pending; trading blocked)',flush=True)
                    missing_strike_logged.add(ticker)
                cached_market_at=0  # Re-fetch detail/strike before permitting a checkpoint
                time.sleep(2);continue
            if not 0<rem<=900:time.sleep(3);continue
            price,ts=client.ethrti()
            regime.add(ts,price)
            if ticker not in history:
                history[ticker]=dict(close=end,minutes=preload_market_history(end,preloaded),ticks=[(t,p) for t,p,_ in preloaded if t>=ts-180],partial=[])
                if history[ticker]['minutes']:print(f'HISTORY_PRELOADED ticker={ticker} minute_slots={len(history[ticker]["minutes"])}',flush=True)
            feats=features(history[ticker],ts,price,D(market['floor_strike']),rem)
            ready=int(feats['minute_pairs'])>=8
            status=(ticker,ready,int(feats['minute_pairs']))
            if status not in readiness_logged:
                readiness_logged.add(status)
                print(f'HISTORY_READINESS ticker={ticker} {"READY" if ready else "NOT READY"} minute_pairs={feats["minute_pairs"]}/8',flush=True)
                record_readiness(ticker,dict(time_utc=nowutc(),minute_pairs=feats['minute_pairs']), 'recent_local_ETHRTI_preload+live' if preloaded else 'live_only',history[ticker])
            if 0<rem<=900:
                # ETH V1.9.4: previous market's mood decides this market's rules
                reg,reg_vol=regime.for_market(end,now)
                reg_trade,reg_eh_gap,reg_steady=regime_rules(reg)
                if (ticker,reg) not in regime_logged:
                    regime_logged.add((ticker,reg))
                    _vtxt=f'{reg_vol:.3f}% per minute' if reg_vol is not None else 'not enough data'
                    _rtxt={'HIGH':'NO new entries this market','CALM':f'calm rules: Early-Hold lead >= {reg_eh_gap}x, steady >= {int(reg_steady*100)}c',
                           'UNKNOWN':f'V1.9.2 rules: Early-Hold lead >= {reg_eh_gap}x, steady >= {int(reg_steady*100)}c'}[reg]
                    print(f'MOOD_REGIME ticker={ticker} previous market {_vtxt} -> {reg}: {_rtxt}',flush=True)
                gap=price-D(market['floor_strike'])
                quotes={}
                for side in ('yes','no'):
                    # Both quotes are fetched for every poll; open stops are checked after both reads.
                    ask,depth,source=base.executable_ask(market,side)
                    quotes[side]=(ask,depth,source)
                    _bid=market.get(f'{side}_bid_dollars')
                    raw=dict(time_utc=nowutc(),ticker=ticker,seconds_left=f'{rem:.2f}',side=side,eth=price,strike=market['floor_strike'],gap_abs=abs(gap),gap_signed=gap,ask=str(ask) if ask is not None else '',quoted_depth=str(depth) if depth is not None else '',quote_source=source,**feats)
                    raw['bid']='' if _bid in (None,'') else str(_bid)
                    try:raw['spread']=str(D(raw['ask'])-D(raw['bid'])) if raw['ask'] and raw['bid'] else ''
                    except Exception:raw['spread']=''
                    add(RAW,RAW_FIELDS,raw)
                    # ETH V1.4 Early-Hold: remember this side's bid; qualify only in the 420-240 s window
                    _h=bid_hist.setdefault((ticker,side),[])
                    try:_h.append((now,D(raw['bid']) if raw['bid'] else None))
                    except Exception:_h.append((now,None))
                    del _h[:-300]
                    # ETH V1.9: remember this side's ask for the steady-60-s filter (checked BEFORE adding this poll)
                    _ah=ask_hist.setdefault((ticker,side),[])
                    _steady=steady_ok(_ah,now,reg_steady)
                    try:_ah.append((now,D(str(ask)) if ask is not None else None))
                    except Exception:_ah.append((now,None))
                    del _ah[:-300]
                    eh_due=(EARLY_HOLD_TO<=rem<=EARLY_HOLD_FROM and (ticker,EARLY_HOLD_CP,side) not in seen
                            and early_hold_ok(_h,now) and checkpoint_candidate(raw,EARLY_HOLD_CP,reg_eh_gap)['status']=='SHADOW_CANDIDATE')
                    if eh_due:print(f'EARLY_HOLD ticker={ticker} side={side} {rem:.0f}s left: bid held >=90c for 2 min, lead {raw.get("scaled_gap")}x >= {reg_eh_gap}x',flush=True)
                    if rem>305 and not eh_due:continue
                    for ecp in EARLY_CHECKPOINTS:
                        ekey=(ticker,ecp,side)
                        if ekey in early_seen or abs(rem-ecp)>3:continue
                        early_seen.add(ekey)
                        erow=research_early(raw,ecp)
                        add(EARLY,EARLY_FIELDS,erow)
                        early_quotes[(ticker,side)]=erow
                        print(f'EARLY_SHADOW_ONLY checkpoint={ecp} ticker={ticker} side={side} ask={raw["ask"] or "none"} readiness={erow["readiness"]} status={erow["status"]}',flush=True)
                    diskey=(ticker,side)
                    if diskey not in disappearance_seen and 88<=rem<=92:
                        disappearance_seen.add(diskey)
                        track_early_disappearance(early_quotes.get(diskey),raw)
                    for cp in ((EARLY_HOLD_CP,) if eh_due else ())+CHECKPOINTS:
                        key=(ticker,cp,side)
                        if key in seen or (cp!=EARLY_HOLD_CP and abs(rem-cp)>3):continue
                        seen.add(key);c=checkpoint_candidate(raw,cp,reg_eh_gap)
                        if c['status']=='SHADOW_CANDIDATE' and not reg_trade:
                            c['status']='NOT_APPROVED';c['reason']=(c['reason']+';' if c['reason'] else '')+'AFTER_HIGH_MOOD'
                            print(f'SKIP_AFTER_HIGH_MOOD ticker={ticker} checkpoint={cp} side={side}: previous market was HIGH ({reg_vol:.3f}% per minute); no entry',flush=True)
                        if c['status']=='SHADOW_CANDIDATE' and any(end-900<=g<=end for g in glitch_at):
                            c['status']='NOT_APPROVED';c['reason']=(c['reason']+';' if c['reason'] else '')+'CONNECTION_GLITCH_THIS_MARKET'
                            print(f'SKIP_MARKET_AFTER_GLITCH ticker={ticker} checkpoint={cp}: a Kalshi connection problem happened in this 15-minute market; no entry',flush=True)
                        if c['status']=='SHADOW_CANDIDATE' and cp!=EARLY_HOLD_CP and not _steady:
                            c['status']='NOT_APPROVED';c['reason']=(c['reason']+';' if c['reason'] else '')+'NOT_STEADY_60S'
                            print(f'SKIP_NOT_STEADY ticker={ticker} checkpoint={cp} side={side}: our side was below {int(reg_steady*100)}c (or unseen) in the last 60 s; no entry',flush=True)
                        # BIGBET 1000: first qualifying entry is the ONLY paper intent.
                        # Subsequent qualifying checkpoint rows remain research alternatives.
                        if c['status']=='SHADOW_CANDIDATE':
                            try:
                                age=max(0.0,time.time()-float(ts))
                                receipt=risk_approve(risk_db,ticker,side,str(D(c['total_cost']).quantize(D('0.01'))),data_age_seconds=age,market_seconds_left=rem,kill_switch=os.environ.get('ETH_V42_KILL_SWITCH','0')=='1',contracts=int(c['contracts']))
                                c['status']='PAPER_APPROVED'
                                print(f'PAPER_APPROVED ticker={ticker} side={side} cost=${receipt["paper_cost_usd"]:.2f} ledger=persistent',flush=True)
                                MLOG.paper(ticker,f'{cp}s {side} ask {c.get("ask")} cost {receipt["paper_cost_usd"]:.2f}')
                                if live is not None:
                                    c['strike']=raw['strike']; c['gap_abs']=raw['gap_abs']; c['close_time']=end; c['exchange_index']=market.get('exchange_index')
                                    filled=live.entry(c)
                                    le=live.last_entry or {}
                                    if filled:
                                        # RE5.1: paper mirrors the REAL fill (quantity and fee-inclusive cost).
                                        real_cost=D(le['gross'])+D(le['fees'])
                                        risk_sync_to_live(risk_db,ticker,max(1,int(D(le['filled']).to_integral_value())),int((real_cost*100).to_integral_value()))
                                        c.update(contracts=str(le['filled']),total_cost=str(real_cost),ask=str(D(le['avg']).quantize(D('0.0001'))),
                                                 estimated_fee=str(le['fees']),profit_if_win=str(D(le['filled'])-real_cost),loss_if_lose=str(-real_cost))
                                        MLOG.live(ticker,'FILLED',f'{le["filled"]}@{le.get("avg")} cost {real_cost:.2f} cp {cp}')
                                        print(f'LIVE_FILLED ticker={ticker} side={side} contracts={le["filled"]} cost=${real_cost:.2f} (paper ledger synced to real fill)',flush=True)
                                    else:
                                        # RE5.1: no real fill -> paper must not count this trade; later checkpoints may retry.
                                        risk_remove_unfilled(risk_db,ticker)
                                        c['status']='LIVE_NO_FILL'
                                        c['reason']=(c['reason']+';' if c['reason'] else '')+'LIVE:'+str(le.get('reason',''))[:80]
                                        _tag='LIVE_REJECTED' if str(le.get('reason','')).startswith('rejected') else ('LIVE_SKIPPED' if le.get('reason') not in ('no fill',) else 'LIVE_NO_FILL')
                                        MLOG.live(ticker,_tag,f'cp {cp}: {le.get("reason","")}')
                                        print(f'{_tag} ticker={ticker} side={side} reason={le.get("reason","")} (paper intent withdrawn; next checkpoint may retry)',flush=True)
                            except RiskRejected as rejected:
                                c['status']='NOT_APPROVED'
                                c['reason']=(c['reason']+';' if c['reason'] else '')+str(rejected)
                                print(f'PAPER_NOT_APPROVED ticker={ticker} checkpoint={cp} reason={rejected}',flush=True)
                        MLOG.checkpoint(ticker,cp,side,c)
                        add(CAND,CAND_FIELDS,c)
                        print(f'{cp:3d}s | {side.upper():3} gap ${abs(gap):.2f} | ask {raw["ask"] or "unavailable"} | scaled {raw["scaled_gap"] or "?"} | {c["status"]} {c["reason"][:75]}',flush=True)
                MLOG.observe(ticker,end,market['floor_strike'],price,rem,quotes,market,feats.get('scaled_gap'))
                process_paper_stops(client,risk_db,market,ticker,rem,quotes,eth_price=price,eth_ts=ts,features_now=feats,history_now=history[ticker])
                if live is not None:
                    _pos=live.j.read()['position']
                    if _pos and _pos['ticker']==ticker:
                        held=_pos['held'];other='no' if held=='yes' else 'yes'
                        held_bid=market.get(f'{held}_bid_dollars')
                        held_bid=D(held_bid) if held_bid not in (None,'') else (1-D(quotes[other][0]) if quotes.get(other) and quotes[other][0] is not None else None)
                        live_reason,_ld=v7c_stop_reason(held,market['floor_strike'],price,ts,rem,feats,history[ticker],held_bid,ticker=ticker)
                        live.on_poll(ticker=ticker,seconds_left=rem,held_bid=held_bid,stop_reason=live_reason,
                                     flatten_requested=(live_dir/'FLATTEN').exists())
                live_errors=0;outage_start=None
                # V7-D telemetry only: after original V7-C decisions, no trading effects.
                try:
                    observer.submit(ticker,rem,risk_outstanding(risk_db),price,market['floor_strike'],
                                    checkpoint=any(abs(rem-cp)<=2 for cp in CHECKPOINTS+(15,)))
                except Exception:
                    pass  # instrumentation must never gate V7-C trading

                if rem<=180 and now-last_line>40:print(f'  {rem:.0f}s remaining | ETH ${price} | strike ${market["floor_strike"]} | tracking quotes',flush=True);last_line=now
            try:
                holding=any(r[0]==ticker for r in risk_outstanding(risk_db))
                if not holding and live is not None:
                    _p=live.j.read()['position'];holding=bool(_p and _p['ticker']==ticker)
            except Exception:
                holding=False
            time.sleep(poll_pause(holding,rem))
        except KeyboardInterrupt:
            summarize();print('Stopped; historical files preserved.',flush=True);return
        except Exception as e:
            add(ERRORS,ERROR_FIELDS,dict(time_utc=nowutc(),stage='run_loop',ticker=last_market or '',error_type=type(e).__name__,message=str(e)[:500]))
            if is_connection_glitch(e):glitch_at.append(time.time())
            if live is not None and is_connection_glitch(e) and not live.j.read()['pending']:
                outage_start=outage_start or time.time();waited=time.time()-outage_start
                if waited<OUTAGE_GRACE_S:
                    print(f'CONNECTION_GLITCH {type(e).__name__}: Kalshi or ETH price feed not usable for {waited:.0f}s; retrying, this market skipped (pause after {OUTAGE_GRACE_S}s)',flush=True)
                    time.sleep(3);continue
            print(f'FAIL CLOSED: {type(e).__name__}: {str(e)[:160]}',flush=True)
            if live is not None:
                live.failure(e);live_errors+=1
                if live.must_stop_process() or live_errors>=5:
                    raise SystemExit('RE5 HALTED: run RE5_RECONCILE.py before restarting')
                print('RE5 PROTECT MODE: entries blocked; open position still managed',flush=True)
                time.sleep(2);continue
            time.sleep(5)

# V12.7: short Kalshi connection problems (read timeouts, dropped connections, HTTP 429/5xx on reads).
OUTAGE_GRACE_S=120
def is_connection_glitch(e):
    import requests
    if isinstance(e,(requests.exceptions.Timeout,requests.exceptions.ConnectionError)):return True
    if isinstance(e,requests.exceptions.HTTPError):
        return getattr(getattr(e,'response',None),'status_code',None) in base.READ_RETRY_HTTP
    if type(e).__name__=='ApiFailure':      # RE5 exchange reads; OrderRejected / UnknownOrderState are NOT glitches
        m=str(e);return 'transport failure' in m or any(f'HTTP {c}' in m for c in base.READ_RETRY_HTTP)
    # ETH V1.9.1: a momentary stale/implausible ETHUSD_RTI reading from Kalshi's CF Benchmarks feed (Nov 14 11:52 AM:
    # one bad reading halted the session for 5 1/2 hours). The bad value is never used (it is rejected before any
    # decision); it now gets the same 120 s grace as a Kalshi blip, and the market it happened in is skipped.
    if isinstance(e,ValueError) and str(e).startswith('Stale or implausible'):return True
    return False

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['run','report','replay-v38','reconcile','compare'])
    args=p.parse_args()
    for path,fields in ((RAW,RAW_FIELDS),(CAND,CAND_FIELDS),(SUMMARY,OUT_FIELDS),(ERRORS,ERROR_FIELDS),(SETTLE_ALL,SETTLE_FIELDS),(EARLY,EARLY_FIELDS),(DISAPPEAR,DISAPPEAR_FIELDS),(HISTORY_AUDIT,HISTORY_FIELDS)):init_csv(path,fields)
    if args.command=='run':run()
    elif args.command=='replay-v38':replay()
    elif args.command=='reconcile':
        client=base.Client(auth=True)
        while True:
            before=len({r['ticker'] for r in read(SETTLE_ALL) if r['official_side'] in ('yes','no')})
            reconcile(client,limit=20,retry_seconds=0)
            after=len({r['ticker'] for r in read(SETTLE_ALL) if r['official_side'] in ('yes','no')})
            # one pass across completed markets, with a bounded number of requests
            attempted=len(read(SETTLE_ALL))
            if attempted>=len([v for v in _known_market_closes().values() if v<=time.time()-5]) or after==before:break
        summarize()
    elif args.command=='compare':compare_policies()
    else:summarize()
if __name__=='__main__':main()
