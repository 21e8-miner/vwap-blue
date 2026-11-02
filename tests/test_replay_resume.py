"""
The replay grades every prefix of a session, and each analyze_bars call used to read the ticker's
whole history and run the focus day from its first bar again. With analyze_bars(..., memo=m) the next
prefix carries the day list, the day's per-bar state and the prior days' RVOL sums from m and reads
only its new bars. That is a pure speedup: every row must be the row analyze_bars gives without it.

  * every session of parsed parity tapes graded prefix by prefix with one memo per session, as the
    replay does (premarket gap sides that flip restart the day): the rows equal the rows from scratch,
    compared after the session is over, so no later call changed an earlier row
  * any call order and any bars under one memo give the row from scratch: shorter prefixes, the same
    prefix twice, another ticker's bars, a changed earlier bar, shuffled bars, other opts, a single
    crypto session split into D0/D1 on copies (its split moving)
  * _rvol's exact value from the memo (rows round it): prefixes into a new day, a prior day out of
    minute order, a prior day's bar after the focus day's, volumes that add up differently one by one
    than with sum() or fsum; the same bars as an equity and as crypto (another close)
  * with the memo each bar of a session is stepped once (without it, once per prefix that contains it)
  * replay_sessions.replay takes the same trades with the memo as with an engine that has none
"""

import functools
import json
import random
import sys
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine                                                # noqa: E402
import replay_sessions as rs                                 # noqa: E402
from test_engine_parity import make_series                   # noqa: E402


def frame(rows):
    """(utc time, o, h, l, c, volume) rows, None for a feed null, as test_engine_parity feeds analyze()."""
    nan = float("nan")
    cols = {k: [nan if r[j] is None else r[j] for r in rows] for j, k in enumerate(("Open", "High", "Low", "Close"), 1)}
    cols["Volume"] = [float(r[5]) for r in rows]
    return pd.DataFrame(cols, index=pd.DatetimeIndex([r[0] for r in rows]))


def dump(row):
    return json.dumps(row, sort_keys=True, default=repr)


def series(seed, n, crypto=None):
    """Parsed parity tapes: (ticker, bars, {day: first index}), optionally only crypto or only equity."""
    rng = random.Random(seed)
    for k in range(n):
        s = make_series(rng, k)
        if not s or (crypto is not None and s[0].endswith("-USD") != crypto):
            continue
        bars = engine._prep_bars(frame(s[1]))
        first = {}
        for i, b in enumerate(bars):
            first.setdefault(b["d"], i)
        if len(first) >= 2:
            yield s[0], bars, first


