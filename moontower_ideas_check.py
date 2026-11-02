#!/usr/bin/env python3
"""
moontower_ideas_check.py — Moontower's own scanner, scored.

Each evening /v1/trade-ideas ranks every name for four preset ideas (Buy Vol, Sell Vol, Long
Calendar, Short Calendar) with a cross-sectional totalScore. This replays those lists, as snapshotted
after each session's close, against what happened next in the same names' daily cockpit rows:

  vol edge (vol points)  Buy Vol:  realized vol over the next 5 sessions − IV30 at the pick
                         Sell Vol: IV30 at the pick − realized vol over the next 5 sessions
                         (what a long or short straddle roughly earns, before gamma scalping and costs)
  calendar edge (points) Long Calendar:  the 5-session change in (IV90 − IV30): the back leg gains on
                         the front when the term structure steepens; Short Calendar is its negative
  next-day move          |next session's return| / the implied daily move (IV30/√252) at the pick:
                         the reading an intraday scanner cares about, > 1 = moved more than priced

Filters, as the scanner offers them: a score floor (> 0, > 20, > 50), the top 10 by score, and
"exclude earnings within 2 weeks" (from /v1/earnings). Every filtered list is compared with the
names it left out that night (the rest), with standard errors clustered by snapshot date (honest.py).

Reads the git-ignored data/moontower/ cache written by moontower_check.py (cockpit history, earnings,
ideas_<date>.json); nothing is fetched. Writes research/moontower_ideas_check_<today>.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from honest import clustered, verdict

IDEAS = ["Buy Vol", "Sell Vol", "Long Calendar", "Short Calendar"]
HORIZON = 5
TRADING_DAYS = 252
FILTERS = {
    "score>0": lambda rows: [r for r in rows if r["score"] > 0],
    "score>20": lambda rows: [r for r in rows if r["score"] > 20],
    "score>50": lambda rows: [r for r in rows if r["score"] > 50],
    "top10": lambda rows: sorted(rows, key=lambda r: -r["score"])[:10],
    "score>20, no earnings 2w": lambda rows: [r for r in rows if r["score"] > 20 and not r["earnings_2w"]],
}


def load_cache(root: Path):
    ck = defaultdict(dict)
    for p in sorted(root.glob("cockpit_*.json")):
        for r in json.loads(p.read_text()):
            ck[r["ticker"]][r["date"]] = r
    earn = {r["ticker"]: r for r in json.loads((root / "earnings.json").read_text())} if (root / "earnings.json").exists() else {}
    ideas = {}
    for p in sorted(root.glob("ideas_*.json")):
        d = json.loads(p.read_text())
        got = (d.get("summary") or {}).get("tradeDate")
        if got and got == p.stem[len("ideas_"):]:
            ideas[got] = d.get("tickers") or []
    return ck, earn, ideas


def fwd_realized(closes: List[float]) -> Optional[float]:
    """Annualized close-to-close vol, in percent, over the given closes (needs 3+)."""
    if len(closes) < 3 or any(c is None or c <= 0 for c in closes):
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mu = sum(rets) / len(rets)
    var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var * TRADING_DAYS) * 100


def earnings_within(rec: Optional[Dict[str, Any]], date: str, days: int) -> bool:
    if not rec:
        return False
    d0 = dt.date.fromisoformat(date)
    for k in ("next_earnings_date", "prior_earnings_date"):
        x = rec.get(k)
        if x and 0 <= (dt.date.fromisoformat(x) - d0).days <= days:
            return True
    return False


def outcomes(ck_t: Dict[str, Dict[str, Any]], date: str) -> Optional[Dict[str, Any]]:
    """Forward readings for one name from one snapshot date (the row dated `date` is the pick's close)."""
    days = sorted(ck_t)
    if date not in ck_t:
        return None
    i = days.index(date)
    fwd = days[i:i + HORIZON + 1]
    if len(fwd) < HORIZON + 1:
        return None
    r0, rN, r1 = ck_t[date], ck_t[fwd[-1]], ck_t[fwd[1]]
    iv30 = r0.get("iv_30d"); iv90 = r0.get("iv_90d")
    if iv30 is None or iv30 <= 0:
        return None
    rv = fwd_realized([ck_t[d].get("close") for d in fwd])
    out = {"iv30": iv30 * 100, "rv_fwd": rv, "vol_edge_long": None if rv is None else rv - iv30 * 100}
    if iv90 is not None and rN.get("iv_90d") is not None and rN.get("iv_30d") is not None:
        out["term_change"] = ((rN["iv_90d"] - rN["iv_30d"]) - (iv90 - iv30)) * 100
    else:
        out["term_change"] = None
    ret1 = r1.get("daily_return")
    move = iv30 / math.sqrt(TRADING_DAYS)
    out["next_move_ratio"] = None if ret1 is None else abs(ret1) / move
    return out


def stat(vals: List[Optional[float]], dates: List[str]) -> Dict[str, Any]:
    pairs = [(v, d) for v, d in zip(vals, dates) if v is not None]
    s = clustered([v for v, _ in pairs], [d for _, d in pairs])
    s["verdict"] = verdict(s)
    s["positive"] = sum(1 for v, _ in pairs if v > 0)
    return s


def run(root: Path) -> Dict[str, Any]:
    ck, earn, ideas = load_cache(root)
    covered = set(ck)
    dates = sorted(ideas)
    res: Dict[str, Any] = {"snapshots": dates, "horizon_sessions": HORIZON, "names_covered": len(covered),
                           "method": __doc__.strip().split("\n\n")[1], "ideas": {}}
    for idea in IDEAS:
        sign = {"Buy Vol": 1, "Sell Vol": -1, "Long Calendar": 1, "Short Calendar": -1}[idea]
        per_date = {}
        for d in dates:
            rows = []
            for r in ideas[d]:
                if r.get("idea") != idea or r["ticker"] not in covered:
                    continue
                o = outcomes(ck[r["ticker"]], d)
                if not o:
                    continue
                edge = None
                if idea in ("Buy Vol", "Sell Vol") and o["vol_edge_long"] is not None:
                    edge = sign * o["vol_edge_long"]
                elif idea in ("Long Calendar", "Short Calendar") and o["term_change"] is not None:
                    edge = sign * o["term_change"]
                rows.append({"ticker": r["ticker"], "score": float(r.get("totalScore") or 0), "date": d,
                             "earnings_2w": earnings_within(earn.get(r["ticker"]), d, 14),
                             "edge": edge, "next_move_ratio": o["next_move_ratio"]})
            per_date[d] = rows
        block: Dict[str, Any] = {"scored_names_per_night": round(sum(len(v) for v in per_date.values()) / max(1, len(per_date)), 1), "filters": {}}
        for fname, f in FILTERS.items():
            picked, rest = [], []
            for d, rows in per_date.items():
                p = f(rows); ps = {r["ticker"] for r in p}
                picked += p; rest += [r for r in rows if r["ticker"] not in ps]
            ent = {}
            for key in ("edge", "next_move_ratio"):
                sp = stat([r[key] for r in picked], [r["date"] for r in picked])
                sr = stat([r[key] for r in rest], [r["date"] for r in rest])
                diff = None
                if sp.get("se") is not None and sr.get("se") is not None:
                    dlt = sp["mean"] - sr["mean"]; se = (sp["se"] ** 2 + sr["se"] ** 2) ** 0.5
                    diff = {"mean": round(dlt, 3), "se": round(se, 3), "t": round(dlt / se, 2) if se else None}
                ent[key] = {"picked": sp, "rest": sr, "picked_minus_rest": diff}
            block["filters"][fname] = ent
        res["ideas"][idea] = block
    return res


def fmt(s: Dict[str, Any], unit: str) -> str:
    if not s.get("n"):
        return "n=0"
    return f"n={s['n']:>4} {s['mean']:+.2f}{unit}" + (f" ± {s['se']:.2f}" if s.get("se") is not None else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default="data/moontower")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    res = run(Path(a.cache))
    out = Path(a.out or f"research/moontower_ideas_check_{dt.date.today().isoformat()}.json")
    out.write_text(json.dumps(res, indent=1))
    print(f"{len(res['snapshots'])} nightly snapshots {res['snapshots'][0]} → {res['snapshots'][-1]} · {res['names_covered']} names with cockpit history · {HORIZON}-session horizon")
    for idea, block in res["ideas"].items():
        unit = " pts"
        print(f"\n== {idea} ({block['scored_names_per_night']} scored names a night)")
        for fname, ent in block["filters"].items():
            e, m = ent["edge"], ent["next_move_ratio"]
            de, dm = e["picked_minus_rest"] or {}, m["picked_minus_rest"] or {}
            print(f"  {fname:<26} edge picked {fmt(e['picked'], unit)}  rest {fmt(e['rest'], unit)}"
                  + (f"  Δ {de['mean']:+.2f} (t {de['t']:+.1f})" if de.get("t") is not None else "")
                  + f"\n  {'':<26} next-day |move|/implied picked {fmt(m['picked'], 'x')}  rest {fmt(m['rest'], 'x')}"
                  + (f"  Δ {dm['mean']:+.2f} (t {dm['t']:+.1f})" if dm.get("t") is not None else ""))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
