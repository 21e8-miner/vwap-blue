"""
contract.js: a VWAP Blue bracket as a vertical on a Moontower chain. Run under node, like the engine parity test.
"""

import json
import math
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def chain_row(expiry, dte, strike, call=(10.0, 11.0, 12.0), put=(8.0, 9.0, 10.0), spot=100.0):
    return {"ticker": "T", "trade_date": "2026-10-29", "expiry_date": expiry, "dte": dte, "strike": strike, "spot_price": spot,
            "call_bid_price": call[0], "call_mid_price": call[1], "call_ask_price": call[2], "call_delta": 0.5, "call_mid_iv": 30.0,
            "call_volume": 1, "call_open_interest": 2,
            "put_bid_price": put[0], "put_mid_price": put[1], "put_ask_price": put[2], "put_delta": -0.5, "put_mid_iv": 31.0,
            "put_volume": 3, "put_open_interest": 4, "smoothed_vol": 30.5}


CHAIN = [
    chain_row("2026-10-30", 1, 100, call=(5, 5.5, 6)),                       # expires today: skipped
    chain_row("2026-11-06", 8, 97.5, call=(4.0, 4.5, 5.0), put=(1.0, 1.2, 1.4)),
    chain_row("2026-11-06", 8, 100, call=(2.0, 2.3, 2.6), put=(2.0, 2.3, 2.6)),
    chain_row("2026-11-06", 8, 102.5, call=(0.9, 1.0, 1.1), put=(4.0, 4.4, 4.8)),
    chain_row("2026-11-06", 8, 130, call=(0.01, 0.02, 0.03)),                # outside the ±15% band
    chain_row("2026-12-18", 50, 100),                                       # past 45 dte
]


