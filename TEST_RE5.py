"""RE5 offline tests against a simulated Kalshi exchange (documented V2 shapes).

python TEST_RE5.py
"""
import json, sys, tempfile, unittest, uuid
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from RE5.asset import SERIES as SER  # noqa: E402  (KXETH15M)

from RE5.accounting import AccountingError, fill_effect, settlement_cash, summarize_order_fills
from RE5.controller import Controller, SafetyError
from RE5.exchange import (ApiFailure, ExchangeClient, OrderRejected, TradingLocked, UnknownOrderState,
                          fmt_price)

D = lambda x: Decimal(str(x))
T = SER + '-26NOV011915-15'


def fee(p, q):
    p = D(p); return (D('0.07') * q * p * (1 - p)).quantize(D('0.01'), rounding=ROUND_CEILING)


class Clock:
    def __init__(self): self.t = 1_800_000_000.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


class FakeKalshi:
    """Minimal exchange: IOC matching from a scripted fill queue, documented response shapes."""
    def __init__(self, clock, label='v2'):
        self.clock = clock; self.label = label
        self.pos = {}                      # ticker -> (held, qty)
        self.orders = []; self.fills = []; self.settlements = []
        self.balance = D('100'); self.fill_script = []; self.mode = 'ok'
        self.book = None; self.book_reads = 0; self.shard_balance = None
        self.fill_delay_calls = 0; self.posts = []; self.extra_positions = []

    # reads
    def balance_dollars(self): return self.balance
    def balance_on_shard(self, idx): return self.shard_balance.get(int(idx), D(0)) if self.shard_balance is not None else self.balance
    def positions(self):
        out = [{'ticker': t, 'position_fp': f'{(q if h == "yes" else -q)}.00'} for t, (h, q) in self.pos.items() if q]
        return out + self.extra_positions
    def orders_since(self, min_ts): return [o for o in self.orders if o['_ts'] >= min_ts]
    def fills_since(self, min_ts):
        if self.fill_delay_calls > 0:
            self.fill_delay_calls -= 1; return []
        return [f for f in self.fills if f['_ts'] >= min_ts]
    def settlements_since(self, min_ts): return list(self.settlements)
    def get_order(self, oid):
        for o in self.orders:
            if o['order_id'] == oid: return o
        raise ApiFailure('not found', 404)

    # write
    def set_book(self, yes_ask='0.98', no_ask='0.983', depth=1000):
        # Kalshi books hold bids only: a YES ask at p is a NO bid at 1-p.
        self.book = {'yes': [(1 - D(no_ask), D(depth))], 'no': [(1 - D(yes_ask), D(depth))]}

    def orderbook(self, ticker):
        self.book_reads += 1
        if self.book is None:
            raise ApiFailure('no book')
        return {k: list(v) for k, v in self.book.items()}

    def _best_ask(self, outcome):
        other = 'no' if outcome == 'yes' else 'yes'
        lv = self.book.get(other, []) if self.book else []
        return (1 - lv[0][0], lv[0][1]) if lv else (None, D(0))

    def create_ioc(self, *, ticker, client_order_id, book_side, count, yes_price, reduce_only):
        self.posts.append(dict(book_side=book_side, count=count, yes_price=D(yes_price), reduce_only=reduce_only))
        if self.mode == 'reject':
            raise OrderRejected('rejected', 400, 'invalid_price')
        if self.mode == 'timeout_before':
            raise UnknownOrderState('timeout, never arrived')
        q = self.fill_script.pop(0) if self.fill_script else count
        q = min(q, count)
        held, cur = self.pos.get(ticker, (None, 0))
        # outcome traded: bid = YES side up / NO side down; ask = YES down / NO up
        if reduce_only:
            q = min(q, cur)
        oid = 'ord-' + uuid.uuid4().hex[:8]
        yp = D(yes_price)
        if not reduce_only and self.book is not None:
            # buys fill at the BEST offer (price improvement), only if it is within the limit
            outcome = 'yes' if book_side == 'bid' else 'no'
            limit_outcome = yp if outcome == 'yes' else 1 - yp
            best, avail = self._best_ask(outcome)
            if best is None or best > limit_outcome:
                q = 0
            else:
                q = min(q, int(avail))
                yp = best if outcome == 'yes' else 1 - best
        order = {'order_id': oid, 'client_order_id': client_order_id, 'ticker': ticker, 'book_side': book_side,
                 'status': 'executed' if q else 'canceled', 'fill_count_fp': f'{q}.00', 'remaining_count_fp': '0.00',
                 'initial_count_fp': f'{count}.00', '_ts': self.clock()}
        self.orders.append(order)
        if q:
            # buyer of the outcome pays its price; record from the perspective of the order owner
            if book_side == 'bid':       # buys YES (or sells NO)
                if held == 'no':
                    self._fill(oid, ticker, 'no', 'sell', 1 - yp, q); self.pos[ticker] = ('no', cur - q)
                else:
                    self._fill(oid, ticker, 'yes', 'buy', yp, q); self.pos[ticker] = ('yes', cur + q)
            else:                        # sells YES (or buys NO)
                if held == 'yes':
                    self._fill(oid, ticker, 'yes', 'sell', yp, q); self.pos[ticker] = ('yes', cur - q)
                else:
                    self._fill(oid, ticker, 'no', 'buy', 1 - yp, q); self.pos[ticker] = ('no', cur + q)
        if self.mode == 'timeout_after':
            raise UnknownOrderState('timeout after execution')
        return {'order_id': oid, 'client_order_id': client_order_id, 'fill_count': f'{q}.00',
                'remaining_count': '0.00', 'ts_ms': int(self.clock() * 1000)}

    def _fill(self, oid, ticker, side, action, price, q):
        p = D(price)
        f = {'fill_id': 'f-' + uuid.uuid4().hex[:8], 'order_id': oid, 'ticker': ticker, 'count_fp': f'{q}.00',
             'fee_cost': str(fee(p, q)), '_ts': self.clock(), 'is_taker': True}
        yes_up = (side == 'yes') == (action == 'buy')
        if self.label == 'v2':                              # real Kalshi V2 labels (Nov 3 2026 fill)
            f.update(book_side='bid' if yes_up else 'ask', action='buy' if yes_up else 'sell',
                     side=side, outcome_side=side, market_ticker=ticker, exchange_index=2)
        elif self.label == 'yes_book' and side == 'no':     # legacy alternate labelling, no book_side
            f.update(side='yes', action='sell' if action == 'buy' else 'buy')
        else:                                               # legacy native labels, no book_side
            f.update(side=side, action=action)
        yes_p = p if side == 'yes' else 1 - p
        f.update(yes_price_dollars=str(yes_p), no_price_dollars=str(1 - yes_p))
        self.fills.append(f)

    def settle(self, ticker, result):
        held, q = self.pos.get(ticker, (None, 0))
        rec = {'ticker': ticker, 'market_result': result, 'yes_count_fp': f'{q if held == "yes" else 0}.00',
               'no_count_fp': f'{q if held == "no" else 0}.00', 'revenue': (q * 100 if result == held else 0),
               'fee_cost': '0.000000', 'settled_time': 'x'}
        self.settlements.append(rec); self.pos[ticker] = (held, 0)


