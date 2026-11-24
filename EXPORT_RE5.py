"""Command-line export (never includes keys). Default: newest session.

  python .\\EXPORT_RE5.py --env prod
  python .\\EXPORT_RE5.py --env prod --session 20261103_073002
"""
import argparse, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from RE5 import ops  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--env', choices=['demo', 'prod', 'paper'], default='prod')
    ap.add_argument('--session', default=None)
    args = ap.parse_args()
    ops.apply_key_env()
    rt = ops.runtime_dir(args.env)
    sessions = ops.session_dirs(rt)
    if not sessions:
        raise SystemExit(f'No sessions in {rt}')
    session = rt / 'sessions' / args.session if args.session else sessions[-1]
    print('Export written:', ops.export_session(rt, session))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
