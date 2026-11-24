"""ETH V1.9.3 tests: market mood light (display only). python TEST_ETH_V1_9_3.py"""
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from RE5 import mood, ops  # noqa: E402
T0 = 1_794_700_800          # a :00 boundary (divisible by 900)


def path(start, minutes, step_pct, base=2000.0, every=10):
    """Samples every `every` s; price alternates +/- step_pct each minute (so 1-min moves ~ step_pct)."""
    out = []
    for m in range(minutes):
        px = base * (1 + (step_pct / 100) * (1 if m % 2 else -1) / 2)
        for s in range(0, 60, every):
            out.append((start + 60 * m + s, px))
    return out


class Levels(unittest.TestCase):
    def test_version_and_thresholds(self):
        self.assertEqual(ops.VERSION, 'ETH V1.9.4 / RE5.5')
        self.assertEqual((mood.LOW_BELOW, mood.HIGH_FROM), (0.0454, 0.0720))
        self.assertEqual([mood.level(v) for v in (0.01, 0.0454, 0.06, 0.0720, 0.2, None)],
                         ['LOW', 'MEDIUM', 'MEDIUM', 'HIGH', 'HIGH', None])
        self.assertEqual(T0 % 900, 0); self.assertEqual(mood.slot_start(T0 + 899), T0)

    def test_window_volatility(self):
        v, n = mood.window_vol(path(T0, 15, 0.10), T0)
        self.assertEqual(n, 15); self.assertAlmostEqual(v, 0.10, delta=0.005)          # 1-min moves of 0.10%
        self.assertEqual(mood.window_vol(path(T0, 6, 0.10), T0)[0], None)                     # < 10 minutes: no verdict


class Light(unittest.TestCase):
    def test_high_previous_market_is_red(self):
        s = mood.summary(path(T0, 15, 0.30) + path(T0 + 900, 3, 0.01), now=T0 + 900 + 180)
        self.assertEqual(s['level'], 'HIGH'); self.assertEqual(s['color'], mood.RED)
        self.assertIn('stay away', s['head'])

    def test_calm_previous_market_is_green(self):
        for pct, lv in ((0.02, 'LOW'), (0.06, 'MEDIUM')):
            s = mood.summary(path(T0, 15, pct), now=T0 + 900 + 60)
            self.assertEqual(s['level'], lv, (pct, s['prev'])); self.assertEqual(s['color'], mood.GREEN)
            self.assertIn('OK to trade', s['head'])

    def test_not_enough_data_is_grey(self):
        s = mood.summary(path(T0, 5, 0.3), now=T0 + 900 + 30)
        self.assertIsNone(s['level']); self.assertEqual(s['color'], mood.GREY)

    def test_current_market_turning_high_warns(self):
        s = mood.summary(path(T0, 15, 0.02) + path(T0 + 900, 11, 0.4), now=T0 + 900 + 660)
        self.assertEqual((s['level'], s['current_level'], s['color']), ('LOW', 'HIGH', mood.YELLOW))

    def test_falls_back_to_current_market_when_previous_missing(self):
        # Nov 14 7:09 PM: bot was off 6:47-6:57 so the previous market had no data, but this market was HIGH
        s = mood.summary(path(T0 + 900, 11, 0.09), now=T0 + 900 + 660)
        self.assertEqual((s['level'], s['color']), ('HIGH', mood.RED)); self.assertIn('no data', s['prev'])

    def test_last_hour_strip(self):
        s = mood.summary(path(T0 - 2700, 60, 0.02), now=T0 + 900 + 5)
        self.assertTrue(s['last_hour'].startswith('Last hour: '))
        self.assertEqual(s['last_hour'].count('->'), 3)

    def test_save_load_and_trim(self):
        p = Path(tempfile.mkdtemp()) / 'mood_samples.json'
        now = T0 + 4 * 3600
        mood.save(p, [(T0, 2000.0), (now - 60, 2001.0)])
        self.assertEqual(mood.load(p, now=now), [(now - 60, 2001.0)])               # older than 3 h dropped
        self.assertEqual(mood.load(Path(tempfile.mkdtemp()) / 'missing.json'), [])  # missing file is fine


class DisplayOnly(unittest.TestCase):
    def test_engine_never_uses_the_light(self):
        # V1.9.4: the engine has its OWN regime file (ETH_V194_Regime.py) but never imports the display module
        for f in (ROOT / 'RE5' / 'APP').glob('*.py'):
            src = f.read_text(encoding='utf-8')
            self.assertNotIn('import mood', src, f.name); self.assertNotIn('RE5.mood', src, f.name)
        for f in ('controller.py', 'exchange.py', 'accounting.py'):
            self.assertNotIn('import mood', (ROOT / 'RE5' / f).read_text(encoding='utf-8'))

    def test_app_wiring(self):
        src = (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8')
        for needle in ('from RE5 import ops, scaling, asset, mood', 'def _mood_loop', 'def refresh_mood', 'client.ethrti()',
                       "mood_samples.json", 'self.after(5000, self.refresh_mood)', "'TEST_ETH_V1_9_3.py'"):
            self.assertIn(needle, src)


if __name__ == '__main__':
    unittest.main(verbosity=1)
