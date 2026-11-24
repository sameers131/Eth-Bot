"""V12.3 scaling safety: trade-size ladder, news blackout, weekly drawdown stop, size-scaled limits.

Design rules
  * The trade size is ONE number (BIGBET_TRADE_USD) chosen from LADDER. Every cap is derived from
    it here, so nothing can drift: order notional, contract count, daily loss cap, shard top-up,
    the production arm phrase (ARM-PROD-<size>USD) and the paper budget.
  * At $10 everything is exactly what V12.2 did. The blackout and weekly stop are ADVISORY at $10
    (logged as "would have blocked", never block); they are ENFORCED at any size above $10.
  * A bigger size must be EARNED: LADDER_STEP_TRADES closed trades at the current size with at most
    LADDER_STEP_MAX_LOSSES losses, and (for the first step) at least one real stop-loss exit seen.
    The operator then unlocks it by hand in the app. Two losses within the last
    LADDER_STEP_TRADES trades at a size above $10 drops the ladder one step automatically.

Everything is pure file arithmetic: no network.
"""
import json, os, time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

# ETH V1.7: FREE size choice $5-$40 in $5 steps (no ladder lock, no automatic step-down).
SIZES = (5, 10, 15, 20, 25, 30, 35, 40)
LADDER = SIZES                              # kept under the old name for the code that reads it
HARD_MAX_USD = 40
BASE_USD = 10                               # unchanged meaning: blackout / weekly stop are enforced ABOVE $10
LADDER_STEP_TRADES = 40
LADDER_STEP_MAX_LOSSES = 1
FIRST_STEP_NEEDS_REAL_STOP = True
WEEKLY_DAYS = 7
WEEKLY_STOP_FRACTION = Decimal('1.0')      # rolling 7-day P/L <= -1.0 x trade size -> stop until reviewed
BLACKOUT_BEFORE_MIN = 5                    # a market is skipped if its 15 minutes touch
BLACKOUT_AFTER_MIN = 30                    # [release - 5 min, release + 30 min]
MARKET_MINUTES = 15
CALENDAR_FILE = Path(__file__).with_name('news_calendar.json')
LEDGER_NAME = 'closed_trades.jsonl'
LADDER_NAME = 'ladder.json'


# ====================================================================== size and derived limits
def check_size(usd):
    usd = int(usd)
    if usd not in SIZES or usd > HARD_MAX_USD:
        raise ValueError(f'trade size ${usd} is not one of {SIZES}')
    return usd


def trade_usd(environ=None):
    env = os.environ if environ is None else environ
    return check_size(env.get('BIGBET_TRADE_USD', str(BASE_USD)) or BASE_USD)


def max_notional(usd):
    return Decimal(check_size(usd)).quantize(Decimal('0.01'))


def max_contracts(usd):
    """Whole contracts that fit the size at the 90c minimum entry price (ETH V1.3 Rule B: $10 -> 11, $20 -> 22,
    $40 -> 44, $60 -> 66). The dollar cap (max_notional, fees included) is unchanged and always applies."""
    return int((Decimal(check_size(usd)) / Decimal('0.90')).to_integral_value(rounding=ROUND_FLOOR))


def arm_phrase(usd):
    return f'ARM-PROD-{check_size(usd)}USD'


def daily_loss_cap(usd):
    return (Decimal(check_size(usd)) / 2).quantize(Decimal('0.01'))   # $10 -> $5.00 (unchanged)


def topup_params(usd):
    """(top up when below, amount per top-up, most per UTC day). $10 -> 15 / 20 / 40 (unchanged)."""
    u = Decimal(check_size(usd))
    return (u * Decimal('1.5'), u * 2, u * 4)


MAX_TRANSFER_HARD = Decimal('150')          # absolute ceiling for one shard transfer at any size


def weekly_stop_amount(usd):
    return (Decimal(check_size(usd)) * WEEKLY_STOP_FRACTION).quantize(Decimal('0.01'))


def enforced(usd):
    """Blackout / weekly stop block entries only above the base size."""
    return check_size(usd) > BASE_USD


# ====================================================================== closed-trade ledger
def ledger_path(rt):
    return Path(rt) / LEDGER_NAME


def append_closed(rt, rec, usd):
    """One line per closed real trade. Never raises (bookkeeping must not stop trading)."""
    try:
        exited = Decimal(str(rec.get('exited_qty') or 0))
        row = dict(closed_at=float(rec.get('closed_at') or time.time()), day=rec.get('closed_day', ''),
                   ticker=rec.get('ticker', ''), held=rec.get('held', ''), qty=str(rec.get('qty', '')),
                   cost=str(rec.get('cost', '')), pnl=str(rec.get('pnl', '0')), result=rec.get('result', ''),
                   exit_reason=rec.get('exit_reason', ''), real_stop=bool(exited > 0), trade_usd=int(usd))
        with ledger_path(rt).open('a', encoding='utf-8') as f:
            f.write(json.dumps(row) + '\n')
    except Exception:
        pass


