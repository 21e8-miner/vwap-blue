"""
VWAP Blue engine — best-of Blueline + VWAP One + multi-day reverse.

Blueline:
  · blue  = session (day) VWAP ± σ
  · orange = prior-day-anchored VWAP (accumulates prior RTH + today)
  · gap-direction mean-reversion: beyond blue K bars → confirm,
    target orange, stop = blue ± buffer
  · multi-day VWAP reverse (1m desk pattern): sustained extension beyond
    orange → reclaim orange → long/short reverse (helps when no gap)
  · grades A / LA / B / C / ✕  and state text

VWAP One:
  · free multi-provider bars (injected by data layer)
  · SETUP/TRIGGER-style signal badge + edge score
  · RTH / premarket / crypto session awareness
  · never invents prices
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")

RTH_OPEN_M = 9 * 60 + 30   # 09:30
RTH_CLOSE_M = 16 * 60      # 16:00
DEFAULT_ANCHOR_M = 4 * 60  # 04:00 premarket start for orange accumulation
DEFAULT_K = 2              # consecutive bars beyond blue to confirm
DEFAULT_ATR_MULT = 0.35
DEFAULT_GAP_MIN = 0.35     # % gap to arm setup
DEFAULT_RMIN = 1.0
DEFAULT_RVOL_MIN = 1.2
LATE_CUT_M = 14 * 60 + 30  # 14:30 late trigger
# Multi-day VWAP reverse (orange = multi-day anchor)
DEFAULT_MD_MIN_EXT = 8     # bars extended beyond orange before reclaim counts
DEFAULT_MD_MIN_DIST = 0.25 # max % distance from orange during extension
# Modern VWAP-style adaptive bands (volume-weighted σ already in blue path)
DEFAULT_SIGMA_MULT = 0.15  # require this × adaptive-σ beyond blue to count as "beyond"
DEFAULT_KER_LOOKBACK = 20  # bars for Kaufman Efficiency Ratio
DEFAULT_KER_TREND = 0.55   # ER ≥ this → trend regime (suppress gap-fades)
DEFAULT_KER_CHOP = 0.30    # ER ≤ this → chop regime (widen bands, favor fades)
DEFAULT_RVOL_MIN_N = 2     # thin RVOL sample demotes A → B


def _is_crypto(ticker: str) -> bool:
    t = ticker.upper()
    if t.endswith(("-USD", "-USDT", "-USDC")):
        return True
    bare = t.replace("-", "")
    return bare.endswith(("USDT", "USDC")) and len(bare) >= 6


def _session_label(is_crypto: bool) -> Dict[str, Any]:
    now = datetime.now(ET)
    if is_crypto:
        return {"session_state": "crypto", "rth_open": True, "session_label": "24/7 crypto"}
    wd = now.weekday()
    mins = now.hour * 60 + now.minute
    is_wd = wd < 5
    rth = is_wd and RTH_OPEN_M <= mins < RTH_CLOSE_M
    if not is_wd:
        label = "weekend"
    elif mins < RTH_OPEN_M:
        label = "premarket"
    elif mins >= RTH_CLOSE_M:
        label = "afterhours"
    else:
        label = "rth"
    return {
        "session_state": "open" if rth else "closed",
        "rth_open": rth,
        "session_label": label,
        # signals allowed in premarket + RTH (Blueline rule)
        "signal_window": is_wd and mins < RTH_CLOSE_M,
    }


def _to_et(ts) -> pd.Timestamp:
    """Normalize any bar timestamp to America/New_York (for labels + session mins)."""
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        return t.tz_convert(ET)
    # Yahoo / free feeds often emit UTC wall-clock as naive — prefer UTC→ET
    try:
        return t.tz_localize("UTC").tz_convert(ET)
    except Exception:
        try:
            return t.tz_localize(ET)
        except Exception:
            return t


def _bar_mins(ts) -> int:
    t = _to_et(ts)
    return t.hour * 60 + t.minute


def _bar_day(ts) -> str:
    return _to_et(ts).strftime("%Y-%m-%d")


def _fmt_et(ts) -> str:
    """Chart / tooltip label always in US/Eastern."""
    try:
        return _to_et(ts).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)


def _tp(o, h, l, c) -> float:
    return (float(h) + float(l) + float(c)) / 3.0


def _prep_bars(df: pd.DataFrame) -> List[Dict[str, Any]]:
    bars: List[Dict[str, Any]] = []
    if df is None or df.empty:
        return bars
    for ts, row in df.iterrows():
        try:
            o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
            v = float(row["Volume"]) if row["Volume"] == row["Volume"] else 0.0
        except Exception:
            continue
        if not all(map(lambda x: x == x, [o, h, l, c])):
            continue
        t = pd.Timestamp(ts)
        t_et = _to_et(ts)
        try:
            t_ms = int(t.timestamp() * 1000)
        except Exception:
            t_ms = len(bars)
        bars.append({
            "ts": t_ms,
            "d": t_et.strftime("%Y-%m-%d"),
            "mins": t_et.hour * 60 + t_et.minute,
            "o": o, "h": h, "l": l, "c": c, "v": max(0.0, v),
            "hlReal": h > l,
            "time": t_et.strftime("%m-%d %H:%M"),
        })
    return bars


def _sessions(bars: List[Dict[str, Any]]) -> List[str]:
    seen, out = set(), []
    for b in bars:
        if b["d"] not in seen:
            seen.add(b["d"])
            out.append(b["d"])
    return out


def _first_idx(bars: List[Dict[str, Any]], day: str) -> int:
    for i, b in enumerate(bars):
        if b["d"] == day:
            return i
    return -1


def _last_idx(bars: List[Dict[str, Any]], day: str) -> int:
    idx = -1
    for i, b in enumerate(bars):
        if b["d"] == day:
            idx = i
    return idx


def _prior_rth_close(bars: List[Dict[str, Any]], i0: int) -> float:
    if i0 <= 0:
        return bars[0]["c"]
    prior_day = bars[i0 - 1]["d"]
    close = None
    for i in range(i0 - 1, -1, -1):
        b = bars[i]
        if b["d"] != prior_day:
            break
        if RTH_OPEN_M <= b["mins"] < RTH_CLOSE_M:
            close = b["c"]
    if close is not None:
        return close
    for i in range(i0 - 1, -1, -1):
        b = bars[i]
        if b["d"] != prior_day:
            break
        if b["mins"] < RTH_CLOSE_M:
            close = b["c"]
    return close if close is not None else bars[i0 - 1]["c"]


def _atr(bars: List[Dict[str, Any]], end: int, period: int = 14) -> Optional[float]:
    trs = []
    prev_c = None
    start = max(0, end - 40)
    for i in range(start, end + 1):
        b = bars[i]
        if not b["hlReal"]:
            continue
        if prev_c is None:
            trs.append(b["h"] - b["l"])
        else:
            trs.append(max(b["h"] - b["l"], abs(b["h"] - prev_c), abs(b["l"] - prev_c)))
        prev_c = b["c"]
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def _ker(bars: List[Dict[str, Any]], end: int, lookback: int = DEFAULT_KER_LOOKBACK) -> Optional[float]:
    """
    Kaufman Efficiency Ratio on closes: |net move| / sum(|bar moves|).
    Near 1 = efficient trend; near 0 = chop. None if insufficient data.
    """
    if end < 1 or lookback < 3:
        return None
    start = max(0, end - lookback + 1)
    if end - start + 1 < 5:
        return None
    closes = [bars[i]["c"] for i in range(start, end + 1) if bars[i]["c"] and bars[i]["c"] > 0]
    if len(closes) < 5:
        return None
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    if path <= 0:
        return 0.0
    return max(0.0, min(1.0, net / path))


def _adaptive_sigma_mult(ker: Optional[float], base: float = 1.0) -> float:
    """
    Modern VWAP L2 idea: choppy tape widens bands, trending tape tightens.
    mult > 1 → wider (need more extension / looser stop buffer).
    """
    if ker is None:
        return base
    # ER=0 → ~2.0×; ER=1 → ~0.67×
    return base * (1.0 / max(0.35, 0.5 + ker))


def _regime_from_ker(
    ker: Optional[float],
    trend_th: float = DEFAULT_KER_TREND,
    chop_th: float = DEFAULT_KER_CHOP,
) -> str:
    if ker is None:
        return "unknown"
    if ker >= trend_th:
        return "trend"
    if ker <= chop_th:
        return "chop"
    return "mixed"


def _resolve_day(
    bars: List[Dict[str, Any]],
    i0: int,
    iN: int,
    p0: int,
    opts: Dict[str, Any],
) -> Dict[str, Any]:
    anchor = opts["anchor_mins"]
    acc = {"bp": 0.0, "bv": 0.0, "bp2": 0.0, "op": 0.0, "ov": 0.0, "vol": 0.0, "trapV": 0.0}
    # seed orange from prior day (from anchor through RTH close)
    for i in range(p0, i0):
        b = bars[i]
        if b["mins"] >= anchor and b["mins"] < RTH_CLOSE_M and b["v"] > 0:
            tp = _tp(b["o"], b["h"], b["l"], b["c"])
            acc["op"] += tp * b["v"]
            acc["ov"] += b["v"]

    prior_close = _prior_rth_close(bars, i0)
    open_idx = None
    for i in range(i0, iN + 1):
        if bars[i]["mins"] >= RTH_OPEN_M:
            open_idx = i
            break
    if open_idx is not None:
        ob = bars[open_idx]
        gap_pct = ((ob["o"] if ob["o"] is not None else ob["c"]) - prior_close) / prior_close * 100.0
        gap_provisional = False
    else:
        gap_pct = (bars[iN]["c"] - prior_close) / prior_close * 100.0
        gap_provisional = True

    direction = 1 if gap_pct >= 0 else -1
    dev_ok = abs(gap_pct) >= opts["gap_min"]

    st: Dict[str, Any] = {
        "phase": "SIDE",
        "run": 0,
        "fake": 0,
        "firstBreak": None,
        "trig": None,
        "tagged": None,
        "stopped": None,
        "entry": None,
        "stopPx": None,
        "target": None,
        "riskPct": None,
        "runwayPct": None,
        "R": None,
        "noRunway": False,
        "blue": None,
        "sigma": None,
        "orange": None,
        "blueTrail": [],
        "orangeTrail": [],
        "sigTrail": [],
        "atr": None,
        "late": False,
        "_i0": i0,
        # multi-day reverse (orange reclaim after extension)
        "md_ext_side": 0,       # -1 below orange, +1 above
        "md_ext_run": 0,
        "md_ext_extreme": None,
        "md_max_dist": 0.0,
        "md_trig": None,
        "md_side": None,        # "long" | "short"
        "md_entry": None,
        "md_stop": None,
        "md_target": None,
        "md_R": None,
        "setup_mode": None,     # "gap" | "mdrev" | "both"
    }

    for i in range(i0, iN + 1):
        _step_bar(bars, i, acc, st, direction, dev_ok, opts)

    # if gap path never armed but multi-day reverse did, promote MD plan
    if st["trig"] is None and st["md_trig"] is not None:
        st["trig"] = st["md_trig"]
        st["entry"] = st["md_entry"]
        st["stopPx"] = st["md_stop"]
        st["target"] = st["md_target"]
        st["R"] = st["md_R"]
        st["setup_mode"] = "mdrev"
        if st["entry"] and st["stopPx"]:
            st["riskPct"] = abs(st["entry"] - st["stopPx"]) / st["entry"] * 100.0
        if st["entry"] and st["target"] is not None:
            if st["md_side"] == "long":
                st["runwayPct"] = (st["target"] - st["entry"]) / st["entry"] * 100.0
            else:
                st["runwayPct"] = (st["entry"] - st["target"]) / st["entry"] * 100.0
            st["noRunway"] = st["runwayPct"] is not None and st["runwayPct"] <= 0
        # resolve from reclaim bar forward with MD direction
        md_dir = -1 if st["md_side"] == "long" else 1  # dir>0 short-style resolve uses orange as target differently
        # For MD long: target is blue (above); stop is below — resolve via blue/target hit
        _resolve_md_from(bars, st["md_trig"], iN, st)
    elif st["trig"] is not None and st["md_trig"] is not None:
        # same-side agreement?
        gap_side = "short" if direction > 0 else "long"
        if st["md_side"] == gap_side:
            st["setup_mode"] = "both"
        else:
            st["setup_mode"] = "gap"
    elif st["trig"] is not None:
        st["setup_mode"] = "gap"

    return {
        "acc": acc,
        "st": st,
        "gap_pct": gap_pct,
        "gap_provisional": gap_provisional,
        "dir": direction,
        "dev_ok": dev_ok,
        "prior_close": prior_close,
        "open_idx": open_idx,
        "md_side": st.get("md_side"),
        "setup_mode": st.get("setup_mode"),
    }


def _step_bar(
    bars: List[Dict[str, Any]],
    i: int,
    acc: Dict[str, float],
    st: Dict[str, Any],
    direction: int,
    dev_ok: bool,
    opts: Dict[str, Any],
) -> None:
    b = bars[i]
    anchor = opts["anchor_mins"]
    # accumulate blue (today window) and orange (prior+today)
    if b["mins"] >= anchor and b["mins"] < RTH_CLOSE_M and b["v"] > 0:
        tp = _tp(b["o"], b["h"], b["l"], b["c"])
        acc["bp"] += tp * b["v"]
        acc["bv"] += b["v"]
        acc["bp2"] += tp * tp * b["v"]
        acc["op"] += tp * b["v"]
        acc["ov"] += b["v"]
    if b["mins"] < RTH_CLOSE_M:
        acc["vol"] += b["v"]

    if acc["bv"] > 0:
        st["blue"] = acc["bp"] / acc["bv"]
        var = max(acc["bp2"] / acc["bv"] - st["blue"] * st["blue"], 0.0)
        st["sigma"] = var ** 0.5
    if acc["ov"] > 0:
        st["orange"] = acc["op"] / acc["ov"]

    st["blueTrail"].append(st["blue"])
    st["orangeTrail"].append(st["orange"])
    st["sigTrail"].append(st["sigma"])
    st["atr"] = _atr(bars, i)

    # ── multi-day reverse: always track when orange exists (no gap required) ──
    if st["orange"] is not None and b["mins"] < RTH_CLOSE_M and st["md_trig"] is None:
        _track_md_reverse(bars, i, st, opts)

    # Blueline gap path: signals only pre + RTH when gap armed
    eligible = dev_ok and st["blue"] is not None and b["mins"] < RTH_CLOSE_M
    if not eligible:
        return

    if (direction > 0 and b["c"] > st["blue"]) or (direction < 0 and b["c"] < st["blue"]):
        acc["trapV"] += b["v"]

    if st["trig"] is None:
        # Adaptive beyond-threshold using volume-weighted σ (already on st["sigma"])
        # and Kaufman ER: chop → wider band (harder confirm), trend → tighter.
        ker_now = _ker(bars, i, int(opts.get("ker_lookback", DEFAULT_KER_LOOKBACK)))
        adapt = _adaptive_sigma_mult(ker_now, base=1.0)
        band = 0.0
        if st["sigma"] is not None and st["sigma"] > 0:
            band = st["sigma"] * float(opts.get("sigma_mult", DEFAULT_SIGMA_MULT)) * adapt
        if direction > 0:
            # gap-up fade short: confirm once price is meaningfully below blue
            beyond = b["c"] < (st["blue"] - band)
        else:
            beyond = b["c"] > (st["blue"] + band)
        if beyond:
            if st["phase"] != "BEYOND":
                st["phase"] = "BEYOND"
                st["run"] = 1
                if st["firstBreak"] is None:
                    st["firstBreak"] = i
            else:
                st["run"] += 1
            if st["run"] >= opts["K"]:
                st["trig"] = i
                st["entry"] = b["c"]
                st["_ker_at_trig"] = ker_now
                st["_adapt_at_trig"] = adapt
                buf = 0.0
                if opts["atr_mult"] > 0 and st["atr"]:
                    buf = st["atr"] * opts["atr_mult"] * adapt
                elif st["sigma"] is not None:
                    buf = st["sigma"] * 0.35 * adapt
                st["stopPx"] = st["blue"] + direction * buf
                st["target"] = st["orange"]
                st["riskPct"] = abs(st["entry"] - st["stopPx"]) / st["entry"] * 100.0 if st["entry"] else None
                st["late"] = b["mins"] >= opts["late_cut"]
                if st["target"] is not None and st["entry"]:
                    if direction > 0:
                        st["runwayPct"] = (st["entry"] - st["target"]) / st["entry"] * 100.0
                    else:
                        st["runwayPct"] = (st["target"] - st["entry"]) / st["entry"] * 100.0
                    st["noRunway"] = st["runwayPct"] <= 0
                    if st["riskPct"] and st["riskPct"] > 0 and not st["noRunway"]:
                        st["R"] = st["runwayPct"] / st["riskPct"]
                _resolve_open(bars, i, st, direction)
        else:
            if st["phase"] == "BEYOND":
                st["fake"] += 1
            st["phase"] = "SIDE"
            st["run"] = 0
    elif st["tagged"] is None and st["stopped"] is None:
        _resolve_open(bars, i, st, direction)


def _track_md_reverse(
    bars: List[Dict[str, Any]],
    i: int,
    st: Dict[str, Any],
    opts: Dict[str, Any],
) -> None:
    """1m multi-day VWAP reverse: extend beyond orange, then reclaim it."""
    b = bars[i]
    orange = st["orange"]
    if orange is None or orange <= 0:
        return
    min_ext = int(opts.get("md_min_ext", DEFAULT_MD_MIN_EXT))
    min_dist = float(opts.get("md_min_dist", DEFAULT_MD_MIN_DIST))

    if b["c"] < orange:
        side = -1  # below → long reclaim candidate
    elif b["c"] > orange:
        side = 1   # above → short reclaim candidate
    else:
        side = 0

    ext = st.get("md_ext_side") or 0
    run = st.get("md_ext_run") or 0

    # reclaim: was extended one side, close now on the other side of orange
    if ext != 0 and side == -ext and run >= min_ext and (st.get("md_max_dist") or 0) >= min_dist:
        md_side = "long" if ext < 0 else "short"
        entry = b["c"]
        buf = 0.0
        if opts.get("atr_mult", 0) > 0 and st.get("atr"):
            buf = st["atr"] * float(opts["atr_mult"])
        elif st.get("sigma") is not None:
            buf = st["sigma"] * 0.25
        extreme = st.get("md_ext_extreme")
        if md_side == "long":
            stop = (extreme if extreme is not None else entry) - buf
            # target day VWAP if above entry, else stretch from orange
            if st.get("blue") is not None and st["blue"] > entry:
                target = st["blue"]
            else:
                target = entry + max(abs(entry - stop) * 1.5, entry * 0.004)
        else:
            stop = (extreme if extreme is not None else entry) + buf
            if st.get("blue") is not None and st["blue"] < entry:
                target = st["blue"]
            else:
                target = entry - max(abs(stop - entry) * 1.5, entry * 0.004)

        risk = abs(entry - stop)
        runway = abs(target - entry) if target is not None else 0.0
        R = (runway / risk) if risk > 0 else None

        st["md_trig"] = i
        st["md_side"] = md_side
        st["md_entry"] = entry
        st["md_stop"] = stop
        st["md_target"] = target
        st["md_R"] = R
        st["late"] = b["mins"] >= opts.get("late_cut", LATE_CUT_M)
        return

    # still / newly extended
    if side != 0 and (ext == 0 or ext == side):
        if ext != side:
            st["md_ext_side"] = side
            st["md_ext_run"] = 1
            st["md_ext_extreme"] = b["l"] if side < 0 else b["h"]
            st["md_max_dist"] = abs(b["c"] - orange) / orange * 100.0
        else:
            st["md_ext_run"] = run + 1
            if side < 0:
                prev = st.get("md_ext_extreme")
                st["md_ext_extreme"] = b["l"] if prev is None else min(prev, b["l"])
            else:
                prev = st.get("md_ext_extreme")
                st["md_ext_extreme"] = b["h"] if prev is None else max(prev, b["h"])
            dist = abs(b["c"] - orange) / orange * 100.0
            st["md_max_dist"] = max(st.get("md_max_dist") or 0.0, dist)
        return

    # lost extension without reclaim (oscillating on orange) — soft reset
    if side == 0 or (ext != 0 and side != ext and run < min_ext):
        st["md_ext_side"] = side
        st["md_ext_run"] = 1 if side != 0 else 0
        if side != 0:
            st["md_ext_extreme"] = b["l"] if side < 0 else b["h"]
            st["md_max_dist"] = abs(b["c"] - orange) / orange * 100.0
        else:
            st["md_ext_extreme"] = None
            st["md_max_dist"] = 0.0


def _resolve_open(bars: List[Dict[str, Any]], i: int, st: Dict[str, Any], direction: int) -> None:
    b = bars[i]
    orange = st["orange"]
    if orange is not None and b["hlReal"]:
        hit = (b["l"] <= orange) if direction > 0 else (b["h"] >= orange)
        if hit:
            st["tagged"] = i
            return
    if st["stopPx"] is not None and st["trig"] is not None and i > st["trig"]:
        out = (b["c"] > st["stopPx"]) if direction > 0 else (b["c"] < st["stopPx"])
        if out:
            st["stopped"] = i


def _resolve_md_from(bars: List[Dict[str, Any]], i_start: int, iN: int, st: Dict[str, Any]) -> None:
    """Resolve multi-day reverse open trade from reclaim bar forward."""
    md_side = st.get("md_side")
    if not md_side:
        return
    for i in range(i_start, iN + 1):
        b = bars[i]
        if st["tagged"] is not None or st["stopped"] is not None:
            return
        tgt = st.get("target") or st.get("md_target")
        stop = st.get("stopPx") or st.get("md_stop")
        if tgt is not None and b["hlReal"]:
            hit = (b["h"] >= tgt) if md_side == "long" else (b["l"] <= tgt)
            if hit:
                st["tagged"] = i
                return
        if stop is not None and i > i_start:
            out = (b["c"] < stop) if md_side == "long" else (b["c"] > stop)
            if out:
                st["stopped"] = i
                return


def _state_text(S: Dict[str, Any], opts: Dict[str, Any]) -> Dict[str, str]:
    st = S["st"]
    mode = st.get("setup_mode")

    # multi-day reverse path (no gap or gap unused)
    if mode == "mdrev" or (not S["dev_ok"] and st.get("md_trig") is not None):
        if st["stopped"] is not None:
            return {"txt": "MD REV STOPPED ✕", "cls": "err"}
        if st["tagged"] is not None:
            arrow = "▲" if st.get("md_side") == "long" else "▼"
            return {"txt": f"MD REV TAGGED ✓ {arrow}", "cls": "tag"}
        if st["trig"] is not None or st.get("md_trig") is not None:
            arrow = "▲" if st.get("md_side") == "long" else "▼"
            late = " LATE" if st.get("late") else ""
            return {"txt": f"MD REV {arrow} · reclaim orange{late}", "cls": "conf"}
        return {"txt": "MD REV SETUP", "cls": "break"}

    # building multi-day extension (useful even without gap)
    if st.get("md_trig") is None and (st.get("md_ext_run") or 0) >= opts.get("md_min_ext", DEFAULT_MD_MIN_EXT) // 2:
        if (st.get("md_ext_side") or 0) < 0:
            return {"txt": f"MD EXT ▼ orange {st['md_ext_run']}b", "cls": "break"}
        if (st.get("md_ext_side") or 0) > 0:
            return {"txt": f"MD EXT ▲ orange {st['md_ext_run']}b", "cls": "break"}

    if not S["dev_ok"]:
        return {
            "txt": "NO GAP (pre)" if S["gap_provisional"] else "NO GAP",
            "cls": "none",
        }
    if st["trig"] is None:
        if st["firstBreak"] is None:
            gap = "GAP ▲ · above blue" if S["dir"] > 0 else "GAP ▼ · below blue"
            if S["gap_provisional"]:
                gap += " (pre)"
            return {"txt": gap, "cls": "gap"}
        if st["phase"] == "BEYOND":
            return {"txt": f"FIRST BREAK {st['run']}/{opts['K']}", "cls": "break"}
        return {"txt": f"FAKEOUT ×{st['fake']}", "cls": "fake"}
    if st["noRunway"]:
        return {"txt": "CONFIRMED · NO RUNWAY", "cls": "none"}
    if st["tagged"] is not None:
        base = "TAGGED ✓ short" if S["dir"] > 0 else "TAGGED ✓ long"
        if mode == "both":
            base = "MD+GAP " + base
        return {"txt": base, "cls": "tag"}
    if st["stopped"] is not None:
        return {"txt": "STOPPED ✕", "cls": "err"}
    late = " LATE" if st["late"] else ""
    conf = "CONFIRMED ▼" if S["dir"] > 0 else "CONFIRMED ▲"
    if mode == "both":
        conf = "MD+GAP " + conf
    if st["fake"] > 0:
        conf = "FSB " + conf
    return {"txt": conf + late, "cls": "conf"}


def _grade(S: Dict[str, Any], opts: Dict[str, Any]) -> str:
    st = S["st"]
    mode = st.get("setup_mode")
    # multi-day reverse can grade without a gap
    if S.get("error"):
        return "–"
    if not S["dev_ok"] and mode != "mdrev" and st.get("md_trig") is None:
        return "–"
    if st["trig"] is None and st.get("md_trig") is None:
        # extension building → soft C watch
        if (st.get("md_ext_run") or 0) >= opts.get("md_min_ext", DEFAULT_MD_MIN_EXT):
            return "C"
        return "C" if S["dev_ok"] else "–"
    if st["stopped"] is not None:
        return "✕"
    if st["noRunway"] or st["R"] is None:
        g = "B"
    else:
        gR = st["R"] >= opts["Rmin"]
        rvol = S.get("rvol")
        gV = True if rvol is None else rvol >= opts["rvol_min"]
        # Thin RVOL baseline: never award raw A (need n ≥ rvol_min_n when rvol used)
        rvol_n = int(S.get("rvol_n") or 0)
        rvol_min_n = int(opts.get("rvol_min_n", DEFAULT_RVOL_MIN_N))
        thin = rvol is not None and rvol_n < rvol_min_n
        g = "A" if (gR and gV and not thin) else "B"
        # agreement of gap + multi-day reverse boosts quality (keep A/B, flag via mode)
        if mode == "both" and g == "B" and gR and not thin:
            g = "A"
        # Regime: pure gap-fade in trend is not A
        if S.get("regime") == "trend" and mode == "gap" and g == "A":
            g = "B"
        # Chop + both agreement: slight quality keep
        if S.get("regime") == "chop" and mode == "both" and g == "B" and gR and gV and not thin:
            g = "A"
    if st["late"]:
        g = "L" + g
    return g


def _signal_badge(grade: str, state_cls: str, st: Dict[str, Any]) -> str:
    """Map Blueline state → VWAP-style badge."""
    if st.get("tagged") is not None:
        return "TAGGED"
    if st.get("stopped") is not None:
        return "STOPPED"
    # no-runway / inverted geometry must not look like a live trigger
    if st.get("noRunway") or st.get("bad_geom"):
        return "WATCH"
    if st.get("trig") is not None:
        return "TRIGGER"
    if state_cls == "break":
        return "SETUP"
    if state_cls == "fake":
        return "FAKE"
    if state_cls == "gap":
        return "WATCH"
    return "FLAT"


def _geom_ok(side: str, entry, stop, target) -> bool:
    """Long needs stop < entry < target; short needs target < entry < stop."""
    try:
        e, s, t = float(entry), float(stop), float(target)
    except (TypeError, ValueError):
        return False
    if e <= 0:
        return False
    if side == "long":
        return s < e < t
    if side == "short":
        return t < e < s
    return False


def _day_trend_blocks_fade(
    bars: List[Dict[str, Any]],
    i0: int,
    iN: int,
    gap_pct: float,
    side: str,
    min_cont: float = 0.6,
) -> bool:
    """
    Block gap-fade when the session has already continued hard in the gap direction.
    Gap-down → long fade is wrong if price is still grinding lower from the open.
    Gap-up → short fade is wrong if price is still grinding higher from the open.
    """
    if abs(gap_pct) < 0.35:
        return False
    open_px = None
    for i in range(i0, iN + 1):
        if bars[i]["mins"] >= RTH_OPEN_M:
            open_px = bars[i]["o"] if bars[i]["o"] else bars[i]["c"]
            break
    if open_px is None or open_px <= 0:
        return False
    last = bars[iN]["c"]
    day_move = (last - open_px) / open_px * 100.0
    # fade long after gap-down blocked if still down hard
    if side == "long" and gap_pct < 0 and day_move <= -min_cont:
        return True
    # fade short after gap-up blocked if still up hard
    if side == "short" and gap_pct > 0 and day_move >= min_cont:
        return True
    # also block if continuation magnitude already exceeds the gap (trend day)
    if side == "long" and gap_pct < 0 and day_move < gap_pct:
        return True
    if side == "short" and gap_pct > 0 and day_move > gap_pct:
        return True
    return False


# Use every prior session in the fetch window (Yahoo 1m ~7d ≈ 5–6 RTH days).
RVOL_MAX_PRIORS = 7
RVOL_MIN_PRIORS = 2


def _rvol(
    bars: List[Dict[str, Any]], days: List[str], i0: int, iN: int, acc_vol: float
) -> Tuple[Optional[float], int]:
    """
    Relative cumulative volume vs prior days at same minute-of-day.
    Returns (rvol, n_baselines). n is always the count of usable prior sessions
    even when rvol is None (thin sample) so the UI can show sample size.
    """
    if len(days) < 3 or acc_vol <= 0:
        return None, 0
    last_mins = bars[iN]["mins"]
    priors = days[:-1][-RVOL_MAX_PRIORS:]
    bases = []
    for pd in priors:
        cum = 0.0
        for b in bars:
            if b["d"] != pd:
                continue
            if b["mins"] <= last_mins and b["mins"] < RTH_CLOSE_M:
                cum += b["v"]
        if cum > 0:
            bases.append(cum)
    n = len(bases)
    if n < RVOL_MIN_PRIORS:
        return None, n
    return acc_vol / (sum(bases) / n), n


def _edge(grade: str, S: Dict[str, Any], state_cls: str) -> int:
    """Composite 0–100 rank (higher = more interesting)."""
    score = 0
    st = S["st"]
    gmap = {"A": 40, "LA": 32, "B": 22, "LB": 16, "C": 8, "✕": 2, "–": 0}
    score += gmap.get(grade, 0)
    if state_cls == "conf":
        score += 20
    elif state_cls == "break":
        score += 14
    elif state_cls == "tag":
        score += 18
    elif state_cls == "fake":
        score += 6
    if st.get("R") is not None:
        score += min(20, int(st["R"] * 8))
    rvol = S.get("rvol")
    if rvol is not None:
        score += min(12, int(max(0, (rvol - 1.0) * 10)))
    gap = abs(S.get("gap_pct") or 0)
    score += min(10, int(gap * 2))
    # multi-day reverse: real reclaim after extension (SPCX-style)
    if st.get("setup_mode") == "mdrev":
        score += 12
        score += min(10, int((st.get("md_max_dist") or 0) * 2))
    elif st.get("setup_mode") == "both":
        score += 16  # gap path + multi-day reclaim agree
    elif (st.get("md_ext_run") or 0) >= DEFAULT_MD_MIN_EXT:
        score += 6  # extended under/over orange — watch
    # Regime alignment bonus / penalty
    reg = S.get("regime")
    mode = st.get("setup_mode")
    if reg == "chop" and mode in ("gap", "both"):
        score += 8
    elif reg == "trend" and mode == "mdrev":
        score += 8
    elif reg == "trend" and mode == "gap":
        score -= 14
    if S.get("conflict"):
        score -= 22
    if st.get("late"):
        score -= 8
    if st.get("stopped") is not None:
        score = min(score, 15)
    return max(0, min(100, score))


def analyze(
    ticker: str,
    bars_df: Optional[pd.DataFrame],
    daily: Optional[pd.DataFrame] = None,
    live_price: Optional[float] = None,
    bar_provider: Optional[str] = None,
    quote_provider: Optional[str] = None,
    quote_latency_ms: Optional[float] = None,
    opts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    t = ticker.upper().strip()
    is_crypto = _is_crypto(t)
    sess = _session_label(is_crypto)

    o = {
        "anchor_mins": DEFAULT_ANCHOR_M if not is_crypto else 0,
        "K": DEFAULT_K,
        "atr_mult": DEFAULT_ATR_MULT,
        "gap_min": DEFAULT_GAP_MIN if not is_crypto else 0.15,
        "Rmin": DEFAULT_RMIN,
        "rvol_min": DEFAULT_RVOL_MIN,
        "late_cut": LATE_CUT_M,
        "md_min_ext": DEFAULT_MD_MIN_EXT,
        "md_min_dist": DEFAULT_MD_MIN_DIST,
        "sigma_mult": DEFAULT_SIGMA_MULT,
        "ker_lookback": DEFAULT_KER_LOOKBACK,
        "ker_trend": DEFAULT_KER_TREND,
        "ker_chop": DEFAULT_KER_CHOP,
        "rvol_min_n": DEFAULT_RVOL_MIN_N,
        "regime_gate": True,  # suppress pure gap-fades in trend regime
    }
    if opts:
        o.update(opts)

    base = {
        "ticker": t,
        "provider": quote_provider or bar_provider,
        "bar_provider": bar_provider,
        "quote_provider": quote_provider,
        "quote_latency_ms": quote_latency_ms,
        **sess,
        "is_crypto": is_crypto,
    }

    bars = _prep_bars(bars_df)
    if len(bars) < 20:
        return {**base, "error": "insufficient bars", "edge": 0, "signal": "FLAT", "grade": "–"}

    days = _sessions(bars)
    if len(days) < 2:
        # crypto single continuous session — synthesize prior window
        if is_crypto and len(bars) >= 40:
            mid = len(bars) // 2
            # fake day split
            for i, b in enumerate(bars):
                b["d"] = "D0" if i < mid else "D1"
                b["mins"] = (i % 390) + RTH_OPEN_M  # synthetic RTH mins
            days = _sessions(bars)
        else:
            return {**base, "error": "need ≥2 sessions for orange anchor", "edge": 0, "signal": "FLAT", "grade": "–"}

    d0, d1 = days[-1], days[-2]
    i0 = _first_idx(bars, d0)
    p0 = _first_idx(bars, d1)
    iN = len(bars) - 1
    if i0 < 1 or p0 < 0:
        return {**base, "error": "anchor bars missing", "edge": 0, "signal": "FLAT", "grade": "–"}

    resolved = _resolve_day(bars, i0, iN, p0, o)
    st = resolved["st"]
    last = bars[iN]
    price = float(live_price) if live_price and live_price > 0 else float(last["c"])

    rvol_val, rvol_n = _rvol(bars, days, i0, iN, resolved["acc"]["vol"])
    ker_val = _ker(bars, iN, int(o.get("ker_lookback", DEFAULT_KER_LOOKBACK)))
    regime = _regime_from_ker(
        ker_val,
        trend_th=float(o.get("ker_trend", DEFAULT_KER_TREND)),
        chop_th=float(o.get("ker_chop", DEFAULT_KER_CHOP)),
    )
    adapt_now = _adaptive_sigma_mult(ker_val, base=1.0)

    # ── regime gate: pure gap-fade suppressed in trend; keep multi-day reclaim ──
    regime_block = False
    if (
        o.get("regime_gate", True)
        and regime == "trend"
        and st.get("setup_mode") == "gap"
        and st.get("tagged") is None
        and st.get("stopped") is None
    ):
        regime_block = True
        st["regime_block"] = True
    elif (
        o.get("regime_gate", True)
        and regime == "trend"
        and st.get("setup_mode") == "both"
        and st.get("md_trig") is not None
        and st.get("tagged") is None
        and st.get("stopped") is None
    ):
        # keep only multi-day reverse plan when gap+md agree but tape is trending
        st["trig"] = st["md_trig"]
        st["entry"] = st["md_entry"]
        st["stopPx"] = st["md_stop"]
        st["target"] = st["md_target"]
        st["R"] = st["md_R"]
        st["setup_mode"] = "mdrev"
        if st["entry"] and st["stopPx"]:
            st["riskPct"] = abs(st["entry"] - st["stopPx"]) / st["entry"] * 100.0
        st["regime_downgrade"] = "both→mdrev"

    S = {
        "st": st,
        "dir": resolved["dir"],
        "dev_ok": resolved["dev_ok"],
        "gap_pct": resolved["gap_pct"],
        "gap_provisional": resolved["gap_provisional"],
        "prior_close": resolved["prior_close"],
        "open_idx": resolved["open_idx"],
        "acc": resolved["acc"],
        "rvol": rvol_val,
        "rvol_n": rvol_n,
        "session_n": len(days),
        "ker": ker_val,
        "regime": regime,
        "conflict": False,
    }
    state = _state_text(S, o)
    grade = _grade(S, o)
    signal = _signal_badge(grade, state["cls"], st)
    edge = _edge(grade, S, state["cls"])

    blue = st["blue"]
    orange = st["orange"]
    sigma = st["sigma"]
    d_blue_pct = ((price - blue) / blue * 100.0) if blue else None
    dev_sigma = ((price - blue) / sigma) if blue and sigma and sigma > 0 else None
    to_orange = None
    if orange is not None:
        if st.get("setup_mode") == "mdrev" and st.get("md_side") == "long":
            to_orange = (price - orange) / price * 100.0
        elif st.get("setup_mode") == "mdrev" and st.get("md_side") == "short":
            to_orange = (orange - price) / price * 100.0
        elif resolved["dev_ok"]:
            to_orange = (price - orange) / price * 100.0 if resolved["dir"] > 0 else (orange - price) / price * 100.0

    # trade direction: gap-up reverts short (dir>0), gap-down reverts long;
    # multi-day reverse uses reclaim side when that is the active plan
    if st.get("setup_mode") == "mdrev" and st.get("md_side"):
        side = st["md_side"]
    else:
        side = "short" if resolved["dir"] > 0 else "long"

    # ── hard guards: geometry + don't fade into a continuing trend day ──
    entry, stop, target = st.get("entry"), st.get("stopPx"), st.get("target")
    bad_geom = False
    if entry is not None and stop is not None and target is not None:
        if not _geom_ok(side, entry, stop, target):
            bad_geom = True
            st["bad_geom"] = True
            st["noRunway"] = True
    trend_block = False
    if st.get("setup_mode") in ("gap", "both", None) and st.get("trig") is not None:
        if _day_trend_blocks_fade(bars, i0, iN, resolved["gap_pct"], side):
            trend_block = True

    # scrub untradeable levels (no-runway / inverted target / trend-day fade / regime)
    # only for open plans — keep levels on TAGGED/STOPPED for audit
    if (
        (st.get("noRunway") or bad_geom or trend_block or regime_block)
        and st.get("tagged") is None
        and st.get("stopped") is None
    ):
        st["entry"] = st["stopPx"] = st["target"] = None
        st["R"] = st["riskPct"] = st["runwayPct"] = None
        if trend_block:
            st["trend_block"] = True
        if regime_block:
            st["regime_block"] = True

    # recompute state/grade/signal after scrub
    if regime_block:
        state = {"txt": f"REGIME BLOCK · trend tape no gap-fade", "cls": "none"}
        grade = "–"
        signal = _signal_badge(grade, state["cls"], st)
        edge = max(0, _edge(grade, S, state["cls"]) - 12)
    elif trend_block:
        state = {"txt": f"TREND BLOCK · no fade {side}", "cls": "none"}
        grade = "–"
        signal = _signal_badge(grade, state["cls"], st)
        edge = max(0, _edge(grade, S, state["cls"]) - 18)
    elif bad_geom or st.get("noRunway"):
        state = {"txt": "CONFIRMED · NO RUNWAY", "cls": "none"}
        if st.get("trig") is not None and grade not in ("✕", "–"):
            grade = "B"
        signal = _signal_badge(grade, state["cls"], st)
        edge = max(0, _edge(grade, S, state["cls"]) - 10)
    elif st.get("noRunway"):
        # ensure badge is WATCH not TRIGGER
        signal = _signal_badge(grade, state["cls"], st)

    # Thin RVOL: demote live TRIGGER → WATCH for desk hygiene
    if (
        signal == "TRIGGER"
        and rvol_val is not None
        and rvol_n < int(o.get("rvol_min_n", DEFAULT_RVOL_MIN_N))
    ):
        signal = "WATCH"
        st["thin_rvol"] = True
        if grade in ("A", "LA"):
            grade = "B" if grade == "A" else "LB"
        edge = max(0, edge - 8)

    # Replay 2026-08-12: pure mdrev in chop is −0.10R (n=107). Gap/both keep.
    # Mixed-regime mdrev was +0.43R — leave those on the A desk.
    if (
        st.get("setup_mode") == "mdrev"
        and regime == "chop"
        and grade in ("A", "LA")
    ):
        grade = "B" if grade == "A" else "LB"
        st["mdrev_chop_demote"] = True
        edge = max(0, edge - 10)

    has_trig = st["trig"] is not None or st.get("md_trig") is not None
    plan_ok = _geom_ok(side, st.get("entry"), st.get("stopPx"), st.get("target"))
    # Desk default: only A/LA are "actionable" quality; B still visible when grade_min allows
    actionable = (
        grade in ("A", "LA", "B", "LB")
        and has_trig
        and st["stopped"] is None
        and st["tagged"] is None
        and plan_ok
        and not st.get("noRunway")
        and not trend_block
        and not regime_block
        and not st.get("thin_rvol")
    )
    live_actionable = (
        actionable
        and grade in ("A", "LA")
        and (sess["session_label"] in ("rth", "premarket", "24/7 crypto"))
    )

    # chart markers relative to focus day
    def _rel(idx):
        if idx is None:
            return None
        return int(idx - i0)

    return {
        **base,
        "price": round(price, 4 if price < 10 else 2),
        "blue": round(blue, 4 if blue and blue < 10 else 2) if blue else None,
        "orange": round(orange, 4 if orange and orange < 10 else 2) if orange else None,
        "sigma": round(sigma, 6) if sigma is not None else None,
        "vwap": round(blue, 4 if blue and blue < 10 else 2) if blue else None,  # alias for layout
        "avwap": round(orange, 4 if orange and orange < 10 else 2) if orange else None,
        "dist_std": round(dev_sigma, 2) if dev_sigma is not None else None,
        "d_blue_pct": round(d_blue_pct, 3) if d_blue_pct is not None else None,
        "to_orange_pct": round(to_orange, 3) if to_orange is not None else None,
        "gap_pct": round(resolved["gap_pct"], 3),
        "gap_provisional": resolved["gap_provisional"],
        "prior_close": round(resolved["prior_close"], 4 if resolved["prior_close"] < 10 else 2),
        "dir": resolved["dir"],
        "side": side,
        "grade": grade,
        "state": state["txt"],
        "state_cls": state["cls"],
        "signal": signal,
        "edge": edge,
        "entry": round(st["entry"], 4 if st["entry"] and st["entry"] < 10 else 2) if st.get("entry") else None,
        "stop": round(st["stopPx"], 4 if st["stopPx"] and st["stopPx"] < 10 else 2) if st.get("stopPx") else None,
        "target": round(st["target"], 4 if st["target"] and st["target"] < 10 else 2) if st.get("target") else None,
        "rr": round(st["R"], 2) if st.get("R") is not None else None,
        "risk_pct": round(st["riskPct"], 3) if st.get("riskPct") is not None else None,
        "runway_pct": round(st["runwayPct"], 3) if st.get("runwayPct") is not None else None,
        "rvol": round(S["rvol"], 3) if S["rvol"] is not None else None,
        "rvol_n": int(S.get("rvol_n") or 0),
        "session_n": int(S.get("session_n") or len(days)),
        "fakes": st["fake"],
        "late": st["late"],
        "no_runway": bool(st.get("noRunway")),
        "bad_geom": bool(st.get("bad_geom")),
        "trend_block": bool(st.get("trend_block")),
        "regime_block": bool(st.get("regime_block")),
        "thin_rvol": bool(st.get("thin_rvol")),
        "actionable": actionable,
        "live_actionable": live_actionable,
        "focus_day": d0,
        "prior_day": d1,
        "note": state["txt"],
        "setup_mode": st.get("setup_mode"),
        "md_side": st.get("md_side"),
        "md_ext_run": st.get("md_ext_run") or 0,
        "md_max_dist": round(st["md_max_dist"], 3) if st.get("md_max_dist") else None,
        "ker": round(ker_val, 3) if ker_val is not None else None,
        "regime": regime,
        "adapt_mult": round(adapt_now, 3),
        "sigma_vw": round(sigma, 6) if sigma is not None else None,  # volume-weighted σ
        "conflict": False,
        # chart payload hooks
        "_chart": {
            "bars": bars[i0 : iN + 1],
            "blue_trail": st["blueTrail"],
            "orange_trail": st["orangeTrail"],
            "sig_trail": st["sigTrail"],
            "prior_close": resolved["prior_close"],
            "markers": {
                "firstBreak": _rel(st["firstBreak"]),
                "trig": _rel(st["trig"] if st["trig"] is not None else st.get("md_trig")),
                "tagged": _rel(st["tagged"]),
                "stopped": _rel(st["stopped"]),
                "mdTrig": _rel(st.get("md_trig")),
                "openIdx": (resolved["open_idx"] - i0) if resolved["open_idx"] is not None else None,
            },
            "levels": {
                "entry": st["entry"],
                "stop": st["stopPx"],
                "target": st["target"],
                "blue": blue,
                "orange": orange,
            },
            "dir": resolved["dir"],
            "side": side,
            "grade": grade,
            "state": state["txt"],
            "setup_mode": st.get("setup_mode"),
        },
    }


def build_chart_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize analyze() _chart into API-friendly series."""
    ch = row.get("_chart") or {}
    bars = ch.get("bars") or []
    blue_t = ch.get("blue_trail") or []
    orange_t = ch.get("orange_trail") or []
    sig_t = ch.get("sig_trail") or []
    series = []
    for i, b in enumerate(bars):
        blue = blue_t[i] if i < len(blue_t) else None
        orange = orange_t[i] if i < len(orange_t) else None
        sig = sig_t[i] if i < len(sig_t) else None
        upper = (blue + sig) if blue is not None and sig is not None else None
        lower = (blue - sig) if blue is not None and sig is not None else None
        series.append({
            "t": b["ts"],
            "time": b["time"],
            "open": b["o"],
            "high": b["h"],
            "low": b["l"],
            "price": b["c"],
            "volume": b["v"],
            "blue": round(blue, 4) if blue is not None else None,
            "orange": round(orange, 4) if orange is not None else None,
            "upper": round(upper, 4) if upper is not None else None,
            "lower": round(lower, 4) if lower is not None else None,
            "vwap": round(blue, 4) if blue is not None else None,
            "avwap": round(orange, 4) if orange is not None else None,
        })
    levels = ch.get("levels") or {}
    markers = ch.get("markers") or {}
    return {
        "ticker": row.get("ticker"),
        "series": series,
        "levels": levels,
        "markers": markers,
        "prior_close": ch.get("prior_close"),
        "dir": ch.get("dir"),
        "side": ch.get("side"),
        "grade": ch.get("grade") or row.get("grade"),
        "state": ch.get("state") or row.get("state"),
        "price": row.get("price"),
        "blue": row.get("blue"),
        "orange": row.get("orange"),
        "gap_pct": row.get("gap_pct"),
        "entry": row.get("entry"),
        "stop": row.get("stop"),
        "target": row.get("target"),
        "rr": row.get("rr"),
        "rvol": row.get("rvol"),
        "rvol_n": row.get("rvol_n"),
        "session_n": row.get("session_n"),
        "signal": row.get("signal"),
        "edge": row.get("edge"),
        "bar_provider": row.get("bar_provider"),
        "quote_provider": row.get("quote_provider"),
        "session_label": row.get("session_label"),
        "legend": {
            "blue": "day VWAP (session, hlc3×volume)",
            "orange": "prior-day-anchored VWAP",
            "band": "±1 volume-weighted σ around blue (adaptive × KER)",
            "prior": "prior RTH close",
        },
        "regime": row.get("regime"),
        "ker": row.get("ker"),
        "setup_mode": row.get("setup_mode") or ch.get("setup_mode"),
    }
