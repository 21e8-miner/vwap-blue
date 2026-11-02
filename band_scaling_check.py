"""
How does price's distance from blue grow through the session, and does the engine's sigma band keep pace?

The engine confirms a fade once the close is beyond blue by band = sigma * SIGMA_MULT * adapt(KER) and buffers
stops by 0.35 * sigma * adapt, where sigma is the running volume-weighted spread of the typical price about
blue (both from the 04:00 anchor for equities, midnight ET for crypto). That band has a shape in time: if
price wanders diffusively, |close - blue| and sigma both grow like sqrt(t) and the band keeps the same
meaning all day; if sessions trend, |close - blue| grows faster than sigma and the band gets easier to
cross late. This script measures both from cached bars, exactly as the engine accumulates them:

  * rms(close - blue)/blue and rms(sigma)/blue per 5-minute slot since the open (slot = bar start), pooled over sessions,
    and the log-log slope H of each over the session (0.5 = diffusive, 1 = ballistic), with a bootstrap
    over sessions (ticker-days) for the CI
  * the same under two shuffles, the diffusive references for THIS data instead of a textbook 0.5:
      - within-session: each day's returns permuted in time (same returns and volume by slot, no serial
        order, but the open's volatility spread evenly over the day)
      - cross-session: each time slot's returns permuted across the ticker's days (keeps the volatility
        profile of the day and each slot's return distribution; kills the within-day ordering). This is the
        fair reference: a day whose distance from blue grows like this null is a random walk with the
        market's own time-of-day volatility
  * the share of bars beyond the confirm band and beyond the stop buffer by hour, and the same split by
    the bar's own KER regime (chop / mixed / trend, the engine's thresholds)

Read: the exponents against their shuffled twins, and whether the beyond-band share drifts across the day.
Nothing here is a gate; it is a reading of an assumption the engine already makes.

  python3 band_scaling_check.py --bars data/backtests/holdout_60d_2026-10-19.pkl --out research/band_scaling_equity.json
  python3 band_scaling_check.py --bars data/backtests/crypto_bv_365d.pkl --crypto --out research/band_scaling_crypto.json
"""
import argparse
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402

BAR_MIN = 5
FIT_FROM_MIN = 15          # first slots sit on a blue that is mostly premarket; fit from here
BOOT = 300
REGIMES = ("chop", "mixed", "trend")
VR_K = (2, 3, 6, 12, 24, 48, 78)   # bars: 10 min .. 6.5 h


def vr_sums(c_, windows):
    """Sums behind the variance ratio of k-bar returns to 1-bar returns, over each day's anchored window."""
    s2 = {k: 0.0 for k in VR_K}
    n2 = {k: 0 for k in VR_K}
    r2, nr = 0.0, 0
    for win in windows:
        r = np.diff(np.log(c_[win]))
        r = r[np.isfinite(r)]
        r2 += float((r * r).sum())
        nr += len(r)
        cs = np.concatenate([[0.0], np.cumsum(r)])
        for k in VR_K:
            if len(r) >= k:
                S = cs[k:] - cs[:-k]
                s2[k] += float((S * S).sum())
                n2[k] += len(S)
    return {"s2": s2, "n2": n2, "r2": r2, "nr": nr}


def vr_merge(parts):
    out = {"s2": {k: 0.0 for k in VR_K}, "n2": {k: 0 for k in VR_K}, "r2": 0.0, "nr": 0}
    for p in parts:
        for k in VR_K:
            out["s2"][k] += p["s2"][k]
            out["n2"][k] += p["n2"][k]
        out["r2"] += p["r2"]
        out["nr"] += p["nr"]
    return out


def vr_ratio(v):
    base = v["r2"] / v["nr"] if v["nr"] else float("nan")
    return {k: (v["s2"][k] / v["n2"][k]) / (k * base) if v["n2"][k] and base > 0 else float("nan") for k in VR_K}


