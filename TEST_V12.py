"""V12 / Revision 3 tests: fee fix, recovered counters, new logs, app operations (checklist,
auto-clear, shard top-up, export, import, engine lock). Offline; no network, no real keys.

python TEST_V12.py
"""
import csv, json, os, shutil, sys, tempfile, time, unittest, uuid, zipfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from RE5.asset import SERIES as SER  # noqa: E402  (KXETH15M)
_TMP = Path(tempfile.mkdtemp(prefix='v12test_', dir=os.environ.get('RE5_TEST_TMP')))
os.environ['BIGBET_SETTINGS_FILE'] = str(_TMP / 'settings.json')
os.environ['BIGBET_DATA_DIR'] = str(_TMP / 'DATA')
os.environ['BIGBET_ETH_SETTINGS_FILE'] = str(_TMP / 'settings.json')
os.environ['BIGBET_ETH_DATA_DIR'] = str(_TMP / 'DATA')

from TEST_RE5 import Base, Clock, FakeKalshi, REAL_FILL_NOV3, cand, T  # noqa: E402
from RE5 import ops  # noqa: E402
from RE5.accounting import settlement_cash  # noqa: E402
from RE5.controller import Controller  # noqa: E402
from RE5.exchange import UnknownOrderState  # noqa: E402
from RE5.report import write_daily_summary  # noqa: E402

D = lambda x: Decimal(str(x))


class OpsKalshi(FakeKalshi):
    def __init__(self, clock):
        super().__init__(clock); self.shards = {0: D('35'), 2: D('5')}; self.transfers = []; self.transfer_mode = 'ok'
    def balance_breakdown(self): return dict(self.shards)
    def eth_market_shard(self, series=None): return 2
    def transfer_between_shards(self, *, amount_dollars, source_shard, destination_shard, client_transfer_id):
        self.transfers.append((D(amount_dollars), source_shard, destination_shard))
        if self.transfer_mode == 'unknown':
            raise UnknownOrderState('timeout')
        self.shards[source_shard] -= D(amount_dollars); self.shards[destination_shard] += D(amount_dollars)
        return {'transfer_id': 't-1'}


def make_key():
    keyfile = _TMP / 'kalshi.pem'
    if not keyfile.exists():
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        keyfile.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    return keyfile


