"""
Free multi-provider rotation for VWAP One — no API keys.

Ported/simplified from:
  - flow_well_stack live_feed (OKX · Binance · Bybit · Coinbase)
  - vector free-rotate-adapter (crypto rotate + Yahoo chart)
  - vwap_scanner multi_provider_data (Stooq · CoinGecko · EODHD demo · Yahoo)

Never fabricates prices. Failed providers cool down and rotate; a provider that answers it has no
such symbol is skipped for that symbol only. Crypto 5m history is paged from the venue and cached
(see "crypto history" below).
"""

from __future__ import annotations

import collections
import csv
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger("vwap_one.providers")
ET = ZoneInfo("America/New_York")

UA = "VWAP-One/3.1 (+local research scanner)"
TIMEOUT = 8.0
COOLDOWN_SEC = 45.0

# Yahoo: "Only 8 days worth of 1m granularity data are allowed per request."
# Chart API accepts range=8d for 1m (10d → 422). Old 5d starved RVOL baselines.
DEFAULT_BARS_RANGE = "8d"
YAHOO_1M_MAX_DAYS = 8
# yfinance period= only knows named periods; map free-form ranges → calendar lookback.
RANGE_LOOKBACK_DAYS = {
    "1d": 1,
    "5d": 5,
    "7d": 7,
    "8d": 8,
    "10d": 8,  # clamp to Yahoo 1m hard-cap
    "1mo": 32,
    "3mo": 95,
    "6mo": 185,
    "1y": 370,
    "2y": 740,
}

# per-provider cooldown + counters
_lock = threading.Lock()
_cooldown_until: Dict[str, float] = {}
_stats: Dict[str, Dict[str, int]] = {}
# (provider, ticker) → skip until: the provider said it has no such symbol
NOT_LISTED_SEC = 6 * 3600.0
_not_listed: Dict[Tuple[str, str], float] = {}


class NotListed(RuntimeError):
    """
    The provider answered, and has nothing for this symbol: unknown, delisted, or no recent candles.
    Unlike a failure it does not cool the provider down. It used to: one coin a venue does not list
    (TON on OKX, TRX on Coinbase, SUI-USD on Yahoo) put that venue in a 45 s cooldown, and a batch
    then lost names the venue does list (29 of the desk's 42 crypto names got bars in one scan).
    """


class _RateLimited(RuntimeError):
    """A venue's rate-limit answer that arrives as HTTP 200 (OKX code 50011): wait and retry."""


def bars_range_for_interval(interval: str = "5m") -> str:
    """Pick a Yahoo-safe free range. 1m hard-caps at 8d; coarser bars can go longer."""
    iv = (interval or "5m").lower().replace("min", "m")
    if iv in ("1m", "2m"):
        return DEFAULT_BARS_RANGE
    if iv in ("5m", "15m", "30m"):
        return "1mo"
    if iv in ("1h", "60m", "1d"):
        return "3mo"
    return DEFAULT_BARS_RANGE


def _bump(name: str, ok: bool) -> None:
    with _lock:
        s = _stats.setdefault(name, {"ok": 0, "fail": 0, "rotates": 0})
        if ok:
            s["ok"] += 1
        else:
            s["fail"] += 1
            s["rotates"] += 1
            _cooldown_until[name] = time.time() + COOLDOWN_SEC


def _miss(name: str, ticker: str) -> None:
    """`name` has no `ticker`: skip that pair for a while; the provider stays available to the rest."""
    with _lock:
        s = _stats.setdefault(name, {"ok": 0, "fail": 0, "rotates": 0})
        s["miss"] = s.get("miss", 0) + 1
        _not_listed[(name, ticker)] = time.time() + NOT_LISTED_SEC


def _available(name: str, ticker: Optional[str] = None) -> bool:
    with _lock:
        now = time.time()
        if now < _cooldown_until.get(name, 0):
            return False
        return ticker is None or now >= _not_listed.get((name, ticker), 0)


def provider_status() -> Dict[str, Any]:
    with _hist_lock:
        hist = collections.Counter(c["venue"] for c in _hist.values())
    with _lock:
        now = time.time()
        return {
            "cooldown_sec": COOLDOWN_SEC,
            "crypto_history": {"days": CRYPTO_HISTORY_DAYS, "cached": sum(hist.values()), "by_venue": dict(hist)},
            "providers": {
                name: {
                    **_stats.get(name, {"ok": 0, "fail": 0, "rotates": 0}),
                    "cooling": max(0.0, round(_cooldown_until.get(name, 0) - now, 1)),
                }
                for name in sorted(
                    set(list(_stats.keys()) + list(_cooldown_until.keys())
                        + [
                            "okx", "binance", "bybit", "coinbase", "coingecko",
                            "yahoo_chart", "stooq", "eodhd_demo", "yfinance",
                        ])
                )
            },
        }


# HTTP 400 bodies that mean "no such symbol": Coinbase "Not allowed for delisted products",
# Binance {"code":-1121,"msg":"Invalid symbol."}
_UNLISTED_400 = ("delisted", "invalid symbol")