def cand(side='yes', ask='0.98', n=10, depth=500):
    return {'ticker': T, 'side': side, 'ask': ask, 'contracts': n, 'quote_depth': str(depth), 'strike': '80000',
            'close_time': 1_800_000_000.0 + 180}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.clock = Clock(); self.ex = FakeKalshi(self.clock)
        self.logs = []; self.events = []
        self.ex.set_book()
        self.c = self.make()
    def tearDown(self): self.tmp.cleanup()
    def make(self):
        return Controller(self.ex, Path(self.tmp.name) / 'j.json', clock=self.clock, sleep=self.clock.sleep,
                          log=self.logs.append, event_log=self.events.append)
    def state(self): return self.c.j.read()


class Accounting(unittest.TestCase):
    def test_both_no_labels_same_cost(self):
        a = {'fill_id': '1', 'side': 'no', 'action': 'buy', 'count_fp': '10.00', 'yes_price_dollars': '0.03',
             'no_price_dollars': '0.97', 'fee_cost': '0.03'}
        b = dict(a, fill_id='2', side='yes', action='sell')
        self.assertEqual(summarize_order_fills([a], 'no', 'increase')['gross'], D('9.70'))
        self.assertEqual(summarize_order_fills([b], 'no', 'increase')['gross'], D('9.70'))
    def test_direction_mismatch_rejected(self):
        a = {'fill_id': '1', 'side': 'yes', 'action': 'sell', 'count_fp': '5', 'yes_price_dollars': '0.9',
             'no_price_dollars': '0.1', 'fee_cost': '0'}
        with self.assertRaises(AccountingError): summarize_order_fills([a], 'yes', 'increase')
    def test_bad_prices(self):
        with self.assertRaises(AccountingError):
            fill_effect({'side': 'yes', 'action': 'buy', 'count_fp': '1', 'yes_price_dollars': '0.9',
                         'no_price_dollars': '0.2', 'fee_cost': '0'}, 'yes')
    def test_settlement_checks(self):
        ok = {'market_result': 'yes', 'yes_count_fp': '6.00', 'no_count_fp': '0.00', 'revenue': 600, 'fee_cost': '0'}
        self.assertEqual(settlement_cash(ok, 'yes', 6)['net'], D('6'))
        with self.assertRaises(AccountingError): settlement_cash(ok, 'yes', 10)
        with self.assertRaises(AccountingError): settlement_cash(dict(ok, revenue=0), 'yes', 6)


