"""End-to-end: the real RE5 collector loop + RE5 controller against simulated Kalshi data.

Scenario A (stop): ETH sits well above the strike, the bot enters YES at a checkpoint, ETH then
falls through the strike, the shared V7-C stop fires and the position is sold with IOC orders
(first attempt partially filled), session ends flat.
Scenario B (settle): same entry, ETH stays up, contracts settle YES from the settlement record.

python TEST_RE5_LOOP.py
"""
import importlib.util, math, os, shutil, sys, tempfile, threading, unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from RE5.asset import SERIES as SER  # noqa: E402  (KXETH15M)
from TEST_RE5 import FakeKalshi  # noqa: E402

START = 1_800_000_000.0
STRIKE = Decimal('80000')
# ETH V1: TEST_ETH sets SCALE = 2500/80000 to replay the same scenarios at Ether prices.
SCALE = Decimal('1')


def px(v):
    v = Decimal(str(v))
    return v if SCALE == 1 else (v * SCALE).quantize(Decimal('0.01'))   # SCALE 1 = original ETH values exactly
TICKER = SER + '-26NOV011930-30'


class FakeTime:
    def __init__(self): self.t = START; self.lock = threading.Lock()
    def time(self): return self.t
    def __call__(self): return self.t
    def sleep(self, s):
        if threading.current_thread() is threading.main_thread():
            self.t += s
            if self.t > START + 3000:
                raise KeyboardInterrupt('test loop exceeded simulated time')
        else:
            import time as _t; _t.sleep(0.001)
    def monotonic(self): return self.t
    def __getattr__(self, name):
        import time as _t; return getattr(_t, name)


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Scenario:
    def __init__(self, clock, crash):
        self.clock = clock; self.crash = crash
        self.close = START + 600            # market open 300s ago when loop starts; 600s left
    def eth(self):
        left = self.close - self.clock.t
        base = px(Decimal('80300') + Decimal(str(round(25 * math.sin(self.clock.t / 37.0), 2))))
        if self.crash and left < 140:
            base = px('79900')
        return base
    def market(self):
        left = self.close - self.clock.t
        status = 'active' if left > 0 else 'closed'
        if self.crash and left < 140:
            yes_bid, yes_ask = Decimal('0.30'), Decimal('0.32')
        else:
            yes_bid, yes_ask = Decimal('0.965'), Decimal('0.97')
        return {'ticker': TICKER, 'status': status, 'close_time': iso(self.close), 'floor_strike': str(px(STRIKE)),
                'yes_ask_dollars': str(yes_ask), 'yes_ask_size_fp': '1000.00',
                'no_ask_dollars': str(1 - yes_bid), 'no_ask_size_fp': '1000.00',
                'yes_bid_dollars': str(yes_bid), 'yes_bid_size_fp': '1000.00',
                'no_bid_dollars': str(1 - yes_ask), 'no_bid_size_fp': '1000.00', 'result': ''}


