"""
engine._prep_bars reads a frame's columns as arrays (_prep_bars_arrays) instead of looping over its
rows. That is a pure speedup: it must return exactly what the row loop returned, compared here with a
frozen copy of that loop (engine.py at 16661d5) on every key, its order, and every value's type and repr.

  * real-shaped frames take the array path: tz-aware ET (providers), tz-aware UTC (yfinance bulk),
    naive UTC, a fixed offset, a month of equity bars, 24/7 crypto, DST weeks and nights, NaN rows,
    zero / NaN / negative / inf volume, int volume, float32 prices, s/ms/us stamps, unsorted and
    repeated stamps, and 120 random feeds mixing them
  * frames outside its shape (object or nullable columns, MultiIndex or missing columns, non-datetime
    index, NaT, sub-second stamps, far dates) take the row loop and return what it always did
"""

import random
import sys
import unittest
import warnings
from datetime import timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402

ET = ZoneInfo("America/New_York")
NAN = float("nan")
FLAVORS = ("et", "et_zoneinfo", "utc", "naive")


# ── the reference: _to_et and _prep_bars as they were in engine.py at 16661d5 ───────────────────────

def _to_et_ref(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        return t.tz_convert(ET)
    try:
        return t.tz_localize("UTC").tz_convert(ET)
    except Exception:
        try:
            return t.tz_localize(ET)
        except Exception:
            return t


def prep_bars_ref(df):
    bars = []
    if df is None or df.empty:
        return bars
    for ts, row in df.iterrows():
        try:
            o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
            v = float(row["Volume"]) if row["Volume"] == row["Volume"] else 0.0
        except Exception:
            continue
        if not all(map(lambda x: x == x, [o, h, l, c])):
            continue
        t = pd.Timestamp(ts)
        t_et = _to_et_ref(ts)
        try:
            t_ms = int(t.timestamp() * 1000)
        except Exception:
            t_ms = len(bars)
        bars.append({
            "ts": t_ms,
            "d": t_et.strftime("%Y-%m-%d"),
            "mins": t_et.hour * 60 + t_et.minute,
            "o": o, "h": h, "l": l, "c": c, "v": max(0.0, v),
            "hlReal": h > l,
            "time": t_et.strftime("%m-%d %H:%M"),
        })
    return bars


# ── frames ──────────────────────────────────────────────────────────────────────────────────────

def strict(bars):
    """Every key in order with its value's type and repr (repr tells -0.0 from 0.0, inf from a big float)."""
    return [[(k, type(x), repr(x)) for k, x in b.items()] for b in bars]


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
    """The index a feed hands over: tz ET by name (providers._bars_from_rows), zoneinfo ET, UTC
    (yfinance's bulk download), or naive UTC wall clock (free feeds; _to_et reads naive as UTC)."""
    if flavor == "et":
        return stamps_utc.tz_convert("America/New_York")
    if flavor == "et_zoneinfo":
        return stamps_utc.tz_convert(ET)
    if flavor == "utc":
        return stamps_utc.tz_convert(timezone.utc)
    return stamps_utc.tz_localize(None)


def ohlcv(n, rng, price=100.0, f32=True):
    """A random walk printed as the feeds print it: float32 noise (yfinance) or cents, int volume with
    zero-volume bars, and some flat bars (high == low, hlReal False)."""
    close = price * np.exp(np.cumsum(rng.normal(0.0, 0.002, n)))
    open_ = np.r_[price, close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0.0, 0.002, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.0, 0.002, n))
    flat = rng.random(n) < 0.05
    open_[flat] = high[flat] = low[flat] = close[flat]
    vol = rng.integers(1, 80_000, n)
    vol[rng.random(n) < 0.3] = 0
    cols = {"Open": open_, "High": high, "Low": low, "Close": close}
    cols = {k: (x.astype(np.float32).astype(np.float64) if f32 else np.round(x, 2)) for k, x in cols.items()}
    return {**cols, "Volume": vol}


def frame(stamps_utc, flavor, seed, **kw):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(ohlcv(len(stamps_utc), rng, **kw), index=index_as(stamps_utc, flavor))


