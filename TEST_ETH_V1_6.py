"""ETH V1.6 tests (ETH ONLY): the daily-stop override works after EVERY loss. Nothing else changed.
Offline; no network, no real keys.   Run from this folder:  python TEST_ETH_V1_6.py
"""
import sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
from TEST_RE5 import cand, T  # noqa: E402
from TEST_V12 import OpsBase  # noqa: E402
from RE5 import controller as ctl, ops  # noqa: E402


class DailyLimitCounting(unittest.TestCase):
    def test_without_override_same_as_v15(self):
        self.assertTrue(ctl.daily_limit_hit(dict(loss_count=1, realized_usd='0.10')))
        self.assertTrue(ctl.daily_limit_hit(dict(loss_count=0, realized_usd='-5.00')))
        self.assertFalse(ctl.daily_limit_hit(dict(loss_count=0, realized_usd='-4.99')))
    def test_counts_from_override(self):
        s = dict(loss_count=1, realized_usd='-1.50', ovr_losses=1, ovr_realized='-1.50')
        self.assertFalse(ctl.daily_limit_hit(s))
        self.assertTrue(ctl.daily_limit_hit(dict(s, loss_count=2, realized_usd='-1.20')))


class OverrideBase(OpsBase):
    def setUp(self):
        super().setUp()
        self.gtmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.gtmp.cleanup)
        self.risk = ops.risk_module('prod')
        p = mock.patch.object(self.risk, 'DAILY_DB', Path(self.gtmp.name) / 'guard.sqlite3'); p.start(); self.addCleanup(p.stop)
    def ctl(self):
        return ctl.Controller(self.ex, self.rt / 're5_journal.json', log=lambda *_: None)
    def lose(self, tk):
        c = self.ctl(); c.recover()
        self.assertTrue(c.entry(dict(cand(), ticker=tk)), 'entry refused ' + tk)
        self.ex.settle(tk, 'no'); c.poll_settlement(force=True)
        return c


class Override(OverrideBase):
    def test_three_losses_three_overrides(self):
        for i, tk in enumerate(['KXETH15M-26NOV011930-30', 'KXETH15M-26NOV011945-45', 'KXETH15M-26NOV012000-00']):
            c = self.lose(tk)
            s = c.j.read(); self.assertTrue(s['halted'], f'loss {i + 1} did not pause'); self.assertTrue(s['reason'].startswith('DAILY_LOSS'))
            ok, msg = ops.override_daily_stop('prod', api=self.ex, risk=self.risk)
            self.assertTrue(ok, msg); self.assertFalse(self.ctl().j.read()['halted'])
        self.assertIn('OPERATOR_OVERRIDE_DAILY_STOP', (self.rt / 're5_live_events.jsonl').read_text())

    def test_guard_override_repeats(self):
        self.risk.daily_loss_limit_cents(965)
        for k in range(2):
            self.risk.daily_finalize_stop(f'KXETH15M-X{k}', -300); self.risk.daily_finalize_stop(f'KXETH15M-Y{k}', -10)
            self.assertTrue(ops.guard_blocked('prod', self.risk))
            ok, msg = ops.override_daily_stop('prod', api=self.ex, risk=self.risk)
            self.assertTrue(ok, msg); self.assertFalse(ops.guard_blocked('prod', self.risk))
        con = self.risk.connect(Path(self.gtmp.name) / 'i.sqlite3')
        try:
            self.risk.approve(con, 'KXETH15M-Z', 'yes', '9.65', data_age_seconds=1, market_seconds_left=120, contracts=10)
        finally:
            con.close()

    def test_nothing_to_override(self):
        ok, msg = ops.override_daily_stop('prod', api=self.ex, risk=self.risk)
        self.assertFalse(ok); self.assertIn('not active', msg)

    def test_other_pause_not_overridden(self):
        self.journal().halt('OPERATIONAL_ERROR SafetyError: x')
        ok, msg = ops.override_daily_stop('prod', api=self.ex, risk=self.risk)
        self.assertFalse(ok); self.assertTrue(self.journal().read()['halted'])

    def test_open_position_refused(self):
        c = self.ctl(); c.recover(); self.assertTrue(c.entry(cand()))
        s = c.j.read(); s.update(halted=True, reason='DAILY_LOSS_LIMIT losses=1'); c.j.save(s)
        ok, msg = ops.override_daily_stop('prod', api=self.ex, risk=self.risk)
        self.assertFalse(ok); self.assertIn('open', msg)

    def test_running_engine_refused(self):
        self.lose('KXETH15M-26NOV011930-30')
        with mock.patch.object(ops, 'engine_state', return_value=('running', None)):
            ok, msg = ops.override_daily_stop('prod', api=self.ex, risk=self.risk)
        self.assertFalse(ok); self.assertIn('STOP NEW TRADES', msg)

    def test_override_expires_at_day_rollover(self):
        c = self.lose('KXETH15M-26NOV011930-30')
        self.assertTrue(ops.override_daily_stop('prod', api=self.ex, risk=self.risk)[0])
        s = c.j.read(); s['day'] = '2000-01-01'; c.j.save(s)
        s = c._roll_day(c.j.read())
        self.assertNotIn('ovr_losses', s); self.assertEqual(s['loss_count'], 0)

    def test_guard_file_is_eth(self):
        m = ops.risk_module('prod')
        self.assertIn('ETH_LIVE_DAILY_GUARD', str(m.DAILY_DB)); self.assertNotIn('BTC', str(m.DAILY_DB))


class AppWiring(unittest.TestCase):
    def test_app(self):
        src = (ROOT / 'BIGBET_LIVE_STUDIO_V12.py').read_text(encoding='utf-8')
        self.assertIn("'Override: trade today', self.override_daily", src)
        self.assertIn('ops.override_daily_stop(ENV)', src)
        self.assertIn('you can override after every loss', src)
        self.assertIn('V1.9.4 / V7-D Rule C', src); self.assertIn("'TEST_ETH_V1_7.py'", src)
        self.assertEqual(ops.VERSION, 'ETH V1.9.4 / RE5.5')


if __name__ == '__main__':
    unittest.main(verbosity=1)
