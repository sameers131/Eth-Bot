"""V12.3 tests: size ladder, news blackout, weekly drawdown stop, size-scaled limits.
Offline; no network, no real keys.

python TEST_V12_3.py
"""
import json, os, shutil, sys, tempfile, time, unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from RE5.asset import SERIES as SER  # noqa: E402  (KXETH15M)
_TMP = Path(tempfile.mkdtemp(prefix='v123test_', dir=os.environ.get('RE5_TEST_TMP')))
os.environ['BIGBET_SETTINGS_FILE'] = str(_TMP / 'settings.json')
os.environ['BIGBET_DATA_DIR'] = str(_TMP / 'DATA')
os.environ['BIGBET_ETH_SETTINGS_FILE'] = str(_TMP / 'settings.json')
os.environ['BIGBET_ETH_DATA_DIR'] = str(_TMP / 'DATA')
os.environ.pop('BIGBET_TRADE_USD', None)

from TEST_RE5 import Base, T, cand  # noqa: E402
from TEST_V12 import OpsBase, make_key  # noqa: E402
from RE5 import ops, scaling  # noqa: E402
from RE5 import controller as ctl  # noqa: E402
from RE5 import exchange  # noqa: E402

D = lambda x: Decimal(str(x))
DAY = 86400


def trade(rt, pnl, usd=10, at=None, stop=False, ticker=None):
    at = time.time() if at is None else at
    scaling.append_closed(rt, dict(closed_at=at, ticker=ticker or f'{SER}-X{at}', held='yes', qty=10, cost='9.85',
                                   pnl=str(pnl), exited_qty=10 if stop else 0, result='yes'), usd)


class SizeLimits(unittest.TestCase):
    def test_base_size_identical_to_v12_2(self):
        self.assertEqual(exchange.TRADE_USD, 10)
        self.assertEqual(exchange.MAX_NOTIONAL, D('10.00')); self.assertEqual(exchange.MAX_CONTRACTS, 11)   # ETH V1.3: size/0.90
        self.assertEqual(exchange.PROD_ARM_PHRASE, 'ARM-PROD-10USD'); self.assertEqual(ctl.DAILY_MAX_LOSS_USD, D('5.00'))
        self.assertEqual((ops.TOPUP_MIN, ops.TOPUP_AMOUNT, ops.TOPUP_DAILY_CAP), (D(15), D(20), D(40)))
    def test_every_step_derived_from_one_size(self):
        for usd, n, cap, top in ((5, 5, '2.50', ('7.5', 10, 20)), (15, 16, '7.50', ('22.5', 30, 60)), (20, 22, '10.00', (30, 40, 80)), (40, 44, '20.00', (60, 80, 160))):   # ETH V1.7 sizes; contracts = size/0.90
            self.assertEqual(scaling.max_contracts(usd), n)
            self.assertEqual(scaling.daily_loss_cap(usd), D(cap))
            self.assertEqual(scaling.topup_params(usd), tuple(D(x) for x in top))
            self.assertEqual(scaling.arm_phrase(usd), f'ARM-PROD-{usd}USD')
            self.assertLessEqual(scaling.topup_params(usd)[1], scaling.MAX_TRANSFER_HARD)
    def test_bad_sizes_refused(self):
        for bad in (0, 3, 12, 45, 50, 60, 61, 100):                       # ETH V1.7: valid sizes are 5..40 in steps of 5
            with self.assertRaises(ValueError):
                scaling.check_size(bad)
        with self.assertRaises(ValueError):
            scaling.trade_usd({'BIGBET_TRADE_USD': '100'})
    def test_engine_needs_matching_arm_phrase(self):
        os.environ['KALSHI_ENV'] = 'prod'
        os.environ['BIGBET_RE5_PROD_ARM'] = 'ARM-PROD-60USD'          # wrong size for a $10 process
        try:
            with self.assertRaises(exchange.TradingLocked):
                exchange.ExchangeClient(env='prod', write_mode='prod')
        finally:
            os.environ.pop('BIGBET_RE5_PROD_ARM', None)


