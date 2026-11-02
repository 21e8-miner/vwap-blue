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
