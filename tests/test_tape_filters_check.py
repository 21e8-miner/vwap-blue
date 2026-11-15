"""
tape_filters_check.py: the four replay rules (gap × RVOL, SPY half-hour gate, breadth at the trigger, the
small-gap fade) and their mirrors on hand-built sessions.
"""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tape_filters_check as m  # noqa: E402

COST = 0.0008      # honest.COST["equity"], round trip as a fraction of price


def bar(mins, o, h, l, c, v=1000, d="2026-10-01"):
    hh, mm = divmod(mins, 60)
    return {"d": d, "mins": mins, "o": o, "h": h, "l": l, "c": c, "v": v, "time": f"{d[5:]} {hh:02d}:{mm:02d}"}


def flat(px, start, end=955, d="2026-10-01"):
    return [bar(mm, px, px + 0.01, px - 0.01, px, d=d) for mm in range(start, end + 1, 5)]


def net(gross, entry, risk):
    return gross / risk - COST * entry / risk


class TestBracket(unittest.TestCase):

    def test_stop_is_checked_before_target_and_a_gap_through_the_stop_fills_at_the_open(self):
        both = [bar(575, 100, 102, 98, 100)]
        self.assertEqual(m.walk_bracket(both, 0, "long", 100, 99, 101, 925, 0), (99, "stop"))
        self.assertEqual(m.walk_bracket(both, 0, "short", 100, 101, 99, 925, 0), (101, "stop"))
        gapped = [bar(575, 100, 100.2, 99.9, 100), bar(580, 98.5, 98.6, 98.0, 98.2)]
        self.assertEqual(m.walk_bracket(gapped, 0, "long", 100, 99, None, 925, 0), (98.5, "stop"))

    def test_the_fill_bar_itself_can_stop_the_trade_and_the_flat_bar_ends_it(self):
        day = [bar(570, 100, 100, 100, 100), bar(575, 100, 100.1, 98.9, 99.5)] + flat(99.5, 580)
        self.assertEqual(m.walk_bracket(day, 1, "long", 100, 99, None, 925, 0)[1], "stop")
        day = flat(100, 575, 925) + [bar(930, 100, 100, 90, 90)]
        self.assertEqual(m.walk_bracket(day, 0, "long", 100, 99, None, 925, 0), (100, "flat"), "the 15:30 bar is never seen")

    def test_slippage_is_against_the_trade_on_the_way_in_and_out(self):
        self.assertAlmostEqual(m.slipped(100, "long", 10, True), 100.1); self.assertAlmostEqual(m.slipped(100, "long", 10, False), 99.9)
        self.assertAlmostEqual(m.slipped(100, "short", 10, True), 99.9); self.assertAlmostEqual(m.slipped(100, "short", 10, False), 100.1)


