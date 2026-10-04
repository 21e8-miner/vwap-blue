#!/usr/bin/env python3
"""
Session replay of the crypto names in universe.txt.

replay_sessions.main() replays the first --max-tickers names of universe.txt, and those are all
equities (the crypto block sits at the end), so no replay before v1.5.0 said anything about crypto.
This runs the same machinery on the crypto names: replay_sessions.replay, decision-time grade >= A,
next-bar-open fills + 2 bps, held to the session's last bar, R net of the 0.15% crypto round trip,
standard errors clustered by session (honest.py).

Bars (--source yahoo, the default). data.batch_fetch(mode="hybrid") pages crypto from an exchange API
(OKX, Binance, Bybit, Coinbase in turn), 8 prior ET days unless asked for more. For the fetch the
venues are held in cooldown, so batch_fetch takes its own Yahoo chart fallback (5m, the last 30 ET
days), the feed the GitHub Pages demo grades crypto on. Yahoo prints zero volume on about half of its
5m crypto bars, so the VWAPs are built from the rest. --source coinbase pages through Coinbase's 5m
candles instead (every bar has its volume): a data-source cross-check.

Only complete sessions are replayed. A 24/7 market's current session is still open, so bars after
--through (default: the day before the fetch, ET) are dropped. Fetched bars can be cached
(--save-bars) and replayed again with another engine file (--engine, --paired-engine), so two engine
versions are compared on identical bars. --utc-days relabels every bar by its ET offset, so the
engine's ET calendar day becomes the UTC day: a sensitivity check on where crypto's day starts
(windows without a DST switch only).

Check what a symbol fetched. Under the plain ARB-USD, TON-USD and JUP-USD Yahoo lists other tokens
(ARbit, TON Token, a second Jupiter); fetches now ask for providers.YAHOO_CRYPTO_SYMBOLS instead, but
bars cached before that hold the wrong ones. --exclude drops such names.

  python3 replay_crypto.py --save-bars data/backtests/crypto_bars.pkl
  git show <commit>:engine.py > /tmp/engine_old.py
  python3 replay_crypto.py --bars data/backtests/crypto_bars.pkl --paired-engine /tmp/engine_old.py
  python3 replay_crypto.py --bars data/backtests/crypto_bars.pkl --utc-days --out /tmp/utc.json
  python3 replay_crypto.py --bars <a cache from before the symbol map> --exclude ARB-USD,TON-USD,JUP-USD

Research only. Free delayed data. Not trade advice.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pickle
import time
import urllib.error
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import providers
import replay_sessions as rs
from data import batch_fetch, load_universe
from honest import COST

HERE = Path(__file__).resolve().parent
CRYPTO_VENUES = ("okx", "binance", "bybit", "coinbase")
PAIRED_MODELS = ("classic", "time_24", "partial_trail")
COINBASE = "https://api.exchange.coinbase.com"


def crypto_universe() -> List[str]:
    return [t for t in load_universe() if providers.looks_crypto(t)]


def fetch_yahoo_month(tickers: List[str]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """
    batch_fetch one name at a time with the crypto venues in cooldown, so crypto takes the Yahoo chart
    path (5m, the last 30 ET days). One name at a time because a failed fetch puts the shared Yahoo and
    yfinance providers in a 45 s cooldown, which in one batch starves every name after it.
    """
    bars: Dict[str, Any] = {}
    bar_prov: Dict[str, str] = {}
    with providers._lock:
        saved = dict(providers._cooldown_until)
    try:
        for t in tickers:
            with providers._lock:
                providers._cooldown_until.clear()
                for v in CRYPTO_VENUES:
                    providers._cooldown_until[v] = time.time() + 3600
            b, _daily, p, _live, _q = batch_fetch([t], force=True, mode="hybrid", bars_interval="5m", crypto_days=30)
            if t in b and b[t] is not None and len(b[t]):
                bars[t], bar_prov[t] = b[t], p.get(t, "?")
    finally:
        with providers._lock:
            providers._cooldown_until.clear()
            providers._cooldown_until.update(saved)
    return bars, bar_prov


def _coinbase_json(url: str) -> Any:
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": providers.UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            time.sleep(1.5 * (attempt + 1))          # 429 and 5xx: back off and retry
        except (urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(1.0)
    return None


def fetch_coinbase_month(tickers: List[str], days: int = 30) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Coinbase 5m candles for the `days` before today's ET midnight (300 per request, public endpoint)."""
    listed = {p["id"] for p in (_coinbase_json(f"{COINBASE}/products") or []) if p.get("quote_currency") == "USD"}
    end = datetime.now(rs.ET).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    bars: Dict[str, Any] = {}
    for t in tickers:
        if t not in listed:
            continue
        rows: Dict[int, Tuple[float, ...]] = {}
        s = start
        while s < end:
            e = min(s + timedelta(minutes=5 * 299), end)
            for k in _coinbase_json(f"{COINBASE}/products/{t}/candles?granularity=300"
                                    f"&start={s.isoformat()}&end={e.isoformat()}") or []:
                if start.timestamp() <= k[0] < end.timestamp():         # [time, low, high, open, close, volume]
                    rows[int(k[0])] = (float(k[3]), float(k[2]), float(k[1]), float(k[4]), float(k[5]))
            s = e
            time.sleep(0.15)                                            # public limit is 10 requests/s
        if rows:
            ts = sorted(rows)
            bars[t] = pd.DataFrame([rows[x] for x in ts], columns=["Open", "High", "Low", "Close", "Volume"],
                                   index=pd.to_datetime(ts, unit="s", utc=True).tz_convert(rs.ET))
    return bars, {t: "coinbase" for t in bars}