def _get_json(url: str, timeout: float = TIMEOUT) -> Any:
    """GET JSON. A 404 (Yahoo, Coinbase: no such symbol) or an unlisted-symbol 400 raises NotListed."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        try:
            if e.code == 404:
                raise NotListed("HTTP 404") from None
            if e.code == 400:
                body = e.read(300).decode("utf-8", errors="replace")
                if any(m in body.lower() for m in _UNLISTED_400):
                    raise NotListed(f"HTTP 400 {body[:80]}") from None
            raise
        finally:
            e.close()


def _get_text(url: str, timeout: float = TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── symbol helpers ──────────────────────────────────────────────────────────

def looks_crypto(ticker: str) -> bool:
    t = ticker.upper().replace("/", "-")
    if t.endswith("-USD") or t.endswith("-USDT") or t.endswith("-USDC"):
        return True
    bare = t.replace("-", "").replace("/", "")
    if bare.endswith(("USDT", "USDC", "BUSD")) and len(bare) >= 6:
        return True
    if bare in ("BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD", "BNBUSD"):
        return True
    # common bare crypto pairs used in FLOW
    if bare.endswith("USDT") or bare in ("BTC", "ETH", "SOL"):
        return True
    return False


def to_usdt(ticker: str) -> str:
    t = ticker.upper().replace("/", "").replace("-", "")
    if t.endswith("USDT"):
        return t
    if t.endswith("USD"):
        return t[:-3] + "USDT"
    if t in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK"):
        return t + "USDT"
    return t


def to_coinbase_product(ticker: str) -> str:
    t = ticker.upper()
    if "-USD" in t and not t.endswith("USDT"):
        return t if t.endswith("-USD") else t.replace("USDT", "USD")
    s = to_usdt(ticker)
    if s.endswith("USDT"):
        return s[:-4] + "-USD"
    return s + "-USD"


# OKX lists Toncoin as GRAM-USDT (Yahoo: "Gram (prev. Toncoin)"); Coinbase still calls it TON-USD.
OKX_BASES = {"TON": "GRAM"}

# Yahoo lists these coins under numbered symbols. Under the plain symbol it has no 5m bars (SUI-USD, ...)
# or a different token: ARB-USD is "ARbit", TON-USD "TON Token", JUP-USD another "Jupiter". Each target's
# price matched Coinbase and OKX on 2026-10-04. index.html carries the same map (tests/test_pages_demo.py).
YAHOO_CRYPTO_SYMBOLS = {
    "APT-USD": "APT21794-USD",
    "ARB-USD": "ARB11841-USD",
    "GRT-USD": "GRT6719-USD",
    "IMX-USD": "IMX10603-USD",
    "JUP-USD": "JUP29210-USD",
    "PEPE-USD": "PEPE24478-USD",
    "POL-USD": "POL28321-USD",   # Polygon, prev. MATIC
    "STX-USD": "STX4847-USD",
    "SUI-USD": "SUI20947-USD",
    "TAO-USD": "TAO22974-USD",
    "TON-USD": "GRAM-USD",
    "UNI-USD": "UNI7083-USD",
}


def to_okx_inst(ticker: str) -> str:
    s = to_usdt(ticker)
    if s.endswith("USDT"):
        return f"{OKX_BASES.get(s[:-4], s[:-4])}-USDT"
    return s


def to_yahoo_symbol(ticker: str) -> str:
    t = ticker.upper()
    if looks_crypto(t):
        # Yahoo prefers BTC-USD
        s = to_usdt(t)
        y = s[:-4] + "-USD" if s.endswith("USDT") else (t if "-" in t else f"{t}-USD")
        return YAHOO_CRYPTO_SYMBOLS.get(y, y)
    return t


def to_stooq(ticker: str) -> str:
    t = ticker.lower().replace("-usd", ".us")  # crypto not on stooq usually
    if looks_crypto(ticker):
        return ""
    if "." not in t:
        return f"{t}.us"
    return t


# ── crypto bars ─────────────────────────────────────────────────────────────

def _bars_from_rows(
    rows: List[Tuple[int, float, float, float, float, float]],
) -> pd.DataFrame:
    """rows: (ts_ms, o, h, l, c, v) ascending. Index is America/New_York tz-aware."""
    if not rows:
        return pd.DataFrame()
    idx = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True).tz_convert("America/New_York")
    df = pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Volume": [r[5] for r in rows],
        },
        index=idx,
    )
    return df.dropna(how="all")


def _okx_data(url: str) -> List[Any]:
    """OKX answers HTTP 200 with code "51001" for an instrument it does not list."""
    data = _get_json(url)
    code = str(data.get("code", "0"))
    if code == "51001":
        raise NotListed("okx: no such instrument")
    if code == "50011":
        raise _RateLimited("okx: rate limited")
    if code != "0":
        raise RuntimeError(f"okx code {code}: {data.get('msg')}")
    return data.get("data") or []


def _bybit_list(url: str) -> List[Any]:
    """Bybit answers HTTP 200 with a non-zero retCode, "Not supported symbols" for one it does not list."""
    data = _get_json(url)
    rc, msg = data.get("retCode", 0), str(data.get("retMsg") or "")
    if rc not in (0, None):
        if "symbol" in msg.lower():
            raise NotListed(f"bybit: {msg}")
        raise RuntimeError(f"bybit retCode {rc}: {msg}")
    return (data.get("result") or {}).get("list") or []


def _crypto_okx(symbol: str, interval: str, limit: int = 200) -> Tuple[pd.DataFrame, str]:
    bar = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "1d": "1D"}.get(interval, "5m")
    inst = to_okx_inst(symbol)
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar={bar}&limit={limit}"
    lst = _okx_data(url)
    rows = []
    for k in reversed(lst):
        rows.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])))
    df = _bars_from_rows(rows)
    if df.empty:
        raise RuntimeError("okx empty")
    return df, "okx"


def _crypto_binance(symbol: str, interval: str, limit: int = 300) -> Tuple[pd.DataFrame, str]:
    iv = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "1d"}.get(interval, "5m")
    s = to_usdt(symbol)
    url = f"https://api.binance.com/api/v3/klines?symbol={s}&interval={iv}&limit={limit}"
    raw = _get_json(url)
    if not isinstance(raw, list):
        raise RuntimeError("binance bad")
    rows = [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in raw]
    df = _bars_from_rows(rows)
    if df.empty:
        raise RuntimeError("binance empty")
    return df, "binance"


def _crypto_bybit(symbol: str, interval: str, limit: int = 200) -> Tuple[pd.DataFrame, str]:
    iv = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "1d": "D"}.get(interval, "5")
    s = to_usdt(symbol)
    url = (
        f"https://api.bybit.com/v5/market/kline?category=spot&symbol={s}"
        f"&interval={iv}&limit={limit}"
    )
    lst = _bybit_list(url)
    rows = []
    for k in reversed(lst):
        rows.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])))
    df = _bars_from_rows(rows)
    if df.empty:
        raise RuntimeError("bybit empty")
    return df, "bybit"


def _crypto_coinbase(symbol: str, interval: str) -> Tuple[pd.DataFrame, str]:
    product = to_coinbase_product(symbol)
    gran = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}.get(interval, 300)
    url = f"https://api.exchange.coinbase.com/products/{product}/candles?granularity={gran}"
    raw = _get_json(url)
    # [time, low, high, open, close, volume] newest first
    rows = []
    for k in reversed(raw):
        rows.append(
            (int(k[0]) * 1000, float(k[3]), float(k[2]), float(k[1]), float(k[4]), float(k[5]))
        )
    df = _bars_from_rows(rows)
    if df.empty:
        raise RuntimeError("coinbase empty")
    return df, "coinbase"


# ── crypto history: venue candles paged back, cached, tail refreshed ────────
#
# One candle call returns 200-350 bars, about a day of 5m: too short for the engine, whose RVOL compares
# today with up to 7 prior ET days (engine.RVOL_MAX_PRIORS) and needs 3 sessions at all. The pager walks
# a venue's candle endpoint from ET midnight CRYPTO_HISTORY_DAYS days ago to now; the series is cached
# per ticker and later calls fetch only its tail. The venue that built a series keeps it: venues quote
# different books (USD or USDT, their own volume), so a series is never stitched from two of them. If
# that venue fails, its cached series is served (state "cached") for CACHE_GRACE_SEC, then the next
# venue builds a new one. Every bar is a venue candle; gaps (Coinbase prints no 5m candle when nothing
# traded) stay gaps.

CRYPTO_HISTORY_DAYS = 8          # whole prior ET days: RVOL's 7-prior baseline + 1 spare
CACHE_GRACE_SEC = 300.0          # series' venue failing: serve its cache this long, then rebuild elsewhere
STALE_SERIES_SEC = 6 * 3600.0    # newest candle older than this: the venue no longer trades the coin
TAIL_OVERLAP_BARS = 2            # refetch the newest cached bars too: the last one was still forming
_BAR_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

Row = Tuple[int, float, float, float, float, float]


class _Limiter:
    """At most n requests per `per` seconds (sliding window), shared by every thread."""

    def __init__(self, n: int, per: float):
        self.n, self.per = n, per
        self.sent: collections.deque = collections.deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.sent and now - self.sent[0] >= self.per:
                    self.sent.popleft()
                if len(self.sent) < self.n:
                    self.sent.append(now)
                    return
                delay = self.per - (now - self.sent[0])
            time.sleep(delay)


# the venues' public limits, with headroom for the unthrottled quote and daily calls to the same hosts
_LIMITS = {
    "okx_candles": _Limiter(30, 2.0),     # market/candles: 40 per 2 s, newest 1,440 bars only
    "okx_history": _Limiter(16, 2.0),     # market/history-candles: 20 per 2 s
    "binance": _Limiter(10, 1.0),
    "bybit": _Limiter(10, 1.0),
    "coinbase": _Limiter(8, 1.0),         # 10 per s
}


def _limited(limiter: _Limiter, get: Callable[[], Any], tries: int = 3) -> Any:
    """Throttle `get`; a rate-limit answer (HTTP 429, OKX 50011) waits and retries, other errors propagate."""
    for attempt in range(tries):
        limiter.wait()
        try:
            return get()
        except (urllib.error.HTTPError, _RateLimited) as e:
            if getattr(e, "code", 429) != 429 or attempt == tries - 1:
                raise
            time.sleep(1.0 + attempt)


def _okx_window(symbol: str, interval: str, s_ms: int, e_ms: int) -> List[Row]:
    bar = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H"}[interval]
    recent = s_ms >= (time.time() - 1400 * _BAR_SEC[interval]) * 1000
    path = "candles" if recent else "history-candles"
    url = (f"https://www.okx.com/api/v5/market/{path}?instId={to_okx_inst(symbol)}&bar={bar}&limit=300"
           f"&after={e_ms + 1}&before={s_ms - 1}")        # exclusive bounds: the candles in [s, e]
    lst = _limited(_LIMITS["okx_candles" if recent else "okx_history"], lambda: _okx_data(url))
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in lst]


def _binance_window(symbol: str, interval: str, s_ms: int, e_ms: int) -> List[Row]:
    url = (f"https://api.binance.com/api/v3/klines?symbol={to_usdt(symbol)}&interval={interval}"
           f"&startTime={s_ms}&endTime={e_ms}&limit=1000")
    raw = _limited(_LIMITS["binance"], lambda: _get_json(url))
    if not isinstance(raw, list):
        raise RuntimeError("binance bad")
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in raw]


def _bybit_window(symbol: str, interval: str, s_ms: int, e_ms: int) -> List[Row]:
    iv = {"1m": "1", "5m": "5", "15m": "15", "1h": "60"}[interval]
    url = (f"https://api.bybit.com/v5/market/kline?category=spot&symbol={to_usdt(symbol)}&interval={iv}"
           f"&start={s_ms}&end={e_ms}&limit=1000")
    lst = _limited(_LIMITS["bybit"], lambda: _bybit_list(url))
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in lst]


def _coinbase_window(symbol: str, interval: str, s_ms: int, e_ms: int) -> List[Row]:
    def iso(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (f"https://api.exchange.coinbase.com/products/{to_coinbase_product(symbol)}/candles"
           f"?granularity={_BAR_SEC[interval]}&start={iso(s_ms)}&end={iso(e_ms)}")
    raw = _limited(_LIMITS["coinbase"], lambda: _get_json(url))
    if not isinstance(raw, list):
        raise RuntimeError("coinbase bad")
    # [time, low, high, open, close, volume]; a delisted product answers [] for any recent window
    return [(int(k[0]) * 1000, float(k[3]), float(k[2]), float(k[1]), float(k[4]), float(k[5])) for k in raw]


# (venue, candles in [s_ms, e_ms], most candles per request), in rotation order
_CRYPTO_PAGERS: List[Tuple[str, Callable[[str, str, int, int], List[Row]], int]] = [
    ("okx", _okx_window, 300),
    ("binance", _binance_window, 1000),
    ("bybit", _bybit_window, 1000),
    ("coinbase", _coinbase_window, 300),
]

_hist_lock = threading.Lock()
_hist: Dict[Tuple[str, str], Dict[str, Any]] = {}       # (ticker, interval) → venue, rows, start_ms, refreshed
_hist_busy: Dict[Tuple[str, str], threading.Lock] = {}   # one fetch per series at a time


def history_start_ms(days: int, now_s: Optional[float] = None) -> int:
    """ET midnight `days` days before today's ET date (the engine's crypto session is the ET day)."""
    today = datetime.fromtimestamp(time.time() if now_s is None else now_s, ET).date()
    return int(datetime.combine(today - timedelta(days=days), dtime(0, 0), tzinfo=ET).timestamp() * 1000)


def _page(window: Callable[[str, str, int, int], List[Row]], symbol: str, interval: str,
          s_ms: int, e_ms: int, per_page: int) -> List[Row]:
    """Every candle in [s_ms, e_ms], ascending: one request per `per_page` bars, oldest first."""
    step = _BAR_SEC[interval] * 1000
    rows: Dict[int, Row] = {}
    s = s_ms - s_ms % step
    while s <= e_ms:
        e = min(s + (per_page - 1) * step, e_ms)
        for r in window(symbol, interval, s, e):
            if s <= r[0] <= e:
                rows[r[0]] = r
        s = e + step
    return [rows[k] for k in sorted(rows)]


def _fresh(rows: List[Row], now_s: float) -> List[Row]:
    """A venue that has no candle for hours has stopped trading the coin (a delisted one answers [])."""
    if not rows:
        raise NotListed("no candles in the window")
    if rows[-1][0] < (now_s - STALE_SERIES_SEC) * 1000:
        last = datetime.fromtimestamp(rows[-1][0] / 1000, ET).strftime("%Y-%m-%d %H:%M ET")
        raise NotListed(f"no candle since {last}")
    return rows


def crypto_history(ticker: str, interval: str = "5m", days: int = CRYPTO_HISTORY_DAYS,
                   errors: Optional[List[str]] = None, now_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    Venue candles from ET midnight `days` days ago to now: {"venue", "rows", "state"}, or None when no
    venue can serve the coin (fetch_intraday then falls back to Yahoo). state "live": fetched now;
    "cached": the series' venue is failing and its last refresh is under CACHE_GRACE_SEC old.
    """
    errors = [] if errors is None else errors
    now_s = time.time() if now_s is None else now_s
    key = (ticker, interval)
    start_ms, end_ms = history_start_ms(days, now_s), int(now_s * 1000)
    step = _BAR_SEC[interval] * 1000
    pagers = {name: (fn, per) for name, fn, per in _CRYPTO_PAGERS}
    with _hist_lock:
        busy = _hist_busy.setdefault(key, threading.Lock())
    with busy:
        with _hist_lock:
            c = _hist.get(key)
        if c is not None and _available(c["venue"], ticker):
            name = c["venue"]
            fn, per = pagers[name]
            try:
                head = _page(fn, ticker, interval, start_ms, c["start_ms"] - step, per) if start_ms < c["start_ms"] else []
                tail_from = c["rows"][-min(TAIL_OVERLAP_BARS, len(c["rows"]))][0] if c["rows"] else start_ms
                merged = {r[0]: r for r in head + c["rows"] + _page(fn, ticker, interval, tail_from, end_ms, per)}
                rows = _fresh([merged[k] for k in sorted(merged) if k >= start_ms], now_s)
                _bump(name, True)
                with _hist_lock:
                    _hist[key] = {"venue": name, "rows": rows, "start_ms": start_ms, "refreshed": now_s}
                return {"venue": name, "rows": rows, "state": "live"}
            except NotListed as e:
                _miss(name, ticker)
                errors.append(f"{name}:{e}")
                c = None
                with _hist_lock:
                    _hist.pop(key, None)
            except Exception as e:
                _bump(name, False)
                errors.append(f"{name}:{e}")
        if c is not None and now_s - c["refreshed"] < CACHE_GRACE_SEC:
            return {"venue": c["venue"], "rows": [r for r in c["rows"] if r[0] >= start_ms], "state": "cached"}
        for name, fn, per in _CRYPTO_PAGERS:
            if not _available(name, ticker):
                continue
            try:
                rows = _fresh(_page(fn, ticker, interval, start_ms, end_ms, per), now_s)
            except NotListed as e:
                _miss(name, ticker)
                errors.append(f"{name}:{e}")
                continue
            except Exception as e:
                _bump(name, False)
                errors.append(f"{name}:{e}")
                continue
            _bump(name, True)
            with _hist_lock:
                _hist[key] = {"venue": name, "rows": rows, "start_ms": start_ms, "refreshed": now_s}
            return {"venue": name, "rows": rows, "state": "live"}
    return None


