"""ETH V1.9.2 tests: Early-Hold needs lead >= 2.0x (option B). python TEST_ETH_V1_9_2.py"""
import importlib.util, sys, unittest
from decimal import Decimal as D
from pathlib import Path
ROOT = Path(__file__).resolve().parent
APP = ROOT / 'RE5' / 'APP'
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(APP))
spec = importlib.util.spec_from_file_location('eth_collector_v192', APP / 'ETH_V42_Collector.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class OptionB(unittest.TestCase):
    def test_version_and_setting(self):
        import RE5.ops as ops
        self.assertEqual(ops.VERSION, 'ETH V1.9.4 / RE5.5')
        self.assertEqual(m.EARLY_HOLD_MIN_SCALED_GAP, D('2.0'))
        self.assertEqual(m.scaled_gate_for_checkpoint(m.EARLY_HOLD_CP), D('2.0'))
        self.assertIn("'TEST_ETH_V1_9_2.py'", (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8'))

    def test_everything_else_unchanged(self):
        self.assertEqual(m.EARLY_MIN_SCALED_GAP, D('2.0'))           # 180/150 s
        self.assertEqual(m.scaled_gate_for_checkpoint(120), m.MIN_SCALED_GAP)
        self.assertEqual((m.EARLY_HOLD_FROM, m.EARLY_HOLD_TO, m.EARLY_HOLD_BID, m.EARLY_HOLD_SECONDS), (420, 240, D('0.90'), 120))
        self.assertEqual((m.ETH_CROSS_HOLD_S, m.STEADY_ASK, m.STEADY_SECONDS), (10, D('0.90'), 60))

    def test_log_line_shows_real_threshold(self):
        src = (APP / 'ETH_V42_Collector.py').read_text(encoding='utf-8')
        self.assertIn('>= {reg_eh_gap}x', src)                         # V1.9.4: the threshold actually used (2.0 or calm 1.5)
        self.assertNotIn("x >= 1.5x'", src)


if __name__ == '__main__':
    unittest.main(verbosity=1)
