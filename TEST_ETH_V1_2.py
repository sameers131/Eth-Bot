"""ETH V1.2 tests (ported from ETH V12.7): Kalshi connection glitches no longer stop the bot, and the app restarts a LIVE
session by itself only when it is safe. Offline; no network, no real keys.
Run from this folder:
python TEST_ETH_V1_2.py
"""
import importlib.util, json, os, sys, tempfile, unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
import requests  # noqa: E402

import TEST_RE5_LOOP as LOOP  # noqa: E402
from TEST_RE5 import Clock  # noqa: E402
from TEST_V12 import OpsBase, OpsKalshi  # noqa: E402
from RE5 import ops  # noqa: E402
from RE5.exchange import ApiFailure, OrderRejected, UnknownOrderState  # noqa: E402


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'RE5' / 'APP' / file)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


class Resp:
    def __init__(self, code, body=None):
        self.status_code = code; self._b = body or {'ok': True}
    def json(self): return self._b
    def raise_for_status(self):
        if self.status_code >= 400:
            e = requests.exceptions.HTTPError(f'{self.status_code} Error'); e.response = self; raise e


class ReadRetries(unittest.TestCase):
    def setUp(self):
        self.v37 = load('v37_v127', 'kalshi_eth_bot_v37.py')
        self.c = self.v37.Client.__new__(self.v37.Client)
        self.c.host = 'https://example.invalid'; self.c.private = None; self.c.keyid = ''
        self.v37.time.sleep = lambda s: None
    def script(self, *steps):
        calls = []
        def req(method, url, **k):
            calls.append(method); step = steps[len(calls) - 1]
            if isinstance(step, Exception): raise step
            return step
        self.c.http = mock.Mock(request=req)
        return calls
    def test_get_retries_after_timeout(self):
        calls = self.script(requests.exceptions.ReadTimeout('slow'), Resp(200, {'x': 1}))
        self.assertEqual(self.c.request('GET', '/markets'), {'x': 1}); self.assertEqual(len(calls), 2)
    def test_get_retries_on_503_and_429(self):
        calls = self.script(Resp(503), Resp(429), Resp(200, {'x': 2}))
        self.assertEqual(self.c.request('GET', '/markets'), {'x': 2}); self.assertEqual(len(calls), 3)
    def test_get_gives_up_after_4_tries(self):
        calls = self.script(*[requests.exceptions.ConnectionError('down')] * 4)
        with self.assertRaises(requests.exceptions.ConnectionError): self.c.request('GET', '/markets')
        self.assertEqual(len(calls), 4)
    def test_post_is_never_retried(self):
        calls = self.script(requests.exceptions.ReadTimeout('slow'), Resp(200))
        with self.assertRaises(requests.exceptions.ReadTimeout): self.c.request('POST', '/portfolio/orders', payload={})
        self.assertEqual(calls, ['POST'])
    def test_client_errors_are_not_retried(self):
        calls = self.script(Resp(400), Resp(200))
        with self.assertRaises(requests.exceptions.HTTPError): self.c.request('GET', '/markets')
        self.assertEqual(len(calls), 1)


class GlitchClassification(unittest.TestCase):
    def setUp(self):
        self.m = load('coll_v127', 'ETH_V42_Collector.py')
    def test_glitches(self):
        g = self.m.is_connection_glitch
        self.assertTrue(g(requests.exceptions.ReadTimeout('x'))); self.assertTrue(g(requests.exceptions.ConnectionError('x')))
        e = requests.exceptions.HTTPError('503'); e.response = Resp(503); self.assertTrue(g(e))
        self.assertTrue(g(ApiFailure('read transport failure'))); self.assertTrue(g(ApiFailure('Kalshi HTTP 502', 502, None)))
    def test_not_glitches(self):
        g = self.m.is_connection_glitch
        e = requests.exceptions.HTTPError('400'); e.response = Resp(400); self.assertFalse(g(e))
        self.assertFalse(g(OrderRejected('order rejected HTTP 400', 400, None)))
        self.assertFalse(g(UnknownOrderState('write transport failure; outcome unknown')))
        self.assertFalse(g(ValueError('some other bad value'))); self.assertFalse(g(ApiFailure('Kalshi HTTP 401', 401, None)))
        self.assertTrue(g(ValueError('Stale or implausible ETHUSD_RTI value; aborting signal')))   # ETH V1.9.1: feed blip gets the grace
    def test_connection_problem_text(self):
        self.assertTrue(ops.is_connection_problem("OPERATIONAL_ERROR ReadTimeout: HTTPSConnectionPool(host='external-api.kalshi.com', port=443): Read timed out."))
        self.assertTrue(ops.is_connection_problem('Account does not match the journal: ApiFailure: read transport failure'))
        self.assertFalse(ops.is_connection_problem('DAILY_LOSS_LIMIT'))
        self.assertFalse(ops.is_connection_problem('OPERATIONAL_ERROR SafetyError: exchange holds KXETH15M exposure but journal is flat'))


