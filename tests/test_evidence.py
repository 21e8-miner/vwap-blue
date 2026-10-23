"""
evidence.py: the Record column's table is the banner's replays cut by class, and the classes v1.7.0 capped to B
come from the paired pre-cap replay, labelled with the grade they had then; app.py serves the written table.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app                       # noqa: E402
import evidence                  # noqa: E402
from engine import ENGINE_VERSION  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def trade(session, ticker, mode, side, r_net, exit_reason="stop", mirror=None, trigger="09:35"):
    t = {"model": "classic", "session": session, "ticker": ticker, "trigger_time": f"{session[5:]} {trigger}",
         "setup_mode": mode, "side": side, "r_net": r_net, "exit_reason": exit_reason}
    if mirror is not None:
        t["mirror_r_net"] = mirror
    return t


def replay(trades, version=ENGINE_VERSION, method=None):
    return {"engine_version": version, "method": method or {}, "trades": trades}


class TestSegments(unittest.TestCase):

    def test_classes_are_mode_by_side_plus_any_with_direction_over_mirror(self):
        tr = [trade(f"2026-10-{d:02d}", "AAPL", "mdrev", "long", 1.0, "target", mirror=-0.5) for d in range(1, 11)] + \
             [trade(f"2026-10-{d:02d}", "MSFT", "mdrev", "short", -1.0, mirror=0.5) for d in range(1, 6)]
        seg = evidence.segments(replay(tr), "equity")
        self.assertEqual(set(seg), {"equity|mdrev|all", "equity|mdrev|long", "equity|mdrev|short",
                                    "equity|any|all", "equity|any|long", "equity|any|short"})
        longs = seg["equity|mdrev|long"]
        self.assertEqual((longs["n"], longs["sessions"], longs["as_graded"], longs["target"], longs["stopped"]), (10, 10, "A", 10, 0))
        self.assertAlmostEqual(longs["net"]["mean"], 1.0)
        self.assertAlmostEqual(longs["direction"]["mean"], 1.5)
        self.assertEqual(seg["equity|mdrev|all"]["n"], 15)
        self.assertEqual(seg["equity|any|all"], seg["equity|mdrev|all"])

    def test_a_replay_without_mirrors_has_no_direction(self):
        seg = evidence.segments(replay([trade("2026-10-01", "AAPL", "gap", "long", 0.2)]), "equity")
        self.assertIsNone(seg["equity|gap|long"]["direction"])
        self.assertIsNone(seg["equity|gap|long"]["mirror"])

    def test_capped_classes_are_the_paired_replays_trades_the_cap_dropped(self):
        kept = [trade(f"2026-10-{d:02d}", "AAPL", "mdrev", "long", 0.5, mirror=0.0) for d in range(1, 4)]
        fades = [trade(f"2026-10-{d:02d}", "NVDA", "gap", "short", -0.4) for d in range(1, 4)] + \
                [trade("2026-10-01", "META", "both", "long", -0.2)]
        now = replay(kept, method={evidence.PAIRED_KEY: {"classic_overlap": {"kept": 3, "added": 0, "dropped": 4}}})
        old = replay([dict(t, mirror_r_net=None) for t in kept] + fades, version=evidence.PAIRED_VERSION)
        seg = evidence.capped_segments(now, old, "equity")
        self.assertEqual(set(seg), {"equity|gap|all", "equity|gap|short", "equity|both|all", "equity|both|long"})
        self.assertEqual(seg["equity|gap|short"]["as_graded"], evidence.AS_GRADED_CAPPED)
        self.assertEqual(seg["equity|gap|short"]["n"], 3)
        self.assertIsNone(seg["equity|gap|short"]["direction"])
        self.assertNotIn("equity|mdrev|all", seg)            # the kept reclaims are the current replay's to report

    def test_a_stale_pairing_fails_loudly(self):
        kept = [trade("2026-10-01", "AAPL", "mdrev", "long", 0.5)]
        now = replay(kept, method={evidence.PAIRED_KEY: {"classic_overlap": {"kept": 1, "added": 0, "dropped": 9}}})
        old = replay(kept + [trade("2026-10-02", "NVDA", "gap", "short", -0.4)], version=evidence.PAIRED_VERSION)
        with self.assertRaises(SystemExit):
            evidence.capped_segments(now, old, "equity")

    def test_paired_file_is_the_same_bars_replay_of_the_pre_cap_engine(self):
        path = f"research/replay_2026-10-19_v{ENGINE_VERSION}.json"
        self.assertEqual(evidence.paired_file(path, {"method": {evidence.PAIRED_KEY: {}}}),
                         f"research/replay_2026-10-19_v{evidence.PAIRED_VERSION}.json")
        self.assertIsNone(evidence.paired_file(path, {"method": {}}))          # not run paired
        self.assertIsNone(evidence.paired_file("research/nothing_here_v1.7.0.json", {"method": {evidence.PAIRED_KEY: {}}}))


class TestWrittenTable(unittest.TestCase):
    """The committed table is what the banner's files give, and the capped fades are in it."""

    @classmethod
    def setUpClass(cls):
        cls.table = json.loads((ROOT / "research" / f"evidence_v{ENGINE_VERSION}.json").read_text())

    def test_the_equity_fades_are_there_as_capped_classes(self):
        seg = self.table["segments"]
        for k in ("equity|gap|all", "equity|gap|long", "equity|gap|short", "equity|both|all"):
            self.assertEqual(seg[k]["as_graded"], evidence.AS_GRADED_CAPPED, k)
            self.assertIsNone(seg[k]["direction"], k)
        self.assertEqual(seg["equity|gap|all"]["n"] + seg["equity|both|all"]["n"], self.table["capped"]["equity"]["n"])
        self.assertEqual(seg["equity|mdrev|all"]["as_graded"], "A")
        self.assertNotIn("crypto|gap|all", seg)               # the crypto year had no grade-A fades to cap
        self.assertIn("paired", self.table["sources"]["equity"])

    def test_the_desk_serves_the_written_table(self):
        self.assertEqual(app.evidence(), self.table)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(app, "ROOT", Path(d)):
            self.assertEqual(app.evidence(), {})


if __name__ == "__main__":
    unittest.main()
