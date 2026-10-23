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

`clustered()` returns the cluster-robust mean, SE, t and a 90% interval; `verdict()` labels a
result positive / negative only across at least MIN_CLUSTERS sessions (a cluster-robust SE from a
handful of sessions is itself unreliable) and only when |t| clears the two-sided 5% critical value
of Student's t with (sessions - 1) degrees of freedom, never less than 2 (2.26 at 10 sessions,
2.09 at 21, 2.00 at 61); otherwise inconclusive or insufficient. The interval uses t as well.
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
    q = t_quantile(g - 1, _T95)
    return {"n": n, "clusters": g, "mean": round(mu, 4), "se": round(se, 4),
            "t": round(t, 2) if t is not None else None,
            "ci90": [round(mu - q * se, 4), round(mu + q * se, 4)]}


MIN_CLUSTERS = 10

# Student's t quantiles: two-sided 5% (0.975) and the 90% interval's 0.95, by degrees of freedom
_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
         10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110,
         18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
         26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}
_T95 = {1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015, 6: 1.943, 7: 1.895, 8: 1.860, 9: 1.833,
        10: 1.812, 11: 1.796, 12: 1.782, 13: 1.771, 14: 1.761, 15: 1.753, 16: 1.746, 17: 1.740,
        18: 1.734, 19: 1.729, 20: 1.725, 21: 1.721, 22: 1.717, 23: 1.714, 24: 1.711, 25: 1.708,
        26: 1.706, 27: 1.703, 28: 1.701, 29: 1.699, 30: 1.697, 40: 1.684, 60: 1.671, 120: 1.658}


def t_quantile(df: int, table: Dict[int, float]) -> float:
    """Tabled quantile; between table rows (df > 30) interpolated in 1/df, which is close to exact."""
    df = max(1, int(df))
    if df in table:
        return table[df]
    keys = sorted(table)
    if df > keys[-1]:
        lo, hi = keys[-1], math.inf
        vlo, vhi = table[lo], (1.960 if table is _T975 else 1.645)
    else:
        lo = max(k for k in keys if k < df)
        hi = min(k for k in keys if k > df)
        vlo, vhi = table[lo], table[hi]
    w = (1 / lo - 1 / df) / (1 / lo - (0 if hi == math.inf else 1 / hi))
    return vlo + w * (vhi - vlo)


def t_critical(clusters: int) -> float:
    """|t| a verdict needs with this many sessions: Student's t(G-1) at two-sided 5%, at least 2."""
    return max(2.0, t_quantile(clusters - 1, _T975))


def verdict(stat: Dict[str, Any], min_clusters: int = MIN_CLUSTERS) -> str:
    t = stat.get("t")
    if t is None or (stat.get("clusters") or 0) < min_clusters:
        return "insufficient"
    crit = t_critical(int(stat["clusters"]))
    return "positive" if t >= crit else "negative" if t <= -crit else "inconclusive"


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
