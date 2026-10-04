"""
engine.js (the engine the GitHub Pages demo runs in the browser) must grade bars exactly as
engine.py does: the replay and the forward ledger evaluate engine.py, and the demo's results
warning only applies if the demo shows the same signals.

Synthetic sessions (gap fades, trend days, chop, multi-day extensions and reclaims, partial live
sessions, DST weeks, crypto, gaps and nulls in the feed, too-short histories) go through both
engines with the same clock. Every row field is compared: categorical fields exactly, prices and
ratios to their display rounding. Skipped when node is not installed.
"""

import json
import random
import shutil
import subprocess
import sys
import unittest
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import _grade_ok                                                        # noqa: E402
from data import rotation_score, session_dollar_volume                           # noqa: E402
from engine import ENGINE_VERSION, _geom_ok, analyze, apply_stale_guard, bar_age_min  # noqa: E402

ET = ZoneInfo("America/New_York")
NODE = shutil.which("node")
RUNNER = Path(__file__).resolve().parent / "engine_parity_runner.js"
MAX_AGE_MIN = 15.0
MIN_DVOL = 2_000_000.0           # the desk's default session $ volume floor
GRADES = ("A", "LA", "B", "LB", "C", "✕", "–", "", None)
FLOORS = ("A", "B", "C", "a", "–", "")
# plan geometry incl. exact ties, which real VWAP levels almost never produce
GEOMETRY = [(side, e, s, t) for side in ("long", "short", "flat") for e in (99.0, 100.0, 101.0)
            for s in (99.0, 100.0, 101.0) for t in (99.0, 100.0, 101.0)] + [
            ("long", None, 99.0, 101.0), ("short", 100.0, None, 99.0), ("long", 0.0, -1.0, 1.0)]
N_SERIES = 200
SEED = 20261004

# Mon 2 Mar and Mon 26 Oct 2026 weeks contain the DST switches (8 Mar, 1 Nov).
STARTS = (date(2026, 3, 2), date(2026, 10, 26), date(2026, 6, 8), date(2026, 9, 14))
STYLES = ("fade", "fade", "trend", "chop", "reclaim", "reclaim", "drift")
EXACT = ("error", "signal", "grade", "state", "state_cls", "note", "side", "dir", "setup_mode", "md_side",
         "regime", "late", "gap_provisional", "no_runway", "bad_geom", "trend_block", "regime_block",
         "thin_rvol", "actionable", "live_actionable", "session_label", "focus_day", "prior_day",
         "stale_bars", "fakes", "rvol_n", "session_n", "md_ext_run", "edge", "markers")
PRICES = ("price", "blue", "orange", "prior_close", "entry", "stop", "target")     # 4 dp under $10, else 2
DECIMALS = {"sigma": 6, "dist_std": 2, "d_blue_pct": 3, "to_orange_pct": 3, "gap_pct": 3, "rr": 2,
            "risk_pct": 3, "runway_pct": 3, "cost_r": 3, "rvol": 3, "md_max_dist": 3, "ker": 3,
            "adapt_mult": 3, "bar_age_min": 1}


