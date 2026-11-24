"""ETH app operations shared by the desktop app and the command-line tools.
Standalone: data ~/BIGBET_ETH_DATA, settings ~/.bigbet/settings_eth.json. Nothing is read from any other app.

Everything the operator used to do by hand lives here:
  * settings (key ID + key FILE PATH only, never the key itself) in ~/.bigbet/settings_eth.json
  * one fixed data folder (default ~/BIGBET_ETH_DATA) so upgrades never need copying
  * read-only account check that also clears stale locks / benign halts automatically
  * shard cash top-up (option A: below $15 -> move $20, at most $40 per UTC day)
  * starting an engine session that keeps running even if the app window is closed
  * export of a session (refuses anything that looks like a credential)
"""
import json, os, shutil, subprocess, sys, time, uuid, zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # package folder (contains RE5/)
APP_SRC = ROOT / 'RE5' / 'APP'
from . import scaling
from . import asset
# The ETH app has its OWN settings file and data folder: it never shares a journal, ladder, lock,
# top-up ledger, stop counter or saved "Per trade" size with any other app, and reads no other app's files.
_SETTINGS_ENV = 'BIGBET_ETH_SETTINGS_FILE'
_DATA_ENV = 'BIGBET_ETH_DATA_DIR'
SETTINGS_PATH = Path(os.environ.get(_SETTINGS_ENV) or (Path.home() / '.bigbet' / asset.P['settings_name']))
VERSION = 'ETH V1.9.4 / RE5.5'

# Top-up at the $10 size (V12.3 scales these with the trade size: 1.5x / 2x / 4x the size).
TOPUP_MIN, TOPUP_AMOUNT, TOPUP_DAILY_CAP = scaling.topup_params(scaling.BASE_USD)   # 15 / 20 / 40
DEFAULT_ETH_SHARD = 2
BENIGN_HALTS = ('OPERATOR_KILL_SWITCH', 'SESSION_END')
ALLOWED_EXPORT = {'.json', '.jsonl', '.log', '.csv', '.txt'}
SECRET_MARKERS = ('PRIVATE KEY', 'BEGIN RSA', 'BEGIN EC', 'BEGIN OPENSSH')
HEARTBEAT_STALE_S = 120


def utcday():
    return datetime.now(timezone.utc).date().isoformat()


# ====================================================================== settings / folders
def _read_json(path):
    try:
        d = json.loads(Path(path).read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_settings():
    return _read_json(SETTINGS_PATH)          # ETH settings only; the key is entered once in Settings


def save_settings(s):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(s, indent=2), encoding='utf-8')
    os.replace(tmp, SETTINGS_PATH)


class WrongAssetFolder(RuntimeError):
    pass


def data_dir(settings=None):
    s = settings if settings is not None else load_settings()
    d = Path(os.environ.get(_DATA_ENV) or s.get('data_dir') or (Path.home() / asset.P['data_dir_name']))
    for name in asset.P['refuse_data_dirs']:
        other = Path.home() / name
        try:
            same = d.resolve() == other.resolve()
        except OSError:
            same = False
        if same:
            raise WrongAssetFolder(f'ETH app refuses to use another app\'s data folder {other}')
    d.mkdir(parents=True, exist_ok=True)
    marker = d / 'ASSET.txt'
    if marker.exists():
        have = marker.read_text(encoding='utf-8', errors='replace').strip().upper()
        if have and have != asset.ASSET:
            raise WrongAssetFolder(f'Data folder {d} belongs to {have}, not {asset.ASSET}')
    else:
        marker.write_text(asset.ASSET + '\n', encoding='utf-8')
    return d


def runtime_dir(env, settings=None):
    """env: 'prod' | 'demo' | 'paper'."""
    name = 'PAPER' if env == 'paper' else f'RE5_{env}'
    d = data_dir(settings) / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_dir(settings=None):
    d = data_dir(settings) / 'EXPORTS'
    d.mkdir(parents=True, exist_ok=True)
    return d


