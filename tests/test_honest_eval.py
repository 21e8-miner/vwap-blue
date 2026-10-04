"""
Tests for the honest-evaluation additions (run: python3 -m unittest discover tests).

  * honest.py: cost in R, session-clustered standard errors, verdicts, segments
  * replay_sessions: next-bar-open fills (with slippage, gap-through skips); trigger_close unchanged
  * engine: stale-bar guard (live only, market open only)
  * ledger: record (dedup, live-actionable triggers only), resolve with the replay simulator, report
No network: the ledger's fetch is injected.
"""

import math
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ledger                                                    # noqa: E402
from engine import apply_stale_guard, bar_age_min                # noqa: E402
from honest import clustered, cost_r, net_r, segments, verdict   # noqa: E402
from replay_sessions import MODELS, _fill_entry, _simulate       # noqa: E402

ET = ZoneInfo("America/New_York")
CLASSIC = [m for m in MODELS if m.name == "classic"][0]


def bar(o, h, l, c, ts=0):
    return {"o": o, "h": h, "l": l, "c": c, "ts": ts, "v": 1000.0}


class TestHonest(unittest.TestCase):

    def test_cost_in_r(self):
        # 0.08% round trip on a 1% stop is 0.08R; on a 0.25% stop it is 0.32R
        self.assertAlmostEqual(cost_r("AAPL", 100.0, 99.0), 0.08)
        self.assertAlmostEqual(cost_r("AAPL", 100.0, 99.75), 0.32)
        self.assertAlmostEqual(cost_r("BTC-USD", 100.0, 99.0), 0.15)
        self.assertAlmostEqual(net_r(1.0, "AAPL", 100.0, 99.0), 0.92)

    def test_clustered_se_is_wider_when_sessions_move_together(self):
        # 10 sessions x 10 trades; within a session all trades share the session's outcome
        vals, cl = [], []
        for s in range(10):
            v = 1.0 if s % 2 == 0 else -0.6
            vals += [v] * 10
            cl += [f"d{s}"] * 10
        stat = clustered(vals, cl)
        naive = (sum((x - stat["mean"]) ** 2 for x in vals) / (len(vals) - 1)) ** 0.5 / math.sqrt(len(vals))
        self.assertAlmostEqual(stat["mean"], 0.2)
        self.assertGreater(stat["se"], 2.5 * naive)            # ~sqrt(10) wider: 10 draws, not 100
        self.assertEqual(stat["clusters"], 10)
        self.assertEqual(verdict(stat), "inconclusive")
        self.assertIsNone(clustered([1.0, 2.0], ["a", "a"])["se"])
        self.assertEqual(verdict(clustered([1.0, 2.0], ["a", "a"])), "insufficient")

    def test_verdict_and_segments(self):
        strong = clustered([1.0 + 0.01 * i for i in range(40)], [f"d{i % 20}" for i in range(40)])
        self.assertEqual(verdict(strong), "positive")
        rows = [{"setup_mode": "gap", "regime": "chop", "r_net": 0.5, "session": f"d{i}"} for i in range(6)]
        rows += [{"setup_mode": "mdrev", "regime": "chop", "r_net": -0.2, "session": "d1"}]
        segs = segments(rows, ("setup_mode", "regime"), min_n=5)
        self.assertEqual([(s["setup_mode"], s["regime"]) for s in segs], [("gap", "chop")])