def _within_day_cumsum(x, day_start_of):
    cs = np.cumsum(x)
    offs = cs[day_start_of] - x[day_start_of]
    return cs - offs


def _ker_vec(c, lookback=engine.DEFAULT_KER_LOOKBACK):
    """engine._ker on every bar at once: |c_i - c_{i-L+1}| / sum |dc| over the window; NaN for the first L-1."""
    n = len(c)
    ker = np.full(n, np.nan)
    if n < lookback:
        return ker
    dc = np.abs(np.diff(c))
    path = np.convolve(dc, np.ones(lookback - 1), mode="valid")       # path[i-(L-1)] = sum dc[i-L+1 .. i-1]
    net = np.abs(c[lookback - 1:] - c[:n - lookback + 1])
    with np.errstate(divide="ignore", invalid="ignore"):
        k = np.where(path > 0, net / path, 0.0)
    ker[lookback - 1:] = np.clip(k, 0.0, 1.0)
    return ker


def _regime(ker):
    r = np.full(len(ker), -1, dtype=np.int8)
    r[ker >= engine.DEFAULT_KER_TREND] = 2
    r[(ker > engine.DEFAULT_KER_CHOP) & (ker < engine.DEFAULT_KER_TREND)] = 1
    r[ker <= engine.DEFAULT_KER_CHOP] = 0
    return r


