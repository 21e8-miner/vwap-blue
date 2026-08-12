#!/usr/bin/env python3
"""
Prefix-honest multi-session replay for VWAP Blue.

Unlike backtest_today_scans.py (today's leftover bars, first price-touch entry)
this:

  · fetches the same 5m hybrid book the live desk uses (~1mo free)
  · treats each ET session as its own trade day
  · finds the engine trigger on that day's prefix (no future days)
  · re-scores grade / regime / blocks at the trigger bar (no EOD look-ahead)
  · enters at the trigger close (engine entry), not a historical price touch
  · holds only to that session's last bar — no overnight
  · runs several exit models against the same entries

Research only. Free delayed data. Not trade advice.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from data import batch_fetch, load_universe, passes_volume_filter
from engine import analyze

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "data" / "backtests"
RESEARCH = HERE / "research"

COST = {"equity": 0.0008, "crypto": 0.0015}
GRADE_OK = {"A", "LA"}


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


def _bar_ms(ts) -> int:
    t = pd.Timestamp(ts)
    return int(t.timestamp() * 1000)


def _prefix_through(df: pd.DataFrame, day: date, until_ts_ms: Optional[int] = None) -> pd.DataFrame:
    idx = _et_index(df)
    mask = np.array([d <= day for d in idx.date])
    out = df.loc[mask]
    if until_ts_ms is None or out.empty:
        return out
    ms = np.array([_bar_ms(ts) for ts in out.index])
    return out.loc[ms <= int(until_ts_ms)]


def _day_slice(df: pd.DataFrame, day: date) -> pd.DataFrame:
    idx = _et_index(df)
    return df.loc[idx.date == day]


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
    }


def replay(
    tickers: List[str],
    bars: Dict[str, pd.DataFrame],
    daily: Dict[str, pd.DataFrame],
    bar_prov: Dict[str, str],
    live: Dict[str, float],
    qmeta: Dict[str, Any],
    grade_min: str,
    models: List[ExitModel],
) -> List[Trade]:
    trades: List[Trade] = []
    n_days = 0
    n_trig = 0
    n_take = 0

    for ti, t in enumerate(tickers, 1):
        df = bars.get(t)
        if df is None or df.empty or len(df) < 40:
            continue
        days = _session_days(df)
        if len(days) < 2:
            continue
        ok, dvol = passes_volume_filter(t, df, min_dvol=2_000_000)
        # don't drop crypto for $2M; filter already handles that
        if not ok and not _is_crypto(t):
            # still allow if any session was liquid — keep, just tag
            pass

        for day in days[1:]:
            n_days += 1
            prefix = _prefix_through(df, day)
            if prefix is None or len(prefix) < 30:
                continue
            try:
                row_eod = analyze(
                    t,
                    prefix,
                    daily.get(t),
                    live_price=None,
                    bar_provider=bar_prov.get(t),
                    quote_provider=(qmeta.get(t) or {}).get("provider"),
                )
            except Exception:
                continue
            ch = row_eod.get("_chart") or {}
            trig_rel = (ch.get("markers") or {}).get("trig")
            if trig_rel is None:
                continue
            day_bars = ch.get("bars") or []
            if trig_rel < 0 or trig_rel >= len(day_bars):
                continue
            n_trig += 1
            trig_ts = day_bars[trig_rel].get("ts")
            # decision-time prefix: through trigger bar only
            try:
                trig_prefix = _prefix_through(df, day, until_ts_ms=trig_ts)
                row = analyze(
                    t,
                    trig_prefix,
                    daily.get(t),
                    live_price=None,
                    bar_provider=bar_prov.get(t),
                    quote_provider=(qmeta.get(t) or {}).get("provider"),
                )
            except Exception:
                row = row_eod
            if not _tradeable(row, grade_min):
                continue
            n_take += 1
            entry = float(row["entry"])
            stop = float(row["stop"])
            target = float(row["target"])
            side = row["side"]
            cost = COST["crypto" if _is_crypto(t) else "equity"] * 100.0
            risk_pct = abs(entry - stop) / entry * 100.0 if entry else 0.0

            for model in models:
                exit_px, reason, r, mfe_r, mae_r, held = _simulate(
                    side, entry, stop, target, day_bars, int(trig_rel), model, t,
                )
                trades.append(
                    Trade(
                        ticker=t,
                        session=str(day),
                        side=side,
                        signal=str(row.get("signal")),
                        grade=str(row.get("grade")),
                        grade_eod=str(row_eod.get("grade") or ""),
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
                        dollar_vol=float(dvol or 0),
                    )
                )
        if ti % 10 == 0:
            print(f"    … {ti}/{len(tickers)} tickers  days={n_days} trigs={n_trig} taken={n_take} trades={len(trades)}")

    print(f"  ticker-days={n_days}  engine-trigs={n_trig}  taken({grade_min}+)={n_take}")
    return trades


def main() -> int:
    ap = argparse.ArgumentParser(description="Prefix-honest VWAP Blue session replay")
    ap.add_argument("--max-tickers", type=int, default=96)
    ap.add_argument("--grade-min", default="A")
    ap.add_argument("--interval", default="5m", help="5m matches live desk; 1m is 8d only")
    ap.add_argument("--mode", default="hybrid")
    args = ap.parse_args()

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
    print("  Entry: trigger-bar close · hold: that session only · no overnight")
    print("  Grade/regime scored at trigger bar (not EOD)")
    print("=" * 72)

    t0 = time.time()
    bars, daily, bar_prov, live, qmeta = batch_fetch(
        tickers, force=True, mode=args.mode, bars_interval=args.interval,
    )
    have = sum(1 for t in tickers if t in bars and bars[t] is not None and len(bars[t]) > 20)
    print(f"  Bars ready: {have}/{len(tickers)} in {time.time()-t0:.1f}s")

    trades = replay(tickers, bars, daily, bar_prov, live, qmeta, args.grade_min, MODELS)

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

    stamp = now.strftime("%Y%m%d_%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RESEARCH.mkdir(parents=True, exist_ok=True)
    payload = {
        "asof": now.isoformat(),
        "method": {
            "bars": f"{args.interval} hybrid ~{ '8d' if args.interval=='1m' else '1mo' }",
            "entry": "engine trigger close, prefix through trigger bar",
            "hold": "focus session only (no overnight)",
            "grade": f"decision-time ≥ {args.grade_min}",
            "same_bar": "stop before target; no same-bar entry fill",
            "cost": COST,
        },
        "tickers": tickers,
        "summaries": summaries,
        "trades": [asdict(t) for t in trades],
    }
    out_json = OUT_DIR / f"replay_sessions_{stamp}.json"
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # also a stable research snapshot
    snap = RESEARCH / f"replay_{now.strftime('%Y-%m-%d')}.json"
    snap.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\n  Wrote {out_json}")
    print(f"  Wrote {snap}")
    print("  Research only — free delayed 5m · not trade advice.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