class TestEntryFill(unittest.TestCase):

    def setUp(self):
        self.day = [bar(100, 100.2, 99.8, 100.0, 0), bar(100.3, 100.6, 100.1, 100.5, 1), bar(100.5, 102.5, 100.4, 102.2, 2)]

    def test_trigger_close_is_the_engine_entry(self):
        self.assertEqual(_fill_entry("long", 100.0, 99.0, 102.0, self.day, 0, "trigger_close"), (100.0, "filled"))

    def test_next_open_pays_the_next_open_plus_slippage(self):
        px, why = _fill_entry("long", 100.0, 99.0, 102.0, self.day, 0, "next_open", slip_bps=2.0)
        self.assertEqual(why, "filled")
        self.assertAlmostEqual(px, 100.3 * 1.0002)
        short_px, _ = _fill_entry("short", 100.0, 101.0, 98.0, self.day, 0, "next_open", slip_bps=2.0)
        self.assertAlmostEqual(short_px, 100.3 * 0.9998)

    def test_gap_through_and_missing_bar_are_skipped(self):
        self.assertEqual(_fill_entry("long", 100.0, 100.4, 102.0, self.day, 0, "next_open")[1], "gapped_through_stop")
        self.assertEqual(_fill_entry("long", 100.0, 99.0, 100.2, self.day, 0, "next_open")[1], "gapped_through_target")
        self.assertEqual(_fill_entry("long", 100.0, 99.0, 102.0, self.day, 2, "next_open")[1], "no_next_bar")

    def test_worse_fill_lowers_r(self):
        tc = _simulate("long", 100.0, 99.0, 102.0, self.day, 0, CLASSIC, "AAPL")
        px, _ = _fill_entry("long", 100.0, 99.0, 102.0, self.day, 0, "next_open")
        no = _simulate("long", px, 99.0, 102.0, self.day, 0, CLASSIC, "AAPL")
        self.assertEqual(tc[1], "target")
        self.assertEqual(no[1], "target")
        self.assertLess(no[2], tc[2])


class TestStaleGuard(unittest.TestCase):

    def test_bar_age(self):
        idx = pd.DatetimeIndex([pd.Timestamp("2026-09-15 14:00", tz="UTC")])
        df = pd.DataFrame({"Close": [1.0]}, index=idx)
        now = pd.Timestamp("2026-09-15 14:20", tz="UTC").timestamp()
        self.assertAlmostEqual(bar_age_min(df, now), 20.0)
        naive = pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex([pd.Timestamp("2026-09-15 14:00")]))
        self.assertAlmostEqual(bar_age_min(naive, now), 20.0)                 # naive = UTC
        self.assertIsNone(bar_age_min(None))

    def test_trigger_on_stale_bars_is_demoted_only_while_open(self):
        row = {"signal": "TRIGGER", "live_actionable": True, "session_label": "rth", "note": "CONFIRMED ▲", "edge": 70}
        self.assertTrue(apply_stale_guard(row, 22.0, 15.0))
        self.assertEqual((row["signal"], row["live_actionable"]), ("WATCH", False))
        self.assertIn("STALE BARS 22m", row["note"])
        self.assertEqual(row["edge"], 55)
        fresh = {"signal": "TRIGGER", "live_actionable": True, "session_label": "rth"}
        self.assertFalse(apply_stale_guard(fresh, 4.0, 15.0))
        self.assertEqual(fresh["signal"], "TRIGGER")
        pre = {"signal": "TRIGGER", "live_actionable": True, "session_label": "premarket"}
        self.assertFalse(apply_stale_guard(pre, 60.0, 15.0))                  # thin premarket tape is exempt


def session_df(day="2026-09-15", path=None):
    """5m bars 09:30-16:00 ET; price path given as closes (open = previous close)."""
    times = pd.date_range(f"{day} 09:30", f"{day} 15:55", freq="5min", tz=ET)
    closes = path or [100.0] * len(times)
    closes = (closes + [closes[-1]] * len(times))[:len(times)]
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({"Open": opens, "High": [max(o, c) + 0.05 for o, c in zip(opens, closes)],
                         "Low": [min(o, c) - 0.05 for o, c in zip(opens, closes)], "Close": closes,
                         "Volume": [10_000] * len(times)}, index=times)