class TestGap(unittest.TestCase):

    def day(self):
        return [bar(570, 100, 100.6, 99.9, 100.4), bar(575, 100.5, 100.9, 100.4, 100.8)] + \
               [bar(mm, 101, 102.1, 100.9, 102) for mm in range(580, 926, 5)] + [bar(930, 102, 102, 90, 90)]

    def test_continuation_fills_at_the_0935_open_with_a_half_atr_stop_and_is_flat_at_1525(self):
        t = m.gap_trade("AAA", "2026-10-01", self.day(), atr=2.0, rvol=2.0, prev_close=98.0, slip_bps=0)
        self.assertEqual((t["side"], t["fill_time"]), ("long", "10-01 09:35"))
        self.assertAlmostEqual(t["entry"], 100.5); self.assertAlmostEqual(t["stop"], 99.5); self.assertAlmostEqual(t["exit"], 102.0)
        self.assertAlmostEqual(t["r_net"], net(1.5, 100.5, 1.0), places=3)
        self.assertEqual(t["mirror_reason"], "stop", "the fade of the same open is stopped 1 ATR/2 higher")
        self.assertAlmostEqual(t["mirror_r_net"], net(-1.0, 100.5, 1.0), places=3)
        self.assertAlmostEqual(t["gap_atr"], 1.0)

    def test_a_gap_down_is_a_short_and_slippage_costs(self):
        day = [bar(570, 100, 100.2, 99.5, 99.7), bar(575, 99.6, 99.7, 99.0, 99.1)] + flat(98.5, 580)
        a = m.gap_trade("AAA", "2026-10-01", day, 2.0, 0.5, 101.0, 0)
        b = m.gap_trade("AAA", "2026-10-01", day, 2.0, 0.5, 101.0, 10)
        self.assertEqual(a["side"], "short"); self.assertAlmostEqual(a["gap_atr"], -0.5)
        self.assertGreater(a["r_net"], 0); self.assertLess(b["r_net"], a["r_net"]); self.assertLess(b["entry"], a["entry"])

    def test_no_trade_without_a_gap_a_prior_close_or_the_universe(self):
        d = self.day()
        self.assertIsNone(m.gap_trade("AAA", "s", d, 2.0, 1.0, 100.0, 0), "no gap")
        self.assertIsNone(m.gap_trade("AAA", "s", d, 2.0, 1.0, None, 0), "first session: no prior close")
        self.assertIsNone(m.gap_trade("AAA", "s", d, None, 1.0, 98.0, 0), "no ATR yet")
        self.assertIsNone(m.gap_trade("SGOV", "s", d, 0.02, 1.0, 98.0, 0), "ATR under $0.50")
        penny = [bar(570, 3, 3.1, 2.9, 3.05), bar(575, 3.1, 3.2, 3.0, 3.15)] + flat(3.15, 580)
        self.assertIsNone(m.gap_trade("PNY", "s", penny, 0.6, 1.0, 2.9, 0), "price under $5")
        self.assertIsNone(m.gap_trade("AAA", "s", d[1:], 2.0, 1.0, 98.0, 0), "no 09:30 bar")

    def test_cells_are_gap_over_atr_by_rvol_and_unknown_rvol_is_left_out(self):
        tr = [{"session": f"s{i}", "r_net": 1.0, "mirror_r_net": -1.2, "reason": "flat", "gap_atr": g, "rvol": r}
              for i, (g, r) in enumerate([(0.2, 0.5), (-0.7, 2.0), (1.5, 5.0), (-3.0, 0.9), (2.0, None), (0.5, 1.0)])]
        g = m.tabulate_gap(tr)
        self.assertEqual(len(g["cells"]), 12)
        for key in ("<0.5 | <1", "1–2 | ≥3", "≥2 | <1"):
            self.assertEqual(g["cells"][key]["trade"]["n"], 1, key)
        self.assertEqual(g["cells"]["0.5–1 | 1–3"]["trade"]["n"], 2, "a gap of exactly 0.5 ATR and an RVOL of exactly 1 sit in the upper buckets")
        self.assertEqual(g["cells"]["0.5–1 | <1"]["trade"]["n"], 0)
        self.assertEqual(g["margins"]["all"]["trade"]["n"], 5)
        self.assertAlmostEqual(g["margins"]["all"]["direction"]["mean"], 2.2)