class Entries(Base):
    def test_yes_full_fill_cost_from_fills(self):
        self.assertTrue(self.c.entry(cand()))
        p = self.state()['position']
        self.assertEqual(p['qty'], 10); self.assertEqual(D(p['entry_gross']), D('9.8')); self.assertEqual(D(p['entry_fees']), fee('0.98', 10))
        self.assertEqual(self.ex.posts[0]['book_side'], 'bid'); self.assertEqual(self.ex.posts[0]['yes_price'], D('0.99'))  # best ask .98 + 1c room
        self.assertIsNone(self.state()['pending'])
    def _no_entry(self, label):
        self.ex.label = label
        self.assertTrue(self.c.entry(cand('no', '0.983')))
        self.assertEqual(self.ex.posts[0]['book_side'], 'ask'); self.assertEqual(self.ex.posts[0]['yes_price'], D('0.01'))   # NO limit .99 == YES .01
        self.assertEqual(D(self.state()['position']['entry_gross']), D('9.83'))
    def test_no_entry_native_labels(self): self._no_entry('native')
    def test_no_entry_yes_book_labels(self): self._no_entry('yes_book')
    def test_partial_entry(self):
        self.ex.fill_script = [4]
        self.assertTrue(self.c.entry(cand())); self.assertEqual(self.state()['position']['qty'], 4)
    def test_zero_fill(self):
        self.ex.fill_script = [0]
        self.assertFalse(self.c.entry(cand())); s = self.state()
        self.assertIsNone(s['position']); self.assertNotIn(T, s['seen']); self.assertEqual(s['attempts'][T], 1); self.assertFalse(s['halted'])
    def test_reject_clears_pending(self):
        self.ex.mode = 'reject'
        self.assertFalse(self.c.entry(cand())); self.assertIsNone(self.state()['pending'])
    def test_timeout_after_execution_resolves_by_client_id(self):
        self.ex.mode = 'timeout_after'
        self.assertTrue(self.c.entry(cand())); s = self.state()
        self.assertEqual(s['position']['qty'], 10); self.assertIsNone(s['pending']); self.assertFalse(s['halted'])
    def test_timeout_never_arrived(self):
        self.ex.mode = 'timeout_before'
        self.assertFalse(self.c.entry(cand())); s = self.state()
        self.assertIsNone(s['pending']); self.assertIsNone(s['position'])
    def test_fills_eventual_consistency(self):
        self.ex.fill_delay_calls = 3
        self.assertTrue(self.c.entry(cand())); self.assertEqual(self.state()['position']['qty'], 10)
    def test_policy_skips_never_raise(self):
        self.assertFalse(self.c.entry(cand(ask='0.89')))          # ETH V1.3 Rule B: live range is now 0.90-0.99
        self.assertFalse(self.c.entry(cand(depth=0)))
        self.assertFalse(self.c.entry(dict(cand(), ticker='SENATE-X')))
        self.assertEqual(self.ex.posts, [])
    def test_depth_caps_quantity(self):
        # V12.2: a crumb at the front level (0.4) no longer blocks; the live book decides the size.
        self.assertTrue(self.c.entry(cand(depth='0.40'))); self.assertEqual(self.ex.posts[0]['count'], 10)
    def test_crumb_front_level_thin_book_buys_what_exists(self):
        self.ex.set_book(yes_ask='0.98', depth=3)
        self.assertTrue(self.c.entry(cand(depth='0.40'))); self.assertEqual(self.ex.posts[0]['count'], 3)
    def test_cap_fits_with_fee(self):
        self.ex.set_book(yes_ask='0.99')
        self.assertTrue(self.c.entry(cand(ask='0.99', n=10)))
        p = self.state()['position']; self.assertLessEqual(D(p['entry_gross']) + D(p['entry_fees']), D('10'))
    def test_one_position_and_no_reentry(self):
        self.assertTrue(self.c.entry(cand()))
        self.assertFalse(self.c.entry(cand()))
        self.assertEqual(len(self.ex.posts), 1)


