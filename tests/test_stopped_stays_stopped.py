"""
A stopped gap fade stays STOPPED ✕ for the rest of its session. Before v1.7.0 the trend-day check re-ran on
every later bar, so a stopped fade on a trend day flapped between "STOPPED ✕" (grade ✕) and "TREND BLOCK"
(grade –) bar by bar (PBR, 2026-10-26). With rearm_after_stop the same tape triggers again on a fresh break.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402
from test_replay_decision_time import day_bars  # noqa: E402


def tape():
    prior = day_bars("2026-10-23", [(f"{h:02d}:{m:02d}", 100.0 + ((h * 12 + m // 5) % 3) * 0.05)
                                    for h in range(9, 16) for m in range(0, 60, 5) if (h, m) >= (9, 30)])
    # gap down 6%, two bars reclaim blue (fade long triggers), then the day grinds lower all session
    closes = [("09:30", 94.0), ("09:35", 94.3), ("09:40", 94.9), ("09:45", 95.2), ("09:50", 95.4)]
    px = 95.4
    for h in range(9, 16):
        for m in range(0, 60, 5):
            if (h, m) <= (9, 50):
                continue
            px -= 0.08
            closes.append((f"{h:02d}:{m:02d}", round(px, 2)))
    today = day_bars("2026-10-26", closes)
    for b in today:
        b["v"] = 50000.0
    return prior + today


class TestStoppedStaysStopped(unittest.TestCase):

    def grades_after_stop(self, opts=None):
        bars = tape()
        i0 = next(i for i, b in enumerate(bars) if b["d"] == "2026-10-26")
        seen, stopped_at = [], None
        for k in range(i0 + 3, len(bars)):
            row = engine.analyze_bars("FAKE", bars[: k + 1], opts=opts)
            if stopped_at is None and row["state"].startswith("STOPPED"):
                stopped_at = k
            if stopped_at is not None:
                seen.append((row["grade"], row["state"]))
        self.assertIsNotNone(stopped_at, "the tape never stopped out")
        return seen

    def test_no_flapping_after_the_stop(self):
        seen = self.grades_after_stop()
        self.assertTrue(all(g == "✕" and s == "STOPPED ✕" for g, s in seen), set(seen))

    def test_rearm_lets_the_tape_watch_again(self):
        seen = self.grades_after_stop({"rearm_after_stop": True})
        self.assertNotEqual({s for _, s in seen}, {"STOPPED ✕"})


if __name__ == "__main__":
    unittest.main()