class EngineRidesOutGlitches(LOOP.Loop):
    """Full engine loop with Kalshi market data failing for a while."""
    def _with_outage(self, start, end):
        orig = LOOP.load_collector
        def patched(app_dir, ft, scen):
            m = orig(app_dir, ft, scen)
            inner = m.base.Client
            class Flaky(inner):
                def request(self, method, path, params=None, payload=None, auth=False):
                    if LOOP.START + start <= ft.t < LOOP.START + end:
                        raise requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='external-api.kalshi.com', port=443): Read timed out.")
                    return super().request(method, path, params, payload, auth)
            m.base.Client = Flaky
            return m
        LOOP.load_collector = patched
        self.addCleanup(setattr, LOOP, 'load_collector', orig)

    def test_short_outage_no_pause_but_that_market_is_skipped(self):
        self._with_outage(100, 160)                         # 60 s of timeouts inside this 15-minute market
        state, fake, out, tmp = self.run_loop(crash=False, fill_script=[10])
        self.assertTrue(any('CONNECTION_GLITCH' in l for l in out), '\n'.join(out[-30:]))
        self.assertFalse(any('FAIL CLOSED' in l for l in out))                  # entries were never paused
        self.assertFalse(state['halted'] and state['reason'].startswith('OPERATIONAL_ERROR'))
        self.assertEqual([p for p in fake.posts if not p['reduce_only']], [])   # but NO entry in the glitch market
        self.assertTrue(any('SKIP_MARKET_AFTER_GLITCH' in l for l in out))
        import csv
        rows = list(csv.DictReader(open(self.app / 'eth_v42_shadow_candidates.csv', encoding='utf-8')))
        self.assertTrue(rows and all('CONNECTION_GLITCH_THIS_MARKET' in r['reason'] for r in rows
                                     if r['status'] == 'NOT_APPROVED'))
        self.assertFalse(any(r['status'] in ('PAPER_APPROVED', 'SETTLED_WIN') for r in rows))   # paper skips it too

    def test_restart_glitch_in_this_market_skips_it(self):
        with mock.patch.dict(os.environ, {'BIGBET_GLITCH_AT': str(LOOP.START - 5)}):     # outage just before restart
            state, fake, out, tmp = self.run_loop(crash=False, fill_script=[10])
        self.assertEqual([p for p in fake.posts if not p['reduce_only']], [], '\n'.join(out[-20:]))

    def test_glitch_in_an_earlier_market_does_not_block_this_one(self):
        with mock.patch.dict(os.environ, {'BIGBET_GLITCH_AT': str(LOOP.START - 2000)}):  # previous market's window
            state, fake, out, tmp = self.run_loop(crash=False, fill_script=[10])
        self.assertEqual(len([p for p in fake.posts if not p['reduce_only']]), 1, '\n'.join(out[-20:]))
        self.assertEqual(state['history'][-1]['result'], 'yes')

    def test_long_outage_still_pauses_after_two_minutes(self):
        self._with_outage(100, 420)                         # 320 s: longer than the 120 s grace
        d = Path(tempfile.mkdtemp(prefix='v127loop_'))
        with mock.patch.dict(os.environ, {'RE5_TEST_TMP': str(d)}):
            with self.assertRaises(SystemExit):
                self.run_loop(crash=False, fill_script=[10])
        j = next(d.rglob('j.json'))
        s = json.loads(j.read_text())
        self.assertTrue(s['halted']); self.assertIn('ReadTimeout', s['reason'])
        self.assertTrue(ops.is_connection_problem(s['reason']))
        import RE5.exchange as ex, RE5.controller as ctl          # run_loop's cleanup was skipped by SystemExit
        ex.ExchangeClient = self._orig_client; ctl.time = self._orig_ctl_time

    def setUp(self):
        import RE5.exchange as ex, RE5.controller as ctl
        self._orig_client, self._orig_ctl_time = ex.ExchangeClient, ctl.time
        import builtins; self._print = builtins.print
        self.addCleanup(setattr, builtins, 'print', self._print)