class Exits(Base):
    def test_partial_exit_retries_until_flat_with_real_pnl(self):
        self.c.entry(cand())                             # 10 @ 0.98
        self.ex.fill_script = [3, 0, 7]
        self.ex.book['yes'] = [(D('0.85'), D(1000))]     # ETH V1.4: exits are priced from the live book; keep it equal to the bid
        for _ in range(3):
            self.c.on_poll(ticker=T, seconds_left=60, held_bid=D('0.85'), stop_reason='ETH_DISTANCE_MULTIPLE_LE_0P5')
        s = self.state(); self.assertIsNone(s['position'])
        h = s['history'][-1]
        sell_px = D('0.83'); cost = D('9.80') + fee('0.98', 10)
        expected = (3 * sell_px - fee(sell_px, 3)) + (7 * sell_px - fee(sell_px, 7)) - cost
        self.assertEqual(D(h['pnl']), expected.quantize(D('0.000001')))
        self.assertTrue(all(p['reduce_only'] for p in self.ex.posts[1:]))
        self.assertEqual(s['loss_count'], 1); self.assertTrue(s['halted'])     # daily loss rule
    def test_stop_is_sticky_after_trigger(self):
        self.c.entry(cand()); self.ex.fill_script = [5, 5]
        self.c.on_poll(ticker=T, seconds_left=60, held_bid=D('0.85'), stop_reason='X')
        self.c.on_poll(ticker=T, seconds_left=58, held_bid=D('0.86'), stop_reason=None)
        self.assertIsNone(self.state()['position'])
    def test_no_exit_last_5_seconds_then_settle_remainder(self):
        self.c.entry(cand('no')); self.ex.fill_script = [4]
        self.ex.book['no'] = [(D('0.80'), D(1000))]      # ETH V1.4: book matches the held bid
        self.c.on_poll(ticker=T, seconds_left=40, held_bid=D('0.80'), stop_reason='ETH_CROSSED_STRIKE')
        self.c.on_poll(ticker=T, seconds_left=4, held_bid=D('0.80'), stop_reason='ETH_CROSSED_STRIKE')
        self.assertEqual(len(self.ex.posts), 2)
        self.ex.settle(T, 'no'); self.clock.t += 400
        self.assertTrue(self.c.poll_settlement())
        h = self.state()['history'][-1]
        self.assertEqual(h['result'], 'no'); self.assertEqual(D(h['settlement_net']), D('6'))
        exit_net = 4 * D('0.78') - fee(D('0.78'), 4)
        self.assertEqual(D(h['pnl']), (exit_net + 6 - D('9.83') - fee('0.983', 10)).quantize(D('0.000001')))  # NO filled at book best .983
    def test_no_exit_price_on_yes_book(self):
        self.c.entry(cand('no')); self.ex.fill_script = [10]
        self.ex.book['no'] = [(D('0.86'), D(1000))]      # ETH V1.4: book matches the held bid
        self.c.on_poll(ticker=T, seconds_left=60, held_bid=D('0.86'), stop_reason='X')
        self.assertEqual(self.ex.posts[1]['book_side'], 'bid'); self.assertEqual(self.ex.posts[1]['yes_price'], D('0.16'))
    def test_flatten_request(self):
        self.c.entry(cand())
        self.c.on_poll(ticker=T, seconds_left=100, held_bid=D('0.97'), stop_reason=None, flatten_requested=True)
        self.assertIsNone(self.state()['position'])


class Settlement(Base):
    def test_win(self):
        self.c.entry(cand()); self.ex.settle(T, 'yes'); self.clock.t += 400
        self.assertTrue(self.c.poll_settlement()); s = self.state()
        self.assertEqual(D(s['history'][-1]['pnl']), D('10') - D('9.80') - fee('0.98', 10)); self.assertFalse(s['halted'])
    def test_loss_halts_day_and_rollover_clears(self):
        self.c.entry(cand()); self.ex.settle(T, 'no'); self.clock.t += 400
        self.c.poll_settlement(); s = self.state()
        self.assertTrue(s['halted']); self.assertTrue(s['reason'].startswith('DAILY_LOSS'))
        s['day'] = '2000-01-01'; self.c.j.save(s)
        self.c.recover(); self.assertFalse(self.state()['halted'])
    def test_settlement_count_mismatch_halts(self):
        self.c.entry(cand()); self.ex.settle(T, 'yes'); self.ex.settlements[-1]['yes_count_fp'] = '9.00'
        self.clock.t += 400
        with self.assertRaises(SafetyError): self.c.poll_settlement()
        self.assertTrue(self.state()['halted']); self.assertIsNotNone(self.state()['position'])
    def test_waits_before_close(self):
        self.c.entry(cand()); self.assertFalse(self.c.poll_settlement())