# ── equity / general: Yahoo chart REST (no key) ─────────────────────────────

def _yahoo_chart(symbol: str, interval: str = "5m", range_: str = DEFAULT_BARS_RANGE,
                 start_s: Optional[float] = None) -> Tuple[pd.DataFrame, str]:
    """`start_s` (crypto: the history window) asks for bars from then to now instead of `range_`."""
    sym = to_yahoo_symbol(symbol)
    # includePrePost: premarket + after-hours for equities (ignored for 1d)
    span = ({"period1": int(start_s), "period2": int(time.time())} if start_s is not None
            else {"range": range_})
    q = urllib.parse.urlencode({
        "interval": interval,
        **span,
        "includePrePost": "true",
    })
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?{q}"
    data = _get_json(url)
    r = (data.get("chart") or {}).get("result") or []
    if not r:
        raise RuntimeError("yahoo empty")
    r0 = r[0]
    ts = r0.get("timestamp") or []
    q0 = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for i, t in enumerate(ts):
        o, h, l, c = q0.get("open", [None])[i], q0.get("high", [None])[i], q0.get("low", [None])[i], q0.get("close", [None])[i]
        v = (q0.get("volume") or [0])[i] or 0
        if None in (o, h, l, c):
            continue
        if not all(map(lambda x: x == x, [o, h, l, c])):
            continue
        rows.append((int(t) * 1000, float(o), float(h), float(l), float(c), float(v)))
    df = _bars_from_rows(rows)
    if df.empty:
        raise RuntimeError("yahoo no bars")
    return df, "yahoo_chart"