def record_close(rt, rec, usd, prior_history=None):
    """Controller hook: seed from the journal's earlier history on first use, then append."""
    try:
        if not ledger_path(rt).exists() and prior_history:
            read_ledger(rt, prior_history)
    except Exception:
        pass
    append_closed(rt, rec, usd)


def read_ledger(rt, journal_history=None):
    """All closed trades, oldest first. On first use, seeds the ledger from the journal history
    (trades before V12.3 were all at $10)."""
    p = ledger_path(rt)
    if not p.exists() and journal_history:
        for h in sorted(journal_history, key=lambda r: float(r.get('closed_at') or 0)):
            append_closed(rt, h, BASE_USD)
    rows = []
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    seen = set(); out = []
    for r in sorted(rows, key=lambda r: float(r.get('closed_at') or 0)):
        k = (r.get('ticker'), round(float(r.get('closed_at') or 0), 1))
        if k not in seen:
            seen.add(k); out.append(r)
    return out


def weekly_pnl(rows, now=None):
    now = time.time() if now is None else now
    return sum((Decimal(str(r.get('pnl') or 0)) for r in rows if float(r.get('closed_at') or 0) >= now - WEEKLY_DAYS * 86400),
               Decimal(0))


def weekly_check(rt, usd, now=None, journal_history=None):
    """Returns (blocked, message). `blocked` already respects enforcement at this size."""
    rows = read_ledger(rt, journal_history)
    pnl = weekly_pnl(rows, now)
    limit = weekly_stop_amount(usd)
    hit = pnl <= -limit
    msg = f'last 7 days ${pnl:+.2f} (stop at -${limit})'
    review = _read_ladder(rt).get('weekly_review_after', 0)
    if hit and float(review) >= (time.time() if now is None else now) - WEEKLY_DAYS * 86400:
        # operator reviewed and released the stop; only losses AFTER the review count again
        after = [r for r in rows if float(r.get('closed_at') or 0) > float(review)]
        pnl2 = sum((Decimal(str(r.get('pnl') or 0)) for r in after), Decimal(0))
        hit = pnl2 <= -limit
        msg += f' — reviewed; since review ${pnl2:+.2f}'
    return (hit and enforced(usd)), ('WEEKLY STOP: ' if hit else '') + msg + ('' if enforced(usd) or not hit else ' (advisory at $10)')


# ====================================================================== ladder state
def _ladder_file(rt):
    return Path(rt) / LADDER_NAME


def _read_ladder(rt):
    try:
        d = json.loads(_ladder_file(rt).read_text(encoding='utf-8'))
    except Exception:
        d = {}
    d.setdefault('unlocked', BASE_USD); d.setdefault('events', [])
    if d['unlocked'] not in LADDER:
        d['unlocked'] = BASE_USD
    return d


def _write_ladder(rt, d):
    tmp = _ladder_file(rt).with_suffix('.tmp')
    tmp.write_text(json.dumps(d, indent=1), encoding='utf-8')
    os.replace(tmp, _ladder_file(rt))


def _next(usd):
    i = LADDER.index(usd)
    return LADDER[i + 1] if i + 1 < len(LADDER) else None


def _prev(usd):
    i = LADDER.index(usd)
    return LADDER[i - 1] if i > 0 else BASE_USD


def ladder_status(rt, journal_history=None, now=None):
    """ETH V1.7: no ladder lock. Every size in SIZES is allowed; nothing is stepped down.
    Still reports the trade record for the app's information line."""
    rows = read_ledger(rt, journal_history)
    recent = rows[-LADDER_STEP_TRADES:]
    return dict(unlocked=HARD_MAX_USD, allowed=list(SIZES), trades_at_size=len(rows),
                losses_recent=sum(1 for r in recent if Decimal(str(r.get('pnl') or 0)) < 0),
                real_stops=sum(1 for r in rows if r.get('real_stop')), total_trades=len(rows), next=None,
                eligible=False, needs=[], stepped_down=None, events=[], free_choice=True)