def apply_key_env(settings=None):
    """Put the key id / key path from settings into this process's environment (settings win).
    Returns (ok, message). Never reads or prints the key contents."""
    s = settings if settings is not None else load_settings()
    if s.get('key_id'):
        os.environ['KALSHI_API_KEY_ID'] = s['key_id']
    if s.get('key_path'):
        os.environ['KALSHI_PRIVATE_KEY_PATH'] = s['key_path']
    kid = os.environ.get('KALSHI_API_KEY_ID', '')
    kpath = os.environ.get('KALSHI_PRIVATE_KEY_PATH', '')
    if not kid or not kpath:
        return False, 'Kalshi key not set up yet (Settings)'
    if not Path(kpath).is_file():
        return False, f'Key file not found: {kpath}'
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        load_pem_private_key(Path(kpath).read_bytes(), password=None)
    except Exception:
        return False, 'Key file could not be read as a private key'
    return True, f'Key ID …{kid[-4:]} | key file found'


def mask(key_id):
    return ('…' + key_id[-4:]) if key_id else 'not set'


# ====================================================================== engine process / lock
def pid_alive(pid):
    if not pid:
        return False
    pid = int(pid)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = wintypes.DWORD()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:   # zombie child of this process counts as finished
        if os.waitpid(pid, os.WNOHANG) != (0, 0):
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    return True


def read_lock(rt):
    p = Path(rt) / 'RUNNING.lock'
    if not p.exists():
        return None
    txt = p.read_text(encoding='utf-8', errors='replace').strip()
    try:
        d = json.loads(txt)
        if isinstance(d, dict):
            return d
    except ValueError:
        pass
    return {'stamp': txt, 'pid': None, 'legacy': True}


def heartbeat_age(rt):
    try:
        d = json.loads((Path(rt) / 'HEARTBEAT').read_text(encoding='utf-8'))
        return time.time() - float(d['time'])
    except Exception:
        return None


def engine_state(rt):
    """'none' (no lock) | 'running' | 'not_responding' | 'stopped' (lock left behind)."""
    lock = read_lock(rt)
    if not lock:
        return 'none', lock
    if lock.get('pid') and pid_alive(lock['pid']):
        age = heartbeat_age(rt)
        return ('running' if age is not None and age < HEARTBEAT_STALE_S else 'not_responding'), lock
    return 'stopped', lock


def session_dirs(rt):
    d = Path(rt) / 'sessions'
    return sorted(p for p in d.glob('*') if p.is_dir()) if d.exists() else []