class AutoRestartDecision(OpsBase):
    """ops.auto_restart_prepare: 'go' only when flat, account matches and the pause was a connection problem."""
    def _halt(self, reason):
        rt = ops.runtime_dir('prod'); j = rt / 're5_journal.json'
        from RE5.controller import Controller
        c = Controller(self.ex, j, log=lambda *_: None); c.recover()
        s = c.j.read(); s['halted'] = True; s['reason'] = reason; c.j.save(s)
        return c
    def test_connection_pause_is_cleared_and_go(self):
        c = self._halt("OPERATIONAL_ERROR ReadTimeout: HTTPSConnectionPool(host='external-api.kalshi.com', port=443): Read timed out.")
        verdict, msg = ops.auto_restart_prepare('prod', api=self.ex)
        self.assertEqual(verdict, 'go', msg); self.assertFalse(c.j.read()['halted'])
        self.assertIn('AUTO_CLEAR_CONNECTION_HALT', (ops.runtime_dir('prod') / 're5_live_events.jsonl').read_text())
    def test_other_pauses_need_a_person(self):
        for reason in ('DAILY_LOSS_LIMIT realized -2.17', 'OPERATIONAL_ERROR SafetyError: exchange holds KXETH15M exposure but journal is flat',
                       'SETTLED_BUT_POSITION_REMAINS'):
            c = self._halt(reason)
            verdict, msg = ops.auto_restart_prepare('prod', api=self.ex)
            self.assertEqual(verdict, 'never', reason); self.assertTrue(c.j.read()['halted'])
    def test_kalshi_unreachable_means_wait(self):
        self._halt('OPERATIONAL_ERROR ReadTimeout: Read timed out.')
        class Down(OpsKalshi):
            def positions(self): raise ApiFailure('read transport failure')
        verdict, msg = ops.auto_restart_prepare('prod', api=Down(Clock()))
        self.assertEqual(verdict, 'wait', msg)
    def test_open_position_never(self):
        from TEST_RE5 import cand
        from RE5.controller import Controller
        c = Controller(self.ex, ops.runtime_dir('prod') / 're5_journal.json', log=lambda *_: None); c.recover()
        self.assertTrue(c.entry(cand()))
        verdict, msg = ops.auto_restart_prepare('prod', api=self.ex)
        self.assertEqual(verdict, 'never'); self.assertIn('open', msg)