class Ladder(unittest.TestCase):
    def setUp(self):
        self.rt = Path(tempfile.mkdtemp(dir=_TMP))
    @unittest.skip('ETH V1.7: ladder lock / step-down removed on request; free-choice rules are tested in TEST_ETH_V1_7.py')
    def test_fresh_is_10_and_not_eligible(self):
        st = scaling.ladder_status(self.rt)
        self.assertEqual(st['unlocked'], 10); self.assertEqual(st['allowed'], [10]); self.assertFalse(st['eligible'])
        self.assertTrue(any('40 more trades' in n for n in st['needs']))
    @unittest.skip('ETH V1.7: ladder lock / step-down removed on request; free-choice rules are tested in TEST_ETH_V1_7.py')
    def test_40_wins_still_need_a_real_stop(self):
        now = time.time()
        for i in range(40):
            trade(self.rt, '0.20', at=now - 3600 + i)
        st = scaling.ladder_status(self.rt)
        self.assertFalse(st['eligible']); self.assertEqual(st['needs'], ['one real stop-loss exit seen (none yet)'])
        with self.assertRaises(ValueError):
            scaling.unlock_next(self.rt)
    @unittest.skip('ETH V1.7: ladder lock / step-down removed on request; free-choice rules are tested in TEST_ETH_V1_7.py')
    def test_unlock_after_40_with_one_stopped_loss(self):
        now = time.time()
        for i in range(39):
            trade(self.rt, '0.20', at=now - 3600 + i)
        trade(self.rt, '-0.90', at=now - 100, stop=True)
        st = scaling.ladder_status(self.rt)
        self.assertTrue(st['eligible'], st['needs']); self.assertEqual(st['next'], 20)
        self.assertEqual(scaling.unlock_next(self.rt), 20)
        st = scaling.ladder_status(self.rt)
        self.assertEqual(st['allowed'], [10, 20]); self.assertEqual(st['trades_at_size'], 0)   # count restarts at $20
    @unittest.skip('ETH V1.7: ladder lock / step-down removed on request; free-choice rules are tested in TEST_ETH_V1_7.py')
    def test_two_losses_block_the_step(self):
        now = time.time()
        for i in range(38):
            trade(self.rt, '0.20', at=now - 3600 + i)
        trade(self.rt, '-9.85', at=now - 200); trade(self.rt, '-0.9', at=now - 100, stop=True)
        st = scaling.ladder_status(self.rt)
        self.assertFalse(st['eligible']); self.assertTrue(any('at most 1 loss' in n for n in st['needs']))
    @unittest.skip('ETH V1.7: ladder lock / step-down removed on request; free-choice rules are tested in TEST_ETH_V1_7.py')
    def test_two_losses_at_20_step_down(self):
        scaling._write_ladder(self.rt, dict(unlocked=20, unlocked_at=time.time() - 9 * DAY, events=[]))
        now = time.time()
        trade(self.rt, '-19.7', usd=20, at=now - 8 * DAY); trade(self.rt, '0.4', usd=20, at=now - 7.5 * DAY)
        trade(self.rt, '-2.0', usd=20, at=now - 100, stop=True)
        st = scaling.ladder_status(self.rt)
        self.assertEqual(st['unlocked'], 10); self.assertEqual(st['stepped_down'], (20, 10))
        self.assertEqual(json.loads((self.rt / 'ladder.json').read_text())['events'][-1]['kind'], 'STEP_DOWN')
    @unittest.skip('ETH V1.7: ladder lock / step-down removed on request; free-choice rules are tested in TEST_ETH_V1_7.py')
    def test_manual_down_only(self):
        scaling._write_ladder(self.rt, dict(unlocked=40, events=[]))
        self.assertEqual(scaling.lower_to(self.rt, 20), 20)
        self.assertEqual(scaling.lower_to(self.rt, 60), 20)          # never up by hand
    def test_seed_from_journal_history_once(self):
        hist = [dict(closed_at=1000 + i, ticker=f'{SER}-H{i}', pnl='0.2', exited_qty=0) for i in range(20)]
        scaling.record_close(self.rt, dict(closed_at=2000, ticker=SER + '-NEW', pnl='0.2', exited_qty=0), 10, hist)
        rows = scaling.read_ledger(self.rt, hist)
        self.assertEqual(len(rows), 21); self.assertTrue(all(r['trade_usd'] == 10 for r in rows))


