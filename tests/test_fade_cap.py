"""v1.7.0: grade A means a multi-day reclaim; a gap fade, alone or with a reclaim, is capped at B."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402

OPTS = {"Rmin": 1.0, "rvol_min": 1.2, "rvol_min_n": 2, "md_min_ext": 8, "rvol_missing": "pass"}


def scene(mode, regime="chop", late=False, trig=5, md_trig=None):
    """A row that would grade A under every pre-v1.7.0 rule: runway, R:R 2, RVOL 2.0 on 5 priors."""
    st = {"setup_mode": mode, "trig": trig, "md_trig": md_trig, "stopped": None, "noRunway": False, "R": 2.0,
          "late": late, "md_ext_run": 0}
    return {"st": st, "error": None, "dev_ok": True, "rvol": 2.0, "rvol_n": 5, "regime": regime}


class TestFadeCap(unittest.TestCase):

    def test_a_gap_fade_that_was_a_is_now_b(self):
        self.assertEqual(engine._grade(scene("gap"), OPTS), "B")
        self.assertEqual(engine._grade(scene("gap", late=True), OPTS), "LB")

    def test_a_fade_with_a_reclaim_is_capped_too(self):
        self.assertEqual(engine._grade(scene("both", md_trig=4), OPTS), "B")
        self.assertEqual(engine._grade(scene("both", regime="chop", md_trig=4), OPTS), "B")

    def test_a_reclaim_still_grades_a(self):
        self.assertEqual(engine._grade(scene("mdrev", trig=None, md_trig=4), OPTS), "A")
        self.assertEqual(engine._grade(scene("mdrev", trig=None, md_trig=4, late=True), OPTS), "LA")

    def test_the_cap_is_an_option(self):
        self.assertEqual(engine._grade(scene("gap"), {**OPTS, "fade_max_grade": "A"}), "A")
        self.assertEqual(engine._grade(scene("gap"), {**OPTS, "fade_max_grade": "B"}), "B")

    def test_defaults_carry_the_cap(self):
        self.assertEqual(engine.DEFAULT_FADE_MAX_GRADE, "B")
        self.assertEqual(engine.ENGINE_VERSION, "1.7.0")


if __name__ == "__main__":
    unittest.main()
