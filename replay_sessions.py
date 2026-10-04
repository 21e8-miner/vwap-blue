#!/usr/bin/env python3
"""
Prefix-honest multi-session replay for VWAP Blue.

Unlike backtest_today_scans.py (today's leftover bars, first price-touch entry)
this:

  · fetches the same 5m hybrid book the live desk uses (~1mo free)
  · treats each ET session as its own trade day
  · grades every bar of the session on the prefix ending at that bar, as the desk saw it then,
    and trades the first bar whose row is tradeable with its trigger on that bar (one trade per
    name and session). Until 2026-10-04 the trigger came from the end-of-day row and was then
    re-graded at its bar, which leaked: before 09:30 a gap's fade side is provisional, and an
    end-of-day gap trigger hid an earlier multi-day reverse the desk had shown live
    (causal_check.py replays that search on the same bars)
  · enters on the bar AFTER the trigger (next open + slippage) by default; the trigger-bar
    close the engine prints is not a fillable price on a delayed free feed
    (`--entry trigger_close` reproduces the old optimistic fill)
  · holds to the session's close, no overnight: equities flatten at the 16:00 close and
    after-hours prints fill no entry, stop or target; crypto's session is the whole ET day
    (exit model classic_after_hours keeps the old equity hold, to the last after-hours bar)
  · runs several exit models against the same entries
  · reports R net of round-trip costs (cost / risk, per trade) and a session-clustered
    standard error: trades on the same session share the tape, so 400 trades from 22
    sessions are about 22 independent draws, not 400 (see honest.py)

  python3 replay_sessions.py --max-tickers 96 --grade-min A
  python3 replay_sessions.py --save-bars data/backtests/equity_bars.pkl   # keep the fetch for re-runs
  python3 replay_sessions.py --bars data/backtests/equity_bars.pkl        # replay a cached fetch
  python3 replay_sessions.py --rescore research/replay_2026-08-12.json   # honest stats for a saved run

Research only. Free delayed data. Not trade advice.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import engine
from data import batch_fetch, load_universe, passes_volume_filter
from engine import CRYPTO_CLOSE_M, ENGINE_VERSION, RTH_CLOSE_M
from honest import COST, clustered, cost_r, fmt_stat, net_r, segments, verdict

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "data" / "backtests"
RESEARCH = HERE / "research"

GRADE_OK = {"A", "LA"}

# The engine module replay() grades with; replay_crypto.load_engine swaps in another engine file.
ENGINE = engine


@dataclass
class ExitModel:
    name: str
    partial_r: Optional[float] = None      # take PARTIAL_FRAC at this R
    partial_frac: float = 0.5
    trail_be: bool = False                 # after partial (or after be_after_r)
    be_after_r: Optional[float] = None     # move stop to entry once MFE ≥ this
    giveback: Optional[float] = None       # exit when give-back ≥ this × peak MFE (needs mfe≥0.5R)
    time_stop_bars: Optional[int] = None
    time_stop_min_r: float = 0.5
    flatten_at_r: Optional[float] = None   # full flatten at this R (no runner)
    after_hours: bool = False              # equities: hold past 16:00 to the day's last after-hours bar


MODELS: List[ExitModel] = [
    ExitModel(name="classic"),
    ExitModel(name="partial_trail", partial_r=1.0, trail_be=True, time_stop_bars=48),
    ExitModel(name="full_1R", flatten_at_r=1.0),
    ExitModel(name="full_075R", flatten_at_r=0.75),
    ExitModel(name="be_after_05", be_after_r=0.5),
    ExitModel(name="giveback_50", giveback=0.50),
    ExitModel(name="partial_075_be", partial_r=0.75, trail_be=True),
    ExitModel(name="partial_075_gb50", partial_r=0.75, trail_be=True, giveback=0.50),
    ExitModel(name="time_24", time_stop_bars=24),
    # the replay's equity hold until 2026-10-04 (to ~19:55 ET), kept as a sensitivity; crypto: = classic
    ExitModel(name="classic_after_hours", after_hours=True),
]


@dataclass
class Trade:
    ticker: str
    session: str
    side: str
    signal: str
    grade: str
    grade_eod: str
    edge: float
    regime: str
    setup_mode: str
    ker: Optional[float]
    rvol: Optional[float]
    rvol_n: int
    gap_pct: Optional[float]
    entry: float
    stop: float
    target: float
    rr_plan: Optional[float]
    model: str
    exit: float
    exit_reason: str
    r_multiple: float
    mfe_r: float
    mae_r: float
    bars_held: int
    pnl_pct_net: float
    dollar_vol: float = 0.0
    r_net: float = 0.0          # r_multiple minus round-trip cost expressed in R
    cost_r: float = 0.0
    entry_mode: str = "trigger_close"
    entry_plan: float = 0.0     # the engine's printed entry (trigger-bar close)
    trigger_time: str = ""      # the trigger bar, ET "MM-DD HH:MM"


def _is_crypto(t: str) -> bool:
    t = t.upper()
    return t.endswith(("-USD", "-USDT", "-USDC"))


def _et_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is None:
        try:
            idx = idx.tz_localize("UTC")
        except Exception:
            idx = idx.tz_localize(ET)
    return idx.tz_convert(ET)


def _session_days(df: pd.DataFrame) -> List[date]:
    idx = _et_index(df)
    return sorted(set(idx.date))


def complete_sessions(bars: Dict[str, Any], through: date) -> Dict[str, Any]:
    """Drop every bar after `through` (ET): the replay holds to a session's close, so it must be final."""
    out = {}
    for t, df in bars.items():
        if df is None or df.empty:
            continue
        cut = df.loc[_et_index(df).date <= through]
        if len(cut):
            out[t] = cut
    return out