class Recovery(Base):
    def test_crash_after_post_before_journal_update(self):
        # simulate: intent saved, order executed on exchange, process died before resolving
        s = self.state(); cid = str(uuid.uuid4())
        s['pending'] = dict(client_id=cid, purpose='ENTRY', ticker=T, held='yes', qty=10, yes_price='0.98',
                            book_side='bid', reduce_only=False, submitted_at=self.clock(), order_id=None,
                            close_time=self.clock() + 180, strike='80000')
        self.c.j.save(s)
        self.ex.create_ioc(ticker=T, client_order_id=cid, book_side='bid', count=10, yes_price='0.98', reduce_only=False)
        c2 = self.make(); st = c2.recover()
        self.assertEqual(st['position']['qty'], 10); self.assertIsNone(st['pending']); self.assertFalse(st['halted'])
    def test_crash_before_post(self):
        s = self.state()
        s['pending'] = dict(client_id=str(uuid.uuid4()), purpose='ENTRY', ticker=T, held='yes', qty=10, yes_price='0.98',
                            book_side='bid', reduce_only=False, submitted_at=self.clock(), order_id=None)
        self.c.j.save(s)
        st = self.make().recover(); self.assertIsNone(st['pending']); self.assertIsNone(st['position'])
    def test_crash_with_position_then_settled_while_down(self):
        self.c.entry(cand()); self.ex.settle(T, 'yes'); self.clock.t += 900
        st = self.make().recover()
        self.assertIsNone(st['position']); self.assertEqual(st['history'][-1]['result'], 'yes')
    def test_unknown_eth_exposure_blocks(self):
        self.ex.extra_positions = [{'ticker': SER + '-OTHER', 'position_fp': '3.00'}]
        with self.assertRaises(SafetyError): self.make().recover()
        self.assertTrue(self.state()['halted'])
    def test_position_mismatch_blocks(self):
        self.c.entry(cand()); self.ex.pos[T] = ('yes', 6)
        with self.assertRaises(SafetyError): self.make().recover()
    def test_resting_order_blocks(self):
        self.ex.orders.append({'order_id': 'r1', 'ticker': T, 'status': 'resting', '_ts': self.clock()})
        with self.assertRaises(SafetyError): self.make().recover()
    def test_unrelated_markets_ignored(self):
        self.ex.extra_positions = [{'ticker': 'PRES-2028-X', 'position_fp': '5.00'}]
        self.make().recover()


class ExchangeClientShape(unittest.TestCase):
    class Resp:
        def __init__(self, code, body): self.status_code = code; self._b = body
        def json(self):
            if isinstance(self._b, Exception): raise self._b
            return self._b
    class Sess:
        def __init__(self, resp): self.resp = resp; self.calls = []
        def request(self, method, url, **kw):
            self.calls.append((method, url, kw))
            if isinstance(self.resp, Exception): raise self.resp
            return self.resp
    def client(self, resp, write_mode='demo', env='demo'):
        c = object.__new__(ExchangeClient); c.env = env; c.base = 'https://x'; c.write_mode = write_mode
        c.session = self.Sess(resp); c._headers = lambda m, p: {}
        return c
    def test_payload_and_top_level_response(self):
        c = self.client(self.Resp(201, {'order_id': 'o1', 'fill_count': '10.00', 'remaining_count': '0.00', 'ts_ms': 1}))
        r = c.create_ioc(ticker=T, client_order_id=str(uuid.uuid4()), book_side='ask', count=10, yes_price='0.017', reduce_only=False)
        self.assertEqual(r['order_id'], 'o1')
        body = c.session.calls[0][2]['json']
        self.assertEqual(body['time_in_force'], 'immediate_or_cancel'); self.assertEqual(body['price'], '0.0170')
        self.assertEqual(body['count'], '10.00'); self.assertEqual(body['self_trade_prevention_type'], 'taker_at_cross')
        self.assertTrue(c.session.calls[0][1].endswith('/trade-api/v2/portfolio/events/orders'))
    def test_write_lock(self):
        c = self.client(self.Resp(201, {}), write_mode='off')
        with self.assertRaises(TradingLocked):
            c.create_ioc(ticker=T, client_order_id=str(uuid.uuid4()), book_side='bid', count=1, yes_price='0.5', reduce_only=False)
    def test_prod_requires_arm(self):
        import os
        os.environ.pop('BIGBET_RE5_PROD_ARM', None)
        with self.assertRaises(TradingLocked): ExchangeClient(env='prod', write_mode='prod', key_id='k', key_path='nofile')
        with self.assertRaises(TradingLocked): ExchangeClient(env='demo', write_mode='prod', key_id='k', key_path='nofile')
    def test_unknown_on_500_and_transport(self):
        import requests
        with self.assertRaises(UnknownOrderState):
            self.client(self.Resp(500, {})).create_ioc(ticker=T, client_order_id=str(uuid.uuid4()), book_side='bid', count=1, yes_price='0.5', reduce_only=False)
        with self.assertRaises(UnknownOrderState):
            self.client(requests.ConnectionError()).create_ioc(ticker=T, client_order_id=str(uuid.uuid4()), book_side='bid', count=1, yes_price='0.5', reduce_only=False)
        with self.assertRaises(UnknownOrderState):
            self.client(self.Resp(409, {})).create_ioc(ticker=T, client_order_id=str(uuid.uuid4()), book_side='bid', count=1, yes_price='0.5', reduce_only=False)
    def test_rejection_is_definite(self):
        with self.assertRaises(OrderRejected):
            self.client(self.Resp(400, {'error': {'code': 'invalid_price'}})).create_ioc(ticker=T, client_order_id=str(uuid.uuid4()), book_side='bid', count=1, yes_price='0.5', reduce_only=False)
    def test_caps(self):
        c = self.client(self.Resp(201, {'order_id': 'o', 'fill_count': '0.00'}))
        for kw in (dict(count=12, yes_price='0.5'), dict(count=10, yes_price='0.9999'), dict(count=0, yes_price='0.5')):   # ETH V1.3: max 11 at $10
            with self.assertRaises(ApiFailure):
                c.create_ioc(ticker=T, client_order_id=str(uuid.uuid4()), book_side='bid', reduce_only=False, **kw)
        with self.assertRaises(ApiFailure):
            c.create_ioc(ticker='SENATE-X', client_order_id=str(uuid.uuid4()), book_side='bid', count=1, yes_price='0.5', reduce_only=False)
    def test_price_format_tenth_cent(self):
        self.assertEqual(fmt_price('0.983'), '0.9830')
        with self.assertRaises(ApiFailure): fmt_price('0.9835')


