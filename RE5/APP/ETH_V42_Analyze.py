#!/usr/bin/env python3
"""V42 offline, read-only market-level research and risk stress report. No API, no trading."""
import argparse,csv,html,json,statistics,math,sqlite3
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from collections import Counter,defaultdict

ROOT=Path(__file__).resolve().parent
R=Decimal
GATE_A='STRIKE_CUSHION_BELOW_0_03_PERCENT'
GATE_B='STRIKE_CUSHION_BELOW_CONFIGURED_MINIMUM'
GATE_C='SCALED_CUSHION_BELOW_1_4'
POLICIES={
 'CURRENT_0.03_AND_SCALED_1.4':set(),
 'PERCENTAGE_0.03_ONLY':{GATE_C},
 'SCALED_1.4_ONLY':{GATE_A,GATE_B},
 'NEITHER_NEW_GATE':{GATE_A,GATE_B,GATE_C},
}
def rows(p):
 if not p.exists():return []
 with p.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def dec(x,default=None):
 try:return R(str(x)) if str(x).strip() else default
 except (InvalidOperation,ValueError):return default
def accepted(c,exempt):
 if int(c.get('contracts') or 0)<1:return False
 return set(filter(None,(c.get('reason') or '').split(';'))).issubset(exempt)
def analyze(root,initial=100,slippage_cents=0.0):
 candidates=rows(root/'eth_v42_shadow_candidates.csv')
 # Fail closed on paper candidates larger than the $1000 BIGBET release cap.
 oversized=[r for r in candidates if r.get('status','').startswith(('SHADOW_CANDIDATE','SETTLED_')) and (dec(r.get('total_cost'),R(0)) or R(0))>R('10.00')]
 if oversized: raise ValueError(f'Found {len(oversized)} paper candidates exceeding $1000.00; verify BIGBET input files')
 settlements=rows(root/'eth_v42_all_market_settlements.csv')
 market_outcomes=rows(root/'eth_v42_market_outcomes.csv')
 quoted=rows(root/'eth_v42_market_quotes.csv')
 official={r['ticker']:r['official_side'].lower() for r in settlements if r.get('official_side','').lower() in ('yes','no')}
 for r in market_outcomes:
  if r.get('settlement_side','').lower() in ('yes','no'):official.setdefault(r['ticker'],r['settlement_side'].lower())
 for r in candidates:
  if r.get('status') in ('SETTLED_WIN','SETTLED_LOSS') and r.get('settlement_side','').lower() in ('yes','no'):official.setdefault(r['ticker'],r['settlement_side'].lower())
 grouped=defaultdict(list)
 for r in candidates:grouped[r['ticker']].append(r)
 for group in grouped.values(): group.sort(key=lambda r:(-int(r['checkpoint_s']),r.get('time_utc','')))
 result={}
 flat=[]
 for policy,exempt in POLICIES.items():
  selected=[]
  for ticker,records in sorted(grouped.items()):
   # Only earliest eligible checkpoint; never retroactively select using settlement outcome.
   c=next((r for r in records if accepted(r,exempt)),None)
   if c is None:continue
   out=official.get(ticker)
   ask=dec(c.get('ask'));fee=dec(c.get('estimated_fee'),R(0));n=int(c['contracts']);cost=dec(c.get('total_cost'))
   if cost is None and ask is not None:cost=ask*n+fee
   if cost is None:continue
   status='UNVERIFIED' if out not in ('yes','no') else ('WIN' if out==c['side'].lower() else 'LOSS')
   # Adverse execution stress: cost increase per contract, payout fixed.
   stressed_cost=cost+R(str(slippage_cents))/100*n
   pnl=None if status=='UNVERIFIED' else (R(n)-stressed_cost if status=='WIN' else -stressed_cost)
   d={'policy':policy,'ticker':ticker,'checkpoint_s':c['checkpoint_s'],'side':c['side'],'status':status,'contracts':n,'cost':float(cost),'stressed_cost':float(stressed_cost),'pnl':float(pnl) if pnl is not None else None,'cushion_pct':c.get('cushion_pct'),'scaled_gap':c.get('scaled_gap')}
   selected.append(d);flat.append(d)
  known=[a for a in selected if a['pnl'] is not None]
  balance=initial;high=initial;max_dd=0;max_loss=0
  for entry in known:
   # Constant-sized counterfactual replay; not full account compounding/fill simulation.
   balance+=entry['pnl'];high=max(high,balance);max_dd=max(max_dd,high-balance)
   max_loss=min(max_loss,entry['pnl'])
  result[policy]={'selected':len(selected),'settled':len(known),'unverified':len(selected)-len(known),'wins':sum(x['status']=='WIN' for x in known),'losses':sum(x['status']=='LOSS' for x in known),'net_pnl':round(sum(x['pnl'] for x in known),2),'total_cost':round(sum(x['stressed_cost'] for x in known),2),'max_observed_drawdown':round(max_dd,2),'hypothetical_ending_balance':round(balance,2),'one_full_cost_loss_stress':round(sum(x['pnl'] for x in known)-max((x['stressed_cost'] for x in known),default=0),2)}
 errors=rows(root/'eth_v42_errors.csv')
 output={'generated_utc':datetime.now(timezone.utc).isoformat(),'mode':'SHADOW_ONLY','paper_position_max_usd':1000,'live_order_support':False,'data_source':str(root),'quote_side_observations':len(quoted),'markets_in_quotes':len(set(r.get('ticker') for r in quoted)),'checkpoints':len(candidates),'officially_settled_market_count':len(official),'error_records':len(errors),'error_types':dict(Counter(r.get('error_type','') for r in errors)),'slippage_stress_cents_per_contract':slippage_cents,'policies':result,'disclaimers':['Historical snapshots and quoted depth are not guaranteed fills.','Outcomes in same market are correlated; one entry per market is selected.','Full bankroll constraints, settlement availability, fees, and liquidity are not simulated perfectly.','Historical fitted policies do not establish calibrated probabilities or a live edge.','No real orders or account access in this analyzer.']}
 # Actual approved simulated positions, not the separate counterfactual candidate replays.
 config_path=root/'SESSION_CONFIG.json'
 config=json.loads(config_path.read_text(encoding='utf8')) if config_path.exists() else {}
 ledger_name=config.get('ledger_file','eth_v7c_paper_ledger.sqlite3')
 ledger=root/Path(ledger_name).name
 if not ledger.exists(): raise FileNotFoundError(f'Paper ledger missing: {ledger}. Refusing zero-trade report.')
 ledger_entries=[]
 if ledger.exists():
  conn=sqlite3.connect(str(ledger))
  try:
   for tick,side,cost,n,status,pnl in conn.execute('SELECT ticker,side,cost_cents,contracts,status,realized_pnl_cents FROM intents ORDER BY created_utc'):
    ledger_entries.append({'ticker':tick,'side':side,'entry_cost_usd':cost/100,'contracts':n,'status':status,'realized_pnl_usd':pnl/100 if status in ('SETTLED','STOPPED','PARTIALLY_EXITED') else None,'verified_fill':False})
  finally:conn.close()
 completed=[e for e in ledger_entries if e['realized_pnl_usd'] is not None]
 output['actual_approved_paper_ledger']={'approved':len(ledger_entries),'stopped':sum(e['status']=='STOPPED' for e in completed),'settled':sum(e['status']=='SETTLED' for e in completed),'open':sum(e['status']=='OPEN' for e in ledger_entries),'realized_modeled_pnl_usd':round(sum(e['realized_pnl_usd'] for e in completed),2),'fills_verified':False,'positions':ledger_entries}
 output['report_warning']='Policy tables are hypothetical checkpoint replays that do NOT incorporate the V5 stop-out decisions. Use actual_approved_paper_ledger for stopped positions and session P/L.'
 with (root/'V5_PAPER_LEDGER.csv').open('w',newline='',encoding='utf8') as f:
  w=csv.DictWriter(f,fieldnames=['ticker','side','entry_cost_usd','contracts','status','realized_pnl_usd','verified_fill']);w.writeheader();w.writerows(ledger_entries)
 with (root/'ETH_V42_report.json').open('w',encoding='utf8') as f:json.dump(output,f,indent=2)
 with (root/'ETH_V42_selected_positions.csv').open('w',newline='',encoding='utf8') as f:
  fields=['policy','ticker','checkpoint_s','side','status','contracts','cost','stressed_cost','pnl','cushion_pct','scaled_gap'];w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(flat)
 trs=''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in [k,r['selected'],r['wins'],r['losses'],r['unverified'],f"${r['net_pnl']:+.2f}",f"${r['one_full_cost_loss_stress']:+.2f}"] )+'</tr>' for k,r in result.items())
 page=f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>ETH V42.2 Shadow Audit</title><style>body{{font-family:system-ui;max-width:1100px;margin:40px auto;color:#1c2733;padding:0 22px}}h1{{color:#183858}}table{{border-collapse:collapse;width:100%}}td,th{{padding:12px;border-bottom:1px solid #ddd;text-align:left}}th{{background:#ecf2f7}}.alert{{background:#fff5dd;border-left:4px solid #b97813;padding:16px;margin-top:20px}}.stats{{display:flex;gap:35px;flex-wrap:wrap;margin:20px 0}}.stat{{font-size:1.6rem;font-weight:bold}}small{{color:#5b6875}}</style><h1>ETH V42.2 — Paper Research Report</h1><p>Generated {html.escape(output['generated_utc'])}</p><div class="alert"><strong>NO LIVE ORDERS.</strong> Quotes are hypothetical fills. A winning streak cannot establish low risk.</div><div class="stats"><div>Quote-side records<div class="stat">{len(quoted):,}</div></div><div>Markets observed<div class="stat">{output['markets_in_quotes']}</div></div><div>Checkpoint-side records<div class="stat">{len(candidates):,}</div></div><div>Errors<div class="stat">{len(errors)}</div></div></div><h2>One selected entry per market — four predefined policies</h2><table><thead><tr><th>Policy</th><th>Selected</th><th>Wins</th><th>Losses</th><th>Unverified</th><th>Net P/L*</th><th>Stress: subtract one full-cost loss**</th></tr></thead><tbody>{trs}</tbody></table><p><small>* Hypothetical, after existing modeled fees and additionally {slippage_cents}¢/contract slippage stress. ** Illustrates tail exposure, not a probability estimate or full simulation.</small></p><h2>Limitations</h2><ul>{''.join('<li>'+html.escape(d)+'</li>' for d in output['disclaimers'])}</ul></html>'''
 (root/'ETH_V42_report.html').write_text(page,encoding='utf8')
 return output

def main():
 p=argparse.ArgumentParser();p.add_argument('--folder',type=Path,default=ROOT);p.add_argument('--slippage-cents',type=float,default=0);a=p.parse_args()
 if a.slippage_cents<0: p.error('slippage cannot be negative')
 result=analyze(a.folder,slippage_cents=a.slippage_cents)
 print('BIGBET V5 PAPER AUDIT — no live orders')
 x=result.get('actual_approved_paper_ledger',{});print(f"ACTUAL_APPROVED_PAPER: approved={x.get('approved',0)} stopped={x.get('stopped',0)} settled={x.get('settled',0)} open={x.get('open',0)} modeled_pnl=${x.get('realized_modeled_pnl_usd',0):+.2f}")
 for name,r in result['policies'].items():print(f"{name}: selected={r['selected']} wins={r['wins']} losses={r['losses']} unverified={r['unverified']} pnl=${r['net_pnl']:+.2f}")
 print('Reports:',a.folder/'ETH_V42_report.html',a.folder/'ETH_V42_report.json')
if __name__=='__main__':main()
