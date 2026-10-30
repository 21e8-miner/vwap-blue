#!/usr/bin/env python3
"""
moontower_check.py — the Moontower lens, replayed.

VWAP Blue reads the price consensus: where the volume traded (the anchors) and whether price is
reclaiming or fading them. Moontower (api.moontower.ai) reads the options consensus on the same
names: how wide a range the market is paying for, whether that range is being repriced, whether an
information event (earnings) just reset the anchor, and whether the options are cheap or rich
against what the stock actually does. This script asks whether any of those readings, known at
decision time, separates the engine's winners from its losers on the replayed sessions.

Lenses (each a yes/no on a row, from the prior session's close):
  earnings_clear  did not report earnings today or yesterday (the anchor was not just reset by news;
                  the one-day-either-side variant is reported beside it under "variants")
  iv_rising       30d implied vol up at least 3 vol points on the week
  iv_high         30d implied vol at or above 50%
  vol_cheap       realized vol above implied (monthly VRP < 0): the options are cheap for the move
  vol_rich        implied well above realized (monthly VRP >= 0.10): the options are rich
  stretched       |monthly return z-score| >= 1.5: stretched on the quarter-scale anchor too
  buy_vol_idea    on Moontower's Buy Vol list that evening (score > 20 across its universe)
  sell_vol_idea   on Moontower's Sell Vol list that evening (score > 20)

Decision time means: the cockpit row is the last one dated before the session, the trade-ideas
snapshot is the one computed after the prior session's close, and earnings dates are the calendar.

Usage:
  python3 moontower_check.py --replay research/replay_2026-10-29_v1.7.0.json \
      --cache data/moontower --out research/moontower_check_2026-10-30.json
  add --pull to fetch what the cache lacks (needs MOONTOWER_API_KEY, or MOONTOWER_API_KEY_FILE,
  or ~/.config/active_projects_secrets/moontower_api_key). Each /v1 call costs 2 Moontower credits:
  cockpit and earnings are 1 call per 100 names, trade ideas 1 call per session.
The raw pulls are Moontower's data and stay in the (git-ignored) cache; only the per-lens table
is written to research/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from honest import clustered, verdict

API = "https://api.moontower.ai/v1"
THRESHOLDS = {
    "iv_rising_weekly_change": 0.03,   # iv_30d_weekly_change, decimal vol points
    "iv_high": 0.50,                   # iv_30d, decimal
    "vol_cheap_vrp": 0.0,              # vrp_monthly below this
    "vol_rich_vrp": 0.10,              # vrp_monthly at or above this
    "stretched_z": 1.5,                # |monthly_return_zscore|
    "idea_score": 20.0,                # trade-ideas totalScore above this counts as "on the list"
    "earnings_clear_post_days": 1,     # reported 0..this many days ago = not clear
}
LENSES = ["earnings_clear", "iv_rising", "iv_high", "vol_cheap", "vol_rich", "stretched",
          "buy_vol_idea", "sell_vol_idea"]
LENS_TEXT = {
    "earnings_clear": "did not report earnings today or yesterday",
    "iv_rising": "IV30 up ≥ 3 pts on the week",
    "iv_high": "IV30 ≥ 50%",
    "vol_cheap": "realized above implied (VRP < 0)",
    "vol_rich": "implied well above realized (VRP ≥ 0.10)",
    "stretched": "|monthly return z| ≥ 1.5",
    "buy_vol_idea": "on Moontower's Buy Vol list",
    "sell_vol_idea": "on Moontower's Sell Vol list",
}


# ── key + fetch ──────────────────────────────────────────────────────────────────────────────────

def resolve_api_key(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit
    k = os.environ.get("MOONTOWER_API_KEY", "").strip()
    if k:
        return k
    f = os.environ.get("MOONTOWER_API_KEY_FILE") or os.path.expanduser("~/.config/active_projects_secrets/moontower_api_key")
    p = Path(f)
    if p.is_file():
        if p.stat().st_mode & 0o077:
            raise SystemExit(f"{p}: key file is readable by others; chmod 600 it")
        return p.read_text().strip() or None
    return None


def api_get(key: str, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    q = urllib.parse.urlencode(params, doseq=True)
    # a plain python User-Agent trips Cloudflare's WAF (1010/403); any browser-ish or curl UA passes
    req = urllib.request.Request(f"{API}/{path}?{q}", headers={
        "X-API-Key": key, "Accept": "application/json", "User-Agent": "curl/8.7.1 vwap-blue moontower_check"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        if e.code == 402:
            raise SystemExit(f"Moontower: out of credits ({body}); not retrying")
        raise SystemExit(f"Moontower {path} → {e.code}: {body}")


# ── cache ────────────────────────────────────────────────────────────────────────────────────────

class Cache:
    """data/moontower/: tickers.json, cockpit_<start>_<end>.json, earnings.json, ideas_<date>.json"""

    def __init__(self, root: Path, key: Optional[str], pull: bool):
        self.root, self.key, self.pull = root, key, pull
        root.mkdir(parents=True, exist_ok=True)

    def _need(self, what: str) -> None:
        if not self.pull:
            raise SystemExit(f"cache lacks {what}; re-run with --pull (costs credits)")
        if not self.key:
            raise SystemExit("no Moontower key: set MOONTOWER_API_KEY or MOONTOWER_API_KEY_FILE")

    def directory(self) -> Dict[str, Dict[str, Any]]:
        p = self.root / "tickers.json"
        if not p.exists():
            self._need("the ticker directory")
            p.write_text(json.dumps(api_get(self.key, "tickers", {})))
        return {r["ticker"]: r for r in json.loads(p.read_text())["data"]}

    def cockpit(self, tickers: List[str], start: str, end: str) -> List[Dict[str, Any]]:
        p = self.root / f"cockpit_{start}_{end}.json"
        if not p.exists():
            self._need(f"cockpit {start}..{end}")
            rows: List[Dict[str, Any]] = []
            for i in range(0, len(tickers), 100):
                rows += api_get(self.key, "cockpit", {"ticker": tickers[i:i + 100], "start_date": start, "end_date": end})["data"]
            p.write_text(json.dumps(rows))
        return json.loads(p.read_text())

    def earnings(self, tickers: List[str]) -> Dict[str, Dict[str, Any]]:
        p = self.root / "earnings.json"
        if not p.exists():
            self._need("earnings dates")
            rows: List[Dict[str, Any]] = []
            for i in range(0, len(tickers), 100):
                rows += api_get(self.key, "earnings", {"ticker": tickers[i:i + 100]})["data"]
            p.write_text(json.dumps(rows))
        return {r["ticker"]: r for r in json.loads(p.read_text())}

    def ideas(self, trade_date: str) -> Dict[str, Dict[str, float]]:
        """ticker -> {idea: totalScore} as of that evening's snapshot."""
        p = self.root / f"ideas_{trade_date}.json"
        if not p.exists():
            self._need(f"trade ideas as of {trade_date}")
            p.write_text(json.dumps(api_get(self.key, "trade-ideas", {"universe": "all", "trade_date": trade_date})))
        d = json.loads(p.read_text())
        got = (d.get("summary") or {}).get("tradeDate")
        if got and got != trade_date:
            # the API falls back to the latest snapshot for a date it has no rows for; that would leak the future
            return {}
        out: Dict[str, Dict[str, float]] = defaultdict(dict)
        for r in d.get("tickers") or []:
            out[r["ticker"]][r["idea"]] = float(r.get("totalScore") or 0)
        return out