def _days(start: date, n: int, crypto: bool):
    out, d = [], start
    while len(out) < n:
        if crypto or d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bar_times(days, crypto: bool):
    """Bar open times in UTC: equities 04:00-19:55 ET each day; crypto continuous through the DST switch."""
    if crypto:
        t = datetime.combine(days[0], dtime(0, 0), tzinfo=ET).astimezone(timezone.utc)
        end = datetime.combine(days[-1] + timedelta(days=1), dtime(0, 0), tzinfo=ET).astimezone(timezone.utc)
        out = []
        while t < end:
            out.append(t)
            t += timedelta(minutes=5)
        return out
    return [datetime.combine(d, dtime(m // 60, m % 60), tzinfo=ET).astimezone(timezone.utc)
            for d in days for m in range(4 * 60, 20 * 60, 5)]


def make_series(rng: random.Random, k: int):
    crypto = rng.random() < 0.22
    ticker = f"SYN{k}-USD" if crypto else f"SYN{k}"
    n_days = 1 if k % 40 == 1 else rng.randint(3, 9)              # k % 40 == 1: single session
    days = _days(rng.choice(STARTS) + timedelta(days=rng.randint(0, 3)), n_days, crypto)
    times = _bar_times(days, crypto)
    if k % 40 == 0:
        times = times[:15]                                         # too short for the engine
    px = rng.uniform(0.4, 400) if not crypto else rng.choice((0.08, 2.5, 140.0, 2600.0, 61000.0))
    tick = 0.0001 if px < 1 else 0.01
    vbase = 10 ** rng.uniform(2.5, 6)
    rows, cur, day_i = [], None, -1
    for t in times:
        et = t.astimezone(ET)
        m = et.hour * 60 + et.minute
        if et.date() != cur:                                       # new session: draw its personality
            cur, day_i = et.date(), day_i + 1
            prior = px
            style = rng.choice(STYLES)
            sig = rng.uniform(0.03, 0.30) / 100
            gap = rng.choice((0.0, rng.gauss(0, 0.6), rng.gauss(0, 1.6))) / 100
            switch = rng.randint(9 * 60 + 45, 14 * 60 + 45)
            ext = rng.uniform(0.25, 1.4) / 100 * rng.choice((-1, 1))
            open_px = None
        o = px
        if m < 9 * 60 + 30:
            target, s = prior * (1 + gap), sig * 0.4
        elif m < 16 * 60:
            if open_px is None:
                open_px = o = px = prior * (1 + gap)               # the opening print gaps
            mid = m >= switch
            target = {
                "fade": prior if mid else open_px * (1 + 0.3 * gap),
                "trend": open_px * (1 + 2 * gap + (0.004 if gap >= 0 else -0.004)),
                "chop": open_px,
                "reclaim": open_px * (1 + ext) if mid else open_px * (1 - ext),
                "drift": open_px * (1 + ext * 0.7),
            }[style]
            s = sig
        else:
            target, s = px, sig * 0.3
        px = max(tick, px + 0.08 * (target - px) + px * s * rng.gauss(0, 1))
        c = px
        h = max(o, c) * (1 + abs(rng.gauss(0, 1)) * s * 0.6)
        lo = min(o, c) * (1 - abs(rng.gauss(0, 1)) * s * 0.6)
        nd = 4 if px < 1 else 2
        o, h, lo, c = (round(x, nd) for x in (o, h, lo, c))
        h, lo = max(h, o, c), min(lo, o, c)
        rth = 9 * 60 + 30 <= m < 16 * 60 or crypto
        vol = round(vbase * (1 if rth else 0.06) * rng.lognormvariate(0, 0.7))
        if not rth and rng.random() < 0.25:
            h = lo = o = c                                         # single print: no real range
        if rng.random() < 0.03:
            vol = 0
        if rng.random() < 0.01:
            continue                                               # missing bar
        if rng.random() < 0.006:
            o = h = lo = c = None                                  # feed null
        rows.append((t, o, h, lo, c, vol))
    while rows and rows[-1][1] is None:
        rows.pop()
    return (ticker, rows) if rows else None


def _now_after(rng: random.Random, rows, fresh: bool = False):
    minutes = rng.choice((1, 2, 4, 6, 9, 12)) if fresh else rng.choice((1, 3, 6, 9, 14, 15, 17, 25, 45, 180, 2000))
    return rows[-1][0] + timedelta(seconds=minutes * 60 + rng.choice((0, 7, 19, 41, 53)))


def make_cases(rng: random.Random, n_series: int):
    """
    Per series: the full history; a random live cut of its last session; and, like the replay,
    decision-time prefixes that end on the trigger bar or just after it (open trades).
    """
    cases = []
    for k in range(n_series):
        series = make_series(rng, k)
        if not series:
            continue
        ticker, rows = series
        full = (ticker, rows, _now_after(rng, rows))
        cases.append(full)
        last_day = rows[-1][0].astimezone(ET).date()
        day_idx = [i for i, r in enumerate(rows) if r[0].astimezone(ET).date() == last_day]
        if len(day_idx) > 2 and rng.random() < 0.6:
            cut = rows[: rng.choice(day_idx[1:]) + 1]
            while cut and cut[-1][1] is None:
                cut.pop()
            cases.append((ticker, cut, _now_after(rng, cut)))
        trig_ts = _py_row(*full).get("trig_ts")
        if trig_ts is not None:
            for extra in sorted(rng.sample((0, 1, 2, 4), 2)):
                cut = [r for r in rows if int(r[0].timestamp() * 1000) <= trig_ts + extra * 300_000]
                while cut and cut[-1][1] is None:
                    cut.pop()
                cases.append((ticker, cut, _now_after(rng, cut, fresh=rng.random() < 0.8)))
    return cases


def _py_row(ticker, rows, now):
    nan = float("nan")
    df = pd.DataFrame(
        {"Open": [nan if r[1] is None else r[1] for r in rows],
         "High": [nan if r[2] is None else r[2] for r in rows],
         "Low": [nan if r[3] is None else r[3] for r in rows],
         "Close": [nan if r[4] is None else r[4] for r in rows],
         "Volume": [float(r[5]) for r in rows]},
        index=pd.DatetimeIndex([r[0] for r in rows]),
    )
    row = analyze(ticker, df, now=now)
    apply_stale_guard(row, bar_age_min(df, now.timestamp()), MAX_AGE_MIN)
    dvol = session_dollar_volume(df)                              # as app.run_scan, after the guard
    row["dollar_vol"] = round(dvol, 0) if dvol else 0
    row["illiquid"] = bool(dvol > 0 and dvol < MIN_DVOL)
    row["rot"] = rotation_score(row)
    chart = row.pop("_chart", None) or {}
    row["markers"] = chart.get("markers")
    trig = (row["markers"] or {}).get("trig")
    row["trig_ts"] = chart["bars"][trig]["ts"] if trig is not None else None
    return row


def _js_case(ticker, rows, now):
    return {"ticker": ticker, "now_ms": int(now.timestamp() * 1000), "max_age_min": MAX_AGE_MIN,
            "raw": [{"ts": int(r[0].timestamp() * 1000), "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
                    for r in rows]}


def _close(a, b, nd):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= 1.01 * 10 ** -nd + 1e-12


@unittest.skipUnless(NODE, "node is not installed")
class TestEngineParity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cases = make_cases(random.Random(SEED), N_SERIES)
        cls.py = [_py_row(*c) for c in cases]
        payload = {"cases": [_js_case(*c) for c in cases], "grades": GRADES, "floors": FLOORS,
                   "geometry": GEOMETRY, "min_dvol": MIN_DVOL}
        proc = subprocess.run([NODE, str(RUNNER)], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=300, cwd=str(ROOT))
        if proc.returncode != 0:
            raise RuntimeError(f"engine.js runner failed: {proc.stderr[-2000:]}")
        out = json.loads(proc.stdout)
        cls.js, cls.js_version, cls.js_grade_ok, cls.js_geom_ok = out["rows"], out["version"], out["grade_ok"], out["geom_ok"]

    def test_same_engine_version(self):
        self.assertEqual(self.js_version, ENGINE_VERSION)

    def test_every_field_matches(self):
        self.assertEqual(len(self.py), len(self.js))
        mismatches = []
        for p, j in zip(self.py, self.js):
            for f in EXACT:
                if p.get(f) != j.get(f):
                    mismatches.append((p["ticker"], f, p.get(f), j.get(f)))
            for f in PRICES:
                nd = 4 if p.get(f) is not None and abs(p[f]) < 10 else 2
                if not _close(p.get(f), j.get(f), nd):
                    mismatches.append((p["ticker"], f, p.get(f), j.get(f)))
            for f, nd in DECIMALS.items():
                if not _close(p.get(f), j.get(f), nd):
                    mismatches.append((p["ticker"], f, p.get(f), j.get(f)))
        self.assertFalse(mismatches, f"{len(mismatches)} mismatches, first: {mismatches[:12]}")

    def test_scan_helpers_match_the_desk(self):
        """Session $ volume, the illiquid flag and the rotation rank (data.py), and the grade floor (app.py)."""
        for p, j in zip(self.py, self.js):
            self.assertAlmostEqual(p["dollar_vol"], j["dollar_vol"], delta=1.0, msg=p["ticker"])
            self.assertEqual(p["illiquid"], j["illiquid"], p["ticker"])
            self.assertAlmostEqual(p["rot"], j["rot"], delta=1e-6 * max(1.0, abs(p["rot"])), msg=p["ticker"])
        for gi, g in enumerate(GRADES):
            for fi, f in enumerate(FLOORS):
                self.assertEqual(_grade_ok(g, f), self.js_grade_ok[gi][fi], (g, f))

    def test_plan_geometry_matches_at_the_ties(self):
        self.assertEqual([_geom_ok(*g) for g in GEOMETRY], self.js_geom_ok)

    def test_cases_cover_the_engine(self):
        """The comparison only means something if the synthetic tape reaches every branch."""
        seen = Counter()
        for r in self.py:
            seen[f"signal:{r.get('signal')}"] += 1
            seen[f"mode:{r.get('setup_mode')}"] += 1
            seen[f"regime:{r.get('regime')}"] += 1
            seen[f"grade:{r.get('grade')}"] += 1
            seen[f"session:{r.get('session_label')}"] += 1
            if r.get("signal") == "TRIGGER" and r.get("grade") in ("A", "LA"):
                seen["A trigger"] += 1
            for flag in ("error", "trend_block", "regime_block", "no_runway", "thin_rvol", "stale_bars",
                         "live_actionable", "late", "gap_provisional"):
                if r.get(flag):
                    seen[flag] += 1
        need = ["signal:TRIGGER", "signal:TAGGED", "signal:STOPPED", "signal:WATCH", "signal:SETUP",
                "mode:gap", "mode:mdrev", "mode:both", "regime:trend", "regime:chop", "regime:mixed",
                "grade:A", "grade:B", "grade:C", "grade:✕", "error", "trend_block", "regime_block",
                "no_runway", "stale_bars", "live_actionable", "late", "session:rth", "session:24/7 crypto",
                "A trigger"]
        missing = [k for k in need if seen[k] == 0]
        self.assertFalse(missing, f"uncovered: {missing}; seen: {dict(seen)}")


if __name__ == "__main__":
    unittest.main()
