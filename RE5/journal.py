"""Crash-safe JSON journal for the single live position. Written BEFORE every exchange write.

State fields
  day            UTC date the daily counters belong to
  halted/reason  entries blocked (exits and settlement still run unless an order is unresolved)
  pending        an order sent (or about to be sent) whose outcome is not yet reconciled
  position       the open position, including partial exits already realised
  loss_count, realized_usd   daily counters (realized_usd is signed P/L for the day)
  seen           markets already entered today (never re-enter the same market)
  history        last closed trades with real exchange P/L
"""
import json, os, uuid
from datetime import datetime, timezone
from pathlib import Path


def utcday():
    return datetime.now(timezone.utc).date().isoformat()


def fresh_state():
    return dict(schema='RE5-1', day=utcday(), halted=False, reason='', pending=None, position=None,
                loss_count=0, realized_usd='0', seen=[], history=[], attempts={}, stats={})


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(fresh_state())
        s = self.read()
        if s.get('schema') != 'RE5-1':
            raise RuntimeError('journal schema mismatch: use a new RE5 journal file, do not reuse RE3/RE4 files')

    def read(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def save(self, state):
        tmp = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def update(self, **changes):
        s = self.read(); s.update(changes); self.save(s); return s

    def halt(self, reason):
        s = self.read()
        if not s['halted']:
            s['halted'] = True; s['reason'] = str(reason)
        else:
            s['reason'] = (s['reason'] + ' | ' + str(reason))[-500:]
        self.save(s)