# ── decision-time readings ───────────────────────────────────────────────────────────────────────

def prior_cockpit(rows_by_date: Dict[str, Dict[str, Any]], session: str) -> Optional[Dict[str, Any]]:
    """The last cockpit row dated before the session (its prior close)."""
    ks = [k for k in rows_by_date if k < session]
    return rows_by_date[max(ks)] if ks else None


def earnings_distance(rec: Optional[Dict[str, Any]], session: str) -> Optional[int]:
    """Signed days from the nearest earnings date: 0 = reports today, +1 = reported yesterday, -1 = tomorrow."""
    if not rec:
        return None
    s = dt.date.fromisoformat(session)
    best: Optional[int] = None
    for k in ("prior_earnings_date", "next_earnings_date"):
        x = rec.get(k)
        if not x:
            continue
        d = (s - dt.date.fromisoformat(x)).days
        if best is None or abs(d) < abs(best):
            best = d
    return best


def lenses(ck: Optional[Dict[str, Any]], e_days: Optional[int], ideas: Optional[Dict[str, float]],
           th: Dict[str, float] = THRESHOLDS) -> Dict[str, Optional[bool]]:
    """Each lens: True/False, or None when the reading is missing (the row is then uncovered)."""
    g = (lambda k: None if ck is None else ck.get(k))
    def cmp(v, f):
        return None if v is None else bool(f(v))
    out = {
        "earnings_clear": None if e_days is None else not (0 <= e_days <= th["earnings_clear_post_days"]),
        "_earnings_clear_pm1": None if e_days is None else abs(e_days) > 1,   # variant: no report within a day either way
        "iv_rising": cmp(g("iv_30d_weekly_change"), lambda v: v >= th["iv_rising_weekly_change"]),
        "iv_high": cmp(g("iv_30d"), lambda v: v >= th["iv_high"]),
        "vol_cheap": cmp(g("vrp_monthly"), lambda v: v < th["vol_cheap_vrp"]),
        "vol_rich": cmp(g("vrp_monthly"), lambda v: v >= th["vol_rich_vrp"]),
        "stretched": cmp(g("monthly_return_zscore"), lambda v: abs(v) >= th["stretched_z"]),
        "buy_vol_idea": None if ideas is None else (ideas.get("Buy Vol") or 0) > th["idea_score"],
        "sell_vol_idea": None if ideas is None else (ideas.get("Sell Vol") or 0) > th["idea_score"],
    }
    return out