class Weekly(unittest.TestCase):
    def setUp(self):
        self.rt = Path(tempfile.mkdtemp(dir=_TMP))
    def test_advisory_at_10_enforced_at_20(self):
        now = time.time()
        trade(self.rt, '-9.85', at=now - 2 * DAY); trade(self.rt, '-9.85', at=now - DAY); trade(self.rt, '0.5', at=now - 100)
        blocked, msg = scaling.weekly_check(self.rt, 10)
        self.assertFalse(blocked); self.assertIn('advisory', msg)
        blocked, msg = scaling.weekly_check(self.rt, 20)
        self.assertFalse(blocked)                                     # -19.20 > -20
        trade(self.rt, '-1.0', at=now - 50)
        blocked, msg = scaling.weekly_check(self.rt, 20)
        self.assertTrue(blocked); self.assertTrue(msg.startswith('WEEKLY STOP'))
    def test_old_losses_roll_off(self):
        trade(self.rt, '-30', usd=20, at=time.time() - 8 * DAY)
        self.assertFalse(scaling.weekly_check(self.rt, 20)[0])
    def test_release_after_review(self):
        trade(self.rt, '-25', usd=20, at=time.time() - DAY)
        self.assertTrue(scaling.weekly_check(self.rt, 20)[0])
        scaling.release_weekly_stop(self.rt)
        self.assertFalse(scaling.weekly_check(self.rt, 20)[0])
        trade(self.rt, '-21', usd=20, at=time.time() + 1)
        self.assertTrue(scaling.weekly_check(self.rt, 20)[0])


class Blackout(unittest.TestCase):
    CAL = None
    @classmethod
    def setUpClass(cls):
        cls.CAL = scaling.load_calendar()
    def close(self, y, mo, d, h, mi):
        return scaling.et_to_utc(datetime(y, mo, d, h, mi)).timestamp()
    def test_calendar_loads_and_dst(self):
        self.assertGreaterEqual(len(self.CAL), 8)
        t, name = self.CAL[0]
        self.assertEqual(t, datetime(2026, 11, 6, 13, 30, tzinfo=timezone.utc))      # 8:30 ET (EST) = 13:30 UTC
        self.assertEqual(scaling.et_to_utc(datetime(2026, 7, 1, 8, 30)).hour, 12)     # summer: EDT
        self.assertEqual(scaling.utc_to_et(t).strftime('%H:%M'), '08:30')
    def test_jobs_report_window(self):
        f = lambda h, m: scaling.blackout_for(self.close(2026, 11, 6, h, m), self.CAL)
        self.assertIsNone(f(8, 15))                 # 8:00-8:15 ends before 8:25
        self.assertIn('Jobs', f(8, 30))             # 8:15-8:30
        self.assertIn('Jobs', f(8, 45)); self.assertIn('Jobs', f(9, 0))
        self.assertIsNone(f(9, 15))                 # 9:00-9:15 starts at release + 30
    def test_fed_day_covers_decision_and_press(self):
        f = lambda h, m: scaling.blackout_for(self.close(2026, 12, 9, h, m), self.CAL)
        self.assertIsNone(f(13, 45))
        for h, m in ((14, 0), (14, 15), (14, 30), (14, 45), (15, 0)):
            self.assertIsNotNone(f(h, m), (h, m))
        self.assertIsNone(f(15, 15))
    def test_ordinary_day_clear(self):
        self.assertIsNone(scaling.blackout_for(self.close(2026, 11, 5, 8, 45), self.CAL))