def with_gaps(df, seed, frac=0.04):
    """Feed nulls: a NaN in one of O/H/L/C, whole NaN rows, NaN / negative / -0.0 volume; and an inf
    high and volume, which the row loop keeps."""
    rng = np.random.default_rng(seed)
    df = df.astype({"Volume": np.float64})
    n = len(df)
    df.iloc[int(rng.integers(0, n)), 1] = float("inf")
    for i in rng.choice(n, int(n * frac), replace=False):
        df.iloc[i, int(rng.integers(0, 4))] = NAN
    for i in rng.choice(n, int(n * frac / 2), replace=False):
        df.iloc[i, :] = NAN
    v = df["Volume"].to_numpy(copy=True)
    v[rng.choice(n, int(n * frac), replace=False)] = NAN
    v[rng.choice(n, int(n * frac / 2), replace=False)] = -250.0
    v[rng.choice(n, min(n, 3), replace=False)] = -0.0
    v[int(rng.integers(0, n))] = float("inf")
    df["Volume"] = v
    return df


class PrepBarsCase(unittest.TestCase):

    def assertSameBars(self, df, array_path=True):
        """_prep_bars(df) is the reference's list; array_path says which branch must have produced it."""
        bars = engine._prep_bars(df)
        got, want = strict(bars), strict(prep_bars_ref(df))
        if got != want:   # name the first bar that differs (a diff of thousands of bars takes minutes)
            i = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want)))
            self.fail(f"bar {i} of {len(want)} (got {len(got)} bars): {got[i:i + 1]} != {want[i:i + 1]}")
        if df is not None and not df.empty:
            self.assertEqual(engine._prep_bars_arrays(df) is not None, array_path)
        return bars


class TestRealShapedFrames(PrepBarsCase):

    def test_a_month_of_equity_bars_in_every_index_flavor(self):
        stamps = equity_stamps("2026-10-05", "2026-11-06")          # DST ends Sun Nov 1
        for k, flavor in enumerate(FLAVORS):
            with self.subTest(flavor=flavor):
                bars = self.assertSameBars(frame(stamps, flavor, k))
                self.assertEqual(len(bars), 25 * 192)
                self.assertEqual((bars[0]["time"], bars[-1]["time"]), ("10-05 04:00", "11-06 19:55"))

    def test_crypto_around_the_clock_in_every_index_flavor(self):
        stamps = crypto_stamps("2026-09-25 04:00", 2511)           # ET midnight 8 days back, as the desk
        for k, flavor in enumerate(FLAVORS):
            with self.subTest(flavor=flavor):
                bars = self.assertSameBars(frame(stamps, flavor, 10 + k, price=60_000.0, f32=False))
                self.assertEqual(bars[0]["time"], "09-25 00:00")

    def test_dst_weeks_and_nights(self):
        cases = {
            "equity week, DST starts": equity_stamps("2026-03-02", "2026-03-13"),
            "equity week, DST ends": equity_stamps("2026-10-26", "2026-11-06"),
            "crypto night, DST starts": crypto_stamps("2026-03-07 12:00", 600),
            "crypto night, DST ends": crypto_stamps("2026-10-31 12:00", 600),
        }
        for k, (name, stamps) in enumerate(cases.items()):
            for flavor in FLAVORS:
                with self.subTest(case=name, flavor=flavor):
                    bars = self.assertSameBars(frame(stamps, flavor, 20 + k))
                    if name == "crypto night, DST starts":           # 02:00-02:55 ET never happens
                        self.assertFalse([b for b in bars if b["d"] == "2026-03-08" and 120 <= b["mins"] < 180])
                    if name == "crypto night, DST ends":             # 01:00-01:55 ET happens twice
                        self.assertEqual(sum(b["d"] == "2026-11-01" and b["mins"] == 90 for b in bars), 2)

    def test_nan_rows_and_zero_nan_negative_volume(self):
        stamps = equity_stamps("2026-09-14", "2026-09-25")
        for k, flavor in enumerate(FLAVORS):
            with self.subTest(flavor=flavor):
                df = with_gaps(frame(stamps, flavor, 30 + k), 40 + k)
                bars = self.assertSameBars(df)
                ohlc_nan = df[["Open", "High", "Low", "Close"]].isna().any(axis=1)
                self.assertEqual(len(bars), int((~ohlc_nan).sum()))
                vol = df["Volume"][~ohlc_nan].to_numpy()
                self.assertEqual([b["v"] for b in bars], [x if x > 0 else 0.0 for x in vol])
                self.assertGreater(sum(b["v"] == 0.0 for b in bars), sum(x == 0.0 for x in vol))

    def test_feed_dtypes_units_and_order(self):
        base = frame(equity_stamps("2026-09-21", "2026-09-25"), "utc", 50)
        frames = {
            "float64 volume": base.astype({"Volume": np.float64}),
            "float32 prices, uint volume": base.astype({c: np.float32 for c in ("Open", "High", "Low", "Close")})
                                               .astype({"Volume": np.uint32}),
            "int prices": base.assign(**{c: (base[c] * 100).round().astype(np.int64)
                                         for c in ("Open", "High", "Low", "Close")}),
            "extra columns": base.assign(Dividends=0.0, Symbol="AAPL"),
            "fixed-offset tz": base.set_axis(base.index.tz_convert(timezone(timedelta(hours=-5)))),
            "unsorted": base.sample(frac=1.0, random_state=1),
            "repeated stamps": pd.concat([base, base.iloc[-5:]]),
            "single bar": base.iloc[:1],
        }
        for unit in ("s", "ms", "us"):
            frames[f"{unit} stamps"] = base.set_axis(base.index.as_unit(unit))
        for name, df in frames.items():
            with self.subTest(frame=name):
                self.assertSameBars(df)


