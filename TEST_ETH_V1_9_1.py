"""ETH V1.9.1 tests: a momentary stale ETH price reading no longer halts the session.
python TEST_ETH_V1_9_1.py"""
import importlib.util, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
import TEST_RE5_LOOP as LOOP  # noqa: E402
from RE5 import ops  # noqa: E402
REAL_MSG = 'Stale or implausible ETHUSD_RTI value; aborting signal'      # exact text from Nov 14 11:52 AM


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'RE5' / 'APP' / file)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class Classification(unittest.TestCase):
    def test_version(self):
        self.assertEqual(ops.VERSION, 'ETH V1.9.4 / RE5.5')
        self.assertIn("'TEST_ETH_V1_9_1.py'", (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8'))
    def test_stale_feed_is_a_short_glitch(self):
        g = load('c191', 'ETH_V42_Collector.py').is_connection_glitch
        self.assertTrue(g(ValueError(REAL_MSG)))
        self.assertFalse(g(ValueError('invalid literal for int()')))         # other bugs still fail closed
        self.assertFalse(g(RuntimeError(REAL_MSG)))                         # only the price-check ValueError
    def test_auto_restart_recognises_the_halt_text(self):
        self.assertTrue(ops.is_connection_problem('OPERATIONAL_ERROR ValueError: ' + REAL_MSG))
        self.assertFalse(ops.is_connection_problem('OPERATIONAL_ERROR ValueError: something else'))
        self.assertFalse(ops.is_connection_problem('DAILY_LOSS_LIMIT'))
    def test_price_check_itself_unchanged(self):
        src = (ROOT / 'RE5' / 'APP' / 'kalshi_eth_bot_v37.py').read_text(encoding='utf-8')
        self.assertIn("abs(time.time()-timestamp) > 5", src)                 # still rejects readings older than 5 s


class EngineRidesOutFeedBlips(LOOP.Loop):
    """Full engine loop with the ETH price feed returning a stale reading for a while."""
    def _with_stale_feed(self, start, end):
        orig = LOOP.load_collector
        def patched(app_dir, ft, scen):
            m = orig(app_dir, ft, scen)
            inner = m.base.Client
            class Stale(inner):
                def ethrti(self):
                    if LOOP.START + start <= ft.t < LOOP.START + end:
                        raise ValueError(REAL_MSG)
                    return super().ethrti()
            m.base.Client = Stale
            return m
        LOOP.load_collector = patched
        self.addCleanup(setattr, LOOP, 'load_collector', orig)

    def test_short_blip_no_halt_market_skipped(self):
        self._with_stale_feed(100, 130)                    # 30 s of stale readings inside this market
        state, fake, out, tmp = self.run_loop(crash=False, fill_script=[10])
        self.assertTrue(any('CONNECTION_GLITCH ValueError' in l for l in out), '\n'.join(out[-30:]))
        self.assertFalse(any('FAIL CLOSED' in l for l in out))
        self.assertFalse(state['halted'] and state['reason'].startswith('OPERATIONAL_ERROR'))
        self.assertEqual([p for p in fake.posts if not p['reduce_only']], [])   # no entry in that market
        self.assertTrue(any('SKIP_MARKET_AFTER_GLITCH' in l for l in out))

    def test_long_feed_problem_still_stops_safely(self):
        self._with_stale_feed(100, 420)                    # 320 s: longer than the 120 s grace
        d = Path(tempfile.mkdtemp(prefix='v191loop_'))
        with mock.patch.dict(os.environ, {'RE5_TEST_TMP': str(d)}):
            with self.assertRaises(SystemExit):
                self.run_loop(crash=False, fill_script=[10])


if __name__ == '__main__':
    unittest.main(verbosity=1)
