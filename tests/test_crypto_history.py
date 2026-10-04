"""
Crypto 5m history for the desk and the forward ledger (providers.crypto_history).

The venues' one-shot candle calls returned 200-350 5m bars, about a day, so the engine never had the
three sessions crypto RVOL needs, and one coin a venue does not list put that venue in cooldown for
every other coin. These tests run the real pager, cache and provider rotation against a fake exchange
(urllib is patched; no network): a cold fetch reaches ET midnight 8 days back, a warm one asks only
for the tail, a series never mixes two venues, an unlisted coin no longer cools its venue down, and
nothing is filled in.
"""

import json
import math
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ledger                     # noqa: E402
import providers                  # noqa: E402
from engine import analyze        # noqa: E402

ET = ZoneInfo("America/New_York")
STEP = 300_000                    # 5m in ms
LEVEL = {"okx": 100.0, "coinbase": 100.05, "yahoo": 99.9}     # each feed quotes its own book


class _Resp:
    def __init__(self, obj):
        self.body = json.dumps(obj).encode()

    def read(self, *_):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _http_error(url, code, obj):
    return urllib.error.HTTPError(url, code, "error", {}, BytesIO(json.dumps(obj).encode()))


class FakeExchange:
    """Answers the venues' 5m candle URLs in their own formats, bounds and error answers."""

    def __init__(self, okx=(), coinbase=(), yahoo=(), gaps=(), stale=()):
        self.okx, self.coinbase, self.yahoo = set(okx), set(coinbase), set(yahoo)
        self.okx_down = False
        self.gaps = set(gaps)          # bar times Coinbase prints no candle for (nothing traded)
        self.stale = set(stale)        # Coinbase products whose newest candle is a day old
        self.revision = 0              # moves the forming bar's close, as a live feed does
        self.urls = []

    def candle(self, feed, ts):
        c = LEVEL[feed] * (1 + 0.004 * math.sin(ts / 3.6e6))
        if ts == int(time.time() * 1000) // STEP * STEP:
            c += 0.01 * self.revision
        return c, c * 1.0005, c * 0.9995, c, 1000.0 + (ts // STEP) % 7      # o, h, l, c, v

    @staticmethod
    def grid(lo, hi):
        hi = min(hi, int(time.time() * 1000))                    # nothing from the future
        return list(range(-(-lo // STEP) * STEP, hi + 1, STEP))

    def __call__(self, req, timeout=None):
        url = getattr(req, "full_url", req)
        self.urls.append(url)
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        now_ms = int(time.time() * 1000)
        if u.netloc == "www.okx.com":
            if self.okx_down:
                raise _http_error(url, 503, {"msg": "down"})
            if q["instId"] not in self.okx:
                return _Resp({"code": "51001", "data": [], "msg": "Instrument ID doesn't exist."})
            lo, hi = int(q["before"]) + 1, int(q["after"]) - 1
            if u.path.endswith("/market/candles"):               # this endpoint keeps the newest 1,440 only
                lo = max(lo, now_ms - 1440 * STEP)
            ts = self.grid(lo, hi)[-int(q["limit"]):]
            rows = [[str(t), *(str(x) for x in self.candle("okx", t)), "0", "0", "1"] for t in reversed(ts)]
            return _Resp({"code": "0", "data": rows, "msg": ""})
        if u.netloc == "api.binance.com":
            raise _http_error(url, 451, {"code": 0, "msg": "restricted location"})
        if u.netloc == "api.bybit.com":
            raise _http_error(url, 403, {})
        if u.netloc == "api.exchange.coinbase.com":
            product = u.path.split("/")[2]
            if product not in self.coinbase:
                raise _http_error(url, 404, {"message": "NotFound"})
            iso = lambda s: int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)
            lo, hi = iso(q["start"]), iso(q["end"])
            assert (hi - lo) // STEP + 1 <= 300, "Coinbase rejects more than 300 candles"
            if product in self.stale:
                hi = min(hi, now_ms - 86_400_000)
            out = []
            for t in reversed(self.grid(lo, hi)):
                if t in self.gaps:
                    continue
                o, h, l, c, v = self.candle("coinbase", t)
                out.append([t // 1000, l, h, o, c, v])            # [time, low, high, open, close, volume]
            return _Resp(out)
        if u.netloc == "query1.finance.yahoo.com":
            sym = urllib.parse.unquote(u.path.rsplit("/", 1)[1])
            if sym not in self.yahoo:
                raise _http_error(url, 404, {"chart": {"result": None, "error": {"code": "Not Found"}}})
            ts = self.grid(int(q["period1"]) * 1000, int(q["period2"]) * 1000)
            cs = [self.candle("yahoo", t) for t in ts]
            quote = {k: [c[i] for c in cs] for i, k in enumerate(("open", "high", "low", "close", "volume"))}
            return _Resp({"chart": {"result": [{"timestamp": [t // 1000 for t in ts],
                                                "indicators": {"quote": [quote]}}], "error": None}})
        raise AssertionError(f"unexpected URL {url}")


class CryptoHistoryCase(unittest.TestCase):

    def setUp(self):
        for d in (providers._cooldown_until, providers._not_listed, providers._stats, providers._hist):
            d.clear()
        providers._cooldown_until["yfinance"] = time.time() + 3600         # last resort: real network, keep out
        self.patches = [mock.patch.dict(providers._LIMITS, {k: providers._Limiter(10 ** 6, 1.0)
                                                            for k in providers._LIMITS})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for d in (providers._cooldown_until, providers._not_listed, providers._stats, providers._hist):
            d.clear()

    def run_with(self, fake, fn, *a, **k):
        with mock.patch("urllib.request.urlopen", fake):
            return fn(*a, **k)


class TestCryptoHistory(CryptoHistoryCase):

    def test_cold_fetch_reaches_et_midnight_eight_days_back(self):
        fake = FakeExchange(okx={"FIX-USDT"})
        got = self.run_with(fake, providers.crypto_history, "FIX-USD")
        start = providers.history_start_ms(8)
        first = datetime.fromtimestamp(got["rows"][0][0] / 1000, ET)
        self.assertEqual((got["venue"], got["state"]), ("okx", "live"))
        self.assertEqual(got["rows"][0][0], start)
        self.assertEqual((first.hour, first.minute), (0, 0))
        self.assertEqual(got["rows"][-1][0], int(time.time() * 1000) // STEP * STEP)    # the forming bar
        self.assertEqual(len(got["rows"]), (got["rows"][-1][0] - start) // STEP + 1)     # no gaps on OKX
        self.assertEqual(len(fake.urls), -(-len(got["rows"]) // 300))                    # 300 a page
        # beyond the newest 1,440 bars only history-candles has them
        old = [u for u in fake.urls if int(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(u).query))["before"])
               < time.time() * 1000 - 1440 * STEP]
        self.assertTrue(old and all("/history-candles?" in u for u in old))

    def test_warm_fetch_asks_only_for_the_tail(self):
        fake = FakeExchange(okx={"FIX-USDT"})
        cold = self.run_with(fake, providers.crypto_history, "FIX-USD")
        fake.urls.clear()
        fake.revision = 1
        warm = self.run_with(fake, providers.crypto_history, "FIX-USD")
        self.assertEqual(len(fake.urls), 1)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(fake.urls[0]).query))
        self.assertEqual(int(q["before"]) + 1, cold["rows"][-2][0])          # refetch from the last closed bar
        self.assertEqual([r[0] for r in warm["rows"]][:len(cold["rows"])], [r[0] for r in cold["rows"]])
        if warm["rows"][-1][0] == cold["rows"][-1][0]:                       # (no new bar began in between)
            self.assertAlmostEqual(warm["rows"][-1][4] - cold["rows"][-1][4], 0.01)   # the forming bar updated

    def test_engine_gets_its_full_rvol_baseline(self):
        fake = FakeExchange(okx={"FIX-USDT"})
        r = self.run_with(fake, providers.fetch_intraday, "FIX-USD", "5m")
        row = analyze("FIX-USD", r["bars"])
        self.assertEqual((r["provider"], r["state"]), ("okx", "live"))
        self.assertEqual((row["session_n"], row["rvol_n"]), (9, 7))
        self.assertIsNotNone(row["rvol"])
        self.assertEqual(row["focus_day"], datetime.now(ET).strftime("%Y-%m-%d"))      # no synthetic D0/D1

    def test_a_coin_one_venue_lacks_does_not_cool_that_venue(self):
        fake = FakeExchange(okx={"FIX-USDT"}, coinbase={"NOPE-USD", "FIX-USD"})
        got = self.run_with(fake, providers.crypto_history, "NOPE-USD")
        self.assertEqual(got["venue"], "coinbase")
        self.assertTrue(providers._available("okx"))                         # OKX stays up for everyone else
        self.assertFalse(providers._available("okx", "NOPE-USD"))
        self.assertFalse(providers._available("binance"))                    # a refusal (451) still cools
        self.assertEqual(self.run_with(fake, providers.crypto_history, "FIX-USD")["venue"], "okx")
        fake.urls.clear()
        self.run_with(fake, providers.crypto_history, "NOPE-USD")
        self.assertFalse([u for u in fake.urls if "okx.com" in u])          # not asked again for NOPE

    def test_a_failing_venue_serves_its_cache_then_hands_over_whole(self):
        fake = FakeExchange(okx={"FIX-USDT"}, coinbase={"FIX-USD"})
        built = self.run_with(fake, providers.crypto_history, "FIX-USD")
        fake.okx_down = True
        fake.urls.clear()
        cached = self.run_with(fake, providers.crypto_history, "FIX-USD")
        self.assertEqual((cached["venue"], cached["state"]), ("okx", "cached"))
        self.assertEqual(cached["rows"], built["rows"])
        self.assertFalse(providers._available("okx"))
        self.assertFalse([u for u in fake.urls if "coinbase" in u])          # one blip does not rebuild
        later = time.time() + providers.CACHE_GRACE_SEC + 1
        moved = self.run_with(fake, providers.crypto_history, "FIX-USD", now_s=later)
        self.assertEqual((moved["venue"], moved["state"]), ("coinbase", "live"))
        for t, o, h, l, c, v in moved["rows"]:                               # never stitched from two venues
            self.assertAlmostEqual(c, fake.candle("coinbase", t)[3])

    def test_a_delisted_or_stale_product_is_skipped_not_cooled(self):
        fake = FakeExchange(coinbase={"OLD-USD"}, stale={"OLD-USD"})
        self.assertIsNone(self.run_with(fake, providers.crypto_history, "OLD-USD"))
        self.assertTrue(providers._available("coinbase"))
        self.assertFalse(providers._available("coinbase", "OLD-USD"))
        self.assertTrue(providers._available("okx"))

    def test_gaps_stay_gaps(self):
        now = int(time.time() * 1000) // STEP * STEP
        gaps = {now - 10 * STEP, now - 500 * STEP, now - 2000 * STEP}
        fake = FakeExchange(coinbase={"GAP-USD"}, gaps=gaps)
        rows = self.run_with(fake, providers.crypto_history, "GAP-USD")["rows"]
        ts = {r[0] for r in rows}
        self.assertFalse(ts & gaps)
        self.assertEqual(len(rows), (rows[-1][0] - rows[0][0]) // STEP + 1 - len(gaps))

    def test_yahoo_fallback_uses_the_mapped_symbol_and_the_same_window(self):
        fake = FakeExchange(yahoo={"SUI20947-USD"})
        r = self.run_with(fake, providers.fetch_intraday, "SUI-USD", "5m")
        self.assertEqual(r["provider"], "yahoo_chart")
        url = [u for u in fake.urls if "finance.yahoo.com" in u][0]
        self.assertIn("/SUI20947-USD?", url)
        self.assertEqual(int(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["period1"]) * 1000,
                         providers.history_start_ms(8))
        self.assertEqual(int(r["bars"].index[0].timestamp() * 1000), providers.history_start_ms(8))
        self.assertEqual(self.run_with(fake, providers.fetch_intraday, "NOPE-USD", "5m")["state"], "error")
        self.assertTrue(providers._available("yahoo_chart"))                # its 404 cooled nothing


class TestSymbols(unittest.TestCase):

    def test_yahoo_and_okx_symbols(self):
        self.assertEqual(providers.to_yahoo_symbol("SUI-USD"), "SUI20947-USD")
        self.assertEqual(providers.to_yahoo_symbol("TON-USD"), "GRAM-USD")          # TON-USD is "TON Token"
        self.assertEqual(providers.to_yahoo_symbol("SUI20947-USD"), "SUI20947-USD")
        self.assertEqual(providers.to_yahoo_symbol("BTC-USD"), "BTC-USD")
        self.assertEqual(providers.to_yahoo_symbol("AAPL"), "AAPL")
        self.assertEqual(providers.to_okx_inst("TON-USD"), "GRAM-USDT")
        self.assertEqual(providers.to_okx_inst("BTC-USD"), "BTC-USDT")

    def test_history_starts_at_et_midnight_across_dst(self):
        for now in (datetime(2026, 3, 10, 12, 0, tzinfo=ET), datetime(2026, 11, 3, 0, 30, tzinfo=ET)):
            start = datetime.fromtimestamp(providers.history_start_ms(8, now.timestamp()) / 1000, ET)
            self.assertEqual((start.date(), start.hour, start.minute), (now.date() - timedelta(days=8), 0, 0))


def crypto_day(day, until="23:55", ticker_level=100.0):
    """5m bars of one ET day from 00:00 through `until` (flat around `ticker_level`)."""
    t = datetime.fromisoformat(f"{day}T00:00").replace(tzinfo=ET)
    end = datetime.fromisoformat(f"{day}T{until}").replace(tzinfo=ET)
    idx = []
    while t <= end:
        idx.append(t)
        t += timedelta(minutes=5)
    n = len(idx)
    return pd.DataFrame({"Open": [ticker_level] * n, "High": [ticker_level * 1.001] * n,
                         "Low": [ticker_level * 0.999] * n, "Close": [ticker_level] * n,
                         "Volume": [1000.0] * n}, index=pd.DatetimeIndex(idx))


class TestLedgerCrypto(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        ledger._seen.clear()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def signal(self, session, ticker="FIX-USD"):
        trig = int(datetime.fromisoformat(f"{session}T06:00").replace(tzinfo=ET).timestamp() * 1000)
        ledger._append(self.dir / "signals.jsonl", [{
            "key": f"{ticker}|{session}|{trig}", "ticker": ticker, "session": session, "trigger_ts": trig,
            "side": "long", "entry": 100.0, "stop": 99.0, "target": 102.0, "grade": "A", "bar_provider": "okx"}])

    def test_resolve_reaches_back_to_the_oldest_pending_session(self):
        now = datetime(2026, 10, 4, 9, 0, tzinfo=ET)
        seen = []

        def fetch(*a, **k):
            seen.append(k.get("crypto_days"))
            return ({},)
        self.signal("2026-10-01")
        ledger.resolve(now, fetch=fetch, base=self.dir)
        self.signal("2026-09-22")
        counts = ledger.resolve(now, fetch=fetch, base=self.dir)
        self.assertEqual(seen, [None, 12])                     # 3 days fit the default 8; 12 are asked for
        self.assertEqual(counts["retry_later"], 2)

    def test_a_crypto_session_the_feed_has_not_finished_stays_pending(self):
        now = datetime(2026, 10, 4, 9, 0, tzinfo=ET)
        self.signal("2026-10-03")
        partial = crypto_day("2026-10-03", until="15:00")       # a stalled feed: the session ends at 23:55
        counts = ledger.resolve(now, fetch=lambda *a, **k: ({"FIX-USD": partial}, {}, {"FIX-USD": "okx"}),
                                base=self.dir)
        self.assertEqual((counts["retry_later"], counts["resolved"]), (1, 0))
        full = pd.concat([crypto_day("2026-10-03"), crypto_day("2026-10-04", until="00:10")])
        counts = ledger.resolve(now, fetch=lambda *a, **k: ({"FIX-USD": full}, {}, {"FIX-USD": "okx"}),
                                base=self.dir)
        self.assertEqual(counts["resolved"], 1)
        outs = ledger._read(self.dir / "outcomes.jsonl")
        self.assertTrue(outs and all(o["resolved_on"] == "okx" for o in outs))


if __name__ == "__main__":
    unittest.main()
