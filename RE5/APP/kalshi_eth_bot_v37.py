#!/usr/bin/env python3
"""Kalshi KXETH15M ETHRTI monitor, paper entry logger, and CSV historical backtester.

Default: PAPER mode. LIVE remains intentionally disabled.
Historical evaluation requires actual quote and ETHRTI minute/settlement records; no fabricated stats.
"""
import argparse
import base64
import csv
import ctypes
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from pathlib import Path
from math import erfc, sqrt
from statistics import NormalDist
from collections import deque

import requests

# ETH V1: the market series and price index come from RE5/asset.py (one place for the asset).
try:
    from RE5 import asset as ASSET
except ImportError:                       # running from a copied session/APP folder without PYTHONPATH
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from RE5 import asset as ASSET
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[4]))   # <data>/RE5_x/sessions/<stamp>/APP
        from RE5 import asset as ASSET

API = '/trade-api/v2'
READ_RETRY_HTTP = (429, 500, 502, 503, 504)   # V12.7: read-only retry on these
PROD = 'https://external-api.kalshi.com'
DEMO = 'https://external-api.demo.kalshi.co'
D = lambda v: Decimal(str(v))
SPEND = D('1.00')
MAX_PRICE = D('0.99')
# Standard KXETH15M taker fee estimate. Actual fees may differ by account,
# order type, fill grouping or future Kalshi fee schedule changes.
def estimate_fee(price, contracts=1):
    price = D(price)
    n = D(contracts)
    raw = D('0.07') * n * price * (D('1') - price)
    return raw.quantize(D('0.01'), rounding=ROUND_CEILING)

POLL = 2
MIN_SIDE_ASK = D('0.89')  # 89% or higher for the selected side; these are quote prices, not probabilities
UP_ENTRY_SECONDS = 150
DOWN_ENTRY_SECONDS = 150
MIN_ENTRY_SECONDS = 30
DASHBOARD_VERSION = 'v37-atomic-entry-audit-colors-paper'
MIN_NET_WIN_PROFIT = D('0.03')  # assumed fee-adjusted profit per contract; not an EV guarantee
MAX_CROSS_RISK = float(os.environ.get('KALSHI_MAX_CROSS_RISK', '0.10'))
if not 0.01 <= MAX_CROSS_RISK <= 0.30:
    raise ValueError('KALSHI_MAX_CROSS_RISK must be between 0.01 and 0.30')
MULTIPLIER = D(os.environ.get('KALSHI_VOLATILITY_MULTIPLIER', '2.5'))
if not D('1') <= MULTIPLIER <= D('6'):
    raise ValueError('KALSHI_VOLATILITY_MULTIPLIER must be between 1 and 6')
SAMPLES_FILE = Path('kalshi_ethrtiminute_samples.json')
MIN_ROLLING_PAIRS = 8  # >= 8 actual consecutive minute-to-minute movements; never invent prices
SPIKE_MULTIPLIER = D('2.5')  # recent 3 minute average versus earlier baseline
SIGNAL_JOURNAL = Path('kalshi_signal_decisions.csv')
DECISION_FIELDS = ['ticker','observed_utc','seconds_remaining','direction','eth','target',
                   'gap_dollars','required_gap_dollars','gap_progress_pct','gap_shortfall_dollars',
                   'rate_per_minute','sample_pairs','volatility_spike','target_crossed_last_180s',
                   'ask_dollars','available_contracts','candidate_contracts','reasons',
                   'paper_entry','settlement_result','candidate_side_won','hypothetical_gross_pnl',
                   'note']
CROSSING_FILE = Path('kalshi_target_crossings.json')
CROSSING_AUDIT_FILE = Path('kalshi_target_crossing_audit.csv')
CROSSING_AUDIT_FIELDS = ['ticker','observed_utc','ethrti_utc','seconds_remaining','event','previous_side','current_side','previous_eth','current_eth','target','ethrti_gap_seconds','crossing_window','note']
V35_DIAGNOSTICS = Path('kalshi_v35_entry_diagnostics.csv')
V35_FIELDS = ['ticker','observed_utc','seconds_remaining','side','eth','target','gap_dollars',
              'ask_dollars','quantity','estimated_fee','net_profit_if_win','net_loss_if_lost',
              'minimum_net_win_profit','crossings_total','crossings_last_180s',
              'seconds_since_latest_crossing','latest_crossing_direction','reasons',
              'eligible','actual_paper_entry','note']

V37_ENTRY_AUDIT = Path('kalshi_v37_atomic_entry_audit.csv')
V37_AUDIT_FIELDS = ['ticker','decision_utc','ethrti_utc','ethrti_age_seconds','seconds_remaining',
    'side','eth','target','gap_signed_dollars','gap_absolute_dollars',
    'volatility_dollars_per_minute','volatility_method','sample_pairs','sample_start_minute','sample_end_minute',
    'recent_3min_average','earlier_average','model_target_touch_risk','risk_limit',
    'required_gap','gap_pass','spike_detected','crossed_final_180s','crossings_all_market',
    'crossings_rolling_180s','crossings_final_market_180s','seconds_since_latest_crossing',
    'ask_dollars','available_contracts','quote_source','quantity','estimated_fee',
    'net_profit_if_win','net_loss_if_lost','minimum_net_profit','gate_side','gate_entry_window',
    'gate_samples','gate_gap','gate_spike','gate_final_crossing','gate_quote','gate_price',
    'gate_liquidity','gate_minimum_net_profit','all_gates_pass','decision_reasons',
    'note']

# Default to color only when Python is directly attached to a suitable terminal.
# Tee-Object makes stdout a pipe; prefer Start-Transcript to capture colorful output.
COLOR_SUCCESS = '\033[92m'
COLOR_WARNING = '\033[93m'
COLOR_FAILURE = '\033[91m'
COLOR_INFO = '\033[96m'

def emit_status(message, color='', *, enabled=False):
    if enabled and color:
        print(f'{color}{message}{RESET_COLOR}', flush=True)
    else:
        print(message, flush=True)


def atomic_entry_audit(market, price, ethrti_timestamp, remaining, decision, crossed,
                       crossings, *, quote_source='', decision_timestamp=None):
    """One frozen entry decision with exact input and per-gate evidence.

    `ethrti_utc` identifies the ETH source observation, not order execution time.
    Records paper-entry decisions, never an actual exchange fill.
    """
    observed = time.time() if decision_timestamp is None else decision_timestamp
    chk = decision['check']
    side = decision['side']
    ask = decision['ask']
    size = decision['size']
    count = decision['count']
    gap = decision['gap']
    stats = crossing_metrics(crossings, market['ticker'], observed_timestamp=observed)
    events = crossings.get(market['ticker'], {}).get('crossings') or []
    final_crossings = sum(1 for e in events if 0 < float(e.get('seconds_remaining', 99999)) <= 180)
    fee = estimate_fee(ask, count) if ask is not None and count else D('0')
    profit = (D(count) - D(ask)*count - fee) if ask is not None and count else None
    loss = (-D(ask)*count - fee) if ask is not None and count else None
    gates = {
        'side':bool(side),
        'entry_window':bool(side and in_entry_window(side, remaining)),
        'samples':chk is not None,
        'gap':bool(chk and chk['gap_pass']),
        'spike':bool(chk and not chk['spike']),
        'final_crossing':not crossed,
        'quote':ask is not None and size is not None,
        'price':bool(side and ask is not None and side_quote_allowed(side, ask) and ask <= MAX_PRICE),
        'liquidity':count > 0,
        'minimum_net_profit':bool(profit is not None and count and profit >= MIN_NET_WIN_PROFIT*count),
    }
    row = {
      'ticker':market['ticker'],
      'decision_utc':datetime.fromtimestamp(observed,timezone.utc).isoformat(),
      'ethrti_utc':datetime.fromtimestamp(ethrti_timestamp,timezone.utc).isoformat(),
      'ethrti_age_seconds':f'{observed-ethrti_timestamp:.3f}',
      'seconds_remaining':f'{remaining:.3f}', 'side':side or '',
      'eth':str(price),'target':str(market['floor_strike']),
      'gap_signed_dollars':str(gap), 'gap_absolute_dollars':str(abs(gap)),
      'volatility_dollars_per_minute':str(chk['rate']) if chk else '',
      'volatility_method':chk['method'] if chk else '',
      'sample_pairs':str(chk['pairs']) if chk else '',
      'sample_start_minute':str(chk['start_minute']) if chk else '',
      'sample_end_minute':str(chk['end_minute']) if chk else '',
      'recent_3min_average':str(chk.get('recent_rate','')) if chk else '',
      'earlier_average':str(chk.get('baseline_rate','')) if chk else '',
      'model_target_touch_risk':str(chk['cross_risk']) if chk else '',
      'risk_limit':str(MAX_CROSS_RISK),
      'required_gap':str(chk['required']) if chk else '',
      'gap_pass':str(gates['gap']), 'spike_detected':str(chk['spike']) if chk else '',
      'crossed_final_180s':str(crossed),'crossings_all_market':stats['total'],
      'crossings_rolling_180s':stats['recent_180s'],
      'crossings_final_market_180s':final_crossings,
      'seconds_since_latest_crossing':stats['seconds_since_last'] if stats['seconds_since_last'] is not None else '',
      'ask_dollars':str(ask) if ask is not None else '',
      'available_contracts':str(size) if size is not None else '',
      'quote_source':quote_source,'quantity':count,
      'estimated_fee':str(fee), 'net_profit_if_win':str(profit) if profit is not None else '',
      'net_loss_if_lost':str(loss) if loss is not None else '',
      'minimum_net_profit':str(MIN_NET_WIN_PROFIT),
      'all_gates_pass':str(all(gates.values())),
      'decision_reasons':';'.join(decision['reasons']) or 'ALL_CHECKS_PASS',
      'note':'One frozen paper decision; model uncalibrated; fees estimated; quote fill not guaranteed.'
    }
    for gate, passed in gates.items():
        row['gate_'+gate] = 'PASS' if passed else 'FAIL'
    append_csv(V37_ENTRY_AUDIT, V37_AUDIT_FIELDS, row)
    return row

# Distinguishable market headers on Windows Terminal / PowerShell (ANSI fallback).
MARKET_COLORS = ('\033[96m', '\033[93m', '\033[95m', '\033[92m')
RESET_COLOR = '\033[0m'

