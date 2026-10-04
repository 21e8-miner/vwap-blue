"""
The session replay trades what the desk showed at each bar, and closes equity trades by 16:00.

  * engine.analyze_bars on one parse of a frame is analyze() on the frame's prefix, and leaves the
    parsed bars as they were; replay_sessions.engine_api feeds parsed bars to an engine file that
    predates analyze_bars (paired replays of old versions)
  * replay_sessions.replay: a session's trade is the first bar whose own prefix grades a tradeable
    trigger on that bar. The old end-of-day search (causal_check.py) disagrees on synthetic tapes
    from test_engine_parity.make_series: premarket fades whose side the 09:30 open flipped, earlier
    multi-day reverses hidden behind an end-of-day gap trigger
  * session_bars / trades_at: equities fill and exit by the 16:00 close (classic_after_hours keeps
    the old hold to the last after-hours bar); crypto's session is its whole ET day
"""

import copy
import random
import sys
import types
import unittest
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import causal_check                                         # noqa: E402
import engine                                               # noqa: E402
import replay_sessions as rs                                # noqa: E402
from providers import looks_crypto                          # noqa: E402
from test_crypto_session import tape                        # noqa: E402
from test_engine_parity import make_series, trend_day_case  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=ET)
MODELS = {m.name: m for m in rs.MODELS}


def frame(rows):
    """(utc time, o, h, l, c, volume) rows, None for a feed null, as test_engine_parity feeds analyze()."""
    nan = float("nan")
    return pd.DataFrame({"Open": [nan if r[1] is None else r[1] for r in rows],
                         "High": [nan if r[2] is None else r[2] for r in rows],
                         "Low": [nan if r[3] is None else r[3] for r in rows],
                         "Close": [nan if r[4] is None else r[4] for r in rows],
                         "Volume": [float(r[5]) for r in rows]},
                        index=pd.DatetimeIndex([r[0] for r in rows]))


def classic(trades):
    return sorted((t.session, t.trigger_time, t.side, t.setup_mode) for t in trades if t.model == "classic")


def day_bars(day, rows):
    """Parsed bars for one ET day from (HH:MM, close) rows; each bar opens at the prior close."""
    out, prev = [], rows[0][1]
    for hhmm, c in rows:
        ts = pd.Timestamp(f"{day} {hhmm}", tz=ET)
        out.append({"ts": int(ts.timestamp() * 1000), "d": day, "mins": ts.hour * 60 + ts.minute, "o": prev,
                    "h": max(prev, c) + 0.05, "l": min(prev, c) - 0.05, "c": c, "v": 1000.0, "hlReal": True,
                    "time": ts.strftime("%m-%d %H:%M")})
        prev = c
    return out


class TestAnalyzeBars(unittest.TestCase):

    def test_analyze_bars_is_analyze_on_the_prefix(self):
        ticker, rows, _now = trend_day_case()
        df = frame(rows)
        parsed = engine._prep_bars(df)
        before = copy.deepcopy(parsed)
        for n in (230, 300, len(parsed)):
            self.assertEqual(engine.analyze_bars(ticker, parsed[:n], now=NOW), engine.analyze(ticker, df.iloc[:n], now=NOW))
        self.assertEqual(parsed, before)

    def test_single_crypto_session_does_not_touch_the_callers_bars(self):
        ticker, rows = tape({})
        df = frame(rows[:288])                                   # one ET day: analyze() splits it in two
        parsed = engine._prep_bars(df)
        before = copy.deepcopy(parsed)
        row = engine.analyze_bars(ticker, parsed, now=NOW)
        self.assertEqual(row["focus_day"], "D1")
        self.assertEqual(row, engine.analyze(ticker, df, now=NOW))
        self.assertEqual(parsed, before)

    def test_engine_api_hands_parsed_bars_to_an_engine_without_analyze_bars(self):
        ticker, rows, _now = trend_day_case()
        df = frame(rows)
        old = types.SimpleNamespace(_prep_bars=engine._prep_bars)
        old.analyze = lambda t, bars_df, **kw: engine.analyze_bars(t, old._prep_bars(bars_df), **kw)
        prep, grade = rs.engine_api(old)
        parsed = prep(df)
        self.assertEqual(grade(ticker, parsed[:300], now=NOW), engine.analyze(ticker, df.iloc[:300], now=NOW))
        self.assertIs(old._prep_bars, engine._prep_bars)        # restored after the call


