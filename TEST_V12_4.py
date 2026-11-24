"""V12.4 tests: Kalshi settlement lag (payout posted while the positions list still shows the market).
Run from this folder:
python TEST_V12_4.py
"""
import unittest
from TEST_RE5 import Base, cand, T
from RE5.controller import SafetyError, SETTLE_LAG_GRACE_SECONDS


class SettleLag(Base):
    def _settle_with_lag(self):
        self.c.entry(cand()); held, q = self.ex.pos[T]
        self.ex.settle(T, 'yes'); self.ex.pos[T] = (held, q)   # payout posted, position still listed
        self.clock.t += 400

    def test_short_lag_waits_then_closes_without_pause(self):
        self._settle_with_lag()
        self.assertFalse(self.c.poll_settlement())
        s = self.state(); self.assertFalse(s['halted']); self.assertIsNotNone(s['position'])
        self.clock.t += 15; self.ex.pos[T] = ('yes', 0)          # Kalshi catches up
        self.assertTrue(self.c.poll_settlement())
        s = self.state(); self.assertFalse(s['halted']); self.assertIsNone(s['position'])
        kinds = [e['event'] for e in self.events]
        self.assertIn('SETTLE_LAG_WAIT', kinds); self.assertIn('SETTLE_LAG_CLEARED', kinds)

    def test_no_new_entry_while_waiting(self):
        self._settle_with_lag()
        self.assertFalse(self.c.poll_settlement())
        self.assertFalse(self.c.entry(dict(cand(), ticker=T)))   # still holding: no second position
        self.assertIsNotNone(self.state()['position'])

    def test_long_lag_still_pauses(self):
        self._settle_with_lag()
        self.assertFalse(self.c.poll_settlement())
        for _ in range(int(SETTLE_LAG_GRACE_SECONDS // 15) - 1):
            self.clock.t += 15; self.assertFalse(self.c.poll_settlement())
        self.clock.t += 15
        with self.assertRaises(SafetyError): self.c.poll_settlement()
        s = self.state(); self.assertTrue(s['halted']); self.assertIn('SETTLED_BUT_POSITION_REMAINS', s['reason'])
        self.assertIsNotNone(s['position'])

    def test_no_lag_unchanged(self):
        self.c.entry(cand()); self.ex.settle(T, 'yes'); self.clock.t += 400
        self.assertTrue(self.c.poll_settlement())
        self.assertNotIn('SETTLE_LAG_WAIT', [e['event'] for e in self.events])


if __name__ == '__main__':
    unittest.main(verbosity=1)