def start_session(env, minutes, *, settings=None, live=False, python=None, engine_cmd=None, trade_usd=10, glitch_at=None):
    """Start the engine detached from the app. env: 'prod'|'demo' for live, 'paper' for paper.
    Returns dict(proc, session, stamp, log). Caller must have passed the checklist first."""
    if not 1 <= int(minutes) <= 600:
        raise ValueError('minutes must be 1..600')
    s = settings if settings is not None else load_settings()
    ok, msg = apply_key_env(s)
    if not ok:
        raise RuntimeError(msg)
    rt = runtime_dir(env, s)
    state, lock = engine_state(rt)
    if state != 'none':
        raise RuntimeError(f'engine lock present ({state}); run the account check first')
    usd = scaling.check_size(trade_usd)
    if live:
        lad = ladder_info(env, s)
        if usd not in lad['allowed']:
            raise RuntimeError(f'${usd} per trade is not unlocked (unlocked: ${lad["unlocked"]}). Needs: ' + '; '.join(lad['needs']))
        blocked, wmsg = scaling.weekly_check(rt, usd, journal_history=lad['_history'])
        if blocked:
            raise RuntimeError(wmsg + ' — review with Claude, then use "Release weekly stop" in the app')
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    session = rt / 'sessions' / stamp
    shutil.copytree(APP_SRC, session / 'APP', ignore=shutil.ignore_patterns('*.csv', '*.sqlite3*', '__pycache__'))
    child = os.environ.copy()
    child.update(BIGBET_RE5_END_TIME=str(time.time() + int(minutes) * 60), ETH_V42_KILL_SWITCH='0',
                 ETH_V42_MIN_CUSHION_PCT='0.03', BIGBET_LOG_DIR=str(session), BIGBET_HEARTBEAT_FILE=str(rt / 'HEARTBEAT'),
                 PYTHONPATH=str(ROOT) + os.pathsep + str(session / 'APP'), PYTHONUNBUFFERED='1',
                 PYTHONIOENCODING='utf-8', BIGBET_TRADE_USD=str(usd),
                 BIGBET_DAILY_GUARD_DIR=str(data_dir(s) / 'DAILY_GUARD'),
                 BIGBET_MOOD_SAMPLES=str(data_dir(s) / 'mood_samples.json'))   # ETH V1.9.4: engine reads the app's ETH prices for the mood regime   # ETH V1.1: stop counters live in the ETH data folder
    child.pop('BIGBET_GLITCH_AT', None)
    if glitch_at:                                   # auto-restart: skip the 15-minute market of the outage
        child['BIGBET_GLITCH_AT'] = str(float(glitch_at))
    if live:
        if env not in ('demo', 'prod'):
            raise ValueError('live sessions need env demo or prod')
        child.update(BIGBET_RE5_LIVE=env, KALSHI_ENV=env, BIGBET_RE5_STATE_FILE=str(rt / 're5_journal.json'))
        if env == 'prod':
            child['BIGBET_RE5_PROD_ARM'] = scaling.arm_phrase(usd)   # only inside the engine process; names the size
    else:
        for k in ('BIGBET_RE5_LIVE', 'BIGBET_RE5_STATE_FILE', 'BIGBET_RE5_PROD_ARM'):
            child.pop(k, None)
        child['KALSHI_ENV'] = 'prod'
    for f in ('KILL_SWITCH', 'FLATTEN', 'HEARTBEAT'):
        (rt / f).unlink(missing_ok=True)
    log = session / 'collector_stdout.log'
    flags = 0
    if os.name == 'nt':
        flags = 0x08000000 | 0x00000200                    # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    with open(log, 'ab') as out:
        out.write(f'=== BIGBET {VERSION} session {stamp} | {"LIVE " + env.upper() if live else "PAPER"} | {minutes} min | ${usd} per trade ===\n'.encode())
        out.flush()
        cmd = engine_cmd or [python or sys.executable, '-u', str(session / 'APP' / 'ETH_V42_Collector.py'), 'run']
        proc = subprocess.Popen(cmd,
                                cwd=str(session / 'APP'), env=child, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)
    lock = dict(stamp=stamp, pid=proc.pid, env=env, live=bool(live), minutes=int(minutes), started=time.time(), trade_usd=usd,
                end_time=time.time() + int(minutes) * 60)
    tmp = rt / 'RUNNING.lock.tmp'
    tmp.write_text(json.dumps(lock), encoding='utf-8')
    os.replace(tmp, rt / 'RUNNING.lock')
    if live:
        _event(rt, 'SESSION_START', stamp=stamp, minutes=int(minutes), env=env, trade_usd=usd)
    return dict(proc=proc, session=session, stamp=stamp, log=log, rt=rt)


def request_stop(rt):
    """Stop new trades: engine finishes/protects any open position, then exits."""
    (Path(rt) / 'KILL_SWITCH').write_text(datetime.now(timezone.utc).isoformat(), encoding='utf-8')


def request_flatten(rt):
    """Emergency: sell the open position now AND stop new trades."""
    (Path(rt) / 'FLATTEN').write_text(datetime.now(timezone.utc).isoformat(), encoding='utf-8')
    request_stop(rt)


