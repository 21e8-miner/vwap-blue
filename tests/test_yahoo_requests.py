"""
What the desk asks Yahoo for, and what a failed request shows.

On 2026-10-05 yfinance's bulk download failed for 191 stocks at 08:15 ET and for 380, then 104, around
15:00, every one as TypeError("'NoneType' object is not subscriptable"); a minute later the desk's own
one-by-one Yahoo requests got 379 of 380. yfinance (1.0 to 1.7) unhides exceptions for yf.download
on config.network while history() reads config.debug, so the real error is swallowed, and its
network.retries default is 0. Every scan also downloaded two years of daily bars for each stock, which
the engine does not read, and kept asking for EA, which stopped trading in August.
No network: yfinance and the fetch functions are replaced here.
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app                       # noqa: E402
import data                      # noqa: E402
import providers                 # noqa: E402

try:
    from yfinance.exceptions import YFPricesMissingError
except ImportError:              # yfinance < 1.0 has no config and no exception classes
    YFPricesMissingError = None


class FakeTicker:
    def __init__(self, error):
        self.error = error

    @property
    def fast_info(self):
        raise self.error

    def history(self, **kw):
        raise self.error


class TestYahooRequests(unittest.TestCase):

    def setUp(self):
        for d in (providers._cooldown_until, providers._not_listed, providers._stats):
            d.clear()

    def tearDown(self):
        for d in (providers._cooldown_until, providers._not_listed, providers._stats):
            d.clear()

    def test_the_desk_skips_daily_bars_unless_vwap_one_reads_them(self):
        fetched = ({}, {}, {}, {}, {})
        for one, want in ((None, False), (lambda *a, **k: {}, True)):
            with mock.patch.object(app, "batch_fetch", return_value=fetched) as bf, \
                    mock.patch.object(app, "one_analyze", one), mock.patch.object(app, "LEDGER_ON", False):
                app.run_scan(["AAPL"], max_n=1, grade_min="C")
            self.assertIs(bf.call_args.kwargs["with_daily"], want)

    def test_no_daily_download_or_daily_fetch_when_skipped(self):
        calls = []
        fake_yf = types.SimpleNamespace(download=lambda syms, **kw: calls.append(kw.get("interval")) or pd.DataFrame())
        with mock.patch.object(providers, "yf_module", return_value=fake_yf):
            data._yfinance_bulk(["AAPL", "MSFT"], "1mo", "5m", "2y", with_daily=False)
            self.assertNotIn("1d", calls)
            data._yfinance_bulk(["AAPL", "MSFT"], "1mo", "5m", "2y", with_daily=True)
            self.assertEqual(calls.count("1d"), 1)
        empty = {"bars": pd.DataFrame(), "provider": None}
        with mock.patch.object(providers, "fetch_intraday", return_value=empty), \
                mock.patch.object(providers, "fetch_quote", return_value={"price": None}), \
                mock.patch.object(providers, "fetch_daily", return_value=empty) as daily:
            providers.batch_rotate_fetch(["BTC-USD", "ETH-USD"], with_daily=False)
            self.assertEqual(daily.call_count, 0)
            providers.batch_rotate_fetch(["BTC-USD", "ETH-USD"])
            self.assertEqual(daily.call_count, 2)

    @unittest.skipIf(YFPricesMissingError is None, "yfinance < 1.0")
    def test_yfinance_retries_and_shows_its_errors(self):
        yf = providers.yf_module()
        self.assertEqual(yf.config.network.retries, providers.YF_RETRIES)
        self.assertIs(yf.config.debug.hide_exceptions, False)

    @unittest.skipIf(YFPricesMissingError is None, "yfinance < 1.0")
    def test_a_symbol_yahoo_has_no_prices_for_skips_only_that_symbol(self):
        missing = types.SimpleNamespace(Ticker=lambda s: FakeTicker(YFPricesMissingError(s, "")))
        down = types.SimpleNamespace(Ticker=lambda s: FakeTicker(ConnectionError("reset by peer")))
        for name in ("yahoo_chart", "stooq", "eodhd_demo"):          # straight to the yfinance fallback
            providers._cooldown_until[name] = 1e12
        with mock.patch.object(providers, "yf_module", return_value=missing):
            self.assertEqual(providers.fetch_intraday("GONE")["state"], "error")
            self.assertEqual(providers.fetch_daily("GONE")["state"], "error")
            self.assertIsNone(providers.fetch_quote("GONE")["price"])
        self.assertTrue(providers._available("yfinance"))             # the others keep yfinance
        self.assertFalse(providers._available("yfinance", "GONE"))
        with mock.patch.object(providers, "yf_module", return_value=down):
            self.assertIn("reset by peer", providers.fetch_intraday("ELSE")["error"])
        self.assertFalse(providers._available("yfinance"))            # a failure still cools it down

    def test_the_scan_log_shows_the_whole_elapsed_time(self):
        """It printed %.2s of the float: a 110.5 s scan logged as "11s", an 8.6 s one as "8."."""
        clock = iter([1000.0] + [1110.53] * 20)
        fake_time = types.SimpleNamespace(time=lambda: next(clock), strftime=app.time.strftime)
        with mock.patch.object(app, "batch_fetch", return_value=({}, {}, {}, {}, {})), \
                mock.patch.object(app, "time", fake_time), mock.patch.object(app, "LEDGER_ON", False), \
                self.assertLogs("vwap_blue", "INFO") as logs:
            app.run_scan(["AAPL"], max_n=1, grade_min="C")
        self.assertTrue(any("scan ok" in m and m.endswith(" in 110.5s") for m in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
