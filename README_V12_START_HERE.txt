BIGBET LIVE STUDIO V12  (engine RE5.3 = RE5.2 trading rules + Revision 3 logging and fixes)
=========================================================================================

WHAT CHANGED FOR YOU
 * One window does everything: checks, start, stop, emergency sell, results, export.
 * No .bat files, no environment variables to type, no CLEAR to type after a normal session,
   no folder copying when you upgrade.
 * The trading rules are exactly the same as RE5.2 (same entries, same stops, $10 cap,
   one loss stops the day).

FIRST TIME ONLY (about 3 minutes)
 1. Let the Revision 2 session that is running now FINISH first (its PowerShell window
    says the session ended). Never run two bots at the same time.
 2. Extract this zip into:  ...\Production Model\Revision 3
 3. Open that folder in File Explorer, click the address bar, type  powershell  and press Enter.
 4. In PowerShell type:
        python .\BIGBET_LIVE_STUDIO_V12.py
 5. The app asks for your Kalshi key ID and your key FILE (Browse... to the same .pem/.txt
    file you used before). Only the file LOCATION is saved, never the key itself.
 6. The app finds your Revision 2 journal and asks to copy it. Click YES.
    (This keeps today's P/L, the loss counter and your trade history.)

EVERY SESSION AFTER THAT
 1. Open the folder, type powershell in the address bar, then:  python .\BIGBET_LIVE_STUDIO_V12.py
 2. Wait for the READY CHECK lights to turn green (the first time it also runs the engine
    self-test, about 10 seconds; after that it remembers).
 3. Click LIVE TRADING, type LIVE.
 4. Pick "Run for", click START LIVE, click Yes.
    If shard 2 has less than $15 the app first moves $20 from your main shard
    (at most $40 per day) and shows it in the activity panel.
 5. When the session ends, the app checks the account and exports the logs by itself.
    The activity panel shows the zip file path: send me that file.

BUTTONS
 STOP NEW TRADES      no new entries; an open position stays protected until sold/settled,
                      then the engine stops by itself.
 EMERGENCY SELL NOW   sells the open position at the best price available and stops new trades.
 CHECK AGAIN          re-runs the ready check (it also clears leftovers from a normal finish).
 Clear pause          only shows when entries were paused by an error. Look at Kalshi first.
 A daily-loss pause cannot be cleared: it ends at 7 PM New York time (midnight UTC).

CLOSING THE WINDOW
 Closing the window does NOT stop a live session. The engine keeps running and protecting
 any position. Open the app again any time: it reattaches and shows the session.

WHERE YOUR FILES ARE
 C:\Users\<you>\BIGBET_ETH_DATA\RE5_prod      journal, events, live_orders.csv, daily_summary.csv
 C:\Users\<you>\BIGBET_ETH_DATA\EXPORTS       zip files to send for analysis (no keys inside)
 Upgrading later = extract the new zip and run it. The data folder stays where it is.

NEW LOGS (Revision 3)
 live_orders.csv      every real order: signal price vs best ask vs fill price (slippage in cents),
                      latency, quote age, book depth, shard cash, reject code, exit reason
 markets.csv          one row per 15-min market: strike, ETH path, YES/NO ask ranges and bids,
                      every checkpoint decision, paper decision, live action, result, real P/L
 market_tape.csv      Kalshi public trades in the last 5 minutes of each market
 daily_summary.csv    one row per day: trades, wins/losses, fills/no-fills/rejects, average
                      slippage and latency, shard top-ups
 eth_v42_market_quotes.csv now also records the BID and the spread every poll.

FIXES IN RE5.3
 * V12.2: a tiny fractional offer at the front price level (e.g. 0.40 contracts) no longer blocks
   a valid signal; the size comes from the full live order book (still max 99c, max $10).
 * FRACTIONAL FILLS (Nov 3 08:28 halt): Kalshi fills can be fractions of a contract (e.g. 0.20 + 9.80).
   They are now accepted and recovered; the 08:30 order left 'pending' in Revision 2 is finished
   automatically after you import the journal (then click 'Clear pause' once).
 * Settlement fee no longer subtracted twice (your Nov 3 trade is +$0.121, as on Kalshi's card;
   the old journal line still shows +$0.112 for that one trade).
 * Status counters include orders finished by crash recovery ("recovered").
 * Session-end and stop-button pauses are cleared automatically once the account is flat
   and matches the journal; error pauses still need your OK.

IF SOMETHING IS RED
 Take a screenshot of the window and send it with the TECHNICAL LOG tab contents.

COMMAND-LINE BACKUP (same engine, same data folder) if the window ever will not open:
   python .\RE5_RECONCILE.py --env prod
   python .\RUN_LIVE_RE5.py --env prod --minutes 240
   python .\EXPORT_RE5.py --env prod