def stop_paper(rt):
    """Paper sessions hold no money: terminate the engine process directly."""
    st, lock = engine_state(rt)
    if lock and lock.get('pid') and pid_alive(lock['pid']):
        pid = int(lock['pid'])
        if os.name == 'nt':
            subprocess.run(['taskkill.exe', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=15)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    (Path(rt) / 'RUNNING.lock').unlink(missing_ok=True)


# ====================================================================== account check / halts
def _event(rt, kind, **data):
    row = dict(time=round(time.time(), 3), event=kind, **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in data.items()})
    try:
        with (Path(rt) / 're5_live_events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(row) + '\n')
    except Exception:
        pass


def make_api(env, write=False):
    from .exchange import ExchangeClient
    os.environ['KALSHI_ENV'] = env
    if write and env == 'prod':
        from .exchange import PROD_ARM_PHRASE
        os.environ['BIGBET_RE5_PROD_ARM'] = PROD_ARM_PHRASE
    try:
        return ExchangeClient(env=env, write_mode=(env if write else 'off'))
    finally:
        if write and env == 'prod':
            os.environ.pop('BIGBET_RE5_PROD_ARM', None)


def check_account(env, *, api=None, settings=None):
    """READ-ONLY reconciliation of the journal against Kalshi, plus automatic housekeeping:
      * removes a RUNNING.lock left by an engine that is no longer running
      * clears KILL_SWITCH / SESSION_END halts (they only meant 'stop this session') once flat
      * removes leftover KILL_SWITCH / FLATTEN files once flat
    Returns dict(ok, state, message, actions, engine)."""
    from .controller import Controller
    rt = runtime_dir(env, settings)
    actions = []
    eng, lock = engine_state(rt)
    if eng in ('running', 'not_responding'):
        return dict(ok=True, state=None, engine=eng, actions=actions,
                    message='Engine is running' + (' (not responding)' if eng == 'not_responding' else ''))
    api = api or make_api(env)
    c = Controller(api, rt / 're5_journal.json', log=lambda *_: None,
                   event_log=lambda row: _event(rt, row.pop('event'), **{k: v for k, v in row.items() if k != 'time'}))
    try:
        s = c.recover()
    except Exception as e:
        s = c.j.read()
        return dict(ok=False, state=s, engine=eng, actions=actions,
                    message=f'Account does not match the journal: {type(e).__name__}: {e}')
    flat = not s['pending'] and not s['position']
    if eng == 'stopped':
        (rt / 'RUNNING.lock').unlink(missing_ok=True)
        actions.append('removed leftover run lock (engine no longer running; account matches)')
    if flat:
        for f in ('KILL_SWITCH', 'FLATTEN'):
            if (rt / f).exists():
                (rt / f).unlink(); actions.append(f'removed {f} file')
        if s['halted']:
            parts = [x.strip() for x in s['reason'].split('|') if x.strip()]
            if parts and all(any(x.startswith(b) for b in BENIGN_HALTS) for x in parts):
                s = c.j.read(); s['halted'] = False; s['reason'] = ''; c.j.save(s)
                actions.append('cleared end-of-session pause (' + ', '.join(sorted({x.split(':')[0] for x in parts})) + ')')
                _event(rt, 'AUTO_CLEAR_HALT', reasons=';'.join(parts))
    s = c.j.read()
    return dict(ok=True, state=s, engine='none' if eng == 'stopped' else eng, actions=actions,
                message='Account matches the journal' + (' (position open)' if s['position'] else ' (flat)'))


def halt_kind(state):
    if not state or not state.get('halted'):
        return None
    if state['reason'].startswith('DAILY_LOSS'):
        return 'daily_loss'
    return 'other'


def clear_halt(env, *, api=None, settings=None):
    """Operator-confirmed clear of a non-daily halt. Requires a passing read-only check and a flat account."""
    from .controller import Controller
    r = check_account(env, api=api, settings=settings)
    if not r['ok']:
        return False, r['message']
    s = r['state']
    if s is None:
        return False, 'engine is running'
    if s['pending'] or s['position']:
        return False, 'an order or position is still open'
    if halt_kind(s) == 'daily_loss':
        return False, 'daily loss limit lasts until the next UTC day (7 PM New York time)'
    rt = runtime_dir(env, settings)
    c = Controller(api or make_api(env), rt / 're5_journal.json', log=lambda *_: None)
    s = c.j.read(); old = s['reason']; s['halted'] = False; s['reason'] = ''; c.j.save(s)
    _event(rt, 'OPERATOR_CLEAR_HALT', previous=old)
    return True, 'Pause cleared: ' + old[:120]


# ====================================================================== auto-restart (from ETH V12.7, ported)
AUTO_RESTART_MAX = 3            # restarts per planned session
AUTO_RESTART_RETRY_S = 60       # how often to re-check while Kalshi is unreachable
AUTO_RESTART_MIN_MINUTES = 15   # do not restart for less than this much planned time
_CONN_WORDS = ('ReadTimeout', 'ConnectTimeout', 'Timeout', 'ConnectionError', 'RemoteDisconnected',
               'transport failure', 'HTTP 429', 'HTTP 500', 'HTTP 502', 'HTTP 503', 'HTTP 504',
               '429 Client Error', '500 Server Error', '502 Server Error', '503 Server Error', '504 Server Error',
               'Stale or implausible ETHUSD_RTI')   # ETH V1.9.1: price-feed blip (Nov 14) -> safe auto-restart when flat


def is_connection_problem(text):
    t = text or ''
    return any(w in t for w in _CONN_WORDS)


def auto_restart_prepare(env, *, api=None, settings=None):
    """Decide whether the app may restart a LIVE session by itself after the engine stopped.
    Returns (verdict, message): verdict 'go' (pause cleared, start now), 'wait' (Kalshi still unreachable:
    try again later) or 'never' (needs a person: open position/order, mismatch, loss limit, any non-connection pause)."""
    from .controller import Controller
    r = check_account(env, api=api, settings=settings)
    s = r.get('state')
    if not r['ok']:
        if is_connection_problem(r['message']):
            return 'wait', 'Kalshi still unreachable: ' + r['message'][:120]
        return 'never', 'account check failed: ' + r['message'][:160]
    if s is None:
        return 'never', 'an engine is already running'
    if s['pending'] or s['position']:
        return 'never', 'an order or position is open'
    if s['halted']:
        if halt_kind(s) == 'daily_loss' or not s['reason'].startswith('OPERATIONAL_ERROR') or not is_connection_problem(s['reason']):
            return 'never', 'paused for a reason that needs you: ' + s['reason'][:120]
        rt = runtime_dir(env, settings)
        c = Controller(api or make_api(env), rt / 're5_journal.json', log=lambda *_: None)
        st = c.j.read(); old = st['reason']; st['halted'] = False; st['reason'] = ''; c.j.save(st)
        _event(rt, 'AUTO_CLEAR_CONNECTION_HALT', previous=old)
        return 'go', 'connection pause cleared automatically: ' + old[:100]
    return 'go', 'account matches, flat, no pause'


# ====================================================================== shard cash
def shard_cash(env, *, api=None):
    """Returns dict(shard, balances{idx: dollars}, eth_cash, protected).
    eth_cash = cash on the shard the ETH market uses.
    protected = shards another BIGBET app trades on; never used as a top-up source."""
    api = api or make_api(env)
    try:
        shard = api.eth_market_shard()
    except Exception:
        shard = None
    shard = DEFAULT_ETH_SHARD if shard is None else shard
    protected = set()
    for series in asset.P['protect_series']:
        try:
            ps = api.eth_market_shard(series=series)
        except Exception:
            ps = None
        protected.add(DEFAULT_ETH_SHARD if ps is None else ps)
    bal = api.balance_breakdown()
    return dict(shard=shard, balances=bal, eth_cash=bal.get(shard, Decimal(0)), protected=protected)


def _topup_ledger(rt):
    p = Path(rt) / 'topups.json'
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        d = {}
    return p, d


def topup_plan(env, *, api=None, settings=None, info=None, trade_usd=10):
    """What a top-up would do right now (no money moves). Returns dict(action, ...).
    action: 'none' | 'transfer' | 'blocked'."""
    rt = runtime_dir(env, settings)
    info = info or shard_cash(env, api=api)
    shard = info['shard']; cash = info['eth_cash']
    _, led = _topup_ledger(rt)
    used = Decimal(str(led.get(utcday(), {}).get('total', '0')))
    TOPUP_MIN, TOPUP_AMOUNT, TOPUP_DAILY_CAP = scaling.topup_params(trade_usd)   # V12.3: scaled with the size
    if cash >= TOPUP_MIN:
        return dict(action='none', shard=shard, cash=cash, used_today=used,
                    message=f'Shard {shard} cash ${cash:.2f} (enough)')
    if used + TOPUP_AMOUNT > TOPUP_DAILY_CAP:
        return dict(action='blocked', shard=shard, cash=cash, used_today=used,
                    message=f'Shard {shard} cash ${cash:.2f}; daily top-up limit ${TOPUP_DAILY_CAP} reached')
    protected = info.get('protected') or set()
    sources = sorted(((b, i) for i, b in info['balances'].items()
                      if i != shard and i not in protected and b >= TOPUP_AMOUNT), reverse=True)
    if not sources:
        total = sum(info['balances'].values(), Decimal(0))
        extra = f' (shard {", ".join(map(str, sorted(protected)))} is kept for the other BIGBET bot)' if protected else ''
        return dict(action='blocked', shard=shard, cash=cash, used_today=used,
                    message=f'Shard {shard} cash ${cash:.2f} and no other shard has ${TOPUP_AMOUNT}{extra} (total cash ${total:.2f}). Deposit on Kalshi.')
    return dict(action='transfer', shard=shard, cash=cash, used_today=used, source=sources[0][1], amount=TOPUP_AMOUNT,
                message=f'Shard {shard} cash ${cash:.2f}: will move ${TOPUP_AMOUNT} from shard {sources[0][1]} on Start')


def topup_if_needed(env, *, api=None, settings=None, wait_s=20, sleep=time.sleep, trade_usd=10):
    """Option A top-up. Writes the intent to topups.json BEFORE sending (counts against the daily cap
    even if the outcome is unknown), then waits for the cash to arrive. Returns dict(ok, message)."""
    rt = runtime_dir(env, settings)
    api = api or make_api(env, write=True)
    plan = topup_plan(env, api=api, settings=settings, trade_usd=trade_usd)
    TOPUP_MIN = scaling.topup_params(trade_usd)[0]
    if plan['action'] == 'none':
        return dict(ok=True, moved=False, message=plan['message'])
    if plan['action'] == 'blocked':
        return dict(ok=False, moved=False, message=plan['message'])
    path, led = _topup_ledger(rt)
    day = led.setdefault(utcday(), {'total': '0', 'transfers': []})
    tid = str(uuid.uuid4())
    day['total'] = str(Decimal(day['total']) + plan['amount'])
    day['transfers'].append(dict(id=tid, amount=str(plan['amount']), source=plan['source'], dest=plan['shard'],
                                 time=time.time(), status='sent'))
    path.write_text(json.dumps(led, indent=2), encoding='utf-8')
    before = plan['cash']
    try:
        resp = api.transfer_between_shards(amount_dollars=plan['amount'], source_shard=plan['source'],
                                           destination_shard=plan['shard'], client_transfer_id=tid)
        status = 'accepted ' + str(resp.get('transfer_id', ''))
    except Exception as e:
        status = f'unknown/failed: {type(e).__name__}: {str(e)[:100]}'
    _event(rt, 'SHARD_TOPUP', amount=plan['amount'], source=plan['source'], dest=plan['shard'], status=status, before=before)
    deadline = time.time() + wait_s
    after = before
    while True:
        try:
            after = shard_cash(env, api=api)['eth_cash']
        except Exception:
            pass
        if after >= before + plan['amount'] - Decimal('0.01') or time.time() >= deadline:
            break
        sleep(2)
    day['transfers'][-1]['status'] = status + f' | shard cash after ${after:.2f}'
    path.write_text(json.dumps(led, indent=2), encoding='utf-8')
    arrived = after >= TOPUP_MIN
    return dict(ok=arrived, moved=True, message=(f'Moved ${plan["amount"]} from shard {plan["source"]} to shard {plan["shard"]}: '
                                                 f'cash now ${after:.2f}' if arrived else
                                                 f'Top-up sent ({status}) but shard {plan["shard"]} shows ${after:.2f}; check Kalshi'))


# ====================================================================== export / import
def export_session(rt, session, *, key_id=None, out_dir=None):
    """Zip one session for analysis. Never includes keys: refuses any file containing a key marker
    or the key id. Returns the zip path."""
    from .report import summary_text, write_daily_summary
    rt = Path(rt); session = Path(session)
    key_id = key_id if key_id is not None else os.environ.get('KALSHI_API_KEY_ID', '')
    try:
        if (rt / 're5_live_events.jsonl').exists():
            write_daily_summary(rt)
    except Exception:
        pass
    since = datetime.strptime(session.name, '%Y%m%d_%H%M%S').timestamp() - 60
    files = [(rt / n, n) for n in ('re5_journal.json', 're5_live_events.jsonl', 'live_orders.csv', 'daily_summary.csv', 'topups.json',
                                         scaling.LEDGER_NAME, scaling.LADDER_NAME)
             if (rt / n).exists()]
    for f in sorted(session.rglob('*')):
        if f.is_file() and f.suffix.lower() in ALLOWED_EXPORT and '__pycache__' not in f.parts:
            files.append((f, 'session/' + f.relative_to(session).as_posix()))
    out_dir = Path(out_dir) if out_dir else (rt.parent / 'EXPORTS')
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{asset.P['export_tag']}_{rt.name}_{session.name}.zip"
    summary = summary_text(rt, since=since, title=f'BIGBET {VERSION} REAL RESULTS | {rt.name} | session {session.name}') \
        if (rt / 're5_journal.json').exists() else 'PAPER session (no real orders)'
    tmp = out.with_suffix('.tmp')
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        for src, name in files:
            if src.suffix.lower() not in ALLOWED_EXPORT:
                continue
            data = src.read_bytes()
            text = data[:5_000_000].decode('utf-8', errors='ignore')
            if any(m in text for m in SECRET_MARKERS) or (len(key_id) >= 8 and key_id in text):
                z.close(); tmp.unlink(missing_ok=True)
                raise RuntimeError(f'REFUSED: {src.name} looks like it contains a credential. Nothing exported.')
            z.writestr(name, data)
        z.writestr('REAL_RESULTS.txt', summary)
    os.replace(tmp, out)
    (session / 'EXPORTED.txt').write_text(str(out), encoding='utf-8')
    return out


def unexported_sessions(rt):
    return [s for s in session_dirs(rt) if not (s / 'EXPORTED.txt').exists()]


class LockedOldRuntime(RuntimeError):
    pass


def find_old_runtimes(start=None, env='prod', max_depth=5):
    """Find PRIVATE_RUNTIME/RE5_<env> folders from earlier revisions near this package (newest first)."""
    return []              # ETH app: never imports journals from any other app or revision
    start = Path(start) if start else ROOT.parent
    found = []
    roots = [start, start.parent]
    seen = set()
    for base in roots:
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            depth = len(Path(dirpath).relative_to(base).parts)
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in ('sessions', '__pycache__', 'APP', 'node_modules', '.git')]
            p = Path(dirpath)
            if p.name == f'RE5_{env}' and p.parent.name == 'PRIVATE_RUNTIME' and (p / 're5_journal.json').exists():
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp); found.append(p)
    return sorted(found, key=lambda p: (p / 're5_journal.json').stat().st_mtime, reverse=True)


