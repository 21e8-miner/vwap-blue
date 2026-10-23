"""
The replay's mirror control (replay_sessions._mirror) and the small-sample verdict (honest.t_critical).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import honest                    # noqa: E402
import replay_sessions as rs    # noqa: E402


def bar(o, h, l, c, mins=600):
    return {"o": o, "h": h, "l": l, "c": c, "v": 1000, "mins": mins, "d": "2026-10-21", "time": "10-21 10:00"}


class TestMirror(unittest.TestCase):

    def test_same_distances_other_side_from_the_same_raw_open(self):
        raw = 100.0
        entry = raw * (1 + 2 / 1e4)                       # a long's next-open fill, 2 bps against it
        side, m_entry, m_stop, m_tgt = rs._mirror("long", entry, entry - 1.0, entry + 1.5, "next_open", 2.0)
        self.assertEqual(side, "short")
        self.assertAlmostEqual(m_entry, raw * (1 - 2 / 1e4))   # the short pays its own slippage
        self.assertAlmostEqual(m_stop - m_entry, 1.0)
        self.assertAlmostEqual(m_entry - m_tgt, 1.5)

    def test_trigger_close_entries_are_not_repriced(self):
        _, m_entry, m_stop, m_tgt = rs._mirror("short", 50.0, 51.0, 48.0, "trigger_close", 2.0)
        self.assertEqual((m_entry, m_stop, m_tgt), (50.0, 49.0, 52.0))

    def test_a_long_that_hits_its_target_is_a_mirror_that_hits_its_stop(self):
        day = [bar(100, 100, 100, 100), bar(100, 100.2, 99.9, 100.1), bar(100.1, 102, 100, 101.8)]
        row = {"side": "long", "entry": 100.0, "stop": 99.0, "target": 101.5, "signal": "TRIGGER", "grade": "A"}
        trades, fill = rs.trades_at("XYZ", row, day, 0, [rs.ExitModel(name="classic")], "next_open", 0.0)
        self.assertEqual(fill, "filled")
        t = trades[0]
        self.assertEqual(t.exit_reason, "target")
        self.assertGreater(t.r_net, 0)
        self.assertLess(t.mirror_r_net, -0.9)              # the short's stop is the long's target distance away
        self.assertAlmostEqual(t.r_net - t.mirror_r_net, t.r_multiple + 1.0, places=2)

    def test_summary_reports_trade_minus_mirror(self):
        mk = lambda s, r, m: rs.Trade(ticker="X", session=s, side="long", signal="T", grade="A", grade_eod="", edge=0,
                                      regime="chop", setup_mode="gap", ker=None, rvol=None, rvol_n=0, gap_pct=None,
                                      entry=1, stop=0.9, target=1.2, rr_plan=2, model="classic", exit=1, exit_reason="eod",
                                      r_multiple=r, mfe_r=0, mae_r=0, bars_held=1, pnl_pct_net=0, r_net=r,
                                      mirror_r_net=m)
        s = rs._pack([mk("d1", 0.5, -0.6), mk("d2", -1.0, 0.9), mk("d3", 0.1, -0.2)])
        self.assertAlmostEqual(s["mirror"]["direction"]["mean"], round(((1.1) + (-1.9) + (0.3)) / 3, 4))
        old = rs._pack([mk("d1", 0.5, None)])
        self.assertNotIn("mirror", old)                    # files saved before the control have no mirror block


class TestSmallSampleVerdict(unittest.TestCase):

    def test_critical_t_by_sessions(self):
        self.assertAlmostEqual(honest.t_critical(10), 2.262)
        self.assertAlmostEqual(honest.t_critical(21), 2.086)
        self.assertAlmostEqual(honest.t_critical(59), 2.0017, places=3)
        self.assertEqual(honest.t_critical(500), 2.0)       # never looser than the old |t| >= 2

    def test_a_t_of_minus_2_1_needs_more_than_ten_sessions(self):
        st = {"t": -2.1, "clusters": 10, "mean": -0.2, "se": 0.095, "n": 100}
        self.assertEqual(honest.verdict(st), "inconclusive")
        self.assertEqual(honest.verdict({**st, "clusters": 40}), "negative")

    def test_the_interval_uses_t(self):
        st = honest.clustered([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0], list(range(10)))
        half = st["ci90"][1] - st["mean"]
        self.assertAlmostEqual(half / st["se"], 1.833, places=2)


if __name__ == "__main__":
    unittest.main()
