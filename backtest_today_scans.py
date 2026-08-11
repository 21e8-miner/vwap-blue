#!/usr/bin/env python3
"""
Backtest ALL scan setups from today's session across our scanners.

Sources:
  1. VWAP Blue engine (Blueline dual-VWAP grades A/B/C)
  2. VWAP One engine (SETUP/TRIGGER tiers) if importable
  3. Live API last-scan payloads if servers are up
  4. idiot-flow board (optional embedded payload / fresh scan)

Method (honest, not fantasy):
  · Rebuild each setup on today's free multi-provider bars
  · Enter at engine entry (or first confirm bar close)
  · Exit at first of: target touch, stop breach, or last available bar (EOD/mark)
  · Cost haircut: equities 8 bps RT, crypto 15 bps RT
  · Report R-multiples, hit rate, expectancy by source/grade/side

Research only. Free data delayed. Not financial advice.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "vwap_simple_scanner"))

from data import batch_fetch, load_universe  # noqa: E402
from engine import analyze as blue_analyze  # noqa: E402

try:
    from scanner import analyze as one_analyze  # type: ignore
except Exception:
    one_analyze = None

OUT_DIR = HERE / "data" / "backtests"
OUT_DIR.mkdir(parents=True, exist_ok=True)

COST = {"equity": 0.0008, "crypto": 0.0015}
RISK_USD = 50.0
# Exit model upgrades (close MFE gap without fantasy fills)
PARTIAL_R = 1.0          # take 50% off at +1R
PARTIAL_FRAC = 0.5
TRAIL_AFTER_PARTIAL = True  # trail rest under/above blue-proxied stop = entry (breakeven)
TIME_STOP_BARS = 48      # ~4h on 5m bars; flatten if never reached +0.5R
TIME_STOP_MIN_R = 0.5


@dataclass
class Setup:
    source: str
    ticker: str
    side: str  # long | short
    signal: str
    grade: str
    edge: float
    entry: float
    stop: float
    target: float
    bar_provider: Optional[str] = None
    note: str = ""
    rr_plan: Optional[float] = None
    regime: str = ""
    setup_mode: str = ""


@dataclass
class TradeResult:
    source: str
    ticker: str
    side: str
    signal: str
    grade: str
    edge: float
    entry: float
    stop: float
    target: float
    exit: float
    exit_reason: str  # target | stop | eod | partial_target | time_stop | invalid
    r_multiple: Optional[float]
    pnl_pct: Optional[float]
    pnl_pct_net: Optional[float]
    mfe_r: Optional[float]
    mae_r: Optional[float]
    bars_held: int
    risk_pct: Optional[float]
    plan_rr: Optional[float]
    provider: Optional[str] = None
    note: str = ""
    model: str = "classic"  # classic | partial_trail


def _is_crypto(t: str) -> bool:
    t = t.upper()
    return t.endswith(("-USD", "-USDT", "-USDC"))


def _fetch_live_last(url: str, source: str) -> List[Setup]:
    out: List[Setup] = []
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read().decode())
    except Exception:
        return out
    for row in d.get("results") or []:
        entry, stop, target = row.get("entry"), row.get("stop"), row.get("target")
        side = (row.get("side") or "").lower()
        sig = row.get("signal") or row.get("tier") or ""
        if not entry or not stop or not target or side not in ("long", "short"):
            continue
        # skip pure FLAT/WATCH without levels
        if "FLAT" in str(sig).upper() and not row.get("actionable"):
            continue
        out.append(
            Setup(
                source=source,
                ticker=(row.get("ticker") or row.get("symbol") or "").upper(),
                side=side,
                signal=str(sig),
                grade=str(row.get("grade") or "–"),
                edge=float(row.get("edge") or 0),
                entry=float(entry),
                stop=float(stop),
                target=float(target),
                bar_provider=row.get("bar_provider") or row.get("provider"),
                note=str(row.get("state") or row.get("note") or ""),
                rr_plan=float(row["rr"]) if row.get("rr") is not None else None,
            )
        )
    return out


def _setups_from_blue(tickers: List[str], mode: str = "hybrid") -> Tuple[List[Setup], Dict[str, pd.DataFrame]]:
    bars, daily, bar_prov, live, qmeta = batch_fetch(tickers, force=True, mode=mode)
    setups: List[Setup] = []
    for t in tickers:
        row = blue_analyze(
            t,
            bars.get(t),
            daily.get(t),
            live_price=live.get(t),
            bar_provider=bar_prov.get(t),
            quote_provider=(qmeta.get(t) or {}).get("provider"),
        )
        if row.get("error"):
            continue
        if not row.get("entry") or not row.get("stop") or not row.get("target"):
            continue
        if row.get("side") not in ("long", "short"):
            continue
        # keep anything with a defined trade plan (TRIGGER/SETUP/TAGGED/STOPPED)
        if row.get("signal") in ("FLAT", None) and not row.get("actionable"):
            continue
        setups.append(
            Setup(
                source="vwap_blue",
                ticker=t,
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
            )
        )
    return setups, bars


def _setups_from_one(tickers: List[str], bars: Dict[str, pd.DataFrame], daily, live, bar_prov, qmeta) -> List[Setup]:
    if one_analyze is None:
        return []
    setups: List[Setup] = []
    for t in tickers:
        try:
            row = one_analyze(
                t,
                bars.get(t),
                daily.get(t) if daily else None,
                live_price=live.get(t) if live else None,
                bar_provider=bar_prov.get(t) if bar_prov else None,
                quote_provider=(qmeta.get(t) or {}).get("provider") if qmeta else None,
            )
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        entry, stop, target = row.get("entry"), row.get("stop"), row.get("target")
        side = (row.get("side") or "").lower()
        if not entry or not stop or not target or side not in ("long", "short"):
            continue
        sig = str(row.get("signal") or "")
        if "FLAT" in sig.upper() and not row.get("actionable") and not row.get("live_actionable"):
            continue
        setups.append(
            Setup(
                source="vwap_one",
                ticker=t,
                side=side,
                signal=sig,
                grade="–",
                edge=float(row.get("edge") or 0),
                entry=float(entry),
                stop=float(stop),
                target=float(target),
                bar_provider=row.get("bar_provider") or row.get("provider"),
                note=str(row.get("note") or ""),
                rr_plan=float(row["rr"]) if row.get("rr") is not None else None,
            )
        )
    return setups


def _idiot_flow_setups() -> List[Setup]:
    """Parse today's idiot-flow HTML payload if present."""
    path = Path.home() / "idiot-flow-bourse" / "idiot_flow_lab.html"
    if not path.exists():
        path = Path.home() / "idiot-flow-bourse" / "index.html"
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    # embedded JSON often after const DATA = or similar
    import re

    m = re.search(r'(?:const\s+(?:DATA|SCAN|PAYLOAD)\s*=\s*|id="payload"[^>]*>)\s*(\{.*?\})\s*;?\s*(?:</script>|//|$)', text, re.S)
    if not m:
        # try potato / ideas arrays
        m = re.search(r'"potato"\s*:\s*(\[.*?\])\s*[,}]', text, re.S)
        if not m:
            return []
        try:
            potato = json.loads(m.group(1))
        except Exception:
            return []
    else:
        try:
            blob = json.loads(m.group(1))
            potato = blob.get("potato") or blob.get("ideas") or []
        except Exception:
            return []

    out: List[Setup] = []
    for p in potato[:20]:
        if not isinstance(p, dict):
            continue
        side = (p.get("side") or "").lower()
        if side not in ("long", "short"):
            continue
        px = p.get("price")
        if not px:
            continue
        # synthetic 1R stop / 1.5R target from edge heuristics
        risk = abs(float(px)) * 0.015
        if side == "long":
            entry, stop, target = float(px), float(px) - risk, float(px) + 1.5 * risk
        else:
            entry, stop, target = float(px), float(px) + risk, float(px) - 1.5 * risk
        sym = str(p.get("symbol") or "").replace("USDT", "-USD")
        if not sym.endswith("-USD") and not sym.endswith("USDT"):
            sym = sym + "-USD" if not sym.endswith("USD") else sym
        out.append(
            Setup(
                source="idiot_flow",
                ticker=sym,
                side=side,
                signal=str(p.get("setup_state") or "READY"),
                grade="–",
                edge=float(p.get("score") or 0),
                entry=entry,
                stop=stop,
                target=target,
                note=str(p.get("reason") or "")[:120],
                rr_plan=1.5,
            )
        )
    return out


