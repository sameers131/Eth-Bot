"""ETH V1.8 stop-loss tests (offline). python TEST_ETH_V1_8.py"""
import importlib.util, sys, unittest
from decimal import Decimal as D
from pathlib import Path
ROOT = Path(__file__).resolve().parent
APP = ROOT / 'RE5' / 'APP'
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(APP))
spec = importlib.util.spec_from_file_location('eth_collector_v18', APP / 'ETH_V42_Collector.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
F = {'sigma_min': '0.9'}
K = '2061.61'
T0 = 1_800_000_000.0


def at(t):
    m.time.time = lambda: T0 + t


def call(price, t, implied='0.97', rem=120, tk='T', side='no', fresh=True, hist=None):
    at(t)
    ts = T0 + t if fresh else T0 + t - 60
    return m.v7c_stop_reason(side, K, D(price), ts, rem, F, hist, D(implied), ticker=tk, now=T0 + t)


class Rules(unittest.TestCase):
    def setUp(self):
        m._WRONG_SINCE.clear(); self._t = m.time.time
    def tearDown(self):
        m.time.time = self._t

    def test_constants(self):
        self.assertEqual(m.ETH_CROSS_HOLD_S, 10); self.assertEqual(m.ETH_EMERGENCY_PRICE, D('0.50'))
        self.assertEqual((m.ETH_STOP_MULTIPLE, m.STOP_TRIGGER), (D('0.5'), D('0.88')))   # old rule kept for comparison

    def test_brief_cross_is_ignored(self):              # held NO: wrong side = ETH above the target
        for t in (0, 3, 6, 9):
            r, d = call('2062.87', t, tk='A')
            self.assertIsNone(r, t)
        self.assertEqual(d['old_reason'], 'ETH_CROSSED_STRIKE')     # V1.7 would already have sold

    def test_cross_lasting_10s_sells(self):
        call('2062.87', 0, tk='B')
        self.assertEqual(call('2063.00', 10, tk='B')[0], 'ETH_WRONG_SIDE_10S')

    def test_coming_back_resets_the_timer(self):
        call('2062.00', 0, tk='C')
        self.assertIsNone(call('2061.00', 6, tk='C')[0])           # back below the target
        self.assertIsNone(call('2062.00', 8, tk='C')[0])
        self.assertIsNone(call('2062.00', 17, tk='C')[0])          # only 9 s since the new cross
        self.assertEqual(call('2062.00', 18, tk='C')[0], 'ETH_WRONG_SIDE_10S')

    def test_small_lead_and_88c_no_longer_sell(self):
        r, d = call('2061.40', 0, tk='D', implied='0.86')          # tiny lead, 86c
        self.assertIsNone(r); self.assertIsNotNone(d['old_reason'])

    def test_emergency_50c_only_when_blind(self):
        self.assertIsNone(call('2050.00', 0, tk='E', implied='0.22')[0])            # fresh ETH on our side: hold
        self.assertEqual(call('2050.00', 0, tk='E2', implied='0.45', fresh=False)[0], 'PRICE_EMERGENCY_50C')

    def test_stale_price_decides_nothing(self):
        self.assertIsNone(call('2070.00', 0, tk='F', fresh=False)[0])
        self.assertIsNone(call('2070.00', 30, tk='F', fresh=False)[0])

    def test_markets_and_sides_have_separate_timers(self):
        call('2062.87', 0, tk='G1')
        self.assertIsNone(call('2062.87', 10, tk='G2')[0])
        self.assertEqual(call('2062.87', 10, tk='G1')[0], 'ETH_WRONG_SIDE_10S')
        call('2060.00', 0, tk='G3', side='yes')                   # YES: wrong side = below the target
        self.assertEqual(call('2060.00', 10, tk='G3', side='yes')[0], 'ETH_WRONG_SIDE_10S')

    def test_final_minute_uses_settlement_average(self):
        # 30 s left; the first 30 s of the final minute averaged well BELOW the target (good for NO).
        close = T0 + 30
        base = [(close - 60 + i, 2058.0) for i in range(0, 30)]
        for t in (0, 10, 25):
            at(t)
            hist = {'close': close, 'partial': base + [(close - 30 + j, 2062.5) for j in range(0, t + 1)]}
            r, d = m.v7c_stop_reason('no', K, D('2062.5'), T0 + t, 30 - t, F, hist, D('0.90'), ticker='H', now=T0 + t)
            self.assertIsNone(r, (t, d['settlement']))
            self.assertEqual(d['settlement']['quality'], 'SAMPLED_PROXY')


if __name__ == '__main__':
    unittest.main(verbosity=1)
