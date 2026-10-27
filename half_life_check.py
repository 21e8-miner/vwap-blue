"""
Does the reclaim pay where the VWAP deviation snaps back fast? (idea from 21e8-miner/holographic-vwap-noise)

For every decision-time trade of the base engine (floor B, next-bar fills, flat by 16:00, net of costs), the
Ornstein-Uhlenbeck half-life of price - session VWAP over the 60 bars ending at the trigger bar: an AR(1)
fit dev_t = beta * dev_{t-1} + e, theta = -ln(beta) per bar, half-life = ln 2 / theta in minutes (5m bars);
beta >= 1 (no reversion) = inf. Each bar's deviation is from its own session's VWAP (04:00 anchor, as the
engine's blue). Trades are then cut by half-life tercile within each class, with the mirror control, so
the read is "did the edge sit in the fast-reverting names", not a new gate.

  python3 half_life_check.py --bars <60d pkl> --out research/half_life_check_<date>.json
  python3 half_life_check.py --replay research/replay_crypto_..._v1.7.0.json --bars data/backtests/crypto_bv_365d.pkl
"""
import argparse
import json
import math
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402
import honest  # noqa: E402
import second_look_check as slc  # noqa: E402

LOOKBACK = 60
TP = lambda b: (b["h"] + b["l"] + b["c"]) / 3.0


def deviations(parsed, opts):
    """price - session VWAP for every bar (None before the session has volume), the engine's blue."""
    anchor, close_m = opts["anchor_mins"], opts["close_mins"]
    out, d, bp, bv = [], None, 0.0, 0.0
    for b in parsed:
        if b["d"] != d:
            d, bp, bv = b["d"], 0.0, 0.0
        if anchor <= b["mins"] < close_m and b["v"] > 0:
            bp += TP(b) * b["v"]
            bv += b["v"]
        out.append(b["c"] - bp / bv if bv > 0 else None)
    return out


def half_life_min(devs, i, bar_min=5.0, n=LOOKBACK):
    xs = [v for v in devs[max(0, i - n + 1): i + 1] if v is not None]
    if len(xs) < 20:
        return None
    x, y = xs[:-1], xs[1:]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx <= 0:
        return None
    beta = sum((a - mx) * (b - my) for a, b in zip(x, y)) / sxx
    if beta >= 0.999 or beta <= 0:
        return math.inf
    return math.log(2) / -math.log(beta) * bar_min


def attach(trades, bars):
    """half_life (minutes) on every trade whose trigger bar is found in its ticker's bars."""
    by_t = {}
    for t in trades:
        by_t.setdefault(t["ticker"], []).append(t)
    for ticker, tl in by_t.items():
        df = bars.get(ticker)
        if df is None:
            continue
        parsed = engine._prep_bars(df)
        opts = {"anchor_mins": 0 if engine._is_crypto(ticker) else engine.DEFAULT_ANCHOR_M,
                "close_mins": engine.CRYPTO_CLOSE_M if engine._is_crypto(ticker) else engine.RTH_CLOSE_M}
        devs = deviations(parsed, opts)
        idx = {(b["d"], b["time"]): i for i, b in enumerate(parsed)}
        for t in tl:
            i = idx.get((t["session"], t["trigger_time"]))
            t["half_life"] = half_life_min(devs, i) if i is not None else None
    return trades


def cut(trades, label):
    sel = [t for t in trades if t.get("half_life") is not None]
    if len(sel) < 30:
        return {}
    fin = sorted(t["half_life"] for t in sel if math.isfinite(t["half_life"]))
    q1, q2 = fin[len(fin) // 3], fin[2 * len(fin) // 3]
    bins = {"fast": lambda h: h <= q1, "mid": lambda h: q1 < h <= q2, "slow": lambda h: h > q2}
    out = {"n": len(sel), "cuts_min": [round(q1, 1), round(q2, 1)], "none": sum(1 for t in sel if not math.isfinite(t["half_life"]))}
    for name, f in bins.items():
        g = [t for t in sel if f(t["half_life"])]
        if not g:
            continue
        out[name] = slc.stat(g)
        out[name]["median_half_life"] = sorted(t["half_life"] for t in g)[len(g) // 2]
    print(f"{label:28s} n{out['n']} cuts {out['cuts_min']} no-reversion {out['none']}")
    for name in bins:
        if name in out:
            print(f"   {name:5s} {slc.fmt(out[name])} t½~{out[name]['median_half_life']:.0f}m")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True)
    ap.add_argument("--replay", help="use this replay's classic trades instead of replaying the bars at floor B")
    ap.add_argument("--out")
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args()
    cache = pickle.loads(Path(a.bars).read_bytes())
    if a.replay:
        trades = [t for t in json.loads(Path(a.replay).read_text())["trades"] if t.get("model") == "classic"]
    else:
        trades = slc.run(cache, slc.VARIANTS[0], a.jobs)
    attach(trades, cache["bars"])
    res = {}
    for label, f in (("all", lambda t: True), ("mdrev", lambda t: t["setup_mode"] == "mdrev"),
                     ("mdrev long", lambda t: t["setup_mode"] == "mdrev" and t["side"] == "long"),
                     ("mdrev short", lambda t: t["setup_mode"] == "mdrev" and t["side"] == "short"),
                     ("fades", lambda t: t["setup_mode"] != "mdrev")):
        r = cut([t for t in trades if f(t)], label)
        if r:
            res[label] = r
    if a.out:
        Path(a.out).write_text(json.dumps({"method": __doc__, "source": a.replay or a.bars, "results": res,
                                           "trades": [{k: t.get(k) for k in ("ticker", "session", "trigger_time", "setup_mode", "side",
                                                                               "r_net", "mirror_r_net", "exit_reason", "half_life")} for t in trades]},
                                          indent=1, default=str))
        print("wrote", a.out)


if __name__ == "__main__":
    main()
