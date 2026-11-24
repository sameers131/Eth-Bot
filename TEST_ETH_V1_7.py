"""ETH V1.7 tests (ETH ONLY):
  * free trade size $5-$40 in $5 steps; no ladder lock and no automatic step-down
  * every cap still follows the ONE chosen size (order $, contracts, daily loss cap, weekly stop, top-ups)
  * new ETH colors (purple paper / orange live), different from the BTC app
Each size is checked in its OWN process, exactly like a real session start.
Offline; no network, no real keys.   Run from this folder:  python TEST_ETH_V1_7.py
"""
import json, os, subprocess, sys, tempfile, unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
from RE5 import scaling, ops  # noqa: E402

SIZES = (5, 10, 15, 20, 25, 30, 35, 40)
BAD = ('0', '3', '12', '45', '50', '60', '100', 'abc')

PROBE = r'''
import json, sys, importlib.util, tempfile
from decimal import Decimal as D
from pathlib import Path
ROOT = Path(sys.argv[1]); sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
from RE5 import exchange, controller as ctl, scaling
import ETH_V42_Risk as R
spec = importlib.util.spec_from_file_location('c', ROOT / 'RE5' / 'APP' / 'ETH_V42_Collector.py'); col = importlib.util.module_from_spec(spec); spec.loader.exec_module(col)
from TEST_RE5 import FakeKalshi, Clock, cand
out = dict(trade=exchange.TRADE_USD, notional=str(exchange.MAX_NOTIONAL), contracts=exchange.MAX_CONTRACTS,
           arm=exchange.PROD_ARM_PHRASE, daily=str(ctl.DAILY_MAX_LOSS_USD), budget=str(col.BUDGET), paper_max=str(R.MAX_PAPER_USD),
           weekly=str(scaling.weekly_stop_amount(exchange.TRADE_USD)), enforced=scaling.enforced(exchange.TRADE_USD), entries=[])
for ask in ('0.90', '0.95', '0.99'):
    d = Path(tempfile.mkdtemp()); clock = Clock(); ex = FakeKalshi(clock)
    ex.set_book(yes_ask=ask)
    c = ctl.Controller(ex, d / 'j.json', clock=clock, sleep=clock.sleep, log=lambda *_: None)
    n = exchange.MAX_CONTRACTS
    ok = c.entry(cand(ask=ask, n=n))
    p = ex.posts[0] if ex.posts else None
    pos = c.j.read()['position']
    out['entries'].append(dict(ask=ask, ok=ok, count=(p or {}).get('count'), price=str((p or {}).get('yes_price')),
                               cost=str(D(pos['entry_gross']) + D(pos['entry_fees'])) if pos else None))
con = R.connect(Path(tempfile.mkdtemp()) / 'i.sqlite3')
try:
    R.approve(con, 'KXETH15M-OK', 'yes', str(D(exchange.TRADE_USD) - D('0.01')), data_age_seconds=1, market_seconds_left=120, contracts=1); out['paper_ok'] = True
except Exception as e:
    out['paper_ok'] = repr(e)
try:
    R.approve(con, 'KXETH15M-BIG', 'yes', str(D(exchange.TRADE_USD) + D('0.01')), data_age_seconds=1, market_seconds_left=120, contracts=1); out['paper_over'] = 'ACCEPTED'
except Exception as e:
    out['paper_over'] = type(e).__name__
con.close()
print('PROBE' + json.dumps(out, default=str))
'''


def probe(size):
    home = tempfile.mkdtemp(prefix='eth17home_')
    env = dict(os.environ, BIGBET_TRADE_USD=str(size), HOME=home, USERPROFILE=home, BIGBET_RE5_LIVE='')
    r = subprocess.run([sys.executable, '-W', 'ignore', '-c', PROBE, str(ROOT)], env=env, capture_output=True, text=True, timeout=120)
    line = [l for l in r.stdout.splitlines() if l.startswith('PROBE')]
    return (json.loads(line[-1][5:]) if line else None), r


class EverySize(unittest.TestCase):
    def test_every_size_end_to_end(self):
        for size in SIZES:
            with self.subTest(size=size):
                out, r = probe(size)
                self.assertIsNotNone(out, r.stderr[-1500:])
                self.assertEqual(out['trade'], size)
                self.assertEqual(D(out['notional']), D(size)); self.assertEqual(D(out['budget']), D(size)); self.assertEqual(D(out['paper_max']), D(size))
                self.assertEqual(out['contracts'], int(D(size) / D('0.90')))
                self.assertEqual(out['arm'], f'ARM-PROD-{size}USD')
                self.assertEqual(D(out['daily']), D(size) / 2)
                self.assertEqual(D(out['weekly']), D(size))
                self.assertEqual(out['enforced'], size > 10)                       # blackout / weekly stop ON above $10 (unchanged rule)
                for e in out['entries']:
                    self.assertTrue(e['ok'], (size, e))
                    self.assertLessEqual(D(e['cost']), D(size), (size, e))           # never more than the chosen size, fees included
                    self.assertLessEqual(D(str(e['count'])), out['contracts'])
                self.assertIs(out['paper_ok'], True, out['paper_ok'])
                self.assertEqual(out['paper_over'], 'Rejected')

    def test_bad_sizes_refused_everywhere(self):
        for bad in BAD:
            with self.subTest(bad=bad):
                out, r = probe(bad)
                self.assertIsNone(out, f'size {bad} was accepted')
        for bad in (0, 3, 12, 45, 60, 100):
            with self.assertRaises(ValueError):
                scaling.check_size(bad)


