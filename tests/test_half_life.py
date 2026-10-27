"""engine._dev_half_life: the OU half-life of close - blue, carried on every row as half_life_min."""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402
from test_stopped_stays_stopped import tape  # noqa: E402


def series(beta, n=60, start=1.0):
    devs, d = [], start
    for _ in range(n):
        devs.append(d)
        d *= beta
    return devs


class TestHalfLife(unittest.TestCase):

    def test_an_exact_ar1_recovers_its_half_life(self):
        for beta, want in ((0.5, 5.0), (0.8, round(math.log(2) / -math.log(0.8) * 5, 1))):
            devs = series(beta)
            bars = [{"c": 100.0 + v} for v in devs]
            self.assertEqual(engine._dev_half_life(bars, 0, len(bars) - 1, [100.0] * len(bars)), want)

    def test_no_reversion_and_too_few_bars(self):
        bars = [{"c": 100.0 + v} for v in series(1.05)]            # growing: beta >= 0.999
        self.assertEqual(engine._dev_half_life(bars, 0, 59, [100.0] * 60), 999.0)
        bars = [{"c": 100.0 + v} for v in series(0.5, n=15)]
        self.assertIsNone(engine._dev_half_life(bars, 0, 14, [100.0] * 15))

    def test_only_the_last_window_counts_and_blue_gaps_are_skipped(self):
        devs = series(0.5, n=30) + series(0.9, n=70)
        bars = [{"c": 100.0 + v} for v in devs]
        full = engine._dev_half_life(bars, 0, 99, [100.0] * 100)
        self.assertEqual(full, round(math.log(2) / -math.log(0.9) * 5, 1))     # the 0.5 bars fell outside 60
        trail = [None] * 20 + [100.0] * 80
        self.assertIsNotNone(engine._dev_half_life(bars, 0, 99, trail))

    def test_the_row_carries_it(self):
        bars = tape()
        row = engine.analyze_bars("FAKE", bars)
        self.assertIn("half_life_min", row)
        self.assertTrue(row["half_life_min"] is None or row["half_life_min"] > 0)


if __name__ == "__main__":
    unittest.main()