class TestLedger(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        ledger._seen.clear()
        path = [100.0] * 10 + [100.2, 100.6, 101.0, 101.5, 102.2] + [102.0] * 70
        self.df = session_df(path=path)
        from engine import _prep_bars
        self.bars = _prep_bars(self.df)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _row(self, trig=9, **kw):
        row = {"ticker": "AAPL", "signal": "TRIGGER", "live_actionable": True, "focus_day": "2026-09-15",
               "side": "long", "entry": 100.0, "stop": 99.0, "target": 102.0, "rr": 2.0, "grade": "A", "regime": "chop",
               "setup_mode": "gap", "edge": 72, "_chart": {"bars": self.bars, "markers": {"trig": trig}}}
        row.update(kw)
        return row

    def test_record_dedups_and_ignores_non_live(self):
        n1 = ledger.record({"AAPL": self._row(), "MSFT": self._row(live_actionable=False)}, "t", base=self.dir)
        n2 = ledger.record({"AAPL": self._row()}, "t", base=self.dir)
        n3 = ledger.record({"AAPL": self._row(signal="WATCH")}, "t", base=self.dir)
        self.assertEqual((n1, n2, n3), (1, 0, 0))
        sig = ledger._read(self.dir / "signals.jsonl")
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig[0]["grade"], "A")
        self.assertNotIn("_chart", sig[0])

    def test_resolve_and_report(self):
        ledger.record({"AAPL": self._row()}, "t", base=self.dir)
        before_close = datetime(2026, 9, 15, 15, 0, tzinfo=ET)
        self.assertEqual(ledger.resolve(before_close, fetch=lambda *a, **k: ({"AAPL": self.df},), base=self.dir)["pending_closed"], 0)
        after = datetime(2026, 9, 16, 9, 0, tzinfo=ET)
        counts = ledger.resolve(after, fetch=lambda *a, **k: ({"AAPL": self.df},), base=self.dir)
        self.assertEqual(counts["resolved"], 1)
        outs = [o for o in ledger._read(self.dir / "outcomes.jsonl") if o["status"] == "resolved"]
        self.assertEqual(len(outs), len(ledger.ENTRY_MODES) * len(ledger.RESOLVE_MODELS))
        nxt = [o for o in outs if o["entry_mode"] == "next_open" and o["model"] == "classic"][0]
        self.assertEqual(nxt["exit_reason"], "target")
        self.assertGreater(nxt["entry_fill"], 100.0)
        self.assertLess(nxt["r_net"], nxt["r"])                               # costs come off
        rep = ledger.report(base=self.dir)
        self.assertEqual((rep["signals"], rep["resolved"], rep["pending"]), (1, 1, 0))
        self.assertAlmostEqual(rep["net_r"]["mean"], nxt["r_net"])
        self.assertEqual(ledger.resolve(after, fetch=lambda *a, **k: ({"AAPL": self.df},), base=self.dir)["pending_closed"], 0)

    def test_session_outside_window_is_unresolvable(self):
        ledger.record({"AAPL": self._row(focus_day="2026-08-01")}, "t", base=self.dir)
        counts = ledger.resolve(datetime(2026, 9, 16, 9, 0, tzinfo=ET), fetch=lambda *a, **k: ({"AAPL": self.df},), base=self.dir)
        self.assertEqual(counts["unresolvable"], 1)

    def test_app_auto_resolve_after_close(self):
        import unittest.mock as mock
        import app as desk_app

        with mock.patch.object(desk_app.ledger, "resolve", return_value={"pending_closed": 1, "resolved": 1}) as mock_res:
            desk_app._last_auto_resolve_session = None
            desk_app._last_auto_resolve_check = 0.0

            # Midday: runs once on boot / initial check
            midday = datetime(2026, 9, 15, 14, 0, tzinfo=ET)
            res1 = desk_app._maybe_auto_resolve_ledger(midday)
            self.assertEqual(res1, {"pending_closed": 1, "resolved": 1})
            self.assertEqual(mock_res.call_count, 1)

            # Within throttle window: throttles
            res2 = desk_app._maybe_auto_resolve_ledger(midday)
            self.assertIsNone(res2)
            self.assertEqual(mock_res.call_count, 1)

            # Post close: triggers because today's post-close resolve hasn't run yet
            post_close = datetime(2026, 9, 15, 16, 30, tzinfo=ET)
            res3 = desk_app._maybe_auto_resolve_ledger(post_close)
            self.assertEqual(res3, {"pending_closed": 1, "resolved": 1})
            self.assertEqual(mock_res.call_count, 2)
            self.assertEqual(desk_app._last_auto_resolve_session, "2026-09-15")

            # Subsequent check after close today throttles
            res4 = desk_app._maybe_auto_resolve_ledger(post_close)
            self.assertIsNone(res4)
            self.assertEqual(mock_res.call_count, 2)


if __name__ == "__main__":
    unittest.main()