def rows(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


class Rev3Accounting(unittest.TestCase):
    def test_real_settlement_fee_not_double_counted(self):
        rec = {'ticker': SER + '-26NOV030645-45', 'market_result': 'no', 'yes_count_fp': '0.00', 'no_count_fp': '10.00',
               'revenue': 1000, 'fee_cost': '0.009000'}
        s = settlement_cash(rec, 'no', 10)
        self.assertEqual(s['net'], D('10')); self.assertEqual(s['reported_fee'], D('0.009'))


class Rev3Recovery(Base):
    def test_nov3_real_record_pnl_matches_kalshi_card(self):
        tk = SER + '-26NOV030645-45'; cid = str(uuid.uuid4()); s = self.state()
        s['pending'] = dict(client_id=cid, purpose='ENTRY', ticker=tk, held='no', qty=10, yes_price='0.01', book_side='ask',
                            reduce_only=False, submitted_at=self.clock(), order_id='01a1a137-c0a0-7c9c-9247-33ae39319086',
                            close_time=self.clock() + 150, strike='77075.55')
        self.c.j.save(s)
        self.ex.orders.append({'order_id': '01a1a137-c0a0-7c9c-9247-33ae39319086', 'client_order_id': cid, 'ticker': tk,
                               'status': 'executed', 'fill_count_fp': '10.00', 'remaining_count_fp': '0.00', '_ts': self.clock()})
        self.ex.fills.append(dict(REAL_FILL_NOV3, _ts=self.clock()))
        self.ex.settlements.append({'ticker': tk, 'market_result': 'no', 'yes_count_fp': '0.00', 'no_count_fp': '10.00',
                                    'revenue': 1000, 'fee_cost': '0.009000'})        # as Kalshi really reported it
        self.clock.t += 600
        st = self.make().recover()
        self.assertEqual(D(st['history'][-1]['pnl']), D('0.121'))                    # Kalshi card: +$0.12
        self.assertEqual(st['stats']['recovered_orders'], 1); self.assertEqual(st['stats']['fills'], 1)
        r = rows(Path(self.tmp.name) / 'live_orders.csv')
        self.assertEqual(r[-1]['recovered'], 'yes'); self.assertEqual(r[-1]['result'], 'FILLED')
        self.assertIn('recovered 1', self.make().status_line())


class Rev3OrderLog(Base):
    def test_entry_fill_row_has_signal_book_and_slippage(self):
        self.ex.set_book(yes_ask='0.97')
        self.assertTrue(self.c.entry(dict(cand(ask='0.96'), checkpoint_s='150', actual_seconds_left='149.5')))
        r = rows(Path(self.tmp.name) / 'live_orders.csv')[-1]
        self.assertEqual((r['purpose'], r['result'], r['checkpoint_s']), ('ENTRY', 'FILLED', '150'))
        self.assertEqual(D(r['signal_ask']), D('0.96')); self.assertEqual(D(r['best_ask']), D('0.97'))
        self.assertEqual(D(r['slip_vs_signal_c']), D('1.00')); self.assertEqual(D(r['slip_vs_best_c']), D('0'))
        self.assertTrue(r['book_asks_top3']); self.assertTrue(r['shard_balance'])
        self.assertEqual(D(r['cost_or_proceeds']), D(r['avg_fill']) * int(r['filled']) + D(r['fees']))
    def test_no_fill_and_reject_rows(self):
        self.ex.fill_script = [0]
        self.assertFalse(self.c.entry(cand()))
        self.ex.mode = 'reject'
        self.assertFalse(self.c.entry(cand()))
        r = rows(Path(self.tmp.name) / 'live_orders.csv')
        self.assertEqual([x['result'] for x in r], ['NO_FILL', 'REJECTED'])
        self.assertEqual(r[-1]['reject_code'], '400:invalid_price')
    def test_exit_rows(self):
        self.assertTrue(self.c.entry(cand()))
        self.ex.fill_script = [4, 6]
        self.ex.book['yes'] = [(D('0.60'), D(1000))]     # ETH V1.4: exits are priced from the live book
        self.c.on_poll(ticker=T, seconds_left=100, held_bid='0.60', stop_reason='TEST_STOP')
        self.ex.book['yes'] = [(D('0.58'), D(1000))]
        self.c.on_poll(ticker=T, seconds_left=98, held_bid='0.58', stop_reason=None)
        r = [x for x in rows(Path(self.tmp.name) / 'live_orders.csv') if x['purpose'] == 'EXIT']
        self.assertEqual([x['result'] for x in r], ['PARTIAL', 'FILLED'])
        self.assertEqual(r[0]['exit_reason'], 'TEST_STOP'); self.assertEqual(D(r[0]['limit_held']), D('0.58'))


class Rev3PriceLogs(unittest.TestCase):
    def test_markets_csv_and_bid_columns_from_real_loop(self):
        import TEST_RE5_LOOP
        t = TEST_RE5_LOOP.Loop('test_settlement_path')
        state, fake, out, tmp = t.run_loop(crash=False, fill_script=[10])
        m = rows(t.app / 'markets.csv')
        self.assertEqual(len(m), 1, out[-20:])
        self.assertEqual(m[0]['ticker'], TEST_RE5_LOOP.TICKER)
        self.assertIn('FILLED', m[0]['live_action']); self.assertTrue(m[0]['checkpoints'])
        self.assertTrue(m[0]['paper_decision']); self.assertEqual(m[0]['strike'], '80000')
        raw = rows(t.app / 'eth_v42_market_quotes.csv')
        self.assertIn('bid', raw[0]); self.assertTrue(any(r['bid'] and r['spread'] for r in raw))
        self.assertTrue((tmp / 'rt' / 'live_orders.csv').exists())

    def test_daily_summary(self):
        rt = Path(tempfile.mkdtemp(dir=_TMP))
        ev = [dict(time=1793706148, event='ORDER_RESULT', purpose='ENTRY', filled=10),
              dict(time=1793706150, event='ORDER_RESULT', purpose='ENTRY', filled=0),
              dict(time=1793706160, event='POSITION_CLOSED', pnl='0.121'),
              dict(time=1793706170, event='SHARD_TOPUP', amount='20'),
              dict(time=1793706180, event='ENTRY_SKIPPED')]
        (rt / 're5_live_events.jsonl').write_text('\n'.join(json.dumps(e) for e in ev), encoding='utf-8')
        r = rows(write_daily_summary(rt))
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]['entry_orders'], r[0]['entry_filled'], r[0]['entry_no_fill'], r[0]['wins'], r[0]['skips']),
                         ('2', '1', '1', '1', '1'))
        self.assertEqual(D(r[0]['real_pnl']), D('0.121')); self.assertEqual(D(r[0]['shard_topups_usd']), D('20'))


