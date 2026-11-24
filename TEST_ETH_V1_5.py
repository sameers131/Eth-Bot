"""ETH V1.5 tests: run length up to 10 hours; nothing else changed.  Run:  python TEST_ETH_V1_5.py"""
import inspect, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
from RE5 import ops  # noqa: E402


class Durations(unittest.TestCase):
    def test_up_to_10_hours(self):
        lines = (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8').splitlines(True)
        i = next(k for k, l in enumerate(lines) if l.startswith('DURATIONS'))
        ns = {}; exec(lines[i] + lines[i + 1], ns); d = ns['DURATIONS']
        self.assertEqual((d['10 hours'], d['8 hours'], d['15 min'], max(d.values())), (600, 480, 15, 600))
    def test_engine_accepts_600(self):
        self.assertIn('1 <= int(minutes) <= 600', inspect.getsource(ops.start_session))
    def test_version_and_eth_only(self):
        from RE5 import asset
        self.assertEqual(ops.VERSION, 'ETH V1.9.4 / RE5.5'); self.assertEqual(asset.P['series'], 'KXETH15M')


if __name__ == '__main__':
    unittest.main(verbosity=1)
