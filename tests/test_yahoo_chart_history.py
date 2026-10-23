"""data.yahoo_chart_history: the 60-day Yahoo chart fetch behind replay_sessions.py --range-60d."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data  # noqa: E402
from providers import NotListed  # noqa: E402


def payload(ts, closes):
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [100 if c is not None else None for c in closes]}]}}]}}


class FakeYahoo:
    def __init__(self, answers):
        self.answers = answers        # symbol -> list of responses (dict, or an exception to raise), in order
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        sym = url.split("/chart/")[1].split("?")[0]
        r = self.answers[sym].pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fetch(fake, tickers):
    return data.yahoo_chart_history(tickers, get_json=fake, sleep=lambda s: None)


class TestYahooChartHistory(unittest.TestCase):

    def test_frames_are_utc_ohlcv_and_drop_rows_without_a_close(self):
        fake = FakeYahoo({"AAPL": [payload([1792071000, 1792071300, 1792071600], [1.0, None, 3.0])]})
        bars, failed = fetch(fake, ["AAPL"])
        df = bars["AAPL"]
        self.assertEqual(failed, {})
        self.assertEqual(list(df.columns), ["Open", "High", "Low", "Close", "Volume"])
        self.assertEqual(str(df.index.tz), "UTC")
        self.assertEqual(list(df["Close"]), [1.0, 3.0])
        self.assertIn("interval=5m", fake.urls[0])
        self.assertIn("range=60d", fake.urls[0])
        self.assertIn("includePrePost=true", fake.urls[0])

    def test_a_dropped_request_is_retried(self):
        fake = FakeYahoo({"MSFT": [ConnectionResetError("reset"), payload([1792071000], [5.0])]})
        bars, failed = fetch(fake, ["MSFT"])
        self.assertEqual(list(bars["MSFT"]["Close"]), [5.0])
        self.assertEqual(failed, {})
        self.assertEqual(len(fake.urls), 2)

    def test_an_unlisted_name_fails_at_once_and_the_rest_still_come_back(self):
        fake = FakeYahoo({"ZZZZQ": [NotListed("HTTP 404")], "NVDA": [payload([1792071000], [7.0])]})
        bars, failed = fetch(fake, ["ZZZZQ", "NVDA"])
        self.assertEqual(sorted(bars), ["NVDA"])
        self.assertIn("not listed", failed["ZZZZQ"])
        self.assertEqual(sum("ZZZZQ" in u for u in fake.urls), 1)

    def test_an_empty_series_counts_as_a_failure(self):
        fake = FakeYahoo({"XYZ": [payload([1792071000], [None])] * 3})
        bars, failed = fetch(fake, ["XYZ"])
        self.assertEqual(bars, {})
        self.assertIn("no bars", failed["XYZ"])

    def test_crypto_uses_the_yahoo_symbol(self):
        fake = FakeYahoo({"BTC-USD": [payload([1792071000], [60000.0])]})
        bars, _ = fetch(fake, ["BTC-USD"])
        self.assertIn("BTC-USD", bars)


if __name__ == "__main__":
    unittest.main()
