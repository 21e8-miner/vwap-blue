"""
moontower_ideas_check.py: Moontower's nightly idea lists scored against the next sessions' cockpit rows.
"""

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import moontower_ideas_check as mi  # noqa: E402

DAYS = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12"]


def cockpit_rows(ticker, closes, iv30=0.32, iv90=0.30, iv90_end=0.30, iv30_end=0.32):
    rows = []
    for i, (d, c) in enumerate(zip(DAYS, closes)):
        last = i == len(DAYS) - 1
        rows.append({"ticker": ticker, "date": d, "close": c, "iv_30d": iv30_end if last else iv30, "iv_90d": iv90_end if last else iv90,
                     "daily_return": None if i == 0 else c / closes[i - 1] - 1})
    return rows


class TestOutcomes(unittest.TestCase):

    def test_forward_realized_vol_is_annualized_close_to_close(self):
        closes = [100, 101, 100, 102, 101, 103]
        rv = mi.fwd_realized(closes)
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        mu = sum(rets) / len(rets)
        var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
        self.assertAlmostEqual(rv, math.sqrt(var * 252) * 100, places=6)
        self.assertIsNone(mi.fwd_realized([100, 101]))
        self.assertIsNone(mi.fwd_realized([100, None, 101, 102]))

    def test_outcomes_use_the_next_five_sessions_and_the_picks_own_iv(self):
        closes = [100, 100, 100, 100, 100, 100, 100, 110]        # flat, then a jump outside the horizon
        ck = {r["date"]: r for r in cockpit_rows("AAA", closes, iv30=0.32)}
        o = mi.outcomes(ck, "2026-10-01")
        self.assertEqual(o["iv30"], 32.0)
        self.assertAlmostEqual(o["rv_fwd"], 0.0)
        self.assertAlmostEqual(o["vol_edge_long"], -32.0)
        self.assertAlmostEqual(o["next_move_ratio"], 0.0)
        self.assertIsNone(mi.outcomes(ck, "2026-10-07"), "fewer than five sessions ahead")
        self.assertIsNone(mi.outcomes(ck, "2026-09-30"), "not a cockpit date")

    def test_next_day_move_is_in_implied_daily_moves(self):
        closes = [100, 102, 102, 102, 102, 102, 102, 102]        # +2% the next day on a 32% IV
        ck = {r["date"]: r for r in cockpit_rows("AAA", closes, iv30=0.32)}
        o = mi.outcomes(ck, "2026-10-01")
        self.assertAlmostEqual(o["next_move_ratio"], 0.02 / (0.32 / math.sqrt(252)), places=6)

    def test_term_change_is_back_minus_front_over_the_horizon(self):
        closes = [100] * 8
        ck = {r["date"]: r for r in cockpit_rows("AAA", closes, iv30=0.32, iv90=0.30, iv30_end=0.30, iv90_end=0.34)}
        # the row five sessions after Oct 1 is Oct 8 (not the last), so no change there; from Oct 2 the horizon ends Oct 9... use Oct 5 → Oct 12
        o = mi.outcomes(ck, "2026-10-05")
        self.assertAlmostEqual(o["term_change"], ((0.34 - 0.30) - (0.30 - 0.32)) * 100, places=6)

    def test_earnings_within_looks_forward_only(self):
        rec = {"next_earnings_date": "2026-10-10", "prior_earnings_date": "2026-07-20"}
        self.assertTrue(mi.earnings_within(rec, "2026-10-01", 14))
        self.assertFalse(mi.earnings_within(rec, "2026-10-01", 5))
        self.assertFalse(mi.earnings_within(None, "2026-10-01", 14))


class TestRun(unittest.TestCase):

    def test_filters_split_picked_from_rest_and_sign_follows_the_idea(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # AAA realizes far above its IV (a Buy Vol winner); BBB sits still (a Sell Vol winner)
            aaa = cockpit_rows("AAA", [100, 108, 100, 109, 99, 110, 100, 100], iv30=0.30)
            bbb = cockpit_rows("BBB", [100, 100.1, 100, 100.1, 100, 100.1, 100, 100], iv30=0.30)
            (root / "cockpit_x.json").write_text(json.dumps(aaa + bbb))
            (root / "earnings.json").write_text(json.dumps([{"ticker": "AAA", "next_earnings_date": "2026-10-08", "prior_earnings_date": None}]))
            for day in DAYS[:2]:
                (root / f"ideas_{day}.json").write_text(json.dumps({"summary": {"tradeDate": day}, "tickers": [
                    {"ticker": "AAA", "idea": "Buy Vol", "totalScore": 60}, {"ticker": "BBB", "idea": "Buy Vol", "totalScore": -5},
                    {"ticker": "AAA", "idea": "Sell Vol", "totalScore": -5}, {"ticker": "BBB", "idea": "Sell Vol", "totalScore": 60},
                    {"ticker": "ZZZ", "idea": "Buy Vol", "totalScore": 99}]}))      # no cockpit history: ignored
            (root / "ideas_2026-10-03.json").write_text(json.dumps({"summary": {"tradeDate": "2026-10-02"}, "tickers": []}))   # fallback snapshot: refused
            res = mi.run(root)
        self.assertEqual(res["snapshots"], DAYS[:2])
        bv = res["ideas"]["Buy Vol"]["filters"]["score>20"]["edge"]
        self.assertEqual(bv["picked"]["n"], 2); self.assertEqual(bv["rest"]["n"], 2)
        self.assertGreater(bv["picked"]["mean"], 0); self.assertLess(bv["rest"]["mean"], 0)
        sv = res["ideas"]["Sell Vol"]["filters"]["score>20"]["edge"]
        self.assertGreater(sv["picked"]["mean"], 0, "a still name is a Sell Vol winner")
        ne = res["ideas"]["Buy Vol"]["filters"]["score>20, no earnings 2w"]["edge"]
        self.assertEqual(ne["picked"]["n"], 0, "AAA reports inside two weeks")
        top = res["ideas"]["Buy Vol"]["filters"]["top10"]["edge"]
        self.assertEqual(top["picked"]["n"], 4, "top 10 of two names a night is both")


if __name__ == "__main__":
    unittest.main()
