"""
Direction check: at the engine's own gap states, does trading WITH the extension (breakout) beat
trading AGAINST it (fade)? Same events, both directions, same exits, net of costs.

Events (decision time, RTH 09:35-14:55, at most one of each per ticker-session, before any trigger):
  first_break  the first bar beyond the adaptive blue band (FIRST BREAK k/K)
  fakeout      the first bar back inside the band after a break (FAKEOUT)
Extension side = the gap direction (row "dir"). Fill: next bar open, 2 bps against.
Exits: 30 / 60 min, the 15:55 close, and a 1 ATR stop / 1.5 ATR target bracket (stop first on a
two-sided bar). R = move / ATR14 (5m) at the event; net = R - 0.08% of entry / ATR.
Stats: honest.clustered by session; BO-F is the paired per-event difference.
Usage: python3 direction_check.py   (fetches 60 days of 5m bars for the first 96 equities)
"""

import collections
import json
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import engine  # noqa: E402
import honest  # noqa: E402
from data import load_universe  # noqa: E402

HERE = REPO / "data" / "backtests"
ET = ZoneInfo("America/New_York")
COST = 0.0008
SLIP = 0.0002
TICKERS = [t for t in load_universe() if not engine._is_crypto(t)][:96]


def fetch():
    HERE.mkdir(parents=True, exist_ok=True)
    cache = HERE / f"direction_check_bars_{datetime.now(ET):%Y-%m-%d}.pkl"
    if cache.exists():
        return pickle.loads(cache.read_bytes())
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    out = {}
    for t in TICKERS:
        for attempt in range(3):
            try:
                j = s.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}",
                          params={"interval": "5m", "range": "60d", "includePrePost": "true"}, timeout=20).json()
                r = j["chart"]["result"][0]
                q = r["indicators"]["quote"][0]
                df = pd.DataFrame({"Open": q["open"], "High": q["high"], "Low": q["low"], "Close": q["close"],
                                   "Volume": [v or 0 for v in q["volume"]]},
                                  index=pd.to_datetime(r["timestamp"], unit="s", utc=True)).dropna(subset=["Close"])
                out[t] = engine._prep_bars(df)
                break
            except Exception as e:  # noqa: BLE001
                print("retry", t, e, file=sys.stderr)
                time.sleep(2)
        time.sleep(0.25)
    cache.write_bytes(pickle.dumps(out))
    return out




def simulate(ev, d):
    """d = +1 breakout (with ext), -1 fade. Returns {model: gross R}."""
    side = ev["ext"] * d
    sb = ev["session_bars"]
    if len(sb) < 2:
        return None
    entry = sb[0][0] * (1 + SLIP * side)
    atr = ev["atr"]
    res = {}
    for name, n in (("time_30", 6), ("time_60", 12)):
        k = min(n, len(sb)) - 1
        res[name] = side * (sb[k][3] - entry) / atr
    res["close"] = side * (sb[-1][3] - entry) / atr
    stop = entry - side * atr
    tgt = entry + side * 1.5 * atr
    r = side * (sb[-1][3] - entry) / atr
    for o, h, l, c in sb:
        hit_stop = l <= stop if side > 0 else h >= stop
        hit_tgt = h >= tgt if side > 0 else l <= tgt
        if hit_stop:
            r = -1.0
            break
        if hit_tgt:
            r = 1.5
            break
    res["bracket"] = r
    res["_cost"] = COST * entry / atr
    return res




def events_for(args):
    t, bars = args
    days = sorted({b["d"] for b in bars})
    out = []
    for day in days[1:]:
        rth = [i for i, b in enumerate(bars) if b["d"] == day and 570 <= b["mins"] < 960]
        if len(rth) < 60:
            continue
        memo, seen = {}, set()
        for i in rth:
            m = bars[i]["mins"]
            if m + 5 < 575 or m + 5 > 895:
                continue
            h, mm = divmod(m + 5, 60)
            row = engine.analyze_bars(t, bars[: i + 1], now=datetime.fromisoformat(f"{day}T{h:02d}:{mm:02d}").replace(tzinfo=ET), memo=memo)
            mk = (row.get("_chart") or {}).get("markers") or {}
            if mk.get("trig") is not None or not row.get("dir"):
                if mk.get("trig") is not None:
                    break
                continue
            kind = None
            if mk.get("firstBreak") is not None and "first_break" not in seen:
                kind = "first_break"
            elif (row.get("fakes") or 0) >= 1 and "fakeout" not in seen:
                kind = "fakeout"
            if not kind:
                continue
            seen.add(kind)
            atr = engine._atr(bars, i)
            if not atr:
                continue
            out.append({"kind": kind, "ticker": t, "session": day, "ext": int(row["dir"]), "atr": atr,
                        "rvol": row.get("rvol"), "rvol_n": row.get("rvol_n"), "regime": row.get("regime"),
                        "gap_pct": row.get("gap_pct"), "time": bars[i]["time"], "fakes": row.get("fakes"),
                        "session_bars": [(b["o"], b["h"], b["l"], b["c"]) for b in (bars[j] for j in rth if j > i)]})
            if seen == {"first_break", "fakeout"}:
                break
    return out



if __name__ == "__main__":
    bars = fetch()
    with ProcessPoolExecutor(8) as ex:
        evs = [e for c in ex.map(events_for, bars.items()) for e in c]
    res = {}
    for kind in ("first_break", "fakeout"):
        ev = [e for e in evs if e["kind"] == kind]
        print(f"\n== {kind}: {len(ev)} events, {len({e['session'] for e in ev})} sessions; decision hour",
              sorted(collections.Counter(e['time'][-5:-3] for e in ev).items()))
        for sub, f in {"all": lambda e: True, "rvol>=2": lambda e: (e["rvol"] or 0) >= 2 and (e["rvol_n"] or 0) >= 3,
                       "chop": lambda e: e["regime"] == "chop", "trend/mixed": lambda e: e["regime"] in ("trend", "mixed")}.items():
            sel = [e for e in ev if f(e)]
            if len(sel) < 20:
                continue
            pairs = [(simulate(e, 1), simulate(e, -1), e) for e in sel]
            pairs = [p for p in pairs if p[0] and p[1]]
            line = f"  {sub:11s} n={len(pairs):4d}"
            for m in ("time_30", "time_60", "close", "bracket"):
                cl = [e["session"] for *_, e in pairs]
                diff = honest.clustered([b[m] - a[m] for b, a, _ in pairs], cl)
                bo = honest.clustered([b[m] - b["_cost"] for b, a, _ in pairs], cl)
                fa = honest.clustered([a[m] - a["_cost"] for b, a, _ in pairs], cl)
                res[f"{kind}|{sub}|{m}"] = {"bo_minus_fade": diff, "breakout_net": bo, "fade_net": fa}
                line += f" | {m}: BO−F {diff['mean']:+.2f} t{diff['t']:+.1f} (BO {bo['mean']:+.2f}, F {fa['mean']:+.2f} net)"
            print(line)
    out = REPO / "research" / f"direction_check_{datetime.now(ET):%Y-%m-%d}.json"
    json.dump({"method": __doc__, "events": len(evs), "results": res}, open(out, "w"), indent=1, default=str)
    print("wrote", out)
