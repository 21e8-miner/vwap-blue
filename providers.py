"""
Free multi-provider rotation for VWAP One — no API keys.

Ported/simplified from:
  - flow_well_stack live_feed (OKX · Binance · Bybit · Coinbase)
  - vector free-rotate-adapter (crypto rotate + Yahoo chart)
  - vwap_scanner multi_provider_data (Stooq · CoinGecko · EODHD demo · Yahoo)

Never fabricates prices. Failed providers cool down and rotate.
"""

from __future__ import annotations

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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

log = logging.getLogger("vwap_one.providers")

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


def _available(name: str) -> bool:
    with _lock:
        return time.time() >= _cooldown_until.get(name, 0)


def provider_status() -> Dict[str, Any]:
    with _lock:
        now = time.time()
        return {
            "cooldown_sec": COOLDOWN_SEC,
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


def _get_json(url: str, timeout: float = TIMEOUT) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


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


def to_okx_inst(ticker: str) -> str:
    s = to_usdt(ticker)
    if s.endswith("USDT"):
        return f"{s[:-4]}-USDT"
    return s


def to_yahoo_symbol(ticker: str) -> str:
    t = ticker.upper()
    if looks_crypto(t):
        # Yahoo prefers BTC-USD
        s = to_usdt(t)
        if s.endswith("USDT"):
            return s[:-4] + "-USD"
        return t if "-" in t else f"{t}-USD"
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


def _crypto_okx(symbol: str, interval: str, limit: int = 200) -> Tuple[pd.DataFrame, str]:
    bar = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "1d": "1D"}.get(interval, "5m")
    inst = to_okx_inst(symbol)
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar={bar}&limit={limit}"
    data = _get_json(url)
    lst = data.get("data") or []
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
    data = _get_json(url)
    lst = data.get("result", {}).get("list") or []
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


_CRYPTO_BAR_FNS: List[Tuple[str, Callable[..., Tuple[pd.DataFrame, str]]]] = [
    ("okx", lambda s, iv: _crypto_okx(s, iv)),
    ("binance", lambda s, iv: _crypto_binance(s, iv)),
    ("bybit", lambda s, iv: _crypto_bybit(s, iv)),
    ("coinbase", lambda s, iv: _crypto_coinbase(s, iv)),
]


# ── equity / general: Yahoo chart REST (no key) ─────────────────────────────

