#!/usr/bin/env python3
"""
tape_filters_check.py — replays 2–5 of research/notes/2026-11-02_external_research.md on the two-year 5-minute
bars, built like orb_rvol_check.py (replay 1): the paper's universe (ATR14 ≥ $0.50, price > $5), ATR14 and RVOL
from prior sessions only, fills on the bar after the decision with slippage against the trade, R net of the
desk's cost (honest.net_r), every cell beside its mirror (the same bracket the other way from the same bar) and
with session-clustered error (honest.clustered).

  2  gap  Gap/ATR × opening RVOL. ATR14 stands in for the implied daily move. Decision at the 09:30 bar's close
          (its volume is the RVOL), fill at the 09:35 open, in the direction of the gap; stop 0.5 × ATR14 from the
          fill, no target, flat at the 15:25 bar's close. Mirror = the gap fade.
          Cells: |gap|/ATR {<0.5, 0.5–1, 1–2, ≥2} × RVOL {<1, 1–3, ≥3}.
  3  spy  Opening-vol gate + SPY sign. σ₁ = SPY realized vol 09:30–10:00 (six 5-minute log returns), ranked against
          its own prior 60 sessions (≥ 20 needed) into terciles; m₁₀ = SPY 10:00 / prior close − 1. The trade is each
          name's own first half-hour (10:00 vs its prior close), filled at the 10:00 bar's open, stop 0.5 × ATR14,
          flat 15:25. Cells: σ₁ tercile {low, mid, high} × {with, against} the sign of m₁₀.
  4  breadth  Breadth at the trigger. The trade is replay 1's opening-range continuation with the range stop.
          AD = (names above − names below their prior close) / names, at the close of the bar before the fill
          (the last thing known when the stop order fills), the name itself left out.
          Cells: |AD| {<0.3, 0.3–0.6, ≥0.6} × {with, against} the sign of AD. The mirror of "against" is the
          fade that leans with the tape, and of "with" the counter-trend fade.
  5  fade The fade's last test. |gap| 0.15–0.5 × ATR14 and RVOL < 1: fade at the 09:35 open, target the prior
          close, stop 0.5 × ATR14 beyond the 09:30 open, flat at 12:00 (the 11:55 bar's close). One cell is the
          hypothesis; three neighbours (RVOL ≥ 1, |gap| 0.5–1) are printed as context.

Stops fill at the stop or a worse open, with slippage; a bar that touches stop and target is a stop; targets fill
at the target. `multiple_comparisons` in every result counts the cells looked at and gives the Bonferroni |t|.

    python3 tape_filters_check.py --bars data/backtests/alpaca_5m_2y.pkl --out-dir research --tag alpaca_2y
    python3 tape_filters_check.py --bars data/backtests/alpaca_5m_2y.pkl --replay 5
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import orb_rvol_check as orb  # noqa: E402
from honest import t_critical  # noqa: E402
from orb_rvol_check import ATR_N, FLAT_M, MIN_ATR, MIN_PRICE, OPEN_M, RVOL_MIN_N, RVOL_N, r_of, stat  # noqa: E402

STOP_ATR = 0.5                       # replays 2, 3, 5: wide enough to sit outside 5-minute noise (replay 1's lesson)
HALF_HOUR_M, LAST_ENTRY_M = 600, 630  # 10:00 decision; no entry after 10:30
NOON_FLAT_M = 715                    # the 11:55 bar closes at 12:00
SIGMA_N, SIGMA_MIN_N = 60, 20
GAP_BUCKETS = [("<0.5", 0.0, 0.5), ("0.5–1", 0.5, 1.0), ("1–2", 1.0, 2.0), ("≥2", 2.0, math.inf)]
RVOL3 = [("<1", 0.0, 1.0), ("1–3", 1.0, 3.0), ("≥3", 3.0, math.inf)]
AD_BUCKETS = [("<0.3", 0.0, 0.3), ("0.3–0.6", 0.3, 0.6), ("≥0.6", 0.6, math.inf)]
FADE_GAP = [("0.15–0.5", 0.15, 0.5), ("0.5–1", 0.5, 1.0)]
FADE_RVOL = [("<1", 0.0, 1.0), ("≥1", 1.0, math.inf)]
REPLAYS = {2: "gap", 3: "spy", 4: "breadth", 5: "fade"}


def in_bucket(x: Optional[float], buckets) -> Optional[str]:
    if x is None:
        return None
    for name, lo, hi in buckets:
        if lo <= x < hi:
            return name
    return None


# ── sessions with prior-only ATR and RVOL ───────────────────────────────────────────────────────
def session_inputs(parsed, first) -> Iterator[Tuple[str, List[Dict[str, Any]], Optional[float], Optional[float], Optional[float]]]:
    """(session, RTH bars, ATR14, opening RVOL, prior close), ATR and RVOL from the sessions before this one."""
    trs: List[float] = []
    open_vols: List[float] = []
    prev_close: Optional[float] = None
    for d, day in orb.day_slices(parsed, first):
        atr = sum(trs[-ATR_N:]) / ATR_N if len(trs) >= ATR_N else None
        base = [v for v in open_vols[-RVOL_N:] if v > 0]
        ob = orb.opening_bar(day)
        rvol = (ob["v"] / (sum(base) / len(base))) if (ob and len(base) >= RVOL_MIN_N and ob["v"] > 0) else None
        yield d, day, atr, rvol, prev_close
        trs.append(orb.true_range(day, prev_close))
        if ob:
            open_vols.append(ob["v"])
        prev_close = day[-1]["c"]


def tradable(day, atr: Optional[float], prev_close: Optional[float]) -> bool:
    ob = orb.opening_bar(day)
    return bool(ob and atr and atr >= MIN_ATR and ob["o"] > MIN_PRICE and prev_close)


# ── the bracket walk ────────────────────────────────────────────────────────────────────────────
def slipped(px: float, side: str, slip_bps: float, opening: bool) -> float:
    """A market fill, slippage against the trade: a long buys higher and sells lower."""
    worse_up = (side == "long") == opening
    return px * (1 + slip_bps / 1e4) if worse_up else px * (1 - slip_bps / 1e4)


def walk_bracket(day, k: int, side: str, entry: float, stop: float, target: Optional[float], flat_m: int,
                 slip_bps: float) -> Tuple[float, str]:
    """
    A position opened at the open of bar k. From bar k on: the stop (at the stop or a worse open, slipped) is
    checked before the target (filled at the target); else the close of the first bar at or after flat_m, slipped.
    """
    last = entry
    for b in day[k:]:
        if side == "long":
            if b["l"] <= stop:
                return slipped(min(stop, b["o"]), side, slip_bps, False), "stop"
            if target is not None and b["h"] >= target:
                return target, "target"
        else:
            if b["h"] >= stop:
                return slipped(max(stop, b["o"]), side, slip_bps, False), "stop"
            if target is not None and b["l"] <= target:
                return target, "target"
        last = b["c"]
        if b["mins"] >= flat_m:
            break
    return slipped(last, side, slip_bps, False), "flat"


def paired(ticker: str, session: str, day, k: int, side: str, risk: float, target_dist: Optional[float], flat_m: int,
           slip_bps: float, stop: Optional[float] = None) -> Dict[str, Any]:
    """The trade and its mirror from the open of bar k: same stop and target distances, other side, own slippage."""
    raw = day[k]["o"]
    out: Dict[str, Any] = {"ticker": ticker, "session": session, "side": side, "fill_time": day[k]["time"]}
    for key, s in (("", side), ("mirror_", "short" if side == "long" else "long")):
        sign = 1 if s == "long" else -1
        entry = slipped(raw, s, slip_bps, True)
        st = stop if (stop is not None and key == "") else entry - sign * risk
        r = abs(entry - st)
        tgt = None if target_dist is None else entry + sign * target_dist
        exit_px, reason = walk_bracket(day, k, s, entry, st, tgt, flat_m, slip_bps)
        out[key + "r_net"] = round(r_of(ticker, s, entry, exit_px, r), 4)
        out[key + "reason"] = reason
        if key == "":
            out.update(entry=round(entry, 4), stop=round(st, 4), risk=round(r, 4), exit=round(exit_px, 4))
    return out


# ── replay 2: gap continuation ──────────────────────────────────────────────────────────────────
def gap_trade(ticker, session, day, atr, rvol, prev_close, slip_bps: float) -> Optional[Dict[str, Any]]:
    if not tradable(day, atr, prev_close) or len(day) < 2 or day[1]["mins"] > LAST_ENTRY_M:
        return None
    gap = day[0]["o"] - prev_close
    if gap == 0:
        return None
    t = paired(ticker, session, day, 1, "long" if gap > 0 else "short", STOP_ATR * atr, None, FLAT_M, slip_bps)
    t.update(gap_atr=round(gap / atr, 3), rvol=None if rvol is None else round(rvol, 3))
    return t


# ── replay 3: the first half-hour, gated by SPY ─────────────────────────────────────────────────
def half_hour_index(day) -> Optional[int]:
    """Index of the 10:00 entry bar: the first bar at or after 10:00 and by 10:30, with a bar before it."""
    for k, b in enumerate(day):
        if b["mins"] >= HALF_HOUR_M:
            return k if (k > 0 and b["mins"] <= LAST_ENTRY_M) else None
    return None


def half_hour_trade(ticker, session, day, atr, prev_close, slip_bps: float) -> Optional[Dict[str, Any]]:
    if not tradable(day, atr, prev_close):
        return None
    k = half_hour_index(day)
    if k is None:
        return None
    move = day[k - 1]["c"] - prev_close            # the name's own m10, known at 10:00
    if move == 0:
        return None
    t = paired(ticker, session, day, k, "long" if move > 0 else "short", STOP_ATR * atr, None, FLAT_M, slip_bps)
    t["own_m10_atr"] = round(move / atr, 3)
    return t


def opening_sigma(day) -> Optional[float]:
    """Realized vol 09:30–10:00: root sum of squared 5-minute log returns, the 09:30 bar's from its own open."""
    bars = [b for b in day if OPEN_M <= b["mins"] < HALF_HOUR_M]
    if len(bars) < 5 or bars[0]["mins"] != OPEN_M:
        return None
    prev, ss = bars[0]["o"], 0.0
    for b in bars:
        ss += math.log(b["c"] / prev) ** 2
        prev = b["c"]
    return math.sqrt(ss)