class TestReplayResume(unittest.TestCase):

    def assertSameRow(self, got, want, where):
        if got != want:   # name the fields that differ, not a diff of two long rows
            g, w = json.loads(got), json.loads(want)
            self.fail(f"{where}: {[k for k in sorted(set(g) | set(w)) if g.get(k) != w.get(k)]}")

    def test_every_prefix_resumed_is_the_row_from_scratch(self):
        rng = random.Random(3)
        restarts = resumed = compared = 0
        start = engine._day_start

        def count_start(*a):
            nonlocal restarts
            restarts += 1
            return start(*a)

        for ticker, bars, first in series(41, 7):
            days = list(first)
            for j in range(1, len(days)):
                i0 = first[days[j]]
                end = first[days[j + 1]] if j + 1 < len(days) else len(bars)
                memo, kept = {}, []
                for n in range(max(i0 + 1, 30), end + 1):
                    with mock.patch.object(engine, "_day_start", count_start):
                        before = restarts
                        row = engine.analyze_bars(ticker, bars[:n], memo=memo)
                        resumed += restarts == before
                    trig = ((row.get("_chart") or {}).get("markers") or {}).get("trig")
                    if trig == n - 1 - i0 or rng.random() < 0.1:    # every trigger bar, a tenth of the rest
                        kept.append((n, row, dump(engine.analyze_bars(ticker, bars[:n]))))
                for n, row, want in kept:                              # after the whole session
                    self.assertSameRow(dump(row), want, f"{ticker} {days[j]} prefix {n}")
                compared += len(kept)
        self.assertGreater(compared, 300)
        self.assertGreater(resumed, 10 * restarts)                     # most prefixes resume
        self.assertGreater(restarts, 30)                               # and sessions start over

    def test_any_call_order_and_bars_give_the_row_from_scratch(self):
        (t1, b1, f1), (t2, b2, f2) = list(series(43, 6))[:2]
        i0 = list(f1.values())[-1]
        changed = [dict(b) for b in b1]
        changed[i0 - 5]["c"] *= 1.01                                   # an earlier bar, inside the ATR window
        shuffled = b1[:]
        random.Random(1).shuffle(shuffled)
        one_crypto = next(b for t, b, f in series(47, 30, crypto=True))
        one_day = [b for b in one_crypto if b["d"] == one_crypto[-1]["d"]]
        calls = [(t1, b1[: i0 + 40], None), (t1, b1[: i0 + 60], None), (t1, b1[: i0 + 20], None),
                 (t1, b1[: i0 + 20], None), (t1, b1[: i0 + 61], None), (t2, b2, None), (t1, b1[: i0 + 62], None),
                 (t1, changed[: i0 + 63], None), (t1, b1[: i0 + 64], {"K": 3}), (t1, b1[: i0 + 65], None),
                 (t1, shuffled, None), (t1, b1[: i0 + 66], None),
                 ("BTC-USD", one_day[:200], None), ("BTC-USD", one_day[:201], None),
                 ("BTC-USD", one_day[:203], None)]                   # the D0/D1 split moves a bar
        memo, rows = {}, []
        for ticker, bars, opts in calls:
            rows.append((engine.analyze_bars(ticker, bars, opts=opts, memo=memo),
                         dump(engine.analyze_bars(ticker, bars, opts=opts))))
        for k, (row, want) in enumerate(rows):
            self.assertSameRow(dump(row), want, f"call {k}")

    def assertSameFromMemo(self, ticker, lists):
        """Each list graded with one memo: the row, and _rvol's exact value (rows round it), as without."""
        rvol, memo = engine._rvol, {}
        for k, bars in enumerate(lists):
            seen = []

            def spy(*args):
                seen.append((args, rvol(*args)))
                return seen[-1][1]

            with mock.patch.object(engine, "_rvol", spy):
                row = engine.analyze_bars(ticker, bars, memo=memo)
            self.assertSameRow(dump(row), dump(engine.analyze_bars(ticker, bars)), f"{ticker} call {k}")
            for args, got in seen:
                self.assertEqual(repr(got), repr(rvol(*args[:6])), f"{ticker} call {k}: _rvol")

    def test_rvol_and_the_day_list_carried_in_the_memo(self):
        tapes = [(t, b, f) for t, b, f in series(71, 16) if len(f) >= 5]
        (t1, b1, f1), (t2, b2, f2) = tapes[:2]
        d1 = list(f1)
        i0 = f1[d1[-1]]
        # a prior day whose minutes do not rise: its sum is added up from its list
        block = [k for k, b in enumerate(b1) if b["d"] == d1[-3]]
        unsorted = b1[:]
        shuffled = [b1[k] for k in block]
        random.Random(2).shuffle(shuffled)
        for k, b in zip(block, shuffled):
            unsorted[k] = b
        # a prior day's bar that arrives after the focus day's: the sums are listed again
        late = b1[:i0 + 50] + [b1[f1[d1[-2]] + 3]] + b1[i0 + 50:]
        # one by one 1e16 + 1.0 stays 1e16; sum() (3.12+) and fsum keep the ones
        lopsided = [dict(b, v=1e16 if k == f1[d1[-4]] else 1.0) if b["d"] == d1[-4] else b for k, b in enumerate(b1)]
        cases = {
            "prefixes through a day and into the next": [b1[:n] for n in range(f1[d1[-2]] + 20, i0 + 30, 7)],
            "a prior day out of minute order": [unsorted[:n] for n in range(i0 + 1, i0 + 60, 9)],
            "a prior day's bar after the focus day's": [late[:n] for n in range(i0 + 45, i0 + 60, 2)],
            "lopsided prior-day volumes": [lopsided[:n] for n in range(i0 + 1, i0 + 60, 9)],
        }
        for name, lists in cases.items():
            with self.subTest(case=name):
                self.assertSameFromMemo(t1, lists)
        with self.subTest(case="the same bars as an equity, then as crypto (another close)"):
            memo = {}
            for ticker in (t2.replace("-USD", ""), t2.replace("-USD", "") + "-USD", t2.replace("-USD", "")):
                self.assertSameRow(dump(engine.analyze_bars(ticker, b2, memo=memo)), dump(engine.analyze_bars(ticker, b2)),
                                   ticker)

    def test_an_error_mid_day_leaves_the_memo_as_it_was(self):
        """The replay grades on after an engine error; the next prefix must not resume from half a day."""
        ticker, bars, first = next(series(61, 30, crypto=True))      # crypto: the day's side is set at 00:00
        i0 = list(first.values())[-1]
        memo = {}
        engine.analyze_bars(ticker, bars[: i0 + 30], memo=memo)
        step, calls = engine._step_bar, []

        def fail_on_the_fifth(*a):
            calls.append(1)
            if len(calls) == 5:
                raise RuntimeError("feed glitch")
            return step(*a)

        restarted = mock.Mock(side_effect=AssertionError("started the day over instead of resuming"))
        with mock.patch.object(engine, "_step_bar", fail_on_the_fifth), mock.patch.object(engine, "_day_start", restarted), \
                self.assertRaises(RuntimeError):
            engine.analyze_bars(ticker, bars[: i0 + 40], memo=memo)   # resumes at i0 + 30, fails at i0 + 34
        self.assertSameRow(dump(engine.analyze_bars(ticker, bars[: i0 + 41], memo=memo)),
                           dump(engine.analyze_bars(ticker, bars[: i0 + 41])), "after the error")

    def test_resolve_day_to_an_earlier_bar_of_the_same_list(self):
        """A memo that reached bar u does not serve a _resolve_day that stops before u on the same bars."""
        ticker, bars, first = next(series(67, 10))
        p0, i0 = list(first.values())[-2:]
        opts = {"anchor_mins": engine.DEFAULT_ANCHOR_M, "open_mins": engine.RTH_OPEN_M, "close_mins": engine.RTH_CLOSE_M,
                "gap_min": engine.DEFAULT_GAP_MIN, "K": engine.DEFAULT_K, "atr_mult": engine.DEFAULT_ATR_MULT,
                "late_cut": engine.LATE_CUT_M}
        memo = {}
        engine._resolve_day(bars, i0, i0 + 80, p0, opts, memo)
        self.assertEqual(dump(engine._resolve_day(bars, i0, i0 + 60, p0, opts, memo)),
                         dump(engine._resolve_day(bars, i0, i0 + 60, p0, opts)))

    def test_resuming_steps_each_bar_once(self):
        ticker, bars, first = next(series(53, 30, crypto=True))      # crypto: the day's side is set at 00:00
        i0 = list(first.values())[-1]
        steps = 0
        step = engine._step_bar

        def count(*a):
            nonlocal steps
            steps += 1
            return step(*a)

        memo = {}
        with mock.patch.object(engine, "_step_bar", count):
            for n in range(i0 + 1, len(bars) + 1):
                engine.analyze_bars(ticker, bars[:n], memo=memo)
        self.assertEqual(steps, len(bars) - i0)

    def test_the_replay_trades_the_same_without_the_memo(self):
        rng = random.Random(59)
        frames = {t: frame(s) for t, s in (make_series(rng, k) or (None, None) for k in range(6)) if t is not None}
        passed = []
        analyze_bars = engine.analyze_bars

        @functools.wraps(analyze_bars)
        def spy(*a, **kw):
            passed.append(isinstance(kw.get("memo"), dict))
            return analyze_bars(*a, **kw)

        with mock.patch.object(engine, "analyze_bars", spy):
            with_memo = rs.replay(list(frames), frames, {}, {}, {}, {}, "B", rs.MODELS)
        self.assertTrue(passed and all(passed))                        # every grade got its session's memo
        no_memo_api = lambda eng=None: (engine._prep_bars, lambda t, b, **kw: engine.analyze_bars(t, b, **kw))
        with mock.patch.object(rs, "engine_api", no_memo_api):
            without = rs.replay(list(frames), frames, {}, {}, {}, {}, "B", rs.MODELS)
        self.assertEqual([asdict(x) for x in with_memo], [asdict(x) for x in without])
        self.assertGreater(len(with_memo), 50)


if __name__ == "__main__":
    unittest.main()
