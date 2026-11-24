"""Revision 3 price logging (research only; never affects trading decisions).

markets.csv      one row per 15-minute market: strike, ETH path, ask/bid ranges for YES and NO,
                 every checkpoint decision, paper decision, live action, official result, real P/L.
market_tape.csv  Kalshi public trades in the last 5 minutes of each market (what others paid),
                 fetched once ~4 minutes after close.

Every method swallows its own errors: a logging failure can never stop or change the bot.
"""
import csv, json, os, time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

MARKET_FIELDS = ['ticker', 'close_utc', 'strike', 'polls', 'eth_first', 'eth_last', 'gap_last', 'min_abs_gap_last_180s',
                 'max_abs_gap', 'yes_ask_min', 'yes_ask_max', 'no_ask_min', 'no_ask_max', 'yes_bid_last', 'no_bid_last',
                 'max_scaled_gap', 'favored_side_last', 'checkpoints', 'paper_decision', 'live_action', 'live_detail',
                 'official_result', 'real_pnl', 'tape_trades_last_5min', 'tape_note']
TAPE_FIELDS = ['ticker', 'trade_id', 'created_time', 'seconds_before_close', 'taker_side', 'count', 'yes_price', 'no_price']
FLUSH_AFTER_CLOSE = 240


def _d(x):
    try:
        if x in (None, ''):
            return None
        v = Decimal(str(x))
        return v if v.is_finite() else None
    except Exception:
        return None


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='seconds')


def _parse_time(value):
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


