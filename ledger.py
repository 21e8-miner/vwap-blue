#!/usr/bin/env python3
"""
Forward signal ledger — out of sample by construction.

Every replay in this repo looks back at bars that already happened, so any rule tuned on it
(grade floors, regime gates, the mdrev-in-chop demotion) is fitted to the same month it is
judged on. The ledger is the other half: each live grade-A TRIGGER the desk shows is written
down the moment it appears, with decision-time fields only, and resolved after its session
closes with the replay's own exit simulator (next-bar-open fill + slippage, round-trip costs
in R). The report clusters standard errors by session (honest.py). A few weeks of this is
worth more than any re-tuning on last month's replay, and it is the dataset a learned setup
filter would eventually need.

Files (data/signals/, gitignored):
  signals.jsonl    append-only, one line per new trigger (deduped by ticker|session|trigger bar)
  outcomes.jsonl   append-only, one line per resolved signal and exit model

  python3 ledger.py resolve       # resolve signals whose session has closed (fetches bars)
  python3 ledger.py report        # the forward record

The live desk records automatically (app.py run_scan); GET /api/ledger serves the report.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from honest import clustered, fmt_stat, net_r, segments, verdict

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
SIGNALS_DIR = ROOT / "data" / "signals"
RESOLVE_MODELS = ("classic", "time_24")
ENTRY_MODES = ("next_open", "trigger_close")
SLIP_BPS = 2.0
_lock = threading.Lock()            # record(): dedup set + append
_resolve_lock = threading.Lock()    # resolve(): one resolver at a time, so a signal is never resolved twice
_seen: Dict[str, set] = {}
WINDOW_DAYS = 35                    # free 5m history reaches back ~1 month; older sessions cannot be refetched

DECISION_FIELDS = ("side", "entry", "stop", "target", "rr", "grade", "regime", "setup_mode", "ker", "rvol", "rvol_n",
                   "gap_pct", "edge", "dollar_vol", "bar_provider", "quote_provider", "bar_age_min", "one_agree")


def _paths(base: Optional[Path] = None) -> Dict[str, Path]:
    d = base or SIGNALS_DIR
    return {"dir": d, "signals": d / "signals.jsonl", "outcomes": d / "outcomes.jsonl"}


def _read(path: Path) -> List[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


def _append(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")


# ---------------------------------------------------------------------------
# Recording (called by the live desk)
# ---------------------------------------------------------------------------

def record(by_ticker: Dict[str, Dict[str, Any]], version: str = "", now_s: Optional[float] = None,
           base: Optional[Path] = None) -> int:
    """Append every new live-actionable TRIGGER in a scan. Returns how many were new."""
    p = _paths(base)
    now_s = time.time() if now_s is None else now_s
    new: List[Dict[str, Any]] = []
    with _lock:
        seen = _seen.setdefault(str(p["signals"]), {r["key"] for r in _read(p["signals"])})
        for t, row in by_ticker.items():
            if row.get("signal") != "TRIGGER" or not row.get("live_actionable"):
                continue
            ch = row.get("_chart") or {}
            trig = (ch.get("markers") or {}).get("trig")
            bars = ch.get("bars") or []
            if trig is None or not (0 <= trig < len(bars)):
                continue
            trig_ts = int(bars[trig]["ts"])
            key = f"{t}|{row.get('focus_day')}|{trig_ts}"
            if key in seen:
                continue
            seen.add(key)
            # bar timestamps mark the bar's open: the trigger was knowable one bar later
            spacing_ms = (bars[trig]["ts"] - bars[trig - 1]["ts"]) if trig > 0 else 300_000
            new.append({
                "key": key, "ticker": t, "session": row.get("focus_day"), "trigger_ts": trig_ts,
                "trigger_time": bars[trig].get("time"),
                "observed_at": datetime.fromtimestamp(now_s, ET).isoformat(timespec="seconds"),
                # how long after the trigger bar closed the desk actually showed it (feed delay + scan cadence)
                "observed_lag_min": round((now_s * 1000 - trig_ts - spacing_ms) / 60000.0, 1),
                "version": version,
                **{k: row.get(k) for k in DECISION_FIELDS},
            })
        _append(p["signals"], new)
    return len(new)


# ---------------------------------------------------------------------------
# Resolution (after the session closes)
# ---------------------------------------------------------------------------

def _session_closed(sig: Dict[str, Any], now: datetime) -> bool:
    day = datetime.strptime(sig["session"], "%Y-%m-%d").date()
    if day < now.date():
        return True
    crypto = str(sig.get("ticker", "")).upper().endswith(("-USD", "-USDT", "-USDC"))
    return (not crypto) and day == now.date() and (now.hour, now.minute) >= (16, 15)


def _day_bars(df, session: str) -> List[Dict[str, Any]]:
    from engine import _prep_bars
    return [b for b in _prep_bars(df) if b["d"] == session]


def resolve(now: Optional[datetime] = None, fetch: Optional[Callable] = None, base: Optional[Path] = None) -> Dict[str, int]:
    """
    Resolve every signal whose session has closed and that has no outcome yet.

    Serialized: concurrent callers (startup, the live loop, POST /api/ledger/resolve) wait their
    turn and then see the outcomes the first one wrote, so no signal is resolved twice. A fetch
    that comes back empty, or without the session, leaves the signal pending for a later retry
    (free feeds fail transiently); it becomes unresolvable only once the session is older than
    the free data window.
    """
    with _resolve_lock:
        return _resolve(now, fetch, base)


def _resolve(now: Optional[datetime], fetch: Optional[Callable], base: Optional[Path]) -> Dict[str, int]:
    from replay_sessions import MODELS, _fill_entry, _simulate
    if fetch is None:
        from data import batch_fetch as fetch
    p = _paths(base)
    now = now or datetime.now(ET)
    signals = _read(p["signals"])
    done = {o["key"] for o in _read(p["outcomes"])}
    pending = [s for s in signals if s["key"] not in done and _session_closed(s, now)]
    counts = {"pending_closed": len(pending), "resolved": 0, "unresolvable": 0, "retry_later": 0}
    if not pending:
        return counts
    tickers = sorted({s["ticker"] for s in pending})
    bars, *_ = fetch(tickers, force=True, mode="hybrid", bars_interval="5m")
    models = [m for m in MODELS if m.name in RESOLVE_MODELS]
    out: List[Dict[str, Any]] = []
    stamp = now.isoformat(timespec="seconds")
    for s in pending:
        df = bars.get(s["ticker"])
        day = _day_bars(df, s["session"]) if df is not None and len(df) else []
        idx = max((i for i, b in enumerate(day) if b["ts"] <= s["trigger_ts"]), default=None)
        age_days = (now.date() - datetime.strptime(s["session"], "%Y-%m-%d").date()).days
        reason = None
        if not (s.get("entry") and s.get("stop") and s.get("target")):
            reason = "no levels recorded"
        elif not day or idx is None:
            if age_days <= WINDOW_DAYS:
                counts["retry_later"] += 1          # empty or partial fetch: keep it pending, try again later
                continue
            reason = f"session older than the {WINDOW_DAYS}-day free data window"
        if reason:
            out.append({"key": s["key"], "status": "unresolvable", "reason": reason, "resolved_at": stamp})
            counts["unresolvable"] += 1
            continue
        for mode in ENTRY_MODES:
            entry, fill = _fill_entry(s["side"], float(s["entry"]), float(s["stop"]), float(s["target"]), day, idx, mode, SLIP_BPS)
            for m in models:
                if entry is None:
                    out.append({"key": s["key"], "status": "skipped", "reason": fill, "model": m.name,
                                "entry_mode": mode, "resolved_at": stamp})
                    continue
                exit_px, reason, r, mfe, mae, held = _simulate(s["side"], entry, float(s["stop"]), float(s["target"]),
                                                               day, idx, m, s["ticker"])
                out.append({"key": s["key"], "status": "resolved", "model": m.name, "entry_mode": mode,
                            "entry_fill": round(entry, 6), "exit": round(float(exit_px), 6), "exit_reason": reason,
                            "r": round(float(r), 3), "r_net": round(net_r(float(r), s["ticker"], entry, float(s["stop"])), 3),
                            "mfe_r": round(float(mfe), 3), "mae_r": round(float(mae), 3), "bars_held": int(held),
                            "resolved_at": stamp})
        counts["resolved"] += 1
    _append(p["outcomes"], out)
    return counts


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(base: Optional[Path] = None, model: str = "classic", entry_mode: str = "next_open") -> Dict[str, Any]:
    p = _paths(base)
    signals = {s["key"]: s for s in _read(p["signals"])}
    outcomes, seen = [], set()
    for o in _read(p["outcomes"]):        # ignore duplicate outcome lines (first one wins)
        k = (o["key"], "unresolvable") if o.get("status") == "unresolvable" else (o["key"], o.get("model"), o.get("entry_mode"))
        if k not in seen:
            seen.add(k)
            outcomes.append(o)
    resolved_keys = {o["key"] for o in outcomes}
    rows = [{**signals[o["key"]], **o} for o in outcomes
            if o.get("status") == "resolved" and o.get("model") == model and o.get("entry_mode") == entry_mode
            and o["key"] in signals]
    stat = clustered([r["r_net"] for r in rows], [r["session"] for r in rows])
    lags = sorted(s["observed_lag_min"] for s in signals.values() if s.get("observed_lag_min") is not None)
    return {
        "signals": len(signals), "resolved": len(rows), "pending": len([k for k in signals if k not in resolved_keys]),
        "unresolvable": sum(1 for o in outcomes if o.get("status") == "unresolvable"),
        "skipped": sum(1 for o in outcomes if o.get("status") == "skipped" and o.get("model") == model
                       and o.get("entry_mode") == entry_mode),
        "model": model, "entry_mode": entry_mode,
        "net_r": stat, "verdict": verdict(stat),
        "gross_r": clustered([r["r"] for r in rows], [r["session"] for r in rows]),
        "win_rate": round(sum(1 for r in rows if r["r_net"] > 0) / len(rows), 3) if rows else None,
        "segments": segments(rows, ("setup_mode", "regime"), min_n=3),
        "median_observed_lag_min": lags[len(lags) // 2] if lags else None,
        "sessions": sorted({r["session"] for r in rows}),
        # engine versions grade differently (v1.4.1 fixed the prior close): never pool them blindly
        "versions": dict(sorted(Counter(s.get("version") or "?" for s in signals.values()).items())),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="VWAP Blue forward signal ledger")
    ap.add_argument("command", choices=("resolve", "report"))
    ap.add_argument("--model", default="classic")
    ap.add_argument("--entry", default="next_open", choices=ENTRY_MODES)
    args = ap.parse_args(argv)
    if args.command == "resolve":
        print(json.dumps(resolve()))
    rep = report(model=args.model, entry_mode=args.entry)
    print(f"Forward record ({rep['model']}, {rep['entry_mode']}): {rep['signals']} signals, {rep['resolved']} resolved, "
          f"{rep['pending']} pending, {rep['skipped']} skipped fills, {rep['unresolvable']} unresolvable")
    print(f"  net R: {fmt_stat(rep['net_r'])}")
    if len(rep["versions"]) > 1:
        print(f"  mixed engine versions {rep['versions']}: compare them before pooling")
    if rep["median_observed_lag_min"] is not None:
        print(f"  the desk showed triggers a median {rep['median_observed_lag_min']} min after the trigger bar")
    for seg in rep["segments"]:
        print(f"  {seg['setup_mode']}/{seg['regime']:7s} {fmt_stat(seg)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