class SharedStopRule(unittest.TestCase):
    def test_collector_imports_and_rule(self):
        import importlib.util, time
        app = ROOT / 'RE5' / 'APP'
        sys.path.insert(0, str(app))
        spec = importlib.util.spec_from_file_location('re5_collector', app / 'ETH_V42_Collector.py')
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        now = time.time()
        # ETH V1.8: the V1.7 rule is still computed (old_reason) but only a 10-second cross (or a blind 50c) sells
        r, d = m.v7c_stop_reason('yes', '80000', D('79990'), now, 120, {'sigma_min': '30'}, None, D('0.97'), ticker='T1', now=now)
        self.assertIsNone(r); self.assertEqual(d['old_reason'], 'ETH_CROSSED_STRIKE')
        r, d = m.v7c_stop_reason('yes', '80000', D('79990'), now, 110, {'sigma_min': '30'}, None, D('0.97'), ticker='T1', now=now + 10)
        self.assertEqual(r, 'ETH_WRONG_SIDE_10S')
        r, d = m.v7c_stop_reason('yes', '80000', D('80010'), now, 120, {'sigma_min': '30'}, None, D('0.97'), ticker='T2', now=now)
        self.assertIsNone(r); self.assertEqual(d['old_reason'], 'ETH_DISTANCE_MULTIPLE_LE_0P5')
        r, d = m.v7c_stop_reason('yes', '80000', D('80035'), now, 120, {'sigma_min': '30'}, None, D('0.85'), ticker='T3', now=now)
        self.assertIsNone(r); self.assertEqual(d['old_reason'], 'PRICE_BACKUP_88C_ETH_LT_1X')
        r, d = m.v7c_stop_reason('yes', '80000', D('80100'), now, 120, {'sigma_min': '30'}, None, D('0.50'), ticker='T4', now=now)
        self.assertIsNone(r)                                           # fresh ETH on our side: 50c alone does not sell
        r, d = m.v7c_stop_reason('yes', '80000', D('80100'), now - 30, 120, {'sigma_min': '30'}, None, D('0.50'), ticker='T4b', now=now)
        self.assertEqual(r, 'PRICE_EMERGENCY_50C')                     # blind (stale ETH) and 50c: sell
        r, d = m.v7c_stop_reason('yes', '80000', D('79990'), now - 30, 120, {'sigma_min': '30'}, None, D('0.97'), ticker='T5', now=now + 60)
        self.assertIsNone(r)                                           # stale ETH never triggers ETH stops