def as_utc_days(bars: Dict[str, Any]) -> Dict[str, Any]:
    """Relabel each bar by its ET offset (+4 h in EDT) so the engine's ET calendar day is the UTC day."""
    out = {}
    for t, df in bars.items():
        idx = rs._et_index(df)
        offsets = {ts.utcoffset() for ts in idx}
        if len(offsets) != 1:
            raise SystemExit(f"--utc-days: {t} spans a DST switch")
        out[t] = df.set_axis(idx - offsets.pop())
    return out


complete_sessions = rs.complete_sessions


def load_engine(path: Optional[str]) -> str:
    """Point the replay at an engine file (same API; default ./engine.py); returns its version."""
    if not path:
        import engine
        rs.ENGINE = engine
        return engine.ENGINE_VERSION
    spec = importlib.util.spec_from_file_location(f"engine_alt_{abs(hash(path))}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rs.ENGINE = mod
    return mod.ENGINE_VERSION


def _chunk(job: Tuple) -> Tuple[List[rs.Trade], Dict[str, int]]:
    engine_path, tickers, bars, bar_prov, grade_min, entry_mode, slip_bps = job
    load_engine(engine_path)
    skips: Dict[str, int] = {}
    trades = rs.replay(tickers, bars, {}, bar_prov, {}, {}, grade_min, rs.MODELS,
                       entry_mode=entry_mode, slip_bps=slip_bps, skips=skips)
    return trades, skips


def run(tickers: List[str], bars: Dict[str, Any], bar_prov: Dict[str, str], engine_path: Optional[str],
        grade_min: str, entry_mode: str, slip_bps: float, jobs: int) -> Tuple[str, List[rs.Trade], Dict[str, int]]:
    version = load_engine(engine_path)
    have = [t for t in tickers if t in bars]
    if jobs <= 1:
        trades, skips = _chunk((engine_path, have, bars, bar_prov, grade_min, entry_mode, slip_bps))
        return version, trades, skips
    parts = [p for p in (have[i::jobs] for i in range(jobs)) if p]
    work = [(engine_path, p, {t: bars[t] for t in p}, bar_prov, grade_min, entry_mode, slip_bps) for p in parts]
    by_ticker: Dict[str, List[rs.Trade]] = {}
    skips: Dict[str, int] = {}
    with ProcessPoolExecutor(max_workers=len(work)) as ex:
        for trades, sk in ex.map(_chunk, work):
            for tr in trades:
                by_ticker.setdefault(tr.ticker, []).append(tr)
            for k, v in sk.items():
                skips[k] = skips.get(k, 0) + v
    return version, [tr for t in have for tr in by_ticker.get(t, [])], skips


def summarize(trades: List[rs.Trade]) -> Dict[str, Dict[str, Any]]:
    by_model: Dict[str, List[rs.Trade]] = {}
    for tr in trades:
        by_model.setdefault(tr.model, []).append(tr)
    return {name: rs._pack(ts) for name, ts in by_model.items()}


def diff_clustered(a: List[rs.Trade], b: List[rs.Trade]) -> Dict[str, Any]:
    """mean(a.r_net) - mean(b.r_net), CR1 standard error over sessions (two means on shared sessions)."""
    if not a or not b:
        return {"diff": None, "se": None, "t": None}
    ma = sum(t.r_net for t in a) / len(a)
    mb = sum(t.r_net for t in b) / len(b)
    score: Dict[str, float] = {}
    for t in a:
        score[t.session] = score.get(t.session, 0.0) + (t.r_net - ma) / len(a)
    for t in b:
        score[t.session] = score.get(t.session, 0.0) - (t.r_net - mb) / len(b)
    g = len(score)
    se = (g / (g - 1) * sum(v * v for v in score.values())) ** 0.5 if g > 1 else None
    if se is not None and se < 1e-9:    # identical trade sets: the difference is exactly zero, not a t-stat
        return {"diff": 0.0, "se": 0.0, "t": None, "sessions": g}
    return {"diff": round(ma - mb, 4), "se": round(se, 4) if se else None,
            "t": round((ma - mb) / se, 2) if se else None, "sessions": g}


def trade_key(t: rs.Trade) -> Tuple:
    return (t.ticker, t.session, t.side, t.trigger_time, round(t.entry, 10))


def paired_block(trades: List[rs.Trade], alt: List[rs.Trade]) -> Dict[str, Any]:
    """The other engine on the same bars, in the shape research/replay_2026-10-04_v1.4.1.json uses."""
    alt_sum = summarize(alt)
    out: Dict[str, Any] = {}
    for name in PAIRED_MODELS:
        s = alt_sum.get(name) or {}
        if s.get("n"):
            out[name] = {"net": s["net_clustered"], "gross": s["gross_clustered"], "verdict": s["verdict"]}
    a = [t for t in trades if t.model == "classic"]
    b = [t for t in alt if t.model == "classic"]
    ka, kb = {trade_key(t) for t in a}, {trade_key(t) for t in b}
    out["classic_overlap"] = {"kept": len(ka & kb), "added": len(ka - kb), "dropped": len(kb - ka)}
    out["classic_net_difference"] = diff_clustered(a, b)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Session replay of the crypto names in universe.txt")
    ap.add_argument("--bars", help="replay cached bars (a --save-bars file) instead of fetching")
    ap.add_argument("--save-bars", help="write the fetched bars here (pickle) for paired re-runs")
    ap.add_argument("--source", choices=("yahoo", "coinbase"), default="yahoo", help="where to fetch bars")
    ap.add_argument("--through", help="last session to replay, YYYY-MM-DD (default: the day before the fetch, ET)")
    ap.add_argument("--utc-days", action="store_true", help="sensitivity: replay crypto's day as the UTC day")
    ap.add_argument("--engine", help="engine file to replay (default: ./engine.py)")
    ap.add_argument("--paired-engine", help="also replay this engine file on the same bars; stored under method")
    ap.add_argument("--grade-min", default="A")
    ap.add_argument("--entry", choices=("next_open", "trigger_close"), default="next_open")
    ap.add_argument("--slip-bps", type=float, default=2.0)
    ap.add_argument("--jobs", type=int, default=1, help="worker processes (split by ticker)")
    ap.add_argument("--out", help="output JSON (default: research/replay_crypto_<fetch date>_v<engine>.json)")
    ap.add_argument("--note", default="", help="free-text note stored in method.note")
    ap.add_argument("--exclude", default="", help="comma-separated names to leave out (e.g. a symbol that fetched another token)")
    args = ap.parse_args()
    exclude = sorted({t.strip().upper() for t in args.exclude.split(",") if t.strip()})

    if args.bars:
        cache = pickle.loads(Path(args.bars).read_bytes())
    else:
        tickers = crypto_universe()
        t0 = time.time()
        fetch = fetch_coinbase_month if args.source == "coinbase" else fetch_yahoo_month
        bars, bar_prov = fetch(tickers)
        cache = {"fetched_at": datetime.now(rs.ET).isoformat(timespec="seconds"), "source": args.source,
                 "tickers": tickers, "bars": bars, "bar_prov": bar_prov}
        print(f"  fetched {sum(1 for t in tickers if t in bars)}/{len(tickers)} in {time.time() - t0:.1f}s")
        if args.save_bars:
            Path(args.save_bars).parent.mkdir(parents=True, exist_ok=True)
            Path(args.save_bars).write_bytes(pickle.dumps(cache))
            print(f"  cached bars → {args.save_bars}")
    fetched_at = datetime.fromisoformat(cache["fetched_at"])
    source = cache.get("source", "yahoo")
    through = date.fromisoformat(args.through) if args.through else fetched_at.date() - timedelta(days=1)
    tickers = [t for t in cache["tickers"] if t not in exclude]
    bar_prov = cache["bar_prov"]
    bars = {t: df for t, df in cache["bars"].items() if t not in exclude}
    bars = complete_sessions(as_utc_days(bars) if args.utc_days else bars, through)

    version, trades, skips = run(tickers, bars, bar_prov, args.engine, args.grade_min, args.entry, args.slip_bps, args.jobs)
    summaries = summarize(trades)
    print(f"\n  engine v{version} · {len(bars)} names · {source} bars · sessions through {through}"
          + (" · UTC days" if args.utc_days else ""))
    rs.print_honest(summaries)

    sessions = sorted({str(d) for df in bars.values() for d in rs._session_days(df)[1:]})
    fetch_meta = {}
    for t in tickers:
        df = cache["bars"].get(t)
        if df is None or df.empty:
            fetch_meta[t] = {"provider": None, "bars": 0}
            continue
        idx = rs._et_index(df)
        fetch_meta[t] = {"provider": bar_prov.get(t), "bars": int(len(df)), "first": idx[0].isoformat(),
                         "last": idx[-1].isoformat(), "zero_volume_share": round(float((df["Volume"] <= 0).mean()), 3)}
    method: Dict[str, Any] = {
        "bars": ("5m Yahoo chart, the last 30 ET days: data.batch_fetch(mode='hybrid', crypto_days=30) with "
                 "the crypto venues held in cooldown") if source == "yahoo"
                else "5m Coinbase candles, 30 days, paged through the public candles endpoint",
        "session_clock": "UTC day (bars relabelled by their ET offset)" if args.utc_days else "ET calendar day",
        "window": [sessions[0], sessions[-1]] if sessions else None,
        "sessions_replayed": len(sessions),
        "through": str(through),
        "entry": (f"next bar open + {args.slip_bps:.1f} bps slippage" if args.entry == "next_open"
                  else "engine trigger close (optimistic)"),
        "entry_mode": args.entry,
        "skipped_entries": skips,
        "hold": "focus session only (no overnight): exit at the session's last bar",
        "grade": f"decision-time ≥ {args.grade_min}",
        "same_bar": "stop before target; no same-bar entry fill",
        "cost": COST,
        "r_net": "r_multiple - round-trip cost / risk (per trade)",
        "standard_error": "clustered by session (honest.py)",
        "fetched_at": cache["fetched_at"],
        "fetch": fetch_meta,
    }
    if exclude:
        method["excluded"] = exclude
    if args.note:
        method["note"] = args.note
    if args.paired_engine:
        alt_version, alt_trades, alt_skips = run(tickers, bars, bar_prov, args.paired_engine, args.grade_min,
                                                 args.entry, args.slip_bps, args.jobs)
        key = "paired_v" + alt_version.replace(".", "_") + "_same_bars"
        method[key] = {**paired_block(trades, alt_trades), "skipped_entries": alt_skips}
        p = method[key]
        if "classic" in p:
            print(f"\n  paired v{alt_version} on the same bars: classic net "
                  f"{p['classic']['net']['mean']:+.3f} ± {p['classic']['net']['se']:.3f} ({p['classic']['verdict']})")
        print(f"  overlap {p['classic_overlap']} · difference {p['classic_net_difference']}")

    payload = {
        "asof": cache["fetched_at"],
        "run_at": datetime.now(rs.ET).isoformat(timespec="seconds"),
        "engine_version": version,
        "method": method,
        "tickers": [t for t in tickers if t in bars],
        "summaries": summaries,
        "trades": [asdict(t) for t in trades],
    }
    out = Path(args.out) if args.out else HERE / "research" / f"replay_crypto_{fetched_at:%Y-%m-%d}_v{version}.json"
    if out.exists():                    # never overwrite a snapshot (committed results live in research/)
        out = out.with_name(f"{out.stem}_{datetime.now(rs.ET):%H%M%S}{out.suffix}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n  Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
