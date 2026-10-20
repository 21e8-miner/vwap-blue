"""engine._md_target: an mdrev plan targets the first real level beyond entry, or has none (v1.6.0)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402

OPTS = {"open_mins": engine.RTH_OPEN_M, "close_mins": engine.RTH_CLOSE_M, "md_target": "nearest"}


def bar(d, mins, h, l, c):
    return {"d": d, "mins": mins, "o": c, "h": h, "l": l, "c": c, "v": 1000.0}


def tape():
    """Prior day: RTH high 105, low 95, last RTH close 101 (an after-hours print at 108 must not count)."""
    prior = [bar("2026-10-15", 570, 105.0, 99.0, 100.0), bar("2026-10-15", 900, 102.0, 95.0, 101.0),
             bar("2026-10-15", 1000, 108.0, 107.0, 108.0)]
    today = [bar("2026-10-16", 570, 99.0, 96.0, 97.0), bar("2026-10-16", 575, 98.5, 96.5, 97.5),
             bar("2026-10-16", 580, 98.0, 97.0, 98.0)]
    return prior + today, len(prior)


class TestMdTarget(unittest.TestCase):

    def test_long_takes_the_nearest_level_above_entry(self):
        bars, i0 = tape()
        st = {"_i0": i0, "blue": 97.4, "orange": 97.8}
        # today's high before the reclaim bar = 99.0; prior high 105; prior close 101; blue below entry
        t = engine._md_target(bars, i0 + 2, st, OPTS, "long", 98.0, 96.0, 96.5)
        self.assertEqual(t, 99.0)

    def test_short_takes_the_nearest_level_below_entry(self):
        bars, i0 = tape()
        st = {"_i0": i0, "blue": 97.4, "orange": 97.0}
        t = engine._md_target(bars, i0 + 2, st, OPTS, "short", 98.0, 100.0, 99.5)
        self.assertEqual(t, 97.4)                              # blue, above today's low 96.0 and prior low 95

    def test_no_level_beyond_entry_means_no_target(self):
        bars, i0 = tape()
        st = {"_i0": i0, "blue": 97.0, "orange": 97.5}
        self.assertIsNone(engine._md_target(bars, i0 + 2, st, OPTS, "long", 120.0, 110.0, 111.0))

    def test_after_hours_bars_are_not_levels(self):
        bars, i0 = tape()
        lv = engine._md_levels(bars, i0 + 2, {"_i0": i0, "blue": None}, OPTS, True)
        self.assertEqual((lv["prior"], lv["prior_close"]), (105.0, 101.0))

    def test_stretch_keeps_the_v1_5_rule(self):
        bars, i0 = tape()
        st = {"_i0": i0, "blue": 97.0, "orange": 97.5}
        t = engine._md_target(bars, i0 + 2, st, {**OPTS, "md_target": "stretch"}, "long", 98.0, 96.0, 96.5)
        self.assertAlmostEqual(t, 98.0 + 1.5 * 2.0)


if __name__ == "__main__":
    unittest.main()
