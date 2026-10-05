"""
While live is on, the live loop is the desk's only scanner.

Startup used to run two full scans at once: _boot_live_party started the loop, whose first pass
scans straight away, and then ran a scan of its own. POST /api/live did the same when it started
the loop. Two concurrent scans fetch everything twice, and two yfinance bulk downloads at once can
corrupt each other's results. run_scan is replaced by a recorder here; nothing is fetched.
"""

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app                       # noqa: E402

LOOP = "vwap-blue-live"


class TestLiveLoop(unittest.TestCase):

    def setUp(self):
        self.scans = []
        self.scanned = threading.Semaphore(0)

        def run_scan(*a, **k):
            self.scans.append((threading.current_thread().name, a, k))
            self.scanned.release()
            return {"results": [], "meta": {}}

        self.saved_cfg = dict(app._live_cfg)
        self.patches = [mock.patch.object(app, "run_scan", run_scan),
                        mock.patch.object(app, "_maybe_auto_resolve_ledger", lambda *a, **k: None),
                        mock.patch.dict(os.environ, {"VWAP_BLUE_LIVE": "1", "VWAP_BLUE_LIVE_SEC": "300"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        with app._live_lock:
            app._live_cfg["enabled"] = False
        if hasattr(app, "_live_wake"):
            app._live_wake.set()
        if app._live_thread is not None:
            app._live_thread.join(5)
        for p in self.patches:
            p.stop()
        app._live_cfg.clear()
        app._live_cfg.update(self.saved_cfg)

    def wait_for_scans(self, n):
        for _ in range(n):
            self.assertTrue(self.scanned.acquire(timeout=5), "expected a scan")
        time.sleep(0.3)                    # a duplicate would have started by now (the interval is 300 s)
        return self.scans

    def test_startup_runs_one_scan(self):
        app._boot_live_party()
        scans = self.wait_for_scans(1)
        self.assertEqual([s[0] for s in scans], [LOOP])

    def test_turning_live_on_runs_one_scan(self):
        app.live_set(app.LiveBody(enabled=True, interval_sec=300, max=5))
        scans = self.wait_for_scans(1)
        self.assertEqual([s[0] for s in scans], [LOOP])

    def test_new_settings_rescan_in_the_loop_right_away(self):
        app._boot_live_party()
        self.wait_for_scans(1)
        app.live_set(app.LiveBody(enabled=True, interval_sec=300, max=5, grade_min="B"))
        scans = self.wait_for_scans(1)
        self.assertEqual([s[0] for s in scans], [LOOP, LOOP])
        self.assertEqual((scans[1][1][1], scans[1][2]["grade_min"]), (5, "B"))     # max_n, grade floor

    def test_turning_live_off_stops_the_loop(self):
        app._boot_live_party()
        self.wait_for_scans(1)
        app.live_set(app.LiveBody(enabled=False))
        app._live_thread.join(5)
        self.assertFalse(app._live_thread.is_alive())
        self.assertEqual(len(self.scans), 1)


if __name__ == "__main__":
    unittest.main()
