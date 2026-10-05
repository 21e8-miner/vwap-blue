"""
data.session_dollar_volume finds the last row's ET calendar day with numpy day numbers for the whole
index (_et_days) instead of pd.to_datetime, a date object per bar and a list mask. That is a pure
speedup: it must return exactly the float the old code returned, compared here bit for bit with a
frozen copy of it (data.py at 82c8da5). The float feeds the liquidity floor, the desk's dollar_vol
column and rotation_score, and the Python side of tests/test_engine_parity.py.

  * real-shaped frames take the day-number path: tz ET by name (providers._bars_from_rows), tz UTC
    (yfinance's bulk download through _split_batch), naive UTC, zoneinfo and dateutil ET, a fixed
    offset; a month of equity bars, 24/7 crypto, DST weeks and nights, a last bar at ET midnight,
    NaN rows, zero / NaN / negative volume, inf, int / float32 / nullable / object columns,
    s/ms/us/ns stamps, unsorted and repeated stamps, and 150 random feeds mixing them
  * frames outside its shape (a non-datetime index, NaT, stamps outside 1900-2100) take the per-stamp
    path, frames without Close/Volume columns go through _norm_ohlcv, frames that raise take the
    fallbacks, and all of them return what they always did
"""

import math
import random
import struct
import sys
import unittest
import warnings
from datetime import timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from dateutil import tz as dateutil_tz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import data  # noqa: E402
from providers import _bars_from_rows  # noqa: E402

ET = ZoneInfo("America/New_York")
NAN = float("nan")
FLAVORS = ("et", "et_zoneinfo", "et_dateutil", "utc", "naive")


# ── the reference: session_dollar_volume as it was in data.py at 82c8da5 ────────────────────────────

def session_dollar_volume_ref(df):
    if df is None or getattr(df, "empty", True):
        return 0.0
    try:
        if not {"Close", "Volume"}.issubset(set(map(str, df.columns))):
            n = data._norm_ohlcv(df)
        else:
            n = df
        if n is None or n.empty:
            return 0.0
        ts = pd.to_datetime(n.index, utc=True, errors="coerce")
        if ts.isna().all():
            day = n.tail(min(100, len(n)))
        else:
            # group by US/Eastern calendar day
            try:
                days = ts.tz_convert("America/New_York").date
            except Exception:
                days = pd.DatetimeIndex(ts).tz_localize(None).date
            last = days[-1]
            mask = [d == last for d in days]
            day = n.loc[mask]
            if day is None or len(day) == 0:
                day = n.tail(min(100, len(n)))
        c = pd.to_numeric(day["Close"], errors="coerce").fillna(0.0)
        v = pd.to_numeric(day["Volume"], errors="coerce").fillna(0.0)
        return float((c * v).sum())
    except Exception:
        try:
            c = pd.to_numeric(df["Close"], errors="coerce").fillna(0.0).tail(100)
            v = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0).tail(100)
            return float((c * v).sum())
        except Exception:
            return 0.0


# ── frames ──────────────────────────────────────────────────────────────────────────────────────

def bits(x):
    """The value's type and its 8 bytes: tells -0.0 from 0.0, and NaN compares equal to itself."""
    return type(x), struct.pack("<d", x)


def equity_stamps(first, last):
    """5m bars 04:00-19:55 ET (premarket, RTH, after hours) on the weekdays first..last, as UTC stamps."""
    out = []
    for day in pd.date_range(first, last, freq="D"):
        if day.weekday() < 5:
            d = day.strftime("%Y-%m-%d")
            out.extend(pd.date_range(f"{d} 04:00", f"{d} 19:55", freq="5min", tz=ET).tz_convert("UTC"))
    return pd.DatetimeIndex(out)


def crypto_stamps(start_utc, n):
    return pd.date_range(start_utc, periods=n, freq="5min", tz="UTC")


def index_as(stamps_utc, flavor):
    """The index a feed hands over: tz ET by name (providers), zoneinfo or dateutil ET, UTC (yfinance's
    bulk download), or naive UTC wall clock (free feeds; session_dollar_volume reads naive as UTC)."""
    if flavor == "et":
        return stamps_utc.tz_convert("America/New_York")
    if flavor == "et_zoneinfo":
        return stamps_utc.tz_convert(ET)
    if flavor == "et_dateutil":
        return stamps_utc.tz_convert(dateutil_tz.gettz("America/New_York"))
    if flavor == "utc":
        return stamps_utc.tz_convert(timezone.utc)
    return stamps_utc.tz_localize(None)


