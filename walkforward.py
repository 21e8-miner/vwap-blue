#!/usr/bin/env python3
"""
Walk-forward / parameter grid for VWAP Blue (research only).

Uses free hybrid bars (same as desk). For each param combo:
  · rebuild setups with engine.analyze(opts=...)
  · simulate partial_trail exits
  · report mean R, sum R, win rate, n, grade-A only stats

Not a production optimizer — thin free history + delayed data.
Usage:
  python3 walkforward.py
  python3 walkforward.py --max-tickers 12 --quick
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np

from data import batch_fetch, load_universe
from engine import analyze
from backtest_today_scans import Setup, simulate, OUT_DIR

ET = ZoneInfo("America/New_York")


def _grid(quick: bool) -> List[Dict[str, Any]]:
    if quick:
        return [
            {"K": 2, "gap_min": 0.35, "rvol_min": 1.2, "sigma_mult": 0.15, "regime_gate": True},
            {"K": 2, "gap_min": 0.35, "rvol_min": 1.2, "sigma_mult": 0.15, "regime_gate": False},
            {"K": 2, "gap_min": 0.50, "rvol_min": 1.2, "sigma_mult": 0.25, "regime_gate": True},
            {"K": 3, "gap_min": 0.35, "rvol_min": 1.0, "sigma_mult": 0.15, "regime_gate": True},
            {"K": 2, "gap_min": 0.25, "rvol_min": 1.5, "sigma_mult": 0.10, "regime_gate": True},
        ]
    Ks = [1, 2, 3]
    gaps = [0.25, 0.35, 0.50, 0.75]
    rvols = [1.0, 1.2, 1.5]
    sigs = [0.10, 0.15, 0.25]
    gates = [True, False]
    out = []
    for K, g, rv, sm, rg in itertools.product(Ks, gaps, rvols, sigs, gates):
        out.append({"K": K, "gap_min": g, "rvol_min": rv, "sigma_mult": sm, "regime_gate": rg})
    return out


def _row_to_setup(row: Dict[str, Any]) -> Optional[Setup]:
    if row.get("error") or not row.get("entry") or not row.get("stop") or not row.get("target"):
        return None
    if row.get("side") not in ("long", "short"):
        return None
    if row.get("signal") in ("FLAT", "WATCH") and not row.get("actionable"):
        if not row.get("entry"):
            return None
    return Setup(
        source="vwap_blue",
        ticker=row["ticker"],
        side=row["side"],
        signal=str(row.get("signal")),
        grade=str(row.get("grade") or "–"),
        edge=float(row.get("edge") or 0),
        entry=float(row["entry"]),
        stop=float(row["stop"]),
        target=float(row["target"]),
        bar_provider=row.get("bar_provider"),
        note=str(row.get("state") or ""),
        rr_plan=float(row["rr"]) if row.get("rr") is not None else None,
        regime=str(row.get("regime") or ""),
        setup_mode=str(row.get("setup_mode") or ""),
    )


def run_combo(
    tickers: List[str],
    bars: Dict,
    daily: Dict,
    live: Dict,
    bar_prov: Dict,
    qmeta: Dict,
    opts: Dict[str, Any],
) -> Dict[str, Any]:
    setups: List[Setup] = []
    for t in tickers:
        row = analyze(
            t, bars.get(t), daily.get(t),
            live_price=live.get(t),
            bar_provider=bar_prov.get(t),
            quote_provider=(qmeta.get(t) or {}).get("provider"),
            opts=opts,
        )
        s = _row_to_setup(row)
        if s:
            setups.append(s)

    results = [simulate(s, bars.get(s.ticker), model="partial_trail") for s in setups]
    valid = [r for r in results if r.r_multiple is not None]
    a_only = [r for r in valid if r.grade in ("A", "LA")]

    def pack(rs):
        if not rs:
            return {"n": 0, "avg_r": None, "sum_r": None, "win_rate": None}
        xs = [r.r_multiple for r in rs]
        return {
            "n": len(rs),
            "avg_r": round(float(np.mean(xs)), 3),
            "sum_r": round(float(np.sum(xs)), 3),
            "win_rate": round(sum(1 for x in xs if x > 0) / len(xs), 3),
            "median_r": round(float(np.median(xs)), 3),
        }

    return {
        "opts": opts,
        "all": pack(valid),
        "grade_a": pack(a_only),
        "by_regime": {
            reg: pack([r for r in valid if (next((s.regime for s in setups if s.ticker == r.ticker), "") == reg)])
            for reg in ("chop", "mixed", "trend")
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tickers", type=int, default=16)
    ap.add_argument("--quick", action="store_true", help="5 combos only")
    args = ap.parse_args()

    now = datetime.now(ET)
    tickers = load_universe(max_n=args.max_tickers)
    for t in ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]:
        if t not in tickers:
            tickers.append(t)
    tickers = list(dict.fromkeys(tickers))[: args.max_tickers]

    print("=" * 72)
    print("  WALK-FORWARD GRID · VWAP Blue ·", now.strftime("%Y-%m-%d %H:%M %Z"))
    print("=" * 72)
    print(f"  Tickers: {len(tickers)}  ·  grid: {'quick' if args.quick else 'full'}")

    t0 = time.time()
    bars, daily, bar_prov, live, qmeta = batch_fetch(tickers, force=True, mode="hybrid")
    print(f"  Bars ready in {time.time()-t0:.1f}s")

    combos = _grid(args.quick)
    results = []
    for i, opts in enumerate(combos, 1):
        t1 = time.time()
        r = run_combo(tickers, bars, daily, live, bar_prov, qmeta, opts)
        results.append(r)
        a = r["all"]
        ga = r["grade_a"]
        print(
            f"  [{i:3d}/{len(combos)}] K={opts['K']} gap={opts['gap_min']} "
            f"rvol≥{opts['rvol_min']} σm={opts['sigma_mult']} gate={opts['regime_gate']}  "
            f"all n={a['n']} avgR={a['avg_r']} sumR={a['sum_r']}  "
            f"A n={ga['n']} avgR={ga['avg_r']}  ({time.time()-t1:.1f}s)"
        )

    # rank by grade-A sum_r then all avg_r
    def key(r):
        ga, al = r["grade_a"], r["all"]
        return (
            ga["sum_r"] if ga["sum_r"] is not None else -999,
            ga["n"],
            al["avg_r"] if al["avg_r"] is not None else -999,
        )

    ranked = sorted(results, key=key, reverse=True)
    print("\n  TOP 5 (by Grade-A sum R):")
    for r in ranked[:5]:
        o, ga, al = r["opts"], r["grade_a"], r["all"]
        print(
            f"    gate={o['regime_gate']} K={o['K']} gap={o['gap_min']} "
            f"rvol={o['rvol_min']} σm={o['sigma_mult']}  "
            f"A: n={ga['n']} avg={ga['avg_r']} sum={ga['sum_r']}  "
            f"all: n={al['n']} avg={al['avg_r']} sum={al['sum_r']}"
        )

    stamp = now.strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"walkforward_{stamp}.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "asof": now.isoformat(),
                "tickers": tickers,
                "quick": args.quick,
                "results": results,
                "top": ranked[:10],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  Wrote {out}")
    print("  Research only — free delayed data · not trade advice.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
