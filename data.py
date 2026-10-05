"""
Data layer for VWAP One.

Primary path: free multi-provider rotation (providers.py).
Optional bulk yfinance for pure equity lists when rotate is slow / offline.
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
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
    with_daily: bool = True,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
    """Legacy bulk path — equities only works well. with_daily=False skips the daily download."""
    from providers import yf_module
    try:
        yf = yf_module()
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
    days = pd.DataFrame()
    if with_daily:
        try:
            days = yf.download(
                syms, period=daily_period, interval="1d",
                group_by="ticker", threads=True, progress=False, auto_adjust=True,
            )
        except Exception as e:
            log.warning("yfinance daily: %s", e)

    bar_map = _split_batch(bars, equity)
    day_map = _split_batch(days, equity)
    if len(equity) == 1:
        t = equity[0]
        if t not in bar_map:
            n = _norm_ohlcv(bars)
            if n is not None:
                bar_map[t] = n
        if t not in day_map and with_daily:
            n = _norm_ohlcv(days)
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
    crypto_days: Optional[int] = None,
    with_daily: bool = True,
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
    crypto_days: whole prior ET days of crypto bars, paged from the venue and cached
    (default providers.CRYPTO_HISTORY_DAYS, enough for the engine's 7-prior RVOL baseline).
    with_daily=False skips the daily bars: the engine does not read them (only VWAP One's cross-check
    does), and they were half of every scan's Yahoo requests.
    """
    from providers import bars_range_for_interval

    if not bars_period:
        bars_period = bars_range_for_interval(bars_interval)

    tickers = [t.upper().strip() for t in tickers if t.strip()]
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return {}, {}, {}, {}, {}

    # v3: longer free history (was 5d) — bust thin-history cache
    key = f"v3hist|{mode}|{','.join(tickers)}|{bars_interval}|{bars_period}|{crypto_days}|{with_daily}"
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
        yb, yd = _yfinance_bulk(tickers, bars_period, bars_interval, daily_period, with_daily=with_daily)
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
        rb, rd, rp, rl, rq = batch_rotate_fetch(need, bars_interval=bars_interval, history_days=crypto_days,
                                                with_daily=with_daily)
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


# The desk's liquidity floor (VWAP_BLUE_MIN_DVOL, the $M box) is an equity session floor. Crypto's is a
# quarter of it, on 24h volume: $2M / $0.5M at the default, as the README and /api/scan always said.
EQUITY_MIN_DVOL = 2_000_000.0
CRYPTO_DVOL_SHARE = 0.25

# The session's day is read in this tz by name, as it always was (pytz on pandas 2, zoneinfo on 3).
_ET_NAME = "America/New_York"
# Stamps _utc_stamps takes: [1900, 2100), far inside the range of every DatetimeIndex unit.
_UTC_STAMPS_SPAN = ("1900-01-01", "2100-01-01")


def _utc_stamps(idx: pd.Index) -> Optional[pd.DatetimeIndex]:
    """
    The index as tz-aware UTC stamps (naive stamps are UTC): the instants pd.to_datetime(idx, utc=True)
    returns, without its cache check, which boxed every stamp as a Timestamp. None for an index it does
    not cover (not a DatetimeIndex, NaT, a stamp outside 1900-2100), which session_dollar_volume then
    reads through pd.to_datetime and per-stamp dates, as before.
    """
    if not isinstance(idx, pd.DatetimeIndex) or idx.hasnans:
        return None
    i8 = idx.asi8   # UTC instants in the index's unit
    lo, hi = (np.datetime64(x, idx.unit).astype(np.int64) for x in _UTC_STAMPS_SPAN)
    if i8.min() < lo or i8.max() >= hi:
        return None
    return idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")


def _et_days(utc: pd.DatetimeIndex) -> np.ndarray:
    """
    The ET calendar day of every stamp as datetime64[D], for the whole index at once: the same
    per-stamp conversion DatetimeIndex.date ran, without a date object per bar.
    """
    return utc.tz_convert(_ET_NAME).tz_localize(None).values.astype("datetime64[D]")


def _dollars(frame: pd.DataFrame) -> float:
    c = pd.to_numeric(frame["Close"], errors="coerce").fillna(0.0)
    v = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0.0)
    return float((c * v).sum())


