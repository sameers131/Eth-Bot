"""BIGBET ETH Live Studio V1.3 — desktop app for the Kalshi ETH 15-minute market (KXETH15M, ETHUSD_RTI).

A standalone ETH app: own data folder (~/BIGBET_ETH_DATA), own settings (~/.bigbet/settings_eth.json),
own journal, ladder, daily stop counters (paper and live separate) and exports. Paper or live, in its own
PowerShell window, at the same time as any other BIGBET app. V7-D rules (same as the original model).
"""
import csv, hashlib, json, os, queue, re, subprocess, sys, threading, time, traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from RE5 import ops, scaling, asset, mood  # noqa: E402
SYM = asset.SYMBOL

ENV = 'prod'
TITLE = f"{asset.P['app_title']} V1.9.4 / V7-D Rule C"
DURATIONS = {('%d min' % n if n < 60 else ('%d hour%s' % (n // 60, '' if n // 60 == 1 else 's') if n % 60 == 0
              else '%d hr %d min' % (n // 60, n % 60))): n for n in range(15, 601, 15)}   # ETH V1.5: up to 10 hours
GREEN, YELLOW, RED, GRAY = '#1f9d55', '#d39e00', '#c62828', '#9aa5ad'
PAPER_BG, LIVE_BG = '#3d2a8a', '#b34a00'   # ETH V1.7: PURPLE = paper, ORANGE = live (BTC app is green / red)
PAPER_BTN, LIVE_BTN, ROOT_BG = '#7b5cff', '#ff7a1a', '#1f1745'
PANEL = '#efe9ff'                 # ETH V1.7: lavender panels (BTC app uses yellow)
TEST_FILES = ('TEST_RE5.py', 'TEST_RE5_LOOP.py', 'TEST_V12.py', 'TEST_V12_3.py', 'TEST_V12_4.py', 'TEST_ETH.py', 'TEST_ETH_V1_2.py', 'TEST_ETH_V1_3.py', 'TEST_ETH_V1_4.py', 'TEST_ETH_V1_5.py', 'TEST_ETH_V1_6.py', 'TEST_ETH_V1_7.py', 'TEST_ETH_V1_8.py', 'TEST_ETH_V1_9.py', 'TEST_ETH_V1_9_1.py', 'TEST_ETH_V1_9_2.py', 'TEST_ETH_V1_9_3.py', 'TEST_ETH_V1_9_4.py')
D = lambda x: Decimal(str(x))


def money(x, sign=True):
    try:
        v = D(x)
    except Exception:
        return '—'
    return f'{"+" if sign and v > 0 else ""}{"-" if v < 0 else ""}${abs(v):.2f}'


def local(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime('%b %d %I:%M %p').replace(' 0', ' ')
    except Exception:
        return ''


def short_market(ticker):
    # KXETH15M-26NOV030645-45 / KXETH15M-... -> Nov 03 06:45 (ET market label)
    m = re.match(r'KX[A-Z]+15M-(\d\d)([A-Z]{3})(\d\d)(\d\d)(\d\d)', str(ticker))
    return f'{m.group(2).title()} {m.group(3)} {m.group(4)}:{m.group(5)}' if m else str(ticker)


REASONS = (('STRIKE_CUSHION_BELOW', f'{SYM} too close to the strike'), ('LESS_THAN_8_MINUTE_PAIRS', f'{SYM} history not ready'),
           ('ASK_OUTSIDE_90', 'price outside 90–99¢'), ('SCALED_CUSHION_BELOW', f'{SYM} lead too small for current volatility'),
           ('NO_ASK', 'no price offered'), ('DEPTH', 'not enough contracts offered'), ('STALE', 'data too old'),
           ('CONNECTION_GLITCH_THIS_MARKET', 'Kalshi connection problem in this market — skipped'))


def reasons(text):
    out = [label for key, label in REASONS if key in text]
    return ', '.join(dict.fromkeys(out)) if out else text.strip()[:70]


def plain(line):
    """Translate one engine log line into plain English for the Activity panel (None = technical only)."""
    s = line.strip()
    if not s:
        return None
    m = re.match(r'MARKET (\S+) \| strike \$(\S+)', s)
    if m:
        strike = 'strike not published yet' if m.group(2) == 'not' else f'strike ${m.group(2)}'
        return f'New 15-minute market {short_market(m.group(1))} — {strike}'
    if s.startswith('WAITING_FOR_STRIKE'):
        return 'Waiting for Kalshi to publish the strike price'
    if s.startswith('WAITING_FOR_ACTIVE_ETH_MARKET'):
        return f'Waiting for the next {SYM} market to open'
    m = re.match(r'HISTORY_READINESS \S+ (READY|NOT READY) minute_pairs=(\S+)', s)
    if m:
        if m.group(1) == 'READY':
            return f'{SYM} history ready (8 of 8 minutes) — checkpoints can trade'
        return f'Collecting {SYM} history before trading (needs 8 minutes)' if m.group(2).startswith('0') else None
    m = re.match(r'\s*(\d+)s \| (YES|NO)\s+gap \$(\S+) \| ask (\S+) \| scaled (\S+) \| (\S+)\s*(.*)', line)
    if m:
        cp, side, gap, ask, scaled, status, why = m.groups()
        if 'SIDE_NOT_FAVORED' in why or (status in ('REJECT', 'NOT_APPROVED') and ask == 'unavailable'):
            return None                                   # the losing side is never a trade candidate
        verdict = {'PAPER_APPROVED': 'SIGNAL ✔', 'REJECT': 'no trade', 'NOT_APPROVED': 'blocked by risk rules',
                   'LIVE_NO_FILL': 'signal, but no real fill'}.get(status, status)
        why = (' (' + reasons(why) + ')') if why.strip() and status != 'PAPER_APPROVED' else ''
        try:
            ask = f'{D(ask) * 100:.1f}¢'
        except Exception:
            pass
        try:
            scaled = f'{D(scaled):.2f}'
        except Exception:
            pass
        return f'{cp}s before close: {side} costs {ask}, {SYM} ${gap} from strike, lead {scaled}× volatility → {verdict}{why}'
    m = re.match(r'PAPER_APPROVED ticker=(\S+) side=(\S+) cost=\$(\S+)', s)
    if m:
        return f'Signal: buy {m.group(2).upper()} (model cost ${m.group(3)})'
    m = re.match(r'LIVE_FILLED ticker=(\S+) side=(\S+) contracts=(\d+) cost=\$(\S+)', s)
    if m:
        return f'★ BOUGHT {m.group(3)} {m.group(2).upper()} contracts for ${m.group(4)} ({short_market(m.group(1))})'
    m = re.match(r'(LIVE_NO_FILL|LIVE_SKIPPED|LIVE_REJECTED) ticker=\S+ side=(\S+) reason=(.*?) \(paper', s)
    if m:
        label = {'LIVE_NO_FILL': 'No fill', 'LIVE_SKIPPED': 'Skipped', 'LIVE_REJECTED': 'Kalshi rejected the order'}[m.group(1)]
        return f'{label} on {m.group(2).upper()}: {m.group(3)}'
    m = re.match(r'RE5 STOP_TRIGGERED .*reason=(\S+)', s)
    if m:
        return f'⚠ STOP triggered ({m.group(1)}) — selling the position'
    m = re.match(r'RE5 EXIT_FILL .*qty=(\d+) avg=(\S+) remaining=(\d+)', s)
    if m:
        return f'Sold {m.group(1)} at {m.group(2)} ({m.group(3)} left)'
    m = re.match(r'RE5 POSITION_CLOSED .*held=(\S+) qty=(\d+) .*result=(\S+) pnl=(\S+)', s)
    if m:
        res = {'yes': 'settled YES', 'no': 'settled NO', 'FULLY_EXITED': 'sold by stop'}.get(m.group(3), m.group(3))
        return f'TRADE CLOSED: {m.group(2)} {m.group(1).upper()} {res} — real P/L {money(m.group(4))}'
    if s.startswith('RE5 STATUS'):
        return 'Status: ' + s[len('RE5 STATUS | '):]
    if s.startswith('RE5 ORDER_REJECTED'):
        return 'Kalshi rejected an order: ' + s[len('RE5 ORDER_REJECTED '):]
    if s.startswith('CONNECTION_GLITCH'):
        return '⚠ Kalshi slow/unreachable — retrying (entries continue up to 2 min; this 15-min market will be skipped)'
    m = re.match(r'SKIP_MARKET_AFTER_GLITCH ticker=(\S+)', s)
    if m:
        return f'Skipping market {short_market(m.group(1))}: a Kalshi connection problem happened in it — no entry'
    if s.startswith('FAIL CLOSED'):
        return '⚠ Error — new entries paused, any open position still protected: ' + s[len('FAIL CLOSED: '):][:120]
    if s.startswith('RE5 PROTECT MODE'):
        return 'Protect mode: no new entries; open position still managed'
    if s.startswith('RE5 KILL SWITCH'):
        return 'Stop requested: ' + ('no open position, engine stopping' if 'exiting' in s else 'no new entries; protecting open position')
    if 'SESSION TIME COMPLETE' in s:
        return 'Session time complete'
    if s.startswith('RE5 HALTED') or 'LIVE EXECUTION ACTIVE' in s or s.startswith('=== BIGBET'):
        return s
    if s.startswith('RE5 RECOVER_OK'):
        return 'Engine started — account matches the journal'
    if s.startswith('Traceback') or s.startswith('SystemExit'):
        return '⚠ Engine error: ' + s[:140]
    return None


class Studio(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = ops.load_settings()
        self.mode = 'PAPER'
        self.q = queue.Queue()
        self.attached = None          # dict(rt, session, stamp, live, proc?)
        self.log_pos = 0
        self.checks = {}
        self.check_results = {}
        self.busy = False
        self.last_cash = None
        self.last_cash_at = 0
        self.last_journal_sig = None
        self.finishing = False
        self.plan = None              # V12.7: dict(end, usd, restarts) of the LIVE session the person started
        self.duration = tk.StringVar(value='4 hours')
        self.size = tk.StringVar(value='$10')
        self.ladder = None
        self.status = tk.StringVar(value='Starting…')
        self.title(f'{TITLE}  |  engine {ops.VERSION}')
        self.geometry('1440x980'); self.minsize(1100, 760)
        self._ui()
        self._paint_mode()
        self.protocol('WM_DELETE_WINDOW', self.on_close)
        self.after(200, self.first_run)
        self.after(500, self.tick)
        # V1.9.3 market mood light: own background sampler of the ETH index (display only, never trades)
        self._mood_path = ops.data_dir(self.settings) / 'mood_samples.json'
        self._mood_samples = mood.load(self._mood_path)
        self._mood_err = ''
        threading.Thread(target=self._mood_loop, daemon=True).start()
        self.after(1500, self.refresh_mood)

    # ================================================================== layout
    def _ui(self):
        self.configure(bg=ROOT_BG)
        self.header = tk.Frame(self, padx=15, pady=10); self.header.pack(fill='x')
        left = tk.Frame(self.header); left.pack(side='left', fill='both', expand=True)
        self.h_title = tk.Label(left, font=('Segoe UI', 17, 'bold'), fg='white'); self.h_title.pack(anchor='w')
        self.h_desc = tk.Label(left, font=('Segoe UI', 9), fg='white'); self.h_desc.pack(anchor='w', pady=(2, 8))
        row = tk.Frame(left); row.pack(anchor='w'); self.h_row = row
        self.paper_btn = tk.Button(row, text='PAPER TRADING', width=17, font=('Segoe UI', 10, 'bold'), bg=PAPER_BTN, fg='white',
                                   command=self.select_paper)
        self.paper_btn.pack(side='left', padx=(0, 9))
        self.live_btn = tk.Button(row, text='LIVE TRADING', width=17, font=('Segoe UI', 10, 'bold'), bg=LIVE_BTN, fg='white',
                                  command=self.select_live)
        self.live_btn.pack(side='left')
        self.h_note = tk.Label(left, font=('Segoe UI', 9, 'bold')); self.h_note.pack(anchor='w', pady=(7, 0))
        self.engine_pill = tk.Label(self.header, text='ENGINE: STOPPED', font=('Segoe UI', 12, 'bold'), bg='#3b4650', fg='white',
                                    padx=14, pady=8)
        self.engine_pill.pack(side='right', anchor='n')
        # Big ETH badge so this window can never be mixed up with another BIGBET app.
        tk.Label(self.header, text=f' {asset.SYMBOL} ', font=('Segoe UI', 20, 'bold'), bg=asset.P['badge'], fg='white',
                 padx=10, pady=2).pack(side='right', anchor='n', padx=(0, 12))

        body = ttk.Frame(self, padding=(12, 10)); body.pack(fill='both', expand=True)
        top = ttk.Frame(body); top.pack(fill='x')

        # --- 1 readiness checklist
        cl = tk.LabelFrame(top, text='1  READY CHECK — runs by itself', bg=PANEL, font=('Segoe UI', 10, 'bold'), padx=8, pady=6)
        cl.pack(side='left', fill='both', padx=(0, 6))
        for key, name in (('key', 'Kalshi key'), ('tests', 'Engine self-test'), ('account', 'Account matches journal'),
                          ('cash', f'{SYM} shard cash'), ('entries', 'Entries allowed today'), ('ladder', 'Size & safety'),
                          ('engine', 'Engine')):
            f = tk.Frame(cl, bg=PANEL); f.pack(fill='x', pady=1)
            dot = tk.Label(f, text='●', fg=GRAY, bg=PANEL, font=('Segoe UI', 13)); dot.pack(side='left')
            tk.Label(f, text=name, bg=PANEL, font=('Segoe UI', 9, 'bold'), width=21, anchor='w').pack(side='left')
            txt = tk.Label(f, text='waiting…', bg=PANEL, font=('Segoe UI', 9), anchor='w', width=46, justify='left', wraplength=330)
            txt.pack(side='left')
            btn = tk.Button(f, text='', font=('Segoe UI', 8), padx=4)
            self.checks[key] = dict(dot=dot, txt=txt, btn=btn)
        bb = tk.Frame(cl, bg=PANEL); bb.pack(fill='x', pady=(6, 0))
        self.recheck_btn = ttk.Button(bb, text='↻ CHECK AGAIN', command=self.run_checks); self.recheck_btn.pack(side='left')
        ttk.Button(bb, text='⚙ SETTINGS', command=self.open_settings).pack(side='left', padx=6)

        # --- 2 controls
        ctl = tk.LabelFrame(top, text='2  CONTROLS', bg=PANEL, font=('Segoe UI', 10, 'bold'), padx=10, pady=6)
        ctl.pack(side='left', fill='both', padx=6)
        r = tk.Frame(ctl, bg=PANEL); r.pack(fill='x', pady=(0, 6))
        tk.Label(r, text='Run for:', bg=PANEL, font=('Segoe UI', 9)).pack(side='left')
        ttk.Combobox(r, textvariable=self.duration, values=list(DURATIONS), state='readonly', width=12).pack(side='left', padx=6)
        rs = tk.Frame(ctl, bg=PANEL); rs.pack(fill='x', pady=(0, 6))
        tk.Label(rs, text='Per trade:', bg=PANEL, font=('Segoe UI', 9)).pack(side='left')
        self.size_box = ttk.Combobox(rs, textvariable=self.size, values=['$10'], state='readonly', width=7)
        self.size_box.pack(side='left', padx=6)
        self.size_box.bind('<<ComboboxSelected>>', lambda e: self._size_changed())
        self.unlock_btn = tk.Button(rs, text='Unlock next size', font=('Segoe UI', 8), command=self.unlock_size, state='disabled')
        self.unlock_btn.pack(side='left', padx=2)
        self.start_btn = tk.Button(ctl, text='▶ START', font=('Segoe UI', 12, 'bold'), bg=GREEN, fg='white', width=22,
                                   command=self.start, state='disabled')
        self.start_btn.pack(fill='x', pady=2)
        self.stop_btn = tk.Button(ctl, text='■ STOP NEW TRADES', font=('Segoe UI', 10, 'bold'), width=22, command=self.stop,
                                  state='disabled')
        self.stop_btn.pack(fill='x', pady=2)
        self.flat_btn = tk.Button(ctl, text='⚠ EMERGENCY SELL NOW', font=('Segoe UI', 10, 'bold'), bg='#8b1a1a', fg='white',
                                  width=22, command=self.flatten, state='disabled')
        self.flat_btn.pack(fill='x', pady=2)
        r2 = tk.Frame(ctl, bg=PANEL); r2.pack(fill='x', pady=(8, 0))
        ttk.Button(r2, text='EXPORT LAST SESSION', command=self.export_last).pack(side='left')
        ttk.Button(r2, text='OPEN EXPORTS', command=lambda: self.open_folder(ops.export_dir(self.settings))).pack(side='left', padx=4)
        ttk.Button(ctl, text='OPEN DATA FOLDER', command=lambda: self.open_folder(ops.data_dir(self.settings))).pack(fill='x', pady=(4, 0))

        # --- 3 real results
        res = tk.LabelFrame(top, text='3  REAL MONEY — from your Kalshi journal', bg=PANEL, font=('Segoe UI', 10, 'bold'), padx=10, pady=6)
        res.pack(side='left', fill='both', expand=True, padx=(6, 0))
        self.res = {}
        for key, name in (('today', 'Today (UTC day) P/L'), ('trades', 'Trades today'), ('position', 'Open position'),
                          ('orders', 'Orders today'), ('cash', f'{SYM} shard cash'), ('alltime', 'All recorded trades'),
                          ('pause', 'New entries')):
            f = tk.Frame(res, bg=PANEL); f.pack(fill='x', pady=1)
            tk.Label(f, text=name, bg=PANEL, font=('Segoe UI', 9), width=20, anchor='w').pack(side='left')
            v = tk.Label(f, text='—', bg=PANEL, font=('Segoe UI', 10, 'bold'), anchor='w', justify='left', wraplength=330)
            v.pack(side='left', fill='x')
            self.res[key] = v

        # --- V1.9.3 market mood light
        md = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground='#cfcfcf')
        md.pack(fill='x', pady=(10, 0))
        self.mood_dot = tk.Label(md, text='\u25cf', fg=mood.GREY, bg=PANEL, font=('Segoe UI', 26))
        self.mood_dot.pack(side='left', padx=(10, 6))
        mtxt = tk.Frame(md, bg=PANEL); mtxt.pack(side='left', fill='x', expand=True, pady=4)
        self.mood_head = tk.Label(mtxt, text='MARKET MOOD: starting\u2026', bg=PANEL, fg=mood.GREY, font=('Segoe UI', 13, 'bold'), anchor='w')
        self.mood_head.pack(fill='x')
        self.mood_lines = tk.Label(mtxt, text='', bg=PANEL, font=('Segoe UI', 9), anchor='w', justify='left')
        self.mood_lines.pack(fill='x')

        # --- market feed
        mf = ttk.LabelFrame(body, text=f'LIVE {asset.NAME.upper()} / KALSHI {asset.SERIES} 15-MIN MARKET (observed quotes)', padding=6)
        mf.pack(fill='x', pady=(10, 4))
        cols = (('side', 'Side', 60), ('eth', f'{SYM} $', 110), ('strike', 'Strike $', 110), ('gap', f'{SYM} − strike', 110),
                ('left', 'Time left', 80), ('ask', 'Ask', 70), ('bid', 'Bid', 70), ('depth', 'Ask depth', 80),
                ('scaled', 'Scaled gap', 85), ('hist', 'History', 70), ('stamp', 'Quote time', 110))
        self.feed = ttk.Treeview(mf, columns=[c[0] for c in cols], show='headings', height=2, selectmode='none')
        for c, t, w in cols:
            self.feed.heading(c, text=t); self.feed.column(c, width=w, anchor='center', stretch=True)
        self.feed.pack(fill='x')
        self.feed_note = ttk.Label(mf, text='No session running.', foreground='#585858'); self.feed_note.pack(anchor='w')

        # --- tabs
        nb = ttk.Notebook(body); nb.pack(fill='both', expand=True, pady=(6, 0))
        self.activity = self._textbox(nb, 'WHAT THE BOT IS DOING', bg='#fffde3')
        trades = ttk.Frame(nb, padding=4); nb.add(trades, text='REAL TRADES')
        tcols = (('closed', 'Closed', 130), ('market', 'Market', 140), ('side', 'Side', 50), ('qty', 'Qty', 45),
                 ('entry', 'Entry ¢', 70), ('cost', 'Cost $', 80), ('result', 'Result', 110), ('pnl', 'P/L $', 80))
        self.trades = ttk.Treeview(trades, columns=[c[0] for c in tcols], show='headings')
        for c, t, w in tcols:
            self.trades.heading(c, text=t); self.trades.column(c, width=w, anchor='center')
        self.trades.tag_configure('win', foreground='#146c2e'); self.trades.tag_configure('loss', foreground='#b3141f')
        self.trades.pack(fill='both', expand=True)
        pvr = ttk.Frame(nb, padding=6); nb.add(pvr, text='PAPER vs REAL (this session)')
        self.pvr = {}
        for kind, label in (('paper', 'PAPER — what the model wanted (settlement-only)'), ('real', 'REAL — Kalshi fills and P/L')):
            fr = ttk.LabelFrame(pvr, text=label, padding=6); fr.pack(side='left', fill='both', expand=True, padx=4)
            tv = ttk.Treeview(fr, columns=('k', 'v'), show='headings', height=8, selectmode='none')
            tv.heading('k', text='Measure'); tv.heading('v', text='Value')
            tv.column('k', width=230, anchor='w'); tv.column('v', width=120, anchor='center')
            tv.pack(fill='both', expand=True); self.pvr[kind] = tv
        self.tech = self._textbox(nb, 'TECHNICAL LOG', bg='#f4f6f8', font=('Consolas', 8))
        ttk.Label(self, textvariable=self.status, padding=(14, 4)).pack(fill='x', side='bottom')

    def _textbox(self, nb, title, bg, font=('Consolas', 9)):
        fr = ttk.Frame(nb); nb.add(fr, text=title)
        t = tk.Text(fr, bg=bg, font=font, wrap='word', state='disabled', relief='flat')
        sb = ttk.Scrollbar(fr, orient='vertical', command=t.yview); t.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y'); t.pack(fill='both', expand=True)
        return t

    def _append(self, box, text, limit=4000):
        box.configure(state='normal')
        box.insert('end', text + '\n')
        n = int(box.index('end-1c').split('.')[0])
        if n > limit:
            box.delete('1.0', f'{n - limit}.0')
        box.see('end'); box.configure(state='disabled')

    def say(self, text):
        self._append(self.activity, datetime.now().strftime('%I:%M:%S %p ') + text)

    # ================================================================== mode
    def _paint_mode(self):
        live = self.mode == 'LIVE'
        bg = LIVE_BG if live else PAPER_BG
        for w in (self.header, self.h_title, self.h_desc, self.h_note, self.h_row, self.h_title.master):
            w.configure(bg=bg)
        self.h_title.configure(text=f'{TITLE}  |  {"LIVE TRADING — REAL MONEY" if live else "PAPER TRADING"}')
        self.h_desc.configure(text=(f'ORANGE = ETH LIVE • Real Kalshi orders • max ${self.usd()} per trade • 1 loss pauses (override available)' if live
                                    else 'PURPLE = ETH PAPER • Watches the market and records signals • no orders sent'))
        self.h_note.configure(text=('Live orders ENABLED for this window.' if live else 'Real-money orders disabled.'),
                              fg='#ffe3cc' if live else '#e6ddff')
        self.paper_btn.configure(relief='raised' if live else 'sunken', bg='#5b6770' if live else PAPER_BTN)
        self.live_btn.configure(relief='sunken' if live else 'raised', bg=LIVE_BTN if live else '#5b6770')
        self.start_btn.configure(text='▶ START LIVE' if live else '▶ START PAPER')
        self._update_buttons()

    def select_paper(self):
        if self.attached:
            messagebox.showinfo('Session running', 'Stop the running session before switching mode.'); return
        self.mode = 'PAPER'; self._paint_mode(); self.say('PAPER mode selected — no real orders.')
        self.run_checks()

    def select_live(self):
        if self.attached:
            messagebox.showinfo('Session running', 'Stop the running session before switching mode.'); return
        word = simpledialog.askstring('Turn on LIVE trading',
                                      'LIVE uses real money on Kalshi.\n\n'
                                      f'• Max ${self.usd()} per trade, one position at a time\n'
                                      f'• A stop is a limit order and can fail to fill — up to ${self.usd()} can be lost\n'
                                      '• One losing trade stops new entries until 7 PM New York time\n\n'
                                      'Type LIVE to continue:', parent=self)
        if (word or '').strip().upper() != 'LIVE':
            self.say('LIVE not turned on.'); return
        self.mode = 'LIVE'; self._paint_mode(); self.say('LIVE mode turned on for this window.')
        self.run_checks()

    # ================================================================== background work
    def work(self, fn, done=None, label=None):
        if label:
            self.status.set(label)

        def run():
            try:
                r = fn()
                self.q.put((done, r, None))
            except Exception as e:
                self.q.put((done, None, e))
                self.q.put((None, None, ('trace', traceback.format_exc())))
        threading.Thread(target=run, daemon=True).start()

    def tick(self):
        try:
            while True:
                done, r, err = self.q.get_nowait()
                if isinstance(err, tuple) and err[0] == 'trace':
                    self._append(self.tech, err[1]); continue
                if done:
                    try:
                        done(r, err)
                    except Exception:
                        self._append(self.tech, traceback.format_exc())
        except queue.Empty:
            pass
        try:
            self.tail_log()
            now = time.time()
            if now - getattr(self, '_last_slow', 0) >= 2.5:
                self._last_slow = now
                self.refresh_feed(); self.refresh_results(); self.watch_engine()
        except Exception:
            self._append(self.tech, traceback.format_exc())
        self.after(700, self.tick)

    # ================================================================== first run / settings
    def first_run(self):
        ok, _ = ops.apply_key_env(self.settings)
        if not ok and not self.settings.get('key_path'):
            messagebox.showinfo('One-time setup', 'Welcome to BIGBET Live Studio V12.\n\nOne-time setup: tell the app where your '
                                'Kalshi key file is and your key ID. Only the file LOCATION is saved, never the key itself.')
            self.open_settings(first=True)
        rt = ops.runtime_dir(ENV, self.settings)
        if not (rt / 're5_journal.json').exists() and not self.settings.get('import_declined'):
            olds = ops.find_old_runtimes()
            if olds:
                src = olds[0]
                if messagebox.askyesno('Bring over your trade history?',
                                       f'Found your journal from an earlier revision:\n\n{src}\n\n'
                                       f'Copy it into the new permanent data folder?\n{rt}\n\n'
                                       '(Keeps today\'s P/L, loss counter and trade history. After this you never copy folders again.)'):
                    try:
                        try:
                            files = ops.import_runtime(src, ENV, self.settings)
                        except ops.LockedOldRuntime:
                            if not messagebox.askyesno('Old bot finished?',
                                                       'The old revision left a RUNNING.lock (normal after it stopped with an error).\n\n'
                                                       'Is the old PowerShell bot window CLOSED or showing a PS> prompt (not running)?'):
                                raise RuntimeError('Not imported: let the old session finish first, then reopen this app.')
                            files = ops.import_runtime(src, ENV, self.settings, old_window_closed=True)
                        self.say('Imported from earlier revision: ' + ', '.join(files))
                    except Exception as e:
                        messagebox.showwarning('Not imported', str(e))
                else:
                    self.settings['import_declined'] = True; ops.save_settings(self.settings)
        self.say(f'App ready. Data folder: {ops.data_dir(self.settings)}')
        self.reattach_if_running()
        self.run_checks()

    def open_settings(self, first=False):
        w = tk.Toplevel(self); w.title('Settings'); w.transient(self); w.grab_set(); w.resizable(False, False)
        frm = ttk.Frame(w, padding=14); frm.pack(fill='both')
        kid = tk.StringVar(value=self.settings.get('key_id') or os.environ.get('KALSHI_API_KEY_ID', ''))
        kpath = tk.StringVar(value=self.settings.get('key_path') or os.environ.get('KALSHI_PRIVATE_KEY_PATH', ''))
        ttk.Label(frm, text='Kalshi API key ID').grid(row=0, column=0, sticky='w')
        e = ttk.Entry(frm, textvariable=kid, width=48, show='•'); e.grid(row=0, column=1, sticky='w', pady=4)
        show = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text='show', variable=show, command=lambda: e.configure(show='' if show.get() else '•')).grid(row=0, column=2)
        ttk.Label(frm, text='Private key FILE (.pem / .txt)').grid(row=1, column=0, sticky='w')
        ttk.Entry(frm, textvariable=kpath, width=48).grid(row=1, column=1, sticky='w', pady=4)

        def browse():
            p = filedialog.askopenfilename(parent=w, title='Choose your Kalshi private key file')
            if p:
                kpath.set(p)
        ttk.Button(frm, text='Browse…', command=browse).grid(row=1, column=2, padx=4)
        ttk.Label(frm, text=f'Data folder (journal, logs, exports): {ops.data_dir(self.settings)}', foreground='#555').grid(
            row=2, column=0, columnspan=3, sticky='w', pady=(8, 0))
        ttk.Label(frm, text='Saved: key ID and the key file LOCATION only. The key contents are never copied, shown or exported.',
                  foreground='#1d5f32').grid(row=3, column=0, columnspan=3, sticky='w', pady=(4, 8))

        def do_import():
            olds = ops.find_old_runtimes()
            start = str(olds[0]) if olds else str(ROOT.parent)
            p = filedialog.askdirectory(parent=w, title='Choose the old PRIVATE_RUNTIME\\RE5_prod folder', initialdir=start)
            if p:
                try:
                    try:
                        files = ops.import_runtime(p, ENV, self.settings)
                    except ops.LockedOldRuntime:
                        if not messagebox.askyesno('Old bot finished?', 'Is the old bot window closed (not running)?', parent=w):
                            return
                        files = ops.import_runtime(p, ENV, self.settings, old_window_closed=True)
                    messagebox.showinfo('Imported', 'Copied: ' + ', '.join(files), parent=w)
                except Exception as ex:
                    messagebox.showwarning('Not imported', str(ex), parent=w)
        ttk.Button(frm, text='Import journal from an earlier revision…', command=do_import).grid(row=4, column=0, columnspan=2, sticky='w')

        def save():
            self.settings['key_id'] = kid.get().strip()
            self.settings['key_path'] = kpath.get().strip().strip('"')
            ops.save_settings(self.settings)
            ok, msg = ops.apply_key_env(self.settings)
            if not ok:
                if not messagebox.askyesno('Key problem', msg + '\n\nSave anyway?', parent=w):
                    return
            w.destroy(); self.say('Settings saved (' + msg + ')'); self.run_checks()
        ttk.Button(frm, text='SAVE', command=save).grid(row=5, column=1, sticky='e', pady=(10, 0))
        ttk.Button(frm, text='Cancel', command=w.destroy).grid(row=5, column=2, pady=(10, 0))
        if first:
            self.wait_window(w)

    # ================================================================== readiness checklist
    def set_check(self, key, color, text, button=None, action=None):
        c = self.checks[key]
        c['dot'].configure(fg=color); c['txt'].configure(text=text)
        self.check_results[key] = color
        if button:
            c['btn'].configure(text=button, command=action); c['btn'].pack(side='right')
        else:
            c['btn'].pack_forget()

    def code_hash(self):
        h = hashlib.sha256()
        for p in sorted(list(ROOT.glob('*.py')) + list((ROOT / 'RE5').rglob('*.py'))):
            if '__pycache__' not in p.parts:
                h.update(p.name.encode()); h.update(p.read_bytes())
        return h.hexdigest()[:16]

    def run_checks(self):
        if self.busy:
            return
        self.busy = True; self._update_buttons()
        for k in self.checks:
            self.set_check(k, GRAY, 'checking…')
        live = self.mode == 'LIVE'
        settings = dict(self.settings)
        usd = self.usd()

        def job():
            out = {}
            try:
                out['ladder'] = ops.ladder_info(ENV, settings)
            except Exception as e:
                out['ladder'] = dict(error=f'{type(e).__name__}: {e}')
            ok, msg = ops.apply_key_env(settings)
            out['key'] = (ok, msg)
            h = self.code_hash()
            if settings.get('selftest_ok') == h:
                out['tests'] = (True, 'passed (this version already tested)', h)
            else:
                fails = []
                for t in TEST_FILES:
                    r = subprocess.run([sys.executable, str(ROOT / t)], cwd=str(ROOT), capture_output=True, text=True,
                                       encoding='utf-8', errors='replace', timeout=300,
                                       env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                                       creationflags=(0x08000000 if os.name == 'nt' else 0))
                    if r.returncode != 0:
                        fails.append(t + ':\n' + (r.stderr or r.stdout)[-1500:])
                out['tests'] = (not fails, 'all engine tests passed' if not fails else f'{len(fails)} test file(s) FAILED', h, fails)
            if ok:
                try:
                    out['account'] = ops.check_account(ENV)
                except Exception as e:
                    out['account'] = dict(ok=False, state=None, engine='?', actions=[], message=f'{type(e).__name__}: {e}')
                try:
                    out['cash'] = ops.topup_plan(ENV, trade_usd=usd)
                except Exception as e:
                    out['cash'] = dict(action='error', message=f'could not read balances: {type(e).__name__}: {e}')
            prt = ops.runtime_dir('paper', settings)
            if ops.engine_state(prt)[0] == 'stopped':          # paper holds no money: just clear its old lock
                (prt / 'RUNNING.lock').unlink(missing_ok=True)
            out['eng_live'] = ops.engine_state(ops.runtime_dir(ENV, settings))[0]
            out['eng_paper'] = ops.engine_state(prt)[0]
            try:
                out['old_running'] = [str(x) for x in ops.find_old_runtimes() if (x / 'RUNNING.lock').exists()]
            except Exception:
                out['old_running'] = []
            return out
        self.work(job, self._checks_done, 'Running ready check…')

    def _checks_done(self, out, err):
        self.busy = False
        if err:
            self.set_check('engine', RED, f'check failed: {err}'); self._update_buttons(); return
        ok, msg = out['key']
        self.set_check('key', GREEN if ok else RED, msg, None if ok else 'Fix', None if ok else self.open_settings)
        t = out['tests']
        if t[0]:
            self.settings['selftest_ok'] = t[2]; ops.save_settings(self.settings)
            self.set_check('tests', GREEN, t[1])
        else:
            self.set_check('tests', RED, t[1] + ' — send me the Technical log', 'Retry', self.run_checks)
            for f in t[3]:
                self._append(self.tech, 'SELF-TEST FAILURE ' + f)
        acc = out.get('account')
        if not ok:
            for k in ('account', 'cash', 'entries'):
                self.set_check(k, GRAY, 'needs the Kalshi key first')
        elif acc:
            for a in acc.get('actions', []):
                self.say('Auto-fixed: ' + a)
            if acc.get('engine') in ('running', 'not_responding'):
                self.set_check('account', GREEN, 'engine is running (checked at its start)')
                self.set_check('entries', GREEN, 'managed by the running engine')
            elif acc['ok']:
                st = acc['state']
                pos = st['position']
                self.set_check('account', GREEN, acc['message'] + (f" — {pos['held'].upper()} {pos['qty'] - pos['exited_qty']} in {short_market(pos['ticker'])}" if pos else ''))
                hk = ops.halt_kind(st)
                if hk == 'daily_loss' or (not hk and ops.guard_blocked(ENV)):
                    self.set_check('entries', RED, 'Daily loss stop reached — resumes automatically after 7 PM New York time (or midnight for the 2nd guard)',
                                   'Override: trade today', self.override_daily)
                elif hk:
                    self.set_check('entries', RED, 'Paused: ' + st['reason'][:110], 'Clear pause', self.clear_pause)
                else:
                    self.set_check('entries', GREEN, f"yes — today {money(st['realized_usd'])}, losses {st['loss_count']}/1")
            else:
                self.set_check('account', RED, acc['message'][:160], 'Retry', self.run_checks)
                self.set_check('entries', RED, 'blocked until the account check passes')
        cash = out.get('cash')
        if cash:
            a = cash.get('action')
            color = {'none': GREEN, 'transfer': YELLOW, 'blocked': RED}.get(a, RED)
            self.set_check('cash', color, cash['message'])
            if 'cash' in cash:
                self.last_cash = (cash.get('shard'), cash['cash']); self.last_cash_at = time.time()
        self._show_ladder(out.get('ladder'))
        el, ep = out['eng_live'], out['eng_paper']
        running = [n for n, s in (('LIVE', el), ('PAPER', ep)) if s in ('running', 'not_responding')]
        if out.get('old_running') and not running:
            old = out['old_running'][0]
            self.set_check('engine', RED, f'An OLDER revision may still be running (lock in {old}). Let it finish first.',
                           'It is not running', lambda: self.clear_old_lock(old))
        elif running:
            self.set_check('engine', GREEN, f'{running[0]} session running' + (' (not responding!)' if 'not_responding' in (el, ep) else ''))
        elif el == 'stopped' or ep == 'stopped':
            self.set_check('engine', YELLOW, 'previous session ended without cleanup — Check again clears it once the account matches')
        else:
            self.set_check('engine', GREEN, 'ready (no session running)')
        self.status.set('Ready check finished ' + datetime.now().strftime('%I:%M:%S %p'))
        self._update_buttons()

    def ready_to_start(self):
        need = ['key', 'tests', 'engine'] + (['account', 'cash', 'entries', 'ladder'] if self.mode == 'LIVE' else [])
        bad = [k for k in need if self.check_results.get(k) not in (GREEN, YELLOW)]
        if self.mode == 'LIVE' and self.check_results.get('engine') == YELLOW:
            bad.append('engine')
        return not bad and not self.busy and not self.attached

    def _update_buttons(self):
        ok = self.ready_to_start()
        self.start_btn.configure(state='normal' if ok else 'disabled',
                                 bg=(('#b3141f' if self.mode == 'LIVE' else GREEN) if ok else '#c9cfd4'))
        run = bool(self.attached)
        self.stop_btn.configure(state='normal' if run else 'disabled', bg='#f0c040' if run else '#e3e6e8')
        fl = run and self.attached.get('live')
        self.flat_btn.configure(state='normal' if fl else 'disabled', bg='#8b1a1a' if fl else '#c9cfd4')
        self.recheck_btn.configure(state='disabled' if self.busy else 'normal')
        self.live_btn.configure(state='disabled' if run else 'normal'); self.paper_btn.configure(state='disabled' if run else 'normal')
        self.size_box.configure(state='disabled' if run else 'readonly')

    def clear_old_lock(self, folder):
        if messagebox.askyesno('Older revision', f'Is the old bot window CLOSED and finished?\n\n{folder}\n\n'
                               'Only say Yes if no old PowerShell bot window is open. The app then marks that old lock as finished '
                               '(it is renamed, not deleted).'):
            lk = Path(folder) / 'RUNNING.lock'
            if lk.exists():
                lk.rename(lk.with_name('RUNNING.lock.cleared_by_V12'))
            self.say('Old revision lock marked finished: ' + folder)
            self.run_checks()

    def clear_pause(self):
        if not messagebox.askyesno('Clear pause?', 'New entries are paused because of an earlier problem (see the line).\n\n'
                                   'Only clear it if you have looked at your Kalshi account and nothing is open or wrong.\n\n'
                                   'The app checks again that the account is flat and matches before clearing. Clear now?'):
            return
        self.work(lambda: ops.clear_halt(ENV), lambda r, e: (self.say(('Pause cleared. ' if r and r[0] else 'Not cleared: ') +
                                                                      (r[1] if r else str(e))), self.run_checks()))

    def override_daily(self):
        if not messagebox.askyesno('Override the daily stop?',
                                   'ETH trading paused because of a loss.\n\n'
                                   'Override = keep trading now. Rules:\n'
                                   '  • you can override after every loss (your choice each time)\n'
                                   '  • every new loss pauses trading again until you override again\n'
                                   '  • the weekly stop and the size ladder still apply\n\n'
                                   'Best practice: send the export to Claude first.\n\nOverride now?'):
            return
        self.work(lambda: ops.override_daily_stop(ENV), lambda r, e: (self.say(('Override done. ' if r and r[0] else 'Not overridden: ') +
                                                                              (r[1] if r else str(e))), self.run_checks()))

    # ================================================================== V12.3 size ladder
    def usd(self):
        try:
            return scaling.check_size(self.size.get().strip().lstrip('$'))
        except Exception:
            return scaling.BASE_USD

    def _size_changed(self):
        self.settings['trade_usd'] = self.usd(); ops.save_settings(self.settings)
        self._paint_mode()
        self.say(f'Trade size set to ${self.usd()} per trade.' + (' (News blackout and weekly stop are ON at this size.)'
                                                                 if self.usd() > scaling.BASE_USD else ''))
        self.run_checks()

    def _show_ladder(self, st):
        self.ladder = st
        if not st or st.get('error'):
            self.set_check('ladder', RED, 'ladder files unreadable: ' + str((st or {}).get('error', '?'))[:120]); return
        allowed = st['allowed']
        if st.get('stepped_down'):
            a, b = st['stepped_down']
            self.say(f'SIZE LADDER stepped DOWN from ${a} to ${b} (2 losses within the last {scaling.LADDER_STEP_TRADES} trades).')
        want = self.settings.get('trade_usd', scaling.BASE_USD)
        if want not in allowed:
            want = max(allowed)
        self.size_box.configure(values=[f'${u}' for u in allowed])
        if not self.attached:
            self.size.set(f'${want}')
        usd = self.usd()
        nxt = st['next']
        progress = (f"Free size choice ${min(scaling.SIZES)}-${max(scaling.SIZES)} (no ladder lock) · {st['total_trades']} trades recorded · "
                    f"{st['losses_recent']} loss in last {scaling.LADDER_STEP_TRADES} · {st['real_stops']} real stop")
        ev = scaling.next_events(1)
        news = ('next news skip: ' + f"{ev[0][1]} {scaling.utc_to_et(ev[0][0]).strftime('%a %b %d %I:%M %p')}") if ev else 'news list EMPTY'
        news += '' if usd > scaling.BASE_USD else ' (logged only at $10 or less)'
        last = scaling.calendar_last_date()
        stale = last is None or last.timestamp() < time.time() + 14 * 86400
        blocked, wmsg = scaling.weekly_check(ops.runtime_dir(ENV, self.settings), usd, journal_history=st.get('_history'))
        color = RED if blocked else (YELLOW if stale else GREEN)
        text = progress + '\n' + wmsg + '\n' + news + ('\nNews list runs out soon — ask Claude for new dates' if stale else '')
        self.set_check('ladder', color, text, 'Release weekly stop' if blocked else None, self.release_weekly if blocked else None)
        self.unlock_btn.configure(state='disabled', text='No ladder')

    def unlock_size(self):
        st = self.ladder or {}
        if not st.get('eligible'):
            return
        nxt = st['next']
        word = simpledialog.askstring('Unlock bigger size',
                                      f"The bot earned the next step: {st['trades_at_size']} trades at ${st['unlocked']}, "
                                      f"{st['losses_recent']} loss, {st['real_stops']} real stop.\n\n"
                                      f'Unlocking lets you CHOOSE ${nxt} per trade (it does not switch by itself).\n'
                                      f'One loss at ${nxt} costs about ${nxt}.\n\nType UNLOCK {nxt} to continue:', parent=self)
        if (word or '').strip().upper() != f'UNLOCK {nxt}':
            self.say('Size not unlocked.'); return
        try:
            new = ops.ladder_unlock(ENV, self.settings)
            self.say(f'${new} per trade unlocked. Pick it in "Per trade" when you want to use it.')
        except Exception as e:
            messagebox.showerror('Not unlocked', str(e))
        self.run_checks()

    def release_weekly(self):
        if not messagebox.askyesno('Release weekly stop?',
                                   'The weekly loss stop fired: the last 7 days lost more than one trade size.\n\n'
                                   'Only release it after reviewing the trades (send the export to Claude first).\n\n'
                                   'Release now? Losses after this moment count toward a new stop.'):
            return
        ops.ladder_release_weekly(ENV, self.settings)
        self.say('Weekly stop released by operator.'); self.run_checks()

    # ================================================================== start / stop
    def start(self):
        if not self.ready_to_start():
            return
        minutes = DURATIONS[self.duration.get()]
        live = self.mode == 'LIVE'
        usd = self.usd()
        tmin, tamt, tcap = scaling.topup_params(usd)
        if live and not messagebox.askyesno('Start LIVE', f'Start LIVE trading for {self.duration.get()}?\n\n'
                                            f'Real money: max ${usd} per trade. The app moves ${tamt} to the {SYM} shard first if it is '
                                            f'below ${tmin} (at most ${tcap} per day).'):
            return
        if live and usd > scaling.BASE_USD:
            word = simpledialog.askstring('Bigger size', f'This session trades ${usd} per trade.\n\n'
                                          f'One loss can cost about ${usd}. Daily loss cap ${scaling.daily_loss_cap(usd)}.\n'
                                          'News blackout and weekly loss stop are ON at this size.\n\n'
                                          f'Type {usd} to confirm:', parent=self)
            if (word or '').strip().lstrip('$') != str(usd):
                self.say('Not started: size not confirmed.'); return
        self.busy = True; self._update_buttons()

        def job():
            msgs = []
            if live:
                t = ops.topup_if_needed(ENV, trade_usd=usd)
                msgs.append(t['message'])
                if not t['ok']:
                    return dict(started=False, msgs=msgs)
            s = ops.start_session(ENV if live else 'paper', minutes, settings=self.settings, live=live, trade_usd=usd)
            return dict(started=True, msgs=msgs, sess=s)
        self.work(job, self._started, 'Starting…')

    def _started(self, r, err):
        self.busy = False
        if err:
            messagebox.showerror('Not started', f'{type(err).__name__}: {err}'); self.say(f'Not started: {err}')
            self.run_checks(); return
        for m in r['msgs']:
            self.say('Shard cash: ' + m)
        if not r['started']:
            messagebox.showwarning('Not started', '\n'.join(r['msgs'])); self.run_checks(); return
        s = r['sess']
        if self.mode == 'LIVE':
            self.plan = dict(end=time.time() + DURATIONS[self.duration.get()] * 60, usd=self.usd(), restarts=0)
        else:
            self.plan = None
        self.attach(dict(rt=s['rt'], session=s['session'], stamp=s['stamp'], live=self.mode == 'LIVE', proc=s['proc']))
        self.say(f'{"LIVE" if self.mode == "LIVE" else "PAPER"} session {s["stamp"]} started for {self.duration.get()} at ${self.usd()} per trade.')

    def attach(self, a):
        self.attached = a; self.log_pos = 0; self.finishing = False
        self.set_check('engine', GREEN, f'{"LIVE" if a["live"] else "PAPER"} session {a["stamp"]} running')
        self.engine_pill.configure(text=f'ENGINE: RUNNING {"LIVE" if a["live"] else "PAPER"}', bg=('#b3141f' if a['live'] else GREEN))
        self._update_buttons()

    def reattach_if_running(self):
        for env, live in ((ENV, True), ('paper', False)):
            rt = ops.runtime_dir(env, self.settings)
            st, lock = ops.engine_state(rt)
            if st in ('running', 'not_responding') and lock:
                self.mode = 'LIVE' if live else 'PAPER'
                self.size.set(f"${lock.get('trade_usd', scaling.BASE_USD)}"); self._paint_mode()
                self.attach(dict(rt=rt, session=rt / 'sessions' / lock['stamp'], stamp=lock['stamp'], live=live, proc=None))
                self.say(f'Reattached to the {self.mode} session {lock["stamp"]} that kept running while the app was closed.')
                return

    def stop(self):
        a = self.attached
        if not a:
            return
        if a['live']:
            if messagebox.askyesno('Stop new trades', 'Stop taking new trades?\n\nIf a position is open, the engine keeps protecting '
                                   'it (stop rules still active) until it is sold or settles, then shuts down by itself.'):
                ops.request_stop(a['rt']); self.plan = None
                self.say('Stop requested — no new trades. Engine ends once flat. (Auto-restart off for this session.)')
        else:
            ops.stop_paper(a['rt']); self.say('Paper session stopped.')

    def flatten(self):
        a = self.attached
        if not a or not a['live']:
            return
        if messagebox.askyesno('EMERGENCY SELL', 'Sell the open position NOW at the best available price and stop new trades?\n\n'
                               'Use this only if something looks wrong. Selling early can lock in a loss.', icon='warning'):
            ops.request_flatten(a['rt']); self.plan = None
            self.say('EMERGENCY SELL requested — selling the open position and stopping new trades. (Auto-restart off.)')

    def watch_engine(self):
        a = self.attached
        if not a or self.finishing:
            if not a:
                self.engine_pill.configure(text='ENGINE: STOPPED', bg='#3b4650')
            return
        if a.get('proc') is not None and a['proc'].poll() is None:
            st = 'running'
        else:
            st, _ = ops.engine_state(a['rt'])
        if st == 'not_responding':
            self.engine_pill.configure(text='ENGINE: NOT RESPONDING', bg=YELLOW)
        elif st == 'running':
            self.engine_pill.configure(text=f'ENGINE: RUNNING {"LIVE" if a["live"] else "PAPER"}', bg=('#b3141f' if a['live'] else GREEN))
        else:
            self.finish_session()

    def finish_session(self):
        a = self.attached; self.finishing = True
        self.tail_log()
        self.say('Engine stopped. Checking the account and exporting the session…')

        def job():
            out = {}
            if a['live']:
                out['acc'] = ops.check_account(ENV)
            else:
                (a['rt'] / 'RUNNING.lock').unlink(missing_ok=True)
            try:
                out['zip'] = ops.export_session(a['rt'], a['session'], key_id=os.environ.get('KALSHI_API_KEY_ID', ''),
                                                out_dir=ops.export_dir(self.settings))
            except Exception as e:
                out['zip_err'] = str(e)
            return out

        def done(r, err):
            self.attached = None; self.finishing = False
            self.engine_pill.configure(text='ENGINE: STOPPED', bg='#3b4650')
            if err:
                self.say(f'After-session step failed: {err}')
            else:
                if r.get('acc'):
                    for x in r['acc']['actions']:
                        self.say('Auto-fixed: ' + x)
                    self.say(('Account check after session: ' + r['acc']['message']) if r['acc']['ok'] else
                             '⚠ Account check after session FAILED: ' + r['acc']['message'])
                if r.get('zip'):
                    self.say(f'Session exported: {r["zip"]}  ← send me this file')
                if r.get('zip_err'):
                    self.say('Export problem: ' + r['zip_err'])
            self.run_checks()
            if a['live'] and not err:
                self.consider_auto_restart(r.get('acc'))
        self.work(job, done, 'Finishing session…')

    # ================================================================== V12.7 auto-restart
    def _plan_minutes_left(self):
        return 0 if not self.plan else int((self.plan['end'] - time.time()) // 60)

    def consider_auto_restart(self, acc):
        """Only after a LIVE session a person started, stopped by a Kalshi CONNECTION problem, while time is left."""
        p = self.plan
        if not p:
            return
        st = (acc or {}).get('state') or {}
        why = (st.get('reason') or '') + ' ' + ((acc or {}).get('message') or '')
        if not ops.is_connection_problem(why):
            self.plan = None; return                      # normal end, stop button, loss limit, mismatch: never auto-restart
        if p['restarts'] >= ops.AUTO_RESTART_MAX:
            self.plan = None
            self.say(f'Auto-restart: already restarted {ops.AUTO_RESTART_MAX} times this session — leaving it stopped. Check the internet, then start by hand.')
            return
        if self._plan_minutes_left() < ops.AUTO_RESTART_MIN_MINUTES:
            self.plan = None; self.say('Auto-restart: less than 15 minutes of the planned session left — not restarting.'); return
        self.say(f'Auto-restart: stopped by a Kalshi connection problem. Will check again in {ops.AUTO_RESTART_RETRY_S} s '
                 f'(restart {p["restarts"] + 1} of {ops.AUTO_RESTART_MAX}; only if flat and the account matches).')
        self.after(ops.AUTO_RESTART_RETRY_S * 1000, self.try_auto_restart)

    def try_auto_restart(self):
        p = self.plan
        if not p or self.attached or self.busy or self.mode != 'LIVE':
            return
        if self._plan_minutes_left() < ops.AUTO_RESTART_MIN_MINUTES:
            self.plan = None; self.say('Auto-restart: planned session time is over — not restarting.'); return

        def job():
            verdict, msg = ops.auto_restart_prepare(ENV, settings=self.settings)
            if verdict != 'go':
                return dict(verdict=verdict, msg=msg)
            t = ops.topup_if_needed(ENV, trade_usd=p['usd'])
            if not t['ok']:
                return dict(verdict='never', msg='shard cash: ' + t['message'])
            s = ops.start_session(ENV, self._plan_minutes_left(), settings=self.settings, live=True, trade_usd=p['usd'],
                                  glitch_at=time.time())   # the market open right now had the outage: no entry in it
            return dict(verdict='go', msg=msg, sess=s, topup=t['message'])

        def done(r, err):
            self.busy = False
            if err:
                if ops.is_connection_problem(f'{type(err).__name__}: {err}'):
                    self.say(f'Auto-restart: Kalshi still unreachable ({type(err).__name__}); trying again in {ops.AUTO_RESTART_RETRY_S} s.')
                    self.after(ops.AUTO_RESTART_RETRY_S * 1000, self.try_auto_restart); return
                self.plan = None; self.say(f'Auto-restart stopped: {type(err).__name__}: {err}'); self.run_checks(); return
            if r['verdict'] == 'wait':
                self.say(f'Auto-restart: {r["msg"]} — trying again in {ops.AUTO_RESTART_RETRY_S} s.')
                self.after(ops.AUTO_RESTART_RETRY_S * 1000, self.try_auto_restart); return
            if r['verdict'] == 'never':
                self.plan = None; self.say(f'Auto-restart NOT done: {r["msg"]}. Start by hand when ready.'); self.run_checks(); return
            p['restarts'] += 1
            s = r['sess']
            self.say('Shard cash: ' + r['topup'])
            self.attach(dict(rt=s['rt'], session=s['session'], stamp=s['stamp'], live=True, proc=s['proc']))
            self.say(f'AUTO-RESTARTED LIVE session {s["stamp"]} ({r["msg"]}) for the remaining {self._plan_minutes_left()} min at ${p["usd"]} per trade '
                     f'(restart {p["restarts"]} of {ops.AUTO_RESTART_MAX}).')
        self.busy = True
        self.work(job, done, 'Auto-restarting…')

    # ================================================================== live panels
    def tail_log(self):
        a = self.attached
        if not a:
            return
        p = a['session'] / 'collector_stdout.log'
        if not p.exists():
            return
        with open(p, 'r', encoding='utf-8', errors='replace') as f:
            if self.log_pos == 0:
                size = p.stat().st_size
                if size > 400_000:                     # reattach: only the recent part
                    f.seek(size - 400_000); f.readline()
            else:
                f.seek(self.log_pos)
            chunk = f.read(); self.log_pos = f.tell()
        recent = getattr(self, '_recent_plain', [])
        for line in chunk.splitlines():
            if not line.strip():
                continue
            self._append(self.tech, line)
            t = plain(line)
            if t and t not in recent:
                self.say(t)
                recent = (recent + [t])[-6:]
        self._recent_plain = recent

    # ================================================================== V1.9.3 market mood
    def _mood_loop(self):
        """Background thread: sample ETHUSD_RTI every 10 s (read-only, needs the Kalshi key). Display only."""
        client = None; last_save = 0
        while True:
            try:
                if client is None:
                    ok, msg = ops.apply_key_env(self.settings)
                    if not ok:
                        self._mood_err = 'needs your Kalshi key (Settings)'; time.sleep(30); continue
                    import importlib.util
                    spec = importlib.util.spec_from_file_location('eth_v37_mood', ROOT / 'RE5' / 'APP' / 'kalshi_eth_bot_v37.py')
                    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
                    client = mod.Client(auth=True)
                px, ts = client.ethrti()
                self._mood_samples = mood.trim(self._mood_samples + [(float(ts), float(px))])
                self._mood_err = ''
                if time.time() - last_save > 60:
                    mood.save(self._mood_path, self._mood_samples); last_save = time.time()
            except Exception as e:                     # a bad reading or a blip: just try again next time
                self._mood_err = f'price feed: {type(e).__name__}'
                if 'Stale or implausible' not in str(e):
                    client = None
            time.sleep(mood.SAMPLE_EVERY_S)

    def refresh_mood(self):
        try:
            m = mood.summary(list(self._mood_samples))
            self.mood_dot.configure(fg=m['color']); self.mood_head.configure(text=m['head'], fg=m['color'])
            extra = f"   (light paused: {self._mood_err})" if self._mood_err else ''
            self.mood_lines.configure(text=f"{m['prev']}   |   {m['current']}   |   {m['last_hour']}\n{m['advice']}{extra}")
        except Exception:
            self._append(self.tech, traceback.format_exc())
        self.after(5000, self.refresh_mood)

    def refresh_feed(self):
        a = self.attached
        if not a:
            if self.feed.get_children():
                self.feed.delete(*self.feed.get_children())
            self.feed_note.configure(text='No session running.')
            return
        p = a['session'] / 'APP' / 'eth_v42_market_quotes.csv'
        if not p.exists():
            self.feed_note.configure(text='Waiting for the first quotes…'); return
        try:
            with open(p, 'rb') as f:
                header = f.readline().decode('utf-8', 'replace').strip().split(',')
                f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 6000))
                lines = f.read().decode('utf-8', 'replace').splitlines()[1:]
            rows = [dict(zip(header, r)) for r in csv.reader(lines[-6:])]
        except Exception:
            return
        latest = {}
        for r in rows:
            if r.get('side') in ('yes', 'no'):
                latest[r['side']] = r
        self.feed.delete(*self.feed.get_children())
        for side in ('yes', 'no'):
            r = latest.get(side)
            if not r:
                continue
            try:
                left = float(r['seconds_left']); left_s = f'{int(left // 60)}:{int(left % 60):02d}'
            except Exception:
                left_s = r.get('seconds_left', '')
            stamp = r.get('time_utc', '')
            try:
                stamp = datetime.fromisoformat(stamp).astimezone().strftime('%I:%M:%S %p')
            except Exception:
                pass
            self.feed.insert('', 'end', values=(side.upper(), r.get('eth', ''), r.get('strike', ''), r.get('gap_signed', '')[:10],
                                                left_s, r.get('ask', '') or '—', r.get('bid', '') or '—', r.get('quoted_depth', ''),
                                                (r.get('scaled_gap', '') or '—')[:6], f"{r.get('minute_pairs', '?')}/8", stamp))
        tk_ = (latest.get('yes') or latest.get('no') or {}).get('ticker', '')
        self.feed_note.configure(text=f'Market {short_market(tk_)} ({tk_}). Ask = price to buy now; Bid = price you could sell at.')

    def refresh_results(self):
        rt = ops.runtime_dir(ENV, self.settings)
        jp = rt / 're5_journal.json'
        if not jp.exists():
            self.res['today'].configure(text='no live journal yet'); return
        try:
            sig = jp.stat().st_mtime
            s = json.loads(jp.read_text(encoding='utf-8'))
        except Exception:
            return
        st = s.get('stats', {})
        today = [h for h in s.get('history', []) if str(h.get('closed_day')) == s.get('day')]
        r = D(s.get('realized_usd', 0))
        self.res['today'].configure(text=money(r), fg=('#146c2e' if r > 0 else '#b3141f' if r < 0 else 'black'))
        self.res['trades'].configure(text=f"{len(today)} closed • {sum(1 for h in today if D(h['pnl']) > 0)} won • {s.get('loss_count', 0)} lost")
        p = s.get('position')
        self.res['position'].configure(text=(f"{p['held'].upper()} {p['qty'] - p['exited_qty']} @ {D(p['entry_avg']) * 100:.1f}¢ in {short_market(p['ticker'])}"
                                             + (' — SELLING (stop)' if p.get('exiting') else '')) if p else 'none (flat)',
                                       fg='#7a4b00' if p else 'black')
        self.res['orders'].configure(text=f"{st.get('orders', 0)} sent • {st.get('fills', 0)} filled • {st.get('no_fills', 0)} no-fill • "
                                          f"{st.get('rejects', 0)} rejected • {st.get('recovered_orders', 0)} recovered")
        if self.last_cash:
            self.res['cash'].configure(text=f'shard {self.last_cash[0]}: ${self.last_cash[1]:.2f}  (checked {local(self.last_cash_at)})')
        hist = s.get('history', [])
        tot = sum((D(h['pnl']) for h in hist), Decimal(0))
        self.res['alltime'].configure(text=f"{len(hist)} trades • {sum(1 for h in hist if D(h['pnl']) > 0)} won • "
                                           f"{sum(1 for h in hist if D(h['pnl']) < 0)} lost • {money(tot)}")
        self.res['pause'].configure(text=('PAUSED: ' + s['reason'][:90]) if s.get('halted') else 'allowed',
                                    fg='#b3141f' if s.get('halted') else '#146c2e')
        if sig != self.last_journal_sig:
            self.last_journal_sig = sig
            self.trades.delete(*self.trades.get_children())
            for h in reversed(hist):
                pnl = D(h['pnl'])
                res = {'yes': 'settled YES', 'no': 'settled NO', 'FULLY_EXITED': 'sold (stop)'}.get(h['result'], h['result'])
                self.trades.insert('', 'end', values=(local(h.get('closed_at')), short_market(h['ticker']), h['held'].upper(), h['qty'],
                                                      f"{D(h.get('entry_avg', 0)) * 100:.1f}", f"{D(h['cost']):.2f}", res, money(pnl)),
                                   tags=('win' if pnl > 0 else 'loss',))
            self.refresh_pvr(s)
        if time.time() - self.last_cash_at > (120 if self.attached else 60) and not self.busy and ops.apply_key_env(self.settings)[0]:
            self.last_cash_at = time.time()
            self.work(lambda: ops.shard_cash(ENV), self._cash_done)

    def _cash_done(self, r, err):
        if r:
            self.last_cash = (r['shard'], r['eth_cash'])

    def refresh_pvr(self, journal=None):
        a = self.attached
        sess = a['session'] if a else None
        if sess is None:
            for rt in (ops.runtime_dir(ENV, self.settings), ops.runtime_dir('paper', self.settings)):
                ss = ops.session_dirs(rt)
                if ss and (sess is None or ss[-1].name > sess.name):
                    sess = ss[-1]
        if sess is None:
            return
        app = sess / 'APP'
        paper = dict(signals=0, nofill=0, wins=0, losses=0, pending=0, pnl=Decimal(0))
        try:
            official = {}
            if (app / 'eth_v42_all_market_settlements.csv').exists():
                with open(app / 'eth_v42_all_market_settlements.csv', newline='', encoding='utf-8') as f:
                    official = {r['ticker']: r['official_side'] for r in csv.DictReader(f) if r.get('official_side') in ('yes', 'no')}
            if (app / 'eth_v42_shadow_candidates.csv').exists():
                with open(app / 'eth_v42_shadow_candidates.csv', newline='', encoding='utf-8') as f:
                    for c in csv.DictReader(f):
                        if c['status'] == 'LIVE_NO_FILL':
                            paper['nofill'] += 1
                        if c['status'] not in ('PAPER_APPROVED', 'SETTLED_WIN', 'SETTLED_LOSS'):
                            continue
                        paper['signals'] += 1
                        o = official.get(c['ticker'])
                        if not o:
                            paper['pending'] += 1
                        elif o == c['side']:
                            paper['wins'] += 1; paper['pnl'] += D(c['contracts']) - D(c['total_cost'])
                        else:
                            paper['losses'] += 1; paper['pnl'] -= D(c['total_cost'])
        except Exception:
            pass
        self.pvr['paper'].delete(*self.pvr['paper'].get_children())
        for k, v in (('Session', sess.name), ('Signals approved', paper['signals']), ('Signals with no real fill', paper['nofill']),
                     ('Won at settlement', paper['wins']), ('Lost at settlement', paper['losses']), ('Waiting for result', paper['pending']),
                     ('Paper P/L (ignores stops)', money(paper['pnl']))):
            self.pvr['paper'].insert('', 'end', values=(k, v))
        self.pvr['real'].delete(*self.pvr['real'].get_children())
        try:
            since = datetime.strptime(sess.name, '%Y%m%d_%H%M%S').timestamp() - 60
        except Exception:
            since = 0
        s = journal or {}
        hist = [h for h in s.get('history', []) if float(h.get('closed_at', 0)) >= since]
        orders = []
        op = ops.runtime_dir(ENV, self.settings) / 'live_orders.csv'
        if op.exists():
            with open(op, newline='', encoding='utf-8') as f:
                for o in csv.DictReader(f):
                    try:
                        if datetime.fromisoformat(o['time_utc']).timestamp() >= since and o['purpose'] == 'ENTRY':
                            orders.append(o)
                    except Exception:
                        pass
        slips = [D(o['slip_vs_signal_c']) for o in orders if o.get('slip_vs_signal_c')]
        for k, v in (('Entry orders sent', len(orders)), ('Filled', sum(1 for o in orders if o['result'] in ('FILLED', 'PARTIAL'))),
                     ('No fill / rejected', sum(1 for o in orders if o['result'] in ('NO_FILL', 'REJECTED'))),
                     ('Avg price vs signal', f'{sum(slips) / len(slips):+.2f}¢' if slips else '—'),
                     ('Trades closed', len(hist)), ('Won / lost', f"{sum(1 for h in hist if D(h['pnl']) > 0)} / {sum(1 for h in hist if D(h['pnl']) < 0)}"),
                     ('Real P/L', money(sum((D(h['pnl']) for h in hist), Decimal(0))))):
            self.pvr['real'].insert('', 'end', values=(k, v))

    # ================================================================== misc
    def export_last(self):
        sess = None
        for env in (ENV, 'paper'):
            rt = ops.runtime_dir(env, self.settings)
            ss = ops.session_dirs(rt)
            if ss and (sess is None or ss[-1].name > sess[1].name):
                sess = (rt, ss[-1])
        if not sess:
            messagebox.showinfo('Export', 'No sessions yet.'); return
        self.work(lambda: ops.export_session(sess[0], sess[1], key_id=os.environ.get('KALSHI_API_KEY_ID', ''),
                                             out_dir=ops.export_dir(self.settings)),
                  lambda r, e: self.say(f'Exported: {r}  ← send me this file' if r else f'Export failed: {e}'))

    def open_folder(self, path):
        try:
            if os.name == 'nt':
                os.startfile(str(path))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path)])
        except Exception as e:
            messagebox.showinfo('Folder', str(path) + f'\n\n({e})')

    def on_close(self):
        a = self.attached
        if not a:
            self.destroy(); return
        if not a['live']:
            if messagebox.askyesno('Close', 'A PAPER session is running. Stop it and close the app?'):
                ops.stop_paper(a['rt']); self.destroy()
            return
        ans = messagebox.askyesnocancel('LIVE session is running',
                                        'The LIVE engine keeps running in the background when this window closes '
                                        '(it keeps protecting any open position and ends at its set time).\n\n'
                                        'YES = close the window, engine keeps running (reopen the app any time to see it)\n'
                                        'NO = stop new trades first, then close\n'
                                        'CANCEL = keep the window open')
        if ans is None:
            return
        if ans is False:
            ops.request_stop(a['rt'])
        self.destroy()


def main():
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    app = Studio()
    try:
        style = ttk.Style(app)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
    except Exception:
        pass
    app.mainloop()


if __name__ == '__main__':
    main()