class TestSpyGate(unittest.TestCase):

    def half_hour(self, last, d="2026-10-01"):
        """09:30–09:55 walking from 100 to `last`, then a 10:00 bar opening 0.1 further on."""
        step = (last - 100) / 6
        px = [100 + step * i for i in range(7)]
        day = [bar(570 + 5 * i, px[i], max(px[i], px[i + 1]), min(px[i], px[i + 1]), px[i + 1], d=d) for i in range(6)]
        nxt = last + (0.1 if last > 100 else -0.1)
        return day + [bar(600, nxt, nxt + 0.01, nxt - 0.01, nxt, d=d)] + flat(nxt, 605, d=d)

    def test_the_trade_is_the_names_own_first_half_hour_filled_at_the_1000_open(self):
        t = m.half_hour_trade("AAA", "2026-10-01", self.half_hour(101.0), atr=2.0, prev_close=100.0, slip_bps=0)
        self.assertEqual((t["side"], t["fill_time"]), ("long", "10-01 10:00"))
        self.assertAlmostEqual(t["entry"], 101.1); self.assertAlmostEqual(t["stop"], 100.1); self.assertAlmostEqual(t["own_m10_atr"], 0.5)
        s = m.half_hour_trade("AAA", "2026-10-01", self.half_hour(99.0), 2.0, 100.0, 0)
        self.assertEqual(s["side"], "short"); self.assertAlmostEqual(s["stop"], 99.9)

    def test_no_trade_without_a_bar_between_1000_and_1030_or_when_unchanged_at_1000(self):
        d = self.half_hour(101.0)
        self.assertIsNone(m.half_hour_trade("AAA", "s", d[:6] + flat(101, 635), 2.0, 100.0, 0), "first bar after 10:00 is 10:35")
        self.assertIsNone(m.half_hour_trade("AAA", "s", d, 2.0, 101.0, 0), "09:55 close equals the prior close")
        self.assertIsNone(m.half_hour_trade("AAA", "s", d[6:], 2.0, 100.0, 0), "nothing before 10:00")

    def test_opening_sigma_is_the_root_sum_of_squared_five_minute_log_returns(self):
        day = [bar(570, 100, 101, 100, 101)] + [bar(mm, 101, 101, 101, 101) for mm in range(575, 600, 5)] + [bar(600, 150, 150, 150, 150)]
        self.assertAlmostEqual(m.opening_sigma(day), math.log(1.01), places=12, msg="the 10:00 bar is not in it")
        self.assertIsNone(m.opening_sigma(day[1:]), "no 09:30 bar")

    def test_spy_context_ranks_sigma_against_prior_sessions_only(self):
        parsed, first = [], {}
        for i in range(1, 25):
            d = f"2026-09-{i:02d}"
            last = {22: 100.01, 23: 103.0, 24: 99.0}.get(i, 100.3)      # quiet, wild up, down
            first[d] = len(parsed); parsed.extend(self.half_hour(last, d=d))
        ctx = m.spy_context(parsed, first)
        self.assertNotIn("2026-09-01", ctx, "no prior close on the first session")
        self.assertIsNone(ctx["2026-09-21"]["tercile"], "19 prior sigmas: not enough to rank")
        self.assertEqual(ctx["2026-09-22"]["tercile"], "low"); self.assertEqual(ctx["2026-09-22"]["pct"], 0.0)
        self.assertEqual(ctx["2026-09-23"]["tercile"], "high"); self.assertEqual(ctx["2026-09-23"]["pct"], 1.0)
        self.assertEqual(ctx["2026-09-23"]["sign"], 1); self.assertEqual(ctx["2026-09-24"]["sign"], -1)
        self.assertLess(ctx["2026-09-24"]["pct"], 1.0, "the wild session before it now counts against it")

    def test_cells_are_tercile_by_with_or_against_spy(self):
        ctx = {"s1": {"tercile": "high", "sign": 1}, "s2": {"tercile": "high", "sign": -1}, "s3": {"tercile": None, "sign": 1},
               "s4": {"tercile": "low", "sign": 0}}
        tr = [{"session": s, "side": side, "r_net": 0.5, "mirror_r_net": -0.7, "reason": "flat"}
              for s, side in (("s1", "long"), ("s1", "short"), ("s2", "long"), ("s3", "long"), ("s4", "long"), ("s9", "long"))]
        g = m.tabulate_spy(tr, ctx)
        self.assertEqual(g["cells"]["high | with"]["trade"]["n"], 1); self.assertEqual(g["cells"]["high | against"]["trade"]["n"], 2)
        self.assertEqual(g["margins"]["all"]["trade"]["n"], 3, "unranked sessions, a flat SPY and unknown sessions are dropped")
        self.assertEqual(m.with_or_against("short", -1), "with"); self.assertIsNone(m.with_or_against("long", 0))