def _yahoo_daily(symbol: str, range_: str = "2y") -> Tuple[pd.DataFrame, str]:
    return _yahoo_chart(symbol, interval="1d", range_=range_)


# ── last price quotes ───────────────────────────────────────────────────────

def _quote_coinbase(ticker: str) -> Tuple[float, str]:
    product = to_coinbase_product(ticker)
    url = f"https://api.exchange.coinbase.com/products/{product}/ticker"
    data = _get_json(url)
    px = float(data["price"])
    if px <= 0:
        raise RuntimeError("coinbase px")
    return px, "coinbase"


def _quote_binance(ticker: str) -> Tuple[float, str]:
    s = to_usdt(ticker)
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={s}"
    data = _get_json(url)
    px = float(data["price"])
    if px <= 0:
        raise RuntimeError("binance px")
    return px, "binance"


def _quote_okx(ticker: str) -> Tuple[float, str]:
    inst = to_okx_inst(ticker)
    url = f"https://www.okx.com/api/v5/market/ticker?instId={inst}"
    lst = _okx_data(url)
    if not lst:
        raise RuntimeError("okx empty")
    px = float(lst[0]["last"])
    if px <= 0:
        raise RuntimeError("okx px")
    return px, "okx"


def _quote_bybit(ticker: str) -> Tuple[float, str]:
    s = to_usdt(ticker)
    url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={s}"
    lst = _bybit_list(url)
    if not lst:
        raise RuntimeError("bybit empty")
    px = float(lst[0]["lastPrice"])
    if px <= 0:
        raise RuntimeError("bybit px")
    return px, "bybit"


