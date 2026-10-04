#!/usr/bin/env python3
"""
The replay's old end-of-day trigger search, against the decision-time search it runs now.

Until 2026-10-04 replay_sessions.replay found each session's trigger on the end-of-day row, then re-graded
it at the trigger bar. Which trigger that was could depend on bars the desk had not seen yet:

  · before 09:30 an equity gap (and a v1.4.1 crypto gap) is provisional, price vs the prior close; the
    end-of-day row searched the premarket with the side fixed by the 09:30 open;
  · when the end-of-day row had a gap trigger, that was the trigger, and an earlier multi-day reverse
    the desk showed live was never graded.

replay_sessions.replay now grades every bar's prefix as the desk saw it and takes the first bar whose row
is tradeable with its trigger on that bar. This runs the old search on the same bars, with the replay's
fills, exits and costs (replay_sessions.trades_at), and compares the two. Where no trigger depends on
later bars (crypto on v1.5.0: no gap path) they agree trade for trade.

  python3 causal_check.py --bars data/backtests/equity_bars.pkl --merge-into research/<replay>.json
  python3 causal_check.py --bars data/backtests/crypto_bars.pkl --through 2026-10-03

--bars takes a replay_sessions.py or replay_crypto.py --save-bars file (a pickle of {"fetched_at",
"tickers", "bars", "bar_prov"}).
Research only. Free delayed data. Not trade advice.
"""

from __future__ import annotations

import argparse
import json
import pickle
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import replay_crypto as rc
import replay_sessions as rs
from honest import clustered, fmt_stat, verdict


def _eod_ticker(job: Tuple) -> Tuple[List[rs.Trade], Dict[str, int]]:
    """The old search for one name: each session's trigger from its end-of-day row, graded at its bar."""
    engine_path, t, df, grade_min = job
    rc.load_engine(engine_path)
    prep, grade = rs.engine_api()
    trades: List[rs.Trade] = []
    skips: Dict[str, int] = {}
    if df is None or len(df) < 40:
        return trades, skips
    parsed = prep(df)
    first: Dict[str, int] = {}
    for i, b in enumerate(parsed):
        first.setdefault(b["d"], i)
    days = list(first)
    _ok, dvol = rs.passes_volume_filter(t, df, min_dvol=2_000_000)
    for j in range(1, len(days)):
        i0 = first[days[j]]
        i1 = first[days[j + 1]] if j + 1 < len(days) else len(parsed)
        if i1 < 30:
            continue
        eod = grade(t, parsed[:i1])
        k = ((eod.get("_chart") or {}).get("markers") or {}).get("trig")
        day = parsed[i0:i1]
        if k is None or not 0 <= k < len(rs.session_bars(t, day)):
            continue
        row = grade(t, parsed[: i0 + k + 1])
        if not rs._tradeable(row, grade_min):
            continue
        new, fill = rs.trades_at(t, row, day, k, rs.MODELS, "next_open", 2.0, dvol, eod.get("grade"))
        trades += new
        if not new:
            skips[fill] = skips.get(fill, 0) + 1
    return trades, skips


def eod_search(tickers: List[str], bars: Dict[str, Any], engine_path: Optional[str],
               grade_min: str = "A", jobs: int = 8) -> Tuple[List[rs.Trade], Dict[str, int]]:
    work = [(engine_path, t, bars[t], grade_min) for t in tickers if t in bars]
    trades: List[rs.Trade] = []
    skips: Dict[str, int] = {}
    with ProcessPoolExecutor(max_workers=max(1, jobs)) as ex:
        for tr, sk in ex.map(_eod_ticker, work):
            trades += tr
            for k, v in sk.items():
                skips[k] = skips.get(k, 0) + v
    return trades, skips


def _premarket(t: rs.Trade) -> bool:
    return t.trigger_time.split()[1] < "09:30"


def compare(replay_trades: List[rs.Trade], eod_trades: List[rs.Trade]) -> Dict[str, Any]:
    key = lambda t: (t.ticker, t.session, t.trigger_time)
    r = [t for t in replay_trades if t.model == "classic"]
    e = [t for t in eod_trades if t.model == "classic"]
    kr, ke = {key(t) for t in r}, {key(t) for t in e}
    only = [t for t in r if key(t) in kr - ke]
    stat = lambda xs: clustered([t.r_net for t in xs], [t.session for t in xs])
    fields = lambda ts: sorted((t.ticker, t.session, t.model, t.side, t.trigger_time, t.entry, t.stop, t.target,
                                t.exit, t.exit_reason, t.r_multiple, t.r_net) for t in ts)
    return {
        "replay_classic_net": stat(r),
        "eod_search": {"n": len(e), "net": stat(e), "gross": clustered([t.r_multiple for t in e], [t.session for t in e]),
                       "verdict": verdict(stat(e)),
                       # with the replay's old equity hold too, to the last after-hours bar
                       "after_hours_net": stat([t for t in eod_trades if t.model == "classic_after_hours"])},
        "same_trigger": len(kr & ke), "only_replay": len(kr - ke), "only_eod_search": len(ke - kr),
        "only_replay_by_mode": dict(Counter(f"{'premarket' if _premarket(t) else 'rth'}/{t.setup_mode}" for t in only)),
        "only_replay_net": stat(only),
        "identical_trades": fields(replay_trades) == fields(eod_trades),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="The replay's old end-of-day trigger search vs its decision-time search")
    ap.add_argument("--bars", required=True, help="a replay_sessions.py or replay_crypto.py --save-bars pickle")
    ap.add_argument("--through", help="last session, YYYY-MM-DD (default: the last one that was over at the fetch)")
    ap.add_argument("--engine", help="engine file (default: ./engine.py)")
    ap.add_argument("--grade-min", default="A")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--merge-into", help="research JSON whose method gets this as causal_check")
    ap.add_argument("--exclude", default="", help="comma-separated names to leave out (as replay_crypto.py --exclude)")
    args = ap.parse_args()
    exclude = {t.strip().upper() for t in args.exclude.split(",") if t.strip()}

    cache = pickle.loads(Path(args.bars).read_bytes())
    fetched = datetime.fromisoformat(cache["fetched_at"])
    tickers = [t for t in cache["tickers"] if t in cache["bars"] and t not in exclude]
    through = date.fromisoformat(args.through) if args.through else rs._default_through(fetched, tickers)
    bars = rc.complete_sessions(cache["bars"], through)
    version, replay_trades, _ = rc.run(tickers, bars, cache.get("bar_prov", {}), args.engine, args.grade_min,
                                       "next_open", 2.0, args.jobs)
    eod_trades, skips = eod_search(tickers, bars, args.engine, args.grade_min, args.jobs)
    out = {"engine_version": version, "through": str(through), "bars_fetched_at": cache["fetched_at"],
           "rule": "end-of-day search (replay_sessions.replay before 2026-10-04): the trigger on the session's "
                   "end-of-day row, graded on the prefix through its bar; same fills, exits and costs (causal_check.py)",
           "excluded": sorted(exclude),
           **compare(replay_trades, eod_trades), "skipped_entries": skips}
    print(f"  engine v{version} · {len(tickers)} names · sessions through {through}")
    print(f"  replay (decision time)  classic net {fmt_stat(out['replay_classic_net'])}")
    print(f"  end-of-day search       classic net {fmt_stat(out['eod_search']['net'])}")
    print(f"  same trigger {out['same_trigger']} · only replay {out['only_replay']} {out['only_replay_by_mode']} · "
          f"only end-of-day {out['only_eod_search']} · identical trades: {out['identical_trades']}")
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
