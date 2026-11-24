"""ETH V1.9 tests: "steady 60 s" entry filter. python TEST_ETH_V1_9.py"""
import importlib.util, sys, unittest
from decimal import Decimal as D
from pathlib import Path
ROOT = Path(__file__).resolve().parent
APP = ROOT / 'RE5' / 'APP'
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(APP))
spec = importlib.util.spec_from_file_location('eth_collector_v19', APP / 'ETH_V42_Collector.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
T0 = 1_800_000_000.0


def hist(*pairs):
    return [(T0 + t, None if a is None else D(a)) for t, a in pairs]


class Steady(unittest.TestCase):
    def test_constants_and_version(self):
        import RE5.ops as ops
        self.assertEqual((m.STEADY_ASK, m.STEADY_SECONDS), (D('0.90'), 60))
        self.assertEqual(ops.VERSION, 'ETH V1.9.4 / RE5.5')
        src = (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8')
        self.assertIn('V1.9.4 / V7-D Rule C', src); self.assertIn("'TEST_ETH_V1_9.py'", src)
        self.assertEqual(m.ETH_CROSS_HOLD_S, 10)                       # V1.8 stop unchanged

    def test_steady_market_allowed(self):
        h = hist(*[(t, '0.95') for t in range(0, 60, 2)])
        self.assertTrue(m.steady_ok(h, T0 + 60))

    def test_nov13_4am_market_is_skipped(self):
        # Real path (YES ask) before the 150 s entry at 08:57:27 UTC: 0.88/0.87 at 240-221 s, then 0.906 ... 0.947, 0.89
        h = hist((0, '0.880'), (10, '0.870'), (20, '0.906'), (30, '0.906'), (40, '0.938'), (50, '0.947'), (58, '0.890'))
        self.assertFalse(m.steady_ok(h, T0 + 60))

    def test_old_dip_outside_window_does_not_count(self):
        h = hist((0, '0.80'), (5, '0.85'), (10, '0.93'), (40, '0.95'), (65, '0.96'))
        self.assertTrue(m.steady_ok(h, T0 + 70))                      # window = 10..70 s: all >= 90c

    def test_missing_quote_in_window_blocks(self):
        h = hist((20, '0.95'), (30, None), (50, '0.96'))
        self.assertFalse(m.steady_ok(h, T0 + 60))

    def test_no_history_blocks(self):
        self.assertFalse(m.steady_ok([], T0))
        self.assertFalse(m.steady_ok(hist((0, '0.95')), T0 + 120))    # nothing seen in the last 60 s

    def test_exactly_90c_is_steady(self):
        self.assertTrue(m.steady_ok(hist((10, '0.90'), (30, '0.91')), T0 + 60))

    def test_current_poll_not_included(self):
        # the poll being judged (at `now`) is not part of its own window
        self.assertTrue(m.steady_ok(hist((10, '0.95'), (60, '0.50')), T0 + 60))


class Wiring(unittest.TestCase):
    def test_filter_applies_to_normal_checkpoints_only(self):
        src = (APP / 'ETH_V42_Collector.py').read_text(encoding='utf-8')
        self.assertIn("if c['status']=='SHADOW_CANDIDATE' and cp!=EARLY_HOLD_CP and not _steady:", src)
        self.assertIn("NOT_STEADY_60S", src)
        i_steady = src.index('_steady=steady_ok(_ah,now,reg_steady)')   # V1.9.4: threshold from the mood regime
        i_append = src.index('_ah.append((now,')
        self.assertLess(i_steady, i_append)                             # judged before this poll is added
        self.assertIn('ask_hist={}', src)


if __name__ == '__main__':
    unittest.main(verbosity=1)