def session_rows(parsed, is_crypto, rng, sess_base, equal_weight=False):
    """Per-bar rows for one ticker: real, and with each day's returns shuffled in time."""
    anchor = engine.DEFAULT_ANCHOR_M if not is_crypto else 0
    open_m = engine.RTH_OPEN_M if not is_crypto else 0
    close_m = engine.RTH_CLOSE_M if not is_crypto else 1440
    n = len(parsed)
    if n < 50:
        return None, None, None
    mins = np.array([b["mins"] for b in parsed], dtype=np.int32)
    o = np.array([b["o"] for b in parsed], dtype=np.float64)
    h = np.array([b["h"] for b in parsed], dtype=np.float64)
    l = np.array([b["l"] for b in parsed], dtype=np.float64)
    c = np.array([b["c"] for b in parsed], dtype=np.float64)
    v = np.array([b["v"] for b in parsed], dtype=np.float64)
    days = np.array([b["d"] for b in parsed])
    _, di, counts = np.unique(days, return_inverse=True, return_counts=True)
    day_start = np.concatenate([[0], np.cumsum(counts)[:-1]])
    day_start_of = day_start[di]
    ok = np.isfinite(c) & (c > 0) & np.isfinite(h) & np.isfinite(l)
    c = np.where(ok, c, np.nan)

    def accumulate(h_, l_, c_):
        tp = (h_ + l_ + c_) / 3.0
        w = np.where((mins >= anchor) & (mins < close_m) & (v > 0) & np.isfinite(tp), 1.0 if equal_weight else v, 0.0)
        tpw = np.where(w > 0, tp * w, 0.0)
        tp2w = np.where(w > 0, tp * tp * w, 0.0)
        W = _within_day_cumsum(w, day_start_of)
        with np.errstate(divide="ignore", invalid="ignore"):
            blue = _within_day_cumsum(tpw, day_start_of) / W
            var = _within_day_cumsum(tp2w, day_start_of) / W - blue * blue
        sigma = np.sqrt(np.maximum(var, 0.0))
        valid = (W > 0) & np.isfinite(c_) & (mins >= open_m) & (mins < close_m)
        return blue, sigma, valid

    ker = _ker_vec(np.where(np.isfinite(c), c, 0.0))
    reg = _regime(ker)
    adapt = np.where(np.isfinite(ker), 1.0 / np.maximum(0.35, 0.5 + ker), 1.0)

    def rows(h_, l_, c_):
        blue, sigma, valid = accumulate(h_, l_, c_)
        idx = np.nonzero(valid)[0]
        t = (mins[idx] - open_m).astype(np.int32)          # bar start, minutes since the open
        dev = (c_[idx] - blue[idx]) / blue[idx]
        sig = sigma[idx] / blue[idx]
        band = sigma[idx] * engine.DEFAULT_SIGMA_MULT * adapt[idx]
        stop = sigma[idx] * 0.35 * adapt[idx]
        absd = np.abs(c_[idx] - blue[idx])
        return {
            "sess": (sess_base + di[idx]).astype(np.int64),
            "t": t,
            "dev2": (dev * dev).astype(np.float64),
            "absdev": np.abs(dev),
            "sig2": (sig * sig).astype(np.float64),
            "ratio": np.where(sigma[idx] > 0, absd / np.where(sigma[idx] > 0, sigma[idx], 1.0), np.nan),
            "beyond": (absd > band) & (sigma[idx] > 0),
            "beyond_stop": (absd > stop) & (sigma[idx] > 0),
            "reg": reg[idx],
        }

    real = rows(h, l, c)
    windows = []
    for d0, cnt in zip(day_start, counts):
        sl = np.arange(d0, d0 + cnt)
        win = sl[(mins[sl] >= anchor) & (mins[sl] < close_m) & np.isfinite(c[sl])]
        if len(win) >= 3:
            windows.append(win)
    real["vr"] = vr_sums(c, windows)
    rth = [w[mins[w] >= open_m] for w in windows]          # the same without the premarket (equities)
    real["vr_rth"] = vr_sums(c, rth)

    # shuffled twin: within each day's anchored window, permute the bars' log returns (each return carries
    # its bar's high/low ratios), chain closes from the first one, keep volume by time slot
    hs, ls, cs = h.copy(), l.copy(), c.copy()
    for d0, cnt in zip(day_start, counts):
        sl = np.arange(d0, d0 + cnt)
        win = sl[(mins[sl] >= anchor) & (mins[sl] < close_m) & np.isfinite(c[sl])]
        if len(win) < 3:
            continue
        r = np.diff(np.log(c[win]))
        hr = (h[win] / c[win])[1:]
        lr = (l[win] / c[win])[1:]
        p = rng.permutation(len(r))
        newc = c[win[0]] * np.exp(np.cumsum(r[p]))
        cs[win[1:]] = newc
        hs[win[1:]] = newc * hr[p]
        ls[win[1:]] = newc * lr[p]
    shuf = rows(hs, ls, cs)
    shuf["vr"] = vr_sums(cs, windows)
    shuf["vr_rth"] = vr_sums(cs, rth)

    # cross-session twin: for every slot of the anchored window, permute that slot's log return (with its
    # bar's high/low ratios) across the ticker's days, then chain each day's closes from its own first close
    hs2, ls2, cs2 = h.copy(), l.copy(), c.copy()
    slot_of = {}            # mins -> list of (bar index, prev bar index) over all days
    day_first = {}
    for d0, cnt in zip(day_start, counts):
        sl = np.arange(d0, d0 + cnt)
        win = sl[(mins[sl] >= anchor) & (mins[sl] < close_m) & np.isfinite(c[sl])]
        if len(win) < 3:
            continue
        day_first[d0] = win
        for prev, cur in zip(win[:-1], win[1:]):
            slot_of.setdefault(int(mins[cur]), []).append((cur, prev))
    r_new = np.full(n, np.nan)
    hr_new = np.ones(n)
    lr_new = np.ones(n)
    for m_, pairs in slot_of.items():
        cur = np.array([p[0] for p in pairs])
        prev = np.array([p[1] for p in pairs])
        r = np.log(c[cur] / c[prev])
        p = rng.permutation(len(cur))
        r_new[cur] = r[p]
        hr_new[cur] = (h[cur] / c[cur])[p]
        lr_new[cur] = (l[cur] / c[cur])[p]
    for d0, win in day_first.items():
        newc = c[win[0]] * np.exp(np.cumsum(r_new[win[1:]]))
        cs2[win[1:]] = newc
        hs2[win[1:]] = newc * hr_new[win[1:]]
        ls2[win[1:]] = newc * lr_new[win[1:]]
    shuf2 = rows(hs2, ls2, cs2)
    shuf2["vr"] = vr_sums(cs2, windows)
    shuf2["vr_rth"] = vr_sums(cs2, rth)
    return real, shuf, shuf2


