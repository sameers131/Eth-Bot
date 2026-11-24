"""Command-line account check (READ-ONLY toward Kalshi). Same as the app's checklist.

  python .\\RE5_RECONCILE.py --env prod
  python .\\RE5_RECONCILE.py --env prod --clear-halt
"""
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from RE5 import ops  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--env', choices=['demo', 'prod'], default='prod')
    ap.add_argument('--clear-halt', action='store_true')
    args = ap.parse_args()
    ok, msg = ops.apply_key_env(); print('Key:', msg)
    if not ok:
        return 2
    r = ops.check_account(args.env)
    for a in r['actions']:
        print('Auto-fix:', a)
    print(r['message'])
    if r['state']:
        print(json.dumps({k: r['state'][k] for k in ('day', 'halted', 'reason', 'pending', 'position', 'loss_count', 'realized_usd')}, indent=2))
    if args.clear_halt:
        if input('Type CLEAR to resume entries: ').strip().upper() != 'CLEAR':
            print('Unchanged.'); return 1
        done, why = ops.clear_halt(args.env); print(why)
        return 0 if done else 2
    return 0 if r['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
