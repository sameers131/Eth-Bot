"""ETH app: every ETH-specific value in ONE place. This folder trades only Kalshi KXETH15M.

It is a standalone app: its own data folder (~/BIGBET_ETH_DATA), its own settings file
(~/.bigbet/settings_eth.json, key entered once in Settings), its own journal, size ladder,
daily stop counters (paper / live kept apart), exports and top-up ledger. It reads and writes
nothing that belongs to any other BIGBET app.

The V7-D rules with Rule B (90-99c, 0.03% strike cushion, scaled gap 2.0 / 1.6, 8 minute-pairs, V7-C stop) are
price-relative; only values measured in dollars of ETH are set here.
"""
ASSET = 'ETH'
P = dict(
    symbol='ETH', name='Ethereum', series='KXETH15M', index_id='ETHUSD_RTI',
    price_min=50, price_max=25_000,          # outside this range the ETHUSD_RTI value is rejected
    coherence_floor_usd=3.0,                 # settlement-proxy sanity floor in dollars (6x sigma usually dominates)
    momentum_research_usd='0.5',             # research log only, never used to trade
    data_dir_name='BIGBET_ETH_DATA', settings_name='settings_eth.json', export_tag='BIGBET_ETH',
    panel='#e6efff', badge='#627eea', app_title='BIGBET ETH Live Studio',
    # One-way safety only (never read or written): shards of these series are never used as a
    # top-up source, and these data folder names are refused, so this app cannot touch another bot.
    protect_series=('KXBTC15M',), refuse_data_dirs=('BIGBET_DATA',),
)
SYMBOL = P['symbol']; NAME = P['name']; SERIES = P['series']; INDEX_ID = P['index_id']
MARKET_PREFIX = SERIES + '-'


def plausible_price(x):
    return P['price_min'] < x < P['price_max']
