#!/usr/bin/env python3
"""
moontower_deeper_check.py — four follow-ups to the Moontower lens, on the same replayed trades.

  sizing    The bracket re-cut in the options market's units: the stop moved to k × the implied daily move
            (IV30/√252 at the prior close) from the fill, the target kept or pulled inside one move, and
            every trade re-simulated on the same bars with the same next-open fill. Paired against the
            engine's own σ-band bracket, in R and in net % (the two brackets risk different amounts).
  regime    A board-level gate: how much the universe actually moved the prior session against what its
            options had priced the day before (median |daily return| / implied daily move). Sessions cut
            into terciles of that ratio; the fades' and reclaims' net R in each.
  event     The post-earnings gap as the engine's own trade and as its mirror (same bracket, other side):
            gap trades on a name that reported today or yesterday, and the ones whose gap exceeded the
            implied move IV10 had priced.
  hedge     Each trade net of its index exposure: beta to SPY from ~60 sessions of daily closes (the
            60-day bars pickle, SPY's 60-day bars; a month of cockpit returns gave betas from −0.3 to 4),
            SPY's move from the fill bar's open to the exit bar's close (5m bars), taken out of the
            trade's net % and re-expressed in R.
  vertical  Grade-A trades as the call/put vertical the contract stat quotes (long leg nearest the fill,
            short leg at the first strike beyond the target, first expiry after the session), bought at
            the prior close's asks/bids and marked by Black–Scholes at the leg's own mid IV on the stock's
            exit price (one session later) and on the close. Reads data/moontower/chains/<T>_<date>.json.

All readings are decision-time (prior close). Reads the replay snapshot, its saved bars, SPY's bars
and the git-ignored data/moontower cache; fetches nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pickle
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import replay_sessions as rs
from honest import clustered, net_r, verdict
from moontower_check import earnings_distance, implied_daily_move, prior_cockpit, sizing

CLASSIC = rs.ExitModel(name="classic")
STOP_KS = (0.5, 0.75, 1.0)


# ── shared ────────────────────────────────────────────────────────────────────────────────────────

def stat(vals: List[Optional[float]], clusters: List[str]) -> Dict[str, Any]:
    pairs = [(v, c) for v, c in zip(vals, clusters) if v is not None]
    s = clustered([v for v, _ in pairs], [c for _, c in pairs])
    s["verdict"] = verdict(s)
    s["wins"] = sum(1 for v, _ in pairs if v > 0)
    return s


def paired(a: List[float], b: List[float], clusters: List[str]) -> Dict[str, Any]:
    """b − a per trade, clustered."""
    return stat([y - x for x, y in zip(a, b)], clusters)


def load_cache(root: Path):
    ck: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for p in sorted(root.glob("cockpit_*.json")):
        rows = json.loads(p.read_text())
        rows = rows["data"] if isinstance(rows, dict) else rows
        for r in rows:
            ck[r["ticker"]][r["date"]] = r
    earn = {r["ticker"]: r for r in json.loads((root / "earnings.json").read_text())} if (root / "earnings.json").exists() else {}
    return ck, earn


def session_frames(bars_pkl: Path) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, int]]]:
    """ticker → parsed bars, and ticker → {ET day → index of its first bar}."""
    blob = pickle.load(open(bars_pkl, "rb"))
    prep, _ = rs.engine_api()
    parsed, first = {}, {}
    for t, df in (blob.get("bars") or {}).items():
        if df is None or df.empty:
            continue
        p = prep(df)
        parsed[t] = p
        f: Dict[str, int] = {}
        for i, b in enumerate(p):
            f.setdefault(b["d"], i)
        first[t] = f
    return parsed, first


def day_session(parsed, first, t: str, session: str) -> Optional[List[Dict[str, Any]]]:
    f = first.get(t) or {}
    days = list(f)
    if session not in f:
        return None
    j = days.index(session)
    day = parsed[t][f[session]: f[days[j + 1]] if j + 1 < len(days) else len(parsed[t])]
    return rs.session_bars(t, day)


def trigger_index(session: List[Dict[str, Any]], trigger_time: str) -> Optional[int]:
    for i, b in enumerate(session):
        if b.get("time") == trigger_time:
            return i
    return None


# ── sizing ────────────────────────────────────────────────────────────────────────────────────────

def resize(trade: Dict[str, Any], session: List[Dict[str, Any]], k: int, move: float,
           stop_k: float, cap_target: bool) -> Optional[Dict[str, float]]:
    t, side, entry = trade["ticker"], trade["side"], float(trade["entry"])
    dist = stop_k * move * entry
    stop = entry - dist if side == "long" else entry + dist
    target = float(trade["target"])
    if cap_target:
        lim = entry + move * entry if side == "long" else entry - move * entry
        target = min(target, lim) if side == "long" else max(target, lim)
    if (side == "long" and target <= entry) or (side == "short" and target >= entry):
        return None
    cost = rs.COST["crypto" if rs._is_crypto(t) else "equity"] * 100.0
    _, reason, r, _, _, held = rs._simulate(side, entry, stop, target, session, k, CLASSIC, t)
    risk_pct = abs(entry - stop) / entry * 100.0
    return {"r_net": round(net_r(float(r), t, entry, stop), 3), "pnl_pct_net": round(float(r) * risk_pct - cost, 4),
            "risk_pct": round(risk_pct, 3), "reason": reason, "held": held}


def sizing_study(trades, parsed, first, ck) -> Dict[str, Any]:
    variants = [(k, cap) for k in STOP_KS for cap in (False, True)]
    rows = []
    for tr in trades:
        row = prior_cockpit(ck.get(tr["ticker"], {}), tr["session"])
        move = implied_daily_move(row.get("iv_30d")) if row else None
        session = day_session(parsed, first, tr["ticker"], tr["session"])
        if move is None or session is None:
            continue
        k = trigger_index(session, tr["trigger_time"])
        if k is None:
            continue
        out = {"base": {"r_net": tr["r_net"], "pnl_pct_net": tr["pnl_pct_net"], "risk_pct": abs(tr["entry"] - tr["stop"]) / tr["entry"] * 100}}
        ok = True
        for sk, cap in variants:
            v = resize(tr, session, k, move, sk, cap)
            if v is None:
                ok = False
                break
            out[f"stop{sk}" + ("_cap" if cap else "")] = v
        if ok:
            rows.append((tr, out))
    res: Dict[str, Any] = {"n": len(rows), "variants": {}}
    for label, pick in (("A", lambda x: x["grade"] == "A"), ("A+B", lambda x: x["grade"] in ("A", "B"))):
        sub = [(t, o) for t, o in rows if pick(t)]
        cl = [t["session"] for t, _ in sub]
        block = {"base": {"r_net": stat([o["base"]["r_net"] for _, o in sub], cl),
                          "pnl_pct_net": stat([o["base"]["pnl_pct_net"] for _, o in sub], cl),
                          "risk_pct_median": round(statistics.median([o["base"]["risk_pct"] for _, o in sub]), 3) if sub else None}}
        for sk, cap in variants:
            key = f"stop{sk}" + ("_cap" if cap else "")
            vals = [o[key] for _, o in sub]
            ent = {"r_net": stat([v["r_net"] for v in vals], cl),
                   "pnl_pct_net": stat([v["pnl_pct_net"] for v in vals], cl),
                   "pnl_minus_base": paired([o["base"]["pnl_pct_net"] for _, o in sub], [v["pnl_pct_net"] for v in vals], cl),
                   "risk_pct_median": round(statistics.median([v["risk_pct"] for v in vals]), 3) if vals else None,
                   "stopped": sum(1 for v in vals if v["reason"] == "stop"), "target": sum(1 for v in vals if v["reason"] == "target")}
            for mode in ("mdrev", "gap"):
                ms = [(t, o) for t, o in sub if t["setup_mode"] == mode]
                if ms:
                    ent[f"pnl_minus_base_{mode}"] = paired([o["base"]["pnl_pct_net"] for _, o in ms], [o[key]["pnl_pct_net"] for _, o in ms], [t["session"] for t, _ in ms])
            block[key] = ent
        res["variants"][label] = block
    return res


# ── regime ────────────────────────────────────────────────────────────────────────────────────────

def regime_ratio(ck, names: List[str], session: str, lag: int) -> Optional[float]:
    """Median over names of |return on the session `lag` before| / the implied daily move priced the session before that."""
    vals = []
    for t in names:
        d = ck.get(t) or {}
        days = sorted(k for k in d if k <= session)
        if len(days) < lag + 2:
            continue
        day = days[-1 - lag] if lag else days[-1]
        prev = days[-2 - lag] if lag else days[-2]
        ret = d[day].get("daily_return"); iv = d[prev].get("iv_30d")
        m = implied_daily_move(iv)
        if ret is None or m is None:
            continue
        vals.append(abs(ret) / m)
    return statistics.median(vals) if len(vals) >= 20 else None


def regime_study(trades, ck, names) -> Dict[str, Any]:
    sessions = sorted({t["session"] for t in trades})
    prior = {s: regime_ratio(ck, names, s, 1) for s in sessions}      # decision-time: the prior session's ratio
    same = {s: regime_ratio(ck, names, s, 0) for s in sessions}       # hindsight: the session's own
    res: Dict[str, Any] = {"ratio_prior_session": {s: round(v, 3) for s, v in prior.items() if v is not None},
                           "ratio_same_session": {s: round(v, 3) for s, v in same.items() if v is not None}, "cuts": {}}
    for label, table in (("prior_session", prior), ("same_session_hindsight", same)):
        vals = sorted(v for v in table.values() if v is not None)
        if len(vals) < 6:
            continue
        lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        def tercile(s):
            v = table.get(s)
            return None if v is None else ("quiet" if v < lo else "mid" if v < hi else "lively")
        block = {"cuts": [round(lo, 3), round(hi, 3)], "by": {}}
        for grade_label, pick in (("A", lambda x: x["grade"] == "A"), ("A+B", lambda x: x["grade"] in ("A", "B"))):
            for mode in ("all", "mdrev", "gap"):
                for ter in ("quiet", "mid", "lively"):
                    sub = [t for t in trades if pick(t) and (mode == "all" or t["setup_mode"] == mode) and tercile(t["session"]) == ter]
                    if sub:
                        block["by"][f"{grade_label}|{mode}|{ter}"] = {**stat([t["r_net"] for t in sub], [t["session"] for t in sub]),
                                                                       "mirror": stat([t.get("mirror_r_net") for t in sub], [t["session"] for t in sub])}
        res["cuts"][label] = block
    return res


# ── event ─────────────────────────────────────────────────────────────────────────────────────────

def event_study(trades, ck, earn) -> Dict[str, Any]:
    rows = []
    for t in trades:
        if t["setup_mode"] != "gap":
            continue
        e = earnings_distance(earn.get(t["ticker"]), t["session"])
        row = prior_cockpit(ck.get(t["ticker"], {}), t["session"])
        z = sizing(row, t)
        rows.append((t, e, z["gap_moves"]))
    res = {}
    for label, pick in (("A+B", lambda x: x["grade"] in ("A", "B")),):
        g = [(t, e, gm) for t, e, gm in rows if pick(t)]
        def both(sub, name):
            cl = [t["session"] for t, _, _ in sub]
            res[f"{label}|{name}"] = {"trade": stat([t["r_net"] for t, _, _ in sub], cl),
                                      "mirror": stat([t.get("mirror_r_net") for t, _, _ in sub], cl),
                                      "mirror_minus_trade": paired([t["r_net"] for t, _, _ in sub], [t.get("mirror_r_net") or 0.0 for t, _, _ in sub], cl)}
        both(g, "all gap trades")
        both([x for x in g if x[1] is not None and 0 <= x[1] <= 1], "post-earnings (reported today or yesterday)")
        both([x for x in g if x[1] is not None and 0 <= x[1] <= 1 and x[2] is not None and x[2] >= 1], "post-earnings, gap ≥ implied move")
        both([x for x in g if x[2] is not None and x[2] >= 1], "any gap ≥ implied move")
        both([x for x in g if x[2] is not None and x[2] < 1], "any gap < implied move")
        both([x for x in g if not (x[1] is not None and 0 <= x[1] <= 1)], "no report today or yesterday")
    return res


# ── hedge ─────────────────────────────────────────────────────────────────────────────────────────

def daily_returns(parsed: List[Dict[str, Any]], ticker: str) -> Dict[str, float]:
    """Session close to session close from parsed 5m bars (the replay's own session cut)."""
    closes: Dict[str, float] = {}
    first: Dict[str, int] = {}
    for i, b in enumerate(parsed):
        first.setdefault(b["d"], i)
    days = list(first)
    for j, d in enumerate(days):
        day = parsed[first[d]: first[days[j + 1]] if j + 1 < len(days) else len(parsed)]
        sess = rs.session_bars(ticker, day)
        if sess:
            closes[d] = float(sess[-1]["c"])
    out = {}
    for a, b in zip(days, days[1:]):
        if a in closes and b in closes and closes[a] > 0:
            out[b] = closes[b] / closes[a] - 1
    return out


def betas(returns: Dict[str, Dict[str, float]], spy: Dict[str, float], names, min_days: int = 30) -> Dict[str, float]:
    """Regression slope of each name's daily return on SPY's, on the days both have."""
    out = {}
    for t in names:
        xs, ys = [], []
        for d, y in (returns.get(t) or {}).items():
            if d in spy:
                xs.append(spy[d]); ys.append(y)
        if len(xs) >= min_days:
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            vx = sum((x - mx) ** 2 for x in xs)
            if vx > 0:
                out[t] = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
    return out


def hedge_study(trades, ck, spy_rows, spy_parsed, spy_first, betamap) -> Dict[str, Any]:
    rows = []
    for t in trades:
        b = betamap.get(t["ticker"])
        session = day_session(spy_parsed, spy_first, "SPY", t["session"])
        if b is None or session is None:
            continue
        k = trigger_index(session, t["trigger_time"])
        if k is None:
            continue
        fill_i, exit_i = k + 1, k + int(t["bars_held"])
        if exit_i >= len(session) or fill_i >= len(session):
            continue
        spy_ret = float(session[exit_i]["c"]) / float(session[fill_i]["o"] or session[fill_i]["c"]) - 1
        sign = 1 if t["side"] == "long" else -1
        risk_pct = abs(t["entry"] - t["stop"]) / t["entry"] * 100
        hedged_pct = t["pnl_pct_net"] - sign * b * spy_ret * 100
        rows.append((t, b, spy_ret, hedged_pct, hedged_pct / risk_pct if risk_pct else None))
    bq = sorted(betamap.values())
    res = {"n": len(rows), "betas": len(betamap), "beta_q10_50_90": [round(bq[len(bq) // 10], 2), round(bq[len(bq) // 2], 2), round(bq[9 * len(bq) // 10], 2)] if len(bq) >= 10 else None, "by": {}}
    for label, pick in (("A", lambda x: x["grade"] == "A"), ("A+B", lambda x: x["grade"] in ("A", "B"))):
        for mode in ("all", "mdrev", "gap"):
            for side in ("all", "long", "short"):
                sub = [r for r in rows if pick(r[0]) and (mode == "all" or r[0]["setup_mode"] == mode) and (side == "all" or r[0]["side"] == side)]
                if len(sub) < 5:
                    continue
                cl = [r[0]["session"] for r in sub]
                res["by"][f"{label}|{mode}|{side}"] = {"r_net": stat([r[0]["r_net"] for r in sub], cl), "r_hedged": stat([r[4] for r in sub], cl),
                                                     "hedged_minus_raw": paired([r[0]["r_net"] for r in sub], [r[4] or 0.0 for r in sub], cl),
                                                     "index_share_of_pnl": round(statistics.median([abs(r[1] * r[2] * 100) / max(1e-9, abs(r[0]["pnl_pct_net"])) for r in sub]), 3)}
    return res


# ── vertical ──────────────────────────────────────────────────────────────────────────────────────

def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(kind: str, s: float, k: float, t_years: float, iv: float, r: float = 0.04) -> float:
    if t_years <= 0 or iv <= 0:
        return max(0.0, s - k) if kind == "call" else max(0.0, k - s)
    d1 = (math.log(s / k) + (r + iv * iv / 2) * t_years) / (iv * math.sqrt(t_years))
    d2 = d1 - iv * math.sqrt(t_years)
    if kind == "call":
        return s * _ncdf(d1) - k * math.exp(-r * t_years) * _ncdf(d2)
    return k * math.exp(-r * t_years) * _ncdf(-d2) - s * _ncdf(-d1)


def pick_vertical(chain: List[Dict[str, Any]], side: str, entry: float, target: float, session: str) -> Optional[Dict[str, Any]]:
    """Mirrors contract.js bracketContract: first expiry after the session, long nearest entry, short first beyond target."""
    exps = sorted({r["expiry_date"] for r in chain if r.get("expiry_date") and r["expiry_date"] > session})
    if not exps:
        return None
    rows = [r for r in chain if r["expiry_date"] == exps[0] and r.get("strike") is not None]
    if not rows:
        return None
    long = min(rows, key=lambda r: (abs(r["strike"] - entry), r["strike"]))
    beyond = [r for r in rows if (r["strike"] >= target if side == "long" else r["strike"] <= target)]
    short = min(beyond or rows, key=lambda r: (abs(r["strike"] - target), r["strike"]))
    if short["strike"] == long["strike"]:
        return None
    leg = "call" if side == "long" else "put"
    la, lm, sb, sm = long.get(f"{leg}_ask_price"), long.get(f"{leg}_mid_price"), short.get(f"{leg}_bid_price"), short.get(f"{leg}_mid_price")
    if None in (la, lm, sb, sm):
        return None
    return {"expiry": exps[0], "dte": long.get("dte"), "leg": leg, "long": long, "short": short,
            "debit_ask": round(la - sb, 3), "debit_mid": round(lm - sm, 3), "width": abs(short["strike"] - long["strike"])}


def vertical_study(trades, chains_dir: Path, parsed, first) -> Dict[str, Any]:
    sessions = sorted({t["session"] for t in trades})
    prev = {s: sessions[i - 1] for i, s in enumerate(sessions) if i}
    rows = []
    for t in trades:
        if t["grade"] != "A" or t["session"] not in prev:
            continue
        p = chains_dir / f"{t['ticker']}_{prev[t['session']]}.json"
        if not p.exists():
            continue
        v = pick_vertical(json.loads(p.read_text()), t["side"], float(t["entry"]), float(t["target"]), t["session"])
        if not v or v["debit_ask"] <= 0:
            continue
        leg, lg, sh = v["leg"], v["long"], v["short"]
        iv_l = (lg.get(f"{leg}_mid_iv") or 0) / 100; iv_s = (sh.get(f"{leg}_mid_iv") or iv_l * 100) / 100
        t_years = max(0.0, ((v["dte"] or 1) - 1)) / 365
        def mark(spot):
            return bs(leg, spot, lg["strike"], t_years, iv_l) - bs(leg, spot, sh["strike"], t_years, iv_s)
        session = day_session(parsed, first, t["ticker"], t["session"])
        close = float(session[-1]["c"]) if session else None
        at_exit = mark(float(t["exit"]))
        rows.append({"ticker": t["ticker"], "session": t["session"], "side": t["side"], "debit": v["debit_ask"], "width": v["width"],
                     "mark_exit": round(at_exit, 3), "mark_close": round(mark(close), 3) if close else None,
                     "ret_exit": (at_exit - v["debit_ask"]) / v["debit_ask"], "ret_close": (mark(close) - v["debit_ask"]) / v["debit_ask"] if close else None,
                     "stock_r_net": t["r_net"], "stock_pnl_pct_net": t["pnl_pct_net"],
                     "spread_rr": round((v["width"] - v["debit_ask"]) / v["debit_ask"], 2), "fill": round(float(t["entry"]), 2)})
    cl = [r["session"] for r in rows]
    return {"n": len(rows), "trades": rows,
            "spread_return_at_stock_exit": stat([r["ret_exit"] for r in rows], cl),
            "spread_return_held_to_close": stat([r["ret_close"] for r in rows], cl),
            "stock_r_net_same_trades": stat([r["stock_r_net"] for r in rows], cl),
            "stock_pnl_pct_same_trades": stat([r["stock_pnl_pct_net"] for r in rows], cl),
            "note": "spread return = (Black–Scholes mark at the leg's own mid IV, one day of decay, minus the prior close's debit at ask/bid) ÷ debit; no intraday option quotes exist, so this is a model mark, not a fill"}


# ── main ──────────────────────────────────────────────────────────────────────────────────────────

def fmt(s: Dict[str, Any], unit="R") -> str:
    if not s.get("n"):
        return "n=0"
    return f"n={s['n']:>4} {s['mean']:+.3f}{unit}" + (f" ± {s['se']:.3f}" if s.get("se") is not None else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", default="research/replay_2026-10-30_v1.7.0.json")
    ap.add_argument("--bars", default="data/backtests/equity_bars_2026-10-30.pkl")
    ap.add_argument("--spy-bars", default="data/backtests/spy_bars_2026-10-30.pkl")
    ap.add_argument("--beta-bars", default="data/backtests/holdout_60d_2026-10-19.pkl", help="60 days of 5m bars for the betas")
    ap.add_argument("--cache", default="data/moontower")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    rep = json.loads(Path(a.replay).read_text())
    trades = [t for t in rep["trades"] if t.get("model") == "classic"]
    ck, earn = load_cache(Path(a.cache))
    spy_rows = ck.pop("SPY", {})
    names = sorted({t["ticker"] for t in trades if t["ticker"] in ck})
    trades = [t for t in trades if t["ticker"] in ck]
    parsed, first = session_frames(Path(a.bars))
    spy_parsed, spy_first = session_frames(Path(a.spy_bars))
    res = {"replay": a.replay, "trades": len(trades), "names": len(names), "window": rep.get("method", {}).get("window")}
    res["sizing"] = sizing_study(trades, parsed, first, ck)
    res["regime"] = regime_study(trades, ck, names)
    res["event"] = event_study(trades, ck, earn)
    beta_parsed, _ = session_frames(Path(a.beta_bars))
    rets = {t: daily_returns(p, t) for t, p in beta_parsed.items()}
    bm = betas(rets, daily_returns(spy_parsed["SPY"], "SPY"), names)
    res["hedge"] = hedge_study(trades, ck, spy_rows, spy_parsed, spy_first, bm)
    res["vertical"] = vertical_study(trades, Path(a.cache) / "chains", parsed, first)
    out = Path(a.out or f"research/moontower_deeper_check_{dt.date.today().isoformat()}.json")
    out.write_text(json.dumps(res, indent=1))

    print(f"{len(trades)} classic trades on {len(names)} covered names")
    print("\n== sizing (net %, paired vs the engine's bracket; stops at k × implied daily move)")
    for label, block in res["sizing"]["variants"].items():
        print(f"  grade {label}: base {fmt(block['base']['r_net'])} | {fmt(block['base']['pnl_pct_net'], '%')}  median risk {block['base']['risk_pct_median']}%")
        for key, ent in block.items():
            if key == "base":
                continue
            d = ent["pnl_minus_base"]
            print(f"    {key:<14} {fmt(ent['r_net'])} | {fmt(ent['pnl_pct_net'], '%')}  Δ {d['mean']:+.3f}% ± {d['se'] or 0:.3f} (t {d['t']})  risk {ent['risk_pct_median']}%  stopped {ent['stopped']} target {ent['target']}"
                  + "".join(f"  {m}: Δ {ent[f'pnl_minus_base_{m}']['mean']:+.3f} (t {ent[f'pnl_minus_base_{m}']['t']})" for m in ("mdrev", "gap") if f"pnl_minus_base_{m}" in ent))
    print("\n== regime (terciles of the prior session's |move| / implied move across the universe)")
    for label, block in res["regime"]["cuts"].items():
        print(f"  {label}: cuts {block['cuts']}")
        for key, s in block["by"].items():
            print(f"    {key:<18} {fmt(s)}  win {s['wins']}/{s['n']}  mirror {s['mirror']['mean']:+.3f}")
    print("\n== event (gap trades: the engine's fade vs its mirror)")
    for key, s in res["event"].items():
        print(f"  {key:<50} trade {fmt(s['trade'])}  mirror {fmt(s['mirror'])}  mirror−trade {s['mirror_minus_trade']['mean']:+.3f} (t {s['mirror_minus_trade']['t']})")
    print(f"\n== hedge (beta to SPY from ~60 sessions, q10/50/90 {res['hedge']['beta_q10_50_90']}; {res['hedge']['n']} trades with SPY bars)")
    for key, s in res["hedge"]["by"].items():
        print(f"  {key:<18} raw {fmt(s['r_net'])}  hedged {fmt(s['r_hedged'])}  Δ {s['hedged_minus_raw']['mean']:+.3f} (t {s['hedged_minus_raw']['t']})  index share of |pnl| median {s['index_share_of_pnl']}")
    v = res["vertical"]
    print(f"\n== vertical ({v['n']} grade-A trades with a prior-close chain)")
    print(f"  spread at the stock's exit {fmt(v['spread_return_at_stock_exit'], 'x')}   held to the close {fmt(v['spread_return_held_to_close'], 'x')}   same trades in stock {fmt(v['stock_r_net_same_trades'])} / {fmt(v['stock_pnl_pct_same_trades'], '%')}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
