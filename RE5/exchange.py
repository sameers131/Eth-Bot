"""RE5 Kalshi exchange client (V2 order endpoints), demo or production.

Request/response shapes follow docs.kalshi.com (checked 2026-11-01):
  POST   /portfolio/events/orders            -> top-level order_id, fill_count, remaining_count,
                                                 average_fill_price, average_fee_paid, ts_ms
  GET    /portfolio/orders/{order_id}         -> {"order": {status in resting|canceled|executed,
                                                 fill_count_fp, remaining_count_fp, ...}}
  GET    /portfolio/orders                    -> {"orders": [...], "cursor": ...}
  GET    /portfolio/fills                     -> {"fills": [{fill_id, order_id, ticker, side yes|no,
                                                 action buy|sell, count_fp, yes_price_dollars,
                                                 no_price_dollars, fee_cost, ...}], "cursor": ...}
  GET    /portfolio/positions                 -> {"market_positions": [{ticker, position_fp, ...}]}
  GET    /portfolio/settlements               -> {"settlements": [{ticker, market_result, yes_count_fp,
                                                 no_count_fp, revenue (cents), fee_cost, ...}]}
  GET    /portfolio/balance

Safety rules enforced HERE (not only in the controller):
  * Writes are impossible unless the client was constructed with an explicit arm mode.
  * Production writes additionally require BIGBET_RE5_PROD_ARM=ARM-PROD-10USD in the environment.
  * Only this folder's market series (KXETH15M, set in RE5/asset.py), only immediate_or_cancel, at most size/0.90 contracts (11 at $10) and the size in dollars incl. fees.
  * Writes are NEVER retried automatically. Any transport/5xx/invalid-JSON outcome after a
    write raises UnknownOrderState so the caller reconciles by client_order_id.
"""
import base64, os, re, time
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from urllib.parse import quote

import requests

HOSTS = {'demo': 'https://external-api.demo.kalshi.co', 'prod': 'https://external-api.kalshi.com'}
PREFIX = '/trade-api/v2'
from . import scaling as _scaling
from . import asset as _asset
# V12.3: every cap follows the ONE trade size chosen for this process (BIGBET_TRADE_USD, default $10).
# At $10 these are exactly the V12.2 values: ARM-PROD-10USD, 10 contracts, $10.00.
TRADE_USD = _scaling.trade_usd()
PROD_ARM_PHRASE = _scaling.arm_phrase(TRADE_USD)
MAX_CONTRACTS = _scaling.max_contracts(TRADE_USD)
MAX_NOTIONAL = _scaling.max_notional(TRADE_USD)
MARKET_PREFIX = _asset.MARKET_PREFIX   # 'KXETH15M-'
MAX_TRANSFER = _scaling.MAX_TRANSFER_HARD   # hard ceiling for one shard top-up ($150; a top-up is 2x the size)


class TradingLocked(RuntimeError): pass
class UnknownOrderState(RuntimeError): pass
class ApiFailure(RuntimeError):
    def __init__(self, message, status=None, code=None):
        super().__init__(message); self.status = status; self.code = code
class OrderRejected(ApiFailure):
    """Exchange answered a write with a definite 4xx (not 409/429): order was not accepted."""


def D(value):
    x = Decimal(str(value))
    if not x.is_finite():
        raise ValueError('non-finite number')
    return x


def fmt_price(p):
    """Fixed-point dollar string, 4 decimals (API accepts 2-4). Rejects sub-0.1c precision."""
    p = D(p)
    if p != p.quantize(Decimal('0.001')):
        raise ApiFailure('price finer than 0.1 cent')
    if not Decimal('0.001') <= p <= Decimal('0.999'):
        raise ApiFailure('price outside 0.001-0.999')
    return f'{p:.4f}'


