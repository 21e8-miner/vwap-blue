"""
The desk's yfinance bulk download is paced (data._paced_download).

yfinance sends a bulk download's requests one after another, about 45 a second whatever its `threads`
setting (they share one session): the desk's 437 stocks went out in ~10 s each scan, and on 2026-10-05
a scan right after another drew HTTP 429s. The download now goes out in chunks at YF_BULK_RPS requests a
second on average; a chunk Yahoo rate-limited holds the next back and its names are asked for again.
A fake yfinance and a fake clock: no network, no waiting.
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data                      # noqa: E402

SECONDS_PER_REQUEST = 1 / 45     # yfinance's own pace


class FakeYahoo:
    """yf.download over a fake clock: each name costs SECONDS_PER_REQUEST; names in `limit` are refused
    with a YFRateLimitError the first time they are asked for, as yfinance reports it."""

    def __init__(self, limit=(), broken=()):
        self.now = 0.0
        self.calls = []                  # (start time, names)
        self.kwargs = []                 # each call's timeout
        self.limit = set(limit)
        self.broken = set(broken)        # chunks containing these raise
        self.shared = types.SimpleNamespace(_ERRORS={})

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += max(0.0, s)

    def download(self, syms, **kw):
        names = syms.split()
        self.calls.append((self.now, names))
        self.kwargs.append(kw.get("timeout"))
        self.now += SECONDS_PER_REQUEST * len(names)
        if self.broken & set(names):
            raise ConnectionError("chunk failed")
        self.shared._ERRORS = {t: "YFRateLimitError('Too Many Requests. Rate limited. Try after a while.')"
                               for t in names if t in self.limit}
        self.limit -= set(names)
        ok = [t for t in names if t not in self.shared._ERRORS]
        idx = pd.date_range("2026-10-05 13:30", periods=10, freq="5min", tz="UTC")
        cols = pd.MultiIndex.from_product([names, ["Open", "High", "Low", "Close", "Volume"]])
        df = pd.DataFrame(np.nan, index=idx, columns=cols)
        for t in ok:
            df.loc[:, t] = 1.0
        return df


class TestPacedDownload(unittest.TestCase):

    def run_paced(self, fake, names, rps=20.0):
        with mock.patch.object(data.time, "monotonic", fake.monotonic), \
                mock.patch.object(data.time, "sleep", fake.sleep), \
                mock.patch.object(data, "YF_BULK_RPS", rps):
            return data._paced_download(fake, names, interval="5m", group_by="ticker")

    def test_chunks_go_out_at_the_set_pace(self):
        fake = FakeYahoo()
        names = [f"S{i}" for i in range(437)]
        got = self.run_paced(fake, names)
        self.assertEqual(sorted(got), sorted(names))
        self.assertTrue(all(len(n) <= data.YF_BULK_CHUNK for _, n in fake.calls))
        starts = [t for t, _ in fake.calls]
        for (a, n), b in zip(fake.calls, starts[1:]):
            self.assertGreaterEqual(b - a, len(n) / 20.0 - 1e-9)        # never faster than 20 a second
        self.assertAlmostEqual(starts[-1], 436 // 25 * 25 / 20.0)      # and no slower: 21.75 s to the last chunk
        self.assertLess(fake.now, 437 / 20.0 + 1)                       # ~10 s unpaced

    def test_no_pacing_when_switched_off(self):
        fake = FakeYahoo()
        self.run_paced(fake, [f"S{i}" for i in range(100)], rps=0)
        self.assertAlmostEqual(fake.now, 100 * SECONDS_PER_REQUEST)

    def test_rate_limited_names_wait_and_are_asked_for_again(self):
        fake = FakeYahoo(limit={"S3", "S30"})
        names = [f"S{i}" for i in range(60)]
        with self.assertLogs("vwap_one.data", "WARNING"):
            got = self.run_paced(fake, names)
        self.assertEqual(sorted(got), sorted(names))                    # all of them, in the end
        (t1, _), (t2, _) = fake.calls[0], fake.calls[1]
        self.assertGreaterEqual(t2 - t1, data.YF_RATE_LIMIT_PAUSE)     # a 429 holds the next chunk back
        self.assertEqual(fake.calls[-1][1], ["S3", "S30"])

    def test_a_failed_chunk_does_not_stop_the_rest(self):
        fake = FakeYahoo(broken={"S0"})
        with self.assertLogs("vwap_one.data", "WARNING"):
            got = self.run_paced(fake, [f"S{i}" for i in range(60)])
        self.assertEqual(sorted(got), sorted(f"S{i}" for i in range(25, 60)))

    def test_bulk_fetch_uses_it_for_bars_and_daily(self):
        fake = FakeYahoo()
        with mock.patch("providers.yf_module", return_value=fake), \
                mock.patch.object(data.time, "monotonic", fake.monotonic), \
                mock.patch.object(data.time, "sleep", fake.sleep):
            bars, days = data._yfinance_bulk(["AAPL", "MSFT", "BTC-USD"], "1mo", "5m", "2y", with_daily=True)
        self.assertEqual((sorted(bars), sorted(days)), (["AAPL", "MSFT"], ["AAPL", "MSFT"]))   # no crypto
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.kwargs, [data.YF_TIMEOUT] * 2)          # 5 s a request, not yfinance's 10


if __name__ == "__main__":
    unittest.main()