def _yahoo_chart(symbol: str, interval: str = "5m", range_: str = DEFAULT_BARS_RANGE) -> Tuple[pd.DataFrame, str]:
    sym = to_yahoo_symbol(symbol)
    # includePrePost: premarket + after-hours for equities (ignored for 1d)
    q = urllib.parse.urlencode({
        "interval": interval,
        "range": range_,
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
    data = _get_json(url)
    lst = data.get("data") or []
    if not lst:
        raise RuntimeError("okx empty")
    px = float(lst[0]["last"])
    if px <= 0:
        raise RuntimeError("okx px")
    return px, "okx"


def _quote_bybit(ticker: str) -> Tuple[float, str]:
    s = to_usdt(ticker)
    url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={s}"
    data = _get_json(url)
    lst = (data.get("result") or {}).get("list") or []
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
        raise RuntimeError("coingecko unmapped")
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
        raise RuntimeError("stooq n/a crypto")
    url = f"https://stooq.com/q/l/?s={s}&f=sd2t2ohlcv&h&e=csv"
    text = _get_text(url)
    # Symbol,Date,Time,Open,High,Low,Close,Volume
    reader = csv.DictReader(io.StringIO(text))
    row = next(reader, None)
    if not row:
        raise RuntimeError("stooq empty")
    close = row.get("Close") or row.get("close")
    if not close or close == "N/D":
        raise RuntimeError("stooq N/D")
    px = float(close)
    if px <= 0:
        raise RuntimeError("stooq px")
    return px, "stooq"


def _quote_eodhd_demo(ticker: str) -> Tuple[float, str]:
    demo = {"AAPL", "MSFT", "TSLA", "BTC-USD", "ETH-USD"}
    clean = ticker.upper()
    ysym = to_yahoo_symbol(clean)
    if clean not in demo and ysym not in demo:
        raise RuntimeError("eodhd demo only")
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


def _quote_yfinance(ticker: str) -> Tuple[float, str]:
    import yfinance as yf
    t = yf.Ticker(to_yahoo_symbol(ticker))
    info = getattr(t, "fast_info", None)
    if info is not None:
        px = float(getattr(info, "last_price", None) or 0)
        if px > 0:
            return px, "yfinance"
    hist = t.history(period="1d", interval="1m", prepost=True)
    if hist is not None and not hist.empty:
        return float(hist["Close"].iloc[-1]), "yfinance"
    raise RuntimeError("yfinance empty")


def fetch_quote(ticker: str) -> Dict[str, Any]:
    """Rotate free quote sources. Returns {price, provider, ...} or error."""
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
    else:
        chain = [
            ("yahoo_chart", _quote_yahoo_chart),
            ("stooq", _quote_stooq),
            ("eodhd_demo", _quote_eodhd_demo),
            ("yfinance", _quote_yfinance),
        ]

    for name, fn in chain:
        if not _available(name):
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


def fetch_intraday(ticker: str, interval: str = "5m") -> Dict[str, Any]:
    """OHLCV bars via free rotation. Crypto venues first, then Yahoo chart."""
    t0 = time.time()
    t = ticker.upper().strip()
    errors: List[str] = []

    if looks_crypto(t):
        for name, fn in _CRYPTO_BAR_FNS:
            if not _available(name):
                continue
            try:
                df, provider = fn(t, interval)
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
            except Exception as e:
                _bump(name, False)
                errors.append(f"{name}:{e}")

    range_ = bars_range_for_interval(interval)

    # equity + crypto fallback: Yahoo chart REST
    for name, fn, kwargs in (
        ("yahoo_chart", _yahoo_chart, {"interval": interval, "range_": range_}),
    ):
        if not _available(name):
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
        except Exception as e:
            _bump(name, False)
            errors.append(f"{name}:{e}")

    # last resort yfinance single (start/end so 7d is not silently collapsed to 5d)
    if _available("yfinance"):
        try:
            import yfinance as yf
            from datetime import datetime, timedelta, timezone

            lookback = RANGE_LOOKBACK_DAYS.get(range_, RANGE_LOOKBACK_DAYS[DEFAULT_BARS_RANGE])
            if (interval or "").lower() in ("1m", "1min", "2m"):
                lookback = min(lookback, YAHOO_1M_MAX_DAYS)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=lookback)
            hist = yf.Ticker(to_yahoo_symbol(t)).history(
                start=start, end=end, interval=interval, prepost=True
            )
            if hist is None or hist.empty:
                # named-period fallback (5d is the longest named period safe for 1m)
                period = range_ if range_ in ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y") else "5d"
                if (interval or "").lower() in ("1m", "1min", "2m") and period not in ("1d", "5d"):
                    period = "5d"
                hist = yf.Ticker(to_yahoo_symbol(t)).history(period=period, interval=interval, prepost=True)
            if hist is not None and not hist.empty:
                df = hist.rename(columns=str.title) if "Close" not in hist.columns else hist
                need = ["Open", "High", "Low", "Close", "Volume"]
                for c in need:
                    if c not in df.columns:
                        raise RuntimeError("cols")
                df = df[need]
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
            if not _available(name):
                continue
            try:
                df, provider = fn()
                _bump(name, True)
                return {
                    "ticker": t, "provider": provider, "bars": df, "state": "live",
                    "latency_ms": round((time.time() - t0) * 1000, 1), "error": None,
                }
            except Exception as e:
                _bump(name, False)
                errors.append(f"{name}:{e}")

    if _available("yahoo_chart"):
        try:
            df, provider = _yahoo_daily(t, "2y")
            _bump("yahoo_chart", True)
            return {
                "ticker": t, "provider": provider, "bars": df, "state": "live",
                "latency_ms": round((time.time() - t0) * 1000, 1), "error": None,
            }
        except Exception as e:
            _bump("yahoo_chart", False)
            errors.append(f"yahoo_chart:{e}")

    if _available("yfinance"):
        try:
            import yfinance as yf
            hist = yf.Ticker(to_yahoo_symbol(t)).history(period="2y", interval="1d")
            if hist is not None and not hist.empty:
                df = hist[["Open", "High", "Low", "Close", "Volume"]].copy()
                _bump("yfinance", True)
                return {
                    "ticker": t, "provider": "yfinance", "bars": df, "state": "live",
                    "latency_ms": round((time.time() - t0) * 1000, 1), "error": None,
                }
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
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame], Dict[str, str], Dict[str, float], Dict[str, Any]]:
    """
    Parallel per-ticker free rotation.
    Returns (intraday, daily, bar_provider, live_price, quote_provider_meta)
    """
    tickers = [t.upper().strip() for t in tickers if t.strip()]
    tickers = list(dict.fromkeys(tickers))
    bars: Dict[str, pd.DataFrame] = {}
    daily: Dict[str, pd.DataFrame] = {}
    bar_prov: Dict[str, str] = {}
    live: Dict[str, float] = {}
    quote_meta: Dict[str, Any] = {}

    def one(t: str):
        bi = fetch_intraday(t, bars_interval)
        dy = fetch_daily(t)
        q = fetch_quote(t)
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
