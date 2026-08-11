"""
Data layer for VWAP One.

Primary path: free multi-provider rotation (providers.py).
Optional bulk yfinance for pure equity lists when rotate is slow / offline.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from providers import (
    batch_rotate_fetch,
    fetch_quote,
    looks_crypto,
    provider_status,
)

log = logging.getLogger("vwap_one.data")

DEFAULT = [
    # core liquid book — full list lives in universe.txt
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "NFLX",
    "AMD", "AVGO", "SMCI", "TSM", "MU", "PLTR", "CRM", "ORCL",
    "JPM", "GS", "V", "XOM", "LLY", "UNH",
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "GLD", "TLT",
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD",
]

_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_TTL = 18.0


def _norm_ohlcv(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)
    rename = {}
    for c in out.columns:
        cl = str(c).lower().replace(" ", "_")
        if cl == "open":
            rename[c] = "Open"
        elif cl == "high":
            rename[c] = "High"
        elif cl == "low":
            rename[c] = "Low"
        elif cl in ("close", "adj_close", "adjclose"):
            rename[c] = "Close"
        elif cl == "volume":
            rename[c] = "Volume"
    out = out.rename(columns=rename)
    need = {"Open", "High", "Low", "Close", "Volume"}
    if not need.issubset(set(out.columns)):
        return None
    return out[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")


def _split_batch(raw: pd.DataFrame, tickers: List[str]) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        if any(t in level0 for t in tickers):
            for t in tickers:
                try:
                    n = _norm_ohlcv(raw[t])
                    if n is not None and len(n) > 5:
                        out[t] = n
                except Exception:
                    continue
        else:
            for t in tickers:
                try:
                    cols = [c for c in raw.columns if c[1] == t]
                    if not cols:
                        continue
                    sub = raw[cols].copy()
                    sub.columns = [c[0] for c in cols]
                    n = _norm_ohlcv(sub)
                    if n is not None and len(n) > 5:
                        out[t] = n
                except Exception:
                    continue
    else:
        if len(tickers) == 1:
            n = _norm_ohlcv(raw)
            if n is not None:
                out[tickers[0]] = n
    return out


def _yfinance_bulk(
    tickers: List[str],
    bars_period: str = "8d",
    bars_interval: str = "5m",
    daily_period: str = "2y",
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
    """Legacy bulk path — equities only works well."""
    try:
        import yfinance as yf
    except ImportError:
        return {}, {}

    from datetime import datetime, timedelta, timezone

    from providers import (
        RANGE_LOOKBACK_DAYS,
        DEFAULT_BARS_RANGE,
        YAHOO_1M_MAX_DAYS,
        bars_range_for_interval,
    )

    equity = [t for t in tickers if not looks_crypto(t)]
    if not equity:
        return {}, {}

    # Prefer start/end so free-form ranges (8d) are not silently collapsed to 5d.
    period_label = bars_period or bars_range_for_interval(bars_interval)
    lookback = RANGE_LOOKBACK_DAYS.get(period_label, RANGE_LOOKBACK_DAYS[DEFAULT_BARS_RANGE])
    if (bars_interval or "").lower() in ("1m", "1min", "2m"):
        lookback = min(lookback, YAHOO_1M_MAX_DAYS)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback)

    syms = " ".join(equity)
    try:
        bars = yf.download(
            syms, start=start, end=end, interval=bars_interval,
            group_by="ticker", threads=True, progress=False, auto_adjust=True, prepost=True,
        )
        if bars is None or (hasattr(bars, "empty") and bars.empty):
            named = period_label if period_label in ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y") else "5d"
            bars = yf.download(
                syms, period=named, interval=bars_interval,
                group_by="ticker", threads=True, progress=False, auto_adjust=True, prepost=True,
            )
    except Exception as e:
        log.warning("yfinance bars: %s", e)
        bars = pd.DataFrame()
    try:
        daily = yf.download(
            syms, period=daily_period, interval="1d",
            group_by="ticker", threads=True, progress=False, auto_adjust=True,
        )
    except Exception as e:
        log.warning("yfinance daily: %s", e)
        daily = pd.DataFrame()

    bar_map = _split_batch(bars, equity)
    day_map = _split_batch(daily, equity)
    if len(equity) == 1:
        t = equity[0]
        if t not in bar_map:
            n = _norm_ohlcv(bars)
            if n is not None:
                bar_map[t] = n
        if t not in day_map:
            n = _norm_ohlcv(daily)
            if n is not None:
                day_map[t] = n
    return bar_map, day_map


def batch_fetch(
    tickers: List[str],
    bars_period: Optional[str] = None,
    bars_interval: str = "5m",
    daily_period: str = "2y",
    force: bool = False,
    mode: str = "rotate",
) -> Tuple[
    Dict[str, pd.DataFrame],
    Dict[str, pd.DataFrame],
    Dict[str, str],
    Dict[str, float],
    Dict[str, Any],
]:
    """
    Returns (bars, daily, bar_provider, live_prices, quote_meta).

    mode:
      rotate — free multi-provider (default)
      hybrid — yfinance bulk equities + rotate crypto + fill gaps with rotate
      yfinance — bulk only (no crypto venues)

    bars_period defaults via bars_range_for_interval (1m→8d, 5m→1mo).
    """
    from providers import bars_range_for_interval

    if not bars_period:
        bars_period = bars_range_for_interval(bars_interval)

    tickers = [t.upper().strip() for t in tickers if t.strip()]
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return {}, {}, {}, {}, {}

    # v3: longer free history (was 5d) — bust thin-history cache
    key = f"v3hist|{mode}|{','.join(tickers)}|{bars_interval}|{bars_period}"
    now = time.time()
    hit = _cache.get(key)
    if not force and hit and now - hit[0] < _TTL:
        c = hit[1]
        return c["bars"], c["daily"], c["bar_prov"], c["live"], c["quote_meta"]

    bar_map: Dict[str, pd.DataFrame] = {}
    day_map: Dict[str, pd.DataFrame] = {}
    bar_prov: Dict[str, str] = {}
    live: Dict[str, float] = {}
    quote_meta: Dict[str, Any] = {}

    if mode in ("yfinance", "hybrid"):
        yb, yd = _yfinance_bulk(tickers, bars_period, bars_interval, daily_period)
        bar_map.update(yb)
        day_map.update(yd)
        for t in yb:
            bar_prov[t] = "yfinance"
            try:
                live[t] = float(yb[t]["Close"].iloc[-1])
                quote_meta[t] = {"provider": "yfinance/bar", "state": "bar"}
            except Exception:
                pass

    need = [t for t in tickers if t not in bar_map or looks_crypto(t) or mode == "rotate"]
    if mode == "rotate":
        need = list(tickers)
    elif mode == "hybrid":
        # re-fetch crypto always via venues; fill equity gaps
        need = [t for t in tickers if looks_crypto(t) or t not in bar_map]

    if need:
        rb, rd, rp, rl, rq = batch_rotate_fetch(need, bars_interval=bars_interval)
        # prefer rotate bars for crypto; fill missing equities
        for t, df in rb.items():
            if looks_crypto(t) or t not in bar_map or mode == "rotate":
                bar_map[t] = df
                bar_prov[t] = rp.get(t, "rotate")
        for t, df in rd.items():
            if looks_crypto(t) or t not in day_map or mode == "rotate":
                day_map[t] = df
        for t, px in rl.items():
            live[t] = px
        quote_meta.update(rq)

    # any ticker still missing live → quote rotate
    for t in tickers:
        if t not in live:
            q = fetch_quote(t)
            if q.get("price"):
                live[t] = float(q["price"])
                quote_meta[t] = {
                    "provider": q.get("provider"),
                    "latency_ms": q.get("latency_ms"),
                    "state": q.get("state"),
                }

    payload = {
        "bars": bar_map,
        "daily": day_map,
        "bar_prov": bar_prov,
        "live": live,
        "quote_meta": quote_meta,
    }
    _cache[key] = (now, payload)
    return bar_map, day_map, bar_prov, live, quote_meta


def live_last_prices(tickers: List[str], force: bool = False) -> Dict[str, float]:
    """Compatibility helper — rotate quotes only."""
    out: Dict[str, float] = {}
    for t in tickers:
        q = fetch_quote(t)
        if q.get("price"):
            out[t.upper()] = float(q["price"])
    return out


def load_universe(max_n: Optional[int] = None) -> List[str]:
    path = Path(__file__).resolve().parent / "universe.txt"
    tickers: List[str] = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tickers.append(line.split()[0].upper())
    if not tickers:
        tickers = list(DEFAULT)
    tickers = list(dict.fromkeys(tickers))
    return tickers[:max_n] if max_n else tickers


def rotation_score(row: Dict[str, Any]) -> float:
    """
    Rank candidates for the desk list when scanning a wide pool.
    Prefers: large |gap|, distance from blue, RVOL, edge, grade A, live_actionable.
    Penalizes: conflict, regime_block, trend_block, thin samples.
    """
    if not isinstance(row, dict):
        return -1e9
    if row.get("error"):
        return -1e6
    gap = abs(float(row.get("gap_pct") or 0.0))
    d_blue = abs(float(row.get("d_blue_pct") or 0.0))
    rvol = float(row.get("rvol") or 1.0)
    edge = float(row.get("edge") or 0.0)
    grade = str(row.get("grade") or "–")
    gboost = {"A": 25, "LA": 18, "B": 8, "LB": 5, "C": 2, "✕": 0, "–": 0}.get(grade, 0)
    score = gap * 3.0 + d_blue * 1.5 + max(0.0, rvol - 0.8) * 12.0 + edge * 0.35 + gboost
    if row.get("live_actionable"):
        score += 20
    elif row.get("actionable"):
        score += 10
    if row.get("setup_mode") == "both":
        score += 12
    elif row.get("setup_mode") == "mdrev":
        score += 6
    if row.get("regime") == "chop" and row.get("setup_mode") in ("gap", "both"):
        score += 8
    if row.get("regime") == "trend" and row.get("setup_mode") == "mdrev":
        score += 8
    if row.get("conflict"):
        score -= 30
    if row.get("regime_block") or row.get("trend_block"):
        score -= 40
    if row.get("thin_rvol"):
        score -= 10
    if (row.get("rvol_n") or 0) < 2 and row.get("rvol") is not None:
        score -= 6
    return score


# re-export status
get_provider_status = provider_status