class TestBreadth(unittest.TestCase):

    def test_tally_counts_names_above_and_below_their_prior_close_per_bar(self):
        acc = m.defaultdict(dict)
        m.breadth_add(acc, "s", [bar(570, 100, 101, 99, 101), bar(575, 101, 101, 99, 99.5)], 100.0)
        m.breadth_add(acc, "s", [bar(570, 50, 50, 49, 49), bar(575, 49, 50, 49, 50)], 50.0)
        m.breadth_add(acc, "s", [bar(570, 10, 10, 10, 10)], None)
        self.assertEqual(acc["s"][570], [1, 1, 2]); self.assertEqual(acc["s"][575], [0, 1, 2], "an unchanged name is neither")

    def test_ad_leaves_the_name_itself_out_and_needs_twenty_others(self):
        self.assertAlmostEqual(m.ad_without([15, 5, 25], 1), (14 - 5) / 24)
        self.assertAlmostEqual(m.ad_without([15, 5, 25], -1), (15 - 4) / 24)
        self.assertAlmostEqual(m.ad_without([15, 5, 25], 0), 10 / 24)
        self.assertIsNone(m.ad_without([10, 5, 20], 1)); self.assertIsNone(m.ad_without(None, 1))

    def test_the_trade_is_the_range_stop_breakout_tagged_with_the_bar_before_its_fill(self):
        day = [bar(570, 100, 101, 99.5, 100.8), bar(575, 100.7, 100.9, 100.5, 100.8), bar(580, 100.9, 101.6, 100.8, 101.5)] + flat(101.6, 585)
        t = m.breadth_trade("AAA", "2026-10-01", day, atr=2.0, rvol=1.5, prev_close=100.75, slip_bps=0)
        self.assertEqual((t["fill_time"], t["trig_min"], t["own_sign"]), ("10-01 09:40", 575, 1))
        self.assertAlmostEqual(t["risk"], 1.5, msg="the opening range")
        self.assertIsNone(m.breadth_trade("AAA", "s", day, 2.0, 1.5, None, 0))

    def test_cells_are_one_sidedness_by_with_or_against_the_tape(self):
        acc = {"s1": {575: [90, 10, 101]}, "s2": {575: [10, 85, 101]}, "s3": {575: [55, 45, 101]}, "s4": {575: [5, 5, 11]}}
        base = {"r_net": -0.2, "mirror_r_net": 0.1, "reason": "stop", "trig_min": 575, "own_sign": 0}
        tr = [{**base, "session": s, "side": side} for s, side in
              (("s1", "long"), ("s1", "short"), ("s2", "long"), ("s3", "long"), ("s4", "long"), ("s5", "long"))]
        g = m.tabulate_breadth(tr, acc)
        self.assertEqual(g["cells"]["≥0.6 | with"]["trade"]["n"], 1); self.assertEqual(g["cells"]["≥0.6 | against"]["trade"]["n"], 2)
        self.assertEqual(g["cells"]["<0.3 | with"]["trade"]["n"], 1)
        self.assertEqual(g["margins"]["all"]["trade"]["n"], 4, "a thin bar (10 other names) and an unknown session are dropped")
        self.assertEqual(g["cells"]["≥0.6 | against"]["stop_share"], 1.0)