class RE51Entry(Base):
    def test_book_moved_above_99_skips(self):
        self.ex.set_book(yes_ask='0.995')
        self.assertFalse(self.c.entry(cand())); self.assertEqual(self.ex.posts, [])
        self.assertIn('moved above', self.c.last_entry['reason'])
    def test_book_below_90_skips(self):                             # ETH V1.3 Rule B: floor is 0.90 (was 0.95)
        self.ex.set_book(yes_ask='0.89')
        self.assertFalse(self.c.entry(cand())); self.assertEqual(self.ex.posts, [])
    def test_book_unavailable_skips(self):
        self.ex.book = None
        self.assertFalse(self.c.entry(cand())); self.assertEqual(self.ex.posts, [])
    def test_room_fills_when_price_ticks_up(self):
        # signal said 95c, live book now 95.6c: +1c room still fills at the real best offer
        self.ex.set_book(yes_ask='0.956')
        self.assertTrue(self.c.entry(cand(ask='0.95')))
        self.assertEqual(self.ex.posts[0]['yes_price'], D('0.966'))
        self.assertEqual(D(self.state()['position']['entry_gross']), D('9.56'))
    def test_size_from_book_depth(self):
        self.ex.set_book(depth=4)
        self.assertTrue(self.c.entry(cand())); self.assertEqual(self.ex.posts[0]['count'], 4)
    def test_zero_fill_does_not_burn_market_and_attempt_cap(self):
        self.ex.fill_script = [0, 0, 0, 10]
        for i in range(3):
            self.assertFalse(self.c.entry(cand()))
            self.assertNotIn(T, self.state()['seen'])
        self.assertFalse(self.c.entry(cand()))                       # 4th attempt refused
        self.assertEqual(len(self.ex.posts), 3)
        self.assertIn('attempts', self.c.last_entry['reason'])
    def test_retry_after_zero_fill_can_fill(self):
        self.ex.fill_script = [0, 10]
        self.assertFalse(self.c.entry(cand())); self.assertTrue(self.c.entry(cand()))
        self.assertIn(T, self.state()['seen']); self.assertEqual(self.state()['stats']['no_fills'], 1)
    def test_order_diagnostics_logged(self):
        c = cand(); c['time_utc'] = '2027-01-15T08:00:00+00:00'
        self.c.entry(c)
        r = [e for e in self.events if e['event'] == 'ORDER_RESULT'][-1]
        for k in ('requested', 'filled', 'avg', 'latency_s', 'book_asks_top3', 'book_age_s', 'quote_age_s', 'limit_yes_price'):
            self.assertIn(k, r)
        self.assertEqual(r['book_asks_top3'], '0.98x1000')
    def test_last_entry_reports_real_cost(self):
        self.c.entry(cand())
        le = self.c.last_entry
        self.assertEqual(le['filled'], 10); self.assertEqual(le['gross'], D('9.80')); self.assertEqual(le['fees'], fee('0.98', 10))


class RE51Reporting(Base):
    def test_status_line_and_summary(self):
        from RE5.report import summary_text
        self.c.entry(cand()); self.ex.settle(T, 'yes'); self.clock.t += 400; self.c.poll_settlement()
        self.ex.fill_script = [0]
        c2 = dict(cand(), ticker=SER + '-26NOV012000-00'); self.c.entry(c2)
        line = self.c.status_line()
        self.assertIn('flat', line); self.assertIn('orders 2', line); self.assertIn('no-fills 1', line)
        rt = Path(self.tmp.name)
        (rt / 're5_journal.json').write_text((rt / 'j.json').read_text())
        with open(rt / 're5_live_events.jsonl', 'w') as f:
            for e in self.events: f.write(json.dumps(e) + '\n')
        text = summary_text(rt, since=0)
        self.assertIn('Entry orders: 2 | filled: 1 | no fill: 1', text)
        self.assertIn('+0.18', text)                                  # 10 - 9.80 - 0.02 fee
        self.assertIn('No-fill diagnostics', text)


class RE51PaperLedger(unittest.TestCase):
    def test_remove_unfilled_and_sync(self):
        import importlib.util, sqlite3
        tmp = tempfile.TemporaryDirectory(); app = ROOT / 'RE5' / 'APP'
        spec = importlib.util.spec_from_file_location('re5_risk_test', app / 'ETH_V42_Risk.py')
        r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
        r.DAILY_DB = Path(tmp.name) / 'daily.sqlite3'
        con = r.connect(Path(tmp.name) / 'paper.sqlite3')
        r.approve(con, T, 'yes', '9.54', data_age_seconds=1, market_seconds_left=150, contracts=10)
        r.remove_unfilled(con, T)
        self.assertEqual(r.outstanding(con), [])
        r.approve(con, T, 'yes', '9.54', data_age_seconds=1, market_seconds_left=120, contracts=10)  # retry allowed
        r.sync_to_live(con, T, 6, 583)
        self.assertEqual(r.outstanding(con), [(T, 'yes', 583, 6)])
        con.close(); tmp.cleanup()