class MarketLog:
    def __init__(self, log_dir, settlements_csv=None, journal_file=None):
        self.dir = Path(log_dir)
        self.markets_csv = self.dir / 'markets.csv'
        self.tape_csv = self.dir / 'market_tape.csv'
        self.settlements_csv = Path(settlements_csv) if settlements_csv else None
        self.journal_file = Path(journal_file) if journal_file else None
        self.m = {}

    # ------------------------------------------------------------ observation hooks
    def observe(self, ticker, close_ts, strike, eth, remaining, quotes, market, scaled_gap=None):
        try:
            r = self.m.get(ticker)
            if r is None:
                r = self.m[ticker] = dict(ticker=ticker, close=float(close_ts), strike=str(strike), polls=0, eth_first=str(eth),
                                          gaps=[], checkpoints=[], paper='', live='', live_detail='', max_scaled=None)
            r['polls'] += 1
            r['eth_last'] = str(eth)
            gap = _d(eth) - _d(strike)
            r['gap_last'] = gap
            r['gaps'].append((float(remaining), abs(gap)))
            r['favored'] = 'yes' if gap > 0 else 'no'
            for side in ('yes', 'no'):
                ask = _d((quotes.get(side) or (None,))[0])
                if ask is not None:
                    r[side + '_ask_min'] = min(r.get(side + '_ask_min', ask), ask)
                    r[side + '_ask_max'] = max(r.get(side + '_ask_max', ask), ask)
                bid = _d(market.get(side + '_bid_dollars'))
                if bid is not None:
                    r[side + '_bid_last'] = bid
            sg = _d(scaled_gap)
            if sg is not None:
                r['max_scaled'] = sg if r['max_scaled'] is None else max(r['max_scaled'], sg)
        except Exception:
            pass

    def checkpoint(self, ticker, cp, side, cand):
        try:
            r = self.m.get(ticker)
            if r is None:
                return
            ask = cand.get('ask') or '-'
            r['checkpoints'].append(f"{cp}:{side}:{ask}:{cand.get('scaled_gap') or '-'}:{cand.get('status')}")
        except Exception:
            pass

    def paper(self, ticker, text):
        if ticker in self.m and not self.m[ticker]['paper']:
            self.m[ticker]['paper'] = text

    def live(self, ticker, action, detail=''):
        if ticker in self.m:
            r = self.m[ticker]
            r['live'] = (r['live'] + ';' if r['live'] else '') + action
            if detail:
                r['live_detail'] = (r['live_detail'] + ';' if r['live_detail'] else '') + str(detail)[:120]

    # ------------------------------------------------------------ flushing
    def maybe_flush(self, client, now=None, force=False):
        now = time.time() if now is None else now
        for ticker in list(self.m):
            r = self.m[ticker]
            if force or now >= r['close'] + FLUSH_AFTER_CLOSE:
                try:
                    self._write(client, r, now)
                except Exception:
                    pass
                self.m.pop(ticker, None)

    def _official(self, ticker):
        try:
            if self.settlements_csv and self.settlements_csv.exists():
                with self.settlements_csv.open(newline='', encoding='utf-8') as f:
                    for row in csv.DictReader(f):
                        if row.get('ticker') == ticker and row.get('official_side') in ('yes', 'no'):
                            return row['official_side']
        except Exception:
            pass
        return ''

    def _real_pnl(self, ticker):
        try:
            if self.journal_file and self.journal_file.exists():
                s = json.loads(self.journal_file.read_text(encoding='utf-8'))
                for h in reversed(s.get('history', [])):
                    if h.get('ticker') == ticker:
                        return h.get('pnl', ''), h.get('result', '')
        except Exception:
            pass
        return '', ''

    def _tape(self, client, r):
        """Public trades in the final 5 minutes. Returns (count, note)."""
        if client is None:
            return '', 'no client'
        close = r['close']
        rows = []
        cursor = ''
        for _ in range(5):
            params = {'ticker': r['ticker'], 'min_ts': int(close - 300), 'max_ts': int(close + 5), 'limit': 1000}
            if cursor:
                params['cursor'] = cursor
            data = client.request('GET', '/markets/trades', params) or {}
            for t in data.get('trades') or []:
                if not isinstance(t, dict):
                    continue
                ts = _parse_time(t.get('created_time') or t.get('ts'))
                yp = t.get('yes_price_dollars')
                if yp in (None, '') and t.get('yes_price') not in (None, ''):
                    yp = str(Decimal(str(t['yes_price'])) / 100)
                npr = t.get('no_price_dollars')
                if npr in (None, '') and t.get('no_price') not in (None, ''):
                    npr = str(Decimal(str(t['no_price'])) / 100)
                rows.append(dict(ticker=r['ticker'], trade_id=t.get('trade_id', ''), created_time=t.get('created_time', ''),
                                 seconds_before_close='' if ts is None else round(close - ts, 1),
                                 taker_side=t.get('taker_side', ''), count=t.get('count_fp', t.get('count', '')),
                                 yes_price=yp or '', no_price=npr or ''))
            cursor = data.get('cursor') or ''
            if not cursor:
                break
        new = not self.tape_csv.exists() or self.tape_csv.stat().st_size == 0
        with self.tape_csv.open('a', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=TAPE_FIELDS, extrasaction='ignore')
            if new:
                w.writeheader()
            w.writerows(rows)
        return len(rows), ''

    def _write(self, client, r, now):
        try:
            n, note = self._tape(client, r)
        except Exception as e:
            n, note = '', 'tape error ' + type(e).__name__
        late = [g for rem, g in r['gaps'] if rem <= 180]
        pnl, result = self._real_pnl(r['ticker']) if r['live'] else ('', '')
        row = dict(ticker=r['ticker'], close_utc=_iso(r['close']), strike=r['strike'], polls=r['polls'],
                   eth_first=r['eth_first'], eth_last=r.get('eth_last', ''), gap_last=r.get('gap_last', ''),
                   min_abs_gap_last_180s=min(late) if late else '', max_abs_gap=max(g for _, g in r['gaps']) if r['gaps'] else '',
                   yes_ask_min=r.get('yes_ask_min', ''), yes_ask_max=r.get('yes_ask_max', ''),
                   no_ask_min=r.get('no_ask_min', ''), no_ask_max=r.get('no_ask_max', ''),
                   yes_bid_last=r.get('yes_bid_last', ''), no_bid_last=r.get('no_bid_last', ''),
                   max_scaled_gap='' if r['max_scaled'] is None else r['max_scaled'], favored_side_last=r.get('favored', ''),
                   checkpoints=' '.join(r['checkpoints']), paper_decision=r['paper'], live_action=r['live'],
                   live_detail=r['live_detail'], official_result=self._official(r['ticker']) or result, real_pnl=pnl,
                   tape_trades_last_5min=n, tape_note=note)
        new = not self.markets_csv.exists() or self.markets_csv.stat().st_size == 0
        with self.markets_csv.open('a', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=MARKET_FIELDS, extrasaction='ignore')
            if new:
                w.writeheader()
            w.writerow(row)


def heartbeat(path):
    """Tell the desktop app the engine is alive (pid + time). Never raises."""
    if not path:
        return
    try:
        tmp = Path(str(path) + '.tmp')
        tmp.write_text(json.dumps({'pid': os.getpid(), 'time': time.time()}), encoding='utf-8')
        os.replace(tmp, path)
    except Exception:
        pass