def readings(ck: Optional[Dict[str, Any]], e_days: Optional[int], ideas: Optional[Dict[str, float]]) -> Dict[str, Any]:
    """The compact per-row reading the page shows: same fields the lenses use."""
    g = (lambda k: None if ck is None else ck.get(k))
    return {"date": g("date"), "iv_30d": g("iv_30d"), "iv_30d_weekly_change": g("iv_30d_weekly_change"),
            "vrp_monthly": g("vrp_monthly"), "monthly_return_zscore": g("monthly_return_zscore"),
            "earnings_days": e_days,
            "buy_vol": None if ideas is None else ideas.get("Buy Vol"),
            "sell_vol": None if ideas is None else ideas.get("Sell Vol")}


# ── the check ────────────────────────────────────────────────────────────────────────────────────

def stat(trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    tr = list(trades)
    s = clustered([float(t["r_net"]) for t in tr], [t["session"] for t in tr])
    s["verdict"] = verdict(s)
    s["wins"] = sum(1 for t in tr if t["r_net"] > 0)
    return s


def run(replay: Dict[str, Any], cache: Cache, grade_sets=(("A",), ("A", "B"))) -> Dict[str, Any]:
    trades = [t for t in replay["trades"] if t.get("model") == "classic" and t.get("r_net") is not None]
    sessions = sorted({t["session"] for t in trades})
    directory = cache.directory()
    names = sorted({t["ticker"] for t in trades if t["ticker"] in directory})
    start = (dt.date.fromisoformat(sessions[0]) - dt.timedelta(days=4)).isoformat()
    ck_rows = cache.cockpit(names, start, sessions[-1])
    ck: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for r in ck_rows:
        ck[r["ticker"]][r["date"]] = r
    earn = cache.earnings(names)
    # ideas as of the prior session's evening: the snapshot for date d is computed after d's close
    ideas_by_session: Dict[str, Dict[str, Dict[str, float]]] = {}
    for i, s in enumerate(sessions):
        prev = sessions[i - 1] if i else (dt.date.fromisoformat(s) - dt.timedelta(days=1)).isoformat()
        ideas_by_session[s] = cache.ideas(prev)

    tagged = []
    for t in trades:
        if t["ticker"] not in directory:
            continue
        row = prior_cockpit(ck.get(t["ticker"], {}), t["session"])
        e_days = earnings_distance(earn.get(t["ticker"]), t["session"])
        ideas = ideas_by_session[t["session"]].get(t["ticker"]) if ideas_by_session[t["session"]] else None
        tagged.append({**t, "_lens": lenses(row, e_days, ideas)})

    out: Dict[str, Any] = {
        "replay": replay.get("method", {}), "engine_version": replay.get("engine_version"),
        "window": [sessions[0], sessions[-1]], "sessions": len(sessions),
        "trades": len(trades), "covered": len(tagged), "names_covered": len(names),
        "thresholds": THRESHOLDS, "lens_text": LENS_TEXT, "decision_time":
            "cockpit = last row before the session; ideas = snapshot after the prior session's close",
        "grades": {},
    }
    for gs in grade_sets:
        label = "+".join(gs)
        g = [t for t in tagged if t.get("grade") in gs]
        block: Dict[str, Any] = {"all": stat(g), "by_mode": {}, "lenses": {}}
        for mode in ("mdrev", "gap"):
            sub = [t for t in g if t.get("setup_mode") == mode]
            if sub:
                block["by_mode"][mode] = stat(sub)
        for lens in LENSES:
            yes = [t for t in g if t["_lens"][lens] is True]
            no = [t for t in g if t["_lens"][lens] is False]
            unk = sum(1 for t in g if t["_lens"][lens] is None)
            ent: Dict[str, Any] = {"pass": stat(yes), "fail": stat(no), "unknown": unk, "by_mode": {}}
            for mode in ("mdrev", "gap"):
                ys = [t for t in yes if t.get("setup_mode") == mode]
                ns = [t for t in no if t.get("setup_mode") == mode]
                if ys or ns:
                    ent["by_mode"][mode] = {"pass": stat(ys), "fail": stat(ns)}
            # the lens's effect: pass minus fail, with the two groups' SEs combined
            p, f = ent["pass"], ent["fail"]
            if p.get("se") is not None and f.get("se") is not None:
                d = p["mean"] - f["mean"]
                se = (p["se"] ** 2 + f["se"] ** 2) ** 0.5
                ent["pass_minus_fail"] = {"mean": round(d, 4), "se": round(se, 4), "t": round(d / se, 2) if se else None}
            block["lenses"][lens] = ent
        # the earnings lens as first drafted (no report within a day either way): pre-earnings sessions
        # carry no overnight risk for a flat-by-close engine, so the shipped lens is post-report only
        v = [t for t in g if t["_lens"]["_earnings_clear_pm1"] is True]
        w = [t for t in g if t["_lens"]["_earnings_clear_pm1"] is False]
        block["variants"] = {"earnings_clear_within_1d_either_way": {"pass": stat(v), "fail": stat(w)}}
        out["grades"][label] = block
    return out


def compact(res: Dict[str, Any]) -> Dict[str, Any]:
    """What the page embeds: per grade set and lens, pass/fail n · mean · se, plus thresholds and the window."""
    out = {"window": res["window"], "sessions": res["sessions"], "covered": res["covered"], "trades": res["trades"],
           "thresholds": res["thresholds"], "lens_text": res["lens_text"], "grades": {}}
    keep = lambda s: {k: s.get(k) for k in ("n", "mean", "se", "t", "verdict")}
    for label, block in res["grades"].items():
        out["grades"][label] = {"all": keep(block["all"]),
                                "lenses": {lens: {"pass": keep(e["pass"]), "fail": keep(e["fail"]),
                                                  "delta": e.get("pass_minus_fail")} for lens, e in block["lenses"].items()}}
    return out


def embed(html_path: Path, res: Dict[str, Any]) -> None:
    import re
    blob = json.dumps(compact(res), separators=(",", ":"), ensure_ascii=False)
    s = html_path.read_text()
    # a function replacement: the blob may hold backslashes, which a template string would reinterpret
    new, n = re.subn(r"const MOONTOWER = \{.*?\};\n", lambda _m: f"const MOONTOWER = {blob};\n", s, count=1, flags=re.S)
    if n != 1:
        raise SystemExit(f"{html_path}: no `const MOONTOWER = {{...}};` line to replace")
    html_path.write_text(new)


def fmt(s: Dict[str, Any]) -> str:
    if not s.get("n"):
        return "n=0"
    se = f" ± {s['se']:.2f}" if s.get("se") is not None else ""
    return f"n={s['n']:>4}  {s['mean']:+.3f}R{se}  win {s['wins'] / s['n']:.0%}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", required=True, help="a replay_sessions.py snapshot (grade floor B, so both grades are in it)")
    ap.add_argument("--cache", default="data/moontower", help="where the Moontower pulls live (git-ignored)")
    ap.add_argument("--out", default=None, help="research/moontower_check_<today>.json")
    ap.add_argument("--pull", action="store_true", help="fetch what the cache lacks (2 credits a call)")
    ap.add_argument("--key", default=None, help="Moontower API key (else env / key file)")
    ap.add_argument("--embed", nargs="*", default=None, metavar="HTML",
                    help="also rewrite each page's `const MOONTOWER = {...};` from the result")
    a = ap.parse_args(argv)
    replay = json.loads(Path(a.replay).read_text())
    cache = Cache(Path(a.cache), resolve_api_key(a.key) if a.pull else None, a.pull)
    res = run(replay, cache)
    out = Path(a.out or f"research/moontower_check_{dt.date.today().isoformat()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1))
    print(f"{res['covered']} of {res['trades']} replayed trades on {res['names_covered']} Moontower-covered names · "
          f"{res['sessions']} sessions {res['window'][0]} → {res['window'][1]}")
    for label, block in res["grades"].items():
        print(f"\n== grade {label}: {fmt(block['all'])}")
        for lens in LENSES:
            e = block["lenses"][lens]
            d = e.get("pass_minus_fail") or {}
            dtxt = f"  Δ {d['mean']:+.2f} (t {d['t']:+.1f})" if d.get("t") is not None else ""
            print(f"  {lens:<15} pass {fmt(e['pass'])}   fail {fmt(e['fail'])}{dtxt}" + (f"   ?{e['unknown']}" if e["unknown"] else ""))
    print(f"\nwrote {out}")
    for h in a.embed or []:
        embed(Path(h), res)
        print(f"embedded in {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