def simulate(
    setup: Setup,
    bars: Optional[pd.DataFrame],
    model: str = "partial_trail",
) -> TradeResult:
    """
    Walk forward from entry through remaining bars.

    model:
      classic       — all-in, stop/target/EOD (original)
      partial_trail — 50% at +PARTIAL_R, trail rest to breakeven, time-stop
    """
    invalid = TradeResult(
        source=setup.source, ticker=setup.ticker, side=setup.side, signal=setup.signal,
        grade=setup.grade, edge=setup.edge, entry=setup.entry, stop=setup.stop,
        target=setup.target, exit=setup.entry, exit_reason="invalid",
        r_multiple=None, pnl_pct=None, pnl_pct_net=None, mfe_r=None, mae_r=None,
        bars_held=0, risk_pct=None, plan_rr=setup.rr_plan, provider=setup.bar_provider,
        note=setup.note, model=model,
    )
    if bars is None or bars.empty:
        invalid.exit_reason = "no_bars"
        return invalid

    entry, stop, target = setup.entry, setup.stop, setup.target
    risk = abs(entry - stop)
    if risk <= 0 or not (entry > 0):
        return invalid

    risk_pct = risk / entry * 100.0
    closes = bars["Close"].astype(float)
    highs = bars["High"].astype(float)
    lows = bars["Low"].astype(float)

    entry_i = None
    for i in range(len(bars)):
        if lows.iloc[i] <= entry <= highs.iloc[i]:
            entry_i = i
            break
    if entry_i is None:
        entry_i = int((closes - entry).abs().values.argmin())

    mfe = 0.0
    mae = 0.0
    exit_px = float(closes.iloc[-1])
    reason = "eod"
    held = 0
    working_stop = stop
    partial_done = False
    realized_r = 0.0  # weighted R from partial + final

    use_partial = model == "partial_trail"
    partial_level = entry + PARTIAL_R * risk if setup.side == "long" else entry - PARTIAL_R * risk

    for i in range(entry_i, len(bars)):
        held = i - entry_i
        h, l, c = float(highs.iloc[i]), float(lows.iloc[i]), float(closes.iloc[i])
        if setup.side == "long":
            fav = h - entry
            adv = entry - l
            mfe = max(mfe, fav)
            mae = max(mae, adv)
            # stop first (conservative same-bar)
            if l <= working_stop:
                final_r = (working_stop - entry) / risk
                if partial_done:
                    realized_r = PARTIAL_FRAC * PARTIAL_R + (1.0 - PARTIAL_FRAC) * final_r
                else:
                    realized_r = final_r
                exit_px, reason = working_stop, "stop"
                break
            if use_partial and not partial_done and h >= partial_level:
                partial_done = True
                if TRAIL_AFTER_PARTIAL:
                    working_stop = max(working_stop, entry)  # breakeven
            if h >= target:
                final_r = (target - entry) / risk
                if partial_done:
                    realized_r = PARTIAL_FRAC * PARTIAL_R + (1.0 - PARTIAL_FRAC) * final_r
                    reason = "partial_target"
                else:
                    realized_r = final_r
                    reason = "target"
                exit_px = target
                break
            # time stop: never reached +0.5R
            if use_partial and held >= TIME_STOP_BARS and (mfe / risk) < TIME_STOP_MIN_R and not partial_done:
                final_r = (c - entry) / risk
                realized_r = final_r
                exit_px, reason = c, "time_stop"
                break
        else:
            fav = entry - l
            adv = h - entry
            mfe = max(mfe, fav)
            mae = max(mae, adv)
            if h >= working_stop:
                final_r = (entry - working_stop) / risk
                if partial_done:
                    realized_r = PARTIAL_FRAC * PARTIAL_R + (1.0 - PARTIAL_FRAC) * final_r
                else:
                    realized_r = final_r
                exit_px, reason = working_stop, "stop"
                break
            if use_partial and not partial_done and l <= partial_level:
                partial_done = True
                if TRAIL_AFTER_PARTIAL:
                    working_stop = min(working_stop, entry)
            if l <= target:
                final_r = (entry - target) / risk
                if partial_done:
                    realized_r = PARTIAL_FRAC * PARTIAL_R + (1.0 - PARTIAL_FRAC) * final_r
                    reason = "partial_target"
                else:
                    realized_r = final_r
                    reason = "target"
                exit_px = target
                break
            if use_partial and held >= TIME_STOP_BARS and (mfe / risk) < TIME_STOP_MIN_R and not partial_done:
                final_r = (entry - c) / risk
                realized_r = final_r
                exit_px, reason = c, "time_stop"
                break
        exit_px = c
    else:
        # EOD
        if setup.side == "long":
            final_r = (exit_px - entry) / risk
        else:
            final_r = (entry - exit_px) / risk
        if partial_done:
            realized_r = PARTIAL_FRAC * PARTIAL_R + (1.0 - PARTIAL_FRAC) * final_r
            reason = "eod_partial"
        else:
            realized_r = final_r
            reason = "eod"

    r_mult = realized_r
    pnl_pct = r_mult * risk_pct  # R * risk% ≈ return %
    cost = COST["crypto" if _is_crypto(setup.ticker) else "equity"] * 100.0  # bps as %
    pnl_net = pnl_pct - cost

    return TradeResult(
        source=setup.source,
        ticker=setup.ticker,
        side=setup.side,
        signal=setup.signal,
        grade=setup.grade,
        edge=setup.edge,
        entry=round(entry, 6),
        stop=round(stop, 6),
        target=round(target, 6),
        exit=round(exit_px, 6),
        exit_reason=reason,
        r_multiple=round(r_mult, 3),
        pnl_pct=round(pnl_pct, 4),
        pnl_pct_net=round(pnl_net, 4),
        mfe_r=round(mfe / risk, 3),
        mae_r=round(mae / risk, 3),
        bars_held=held,
        risk_pct=round(risk_pct, 4),
        plan_rr=setup.rr_plan,
        provider=setup.bar_provider,
        note=setup.note,
        model=model,
    )