def engine_api(eng=None) -> Tuple[Callable[..., List[Dict[str, Any]]], Callable[..., Dict[str, Any]]]:
    """
    (prep, grade) for an engine module: prep(df) parses a frame once, grade(ticker, bars, ...) is analyze()
    on bars it parsed. An engine file older than analyze_bars (a paired replay of an old version) gets its
    analyze() handed the parsed bars through _prep_bars.
    """
    eng = eng or ENGINE
    prep = eng._prep_bars
    if hasattr(eng, "analyze_bars"):
        return prep, eng.analyze_bars

    def grade(ticker: str, bars: List[Dict[str, Any]], **kw: Any) -> Dict[str, Any]:
        eng._prep_bars = lambda _df: bars
        try:
            return eng.analyze(ticker, None, **kw)
        finally:
            eng._prep_bars = prep
    return prep, grade


def session_bars(ticker: str, day: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    One ET day's bars up to the replay's flat time. Equities stop at the 16:00 close (the last bar is the
    15:55 bar, whose close is the closing print), so after-hours prints fill no entry, stop or target.
    Crypto's session is the whole ET day (engine v1.5.0).
    """
    close_m = CRYPTO_CLOSE_M if _is_crypto(ticker) else RTH_CLOSE_M
    end = next((i for i, b in enumerate(day) if b["mins"] >= close_m), len(day))
    return day[:end]


def _tradeable(row: Dict[str, Any], grade_min: str) -> bool:
    if row.get("error"):
        return False
    if not row.get("entry") or not row.get("stop") or not row.get("target"):
        return False
    if row.get("side") not in ("long", "short"):
        return False
    if row.get("no_runway") or row.get("bad_geom"):
        return False
    if row.get("trend_block") or row.get("regime_block"):
        return False
    if row.get("thin_rvol"):
        return False
    if row.get("signal") not in ("TRIGGER", "SETUP", "TAGGED"):
        # at the trigger bar the badge should be TRIGGER (or TAGGED if same-bar tag)
        return False
    g = str(row.get("grade") or "–")
    if grade_min.upper() == "A":
        return g in GRADE_OK
    if grade_min.upper() == "B":
        return g in GRADE_OK or g in {"B", "LB"}
    return g not in {"–", "✕"}


def _fill_entry(
    side: str,
    entry_plan: float,
    stop: float,
    target: float,
    day_bars: List[Dict[str, Any]],
    trig_rel: int,
    entry_mode: str = "next_open",
    slip_bps: float = 2.0,
) -> Tuple[Optional[float], str]:
    """
    The price a trader acting on the alert could actually get.
    next_open: open of the bar after the trigger, plus slippage against us. A fill that has
    already gapped through the stop (or the target) is skipped, as a trader would skip it.
    """
    if entry_mode == "trigger_close":
        return entry_plan, "filled"
    if trig_rel + 1 >= len(day_bars):
        return None, "no_next_bar"
    nb = day_bars[trig_rel + 1]
    px = float(nb.get("o") or nb["c"])
    px *= (1 + slip_bps / 1e4) if side == "long" else (1 - slip_bps / 1e4)
    if (side == "long" and px <= stop) or (side == "short" and px >= stop):
        return None, "gapped_through_stop"
    if (side == "long" and px >= target) or (side == "short" and px <= target):
        return None, "gapped_through_target"
    return px, "filled"


def _simulate(
    side: str,
    entry: float,
    stop: float,
    target: float,
    day_bars: List[Dict[str, Any]],
    trig_rel: int,
    model: ExitModel,
    ticker: str,
) -> Tuple[float, str, float, float, float, int]:
    """
    Walk focus-day bars from trig_rel forward (entry is that bar's close).
    Resolution starts on the NEXT bar (same as engine _resolve_open).
    Returns (exit_px, reason, r, mfe_r, mae_r, bars_held).
    """
    risk = abs(entry - stop)
    if risk <= 0 or not day_bars or trig_rel < 0 or trig_rel >= len(day_bars):
        return entry, "invalid", 0.0, 0.0, 0.0, 0

    working_stop = stop
    partial_done = False
    realized = 0.0
    mfe = 0.0
    mae = 0.0
    exit_px = float(day_bars[-1]["c"])
    reason = "eod"
    held = 0

    partial_level = None
    if model.partial_r is not None:
        partial_level = entry + model.partial_r * risk if side == "long" else entry - model.partial_r * risk
    flatten_level = None
    if model.flatten_at_r is not None:
        flatten_level = entry + model.flatten_at_r * risk if side == "long" else entry - model.flatten_at_r * risk

    start = trig_rel + 1  # no same-bar fill fantasy
    if start >= len(day_bars):
        return float(day_bars[trig_rel]["c"]), "eod", 0.0, 0.0, 0.0, 0

    for i in range(start, len(day_bars)):
        held = i - trig_rel
        b = day_bars[i]
        h, l, c = float(b["h"]), float(b["l"]), float(b["c"])

        if side == "long":
            mfe = max(mfe, h - entry)
            mae = max(mae, entry - l)
            mfe_r = mfe / risk
            # conservative: stop before target on the same bar
            if l <= working_stop:
                final = (working_stop - entry) / risk
                realized = (model.partial_frac * (model.partial_r or 0.0) + (1.0 - model.partial_frac) * final) if partial_done else final
                return working_stop, "stop", realized, mfe_r, mae / risk, held
            if flatten_level is not None and h >= flatten_level:
                return flatten_level, "flatten", model.flatten_at_r or 0.0, mfe_r, mae / risk, held
            if model.partial_r is not None and not partial_done and h >= partial_level:
                partial_done = True
                if model.trail_be:
                    working_stop = max(working_stop, entry)
            if model.be_after_r is not None and mfe_r >= model.be_after_r:
                working_stop = max(working_stop, entry)
            if model.giveback is not None and mfe_r >= 0.5:
                give = h - l  # worst intra-bar giveback from high
                # more honest: close vs peak (use high as peak, close as now)
                peak = entry + mfe
                if (peak - c) >= model.giveback * mfe and mfe > 0:
                    final = (c - entry) / risk
                    realized = (model.partial_frac * (model.partial_r or 0.0) + (1.0 - model.partial_frac) * final) if partial_done else final
                    return c, "giveback", realized, mfe_r, mae / risk, held
            if h >= target:
                final = (target - entry) / risk
                realized = (model.partial_frac * (model.partial_r or 0.0) + (1.0 - model.partial_frac) * final) if partial_done else final
                return target, "partial_target" if partial_done else "target", realized, mfe_r, mae / risk, held
            if (
                model.time_stop_bars is not None
                and held >= model.time_stop_bars
                and mfe_r < model.time_stop_min_r
                and not partial_done
            ):
                final = (c - entry) / risk
                return c, "time_stop", final, mfe_r, mae / risk, held
        else:
            mfe = max(mfe, entry - l)
            mae = max(mae, h - entry)
            mfe_r = mfe / risk
            if h >= working_stop:
                final = (entry - working_stop) / risk
                realized = (model.partial_frac * (model.partial_r or 0.0) + (1.0 - model.partial_frac) * final) if partial_done else final
                return working_stop, "stop", realized, mfe_r, mae / risk, held
            if flatten_level is not None and l <= flatten_level:
                return flatten_level, "flatten", model.flatten_at_r or 0.0, mfe_r, mae / risk, held
            if model.partial_r is not None and not partial_done and l <= partial_level:
                partial_done = True
                if model.trail_be:
                    working_stop = min(working_stop, entry)
            if model.be_after_r is not None and mfe_r >= model.be_after_r:
                working_stop = min(working_stop, entry)
            if model.giveback is not None and mfe_r >= 0.5:
                peak = entry - mfe
                if (c - peak) >= model.giveback * mfe and mfe > 0:
                    final = (entry - c) / risk
                    realized = (model.partial_frac * (model.partial_r or 0.0) + (1.0 - model.partial_frac) * final) if partial_done else final
                    return c, "giveback", realized, mfe_r, mae / risk, held
            if l <= target:
                final = (entry - target) / risk
                realized = (model.partial_frac * (model.partial_r or 0.0) + (1.0 - model.partial_frac) * final) if partial_done else final
                return target, "partial_target" if partial_done else "target", realized, mfe_r, mae / risk, held
            if (
                model.time_stop_bars is not None
                and held >= model.time_stop_bars
                and mfe_r < model.time_stop_min_r
                and not partial_done
            ):
                final = (entry - c) / risk
                return c, "time_stop", final, mfe_r, mae / risk, held
        exit_px = c

    # session end
    if side == "long":
        final = (exit_px - entry) / risk
    else:
        final = (entry - exit_px) / risk
    if partial_done:
        realized = model.partial_frac * (model.partial_r or 0.0) + (1.0 - model.partial_frac) * final
        reason = "eod_partial"
    else:
        realized = final
        reason = "eod"
    return exit_px, reason, realized, mfe / risk, mae / risk, held


def _pack(trades: List[Trade]) -> Dict[str, Any]:
    if not trades:
        return {"n": 0}
    rs = [t.r_multiple for t in trades]
    sessions = [t.session for t in trades]
    net = clustered([t.r_net for t in trades], sessions)
    rows = [asdict(t) for t in trades]
    wins = [t for t in trades if t.r_multiple > 0]
    losses = [t for t in trades if t.r_multiple <= 0]
    by_exit: Dict[str, int] = {}
    for t in trades:
        by_exit[t.exit_reason] = by_exit.get(t.exit_reason, 0) + 1
    by_reg: Dict[str, Any] = {}
    for reg in sorted({t.regime or "unknown" for t in trades}):
        sub = [t for t in trades if (t.regime or "unknown") == reg]
        xs = [t.r_multiple for t in sub]
        by_reg[reg] = {
            "n": len(sub),
            "avg_r": round(float(np.mean(xs)), 3),
            "win_rate": round(sum(1 for x in xs if x > 0) / len(xs), 3),
        }
    by_mode: Dict[str, Any] = {}
    for mode in sorted({t.setup_mode or "?" for t in trades}):
        sub = [t for t in trades if (t.setup_mode or "?") == mode]
        xs = [t.r_multiple for t in sub]
        by_mode[mode] = {
            "n": len(sub),
            "avg_r": round(float(np.mean(xs)), 3),
            "win_rate": round(sum(1 for x in xs if x > 0) / len(xs), 3),
        }
    return {
        "n": len(trades),
        "win_rate": round(len(wins) / len(trades), 3),
        "avg_r": round(float(np.mean(rs)), 3),
        "median_r": round(float(np.median(rs)), 3),
        "sum_r": round(float(np.sum(rs)), 3),
        "avg_win_r": round(float(np.mean([t.r_multiple for t in wins])), 3) if wins else None,
        "avg_loss_r": round(float(np.mean([t.r_multiple for t in losses])), 3) if losses else None,
        "avg_mfe_r": round(float(np.mean([t.mfe_r for t in trades])), 3),
        "avg_mae_r": round(float(np.mean([t.mae_r for t in trades])), 3),
        "giveback_r": round(float(np.mean([t.mfe_r - t.r_multiple for t in trades])), 3),
        "by_exit": by_exit,
        "by_regime": by_reg,
        "by_setup_mode": by_mode,
        # honest view: net of costs, standard error clustered by session
        "avg_r_net": round(float(np.mean([t.r_net for t in trades])), 3),
        "avg_cost_r": round(float(np.mean([t.cost_r for t in trades])), 3),
        "net_clustered": net,
        "gross_clustered": clustered(rs, sessions),
        "verdict": verdict(net),
        "segments_net": segments(rows, ("setup_mode", "regime")),
    }


def trades_at(
    t: str,
    row: Dict[str, Any],
    day: List[Dict[str, Any]],
    k: int,
    models: List[ExitModel],
    entry_mode: str = "next_open",
    slip_bps: float = 2.0,
    dollar_vol: float = 0.0,
    grade_eod: str = "",
) -> Tuple[List[Trade], str]:
    """
    The trades a decision-time row makes, one per exit model, for its trigger on bar k of `day` (that ET
    day's bars): filled within the session (session_bars) and held to its close, or to the day's last bar
    for an after_hours model. ([], why) when the entry cannot be filled.
    """
    session = session_bars(t, day)
    side, entry_plan, stop, target = row["side"], float(row["entry"]), float(row["stop"]), float(row["target"])
    entry, fill = _fill_entry(side, entry_plan, stop, target, session, k, entry_mode, slip_bps)
    if entry is None:
        return [], fill
    cost = COST["crypto" if _is_crypto(t) else "equity"] * 100.0
    risk_pct = abs(entry - stop) / entry * 100.0 if entry else 0.0
    out: List[Trade] = []
    for model in models:
        exit_px, reason, r, mfe_r, mae_r, held = _simulate(
            side, entry, stop, target, day if model.after_hours else session, k, model, t,
        )
        out.append(
            Trade(
                ticker=t,
                session=str(day[k]["d"]),
                side=side,
                signal=str(row.get("signal")),
                grade=str(row.get("grade")),
                grade_eod=str(grade_eod or ""),
                edge=float(row.get("edge") or 0),
                regime=str(row.get("regime") or ""),
                setup_mode=str(row.get("setup_mode") or ""),
                ker=float(row["ker"]) if row.get("ker") is not None else None,
                rvol=float(row["rvol"]) if row.get("rvol") is not None else None,
                rvol_n=int(row.get("rvol_n") or 0),
                gap_pct=float(row["gap_pct"]) if row.get("gap_pct") is not None else None,
                entry=entry,
                stop=stop,
                target=target,
                rr_plan=float(row["rr"]) if row.get("rr") is not None else None,
                model=model.name,
                exit=float(exit_px),
                exit_reason=reason,
                r_multiple=round(float(r), 3),
                mfe_r=round(float(mfe_r), 3),
                mae_r=round(float(mae_r), 3),
                bars_held=int(held),
                pnl_pct_net=round(float(r) * risk_pct - cost, 4),
                dollar_vol=float(dollar_vol or 0),
                r_net=round(net_r(float(r), t, entry, stop), 3),
                cost_r=round(cost_r(t, entry, stop), 3),
                entry_mode=entry_mode,
                entry_plan=entry_plan,
                trigger_time=str(day[k].get("time") or ""),
            )
        )
    return out, fill


def replay(
    tickers: List[str],
    bars: Dict[str, pd.DataFrame],
    daily: Dict[str, pd.DataFrame],
    bar_prov: Dict[str, str],
    live: Dict[str, float],
    qmeta: Dict[str, Any],
    grade_min: str,
    models: List[ExitModel],
    entry_mode: str = "next_open",
    slip_bps: float = 2.0,
    skips: Optional[Dict[str, int]] = None,
) -> List[Trade]:
    """
    Every session after a name's first is replayed at decision time: each of its bars up to the close is
    graded on the prefix ending at that bar (engine.analyze_bars on one parse of the frame), as the desk
    saw it then. The trade is the first bar whose row is tradeable with its trigger on that bar. One trade
    per name and session: a trigger whose entry cannot be filled ends that session's search.
    """
    prep, grade = engine_api()
    trades: List[Trade] = []
    n_days = n_graded = n_trig = n_take = n_err = 0
    skips = skips if skips is not None else {}

    for ti, t in enumerate(tickers, 1):
        df = bars.get(t)
        if df is None or df.empty or len(df) < 40:
            continue
        parsed = prep(df)
        first: Dict[str, int] = {}
        for i, b in enumerate(parsed):
            first.setdefault(b["d"], i)
        days = list(first)
        if len(days) < 2:
            continue
        _ok, dvol = passes_volume_filter(t, df, min_dvol=2_000_000)
        kw = {"daily": daily.get(t), "bar_provider": bar_prov.get(t),
              "quote_provider": (qmeta.get(t) or {}).get("provider")}

        for j in range(1, len(days)):
            n_days += 1
            i0 = first[days[j]]
            day = parsed[i0: first[days[j + 1]] if j + 1 < len(days) else len(parsed)]
            session = session_bars(t, day)
            for k in range(len(session)):
                if i0 + k + 1 < 30:
                    continue
                n_graded += 1
                try:
                    row = grade(t, parsed[: i0 + k + 1], **kw)
                except Exception:
                    n_err += 1
                    continue
                if ((row.get("_chart") or {}).get("markers") or {}).get("trig") != k:
                    continue
                n_trig += 1
                if not _tradeable(row, grade_min):
                    continue
                try:
                    grade_eod = grade(t, parsed[: i0 + len(session)], **kw).get("grade")
                except Exception:
                    grade_eod = ""
                new, fill = trades_at(t, row, day, k, models, entry_mode, slip_bps, dvol, grade_eod)
                if new:
                    n_take += 1
                    trades += new
                else:
                    skips[fill] = skips.get(fill, 0) + 1
                break
        if ti % 10 == 0:
            print(f"    … {ti}/{len(tickers)} tickers  days={n_days} trigger bars={n_trig} taken={n_take} trades={len(trades)}")

    print(f"  ticker-days={n_days}  bars graded={n_graded}  trigger bars={n_trig}  taken({grade_min}+)={n_take}"
          + (f"  skipped {skips}" if skips else "") + (f"  engine errors {n_err}" if n_err else ""))
    return trades


def _replay_part(job: Tuple) -> Tuple[List[Trade], Dict[str, int]]:
    tickers, bars, daily, bar_prov, qmeta, grade_min, entry_mode, slip_bps = job
    skips: Dict[str, int] = {}
    trades = replay(tickers, bars, daily, bar_prov, {}, qmeta, grade_min, MODELS,
                    entry_mode=entry_mode, slip_bps=slip_bps, skips=skips)
    return trades, skips


def replay_parallel(tickers: List[str], bars: Dict[str, pd.DataFrame], daily: Dict[str, pd.DataFrame],
                    bar_prov: Dict[str, str], qmeta: Dict[str, Any], grade_min: str, entry_mode: str,
                    slip_bps: float, jobs: int) -> Tuple[List[Trade], Dict[str, int]]:
    """replay() with the default engine and MODELS, split by ticker over `jobs` processes; trades in ticker order."""
    have = [t for t in tickers if t in bars]
    parts = [p for p in (have[i::max(1, jobs)] for i in range(max(1, jobs))) if p]
    work = [(p, {t: bars[t] for t in p}, {t: daily[t] for t in p if t in daily}, bar_prov, qmeta,
             grade_min, entry_mode, slip_bps) for p in parts]
    if len(work) <= 1:
        return _replay_part(work[0]) if work else ([], {})
    by_ticker: Dict[str, List[Trade]] = {}
    skips: Dict[str, int] = {}
    with ProcessPoolExecutor(max_workers=len(work)) as ex:
        for trades, sk in ex.map(_replay_part, work):
            for tr in trades:
                by_ticker.setdefault(tr.ticker, []).append(tr)
            for k, v in sk.items():
                skips[k] = skips.get(k, 0) + v
    return [tr for t in have for tr in by_ticker.get(t, [])], skips


def print_honest(summaries: Dict[str, Dict[str, Any]]) -> None:
    print("\n" + "=" * 72)
    print("  HONEST VIEW · net of round-trip costs · SE clustered by session")
    print("=" * 72)
    for name in ("classic", "classic_after_hours", "time_24", "partial_trail"):
        s = summaries.get(name) or {}
        if not s.get("n"):
            continue
        if name == "classic_after_hours" and s["net_clustered"] == (summaries.get("classic") or {}).get("net_clustered"):
            continue                                    # crypto: no after-hours, the same as classic
        print(f"  {name:19s} gross {s['avg_r']:+.3f}R  cost {s['avg_cost_r']:.3f}R  net {fmt_stat(s['net_clustered'])}")
    s = summaries.get("classic") or {}
    if s.get("segments_net"):
        print("\n  classic, net R by setup mode x regime:")
        for seg in s["segments_net"]:
            label = f"{seg['setup_mode']}/{seg['regime']}"
            print(f"    {label:16s} {fmt_stat(seg)}")


def rescore(path: Path) -> int:
    """Honest stats for a saved replay (older files: trigger-close entries, net R recomputed here)."""
    d = json.loads(path.read_text(encoding="utf-8"))
    by_model: Dict[str, List[Trade]] = defaultdict(list)
    fields = set(Trade.__dataclass_fields__)
    for row in d.get("trades", []):
        t = Trade(**{k: v for k, v in row.items() if k in fields})
        if not row.get("r_net") and t.entry and t.stop:
            t.r_net = round(net_r(t.r_multiple, t.ticker, t.entry, t.stop), 3)
            t.cost_r = round(cost_r(t.ticker, t.entry, t.stop), 3)
        by_model[t.model].append(t)
    print(f"  {path.name}: asof {d.get('asof')} · entry: {(d.get('method') or {}).get('entry')}")
    print_honest({name: _pack(ts) for name, ts in by_model.items()})
    return 0


def _default_through(fetched_at: datetime, tickers: List[str]) -> date:
    """The last ET session over at the fetch: equities close at 16:00 (16:15 allows a delayed feed), crypto at midnight."""
    done_today = not any(_is_crypto(t) for t in tickers) and (fetched_at.hour, fetched_at.minute) >= (16, 15)
    return fetched_at.date() if done_today else fetched_at.date() - timedelta(days=1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Prefix-honest VWAP Blue session replay")
    ap.add_argument("--max-tickers", type=int, default=96)
    ap.add_argument("--grade-min", default="A")
    ap.add_argument("--interval", default="5m", help="5m matches live desk; 1m is 8d only")
    ap.add_argument("--mode", default="hybrid")
    ap.add_argument("--entry", choices=("next_open", "trigger_close"), default="next_open",
                    help="next_open: fill on the bar after the trigger (+slippage); trigger_close: the old optimistic fill")
    ap.add_argument("--slip-bps", type=float, default=2.0, help="slippage against us on next_open fills (bps)")
    ap.add_argument("--bars", help="replay a --save-bars file instead of fetching")
    ap.add_argument("--save-bars", help="write the fetch here (pickle), to replay the same bars again with --bars")
    ap.add_argument("--through", help="last session, YYYY-MM-DD (default: the last one that was over at the fetch)")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1), help="worker processes (split by ticker)")
    ap.add_argument("--out", help="research snapshot (default: research/replay_<fetch date>.json); never overwritten")
    ap.add_argument("--note", default="", help="free-text note stored in method.note")
    ap.add_argument("--rescore", default=None, help="print honest stats for a saved replay JSON (no fetching)")
    args = ap.parse_args()
    if args.rescore:
        return rescore(Path(args.rescore))

    now = datetime.now(ET)
    tickers = load_universe(max_n=args.max_tickers)
    for t in ["SPY", "QQQ", "IWM", "NVDA", "AAPL", "MSFT", "AMD", "TSLA", "META"]:
        if t not in tickers:
            tickers.append(t)
    tickers = list(dict.fromkeys(tickers))[: args.max_tickers]

    print("=" * 72)
    print("  SESSION REPLAY · VWAP Blue ·", now.strftime("%Y-%m-%d %H:%M %Z"))
    print("=" * 72)
    print(f"  Universe: {len(tickers)}  interval={args.interval}  grade≥{args.grade_min}")
    print(f"  Entry: {'next bar open + %.1f bps slippage' % args.slip_bps if args.entry == 'next_open' else 'trigger-bar close (optimistic)'}"
          " · hold: to the session's close (equities 16:00) · no overnight")
    print("  Every bar graded on its own prefix (decision time); first tradeable trigger per session")
    print("=" * 72)

    t0 = time.time()
    if args.bars:
        cache = pickle.loads(Path(args.bars).read_bytes())
        missing = [t for t in tickers if t not in cache["tickers"]]
        if missing:
            raise SystemExit(f"{args.bars} was not fetched for {missing[:5]}… ({len(missing)}): use its --max-tickers")
        print(f"  Bars: {args.bars} (fetched {cache['fetched_at']})")
    else:
        bars, daily, bar_prov, live, qmeta = batch_fetch(
            tickers, force=True, mode=args.mode, bars_interval=args.interval,
        )
        cache = {"fetched_at": now.isoformat(timespec="seconds"), "source": args.mode, "tickers": tickers,
                 "bars": bars, "bar_prov": bar_prov, "daily": daily, "live": live, "qmeta": qmeta}
        if args.save_bars:
            Path(args.save_bars).parent.mkdir(parents=True, exist_ok=True)
            Path(args.save_bars).write_bytes(pickle.dumps(cache))
            print(f"  cached bars → {args.save_bars}")
    fetched_at = datetime.fromisoformat(cache["fetched_at"])
    through = date.fromisoformat(args.through) if args.through else _default_through(fetched_at, tickers)
    bars = complete_sessions({t: cache["bars"][t] for t in tickers if t in cache["bars"]}, through)
    daily, bar_prov, qmeta = cache.get("daily") or {}, cache.get("bar_prov") or {}, cache.get("qmeta") or {}
    have = sum(1 for t in tickers if t in bars and len(bars[t]) > 20)
    print(f"  Bars ready: {have}/{len(tickers)} in {time.time()-t0:.1f}s · sessions through {through}")

    trades, skips = replay_parallel(tickers, bars, daily, bar_prov, qmeta, args.grade_min,
                                    args.entry, args.slip_bps, args.jobs)
    print(f"  replayed in {time.time()-t0:.0f}s")

    by_model: Dict[str, List[Trade]] = defaultdict(list)
    for tr in trades:
        by_model[tr.model].append(tr)

    summaries = {name: _pack(ts) for name, ts in by_model.items()}

    print("\n" + "=" * 72)
    print("  RESULTS BY EXIT MODEL")
    print("=" * 72)
    print(f"  {'model':22s} {'n':>4s} {'win':>7s} {'avgR':>8s} {'medR':>8s} {'sumR':>8s} {'MFE':>6s} {'give':>6s}")
    ranked = sorted(summaries.items(), key=lambda kv: (kv[1].get("avg_r") is not None, kv[1].get("avg_r") or -99), reverse=True)
    for name, s in ranked:
        if not s.get("n"):
            print(f"  {name:22s}    0")
            continue
        print(
            f"  {name:22s} {s['n']:4d} {s['win_rate']*100:6.1f}% "
            f"{s['avg_r']:+8.3f} {s['median_r']:+8.3f} {s['sum_r']:+8.3f} "
            f"{s['avg_mfe_r']:6.2f} {s['giveback_r']:6.2f}"
        )

    # detail the current desk model + the winner
    def dump_model(name: str) -> None:
        s = summaries.get(name) or {}
        if not s.get("n"):
            return
        print(f"\n  — {name} —")
        print(f"    exits:   {s.get('by_exit')}")
        print(f"    regime:  {s.get('by_regime')}")
        print(f"    mode:    {s.get('by_setup_mode')}")
        print(f"    avg win / loss: {s.get('avg_win_r')} / {s.get('avg_loss_r')}")

    dump_model("partial_trail")
    dump_model("classic")
    if ranked:
        dump_model(ranked[0][0])
    print_honest(summaries)

    sessions = sorted({str(d) for df in bars.values() for d in _session_days(df)[1:]})
    fetch_meta = {}
    for t in tickers:
        df = cache["bars"].get(t)
        if df is None or df.empty:
            fetch_meta[t] = {"provider": None, "bars": 0}
            continue
        idx = _et_index(df)
        fetch_meta[t] = {"provider": bar_prov.get(t), "bars": int(len(df)), "first": idx[0].isoformat(),
                         "last": idx[-1].isoformat()}
    method: Dict[str, Any] = {
        "bars": f"{args.interval} hybrid ~{ '8d' if args.interval=='1m' else '1mo' }",
        "fetched_at": cache["fetched_at"],
        "window": [sessions[0], sessions[-1]] if sessions else None,
        "sessions_replayed": len(sessions),
        "through": str(through),
        "trigger_search": "decision time: every bar to the session's close graded on the prefix ending at it; "
                          "the first tradeable row with its trigger on that bar, one trade per name and session",
        "entry": ("next bar open + %.1f bps slippage" % args.slip_bps) if args.entry == "next_open"
                 else "engine trigger close (optimistic)",
        "entry_mode": args.entry,
        "skipped_entries": skips,
        "r_net": "r_multiple - round-trip cost / risk (per trade)",
        "standard_error": "clustered by session (honest.py)",
        "hold": "to the session's close, no overnight: equities by the 16:00 close (the 15:55 bar; after-hours "
                "bars fill no entry, stop or target), crypto by its ET day's last bar. classic_after_hours holds "
                "equities to the day's last after-hours bar (~19:55 ET), the replay's hold before 2026-10-04",
        "grade": f"decision-time ≥ {args.grade_min}",
        "same_bar": "stop before target; no same-bar entry fill",
        "cost": COST,
        "fetch": fetch_meta,
    }
    if args.note:
        method["note"] = args.note
    payload = {
        "asof": cache["fetched_at"],
        "run_at": datetime.now(ET).isoformat(timespec="seconds"),
        "engine_version": ENGINE_VERSION,
        "method": method,
        "tickers": tickers,
        "summaries": summaries,
        "trades": [asdict(t) for t in trades],
    }
    stamp = now.strftime("%Y%m%d_%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = OUT_DIR / f"replay_sessions_{stamp}.json"
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # also a stable research snapshot; never overwrite one (committed results live there)
    snap = Path(args.out) if args.out else RESEARCH / f"replay_{fetched_at:%Y-%m-%d}.json"
    if snap.exists():
        snap = snap.with_name(f"{snap.stem}_{now:%H%M%S}{snap.suffix}")
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\n  Wrote {out_json}")
    print(f"  Wrote {snap}")
    print("  Research only — free delayed 5m · not trade advice.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