class OpsBase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(_TMP / 'DATA', ignore_errors=True)
        self.clock = Clock(); self.ex = OpsKalshi(self.clock); self.ex.set_book()
        self.rt = ops.runtime_dir('prod')
    def journal(self):
        return Controller(self.ex, self.rt / 're5_journal.json', log=lambda *_: None).j


class OpsChecklist(OpsBase):
    def test_stale_lock_and_session_end_halt_cleared_automatically(self):
        j = self.journal(); j.halt('SESSION_END: entries stopped'); j.halt('OPERATOR_KILL_SWITCH: no new entries')
        (self.rt / 'RUNNING.lock').write_text('20261103_073002')          # RE5.2-style plain lock, process gone
        (self.rt / 'KILL_SWITCH').write_text('x')
        r = ops.check_account('prod', api=self.ex)
        self.assertTrue(r['ok']); self.assertFalse(r['state']['halted'])
        self.assertFalse((self.rt / 'RUNNING.lock').exists()); self.assertFalse((self.rt / 'KILL_SWITCH').exists())
        self.assertEqual(len(r['actions']), 3)
    def test_error_halt_is_not_auto_cleared_but_can_be_cleared_by_operator(self):
        self.journal().halt('OPERATIONAL_ERROR SafetyError: x')
        r = ops.check_account('prod', api=self.ex)
        self.assertTrue(r['state']['halted']); self.assertEqual(ops.halt_kind(r['state']), 'other')
        ok, msg = ops.clear_halt('prod', api=self.ex)
        self.assertTrue(ok, msg); self.assertFalse(self.journal().read()['halted'])
    def test_daily_loss_cannot_be_cleared(self):
        j = self.journal(); s = j.read(); s.update(halted=True, reason='DAILY_LOSS_LIMIT losses=1'); j.save(s)
        ok, msg = ops.clear_halt('prod', api=self.ex)
        self.assertFalse(ok); self.assertIn('7 PM', msg)
    def test_mismatch_reports_not_ok(self):
        self.ex.extra_positions = [{'ticker': SER + '-26NOV031200-00', 'position_fp': '5.00'}]
        r = ops.check_account('prod', api=self.ex)
        self.assertFalse(r['ok']); self.assertIn('does not match', r['message'])
    def test_running_engine_is_detected_and_not_touched(self):
        (self.rt / 'RUNNING.lock').write_text(json.dumps({'stamp': 'x', 'pid': os.getpid()}))
        (self.rt / 'HEARTBEAT').write_text(json.dumps({'pid': os.getpid(), 'time': time.time()}))
        self.assertEqual(ops.engine_state(self.rt)[0], 'running')
        r = ops.check_account('prod', api=self.ex)
        self.assertEqual(r['engine'], 'running'); self.assertTrue((self.rt / 'RUNNING.lock').exists())
        (self.rt / 'HEARTBEAT').write_text(json.dumps({'pid': os.getpid(), 'time': time.time() - 999}))
        self.assertEqual(ops.engine_state(self.rt)[0], 'not_responding')


