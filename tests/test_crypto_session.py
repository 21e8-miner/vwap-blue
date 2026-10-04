"""
v1.5.0: crypto trades around the clock, so its session is the whole ET calendar day.

Before, crypto ran the equity clock: blue, orange, RVOL and signals stopped at 16:00 ET, the prior
close was the prior day's 15:55 bar and the "gap" ran from there to the 09:30 open. These fixed tapes
check the new session on engine.py; test_engine_parity.py runs the same tapes through engine.js.
"""

import sys
import unittest
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import analyze      # noqa: E402

ET = ZoneInfo("America/New_York")
DAY1 = date(2026, 9, 14)


def tape(steps, ticker="FIX-USD", day1=DAY1):
    """
    Two ET days of 5m bars from 00:00. `steps` maps (day index, "HH:MM") to the close from that bar on
    (a level shift); otherwise each bar repeats the prior close with a small fixed wiggle. Opens are the
    prior close unless the step is at the bar itself (then the bar opens at the new level: a jump).
    Rows are (utc time, o, h, l, c, volume), the format test_engine_parity.py feeds both engines.
    """
    rows, level, prev = [], 100.0, 100.0
    t = datetime.combine(day1, dtime(0, 0), tzinfo=ET)
    end = t + timedelta(days=2)
    i = 0
    while t < end:
        key = ((t.date() - day1).days, t.strftime("%H:%M"))
        jumped = key in steps
        if jumped:
            level = steps[key]
        c = round(level * (1 + 0.0002 * ((i % 5) - 2)), 4)
        o = c if jumped else prev
        rows.append((t.astimezone(timezone.utc), o, round(max(o, c) * 1.0004, 4), round(min(o, c) * 0.9996, 4), c,
                     5_000.0 + 10.0 * (i % 9)))
        prev, t, i = c, t + timedelta(minutes=5), i + 1
    return ticker, rows


def reclaim_tape(at, ticker="FIX-USD"):
    """Day 2 dips 0.6% under orange for the 45 minutes before `at` (HH:MM ET), then closes back above it."""
    hh, mm = map(int, at.split(":"))
    start = (datetime.combine(DAY1, dtime(hh, mm)) - timedelta(minutes=45)).strftime("%H:%M")
    return tape({(1, start): 99.4, (1, at): 100.3}, ticker)


def fixed_cases():
    """(ticker, rows, now) for test_engine_parity.py: each tape on a crypto and an equity ticker."""
    tapes = [
        tape({(1, "16:00"): 102.0}),                                  # the evening moves blue (crypto only)
        tape({(0, "23:55"): 101.0}),                                  # prior close = the prior day's last bar
        tape({(1, "00:00"): 101.0}),                                  # a 1% jump across midnight is no gap
        tape({(1, "09:30"): 101.0}),                                  # ... nor is one at the equity open
        reclaim_tape("18:00"), reclaim_tape("22:25"), reclaim_tape("22:30"), reclaim_tape("23:50"),
    ]
    out = []
    for ticker, rows in tapes:
        for t in (ticker, ticker.replace("-USD", "")):
            for cut in (rows, rows[:-24]):
                out.append((t, cut, cut[-1][0] + timedelta(minutes=2)))
    return out


def run(ticker, rows):
    df = pd.DataFrame({"Open": [r[1] for r in rows], "High": [r[2] for r in rows], "Low": [r[3] for r in rows],
                       "Close": [r[4] for r in rows], "Volume": [r[5] for r in rows]},
                      index=pd.DatetimeIndex([r[0] for r in rows]))
    row = analyze(ticker, df, now=rows[-1][0] + timedelta(minutes=2))
    ch = row.get("_chart") or {}
    trig = (ch.get("markers") or {}).get("trig")
    row["trigger_time"] = ch["bars"][trig]["time"] if trig is not None else None
    return row


class TestCryptoSession(unittest.TestCase):

    def test_the_evening_counts_for_crypto_only(self):
        _, rows = tape({(1, "16:00"): 102.0})
        crypto, equity = run("FIX-USD", rows), run("FIX", rows)
        self.assertGreater(crypto["blue"], 100.5)                # 16:00-23:55 at 102 is a third of the day
        self.assertLess(abs(equity["blue"] - 100.0), 0.05)       # equities still stop at the 16:00 close

    def test_prior_close_is_the_prior_days_last_bar(self):
        _, rows = tape({(0, "23:55"): 101.0})
        self.assertAlmostEqual(run("FIX-USD", rows)["prior_close"], 101.0, delta=0.05)
        self.assertAlmostEqual(run("FIX", rows)["prior_close"], 100.0, delta=0.05)  # equities: the 15:55 bar

    def test_no_gap_fade_for_crypto(self):
        for at in ("00:00", "09:30"):
            _, rows = tape({(1, at): 101.0})
            row = run("FIX-USD", rows)
            self.assertNotIn(row["setup_mode"], ("gap", "both"), at)
            self.assertFalse(row["state"].startswith(("GAP", "FIRST BREAK", "FAKEOUT", "CONFIRMED", "TREND BLOCK")), at)
        self.assertGreater(run("FIX-USD", tape({(1, "00:00"): 101.0})[1])["gap_pct"], 0.9)   # still reported
        self.assertEqual(run("FIX", tape({(1, "09:30"): 101.0})[1])["setup_mode"], "gap")      # equities fade it

    def test_multi_day_reverse_triggers_in_the_evening(self):
        late = {"18:00": False, "22:25": False, "22:30": True, "23:50": True}
        for at, is_late in late.items():
            row = run("FIX-USD", reclaim_tape(at)[1])
            self.assertEqual(row["setup_mode"], "mdrev", at)
            self.assertEqual(row["md_side"], "long", at)
            self.assertTrue(row["trigger_time"].endswith(at), (at, row["trigger_time"]))
            self.assertEqual(row["late"], is_late, at)          # late cut 22:30, as 14:30 is to 16:00
            self.assertIsNone(run("FIX", reclaim_tape(at)[1])["trigger_time"], at)   # equities: after hours


if __name__ == "__main__":
    unittest.main()