def run(expr):
    out = subprocess.run([NODE, "-e", f"const C=require({json.dumps(str(ROOT / 'contract.js'))});"
                          f"const CHAIN={json.dumps(CHAIN)};process.stdout.write(JSON.stringify({expr}))"],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node not installed")
class TestContract(unittest.TestCase):

    def test_long_plan_is_a_call_vertical_on_the_first_expiry_after_today(self):
        c = run("C.bracketContract(CHAIN, {side:'long', entry:100.2, stop:99, target:102.3}, '2026-10-30')")
        self.assertEqual(c["kind"], "call vertical")
        self.assertEqual(c["expiry"], "2026-11-06")
        self.assertEqual((c["long"]["strike"], c["short"]["strike"]), (100, 102.5))
        self.assertAlmostEqual(c["debit_ask"], 2.6 - 0.9, places=2)      # long ask − short bid
        self.assertAlmostEqual(c["debit_mid"], 2.3 - 1.0, places=2)
        self.assertEqual(c["width"], 2.5)
        self.assertAlmostEqual(c["rr_ask"], round((2.5 - 1.7) / 1.7, 2))
        self.assertAlmostEqual(c["stock_rr"], round(2.1 / 1.2, 2))

    def test_short_plan_is_the_mirror_put_vertical(self):
        c = run("C.bracketContract(CHAIN, {side:'short', entry:99.9, stop:101, target:97.4}, '2026-10-30')")
        self.assertEqual(c["kind"], "put vertical")
        self.assertEqual((c["long"]["strike"], c["short"]["strike"]), (100, 97.5))
        self.assertAlmostEqual(c["debit_ask"], 2.6 - 1.0, places=2)

    def test_short_leg_is_the_first_strike_beyond_a_tight_target(self):
        c = run("C.bracketContract(CHAIN, {side:'long', entry:100, stop:99, target:100.9}, '2026-10-30')")
        self.assertEqual((c["long"]["strike"], c["short"]["strike"]), (100, 102.5), "102.5 is the first strike at or past 100.9")
        c = run("C.bracketContract(CHAIN, {side:'short', entry:100, stop:101, target:99.4}, '2026-10-30')")
        self.assertEqual((c["long"]["strike"], c["short"]["strike"]), (100, 97.5))

    def test_same_strike_degenerates_to_a_single_leg(self):
        # nothing listed beyond the target: the nearest strike is the entry's own
        c = run("C.bracketContract(CHAIN, {side:'long', entry:129, stop:127, target:131}, '2026-10-30')")
        self.assertTrue(c["same_strike"])
        self.assertIsNone(c["debit_ask"]); self.assertIsNone(c["rr_ask"])
        self.assertEqual(c["long"]["strike"], 130)

    def test_errors_are_named(self):
        self.assertEqual(run("C.bracketContract(CHAIN, null, '2026-10-30')")["error"], "no plan")
        self.assertEqual(run("C.bracketContract([], {side:'long', entry:1, target:2}, '2026-10-30')")["error"], "no chain")
        self.assertEqual(run("C.bracketContract(CHAIN, {side:'long', entry:1, target:2}, '2026-12-31')")["error"], "no expiry after today")

    def test_trim_keeps_near_strikes_on_short_tenors_only(self):
        t = run("C.trimChain(CHAIN, '2026-10-30')")
        self.assertEqual(sorted((r["expiry_date"], r["strike"]) for r in t),
                         [("2026-11-06", 97.5), ("2026-11-06", 100), ("2026-11-06", 102.5)])
        self.assertNotIn("smoothed_vol", t[0])
        # the trimmed chain prices the same vertical
        c = run("C.bracketContract(C.trimChain(CHAIN, '2026-10-30'), {side:'long', entry:100.2, stop:99, target:102.3}, '2026-10-30')")
        self.assertEqual((c["long"]["strike"], c["short"]["strike"]), (100, 102.5))


@unittest.skipUnless(NODE, "node not installed")
class TestFillAndHedge(unittest.TestCase):

    def test_a_wide_market_or_an_empty_strike_is_not_fillable(self):
        f = run("C.fillability(1.0, 1.2, 1.0, 100, 5)")
        self.assertTrue(f["ok"]); self.assertEqual(f["spread"], 0.2)
        f = run("C.fillability(1.0, 1.6, 1.0, 100, 5)")
        self.assertFalse(f["ok"]); self.assertIn("wide", f["why"])
        f = run("C.fillability(1.0, 1.2, 1.0, 0, 0)")
        self.assertFalse(f["ok"]); self.assertIn("no open interest", f["why"])
        self.assertIsNone(run("C.fillability(null, 1.2, 1.0, 1, 1)")["ok"])
        c = run("C.bracketContract(CHAIN, {side:'long', entry:100.2, stop:99, target:102.3}, '2026-10-30')")
        self.assertIn("ok", c["fill"])

    def test_index_hedge_is_beta_weighted_dollars_in_hedge_shares(self):
        hr = {"beta": 1.8912, "correlation": 0.59, "notional_position": 10000, "idio_vol_remaining": 3430}
        h = run(f"C.indexHedge({json.dumps(hr)}, 100, 646.8, 700)")
        self.assertEqual(h["beta"], 1.89)
        self.assertEqual(h["notional"], 64680)
        self.assertEqual(h["hedge_dollars"], round(-1.8912 * 64680))
        self.assertAlmostEqual(h["hedge_shares"], round(-1.8912 * 64680 / 700, 1))
        self.assertEqual(h["idio_share"], 0.343)
        self.assertIsNone(run("C.indexHedge({}, 100, 1, 1)"))


def graded_row(dte, strike, cd, pd, civ=(29.0, 30.0, 31.0), piv=(29.0, 30.0, 31.0), expiry=None):
    """a chain line with bid/mid/ask IV on both sides; cd/pd are the call and put deltas"""
    r = chain_row(expiry or f"2026-11-{dte:02d}", dte, strike)
    r.update({"call_delta": cd, "put_delta": pd, "call_bid_iv": civ[0], "call_mid_iv": civ[1], "call_ask_iv": civ[2],
              "put_bid_iv": piv[0], "put_mid_iv": piv[1], "put_ask_iv": piv[2], "call_vega": 0.1, "put_vega": 0.1})
    return r


def run_on(chain, expr):
    out = subprocess.run([NODE, "-e", f"const C=require({json.dumps(str(ROOT / 'contract.js'))});"
                          f"const CHAIN={json.dumps(chain)};process.stdout.write(JSON.stringify({expr}))"],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node not installed")
class TestWidthGrade(unittest.TestCase):

    def test_tier_cuts_follow_the_post(self):
        got = run("[0, 0.004, 0.0299, 0.03, 0.073, 0.12, 0.139, 0.14, 0.199, 0.20, 0.8, null, -1].map(C.widthTier)")
        self.assertEqual(got, [1, 1, 1, 2, 3, 4, 4, 5, 5, 6, 6, None, None])

    def test_line_width_uses_iv_quotes_else_price_over_vega(self):
        r = graded_row(30, 100, 0.5, -0.5, civ=(28.5, 30.0, 31.5))
        self.assertAlmostEqual(run_on([r], "C.lineWidth(CHAIN[0], 'call')"), 0.1)     # 3 points on 30
        r2 = dict(r); r2.pop("call_bid_iv"); r2.pop("call_ask_iv")
        r2.update({"call_bid_price": 2.0, "call_ask_price": 2.3, "call_vega": 0.1})   # 0.30 / 0.1 = 3 points
        self.assertAlmostEqual(run_on([r2], "C.lineWidth(CHAIN[0], 'call')"), 0.1)
        r3 = dict(r2); r3["call_bid_price"] = 0
        self.assertIsNone(run_on([r3], "C.lineWidth(CHAIN[0], 'call')"), "no bid is no measurable market")

    def test_name_grade_is_the_median_of_the_35_to_50_delta_lines_nearest_30_days(self):
        chain = [graded_row(29, 95, 0.70, -0.30, civ=(20, 30, 40), piv=(20, 30, 40)),   # neither side in the delta band
                 graded_row(29, 100, 0.48, -0.52, civ=(29.7, 30, 30.3)),                 # call in (2%), put out
                 graded_row(29, 105, 0.36, -0.64, civ=(29.1, 30, 30.9)),                 # call in (6%)
                 graded_row(29, 102, 0.55, -0.45, piv=(28.8, 30, 31.2)),                 # put in (8%)
                 graded_row(8, 100, 0.45, -0.45, civ=(10, 30, 50), piv=(10, 30, 50))]     # wrong expiry
        g = run_on(chain, "C.nameWidth(CHAIN, '2026-10-30')")
        self.assertEqual((g["dte"], g["n"]), (29, 3))
        self.assertAlmostEqual(g["share"], 0.06); self.assertEqual(g["tier"], 3)
        self.assertIsNone(run_on([], "C.nameWidth(CHAIN, '2026-10-30')"))

    def test_bracket_carries_both_grades_and_the_overflow_is_not_fillable(self):
        tight = [graded_row(8, 100, 0.5, -0.5, civ=(29.7, 30, 30.3), piv=(29.7, 30, 30.3), expiry="2026-11-06"),
                 graded_row(8, 102.5, 0.3, -0.7, civ=(29.7, 30, 30.3), expiry="2026-11-06")]
        for r, (b, m, a) in zip(tight, [(2.0, 2.05, 2.1), (0.9, 0.95, 1.0)]):
            r.update({"call_bid_price": b, "call_mid_price": m, "call_ask_price": a})
        plan = "{side:'long', entry:100.2, stop:99, target:102.3}"
        c = run_on(tight, f"C.bracketContract(CHAIN, {plan}, '2026-10-30')")
        self.assertAlmostEqual(c["width_grade"]["leg"]["share"], 0.02); self.assertEqual(c["width_grade"]["leg"]["tier"], 1)
        self.assertEqual(c["width_grade"]["name"]["tier"], 1)
        self.assertTrue(c["fill"]["ok"])
        wide = [dict(r, call_bid_iv=24.0, call_ask_iv=36.0) for r in tight]               # 40% of IV
        c = run_on(wide, f"C.bracketContract(CHAIN, {plan}, '2026-10-30')")
        self.assertEqual(c["width_grade"]["leg"]["tier"], 6)
        self.assertFalse(c["fill"]["ok"]); self.assertIn("past Tier 5", c["fill"]["why"])

    def test_old_chains_without_iv_quotes_grade_as_unknown(self):
        c = run("C.bracketContract(CHAIN, {side:'long', entry:100.2, stop:99, target:102.3}, '2026-10-30')")
        self.assertIsNone(c["width_grade"]["leg"]["tier"])
        self.assertIn("ok", c["fill"])

    def test_trim_keeps_the_iv_quotes_and_vega(self):
        kept = run_on([graded_row(8, 100, 0.5, -0.5, expiry="2026-11-06")], "C.trimChain(CHAIN, '2026-10-30')")
        for k in ("call_bid_iv", "call_ask_iv", "call_vega", "put_bid_iv", "put_ask_iv", "put_vega"):
            self.assertIn(k, kept[0])


class TestRemark(unittest.TestCase):

    def test_black_scholes_parity_and_intrinsic(self):
        c, p = run('C.bsPrice("call", 100, 95, 30/365, 0.3)'), run('C.bsPrice("put", 100, 95, 30/365, 0.3)')
        self.assertAlmostEqual(c - p, 100 - 95 * math.exp(-0.04 * 30 / 365), places=6)
        self.assertEqual(run('C.bsPrice("call", 100, 95, 0, 0.3)'), 5)
        self.assertIsNone(run('C.bsPrice("call", 0, 95, 0.1, 0.3)'))

    def test_remark_reprices_the_vertical_at_the_live_spot(self):
        v = {"side": "long", "dte": 8, "width": 5, "debit_mid": 0.4, "debit_ask": 0.5, "long": {"strike": 100, "iv": 30}, "short": {"strike": 105, "iv": 31}}
        low, high = run(f"C.remarkContract({json.dumps(v)}, 100.2, 1)"), run(f"C.remarkContract({json.dumps(v)}, 104, 1)")
        self.assertGreater(high["debit"], low["debit"], "a call vertical is worth more as spot rises")
        self.assertAlmostEqual(low["change"], round(low["debit"] - 0.4, 2))
        self.assertAlmostEqual(low["t_years"], round(7 / 365, 4))
        self.assertAlmostEqual(high["rr"], round((5 - high["debit"]) / high["debit"], 2))
        self.assertIsNone(run(f"C.remarkContract({json.dumps({**v, 'same_strike': True})}, 100, 1)"))
        self.assertIsNone(run(f"C.remarkContract({json.dumps({**v, 'long': {'strike': 100}})}, 100, 1)"), "no IV on a leg")

    def test_bracket_contract_carries_the_short_legs_iv_for_the_remark(self):
        chain = [{"expiry_date": "2026-10-09", "dte": 8, "strike": k, "call_bid_price": 2.6, "call_mid_price": 2.8, "call_ask_price": 3.0,
                  "call_mid_iv": 30 + k / 100, "put_bid_price": 1, "put_mid_price": 1, "put_ask_price": 1} for k in (100, 105)]
        c = run(f'C.bracketContract({json.dumps(chain)}, {{"side": "long", "entry": 100.2, "stop": 99, "target": 104.9}}, "2026-10-01")')
        self.assertEqual(c["short"]["iv"], 31.05)


class TestPage(unittest.TestCase):

    def test_pages_load_and_mirror_contract_js(self):
        for page in ("index.html", "cf-pages/index.html"):
            self.assertIn('<script src="contract.js"></script>', (ROOT / page).read_text(), page)
        self.assertEqual((ROOT / "contract.js").read_bytes(), (ROOT / "cf-pages/contract.js").read_bytes())


if __name__ == "__main__":
    unittest.main()