class NoLadderLock(unittest.TestCase):
    def setUp(self):
        self.rt = Path(tempfile.mkdtemp(prefix='eth17rt_'))
    def test_all_sizes_allowed_from_fresh(self):
        st = scaling.ladder_status(self.rt)
        self.assertEqual(st['allowed'], list(SIZES)); self.assertFalse(st['eligible']); self.assertIsNone(st['stepped_down'])
    def test_no_step_down_after_losses(self):
        for i in range(5):
            scaling.record_close(self.rt, dict(closed_at=1000 + i, ticker=f'KXETH15M-L{i}', pnl='-2.20', exited_qty=10), 20)
        st = scaling.ladder_status(self.rt)
        self.assertEqual(st['allowed'], list(SIZES)); self.assertIsNone(st['stepped_down'])
        self.assertFalse((self.rt / scaling.LADDER_NAME).exists())                   # nothing written, nothing locked
        self.assertEqual(st['losses_recent'], 5); self.assertEqual(st['total_trades'], 5)
    def test_weekly_stop_still_uses_chosen_size(self):
        for i in range(3):
            scaling.record_close(self.rt, dict(closed_at=__import__('time').time() - 60 * i, ticker=f'KXETH15M-W{i}', pnl='-7.00', exited_qty=10), 20)
        self.assertTrue(scaling.weekly_check(self.rt, 20)[0])                       # -21 <= -20 -> blocked at $20
        self.assertFalse(scaling.weekly_check(self.rt, 25)[0])                      # -21 >  -25
    def test_live_start_not_refused_by_ladder(self):
        src = (ROOT / 'RE5' / 'ops.py').read_text(encoding='utf-8')
        self.assertIn("if usd not in lad['allowed']:", src)                         # the check stays, but allowed = all sizes
        with mock.patch.object(ops, 'runtime_dir', return_value=self.rt), mock.patch.object(ops, 'engine_state', return_value=('none', None)):
            st = ops.ladder_info('prod')
        self.assertEqual(st['allowed'], list(SIZES))


class EthColors(unittest.TestCase):
    def test_colors_differ_from_btc(self):
        src = (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8')
        ns = {}
        for name in ('PAPER_BG', 'PAPER_BTN', 'PANEL ='):
            line = next(l for l in src.splitlines() if l.startswith(name)); exec(line.split('  #')[0], ns)
        self.assertEqual((ns['PAPER_BG'], ns['LIVE_BG']), ('#3d2a8a', '#b34a00'))
        self.assertEqual(ns['PANEL'], '#efe9ff')
        for btc in ("'#0c542b'", "'#7d0f1c'", "'#21b56a'", "'#ab1c28'", "'#fffbd0'", "'#0c341e'"):
            self.assertNotIn(btc, src, 'BTC color still used: ' + btc)
        self.assertIn('PURPLE = ETH PAPER', src); self.assertIn('ORANGE = ETH LIVE', src)
        self.assertNotIn('RED = LIVE', src); self.assertNotIn('GREEN = PAPER', src)
    def test_app_shows_free_choice(self):
        src = (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8')
        self.assertIn('Free size choice', src); self.assertIn("text='No ladder'", src)
        self.assertIn("self.size_box.configure(values=[f'${u}' for u in allowed])", src)
        self.assertIn('V1.9.4 / V7-D Rule C', src); self.assertIn("'TEST_ETH_V1_7.py'", src)
        self.assertEqual(ops.VERSION, 'ETH V1.9.4 / RE5.5')


class NothingElse(unittest.TestCase):
    def test_trading_rules_unchanged(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('c17', ROOT / 'RE5' / 'APP' / 'ETH_V42_Collector.py')
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        self.assertEqual((m.P_MIN, m.P_MAX, m.MIN_SCALED_GAP, m.EARLY_MIN_SCALED_GAP), (D('0.90'), D('0.99'), D('1.6'), D('2.0')))
        self.assertEqual((m.EARLY_HOLD_FROM, m.EARLY_HOLD_TO, m.EARLY_HOLD_MIN_SCALED_GAP), (420, 240, D('2.0')))   # V1.9.2: was 1.5
        self.assertEqual((m.BTC_STOP_MULTIPLE if hasattr(m, 'BTC_STOP_MULTIPLE') else m.ETH_STOP_MULTIPLE, m.STOP_TRIGGER), (D('0.5'), D('0.88')))
        self.assertEqual(m.FAST_POLL_S, 0.5)
        self.assertEqual(scaling.BASE_USD, 10); self.assertEqual(scaling.HARD_MAX_USD, 40)


if __name__ == '__main__':
    unittest.main(verbosity=1)