class ExchangeClient:
    def __init__(self, *, env=None, key_id=None, key_path=None, session=None, write_mode='off'):
        env = (env or os.environ.get('KALSHI_ENV', 'demo')).lower()
        if env not in HOSTS:
            raise ApiFailure('KALSHI_ENV must be demo or prod')
        if write_mode not in ('off', 'demo', 'prod'):
            raise TradingLocked('invalid write mode')
        if write_mode != 'off' and write_mode != env:
            raise TradingLocked(f'write mode {write_mode} does not match environment {env}')
        if write_mode == 'prod' and os.environ.get('BIGBET_RE5_PROD_ARM') != PROD_ARM_PHRASE:
            raise TradingLocked('production writes require BIGBET_RE5_PROD_ARM=' + PROD_ARM_PHRASE)
        self.env = env
        self.base = HOSTS[env]
        self.write_mode = write_mode
        self.key_id = key_id or os.environ.get('KALSHI_API_KEY_ID')
        path = key_path or os.environ.get('KALSHI_PRIVATE_KEY_PATH')
        if not self.key_id or not path:
            raise ApiFailure('KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set')
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
        try:
            self.key = load_pem_private_key(Path(path).read_bytes(), password=None)
        except (OSError, ValueError, TypeError):
            raise ApiFailure('unable to load private key') from None
        if not isinstance(self.key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
            raise ApiFailure('unsupported key algorithm')
        self.session = session or requests.Session()

    # ------------------------------------------------------------------ transport
    def _headers(self, method, path):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa, padding
        stamp = str(int(time.time() * 1000))
        msg = (stamp + method + path).encode()
        if isinstance(self.key, rsa.RSAPrivateKey):
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=hashes.SHA256().digest_size), hashes.SHA256())
        else:
            sig = self.key.sign(msg)
        return {'KALSHI-ACCESS-KEY': self.key_id, 'KALSHI-ACCESS-TIMESTAMP': stamp,
                'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(), 'Content-Type': 'application/json'}

    def _request(self, method, endpoint, *, params=None, payload=None):
        if method not in ('GET', 'POST', 'DELETE'):
            raise ApiFailure('unsupported method')
        write = method != 'GET'
        if write and self.write_mode == 'off':
            raise TradingLocked('exchange writes disabled (write_mode=off)')
        if not endpoint.startswith('/') or '?' in endpoint:
            raise ApiFailure('invalid endpoint')
        path = PREFIX + endpoint
        attempts = 1 if write else 4
        for attempt in range(attempts):
            try:
                r = self.session.request(method, self.base + path, headers=self._headers(method, path),
                                         params=params, json=payload, timeout=(4, 10), allow_redirects=False)
            except requests.RequestException:
                if write:
                    raise UnknownOrderState('write transport failure; outcome unknown') from None
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (attempt + 1)); continue
                raise ApiFailure('read transport failure') from None
            if r.status_code == 429 and not write and attempt + 1 < attempts:
                time.sleep(1.0 * (attempt + 1)); continue
            if r.status_code in (200, 201):
                try:
                    data = r.json()
                except ValueError:
                    if write:
                        raise UnknownOrderState('write returned invalid JSON') from None
                    raise ApiFailure('read returned invalid JSON') from None
                if not isinstance(data, dict):
                    if write:
                        raise UnknownOrderState('write returned unexpected body')
                    raise ApiFailure('unexpected response body')
                return data
            code = None
            try:
                body = r.json()
                code = (body.get('error') or body).get('code') if isinstance(body, dict) else None
            except ValueError:
                pass
            if write:
                # 5xx / 409 (possible duplicate client id) / 429 => cannot be sure the order did not land.
                if r.status_code >= 500 or r.status_code in (409, 429):
                    raise UnknownOrderState(f'write HTTP {r.status_code}; outcome unknown')
                raise OrderRejected(f'order rejected HTTP {r.status_code}', r.status_code, code)
            if r.status_code >= 500 and attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1)); continue
            raise ApiFailure(f'Kalshi HTTP {r.status_code}', r.status_code, code)
        raise ApiFailure('request retries exhausted')

    def _pages(self, endpoint, key, params=None, max_pages=50):
        cursor = None; seen = set(); out = []
        for _ in range(max_pages):
            q = dict(params or {})
            if cursor:
                q['cursor'] = cursor
            data = self._request('GET', endpoint, params=q)
            records = data.get(key)
            if records is None:
                records = []
            if not isinstance(records, list) or any(not isinstance(x, dict) for x in records):
                raise ApiFailure(f'malformed {key} list')
            out.extend(records)
            nxt = data.get('cursor')
            if not nxt:
                return out
            if nxt in seen:
                raise ApiFailure('pagination loop')
            seen.add(nxt); cursor = nxt
        raise ApiFailure('pagination limit reached')

    # ------------------------------------------------------------------ reads
    def balance_dollars(self):
        b = self._request('GET', '/portfolio/balance')
        if b.get('balance_dollars') not in (None, ''):
            return D(b['balance_dollars'])
        if b.get('balance') is None:
            raise ApiFailure('balance missing')
        return D(b['balance']) / 100

    def balance_on_shard(self, exchange_index):
        """Cash available on one exchange shard (orders on a shard can only use that shard's cash)."""
        b = self._request('GET', '/portfolio/balance')
        rows = b.get('balance_breakdown')
        if not isinstance(rows, list):
            raise ApiFailure('balance_breakdown missing')
        for r in rows:
            if int(r.get('exchange_index', -1)) == int(exchange_index):
                return D(r['balance'])
        return Decimal(0)

    def balance_breakdown(self):
        """{exchange_index: cash dollars} for every shard (Revision 3)."""
        b = self._request('GET', '/portfolio/balance')
        rows = b.get('balance_breakdown')
        if not isinstance(rows, list):
            raise ApiFailure('balance_breakdown missing')
        out = {}
        for r in rows:
            out[int(r.get('exchange_index', -1))] = D(r['balance'])
        return out

    def eth_market_shard(self, series=None):
        """exchange_index used by this folder's markets right now (None if not reported).
        `series` (read-only) looks up another bot's market shard so it is never used as a top-up source."""
        data = self._request('GET', '/markets', params={'series_ticker': series or _asset.SERIES, 'limit': 50})
        for m in data.get('markets') or []:
            if isinstance(m, dict) and m.get('exchange_index') not in (None, ''):
                return int(m['exchange_index'])
        return None

    def market(self, ticker):
        _check_ticker(ticker)
        m = self._request('GET', '/markets/' + quote(ticker, safe=''))
        if not isinstance(m.get('market'), dict):
            raise ApiFailure('market missing')
        return m['market']

    def orderbook(self, ticker):
        """Live order book. Kalshi returns BIDS only: orderbook_fp.yes_dollars / no_dollars as
        [[price, qty], ...]. A YES ask at p is the NO bid at 1-p (and vice versa).
        Returns {'yes': [(price, qty)...], 'no': [...]}, each sorted best (highest) bid first."""
        _check_ticker(ticker)
        raw = self._request('GET', '/markets/' + quote(ticker, safe='') + '/orderbook').get('orderbook_fp')
        if not isinstance(raw, dict):
            raise ApiFailure('orderbook_fp missing')
        out = {}
        for side in ('yes', 'no'):
            levels = raw.get(side + '_dollars') or []
            if not isinstance(levels, list):
                raise ApiFailure('malformed orderbook levels')
            parsed = []
            for lvl in levels:
                if not isinstance(lvl, (list, tuple)) or len(lvl) < 2:
                    raise ApiFailure('malformed orderbook level')
                pr, qty = D(lvl[0]), D(lvl[1])
                if 0 < pr < 1 and qty > 0:
                    parsed.append((pr, qty))
            out[side] = sorted(parsed, key=lambda x: x[0], reverse=True)
        return out

    def positions(self):
        return self._pages('/portfolio/positions', 'market_positions',
                           {'settlement_status': 'unsettled', 'count_filter': 'position', 'limit': 200})

    def orders_since(self, min_ts):
        return self._pages('/portfolio/orders', 'orders', {'min_ts': int(min_ts), 'limit': 200})

    def fills_since(self, min_ts):
        return self._pages('/portfolio/fills', 'fills', {'min_ts': int(min_ts), 'limit': 200})

    def settlements_since(self, min_ts):
        return self._pages('/portfolio/settlements', 'settlements', {'min_ts': int(min_ts), 'limit': 200})

    def get_order(self, order_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(order_id)):
            raise ApiFailure('unsafe order id')
        o = self._request('GET', '/portfolio/orders/' + quote(str(order_id), safe=''))
        if not isinstance(o.get('order'), dict):
            raise ApiFailure('order missing')
        return o['order']

    # ------------------------------------------------------------------ write
    def create_ioc(self, *, ticker, client_order_id, book_side, count, yes_price, reduce_only):
        """Single IOC order on the YES book. bid = buy YES / sell NO; ask = sell YES / buy NO."""
        if self.write_mode == 'off':
            raise TradingLocked('exchange writes disabled')
        _check_ticker(ticker)
        if book_side not in ('bid', 'ask'):
            raise ApiFailure('book_side must be bid or ask')
        n = D(count)
        # Entries are always whole; an exit may sell a fractional remainder (Kalshi supports 0.01 steps).
        if n != n.quantize(Decimal('0.01')) or not Decimal('0.01') <= n <= MAX_CONTRACTS or (not reduce_only and n != int(n)):
            raise ApiFailure(f'count must be 1..{MAX_CONTRACTS} (exits: 0.01 steps)')
        price = fmt_price(yes_price)
        outcome_price = D(price) if book_side == 'bid' else 1 - D(price)
        if not reduce_only and outcome_price * n > MAX_NOTIONAL:
            raise ApiFailure(f'notional above ${MAX_NOTIONAL} cap')
        if not re.fullmatch(r'[a-f0-9-]{36}', client_order_id):
            raise ApiFailure('client_order_id must be a uuid4 string')
        body = {'ticker': ticker, 'client_order_id': client_order_id, 'side': book_side,
                'count': f'{n:.2f}', 'price': price, 'time_in_force': 'immediate_or_cancel',
                'self_trade_prevention_type': 'taker_at_cross', 'post_only': False,
                'reduce_only': bool(reduce_only), 'cancel_order_on_pause': True, 'subaccount': 0}
        data = self._request('POST', '/portfolio/events/orders', payload=body)
        if not data.get('order_id') or data.get('fill_count') is None:
            raise UnknownOrderState('create response missing order_id/fill_count')
        return data


    # ------------------------------------------------------------------ shard cash transfer
    def transfer_between_shards(self, *, amount_dollars, source_shard, destination_shard, client_transfer_id):
        """Move cash between your own exchange shards (POST /portfolio/intra_exchange_instance_transfer).

        Not a trade. Amount is sent in centicents ($20 = 200000). Kalshi processes it asynchronously.
        Never retried; 5xx/409 raise UnknownOrderState (the caller re-reads balances)."""
        if self.write_mode == 'off':
            raise TradingLocked('exchange writes disabled')
        amt = D(amount_dollars)
        if amt != amt.quantize(Decimal('0.01')) or not Decimal('1') <= amt <= MAX_TRANSFER:
            raise ApiFailure(f'transfer amount must be $1..${MAX_TRANSFER} in whole cents')
        src, dst = int(source_shard), int(destination_shard)
        if src == dst or not (0 <= src <= 100 and 0 <= dst <= 100):
            raise ApiFailure('invalid shard pair')
        if not re.fullmatch(r'[a-f0-9-]{36}', client_transfer_id):
            raise ApiFailure('client_transfer_id must be a uuid4 string')
        body = {'client_transfer_id': client_transfer_id, 'source': 'event_contract', 'destination': 'event_contract',
                'amount': int(amt * 10000), 'source_exchange_shard': src, 'destination_exchange_shard': dst}
        data = self._request('POST', '/portfolio/intra_exchange_instance_transfer', payload=body)
        if not data.get('transfer_id'):
            raise UnknownOrderState('transfer response missing transfer_id')
        return data


def _check_ticker(ticker):
    if not isinstance(ticker, str) or not ticker.startswith(MARKET_PREFIX) or not re.fullmatch(r'[A-Z0-9._-]{5,80}', ticker):
        raise ApiFailure(f'only {_asset.SERIES} markets are permitted')


def floor_cent(p):
    return D(p).quantize(Decimal('0.01'), rounding=ROUND_FLOOR)