class AppAutoRestartWiring(unittest.TestCase):
    def test_app_source(self):
        src = (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8')
        self.assertIn('self.consider_auto_restart(r.get(\'acc\'))', src)
        self.assertIn('glitch_at=time.time())', src)                                # restart skips the outage market
        self.assertIn("ops.request_stop(a['rt']); self.plan = None", src)          # STOP button cancels auto-restart
        self.assertIn("ops.request_flatten(a['rt']); self.plan = None", src)       # EMERGENCY SELL cancels auto-restart
        self.assertEqual((ops.AUTO_RESTART_MAX, ops.AUTO_RESTART_RETRY_S, ops.AUTO_RESTART_MIN_MINUTES), (3, 60, 15))


class AppAutoRestartDecisions(unittest.TestCase):
    """The app's auto-restart decisions, run with a stand-in window (no screen needed)."""
    @classmethod
    def setUpClass(cls):
        import types
        fake = types.ModuleType('tkinter'); fake.Tk = object
        for sub in ('ttk', 'messagebox', 'filedialog', 'simpledialog'):
            m = types.ModuleType('tkinter.' + sub); setattr(fake, sub, m); sys.modules['tkinter.' + sub] = m
        sys.modules.setdefault('tkinter', fake)
        spec = importlib.util.spec_from_file_location('eth_app_v12', ROOT / 'BIGBET_LIVE_STUDIO_V12.py')
        cls.app = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.app)
        cls.A = cls.app.Studio

    def stub(self, plan):
        A = self.A
        class Stub:
            def __init__(s):
                s.plan = plan; s.attached = None; s.busy = False; s.mode = 'LIVE'; s.settings = {}; s.msgs = []; s.after_calls = []
            def say(s, t): s.msgs.append(t)
            def after(s, ms, fn): s.after_calls.append(ms)
            def run_checks(s): pass
            def attach(s, a): s.attached = a
            def work(s, job, done, label=None):
                try: r = job(); done(r, None)
                except Exception as e: done(None, e)
            _plan_minutes_left = A._plan_minutes_left; try_auto_restart = A.try_auto_restart
        return Stub()

    @staticmethod
    def plan(mins=120, r=0):
        import time
        return dict(end=time.time() + mins * 60, usd=10, restarts=r)

    CONN = {'ok': True, 'message': 'x', 'state': {'reason': "OPERATIONAL_ERROR ReadTimeout: HTTPSConnectionPool(host='external-api.kalshi.com', port=443): Read timed out.", 'halted': True}}

    def test_connection_pause_schedules_retry(self):
        s = self.stub(self.plan()); self.A.consider_auto_restart(s, self.CONN)
        self.assertEqual(s.after_calls, [60000]); self.assertIsNotNone(s.plan)

    def test_never_restarts_for_other_reasons(self):
        for acc in ({'ok': True, 'message': 'flat', 'state': {'reason': 'SESSION_END', 'halted': False}},
                    {'ok': True, 'message': 'x', 'state': {'reason': 'DAILY_LOSS_LIMIT realized -4.35', 'halted': True}},
                    {'ok': False, 'message': 'Account does not match the journal: SafetyError: exchange holds exposure', 'state': {'reason': '', 'halted': False}},
                    {'ok': True, 'message': 'x', 'state': {'reason': 'OPERATOR_KILL_SWITCH', 'halted': True}}):
            s = self.stub(self.plan()); self.A.consider_auto_restart(s, acc)
            self.assertEqual(s.after_calls, []); self.assertIsNone(s.plan)

    def test_limits(self):
        s = self.stub(None); self.A.consider_auto_restart(s, self.CONN); self.assertEqual(s.after_calls, [])
        s = self.stub(self.plan(r=3)); self.A.consider_auto_restart(s, self.CONN); self.assertIsNone(s.plan)
        s = self.stub(self.plan(mins=10)); self.A.consider_auto_restart(s, self.CONN); self.assertIsNone(s.plan)

    def test_try_restart_paths(self):
        import time
        ops_ = self.app.ops; calls = {}
        saved = (ops_.topup_if_needed, ops_.start_session, ops_.auto_restart_prepare)
        self.addCleanup(lambda: (setattr(ops_, 'topup_if_needed', saved[0]), setattr(ops_, 'start_session', saved[1]),
                                 setattr(ops_, 'auto_restart_prepare', saved[2])))
        ops_.topup_if_needed = lambda env, trade_usd: dict(ok=True, message='enough')
        def fake_start(env, minutes, **k):
            calls.update(minutes=minutes, **k); return dict(rt=Path('.'), session=Path('.'), stamp='X', proc=None)
        ops_.start_session = fake_start
        ops_.auto_restart_prepare = lambda env, settings=None: ('go', 'connection pause cleared automatically')
        s = self.stub(self.plan()); s.try_auto_restart()
        self.assertTrue(s.attached and calls['live']); self.assertEqual(calls['trade_usd'], 10)
        self.assertTrue(115 <= calls['minutes'] <= 120); self.assertLess(abs(calls['glitch_at'] - time.time()), 5)
        self.assertEqual(s.plan['restarts'], 1)
        ops_.auto_restart_prepare = lambda env, settings=None: ('wait', 'Kalshi still unreachable')
        s = self.stub(self.plan()); s.try_auto_restart(); self.assertIsNone(s.attached); self.assertEqual(s.after_calls, [60000])
        ops_.auto_restart_prepare = lambda env, settings=None: ('never', 'an order or position is open')
        s = self.stub(self.plan()); s.try_auto_restart(); self.assertIsNone(s.attached); self.assertIsNone(s.plan)
        s = self.stub(self.plan()); s.mode = 'PAPER'; s.try_auto_restart(); self.assertIsNone(s.attached)


if __name__ == '__main__':
    unittest.main(verbosity=1)
