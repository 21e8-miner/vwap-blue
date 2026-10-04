#!/usr/bin/env python3
"""
Bar-by-bar decision-time replay: a check on replay_sessions.replay's trigger search.

replay_sessions.replay finds each session's trigger on the end-of-day row, then re-grades it at the
trigger bar. Which trigger that is can depend on bars the desk had not seen yet:

  · before 09:30 an equity gap (and a v1.4.1 crypto gap) is provisional, price vs the prior close; the
    end-of-day row searches the premarket with the side fixed by the 09:30 open;
  · when the end-of-day row has a gap trigger, that is the trigger, and an earlier multi-day reverse
    the desk showed live is never graded.

This grades every bar's prefix as the desk would have seen it and takes the first bar whose
decision-time row is tradeable (replay_sessions._tradeable) with its trigger on that bar; fills, exits
and costs are the replay's. One analyze() per bar, so it is slow. Where no trigger depends on later
bars (crypto on v1.5.0: no gap path) it reproduces replay_sessions.replay trade for trade.

  python3 causal_check.py --bars data/backtests/crypto_bars.pkl --through 2026-10-03
  python3 causal_check.py --bars data/backtests/crypto_bars.pkl --merge-into research/<replay>.json

--bars takes a replay_crypto.py --save-bars file (or any pickle of {"tickers", "bars", "bar_prov"}).
Research only. Free delayed data. Not trade advice.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pickle
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import replay_crypto as rc
import replay_sessions as rs
from honest import COST, clustered, cost_r, fmt_stat, net_r, verdict

HERE = Path(__file__).resolve().parent


def _engine(path: Optional[str]):
    spec = importlib.util.spec_from_file_location("engine_causal", path or str(HERE / "engine.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ticker(job: Tuple) -> Tuple[List[rs.Trade], Dict[str, int]]:
    engine_path, t, df, through, grade_min = job
    eng = _engine(engine_path)
    df = df.loc[rs._et_index(df).date <= through]
    prepped = eng._prep_bars(df)
    eng._prep_bars = lambda n: prepped[:n]       # analyze(prefix of n bars) without re-parsing the frame
    first: Dict[str, int] = {}
    for i, b in enumerate(prepped):
        first.setdefault(b["d"], i)
    trades: List[rs.Trade] = []
    skips: Dict[str, int] = {}
    for d in list(first)[1:]:
        i0 = first[d]
        day = [b for b in prepped[i0:] if b["d"] == d]
        for k in range(len(day)):
            if i0 + k + 1 < 30:
                continue
            row = eng.analyze(t, i0 + k + 1)
            if ((row.get("_chart") or {}).get("markers") or {}).get("trig") != k or not rs._tradeable(row, grade_min):
                continue
            side, entry_plan, stop, target = row["side"], float(row["entry"]), float(row["stop"]), float(row["target"])
            entry, fill = rs._fill_entry(side, entry_plan, stop, target, day, k, "next_open", 2.0)
            if entry is None:
                skips[fill] = skips.get(fill, 0) + 1
                break
            cost = COST["crypto" if rs._is_crypto(t) else "equity"] * 100.0
            risk_pct = abs(entry - stop) / entry * 100.0
            for m in rs.MODELS:
                exit_px, reason, r, mfe, mae, held = rs._simulate(side, entry, stop, target, day, k, m, t)
                trades.append(rs.Trade(
                    ticker=t, session=d, side=side, signal=str(row.get("signal")), grade=str(row.get("grade")),
                    grade_eod="", edge=float(row.get("edge") or 0), regime=str(row.get("regime") or ""),
                    setup_mode=str(row.get("setup_mode") or ""), ker=row.get("ker"), rvol=row.get("rvol"),
                    rvol_n=int(row.get("rvol_n") or 0), gap_pct=row.get("gap_pct"), entry=entry, stop=stop,
                    target=target, rr_plan=row.get("rr"), model=m.name, exit=float(exit_px), exit_reason=reason,
                    r_multiple=round(float(r), 3), mfe_r=round(float(mfe), 3), mae_r=round(float(mae), 3),
                    bars_held=int(held), pnl_pct_net=round(float(r) * risk_pct - cost, 4),
                    r_net=round(net_r(float(r), t, entry, stop), 3),
                    cost_r=round(cost_r(t, entry, stop), 3), entry_mode="next_open", entry_plan=entry_plan,
                    trigger_time=day[k]["time"]))
            break
    return trades, skips


def causal(tickers: List[str], bars: Dict[str, Any], engine_path: Optional[str], through: date,
           grade_min: str = "A", jobs: int = 8) -> Tuple[List[rs.Trade], Dict[str, int]]:
    work = [(engine_path, t, bars[t], through, grade_min) for t in tickers if t in bars]
    trades: List[rs.Trade] = []
    skips: Dict[str, int] = {}
    with ProcessPoolExecutor(max_workers=max(1, jobs)) as ex:
        for tr, sk in ex.map(_ticker, work):
            trades += tr
            for k, v in sk.items():
                skips[k] = skips.get(k, 0) + v
    return trades, skips


def _premarket(t: rs.Trade) -> bool:
    return t.trigger_time.split()[1] < "09:30"


def compare(causal_trades: List[rs.Trade], replay_trades: List[rs.Trade]) -> Dict[str, Any]:
    key = lambda t: (t.ticker, t.session, t.trigger_time)
    c = [t for t in causal_trades if t.model == "classic"]
    r = [t for t in replay_trades if t.model == "classic"]
    kc, kr = {key(t) for t in c}, {key(t) for t in r}
    only = [t for t in c if key(t) in kc - kr]
    stat = lambda xs: clustered([t.r_net for t in xs], [t.session for t in xs])
    fields = lambda ts: sorted((t.ticker, t.session, t.model, t.side, t.trigger_time, t.entry, t.stop, t.target,
                                t.exit, t.exit_reason, t.r_multiple, t.r_net) for t in ts)
    return {
        "classic": {"n": len(c), "net": stat(c), "gross": clustered([t.r_multiple for t in c], [t.session for t in c]),
                    "verdict": verdict(stat(c))},
        "replay_classic_net": stat(r),
        "same_trigger": len(kc & kr), "only_causal": len(kc - kr), "only_replay": len(kr - kc),
        "only_causal_by_mode": dict(Counter(f"{'premarket' if _premarket(t) else 'rth'}/{t.setup_mode}" for t in only)),
        "only_causal_net": stat(only),
        "identical_trades": fields(causal_trades) == fields(replay_trades),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Bar-by-bar decision-time check of replay_sessions.replay")
    ap.add_argument("--bars", required=True, help="pickle of {tickers, bars, bar_prov} (replay_crypto.py --save-bars)")
    ap.add_argument("--through", help="last session, YYYY-MM-DD (default: the day before the fetch, ET)")
    ap.add_argument("--engine", help="engine file (default: ./engine.py)")
    ap.add_argument("--grade-min", default="A")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--merge-into", help="research JSON whose method gets this as causal_check")
    ap.add_argument("--exclude", default="", help="comma-separated names to leave out (as replay_crypto.py --exclude)")
    args = ap.parse_args()
    exclude = {t.strip().upper() for t in args.exclude.split(",") if t.strip()}

    cache = pickle.loads(Path(args.bars).read_bytes())
    fetched = datetime.fromisoformat(cache["fetched_at"])
    through = date.fromisoformat(args.through) if args.through else fetched.date() - timedelta(days=1)
    tickers = [t for t in cache["tickers"] if t in cache["bars"] and t not in exclude]
    bars = rc.complete_sessions(cache["bars"], through)
    version, replay_trades, _ = rc.run(tickers, bars, cache.get("bar_prov", {}), args.engine, args.grade_min,
                                       "next_open", 2.0, args.jobs)
    causal_trades, skips = causal(tickers, bars, args.engine, through, args.grade_min, args.jobs)
    out = {"engine_version": version, "through": str(through), "bars_fetched_at": cache["fetched_at"],
           "rule": "first bar whose decision-time row is tradeable with its trigger on that bar (causal_check.py)",
           "excluded": sorted(exclude),
           **compare(causal_trades, replay_trades), "skipped_entries": skips}
    print(f"  engine v{version} · {len(tickers)} names · sessions through {through}")
    print(f"  replay  classic net {fmt_stat(out['replay_classic_net'])}")
    print(f"  causal  classic net {fmt_stat(out['classic']['net'])}")
    print(f"  same trigger {out['same_trigger']} · only causal {out['only_causal']} {out['only_causal_by_mode']} · "
          f"only replay {out['only_replay']} · identical trades: {out['identical_trades']}")
    if args.merge_into:
        p = Path(args.merge_into)
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("engine_version") != version:
            raise SystemExit(f"{p} is engine v{d.get('engine_version')}, this check ran v{version}")
        d["method"]["causal_check"] = out
        p.write_text(json.dumps(d, indent=2), encoding="utf-8")
        print(f"  merged into {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
