"""ETH V1.4 tests (ETH ONLY):
  Rule C = Rule B + Early-Hold (420-240 s left, 90-99c, lead >= 1.5x, favored bid >= 90c for the previous 2 minutes)
  stop-loss sells priced from the live order book; 0.5 s checks while holding in the last 3 minutes.
Offline; no network, no real keys.   Run from this folder:  python TEST_ETH_V1_4.py
"""
import importlib.util, json, sys, unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
from TEST_RE5 import Base, cand, T  # noqa: E402
import TEST_RE5_LOOP as LOOP  # noqa: E402
from RE5 import controller as ctl, ops  # noqa: E402


def collector():
    spec = importlib.util.spec_from_file_location('coll_eth14', ROOT / 'RE5' / 'APP' / 'ETH_V42_Collector.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def raw(ask, z, secs='400', side='no'):
    gap = '-4.17' if side == 'no' else '4.17'
    return dict(ticker='KXETH15M-26NOV082015-15', time_utc='t', seconds_left=secs, side=side, eth='2400',
                strike='2404.17' if side == 'no' else '2395.83', gap_abs='4.17', gap_signed=gap, sigma_min='1',
                scaled_gap=str(z), minute_pairs='9', ask=str(ask), quoted_depth='500')


class EarlyHoldRule(unittest.TestCase):
    def setUp(self):
        self.m = collector()

    def test_constants_and_gates(self):
        m = self.m
        self.assertEqual((m.EARLY_HOLD_FROM, m.EARLY_HOLD_TO, m.EARLY_HOLD_SECONDS), (420, 240, 120))
        self.assertEqual((m.EARLY_HOLD_MIN_SCALED_GAP, m.EARLY_HOLD_BID), (D('2.0'), D('0.90')))   # V1.9.2: was 1.5
        self.assertEqual(m.scaled_gate_for_checkpoint(420), D('2.0'))   # V1.9.2: was 1.5
        self.assertEqual(m.scaled_gate_for_checkpoint(180), D('2.0')); self.assertEqual(m.scaled_gate_for_checkpoint(120), D('1.6'))
        self.assertEqual((m.P_MIN, m.P_MAX), (D('0.90'), D('0.99')))                     # Rule B unchanged
        self.assertEqual(m.CHECKPOINTS, (180, 150, 120, 105, 90, 60, 45, 30))

    def test_hold_window(self):
        ok = self.m.early_hold_ok
        steady = [(t, D('0.93')) for t in range(0, 200, 2)]
        self.assertTrue(ok(steady, 199))
        self.assertFalse(ok(steady[-30:], 199))                                          # watched < 2 minutes
        dip = [(t, D('0.89') if t == 150 else b) for t, b in steady]
        self.assertFalse(ok(dip, 199))                                                   # one dip below 90c inside the window
        old_dip = [(t, D('0.80') if t < 60 else b) for t, b in steady]
        self.assertTrue(ok(old_dip, 199))                                                # dip older than 2 minutes is ignored
        self.assertFalse(ok([(t, None if t == 190 else b) for t, b in steady], 199))     # missing bid -> not steady
        self.assertFalse(ok([], 10))

    def test_candidate(self):
        c = self.m.checkpoint_candidate
        self.assertEqual(c(raw('0.93', 2.0), 420)['status'], 'SHADOW_CANDIDATE')          # V1.9.2: 2.0x needed early
        self.assertEqual(c(raw('0.93', 1.81), 420)['status'], 'REJECT')                   # Nov 14 7:15 PM lead: now skipped
        self.assertEqual(c(raw('0.93', 1.5), 420)['status'], 'REJECT')
        self.assertEqual(c(raw('0.93', 1.45), 420)['status'], 'REJECT')
        self.assertEqual(c(raw('0.89', 2.5), 420)['status'], 'REJECT')
        self.assertEqual(c(raw('0.93', 1.5, '120'), 120)['status'], 'REJECT')            # 1.5x is NOT enough at normal checkpoints

    def test_risk_window_allows_early_hold(self):
        import tempfile
        m = self.m
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            with mock.patch.object(m, 'DAILY_DB', Path(d) / 'g.sqlite3', create=True):
                import ETH_V42_Risk as R
                with mock.patch.object(R, 'DAILY_DB', Path(d) / 'g.sqlite3'):
                    con = R.connect(Path(d) / 'i.sqlite3')
                    try:
                        self.assertEqual(R.approve(con, 'KXETH15M-X1', 'yes', '9.40', data_age_seconds=1, market_seconds_left=410, contracts=10)['result'], 'PAPER_APPROVED')
                        with self.assertRaises(R.Rejected):
                            R.approve(con, 'KXETH15M-X2', 'yes', '9.40', data_age_seconds=1, market_seconds_left=430, contracts=10)
                    finally:
                        con.close()


class EarlyHoldLoop(LOOP.Loop):
    """Full ETH engine loop with fake Kalshi + fake clock (market starts with 600 s left, favorite steady at 96.5c)."""
    def setUp(self):
        import RE5.exchange as ex, RE5.controller as c2, builtins
        self._orig = (ex.ExchangeClient, c2.time, builtins.print, LOOP.Scenario.market)
    def tearDown(self):
        import RE5.exchange as ex, RE5.controller as c2, builtins
        ex.ExchangeClient, c2.time, builtins.print, LOOP.Scenario.market = self._orig

    def entries(self, tmp):
        out = []
        for line in (tmp / 'rt' / 're5_live_events.jsonl').read_text().splitlines():
            e = json.loads(line)
            if e.get('event') == 'ORDER_RESULT' and e.get('purpose') == 'ENTRY' and str(e.get('filled')) not in ('0', '', 'None'):
                out.append(e)
        return out

    def test_steady_market_enters_early(self):
        state, fake, out, tmp = self.run_loop(crash=False, fill_script=[10])
        self.assertTrue(any('EARLY_HOLD ticker=' in l for l in out), '\n'.join(out[-30:]))
        posts = [p for p in fake.posts if not p['reduce_only']]
        self.assertEqual(len(posts), 1)                                                  # one trade per market
        import csv
        rows = [r for r in csv.DictReader(open(self.app / 'eth_v42_shadow_candidates.csv', encoding='utf-8'))
                if r['status'] in ('PAPER_APPROVED', 'SETTLED_WIN')]
        self.assertEqual(rows[0]['checkpoint_s'], '420'); self.assertTrue(240 <= float(rows[0]['actual_seconds_left']) <= 420)
        self.assertEqual(state['history'][-1]['result'], 'yes')

    def test_unsteady_market_waits_for_normal_checkpoints(self):
        orig = LOOP.Scenario.market
        def wobbly(sc):
            m = orig(sc); left = sc.close - sc.clock.t
            if 300 <= left <= 330:                                                       # favorite dips to 85c inside the window
                m['yes_bid_dollars'] = '0.85'; m['no_ask_dollars'] = '0.15'
            return m
        LOOP.Scenario.market = wobbly
        state, fake, out, tmp = self.run_loop(crash=False, fill_script=[10])
        import csv
        rows = [r for r in csv.DictReader(open(self.app / 'eth_v42_shadow_candidates.csv', encoding='utf-8'))
                if r['status'] in ('PAPER_APPROVED', 'SETTLED_WIN')]
        self.assertTrue(rows, '\n'.join(out[-30:]))
        first = rows[0]
        if first['checkpoint_s'] == '420':                                               # allowed only BEFORE the dip, never right after it
            self.assertGreater(float(first['actual_seconds_left']), 330)
        else:
            self.assertIn(int(first['checkpoint_s']), (180, 150, 120, 105, 90, 60, 45, 30))

    def test_stop_path_still_works(self):
        state, fake, out, tmp = self.run_loop(crash=True, fill_script=[10, 4, 6])
        self.assertEqual(state['history'][-1]['result'], 'FULLY_EXITED'); self.assertTrue(state['halted'])


class FreshExitPrice(Base):
    def test_sell_uses_live_book_not_old_bid(self):
        self.c.entry(cand()); self.ex.fill_script = [10]
        self.ex.book['yes'] = [(D('0.59'), D('114')), (D('0.58'), D('37'))]
        self.c.on_poll(ticker=T, seconds_left=53, held_bid=D('0.72'), stop_reason='ETH_CROSSED_STRIKE')
        self.assertEqual([p for p in self.ex.posts if p['reduce_only']][-1]['yes_price'], D('0.57'))
    def test_depth_for_whole_position(self):
        self.c.entry(cand()); self.ex.fill_script = [10]
        self.ex.book['yes'] = [(D('0.71'), D('0.87')), (D('0.68'), D('24.75'))]
        self.c.on_poll(ticker=T, seconds_left=53, held_bid=D('0.72'), stop_reason='X')
        self.assertEqual([p for p in self.ex.posts if p['reduce_only']][-1]['yes_price'], D('0.66'))
    def test_fallback_when_book_unreadable(self):
        self.c.entry(cand()); self.ex.fill_script = [10]; self.ex.book = None
        self.c.on_poll(ticker=T, seconds_left=53, held_bid=D('0.80'), stop_reason='X')
        self.assertEqual([p for p in self.ex.posts if p['reduce_only']][-1]['yes_price'], D('0.78'))
    def test_no_book_no_bid_waits(self):
        self.c.entry(cand()); self.ex.book = None
        self.c.on_poll(ticker=T, seconds_left=53, held_bid=None, stop_reason='X')
        self.assertEqual([p for p in self.ex.posts if p['reduce_only']], [])


class FastChecksAndIsolation(unittest.TestCase):
    def test_poll_pause(self):
        m = collector()
        self.assertEqual((m.poll_pause(True, 120), m.poll_pause(True, 181), m.poll_pause(False, 60)), (0.5, 2, 2))
    def test_eth_only(self):
        from RE5 import asset
        self.assertEqual(asset.P['series'], 'KXETH15M')
        self.assertIn('KXBTC15M', asset.P['protect_series'])
        self.assertEqual(ops.VERSION, 'ETH V1.9.4 / RE5.5')
        app = (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8')
        self.assertIn('V1.9.4 / V7-D Rule C', app); self.assertIn("'TEST_ETH_V1_4.py'", app)


if __name__ == '__main__':
    unittest.main(verbosity=1)
