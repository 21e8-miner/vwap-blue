"""
Peer residualization on the crypto year: is a reclaim a name's own move or the whole basket's?

For every classic trade of a crypto replay, at its trigger bar: z = (close - blue) / blue's volume-weighted
sigma for the name, and for every other crypto name in the bars file at the same bar time (its own session
VWAP and sigma); z_basket = the peers' mean, dz = z - z_basket. Cuts, each with the mirror control:
  * "with the basket" vs "against it": does the basket's sign agree with the name's deviation?
  * |dz| tercile: idiosyncratic dislocation (the information_dislocation claim: raw z loses, |dz| >= 0.7 sigma wins)
  * basket breadth: share of peers below their VWAP at the bar (the holographic repo's cascade lock)

  python3 basket_check.py --replay research/replay_crypto_..._v1.7.0.json --bars data/backtests/crypto_bv_365d.pkl --out ...
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
import second_look_check as slc  # noqa: E402

TP = lambda b: (b["h"] + b["l"] + b["c"]) / 3.0


def z_series(parsed):
    """{(d, time): z} for every bar with a session VWAP and sigma (crypto: anchor 00:00, whole ET day)."""
    out, d, bp, bv, bp2 = {}, None, 0.0, 0.0, 0.0
    for b in parsed:
        if b["d"] != d:
            d, bp, bv, bp2 = b["d"], 0.0, 0.0, 0.0
        if b["v"] > 0:
            tp = TP(b)
            bp += tp * b["v"]; bv += b["v"]; bp2 += tp * tp * b["v"]
        if bv > 0:
            blue = bp / bv
            sig = max(bp2 / bv - blue * blue, 0.0) ** 0.5
            if sig > 0:
                out[(b["d"], b["time"])] = (b["c"] - blue) / sig
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", required=True)
    ap.add_argument("--bars", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    cache = pickle.loads(Path(a.bars).read_bytes())
    names = [t for t in cache["bars"] if engine._is_crypto(t)]
    zs = {t: z_series(engine._prep_bars(cache["bars"][t])) for t in names}
    print(f"{len(names)} crypto names with z series")
    trades = [t for t in json.loads(Path(a.replay).read_text())["trades"] if t.get("model") == "classic"]
    kept = []
    for t in trades:
        key = (t["session"], t["trigger_time"])
        own = zs.get(t["ticker"], {}).get(key)
        peers = [zs[n][key] for n in names if n != t["ticker"] and key in zs[n]]
        if own is None or len(peers) < 5:
            continue
        zb = sum(peers) / len(peers)
        t.update({"z": own, "z_basket": zb, "dz": own - zb, "breadth_below": sum(1 for p in peers if p < 0) / len(peers),
                  "with_basket": (own < 0) == (zb < 0)})
        kept.append(t)
    print(f"{len(kept)}/{len(trades)} trades with a basket reading")
    res = {}
    def report(label, groups):
        res[label] = {}
        print(label)
        for name, sel in groups:
            if len(sel) >= 20:
                res[label][name] = slc.stat(sel)
                print(f"   {name:34s} {slc.fmt(res[label][name])}")
    for cls, f in (("all", lambda t: True), ("long", lambda t: t["side"] == "long"), ("short", lambda t: t["side"] == "short")):
        sel = [t for t in kept if f(t)]
        report(f"{cls} · basket sign", [("with the basket (same sign)", [t for t in sel if t["with_basket"]]),
                                        ("against the basket", [t for t in sel if not t["with_basket"]])])
        dz = sorted(abs(t["dz"]) for t in sel)
        if len(dz) >= 60:
            q1, q2 = dz[len(dz) // 3], dz[2 * len(dz) // 3]
            report(f"{cls} · |dz| tercile (cuts {q1:.2f}, {q2:.2f})",
                   [("low |dz| (moves with beta)", [t for t in sel if abs(t["dz"]) <= q1]),
                    ("mid", [t for t in sel if q1 < abs(t["dz"]) <= q2]),
                    ("high |dz| (idiosyncratic)", [t for t in sel if abs(t["dz"]) > q2])])
        report(f"{cls} · breadth below VWAP",
               [("<= 40% of peers below", [t for t in sel if t["breadth_below"] <= 0.4]),
                ("40-80%", [t for t in sel if 0.4 < t["breadth_below"] <= 0.8]),
                ("> 80% (cascade)", [t for t in sel if t["breadth_below"] > 0.8])])
    if a.out:
        Path(a.out).write_text(json.dumps({"method": __doc__, "source": a.replay, "names": names, "results": res,
                                           "trades": [{k: t.get(k) for k in ("ticker", "session", "trigger_time", "side", "r_net", "mirror_r_net",
                                                                               "z", "z_basket", "dz", "breadth_below")} for t in kept]},
                                          indent=1, default=str))
        print("wrote", a.out)


if __name__ == "__main__":
    main()
