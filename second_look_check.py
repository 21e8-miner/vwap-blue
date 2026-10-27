"""
Two refinements the Oct 26 session suggested, replayed paired on the same bars (research only):

  * md_target_min_r: a multi-day reclaim whose nearest level sits closer than this x risk takes the next
    level instead (none left = no runway, not tradeable). On Oct 26 ten of 42 reclaims "hit target" for
    ~0R because the nearest level was cents from entry.
  * rearm_after_stop (+ replay MAX_TRADES_PER_SESSION = 2): a stopped gap fade goes back to watching for
    a fresh break of blue instead of showing STOPPED for the rest of the session (PBR, Oct 26: stopped
    at 10:00, the afternoon reclaim to 22.73 was never shown).

Same harness as md_target_check.py: decision-time replay (next-bar fills + 2 bps, flat by 16:00, net of
costs, session-clustered), engine defaults switched in each worker. Grade floor B so the fades (B since
v1.7.0) are in; stats are reported for all trades, reclaims and fades, with the mirror control.

  python3 second_look_check.py --bars <sept pkl> --holdout <60d pkl> --out research/second_look_check.json
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
import engine  # noqa: E402
import honest  # noqa: E402
import replay_sessions as rs  # noqa: E402

VARIANTS = [
    {"name": "base", "md_target_min_r": 0.0, "rearm": False},
    {"name": "min_r_0.5", "md_target_min_r": 0.5, "rearm": False},
    {"name": "min_r_1.0", "md_target_min_r": 1.0, "rearm": False},
    {"name": "rearm", "md_target_min_r": 0.0, "rearm": True},
    {"name": "rearm+min_r_1.0", "md_target_min_r": 1.0, "rearm": True},
]


def _init(v):
    engine.DEFAULT_MD_TARGET_MIN_R = v["md_target_min_r"]
    engine.DEFAULT_REARM_AFTER_STOP = v["rearm"]
    rs.MAX_TRADES_PER_SESSION = 2 if v["rearm"] else 1


def _part(job):
    return rs._replay_part(job)


def run(cache, v, jobs=8):
    tickers = [t for t in cache["tickers"] if t in cache["bars"]]
    parts = [p for p in (tickers[i::jobs] for i in range(jobs)) if p]
    work = [(p, {t: cache["bars"][t] for t in p}, {t: cache["daily"][t] for t in p if t in cache["daily"]},
             cache["bar_prov"], cache["qmeta"], "B", "next_open", 2.0) for p in parts]
    trades = []
    with ProcessPoolExecutor(len(work), initializer=_init, initargs=(v,)) as ex:
        for tr, _sk in ex.map(_part, work):
            trades += [asdict(t) for t in tr if t.model == "classic"]
    return trades


def stat(sel):
    if not sel:
        return None
    s = honest.clustered([t["r_net"] for t in sel], [t["session"] for t in sel])
    m = honest.clustered([t["r_net"] - t["mirror_r_net"] for t in sel if t.get("mirror_r_net") is not None],
                         [t["session"] for t in sel if t.get("mirror_r_net") is not None])
    return {"n": s["n"], "sessions": s["clusters"], "mean": s["mean"], "se": s["se"], "t": s["t"],
            "verdict": honest.verdict(s), "direction": m["mean"], "direction_se": m["se"],
            "scratch": sum(1 for t in sel if t["exit_reason"] == "target" and abs(t["r_multiple"]) < 0.1),
            "target": sum(t["exit_reason"] == "target" for t in sel), "stop": sum(t["exit_reason"] == "stop" for t in sel)}


def summary(trades):
    out = {"all": stat(trades),
           "mdrev": stat([t for t in trades if t["setup_mode"] == "mdrev"]),
           "fades": stat([t for t in trades if t["setup_mode"] != "mdrev"]),
           "second": stat([t for t in trades if t.get("nth", 1) > 1]),
           "first_fades": stat([t for t in trades if t["setup_mode"] != "mdrev" and t.get("nth", 1) == 1])}
    md = [t for t in trades if t["setup_mode"] == "mdrev" and t.get("rr_plan") is not None]
    out["mdrev_rr_plan_median"] = sorted(t["rr_plan"] for t in md)[len(md) // 2] if md else None
    return out


def fmt(s):
    if not s:
        return "n=0"
    return (f"n{s['n']:4d} {s['mean']:+.3f}±{s['se']:.3f} t{s['t']:+.1f} dir{s['direction']:+.2f}"
            f" scr{s['scratch']} tgt{s['target']} stp{s['stop']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--holdout")
    ap.add_argument("--out")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args()
    main_c = pickle.loads(Path(a.bars).read_bytes())
    hold_c = pickle.loads(Path(a.holdout).read_bytes()) if a.holdout else None
    res, window = {}, None
    for v in VARIANTS:
        tr = run(main_c, v, a.jobs)
        window = window or (min(t["session"] for t in tr), max(t["session"] for t in tr))
        res[v["name"]] = {"in_sample": summary(tr)}
        if hold_c:
            th = [t for t in run(hold_c, v, a.jobs) if not (window[0] <= t["session"] <= window[1])]
            res[v["name"]]["holdout"] = summary(th)
        for part, s in res[v["name"]].items():
            print(f"{v['name']:16s} {part:9s} all {fmt(s['all'])}\n{'':26s} mdrev {fmt(s['mdrev'])} rr~{s['mdrev_rr_plan_median']}"
                  f"\n{'':26s} fades {fmt(s['fades'])}\n{'':26s} 2nd   {fmt(s['second'])}", flush=True)
        if a.out:
            Path(a.out).write_text(json.dumps({"method": __doc__, "window": window, "variants": VARIANTS,
                                               "results": res}, indent=1, default=str))
    print("done")


if __name__ == "__main__":
    main()