def ohlcv(n, rng, price=100.0, f32=True):
    """A random walk printed as the feeds print it: float32 noise (yfinance) or cents, int volume with
    zero-volume bars."""
    close = price * np.exp(np.cumsum(rng.normal(0.0, 0.002, n)))
    open_ = np.r_[price, close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0.0, 0.002, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.0, 0.002, n))
    vol = rng.integers(1, 80_000, n)
    vol[rng.random(n) < 0.3] = 0
    cols = {"Open": open_, "High": high, "Low": low, "Close": close}
    cols = {k: (x.astype(np.float32).astype(np.float64) if f32 else np.round(x, 2)) for k, x in cols.items()}
    return {**cols, "Volume": vol}


def frame(stamps_utc, flavor, seed, **kw):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(ohlcv(len(stamps_utc), rng, **kw), index=index_as(stamps_utc, flavor))


def with_gaps(df, seed, frac=0.04):
    """Feed nulls: a NaN close, whole NaN rows, NaN / negative / -0.0 volume."""
    rng = np.random.default_rng(seed)
    df = df.astype({"Volume": np.float64})
    n = len(df)
    for i in rng.choice(n, int(n * frac), replace=False):
        df.iloc[i, int(rng.integers(0, 4))] = NAN
    for i in rng.choice(n, int(n * frac / 2), replace=False):
        df.iloc[i, :] = NAN
    v = df["Volume"].to_numpy(copy=True)
    v[rng.choice(n, int(n * frac), replace=False)] = NAN
    v[rng.choice(n, int(n * frac / 2), replace=False)] = -250.0
    v[rng.choice(n, min(n, 3), replace=False)] = -0.0
    df["Volume"] = v
    return df