def concat(parts):
    out = {k: np.concatenate([p[k] for p in parts]) for k in parts[0] if k not in ("vr", "vr_rth")}
    out["vr"] = vr_merge([p["vr"] for p in parts])
    out["vr_rth"] = vr_merge([p["vr_rth"] for p in parts])
    return out


def per_bin(rows, t_max, mask=None):
    """Pooled per-slot statistics (slot = minutes since open / 5)."""
    m = np.ones(len(rows["t"]), bool) if mask is None else mask
    b = rows["t"][m] // BAR_MIN
    nb = t_max // BAR_MIN + 1
    n = np.bincount(b, minlength=nb).astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rms_dev = np.sqrt(np.bincount(b, rows["dev2"][m], nb) / n)
        rms_sig = np.sqrt(np.bincount(b, rows["sig2"][m], nb) / n)
        beyond = np.bincount(b, rows["beyond"][m].astype(float), nb) / n
        beyond_stop = np.bincount(b, rows["beyond_stop"][m].astype(float), nb) / n
    ratio = np.full(nb, np.nan)
    rr = rows["ratio"][m]
    for k in np.unique(b):
        x = rr[b == k]
        x = x[np.isfinite(x)]
        if len(x):
            ratio[k] = float(np.median(x))
    return {"n": n, "rms_dev": rms_dev, "rms_sig": rms_sig, "ratio_med": ratio, "beyond": beyond,
            "beyond_stop": beyond_stop}


def fit_H(t_minutes, y, t_from, t_to):
    m = (t_minutes >= t_from) & (t_minutes <= t_to) & np.isfinite(y) & (y > 0)
    if m.sum() < 4:
        return float("nan")
    x = np.log(t_minutes[m])
    return float(np.polyfit(x, np.log(y[m]), 1)[0])


def boot_H(rows, t_max, t_from, t_to, rng, key, reps=BOOT, mask=None):
    """Bootstrap the pooled log-log slope over sessions."""
    m = np.ones(len(rows["t"]), bool) if mask is None else mask
    sess, inv = np.unique(rows["sess"][m], return_inverse=True)
    b = rows["t"][m] // BAR_MIN
    nb = t_max // BAR_MIN + 1
    flat = inv * nb + b
    S = np.bincount(flat, rows[key][m], len(sess) * nb).reshape(len(sess), nb)
    N = np.bincount(flat, minlength=len(sess) * nb).reshape(len(sess), nb).astype(float)
    tm = np.arange(nb) * BAR_MIN
    hs = []
    for _ in range(reps):
        pick = rng.integers(0, len(sess), len(sess))
        s, n = S[pick].sum(0), N[pick].sum(0)
        with np.errstate(divide="ignore", invalid="ignore"):
            y = np.sqrt(s / n)
        hs.append(fit_H(tm, y, t_from, t_to))
    hs = np.array(hs)
    return float(np.nanpercentile(hs, 5)), float(np.nanpercentile(hs, 95))


