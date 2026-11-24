"""Pure accounting from exchange records. No I/O.

Kalshi V2 fills carry book_side (bid|ask on the YES book), outcome_side/side, action,
count_fp, yes_price_dollars, no_price_dollars and fee_cost. book_side is authoritative:
a real NO purchase on Nov 3 2026 was reported as book_side=ask, action=sell, side=no.
Legacy side/action labels are only used when book_side is missing. Every fill is
converted into its effect on the HELD outcome.
"""
from decimal import Decimal, ROUND_CEILING

D = lambda x: Decimal(str(x))
ONE = Decimal('1')


class AccountingError(RuntimeError):
    pass


def qnorm(x):
    """Contract quantity as a Decimal in 0.01 steps (10.00 -> 10)."""
    v = D(x).quantize(Decimal('0.01'))
    return v.to_integral_value() if v == v.to_integral_value() else v


def qjson(x):
    """Quantity for the JSON journal: int when whole (unchanged format), else '9.90'."""
    v = qnorm(x)
    return int(v) if v == v.to_integral_value() else str(v)


def fee_ceiling(price, count):
    """Upper bound of Kalshi's standard taker fee for budgeting (0.07*n*p*(1-p), rounded up to 1c)."""
    p = D(price); n = D(count)
    return (Decimal('0.07') * n * p * (ONE - p)).quantize(Decimal('0.01'), rounding=ROUND_CEILING)


def fill_effect(fill, held):
    """Return (delta_held_contracts, cash_flow_excluding_fee, fee) for one fill.

    delta > 0 increases the held outcome (a buy of it), delta < 0 reduces it.
    cash_flow is negative when we pay, positive when we receive.
    """
    if held not in ('yes', 'no'):
        raise AccountingError('held outcome must be yes/no')
    book_side = str(fill.get('book_side', '')).lower()
    side = str(fill.get('side', '')).lower()
    action = str(fill.get('action', '')).lower()
    if book_side not in ('bid', 'ask') and (side not in ('yes', 'no') or action not in ('buy', 'sell')):
        raise AccountingError('fill missing book_side and side/action')
    q = D(fill.get('count_fp', fill.get('count', 'nan')))
    # RE5.3: Kalshi has fractional contracts (resting sizes like 49.80), so ONE fill of a whole
    # 10-lot order can be 0.10 contracts. Accept positive counts in 0.01 steps.
    if not q.is_finite() or q <= 0 or q != q.quantize(Decimal('0.01')):
        raise AccountingError('fill count must be positive in 0.01 steps')
    try:
        p_yes = D(fill['yes_price_dollars']); p_no = D(fill['no_price_dollars'])
    except KeyError:
        raise AccountingError('fill missing yes/no price') from None
    if not (0 <= p_yes <= 1 and 0 <= p_no <= 1) or abs(p_yes + p_no - ONE) > Decimal('0.0001'):
        raise AccountingError('inconsistent fill prices')
    fee = D(fill.get('fee_cost', 'nan'))
    if not fee.is_finite() or fee < 0:
        raise AccountingError('invalid fill fee')
    if book_side in ('bid', 'ask'):
        # V2 fills: book_side is on the YES book. bid = YES up (buy YES / sell NO); ask = NO up.
        # Real example (Nov 3 2026): book_side=ask, action=sell, side=no  ==> bought NO.
        yes_up = book_side == 'bid'
        delta = q if (yes_up == (held == 'yes')) else -q
    elif side == held:
        delta = q if action == 'buy' else -q
    else:  # legacy labels: buying the other outcome == selling the held one, and vice versa
        delta = -q if action == 'buy' else q
    p_held = p_yes if held == 'yes' else p_no
    return delta, -delta * p_held, fee


def summarize_order_fills(fills, held, expect):
    """Aggregate all fills of one order. expect='increase' (entry) or 'decrease' (exit).

    Returns dict(qty, gross, fees, avg_price). gross is the absolute dollars paid (entry)
    or received (exit) before fees.
    """
    ids = [f.get('fill_id') or f.get('trade_id') for f in fills]
    if any(not i for i in ids) or len(ids) != len(set(ids)):
        raise AccountingError('missing or duplicate fill ids')
    qty = Decimal(0); cash = Decimal(0); fees = Decimal(0)
    for f in fills:
        delta, flow, fee = fill_effect(f, held)
        if (expect == 'increase' and delta <= 0) or (expect == 'decrease' and delta >= 0):
            raise AccountingError(f'fill direction does not match expected {expect}')
        qty += abs(delta); cash += flow; fees += fee
    gross = abs(cash)
    return dict(qty=qnorm(qty), gross=gross, fees=fees, avg_price=(gross / qty if qty else None))


def settlement_cash(record, held, open_qty):
    """Cash received at settlement for the remaining contracts.

    Uses the exchange's own revenue (cents). Revision 3: the settlement record's fee_cost
    REPEATS the trading fees already paid on the fills of this market (real Nov 3 2026 record:
    fill fee 0.009, settlement fee_cost 0.009, Kalshi profit 0.12). It is not an extra charge,
    so it is reported (reported_fee) but not subtracted again. Validates that the settled count
    of the held outcome equals our open quantity.
    """
    result = str(record.get('market_result', '')).lower()
    if result not in ('yes', 'no', 'void', 'scalar'):
        raise AccountingError('unknown market_result')
    count_key = f'{held}_count_fp'
    if record.get(count_key) is None:
        raise AccountingError(f'settlement missing {count_key}')
    settled = D(record[count_key])
    if qnorm(settled) != qnorm(open_qty):
        raise AccountingError(f'settled {held} count {settled} != journal open {open_qty}')
    other = 'no' if held == 'yes' else 'yes'
    if record.get(f'{other}_count_fp') not in (None, '') and D(record[f'{other}_count_fp']) != 0:
        raise AccountingError('settlement shows contracts on the other outcome')
    revenue = D(record.get('revenue', 'nan')) / 100
    fee = D(record.get('fee_cost', '0') or '0')
    if not revenue.is_finite() or revenue < 0 or fee < 0:
        raise AccountingError('invalid settlement revenue/fee')
    if result in ('yes', 'no'):
        expected = D(open_qty) if result == held else Decimal(0)
        if revenue != expected:
            raise AccountingError(f'revenue {revenue} != expected {expected} for result {result}')
    return dict(result=result, revenue=revenue, fee=Decimal(0), reported_fee=fee, net=revenue)