def spy_context(parsed, first) -> Dict[str, Dict[str, Any]]:
    """session → {sigma, pct (rank against the prior 60 sessions), tercile, m10, sign}; all known at 10:00."""
    out: Dict[str, Dict[str, Any]] = {}
    hist: List[float] = []
    for d, day, _atr, _rvol, prev_close in session_inputs(parsed, first):
        sig = opening_sigma(day)
        k = half_hour_index(day)
        if sig is None or k is None or not prev_close:
            continue
        base = hist[-SIGMA_N:]
        pct = sum(1 for x in base if x < sig) / len(base) if len(base) >= SIGMA_MIN_N else None
        m10 = day[k - 1]["c"] / prev_close - 1
        out[d] = {"sigma": sig, "pct": pct, "m10": m10, "sign": (m10 > 0) - (m10 < 0),
                  "tercile": None if pct is None else "low" if pct < 1 / 3 else "high" if pct >= 2 / 3 else "mid"}
        hist.append(sig)
    return out


# ── replay 4: breadth at the trigger ────────────────────────────────────────────────────────────
def breadth_add(acc: Dict[str, Dict[int, List[int]]], session: str, day, prev_close: Optional[float]) -> None:
    """Count this name into the session's per-bar tally [above, below, names] against its prior close."""
    if not prev_close:
        return
    s = acc[session]
    for b in day:
        cell = s.get(b["mins"])
        if cell is None:
            cell = s[b["mins"]] = [0, 0, 0]
        cell[0] += b["c"] > prev_close
        cell[1] += b["c"] < prev_close
        cell[2] += 1


