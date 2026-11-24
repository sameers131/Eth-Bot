"""ETH V1.3 tests: Rule B (entries 90-99c, lead >= 2.0x at 180/150 s and >= 1.6x at later checkpoints).
Everything else is V12.7, including: any loss (a stop-loss exit too) stops new trades for the day.
Offline; no network, no real keys.
Run from this folder:
python TEST_ETH_V1_3.py
"""
import importlib.util, sys, unittest
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
from TEST_RE5 import Base, cand, T  # noqa: E402
from RE5 import controller as ctl, exchange, scaling  # noqa: E402


def collector():
    spec = importlib.util.spec_from_file_location('coll_eth13', ROOT / 'RE5' / 'APP' / 'ETH_V42_Collector.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def raw(ask, z, side='no', depth='500'):
    gap = '-4.17' if side == 'no' else '4.17'
    return dict(ticker='KXETH15M-26NOV062015-15', time_utc='t', seconds_left='120', side=side, eth='2400',
                strike='2404.17' if side == 'no' else '2395.83', gap_abs='4.17', gap_signed=gap, sigma_min='1',
                scaled_gap=str(z), minute_pairs='9', ask=str(ask), quoted_depth=depth)


class RuleBSettings(unittest.TestCase):
    def test_constants(self):
        m = collector()
        self.assertEqual((m.P_MIN, m.P_MAX), (D('0.90'), D('0.99')))
        self.assertEqual(m.scaled_gate_for_checkpoint(180), D('2.0')); self.assertEqual(m.scaled_gate_for_checkpoint(150), D('2.0'))
        self.assertTrue(all(m.scaled_gate_for_checkpoint(c) == D('1.6') for c in (120, 105, 90, 60, 45, 30)))
        self.assertEqual(m.MIN_CUSHION_PCT, D('0.03'))                     # unchanged
        self.assertEqual(m.CHECKPOINTS, (180, 150, 120, 105, 90, 60, 45, 30))  # unchanged
        self.assertEqual((ctl.ENTRY_MIN_PRICE, ctl.ENTRY_MAX_PRICE), (D('0.90'), D('0.99')))
    def test_size_caps(self):
        self.assertEqual(exchange.MAX_NOTIONAL, D('10.00')); self.assertEqual(exchange.MAX_CONTRACTS, 11)
        self.assertEqual([scaling.max_contracts(u) for u in (5, 10, 15, 20, 40)], [5, 11, 16, 22, 44])   # ETH V1.7 sizes
        self.assertEqual(ctl.DAILY_MAX_LOSS_USD, D('5.00'))                 # unchanged at $10


class RuleBDecisions(unittest.TestCase):
    def setUp(self):
        self.m = collector()
    def ok(self, ask, z, cp):
        return self.m.checkpoint_candidate(raw(ask, z), cp)['status'] == 'SHADOW_CANDIDATE'
    def test_price_range(self):
        self.assertTrue(self.ok('0.90', 1.7, 120)); self.assertTrue(self.ok('0.93', 1.7, 120)); self.assertTrue(self.ok('0.99', 1.7, 120))
        self.assertFalse(self.ok('0.89', 1.7, 120)); self.assertFalse(self.ok('0.995', 1.7, 120))
    def test_lead_thresholds(self):
        self.assertTrue(self.ok('0.96', 1.60, 120)); self.assertFalse(self.ok('0.96', 1.55, 120))
        self.assertFalse(self.ok('0.96', 1.50, 30))                      # 1.4 used to pass; 1.6 now required
        self.assertTrue(self.ok('0.96', 2.00, 180)); self.assertFalse(self.ok('0.96', 1.95, 180)); self.assertFalse(self.ok('0.96', 1.85, 150))
    def test_paper_size_at_90c_stays_under_10_dollars(self):
        c = self.m.checkpoint_candidate(raw('0.90', 1.7), 120)
        self.assertEqual(c['contracts'], 11); self.assertLessEqual(D(c['total_cost']), D('10'))
        self.assertIn('ASK_OUTSIDE_90_TO_99_CENTS', self.m.checkpoint_candidate(raw('0.85', 1.7), 120)['reason'])


class RuleBLive(Base):
    def test_live_entry_at_90c_stays_within_10_dollars(self):
        self.ex.set_book(yes_ask='0.90')                      # order goes at best ask + 1c = 0.91
        self.assertTrue(self.c.entry(cand(ask='0.90', n=11)))
        p = self.ex.posts[0]
        self.assertEqual(p['yes_price'], D('0.91')); self.assertLessEqual(p['count'], 11)
        self.assertLessEqual(p['count'] * p['yes_price'] + ctl.fee_ceiling(p['yes_price'], p['count']), D('10'))
        pos = self.state()['position']
        self.assertLessEqual(D(pos['entry_gross']) + D(pos['entry_fees']), D('10'))
    def test_live_entry_below_90c_skips(self):
        self.ex.set_book(yes_ask='0.89')
        self.assertFalse(self.c.entry(cand(ask='0.90', n=11))); self.assertEqual(self.ex.posts, [])
    def test_stop_loss_stops_new_trades_for_the_day(self):
        self.ex.set_book(yes_ask='0.92')
        self.assertTrue(self.c.entry(cand(ask='0.92', n=10)))
        self.ex.fill_script = [10]
        self.c.on_poll(ticker=T, seconds_left=60, held_bid=D('0.80'), stop_reason='PRICE_BACKUP_88C_ETH_LT_1X')
        s = self.state()
        self.assertIsNone(s['position']); self.assertEqual(s['loss_count'], 1)
        self.assertTrue(s['halted']); self.assertTrue(s['reason'].startswith('DAILY_LOSS'))
        self.assertFalse(self.c.entry(dict(cand(ask='0.95'), ticker='KXETH15M-26NOV011930-30')))   # next market: refused
        self.assertEqual(sum(1 for p in self.ex.posts if not p['reduce_only']), 1)


if __name__ == '__main__':
    unittest.main(verbosity=1)