def last_et_day_sum(df):
    """An independent answer for a frame with a datetime index: math.fsum of close × volume over the
    rows on the last row's ET day, one zoneinfo conversion per stamp (naive stamps are UTC)."""
    idx = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
    days = [t.astimezone(ET).date() for t in idx.to_pydatetime()]
    c = pd.to_numeric(df["Close"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    v = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    on = [d == days[-1] for d in days]
    return math.fsum(c[i] * v[i] for i in range(len(df)) if on[i]), sum(on)


class DollarVolumeCase(unittest.TestCase):

    def assertSameDvol(self, df, fast=True):
        """session_dollar_volume(df) is the reference's float, bit for bit; fast says whether the
        frame's index must have taken the day-number path (_et_days) or the per-stamp one."""
        with warnings.catch_warnings():
            # the per-stamp path's pd.to_datetime warns on some indexes (strings it parses one by one)
            warnings.simplefilter("ignore", UserWarning)
            got, want = data.session_dollar_volume(df), session_dollar_volume_ref(df)
        self.assertEqual(bits(got), bits(want), f"{got!r} != {want!r}")
        if fast is not None:
            n = df if {"Close", "Volume"}.issubset(set(map(str, df.columns))) else data._norm_ohlcv(df)
            self.assertEqual(data._et_days(n.index) is not None, fast)
        return got

    def assertSameDays(self, idx):
        """Every stamp's ET day from _et_days is the date the old pd.to_datetime(...).date gave it."""
        got = data._et_days(idx).tolist()
        want = list(pd.to_datetime(idx, utc=True, errors="coerce").tz_convert("America/New_York").date)
        if got != want:   # name the first stamp that differs (a diff of thousands of days takes minutes)
            i = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
            self.fail(f"stamp {i} of {len(want)} ({idx[i]}): {got[i:i + 1]} != {want[i:i + 1]}")

    def assertLastDay(self, df, rows):
        """The float is close × volume over the last ET day, which has this many rows."""
        want, n = last_et_day_sum(df)
        self.assertEqual(n, rows)
        self.assertTrue(math.isclose(self.assertSameDvol(df), want, rel_tol=1e-12, abs_tol=1e-9))


class TestRealShapedFrames(DollarVolumeCase):

    def test_a_month_of_equity_bars_in_every_index_flavor(self):
        stamps = equity_stamps("2026-09-03", "2026-10-02")         # the replay month, 21 sessions
        for k, flavor in enumerate(FLAVORS):
            with self.subTest(flavor=flavor):
                df = frame(stamps, flavor, k)
                self.assertLastDay(df, 192)                          # Oct 2, 04:00-19:55 ET
                self.assertSameDays(df.index)

    def test_provider_frames(self):
        """Bars as providers._bars_from_rows builds them: epoch-ms rows, index tz ET by name."""
        for k, stamps in enumerate((equity_stamps("2026-09-21", "2026-10-02"),
                                    crypto_stamps("2026-09-25 04:00", 2511))):
            rng = np.random.default_rng(70 + k)
            cols = ohlcv(len(stamps), rng, price=(180.0, 64_000.0)[k], f32=False)
            ms = (stamps.as_unit("ns").asi8 // 10**6).tolist()
            rows = [(t, *(float(cols[c][i]) for c in ("Open", "High", "Low", "Close", "Volume")))
                    for i, t in enumerate(ms)]
            df = _bars_from_rows(rows)
            with self.subTest(case=("equity", "crypto")[k]):
                self.assertEqual(str(df.index.tz), "America/New_York")
                self.assertLastDay(df, (192, 207)[k])               # crypto: 2,511 bars end at 17:10 ET
                self.assertSameDays(df.index)

    def test_yfinance_bulk_frames(self):
        """yf.download(group_by="ticker") of several names: (ticker, field) columns on one UTC index, NaN
        where a name has no bar, split per name by data._split_batch."""
        stamps = equity_stamps("2026-09-28", "2026-10-02")
        parts = {}
        for k, t in enumerate(("AAPL", "MSFT", "THIN")):
            f = frame(stamps, "utc", 80 + k)
            if t == "THIN":                                          # trades a few bars a day
                f.iloc[np.random.default_rng(9).random(len(f)) < 0.9] = NAN
            parts[t] = f
        raw = pd.concat(parts, axis=1)
        split = data._split_batch(raw, list(parts))
        self.assertEqual(sorted(split), ["AAPL", "MSFT", "THIN"])
        for t, df in split.items():
            with self.subTest(ticker=t):
                self.assertEqual(str(df.index.tz), "UTC")
                self.assertSameDvol(df)
                self.assertSameDays(df.index)

    def test_crypto_around_the_clock_in_every_index_flavor(self):
        stamps = crypto_stamps("2026-09-25 04:00", 2600)            # ends Oct 4, 00:35 ET
        for k, flavor in enumerate(FLAVORS):
            with self.subTest(flavor=flavor):
                df = frame(stamps, flavor, 10 + k, price=60_000.0, f32=False)
                self.assertLastDay(df, 8)                            # 00:00-00:35 ET
                self.assertSameDays(df.index)

    def test_the_last_bar_at_and_around_et_midnight(self):
        """The ET day turns at 04:00 UTC in summer and 05:00 UTC in winter, not at UTC midnight."""
        ends = {
            "00:00 ET, EDT": ("2026-10-03 04:00", 1), "23:55 ET, EDT": ("2026-10-03 03:55", 288),
            "00:00 ET, EST": ("2026-12-04 05:00", 1), "23:55 ET, EST": ("2026-12-04 04:55", 288),
            "00:00 UTC, EDT": ("2026-10-03 00:00", 241), "00:00 UTC, EST": ("2026-12-04 00:00", 229),
        }
        for k, (name, (end, rows)) in enumerate(ends.items()):
            stamps = pd.date_range(end=end, periods=900, freq="5min", tz="UTC")
            for flavor in FLAVORS:
                with self.subTest(end=name, flavor=flavor):
                    self.assertLastDay(frame(stamps, flavor, 90 + k), rows)

    def test_dst_weeks_and_nights(self):
        cases = {
            "equity week, DST starts": (equity_stamps("2026-03-02", "2026-03-13"), 192),
            "equity week, DST ends": (equity_stamps("2026-10-26", "2026-11-06"), 192),
            "crypto through Mar 8 (23 hours)": (pd.date_range(end="2026-03-09 03:55", periods=600,
                                                              freq="5min", tz="UTC"), 276),
            "crypto through Nov 1 (25 hours)": (pd.date_range(end="2026-11-02 04:55", periods=800,
                                                              freq="5min", tz="UTC"), 300),
            "crypto ending in the skipped hour's place": (pd.date_range(end="2026-03-08 07:00", periods=500,
                                                                        freq="5min", tz="UTC"), 25),
            "crypto ending in the repeated hour": (pd.date_range(end="2026-11-01 06:30", periods=500,
                                                                 freq="5min", tz="UTC"), 31),
        }
        for k, (name, (stamps, rows)) in enumerate(cases.items()):
            for flavor in FLAVORS:
                with self.subTest(case=name, flavor=flavor):
                    df = frame(stamps, flavor, 20 + k)
                    self.assertLastDay(df, rows)
                    self.assertSameDays(df.index)

    def test_nan_rows_and_zero_nan_negative_volume(self):
        stamps = equity_stamps("2026-09-14", "2026-09-25")
        for k, flavor in enumerate(FLAVORS):
            with self.subTest(flavor=flavor):
                self.assertLastDay(with_gaps(frame(stamps, flavor, 30 + k), 40 + k), 192)

    def test_values_that_are_not_plain(self):
        """inf, NaN, -0.0 and a day with no volume: each bit of the float comes from the same sum."""
        base = frame(crypto_stamps("2026-10-02 12:00", 300), "utc", 50)        # last ET day: 108 bars
        last = base.index[-1].tz_convert(ET).date()
        on_last = np.array([t.tz_convert(ET).date() == last for t in base.index])
        inf_last, inf_early = base.astype({"Volume": np.float64}), base.astype({"Volume": np.float64})
        inf_last.iloc[-5, 3] = inf_early.iloc[10, 3] = float("inf")
        inf_minus_inf = inf_last.assign(Volume=np.where(on_last, 3.0, 0.0))
        inf_minus_inf.iloc[-2, 4] = -float("inf")
        frames = {
            "inf close on a bar with volume": inf_last.assign(Volume=np.where(on_last, 3.0, 0.0)),
            "inf close on a bar without volume": inf_last.assign(Volume=np.where(on_last, 0.0, 3.0)),
            "inf close and -inf volume": inf_minus_inf,
            "inf close before the last day": inf_early,
            "no volume on the last day": base.assign(Volume=np.where(on_last, 0, base["Volume"])),
            "-0.0 close": base.assign(Close=-0.0, Volume=np.where(on_last, 7.0, 0.0)),
            "huge values": base.assign(Close=1e300, Volume=1e10),
        }
        with np.errstate(invalid="ignore"):                                      # inf - inf in the sum
            for name, df in frames.items():
                with self.subTest(frame=name):
                    self.assertSameDvol(df)
            dvol = {name: data.session_dollar_volume(df) for name, df in frames.items()}
        self.assertEqual(dvol["inf close on a bar with volume"], float("inf"))
        self.assertTrue(math.isfinite(dvol["inf close on a bar without volume"]))   # inf × 0 is a NaN sum() skips
        self.assertTrue(math.isnan(dvol["inf close and -inf volume"]))
        self.assertTrue(math.isfinite(dvol["inf close before the last day"]))
        self.assertEqual(bits(dvol["-0.0 close"]), bits(0.0))                        # numpy's sum starts at +0.0

    def test_feed_dtypes_units_and_order(self):
        base = frame(equity_stamps("2026-09-21", "2026-09-25"), "utc", 60)
        frames = {
            "float64 volume": base.astype({"Volume": np.float64}),
            "float32 prices, uint volume": base.astype({c: np.float32 for c in ("Open", "High", "Low", "Close")})
                                               .astype({"Volume": np.uint32}),
            "int prices": base.assign(**{c: (base[c] * 100).round().astype(np.int64)
                                         for c in ("Open", "High", "Low", "Close")}),
            "nullable columns": base.astype({"Close": "Float64", "Volume": "Int64"}),
            "object close": base.astype({"Close": object}),
            "close and volume only": base[["Volume", "Close"]],
            "extra columns": base.assign(Dividends=0.0, Symbol="AAPL"),
            "fixed-offset tz": base.set_axis(base.index.tz_convert(timezone(timedelta(hours=-5)))),
            "tz far from ET": base.set_axis(base.index.tz_convert("Asia/Kolkata")),
            "unsorted": base.sample(frac=1.0, random_state=1),
            "newest first": base.iloc[::-1],
            "last row from an earlier day": pd.concat([base, base.iloc[:3]]),
            "repeated stamps": pd.concat([base, base.iloc[-5:]]),
            "every stamp three times (pd.to_datetime's cache path)": base.iloc[np.repeat(np.arange(len(base)), 3)],
            "named index": base.rename_axis("Datetime"),
            "single bar": base.iloc[:1],
        }
        frames["nullable columns"].iloc[[-2, -7], 3] = pd.NA
        frames["object close"].iloc[[-1, -4, -9], 3] = ["n/a", None, "101.25"]
        for unit in ("s", "ms", "us", "ns"):
            for flavor in ("et", "naive"):
                frames[f"{unit} stamps, {flavor}"] = base.set_axis(index_as(base.index, flavor).as_unit(unit))
        for name, df in frames.items():
            with self.subTest(frame=name):
                self.assertSameDvol(df)
                self.assertSameDays(df.index)


class TestRandomFrames(DollarVolumeCase):

    def test_random_feeds(self):
        rng = random.Random(7)
        starts = ("2026-03-07 18:00", "2026-10-31 18:00", "2026-01-02 14:30", "2024-02-29 00:00",
                  "2031-11-02 03:00", "1999-12-31 22:00")
        for k in range(150):
            n = rng.randint(1, 600)
            step = rng.choice(("1min", "5min", "5min", "15min", "1h", "1D"))
            stamps = pd.date_range(starts[k % len(starts)], periods=n, freq=step, tz="UTC")
            stamps = stamps[np.sort(rng.sample(range(n), max(1, n - rng.randint(0, n // 4))))]   # gaps
            df = frame(stamps, rng.choice(FLAVORS), k, price=rng.choice((0.0004, 3.2, 180.0, 64_000.0)),
                       f32=rng.random() < 0.5)
            if rng.random() < 0.6:
                df = with_gaps(df, k, frac=rng.choice((0.02, 0.2, 0.9)))
            if rng.random() < 0.2:
                df = df.sample(frac=1.0, random_state=k)
            if rng.random() < 0.3:
                df = df.set_axis(df.index.as_unit(rng.choice(("s", "ms", "us", "ns"))))
            with self.subTest(case=k):
                self.assertSameDvol(df)
                self.assertSameDays(df.index)


class TestFramesOutsideTheFastPath(DollarVolumeCase):

    @staticmethod
    def bars(idx):
        """Distinct closes so that which rows were summed shows in the float."""
        n = len(idx)
        close = np.arange(1, n + 1, dtype=np.float64) * 1.25
        return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                             "Volume": np.arange(n, 0, -1, dtype=np.float64) * 10.0}, index=idx)

    def test_indexes_that_are_not_datetime(self):
        stamps = pd.date_range("2026-10-01 22:00", periods=10, freq="h", tz="UTC")
        frames = {
            "RangeIndex": self.bars(pd.RangeIndex(10)),
            "epoch-ms integers": self.bars(pd.Index(stamps.as_unit("ms").asi8)),
            "floats": self.bars(pd.Index(np.linspace(1.5, 9.5, 10))),
            "strings": self.bars(pd.Index(stamps.strftime("%Y-%m-%d %H:%M:%S"))),
            "strings with offsets": self.bars(pd.Index(stamps.tz_convert(ET).strftime("%Y-%m-%dT%H:%M:%S%z"))),
            "Timestamps in mixed zones": self.bars(pd.Index(
                [t.tz_convert(("UTC", "Asia/Tokyo", "America/New_York")[i % 3]) for i, t in enumerate(stamps)],
                dtype=object)),
            "PeriodIndex (all NaT to pd.to_datetime)": self.bars(pd.period_range("2026-10-01 22:00", periods=10,
                                                                                 freq="h")),
            "MultiIndex rows": self.bars(pd.MultiIndex.from_product([["AAPL"], stamps])),
        }
        for name, df in frames.items():
            with self.subTest(frame=name):
                self.assertSameDvol(df, fast=False)

    def test_nat_in_the_index(self):
        stamps = list(pd.date_range("2026-10-01 22:00", periods=10, freq="h", tz="UTC"))
        frames = {
            "NaT in the middle": stamps[:4] + [pd.NaT] + stamps[5:],
            "NaT last": stamps[:9] + [pd.NaT],
            "all NaT": [pd.NaT] * 10,
        }
        for name, idx in frames.items():
            with self.subTest(frame=name):
                self.assertSameDvol(self.bars(pd.DatetimeIndex(idx)), fast=False)

    def test_stamps_outside_1900_2100(self):
        def stamps(*s, unit="ns"):
            return pd.DatetimeIndex(np.array(s, dtype=f"datetime64[{unit}]"))
        frames = {
            "1899 into 1900": stamps("1899-12-31T23:00", "1900-01-01T03:00", "1900-01-01T05:00", "1900-01-01T06:00"),
            "2099 into 2100": stamps("2099-12-31T23:00", "2100-01-01T03:00", "2100-01-01T05:00", "2100-01-01T06:00"),
            "one 2150 stamp in the middle": stamps("2026-10-01T23:00", "2026-10-02T03:00", "2150-01-01T05:00",
                                                   "2026-10-02T05:00"),
            "the ns range's ends": pd.DatetimeIndex([pd.Timestamp.min + pd.Timedelta(minutes=1),
                                                     pd.Timestamp("2026-10-02"),
                                                     pd.Timestamp.max - pd.Timedelta(minutes=1)]),
            "year 2500 (s)": stamps("2500-06-01T13:00", "2500-06-01T23:00", "2500-06-02T03:00", "2500-06-02T05:00",
                                    unit="s"),
            # the first hours are in the year 0 in ET: the old code's ET .date raised and it fell back
            # to UTC days (the last one, Jan 2, holds 7 of the 31 bars)
            "year 1 (s)": pd.date_range("0001-01-01", periods=31, freq="h", unit="s"),
            # past 9999 no date at all: the outer except's last 100 rows
            "into year 10000 (s)": stamps("9999-12-31T22:00", "9999-12-31T23:00", "10000-01-01T00:00",
                                          "10000-01-01T05:00", unit="s"),
        }
        for name, idx in frames.items():
            with self.subTest(frame=name):
                self.assertSameDvol(self.bars(idx), fast=False)

    def test_columns_through_norm_ohlcv(self):
        b = frame(equity_stamps("2026-09-28", "2026-10-02"), "et", 61)
        frames = {
            "lower-case columns": b.rename(columns=str.lower),
            "Adj Close for Close": b.rename(columns={"Close": "Adj Close"}),
            "(field, ticker) columns": pd.concat({"AAPL": b}, axis=1).swaplevel(axis=1),
            "(ticker, field) columns": pd.concat({"AAPL": b}, axis=1),
        }
        for name, df in frames.items():
            with self.subTest(frame=name):
                self.assertSameDvol(df, fast=None if name == "(ticker, field) columns" else True)
        self.assertEqual(data.session_dollar_volume(frames["(ticker, field) columns"]), 0.0)   # no OHLCV

    def test_frames_that_raise_or_have_nothing(self):
        b = frame(equity_stamps("2026-09-28", "2026-10-02"), "utc", 62)
        frames = {
            "two Close columns": pd.concat([b, b[["Close"]]], axis=1),               # the except paths: 0.0
            "no Volume": b.drop(columns="Volume"),                                     # _norm_ohlcv: None
            "all-NaN rows": b.iloc[:12] * NAN,                                         # NaN fills as 0.0
            "columns but no rows": b.iloc[:0],
            "rows but no columns": pd.DataFrame(index=b.index),
            "an empty frame": pd.DataFrame(),
        }
        for name, df in frames.items():
            with self.subTest(frame=name):
                self.assertEqual(self.assertSameDvol(df, fast=None), 0.0)
        for df in (None, b["Close"]):                                                  # not a frame at all
            self.assertEqual(data.session_dollar_volume(df), session_dollar_volume_ref(df))


if __name__ == "__main__":
    unittest.main()
