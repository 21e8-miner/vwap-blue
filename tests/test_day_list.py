"""
analyze_bars used to read every bar's ET day three times: _sessions(bars) for the days, then
_first_idx(bars, day) for the first bar of the last day (i0) and of the day before (p0). It now reads
them once into a list and takes the days (dict.fromkeys) and both first bars (list.index) from it, and
_sessions is that same one-liner. A pure speedup: compared with frozen copies of _sessions and
_first_idx as they were in engine.py at 9957d9b.

  * _sessions returns the same list (the same string objects, in first-appearance order) for parsed
    parity tapes, unsorted and interleaved bars, bars from the row loop (a string per bar) and none
  * analyze_bars hands _resolve_day and _rvol the days, i0 and p0 the old passes found: parity cases
    (full histories, live cuts, decision-time prefixes at triggers), unsorted bars, days interleaved,
    and a single crypto session (split into D0/D1 on copies)
  * a single equity session is still "need ≥2 sessions"
"""

import random
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine                                                # noqa: E402
from test_engine_parity import make_cases, make_series       # noqa: E402


# ── the reference: _sessions and _first_idx as they were in engine.py at 9957d9b ────────────────────

def sessions_ref(bars):
    seen, out = set(), []
    for b in bars:
        if b["d"] not in seen:
            seen.add(b["d"])
            out.append(b["d"])
    return out


def first_idx_ref(bars, day):
    for i, b in enumerate(bars):
        if b["d"] == day:
            return i
    return -1


def parse(rows):
    """(utc time, o, h, l, c, volume) rows, None for a feed null, through engine._prep_bars."""
    nan = float("nan")
    cols = {k: [nan if r[j] is None else r[j] for r in rows] for j, k in enumerate(("Open", "High", "Low", "Close"), 1)}
    cols["Volume"] = [float(r[5]) for r in rows]
    return engine._prep_bars(pd.DataFrame(cols, index=pd.DatetimeIndex([r[0] for r in rows])))


def one_session(rng):
    """One ET day of 5m bars around the clock (Sep 15, 00:00-23:55 ET)."""
    px = 100 * (1 + pd.Series([rng.gauss(0, 0.002) for _ in range(288)]).cumsum())
    return engine._prep_bars(pd.DataFrame({"Open": px, "High": px * 1.001, "Low": px * 0.999, "Close": px,
                                           "Volume": [rng.uniform(1e3, 1e5) for _ in range(288)]}).set_axis(
        pd.date_range("2026-09-15 04:00", periods=288, freq="5min", tz="UTC")))


def tapes(seed, n):
    """Parsed parity tapes, each also shuffled and with two of its days interleaved."""
    rng = random.Random(seed)
    for k in range(n):
        series = make_series(rng, k)
        if not series:
            continue
        ticker, rows = series
        bars = parse(rows)
        shuffled = bars[:]
        rng.shuffle(shuffled)
        days = sessions_ref(bars)
        woven = bars
        if len(days) >= 3:   # the second day's bars moved between the third day's
            a = [b for b in bars if b["d"] == days[1]]
            rest = [b for b in bars if b["d"] != days[1]]
            j = first_idx_ref(rest, days[2]) + 3
            woven = rest[:j] + a + rest[j:]
        yield ticker, {"parsed": bars, "shuffled": shuffled, "interleaved": woven}


class TestDayList(unittest.TestCase):

    def assertSameSessions(self, bars):
        got, want = engine._sessions(bars), sessions_ref(bars)
        self.assertEqual(got, want)
        self.assertTrue(all(g is w for g, w in zip(got, want)))     # the first bar's string objects

    def test_sessions(self):
        for ticker, shapes in tapes(5, 30):
            for name, bars in shapes.items():
                with self.subTest(ticker=ticker, shape=name):
                    self.assertSameSessions(bars)
        rows_parsed = engine._prep_bars_rows(pd.DataFrame(
            {"Open": [1.0, 2.0, 3.0], "High": [1.5, 2.5, 3.5], "Low": [0.5, 1.5, 2.5], "Close": [1.0, 2.0, 3.0],
             "Volume": [1.0, 1.0, 1.0]},
            index=pd.DatetimeIndex(["2026-09-14 13:30", "2026-09-15 13:30", "2026-09-14 14:00"], tz="UTC")))
        self.assertSameSessions(rows_parsed)                         # a string per bar, not one per day
        self.assertEqual(engine._sessions([]), [])

    def test_analyze_bars_anchors(self):
        seen = []
        resolve, rvol = engine._resolve_day, engine._rvol

        def spy_resolve(bars, i0, iN, p0, opts, memo=None):
            seen.append({"bars": bars, "i0": i0, "iN": iN, "p0": p0})
            return resolve(bars, i0, iN, p0, opts, memo)

        def spy_rvol(bars, days, i0, iN, acc_vol, close_m, memo=None):
            seen[-1]["days"] = days
            return rvol(bars, days, i0, iN, acc_vol, close_m, memo)

        cases = [(t, parse(rows), now) for t, rows, now in make_cases(random.Random(19), 30)]
        now = cases[0][2]
        for ticker, shapes in tapes(11, 20):
            cases += [(ticker, bars, now) for name, bars in shapes.items() if name != "parsed"]
        cases.append(("BTC-USD", one_session(random.Random(3)), now))   # one crypto session: the D0/D1 split
        with mock.patch.object(engine, "_resolve_day", spy_resolve), mock.patch.object(engine, "_rvol", spy_rvol):
            for ticker, bars, at in cases:
                n_before = len(seen)
                engine.analyze_bars(ticker, bars, now=at)
                for call in seen[n_before:]:
                    with self.subTest(ticker=ticker, bars=len(bars)):
                        b = call["bars"]                             # the split's copies, when it split
                        days = sessions_ref(b)
                        self.assertEqual(call["days"], days)
                        self.assertEqual((call["i0"], call["p0"], call["iN"]),
                                         (first_idx_ref(b, days[-1]), first_idx_ref(b, days[-2]), len(b) - 1))
        self.assertGreater(len(seen), 150)
        self.assertTrue(any(call["days"] == ["D0", "D1"] for call in seen))

    def test_one_equity_session_is_still_not_enough(self):
        row = engine.analyze_bars("SPY", one_session(random.Random(3)))
        self.assertEqual(row["error"], "need ≥2 sessions for orange anchor")


if __name__ == "__main__":
    unittest.main()