def import_runtime(src, env='prod', settings=None, old_window_closed=False):
    """Copy journal + history files from an earlier revision into the fixed data folder.
    Refuses if the old folder still has a run lock or the new folder already has a journal."""
    src = Path(src)
    raise RuntimeError('The ETH app never imports journals from other apps.')
    rt = runtime_dir(env, settings)
    if (src / 'RUNNING.lock').exists():
        if not old_window_closed:
            raise LockedOldRuntime('That folder still has RUNNING.lock: its session may still be running.')
        (src / 'RUNNING.lock').rename(src / 'RUNNING.lock.cleared_by_V12')
    if (rt / 're5_journal.json').exists():
        raise RuntimeError('BIGBET_DATA already has a journal; import is only for first-time setup.')
    copied = []
    for n in ('re5_journal.json', 're5_live_events.jsonl', 'live_orders.csv'):
        if (src / n).exists():
            shutil.copy2(src / n, rt / n); copied.append(n)
    (src / 'MOVED_TO_BIGBET_DATA.txt').write_text(
        f'Journal copied to {rt} by BIGBET {VERSION} on {datetime.now().isoformat(timespec="seconds")}.\n'
        'Do not run live sessions from this old folder any more.\n', encoding='utf-8')
    _event(rt, 'IMPORTED_FROM_OLD_REVISION', source=str(src), files=','.join(copied))
    return copied