class OpsTopUp(OpsBase):
    def test_plan_states(self):
        self.assertEqual(ops.topup_plan('prod', api=self.ex)['action'], 'transfer')
        self.ex.shards = {0: D('3'), 2: D('5')}
        self.assertEqual(ops.topup_plan('prod', api=self.ex)['action'], 'blocked')
        self.ex.shards = {0: D('0'), 2: D('16')}
        self.assertEqual(ops.topup_plan('prod', api=self.ex)['action'], 'none')
    def test_moves_20_and_daily_cap_40(self):
        r = ops.topup_if_needed('prod', api=self.ex, sleep=lambda s: None)
        self.assertTrue(r['ok'], r); self.assertEqual(self.ex.shards[2], D('25')); self.assertEqual(self.ex.transfers, [(D('20'), 0, 2)])
        self.ex.shards = {0: D('100'), 2: D('1')}
        self.assertTrue(ops.topup_if_needed('prod', api=self.ex, sleep=lambda s: None)['ok'])
        self.ex.shards = {0: D('100'), 2: D('1')}
        r = ops.topup_if_needed('prod', api=self.ex, sleep=lambda s: None)
        self.assertFalse(r['ok']); self.assertIn('limit', r['message']); self.assertEqual(len(self.ex.transfers), 2)
        led = json.loads((self.rt / 'topups.json').read_text())
        self.assertEqual(D(led[ops.utcday()]['total']), D('40'))
        self.assertIn('SHARD_TOPUP', (self.rt / 're5_live_events.jsonl').read_text())
    def test_unknown_transfer_still_counts_against_cap(self):
        self.ex.transfer_mode = 'unknown'
        r = ops.topup_if_needed('prod', api=self.ex, wait_s=0, sleep=lambda s: None)
        self.assertFalse(r['ok'])
        self.assertEqual(D(json.loads((self.rt / 'topups.json').read_text())[ops.utcday()]['total']), D('20'))


class OpsFiles(OpsBase):
    def make_session(self, extra=None):
        sess = self.rt / 'sessions' / '20261103_073002'; (sess / 'APP').mkdir(parents=True)
        (sess / 'collector_stdout.log').write_text('LIVE_FILLED ...\n'); (sess / 'markets.csv').write_text('ticker\nX\n')
        (sess / 'APP' / 'key.pem').write_text('-----BEGIN RSA PRIVATE KEY-----')
        for name, text in (extra or {}).items():
            (sess / name).write_text(text)
        self.journal(); (self.rt / 'live_orders.csv').write_text('time_utc\n')
        return sess
    def test_export_contents_without_keys(self):
        sess = self.make_session()
        z = ops.export_session(self.rt, sess, key_id='abcd1234-secret-id', out_dir=_TMP / 'EXP')
        names = zipfile.ZipFile(z).namelist()
        self.assertIn('live_orders.csv', names); self.assertIn('session/markets.csv', names); self.assertIn('REAL_RESULTS.txt', names)
        self.assertFalse(any(n.endswith('.pem') for n in names))
        self.assertFalse(ops.unexported_sessions(self.rt))
    def test_export_refuses_key_id(self):
        sess = self.make_session({'notes.txt': 'my id abcd1234-secret-id'})
        with self.assertRaises(RuntimeError):
            ops.export_session(self.rt, sess, key_id='abcd1234-secret-id', out_dir=_TMP / 'EXP2')
        self.assertFalse(list((_TMP / 'EXP2').glob('*.zip')))
    @unittest.skip('the ETH app never imports journals from other apps (TEST_ETH checks that it refuses)')
    def test_import_old_revision(self):
        old = _TMP / 'Revision 2' / 'PRIVATE_RUNTIME' / 'RE5_prod'; old.mkdir(parents=True, exist_ok=True)
        (old / 're5_journal.json').write_text(json.dumps({'schema': 'RE5-1'})); (old / 're5_live_events.jsonl').write_text('{}\n')
        found = ops.find_old_runtimes(_TMP)
        self.assertEqual([p.resolve() for p in found], [old.resolve()])
        (old / 'RUNNING.lock').write_text('x')
        with self.assertRaises(ops.LockedOldRuntime): ops.import_runtime(old)
        self.assertEqual(ops.import_runtime(old, old_window_closed=True), ['re5_journal.json', 're5_live_events.jsonl'])
        self.assertFalse((old / 'RUNNING.lock').exists()); self.assertTrue((old / 'RUNNING.lock.cleared_by_V12').exists())
        self.assertTrue((old / 'MOVED_TO_BIGBET_DATA.txt').exists())
        with self.assertRaises(RuntimeError): ops.import_runtime(old)
        shutil.rmtree(_TMP / 'Revision 2')
    def test_settings_store_path_not_key(self):
        keyfile = make_key()
        ops.save_settings({'key_id': 'abcd1234-secret-id', 'key_path': str(keyfile)})
        ok, msg = ops.apply_key_env()
        self.assertTrue(ok, msg); self.assertNotIn('abcd1234', msg)
        self.assertNotIn('PRIVATE', ops.SETTINGS_PATH.read_text())


