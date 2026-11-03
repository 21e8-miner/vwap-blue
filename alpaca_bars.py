#!/usr/bin/env python3
"""
alpaca_bars.py — years of 5-minute US equity bars from Alpaca's Market Data API, in the replay's bars pickle layout.

Why: Yahoo serves ~32 days of 5m (60 via the chart API). Every VWAP Blue verdict so far rests on ~20 sessions and
the effects in the literature are 1–3% R², which needs hundreds. Alpaca's Basic (free) plan serves historical SIP
bars back to 2016 with pre-market from 04:00 ET, as long as `end` is at least 15 minutes old; 200 requests/min,
10,000 bars a page. Use the SIP feed: IEX is ~2.5% of consolidated volume and breaks RVOL and the $-volume floor.
Prices are requested raw (unadjusted): dividend-adjusted intraday prices distort gaps and $-volume.

Keys: env ALPACA_KEY_ID / ALPACA_SECRET_KEY, else ~/.config/active_projects_secrets/alpaca_key_id and
alpaca_secret_key (one line each, mode 0600). A paper-trading key pair works for market data. Never commit them.

    python3 alpaca_bars.py --tickers-from desk --start 2024-11-01 --out data/backtests/alpaca_5m_2y.pkl
    python3 alpaca_bars.py --tickers AAPL,MSFT,SPY --start 2026-01-01 --out /tmp/t.pkl

Output pickle: {"fetched_at", "tickers", "bars": {ticker: DataFrame[Open, High, Low, Close, Volume] indexed by UTC
timestamp}, "bar_prov": {ticker: "alpaca_sip_5m"}, "daily": {}, "live": {}, "qmeta": {...}} — what replay_sessions.py
--bars and moontower_deeper_check.session_frames read. Resumable: an existing --out is extended, not refetched.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

API = "https://data.alpaca.markets/v2/stocks/bars"
SECRETS = Path.home() / ".config" / "active_projects_secrets"
PAGE_LIMIT = 10000
RATE_PER_MIN = 190                      # Basic plan allows 200
SYMBOLS_PER_CALL = 50                   # multi-symbol calls share the 10k-bar page; 50 names × 1 day ≈ 10k 5m bars


def load_keys() -> Dict[str, str]:
    kid = os.environ.get("ALPACA_KEY_ID") or _read(SECRETS / "alpaca_key_id")
    sec = os.environ.get("ALPACA_SECRET_KEY") or _read(SECRETS / "alpaca_secret_key")
    if not kid or not sec:
        raise SystemExit("Alpaca keys missing: set ALPACA_KEY_ID / ALPACA_SECRET_KEY or write "
                         f"{SECRETS}/alpaca_key_id and alpaca_secret_key (0600).")
    return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec}


def _read(p: Path) -> Optional[str]:
    try:
        return p.read_text().strip() or None
    except OSError:
        return None


class Pacer:
    def __init__(self, per_min: int = RATE_PER_MIN):
        self.gap = 60.0 / per_min
        self.last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now - self.last < self.gap:
            time.sleep(self.gap - (now - self.last))
        self.last = time.monotonic()


def http_get(url: str, headers: Dict[str, str], retries: int = 5) -> Dict[str, Any]:
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={**headers, "User-Agent": "vwap-blue/alpaca_bars"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            body = e.read().decode("utf-8", "replace")[:200]
            raise SystemExit(f"Alpaca {e.code}: {body}")
    raise SystemExit("Alpaca: too many retries")


def fetch_bars(symbols: List[str], start: datetime, end: datetime, headers: Dict[str, str],
               get: Callable[[str, Dict[str, str]], Dict[str, Any]] = http_get, pacer: Optional[Pacer] = None,
               timeframe: str = "5Min", feed: str = "sip") -> Dict[str, List[Dict[str, Any]]]:
    """All pages for these symbols over [start, end]; raw prices; returns symbol → list of Alpaca bar dicts."""
    out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols}
    token: Optional[str] = None
    while True:
        q = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start.isoformat().replace("+00:00", "Z"),
             "end": end.isoformat().replace("+00:00", "Z"), "limit": PAGE_LIMIT, "adjustment": "raw", "feed": feed, "sort": "asc"}
        if token:
            q["page_token"] = token
        if pacer:
            pacer.wait()
        d = get(f"{API}?{urllib.parse.urlencode(q)}", headers)
        for s, rows in (d.get("bars") or {}).items():
            out.setdefault(s, []).extend(rows)
        token = d.get("next_page_token")
        if not token:
            return out


def to_frame(rows: List[Dict[str, Any]]):
    """Alpaca bars → the replay's frame: UTC index, Open/High/Low/Close/Volume. Duplicates (page seams) dropped."""
    import pandas as pd
    if not rows:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame({"ts": [r["t"] for r in rows], "Open": [r["o"] for r in rows], "High": [r["h"] for r in rows],
                       "Low": [r["l"] for r in rows], "Close": [r["c"] for r in rows], "Volume": [r["v"] for r in rows]})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    df.index.name = None
    return df[["Open", "High", "Low", "Close", "Volume"]].astype(float)