# ====================================================================== V12.3 size ladder
def _journal_history(rt):
    try:
        return json.loads((Path(rt) / 're5_journal.json').read_text(encoding='utf-8')).get('history', [])
    except Exception:
        return []


def ladder_info(env='prod', settings=None):
    """Ladder status for the live runtime (applies an earned automatic step-down)."""
    rt = runtime_dir(env, settings)
    hist = _journal_history(rt)
    st = scaling.ladder_status(rt, journal_history=hist)
    st['weekly_blocked'], st['weekly_msg'] = scaling.weekly_check(rt, st['unlocked'], journal_history=hist)
    st['_history'] = hist
    return st


def ladder_unlock(env='prod', settings=None):
    rt = runtime_dir(env, settings)
    new = scaling.unlock_next(rt, journal_history=_journal_history(rt))
    _event(rt, 'LADDER_UNLOCK', to=new)
    return new


def ladder_release_weekly(env='prod', settings=None):
    rt = runtime_dir(env, settings)
    scaling.release_weekly_stop(rt)
    _event(rt, 'WEEKLY_STOP_RELEASED')



# ====================================================================== ETH V1.6 daily-stop override
def risk_module(env):
    """The collector's daily guard module for this mode (prod / demo / paper each use their own guard file)."""
    import importlib.util
    old = os.environ.get('BIGBET_RE5_LIVE')
    os.environ['BIGBET_RE5_LIVE'] = env if env in ('prod', 'demo') else ''
    try:
        spec = importlib.util.spec_from_file_location(f'bigbet_eth16_risk_{env}', Path(__file__).resolve().parent / 'APP' / 'ETH_V42_Risk.py')
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    finally:
        if old is None:
            os.environ.pop('BIGBET_RE5_LIVE', None)
        else:
            os.environ['BIGBET_RE5_LIVE'] = old
    return m