def enable_console_colors():
    if not sys.stdout.isatty():
        return False
    if os.name != 'nt':
        return True
    try:
        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False

def market_style(number, enabled):
    return MARKET_COLORS[(number - 1) % len(MARKET_COLORS)] if enabled else ''

def format_observation_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().strftime('%Y-%m-%d %I:%M:%S %p %Z')

def open_market_section(ticker, first_seen, number, enabled, close):
    color = market_style(number, enabled)
    reset = RESET_COLOR if enabled else ''
    print(f'\n{color}{"#" * 76}')
    print(f'MARKET #{number:03d} START | {ticker}')
    print(f'FIRST NEW OBSERVATION: {format_observation_time(first_seen)}')
    print(f'SCHEDULED MARKET CLOSE: {format_observation_time(close)}')
    print(f'NOTE: observation start means when THIS BOT first saw the market, not necessarily market open.')
    print(f'{"#" * 76}{reset}', flush=True)

def close_market_section(ticker, first_seen, last_seen, number, enabled, close, observation_count):
    color = market_style(number, enabled)
    reset = RESET_COLOR if enabled else ''
    elapsed = max(0, last_seen - first_seen)
    print(f'\n{color}{"#" * 76}')
    print(f'MARKET #{number:03d} FINISHED | {ticker}')
    print(f'FIRST NEW OBSERVATION: {format_observation_time(first_seen)}')
    print(f'LAST OBSERVATION RECEIVED: {format_observation_time(last_seen)}')
    print(f'SCHEDULED MARKET CLOSE: {format_observation_time(close)}')
    print(f'BOT OBSERVATION SPAN: {int(elapsed // 60)}m {int(elapsed % 60)}s | distinct minute samples: {observation_count}')
    print('NOTE: the last observed time may precede market close; it is not a settlement timestamp.')
    print(f'{"#" * 76}{reset}\n', flush=True)





# Diagnostic/shadow strategy: NEVER sends or creates a paper order.
# Collect unique fresh ETHRTI observations on each poll and log every early candidate.
TICK_FILE = Path('kalshi_ethrtiticks.csv')
SHADOW_FILE = Path('kalshi_momentum_candidates.csv')
EARLY_MAX_SECONDS = 300  # first test: up to five minutes before close
SHADOW_MIN_ASK = D('0.89')
SHADOW_MAX_ASK = D('0.95')
SHADOW_CONSISTENCY = 0.70
TICK_FIELDS = ['ticker','received_utc','ethrti_utc','eth','target','seconds_remaining','side','gap_dollars']
SHADOW_FIELDS = ['ticker','observed_utc','ethrti_utc','seconds_remaining','side','eth','target',
    'ask_dollars','quoted_liquidity','paper_size','net_if_win','net_if_loss',
    'gap_dollars','rate_15s','rate_30s','rate_60s','rate_90s','consistency_60s',
    'reversals_60s','sample_count_90s','crossed_last_180s','touch_model_risk',
    'required_gap_dollars','reasons','shadow_signal','settlement_result','would_win','net_if_settled']

