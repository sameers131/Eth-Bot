BIGBET V7-D RE5.2 — $10 LIVE — NOV 3 2026
==========================================

RE5.2 CHANGES (NOV 3 2026, after the first real fill)
1. FILL READING FIXED. Kalshi V2 fills report book_side on the YES book; a NO purchase arrives
   as book_side=ask, action=sell, side=no. RE5.1 misread this and halted after the first real
   fill (10 NO @ 98.7c, settled +$10.00). RE5.2 uses book_side first; tested on that exact record.
2. SHARD-AWARE BALANCE. Kalshi keeps cash per exchange shard; ETH 15-min markets are on shard 2.
   The bot checks the balance on the market's own shard and skips with
   "shard 2 balance $X below $Y: move cash to shard 2" instead of sending orders that get rejected.
3. An insufficient_balance rejection pauses new entries with that reason (clear with
   RE5_RECONCILE.py --clear-halt after moving cash). No more repeated rejected orders.
4. One entry attempt per checkpoint (up to 3 per market, spread over later checkpoints).
5. Console shows LIVE_REJECTED / LIVE_SKIPPED / LIVE_NO_FILL with the real reason.

FUNDING SHARD 2 (ETH 15-min markets)
  Kalshi's app moves cash between shards automatically for app orders; API orders do NOT.
  Keep at least $10-20 on shard 2 (Kalshi auto-rebalancing setting, or the intra-account transfer).


RE5.1 CHANGES (NOV 1 2026, after the first live session)
1. A zero-fill no longer uses up the market: up to 3 entry attempts per market; only a FILLED
   market is marked traded. In the 6:30 PM market the bot would have retried at 120 s (97.7c).
2. Entry is priced from the LIVE ORDER BOOK read just before sending (not the market summary):
   limit = best offer + 1c (max 99c), size = contracts actually offered up to that limit and $10.
   Skips if the best offer moved above 99c or fell below 90c, or the book cannot be read.
   IOC still fills at the best price available, so the +1c is only paid if the price moved.
3. Paper ledger mirrors the real account: a live no-fill is withdrawn from paper (status
   LIVE_NO_FILL, never settled or counted), a live fill rewrites the paper trade with the real
   contracts and fee-inclusive cost. No more phantom paper profits in live mode.
4. Real results: a status line every 5 min (position, today's real P/L, orders/fills/no-fills),
   a REAL RESULTS summary when the session ends, and EXPORT_RE5.py to zip one session for review
   (journal, events, log, CSVs; .pem/keys never included, refuses any file with a key marker).
5. Fill diagnostics on every order: quote age, order-book age, top 3 book levels at send,
   limit, requested vs filled, average price, fees, latency (in re5_live_events.jsonl).
6. Default session length is now 240 minutes.

UPGRADING FROM RE5
  Extract RE5.1 to a new folder, then copy your old  PRIVATE_RUNTIME\RE5_prod  folder into the new
  folder's PRIVATE_RUNTIME (keeps today's journal, loss counters and history). Then run
  python .\RE5_RECONCILE.py --env prod  and  python .\TEST_RE5.py  before starting.


WHAT RE5 ADDS (the four RE4 blockers)
1. Partial-exit recovery: once a stop fires, a reduce-only IOC sell is sent every poll at
   (current bid - 2c) until flat. Partial fills are recorded and the rest retried. No new exit
   orders in the last 5 s; whatever is still held then settles normally.
2. Realized P/L from the exchange: entry cost and exit proceeds come from Kalshi FILLS
   (prices + fee_cost). Held-to-close contracts use Kalshi's SETTLEMENT record (revenue,
   fee_cost, counts). Every closed trade is stored in the journal history with its real P/L.
3. Crash reconciliation: every order is written to the journal (with a client_order_id)
   BEFORE it is sent. On restart (and before every launch) the order is looked up on Kalshi
   and finished from real fills; positions are compared with the account. Anything that
   does not match stops the bot with a clear message instead of guessing.
4. Exchange behaviour: V2 response shapes from docs.kalshi.com (top-level order_id/fill_count),
   0.1c price ticks, timeouts/5xx/409 treated as "unknown, look it up", 429 retry on reads,
   definite 4xx rejections cleared, resting orders detected, demo and production hosts.

ALSO FIXED FROM RE3/RE4
- RE3 read the create-order response as {"order": {...}}; Kalshi returns the fields at top
  level, so every real fill would have halted the bot with an open position.
- 0.1c prices (e.g. 98.3c) crashed the session; normal skips (95.0c ask, thin depth) crashed it.
- Live stop used a different rule from paper. RE5 uses the SAME V7-C rule (shared function
  v7c_stop_reason): settlement-average proxy, ETH cross / 0.5x, 88c backup only when ETH < 1x.
- Any error exited the process with a position open. RE5 blocks new entries but keeps
  managing the open position (protect mode); it only exits when an order is unresolved.
- Separate daily-guard file for this build (BIGBET_V7D_RE5_10USD_DAILY_GUARD.sqlite3).

LIMITS (hard-coded)
  KXETH15M only | IOC only | max 11 contracts and $10 incl. fees | entry 90-99c (ETH V1.3 Rule B)
  one position at a time | never re-enter the same market | one losing trade or $5 loss
  halts entries until the next UTC day | live entries only after the paper model approves.

HOW TO RUN (PowerShell in this folder; KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH set)
  python .\TEST_RE5.py                      # 59 offline tests
  python .\TEST_RE5_LOOP.py                 # real collector loop vs simulated exchange
  python .\RUN_LIVE_RE5.py --env demo --minutes 240      # Kalshi DEMO, no real money (needs demo API key)
  python .\RE5_RECONCILE.py --env demo      # after any crash / before restarting
  python .\EXPORT_RE5.py --env prod         # zip the latest session to send for review

  Production (only after demo checks pass):
  $env:BIGBET_RE5_PROD_ARM='ARM-PROD-10USD'
  python .\RUN_LIVE_RE5.py --env prod --minutes 240      # asks you to type EXECUTE 10 USD LIVE

CONTROL FILES (PRIVATE_RUNTIME\RE5_<env>\)
  KILL_SWITCH  -> no new entries; open position still protected; exits when flat
  FLATTEN      -> sell the open position now (IOC, retried)
  re5_journal.json       state (never edit by hand while running)
  re5_live_events.jsonl  every order, fill, stop and settlement with real numbers

THINGS ONLY A REAL EXCHANGE RUN CAN CONFIRM (do these on DEMO first)
  a) A NO entry: check the fill labels and that the journal cost equals Kalshi's cost.
  b) A forced exit (create FLATTEN during a position): reduce_only IOC accepted, proceeds match.
  c) A held-to-close trade: settlement record fields and P/L match the Kalshi portfolio page.
  d) Kill the process (close the window) while a position is open, then run RE5_RECONCILE.py.
  e) position_fp sign: RE5 compares absolute quantities, so either sign convention works.
Never call this production-ready until a-d have been done and the numbers match.

RE5.3 (V12 Live Studio, Nov 3 2026) — see README_V12_START_HERE.txt
 - accounting.settlement_cash: settlement fee_cost repeats the fill fees; reported, not subtracted.
 - controller: live_orders.csv per order; recovered orders counted; richer exit diagnostics.
 - exchange: balance_breakdown(), eth_market_shard(), transfer_between_shards() (capped $50, never retried).
 - ops.py: settings (key path only), fixed data folder, auto housekeeping, shard top-up (option A),
   detached engine with heartbeat + lock pid, export, import from older revisions.
 - collector: heartbeat, markets.csv, market_tape.csv, bid/spread columns, paper END_TIME.
