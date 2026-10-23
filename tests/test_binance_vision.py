"""binance_vision.py: archive files read, verified and stitched without the network (a fake server)."""

import hashlib
import io
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import binance_vision as bv  # noqa: E402


def kline_zip(day: date, n: int = 3, micro: bool = True, header: bool = False, price: float = 100.0) -> bytes:
    lines = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,tb_base,tb_quote,ignore"] if header else []
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    for i in range(n):
        ms = t0 + i * 300_000
        ts = ms * 1000 if micro else ms
        lines.append(f"{ts},{price + i},{price + i + 1},{price + i - 1},{price + i + 0.5},{10 + i},{ts},0,0,0,0,0")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("k.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


class FakeArchive:
    """Serves the zips it was given, their .CHECKSUM files, and 404 (None) for anything else."""

    def __init__(self, files, bad_checksum=()):
        self.files, self.bad, self.urls = files, set(bad_checksum), []

    def __call__(self, url):
        self.urls.append(url)
        if url.endswith(".CHECKSUM"):
            name = url.rsplit("/", 1)[1][: -len(".CHECKSUM")]
            if name not in self.files:
                return None
            digest = "0" * 64 if name in self.bad else hashlib.sha256(self.files[name]).hexdigest()
            return f"{digest}  {name}\n".encode()
        return self.files.get(url.rsplit("/", 1)[1])


class TestBinanceVision(unittest.TestCase):

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp())

    def test_both_timestamp_units_and_an_optional_header(self):
        for micro in (True, False):
            for header in (True, False):
                df = bv.parse_zip(kline_zip(date(2026, 9, 1), micro=micro, header=header))
                self.assertEqual(len(df), 3)
                self.assertEqual(str(df.index[0]), "2026-09-01 00:00:00+00:00")
                self.assertEqual(str(df.index[1]), "2026-09-01 00:05:00+00:00")
                self.assertEqual(list(df.columns), ["Open", "High", "Low", "Close", "Volume"])

    def test_a_published_month_then_daily_files_for_the_unpublished_one(self):
        files = {"BTCUSDT-5m-2026-09.zip": kline_zip(date(2026, 9, 30), price=1),
                 "BTCUSDT-5m-2026-10-01.zip": kline_zip(date(2026, 10, 1), micro=False, price=2),
                 "BTCUSDT-5m-2026-10-02.zip": kline_zip(date(2026, 10, 2), price=3)}
        fake = FakeArchive(files)
        df = bv.klines("BTC-USD", date(2026, 9, 30), date(2026, 10, 2), today=date(2026, 10, 3), get=fake, cache=self.cache)
        self.assertEqual(len(df), 9)
        self.assertEqual(sorted({d.date().isoformat() for d in df.index}), ["2026-09-30", "2026-10-01", "2026-10-02"])
        self.assertTrue(df.index.is_monotonic_increasing)
        self.assertTrue(any("/monthly/klines/BTCUSDT/5m/BTCUSDT-5m-2026-10.zip" in u for u in fake.urls))

    def test_never_today_and_the_window_is_cut_exactly(self):
        files = {"BTCUSDT-5m-2026-10-02.zip": kline_zip(date(2026, 10, 2)),
                 "BTCUSDT-5m-2026-10-03.zip": kline_zip(date(2026, 10, 3))}
        df = bv.klines("BTC-USD", date(2026, 10, 2), date(2026, 10, 3), today=date(2026, 10, 3),
                       get=FakeArchive(files), cache=self.cache)
        self.assertEqual({d.date().isoformat() for d in df.index}, {"2026-10-02"})

    def test_a_bad_checksum_is_refused_and_not_cached(self):
        name = "BTCUSDT-5m-2026-09.zip"
        fake = FakeArchive({name: kline_zip(date(2026, 9, 1))}, bad_checksum=[name])
        with self.assertRaises(bv.ChecksumMismatch):
            bv.fetch_file("BTCUSDT", "5m", "2026-09", "monthly", fake, self.cache)
        self.assertFalse((self.cache / "monthly" / "BTCUSDT" / name).exists())

    def test_a_cached_file_is_not_fetched_again(self):
        name = "BTCUSDT-5m-2026-09.zip"
        fake = FakeArchive({name: kline_zip(date(2026, 9, 1))})
        bv.fetch_file("BTCUSDT", "5m", "2026-09", "monthly", fake, self.cache)
        n = len(fake.urls)
        bv.fetch_file("BTCUSDT", "5m", "2026-09", "monthly", fake, self.cache)
        self.assertEqual(len(fake.urls), n)

    def test_history_reports_names_with_no_files(self):
        files = {"BTCUSDT-5m-2026-10-02.zip": kline_zip(date(2026, 10, 2))}
        bars, failed = bv.history(["BTC-USD", "NOPE-USD"], days=1, today=date(2026, 10, 3),
                                  get=FakeArchive(files), cache=self.cache)
        self.assertEqual(sorted(bars), ["BTC-USD"])
        self.assertIn("NOPEUSDT", failed["NOPE-USD"])

    def test_a_network_drop_is_retried_after_a_pause(self):
        import urllib.error
        files = {"BTCUSDT-5m-2026-10-02.zip": kline_zip(date(2026, 10, 2))}
        good = FakeArchive(files)
        calls = {"n": 0}

        def flaky(url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("nodename nor servname provided")
            return good(url)

        pauses = []
        bars, failed = bv.history(["BTC-USD"], days=1, today=date(2026, 10, 3), get=flaky, cache=self.cache,
                                  sleep=pauses.append)
        self.assertEqual(sorted(bars), ["BTC-USD"])
        self.assertEqual(failed, {})
        self.assertEqual(pauses, [30.0])


if __name__ == "__main__":
    unittest.main()
