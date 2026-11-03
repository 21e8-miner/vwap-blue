"""
orb_rvol_check.py: the opening-range continuation rule and its fade mirror on hand-built sessions.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orb_rvol_check as m  # noqa: E402


def bar(mins, o, h, l, c, v=1000):
    hh, mm = divmod(mins, 60)
    return {"d": "2026-10-01", "mins": mins, "o": o, "h": h, "l": l, "c": c, "v": v, "time": f"10-01 {hh:02d}:{mm:02d}"}


def flat_day(px=100.0, start=575, end=955, step=5):
    return [bar(m, px, px + 0.01, px - 0.01, px) for m in range(start, end + 1, step)]


class TestRule(unittest.TestCase):

    def test_long_breakout_fills_on_the_bar_through_the_opening_high_and_runs_to_the_flat_bar(self):
        day = [bar(570, 100, 101, 99.5, 100.8)] + [bar(575, 100.7, 100.9, 100.5, 100.8)] + [bar(580, 100.9, 101.6, 100.8, 101.5)] + \
              [bar(m, 101.5, 101.7, 101.3, 101.6) for m in range(585, 956, 5)]
        t = m.session_trade("AAA", "2026-10-01", day, atr=2.0, rvol=1.5, prev_close=99.0, slip_bps=0)
        self.assertEqual(t["side"], "long")
        self.assertEqual(t["fill_time"], "10-01 09:40", "the 09:35 bar never reached the 101 level")
        self.assertAlmostEqual(t["entry"], 101.0)                  # level itself: the bar opened below it
        self.assertAlmostEqual(t["risk"], 0.2)                      # 0.10 × ATR
        self.assertEqual(t["reason"], "flat"); self.assertAlmostEqual(t["exit"], 101.6)
        self.assertGreater(t["r_net"], 0); self.assertLess(t["mirror_r_net"], 0, "the fade of the same level loses")
        self.assertAlmostEqual(t["gap_atr"], 0.5)

    def test_gap_through_the_level_fills_at_the_open_and_stops_fill_at_a_worse_open(self):
        day = [bar(570, 100, 100.5, 99.8, 100.4), bar(575, 100.9, 101.2, 100.7, 101.0), bar(580, 100.2, 100.3, 99.0, 99.1)] + flat_day(99.1, 585)
        t = m.session_trade("AAA", "2026-10-01", day, atr=2.0, rvol=None, prev_close=None, slip_bps=0)
        self.assertAlmostEqual(t["entry"], 100.9, msg="opened above the 100.5 level: fill at the open")
        self.assertEqual(t["reason"], "stop"); self.assertAlmostEqual(t["exit"], 100.2, msg="gapped below the stop: out at the open")
        self.assertIsNone(t["rvol"]); self.assertIsNone(t["gap_atr"])

    def test_short_breakout_mirrors_the_long_rule(self):
        day = [bar(570, 100, 100.2, 99.0, 99.2), bar(575, 99.1, 99.3, 98.5, 98.6)] + flat_day(98.6, 580)
        t = m.session_trade("AAA", "2026-10-01", day, atr=1.0, rvol=4.0, prev_close=100.5, slip_bps=0)
        self.assertEqual(t["side"], "short"); self.assertAlmostEqual(t["entry"], 99.0); self.assertAlmostEqual(t["stop"], 99.1)
        self.assertEqual(t["reason"], "flat"); self.assertGreater(t["r_net"], 0)

    def test_no_trade_on_a_doji_or_when_the_level_is_not_hit_by_1030(self):
        self.assertIsNone(m.session_trade("AAA", "2026-10-01", [bar(570, 100, 101, 99, 100)] + flat_day(), 1.0, 1.0, 99.0, 0))
        quiet = [bar(570, 100, 101, 99, 100.5)] + flat_day(100.2, 575)
        self.assertIsNone(m.session_trade("AAA", "2026-10-01", quiet, 1.0, 1.0, 99.0, 0), "100.2 never reaches 101")
        late = [bar(570, 100, 101, 99, 100.5)] + flat_day(100.2, 575, 630) + [bar(635, 100.2, 101.5, 100.2, 101.4)] + flat_day(101.4, 640)
        self.assertIsNone(m.session_trade("AAA", "2026-10-01", late, 1.0, 1.0, 99.0, 0), "a fill after 10:30 does not count")

    def test_slippage_is_charged_against_the_trade_both_ways(self):
        day = [bar(570, 100, 101, 99.5, 100.8), bar(575, 100.9, 101.6, 100.8, 101.5)] + flat_day(101.5, 580)
        a = m.session_trade("AAA", "2026-10-01", day, 2.0, 1.0, 99.0, slip_bps=0)
        b = m.session_trade("AAA", "2026-10-01", day, 2.0, 1.0, 99.0, slip_bps=10)
        self.assertGreater(b["entry"], a["entry"]); self.assertLess(b["r_net"], a["r_net"])


    def test_the_papers_universe_filters_and_the_range_stop(self):
        day = [bar(570, 100, 101, 99.5, 100.8), bar(575, 100.9, 101.6, 100.8, 101.5)] + flat_day(101.5, 580)
        self.assertIsNone(m.session_trade("SGOV", "2026-10-01", day, atr=0.02, rvol=1.0, prev_close=99.9, slip_bps=0), "ATR under $0.50")
        penny = [bar(570, 3, 3.1, 2.9, 3.05), bar(575, 3.1, 3.2, 3.0, 3.15)] + flat_day(3.15, 580)
        self.assertIsNone(m.session_trade("PNY", "2026-10-01", penny, atr=0.6, rvol=1.0, prev_close=3.0, slip_bps=0), "price under $5")
        t = m.session_trade("AAA", "2026-10-01", day, atr=2.0, rvol=1.0, prev_close=99.9, slip_bps=0, stop_mode="range")
        self.assertAlmostEqual(t["risk"], 1.5, msg="the opening bar's range, wider than 0.10 ATR")
        self.assertAlmostEqual(t["stop"], 101.0 - 1.5)


class TestStudy(unittest.TestCase):

    def test_atr_and_rvol_come_from_prior_sessions_only(self):
        parsed, first = [], {}
        for i in range(1, 22):
            d = f"2026-10-{i:02d}"
            vol = 5000 if i == 21 else 1000
            day = [{**bar(570, 100, 101, 99, 100.6, vol), "d": d}, {**bar(575, 100.7, 101.4, 100.6, 101.3), "d": d}] + \
                  [{**bar(mm, 101.3, 101.5, 101.1, 101.3), "d": d} for mm in range(580, 956, 5)]
            first[d] = len(parsed); parsed.extend(day)
        out = m.ticker_trades("AAA", parsed, first, 0)
        self.assertEqual(out[0]["session"], "2026-10-15", "14 sessions of true range are needed first")
        last = out[-1]
        self.assertAlmostEqual(last["rvol"], 5.0, places=6, msg="5000 against a 1000 baseline of the prior 14 opens")
        self.assertAlmostEqual(last["atr"], 2.5, places=6, msg="true range: 101.5−99 beats either leg against the 101.3 prior close")
        self.assertEqual(m.bucket(last["rvol"]), "3–10"); self.assertEqual(m.bucket(0.2), "<1"); self.assertIsNone(m.bucket(None))

    def test_tabulate_splits_by_rvol_and_the_gap_gate(self):
        tr = [{"session": "s1", "side": "long", "r_net": 1.0, "mirror_r_net": -1.0, "reason": "flat", "rvol": 0.5, "gap_atr": 0.1},
              {"session": "s2", "side": "long", "r_net": 0.5, "mirror_r_net": -0.5, "reason": "stop", "rvol": 4.0, "gap_atr": 1.0},
              {"session": "s3", "side": "short", "r_net": -0.2, "mirror_r_net": 0.2, "reason": "stop", "rvol": None, "gap_atr": -0.9}]
        b = m.tabulate(tr)
        self.assertEqual(b["all"]["all"]["trade"]["n"], 3); self.assertEqual(b["all"]["<1"]["trade"]["n"], 1)
        self.assertEqual(b["all"]["rvol≥3"]["trade"]["n"], 1); self.assertEqual(b["gap≥0.7atr"]["all"]["trade"]["n"], 2)
        self.assertEqual(b["gap≥0.7atr"]["3–10"]["stop_share"], 1.0)
        self.assertAlmostEqual(b["all"]["all"]["direction"]["mean"], (2.0 + 1.0 - 0.4) / 3, places=3)


if __name__ == "__main__":
    unittest.main()
