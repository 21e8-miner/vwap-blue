"""
_resolve_day used to call engine._atr(bars, i) at every bar of the focus day, re-walking a 41-bar window
each time; engine._atr_trail now computes each bar's true range once for the whole run. That is a pure
speedup: the trail must be exactly [_atr(bars, i) for i in i0..iN] as _atr computed it (a frozen copy of
engine.py's _atr at 82c8da5), compared on each value's type and repr.

  * parsed synthetic sessions from test_engine_parity.make_series (single prints with no range, missing
    bars, feed nulls, DST weeks, crypto days): every session's run of bars, and random (i0, iN) cuts
  * windows the tape rarely builds: fewer ranged bars than the period, a flat stretch longer than the
    window (the next ranged bar has no prior close inside it), i0 under 40, other periods, inf prices,
    and a seed where sum() (compensated on Python 3.12+) and a plain loop or math.fsum disagree
  * analyze_bars on parity cases (full histories, live cuts, decision-time prefixes at triggers):
    _step_bar gets _atr(bars, i) at every bar, and the row is the same as with per-bar _atr (rows round
    stops, so only the first check sees a one-bar shift); the cases include ATR stop buffers
"""

import json
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

INF = float("inf")


# ── the reference: _atr as it was in engine.py at 82c8da5 ───────────────────────────────────────────

def atr_ref(bars, end, period=14):
    trs = []
    prev_c = None
    start = max(0, end - 40)
    for i in range(start, end + 1):
        b = bars[i]
        if not b["hlReal"]:
            continue
        if prev_c is None:
            trs.append(b["h"] - b["l"])
        else:
            trs.append(max(b["h"] - b["l"], abs(b["h"] - prev_c), abs(b["l"] - prev_c)))
        prev_c = b["c"]
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def per_bar(bars, i0, iN, period=14):
    """What _resolve_day used to compute: _atr at every bar."""
    return [atr_ref(bars, i, period) for i in range(i0, iN + 1)]


def strict(values):
    return [(type(x), repr(x)) for x in values]


def dump(x):
    """A row field as exact text (json writes floats with repr)."""
    return json.dumps(x, sort_keys=True, default=repr)


def parse(rows):
    """(utc time, o, h, l, c, volume) rows, None for a feed null, through engine._prep_bars."""
    nan = float("nan")
    cols = {k: [nan if r[j] is None else r[j] for r in rows] for j, k in enumerate(("Open", "High", "Low", "Close"), 1)}
    cols["Volume"] = [float(r[5]) for r in rows]
    return engine._prep_bars(pd.DataFrame(cols, index=pd.DatetimeIndex([r[0] for r in rows])))


def bar(h, l, c):
    """The fields _atr reads."""
    return {"h": h, "l": l, "c": c, "hlReal": h > l}


class TestAtrTrail(unittest.TestCase):

    def assertSameValues(self, got, want, where):
        """Strict, naming the first value that differs (a diff of long lists takes minutes)."""
        got, want = strict(got), strict(want)
        if got != want:
            k = next((k for k, (x, y) in enumerate(zip(got, want)) if x != y), min(len(got), len(want)))
            self.fail(f"{where}: item {k} of {len(want)} (got {len(got)}): {got[k:k + 1]} != {want[k:k + 1]}")

    def assertSameTrail(self, bars, i0, iN, period=14):
        self.assertSameValues(engine._atr_trail(bars, i0, iN, period), per_bar(bars, i0, iN, period),
                              f"i0={i0} iN={iN} period={period}")

    def test_every_session_of_the_parity_tape(self):
        rng = random.Random(41)
        valued = 0
        for k in range(60):
            series = make_series(rng, k)
            if not series:
                continue
            bars = parse(series[1])
            if not bars:
                continue
            with self.subTest(series=k):
                for day in engine._sessions(bars):
                    i0, iN = engine._first_idx(bars, day), engine._last_idx(bars, day)
                    self.assertSameTrail(bars, i0, iN)
                    valued += sum(x is not None for x in per_bar(bars, iN, iN))
                for _ in range(4):
                    i0 = rng.randrange(len(bars))
                    self.assertSameTrail(bars, i0, rng.randrange(i0, len(bars)))
        self.assertGreater(valued, 100)

    def test_windows_the_tape_rarely_builds(self):
        rng = random.Random(7)
        ranged = [bar(100 + x + 0.6, 100 + x - 0.4, 100 + x) for x in (rng.uniform(-2, 2) for _ in range(60))]
        flat = [bar(101.0, 101.0, 101.0)] * 50
        cases = {
            "all flat": flat[:45],
            "13 ranged bars, then more": ranged[:13] + flat[:5] + ranged[13:30],
            "flat stretch longer than the window": ranged[:30] + flat + ranged[30:],
            "every other bar flat": [x for pair in zip(ranged, flat) for x in pair],
            "inf high, inf close, inf high again": ranged[:20] + [bar(INF, 99.0, 100.0), bar(101.0, 99.0, INF),
                                                                   bar(INF, 99.5, 100.0)] + ranged[20:40],
            # sum() of [1e6] + [0.1] * 13 is 1000001.3 on 3.12+ (compensated), 1000001.2999999997 on 3.11:
            # the seed must be sum(), not a loop (3.12+) or math.fsum (3.11)
            "a huge range, then small ones": [bar(1e6 + 100.0, 100.0, 100.05)] + [bar(100.1, 100.0, 100.05)] * 45,
        }
        for name, bars in cases.items():
            for period in (1, 2, 14, 27, 41, 42):
                with self.subTest(case=name, period=period):
                    for i0 in sorted({0, 1, 13, 39, 40, 41, len(bars) // 2, len(bars) - 1}):
                        if i0 < len(bars):
                            self.assertSameTrail(bars, i0, len(bars) - 1, period)
                    i0 = rng.randrange(len(bars))
                    self.assertSameTrail(bars, i0, rng.randrange(i0, len(bars)), period)

    def test_each_bar_gets_its_atr_and_the_rows_do_not_move(self):
        step = engine._step_bar
        stepped = atr_stops = 0
        for ticker, rows, now in make_cases(random.Random(17), 40):
            bars = parse(rows)
            seen = []

            def spy(b, i, *rest):
                seen.append((b, i, rest[-1]))                        # rest[-1]: the atr _resolve_day passed
                return step(b, i, *rest)

            with mock.patch.object(engine, "_step_bar", spy):
                new = engine.analyze_bars(ticker, bars, now=now)
            with mock.patch.object(engine, "_atr_trail", per_bar):
                old = engine.analyze_bars(ticker, bars, now=now)
            with self.subTest(ticker=ticker, bars=len(bars)):
                # _step_bar gets _atr(bars, i) at every bar, as it used to compute it itself
                self.assertSameValues([a for _, _, a in seen], [atr_ref(b, i) for b, i, _ in seen], "_step_bar's atr")
                # and every row field is the same (the fields that differ, not a diff of two rows)
                self.assertEqual([k for k in sorted(set(new) | set(old)) if dump(new.get(k)) != dump(old.get(k))], [])
            stepped += len(seen)
            trig = ((new.get("_chart") or {}).get("markers") or {}).get("trig")
            if trig is not None and atr_ref(new["_chart"]["bars"], trig) is not None:
                atr_stops += 1                                       # the trigger's stop buffer is the ATR
        self.assertGreater(stepped, 10_000)
        self.assertGreater(atr_stops, 50)


if __name__ == "__main__":
    unittest.main()
