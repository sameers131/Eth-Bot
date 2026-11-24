"""ETH V1 tests: the ETH app trades only KXETH15M on ETHUSD_RTI, keeps its own data and settings,
never touches the BTC bot's positions or top-up shard, and runs the full V7-D loop at Ether prices.
Offline; no network, no real keys.

Run from this folder:
python TEST_ETH.py
"""
import importlib.util, json, os, shutil, sys, tempfile, time, types, unittest, zipfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp(prefix='ethtest_', dir=os.environ.get('RE5_TEST_TMP')))
os.environ['BIGBET_ETH_SETTINGS_FILE'] = str(_TMP / 'settings_eth.json')
os.environ['BIGBET_ETH_DATA_DIR'] = str(_TMP / 'ETH_DATA')
os.environ['BIGBET_SETTINGS_FILE'] = str(_TMP / 'settings.json')          # the BTC app's file (key source)
os.environ['BIGBET_DATA_DIR'] = str(_TMP / 'BTC_DATA')
os.environ.pop('BIGBET_TRADE_USD', None)

from RE5 import asset  # noqa: E402
from TEST_RE5 import Base, Clock, FakeKalshi, cand, T  # noqa: E402
from TEST_V12 import OpsKalshi, make_key  # noqa: E402
from RE5 import ops  # noqa: E402
from RE5 import exchange  # noqa: E402
from RE5.controller import Controller, SafetyError  # noqa: E402
import TEST_RE5_LOOP as LOOP  # noqa: E402

# TEST_V12 (imported above for its fakes) points the env at its own temp folder: point it back here.
os.environ['BIGBET_ETH_SETTINGS_FILE'] = str(_TMP / 'settings_eth.json')
os.environ['BIGBET_ETH_DATA_DIR'] = str(_TMP / 'ETH_DATA')
os.environ['BIGBET_SETTINGS_FILE'] = str(_TMP / 'settings.json')
ops.SETTINGS_PATH = _TMP / 'settings_eth.json'

D = lambda x: Decimal(str(x))
BTC_TICKER = 'KXBTC15M-26NOV011915-15'


