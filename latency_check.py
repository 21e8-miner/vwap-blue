"""
Chaos-latency stress (VERITAS-style): the same decision-time trades filled one bar late and with worse slippage.

Variants on the same bars, floor B, with the mirror control (second_look_check harness):
  next_open 2 bps (the replay's standard)  |  next_open 10 bps  |  late_open 2 bps  |  late_open 10 bps
late_open fills at the open of the second bar after the trigger (a trader 5 minutes behind the alert) and
starts the exit simulation after that bar.

  python3 latency_check.py --bars <60d pkl> --out research/latency_check_<date>.json
"""
import argparse
import json
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import replay_sessions as rs  # noqa: E402
import second_look_check as slc  # noqa: E402

VARIANTS = [("next_open", 2.0), ("next_open", 10.0), ("late_open", 2.0), ("late_open", 10.0)]


def _part(job):
    return rs._replay_part(job)


def run(cache, entry_mode, slip, jobs):
    tickers = [t for t in cache["tickers"] if t in cache["bars"]]
    parts = [p for p in (tickers[i::jobs] for i in range(jobs)) if p]
    work = [(p, {t: cache["bars"][t] for t in p}, {t: cache["daily"][t] for t in p if t in cache["daily"]},
             cache["bar_prov"], cache["qmeta"], "B", entry_mode, slip) for p in parts]
    trades = []
    with ProcessPoolExecutor(len(work)) as ex:
        for tr, _sk in ex.map(_part, work):
            trades += [asdict(t) for t in tr if t.model == "classic"]
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--out")
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args()
    cache = pickle.loads(Path(a.bars).read_bytes())
    res = {}
    for mode, slip in VARIANTS:
        tr = run(cache, mode, slip, a.jobs)
        key = f"{mode}/{slip:g}bps"
        res[key] = slc.summary(tr)
        s = res[key]
        print(f"{key:18s} all {slc.fmt(s['all'])}\n{'':18s} mdrev {slc.fmt(s['mdrev'])}\n{'':18s} fades {slc.fmt(s['fades'])}", flush=True)
        if a.out:
            Path(a.out).write_text(json.dumps({"method": __doc__, "variants": VARIANTS, "results": res}, indent=1, default=str))
    print("done")


if __name__ == "__main__":
    main()
