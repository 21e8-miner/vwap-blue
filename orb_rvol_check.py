#!/usr/bin/env python3
"""
orb_rvol_check.py — replay 1 of research/notes/2026-11-02_external_research.md: the Zarattini–Barbon–Aziz
opening-range continuation on VWAP Blue's own bars, with the fade as the mirror control.

Rule (decision-time, next-bar fills, flat before the end-of-day reversal window):
  • 09:30 bar sets the range and the direction: close above open → long, below → short, doji → no trade.
  • Relative volume RVOL = that bar's volume / mean of the same bar over the prior 14 sessions (≥ 5 needed).
  • Gap = 09:30 open vs the prior session's last RTH close, in units of ATR14 (daily true range, prior 14 sessions).
  • Entry: a stop order at the 09:30 bar's high (long) / low (short), live until 10:30. A later bar that trades
    through the level fills at max(level, that bar's open) + slippage (gaps through the level fill at the open).
  • Stop: 0.10 × ATR14 from the fill (the paper's stop). No target. Flat at the 15:25 bar's close (end-of-day
    reversal, Baltussen–Da–Soebhag 2025), or at the stop, whichever first. Stop fills at the stop or the bar's open.
  • Mirror: the fade — same fill level and moment, opposite side, same risk. Costs: slippage both ways + the
    desk's round-trip cost in R (honest.net_r), identical for trade and mirror.

Buckets: RVOL {<1, 1–3, 3–10, ≥10} × gap gate {all, |gap| ≥ 0.7 ATR}. Stats are session-clustered (honest.clustered).
Literature prediction: ≈ −0.02R below RVOL 1, +0.08R above 1, +0.3R and up above 10 (net of commissions, no slippage).

    python3 orb_rvol_check.py --bars data/backtests/holdout_60d_2026-10-19.pkl --out research/orb_rvol_check_<date>.json
    python3 orb_rvol_check.py --bars data/backtests/alpaca_5m_2y.pkl          # after alpaca_bars.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from honest import clustered, net_r, verdict  # noqa: E402
from moontower_deeper_check import session_frames  # noqa: E402

OPEN_M, LAST_FILL_M, FLAT_M, RTH_CLOSE_M = 570, 630, 925, 960
ATR_N, RVOL_N, RVOL_MIN_N = 14, 14, 5
STOP_ATR = 0.10
GAP_ATR_GATE = 0.7
RVOL_BUCKETS = [("<1", 0.0, 1.0), ("1–3", 1.0, 3.0), ("3–10", 3.0, 10.0), ("≥10", 10.0, float("inf"))]


# ── session arithmetic ──────────────────────────────────────────────────────────────────────────
def rth(day: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The regular-hours bars of one ET day (09:30 ≤ bar start < 16:00)."""
    return [b for b in day if OPEN_M <= b["mins"] < RTH_CLOSE_M]


def day_slices(parsed: List[Dict[str, Any]], first: Dict[str, int]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    days = list(first)
    out = []
    for j, d in enumerate(days):
        end = first[days[j + 1]] if j + 1 < len(days) else len(parsed)
        day = rth(parsed[first[d]: end])
        if day:
            out.append((d, day))
    return out


def true_range(day: List[Dict[str, Any]], prev_close: Optional[float]) -> float:
    hi, lo = max(b["h"] for b in day), min(b["l"] for b in day)
    if prev_close is None:
        return hi - lo
    return max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))


