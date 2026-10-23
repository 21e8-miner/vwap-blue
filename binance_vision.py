"""
Binance's public bulk archive (data.binance.vision): spot klines as zipped CSVs, one file per month
and, for days whose month is not published yet, one per day, each with a SHA-256 .CHECKSUM. No key,
no rate limit to speak of, and reachable where api.binance.com answers 451. Years of 5m bars with
the venue's own volume (Yahoo prints zero on about half of its 5m crypto bars).

Files are cached under data/binance_vision/ (not in git) and checked against their checksum.
Kline columns: open time, open, high, low, close, base volume, close time, quote volume, trades, ...
Open times are milliseconds in older files and microseconds from 2025; both are read.
"""

from __future__ import annotations

import csv
import hashlib
import io
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

import providers

BASE = "https://data.binance.vision/data/spot"
CACHE = Path(__file__).resolve().parent / "data" / "binance_vision"


class ChecksumMismatch(RuntimeError):
    pass


def symbol(ticker: str) -> str:
    """BTC-USD → BTCUSDT (the USDT pair is Binance's deep book for every coin in the universe)."""
    return providers.to_usdt(ticker)


def _http_get(url: str, tries: int = 4) -> Optional[bytes]:
    """The body, or None for a file that does not exist (404). Other errors are retried, then raised."""
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": providers.UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if k + 1 == tries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if k + 1 == tries:
                raise
        time.sleep(1.5 * (k + 1))
    return None


def fetch_file(sym: str, interval: str, period: str, kind: str, get: Callable[[str], Optional[bytes]] = _http_get,
               cache: Optional[Path] = CACHE) -> Optional[bytes]:
    """
    One archive zip (kind "monthly", period "2026-09"; or "daily", period "2026-10-21"), checksum-verified.
    None when Binance has no such file (the pair did not trade then, or the month is not published yet).
    """
    name = f"{sym}-{interval}-{period}.zip"
    path = cache / kind / sym / name if cache else None
    if path is not None and path.exists():
        return path.read_bytes()
    url = f"{BASE}/{kind}/klines/{sym}/{interval}/{name}"
    body = get(url)
    if body is None:
        return None
    sums = get(url + ".CHECKSUM")
    if sums is not None:
        want = sums.decode("utf-8", "replace").split()[0].lower()
        got = hashlib.sha256(body).hexdigest()
        if want != got:
            raise ChecksumMismatch(f"{name}: sha256 {got[:12]}… != published {want[:12]}…")
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return body


def parse_zip(body: bytes) -> pd.DataFrame:
    """A kline zip as an OHLCV frame on a UTC index (base-asset volume)."""
    rows: List[Tuple[int, float, float, float, float, float]] = []
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        for name in z.namelist():
            with z.open(name) as fh:
                for rec in csv.reader(io.TextIOWrapper(fh, "utf-8")):
                    if not rec or not rec[0].strip().isdigit():
                        continue                          # a header row, in the files that have one
                    rows.append((int(rec[0]), float(rec[1]), float(rec[2]), float(rec[3]), float(rec[4]), float(rec[5])))
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.DatetimeIndex([], tz="UTC"))
    ts = [r[0] for r in rows]
    unit = "us" if max(ts) > 10 ** 14 else "ms"
    return pd.DataFrame([r[1:] for r in rows], columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.to_datetime(ts, unit=unit, utc=True))


def _months(start: date, end: date) -> List[date]:
    out, m = [], date(start.year, start.month, 1)
    while m <= end:
        out.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return out


def klines(ticker: str, start: date, end: date, interval: str = "5m", today: Optional[date] = None,
           get: Callable[[str], Optional[bytes]] = _http_get, cache: Optional[Path] = CACHE) -> pd.DataFrame:
    """
    The pair's bars from `start` through `end` (UTC dates, inclusive): a month's file where Binance has
    published it, else that month's daily files up to `end` (and never today, which is incomplete).
    """
    sym = symbol(ticker)
    today = today or date.today()
    end = min(end, today - timedelta(days=1))
    frames = []
    for m in _months(start, end):
        body = fetch_file(sym, interval, f"{m:%Y-%m}", "monthly", get, cache)
        if body is not None:
            frames.append(parse_zip(body))
            continue
        d = max(m, start)
        last = min(end, date(m.year + (m.month == 12), m.month % 12 + 1, 1) - timedelta(days=1))
        while d <= last:
            day = fetch_file(sym, interval, f"{d:%Y-%m-%d}", "daily", get, cache)
            if day is not None:
                frames.append(parse_zip(day))
            d += timedelta(days=1)
    if not frames:
        return parse_zip(b"PK\x05\x06" + b"\x00" * 18)     # an empty zip: an empty frame
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end + timedelta(days=1), tz="UTC")
    return df[(df.index >= lo) & (df.index < hi)]


def history(tickers: List[str], days: int, today: Optional[date] = None, interval: str = "5m",
            get: Callable[[str], Optional[bytes]] = _http_get,
            cache: Optional[Path] = CACHE, retry_passes: int = 2, pause_s: float = 30.0,
            sleep: Callable[[float], None] = time.sleep) -> Tuple[Dict[str, pd.DataFrame], Dict[str, str]]:
    """
    `days` complete UTC days ending yesterday, per name: (bars, failed) with the reason a name has none.
    Names lost to a network error get `retry_passes` more tries after `pause_s` (files already cached are kept).
    """
    today = today or date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    bars: Dict[str, pd.DataFrame] = {}
    failed: Dict[str, str] = {}
    todo = list(tickers)
    for attempt in range(1 + retry_passes):
        if attempt:
            sleep(pause_s)                    # a dropped connection or DNS outage: give it time, then retry
        dropped: List[str] = []
        for t in todo:
            try:
                df = klines(t, start, end, interval, today, get, cache)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                failed[t] = f"{type(e).__name__}: {e}"
                dropped.append(t)
                continue
            except Exception as e:  # noqa: BLE001 — one bad file must not end the fetch
                failed[t] = f"{type(e).__name__}: {e}"
                continue
            failed.pop(t, None)
            if df.empty:
                failed[t] = f"no {symbol(t)} files on data.binance.vision"
            else:
                bars[t] = df
        todo = dropped
        if not todo:
            break
    return bars, failed
