"""RE5 live controller: one position at a time, $10 cap, IOC orders only.

Lifecycle of every order
  1. intent written to the journal as `pending` (with client_order_id)   <- crash-safe point
  2. IOC POST
  3. resolve: GET order (terminal status) + fills for that order (must sum to fill_count)
  4. apply real fills to `position`, clear `pending`
Any step that cannot be completed leaves `pending` in the journal; `recover()` finishes it
on the next start by looking the order up by client_order_id. Nothing is ever guessed.

Exits: once a stop fires, the controller keeps sending reduce-only IOC orders every poll at
(current bid - EXIT_CROSS) until the position is flat or the market is within 5 s of close.
Contracts still held at close are settled from the exchange's settlement record.
"""
import csv, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .accounting import AccountingError, D, fee_ceiling, qjson, qnorm, settlement_cash, summarize_order_fills
from .exchange import (ApiFailure, MAX_CONTRACTS, MAX_NOTIONAL, OrderRejected, TradingLocked,
                       UnknownOrderState, MARKET_PREFIX, TRADE_USD)
from . import scaling
from .asset import SERIES as _SERIES
from .journal import Journal, utcday

ENTRY_MIN_PRICE = Decimal('0.90')   # ETH V1.3 Rule B (was 0.95)
ENTRY_MAX_PRICE = Decimal('0.99')
EXIT_CROSS = Decimal('0.02')           # sell limit = bid - 2c (IOC still fills at best available)
ENTRY_ROOM = Decimal('0.01')           # RE5.1: buy limit = live best ask + 1c (capped at 99c)
MAX_ENTRY_ATTEMPTS_PER_MARKET = 3      # RE5.1: a zero-fill no longer burns the market
STATUS_EVERY_SECONDS = 300
EXIT_MIN_SECONDS = 5                   # no new exit orders in the last 5 s (paper rule)
MAX_EXIT_ORDERS_PER_MARKET = 25
DAILY_MAX_LOSSES = 1                   # one losing trade halts entries for the UTC day
DAILY_MAX_LOSS_USD = scaling.daily_loss_cap(TRADE_USD)   # V12.3: half the trade size ($10 -> $5.00)


def daily_limit_hit(s):
    """ETH V1.6: daily loss rule counted from the operator's last override (if any) of this UTC day.
    Without an override this is exactly the V1.5 rule."""
    losses = int(s['loss_count']) - int(s.get('ovr_losses', 0) or 0)
    realized = D(s['realized_usd']) - D(str(s.get('ovr_realized', '0') or '0'))
    return losses >= DAILY_MAX_LOSSES or -realized >= DAILY_MAX_LOSS_USD


def exit_basis(bids, qty):
    """ETH V1.4: price level in the FRESH book where the visible bids cover `qty` contracts
    (or the deepest visible level). None when the book shows no bids."""
    need = D(qty); last = None
    for price, size in bids or []:
        last = D(price); need -= D(size)
        if need <= 0:
            break
    return last
SETTLEMENT_POLL_SECONDS = 15
SETTLE_LAG_GRACE_SECONDS = 45   # V12.4: Kalshi can post the payout a few seconds before its positions list updates
RESOLVE_WAIT_SECONDS = 12              # how long to wait for order/fill records to appear


class SafetyError(RuntimeError):
    pass


# Revision 3: one row per real order (entries and exits), kept next to the journal for all sessions.
ORDER_FIELDS = ['time_utc', 'purpose', 'result', 'ticker', 'held', 'checkpoint_s', 'seconds_left', 'signal_ask',
                'best_ask', 'limit_held', 'requested', 'filled', 'avg_fill', 'slip_vs_signal_c', 'slip_vs_best_c',
                'fees', 'cost_or_proceeds', 'latency_s', 'quote_age_s', 'book_age_s', 'book_asks_top3',
                'book_bids_top3', 'shard_balance', 'reject_code', 'exit_reason', 'order_id', 'client_id', 'recovered']


def _q(x):
    return str(D(x).quantize(Decimal('0.000001')))


def _open(p):
    return qnorm(D(p['qty']) - D(p['exited_qty']))


