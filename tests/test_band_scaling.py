"""band_scaling_check: the deviation-from-blue growth reading and its shuffled references, on known tapes."""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import band_scaling_check as bsc  # noqa: E402
import engine  # noqa: E402


def frame(closes, volumes, start="2026-01-05 00:00", step_min=5, spread=0.001):
    idx = pd.date_range(start, periods=len(closes), freq=f"{step_min}min", tz="America/New_York").tz_convert("UTC")
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * (1 + spread), "Low": np.minimum(o, c) * (1 - spread),
                         "Close": c, "Volume": volumes}, index=idx)


def random_walk(days, bars_per_day, rng, vol=0.002):
    r = rng.normal(0.0, vol, days * bars_per_day)
    return 100.0 * np.exp(np.cumsum(r))


def reverting(days, bars_per_day, rng, beta=0.85, vol=0.002):
    x, out = 0.0, []
    for _ in range(days * bars_per_day):
        x = beta * x + rng.normal(0.0, vol)
        out.append(100.0 * np.exp(x))
    return np.array(out)


class TestBandScaling(unittest.TestCase):

    def setUp(self):
        self.rng = np.random.default_rng(3)

    def _rows(self, closes, is_crypto, equal_weight=False):
        vols = self.rng.integers(500, 5000, len(closes)).astype(float)
        parsed = engine._prep_bars(frame(closes, vols))
        return bsc.session_rows(parsed, is_crypto, self.rng, 0, equal_weight=equal_weight)

    def test_a_random_walk_day_grows_like_sqrt_t_and_so_do_its_shuffles(self):
        real, within, cross = self._rows(random_walk(60, 288, self.rng), is_crypto=True)
        for rows in (real, within, cross):
            pb = bsc.per_bin(rows, 1440)
            tm = np.arange(len(pb["n"])) * bsc.BAR_MIN
            h = bsc.fit_H(tm, pb["rms_dev"], bsc.FIT_FROM_MIN, 1440)
            self.assertAlmostEqual(h, 0.5, delta=0.12)
            vr = bsc.vr_ratio(rows["vr"])
            for k in bsc.VR_K:
                self.assertAlmostEqual(vr[k], 1.0, delta=0.15)

    def test_a_reverting_tape_reads_below_its_shuffles(self):
        real, within, cross = self._rows(reverting(60, 288, self.rng), is_crypto=True)
        vr_real, vr_cross = bsc.vr_ratio(real["vr"]), bsc.vr_ratio(cross["vr"])
        self.assertLess(vr_real[12], 0.5)
        self.assertAlmostEqual(vr_cross[12], 1.0, delta=0.15)       # the shuffle removes the reversion
        pb_real, pb_cross = bsc.per_bin(real, 1440), bsc.per_bin(cross, 1440)
        late = slice(120, 288)
        self.assertLess(np.nanmean(pb_real["rms_dev"][late]), 0.5 * np.nanmean(pb_cross["rms_dev"][late]))

    def test_blue_and_sigma_are_the_engines(self):
        closes = random_walk(6, 288, self.rng)
        vols = self.rng.integers(500, 5000, len(closes)).astype(float)
        parsed = engine._prep_bars(frame(closes, vols))
        # equities: anchor 04:00, session 09:30-16:00, blue and sigma as analyze_bars reports them at the last bar
        last_rth = max(i for i, b in enumerate(parsed) if engine.RTH_OPEN_M <= b["mins"] < engine.RTH_CLOSE_M)
        row = engine.analyze_bars("FAKE", parsed[:last_rth + 1])
        real, _, _ = bsc.session_rows(parsed[:last_rth + 1], False, self.rng, 0)
        c = parsed[last_rth]["c"]
        blue = c / (1 + real["dev2"][-1] ** 0.5 * np.sign(c - row["blue"])) if real["dev2"][-1] > 0 else c
        self.assertAlmostEqual(blue, row["blue"], delta=0.0051)          # the row rounds blue to cents
        self.assertAlmostEqual(real["sig2"][-1] ** 0.5 * blue, row["sigma"], delta=1e-5)
        self.assertEqual(int(real["t"][-1]), parsed[last_rth]["mins"] - engine.RTH_OPEN_M)

    def test_equal_weight_changes_blue_but_not_the_variance_ratio(self):
        closes = random_walk(10, 288, self.rng)
        vols = self.rng.integers(500, 5000, len(closes)).astype(float)
        parsed = engine._prep_bars(frame(closes, vols))
        rng_a, rng_b = np.random.default_rng(11), np.random.default_rng(11)
        vw, _, _ = bsc.session_rows(parsed, True, rng_a, 0)
        ew, _, _ = bsc.session_rows(parsed, True, rng_b, 0, equal_weight=True)
        self.assertFalse(np.allclose(vw["dev2"], ew["dev2"]))
        self.assertEqual(bsc.vr_ratio(vw["vr"]), bsc.vr_ratio(ew["vr"]))


if __name__ == "__main__":
    unittest.main()