_CG_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "DOGE": "dogecoin", "BNB": "binancecoin", "ADA": "cardano", "AVAX": "avalanche-2",
    "LINK": "chainlink",
}


def _quote_coingecko(ticker: str) -> Tuple[float, str]:
    s = to_usdt(ticker)
    base = s.replace("USDT", "").replace("USD", "")
    coin_id = _CG_MAP.get(base)
    if not coin_id:
        raise NotListed("coingecko unmapped")
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={coin_id}&vs_currencies=usd"
    )
    data = _get_json(url)
    px = float(data[coin_id]["usd"])
    if px <= 0:
        raise RuntimeError("coingecko px")
    return px, "coingecko"


def _quote_stooq(ticker: str) -> Tuple[float, str]:
    s = to_stooq(ticker)
    if not s:
        raise NotListed("stooq n/a crypto")
    url = f"https://stooq.com/q/l/?s={s}&f=sd2t2ohlcv&h&e=csv"
    text = _get_text(url)
    # Symbol,Date,Time,Open,High,Low,Close,Volume
    reader = csv.DictReader(io.StringIO(text))
    row = next(reader, None)
    if not row:
        raise RuntimeError("stooq empty")
    close = row.get("Close") or row.get("close")
    if not close or close == "N/D":
        raise NotListed("stooq N/D")
    px = float(close)
    if px <= 0:
        raise RuntimeError("stooq px")
    return px, "stooq"