def _old_ladder_status_v16(rt, journal_history=None, now=None):
    """V1.6 ladder (kept for reference only; not used in V1.7)."""
    d = _read_ladder(rt)
    rows = read_ledger(rt, journal_history)
    unlocked = d['unlocked']
    at = [r for r in rows if int(r.get('trade_usd') or BASE_USD) == unlocked]
    unlocked_at = float(d.get('unlocked_at', 0))
    at = [r for r in at if float(r.get('closed_at') or 0) >= unlocked_at]
    recent = at[-LADDER_STEP_TRADES:]
    losses = sum(1 for r in recent if Decimal(str(r.get('pnl') or 0)) < 0)
    stops = sum(1 for r in rows if r.get('real_stop'))
    stepped_down = None
    if unlocked > BASE_USD and losses >= LADDER_STEP_MAX_LOSSES + 1:
        new = _prev(unlocked)
        d['events'].append(dict(at=time.time(), kind='STEP_DOWN', frm=unlocked, to=new,
                                why=f'{losses} losses in the last {len(recent)} trades at ${unlocked}'))
        d['unlocked'] = new; d['unlocked_at'] = time.time(); _write_ladder(rt, d)
        stepped_down = (unlocked, new)
        return dict(_old_ladder_status_v16(rt, journal_history, now), stepped_down=stepped_down)
    nxt = _next(unlocked)
    need = []
    if nxt is None:
        need.append(f'${unlocked} is the top of the ladder')
    else:
        if len(at) < LADDER_STEP_TRADES:
            need.append(f'{LADDER_STEP_TRADES - len(at)} more trades at ${unlocked}')
        if losses > LADDER_STEP_MAX_LOSSES:
            need.append(f'at most {LADDER_STEP_MAX_LOSSES} loss in the last {LADDER_STEP_TRADES} (now {losses})')
        if FIRST_STEP_NEEDS_REAL_STOP and unlocked == BASE_USD and stops < 1:
            need.append('one real stop-loss exit seen (none yet)')
    wblocked, wmsg = weekly_check(rt, nxt or unlocked, now, journal_history)
    if nxt is not None and wblocked:
        need.append('weekly stop is active')
    return dict(unlocked=unlocked, allowed=[u for u in LADDER if u <= unlocked], trades_at_size=len(at),
                losses_recent=losses, real_stops=stops, total_trades=len(rows), next=nxt,
                eligible=(nxt is not None and not need), needs=need, stepped_down=stepped_down,
                events=d['events'][-10:])


def unlock_next(rt, journal_history=None):
    st = ladder_status(rt, journal_history)
    if not st['eligible']:
        raise ValueError('not eligible: ' + '; '.join(st['needs']))
    d = _read_ladder(rt)
    d['events'].append(dict(at=time.time(), kind='UNLOCK', frm=st['unlocked'], to=st['next'],
                            why=f"{st['trades_at_size']} trades, {st['losses_recent']} loss(es), {st['real_stops']} real stop(s)"))
    d['unlocked'] = st['next']; d['unlocked_at'] = time.time()
    _write_ladder(rt, d)
    return st['next']


def lower_to(rt, usd):
    """Operator can always step DOWN (never up) by hand."""
    usd = check_size(usd)
    d = _read_ladder(rt)
    if usd < d['unlocked']:
        d['events'].append(dict(at=time.time(), kind='MANUAL_DOWN', frm=d['unlocked'], to=usd, why='operator'))
        d['unlocked'] = usd; d['unlocked_at'] = time.time(); _write_ladder(rt, d)
    return d['unlocked']


def release_weekly_stop(rt):
    d = _read_ladder(rt)
    d['weekly_review_after'] = time.time()
    d['events'].append(dict(at=time.time(), kind='WEEKLY_REVIEWED', why='operator reviewed and released the weekly stop'))
    _write_ladder(rt, d)


# ====================================================================== news blackout
# New York time without zoneinfo (Windows Python has no time-zone database unless tzdata is installed).
def _nth_sunday(year, month, n):
    d = datetime(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _is_dst_local(naive):
    start = _nth_sunday(naive.year, 3, 2).replace(hour=2)
    end = _nth_sunday(naive.year, 11, 1).replace(hour=2)
    return start <= naive < end


def et_to_utc(naive):
    return (naive + timedelta(hours=4 if _is_dst_local(naive) else 5)).replace(tzinfo=timezone.utc)


def utc_to_et(dt):
    naive = dt.astimezone(timezone.utc).replace(tzinfo=None)
    guess = naive - timedelta(hours=4)
    return guess if _is_dst_local(guess) else naive - timedelta(hours=5)


def load_calendar(path=None):
    """[(datetime_utc, name)] from news_calendar.json (times in New York time)."""
    p = Path(path) if path else CALENDAR_FILE
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return []
    out = []
    for e in data.get('events', []):
        try:
            t = et_to_utc(datetime.strptime(e['et'], '%Y-%m-%d %H:%M'))
            out.append((t, str(e.get('name', 'release'))))
        except Exception:
            continue
    return sorted(out)


def blackout_for(close_ts, calendar=None):
    """Name of the scheduled release that touches this market's 15 minutes, or None."""
    cal = load_calendar() if calendar is None else calendar
    close = datetime.fromtimestamp(float(close_ts), timezone.utc)
    open_ = close - timedelta(minutes=MARKET_MINUTES)
    for t, name in cal:
        if open_ < t + timedelta(minutes=BLACKOUT_AFTER_MIN) and close > t - timedelta(minutes=BLACKOUT_BEFORE_MIN):
            return f"{name} at {utc_to_et(t).strftime('%a %b %d %I:%M %p')} ET"
    return None


def next_events(n=3, now=None, calendar=None):
    cal = load_calendar() if calendar is None else calendar
    now_dt = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    return [(t, name) for t, name in cal if t + timedelta(minutes=BLACKOUT_AFTER_MIN) >= now_dt][:n]


def calendar_last_date(calendar=None):
    cal = load_calendar() if calendar is None else calendar
    return cal[-1][0] if cal else None
