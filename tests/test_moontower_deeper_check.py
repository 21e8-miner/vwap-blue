"""
moontower_deeper_check.py: the pure pieces of the four follow-ups (re-cut brackets, the regime ratio, betas,
Black–Scholes marks and the vertical pick).
"""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import moontower_deeper_check as m  # noqa: E402


def bar(mins, o, h, l, c, time="10-01 10:00"):
    return {"d": "2026-10-01", "mins": mins, "o": o, "h": h, "l": l, "c": c, "v": 1000, "time": time}


class TestResize(unittest.TestCase):

    def setUp(self):
        # trigger at bar 0 (close 100), fill next open 100.2; price dips to 98.6 then runs to 103
        self.session = [bar(570, 100, 100.5, 99.5, 100, "10-01 09:30"), bar(575, 100.2, 100.4, 98.6, 99.0, "10-01 09:35"),
                        bar(580, 99.0, 103.0, 98.9, 102.5, "10-01 09:40"), bar(585, 102.5, 102.6, 101.0, 101.5, "10-01 09:45")]
        self.trade = {"ticker": "AAA", "side": "long", "entry": 100.2, "stop": 99.0, "target": 102.0, "trigger_time": "10-01 09:30"}

    def test_a_narrow_stop_is_hit_and_a_wide_one_reaches_the_target(self):
        move = 0.02                                              # 2% implied daily move
        narrow = m.resize(self.trade, self.session, 0, move, 0.5, False)    # stop 1% below: 99.2, hit at 98.6
        wide = m.resize(self.trade, self.session, 0, move, 1.0, False)      # stop 2% below: 98.2, survives
        self.assertEqual(narrow["reason"], "stop"); self.assertLess(narrow["r_net"], 0)
        self.assertEqual(wide["reason"], "target"); self.assertGreater(wide["r_net"], 0)
        self.assertAlmostEqual(wide["risk_pct"], 2.0, places=6)

    def test_capping_the_target_inside_one_move(self):
        far = {**self.trade, "target": 106.0}
        v = m.resize(far, self.session, 0, 0.02, 1.0, True)                 # target pulled to 102.204
        self.assertEqual(v["reason"], "target")
        self.assertIsNone(m.resize({**self.trade, "target": 100.0}, self.session, 0, 0.02, 1.0, False), "a target at the fill is no plan")

    def test_trigger_index_matches_the_bar_time(self):
        self.assertEqual(m.trigger_index(self.session, "10-01 09:40"), 2)
        self.assertIsNone(m.trigger_index(self.session, "10-01 11:00"))


class TestRegimeAndBeta(unittest.TestCase):

    def test_regime_ratio_is_the_prior_sessions_move_over_the_move_priced_before_it(self):
        days = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"]
        ck = {}
        for i in range(25):                                                     # 25 names, all identical
            ck[f"T{i}"] = {d: {"daily_return": 0.01, "iv_30d": 0.16 * math.sqrt(252) / 100 * 100 / 100 * 100 / 100 if False else 0.16} for d in days}
        # implied daily move = 0.16/√252 ≈ 1.008%; |ret| 1% → ratio ≈ 0.992
        r = m.regime_ratio(ck, list(ck), "2026-10-06", 1)
        self.assertAlmostEqual(r, 0.01 / (0.16 / math.sqrt(252)), places=6)
        self.assertIsNone(m.regime_ratio(ck, list(ck)[:5], "2026-10-06", 1), "needs 20 names")
        self.assertIsNone(m.regime_ratio(ck, list(ck), "2026-10-02", 1), "needs two sessions before")

    def test_beta_is_the_regression_slope_on_daily_returns(self):
        days = [f"2026-10-{d:02d}" for d in range(1, 41)]
        spy = {d: (i % 5 - 2) / 100 for i, d in enumerate(days)}
        rets = {"X2": {d: 2 * spy[d] + 0.001 for d in days}, "SHORT": {d: 0.0 for d in days[:5]}}
        b = m.betas(rets, spy, ["X2", "SHORT"])
        self.assertAlmostEqual(b["X2"], 2.0, places=9)
        self.assertNotIn("SHORT", b, "fewer than 30 paired days")

    def test_daily_returns_are_session_close_to_session_close(self):
        parsed = [bar(570, 100, 101, 99, 100.5, "10-01 09:30"), {**bar(955, 100, 101, 99, 101.0, "10-01 15:55")},
                  {**bar(970, 101, 102, 100, 103.0, "10-01 16:10")},                                   # after hours: not the close
                  {**bar(570, 101, 103, 100, 102.0, "10-02 09:30"), "d": "2026-10-02"}, {**bar(955, 102, 103, 101, 102.0, "10-02 15:55"), "d": "2026-10-02"}]
        r = m.daily_returns(parsed, "AAA")
        self.assertEqual(list(r), ["2026-10-02"])
        self.assertAlmostEqual(r["2026-10-02"], 102.0 / 101.0 - 1, places=9)


class TestVertical(unittest.TestCase):

    def test_black_scholes_put_call_parity_and_intrinsic_at_expiry(self):
        s, k, t, iv, r = 100.0, 95.0, 30 / 365, 0.3, 0.04
        c, p = m.bs("call", s, k, t, iv, r), m.bs("put", s, k, t, iv, r)
        self.assertAlmostEqual(c - p, s - k * math.exp(-r * t), places=9)
        self.assertAlmostEqual(m.bs("call", 100, 95, 0, 0.3), 5.0)
        self.assertAlmostEqual(m.bs("put", 100, 95, 0, 0.3), 0.0)

    def test_pick_vertical_mirrors_the_pages_contract(self):
        def row(exp, strike, dte=8):
            return {"expiry_date": exp, "dte": dte, "strike": strike, "call_ask_price": 3.0, "call_mid_price": 2.8, "call_bid_price": 2.6,
                    "put_ask_price": 3.0, "put_mid_price": 2.8, "put_bid_price": 2.6, "call_mid_iv": 30, "put_mid_iv": 31}
        chain = [row("2026-10-01", 100, 1), row("2026-10-09", 97.5), row("2026-10-09", 100), row("2026-10-09", 102.5), row("2026-10-09", 105)]
        v = m.pick_vertical(chain, "long", 100.2, 102.6, "2026-10-01")
        self.assertEqual(v["expiry"], "2026-10-09")
        self.assertEqual((v["long"]["strike"], v["short"]["strike"]), (100, 105), "first strike at or beyond the target")
        self.assertAlmostEqual(v["debit_ask"], 3.0 - 2.6); self.assertEqual(v["width"], 5)
        v = m.pick_vertical(chain, "short", 99.9, 97.6, "2026-10-01")
        self.assertEqual((v["leg"], v["long"]["strike"], v["short"]["strike"]), ("put", 100, 97.5))
        self.assertIsNone(m.pick_vertical(chain, "long", 104.9, 110, "2026-10-01"), "nothing beyond the target: same strike")
        self.assertIsNone(m.pick_vertical(chain, "long", 100, 102, "2026-10-09"), "no expiry after the session")


if __name__ == "__main__":
    unittest.main()
