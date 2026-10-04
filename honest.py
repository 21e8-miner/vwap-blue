"""
Honest evaluation helpers shared by the session replay and the forward signal ledger.

Two corrections every expectancy number in this repo needs:

  cost in R    A fixed round-trip cost is a large fraction of a tight stop:
                   cost_R = cost% / risk%,   risk% = |entry - stop| / entry
               With a median risk of ~1.2% of price, 0.08% round trip is ~0.07R, and much
               more on tight stops. Gross R overstates the edge by exactly that much.

  clustering   Setups on the same session share the tape: a gap day moves many names the
               same way. The standard error of average R must be clustered by session, not
               computed as if every trade were independent. 419 trades from 22 sessions carry
               roughly 22 independent draws of the market, not 419.

`clustered()` returns the cluster-robust mean, SE, t and an approximate 90% interval;
`verdict()` labels a result positive / negative only when |t| >= 2 across at least
MIN_CLUSTERS sessions (a cluster-robust SE from a handful of sessions is itself unreliable),
otherwise inconclusive or insufficient - with one month of sessions most segments are.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

COST = {"equity": 0.0008, "crypto": 0.0015}     # round trip, fraction of price (same as the replay)


def is_crypto(ticker: str) -> bool:
    t = (ticker or "").upper()
    return t.endswith(("-USD", "-USDT", "-USDC"))


def cost_pct(ticker: str) -> float:
    """Round-trip cost in percent of price."""
    return COST["crypto" if is_crypto(ticker) else "equity"] * 100.0


def risk_pct(entry: float, stop: float) -> float:
    return abs(entry - stop) / entry * 100.0 if entry else 0.0


def cost_r(ticker: str, entry: float, stop: float) -> float:
    rp = risk_pct(entry, stop)
    return cost_pct(ticker) / rp if rp > 0 else float("inf")


def net_r(r_gross: float, ticker: str, entry: float, stop: float) -> float:
    return r_gross - cost_r(ticker, entry, stop)


def clustered(values: Sequence[float], clusters: Sequence[Any]) -> Dict[str, Any]:
    """Mean with a cluster-robust (CR1) standard error; clusters are sessions."""
    n = len(values)
    if n == 0:
        return {"n": 0, "clusters": 0, "mean": None, "se": None, "t": None, "ci90": None}
    mu = sum(values) / n
    groups: Dict[Any, List[float]] = defaultdict(list)
    for v, c in zip(values, clusters):
        groups[c].append(v)
    g = len(groups)
    if g < 2:
        return {"n": n, "clusters": g, "mean": round(mu, 4), "se": None, "t": None, "ci90": None}
    s = sum((sum(x - mu for x in xs)) ** 2 for xs in groups.values())
    se = math.sqrt(g / (g - 1) * s) / n
    t = mu / se if se > 0 else None
    return {"n": n, "clusters": g, "mean": round(mu, 4), "se": round(se, 4),
            "t": round(t, 2) if t is not None else None,
            "ci90": [round(mu - 1.645 * se, 4), round(mu + 1.645 * se, 4)]}


MIN_CLUSTERS = 10


def verdict(stat: Dict[str, Any], min_clusters: int = MIN_CLUSTERS) -> str:
    t = stat.get("t")
    if t is None or (stat.get("clusters") or 0) < min_clusters:
        return "insufficient"
    return "positive" if t >= 2.0 else "negative" if t <= -2.0 else "inconclusive"


def segments(rows: Iterable[Dict[str, Any]], keys: Sequence[str], value: str = "r_net",
             cluster: str = "session", min_n: int = 5) -> List[Dict[str, Any]]:
    """Clustered stats per combination of `keys` (e.g. setup_mode x regime)."""
    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[tuple(r.get(k) or "?" for k in keys)].append(r)
    out = []
    for combo, sub in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(sub) < min_n:
            continue
        stat = clustered([float(r[value]) for r in sub], [r.get(cluster) for r in sub])
        out.append({**dict(zip(keys, combo)), **stat, "verdict": verdict(stat)})
    return out


def fmt_stat(stat: Dict[str, Any], unit: str = "R") -> str:
    if not stat.get("n"):
        return "n=0"
    if stat.get("se") is None:
        return f"{stat['mean']:+.3f}{unit} (n={stat['n']}, {stat['clusters']} session)"
    return (f"{stat['mean']:+.3f}{unit} ± {stat['se']:.3f} (t={stat['t']:+.1f}, 90% CI "
            f"{stat['ci90'][0]:+.2f}…{stat['ci90'][1]:+.2f}, n={stat['n']}, {stat['clusters']} sessions) "
            f"→ {verdict(stat)}")