def _quote_eodhd_demo(ticker: str) -> Tuple[float, str]:
    demo = {"AAPL", "MSFT", "TSLA", "BTC-USD", "ETH-USD"}
    clean = ticker.upper()
    ysym = to_yahoo_symbol(clean)
    if clean not in demo and ysym not in demo:
        raise NotListed("eodhd demo only")
    # real-time last trade demo
    sym = ysym if ysym in demo else clean
    if not sym.endswith(".US") and not looks_crypto(sym):
        api_sym = f"{sym}.US"
    else:
        api_sym = sym
    url = f"https://eodhd.com/api/real-time/{urllib.parse.quote(api_sym)}?api_token=demo&fmt=json"
    data = _get_json(url)
    px = float(data.get("close") or data.get("previousClose") or 0)
    if px <= 0:
        raise RuntimeError("eodhd empty")
    return px, "eodhd_demo"


def _quote_yahoo_chart(ticker: str) -> Tuple[float, str]:
    df, src = _yahoo_chart(ticker, interval="1m", range_="1d")
    px = float(df["Close"].iloc[-1])
    if px <= 0:
        raise RuntimeError("yahoo px")
    return px, src


# ── yfinance: the desk's bulk equity download (data.py) and the last-resort fallbacks ───────────────

YF_RETRIES = 2
_yf = None


def yf_module():
    """
    yfinance, configured once. Its network.retries default is 0, so one blip failed every stock in a
    bulk download: transient errors now retry twice. And yf.download unhides exceptions on
    config.network while history() reads config.debug (yfinance 1.0 to 1.7), so each failed request
    surfaced as TypeError("'NoneType' object is not subscriptable") and its real cause was lost: errors
    now show. history() then raises YFTickerMissingError where it used to log "possibly delisted" and
    return nothing; _yf_history turns that back into None.
    """
    global _yf
    if _yf is None:
        import yfinance as yf
        cfg = getattr(yf, "config", None)       # yfinance >= 1.0
        if cfg is not None:
            cfg.network.retries = YF_RETRIES
            cfg.debug.hide_exceptions = False
        _yf = yf
    return _yf


def _yf_missing(e: Exception) -> bool:
    try:
        from yfinance.exceptions import YFTickerMissingError
    except ImportError:
        return False
    return isinstance(e, YFTickerMissingError)


def _yf_history(symbol: str, **kw) -> Optional[pd.DataFrame]:
    """yf.Ticker(symbol).history(**kw), or None when Yahoo has no prices for the symbol."""
    try:
        return yf_module().Ticker(symbol).history(**kw)
    except Exception as e:
        if _yf_missing(e):
            return None
        raise


def _quote_yfinance(ticker: str) -> Tuple[float, str]:
    try:
        t = yf_module().Ticker(to_yahoo_symbol(ticker))
        info = getattr(t, "fast_info", None)
        if info is not None:
            px = float(getattr(info, "last_price", None) or 0)
            if px > 0:
                return px, "yfinance"
    except Exception as e:
        if not _yf_missing(e):
            raise
    hist = _yf_history(to_yahoo_symbol(ticker), period="1d", interval="1m", prepost=True)
    if hist is not None and not hist.empty:
        return float(hist["Close"].iloc[-1]), "yfinance"
    raise NotListed("yfinance: no prices")