class Controller:
    def __init__(self, api, journal_path, *, clock=None, sleep=None, log=print, event_log=None, orders_csv='auto'):
        self.api = api
        self.j = Journal(journal_path)
        self.orders_csv = Path(journal_path).parent / 'live_orders.csv' if orders_csv == 'auto' else orders_csv
        self.clock = clock or (lambda: time.time())
        self.sleep = sleep or (lambda s: time.sleep(s))
        self.log = log
        self.event_log = event_log          # callable(dict) for CSV audit, optional
        self.started = False
        self._last_settlement_poll = 0.0
        self._settle_lag = {}            # V12.4: ticker -> time we first saw 'settled but position still listed'
        self.last_entry = {}
        self.trade_usd = TRADE_USD
        self._advised = set()

    # ================================================================ helpers
    def _event(self, kind, **data):
        row = dict(time=round(self.clock(), 3), event=kind, **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in data.items()})
        self.log('RE5 ' + kind + ' ' + ' '.join(f'{k}={v}' for k, v in row.items() if k not in ('time', 'event')))
        if self.event_log:
            try:
                self.event_log(row)
            except Exception:
                pass

    def _bump(self, **inc):
        s = self.j.read(); st = s.setdefault('stats', {})
        for k, v in inc.items():
            st[k] = st.get(k, 0) + v
        self.j.save(s)

    def _order_row(self, pending, *, result, summary=None, code='', recovered=False):
        """Append one row to live_orders.csv. Never raises (logging must not affect trading)."""
        if not self.orders_csv:
            return
        try:
            filled = qjson(summary['qty']) if summary else 0
            avg = summary.get('avg_price') if summary else None
            fees = summary.get('fees') if summary else None
            gross = summary.get('gross') if summary else None
            def cents(a, b):
                try:
                    if a is None or b in (None, ''):
                        return ''
                    d = (D(a) - D(b)) * 100
                    return str(d.quantize(Decimal('0.01')) if pending['purpose'] == 'ENTRY' else (-d).quantize(Decimal('0.01')))
                except Exception:
                    return ''
            flow = ''
            if gross is not None and filled:
                flow = str((D(gross) + D(fees or 0)) if pending['purpose'] == 'ENTRY' else (D(gross) - D(fees or 0)))
            row = dict(time_utc=datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec='seconds'),
                       purpose=pending['purpose'], result=result, ticker=pending['ticker'], held=pending['held'],
                       checkpoint_s=pending.get('checkpoint_s', ''), seconds_left=pending.get('seconds_left', ''),
                       signal_ask=pending.get('signal_ask', ''), best_ask=pending.get('best_ask', ''),
                       limit_held=pending.get('limit_held', ''), requested=pending['qty'], filled=filled,
                       avg_fill='' if avg is None else str(avg), slip_vs_signal_c=cents(avg, pending.get('signal_ask')),
                       slip_vs_best_c=cents(avg, pending.get('best_ask') if pending['purpose'] == 'ENTRY' else pending.get('held_bid')),
                       fees='' if fees is None else str(fees), cost_or_proceeds=flow,
                       latency_s=round(self.clock() - float(pending['submitted_at']), 3),
                       quote_age_s=pending.get('quote_age_s', ''), book_age_s=pending.get('book_age_s', ''),
                       book_asks_top3=pending.get('book_asks_top3', ''), book_bids_top3=pending.get('book_bids_top3', ''),
                       shard_balance=pending.get('shard_balance', ''), reject_code=code or '',
                       exit_reason=pending.get('exit_reason', ''), order_id=(summary or {}).get('order_id') or pending.get('order_id') or '',
                       client_id=pending['client_id'], recovered='yes' if recovered else '')
            path = Path(self.orders_csv)
            new = not path.exists() or path.stat().st_size == 0
            with path.open('a', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=ORDER_FIELDS, extrasaction='ignore')
                if new:
                    w.writeheader()
                w.writerow(row)
        except Exception:
            pass

    def _book_snapshot(self, ticker, held):
        """Live book for diagnostics/pricing: asks to BUY held (from opposite bids), bids to SELL held."""
        try:
            book = self.api.orderbook(ticker)
        except Exception as e:
            return None, f'book_unavailable:{type(e).__name__}'
        other = 'no' if held == 'yes' else 'yes'
        asks = sorted(((1 - p, q) for p, q in book.get(other, [])), key=lambda x: x[0])
        bids = list(book.get(held, []))
        return dict(asks=asks, bids=bids, read_at=self.clock()), None

    @staticmethod
    def _top3(levels):
        return ';'.join(f'{p}x{q}' for p, q in levels[:3])

    def _eth_exposure(self):
        """Exchange-side positions in this folder's series {ticker: abs_qty} and resting orders."""
        pos = {}
        for p in self.api.positions():
            t = str(p.get('ticker') or p.get('market_ticker') or '')
            if not t.startswith(MARKET_PREFIX):
                continue
            v = p.get('position_fp', p.get('position'))
            if v is None:
                raise SafetyError('exchange position quantity missing')
            if D(v) != 0:
                pos[t] = abs(D(v))
        return pos

    def _resting_eth_orders(self, since):
        out = []
        for o in self.api.orders_since(since):
            t = str(o.get('ticker', ''))
            if t.startswith(MARKET_PREFIX) and str(o.get('status', '')).lower() == 'resting':
                out.append(o)
        return out

    def _roll_day(self, s):
        if s['day'] == utcday():
            return s
        if s['pending']:
            raise SafetyError('UTC rollover with unresolved order; run RE5_RECONCILE.py')
        loss_halt = s['halted'] and s['reason'].startswith('DAILY_LOSS')
        s.update(day=utcday(), loss_count=0, realized_usd='0', seen=[], attempts={}, stats={})
        for k in ('ovr_losses', 'ovr_realized', 'ovr_day'):          # ETH V1.6: an operator override lasts one day
            s.pop(k, None)
        if loss_halt:
            s.update(halted=False, reason='')
        self.j.save(s)
        self._event('DAY_ROLLOVER', day=s['day'])
        return s

    # ================================================================ order resolution
    def _find_order_by_client_id(self, client_id, since):
        for o in self.api.orders_since(since):
            if o.get('client_order_id') == client_id:
                return o
        return None

    def _resolve(self, pending, order_id=None):
        """Resolve a pending order to real fills. Returns summary dict or raises SafetyError.

        Returns dict(qty=0, ...) for orders that definitely did not execute (or never existed).
        """
        held = pending['held']; expect = 'increase' if pending['purpose'] == 'ENTRY' else 'decrease'
        since = int(pending['submitted_at']) - 120
        deadline = self.clock() + RESOLVE_WAIT_SECONDS
        order = None
        while True:
            oid = order_id or pending.get('order_id')
            try:
                if oid:
                    order = self.api.get_order(oid)
                else:
                    order = self._find_order_by_client_id(pending['client_id'], since)
            except ApiFailure as e:
                if getattr(e, 'status', None) == 404:
                    order = None
                else:
                    raise
            if order is not None and str(order.get('status', '')).lower() in ('canceled', 'cancelled', 'executed'):
                break
            if order is not None and str(order.get('status', '')).lower() == 'resting':
                raise SafetyError('IOC order is resting on the book; cancel it on Kalshi, then reconcile')
            if self.clock() >= deadline:
                if order is None and not oid:
                    return None          # never seen by the exchange (caller decides using positions)
                raise SafetyError('order did not reach a terminal state in time')
            self.sleep(1.0)
        if order.get('client_order_id') not in (None, '', pending['client_id']):
            raise SafetyError('order client id mismatch')
        if str(order.get('ticker', pending['ticker'])) != pending['ticker']:
            raise SafetyError('order ticker mismatch')
        filled = D(order.get('fill_count_fp', 'nan'))
        if not filled.is_finite() or filled < 0 or filled != filled.quantize(Decimal('0.01')) or filled > D(pending['qty']):
            raise SafetyError('invalid fill_count_fp')
        if filled == 0:
            return dict(qty=0, gross=Decimal(0), fees=Decimal(0), avg_price=None, order_id=order.get('order_id'))
        oid = order['order_id']
        while True:
            fills = [f for f in self.api.fills_since(since) if f.get('order_id') == oid]
            got = sum((D(f.get('count_fp', f.get('count', 0))) for f in fills), Decimal(0))
            if got == filled:
                break
            if got > filled:
                raise SafetyError('fills exceed order fill count')
            if self.clock() >= deadline:
                raise SafetyError('fill records incomplete (eventual consistency timeout)')
            self.sleep(1.0)
        try:
            summary = summarize_order_fills(fills, held, expect)
        except AccountingError as e:
            raise SafetyError('fill accounting: ' + str(e)) from None
        summary['order_id'] = oid
        return summary

    def _submit(self, purpose, ticker, held, qty, yes_price, book_side, reduce_only, extra=None):
        """Persist intent, POST IOC, resolve. Returns fill summary. Never retries the write."""
        s = self.j.read()
        if s['pending']:
            raise SafetyError('another order is unresolved')
        cid = str(uuid.uuid4())
        pending = dict(client_id=cid, purpose=purpose, ticker=ticker, held=held, qty=qjson(qty),
                       yes_price=str(yes_price), book_side=book_side, reduce_only=bool(reduce_only),
                       submitted_at=self.clock(), order_id=None, **(extra or {}))
        s['pending'] = pending
        self.j.save(s)                                   # <- crash-safe point BEFORE the write
        try:
            resp = self.api.create_ioc(ticker=ticker, client_order_id=cid, book_side=book_side,
                                       count=qnorm(qty), yes_price=yes_price, reduce_only=reduce_only)
        except OrderRejected as e:
            # Definite rejection: nothing executed. Clear pending.
            s = self.j.read(); s['pending'] = None; self.j.save(s)
            self._bump(orders=1, rejects=1)
            self._event('ORDER_REJECTED', purpose=purpose, ticker=ticker, status=e.status, code=e.code)
            self._order_row(pending, result='REJECTED', code=f'{e.status}:{e.code}')
            if purpose == 'ENTRY' and str(e.code) in ('insufficient_balance', 'insufficient_funds'):
                self.j.halt(f'ENTRY_REJECTED_{e.code}: add/move cash to the market shard, then RE5_RECONCILE.py --clear-halt')
            return dict(qty=0, gross=Decimal(0), fees=Decimal(0), avg_price=None, order_id=None,
                        rejected=f'{e.status}:{e.code}')
        except (UnknownOrderState, TradingLocked) as e:
            if isinstance(e, TradingLocked):
                s = self.j.read(); s['pending'] = None; self.j.save(s); raise
            self._event('ORDER_OUTCOME_UNKNOWN', purpose=purpose, ticker=ticker, detail=str(e))
            summary = self._resolve(pending)            # look up by client_order_id
            if summary is None:
                # Not visible on the exchange after the wait: confirm the position did not change.
                self._confirm_unchanged_or_halt(pending)
                s = self.j.read(); s['pending'] = None; self.j.save(s)
                self._bump(orders=1, unknown_never_arrived=1)
                self._order_row(pending, result='NOT_FOUND_ON_EXCHANGE')
                return dict(qty=0, gross=Decimal(0), fees=Decimal(0), avg_price=None, order_id=None)
            self._apply(pending, summary)
            self._order_result(pending, summary)
            return summary
        oid = resp['order_id']
        s = self.j.read(); s['pending']['order_id'] = oid; self.j.save(s)
        if D(resp.get('remaining_count', '0')) != 0 and D(resp.get('fill_count')) + D(resp.get('remaining_count')) != D(qty):
            raise SafetyError('IOC response counts inconsistent')
        summary = self._resolve(dict(pending, order_id=oid), order_id=oid)
        if summary is None or D(summary['qty']) != D(resp['fill_count']):
            raise SafetyError('create response fill_count differs from order/fill records')
        self._apply(pending, summary)
        self._order_result(pending, summary)
        return summary

    def _order_result(self, pending, summary, recovered=False):
        filled = qnorm(summary['qty'])
        self._bump(orders=1, fills=1 if filled else 0, no_fills=0 if filled else 1, contracts_filled=float(filled),
                   recovered_orders=1 if recovered else 0)
        self._order_row(pending, summary=summary, recovered=recovered,
                        result='FILLED' if filled == D(pending['qty']) else ('PARTIAL' if filled else 'NO_FILL'))
        self._event('ORDER_RESULT', purpose=pending['purpose'], ticker=pending['ticker'], held=pending['held'],
                    requested=pending['qty'], filled=qjson(filled), avg=summary['avg_price'], fees=summary['fees'],
                    limit_yes_price=pending['yes_price'], book_side=pending['book_side'],
                    latency_s=round(self.clock() - pending['submitted_at'], 3), order_id=summary.get('order_id'),
                    quote_age_s=pending.get('quote_age_s'), book_age_s=pending.get('book_age_s'),
                    book_asks_top3=pending.get('book_asks_top3'), book_bids_top3=pending.get('book_bids_top3'))

    def _confirm_unchanged_or_halt(self, pending):
        expo = self._eth_exposure()
        s = self.j.read()
        expected = 0
        if s['position'] and s['position']['ticker'] == pending['ticker']:
            expected = _open(s['position'])
        if D(expo.get(pending['ticker'], 0)) != D(expected):
            self.j.halt('UNRESOLVED_ORDER: exchange position differs from journal and order not found')
            raise SafetyError('order not found but exchange position changed; manual reconciliation')

    def _apply(self, pending, summary):
        """Apply a resolved order to the journal (idempotent per client id)."""
        s = self.j.read()
        if not s['pending'] or s['pending']['client_id'] != pending['client_id']:
            raise SafetyError('pending order changed while resolving')
        if pending['purpose'] == 'ENTRY':
            if summary['qty'] > 0:
                if s['position']:
                    raise SafetyError('entry fill while another position is open')
                s['position'] = dict(ticker=pending['ticker'], held=pending['held'], qty=qjson(summary['qty']),
                                     entry_gross=_q(summary['gross']), entry_fees=_q(summary['fees']),
                                     entry_avg=_q(summary['avg_price']), exited_qty=0, exit_gross='0',
                                     exit_fees='0', exit_orders=0, exiting=False, exit_reason='',
                                     close_time=pending.get('close_time'), strike=pending.get('strike'),
                                     opened_at=pending['submitted_at'], entry_order=summary['order_id'])
                self._event('ENTRY_FILLED', ticker=pending['ticker'], held=pending['held'], qty=qjson(summary['qty']),
                            avg=summary['avg_price'], fees=summary['fees'], requested=pending['qty'])
            else:
                self._event('ENTRY_NO_FILL', ticker=pending['ticker'], held=pending['held'])
        else:
            p = s['position']
            if not p or p['ticker'] != pending['ticker']:
                raise SafetyError('exit fill without matching position')
            if D(summary['qty']) > _open(p):
                raise SafetyError('exit filled more than the open quantity')
            p['exited_qty'] = qjson(D(p['exited_qty']) + D(summary['qty']))
            p['exit_gross'] = _q(D(p['exit_gross']) + summary['gross'])
            p['exit_fees'] = _q(D(p['exit_fees']) + summary['fees'])
            p['exit_orders'] += 1
            self._event('EXIT_FILL', ticker=p['ticker'], qty=qjson(summary['qty']), avg=summary['avg_price'],
                        remaining=qjson(_open(p)), fees=summary['fees'])
        s['pending'] = None
        self.j.save(s)
        s = self.j.read()
        if s['position'] and _open(s['position']) == 0:
            self._close_position(settle=None)

    # ================================================================ P/L finalisation
    def _close_position(self, settle):
        s = self.j.read(); p = s['position']
        cost = D(p['entry_gross']) + D(p['entry_fees'])
        exit_net = D(p['exit_gross']) - D(p['exit_fees'])
        settle_net = settle['net'] if settle else Decimal(0)
        pnl = exit_net + settle_net - cost
        rec = dict(ticker=p['ticker'], held=p['held'], qty=p['qty'], cost=_q(cost), exit_net=_q(exit_net),
                   exited_qty=p['exited_qty'], settlement_net=_q(settle_net),
                   result=(settle or {}).get('result', 'FULLY_EXITED'), pnl=_q(pnl), closed_at=self.clock(),
                   closed_day=s['day'], entry_avg=p['entry_avg'], exit_reason=p.get('exit_reason', ''))
        s['history'] = (s['history'] + [rec])[-50:]
        s['position'] = None
        s['realized_usd'] = _q(D(s['realized_usd']) + pnl)
        if pnl < 0:
            s['loss_count'] += 1
        if daily_limit_hit(s):
            s['halted'] = True
            s['reason'] = f'DAILY_LOSS_LIMIT losses={s["loss_count"]} realized={s["realized_usd"]}'
        self.j.save(s)
        self._event('POSITION_CLOSED', **rec)
        scaling.record_close(Path(self.j.path).parent, rec, self.trade_usd, s['history'][:-1])   # V12.3 ladder / weekly ledger

    # ================================================================ startup / crash reconciliation
    def recover(self):
        """Bring the journal in line with the exchange. Safe to call any time no order is in flight."""
        s = self._roll_day(self.j.read())
        now = self.clock()
        if s['pending']:
            pending = s['pending']
            self._event('RECOVER_PENDING', purpose=pending['purpose'], ticker=pending['ticker'], client_id=pending['client_id'])
            summary = self._resolve(pending)
            if summary is None:
                self._confirm_unchanged_or_halt(pending)
                s = self.j.read(); s['pending'] = None; self.j.save(s)
                self._event('RECOVER_PENDING_NEVER_SENT', ticker=pending['ticker'])
            else:
                self._apply(pending, summary)
                self._order_result(pending, summary, recovered=True)   # Revision 3: counted in status
            s = self.j.read()
        resting = self._resting_eth_orders(now - 3 * 86400)
        if resting:
            self.j.halt('RESTING_ETH_ORDER on exchange')
            raise SafetyError(f'resting {_SERIES} order exists on Kalshi; cancel it manually')
        expo = self._eth_exposure()
        p = s['position']
        if p:
            open_qty = _open(p)
            others = {t: q for t, q in expo.items() if t != p['ticker']}
            if others:
                self.j.halt('UNKNOWN_ETH_EXPOSURE ' + ','.join(others))
                raise SafetyError(f'exchange holds {_SERIES} positions the journal does not know')
            exch = expo.get(p['ticker'], Decimal(0))
            if exch == open_qty:
                pass
            elif exch == 0 and self._settle_if_available(force=True):
                pass
            elif exch == 0 and p.get('close_time') and now > float(p['close_time']):
                self._event('RECOVER_AWAITING_SETTLEMENT', ticker=p['ticker'])
            else:
                self.j.halt(f'POSITION_MISMATCH journal={open_qty} exchange={exch}')
                raise SafetyError('journal position does not match exchange')
        elif expo:
            self.j.halt('UNKNOWN_ETH_EXPOSURE ' + ','.join(expo))
            raise SafetyError(f'exchange holds {_SERIES} exposure but journal is flat')
        self.started = True
        s = self.j.read()
        self._event('RECOVER_OK', halted=s['halted'], position=(s['position'] or {}).get('ticker'), day=s['day'])
        return s

    # ================================================================ entries
    def entry(self, cand):
        """Try one live entry from a paper-approved V7-D candidate. Returns True if any contracts filled.

        Policy skips return False (session continues). State problems raise SafetyError.
        """
        if not self.started:
            self.recover()
        s = self._roll_day(self.j.read())
        why = None
        if s['halted']:
            why = 'halted:' + s['reason']
        elif s['pending']:
            why = 'pending order'
        elif s['position']:
            why = 'position open'
        elif daily_limit_hit(s):
            why = 'daily loss limit'
        if why is None:
            why = self._scaling_gate(cand, s)
        ticker = str(cand.get('ticker', ''))
        held = str(cand.get('side', ''))
        try:
            ask = D(cand['ask']); qty = int(cand['contracts']); depth = D(cand.get('quote_depth') or 0)
        except Exception:
            why = why or 'malformed candidate'
            ask = Decimal(0); qty = 0; depth = Decimal(0)
        if why is None:
            if not ticker.startswith(MARKET_PREFIX) or held not in ('yes', 'no'):
                why = 'bad ticker/side'
            elif ticker in s['seen']:
                why = 'market already traded today'
            elif s.get('attempts', {}).get(ticker, 0) >= MAX_ENTRY_ATTEMPTS_PER_MARKET:
                why = f'{MAX_ENTRY_ATTEMPTS_PER_MARKET} entry attempts already made in this market'
            elif not ENTRY_MIN_PRICE <= ask <= ENTRY_MAX_PRICE or ask != ask.quantize(Decimal('0.001')):
                why = f'ask {ask} outside live range'
            elif not 1 <= qty <= MAX_CONTRACTS:
                why = f'qty {qty} outside 1..{MAX_CONTRACTS}'
            elif depth <= 0:
                why = 'no displayed depth'
            # RE5.3 (V12.2): the summary only shows the FRONT price level, which can be a fraction of a
            # contract (e.g. 0.40). Size is decided below from the full live book up to the limit.
        book = None; limit = None; worst = Decimal(0)
        if why is None:
            # RE5.1: price and size from the LIVE order book read right now, not the market summary.
            book, err = self._book_snapshot(ticker, held)
            if book is None:
                why = err
            elif not book['asks']:
                why = 'book: no offers'
            else:
                best = book['asks'][0][0]
                if best > ENTRY_MAX_PRICE:
                    why = f'book: best offer {best} moved above {ENTRY_MAX_PRICE}'
                elif best < ENTRY_MIN_PRICE:
                    why = f'book: best offer {best} fell below {ENTRY_MIN_PRICE}'
                else:
                    limit = min(ENTRY_MAX_PRICE, best + ENTRY_ROOM)
                    available = sum((q for pr, q in book['asks'] if pr <= limit), Decimal(0))
                    qty = min(qty, int(available))
                    while qty > 0 and limit * qty + fee_ceiling(limit, qty) > MAX_NOTIONAL:
                        qty -= 1
                    if qty < 1:
                        why = f'book: no contracts available within limit and ${MAX_NOTIONAL} cap'
                    worst = limit * qty + fee_ceiling(limit, qty)
        bal = None
        if why is None:
            shard = cand.get('exchange_index')
            if shard not in (None, ''):
                bal = self.api.balance_on_shard(int(shard))
                if bal < worst:
                    why = f'shard {shard} balance ${bal} below ${worst}: move cash to shard {shard} on Kalshi'
            else:
                bal = self.api.balance_dollars()
                if bal < worst:
                    why = f'balance {bal} below {worst}'
        if why is None:
            expo = self._eth_exposure()
            if expo:
                self.j.halt('UNEXPECTED_ETH_EXPOSURE before entry')
                raise SafetyError(f'exchange shows {_SERIES} exposure before entry')
        if why is not None:
            self.last_entry = dict(filled=0, reason=why)
            self._event('ENTRY_SKIPPED', ticker=ticker, reason=why)
            return False
        s = self.j.read(); att = s.setdefault('attempts', {}); att[ticker] = att.get(ticker, 0) + 1
        s['attempts'] = dict(list(att.items())[-200:]); self.j.save(s)
        yes_price = limit if held == 'yes' else (1 - limit)
        book_side = 'bid' if held == 'yes' else 'ask'
        quote_age = None
        try:
            from datetime import datetime
            quote_age = round(self.clock() - datetime.fromisoformat(str(cand['time_utc']).replace('Z', '+00:00')).timestamp(), 3)
        except Exception:
            pass
        diag = dict(quote_age_s=quote_age, book_age_s=round(self.clock() - book['read_at'], 3),
                    book_asks_top3=self._top3(book['asks']), book_bids_top3=self._top3(book['bids']),
                    signal_ask=str(ask), best_ask=str(book['asks'][0][0]), limit_held=str(limit),
                    shard_balance='' if bal is None else str(bal), checkpoint_s=cand.get('checkpoint_s', ''),
                    seconds_left=cand.get('actual_seconds_left', ''))
        self._event('ENTRY_SUBMIT', ticker=ticker, held=held, qty=qty, signal_ask=ask, best_ask=book['asks'][0][0],
                    yes_price=yes_price, book_side=book_side, attempt=s['attempts'][ticker],
                    **{k: v for k, v in diag.items() if k not in ('signal_ask', 'best_ask')})
        summary = self._submit('ENTRY', ticker, held, qty, yes_price, book_side, False,
                               extra=dict(close_time=cand.get('close_time'), strike=str(cand.get('strike', '')), **diag))
        if summary['qty']:
            s = self.j.read(); s['seen'] = (s['seen'] + [ticker])[-200:]; self.j.save(s)
        self.last_entry = dict(filled=qnorm(summary['qty']), gross=summary['gross'], fees=summary['fees'],
                               avg=summary['avg_price'],
                               reason='' if summary['qty'] else (('rejected ' + summary['rejected']) if summary.get('rejected') else 'no fill'))
        if summary['qty'] and summary['gross'] + summary['fees'] > MAX_NOTIONAL + Decimal('0.05'):
            self.j.halt('ENTRY_COST_ABOVE_CAP')
            raise SafetyError(f'actual entry cost exceeded the ${MAX_NOTIONAL} cap')
        return summary['qty'] > 0

    def _scaling_gate(self, cand, s):
        """V12.3 news blackout + weekly drawdown stop. Enforced above $10; advisory (logged only) at $10."""
        ticker = str(cand.get('ticker', ''))
        rt = Path(self.j.path).parent
        try:
            close = cand.get('close_time')
            if close not in (None, ''):
                try:
                    close_ts = float(close)
                except (TypeError, ValueError):
                    close_ts = datetime.fromisoformat(str(close).replace('Z', '+00:00')).timestamp()
                news = scaling.blackout_for(close_ts)
                if news:
                    if scaling.enforced(self.trade_usd):
                        return 'news blackout: ' + news
                    if ('news', ticker) not in self._advised:
                        self._advised.add(('news', ticker))
                        self._event('ADVISORY_NEWS_BLACKOUT', ticker=ticker, release=news, note='not enforced at $10')
            blocked, msg = scaling.weekly_check(rt, self.trade_usd, now=self.clock(), journal_history=s.get('history'))
            if blocked:
                return msg
            if msg.startswith('WEEKLY STOP') and ('week', ticker) not in self._advised:
                self._advised.add(('week', ticker))
                self._event('ADVISORY_WEEKLY_STOP', ticker=ticker, detail=msg)
        except Exception as e:                     # a broken calendar must never open a hole: block above $10
            if scaling.enforced(self.trade_usd):
                return f'scaling check failed ({type(e).__name__}); entry skipped'
        return None

    # ================================================================ per-poll management
    def on_poll(self, *, ticker, seconds_left, held_bid, stop_reason, flatten_requested=False):
        """Call every collector poll. Manages exits for the open position on `ticker`."""
        s = self.j.read()
        p = s['position']
        if not p or p['ticker'] != ticker:
            return
        if s['pending']:
            raise SafetyError('unresolved order while managing position')
        open_qty = _open(p)
        reason = 'OPERATOR_FLATTEN' if flatten_requested else stop_reason
        if not p['exiting']:
            if not reason:
                return
            p['exiting'] = True; p['exit_reason'] = reason
            self.j.save(s)
            self._event('STOP_TRIGGERED', ticker=ticker, reason=reason, open_qty=open_qty, seconds_left=round(seconds_left, 2), bid=held_bid)
        # Once exiting, keep going until flat (sticky): partial fills are retried every poll.
        if seconds_left <= EXIT_MIN_SECONDS:
            return
        if p['exit_orders'] >= MAX_EXIT_ORDERS_PER_MARKET:
            if 'EXIT_ORDER_LIMIT' not in s['reason']:
                self.j.halt('EXIT_ORDER_LIMIT reached; remaining contracts will settle')
            return
        # ETH V1.4: price every exit attempt from the order book read NOW (not a seconds-old bid).
        book, _err = self._book_snapshot(ticker, p['held'])
        fresh = exit_basis(book['bids'], open_qty) if book else None
        if fresh is None and (held_bid is None or D(held_bid) <= 0):
            self._event('EXIT_WAIT_NO_BID', ticker=ticker, open_qty=open_qty)
            return
        basis = fresh if fresh is not None else D(held_bid)
        limit_held = max(Decimal('0.01'), (basis - EXIT_CROSS).quantize(Decimal('0.01'), rounding=ROUND_FLOOR))
        if p['held'] == 'yes':
            book_side = 'ask'; yes_price = limit_held                       # sell YES at >= limit
        else:
            book_side = 'bid'; yes_price = min(Decimal('0.99'), (1 - limit_held).quantize(Decimal('0.01'), rounding=ROUND_CEILING))  # buy YES <= 1-limit == sell NO >= limit
        diag = dict(book_age_s=round(self.clock() - book['read_at'], 3) if book else None,
                    book_bids_top3=self._top3(book['bids']) if book else _err,
                    book_asks_top3=self._top3(book['asks']) if book else None,
                    held_bid=str(held_bid), fresh_basis=str(fresh), limit_held=str(limit_held), exit_reason=p['exit_reason'],
                    seconds_left=round(seconds_left, 2))
        self._event('EXIT_SUBMIT', ticker=ticker, qty=open_qty, book_side=book_side,
                    reason=p['exit_reason'], **{k: v for k, v in diag.items() if k != 'exit_reason'})
        self._submit('EXIT', ticker, p['held'], open_qty, yes_price, book_side, True, extra=diag)

    def poll_settlement(self, force=False):
        s = self.j.read(); p = s['position']
        if not p or s['pending']:
            return False
        now = self.clock()
        if not force:
            if p.get('close_time') and now < float(p['close_time']) + 3:
                return False
            if now - self._last_settlement_poll < SETTLEMENT_POLL_SECONDS:
                return False
        self._last_settlement_poll = now
        return self._settle_if_available(force=force)

    def _settle_if_available(self, force=False):
        s = self.j.read(); p = s['position']
        if not p:
            return False
        since = int(p['opened_at']) - 300
        recs = [r for r in self.api.settlements_since(since) if r.get('ticker') == p['ticker']]
        if not recs:
            return False
        if len(recs) > 1:
            self.j.halt('MULTIPLE_SETTLEMENT_RECORDS ' + p['ticker'])
            raise SafetyError('multiple settlement records for one market')
        open_qty = _open(p)
        try:
            settle = settlement_cash(recs[0], p['held'], open_qty)
        except AccountingError as e:
            self.j.halt('SETTLEMENT_MISMATCH ' + str(e))
            raise SafetyError('settlement does not match journal: ' + str(e)) from None
        expo = self._eth_exposure()
        if expo.get(p['ticker']):
            # V12.4: wait out Kalshi's short lag instead of pausing at once. The position stays open in the
            # journal while we wait, so no new entry can happen. Pause only if it is still listed after the grace.
            first = self._settle_lag.setdefault(p['ticker'], self.clock())
            waited = self.clock() - first
            if waited < SETTLE_LAG_GRACE_SECONDS:
                if waited == 0:
                    self._event('SETTLE_LAG_WAIT', ticker=p['ticker'], grace_s=SETTLE_LAG_GRACE_SECONDS)
                return False
            self.j.halt('SETTLED_BUT_POSITION_REMAINS')
            raise SafetyError(f'settlement found but exchange still shows a position after {round(waited)}s')
        if self._settle_lag.pop(p['ticker'], None) is not None:
            self._event('SETTLE_LAG_CLEARED', ticker=p['ticker'])
        self._close_position(settle)
        return True

    def status_line(self):
        s = self.j.read(); st = s.get('stats', {}); p = s['position']
        pos = 'flat' if not p else f"{p['ticker'][-9:]} {p['held'].upper()} {qjson(_open(p))} open @ {D(p['entry_avg']):.3f}"
        return (f"RE5 STATUS | {pos} | today realized ${D(s['realized_usd']):+.2f} | closed trades {len([h for h in s['history'] if str(h.get('closed_day', s['day'])) == s['day']])} "
                f"| orders {st.get('orders', 0)} fills {st.get('fills', 0)} no-fills {st.get('no_fills', 0)} rejects {st.get('rejects', 0)} "
                f"recovered {st.get('recovered_orders', 0)} "
                f"| {'HALTED: ' + s['reason'] if s['halted'] else 'entries enabled'}")

    def failure(self, exc):
        """Block new entries after any unexpected error. Exits/settlement continue if no order is unresolved."""
        self.j.halt('OPERATIONAL_ERROR ' + type(exc).__name__ + ': ' + str(exc)[:160])

    def must_stop_process(self):
        """True when the process should exit instead of continuing in protect mode."""
        s = self.j.read()
        return bool(s['pending']) or (not s['position'] and s['halted'])