class TestRandomFrames(PrepBarsCase):

    def test_random_feeds(self):
        rng = random.Random(7)
        starts = ("2026-03-07 18:00", "2026-10-31 18:00", "2026-01-02 14:30", "2024-02-29 00:00", "2031-11-02 03:00")
        for k in range(120):
            n = rng.randint(1, 400)
            step = rng.choice(("1min", "5min", "5min", "15min", "1h", "1D"))
            stamps = pd.date_range(starts[k % len(starts)], periods=n, freq=step, tz="UTC")
            stamps = stamps[np.sort(rng.sample(range(n), max(1, n - rng.randint(0, n // 4))))]   # gaps
            df = frame(stamps, rng.choice(FLAVORS), k, price=rng.choice((0.0004, 3.2, 180.0, 64_000.0)),
                       f32=rng.random() < 0.5)
            if rng.random() < 0.6:
                df = with_gaps(df, k, frac=rng.choice((0.02, 0.2, 0.9)))
            if rng.random() < 0.2:
                df = df.sample(frac=1.0, random_state=k)
            with self.subTest(case=k):
                self.assertSameBars(df)


class TestFramesOutsideTheArrayPath(PrepBarsCase):

    def setUp(self):
        self.base = frame(equity_stamps("2026-09-21", "2026-09-22"), "utc", 60)

    def test_object_and_nullable_columns(self):
        b = self.base.iloc[:12]
        frames = {
            "non-numeric prices": b.astype({"Open": object, "Close": object}),
            "nullable floats": b.astype({"High": "Float64", "Volume": "Float64"}),
        }
        frames["non-numeric prices"].iloc[[1, 4], 0] = ["n/a", None]
        frames["non-numeric prices"].iloc[[2, 3, 5], 3] = ["101.25", Decimal("99.5"), " inf "]
        frames["nullable floats"].iloc[[2, 7], [1, 4]] = pd.NA
        for name, df in frames.items():
            with self.subTest(frame=name):
                bars = self.assertSameBars(df, array_path=False)
                self.assertLess(len(bars), len(df))

    def test_columns_and_index_it_does_not_take(self):
        b = self.base.iloc[:12]
        sub_second = b.set_axis(b.index + pd.to_timedelta(np.arange(12) * 250, unit="ms"))
        nat = b.copy()
        nat.iloc[3, :4] = NAN
        nat.index = nat.index.where(np.arange(12) != 3, pd.NaT)
        frames = {
            "MultiIndex columns": pd.concat({"AAPL": b}, axis=1).swaplevel(axis=1),
            "no Volume": b.drop(columns="Volume"),
            "two Close columns": pd.concat([b, b[["Close"]]], axis=1),
            "RangeIndex": b.reset_index(drop=True),
            "string stamps": b.set_axis(b.index.strftime("%Y-%m-%d %H:%M:%S")),
            "sub-second stamps": sub_second,
            "NaT on a NaN row": nat,
            "far future": b.set_axis(b.index + pd.DateOffset(years=150)),
            "beyond the ns range": b.set_axis(pd.DatetimeIndex(
                b.index.tz_localize(None).to_numpy().astype("datetime64[s]") + np.timedelta64(500 * 365, "D"))),
        }
        for name, df in frames.items():
            with self.subTest(frame=name), warnings.catch_warnings():
                # MultiIndex columns: the row loop's float() of a one-item Series warns on pandas 2 and
                # raises on 3; the row is skipped either way
                warnings.simplefilter("ignore", FutureWarning)
                self.assertSameBars(df, array_path=False)

    def test_none_and_empty(self):
        for df in (None, pd.DataFrame(), self.base.iloc[:0], pd.DataFrame(index=self.base.index)):
            self.assertEqual(engine._prep_bars(df), [])


if __name__ == "__main__":
    unittest.main()
