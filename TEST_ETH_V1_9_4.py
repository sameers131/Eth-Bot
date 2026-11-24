"""ETH V1.9.4 tests: mood regime in the trading engine (option B). python TEST_ETH_V1_9_4.py

CALM previous market  -> Early-Hold lead 1.5x, steady filter 85c
HIGH previous market  -> no new entries
UNKNOWN               -> V1.9.2 rules (2.0x, 90c)
"""
import importlib.util, json, math, os, random, sys, tempfile, unittest
from decimal import Decimal as D
from pathlib import Path
ROOT = Path(__file__).resolve().parent
APP = ROOT / 'RE5' / 'APP'
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(APP))
spec = importlib.util.spec_from_file_location('eth_collector_v194', APP / 'ETH_V42_Collector.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import ETH_V194_Regime as R  # noqa: E402
from RE5 import mood  # noqa: E402
T0 = 1_800_000_000.0          # multiple of 900 -> a market window boundary


def calm_prices(start, end, step=10):
    return [(t, 2000 + 0.3 * math.sin(t / 50)) for t in range(int(start), int(end), step)]


def jumpy_prices(start, end, step=10):
    return [(t, 2000 + (3 if (t // 60) % 2 else -3)) for t in range(int(start), int(end), step)]   # ~0.3% per minute


class Settings(unittest.TestCase):
    def test_version(self):
        import RE5.ops as ops
        self.assertEqual(ops.VERSION, 'ETH V1.9.4 / RE5.5')
        app = (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8')
        self.assertIn('V1.9.4 / V7-D Rule C', app); self.assertIn("'TEST_ETH_V1_9_4.py'", app)

    def test_rules_per_regime(self):
        self.assertEqual(m.regime_rules('CALM'), (True, D('1.5'), D('0.85')))
        self.assertEqual(m.regime_rules('HIGH')[0], False)
        self.assertEqual(m.regime_rules('UNKNOWN'), (True, D('2.0'), D('0.90')))     # = V1.9.2
        self.assertEqual(m.regime_rules('anything else'), (True, D('2.0'), D('0.90')))

    def test_everything_else_unchanged(self):
        self.assertEqual((m.EARLY_HOLD_MIN_SCALED_GAP, m.STEADY_ASK, m.STEADY_SECONDS), (D('2.0'), D('0.90'), 60))
        self.assertEqual((m.EARLY_MIN_SCALED_GAP, m.MIN_SCALED_GAP, m.P_MIN, m.P_MAX), (D('2.0'), D('1.6'), D('0.90'), D('0.99')))
        self.assertEqual((m.EARLY_HOLD_FROM, m.EARLY_HOLD_TO, m.EARLY_HOLD_BID, m.EARLY_HOLD_SECONDS), (420, 240, D('0.90'), 120))
        self.assertEqual(m.ETH_CROSS_HOLD_S, 10)
        self.assertEqual(m.MIN_CUSHION_PCT, D('0.03'))
        self.assertEqual(R.HIGH_FROM, mood.HIGH_FROM)                                   # same line as the light

    def test_calm_lead_only_changes_early_hold(self):
        for cp in m.CHECKPOINTS:
            self.assertEqual(m.scaled_gate_for_checkpoint(cp, D('1.5')), m.scaled_gate_for_checkpoint(cp))
        self.assertEqual(m.scaled_gate_for_checkpoint(m.EARLY_HOLD_CP, D('1.5')), D('1.5'))
        self.assertEqual(m.scaled_gate_for_checkpoint(m.EARLY_HOLD_CP), D('2.0'))


def raw(scaled, side='yes'):
    return dict(ticker='KXETH15M-X', time_utc='t', seconds_left='300', side=side, eth='3005', strike='3000',
                gap_abs='5', gap_signed='5', sigma_min='1', scaled_gap=str(scaled), minute_pairs='10',
                ask='0.95', quoted_depth='100')


class EntryGates(unittest.TestCase):
    def test_early_hold_lead_17x(self):
        self.assertEqual(m.checkpoint_candidate(raw('1.7'), m.EARLY_HOLD_CP)['status'], 'REJECT')                  # strict
        self.assertEqual(m.checkpoint_candidate(raw('1.7'), m.EARLY_HOLD_CP, D('1.5'))['status'], 'SHADOW_CANDIDATE')  # calm
        self.assertEqual(m.checkpoint_candidate(raw('1.4'), m.EARLY_HOLD_CP, D('1.5'))['status'], 'REJECT')       # below calm

    def test_normal_checkpoint_not_loosened(self):
        self.assertEqual(m.checkpoint_candidate(raw('1.55'), 120, D('1.5'))['status'], 'REJECT')                  # still 1.6
        self.assertEqual(m.checkpoint_candidate(raw('1.9'), 150, D('1.5'))['status'], 'REJECT')                   # still 2.0

    def test_steady_85c(self):
        h = [(T0 + t, D('0.87')) for t in range(0, 60, 3)]
        self.assertFalse(m.steady_ok(h, T0 + 60))                    # strict 90c
        self.assertTrue(m.steady_ok(h, T0 + 60, D('0.85')))         # calm 85c
        h.append((T0 + 59, D('0.84')))
        self.assertFalse(m.steady_ok(h, T0 + 60, D('0.85')))
        self.assertFalse(m.steady_ok([(T0 + 30, None)], T0 + 60, D('0.85')))   # missing quote still blocks
        self.assertFalse(m.steady_ok([], T0 + 60, D('0.85')))


class RegimeCalc(unittest.TestCase):
    close = T0 + 1800                       # market T0+900 .. T0+1800; previous market T0 .. T0+900

    def test_calm_high_unknown(self):
        r = R.Regime(); [r.add(t, p) for t, p in calm_prices(T0, T0 + 900)]
        self.assertEqual(r.for_market(self.close, now=self.close - 500)[0], 'CALM')
        r = R.Regime(); [r.add(t, p) for t, p in jumpy_prices(T0, T0 + 900)]
        lab, vol = r.for_market(self.close, now=self.close - 500)
        self.assertEqual(lab, 'HIGH'); self.assertGreater(vol, 0.072)
        r = R.Regime(); [r.add(t, p) for t, p in calm_prices(T0 + 400, T0 + 900)]   # only ~8 minutes
        self.assertEqual(r.for_market(self.close, now=self.close - 500)[0], 'UNKNOWN')
        self.assertEqual(R.Regime().for_market(self.close)[0], 'UNKNOWN')

    def test_only_previous_market_counts(self):
        r = R.Regime()
        [r.add(t, p) for t, p in jumpy_prices(T0 - 900, T0)]        # two markets back: jumpy
        [r.add(t, p) for t, p in calm_prices(T0, T0 + 900)]          # previous: calm
        [r.add(t, p) for t, p in jumpy_prices(T0 + 900, T0 + 1300)]  # this market so far: jumpy
        self.assertEqual(r.for_market(self.close, now=T0 + 1300)[0], 'CALM')

    def test_threshold_edges(self):
        self.assertEqual(R.label(0.0719), 'CALM'); self.assertEqual(R.label(0.0720), 'HIGH'); self.assertEqual(R.label(None), 'UNKNOWN')

    def test_same_number_as_the_light(self):
        rnd = random.Random(7)
        for _ in range(30):
            px, s = 2000.0, []
            amp = rnd.choice([0.2, 1, 2, 4])
            for t in range(int(T0), int(T0 + 900), 7):
                px += rnd.gauss(0, amp); s.append((t, px))
            lv, _ = mood.window_vol(s, T0)
            self.assertAlmostEqual(R.window_vol(s, T0, T0 + 900), lv, places=12)
            self.assertEqual(R.label(lv) == 'HIGH', mood.level(lv) == 'HIGH')

    def test_reads_app_mood_file_and_retries_unknown(self):
        f = Path(tempfile.mkdtemp()) / 'mood_samples.json'
        r = R.Regime(f)
        self.assertEqual(r.for_market(self.close, now=T0 + 1000)[0], 'UNKNOWN')       # no file yet
        f.write_text(json.dumps({'samples': jumpy_prices(T0, T0 + 900)}), encoding='utf-8')
        self.assertEqual(r.for_market(self.close, now=T0 + 1010)[0], 'UNKNOWN')       # cached for 30 s
        self.assertEqual(r.for_market(self.close, now=T0 + 1031)[0], 'HIGH')          # re-checked
        f.write_text('garbage', encoding='utf-8')
        self.assertEqual(r.for_market(self.close, now=T0 + 1200)[0], 'HIGH')          # decided markets stay decided
        self.assertEqual(R.Regime(f).for_market(self.close)[0], 'UNKNOWN')            # bad file never crashes

    def test_memory_trim(self):
        r = R.Regime()
        for i in range(50000):
            r.add(T0 + i * 0.5, 2000)
        self.assertLessEqual(len(r.samples), R.MAX_SAMPLES + 1)
        self.assertGreaterEqual(r.samples[0][0], T0 + 49999 * 0.5 - R.KEEP_S - R.MAX_SAMPLES * 0.5)
        r.add(None, 'x')                                                               # junk ignored


class Wiring(unittest.TestCase):
    src = (APP / 'ETH_V42_Collector.py').read_text(encoding='utf-8')

    def test_high_blocks_before_any_order(self):
        s = self.src
        self.assertLess(s.index("AFTER_HIGH_MOOD"), s.index("receipt=risk_approve("))
        self.assertLess(s.index("AFTER_HIGH_MOOD"), s.index("filled=live.entry(c)"))
        self.assertIn("regime.add(ts,price)", s)
        self.assertIn("_steady=steady_ok(_ah,now,reg_steady)", s)
        self.assertIn("checkpoint_candidate(raw,EARLY_HOLD_CP,reg_eh_gap)", s)

    def test_ops_passes_mood_file(self):
        ops = (ROOT / 'RE5' / 'ops.py').read_text(encoding='utf-8')
        self.assertIn("BIGBET_MOOD_SAMPLES=str(data_dir(s) / 'mood_samples.json')", ops)

    def test_light_text_matches_bot(self):
        now = T0 + 1000
        hi = mood.summary(jumpy_prices(T0, T0 + 900), now=now)
        self.assertEqual(hi['level'], 'HIGH'); self.assertIn('NO new entries', hi['advice'])
        lo = mood.summary(calm_prices(T0, T0 + 900), now=now)
        self.assertIn('calm rules', lo['advice'])


# ---------------------------------------------------------------- end to end: the REAL engine loop
import TEST_RE5_LOOP as L  # noqa: E402


def make_history(kind):
    """Preload for the loop test: previous market [START-900, START-300) per `kind`, current market as the original test."""
    def orig(t):
        i = t - (L.START - 900)
        return L.px(D('80300') + D(str(round(30 * math.sin(i / 23.0), 2))))
    def h(ft):
        out = []
        for t in range(int(ft.t - 900), int(ft.t), 2):
            if t < L.START - 300:
                if kind == 'unknown':
                    continue
                if kind == 'high':
                    p = L.px(D('80300') + (D('150') if (t // 60) % 2 else D('-150')))
                else:
                    p = L.px(D('80300') + D(str(round(3 * math.sin(t / 40.0), 2))))
            else:
                p = orig(t)
            out.append((float(t), p, 'test'))
        return out
    return h


class EndToEnd(unittest.TestCase):
    def run_kind(self, kind):
        orig = L.load_collector
        def patched(app, ft, scen):
            mod = orig(app, ft, scen)
            mod.load_verified_history = lambda now=None: make_history(kind)(ft)
            return mod
        L.load_collector = patched
        os.environ.pop('BIGBET_MOOD_SAMPLES', None)
        try:
            return L.Loop.run_loop(self, crash=False, fill_script=[10])
        finally:
            L.load_collector = orig

    def test_calm_trades(self):
        state, fake, out, tmp = self.run_kind('calm')
        self.assertTrue(any('MOOD_REGIME' in x and '-> CALM' in x for x in out), '\n'.join(out[-30:]))
        self.assertEqual(len([p for p in fake.posts if not p['reduce_only']]), 1)
        self.assertEqual(state['history'][-1]['result'], 'yes')

    def test_high_does_not_trade(self):
        state, fake, out, tmp = self.run_kind('high')
        self.assertTrue(any('MOOD_REGIME' in x and '-> HIGH' in x for x in out), '\n'.join(out[-30:]))
        self.assertTrue(any('SKIP_AFTER_HIGH_MOOD' in x for x in out))
        self.assertEqual(fake.posts, [])                                          # no order of any kind
        self.assertFalse(state.get('history'))
        import csv
        rows = list(csv.DictReader(open(self.app / 'eth_v42_shadow_candidates.csv', encoding='utf-8')))
        self.assertTrue(rows and all('AFTER_HIGH_MOOD' in r['reason'] for r in rows if r['status'] == 'NOT_APPROVED'))
        self.assertFalse([r for r in rows if r['status'] not in ('REJECT', 'NOT_APPROVED')])

    def test_unknown_uses_v192_rules_and_trades(self):
        state, fake, out, tmp = self.run_kind('unknown')
        self.assertTrue(any('MOOD_REGIME' in x and '-> UNKNOWN' in x and '2.0x' in x and '90c' in x for x in out), '\n'.join(out[-30:]))
        self.assertEqual(len([p for p in fake.posts if not p['reduce_only']]), 1)


if __name__ == '__main__':
    unittest.main(verbosity=1)
