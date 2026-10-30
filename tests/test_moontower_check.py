"""
moontower_check.py: the Moontower lens — options-market readings known at decision time, as yes/no lenses on a
replayed trade — and the page's embedded copy of the replayed record.
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import moontower_check as mc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def row(**kw):
    base = {"date": "2026-10-01", "iv_30d": 0.4, "iv_30d_weekly_change": 0.0, "vrp_monthly": 0.05,
            "monthly_return_zscore": 0.0, "rv_30d": 30.0}
    base.update(kw)
    return base


class TestLenses(unittest.TestCase):

    def test_thresholds_are_inclusive_where_the_docs_say(self):
        th = mc.THRESHOLDS
        on = mc.lenses(row(iv_30d_weekly_change=th["iv_rising_weekly_change"], iv_30d=th["iv_high"],
                           vrp_monthly=th["vol_rich_vrp"], monthly_return_zscore=-th["stretched_z"]), 5, {"Buy Vol": 21, "Sell Vol": 20})
        self.assertTrue(on["iv_rising"]); self.assertTrue(on["iv_high"]); self.assertTrue(on["vol_rich"])
        self.assertTrue(on["stretched"]); self.assertFalse(on["vol_cheap"])
        self.assertTrue(on["buy_vol_idea"]); self.assertFalse(on["sell_vol_idea"], "score must exceed the floor")
        off = mc.lenses(row(iv_30d_weekly_change=0.029, iv_30d=0.499, vrp_monthly=-0.01, monthly_return_zscore=1.49), 5, {})
        self.assertFalse(off["iv_rising"]); self.assertFalse(off["iv_high"]); self.assertFalse(off["vol_rich"])
        self.assertTrue(off["vol_cheap"]); self.assertFalse(off["stretched"])

    def test_missing_readings_are_unknown_not_false(self):
        l = mc.lenses(None, None, None)
        self.assertTrue(all(v is None for v in l.values()))
        l = mc.lenses(row(vrp_monthly=None), 3, None)
        self.assertIsNone(l["vol_cheap"]); self.assertIsNone(l["vol_rich"]); self.assertIsNone(l["buy_vol_idea"])
        self.assertTrue(l["iv_high"] is False)

    def test_earnings_clear_is_post_report_only(self):
        # reported today (0) or yesterday (+1): not clear; reports tomorrow (-1) or last week (+6): clear
        self.assertFalse(mc.lenses(row(), 0, {})["earnings_clear"])
        self.assertFalse(mc.lenses(row(), 1, {})["earnings_clear"])
        self.assertTrue(mc.lenses(row(), -1, {})["earnings_clear"])
        self.assertTrue(mc.lenses(row(), 6, {})["earnings_clear"])
        self.assertFalse(mc.lenses(row(), -1, {})["_earnings_clear_pm1"], "the ±1 variant also fails the day before")

    def test_earnings_distance_is_signed_days_from_the_nearest_report(self):
        rec = {"prior_earnings_date": "2026-10-28", "next_earnings_date": "2027-01-27"}
        self.assertEqual(mc.earnings_distance(rec, "2026-10-29"), 1)
        self.assertEqual(mc.earnings_distance(rec, "2026-10-28"), 0)
        self.assertEqual(mc.earnings_distance({"next_earnings_date": "2026-10-30"}, "2026-10-29"), -1)
        self.assertIsNone(mc.earnings_distance(None, "2026-10-29"))
        self.assertIsNone(mc.earnings_distance({"prior_earnings_date": None, "next_earnings_date": None}, "2026-10-29"))


class TestDecisionTime(unittest.TestCase):

    def test_prior_cockpit_is_the_last_row_before_the_session(self):
        rows = {d: row(date=d) for d in ("2026-10-01", "2026-10-02", "2026-10-06")}
        self.assertEqual(mc.prior_cockpit(rows, "2026-10-06")["date"], "2026-10-02")
        self.assertEqual(mc.prior_cockpit(rows, "2026-10-02")["date"], "2026-10-01")
        self.assertIsNone(mc.prior_cockpit(rows, "2026-10-01"))
        self.assertIsNone(mc.prior_cockpit({}, "2026-10-01"))

    def test_ideas_for_a_date_the_api_does_not_have_are_empty_not_the_latest(self):
        with tempfile.TemporaryDirectory() as d:
            c = mc.Cache(Path(d), key=None, pull=False)
            (Path(d) / "ideas_2026-10-11.json").write_text(json.dumps(
                {"summary": {"tradeDate": "2026-10-10"}, "tickers": [{"ticker": "SPY", "idea": "Buy Vol", "totalScore": 50}]}))
            self.assertEqual(c.ideas("2026-10-11"), {}, "a weekend date falls back to Friday's snapshot: refuse it")
            (Path(d) / "ideas_2026-10-10.json").write_text(json.dumps(
                {"summary": {"tradeDate": "2026-10-10"}, "tickers": [{"ticker": "SPY", "idea": "Buy Vol", "totalScore": 50},
                                                                      {"ticker": "SPY", "idea": "Sell Vol", "totalScore": -3}]}))
            self.assertEqual(c.ideas("2026-10-10"), {"SPY": {"Buy Vol": 50.0, "Sell Vol": -3.0}})

    def test_cache_refuses_to_pull_without_the_flag(self):
        with tempfile.TemporaryDirectory() as d:
            c = mc.Cache(Path(d), key=None, pull=False)
            with self.assertRaises(SystemExit):
                c.directory()


class FakeCache:
    """Two names, five sessions; AAA is on Moontower, ZZZ is not."""

    def directory(self):
        return {"AAA": {"ticker": "AAA"}}

    def cockpit(self, tickers, start, end):
        out = []
        for d in ("2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"):
            out.append({"ticker": "AAA", "date": d, "iv_30d": 0.6 if d < "2026-10-06" else 0.3,
                        "iv_30d_weekly_change": 0.0, "vrp_monthly": 0.2, "monthly_return_zscore": 0.0, "rv_30d": 30.0})
        return out

    def earnings(self, tickers):
        return {"AAA": {"prior_earnings_date": "2026-10-07", "next_earnings_date": None}}

    def ideas(self, trade_date):
        if trade_date >= "2026-10-06":
            return {"AAA": {"Buy Vol": 30.0, "Sell Vol": 0.0}}
        if trade_date == "2026-10-05":
            return {"AAA": {"Buy Vol": 5.0, "Sell Vol": 0.0}}     # listed, but below the score floor
        return {}                                                # no snapshot for that date


def trade(session, ticker, r_net, grade="A", mode="mdrev"):
    return {"model": "classic", "session": session, "ticker": ticker, "setup_mode": mode, "side": "long",
            "grade": grade, "r_net": r_net}


class TestRun(unittest.TestCase):

    def setUp(self):
        self.sessions = ["2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
        tr = [trade(s, "AAA", 1.0 if i % 2 else -0.5) for i, s in enumerate(self.sessions)]
        tr += [trade(s, "ZZZ", 2.0) for s in self.sessions]                    # uncovered: never counted
        tr += [trade(s, "AAA", -1.0, grade="B", mode="gap") for s in self.sessions]
        tr += [{**trade(s, "AAA", 9.0), "model": "classic_after_hours"} for s in self.sessions]   # other model: ignored
        self.res = mc.run({"engine_version": "1.7.0", "method": {}, "trades": tr}, FakeCache())

    def test_only_classic_trades_on_covered_names_count(self):
        self.assertEqual(self.res["trades"], 15)
        self.assertEqual(self.res["covered"], 10)
        self.assertEqual(self.res["names_covered"], 1)
        self.assertEqual(self.res["grades"]["A"]["all"]["n"], 5)
        self.assertEqual(self.res["grades"]["A+B"]["all"]["n"], 10)

    def test_pass_fail_unknown_partition_every_trade(self):
        for block in self.res["grades"].values():
            n = block["all"]["n"]
            for lens, e in block["lenses"].items():
                self.assertEqual(e["pass"]["n"] + e["fail"]["n"] + e["unknown"], n, lens)

    def test_readings_are_the_prior_close_not_the_session(self):
        # iv_30d drops to 0.3 from Oct 6; the Oct 6 session still sees Oct 5's 0.6
        e = self.res["grades"]["A"]["lenses"]["iv_high"]
        self.assertEqual(e["pass"]["n"], 3, "Oct 2, 5, 6 read a prior close at 0.6")
        self.assertEqual(e["fail"]["n"], 2)

    def test_ideas_come_from_the_prior_sessions_snapshot(self):
        # the Buy Vol score clears the floor from the Oct 6 snapshot, so the Oct 7 and Oct 8 sessions pass;
        # Oct 6 reads Oct 5's low score and fails; Oct 2 and Oct 5 had no prior snapshot at all
        e = self.res["grades"]["A"]["lenses"]["buy_vol_idea"]
        self.assertEqual(e["pass"]["n"], 2)
        self.assertEqual(e["fail"]["n"], 1)
        self.assertEqual(e["unknown"], 2, "sessions whose prior snapshot had no rows are unknown, not fail")

    def test_earnings_clear_fails_on_the_report_day_and_the_day_after(self):
        e = self.res["grades"]["A"]["lenses"]["earnings_clear"]
        self.assertEqual(e["fail"]["n"], 2)      # Oct 7 (reports) and Oct 8 (day after)
        self.assertEqual(e["pass"]["n"], 3)

    def test_compact_keeps_what_the_page_shows(self):
        c = mc.compact(self.res)
        self.assertEqual(set(c), {"window", "sessions", "covered", "trades", "thresholds", "lens_text", "grades"})
        for block in c["grades"].values():
            for lens in mc.LENSES:
                self.assertEqual(set(block["lenses"][lens]), {"pass", "fail", "delta"})
                self.assertEqual(set(block["lenses"][lens]["pass"]), {"n", "mean", "se", "t", "verdict"})

    def test_embed_replaces_the_pages_constant(self):
        with tempfile.TemporaryDirectory() as d:
            h = Path(d) / "x.html"
            h.write_text("<script>\nconst MOONTOWER = {\"old\":1};\nconst X = 1;\n</script>")
            mc.embed(h, self.res)
            self.assertIn('const MOONTOWER = {"window"', h.read_text())
            self.assertIn("const X = 1;", h.read_text())
            h.write_text("<script>const Y = 2;</script>")
            with self.assertRaises(SystemExit):
                mc.embed(h, self.res)


class TestPage(unittest.TestCase):
    """The page carries the written check, its thresholds and its lens list, and both copies agree."""

    def page(self, name="index.html"):
        return (ROOT / name).read_text()

    def constant(self, html):
        m = re.search(r"const MOONTOWER = (\{.*?\});\n", html, re.S)
        self.assertIsNotNone(m, "the page embeds `const MOONTOWER = {...};`")
        return json.loads(m.group(1))

    def test_embedded_record_is_the_written_check(self):
        files = sorted((ROOT / "research").glob("moontower_check_*.json"))
        self.assertTrue(files, "research/moontower_check_<date>.json")
        res = json.loads(files[-1].read_text())
        self.assertEqual(self.constant(self.page()), mc.compact(res))
        self.assertEqual(self.constant(self.page("cf-pages/index.html")), mc.compact(res))

    def test_page_thresholds_and_lenses_are_the_scripts(self):
        c = self.constant(self.page())
        self.assertEqual(c["thresholds"], mc.THRESHOLDS)
        html = self.page()
        options = re.findall(r'<select id="mtw"[^>]*>(.*?)</select>', html, re.S)
        self.assertEqual(len(options), 1)
        values = re.findall(r'<option value="([^"]*)"', options[0])
        self.assertEqual(values[:2], ["", "show"], "off and readings-only come first")
        self.assertEqual(values[2:], mc.LENSES)
        for lens in mc.LENSES:
            self.assertIn(f'"{lens}"', html, f"the page's lens function handles {lens}")


if __name__ == "__main__":
    unittest.main()