def fetch_quote(ticker: str, prefer: Optional[str] = None) -> Dict[str, Any]:
    """
    Rotate free quote sources. Returns {price, provider, ...} or error. For crypto, `prefer` (the
    venue the bars came from) is tried first, so a row's price and bars quote the same book.
    """
    t0 = time.time()
    t = ticker.upper().strip()
    errors: List[str] = []

    if looks_crypto(t):
        chain: List[Tuple[str, Callable[[str], Tuple[float, str]]]] = [
            ("okx", _quote_okx),
            ("binance", _quote_binance),
            ("bybit", _quote_bybit),
            ("coinbase", _quote_coinbase),
            ("coingecko", _quote_coingecko),
            ("yahoo_chart", _quote_yahoo_chart),
            ("yfinance", _quote_yfinance),
        ]
        if prefer:
            chain.sort(key=lambda nf: nf[0] != prefer)   # stable: the rest keep their order
    else:
        chain = [
            ("yahoo_chart", _quote_yahoo_chart),
            ("stooq", _quote_stooq),
            ("eodhd_demo", _quote_eodhd_demo),
            ("yfinance", _quote_yfinance),
        ]

    for name, fn in chain:
        if not _available(name, t):
            continue
        try:
            px, provider = fn(t)
            _bump(name, True)
            return {
                "ticker": t,
                "price": px,
                "provider": provider,
                "state": "live",
                "latency_ms": round((time.time() - t0) * 1000, 1),
                "error": None,
            }
        except NotListed as e:
            _miss(name, t)
            errors.append(f"{name}:{e}")
        except Exception as e:
            _bump(name, False)
            errors.append(f"{name}:{e}")

    return {
        "ticker": t,
        "price": None,
        "provider": None,
        "state": "error",
        "latency_ms": round((time.time() - t0) * 1000, 1),
        "error": " | ".join(errors)[:400],
    }


def fetch_intraday(ticker: str, interval: str = "5m", history_days: Optional[int] = None) -> Dict[str, Any]:
    """
    OHLCV bars via free rotation. Crypto: paged venue history (crypto_history) from ET midnight
    `history_days` days ago (default CRYPTO_HISTORY_DAYS), then Yahoo chart over the same window.
    Equities: Yahoo chart.
    """
    t0 = time.time()
    t = ticker.upper().strip()
    errors: List[str] = []
    start_s: Optional[float] = None

    if looks_crypto(t):
        days = int(history_days or CRYPTO_HISTORY_DAYS)
        start_s = history_start_ms(days) / 1000.0
        got = crypto_history(t, interval, days, errors) if interval in _BAR_SEC else None
        if got:
            return {
                "ticker": t,
                "provider": got["venue"],
                "interval": interval,
                "bars": _bars_from_rows(got["rows"]),
                "state": got["state"],
                "latency_ms": round((time.time() - t0) * 1000, 1),
                "error": None,
            }

    range_ = bars_range_for_interval(interval)

    # equity + crypto fallback: Yahoo chart REST
    for name, fn, kwargs in (
        ("yahoo_chart", _yahoo_chart, {"interval": interval, "range_": range_, "start_s": start_s}),
    ):
        if not _available(name, t):
            continue
        try:
            df, provider = fn(t, **kwargs)
            _bump(name, True)
            return {
                "ticker": t,
                "provider": provider,
                "interval": interval,
                "bars": df,
                "state": "live",
                "latency_ms": round((time.time() - t0) * 1000, 1),
                "error": None,
            }
        except NotListed as e:
            _miss(name, t)
            errors.append(f"{name}:{e}")
        except Exception as e:
            _bump(name, False)
            errors.append(f"{name}:{e}")

    # last resort yfinance single (start/end so 7d is not silently collapsed to 5d)
    if _available("yfinance", t):
        try:
            lookback = RANGE_LOOKBACK_DAYS.get(range_, RANGE_LOOKBACK_DAYS[DEFAULT_BARS_RANGE])
            if (interval or "").lower() in ("1m", "1min", "2m"):
                lookback = min(lookback, YAHOO_1M_MAX_DAYS)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=lookback) if start_s is None else datetime.fromtimestamp(start_s, timezone.utc)
            hist = _yf_history(to_yahoo_symbol(t), start=start, end=end, interval=interval, prepost=True)
            if hist is None or hist.empty:
                # named-period fallback (5d is the longest named period safe for 1m)
                period = range_ if range_ in ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y") else "5d"
                if (interval or "").lower() in ("1m", "1min", "2m") and period not in ("1d", "5d"):
                    period = "5d"
                hist = _yf_history(to_yahoo_symbol(t), period=period, interval=interval, prepost=True)
            if hist is not None and not hist.empty:
                df = hist.rename(columns=str.title) if "Close" not in hist.columns else hist
                need = ["Open", "High", "Low", "Close", "Volume"]
                for c in need:
                    if c not in df.columns:
                        raise RuntimeError("cols")
                df = df[need]
                if start_s is not None:     # crypto: the history window, as from the venues
                    df = df[df.index >= pd.Timestamp(start_s, unit="s", tz="UTC")]
                _bump("yfinance", True)
                return {
                    "ticker": t,
                    "provider": "yfinance",
                    "interval": interval,
                    "bars": df,
                    "state": "live",
                    "latency_ms": round((time.time() - t0) * 1000, 1),
                    "error": None,
                }
            raise NotListed("no prices")
        except NotListed as e:
            _miss("yfinance", t)
            errors.append(f"yfinance:{e}")
        except Exception as e:
            _bump("yfinance", False)
            errors.append(f"yfinance:{e}")

    return {
        "ticker": t,
        "provider": None,
        "interval": interval,
        "bars": pd.DataFrame(),
        "state": "error",
        "latency_ms": round((time.time() - t0) * 1000, 1),
        "error": " | ".join(errors)[:400],
    }


