"""
mdrev target rules (engine._md_target) and the missing-RVOL gate, replayed on the same bars.

Each variant runs the decision-time session replay (replay_sessions.replay, next-bar fills + 2 bps,
equities flat by 16:00, net of costs, session-clustered) with the engine's defaults switched in each
worker process. Two bar sets:
  --bars     a replay_sessions --save-bars pickle (the September file the README's results use)
  --holdout  a pickle written by --fetch-holdout: 60 days of Yahoo 5m bars (the desk's own fetch only
             reaches 32), scored on sessions outside the --bars window, so a rule picked on one set is
             checked on bars it never saw.
Usage:
  python3 md_target_check.py --fetch-holdout data/backtests/holdout_60d.pkl
  python3 md_target_check.py --bars data/backtests/equity_bars_2026-10-04_1507.pkl \
                             --holdout data/backtests/holdout_60d.pkl --out research/md_target_check.json
"""
import argparse
import json
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402
import honest  # noqa: E402
import replay_sessions as rs  # noqa: E402
from data import load_universe  # noqa: E402

ET = ZoneInfo("America/New_York")
VARIANTS = [
    {"md_target": "stretch", "rvol_missing": "pass"},     # v1.5.x
    {"md_target": "blue_only", "rvol_missing": "pass"},
    {"md_target": "day", "rvol_missing": "pass"},
    {"md_target": "prior", "rvol_missing": "pass"},
    {"md_target": "nearest", "rvol_missing": "pass"},
    {"md_target": "mirror", "rvol_missing": "pass"},
    {"md_target": "stretch", "rvol_missing": "thin"},
    {"md_target": "nearest", "rvol_missing": "thin"},
]


def _init(v):
    engine.DEFAULT_MD_TARGET = v["md_target"]
    engine.DEFAULT_RVOL_MISSING = v["rvol_missing"]


def _part(job):
    return rs._replay_part(job)


def run(cache, v, jobs=8):
    tickers = [t for t in cache["tickers"] if t in cache["bars"]]
    parts = [p for p in (tickers[i::jobs] for i in range(jobs)) if p]
    work = [(p, {t: cache["bars"][t] for t in p}, {t: cache["daily"][t] for t in p if t in cache["daily"]},
             cache["bar_prov"], cache["qmeta"], "A", "next_open", 2.0) for p in parts]
    trades = []
    with ProcessPoolExecutor(len(work), initializer=_init, initargs=(v,)) as ex:
        for tr, _sk in ex.map(_part, work):
            trades += [asdict(t) for t in tr if t.model == "classic"]
    return trades


def summary(trades):
    out = {"all": honest.clustered([t["r_net"] for t in trades], [t["session"] for t in trades])}
    for mode in ("mdrev", "gap", "both"):
        sel = [t for t in trades if t["setup_mode"] == mode]
        if sel:
            out[mode] = honest.clustered([t["r_net"] for t in sel], [t["session"] for t in sel])
    md = [t for t in trades if t["setup_mode"] == "mdrev" and t.get("rr_plan") is not None]
    out["mdrev_rr_plan_median"] = sorted(t["rr_plan"] for t in md)[len(md) // 2] if md else None
    return out


def fetch_holdout(path):
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    tickers = [t for t in load_universe() if not engine._is_crypto(t)][:96]
    bars = {}
    for t in tickers:
        for _ in range(3):
            try:
                j = s.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}",
                          params={"interval": "5m", "range": "60d", "includePrePost": "true"}, timeout=20).json()
                r = j["chart"]["result"][0]
                q = r["indicators"]["quote"][0]
                bars[t] = pd.DataFrame({"Open": q["open"], "High": q["high"], "Low": q["low"], "Close": q["close"],
                                        "Volume": [v or 0 for v in q["volume"]]},
                                       index=pd.to_datetime(r["timestamp"], unit="s", utc=True)).dropna(subset=["Close"])
                break
            except Exception:  # noqa: BLE001
                time.sleep(2)
        time.sleep(0.25)
    cache = {"fetched_at": datetime.now(ET).isoformat(timespec="seconds"), "tickers": tickers, "bars": bars,
             "bar_prov": {t: "yahoo_chart_60d" for t in bars}, "daily": {}, "live": {}, "qmeta": {}}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(pickle.dumps(cache))
    print(f"wrote {path}: {len(bars)}/{len(tickers)} tickers")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars")
    ap.add_argument("--holdout")
    ap.add_argument("--fetch-holdout")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.fetch_holdout:
        return fetch_holdout(a.fetch_holdout)
    main_c = pickle.loads(Path(a.bars).read_bytes())
    hold_c = pickle.loads(Path(a.holdout).read_bytes()) if a.holdout else None
    res = {}
    window = None
    for v in VARIANTS:
        key = f"{v['md_target']}/{v['rvol_missing']}"
        tr = run(main_c, v)
        window = window or (min(t["session"] for t in tr), max(t["session"] for t in tr))
        res[key] = {"in_sample": summary(tr)}
        if hold_c:
            th = [t for t in run(hold_c, v) if not (window[0] <= t["session"] <= window[1])]
            res[key]["holdout"] = summary(th)
        line = f"{key:20s}"
        for part in ("in_sample", "holdout"):
            if part not in res[key]:
                continue
            s = res[key][part]
            a_ = s["all"]
            m = s.get("mdrev") or {}
            line += (f" | {part}: all {a_['mean']:+.3f}±{a_['se']:.3f} t{a_['t']:+.1f} n{a_['n']}"
                     f" · mdrev {m.get('mean', float('nan')):+.3f} n{m.get('n', 0)} rr~{s['mdrev_rr_plan_median']}")
        print(line, flush=True)
    if a.out:
        Path(a.out).write_text(json.dumps({"method": __doc__, "window": window, "variants": VARIANTS,
                                           "results": res}, indent=1, default=str))
        print("wrote", a.out)


if __name__ == "__main__":
    main()
