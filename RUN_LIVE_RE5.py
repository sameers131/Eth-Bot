"""Command-line fallback for the V12 app (same engine, same data folder ~/BIGBET_ETH_DATA).

  python .\\RUN_LIVE_RE5.py --env prod --minutes 240

Uses the key saved by the app's Settings (or KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH).
Runs the read-only account check, tops up the ETH shard if needed (option A), asks you to type
LIVE, then starts the engine and shows its log. Ctrl+C here only stops the log view: the engine
keeps running; use STOP via the app or create KILL_SWITCH in the data folder.
"""
import argparse, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from RE5 import ops  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--env', choices=['demo', 'prod'], default='prod')
    ap.add_argument('--minutes', type=int, default=240)
    args = ap.parse_args()
    ok, msg = ops.apply_key_env()
    print('Key:', msg)
    if not ok:
        return 2
    r = ops.check_account(args.env)
    for a in r['actions']:
        print('Auto-fix:', a)
    print('Account check:', r['message'])
    if not r['ok'] or r['state'] is None:
        return 2
    if r['state']['halted']:
        print('Entries paused: ' + r['state']['reason']); return 2
    print('Shard:', ops.topup_plan(args.env)['message'])
    if input('REAL MONEY, max $10 per trade. Type LIVE to start: ').strip() != 'LIVE':
        print('Not started.'); return 1
    t = ops.topup_if_needed(args.env)
    print('Shard:', t['message'])
    if not t['ok']:
        return 2
    sess = ops.start_session(args.env, args.minutes, live=True)
    print(f'Engine started (session {sess["stamp"]}). Log: {sess["log"]}')
    pos = 0
    try:
        while True:
            with open(sess['log'], 'r', encoding='utf-8', errors='replace') as f:
                f.seek(pos); chunk = f.read(); pos = f.tell()
            if chunk:
                print(chunk, end='')
            if sess['proc'].poll() is not None:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print('\nLog view closed. The engine keeps running in the background.')
        return 0
    r = ops.check_account(args.env)
    print('After session:', r['message'], *r['actions'])
    print('Export:', ops.export_session(sess['rt'], sess['session']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