def breadth_trade(ticker, session, day, atr, rvol, prev_close, slip_bps: float) -> Optional[Dict[str, Any]]:
    """Replay 1's continuation with the range stop, tagged with the bar whose close is the last known tape."""
    if not atr or not prev_close:
        return None
    t = orb.session_trade(ticker, session, day, atr, rvol, prev_close, slip_bps, stop_mode="range")
    if not t:
        return None
    k = next(i for i, b in enumerate(day) if b["time"] == t["fill_time"])
    trig = day[k - 1]
    return {**t, "trig_min": trig["mins"], "own_sign": (trig["c"] > prev_close) - (trig["c"] < prev_close)}


def ad_without(cell: Optional[List[int]], own_sign: int) -> Optional[float]:
    """(above − below) / names with this name taken out; None when fewer than 20 other names printed that bar."""
    if not cell:
        return None
    up, dn, n = cell[0] - (own_sign > 0), cell[1] - (own_sign < 0), cell[2] - 1
    return (up - dn) / n if n >= 20 else None


# ── replay 5: the small-gap, low-RVOL fade ──────────────────────────────────────────────────────
def fade_trade(ticker, session, day, atr, rvol, prev_close, slip_bps: float) -> Optional[Dict[str, Any]]:
    if not tradable(day, atr, prev_close) or len(day) < 2 or day[1]["mins"] > LAST_ENTRY_M:
        return None
    op = day[0]["o"]
    gap = op - prev_close
    if gap == 0:
        return None
    side = "short" if gap > 0 else "long"
    sign = 1 if side == "long" else -1
    entry = slipped(day[1]["o"], side, slip_bps, True)
    stop = op - sign * STOP_ATR * atr
    risk, tdist = sign * (entry - stop), sign * (prev_close - entry)
    if risk <= 0 or tdist <= 0:                     # by 09:35 the gap is already closed, or the stop already passed
        return None
    t = paired(ticker, session, day, 1, side, risk, tdist, NOON_FLAT_M, slip_bps, stop=stop)
    t.update(gap_atr=round(gap / atr, 3), rvol=None if rvol is None else round(rvol, 3), rr=round(tdist / risk, 3))
    return t


