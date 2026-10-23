"""
replay_sessions.replay grades each session on a trailing window (LOOKBACK_SESSIONS prior sessions),
not on every bar before it. The trades must be the same as with the whole history: if the engine ever
reads further back than the window, this fails.
"""

import random
import sys
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import replay_sessions as rs  # noqa: E402


def tape(seed: int, sessions: int = 20) -> pd.DataFrame:
    """Equity 5m bars, premarket to after hours, a random gap and a mean-reverting day each session."""
    rng = random.Random(seed)
    days = pd.bdate_range("2026-08-03", periods=sessions)
    idx, rows = [], []
    px = rng.uniform(20, 300)
    for d in days:
        gap = rng.uniform(-0.04, 0.04)
        open_px = px * (1 + gap)
        p = open_px
        for t in pd.date_range(d + pd.Timedelta(hours=8), d + pd.Timedelta(hours=17, minutes=55), freq="5min"):
            pull = (px - p) * rng.uniform(0.0, 0.06)              # drift back toward yesterday's close
            step = p * rng.gauss(0, 0.002) + pull
            o, c = p, max(0.5, p + step)
            hi, lo = max(o, c) * (1 + abs(rng.gauss(0, 0.0008))), min(o, c) * (1 - abs(rng.gauss(0, 0.0008)))
            rth = 9 * 60 + 30 <= t.hour * 60 + t.minute < 16 * 60
            idx.append(t.tz_localize("America/New_York").tz_convert("UTC"))
            rows.append((o, hi, lo, c, rng.uniform(2e4, 9e4) * (3 if rth else 0.3)))
            p = c
        px = p
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=pd.DatetimeIndex(idx))


class TestReplayLookback(unittest.TestCase):

    def test_trailing_window_gives_the_same_trades_as_the_whole_history(self):
        names = [f"SYN{k}" for k in range(6)]
        bars = {t: tape(k) for k, t in enumerate(names)}
        models = [rs.ExitModel(name="classic")]

        def run():
            return [asdict(x) for x in rs.replay(names, bars, {}, {}, {}, {}, "B", models, skips={})]

        windowed = run()
        with mock.patch.object(rs, "LOOKBACK_SESSIONS", 10_000):
            whole = run()
        self.assertGreater(len(whole), 10, "the tape must produce trades for this test to mean anything")
        self.assertEqual(windowed, whole)


if __name__ == "__main__":
    unittest.main()