class OpsEngineLock(OpsBase):
    def test_session_start_lock_lifecycle_with_stand_in_engine(self):
        stand_in = [sys.executable, '-c', 'import time; time.sleep(2)']     # instead of the real engine (no network)
        keyfile = make_key()
        ops.save_settings({'key_id': 'abcd1234-secret-id', 'key_path': str(keyfile)})
        sess = ops.start_session('paper', 15, live=False, engine_cmd=stand_in)
        rt = sess['rt']
        lock = ops.read_lock(rt)
        self.assertEqual(lock['pid'], sess['proc'].pid); self.assertFalse(lock['live'])
        self.assertEqual(ops.engine_state(rt)[0], 'not_responding')        # alive, no heartbeat yet
        with self.assertRaises(RuntimeError): ops.start_session('paper', 15, engine_cmd=stand_in)
        sess['proc'].wait(10)
        self.assertEqual(ops.engine_state(rt)[0], 'stopped')
        self.assertTrue((sess['session'] / 'APP' / 'ETH_V42_Collector.py').exists())
        self.assertIn('PAPER', sess['log'].read_text())


class FractionalFills(Base):
    """Nov 3 2026 08:28: a whole 10-lot YES order filled against resting orders with fractional
    sizes (book showed 49.80, 3676.10 ...). One fill was a fraction of a contract; RE5.2 halted."""
    TK = SER + '-26NOV030830-30'

    def fill(self, fid, oid, q, yes_p):
        return {'fill_id': fid, 'order_id': oid, 'ticker': self.TK, 'book_side': 'bid', 'action': 'buy', 'side': 'yes',
                'outcome_side': 'yes', 'count_fp': q, 'yes_price_dollars': yes_p, 'no_price_dollars': str(1 - D(yes_p)),
                'fee_cost': '0.000100', '_ts': self.clock()}

    def pending_state(self, oid, filled='10.00'):
        cid = str(uuid.uuid4()); s = self.state()
        s['pending'] = dict(client_id=cid, purpose='ENTRY', ticker=self.TK, held='yes', qty=10, yes_price='0.99', book_side='bid',
                            reduce_only=False, submitted_at=self.clock(), order_id=oid, close_time=self.clock() + 100, strike='77300')
        s['halted'] = True; s['reason'] = 'OPERATIONAL_ERROR SafetyError: fill accounting: fill count must be a positive whole number'
        self.c.j.save(s)
        self.ex.orders.append({'order_id': oid, 'client_order_id': cid, 'ticker': self.TK, 'status': 'executed',
                               'fill_count_fp': filled, 'remaining_count_fp': '0.00', '_ts': self.clock()})

    def test_replay_nov3_0830_fractional_fills_then_settled(self):
        oid = '01a1a19b-0000-7000-8000-000000000001'
        self.pending_state(oid)
        self.ex.fills += [self.fill('f1', oid, '0.20', '0.9860'), self.fill('f2', oid, '9.80', '0.9860')]
        self.ex.settlements.append({'ticker': self.TK, 'market_result': 'yes', 'yes_count_fp': '10.00', 'no_count_fp': '0.00',
                                    'revenue': 1000, 'fee_cost': '0.000200'})
        self.clock.t += 600
        st = self.make().recover()
        self.assertIsNone(st['pending']); self.assertIsNone(st['position'])
        h = st['history'][-1]
        self.assertEqual(h['qty'], 10); self.assertEqual(h['result'], 'yes')
        self.assertEqual(D(h['pnl']), D('10') - D('9.86') - D('0.0002'))
        self.assertEqual(st['stats']['recovered_orders'], 1)

    def test_fractional_total_position_exit_and_settle(self):
        oid = '01a1a19b-0000-7000-8000-000000000002'
        self.pending_state(oid, filled='9.90')
        self.ex.fills += [self.fill('f1', oid, '9.90', '0.9800')]
        self.ex.pos[self.TK] = ('yes', D('9.90'))
        self.ex.positions = lambda: [{'ticker': self.TK, 'position_fp': '9.90'}]
        st = self.make().recover()
        self.assertEqual(st['position']['qty'], '9.90')
        self.assertIn('9.90 open', self.make().status_line())
        self.ex.positions = lambda: []
        self.ex.settlements.append({'ticker': self.TK, 'market_result': 'no', 'yes_count_fp': '9.90', 'no_count_fp': '0.00',
                                    'revenue': 0, 'fee_cost': '0'})
        self.clock.t += 600
        c = self.make(); self.assertTrue(c.poll_settlement(force=True))
        h = c.j.read()['history'][-1]
        self.assertEqual(D(h['pnl']), -(D('9.9') * D('0.98') + D('0.0001')))

    def test_rejects_bad_fractions(self):
        from RE5.accounting import AccountingError, summarize_order_fills
        with self.assertRaises(AccountingError):
            summarize_order_fills([self.fill('x', 'o', '0.005', '0.98')], 'yes', 'increase')
        with self.assertRaises(AccountingError):
            summarize_order_fills([self.fill('x', 'o', '0', '0.98')], 'yes', 'increase')

    def test_exchange_count_format(self):
        from RE5.exchange import ExchangeClient, ApiFailure
        api = ExchangeClient.__new__(ExchangeClient); api.write_mode = 'demo'; sent = {}
        api._request = lambda m, e, params=None, payload=None: sent.update(payload) or {'order_id': 'o', 'fill_count': '0'}
        api.create_ioc(ticker=self.TK, client_order_id=str(uuid.uuid4()), book_side='ask', count=D('9.9'), yes_price='0.5', reduce_only=True)
        self.assertEqual(sent['count'], '9.90')
        with self.assertRaises(ApiFailure):
            api.create_ioc(ticker=self.TK, client_order_id=str(uuid.uuid4()), book_side='bid', count=D('9.9'), yes_price='0.98', reduce_only=False)
        api.create_ioc(ticker=self.TK, client_order_id=str(uuid.uuid4()), book_side='bid', count=10, yes_price='0.98', reduce_only=False)
        self.assertEqual(sent['count'], '10.00')