class TestFade(unittest.TestCase):

    def gap_up(self, after):
        return [bar(570, 100.6, 100.7, 100.4, 100.5), bar(575, 100.5, 100.55, 100.3, 100.4)] + after

    def test_fade_targets_the_prior_close_with_the_stop_half_an_atr_beyond_the_open(self):
        day = self.gap_up([bar(580, 100.4, 100.4, 99.9, 99.95)] + flat(99.9, 585))
        t = m.fade_trade("AAA", "2026-10-01", day, atr=2.0, rvol=0.6, prev_close=100.0, slip_bps=0)
        self.assertEqual((t["side"], t["fill_time"], t["reason"]), ("short", "10-01 09:35", "target"))
        self.assertAlmostEqual(t["entry"], 100.5); self.assertAlmostEqual(t["stop"], 101.6); self.assertAlmostEqual(t["exit"], 100.0)
        self.assertAlmostEqual(t["risk"], 1.1); self.assertAlmostEqual(t["rr"], 0.5 / 1.1, places=3)
        self.assertAlmostEqual(t["r_net"], net(0.5, 100.5, 1.1), places=3)
        self.assertEqual(t["mirror_reason"], "flat", "the long from 100.5 (stop 99.4, target 101.0) sits at 99.9 until noon")
        self.assertAlmostEqual(t["mirror_r_net"], net(-0.6, 100.5, 1.1), places=3)
        self.assertAlmostEqual(t["gap_atr"], 0.3)

    def test_flat_at_noon_and_stop_beyond_the_open(self):
        drift = self.gap_up(flat(100.3, 580, 715) + [bar(720, 100.3, 100.3, 99.0, 99.0)] + flat(99.0, 725))
        t = m.fade_trade("AAA", "2026-10-01", drift, 2.0, 0.6, 100.0, 0)
        self.assertEqual(t["reason"], "flat"); self.assertAlmostEqual(t["exit"], 100.3, msg="the 11:55 close; the 12:00 bar's fill is too late")
        run = self.gap_up([bar(580, 100.4, 101.7, 100.4, 101.65)] + flat(101.65, 585))
        s = m.fade_trade("AAA", "2026-10-01", run, 2.0, 0.6, 100.0, 0)
        self.assertEqual(s["reason"], "stop"); self.assertAlmostEqual(s["exit"], 101.6); self.assertAlmostEqual(s["r_net"], net(-1.1, 100.5, 1.1), places=3)
        self.assertEqual(s["mirror_reason"], "target", "the continuation's target is the fade's distance to the prior close")

    def test_gap_down_is_a_long_and_a_gap_closed_by_0935_is_no_trade(self):
        day = [bar(570, 99.4, 99.6, 99.3, 99.5), bar(575, 99.5, 100.1, 99.5, 100.0)] + flat(100.0, 580)
        t = m.fade_trade("AAA", "2026-10-01", day, 2.0, 0.6, 100.0, 0)
        self.assertEqual((t["side"], t["reason"]), ("long", "target")); self.assertAlmostEqual(t["stop"], 98.4)
        closed = [bar(570, 100.6, 100.7, 99.9, 99.95), bar(575, 99.95, 100, 99.9, 99.9)] + flat(99.9, 580)
        self.assertIsNone(m.fade_trade("AAA", "s", closed, 2.0, 0.6, 100.0, 0))
        self.assertIsNone(m.fade_trade("AAA", "s", day, 0.4, 0.6, 100.0, 0), "ATR under $0.50")

    def test_the_hypothesis_cell_is_small_gap_low_rvol(self):
        tr = [{"session": f"s{i}", "r_net": 0.1, "mirror_r_net": -0.3, "reason": "target", "gap_atr": g, "rvol": r}
              for i, (g, r) in enumerate([(0.15, 0.99), (-0.49, 0.2), (0.3, 1.0), (-0.6, 0.5), (0.1, 0.5), (1.2, 0.5), (0.3, None)])]
        g = m.tabulate_fade(tr)
        self.assertEqual(g["cells"]["0.15–0.5 | <1"]["trade"]["n"], 2); self.assertEqual(g["cells"]["0.15–0.5 | ≥1"]["trade"]["n"], 1)
        self.assertEqual(g["cells"]["0.5–1 | <1"]["trade"]["n"], 1); self.assertEqual(g["margins"]["all"]["trade"]["n"], 4)