def summarize(rows, t_max, t_from, t_to, rng, label, mask=None):
    pb = per_bin(rows, t_max, mask)
    tm = np.arange(len(pb["n"])) * BAR_MIN
    out = {
        "label": label,
        "bars": int(pb["n"].sum()),
        "sessions": int(len(np.unique(rows["sess"][mask] if mask is not None else rows["sess"]))),
        "H_dev": fit_H(tm, pb["rms_dev"], t_from, t_to),
        "H_sig": fit_H(tm, pb["rms_sig"], t_from, t_to),
        "H_ratio": fit_H(tm, pb["ratio_med"], t_from, t_to),
    }
    out["H_dev_ci90"] = boot_H(rows, t_max, t_from, t_to, rng, "dev2", mask=mask)
    out["H_sig_ci90"] = boot_H(rows, t_max, t_from, t_to, rng, "sig2", mask=mask)
    # hourly table
    hours = []
    edges = list(range(0, t_max + 1, 60))
    for a, bnd in zip(edges[:-1], edges[1:]):
        sl = (tm >= max(a, BAR_MIN)) & (tm < bnd) & (pb["n"] > 0)
        if not sl.any():
            continue
        w = pb["n"][sl]
        hours.append({
            "from_min": int(a), "to_min": int(bnd),
            "bars": int(w.sum()),
            "rms_dev_pct": float(np.sqrt(np.average(pb["rms_dev"][sl] ** 2, weights=w)) * 100),
            "rms_sig_pct": float(np.sqrt(np.average(pb["rms_sig"][sl] ** 2, weights=w)) * 100),
            "ratio_med": float(np.nanmedian(pb["ratio_med"][sl])),
            "beyond_band_pct": float(np.average(pb["beyond"][sl], weights=w) * 100),
            "beyond_stop_pct": float(np.average(pb["beyond_stop"][sl], weights=w) * 100),
        })
    out["hours"] = hours
    return out


