"""ETH V1.9.3 "market mood" light: how jumpy ETH has been, in plain LOW / MEDIUM / HIGH.

Display only: this file never blocks or places a trade. Since V1.9.4 the trading engine applies the same
measure itself (RE5/APP/ETH_V194_Regime.py): after a HIGH market it makes no new entries.

How it is measured
  * The app samples Kalshi's ETH settlement index (ETHUSD_RTI) every ~10 seconds.
  * For each 15-minute market window (:00, :15, :30, :45) it takes the last price of every minute and
    measures how much ETH typically moved per minute: the standard deviation of the 1-minute moves, as a
    percent of the price.
  * Levels come from 359 recorded ETH markets (Nov 5-15 2026), split into thirds:
        LOW    < 0.0454 % per minute
        MEDIUM   0.0454 - 0.0720 %
        HIGH   >= 0.0720 %
  * What happened in the NEXT market, on the V1.9.2 rules (154 trades at $10):
        after LOW     41 trades, 0 losing, +$8.43
        after MEDIUM  44 trades, 0 losing, +$9.87
        after HIGH    41 trades, 1 losing, +$2.15   (broad entry set: -12c per trade, weakest in both halves
                                                     of the data)
    So: LOW / MEDIUM = OK to trade; HIGH = stay away for the next market.
"""
import json
import statistics
import time
from pathlib import Path

LOW_BELOW = 0.0454
HIGH_FROM = 0.0720
SLOT_S = 900                 # 15-minute markets
MIN_MINUTES = 10             # need at least 10 minutes of a window to judge it
SAMPLE_EVERY_S = 10
KEEP_S = 3 * 3600            # keep 3 hours of samples
GREEN, YELLOW, RED, GREY = '#1f9d55', '#c98a00', '#c62828', '#7a7a7a'


def slot_start(ts):
    """Start of the 15-minute window containing ts (UTC seconds). Eastern time is a whole-hour offset,
    so :00/:15/:30/:45 line up with Kalshi's market windows."""
    return int(ts) - int(ts) % SLOT_S


def level(vol):
    if vol is None:
        return None
    if vol < LOW_BELOW:
        return 'LOW'
    if vol < HIGH_FROM:
        return 'MEDIUM'
    return 'HIGH'


def minute_prices(samples, start, end):
    """Last price seen in each whole minute of [start, end). samples: [(ts, price)]."""
    out = {}
    for ts, px in samples:
        if start <= ts < end and px:
            out[int(ts // 60)] = float(px)
    return [out[k] for k in sorted(out)]


def window_vol(samples, start, end=None):
    """(vol % per minute, minutes covered) for one window; vol is None if fewer than MIN_MINUTES."""
    end = start + SLOT_S if end is None else end
    px = minute_prices(samples, start, end)
    if len(px) < 2:
        return None, len(px)
    moves = [100 * (px[i + 1] - px[i]) / px[i] for i in range(len(px) - 1)]
    vol = statistics.pstdev(moves) if len(moves) >= 2 else None
    return (vol if len(px) >= MIN_MINUTES else None), len(px)


def summary(samples, now=None, to_local=None):
    """Everything the app shows. to_local(ts) -> 'h:mm AM' text (optional)."""
    now = time.time() if now is None else now
    fmt = to_local or (lambda ts: time.strftime('%I:%M %p', time.localtime(ts)).lstrip('0'))
    cur = slot_start(now)
    prev = cur - SLOT_S
    pv, pmins = window_vol(samples, prev)
    cv, cmins = window_vol(samples, cur, now)
    hist = []
    for k in range(4, 0, -1):                        # last hour, oldest first
        v, _ = window_vol(samples, cur - k * SLOT_S)
        hist.append(level(v) or '?')
    lv = level(pv)
    cl = level(cv)
    if lv is None and cl is not None:                # previous market unknown (app/bot was off): judge this one
        lv, pv = cl, cv
        prev_from_current = True
    else:
        prev_from_current = False
    if lv is None:
        head, color = 'MARKET MOOD: not enough data yet', GREY
        advice = (f'Need {MIN_MINUTES} minutes of the previous market; have {pmins}. '
                  'Keep the app open - the light fills in on its own.')
    elif lv == 'HIGH':
        head, color = 'MARKET MOOD: HIGH - stay away from the next market', RED
        advice = 'V1.9.4: the bot makes NO new entries in this market (after HIGH markets this strategy has done worst).'
    elif lv == 'MEDIUM':
        head, color = 'MARKET MOOD: MEDIUM - OK to trade', GREEN
        advice = 'Normal conditions - the bot uses calm rules (Early-Hold 1.5x, steady 85c).'
    else:
        head, color = 'MARKET MOOD: LOW - OK to trade', GREEN
        advice = 'Calm conditions - the bot uses calm rules (Early-Hold 1.5x, steady 85c).'
    if prev_from_current:
        prev_txt = f'Previous market {fmt(prev)}-{fmt(cur)}: no data (app was off) - judging this market instead'
    else:
        prev_txt = (f'Previous market {fmt(prev)}-{fmt(cur)}: {lv} ({pv:.3f}% per minute)' if lv
                    else f'Previous market {fmt(prev)}-{fmt(cur)}: not enough data')
    cur_txt = (f'This market so far ({cmins} min): {cl} ({cv:.3f}% per minute)' if cl
               else f'This market so far: {cmins} min of data (judged after {MIN_MINUTES} min)')
    if cl == 'HIGH' and lv != 'HIGH':
        color = YELLOW if color == GREEN else color
        advice += ' Warning: this market is turning HIGH.'
    return dict(level=lv, color=color, head=head, prev=prev_txt, current=cur_txt, advice=advice,
                last_hour='Last hour: ' + ' -> '.join(hist), prev_vol=pv, current_level=cl)


def trim(samples, now=None):
    now = time.time() if now is None else now
    return [(t, p) for t, p in samples if now - t <= KEEP_S]


def load(path, now=None):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return trim([(float(t), float(p)) for t, p in data.get('samples', [])], now)
    except Exception:
        return []


def save(path, samples):
    try:
        p = Path(path)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps({'samples': samples[-2000:]}), encoding='utf-8')
        tmp.replace(p)
    except Exception:
        pass