def chunks(xs: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(xs), n):
        yield xs[i: i + n]


def month_spans(start: datetime, end: datetime) -> Iterable[tuple]:
    """Calendar-month windows: 50 names × a month of 5m bars with pre-market is a few pages each."""
    cur = start
    while cur < end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        yield cur, min(nxt, end)
        cur = nxt


def desk_tickers() -> List[str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from data import load_universe  # the desk's list
    return [t for t in load_universe() if not t.endswith("-USD")]


def pull(tickers: List[str], start: datetime, end: datetime, out: Path, headers: Dict[str, str],
         get: Callable[..., Dict[str, Any]] = http_get, log: Callable[[str], None] = print) -> Dict[str, Any]:
    import pandas as pd
    blob: Dict[str, Any] = {"fetched_at": None, "tickers": [], "bars": {}, "bar_prov": {}, "daily": {}, "live": {}, "qmeta": {}}
    if out.exists():
        with open(out, "rb") as fh:
            blob = pickle.load(fh)
        log(f"resuming {out}: {len(blob['bars'])} names held")
    pacer = Pacer()
    have = {t: (blob["bars"][t].index.max().to_pydatetime() if t in blob["bars"] and len(blob["bars"][t]) else None) for t in tickers}
    for group in chunks(tickers, SYMBOLS_PER_CALL):
        g_start = min((have[t] or start) for t in group)
        if g_start >= end:
            continue
        for a, b in month_spans(g_start, end):
            rows = fetch_bars(group, a, b, headers, get, pacer)
            for t in group:
                new = to_frame(rows.get(t, []))
                old = blob["bars"].get(t)
                blob["bars"][t] = new if old is None or old.empty else pd.concat([old, new[~new.index.isin(old.index)]]).sort_index()
                blob["bar_prov"][t] = "alpaca_sip_5m"
            log(f"{group[0]}…{group[-1]} {a.date()}→{b.date()}: " + ", ".join(f"{t} {len(rows.get(t, []))}" for t in group[:3]) + " …")
            blob["fetched_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            blob["tickers"] = sorted(blob["bars"])
            blob["qmeta"] = {"source": "alpaca", "feed": "sip", "timeframe": "5Min", "adjustment": "raw", "start": start.isoformat(), "end": end.isoformat()}
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "wb") as fh:                  # checkpoint after every window
                pickle.dump(blob, fh)
    return blob


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", help="comma-separated; or --tickers-from desk")
    ap.add_argument("--tickers-from", choices=["desk"])
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (ET midnight → UTC)")
    ap.add_argument("--end", help="YYYY-MM-DD, default: 20 minutes ago (free plan needs end ≥ 15 min old)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--max-tickers", type=int)
    a = ap.parse_args()
    tickers = desk_tickers() if a.tickers_from == "desk" else [s.strip().upper() for s in (a.tickers or "").split(",") if s.strip()]
    if not tickers:
        raise SystemExit("no tickers")
    tickers = tickers[: a.max_tickers or None]
    start = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc) + timedelta(hours=4)
    end = (datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc) + timedelta(days=1)) if a.end else datetime.now(timezone.utc) - timedelta(minutes=20)
    headers = load_keys()
    blob = pull(tickers, start, end, a.out, headers)
    n = sum(len(df) for df in blob["bars"].values())
    print(f"{len(blob['bars'])} names, {n:,} bars → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
