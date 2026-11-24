"""ETH V1.9.4 market-mood regime for the TRADING ENGINE (option B, chosen Nov 18 2026).

Before each 15-minute market the engine looks at how jumpy ETH was in the PREVIOUS market
(std of 1-minute % moves of ETHUSD_RTI - the same measure as the app's mood light):

    CALM     previous market < 0.0720 % per minute (LOW or MEDIUM light)
             -> looser entries: Early-Hold lead >= 1.5x (was 2.0x), steady filter: ask >= 85c for 60 s (was 90c)
    HIGH     previous market >= 0.0720 % per minute (RED light)
             -> NO new entries in this market
    UNKNOWN  fewer than 10 minutes of the previous market recorded (e.g. bot just started)
             -> V1.9.2 rules exactly (Early-Hold 2.0x, steady 90c)

Simulation on 455 ETH markets at $10 (10 s stop):
    V1.9.2   170 trades, 2 losing, +$27.26, worst day -$2.70, stress test 6 losing
    V1.9.4   155 trades, 1 losing, +$35.37, worst day +$1.03, stress test 6 losing
    Better than V1.9.2 in both halves of the data.

Prices come from the engine's own ETHRTI polls plus the app's mood_samples.json (env BIGBET_MOOD_SAMPLES),
so the regime is known right away when the app has been open.
"""
import json
import statistics
import time
from pathlib import Path

HIGH_FROM = 0.0720
SLOT_S = 900
MIN_MINUTES = 10
KEEP_S = 3600                # only the last 30 minutes are ever needed
MAX_SAMPLES = 20000
RETRY_UNKNOWN_S = 30


def window_vol(samples, start, end):
    """vol (% per minute) of [start, end) from [(ts, price)], or None with fewer than MIN_MINUTES minutes."""
    last = {}
    for ts, px in samples:
        if start <= ts < end and px:
            last[int(ts // 60)] = float(px)
    px = [last[k] for k in sorted(last)]
    if len(px) < MIN_MINUTES:
        return None
    moves = [100 * (px[i + 1] - px[i]) / px[i] for i in range(len(px) - 1)]
    return statistics.pstdev(moves)


def label(vol):
    if vol is None:
        return 'UNKNOWN'
    return 'HIGH' if vol >= HIGH_FROM else 'CALM'


class Regime:
    def __init__(self, mood_file=None):
        self.mood_file = Path(mood_file) if mood_file else None
        self.samples = []
        self.cache = {}          # market close ts -> (label, vol, decided_at)

    def add(self, ts, price):
        try:
            self.samples.append((float(ts), float(price)))
        except (TypeError, ValueError):
            return
        if len(self.samples) > MAX_SAMPLES:
            cut = self.samples[-1][0] - KEEP_S
            self.samples = [s for s in self.samples if s[0] >= cut]

    def _file_samples(self):
        if not self.mood_file:
            return []
        try:
            data = json.loads(self.mood_file.read_text(encoding='utf-8'))
            return [(float(t), float(p)) for t, p in data.get('samples', [])]
        except Exception:
            return []

    def for_market(self, close, now=None):
        """(label, vol) for the market closing at `close` (UTC seconds), judged on the market before it."""
        now = time.time() if now is None else now
        close = float(close)
        hit = self.cache.get(close)
        if hit and (hit[0] != 'UNKNOWN' or now - hit[2] < RETRY_UNKNOWN_S):
            return hit[0], hit[1]
        start = close - SLOT_S
        vol = window_vol(self.samples + self._file_samples(), start - SLOT_S, start)
        lab = label(vol)
        self.cache[close] = (lab, vol, now)
        return lab, vol