class TestDecisionTimeSearch(unittest.TestCase):
    """Synthetic equity tapes on which the replay and the old end-of-day search take different trades."""

    @classmethod
    def setUpClass(cls):
        cls.cases = []
        for seed in range(40):
            series = make_series(random.Random(seed), 2)
            if not series or looks_crypto(series[0]):
                continue
            ticker, df = series[0], frame(series[1])
            replayed = rs.replay([ticker], {ticker: df}, {}, {}, {}, {}, "A", rs.MODELS)
            eod, _ = causal_check._eod_ticker((None, ticker, df, "A"))
            if classic(replayed) != classic(eod):
                cls.cases.append((ticker, df, replayed))
            if len(cls.cases) == 3:
                break

    def test_the_searches_disagree(self):
        self.assertEqual(len(self.cases), 3)

    def test_each_trade_is_the_first_bar_that_graded_a_tradeable_trigger(self):
        """Every bar of every session, after hours included, graded on its own prefix; one trade a session."""
        for ticker, df, replayed in self.cases:
            parsed = engine._prep_bars(df)
            days = list(dict.fromkeys(b["d"] for b in parsed))
            expect, skips = [], {}
            for d in days[1:]:
                i0 = next(i for i, b in enumerate(parsed) if b["d"] == d)
                day = [b for b in parsed[i0:] if b["d"] == d]
                for k in range(len(day)):
                    if i0 + k + 1 < 30:
                        continue
                    row = engine.analyze_bars(ticker, parsed[: i0 + k + 1])
                    if row["_chart"]["markers"]["trig"] != k or not rs._tradeable(row, "A"):
                        continue
                    eod = engine.analyze_bars(ticker, parsed[: i0 + len(rs.session_bars(ticker, day))])
                    new, fill = rs.trades_at(ticker, row, day, k, rs.MODELS, grade_eod=eod["grade"],
                                             dollar_vol=rs.passes_volume_filter(ticker, df, min_dvol=2_000_000)[1])
                    expect += new
                    break
            self.assertEqual([asdict(t) for t in replayed], [asdict(t) for t in expect], ticker)

    def test_the_trade_is_what_analyze_showed_at_its_bar(self):
        """The desk's own call, analyze() on the frame up to the trigger bar, shows the trade's plan."""
        for ticker, df, replayed in self.cases:
            ts = {b["time"]: b["ts"] for b in engine._prep_bars(df)}
            for t in (t for t in replayed if t.model == "classic"):
                prefix = df[[int(x.timestamp() * 1000) <= ts[t.trigger_time] for x in df.index]]
                row = engine.analyze(ticker, prefix)
                self.assertTrue(rs._tradeable(row, "A"))
                bars = row["_chart"]["bars"]
                self.assertEqual(bars[row["_chart"]["markers"]["trig"]]["time"], t.trigger_time)
                self.assertEqual((row["side"], row["stop"], row["target"], row["entry"]),
                                 (t.side, t.stop, t.target, t.entry_plan))


class TestSessionClose(unittest.TestCase):

    def setUp(self):
        # long from the 15:00 bar at 100; flat into the close, the stop (99) only trades after hours
        self.day = day_bars("2026-09-15", [("15:00", 100.0), ("15:05", 100.2)]
                            + [(f"15:{m:02d}", 100.4) for m in range(10, 60, 5)]
                            + [("16:00", 100.3), ("16:30", 98.0), ("19:55", 97.0)])
        self.row = {"side": "long", "entry": 100.0, "stop": 99.0, "target": 103.0, "signal": "TRIGGER", "grade": "A"}

    def test_equities_exit_at_the_close_and_after_hours_only_in_the_sensitivity(self):
        trades, fill = rs.trades_at("AAPL", self.row, self.day, 0, [MODELS["classic"], MODELS["classic_after_hours"]])
        self.assertEqual(fill, "filled")
        by = {t.model: t for t in trades}
        self.assertEqual((by["classic"].exit_reason, by["classic"].exit), ("eod", 100.4))     # the 15:55 close
        self.assertEqual((by["classic_after_hours"].exit_reason, by["classic_after_hours"].exit), ("stop", 99.0))
        self.assertEqual(rs.session_bars("AAPL", self.day)[-1]["time"], "09-15 15:55")

    def test_a_trigger_on_the_last_regular_bar_has_no_fill(self):
        k = next(i for i, b in enumerate(self.day) if b["time"].endswith("15:55"))
        self.assertEqual(rs.trades_at("AAPL", self.row, self.day, k, rs.MODELS), ([], "no_next_bar"))

    def test_crypto_holds_through_its_whole_day(self):
        self.assertEqual(rs.session_bars("BTC-USD", self.day), self.day)
        trades, _ = rs.trades_at("BTC-USD", self.row, self.day, 0, [MODELS["classic"]])
        self.assertEqual(trades[0].exit_reason, "stop")


if __name__ == "__main__":
    unittest.main()