class CrumbFrontLevel(unittest.TestCase):
    """Nov 3 10:27:28: NO at 96.4c, lead 1.92x, but the front price level held < 1 contract."""
    def test_paper_candidate_not_rejected_for_crumb(self):
        sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
        import ETH_V42_Collector as m
        raw = dict(ticker=SER + '-26NOV031030-30', time_utc='t', seconds_left='150', side='no', eth='77145.30',
                   strike='77277.34', gap_abs='132.04', gap_signed='-132.04', sigma_min='1', scaled_gap='2.05',
                   minute_pairs='9', ask='0.964', quoted_depth='0.40')
        c = m.checkpoint_candidate(raw, 150)          # ETH V1.3 Rule B: 2.0x needed at 150 s (the original 1.92x now rejects)
        self.assertEqual(c['status'], 'SHADOW_CANDIDATE', c['reason']); self.assertEqual(c['contracts'], 10)
        self.assertLessEqual(D(c['total_cost']), D('10'))
        self.assertIn('SCALED_CUSHION_BELOW', m.checkpoint_candidate(dict(raw, scaled_gap='1.92'), 150)['reason'])
        c = m.checkpoint_candidate(dict(raw, ask='0.994'), 150)                      # ceiling unchanged
        self.assertIn('ASK_OUTSIDE_90_TO_99_CENTS', c['reason'])   # ETH V1.3 label (ceiling 99c unchanged)
        c = m.checkpoint_candidate(dict(raw, scaled_gap='1.3'), 150)                 # lead rule unchanged
        self.assertIn('SCALED_CUSHION_BELOW_CHECKPOINT_MINIMUM', c['reason'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