def load_v37():
    spec = importlib.util.spec_from_file_location('v37_eth_test', ROOT / 'RE5' / 'APP' / 'kalshi_eth_bot_v37.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


class AssetProfile(unittest.TestCase):
    def test_eth_identity(self):
        self.assertEqual(asset.SERIES, 'KXETH15M'); self.assertEqual(asset.INDEX_ID, 'ETHUSD_RTI')
        self.assertEqual(exchange.MARKET_PREFIX, 'KXETH15M-'); self.assertTrue(T.startswith('KXETH15M-'))
    def test_eth_only_app(self):
        self.assertEqual(asset.ASSET, 'ETH'); self.assertFalse(hasattr(asset, 'PROFILES'))
    def test_strategy_rules_rule_b(self):
        sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
        spec = importlib.util.spec_from_file_location('coll_rules', ROOT / 'RE5' / 'APP' / 'ETH_V42_Collector.py')
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        self.assertEqual((m.P_MIN, m.P_MAX, m.MIN_SCALED_GAP, m.EARLY_MIN_SCALED_GAP, m.CHECKPOINTS, m.STOP_TRIGGER, m.ETH_STOP_MULTIPLE),
                         (D('0.90'), D('0.99'), D('1.6'), D('2.0'), (180, 150, 120, 105, 90, 60, 45, 30), D('0.88'), D('0.5')))   # ETH V1.3 Rule B
        self.assertEqual(m.MOMENTUM_RESEARCH_USD, D('0.5'))
    def test_plausible_price(self):
        self.assertTrue(asset.plausible_price(D('2500'))); self.assertTrue(asset.plausible_price(D('900')))
        self.assertFalse(asset.plausible_price(D('80000')))          # a BTC price can never pass as ETH
        self.assertFalse(asset.plausible_price(D('10')))


class PriceIndex(unittest.TestCase):
    def setUp(self):
        self.v37 = load_v37()
        self.c = self.v37.Client.__new__(self.v37.Client)
    def _reply(self, key, value, age=0.5):
        return {'data': {'payload': {'latest_values': {key: {'value': str(value), 'time': int((time.time() - age) * 1000)}}}}}
    def test_asks_for_ethusd_rti(self):
        seen = {}
        def req(method, path, params=None, auth=False):
            seen.update(path=path, params=params); return self._reply('ETHUSD_RTI', '2512.37')
        self.c.request = req
        price, ts = self.c.ethrti()
        self.assertEqual(seen['params'], {'id': 'ETHUSD_RTI'}); self.assertEqual(price, D('2512.37'))
    def test_refuses_btc_index_payload(self):
        self.c.request = lambda *a, **k: self._reply('BRTI', '80300.10')
        with self.assertRaises(KeyError): self.c.ethrti()
    def test_refuses_btc_sized_price_and_stale_value(self):
        self.c.request = lambda *a, **k: self._reply('ETHUSD_RTI', '80300.10')
        with self.assertRaises(ValueError): self.c.ethrti()
        self.c.request = lambda *a, **k: self._reply('ETHUSD_RTI', '2500', age=30)
        with self.assertRaises(ValueError): self.c.ethrti()
    def test_market_discovery_uses_eth_series(self):
        sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
        spec = importlib.util.spec_from_file_location('coll_disc', ROOT / 'RE5' / 'APP' / 'ETH_V42_Collector.py')
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        calls = []
        class C:
            def request(self, method, path, params=None):
                calls.append(params); return {'markets': [], 'cursor': ''}
        self.assertIsNone(m.current_eth_market(C(), now=time.time()))
        self.assertEqual(calls[0]['series_ticker'], 'KXETH15M')


def _load_risk(mode, guard_dir):
    sys.path.insert(0, str(ROOT / 'RE5' / 'APP'))
    with mock.patch.dict(os.environ, {'BIGBET_DAILY_GUARD_DIR': str(guard_dir)}):
        if mode: os.environ['BIGBET_RE5_LIVE'] = mode
        else: os.environ.pop('BIGBET_RE5_LIVE', None)
        spec = importlib.util.spec_from_file_location(f'risk_eth_{mode or "paper"}', ROOT / 'RE5' / 'APP' / 'ETH_V42_Risk.py')
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        os.environ.pop('BIGBET_RE5_LIVE', None)
    return m


class DailyGuardIsolation(unittest.TestCase):
    """Stop counters: inside the ETH data folder, one per mode (found live on Nov 5: a shared counter blocked trading)."""
    def setUp(self):
        self.g = Path(tempfile.mkdtemp(prefix='ethguard_'))
        self.live, self.paper, self.demo = _load_risk('prod', self.g), _load_risk('', self.g), _load_risk('demo', self.g)
    def test_files_live_in_eth_data_folder_one_per_mode(self):
        self.assertEqual({self.live.DAILY_DB.name, self.paper.DAILY_DB.name, self.demo.DAILY_DB.name},
                         {'ETH_LIVE_DAILY_GUARD.sqlite3', 'ETH_PAPER_DAILY_GUARD.sqlite3', 'ETH_DEMO_DAILY_GUARD.sqlite3'})
        for m in (self.live, self.paper, self.demo):
            self.assertEqual(m.DAILY_DB.parent, self.g)
            self.assertNotIn('Downloads', str(m.DAILY_DB)); self.assertNotIn('BIGBET_EXPORTS', str(m.DAILY_DB))
    def test_default_folder_is_eth_data(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('BIGBET_DAILY_GUARD_DIR', None)
            spec = importlib.util.spec_from_file_location('risk_eth_default', ROOT / 'RE5' / 'APP' / 'ETH_V42_Risk.py')
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        self.assertEqual(m.DAILY_DB.parent, Path.home() / 'BIGBET_ETH_DATA' / 'DAILY_GUARD')
    def _stop(self, m, ticker):
        m.daily_loss_limit_cents(970)
        with m.closing(m.daily_connect()) as c:
            c.execute("INSERT INTO daily_events VALUES (?,?,?,?,?)", (ticker, m.eastern_day(), 'STOPPED', -300, 0))
    def test_paper_and_live_counters_are_separate(self):
        self._stop(self.paper, 'KXETH15M-26NOV060915-15'); self._stop(self.paper, 'KXETH15M-26NOV060930-30')
        self.assertEqual(self.paper.daily_status()[0], 2); self.assertEqual(self.live.daily_status(), (0, 0))
        self._stop(self.live, 'KXETH15M-26NOV061415-15')
        self.assertEqual(self.live.daily_status()[0], 1); self.assertEqual(self.paper.daily_status()[0], 2)
    def test_engine_is_told_where_the_counters_are(self):
        src = (ROOT / 'RE5' / 'ops.py').read_text(encoding='utf-8')
        self.assertIn("BIGBET_DAILY_GUARD_DIR=str(data_dir(s) / 'DAILY_GUARD')", src)


class TickerGuard(unittest.TestCase):
    def test_exchange_refuses_btc_tickers(self):
        with self.assertRaises(exchange.ApiFailure): exchange._check_ticker(BTC_TICKER)
        exchange._check_ticker(T)


class SideBySideWithBtcBot(Base):
    """The BTC bot holds KXBTC15M positions on the same account: the ETH app must ignore them."""
    def test_btc_position_does_not_block_eth(self):
        self.ex.extra_positions = [{'ticker': BTC_TICKER, 'position_fp': '10.00'}]
        self.c.recover()
        self.assertTrue(self.c.entry(cand()))
        self.assertFalse(self.state()['halted'])
    def test_settles_eth_while_btc_is_open(self):
        self.ex.extra_positions = [{'ticker': BTC_TICKER, 'position_fp': '-10.00'}]
        self.c.entry(cand()); self.ex.settle(T, 'yes'); self.clock.t += 400
        self.assertTrue(self.c.poll_settlement()); self.assertIsNone(self.state()['position'])
    def test_unknown_eth_exposure_still_blocks(self):
        self.ex.extra_positions = [{'ticker': 'KXETH15M-26NOV011930-30', 'position_fp': '3.00'}]
        with self.assertRaises(SafetyError): self.c.recover()
    def test_entry_refuses_btc_candidate(self):
        self.assertFalse(self.c.entry(dict(cand(), ticker=BTC_TICKER)))
        self.assertEqual(self.ex.posts, [])


class Isolation(unittest.TestCase):
    def setUp(self):
        for n in ('ETH_DATA', 'BTC_DATA', 'settings.json', 'settings_eth.json'):
            p = _TMP / n
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    def test_settings_are_eth_only(self):
        keyfile = make_key()
        other = {'key_id': 'abcd1234-other-app', 'key_path': str(keyfile), 'trade_usd': 20}
        (_TMP / 'settings.json').write_text(json.dumps(other))
        self.assertEqual(ops.load_settings(), {})                                     # never reads another app's settings
        ops.save_settings({'key_id': 'eth-key-id', 'key_path': str(keyfile), 'trade_usd': 10})
        self.assertEqual(json.loads((_TMP / 'settings.json').read_text()), other)     # other file untouched
        self.assertEqual(ops.load_settings()['key_id'], 'eth-key-id')
        self.assertTrue(ops.apply_key_env(ops.load_settings())[0])
    def test_own_data_folder_with_marker(self):
        d = ops.data_dir()
        self.assertEqual(d, _TMP / 'ETH_DATA'); self.assertEqual((d / 'ASSET.txt').read_text().strip(), 'ETH')
        self.assertEqual(ops.runtime_dir('prod'), d / 'RE5_prod')
    def test_refuses_btc_data_folder(self):
        with mock.patch.dict(os.environ, {'BIGBET_ETH_DATA_DIR': str(Path.home() / 'BIGBET_DATA')}):
            with self.assertRaises(ops.WrongAssetFolder): ops.data_dir()
        other = _TMP / 'MIXED'; other.mkdir(exist_ok=True); (other / 'ASSET.txt').write_text('BTC\n')
        with mock.patch.dict(os.environ, {'BIGBET_ETH_DATA_DIR': str(other)}):
            with self.assertRaises(ops.WrongAssetFolder): ops.data_dir()
    def test_never_imports_btc_journals(self):
        old = _TMP / 'Revision 2' / 'PRIVATE_RUNTIME' / 'RE5_prod'; old.mkdir(parents=True, exist_ok=True)
        (old / 're5_journal.json').write_text('{}')
        self.assertEqual(ops.find_old_runtimes(_TMP), [])
        with self.assertRaises(RuntimeError): ops.import_runtime(old)
        shutil.rmtree(_TMP / 'Revision 2')
    def test_export_is_named_eth(self):
        rt = ops.runtime_dir('prod'); sess = rt / 'sessions' / '20261105_090000'; sess.mkdir(parents=True)
        (sess / 'collector_stdout.log').write_text('ok\n')
        z = ops.export_session(rt, sess, key_id='', out_dir=_TMP / 'EXP')
        self.assertTrue(Path(z).name.startswith('BIGBET_ETH_RE5_prod_20261105_090000'))
        self.assertIn('session/collector_stdout.log', zipfile.ZipFile(z).namelist())


class EthKalshi(OpsKalshi):
    """ETH markets on shard 3, the BTC bot's markets on shard 2."""
    def eth_market_shard(self, series=None):
        return 2 if series == 'KXBTC15M' else 3


class TopUpNeverRaidsBtcShard(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(_TMP / 'ETH_DATA', ignore_errors=True)
        self.ex = EthKalshi(Clock())
    def test_btc_shard_is_protected(self):
        self.ex.shards = {0: D('0'), 2: D('200'), 3: D('4')}
        p = ops.topup_plan('prod', api=self.ex)
        self.assertEqual(p['action'], 'blocked'); self.assertIn('other BIGBET bot', p['message'])
        r = ops.topup_if_needed('prod', api=self.ex, sleep=lambda s: None)
        self.assertFalse(r['ok']); self.assertEqual(self.ex.transfers, [])
    def test_uses_a_free_shard(self):
        self.ex.shards = {0: D('50'), 2: D('200'), 3: D('4')}
        r = ops.topup_if_needed('prod', api=self.ex, sleep=lambda s: None)
        self.assertTrue(r['ok'], r); self.assertEqual(self.ex.transfers, [(D('20'), 0, 3)])
    def test_btc_shard_lookup_failure_protects_default(self):
        class Flaky(EthKalshi):
            def eth_market_shard(self, series=None):
                if series: raise RuntimeError('down')
                return 3
        ex = Flaky(Clock()); ex.shards = {2: D('200'), 3: D('4')}
        self.assertEqual(ops.topup_plan('prod', api=ex)['action'], 'blocked')


class FullLoopAtEtherPrices(LOOP.Loop):
    """Replays the three end-to-end engine scenarios (win, stop-loss, no-fill retry) at ETH ~$2,500."""
    def setUp(self):
        self._scale = LOOP.SCALE; LOOP.SCALE = D('2500') / D('80000')
    def tearDown(self):
        LOOP.SCALE = self._scale
    def test_prices_really_are_ether_sized(self):
        ft = LOOP.FakeTime(); sc = LOOP.Scenario(ft, False)
        self.assertTrue(D('2400') < sc.eth() < D('2600')); self.assertEqual(sc.market()['floor_strike'], '2500.00')


class AppLabels(unittest.TestCase):
    """Load the desktop app with a stand-in tkinter (the real window needs a display)."""
    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType('tkinter')
        fake.Tk = object; fake.Toplevel = object; fake.Frame = fake.Label = fake.Button = fake.Text = object
        for sub in ('ttk', 'messagebox', 'filedialog', 'simpledialog'):
            mod = types.ModuleType('tkinter.' + sub); setattr(fake, sub, mod); sys.modules['tkinter.' + sub] = mod
        sys.modules.setdefault('tkinter', fake)
        spec = importlib.util.spec_from_file_location('eth_app', ROOT / 'BIGBET_LIVE_STUDIO_V12.py')
        cls.app = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.app)
    def test_title_and_panel(self):
        self.assertIn('ETH', self.app.TITLE); self.assertEqual(self.app.PANEL, '#efe9ff')          # ETH V1.7: lavender (BTC is yellow)
        self.assertIn('TEST_ETH.py', self.app.TEST_FILES)
    def test_plain_english_says_eth(self):
        self.assertEqual(self.app.short_market('KXETH15M-26NOV051015-15'), 'Nov 05 10:15')
        self.assertIn('ETH history ready', self.app.plain('HISTORY_READINESS KXETH15M-X READY minute_pairs=8/8'))
        self.assertIn('ETH', self.app.plain('WAITING_FOR_ACTIVE_ETH_MARKET utc=x'))
        line = '  180s | NO  gap $9.41 | ask 0.9600 | scaled 2.10 | PAPER_APPROVED'
        self.assertIn('ETH $9.41 from strike', self.app.plain(line))
        self.assertEqual(self.app.plain('RE5.5 ETH LIVE EXECUTION ACTIVE on PROD | $10 cap'), 'RE5.5 ETH LIVE EXECUTION ACTIVE on PROD | $10 cap')


if __name__ == '__main__':
    unittest.main(verbosity=1)