def _summary(results: List[TradeResult]) -> Dict[str, Any]:
    valid = [r for r in results if r.r_multiple is not None]
    if not valid:
        return {"n": 0}
    rs = [r.r_multiple for r in valid]
    wins = [r for r in valid if r.r_multiple > 0]
    losses = [r for r in valid if r.r_multiple <= 0]
    by_reason: Dict[str, int] = {}
    for r in valid:
        by_reason[r.exit_reason] = by_reason.get(r.exit_reason, 0) + 1
    by_source: Dict[str, Any] = {}
    for src in sorted({r.source for r in valid}):
        sub = [r for r in valid if r.source == src]
        srs = [r.r_multiple for r in sub]
        by_source[src] = {
            "n": len(sub),
            "win_rate": round(sum(1 for x in srs if x > 0) / len(srs), 3),
            "avg_r": round(float(np.mean(srs)), 3),
            "sum_r": round(float(np.sum(srs)), 3),
            "targets": sum(1 for r in sub if r.exit_reason == "target"),
            "stops": sum(1 for r in sub if r.exit_reason == "stop"),
            "eod": sum(1 for r in sub if r.exit_reason == "eod"),
        }
    by_grade: Dict[str, Any] = {}
    for g in sorted({r.grade for r in valid}):
        sub = [r for r in valid if r.grade == g]
        srs = [r.r_multiple for r in sub]
        by_grade[g] = {
            "n": len(sub),
            "win_rate": round(sum(1 for x in srs if x > 0) / len(srs), 3),
            "avg_r": round(float(np.mean(srs)), 3),
        }
    return {
        "n": len(valid),
        "win_rate": round(len(wins) / len(valid), 3),
        "avg_r": round(float(np.mean(rs)), 3),
        "median_r": round(float(np.median(rs)), 3),
        "sum_r": round(float(np.sum(rs)), 3),
        "expectancy_r": round(float(np.mean(rs)), 3),
        "avg_win_r": round(float(np.mean([r.r_multiple for r in wins])), 3) if wins else None,
        "avg_loss_r": round(float(np.mean([r.r_multiple for r in losses])), 3) if losses else None,
        "by_exit": by_reason,
        "by_source": by_source,
        "by_grade": by_grade,
        "avg_mfe_r": round(float(np.mean([r.mfe_r for r in valid if r.mfe_r is not None])), 3),
        "avg_mae_r": round(float(np.mean([r.mae_r for r in valid if r.mae_r is not None])), 3),
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Backtest scan setups")
    ap.add_argument("--model", choices=("classic", "partial_trail"), default="partial_trail")
    ap.add_argument("--grade-min", default="", help="Optional grade floor e.g. A")
    ap.add_argument("--blue-only", action="store_true")
    args = ap.parse_args()

    now = datetime.now(ET)
    print("=" * 72)
    print("  BACKTEST — all scan setups ·", now.strftime("%Y-%m-%d %H:%M %Z"))
    print("=" * 72)
    print(f"  Method: free multi-provider · model={args.model} · cost haircut")
    print("  Sources: VWAP Blue · VWAP One · live last · idiot-flow (if present)")
    print("=" * 72)

    tickers = load_universe(max_n=24)
    # ensure common names
    for t in ["AAPL", "MSFT", "NVDA", "AMD", "SPY", "QQQ", "BTC-USD", "ETH-USD", "META", "GOOGL", "TSLA", "SMH", "COIN"]:
        if t not in tickers:
            tickers.append(t)
    tickers = list(dict.fromkeys(tickers))[:28]

    print(f"\n  Universe: {len(tickers)} tickers")
    t0 = time.time()

    # 1) live API snapshots
    live_setups: List[Setup] = []
    live_setups += _fetch_live_last("http://127.0.0.1:8795/api/last", "vwap_desk_live")
    live_setups += _fetch_live_last("http://127.0.0.1:8791/api/last", "vwap_blue_live")
    live_setups += _fetch_live_last("http://127.0.0.1:8787/api/last", "vwap_one_live")
    print(f"  Live API setups: {len(live_setups)}")

    # 2) fresh dual engines on same bar batch
    print("  Fetching bars (hybrid free rotate)…")
    bars, daily, bar_prov, live, qmeta = batch_fetch(tickers, force=True, mode="hybrid")
    print(f"  Bars ready: {sum(1 for t in tickers if t in bars)} / {len(tickers)}  ({time.time()-t0:.1f}s)")

    blue_setups: List[Setup] = []
    for t in tickers:
        row = blue_analyze(
            t, bars.get(t), daily.get(t),
            live_price=live.get(t),
            bar_provider=bar_prov.get(t),
            quote_provider=(qmeta.get(t) or {}).get("provider"),
        )
        if row.get("error") or not row.get("entry") or not row.get("stop") or not row.get("target"):
            continue
        if row.get("side") not in ("long", "short"):
            continue
        if row.get("signal") in ("FLAT", "WATCH") and not row.get("actionable"):
            # still include WATCH with no levels? skip
            if not row.get("entry"):
                continue
        blue_setups.append(
            Setup(
                source="vwap_blue",
                ticker=t,
                side=row["side"],
                signal=str(row.get("signal")),
                grade=str(row.get("grade") or "–"),
                edge=float(row.get("edge") or 0),
                entry=float(row["entry"]),
                stop=float(row["stop"]),
                target=float(row["target"]),
                bar_provider=row.get("bar_provider"),
                note=str(row.get("state") or row.get("regime") or ""),
                rr_plan=float(row["rr"]) if row.get("rr") is not None else None,
                regime=str(row.get("regime") or ""),
                setup_mode=str(row.get("setup_mode") or ""),
            )
        )
    print(f"  VWAP Blue setups with levels: {len(blue_setups)}")

    one_setups = _setups_from_one(tickers, bars, daily, live, bar_prov, qmeta)
    print(f"  VWAP One setups with levels: {len(one_setups)}")

    if_setups = _idiot_flow_setups()
    print(f"  idiot-flow setups: {len(if_setups)}")

    # merge / de-dupe key = source|ticker|side|entry rounded
    if args.blue_only:
        all_setups = blue_setups
    else:
        all_setups = blue_setups + one_setups + live_setups + if_setups
    seen = set()
    unique: List[Setup] = []
    for s in all_setups:
        key = (s.source, s.ticker, s.side, round(s.entry, 2), round(s.stop, 2))
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)

    if args.grade_min:
        gm = args.grade_min.upper()
        rank = {"A": 5, "LA": 4, "B": 3, "LB": 2, "C": 1, "✕": 0, "–": -1}
        floor = rank.get(gm, 5)
        unique = [s for s in unique if rank.get(s.grade, -1) >= floor]

    print(f"  Unique setups to simulate: {len(unique)}  (grade_min={args.grade_min or 'none'})")

    # For live/idiot tickers not in bars map, fetch
    need = [s.ticker for s in unique if s.ticker not in bars]
    if need:
        print(f"  Fetching {len(set(need))} extra tickers…")
        b2, d2, bp2, lv2, qm2 = batch_fetch(list(set(need)), force=True, mode="hybrid")
        bars.update(b2)
        daily.update(d2)

    results: List[TradeResult] = []
    for s in unique:
        results.append(simulate(s, bars.get(s.ticker), model=args.model))

    summary = _summary(results)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    out_json = OUT_DIR / f"backtest_scans_{stamp}.json"
    out_csv = OUT_DIR / f"backtest_scans_{stamp}.csv"

    payload = {
        "asof": now.isoformat(),
        "method": {
            "entry": "engine entry (or first touch bar)",
            "exit": args.model,
            "partial_r": PARTIAL_R if args.model == "partial_trail" else None,
            "time_stop_bars": TIME_STOP_BARS if args.model == "partial_trail" else None,
            "cost_pct": COST,
            "same_bar_rule": "stop before target (conservative)",
            "data": "free multi-provider hybrid (Yahoo/venues)",
            "grade_min": args.grade_min or None,
        },
        "summary": summary,
        "setups": [asdict(s) for s in unique],
        "results": [asdict(r) for r in results],
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # CSV
    import csv

    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "source", "ticker", "side", "signal", "grade", "edge",
                "entry", "stop", "target", "exit", "exit_reason",
                "r_multiple", "pnl_pct", "pnl_pct_net", "mfe_r", "mae_r",
                "bars_held", "risk_pct", "plan_rr", "provider", "note", "model",
            ],
        )
        w.writeheader()
        for r in results:
            w.writerow(asdict(r))

    # ── report ──
    print("\n" + "=" * 72)
    print("  RESULTS")
    print("=" * 72)
    if summary.get("n", 0) == 0:
        print("  No valid trades simulated (no leveled setups or no bars).")
        return 1

    print(f"  N setups:     {summary['n']}  ← thin n is honest noise; do not over-read expectancy")
    print(f"  Win rate:     {summary['win_rate']*100:.1f}%  (n={summary['n']})")
    print(f"  Avg R:        {summary['avg_r']:+.3f}")
    print(f"  Median R:     {summary['median_r']:+.3f}")
    print(f"  Sum R:        {summary['sum_r']:+.3f}")
    print(f"  Expectancy R: {summary['expectancy_r']:+.3f}  (mean R · n={summary['n']})")
    print(f"  Avg win R:    {summary.get('avg_win_r')}")
    print(f"  Avg loss R:   {summary.get('avg_loss_r')}")
    print(f"  Avg MFE R:    {summary.get('avg_mfe_r')}  ·  Avg MAE R: {summary.get('avg_mae_r')}")
    print(f"  Exits:        {summary.get('by_exit')}")

    print("\n  BY SOURCE:")
    for src, st in (summary.get("by_source") or {}).items():
        print(
            f"    {src:16s} n={st['n']:3d}  win={st['win_rate']*100:5.1f}%  "
            f"avgR={st['avg_r']:+.3f}  sumR={st['sum_r']:+.3f}  "
            f"tgt={st['targets']} stop={st['stops']} eod={st['eod']}"
        )

    print("\n  BY GRADE (Blue):")
    for g, st in (summary.get("by_grade") or {}).items():
        print(f"    {g:4s} n={st['n']:3d}  win={st['win_rate']*100:5.1f}%  avgR={st['avg_r']:+.3f}")

    print("\n  TOP / BOTTOM (by R):")
    ranked = sorted([r for r in results if r.r_multiple is not None], key=lambda x: x.r_multiple, reverse=True)
    for r in ranked[:8]:
        print(
            f"    + {r.source:14s} {r.ticker:8s} {r.side:5s} {r.grade:3s} "
            f"R={r.r_multiple:+.2f}  {r.exit_reason:6s}  {r.signal}"
        )
    for r in ranked[-5:]:
        if r in ranked[:8]:
            continue
        print(
            f"    - {r.source:14s} {r.ticker:8s} {r.side:5s} {r.grade:3s} "
            f"R={r.r_multiple:+.2f}  {r.exit_reason:6s}  {r.signal}"
        )

    print("\n  DETAIL (all):")
    print(f"  {'SRC':14s} {'TK':8s} {'SIDE':5s} {'GR':3s} {'SIG':12s} {'R':>7s} {'EXIT':6s} {'PNL%':>8s}")
    for r in ranked:
        print(
            f"  {r.source:14s} {r.ticker:8s} {r.side:5s} {r.grade:3s} {r.signal[:12]:12s} "
            f"{r.r_multiple:+7.2f} {r.exit_reason:6s} {r.pnl_pct_net:+8.3f}"
        )

    print("\n  Wrote:")
    print(f"    {out_json}")
    print(f"    {out_csv}")
    print("\n  Caveats: free delayed data · stop-before-target same bar · not walk-forward over historical days.")
    print("  Research only — not trade advice.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