def fmt_summary(s, nulls=()):
    def ci(x):
        return f"[{x[0]:.2f}, {x[1]:.2f}]"

    def hline(x):
        return (f"H(|close-blue|) = {x['H_dev']:.3f} {ci(x['H_dev_ci90'])}   H(sigma) = {x['H_sig']:.3f} "
                f"{ci(x['H_sig_ci90'])}   H(|dev|/sigma) = {x['H_ratio']:.3f}")
    print(f"\n{s['label']}: {s['sessions']} sessions, {s['bars']} bars")
    print("  " + hline(s))
    for nm, x in nulls:
        print(f"  {nm:>14}: " + hline(x))
    head = "  since open   bars    rms|dev|%  rms sig%  |dev|/sig  beyond band%  beyond stop%"
    for nm, _ in nulls:
        head += f"   | {nm}: rms|dev|% rms sig% |dev|/sig band%"
    print(head)
    by = [{h["from_min"]: h for h in x["hours"]} for _, x in nulls]
    for h in s["hours"]:
        line = (f"  {h['from_min']:4d}-{h['to_min']:4d}  {h['bars']:7d}   {h['rms_dev_pct']:7.3f}   {h['rms_sig_pct']:7.3f}   "
                f"{h['ratio_med']:6.2f}      {h['beyond_band_pct']:5.1f}        {h['beyond_stop_pct']:5.1f}")
        for d in by:
            x = d.get(h["from_min"])
            if x:
                line += f"   | {x['rms_dev_pct']:7.3f} {x['rms_sig_pct']:7.3f} {x['ratio_med']:5.2f} {x['beyond_band_pct']:5.1f}"
        print(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True, help="a --save-bars pickle (replay_sessions or replay_crypto)")
    ap.add_argument("--crypto", action="store_true", help="whole-ET-day sessions (crypto_bv / crypto_bars pickles)")
    ap.add_argument("--max-tickers", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--equal-weight", action="store_true", help="blue/sigma with every bar weighted 1 instead of its volume")
    ap.add_argument("--out")
    args = ap.parse_args()

    cache = pickle.loads(Path(args.bars).read_bytes())
    tickers = [t for t in cache["tickers"] if t in cache["bars"]]
    if args.max_tickers:
        tickers = tickers[:args.max_tickers]
    t_max = (engine.RTH_CLOSE_M - engine.RTH_OPEN_M) if not args.crypto else 1440
    rng = np.random.default_rng(args.seed)
    t0 = time.time()
    reals, shufs, shufs2 = [], [], []
    for k, t in enumerate(tickers):
        df = cache["bars"][t]
        if df is None or len(df) == 0:
            continue
        parsed = engine._prep_bars(df)
        r, s, s2 = session_rows(parsed, args.crypto, rng, k * 100000, equal_weight=args.equal_weight)
        if r is None:
            continue
        reals.append(r)
        shufs.append(s)
        shufs2.append(s2)
    real, shuf, shuf2 = concat(reals), concat(shufs), concat(shufs2)
    print(f"{args.bars}: {len(reals)} tickers, {len(real['t'])} session bars parsed in {time.time() - t0:.1f}s "
          f"(fetched {cache.get('fetched_at')}, source {cache.get('source')})")

    res = {"bars": args.bars, "fetched_at": cache.get("fetched_at"), "source": cache.get("source"),
           "crypto": bool(args.crypto), "tickers": len(reals), "fit_window_min": [FIT_FROM_MIN, t_max],
           "sigma_mult": engine.DEFAULT_SIGMA_MULT, "stop_buffer_mult": 0.35,
           "ker": {"lookback": engine.DEFAULT_KER_LOOKBACK, "trend": engine.DEFAULT_KER_TREND, "chop": engine.DEFAULT_KER_CHOP},
           "boot_reps": BOOT, "seed": args.seed, "equal_weight": bool(args.equal_weight)}
    for vk, title in (("vr", "anchored window (04:00 on, equities)"), ("vr_rth", "session only (09:30 on)")):
        res["variance_ratio" + ("" if vk == "vr" else "_session")] = {
            "k_bars": list(VR_K), "real": vr_ratio(real[vk]), "null_within_session": vr_ratio(shuf[vk]),
            "null_cross_session": vr_ratio(shuf2[vk])}
        vr = res["variance_ratio" + ("" if vk == "vr" else "_session")]
        print(f"\n  variance ratio of k-bar to 1-bar close returns, {title} (1 = random walk; < 1 = reversion)")
        print("        k:  " + "".join(f"{k:7d}" for k in VR_K))
        for nm, key in (("real", "real"), ("within-day", "null_within_session"), ("cross-day", "null_cross_session")):
            print(f"  {nm:>10}:  " + "".join(f"{vr[key][k]:7.3f}" for k in VR_K))
        if args.crypto:
            break
    res["all"] = summarize(real, t_max, FIT_FROM_MIN, t_max, rng, "all bars")
    res["null_within_session"] = summarize(shuf, t_max, FIT_FROM_MIN, t_max, rng, "returns shuffled within each session")
    res["null_cross_session"] = summarize(shuf2, t_max, FIT_FROM_MIN, t_max, rng, "each slot's returns shuffled across sessions")
    fmt_summary(res["all"], [("within-day", res["null_within_session"]), ("cross-day", res["null_cross_session"])])
    res["by_regime"] = {}
    for i, name in enumerate(REGIMES):
        m = real["reg"] == i
        if m.sum() < 1000:
            continue
        res["by_regime"][name] = summarize(real, t_max, FIT_FROM_MIN, t_max, rng, f"KER regime {name} at the bar", mask=m)
        fmt_summary(res["by_regime"][name])
    # regime mix by hour (does the day drift from chop to trend?)
    tm = real["t"]
    mix = []
    for a in range(0, t_max, 60):
        sl = (tm >= a) & (tm < a + 60)
        n = sl.sum()
        if n:
            mix.append({"from_min": a, "chop": float((real["reg"][sl] == 0).mean()), "mixed": float((real["reg"][sl] == 1).mean()),
                        "trend": float((real["reg"][sl] == 2).mean())})
    res["regime_mix_by_hour"] = mix
    print("\n  regime share by hour (chop / mixed / trend):")
    print("  " + "  ".join(f"{m['from_min']:4d}: {m['chop'] * 100:4.0f}/{m['mixed'] * 100:4.0f}/{m['trend'] * 100:4.0f}" for m in mix))

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(res, indent=1))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
