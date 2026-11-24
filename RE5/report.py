"""RE5 real-results summary built only from the journal and the live events file (exchange numbers)."""
import json
from decimal import Decimal
from pathlib import Path

D = lambda x: Decimal(str(x))


def load_events(path, since=0.0):
    out = []
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if float(row.get('time', 0)) >= since:
            out.append(row)
    return out


def summary_text(runtime_dir, since=0.0, title='RE5 REAL RESULTS'):
    runtime_dir = Path(runtime_dir)
    jpath = runtime_dir / 're5_journal.json'
    if not jpath.exists():
        return title + ': no journal yet'
    s = json.loads(jpath.read_text(encoding='utf-8'))
    ev = load_events(runtime_dir / 're5_live_events.jsonl', since)
    trades = [h for h in s.get('history', []) if float(h.get('closed_at', 0)) >= since]
    entries = [e for e in ev if e['event'] == 'ORDER_RESULT' and e.get('purpose') == 'ENTRY']
    exits = [e for e in ev if e['event'] == 'ORDER_RESULT' and e.get('purpose') == 'EXIT']
    skips = [e for e in ev if e['event'] == 'ENTRY_SKIPPED']
    lines = ['=' * 78, title, '=' * 78]
    lines.append(f"Entry orders: {len(entries)} | filled: {sum(1 for e in entries if int(e.get('filled', 0)))} | "
                 f"no fill: {sum(1 for e in entries if not int(e.get('filled', 0)))} | exit orders: {len(exits)} | "
                 f"entry skips by live checks: {len(skips)}")
    total = Decimal(0)
    if trades:
        lines.append(f"{'market':<26}{'side':<5}{'qty':>4}{'entry':>8}{'cost':>8}{'exit':>8}{'settle':>8}{'result':>14}{'P/L':>9}")
        for h in trades:
            total += D(h['pnl'])
            lines.append(f"{h['ticker']:<26}{h['held'].upper():<5}{h['qty']:>4}{D(h.get('entry_avg', 0)):>8.3f}{D(h['cost']):>8.2f}"
                         f"{D(h['exit_net']):>8.2f}{D(h['settlement_net']):>8.2f}{h['result']:>14}{D(h['pnl']):>+9.2f}")
    else:
        lines.append('No closed real trades in this period.')
    lines.append(f"Real P/L this period: ${total:+.2f} | today realized: ${D(s.get('realized_usd', 0)):+.2f} | "
                 f"losses today: {s.get('loss_count', 0)}")
    if s.get('position'):
        p = s['position']
        lines.append(f"OPEN POSITION: {p['ticker']} {p['held'].upper()} {p['qty'] - p['exited_qty']} contracts (will settle; run RE5_RECONCILE.py)")
    if s.get('pending'):
        lines.append('UNRESOLVED ORDER in journal: run RE5_RECONCILE.py before the next start')
    lines.append('Halted: ' + (s['reason'] if s.get('halted') else 'no'))
    nofill = [e for e in entries if not int(e.get('filled', 0))]
    if nofill:
        lines.append('No-fill diagnostics (quote age s / book age s / best asks at send):')
        for e in nofill[-5:]:
            lines.append(f"  {e['ticker']} {e.get('held','').upper()} quote_age={e.get('quote_age_s')} book_age={e.get('book_age_s')} "
                         f"limit_yes={e.get('limit_yes_price')} asks={e.get('book_asks_top3')}")
    lines.append('=' * 78)
    return '\n'.join(lines)


DAILY_FIELDS = ['day_utc', 'closed_trades', 'wins', 'losses', 'real_pnl', 'entry_orders', 'entry_filled', 'entry_no_fill',
                'rejects', 'skips', 'exit_orders', 'stops', 'avg_entry_price', 'avg_slip_vs_signal_c', 'avg_latency_s',
                'shard_topups_usd']


def write_daily_summary(runtime_dir):
    """Revision 3: rebuild daily_summary.csv (one row per UTC day) from the full events file and
    live_orders.csv. Safe to call any time; returns the path."""
    import csv
    from datetime import datetime, timezone
    runtime_dir = Path(runtime_dir)
    days = {}

    def day_of(ts):
        return datetime.fromtimestamp(float(ts), timezone.utc).date().isoformat()

    def row(day):
        return days.setdefault(day, dict(day_utc=day, closed_trades=0, wins=0, losses=0, real_pnl=Decimal(0), entry_orders=0,
                                          entry_filled=0, entry_no_fill=0, rejects=0, skips=0, exit_orders=0, stops=0,
                                          _px=[], _slip=[], _lat=[], shard_topups_usd=Decimal(0)))
    for e in load_events(runtime_dir / 're5_live_events.jsonl'):
        try:
            r = row(day_of(e['time'])); ev = e.get('event')
            if ev == 'POSITION_CLOSED':
                r['closed_trades'] += 1; pnl = D(e.get('pnl', 0)); r['real_pnl'] += pnl
                r['wins' if pnl > 0 else 'losses'] += 1 if pnl != 0 else 0
            elif ev == 'ORDER_RESULT' and e.get('purpose') == 'ENTRY':
                r['entry_orders'] += 1
                r['entry_filled' if int(e.get('filled', 0)) else 'entry_no_fill'] += 1
            elif ev == 'ORDER_RESULT' and e.get('purpose') == 'EXIT':
                r['exit_orders'] += 1
            elif ev == 'ORDER_REJECTED':
                r['rejects'] += 1
            elif ev == 'ENTRY_SKIPPED':
                r['skips'] += 1
            elif ev == 'STOP_TRIGGERED':
                r['stops'] += 1
            elif ev == 'SHARD_TOPUP':
                r['shard_topups_usd'] += D(e.get('amount', 0))
        except Exception:
            continue
    orders = runtime_dir / 'live_orders.csv'
    if orders.exists():
        with orders.open(newline='', encoding='utf-8') as f:
            for o in csv.DictReader(f):
                try:
                    if o.get('purpose') != 'ENTRY' or not int(o.get('filled') or 0):
                        continue
                    r = row(o['time_utc'][:10])
                    r['_px'].append(D(o['avg_fill']))
                    if o.get('slip_vs_signal_c'):
                        r['_slip'].append(D(o['slip_vs_signal_c']))
                    if o.get('latency_s'):
                        r['_lat'].append(D(o['latency_s']))
                except Exception:
                    continue
    avg = lambda xs, q: str((sum(xs) / len(xs)).quantize(Decimal(q))) if xs else ''
    out = runtime_dir / 'daily_summary.csv'
    tmp = out.with_suffix('.tmp')
    with tmp.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=DAILY_FIELDS, extrasaction='ignore'); w.writeheader()
        for day in sorted(days):
            r = dict(days[day])
            r['avg_entry_price'] = avg(r['_px'], '0.0001'); r['avg_slip_vs_signal_c'] = avg(r['_slip'], '0.01')
            r['avg_latency_s'] = avg(r['_lat'], '0.001'); r['real_pnl'] = str(r['real_pnl'])
            w.writerow(r)
    import os
    os.replace(tmp, out)
    return out