def append_csv(path, fields, row):
    new = not path.exists() or path.stat().st_size == 0
    with path.open('a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new: writer.writeheader()
        writer.writerow(row)

def add_tick(ticks, ticker, price, ethrti_time, received, target, remaining):
    # Avoid mistaking duplicate data-provider timestamps for new price changes.
    if ticks and ethrti_time <= ticks[-1][0]:
        return False
    ticks.append((ethrti_time, D(price)))
    while ticks and ticks[0][0] < ethrti_time - 105:
        ticks.popleft()
    gap = D(price) - D(target)
    append_csv(TICK_FILE, TICK_FIELDS, {
        'ticker':ticker,'received_utc':datetime.fromtimestamp(received,timezone.utc).isoformat(),
        'ethrti_utc':datetime.fromtimestamp(ethrti_time,timezone.utc).isoformat(),
        'eth':str(price),'target':str(target),'seconds_remaining':f'{remaining:.1f}',
        'side':'yes' if gap>0 else 'no' if gap<0 else 'at_target',
        'gap_dollars':str(abs(gap))})
    return True

def momentum_window(ticks, seconds, side):
    if len(ticks)<2: return None
    end_time, end_price = ticks[-1]
    # Must span the requested horizon, without interpolating missing quotes.
    eligible = [i for i,(t,_) in enumerate(ticks) if t <= end_time-seconds]
    if not eligible: return None
    idx = eligible[-1]
    window = list(ticks)[idx:]
    if any(b[0]-a[0]>8 for a,b in zip(window,window[1:])):
        return None
    duration=end_time-window[0][0]
    if duration<=0: return None
    sign=1 if side=='yes' else -1
    diffs = [sign*float(b[1]-a[1]) for a,b in zip(window,window[1:])]
    nonzero=[v for v in diffs if v!=0]
    return {'rate':sign*float(end_price-window[0][1])*60/duration,
            'consistency': sum(d>0 for d in nonzero)/len(nonzero) if nonzero else 0,
            'reversals':sum(d<0 for d in diffs), 'samples':len(window)}

def shadow_snapshot(market, price, ethrti_time, remaining, ticks, readings, crossed):
    # Called for real fresh ticks only. Shadow candidates are *not* paper orders.
    if not 30<=remaining<=EARLY_MAX_SECONDS:return None
    target=D(market['floor_strike'])
    gap=D(price)-target
    if gap==0:return None
    side='yes' if gap>0 else 'no'
    ask, size, _=executable_ask(market,side)
    if ask is None or not SHADOW_MIN_ASK<=D(ask)<=SHADOW_MAX_ASK:return None
    windows={n:momentum_window(ticks,n,side) for n in (15,30,60,90)}
    check=evaluate_crossing_risk(readings,target,price,remaining)
    qty=quantity(ask,size) if size is not None else 0
    reasons=[]
    if qty<1: reasons.append('NO_AFFORDABLE_SIZE')
    if crossed: reasons.append('CROSSED_LAST_3_MIN')
    if any(windows[n] is None for n in (30,60,90)):reasons.append('INSUFFICIENT_FRESH_MOMENTUM')
    else:
        if windows[30]['rate']<=0 or windows[60]['rate']<=0 or windows[90]['rate']<0:
            reasons.append('MOMENTUM_NOT_AWAY_FROM_TARGET')
        if windows[60]['consistency']<SHADOW_CONSISTENCY:
            reasons.append('MOMENTUM_INCONSISTENT')
    if check is None:reasons.append('INSUFFICIENT_MINUTE_VOLATILITY')
    else:
        if not check['gap_pass']:reasons.append('TOUCH_RISK_OVER_10_PERCENT')
        if check['spike']:reasons.append('VOLATILITY_SPIKE')
    eligible=not reasons
    fee=estimate_fee(ask,qty) if qty else D('0')
    cost=D(ask)*qty+fee
    row={'ticker':market['ticker'],
         'observed_utc':datetime.now(timezone.utc).isoformat(),
         'ethrti_utc':datetime.fromtimestamp(ethrti_time,timezone.utc).isoformat(),
         'seconds_remaining':f'{remaining:.1f}', 'side':side,'eth':str(price),
         'target':str(target),'ask_dollars':str(ask), 'quoted_liquidity':str(size),
         'paper_size':str(qty),'net_if_win':str(D(qty)-cost) if qty else '',
         'net_if_loss':str(-cost) if qty else '', 'gap_dollars':str(abs(gap)),
         'consistency_60s':f'{windows[60]["consistency"]:.3f}' if windows[60] else '',
         'reversals_60s':str(windows[60]['reversals']) if windows[60] else '',
         'sample_count_90s':str(windows[90]['samples']) if windows[90] else '',
         'crossed_last_180s':str(crossed),
         'touch_model_risk':f'{check["cross_risk"]:.5f}' if check else '',
         'required_gap_dollars':str(check['required']) if check else '',
         'reasons':';'.join(reasons) if reasons else 'SHADOW_CHECKS_PASS',
         'shadow_signal':str(eligible), 'settlement_result':'',
         'would_win':'','net_if_settled':''}
    for n in (15,30,60,90):
        row[f'rate_{n}s']=f'{windows[n]["rate"]:+.3f}' if windows[n] else ''
    append_csv(SHADOW_FILE,SHADOW_FIELDS,row)
    return row

def settle_shadow_candidates(client):
    if not SHADOW_FILE.exists():return
    with SHADOW_FILE.open(newline='',encoding='utf-8') as f:rows=list(csv.DictReader(f))
    if not rows:return
    updated=False
    for ticker in {r['ticker'] for r in rows if not r.get('settlement_result')}:
        try:
            m=client.request('GET','/markets/'+ticker)['market']
            if time.time()<iso(m['close_time'])+30:continue
            result=settlement_side(m)
            if not result:continue
        except (requests.RequestException,KeyError,ValueError):continue
        for row in rows:
            if row['ticker']!=ticker or row.get('settlement_result'):continue
            won=row['side']==result
            row['settlement_result']=result
            row['would_win']=str(won)
            if row.get('paper_size') and int(row['paper_size'])>0:
                qty=int(row['paper_size']);price=D(row['ask_dollars'])
                fee=estimate_fee(price,qty)
                row['net_if_settled']=str((D(qty) if won else D('0'))-qty*price-fee)
            updated=True
    if updated:
        tmp=SHADOW_FILE.with_suffix('.csv.tmp')
        with tmp.open('w',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=SHADOW_FIELDS)
            writer.writeheader();writer.writerows(rows)
        os.replace(tmp,SHADOW_FILE)
        print('Updated momentum shadow candidates with official outcomes.',flush=True)



# V3.3 RESEARCH: all signals are hypothetical; only the original strategy can
# write to the existing $1 paper ledger. No network orders are placed.
from bisect import bisect_right
from math import log, exp, isfinite

RESEARCH_TICKS = Path('kalshi_continuous_ethrti.csv')
RESEARCH_QUOTES = Path('kalshi_quote_history.csv')
RESEARCH_FILE = Path('kalshi_research_decisions.csv')
RESEARCH_STATE = Path('kalshi_research_state.json')
RESEARCH_MAX_AGE = 420
RESEARCH_FIELDS = ['ticker','observed_utc','ethrti_utc','seconds_remaining','strategy',
    'side','eth','target','gap_dollars','ask_dollars','bid_dollars','available_contracts',
    'contracts','fee_estimate','entry_cost','profit_if_win','loss_if_lost',
    'price_15s_per_min','price_30s_per_min','price_60s_per_min','price_90s_per_min',
    'price_180s_per_min','consistency_60s','reversals_60s','recent_vol_60s',
    'baseline_vol_180s','distance_vol_ratio','touch_risk_exploratory',
    'settlement_prob_exploratory','model_minus_price','uncertainty_buffer',
    'lower_bound_exploratory','break_even_probability','edge_lower_minus_break_even',
    'model_status','decision','reasons','selected_for_shadow',
    'settlement_result','would_win','hypothetical_net_pnl']
QUOTE_FIELDS=['ticker','observed_utc','ethrti_utc','seconds_remaining','eth','target',
    'yes_bid','yes_ask','yes_ask_liquidity','no_bid','no_ask','no_ask_liquidity',
    'yes_quote_source','no_quote_source']
CONT_FIELDS=['ethrti_utc','received_utc','eth','ticker_at_receipt']


def read_csv_rows(path):
    if not path.exists():return []
    with path.open(newline='',encoding='utf-8') as f:return list(csv.DictReader(f))


def write_csv_rows(path, fields, rows):
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
    os.replace(tmp,path)


def load_continuous_history():
    ticks=deque()
    last=float('-inf')
    for r in read_csv_rows(RESEARCH_TICKS):
        try:
            ts=datetime.fromisoformat(r['ethrti_utc']).timestamp()
            if ts>last:
                ticks.append((ts,D(r['eth'])));last=ts
        except (ValueError,KeyError):continue
    return ticks


def collect_research_tick(history,ticker,price,ethrti_ts,received):
    if history and ethrti_ts<=history[-1][0]:return False
    history.append((ethrti_ts,D(price)))
    while history and history[0][0]<ethrti_ts-RESEARCH_MAX_AGE:history.popleft()
    append_csv(RESEARCH_TICKS,CONT_FIELDS,{
        'ethrti_utc':datetime.fromtimestamp(ethrti_ts,timezone.utc).isoformat(),
        'received_utc':datetime.fromtimestamp(received,timezone.utc).isoformat(),
        'eth':str(price),'ticker_at_receipt':ticker})
    return True


def window_research(history, seconds, side):
    if len(history)<3:return None
    end=history[-1][0]
    items=list(history)
    before=[i for i,(t,_) in enumerate(items) if t<=end-seconds]
    if not before:return None
    data=items[before[-1]:]
    if any(b[0]-a[0]>8 or b[0]<=a[0] for a,b in zip(data,data[1:])):return None
    span=data[-1][0]-data[0][0]
    if span<=0:return None
    sign=1 if side=='yes' else -1
    delta=[float(b[1]-a[1]) for a,b in zip(data,data[1:])]
    directed=[sign*d for d in delta]
    nonzero=[d for d in directed if d]
    return {'rate':sign*float(data[-1][1]-data[0][1])*60/span,
        'vol':sum(abs(d) for d in delta)*60/span,
        'consistency':sum(x>0 for x in nonzero)/len(nonzero) if nonzero else 0,
        'reversals':sum(x<0 for x in nonzero),'count':len(data),
        'span':span}


def exploratory_probability(gap,remaining,short_window,long_window,side):
    # Simple Brownian terminal-return approximation; NOT calibrated and NOT
    # suitable to justify real-money risk. Shrink observed directional drift.
    if not short_window or not long_window or remaining<=0:return None
    # Estimate 1-minute standard deviation from 2-second increments by
    # aggregating 60-second normalized realized squared increments.
    sigma=max(2.0,float(long_window['vol'])*1.25)
    drift=max(-sigma,min(sigma,short_window['rate']))*0.20
    z=(float(gap)+drift*remaining/60)/(sigma*sqrt(max(remaining/60,0.01)))
    return max(0.00001,min(0.99999,NormalDist().cdf(z)))


def research_rows(market, price, ethrti_ts, remaining, history, crossed):
    if not 30<=remaining<=300:return []
    target=D(market['floor_strike']);gap=D(price)-target
    if gap==0:return []
    side='yes' if gap>0 else 'no'
    ask,size,src=executable_ask(market,side)
    if ask is None or size is None:return []
    ask=D(ask)
    if not D('0.89')<=ask<=D('0.95'):return []
    bid=D(str(market.get('yes_bid_dollars') or '0')) if side=='yes' else D(str(market.get('no_bid_dollars') or '0'))
    qty=quantity(ask,size)
    windows={n:window_research(history,n,side) for n in (15,30,60,90,180)}
    w60=windows[60];w180=windows[180]
    prob=exploratory_probability(abs(gap),remaining,w60,w180,side)
    # Conservatively widen uncertainty at early research stage; this is a
    # heuristic buffer, NOT a statistical confidence interval.
    uncertainty=0.05
    lower=max(0,prob-uncertainty) if prob is not None else None
    fee=estimate_fee(ask,qty) if qty else D('0')
    cost=ask*qty+fee
    break_even=float(cost/D(qty)) if qty else None
    common=[]
    if qty<1:common.append('NO_AFFORDABLE_SIZED_QUOTE')
    if crossed:common.append('TARGET_CROSSED_RECENTLY')
    if any(windows[n] is None for n in (30,60,90,180)):
        common.append('INCOMPLETE_FRESH_ETHRTI_HISTORY')
    else:
        if windows[30]['rate']<=0 or windows[60]['rate']<=0 or windows[90]['rate']<0:
            common.append('MOMENTUM_NOT_AWAY_FROM_STRIKE')
        if w60['consistency']<0.70:common.append('MOMENTUM_INCONSISTENT')
        if w60['reversals']>max(4,int(w60['count']*.3)):
            common.append('MANY_SHORT_REVERSALS')
        if w60['vol']>1.8*w180['vol']:
            common.append('VOLATILITY_SPIKE')
    # Maintain a strict distinction between provisional predictions and evidence
    # of calibration. Value C cannot claim a positive edge without external
    # validation and sufficient independent completed markets.
    result=[]
    now_iso=datetime.now(timezone.utc).isoformat()
    ethrti_iso=datetime.fromtimestamp(ethrti_ts,timezone.utc).isoformat()
    for label in ('B_MOMENTUM','C_VALUE'):
        reasons=list(common)
        if label=='C_VALUE':
            if prob is None:reasons.append('PROBABILITY_UNAVAILABLE')
            elif lower<=break_even+0.02:reasons.append('INSUFFICIENT_CONSERVATIVE_VALUE')
            reasons.append('MODEL_NOT_CALIBRATED_NO_VALUE_TRADING')
        valid=not reasons
        row=dict.fromkeys(RESEARCH_FIELDS,'')
        row.update({'ticker':market['ticker'],'observed_utc':now_iso,'ethrti_utc':ethrti_iso,
            'seconds_remaining':f'{remaining:.1f}','strategy':label,'side':side,
            'eth':str(price),'target':str(target),'gap_dollars':str(abs(gap)),
            'ask_dollars':str(ask),'bid_dollars':str(bid),'available_contracts':str(size),
            'contracts':str(qty),'fee_estimate':str(fee),'entry_cost':str(cost) if qty else '',
            'profit_if_win':str(D(qty)-cost) if qty else '',
            'loss_if_lost':str(-cost) if qty else '',
            'consistency_60s':f'{w60["consistency"]:.4f}' if w60 else '',
            'reversals_60s':w60['reversals'] if w60 else '',
            'recent_vol_60s':f'{w60["vol"]:.4f}' if w60 else '',
            'baseline_vol_180s':f'{w180["vol"]:.4f}' if w180 else '',
            'distance_vol_ratio':f'{float(abs(gap))/max(1,w180["vol"]):.4f}' if w180 else '',
            'settlement_prob_exploratory':f'{prob:.5f}' if prob is not None else '',
            'model_minus_price':f'{prob-float(ask):+.5f}' if prob is not None else '',
            'uncertainty_buffer':uncertainty,
            'lower_bound_exploratory':f'{lower:.5f}' if lower is not None else '',
            'break_even_probability':f'{break_even:.5f}' if break_even is not None else '',
            'edge_lower_minus_break_even':f'{lower-break_even:+.5f}' if lower is not None and break_even is not None else '',
            'model_status':'EXPLORATORY_UNCALIBRATED',
            'decision':'PAPER_CANDIDATE' if valid else 'REJECT',
            'reasons':';'.join(reasons) if reasons else 'MOMENTUM_CHECKS_PASS',
            'selected_for_shadow':'False'})
        for n in (15,30,60,90,180):
            row[f'price_{n}s_per_min']=f'{windows[n]["rate"]:+.4f}' if windows[n] else ''
        result.append(row)
    return result


def settle_research(client):
    rows=read_csv_rows(RESEARCH_FILE)
    if not rows:return
    pending={r['ticker'] for r in rows if not r['settlement_result']}
    results={}
    for ticker in pending:
        try:
            market=client.request('GET','/markets/'+ticker)['market']
            if time.time()<iso(market['close_time'])+30:continue
            side=settlement_side(market)
            if side:results[ticker]=side
        except (requests.RequestException,KeyError,ValueError):continue
    if not results:return
    for row in rows:
        outcome=results.get(row['ticker'])
        if not outcome or row['settlement_result']:continue
        row['settlement_result']=outcome
        win=row['side']==outcome
        row['would_win']=str(win)
        if row.get('contracts') and int(row['contracts'])>0:
            row['hypothetical_net_pnl']=str((D(row['contracts']) if win else D('0'))-D(row['entry_cost']))
    write_csv_rows(RESEARCH_FILE,RESEARCH_FIELDS,rows)


def research_report():
    rows=read_csv_rows(RESEARCH_FILE)
    print('RESEARCH REPORT: recorded shadow candidates only; not fills or a validated strategy')
    if not rows:
        print('No observations yet. Run the bot during 89-95 cent opportunities.');return
    for strategy in ('B_MOMENTUM','C_VALUE'):
        items=[r for r in rows if r['strategy']==strategy]
        selected=[r for r in items if r['selected_for_shadow']=='True']
        resolved=[r for r in selected if r['settlement_result'] in ('yes','no')]
        wins=sum(r['would_win']=='True' for r in resolved)
        pnl=sum((D(r['hypothetical_net_pnl']) for r in resolved if r['hypothetical_net_pnl']),D('0'))
        print(f'{strategy}: snapshots={len(items)}, selected_markets={len(selected)}, settled={len(resolved)}, wins={wins}, hypothetical_net_PnL=${pnl:+.4f}')
        print('  Note: these are not independent samples beyond the number of distinct settled markets.')
    print('Model C will never place a trade or claim calibration without independent evidence.')


def iso(v):
    return datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp()


def volatility_estimate(prices, remaining):
    """Use exact early-window history when available, else >=8 consecutive real pairs.

    At T-150s, minute 13 has not occurred. The longest usable full sequence
    is minute 0..12. At T-120s minute 13 is normally available. This is an
    observed-price strategy, NOT a guaranteed risk bound or a synthetic backfill.
    """
    latest_allowed = 12 if remaining > 120 else 13
    # Contiguous suffix, not an average across missing minutes.
    available = [i for i in range(latest_allowed + 1) if i in prices]
    if not available:
        return None
    end = max(available)
    first = end
    while first - 1 in prices:
        first -= 1
    pairs = end - first
    if pairs < MIN_ROLLING_PAIRS:
        return None
    rate = sum(abs(D(prices[i]) - D(prices[i-1])) for i in range(first+1, end+1)) / pairs
    if rate <= 0:
        return None
    return {'rate': rate, 'pairs': pairs, 'start_minute': first,
            'end_minute': end, 'complete': first == 0 and end == 13,
            'method': 'full_13_minute' if first == 0 and end == 13 else 'rolling_observed'}


def evaluate_crossing_risk(prices, target, current, remaining):
    """Estimate the size of a possible reversal using only observations already received.

    The estimate is not a statistical probability or a price guarantee.
    Returns details even when the gap fails, for dashboard diagnostics.
    """
    est = volatility_estimate(prices, remaining)
    if est is None:
        return None
    gap = D(current) - D(target)
    # PROVISIONAL MODEL: independent zero-drift Gaussian minute changes.
    # Estimate sigma from mean absolute one-minute moves for Gaussian returns:
    # E|X| = sigma * sqrt(2/pi). This is NOT historically calibrated.
    # Reflection principle approximates probability of EVER touching the target
    # by expiration, which is more conservative than just finishing across it.
    horizon_minutes = D(str(max(remaining, 0))) / D('60')
    sigma = float(est['rate']) * sqrt(3.141592653589793 / 2)
    horizon = float(horizon_minutes)
    gap_abs = abs(float(gap))
    risk = erfc(gap_abs / (sigma * sqrt(2 * horizon))) if horizon > 0 and sigma > 0 else 0.0
    # Invert the same risk model to show required separation in dollars.
    z = NormalDist().inv_cdf(1 - MAX_CROSS_RISK / 2)
    required = D(str(z * sigma * sqrt(horizon)))
    changes = [abs(D(prices[i]) - D(prices[i - 1]))
               for i in range(est['start_minute'] + 1, est['end_minute'] + 1)]
    recent = sum(changes[-3:], D('0')) / D('3')
    baseline = sum(changes[:-3], D('0')) / D(len(changes) - 3)
    # Reject abrupt recent movement bursts relative to earlier same-market moves.
    spike = baseline <= 0 or recent >= SPIKE_MULTIPLIER * baseline
    return {'side': 'yes' if gap > 0 else 'no' if gap < 0 else None,
            'gap': gap, 'rate': est['rate'], 'required': required,
            'remaining_minutes': horizon_minutes, 'recent_rate': recent,
            'baseline_rate': baseline, 'spike': spike,
            'gap_pass': bool(gap != 0 and risk <= MAX_CROSS_RISK),
            'cross_risk': risk, 'risk_limit': MAX_CROSS_RISK,
            'model': 'Gaussian first-passage approximation (UNVALIDATED)', **est}


def strategy(prices, target, current, remaining=120):
    """Entry signal only if price-crossing buffer passes and no volatility spike."""
    check = evaluate_crossing_risk(prices, target, current, remaining)
    return check if check and check['gap_pass'] and not check['spike'] else None


def in_entry_window(side, remaining):
    upper = UP_ENTRY_SECONDS if side == 'yes' else DOWN_ENTRY_SECONDS
    return MIN_ENTRY_SECONDS <= remaining <= upper


def side_quote_allowed(side, price):
    # Both UP and DOWN buy asks must be at least 89 cents.
    # This is a market-price condition, not a verified win probability.
    return side in ('yes', 'no') and D(price) >= MIN_SIDE_ASK


def quantity(price, liquidity):
    """Whole-contract count under $1, including estimated taker fees."""
    price = D(price)
    if not D('0') < price <= MAX_PRICE:
        return 0
    cap = min(int(D(liquidity)), int((SPEND / price).to_integral_value(rounding=ROUND_FLOOR)))
    for n in range(max(0, cap), 0, -1):
        if price * n + estimate_fee(price, n) <= SPEND:
            return n
    return 0


class Client:
    def __init__(self, demo=False, auth=False):
        self.host = DEMO if demo else PROD
        self.http = requests.Session()
        self.keyid = os.environ.get('KALSHI_API_KEY_ID')
        self.private = None
        if auth:
            key_path = os.environ.get('KALSHI_PRIVATE_KEY_PATH')
            if not self.keyid or not key_path:
                raise RuntimeError('Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH')
            from cryptography.hazmat.primitives import serialization
            self.private = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)

    def request(self, method, path, params=None, payload=None, auth=False):
        headers = {}
        if auth:
            if not self.private:
                raise RuntimeError('Authenticated endpoint requires Kalshi credentials')
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding, ed25519
            ts = str(int(time.time() * 1000))
            msg = (ts + method + API + path).encode()
            if isinstance(self.private, ed25519.Ed25519PrivateKey):
                signature = self.private.sign(msg)
            else:
                signature = self.private.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
            headers = {'KALSHI-ACCESS-KEY': self.keyid,
                       'KALSHI-ACCESS-TIMESTAMP': ts,
                       'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode()}
        # Never automatically retry POST/DELETE (would risk duplicate orders).
        # V12.7: GET (read-only) retries up to 3 more times on a timeout, dropped connection or
        # HTTP 429/500/502/503/504, waiting 0.5 s, 1 s, 2 s. Each try is freshly signed.
        attempts = 4 if method == 'GET' else 1
        for attempt in range(attempts):
            last = attempt + 1 >= attempts
            if auth and attempt:
                ts = str(int(time.time() * 1000)); msg = (ts + method + API + path).encode()
                if isinstance(self.private, ed25519.Ed25519PrivateKey):
                    signature = self.private.sign(msg)
                else:
                    signature = self.private.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
                headers = {'KALSHI-ACCESS-KEY': self.keyid, 'KALSHI-ACCESS-TIMESTAMP': ts,
                           'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode()}
            try:
                r = self.http.request(method, self.host+API+path, params=params,
                                      json=payload, headers=headers, timeout=8)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if last:
                    raise
                time.sleep(0.5 * 2 ** attempt); continue
            if r.status_code in READ_RETRY_HTTP and not last:
                time.sleep(0.5 * 2 ** attempt); continue
            r.raise_for_status()
            return r.json()

    def ethrti(self):
        # ETH: ETHUSD_RTI, the CF Benchmarks real-time index Kalshi settles ETH markets on.
        index_id = ASSET.INDEX_ID
        raw = self.request('GET', '/cfbenchmarks/latest_values', {'id': index_id}, auth=True)
        # Strict schema: never silently use another index or local exchange prices.
        item = raw['data']['payload']['latest_values'][index_id]
        price, timestamp = D(item['value']), int(item['time']) / 1000
        if not ASSET.plausible_price(price) or abs(time.time()-timestamp) > 5:
            raise ValueError(f'Stale or implausible {index_id} value; aborting signal')
        return price, timestamp

    def market(self):
        data = self.request('GET','/markets', {'series_ticker':ASSET.SERIES, 'status':'open', 'limit':100})
        now = time.time()
        candidates = [m for m in data.get('markets', [])
                      if m.get('floor_strike') is not None and
                      0 < iso(m['close_time'])-now <= 900]
        if not candidates:
            return None
        m = min(candidates, key=lambda x: iso(x['close_time']))
        return self.request('GET','/markets/'+m['ticker'])['market']

    def execute(self, ticker, side, count, ask, client_order_id):
        # V2 quotes from YES book: buying NO is an ASK for YES at (1 - no_ask).
        yes_price = D(ask) if side == 'yes' else D('1')-D(ask)
        payload = {'ticker':ticker, 'client_order_id':client_order_id,
                   'side':'bid' if side=='yes' else 'ask',
                   'count':f'{count}.00','price':f'{yes_price:.4f}',
                   'time_in_force':'immediate_or_cancel',
                   'self_trade_prevention_type':'taker_at_cross',
                   'post_only':False,'cancel_order_on_pause':True,
                   'reduce_only':False, 'exchange_index':-1}
        return self.request('POST','/portfolio/events/orders',payload=payload,auth=True)


def minute_bucket(ts, start):
    """Capture the first fresh ETHRTI tick received anywhere in each minute.

    This avoids missing an entire minute when the endpoint responds 4+ seconds
    late. It does not manufacture prices for minutes that were never observed.
    """
    elapsed = ts-start
    i = int(elapsed//60)
    return i if 0 <= i <= 13 else None


def load_samples():
    if not SAMPLES_FILE.exists():
        return {}
    try:
        raw = json.loads(SAMPLES_FILE.read_text(encoding='utf-8'))
        return {ticker: {int(k): D(v) for k, v in readings.items()}
                for ticker, readings in raw.items()}
    except (ValueError, TypeError, OSError) as exc:
        print('WARNING: Could not restore ETHRTI samples:', exc, flush=True)
        return {}


def save_samples(records):
    temp = SAMPLES_FILE.with_suffix('.tmp')
    serialized = {ticker: {str(k): str(v) for k, v in readings.items()}
                  for ticker, readings in records.items()}
    temp.write_text(json.dumps(serialized, indent=2), encoding='utf-8')
    os.replace(temp, SAMPLES_FILE)


def executable_ask(market, side):
    """Resolve the buy-side quote and available quantity.

    Buying YES consumes the YES ask / NO bid; buying NO consumes the NO ask /
    YES bid. A market summary does not always expose size on both sides.
    Returns (ask_price, available_contracts, source), or (None, None, reason).
    """
    direct_ask = market.get(f'{side}_ask_dollars')
    direct_size = market.get(f'{side}_ask_size_fp')
    if direct_ask not in (None, '') and direct_size not in (None, ''):
        try:
            if D(direct_ask) > 0 and D(direct_size) > 0:
                return D(direct_ask), D(direct_size), 'direct market ask'
        except (ValueError, ArithmeticError):
            pass
    opposite = 'no' if side == 'yes' else 'yes'
    bid = market.get(f'{opposite}_bid_dollars')
    size = market.get(f'{opposite}_bid_size_fp')
    if bid in (None, '') or size in (None, ''):
        return None, None, 'no sized direct ask or opposite-side bid; orderbook required'
    try:
        price = D('1')-D(bid)
        available = D(size)
        if D('0') < price <= D('1') and available > 0:
            return price, available, f'from {opposite.upper()} bid'
    except (ValueError, ArithmeticError):
        pass
    return None, None, 'no valid executable sized quote'


# Paper trade history is separate from the original order-attempt journal.
PAPER_FILE = Path('kalshi_paper_trades.csv')
PAPER_FIELDS = ['ticker', 'entry_utc', 'side', 'contracts', 'ask_dollars',
                'entry_cost_dollars', 'estimated_fee_reserve_dollars',
                'target', 'eth_at_entry', 'rate_per_minute', 'required_gap',
                'close_time', 'status', 'settlement_result', 'won',
                'gross_pnl_dollars', 'estimated_pnl_after_fee_reserve_dollars',
                'estimated_total_cost_dollars', 'estimated_payout_dollars']


def load_paper():
    if not PAPER_FILE.exists():
        return {}
    with PAPER_FILE.open(newline='', encoding='utf-8') as f:
        return {r['ticker']: {**dict.fromkeys(PAPER_FIELDS, ''), **r} for r in csv.DictReader(f)}


def save_paper(rows):
    # Atomic replacement helps avoid truncated records if the program closes mid-write.
    tmp = PAPER_FILE.with_suffix('.csv.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=PAPER_FIELDS)
        writer.writeheader()
        writer.writerows(rows[k] for k in sorted(rows))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PAPER_FILE)


def settlement_side(market):
    # Only explicit YES/NO results are accepted, not merely closed/expired status.
    result = str(market.get('result') or '').lower()
    if result in ('yes', 'no'):
        return result
    return None


def paper_summary(rows):
    done = [r for r in rows.values() if r['status'] == 'settled']
    wins = sum(r['won'] == 'True' for r in done)
    gross = sum((D(r['gross_pnl_dollars']) for r in done), D('0'))
    estimated = sum((D(r['estimated_pnl_after_fee_reserve_dollars']) for r in done), D('0'))
    pending = sum(r['status'] == 'pending' for r in rows.values())
    print(f'PAPER RESULTS | trades={len(rows)} settled={len(done)} pending={pending} '
          f'wins={wins} losses={len(done)-wins} '
          f'win rate={(100*wins/len(done)):.1f}%' if done else
          f'PAPER RESULTS | trades={len(rows)} settled=0 pending={pending} win rate=N/A', flush=True)
    if rows:
        paid = sum((D(r.get('entry_cost_dollars') or 0) for r in rows.values()), D('0'))
        fees = sum((D(r.get('estimated_fee_reserve_dollars') or 0) for r in rows.values()), D('0'))
        payout = sum((D(r.get('estimated_payout_dollars') or 0) for r in done), D('0'))
        print(f'   PAPER COSTS: purchase ${paid:.2f} + estimated fees ${fees:.2f} = ${paid+fees:.2f} | settled payout ${payout:.2f}', flush=True)
    if done:
        print(f'   Gross P/L: ${gross:+.2f} | Estimated P/L after assumed fee '
              f'fees: ${estimated:+.2f} (standard taker-fee ESTIMATE, not actual charged fees)',flush=True)


def settle_pending(client, rows):
    changed = False
    for ticker, r in rows.items():
        if r['status'] != 'pending':
            continue
        # Allow time for actual official settlement to be published.
        if time.time() < iso(r['close_time']) + 30:
            continue
        m = client.request('GET', '/markets/' + ticker)['market']
        result = settlement_side(m)
        if not result:
            continue
        won = (r['side'] == result)
        count = D(r['contracts'])
        cost = D(r['entry_cost_dollars'])
        reserve = D(r['estimated_fee_reserve_dollars'])
        gross = (count if won else D('0')) - cost
        r.update({'status':'settled','settlement_result':result,
                  'won':str(won),'gross_pnl_dollars':str(gross),
                  'estimated_pnl_after_fee_reserve_dollars':str(gross-reserve),
                  'estimated_total_cost_dollars':str(cost+reserve),
                  'estimated_payout_dollars':str(count if won else D('0'))})
        changed = True
        print(f'\nSETTLED {ticker} | {"WIN" if won else "LOSS"} | '
              f'entry ${cost:.2f} + estimated fee ${reserve:.2f} = ${cost+reserve:.2f} | '
              f'payout ${count if won else D(0):.2f} | estimated NET ${gross-reserve:+.2f}',flush=True)
    if changed:
        save_paper(rows)
        paper_summary(rows)


def observed_rate(readings):
    """Diagnostic only: rate from all OBSERVED adjacent minutes, never gaps.

    This is not a replacement for the full 0..13 strategy sample.
    """
    pairs = [(i-1, i) for i in range(1, 14)
             if i-1 in readings and i in readings]
    if not pairs:
        return None, 0
    rate = sum(abs(D(readings[b])-D(readings[a])) for a,b in pairs) / len(pairs)
    return rate, len(pairs)


def ask_diagnostic(market, side):
    ask, size, source = executable_ask(market, side)
    if ask is None:
        return source + ' (not proof of no liquidity)'
    count = quantity(ask, size)
    return f'ask ${ask:.4f} | available {size} | paper-size {count} | {source} | ' + (
        'price/liquidity passes assumed controls' if count else 'price/liquidity FAILS controls')


def percent_debug(market):
    """Display quote-implied prices, not independently modeled win probabilities.

    YES and NO purchase prices can both include spread; they need not total 100%.
    Use direct quote fields when provided; otherwise derive from opposite bid.
    Missing prices are explicitly marked unavailable.
    """
    def numeric(field):
        val = market.get(field)
        if val in (None, ''):
            return None
        try:
            num = D(val)
        except (ValueError, TypeError, ArithmeticError):
            return None
        return num if D('0') <= num <= D('1') else None

    result = {}
    for side in ('yes', 'no'):
        other = 'no' if side == 'yes' else 'yes'
        ask = numeric(side + '_ask_dollars')
        ask_source = 'direct ask'
        if ask is None:
            opp_bid = numeric(other + '_bid_dollars')
            if opp_bid is not None:
                ask = D('1') - opp_bid
                ask_source = 'derived from opposite bid'
        bid = numeric(side + '_bid_dollars')
        if bid is None:
            opp_ask = numeric(other + '_ask_dollars')
            if opp_ask is not None:
                bid = D('1') - opp_ask
        result[side] = (bid, ask, ask_source)
    return result


def format_quote_percent(quote):
    bid, ask, source = quote
    bid_text = f'{bid*100:.1f}%' if bid is not None else 'N/A'
    ask_text = f'{ask*100:.1f}%' if ask is not None else 'N/A'
    return f'bid {bid_text} | buy/ask {ask_text} ({source if ask is not None else "no available ask"})'


def load_crossings():
    try:
        return json.loads(CROSSING_FILE.read_text(encoding='utf-8')) if CROSSING_FILE.exists() else {}
    except (OSError, ValueError):
        return {}


def update_crossings(crossings, ticker, gap, remaining, *, price=None, target=None,
                     ethrti_timestamp=None, observed_timestamp=None):
    """Audit ALL timestamped sign transitions; block only final-180s crossings.

    The comparison uses distinct, strictly increasing ETHRTI timestamps to avoid
    treating stale/repeated data as fresh crossings. A detected sign transition
    is not proof of the exact intra-gap crossing time, which is unknown.
    Existing v3.3 crossing JSON records remain readable. Never fabricate events
    during startup, and do not permanently block a market for an early crossing.
    """
    if observed_timestamp is None:
        observed_timestamp = time.time()
    if ethrti_timestamp is None:
        ethrti_timestamp = observed_timestamp
    gap = D(gap)
    target = D(target) if target is not None else None
    price = D(price) if price is not None else (target + gap if target is not None else None)
    side = 'yes' if gap > 0 else 'no' if gap < 0 else None
    state = crossings.setdefault(ticker, {'last_side': None, 'crossings': []})
    state.setdefault('crossings', [])
    last_ts = state.get('last_ethrti_timestamp')
    if last_ts is not None and float(ethrti_timestamp) <= float(last_ts):
        return bool(state.get('final_180_crossings')) or any(
            0 < float(c.get('seconds_remaining', 9999)) <= 180
            for c in state['crossings'])
    prior = state.get('last_side')
    prior_price = state.get('last_eth')
    prior_ts = state.get('last_ethrti_timestamp')
    if side and prior and prior_ts is not None and side != prior:
        in_window = 0 < remaining <= 180
        event = {
            'utc': datetime.fromtimestamp(observed_timestamp, timezone.utc).isoformat(),
            'ethrti_utc': datetime.fromtimestamp(ethrti_timestamp, timezone.utc).isoformat(),
            'seconds_remaining': round(float(remaining), 1),
            'previous_side': prior, 'current_side': side,
            'previous_eth': str(prior_price) if prior_price is not None else '',
            'current_eth': str(price) if price is not None else '',
            'target': str(target) if target is not None else '',
            'in_final_180s': in_window,
        }
        state['crossings'].append(event)
        if in_window:
            state.setdefault('final_180_crossings', []).append(event)
            print(f'*** WARNING: ETH CROSSED TARGET in final 3 minutes: {ticker}; '
                  'paper entry blocked for this market ***', flush=True)
        else:
            print(f'*** CROSSING AUDIT: ETH switched {prior.upper()} -> {side.upper()} '
                  f'with {remaining:.0f}s remaining (EARLY crossing; not an automatic block): '
                  f'{ticker} ***',flush=True)
        append_csv(CROSSING_AUDIT_FILE, CROSSING_AUDIT_FIELDS, {
            'ticker':ticker, 'observed_utc':event['utc'],
            'ethrti_utc':event['ethrti_utc'],
            'seconds_remaining':event['seconds_remaining'], 'event':'TARGET_SIGN_CHANGE',
            'previous_side':prior, 'current_side':side,
            'previous_eth':event['previous_eth'],'current_eth':event['current_eth'],
            'target':event['target'],
            'ethrti_gap_seconds':f'{ethrti_timestamp-float(prior_ts):.3f}' if prior_ts is not None else '',
            'crossing_window':'FINAL_180S' if in_window else 'EARLY',
            'note':'Detected between sampled ETHRTI observations; exact crossing instant unknown',
        })
    if side:
        state['last_side'] = side
    if price is not None:
        state['last_eth'] = str(price)
    state['last_ethrti_timestamp'] = float(ethrti_timestamp)
    state['last_observed_utc'] = datetime.fromtimestamp(observed_timestamp, timezone.utc).isoformat()
    temp = CROSSING_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(crossings, indent=2), encoding='utf-8')
    os.replace(temp, CROSSING_FILE)
    return bool(state.get('final_180_crossings')) or any(
        0 < float(c.get('seconds_remaining', 9999)) <= 180
        for c in state['crossings'])


def crossing_metrics(crossings, ticker, observed_timestamp=None):
    """Audit features from observed sign transitions only; never infer missing prices."""
    if observed_timestamp is None:
        observed_timestamp = time.time()
    state = crossings.get(ticker, {})
    events = state.get('crossings') or []
    valid = []
    for event in events:
        try:
            stamp = datetime.fromisoformat(event.get('ethrti_utc') or event.get('utc')).timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
        if stamp <= observed_timestamp:
            valid.append((stamp, event))
    latest = max(valid, key=lambda item: item[0]) if valid else None
    recent = sum(1 for stamp, _ in valid if observed_timestamp - 180 <= stamp <= observed_timestamp)
    return {'total':len(events), 'recent_180s':recent,
            'seconds_since_last':round(observed_timestamp-latest[0],1) if latest else None,
            'latest_direction':((latest[1].get('previous_side') or '?')+'->'+
                                (latest[1].get('current_side') or '?')) if latest else ''}


def write_v35_diagnostic(market, price, remaining, decision, crossings, *, paper_entry=False):
    """Append transparent eligibility economics without touching legacy CSV headers."""
    ask = decision['ask']; count = decision['count']
    fee = estimate_fee(ask, count) if ask is not None and count else D('0')
    profit = D(count) - D(ask)*count - fee if ask is not None and count else None
    loss = -D(ask)*count-fee if ask is not None and count else None
    metrics = crossing_metrics(crossings, market['ticker'])
    row = {'ticker':market['ticker'], 'observed_utc':datetime.now(timezone.utc).isoformat(),
           'seconds_remaining':f'{remaining:.1f}', 'side':decision['side'] or '',
           'eth':str(price),'target':str(market['floor_strike']),
           'gap_dollars':str(abs(decision['gap'])),
           'ask_dollars':str(ask) if ask is not None else '', 'quantity':str(count),
           'estimated_fee':str(fee) if count else '',
           'net_profit_if_win':str(profit) if profit is not None else '',
           'net_loss_if_lost':str(loss) if loss is not None else '',
           'minimum_net_win_profit':str(MIN_NET_WIN_PROFIT),
           'crossings_total':metrics['total'], 'crossings_last_180s':metrics['recent_180s'],
           'seconds_since_latest_crossing':metrics['seconds_since_last'] if metrics['seconds_since_last'] is not None else '',
           'latest_crossing_direction':metrics['latest_direction'],
           'reasons':';'.join(decision['reasons']) or 'ALL_CHECKS_PASS',
           'eligible':str(decision['eligible']), 'actual_paper_entry':str(paper_entry),
           'note':'Crossing and economics diagnostics only. Estimates do not prove positive expected value.'}
    append_csv(V35_DIAGNOSTICS, V35_FIELDS, row)


def decide(market, price, readings, remaining, crossed):
    target = D(market['floor_strike'])
    gap = D(price) - target
    side = 'yes' if gap > 0 else 'no' if gap < 0 else None
    check = evaluate_crossing_risk(readings, target, price, remaining)
    ask, size, quote_source = executable_ask(market, side) if side else (None, None, '')
    count = quantity(ask, size) if ask is not None and size is not None else 0
    reasons = []
    if not side: reasons.append('AT_TARGET')
    if side and not in_entry_window(side, remaining): reasons.append('TIME_WINDOW')
    if check is None: reasons.append('INSUFFICIENT_SAMPLES')
    else:
        if not check['gap_pass']: reasons.append('GAP_TOO_SMALL')
        if check['spike']: reasons.append('VOLATILITY_SPIKE')
    if crossed: reasons.append('TARGET_CROSSED_LAST_3_MIN')
    if ask is None: reasons.append('QUOTE_UNAVAILABLE')
    elif not side_quote_allowed(side, ask): reasons.append('PRICE_BELOW_89')
    elif ask > MAX_PRICE: reasons.append('PRICE_ABOVE_99')
    if ask is not None and count <= 0: reasons.append('NO_AFFORDABLE_SIZED_QUOTE')
    if ask is not None and count > 0:
        net_win = D(count) - D(ask)*count - estimate_fee(ask, count)
        if net_win < MIN_NET_WIN_PROFIT*count:
            reasons.append('NET_WIN_PROFIT_BELOW_3_CENTS_PER_CONTRACT')
    return {'side':side, 'check':check, 'ask':ask, 'size':size,
            'count':count, 'reasons':reasons, 'eligible':not reasons, 'gap':gap, 'quote_source':quote_source}


def log_decision(market, price, remaining, decision, crossed, paper_entry=False):
    """Append snapshots; outcome updated later from official Kalshi results."""
    check = decision['check']; gap = abs(decision['gap'])
    required = check['required'] if check else None
    progress = min(D('9999'), gap/required*100) if required and required > 0 else None
    shortfall = max(D('0'), required-gap) if required else None
    r = {'ticker':market['ticker'],'observed_utc':datetime.now(timezone.utc).isoformat(),
         'seconds_remaining':f'{remaining:.1f}', 'direction':decision['side'] or 'none',
         'eth':str(price),'target':str(market['floor_strike']),
         'gap_dollars':str(gap),'required_gap_dollars':str(required) if required is not None else '',
         'gap_progress_pct':f'{progress:.1f}' if progress is not None else '',
         'gap_shortfall_dollars':str(shortfall) if shortfall is not None else '',
         'rate_per_minute':str(check['rate']) if check else '',
         'sample_pairs':str(check['pairs']) if check else '',
         'volatility_spike':str(check['spike']) if check else '',
         'target_crossed_last_180s':str(crossed),
         'ask_dollars':str(decision['ask']) if decision['ask'] is not None else '',
         'available_contracts':str(decision['size']) if decision['size'] is not None else '',
         'candidate_contracts':str(decision['count']),
         'reasons':';'.join(decision['reasons']) or 'ALL_CHECKS_PASS',
         'paper_entry':str(paper_entry), 'settlement_result':'',
         'candidate_side_won':'', 'hypothetical_gross_pnl':'',
         'note':(f'Unvalidated Gaussian target-touch risk {check["cross_risk"]:.4f}; ' if check else '') + 'Snapshot, NOT a filled trade. Outcomes resolved using official result.'}
    new = not SIGNAL_JOURNAL.exists()
    with SIGNAL_JOURNAL.open('a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f,fieldnames=DECISION_FIELDS)
        if new: w.writeheader()
        w.writerow(r)


def settle_decision_journal(client):
    if not SIGNAL_JOURNAL.exists(): return
    with SIGNAL_JOURNAL.open(newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    changed = False
    for ticker in {r['ticker'] for r in rows if not r.get('settlement_result')}:
        relevant = [r for r in rows if r['ticker'] == ticker and not r.get('settlement_result')]
        if not relevant: continue
        try:
            market = client.request('GET', '/markets/'+ticker)['market']
            if time.time() < iso(market['close_time']) + 30: continue
            result = settlement_side(market)
            if not result: continue
        except (requests.RequestException, KeyError, ValueError):
            continue
        for r in relevant:
            r['settlement_result'] = result
            if r['direction'] in ('yes', 'no'):
                won = r['direction'] == result
                r['candidate_side_won'] = str(won)
                try:
                    qty = int(r['candidate_contracts'])
                    ask = D(r['ask_dollars'])
                    if qty > 0 and ask > 0:
                        r['hypothetical_gross_pnl'] = str((D(qty) if won else D('0'))-D(qty)*ask)
                except (ValueError, ArithmeticError): pass
            changed = True
    if changed:
        tmp = SIGNAL_JOURNAL.with_suffix('.tmp')
        with tmp.open('w',newline='',encoding='utf-8') as f:
            w = csv.DictWriter(f,fieldnames=DECISION_FIELDS)
            w.writeheader(); w.writerows(rows)
        os.replace(tmp,SIGNAL_JOURNAL)
        print('Updated rejected-candidate journal with official settlement outcomes.',flush=True)


def show_dashboard(market, price, readings, remaining, rows, crossed=False, market_number=1, color_enabled=False):
    target = D(market['floor_strike'])
    gap = price - target
    check = evaluate_crossing_risk(readings, target, price, remaining)
    estimate = check
    valid = estimate is not None
    if valid:
        rate = estimate['rate']
        required = check['required']
        gap_text = f'${abs(gap):.2f} / required ${required:.2f}'
        status = ('GAP PASSED' if check['gap_pass'] else 'GAP TOO SMALL')
        status += ' | VOLATILITY SPIKE: REJECT' if check['spike'] else ' | no spike detected'
        rate_text = (f'${rate:.2f}/minute from minute {estimate["start_minute"]}..'
                     f'{estimate["end_minute"]} ({estimate["pairs"]} pairs, {estimate["method"]})')
    else:
        missing = [i for i in range(14) if i not in readings]
        rate_text = 'collecting 14 observations'
        gap_text = f'${abs(gap):.2f} / unavailable'
        status = f'INSUFFICIENT CONTIGUOUS SAMPLES ({len(readings)}/14; missing {missing})'
    selected_side = 'yes' if gap > 0 else 'no'
    if in_entry_window(selected_side, remaining):
        status += ' | entry window open for ' + selected_side.upper()
    else:
        status += ' | outside ' + selected_side.upper() + ' entry window'
    print('\n'+'='*68)
    color = market_style(market_number, color_enabled)
    reset = RESET_COLOR if color_enabled else ''
    print(f'{color}MARKET #{market_number:03d} | KALSHI ETH 15-MIN LIVE MONITOR | PAPER MODE{reset}')
    print('Market:',market['ticker'])
    print(f'ETH: ${price:,.2f} | target: ${target:,.2f} | '
          f'direction: {"ABOVE" if gap > 0 else "BELOW" if gap < 0 else "EQUAL"}')
    print(f'Seconds remaining: {max(0,remaining):.0f} | observed volatility: {rate_text}')
    quote_percents = percent_debug(market)
    print('UP / YES market quotes:  ', format_quote_percent(quote_percents['yes']))
    print('DOWN / NO market quotes: ', format_quote_percent(quote_percents['no']))
    print('Market quote percentages are prices, NOT verified winning probabilities.')
    print('Both buy/ask percentages may total above 100% due to spread.')
    print('Selected-side price rule: UP or DOWN buy/ask >= 89.0% (and <= 99.0%).')
    for quote_side, label in (('yes', 'UP'), ('no', 'DOWN')):
        ask = quote_percents[quote_side][1]
        outcome = ('N/A (buy/ask unavailable)' if ask is None else
                   ('PASS' if side_quote_allowed(quote_side, ask) and ask <= MAX_PRICE else 'FAIL')
                   + f' (buy/ask {ask*100:.2f}%)')
        print(f'{label} price condition:', outcome)
    print(f'Entry windows: UP and DOWN 30–150 seconds. Require >= {MIN_ROLLING_PAIRS} contiguous actual minute pairs.')
    print('Target gap:',gap_text)
    if check and check['required'] > 0:
        progress = 100*abs(gap)/check['required']
        shortfall = max(D('0'),check['required']-abs(gap))
        print(f'GAP PROGRESS: {progress:.1f}% of required buffer | '
              f'need ${shortfall:.2f} more to pass | '
              f'buffer surplus ${max(D("0"),abs(gap)-check["required"]):.2f}')
    print('TARGET CROSSING LAST 3 MINUTES (NOT all-market crossings):',
          'YES — WARNING, PAPER ENTRY BLOCKED' if crossed else 'NO observed crossing')
    print(f'ACTIVE MODEL: estimated chance of touching target <= {MAX_CROSS_RISK:.1%} before close (UNVALIDATED Gaussian approximation)')
    if check:
        print(f'Estimated target-touch risk {check["cross_risk"]:.1%} vs limit {MAX_CROSS_RISK:.1%}; risk-model required gap ${check["required"]:.2f}; for {check["remaining_minutes"]:.2f} min; '
              f'recent 3-minute average ${check["recent_rate"]:.2f}/min versus '
              f'earlier ${check["baseline_rate"]:.2f}/min; '
              f'spike={"YES / REJECT" if check["spike"] else "NO"}')
    print('Signal check:',status)
    if check: print(f'RISK MODEL: target-touch estimate {check["cross_risk"]:.1%} | maximum {MAX_CROSS_RISK:.1%} | ASSUMPTION-BASED, NOT A MEASURED WIN PROBABILITY')
    # Compare three dynamic thresholds in all status reports. Insufficient
    # observed data is diagnostic only; no paper entry without valid history.
    compare_rate = rate if valid else None
    if not valid:
        compare_rate, pairs = observed_rate(readings)
        if compare_rate is not None:
            print(f'PRELIMINARY ONLY: ${compare_rate:.2f}/min from {pairs} adjacent minute-pairs; '
                  'NOT a valid full-window 13-minute volatility estimate')
    if compare_rate is not None:
        print('LEGACY VOLATILITY CHECKS (comparison only, NOT primary entry rule: '
              f'{MULTIPLIER}x; real minute readings only):')
        for multiplier in (D('1.75'), D('2.5'), D('3')):
            shadow_required = multiplier * (D(str(max(remaining, 0))) / D('60')) * compare_rate
            compare = 'PASS' if abs(gap) >= shadow_required and compare_rate > 0 else 'FAIL'
            qualifier = (estimate['method'] if valid else 'preliminary ONLY; cannot trade')
            print(f'  {multiplier}x threshold ${shadow_required:.2f} '
                  f'vs gap ${abs(gap):.2f}: {compare} ({qualifier}) | '
                  f'UP {format_quote_percent(quote_percents["yes"])} | '
                  f'DOWN {format_quote_percent(quote_percents["no"])}')
    else:
        print('VOLATILITY CHECKS 1.75x / 2.5x / 3x: unavailable (no adjacent minute observations)')
    if in_entry_window(selected_side, remaining):
        preferred = selected_side
        print('Executable quote check:', preferred.upper(), ask_diagnostic(market, preferred))
        if not valid:
            print(f'NO PAPER ORDER: Need at least {MIN_ROLLING_PAIRS} contiguous actual minute pairs; no interpolation.')
    else:
        print('Not within allowed entry window: UP and DOWN 30–150s.')
    paper_summary(rows)
    print('Paper data:',PAPER_FILE.resolve())
    print('='*68, flush=True)


def run_bot(demo=False, live=False):
    if live:
        if os.environ.get('KALSHI_ENABLE_LIVE') != 'YES_I_ACCEPT_RISK':
            raise RuntimeError('LIVE disabled: set KALSHI_ENABLE_LIVE=YES_I_ACCEPT_RISK')
        if os.environ.get('KALSHI_FEE_CEILING_VERIFIED') != 'YES':
            raise RuntimeError('LIVE disabled: fee ceiling unverified; check actual fee schedule first')
        # This upgraded version is intentionally paper-only until precise fee verification.
        raise RuntimeError('LIVE disabled in dashboard version. Use paper mode; do not bypass safeguards.')
    client = Client(demo=demo,auth=True)
    records = load_samples()
    crossings = load_crossings()
    paper = load_paper()
    seen = set(paper)
    journal = Path('kalshi_orders_journal.jsonl')
    if journal.exists():
        for line in journal.read_text().splitlines():
            try: seen.add(json.loads(line)['ticker'])
            except (ValueError,KeyError): pass
    print('MODE PAPER ENV', 'DEMO' if demo else 'PRODUCTION', flush=True)
    print('DIAGNOSTICS VERSION:', DASHBOARD_VERSION, flush=True)
    print('Risk limit:', f'{MAX_CROSS_RISK:.0%}', '| price gate: 89%-99% | both sides <=150s', '| paper budget: $1 INCLUDING estimated fees | samples persisted:', SAMPLES_FILE, flush=True)
    print('Live orders DISABLED. Paper trade tracking enabled. Press Ctrl+C to stop.',flush=True)
    print(f'Rolling fallback: >= {MIN_ROLLING_PAIRS} contiguous actual minute pairs; earlier observations never fabricated.',flush=True)
    print('Risk estimate is a Gaussian first-passage MODEL, NOT historically calibrated; keep paper mode.',flush=True)
    print('Gap progress, rejection/outcome journal and target-crossing warning enabled.',flush=True)
    print('v37: minimum net win profit $0.03/contract (assumed fee); fee estimate is not EV validation.',flush=True)
    print('For colored terminal output, run Python directly in PowerShell; Tee-Object may disable ANSI colors.',flush=True)
    print('v37: atomic paper entry audit:',V37_ENTRY_AUDIT.resolve(),flush=True)
    print('v3.5 backward-compatible diagnostics:',V35_DIAGNOSTICS.resolve(),flush=True)
    print('ALL market target sign changes audited to', CROSSING_AUDIT_FILE.resolve(),
          '| only final-180s crossings block original paper entries.',flush=True)
    print('SHADOW ONLY: 30-300s, ask 89-95 cents, ETHRTI tick rates 15/30/60/90s; NO additional orders.',flush=True)
    print('Momentum files:',TICK_FILE.resolve(),'and',SHADOW_FILE.resolve(),flush=True)
    print('RESEARCH B/C: exploratory probabilities, NO calibrated edge claims, NO additional orders.',flush=True)
    print('Research data:',RESEARCH_TICKS.resolve(), RESEARCH_QUOTES.resolve(), RESEARCH_FILE.resolve(),flush=True)
    color_enabled = enable_console_colors()
    last_screen = 0
    last_settle = 0
    active_section = None
    market_counter = 0
    ticks = deque()
    continuous = load_continuous_history()
    research_selected = {(r['ticker'],r['strategy']) for r in read_csv_rows(RESEARCH_FILE) if r['selected_for_shadow']=='True'}
    last_shadow_banner = set()
    while True:
        try:
            now = time.time()
            if now-last_settle >= 30:
                settle_pending(client, paper)
                settle_decision_journal(client)
                settle_shadow_candidates(client)
                settle_research(client)
                last_settle = now
            market = client.market()
            if market is None:
                if active_section is not None and now >= active_section['close']:
                    item = active_section
                    close_market_section(item['ticker'], item['first'], item['last'],
                                         item['number'], color_enabled, item['close'],
                                         len(records.get(item['ticker'], {})))
                    active_section = None
                if now-last_screen >= 10:
                    print('Waiting for an open ETH 15-minute market...',flush=True)
                    last_screen = now
                time.sleep(POLL)
                continue
            ticker = market['ticker']
            close = iso(market['close_time'])
            start = close - 900
            price, timestamp = client.ethrti()
            if active_section is None or active_section['ticker'] != ticker:
                if active_section is not None:
                    old = active_section
                    close_market_section(old['ticker'], old['first'], old['last'],
                                         old['number'], color_enabled, old['close'],
                                         len(records.get(old['ticker'], {})))
                ticks.clear()
                market_counter += 1
                active_section = {'ticker': ticker, 'first': now, 'last': now,
                                  'close': close, 'number': market_counter}
                open_market_section(ticker, now, market_counter, color_enabled, close)
                last_screen = 0
            else:
                active_section['last'] = now
            readings = records.setdefault(ticker, {})
            minute = minute_bucket(timestamp,start)
            if minute is not None and minute not in readings:
                readings[minute] = price
                save_samples(records)
                print('ETHRTI minute', minute, ticker, price,flush=True)
            remaining = close-time.time()
            fresh_tick = add_tick(ticks,ticker,price,timestamp,now,market["floor_strike"],remaining)
            crossed = update_crossings(crossings,ticker,price-D(market['floor_strike']),remaining,
                                       price=price,target=market['floor_strike'],
                                       ethrti_timestamp=timestamp,observed_timestamp=now)
            new_continuous=collect_research_tick(continuous,ticker,price,timestamp,now)
            if new_continuous:
                qa, qs, qsource=executable_ask(market,'yes')
                na, ns, nsource=executable_ask(market,'no')
                append_csv(RESEARCH_QUOTES,QUOTE_FIELDS,{'ticker':ticker,
                    'observed_utc':datetime.fromtimestamp(now,timezone.utc).isoformat(),
                    'ethrti_utc':datetime.fromtimestamp(timestamp,timezone.utc).isoformat(),
                    'seconds_remaining':f'{remaining:.1f}', 'eth':str(price),
                    'target':str(market['floor_strike']),
                    'yes_bid':str(market.get('yes_bid_dollars') or ''),
                    'yes_ask':str(qa) if qa is not None else '',
                    'yes_ask_liquidity':str(qs) if qs is not None else '',
                    'no_bid':str(market.get('no_bid_dollars') or ''),
                    'no_ask':str(na) if na is not None else '',
                    'no_ask_liquidity':str(ns) if ns is not None else '',
                    'yes_quote_source':qsource,'no_quote_source':nsource})
                for research in research_rows(market,price,timestamp,remaining,continuous,crossed):
                    key=(ticker,research['strategy'])
                    if research['decision']=='PAPER_CANDIDATE' and key not in research_selected:
                        research['selected_for_shadow']='True'
                        research_selected.add(key)
                        print('RESEARCH SHADOW:',research['strategy'],ticker,research['side'],
                              'ask',research['ask_dollars'],'NO REAL ORDER',flush=True)
                    append_csv(RESEARCH_FILE,RESEARCH_FIELDS,research)
            if fresh_tick:
                shadow = shadow_snapshot(market,price,timestamp,remaining,ticks,readings,crossed)
                if shadow and shadow['shadow_signal']=='True' and ticker not in last_shadow_banner:
                    last_shadow_banner.add(ticker)
                    print('*** SHADOW EARLY MOMENTUM SIGNAL (NO ORDER):', ticker,
                          shadow['side'].upper(), 'ask', shadow['ask_dollars'],
                          'gap', shadow['gap_dollars'], 'remaining', shadow['seconds_remaining'], flush=True)
            if now-last_screen >= 10:
                show_dashboard(market,price,readings,remaining,paper,crossed,
                               active_section['number'],color_enabled)
                diagnostics = crossing_metrics(crossings, ticker, observed_timestamp=now)
                print(f'v37 CROSSINGS: all-market={diagnostics["total"]} | rolling previous 180s={diagnostics["recent_180s"]} | final market 180s='+
                      str(sum(1 for e in crossings.get(ticker, {}).get('crossings', []) if 0 < float(e.get('seconds_remaining',99999)) <= 180))+' | '+
                      (f'latest {diagnostics["seconds_since_last"]:.1f}s ago ({diagnostics["latest_direction"]})'
                       if diagnostics['seconds_since_last'] is not None else 'no observed crossing'),flush=True)
                decision = decide(market,price,readings,remaining,crossed)
                if MIN_ENTRY_SECONDS <= remaining <= UP_ENTRY_SECONDS:
                    log_decision(market,price,remaining,decision,crossed)
                write_v35_diagnostic(market,price,remaining,decision,crossings)
                if 'NET_WIN_PROFIT_BELOW_3_CENTS_PER_CONTRACT' in decision['reasons']:
                    emit_status('v37 PAPER ENTRY REJECTED: winning profit below 3 cents per contract after assumed fees.', COLOR_WARNING, enabled=color_enabled)
                elif MIN_ENTRY_SECONDS <= remaining <= UP_ENTRY_SECONDS and decision['reasons']:
                    emit_status('v37 PAPER ENTRY REJECTED: ' + '; '.join(decision['reasons']), COLOR_WARNING, enabled=color_enabled)
                last_screen = now
            if MIN_ENTRY_SECONDS <= remaining <= UP_ENTRY_SECONDS and ticker not in seen:
                decision = decide(market,price,readings,remaining,crossed)
                sig = decision['check'] if decision['eligible'] else None
                if sig and in_entry_window(sig['side'], remaining):
                    side = sig['side']
                    ask, size = decision['ask'], decision['size']
                    quote_source = decision['quote_source']  # identical quote snapshot used by eligibility
                    if ask is not None and size is not None and side_quote_allowed(side, ask):
                        count = decision['count']
                        if count and decision['eligible']:
                            entry_audit = atomic_entry_audit(
                                market, price, timestamp, remaining, decision, crossed, crossings,
                                quote_source=quote_source, decision_timestamp=time.time())
                            if entry_audit['all_gates_pass'] != 'True':
                                raise RuntimeError('Entry audit gate mismatch: paper entry aborted')
                            ask_d = D(ask)
                            cost = count*ask_d
                            reserve = estimate_fee(ask_d, count)
                            r = dict.fromkeys(PAPER_FIELDS,'')
                            r.update({'ticker':ticker,'entry_utc':datetime.now(timezone.utc).isoformat(),
                                'side':side,'contracts':str(count),'ask_dollars':str(ask),
                                'entry_cost_dollars':str(cost),
                                'estimated_fee_reserve_dollars':str(reserve),
                                'estimated_total_cost_dollars':str(cost+reserve),
                                'estimated_payout_dollars':'',
                                'target':str(market['floor_strike']),
                                'eth_at_entry':str(price),'rate_per_minute':str(sig['rate']),
                                'required_gap':str(sig['required']),
                                'close_time':market['close_time'],'status':'pending'})
                            paper[ticker] = r
                            save_paper(paper)
                            log_decision(market,price,remaining,decision,crossed,paper_entry=True)
                            write_v35_diagnostic(market,price,remaining,decision,crossings,paper_entry=True)
                            seen.add(ticker)
                            emit_status(f'v37 ENTRY AUDIT: {V37_ENTRY_AUDIT.name} | ETH source {entry_audit["ethrti_utc"]} | gap ${entry_audit["gap_absolute_dollars"]} | model touch risk {entry_audit["model_target_touch_risk"]} | ALL GATES PASS', COLOR_INFO, enabled=color_enabled)
                            print('\nPAPER ENTRY:',ticker,side.upper(),count,
                                  f'contracts @ ${ask_d:.4f} each; purchase ${cost:.4f}; '
                                  f'estimated fee ${reserve:.2f}; total ${cost+reserve:.4f}; '
                                  f'IF WIN net ${count-cost-reserve:+.4f}; IF LOSS net ${-cost-reserve:+.4f}; '
                                  f'volatility: {sig["method"]} minutes {sig["start_minute"]}..{sig["end_minute"]}; '
                                  f'quote source: {quote_source}; awaiting official market result',flush=True)
                            paper_summary(paper)
                            emit_status('v37 PAPER ENTRY RECORDED (NO REAL ORDER): '+ticker+' '+side.upper(), COLOR_SUCCESS, enabled=color_enabled)
            time.sleep(POLL)
        except KeyboardInterrupt:
            if active_section is not None:
                item = active_section
                close_market_section(item['ticker'], item['first'], item['last'],
                                     item['number'], color_enabled, item['close'],
                                     len(records.get(item['ticker'], {})))
            print('\nStopped. Paper trades saved to',PAPER_FILE.resolve(),flush=True)
            break
        except Exception as e:
            print('FAIL CLOSED:',str(e),file=sys.stderr,flush=True)
            time.sleep(5)

def backtest(filename, output):
    """CSV required: ticker,minute,eth_price,target,yes_ask,no_ask,yes_liquidity,no_liquidity,settled_yes.
    One row per minute 0..13. Minute 13 quote is the entry proxy at T-2; fee reserve
    used as pessimistic fee assumption, not a verified execution cost. Results are hypothetical.
    """
    records={}
    with open(filename,newline='') as f:
        for row in csv.DictReader(f):
            records.setdefault(row['ticker'],[]).append(row)
    results=[]
    for ticker, rows in records.items():
        by_min={int(x['minute']):x for x in rows}
        if set(by_min)!=set(range(14)):
            continue
        latest=by_min[13]
        try:
            prices={i:D(by_min[i]['eth_price']) for i in range(14)}
            sig=strategy(prices,D(latest['target']), prices[13])
            if not sig:
                continue
            side=sig['side']; ask=D(latest[side+'_ask'])
            if not side_quote_allowed(side, ask):
                continue
            count=quantity(ask,latest[side+'_liquidity'])
            if not count:
                continue
            won=(latest['settled_yes'].strip().lower() in ('yes','true','1')) == (side=='yes')
            cost=count*ask+estimate_fee(ask,count)
            pnl=(D(count) if won else D('0'))-cost
            results.append({'ticker':ticker,'side':side,'count':count,'ask':str(ask),
                            'win':won,'estimated_pnl':str(pnl),
                            'required_gap':str(sig['required']),'actual_gap':str(sig['gap'])})
        except (KeyError,ValueError,ArithmeticError):
            continue
    if output:
        with open(output,'w',newline='') as f:
            fields=['ticker','side','count','ask','win','estimated_pnl','required_gap','actual_gap']
            wr=csv.DictWriter(f,fieldnames=fields);wr.writeheader();wr.writerows(results)
    wins=sum(r['win'] for r in results)
    print(json.dumps({'markets_in_input':len(records),'qualifying_entries':len(results),
                      'wins':wins,'losses':len(results)-wins,
                      'win_rate':round(wins/len(results),4) if results else None,
                      'hypothetical_pnl_with_assumed_fee_reserve':str(sum((D(r['estimated_pnl']) for r in results), D('0'))),
                      'warning':'Not executable historical fills: minute-13 quote and fee reserve are assumptions.'},indent=2))


def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest='cmd',required=True)
    a=sub.add_parser('run');a.add_argument('--demo',action='store_true');a.add_argument('--live',action='store_true')
    b=sub.add_parser('backtest');b.add_argument('csv');b.add_argument('--output',default='backtest_results.csv')
    sub.add_parser('report')
    args=p.parse_args()
    if args.cmd=='run':run_bot(args.demo,args.live)
    elif args.cmd=='report':research_report()
    else:backtest(args.csv,args.output)

if __name__=='__main__': main()
