"""
engine._rvol adds up each prior day's volume to the last bar's minute in one pass over the bars; it
used to pass over every bar once per prior day (up to 7). That is a pure speedup: it must return
exactly what that loop returned (a frozen copy of engine.py's _rvol at 37f23f8), compared on each
value's type and repr.

  * every call analyze_bars makes on parity cases (full histories, live cuts, decision-time prefixes at
    triggers), captured and run through both
  * the replay's calls: prefixes through every session of parsed parity tapes, equity and crypto close
  * bars the tape never has: unsorted, a prior day in two runs, the same day twice in `days`, zero and
    negative volume, a day whose volumes add up differently one by one than with sum() or fsum, last
    minutes before, inside and after the close, too few days or bases, no volume today
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

RVOL_MAX_PRIORS, RVOL_MIN_PRIORS = 7, 2
CLOSES = (engine.RTH_CLOSE_M, engine.CRYPTO_CLOSE_M)


# ── the reference: _rvol as it was in engine.py at 37f23f8 ──────────────────────────────────────────

def rvol_ref(bars, days, i0, iN, acc_vol, close_m=engine.RTH_CLOSE_M):
    if len(days) < 3 or acc_vol <= 0:
        return None, 0
    last_mins = bars[iN]["mins"]
    priors = days[:-1][-RVOL_MAX_PRIORS:]
    bases = []
    for day in priors:
        cum = 0.0
        for b in bars:
            if b["d"] != day:
                continue
            if b["mins"] <= last_mins and b["mins"] < close_m:
                cum += b["v"]
        if cum > 0:
            bases.append(cum)
    n = len(bases)
    if n < RVOL_MIN_PRIORS:
        return None, n
    return acc_vol / (sum(bases) / n), n


def strict(result):
    return [(type(x), repr(x)) for x in result]


def parse(rows):
    """(utc time, o, h, l, c, volume) rows, None for a feed null, through engine._prep_bars."""
    nan = float("nan")
    cols = {k: [nan if r[j] is None else r[j] for r in rows] for j, k in enumerate(("Open", "High", "Low", "Close"), 1)}
    cols["Volume"] = [float(r[5]) for r in rows]
    return engine._prep_bars(pd.DataFrame(cols, index=pd.DatetimeIndex([r[0] for r in rows])))


def tape(rng, n_days=6, step=5):
    """{d, mins, v} bars around the clock for n_days days (what _rvol reads), volumes with zeros."""
    return [{"d": f"2026-09-{14 + day:02d}", "mins": m, "v": rng.choice((0.0, rng.uniform(1, 5e4)))}
            for day in range(n_days) for m in range(0, 1440, step)]


class TestRvol(unittest.TestCase):

    def assertSameRvol(self, *args):
        self.assertEqual(strict(engine._rvol(*args)), strict(rvol_ref(*args)),
                         f"iN={args[3]} acc_vol={args[4]!r} close_m={args[5]} days={args[1]}")

    def test_every_call_analyze_bars_makes(self):
        calls = []
        rvol = engine._rvol

        def spy(*args):
            calls.append(args)
            return rvol(*args)

        with mock.patch.object(engine, "_rvol", spy):
            for ticker, rows, now in make_cases(random.Random(23), 40):
                engine.analyze_bars(ticker, parse(rows), now=now)
        for args in calls:
            self.assertSameRvol(*args)
        valued = [r for r in map(lambda a: rvol_ref(*a), calls) if r[0] is not None]
        self.assertGreater(len(valued), 60)                          # most cases have 2+ prior days of volume
        self.assertTrue(any(n == RVOL_MAX_PRIORS for _, n in valued))
        self.assertTrue(any(a[5] == engine.CRYPTO_CLOSE_M for a in calls))

    def test_the_replays_prefixes(self):
        rng = random.Random(29)
        valued = 0
        for k in range(16):
            series = make_series(rng, k)
            if not series:
                continue
            bars = parse(series[1])
            days = engine._sessions(bars)
            with self.subTest(series=k):
                for j in range(2, len(days)):
                    i0, last = engine._first_idx(bars, days[j]), engine._last_idx(bars, days[j])
                    for iN in range(i0, last + 1, rng.randint(7, 13)):
                        for close_m in CLOSES:
                            args = (bars[: iN + 1], days[: j + 1], i0, iN, rng.uniform(1e3, 1e6), close_m)
                            self.assertSameRvol(*args)
                            valued += rvol_ref(*args)[0] is not None
        self.assertGreater(valued, 200)

    def test_bars_the_tape_never_has(self):
        rng = random.Random(31)
        base = tape(rng)
        days = list(dict.fromkeys(b["d"] for b in base))
        i0 = next(i for i, b in enumerate(base) if b["d"] == days[-1])
        moved = [b for b in base if b["d"] == days[2] and b["mins"] >= 600]
        split = [b for b in base if not (b["d"] == days[2] and b["mins"] >= 600)] + moved   # a prior day in two runs
        zero_neg = [dict(b, v=0.0) if b["d"] == days[1] else dict(b, v=-b["v"]) if b["d"] == days[3] else b
                    for b in base]
        # one by one: 1e16 + 1.0 is 1e16 every time; sum() (3.12+) and fsum keep the ones
        lopsided = [dict(b, v=1e16 if b["mins"] == 0 else 1.0) if b["d"] == days[4] else b for b in base]
        shuffled = base[:]
        rng.shuffle(shuffled)
        cases = {
            "sorted": (base, days),
            "shuffled": (shuffled, days),
            "a prior day in two runs": (split, days),
            "the same day twice in days": (base, days[:3] + days[2:]),
            "a zero day and a negative day": (zero_neg, days),
            "volumes that add up differently": (lopsided, days),
            "two days": (base, days[-2:]),
            "one prior day with volume": ([b for b in base if b["d"] in days[-2:]], days[-3:]),
        }
        for name, (bars, ds) in cases.items():
            with self.subTest(case=name):
                ends = sorted({i0, i0 + 1, i0 + 100, i0 + 191, i0 + 192, len(bars) - 1, rng.randrange(len(bars))})
                for iN in (e for e in ends if e < len(bars)):
                    for close_m in CLOSES + (0, 600):
                        for acc_vol in (0.0, -5.0, 1.0, 123456.75):
                            self.assertSameRvol(bars, ds, i0, iN, acc_vol, close_m)


if __name__ == "__main__":
    unittest.main()