def opening_bar(day: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    b = day[0]
    return b if b["mins"] == OPEN_M else None


# ── one session's trade ─────────────────────────────────────────────────────────────────────────
def fill_after(day: List[Dict[str, Any]], side: str, level: float, slip_bps: float) -> Optional[Tuple[int, float]]:
    """First bar after the opening bar, up to 10:30, that trades through the level. Returns (index, fill)."""
    for k in range(1, len(day)):
        b = day[k]
        if b["mins"] > LAST_FILL_M:
            return None
        if side == "long" and b["h"] >= level:
            px = max(level, b["o"])
            return k, px * (1 + slip_bps / 1e4)
        if side == "short" and b["l"] <= level:
            px = min(level, b["o"])
            return k, px * (1 - slip_bps / 1e4)
    return None


def walk(day: List[Dict[str, Any]], k: int, side: str, entry: float, stop: float, slip_bps: float) -> Tuple[float, str, int]:
    """From the bar after the fill: stop (at the stop or a worse open) or the 15:25 close. Returns (exit, reason, bars)."""
    exit_px, reason, held = entry, "flat", 0
    for j in range(k + 1, len(day)):
        b = day[j]
        held += 1
        if side == "long" and b["l"] <= stop:
            return min(stop, b["o"]) * (1 - slip_bps / 1e4), "stop", held
        if side == "short" and b["h"] >= stop:
            return max(stop, b["o"]) * (1 + slip_bps / 1e4), "stop", held
        exit_px = b["c"]
        if b["mins"] >= FLAT_M:
            return exit_px * ((1 - slip_bps / 1e4) if side == "long" else (1 + slip_bps / 1e4)), "flat", held
    return exit_px, "eod", held


def r_of(ticker: str, side: str, entry: float, exit_px: float, risk: float) -> float:
    gross = (exit_px - entry) if side == "long" else (entry - exit_px)
    stop = entry - risk if side == "long" else entry + risk
    return net_r(gross / risk, ticker, entry, stop)


def session_trade(ticker: str, session: str, day: List[Dict[str, Any]], atr: float, rvol: Optional[float],
                  prev_close: Optional[float], slip_bps: float) -> Optional[Dict[str, Any]]:
    ob = opening_bar(day)
    if ob is None or atr <= 0 or ob["c"] == ob["o"]:
        return None
    side = "long" if ob["c"] > ob["o"] else "short"
    level = ob["h"] if side == "long" else ob["l"]
    got = fill_after(day, side, level, slip_bps)
    if not got:
        return None
    k, entry = got
    risk = STOP_ATR * atr
    stop = entry - risk if side == "long" else entry + risk
    exit_px, reason, held = walk(day, k, side, entry, stop, slip_bps)
    # the mirror: fade the same breakout from the same price, same risk
    mside = "short" if side == "long" else "long"
    mentry = level * ((1 - slip_bps / 1e4) if mside == "short" else (1 + slip_bps / 1e4))
    mstop = mentry + risk if mside == "short" else mentry - risk
    mexit, mreason, _ = walk(day, k, mside, mentry, mstop, slip_bps)
    gap_atr = None if prev_close is None else (ob["o"] - prev_close) / atr
    return {"ticker": ticker, "session": session, "side": side, "fill_time": day[k]["time"], "entry": round(entry, 4),
            "stop": round(stop, 4), "risk": round(risk, 4), "exit": round(exit_px, 4), "reason": reason, "bars_held": held,
            "r_net": round(r_of(ticker, side, entry, exit_px, risk), 4), "mirror_r_net": round(r_of(ticker, mside, mentry, mexit, risk), 4),
            "mirror_reason": mreason, "rvol": None if rvol is None else round(rvol, 3), "gap_atr": None if gap_atr is None else round(gap_atr, 3),
            "atr": round(atr, 4), "range_atr": round((ob["h"] - ob["l"]) / atr, 3)}


# ── the study ───────────────────────────────────────────────────────────────────────────────────
def ticker_trades(ticker: str, parsed, first, slip_bps: float) -> List[Dict[str, Any]]:
    days = day_slices(parsed, first)
    trs: List[float] = []
    open_vols: List[float] = []
    prev_close: Optional[float] = None
    out = []
    for d, day in days:
        atr = sum(trs[-ATR_N:]) / len(trs[-ATR_N:]) if len(trs) >= ATR_N else None
        base = [v for v in open_vols[-RVOL_N:] if v > 0]
        ob = opening_bar(day)
        rvol = (ob["v"] / (sum(base) / len(base))) if (ob and len(base) >= RVOL_MIN_N and ob["v"] > 0) else None
        if atr:
            t = session_trade(ticker, d, day, atr, rvol, prev_close, slip_bps)
            if t:
                out.append(t)
        trs.append(true_range(day, prev_close))
        if ob:
            open_vols.append(ob["v"])
        prev_close = day[-1]["c"]
    return out


def bucket(rvol: Optional[float]) -> Optional[str]:
    if rvol is None:
        return None
    for name, lo, hi in RVOL_BUCKETS:
        if lo <= rvol < hi:
            return name
    return None


def stat(rows: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    s = clustered([t[key] for t in rows], [t["session"] for t in rows])
    s["verdict"] = verdict(s)
    s["wins"] = sum(1 for t in rows if t[key] > 0)
    return s


def tabulate(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for gate_name, gate in (("all", lambda t: True), (f"gap≥{GAP_ATR_GATE}atr", lambda t: t["gap_atr"] is not None and abs(t["gap_atr"]) >= GAP_ATR_GATE)):
        g = [t for t in trades if gate(t)]
        rows = {"all": g}
        for name, _, _ in RVOL_BUCKETS:
            rows[name] = [t for t in g if bucket(t["rvol"]) == name]
        rows["rvol≥1"] = [t for t in g if t["rvol"] is not None and t["rvol"] >= 1]
        rows["rvol≥3"] = [t for t in g if t["rvol"] is not None and t["rvol"] >= 3]
        out[gate_name] = {k: {"trade": stat(v, "r_net"), "mirror": stat(v, "mirror_r_net"),
                              "direction": stat([{**t, "d": t["r_net"] - t["mirror_r_net"], "session": t["session"]} for t in v], "d") if v else stat([], "d"),
                              "stop_share": round(sum(1 for t in v if t["reason"] == "stop") / len(v), 3) if v else None}
                          for k, v in rows.items()}
    return out


def run(bars_pkl: Path, slip_bps: float = 2.0, max_tickers: Optional[int] = None) -> Dict[str, Any]:
    parsed, first = session_frames(bars_pkl)
    tickers = [t for t in parsed if not t.endswith("-USD")][: max_tickers or None]
    trades: List[Dict[str, Any]] = []
    for t in tickers:
        trades.extend(ticker_trades(t, parsed[t], first[t], slip_bps))
    sessions = sorted({t["session"] for t in trades})
    by_side = {s: stat([t for t in trades if t["side"] == s], "r_net") for s in ("long", "short")}
    return {"asof": date.today().isoformat(), "bars": str(bars_pkl), "tickers": len(tickers), "sessions": len(sessions),
            "window": [sessions[0], sessions[-1]] if sessions else None, "slip_bps": slip_bps,
            "rule": {"open_bar_min": OPEN_M, "last_fill_min": LAST_FILL_M, "flat_min": FLAT_M, "stop_atr": STOP_ATR, "atr_n": ATR_N, "rvol_n": RVOL_N},
            "n": len(trades), "with_rvol": sum(1 for t in trades if t["rvol"] is not None),
            "buckets": tabulate(trades), "by_side": by_side, "trades": trades}


def fmt(s: Dict[str, Any]) -> str:
    if not s or s.get("mean") is None:
        return "n=0"
    return f"{s['mean']:+.3f}R ± {s['se']:.3f} (t {s['t']:+.1f}, n {s['n']}, {s['wins']} wins) {s['verdict']}"


def report(res: Dict[str, Any]) -> str:
    lines = [f"ORB continuation vs fade · {res['tickers']} names · {res['sessions']} sessions {res['window']} · {res['n']} trades ({res['with_rvol']} with RVOL)"]
    for gate, rows in res["buckets"].items():
        lines.append(f"\n  gate: {gate}")
        for k, v in rows.items():
            lines.append(f"    RVOL {k:6s} trade {fmt(v['trade'])}")
            lines.append(f"    {'':11s} fade  {fmt(v['mirror'])} · stop share {v['stop_share']}")
    lines.append("\n  by side: " + " · ".join(f"{s} {fmt(v)}" for s, v in res["by_side"].items()))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bars", required=True, type=Path)
    ap.add_argument("--slip-bps", type=float, default=2.0)
    ap.add_argument("--max-tickers", type=int)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    res = run(a.bars, a.slip_bps, a.max_tickers)
    print(report(res))
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(res, indent=1))
        print(f"\n  wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