def fetch_daily(ticker: str) -> Dict[str, Any]:
    t0 = time.time()
    t = ticker.upper().strip()
    errors: List[str] = []

    if looks_crypto(t):
        for name, fn in (
            ("okx", lambda: _crypto_okx(t, "1d", 400)),
            ("binance", lambda: _crypto_binance(t, "1d", 500)),
            ("bybit", lambda: _crypto_bybit(t, "1d", 400)),
            ("coinbase", lambda: _crypto_coinbase(t, "1d")),
        ):
            if not _available(name, t):
                continue
            try:
                df, provider = fn()
                # Coinbase still serves a delisted product's last candles (MKR-USD: a year old)
                if time.time() - df.index[-1].timestamp() > 3 * 86400:
                    raise NotListed(f"last daily candle {df.index[-1]:%Y-%m-%d}")
                _bump(name, True)
                return {
                    "ticker": t, "provider": provider, "bars": df, "state": "live",
                    "latency_ms": round((time.time() - t0) * 1000, 1), "error": None,
                }
            except NotListed as e:
                _miss(name, t)
                errors.append(f"{name}:{e}")
            except Exception as e:
                _bump(name, False)
                errors.append(f"{name}:{e}")

    if _available("yahoo_chart", t):
        try:
            df, provider = _yahoo_daily(t, "2y")
            _bump("yahoo_chart", True)
            return {
                "ticker": t, "provider": provider, "bars": df, "state": "live",
                "latency_ms": round((time.time() - t0) * 1000, 1), "error": None,
            }
        except NotListed as e:
            _miss("yahoo_chart", t)
            errors.append(f"yahoo_chart:{e}")
        except Exception as e:
            _bump("yahoo_chart", False)
            errors.append(f"yahoo_chart:{e}")

    if _available("yfinance", t):
        try:
            hist = _yf_history(to_yahoo_symbol(t), period="2y", interval="1d")
            if hist is None or hist.empty:
                raise NotListed("no prices")
            df = hist[["Open", "High", "Low", "Close", "Volume"]].copy()
            _bump("yfinance", True)
            return {
                "ticker": t, "provider": "yfinance", "bars": df, "state": "live",
                "latency_ms": round((time.time() - t0) * 1000, 1), "error": None,
            }
        except NotListed as e:
            _miss("yfinance", t)
            errors.append(f"yfinance:{e}")
        except Exception as e:
            _bump("yfinance", False)
            errors.append(f"yfinance:{e}")

    return {
        "ticker": t, "provider": None, "bars": pd.DataFrame(), "state": "error",
        "latency_ms": round((time.time() - t0) * 1000, 1),
        "error": " | ".join(errors)[:400],
    }


def batch_rotate_fetch(
    tickers: List[str],
    bars_interval: str = "5m",
    max_workers: int = 8,
    history_days: Optional[int] = None,
    with_daily: bool = True,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame], Dict[str, str], Dict[str, float], Dict[str, Any]]:
    """
    Parallel per-ticker free rotation.
    Returns (intraday, daily, bar_provider, live_price, quote_provider_meta)
    history_days: whole prior ET days of crypto bars (default CRYPTO_HISTORY_DAYS).
    with_daily=False skips the daily bars (one request a name; the engine does not read them).
    """
    tickers = [t.upper().strip() for t in tickers if t.strip()]
    tickers = list(dict.fromkeys(tickers))
    bars: Dict[str, pd.DataFrame] = {}
    daily: Dict[str, pd.DataFrame] = {}
    bar_prov: Dict[str, str] = {}
    live: Dict[str, float] = {}
    quote_meta: Dict[str, Any] = {}

    def one(t: str):
        bi = fetch_intraday(t, bars_interval, history_days)
        dy = fetch_daily(t) if with_daily else {"bars": pd.DataFrame()}
        q = fetch_quote(t, prefer=bi.get("provider"))
        return t, bi, dy, q

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(tickers)))) as ex:
        futs = [ex.submit(one, t) for t in tickers]
        for fut in as_completed(futs):
            try:
                t, bi, dy, q = fut.result()
            except Exception as e:
                log.warning("batch rotate fail: %s", e)
                continue
            if bi.get("bars") is not None and not bi["bars"].empty:
                bars[t] = bi["bars"]
                bar_prov[t] = bi.get("provider") or "—"
            if dy.get("bars") is not None and not dy["bars"].empty:
                daily[t] = dy["bars"]
                if t not in bar_prov:
                    bar_prov[t] = dy.get("provider") or "—"
            if q.get("price"):
                live[t] = float(q["price"])
                quote_meta[t] = {
                    "provider": q.get("provider"),
                    "latency_ms": q.get("latency_ms"),
                    "state": q.get("state"),
                }
            elif bi.get("bars") is not None and not bi["bars"].empty:
                # fall back to last bar close — not fabricated, still real bar
                live[t] = float(bi["bars"]["Close"].iloc[-1])
                quote_meta[t] = {
                    "provider": (bi.get("provider") or "bar_close") + "/bar",
                    "latency_ms": bi.get("latency_ms"),
                    "state": "bar",
                }

    log.info(
        "rotate batch %d tickers bars=%d daily=%d quotes=%d in %.2fs",
        len(tickers), len(bars), len(daily), len(live), time.time() - t0,
    )
    return bars, daily, bar_prov, live, quote_meta