def load_collector(app_dir, fake_time, scenario):
    sys.path.insert(0, str(app_dir))
    spec = importlib.util.spec_from_file_location('re5_loop_collector', app_dir / 'ETH_V42_Collector.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    m.time = fake_time
    m.base.time = fake_time
    class FakeClient:
        def __init__(self, *a, **k): pass
        def request(self, method, path, params=None, payload=None, auth=False):
            if path == '/markets':
                return {'markets': [scenario.market()], 'cursor': ''}
            if path.endswith('/orderbook'):
                return {'orderbook_fp': {'yes_dollars': [['0.965', '500']], 'no_dollars': [['0.03', '500']]}}
            if path.startswith('/markets/'):
                return {'market': scenario.market()}
            return {}
        def ethrti(self):
            return scenario.eth(), fake_time.t - 0.5
    m.base.Client = FakeClient
    m.load_verified_history = lambda now=None: [(fake_time.t - 900 + i, px(Decimal('80300') + Decimal(str(round(30 * math.sin(i / 23.0), 2)))), 'test')
                                               for i in range(0, 900, 2)]
    return m


class Loop(unittest.TestCase):
    def run_loop(self, crash, fill_script):
        tmp = Path(tempfile.mkdtemp(dir=os.environ.get('RE5_TEST_TMP')))
        app = tmp / 'APP'
        shutil.copytree(ROOT / 'RE5' / 'APP', app, ignore=shutil.ignore_patterns('*.csv', '*.sqlite3*', '__pycache__'))
        ft = FakeTime()
        scen = Scenario(ft, crash)
        fake = FakeKalshi(ft)
        fake.fill_script = list(fill_script)
        fake.set_book(yes_ask='0.97', no_ask='0.04', depth=1000)
        _orig_settlements = fake.settlements_since
        def _auto_settle(min_ts, _f=fake, _s=scen, _ft=ft, _crash=crash):
            # Kalshi publishes the settlement record shortly after close.
            if _ft.t > _s.close + 30 and not _f.settlements:
                _f.settle(TICKER, 'no' if _crash else 'yes')
            return _orig_settlements(min_ts)
        fake.settlements_since = _auto_settle
        orig_pos = fake.positions
        import RE5.exchange as ex
        import RE5.controller as ctl
        orig_client, orig_time = ex.ExchangeClient, ctl.time
        ex.ExchangeClient = lambda **kw: fake
        ctl.time = ft
        os.environ.update(BIGBET_RE5_LIVE='demo', KALSHI_ENV='demo', BIGBET_RE5_STATE_FILE=str(tmp / 'rt' / 'j.json'),
                          BIGBET_RE5_END_TIME=str(START + 560))
        (tmp / 'rt').mkdir()
        m = load_collector(app, ft, scen)
        m.DAILY_DB = tmp / 'daily.sqlite3'
        import ETH_V42_Risk
        ETH_V42_Risk.DAILY_DB = tmp / 'daily.sqlite3'
        ETH_V42_Risk.time = ft
        out = []
        import builtins
        real_print = builtins.print
        builtins.print = lambda *a, **k: out.append(' '.join(str(x) for x in a))
        for path, fields in ((m.RAW, m.RAW_FIELDS), (m.CAND, m.CAND_FIELDS), (m.SUMMARY, m.OUT_FIELDS), (m.ERRORS, m.ERROR_FIELDS),
                             (m.SETTLE_ALL, m.SETTLE_FIELDS), (m.EARLY, m.EARLY_FIELDS), (m.DISAPPEAR, m.DISAPPEAR_FIELDS),
                             (m.HISTORY_AUDIT, m.HISTORY_FIELDS)):
            m.init_csv(path, fields)
        try:
            m.run()
        finally:
            builtins.print = real_print
            ex.ExchangeClient = orig_client; ctl.time = orig_time
        # after the loop: settle (scenario B) by running settlement polling directly
        from RE5.controller import Controller
        c = Controller(fake, tmp / 'rt' / 'j.json', clock=ft.time, sleep=ft.sleep, log=out.append)
        ft.t = max(ft.t, scen.close + 60)
        c.poll_settlement(force=True)
        self.app = app
        return c.j.read(), fake, out, tmp

    def test_stop_path_partial_then_flat(self):
        state, fake, out, tmp = self.run_loop(crash=True, fill_script=[10, 4, 6])
        entries = [p for p in fake.posts if not p['reduce_only']]
        exits = [p for p in fake.posts if p['reduce_only']]
        self.assertEqual(len(entries), 1, '\n'.join(out[-40:]))
        self.assertEqual(entries[0]['book_side'], 'bid')
        self.assertGreaterEqual(len(exits), 2)
        self.assertIsNone(state['position'])
        h = state['history'][-1]
        self.assertEqual(h['result'], 'FULLY_EXITED'); self.assertLess(Decimal(h['pnl']), 0)
        events = (tmp / 'rt' / 're5_live_events.jsonl').read_text()
        self.assertIn('STOP_TRIGGERED', events); self.assertIn('EXIT_FILL', events)
        self.assertTrue(state['halted'])                       # one loss halts the day

    def test_settlement_path(self):
        state, fake, out, tmp = self.run_loop(crash=False, fill_script=[10])
        self.assertEqual(len([p for p in fake.posts if not p['reduce_only']]), 1, '\n'.join(out[-40:]))
        self.assertEqual([p for p in fake.posts if p['reduce_only']], [])
        self.assertIsNone(state['position'], '\n'.join(out[-30:]))
        h = state['history'][-1]
        self.assertEqual(h['result'], 'yes'); self.assertGreater(Decimal(h['pnl']), 0)
        self.assertFalse(state['halted'] and state['reason'].startswith('DAILY_LOSS'))

    def test_zero_fill_then_retry_and_paper_mirrors_live(self):
        state, fake, out, tmp = self.run_loop(crash=False, fill_script=[0, 10])
        entries = [p for p in fake.posts if not p['reduce_only']]
        self.assertEqual(len(entries), 2, '\n'.join(out[-40:]))
        self.assertTrue(any('LIVE_NO_FILL' in line for line in out))
        self.assertTrue(any('LIVE_FILLED' in line for line in out))
        import csv
        rows = list(csv.DictReader(open(self.app / 'eth_v42_shadow_candidates.csv', encoding='utf-8')))
        statuses = [r['status'] for r in rows if r['ticker'] == TICKER]
        self.assertIn('LIVE_NO_FILL', statuses)
        approved = [r for r in rows if r['ticker'] == TICKER and r['status'] not in ('REJECT', 'LIVE_NO_FILL', 'NOT_APPROVED')]
        self.assertEqual(len(approved), 1)
        self.assertEqual(Decimal(approved[0]['total_cost']), Decimal(state['history'][-1]['cost']))   # paper == real cost
        self.assertEqual(state['history'][-1]['result'], 'yes')



if __name__ == '__main__':
    unittest.main(verbosity=2)