class TestStudy(unittest.TestCase):

    def source(self, sessions=20):
        def name(tk, px, vol_last):
            parsed, first = [], {}
            for i in range(1, sessions + 1):
                d = f"2026-10-{i:02d}"
                v = vol_last if i == sessions else 1000
                day = [bar(570, px, px + 1, px - 1, px + 0.6, v, d=d), bar(575, px + 0.7, px + 1.4, px + 0.6, px + 1.3, d=d)] + \
                      [bar(mm, px + 1.3, px + 1.5, px + 1.1, px + 0.5, d=d) for mm in range(580, 951, 5)] + \
                      [bar(955, px + 0.5, px + 1.0, px + 0.4, px + 0.9, d=d)]
                first[d] = len(parsed); parsed.extend(day)
            return tk, parsed, first
        return [name("SPY", 500.0, 1000), name("AAA", 100.0, 5000)]

    def test_atr_and_rvol_come_from_prior_sessions_only(self):
        _, parsed, first = self.source()[1]
        rows = list(m.session_inputs(parsed, first))
        self.assertIsNone(rows[0][4]); self.assertIsNone(rows[13][2], "13 true ranges are not 14")
        self.assertAlmostEqual(rows[14][2], 2.5, places=6, msg="true range: the day's 101.5−99 beats either leg against the 100.9 prior close")
        self.assertIsNone(rows[4][3]); self.assertAlmostEqual(rows[5][3], 1.0)
        self.assertAlmostEqual(rows[-1][3], 5.0, msg="5000 against the prior 14 opens at 1000")
        self.assertAlmostEqual(rows[-1][4], 100.9)

    def test_study_runs_all_four_and_counts_every_cell(self):
        res = m.study(iter(self.source()), slip_bps=0)
        self.assertEqual(sorted(res), [2, 3, 4, 5])
        self.assertEqual(res[2]["n"], 12, "two names, sessions 15–20")
        self.assertEqual(res[3]["n"], 6, "SPY is the gate, not a trade")
        self.assertEqual(res[2]["window"], ["2026-10-15", "2026-10-20"])
        for r in res.values():
            mc = r["multiple_comparisons"]
            self.assertEqual(mc["cells_all_replays"], 12 + 6 + 6 + 4); self.assertEqual(mc["tests_all_replays"], 56)
            self.assertAlmostEqual(mc["bonferroni_t"], 3.32, places=2)
        self.assertEqual(res[5]["hypothesis_cell"], "0.15–0.5 | <1")
        only = m.study(iter(self.source()), slip_bps=0, which=(5,))
        self.assertEqual(only[5]["multiple_comparisons"]["cells_all_replays"], 4)

    def test_comparisons_name_what_clears_the_nominal_and_the_family_bar(self):
        def c(vals):
            rows = [{"session": f"s{i}", "r_net": v, "mirror_r_net": -v, "reason": "flat"} for i, v in enumerate(vals)]
            return m.cell(rows)
        cells = {"strong": c([0.5, 0.52, 0.48, 0.5] * 5), "weak": c([1.0, -0.3, 0.8, -0.3, 0.6, -0.1] * 4), "few": c([0.5, 0.51, 0.49])}
        mc = m.comparisons(cells, family=28)
        self.assertEqual(mc["trade_positive_bonferroni"], ["strong"]); self.assertEqual(mc["mirror_positive_nominal"], [])
        self.assertIn("weak", mc["trade_positive_nominal"]); self.assertNotIn("weak", mc["trade_positive_bonferroni"])
        self.assertNotIn("few", mc["trade_positive_nominal"], "three sessions prove nothing")
        self.assertEqual(sorted(mc["direction_bonferroni"]), ["strong"])
        self.assertAlmostEqual(m.bonferroni_t(1), 2.24, places=2)


if __name__ == "__main__":
    unittest.main()
