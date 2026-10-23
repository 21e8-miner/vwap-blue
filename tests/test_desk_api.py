"""
Desk API behaviour the page depends on (app.py), with the data layer faked so nothing is fetched:
  * a manual /api/scan while the live loop runs returns its rows but does not replace /api/last
  * feed errors survive the grade and actionable filters (they grade "–")
  * a forced /api/chart applies the stale-bar guard and does not overwrite the scan's row
  * turning live off then on again while the old loop is stopping still leaves a loop running
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app                       # noqa: E402


def frame():
    idx = pd.date_range("2026-10-21 13:30", periods=80, freq="5min", tz="UTC")
    return pd.DataFrame({"Open": 10.0, "High": 10.1, "Low": 9.9, "Close": 10.0, "Volume": 5e5}, index=idx)


def fake_fetch(tickers, **_):
    bars = {t: (None if t.startswith("BAD") else frame()) for t in tickers}
    return bars, {}, {t: "fake" for t in tickers}, {}, {}


def fake_analyze(t, df, *a, **k):
    if df is None:
        return {"ticker": t, "error": "no bars", "grade": "–", "signal": "FLAT", "edge": 0}
    return {"ticker": t, "grade": "C", "signal": "SETUP", "edge": 10, "actionable": False,
            "live_actionable": False, "session_label": "rth", "_chart": {"bars": [], "markers": {}}}


class TestDeskApi(unittest.TestCase):

    def setUp(self):
        self.saved_cfg = dict(app._live_cfg)
        self.saved_last = dict(app._last)
        self.patches = [
            mock.patch.object(app, "batch_fetch", fake_fetch),
            mock.patch.object(app, "analyze", fake_analyze),
            mock.patch.object(app, "_apply_one_conflict", lambda *a, **k: None),
            mock.patch.object(app, "LEDGER_ON", False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        app._live_cfg.clear()
        app._live_cfg.update(self.saved_cfg)
        app._last.clear()
        app._last.update(self.saved_last)

    def test_a_manual_scan_while_live_does_not_replace_the_loops_results(self):
        app._last.update({"ts": 1.0, "rows": ["loop rows"], "meta": {"live": True}, "by_ticker": {}})
        app._live_cfg["enabled"] = True
        out = app.scan_post(app.ScanBody(tickers=["AAA", "BBB"], grade_min="C"))
        self.assertEqual(len(out["results"]), 2)
        self.assertTrue(out["meta"]["one_off"])
        self.assertFalse(out["meta"]["live"])
        self.assertEqual(app._last["rows"], ["loop rows"])
        app._live_cfg["enabled"] = False
        app.scan_post(app.ScanBody(tickers=["AAA"], grade_min="C"))
        self.assertNotEqual(app._last["rows"], ["loop rows"])        # with live off a scan still publishes

    def test_feed_errors_survive_the_grade_and_actionable_filters(self):
        out = app.run_scan(["AAA", "BAD1"], 16, actionable_only=True, grade_min="A", publish=False)
        self.assertEqual([r["ticker"] for r in out["results"]], ["BAD1"])
        self.assertEqual(out["meta"]["errors"], 1)

    def test_a_forced_chart_is_guarded_and_not_written_back(self):
        app._last.update({"ts": time.time(), "rows": [], "meta": {}, "by_ticker": {"AAA": {"ticker": "AAA", "scan": True}}})
        guard = mock.Mock()
        with mock.patch.object(app, "apply_stale_guard", guard), \
             mock.patch.object(app, "build_chart_from_row", lambda row: {"ticker": row["ticker"]}):
            app.chart("AAA", force=True)
        guard.assert_called_once()
        self.assertEqual(app._last["by_ticker"]["AAA"], {"ticker": "AAA", "scan": True})

    def test_off_then_on_while_the_loop_is_stopping_leaves_a_loop_running(self):
        stopped_deciding = threading.Event()
        go_on = threading.Event()
        real_loop = app._live_loop

        def slow_exit_loop():
            # stop as the real loop would, then linger as a thread that is still alive but done
            with app._live_lock:
                app._live_running = False
            stopped_deciding.set()
            go_on.wait(2)

        with mock.patch.object(app, "_live_loop", slow_exit_loop):
            with app._live_lock:
                app._live_running = False
            app._ensure_live_thread()                 # an old loop, now stopping
            stopped_deciding.wait(2)
            old = app._live_thread
            self.assertTrue(old.is_alive())
            started = []
            with mock.patch.object(app, "_live_loop", lambda: started.append(1)):
                app._ensure_live_thread()             # live switched back on
                app._live_thread.join(2)
            go_on.set()
            old.join(2)
        self.assertEqual(started, [1], "a new loop must start when the old one has decided to stop")
        self.assertIs(app._live_loop, real_loop)


if __name__ == "__main__":
    unittest.main()
