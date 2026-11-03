"""
alpaca_bars.py: paging, frame shape and resumable pickles, against a fake Alpaca endpoint (no network, no keys).
"""

import pickle
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alpaca_bars as ab  # noqa: E402


def fake_get_factory(pages):
    """pages: list of (bars dict, next_page_token). Records every URL's query."""
    calls = []

    def get(url, headers):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        calls.append(q)
        i = len(calls) - 1 if len(calls) <= len(pages) else len(pages) - 1
        bars, token = pages[i]
        return {"bars": bars, "next_page_token": token}
    get.calls = calls
    return get


class TestFetch(unittest.TestCase):

    def test_pages_follow_the_token_and_the_query_asks_for_raw_sip_5m(self):
        get = fake_get_factory([({"AAPL": [{"t": "2026-10-01T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]}, "tok1"),
                                ({"AAPL": [{"t": "2026-10-01T13:35:00Z", "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 20}], "MSFT": []}, None)])
        out = ab.fetch_bars(["AAPL", "MSFT"], datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc), {}, get)
        self.assertEqual(len(get.calls), 2)
        self.assertEqual(get.calls[1]["page_token"], ["tok1"]); self.assertNotIn("page_token", get.calls[0])
        q = get.calls[0]
        self.assertEqual((q["feed"], q["adjustment"], q["timeframe"], q["symbols"]), (["sip"], ["raw"], ["5Min"], ["AAPL,MSFT"]))
        self.assertTrue(q["start"][0].endswith("Z"))
        self.assertEqual(len(out["AAPL"]), 2); self.assertEqual(out["MSFT"], [])

    def test_frame_is_utc_indexed_ohlcv_without_page_seam_duplicates(self):
        rows = [{"t": "2026-10-01T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},
                {"t": "2026-10-01T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},
                {"t": "2026-10-01T13:25:00Z", "o": 0.9, "h": 1, "l": 0.8, "c": 1.0, "v": 5}]
        df = ab.to_frame(rows)
        self.assertEqual(list(df.columns), ["Open", "High", "Low", "Close", "Volume"])
        self.assertEqual(len(df), 2); self.assertEqual(str(df.index.tz), "UTC")
        self.assertEqual(df.index[0].isoformat(), "2026-10-01T13:25:00+00:00")
        self.assertTrue(ab.to_frame([]).empty)

    def test_month_spans_cover_the_window_exactly(self):
        s = list(ab.month_spans(datetime(2026, 1, 15, tzinfo=timezone.utc), datetime(2026, 3, 10, tzinfo=timezone.utc)))
        self.assertEqual([(a.date().isoformat(), b.date().isoformat()) for a, b in s],
                         [("2026-01-15", "2026-02-01"), ("2026-02-01", "2026-03-01"), ("2026-03-01", "2026-03-10")])


class TestPull(unittest.TestCase):

    def test_pull_writes_the_replay_pickle_layout_and_resumes_from_the_last_bar(self):
        def rows(day, n):
            return [{"t": f"2026-10-{day:02d}T13:{30 + 5 * i:02d}:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10} for i in range(n)]
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "a.pkl"
            get = fake_get_factory([({"AAPL": rows(1, 3)}, None)])
            blob = ab.pull(["AAPL"], datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc), out, {}, get, log=lambda s: None)
            self.assertEqual(sorted(blob), sorted(["fetched_at", "tickers", "bars", "bar_prov", "daily", "live", "qmeta"]))
            self.assertEqual(blob["bar_prov"], {"AAPL": "alpaca_sip_5m"}); self.assertEqual(len(blob["bars"]["AAPL"]), 3)
            with open(out, "rb") as fh:
                on_disk = pickle.load(fh)
            self.assertEqual(len(on_disk["bars"]["AAPL"]), 3)
            # resume: a later end fetches only from the last held bar, and overlapping rows are not duplicated
            get2 = fake_get_factory([({"AAPL": rows(1, 3) + rows(2, 2)}, None)])
            blob2 = ab.pull(["AAPL"], datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 3, tzinfo=timezone.utc), out, {}, get2, log=lambda s: None)
            self.assertEqual(len(blob2["bars"]["AAPL"]), 5)
            self.assertEqual(get2.calls[0]["start"], ["2026-10-01T13:40:00Z"], "starts at the last bar held, not the window start")

    def test_missing_keys_is_a_clear_error(self):
        import os
        env = {k: os.environ.pop(k) for k in ("ALPACA_KEY_ID", "ALPACA_SECRET_KEY") if k in os.environ}
        old = ab.SECRETS; ab.SECRETS = Path("/nonexistent")
        try:
            with self.assertRaises(SystemExit):
                ab.load_keys()
        finally:
            ab.SECRETS = old; os.environ.update(env)


if __name__ == "__main__":
    unittest.main()


class TestSymbols(unittest.TestCase):

    def test_class_shares_use_a_dot_on_alpaca_and_come_back_with_the_dash(self):
        self.assertEqual(ab.to_alpaca("BRK-B"), "BRK.B"); self.assertEqual(ab.to_alpaca("AAPL"), "AAPL")
        self.assertEqual(ab.to_alpaca("BTC-USD"), "BTC-USD", "crypto spelling is left alone")
        self.assertEqual(ab.from_alpaca("BRK.B"), "BRK-B")

    def test_a_rejected_symbol_is_dropped_and_the_group_retried(self):
        calls = []

        def get(url, headers):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query); calls.append(q["symbols"][0])
            if "ZZZZ" in q["symbols"][0]:
                raise ab.AlpacaError(400, '{"message":"invalid symbol: ZZZZ"}')
            return {"bars": {"AAPL": [{"t": "2026-10-01T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]}, "next_page_token": None}
        with tempfile.TemporaryDirectory() as d:
            blob = ab.pull(["AAPL", "ZZZZ"], datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc),
                           Path(d) / "a.pkl", {}, get, log=lambda s: None)
        self.assertEqual(calls, ["AAPL,ZZZZ", "AAPL"]); self.assertEqual(list(blob["bars"]), ["AAPL"])