# ── tables ──────────────────────────────────────────────────────────────────────────────────────
def cell(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    d = [{"session": t["session"], "d": t["r_net"] - t["mirror_r_net"]} for t in rows]
    return {"trade": stat(rows, "r_net"), "mirror": stat(rows, "mirror_r_net"), "direction": stat(d, "d"),
            "stop_share": round(sum(1 for t in rows if t["reason"] == "stop") / len(rows), 3) if rows else None}


def grid(trades: List[Dict[str, Any]], row_of: Callable, rows: List[str], col_of: Callable, cols: List[str]) -> Dict[str, Any]:
    """rows × cols cells keyed "row | col", plus each row and column margin and "all" (margins are not counted cells)."""
    by: Dict[Tuple[Any, Any], List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by[(row_of(t), col_of(t))].append(t)
    cells = {f"{r} | {c}": cell(by[(r, c)]) for r in rows for c in cols}
    margins = {**{f"{r} | all": cell([t for c in cols for t in by[(r, c)]]) for r in rows},
               **{f"all | {c}": cell([t for r in rows for t in by[(r, c)]]) for c in cols},
               "all": cell([t for r in rows for c in cols for t in by[(r, c)]])}
    return {"cells": cells, "margins": margins}


def bonferroni_t(m: int) -> float:
    """Two-sided 5% family-wise |t| across m cells, each read twice (trade and mirror); normal quantile, since
    every cell here has hundreds of sessions."""
    return round(NormalDist().inv_cdf(1 - 0.025 / max(1, 2 * m)), 2)


def comparisons(cells: Dict[str, Any], family: int) -> Dict[str, Any]:
    """What clears the nominal bar and what clears the bar for every cell examined across the four replays."""
    bar = bonferroni_t(family)

    def hits(key: str, sign: int, crit: Optional[float]) -> List[str]:
        out = []
        for name, c in cells.items():
            s = c[key]
            if s.get("t") is None or s.get("clusters", 0) < 10:
                continue
            if sign * s["t"] >= (crit if crit is not None else t_critical(s["clusters"])):
                out.append(name)
        return out

    return {"cells_this_replay": len(cells), "cells_all_replays": family, "tests_all_replays": 2 * family, "bonferroni_t": bar,
            "trade_positive_nominal": hits("trade", 1, None), "trade_positive_bonferroni": hits("trade", 1, bar),
            "mirror_positive_nominal": hits("mirror", 1, None), "mirror_positive_bonferroni": hits("mirror", 1, bar),
            "direction_nominal": hits("direction", 1, None) + hits("direction", -1, None),
            "direction_bonferroni": hits("direction", 1, bar) + hits("direction", -1, bar)}


def with_or_against(side: str, sign: int) -> Optional[str]:
    if not sign:
        return None
    return "with" if (side == "long") == (sign > 0) else "against"


def tabulate_gap(trades):
    return grid([t for t in trades if t["rvol"] is not None],
                lambda t: in_bucket(abs(t["gap_atr"]), GAP_BUCKETS), [b[0] for b in GAP_BUCKETS],
                lambda t: in_bucket(t["rvol"], RVOL3), [b[0] for b in RVOL3])


def tabulate_spy(trades, ctx):
    rows = [{**t, "tercile": c["tercile"], "rel": with_or_against(t["side"], c["sign"])}
            for t in trades for c in [ctx.get(t["session"])] if c and c["tercile"]]
    return grid(rows, lambda t: t["tercile"], ["low", "mid", "high"], lambda t: t["rel"], ["with", "against"])


def tabulate_breadth(trades, acc):
    rows = []
    for t in trades:
        ad = ad_without((acc.get(t["session"]) or {}).get(t["trig_min"]), t["own_sign"])
        if ad is not None:
            rows.append({**t, "ad": ad, "rel": with_or_against(t["side"], (ad > 0) - (ad < 0))})
    return grid(rows, lambda t: in_bucket(abs(t["ad"]), AD_BUCKETS), [b[0] for b in AD_BUCKETS],
                lambda t: t["rel"], ["with", "against"])


def tabulate_fade(trades):
    return grid([t for t in trades if t["rvol"] is not None],
                lambda t: in_bucket(abs(t["gap_atr"]), FADE_GAP), [b[0] for b in FADE_GAP],
                lambda t: in_bucket(t["rvol"], FADE_RVOL), [b[0] for b in FADE_RVOL])


# ── the study ───────────────────────────────────────────────────────────────────────────────────
def frames(bars_pkl: Path, max_tickers: Optional[int] = None) -> Iterator[Tuple[str, List[Dict[str, Any]], Dict[str, int]]]:
    """One ticker at a time (ticker, parsed bars, {ET day → first index}); SPY first, frames freed as they go."""
    import replay_sessions as rs
    bars = pickle.load(open(bars_pkl, "rb")).get("bars") or {}
    prep, _ = rs.engine_api()
    names = [t for t in bars if not t.endswith("-USD")][: max_tickers or None]
    if "SPY" in bars and "SPY" not in names:
        names.append("SPY")
    names.sort(key=lambda t: t != "SPY")
    for t in names:
        df = bars.pop(t)
        if df is None or df.empty:
            continue
        p = prep(df)
        first: Dict[str, int] = {}
        for i, b in enumerate(p):
            first.setdefault(b["d"], i)
        yield t, p, first


def study(source, slip_bps: float = 2.0, which=(2, 3, 4, 5)) -> Dict[int, Dict[str, Any]]:
    """source yields (ticker, parsed, first). Returns {replay number → result}, each with its cells and counts."""
    trades: Dict[int, List[Dict[str, Any]]] = {n: [] for n in which}
    acc: Dict[str, Dict[int, List[int]]] = defaultdict(dict)
    ctx: Dict[str, Dict[str, Any]] = {}
    tickers = 0
    for ticker, parsed, first in source:
        tickers += 1
        if ticker == "SPY":
            ctx = spy_context(parsed, first)
        for d, day, atr, rvol, prev_close in session_inputs(parsed, first):
            if 4 in which:
                breadth_add(acc, d, day, prev_close)
            for n, t in ((2, gap_trade(ticker, d, day, atr, rvol, prev_close, slip_bps) if 2 in which else None),
                         (3, half_hour_trade(ticker, d, day, atr, prev_close, slip_bps) if 3 in which and ticker != "SPY" else None),
                         (4, breadth_trade(ticker, d, day, atr, rvol, prev_close, slip_bps) if 4 in which else None),
                         (5, fade_trade(ticker, d, day, atr, rvol, prev_close, slip_bps) if 5 in which else None)):
                if t:
                    trades[n].append(t)
    tables = {2: lambda: tabulate_gap(trades[2]), 3: lambda: tabulate_spy(trades[3], ctx),
              4: lambda: tabulate_breadth(trades[4], acc), 5: lambda: tabulate_fade(trades[5])}
    grids = {n: tables[n]() for n in which}
    family = sum(len(g["cells"]) for g in grids.values())
    out = {}
    for n in which:
        sessions = sorted({t["session"] for t in trades[n]})
        out[n] = {"asof": date.today().isoformat(), "replay": n, "name": REPLAYS[n], "tickers": tickers, "sessions": len(sessions),
                  "window": [sessions[0], sessions[-1]] if sessions else None, "slip_bps": slip_bps, "n": len(trades[n]),
                  "rule": {"stop_atr": STOP_ATR if n != 4 else "opening range, ≥ 0.10 ATR", "flat_min": NOON_FLAT_M if n == 5 else FLAT_M,
                           "min_atr": MIN_ATR, "min_price": MIN_PRICE, "atr_n": ATR_N, "rvol_n": RVOL_N},
                  **grids[n], "multiple_comparisons": comparisons(grids[n]["cells"], family), "trades": trades[n]}
        if n == 5:
            out[n]["hypothesis_cell"] = "0.15–0.5 | <1"
        if n == 3:
            out[n]["spy_sessions"] = sum(1 for c in ctx.values() if c["tercile"])
    return out


def run(bars_pkl: Path, slip_bps: float = 2.0, max_tickers: Optional[int] = None, which=(2, 3, 4, 5)) -> Dict[int, Dict[str, Any]]:
    res = study(frames(bars_pkl, max_tickers), slip_bps, which)
    for r in res.values():
        r["bars"] = str(bars_pkl)
    return res


def report(res: Dict[str, Any]) -> str:
    mc = res["multiple_comparisons"]
    lines = [f"replay {res['replay']} ({res['name']}) · {res['tickers']} names · {res['sessions']} sessions {res['window']} · {res['n']} trades"]
    for k, v in {**res["cells"], **res["margins"]}.items():
        lines.append(f"    {k:18s} trade  {orb.fmt(v['trade'])}")
        lines.append(f"    {'':18s} mirror {orb.fmt(v['mirror'])} · direction t {v['direction'].get('t')} · stop share {v['stop_share']}")
    lines.append(f"  cells: {mc['cells_this_replay']} here, {mc['cells_all_replays']} across the replays run, each read as trade and mirror; Bonferroni |t| ≥ {mc['bonferroni_t']}")
    lines.append(f"  positive after costs, nominal: trade {mc['trade_positive_nominal']} · mirror {mc['mirror_positive_nominal']}")
    lines.append(f"  positive after costs, Bonferroni: trade {mc['trade_positive_bonferroni']} · mirror {mc['mirror_positive_bonferroni']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bars", required=True, type=Path)
    ap.add_argument("--replay", choices=["2", "3", "4", "5", "all"], default="all")
    ap.add_argument("--slip-bps", type=float, default=2.0)
    ap.add_argument("--max-tickers", type=int)
    ap.add_argument("--out-dir", type=Path, help="write replay<N>_<name>_check_<date>[_tag].json here")
    ap.add_argument("--tag", default="")
    ap.add_argument("--keep-trades", action="store_true", help="keep the per-trade lists in the JSON (tens of MB each)")
    a = ap.parse_args()
    which = (2, 3, 4, 5) if a.replay == "all" else (int(a.replay),)
    for n, res in run(a.bars, a.slip_bps, a.max_tickers, which).items():
        print(report(res) + "\n")
        if a.out_dir:
            a.out_dir.mkdir(parents=True, exist_ok=True)
            if not a.keep_trades:
                res = {**{k: v for k, v in res.items() if k != "trades"}, "trades_note": f"{len(res['trades'])} trades omitted"}
            p = a.out_dir / f"replay{n}_{REPLAYS[n]}_check_{res['asof']}{'_' + a.tag if a.tag else ''}.json"
            p.write_text(json.dumps(res, indent=1))
            print(f"  wrote {p}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