REAL_FILL_NOV3 = {'action': 'sell', 'book_side': 'ask', 'count_fp': '10.00', 'created_time': '2026-11-03T11:42:28.197197Z',
                  'exchange_index': 2, 'fee_cost': '0.009000', 'fill_id': '072725e8-be39-84b5-d643-555a1b0266df',
                  'is_taker': True, 'market_ticker': SER + '-26NOV030645-45', 'no_price_dollars': '0.9870',
                  'order_id': '01a1a137-c0a0-7c9c-9247-33ae39319086', 'outcome_side': 'no', 'side': 'no',
                  'subaccount_number': 0, 'ticker': SER + '-26NOV030645-45', 'trade_id': '072725e8-be39-84b5-d643-555a1b0266df',
                  'ts': 1793706148, 'yes_price_dollars': '0.0130'}


class RE52RealKalshiRecords(unittest.TestCase):
    def test_real_nov3_no_buy_fill(self):
        s = summarize_order_fills([REAL_FILL_NOV3], 'no', 'increase')
        self.assertEqual(s['qty'], 10); self.assertEqual(s['gross'], D('9.870')); self.assertEqual(s['fees'], D('0.009'))
        self.assertEqual(s['avg_price'], D('0.987'))
    def test_real_fill_is_not_a_yes_increase(self):
        with self.assertRaises(AccountingError): summarize_order_fills([REAL_FILL_NOV3], 'yes', 'increase')
    def test_v2_exit_labels(self):
        sell_no = dict(REAL_FILL_NOV3, fill_id='x', book_side='bid', action='buy', yes_price_dollars='0.15', no_price_dollars='0.85')
        r = summarize_order_fills([sell_no], 'no', 'decrease')
        self.assertEqual(r['gross'], D('8.50'))


class RE52Recovery(Base):
    def test_replay_nov3_crash_then_settled(self):
        """The real Nov 3 sequence: order filled, old code could not read the fill and halted with
        `pending`; market then settled NO. RE5.2 recover() must finish it with the real P/L."""
        tk = SER + '-26NOV030645-45'
        s = self.state(); cid = str(uuid.uuid4())
        s['pending'] = dict(client_id=cid, purpose='ENTRY', ticker=tk, held='no', qty=10, yes_price='0.01', book_side='ask',
                            reduce_only=False, submitted_at=self.clock(), order_id='01a1a137-c0a0-7c9c-9247-33ae39319086',
                            close_time=self.clock() + 150, strike='77075.55')
        s['halted'] = True; s['reason'] = 'OPERATIONAL_ERROR SafetyError: fill accounting: fill direction does not match expected increase'
        self.c.j.save(s)
        self.ex.orders.append({'order_id': '01a1a137-c0a0-7c9c-9247-33ae39319086', 'client_order_id': cid, 'ticker': tk,
                               'status': 'executed', 'fill_count_fp': '10.00', 'remaining_count_fp': '0.00', '_ts': self.clock()})
        self.ex.fills.append(dict(REAL_FILL_NOV3, _ts=self.clock()))
        self.ex.settlements.append({'ticker': tk, 'market_result': 'no', 'yes_count_fp': '0.00', 'no_count_fp': '10.00',
                                    'revenue': 1000, 'fee_cost': '0.000000'})
        self.clock.t += 600
        st = self.make().recover()
        self.assertIsNone(st['pending']); self.assertIsNone(st['position'])
        h = st['history'][-1]
        self.assertEqual(h['result'], 'no'); self.assertEqual(D(h['pnl']), D('0.121'))   # 10.00 - 9.87 - 0.009


class RE52Shard(Base):
    def test_shard_balance_low_skips_no_order(self):
        self.ex.shard_balance = {0: D('35'), 2: D('0')}
        c = dict(cand(), exchange_index=2)
        self.assertFalse(self.c.entry(c)); self.assertEqual(self.ex.posts, [])
        self.assertIn('shard 2 balance', self.c.last_entry['reason'])
    def test_shard_balance_ok_trades(self):
        self.ex.shard_balance = {0: D('15'), 2: D('20')}
        self.assertTrue(self.c.entry(dict(cand(), exchange_index=2)))
    def test_insufficient_balance_rejection_pauses_entries(self):
        def rej(**kw):
            self.ex.posts.append(kw); raise OrderRejected('rejected', 400, 'insufficient_balance')
        self.ex.create_ioc = rej
        self.assertFalse(self.c.entry(cand()))
        self.assertTrue(self.state()['halted']); self.assertIn('insufficient_balance', self.state()['reason'])
        self.assertIn('rejected 400:insufficient_balance', self.c.last_entry['reason'])
        self.assertFalse(self.c.entry(dict(cand(), ticker=SER + '-26NOV012000-00')))
        self.assertEqual(len(self.ex.posts), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