def guard_blocked(env, risk=None):
    """True when the collector's daily guard (Eastern day: 2 stops or 30% loss) would refuse new entries."""
    try:
        m = risk or risk_module(env)
        stops, loss = m.daily_status_since_override()
        lim = m.daily_loss_limit_cents()
        return stops >= m.MAX_DAILY_STOP_OUTS or (lim is not None and loss <= -lim)
    except Exception:
        return False


def override_daily_stop(env, *, api=None, settings=None, risk=None):
    """Operator choice: keep trading after a daily loss stop. Allowed after EVERY loss.
    * engine must be stopped, account flat and matching Kalshi (read-only check)
    * the same limits apply again from this moment (the next loss pauses again)
    * never clears other pauses, and never touches the weekly stop or the size ladder."""
    from .journal import Journal
    r = check_account(env, api=api, settings=settings)
    if not r['ok']:
        return False, r['message']
    s = r['state']
    if s is None:
        return False, 'a session is running: press STOP NEW TRADES, wait for it to end, then try again'
    if s['pending'] or s['position']:
        return False, 'an order or position is still open'
    m = risk or risk_module(env)
    g_blocked = guard_blocked(env, m)
    j_blocked = halt_kind(s) == 'daily_loss'
    if s['halted'] and not j_blocked:
        return False, 'paused for another reason (not the daily stop): ' + s['reason'][:120]
    if j_blocked:
        parts = [x.strip() for x in s['reason'].split('|') if x.strip()]
        if not all(x.startswith('DAILY_LOSS') or any(x.startswith(b) for b in BENIGN_HALTS) for x in parts):
            return False, 'paused for another reason too: ' + s['reason'][:120]
    if not j_blocked and not g_blocked:
        return False, 'the daily stop is not active; nothing to override'
    rt = runtime_dir(env, settings)
    j = Journal(rt / 're5_journal.json')
    if j_blocked:
        s = j.read()
        s.update(halted=False, reason='', ovr_losses=int(s['loss_count']), ovr_realized=str(s['realized_usd']), ovr_day=s['day'])
        j.save(s)
    base = m.record_daily_override()
    _event(rt, 'OPERATOR_OVERRIDE_DAILY_STOP', journal=j_blocked, guard=g_blocked, guard_base=str(base))
    return True, 'Daily stop overridden. The next loss pauses trading again (you can override again).'