def session_dollar_volume(df: Optional[pd.DataFrame], ticker: Optional[str] = None) -> float:
    """
    Approx $ volume for the liquidity rank/filter, sum(Volume × Close): for crypto (pass `ticker`) the
    24 hours to the newest bar; otherwise the larger of the latest ET day so far and the ET day before.
    A day so far is minutes old early on. A 24/7 market's ET day after midnight: 9% of the desk's crypto
    names cleared $2M at 00:55 ET and 79% at 23:55, and 96-97% at every hour on 24h volume and $0.5M.
    A stock in premarket, once it first trades: 27% of the desk's stocks cleared $2M at 04:30 ET and
    9.5% at 09:00 on the day so far, 99.7% at every hour on the larger of today and its prior session,
    which also lets a normally thin stock in once a heavy day takes it past the floor.
    """
    if df is None or getattr(df, "empty", True):
        return 0.0
    try:
        if not {"Close", "Volume"}.issubset(set(map(str, df.columns))):
            n = _norm_ohlcv(df)
        else:
            n = df
        if n is None or n.empty:
            return 0.0
        utc = _utc_stamps(n.index)
        ts = utc if utc is not None else pd.to_datetime(n.index, utc=True, errors="coerce")
        if ts.isna().all():
            day = n.tail(min(100, len(n)))
        elif ticker and looks_crypto(ticker):
            day = n.loc[ts > ts[-1] - pd.Timedelta(hours=24)]
        elif utc is not None:
            days = _et_days(utc)
            last = days[-1]
            day = n.loc[days == last]       # no NaT, so the last row is always in it
            prior = days[days < last]
            if len(prior):
                return max(_dollars(day), _dollars(n.loc[days == prior.max()]))
        else:
            # group by US/Eastern calendar day
            try:
                days = ts.tz_convert(_ET_NAME).date
            except Exception:
                days = pd.DatetimeIndex(ts).tz_localize(None).date
            last = days[-1]
            mask = [d == last for d in days]
            day = n.loc[mask]
            if day is None or len(day) == 0:
                day = n.tail(min(100, len(n)))
            else:
                prior = [d for d in days if d is not pd.NaT and d < last]
                if prior:
                    prev = max(prior)
                    return max(_dollars(day), _dollars(n.loc[[d == prev for d in days]]))
        return _dollars(day)
    except Exception:
        try:
            c = pd.to_numeric(df["Close"], errors="coerce").fillna(0.0).tail(100)
            v = pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0).tail(100)
            return float((c * v).sum())
        except Exception:
            return 0.0


def min_dollar_volume_for(ticker: str, override: Optional[float] = None) -> float:
    """
    The liquidity floor for `ticker`. `override` is the desk's floor (default EQUITY_MIN_DVOL):
    equities use it as is, crypto a quarter of it. The desk always passed its floor, and this used to
    return it unchanged for crypto too, so crypto was held to the equity $2M.
    """
    floor = EQUITY_MIN_DVOL if override is None else max(0.0, float(override))
    return floor * CRYPTO_DVOL_SHARE if looks_crypto(ticker or "") else floor


def passes_volume_filter(
    ticker: str,
    bars_df: Optional[pd.DataFrame],
    min_dvol: Optional[float] = None,
) -> Tuple[bool, float]:
    """Return (pass, dollar_vol). min_dvol is the desk's floor (see min_dollar_volume_for); 0 disables it."""
    dvol = session_dollar_volume(bars_df, ticker)
    if min_dvol is not None and min_dvol <= 0:
        return True, dvol
    return dvol >= min_dollar_volume_for(ticker, min_dvol), dvol


def rotation_score(row: Dict[str, Any]) -> float:
    """
    Rank candidates for the desk list when scanning a wide pool.
    Prefers: large |gap|, distance from blue, RVOL, edge, grade A, live_actionable,
    higher session $ volume (liquidity).
    Penalizes: conflict, regime_block, trend_block, thin samples, illiquid.
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
    # liquidity: log-scale $vol (prefer names with real tape)
    dvol = float(row.get("dollar_vol") or 0.0)
    if dvol > 0:
        score += min(18.0, math.log10(dvol + 1.0) * 2.2)  # ~$10M → ~15 pts
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
    if row.get("illiquid"):
        score -= 25
    return score


# re-export status
get_provider_status = provider_status