class EngineGates(Base):
    """Controller integration: same engine, size set on the instance."""
    def setUp(self):
        super().setUp()
        self._orig = scaling.load_calendar
        t0 = datetime.fromtimestamp(self.clock() + 60, timezone.utc)
        scaling.load_calendar = lambda path=None: [(t0, 'Test release')]
    def tearDown(self):
        scaling.load_calendar = self._orig
        super().tearDown()
    def test_blackout_blocks_above_10(self):
        self.c.trade_usd = 20
        self.assertFalse(self.c.entry(cand()))
        self.assertIn('news blackout', self.c.last_entry['reason']); self.assertEqual(self.ex.posts, [])
    def test_blackout_advisory_at_10_still_trades(self):
        self.assertTrue(self.c.entry(cand()))
        self.assertTrue(any(e['event'] == 'ADVISORY_NEWS_BLACKOUT' for e in self.events))
    def test_broken_calendar_blocks_above_10_only(self):
        def boom(path=None): raise OSError('x')
        scaling.load_calendar = boom
        self.c.trade_usd = 20
        self.assertFalse(self.c.entry(cand())); self.assertIn('scaling check failed', self.c.last_entry['reason'])
        self.c.trade_usd = 10
        self.assertTrue(self.c.entry(cand()))
    def test_weekly_stop_blocks_above_10(self):
        scaling.load_calendar = lambda path=None: []
        rt = Path(self.c.j.path).parent
        trade(rt, '-25', usd=20, at=self.clock() - DAY)
        self.c.trade_usd = 20
        self.assertFalse(self.c.entry(cand())); self.assertIn('WEEKLY STOP', self.c.last_entry['reason'])
    def test_closed_trades_reach_ledger_with_size(self):
        scaling.load_calendar = lambda path=None: []
        self.assertTrue(self.c.entry(cand())); self.ex.settle(T, 'yes'); self.clock.t += 400
        self.assertTrue(self.c.poll_settlement())
        rows = scaling.read_ledger(Path(self.c.j.path).parent)
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['trade_usd'], 10); self.assertGreater(D(rows[0]['pnl']), 0)
    def test_stop_exit_marked_real_stop(self):
        scaling.load_calendar = lambda path=None: []
        self.c.entry(cand()); self.ex.fill_script = [10]
        self.c.on_poll(ticker=T, seconds_left=60, held_bid=D('0.85'), stop_reason='X')
        rows = scaling.read_ledger(Path(self.c.j.path).parent)
        self.assertTrue(rows[-1]['real_stop']); self.assertLess(D(rows[-1]['pnl']), 0)


class OpsScaling(OpsBase):
    def test_live_start_refuses_size_not_unlocked(self):          # ETH V1.7: every size 5..40 may start; a bad size is still refused
        with self.assertRaises(ValueError):
            ops.start_session('prod', 15, live=True, trade_usd=45, engine_cmd=[sys.executable, '-c', 'pass'],
                              settings={'key_id': 'test-key-id-123', 'key_path': str(make_key())})
    def test_topup_scaled(self):
        self.ex.shards = {0: D('100'), 2: D('25')}
        self.assertEqual(ops.topup_plan('prod', api=self.ex)['action'], 'none')            # $10: 25 >= 15
        p = ops.topup_plan('prod', api=self.ex, trade_usd=20)                              # $20: 25 < 30
        self.assertEqual(p['action'], 'transfer'); self.assertEqual(p['amount'], D('40'))
        r = ops.topup_if_needed('prod', api=self.ex, sleep=lambda s: None, trade_usd=20)
        self.assertTrue(r['ok'], r); self.assertEqual(self.ex.shards[2], D('65'))
    def test_ladder_info_and_export_files(self):
        trade(self.rt, '0.2')
        st = ops.ladder_info('prod')
        self.assertEqual(st['allowed'], [5, 10, 15, 20, 25, 30, 35, 40]); self.assertFalse(st['weekly_blocked'])   # ETH V1.7: free choice


if __name__ == '__main__':
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
