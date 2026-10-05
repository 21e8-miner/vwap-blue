"""VWAP Blue — Blueline dual-VWAP engine in the VWAP One desk layout. Port 8791."""

from __future__ import annotations

from datetime import datetime
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from data import (
    CRYPTO_DVOL_SHARE,
    batch_fetch,
    get_provider_status,
    load_universe,
    min_dollar_volume_for,
    passes_volume_filter,
    rotation_score,
)
import ledger
from engine import (
    DEFAULT_MAX_BAR_AGE_MIN, ENGINE_VERSION, analyze, apply_stale_guard, bar_age_min, build_chart_from_row,
)
from providers import fetch_quote

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vwap_blue")

ROOT = Path(__file__).resolve().parent
APP_VERSION = f"{ENGINE_VERSION}-blue"
app = FastAPI(title="VWAP Blue", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

# Scan ~10× more names than the old 48-symbol pool, then volume-filter + rank to max_n.
POOL_MULT = max(1, int(os.environ.get("VWAP_BLUE_POOL_MULT", "10")))
# Default $vol floor (equity: today so far or the prior session, the larger); crypto gets a quarter of it on
# 24h volume (data.session_dollar_volume, data.min_dollar_volume_for).
# Set VWAP_BLUE_MIN_DVOL=0 to disable.
_DEFAULT_MIN_DVOL = float(os.environ.get("VWAP_BLUE_MIN_DVOL", "2000000"))
# Live triggers on bars older than this (minutes, market open) are demoted to WATCH.
MAX_BAR_AGE_MIN = float(os.environ.get("VWAP_BLUE_MAX_BAR_AGE_MIN", str(DEFAULT_MAX_BAR_AGE_MIN)))
# Forward ledger: record every live grade-A trigger (data/signals/). VWAP_BLUE_LEDGER=0 disables.
LEDGER_ON = os.environ.get("VWAP_BLUE_LEDGER", "1") != "0"

_last: Dict[str, Any] = {"ts": 0.0, "rows": [], "meta": {}, "by_ticker": {}}
_live_lock = threading.Lock()
_live_cfg: Dict[str, Any] = {
    "enabled": False,
    "interval_sec": 45,
    "max": 16,
    "tickers": None,
    "actionable_only": False,
    "mode": "hybrid",
    "grade_min": "A",  # desk default: Grade A floor (was C)
    "min_dvol": _DEFAULT_MIN_DVOL,
}
_live_thread: Optional[threading.Thread] = None
# While live is on, the loop is the only scanner: /api/live sets this to make it rescan now with the
# new settings (or stop) instead of scanning alongside it.
_live_wake = threading.Event()

GRADE_RANK = {"A": 5, "LA": 4, "B": 3, "LB": 2, "C": 1, "✕": 0, "–": -1}

# Optional VWAP One for cross-scanner conflict demotion.
# Load via importlib so we never put another package ahead of this app on sys.path
# (uvicorn "app:app" would otherwise import vwap_simple_scanner/app.py).
def _load_one_analyze():
    try:
        import importlib.util
        import sys as _sys

        scan_path = ROOT.parent / "vwap_simple_scanner" / "scanner.py"
        if not scan_path.is_file():
            return None
        # Ensure sibling modules (data, one_engine, …) resolve without hijacking "app"
        one_dir = str(scan_path.parent)
        if one_dir not in _sys.path:
            _sys.path.append(one_dir)  # append, never insert(0)
        spec = importlib.util.spec_from_file_location("vwap_one_scanner_mod", scan_path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "analyze", None)
    except Exception as e:
        log.warning("VWAP One analyze unavailable for conflict checks: %s", e)
        return None


one_analyze = _load_one_analyze()


class ScanBody(BaseModel):
    tickers: Optional[List[str]] = None
    max: int = Field(default=16, ge=1, le=200)
    actionable_only: bool = False
    force: bool = False
    mode: str = Field(default="hybrid")
    grade_min: str = Field(default="A")
    min_dvol: Optional[float] = Field(
        default=None,
        description="Equity $ volume floor (0=off), on the larger of today so far and the prior session; "
                    "crypto gets a quarter of it on 24h volume. Default $2M / $0.5M.",
    )


class LiveBody(BaseModel):
    enabled: bool = True
    interval_sec: int = Field(default=45, ge=15, le=300)
    max: int = Field(default=16, ge=1, le=200)
    tickers: Optional[List[str]] = None
    actionable_only: bool = False
    mode: str = "hybrid"
    grade_min: str = "A"
    min_dvol: Optional[float] = None


def _grade_ok(grade: str, floor: str) -> bool:
    g = grade or "–"
    f = (floor or "C").upper()
    # A floor also accepts late A (LA)
    if f == "A" and g in ("A", "LA"):
        return True
    if f == "B" and g in ("A", "LA", "B", "LB"):
        return True
    return GRADE_RANK.get(g, -1) >= GRADE_RANK.get(f, 1)


def _apply_one_conflict(
    row: Dict[str, Any],
    bars: Dict[str, Any],
    daily: Dict[str, Any],
    live: Dict[str, float],
    bar_prov: Dict[str, str],
    quote_meta: Dict[str, Any],
) -> None:
    """Demote Blue when VWAP One disagrees on side with a live trigger/setup."""
    if one_analyze is None:
        return
    t = row.get("ticker")
    blue_side = (row.get("side") or "").lower()
    if not t or blue_side not in ("long", "short"):
        return
    if row.get("signal") not in ("TRIGGER", "SETUP", "TAGGED"):
        return
    try:
        qm = quote_meta.get(t) or {}
        one = one_analyze(
            t,
            bars.get(t),
            daily.get(t),
            live_price=live.get(t),
            bar_provider=bar_prov.get(t),
            quote_provider=qm.get("provider"),
        )
    except Exception:
        return
    if not isinstance(one, dict):
        return
    one_side = (one.get("side") or "").lower()
    one_sig = str(one.get("signal") or "")
    if one_side not in ("long", "short"):
        return
    if one_side == blue_side:
        row["one_agree"] = True
        row["edge"] = min(100, int(row.get("edge") or 0) + 6)
        return
    # Opposite side with a real One badge → conflict
    if any(k in one_sig.upper() for k in ("TRIGGER", "SETUP", "TAGGED")):
        row["conflict"] = True
        row["one_side"] = one_side
        row["one_signal"] = one_sig
        row["edge"] = max(0, int(row.get("edge") or 0) - 22)
        # never live-actionable under conflict
        row["live_actionable"] = False
        if row.get("grade") in ("A", "LA"):
            row["grade"] = "B" if row["grade"] == "A" else "LB"
            row["note"] = (row.get("note") or "") + " · ONE CONFLICT"
        if row.get("signal") == "TRIGGER":
            row["signal"] = "WATCH"


def _forward_brief() -> Optional[Dict[str, Any]]:
    """Compact forward-ledger record for the scan meta (cheap: two small JSONL files)."""
    if not LEDGER_ON:
        return None
    try:
        rep = ledger.report()
    except Exception:
        return None
    s = rep.get("net_r") or {}
    return {"signals": rep["signals"], "resolved": rep["resolved"], "pending": rep["pending"],
            "net_r": s.get("mean"), "se": s.get("se"), "sessions": s.get("clusters"), "verdict": rep["verdict"]}


def run_scan(
    tickers: Optional[List[str]] = None,
    max_n: int = 16,
    actionable_only: bool = False,
    force: bool = False,
    mode: str = "hybrid",
    grade_min: str = "A",
    min_dvol: Optional[float] = None,
) -> Dict[str, Any]:
    t0 = time.time()
    user_supplied = bool(tickers)
    full = load_universe()
    if not tickers:
        # 10× wider pool than the old max(48, max×3) path — full book when possible
        pool_n = min(len(full), max(48 * POOL_MULT, max_n * max(3, POOL_MULT)))
        tickers = full[:pool_n]
    else:
        # User list: honor order, allow wide paste (cap high so custom books work)
        tickers = [t.strip().upper() for t in tickers if t.strip()][: max(max_n * POOL_MULT, 80)]

    mode = (mode or "hybrid").lower()
    if mode not in ("rotate", "hybrid", "yfinance"):
        mode = "hybrid"

    if min_dvol is None:
        min_dvol = _live_cfg.get("min_dvol", _DEFAULT_MIN_DVOL)

    # daily bars only feed VWAP One's cross-check: without it they were half the scan's Yahoo requests
    bars, daily, bar_prov, live, quote_meta = batch_fetch(
        tickers, force=force, mode=mode, with_daily=one_analyze is not None,
    )

    # Liquidity gate: drop thin-tape names before full engine work
    liquid: List[str] = []
    dvol_map: Dict[str, float] = {}
    vol_dropped = 0
    for t in tickers:
        bdf = bars.get(t)
        ok, dvol = passes_volume_filter(t, bdf, min_dvol=min_dvol)
        dvol_map[t] = dvol
        if bdf is None:
            liquid.append(t)  # surface fetch errors in results
        elif ok:
            liquid.append(t)
        else:
            vol_dropped += 1

    # If the floor was too aggressive, fall back to top $vol so desk never goes empty
    if len([t for t in liquid if bars.get(t) is not None]) < max(8, max_n) and dvol_map:
        ranked = sorted(tickers, key=lambda t: dvol_map.get(t, 0.0), reverse=True)
        keep_n = min(len(ranked), max(max_n * 4, 64))
        liquid = ranked[:keep_n]
        vol_dropped = max(0, len(tickers) - len(liquid))
        log.info("volume floor relaxed → top-%s by $vol", keep_n)

    rows: List[Dict[str, Any]] = []
    by_ticker: Dict[str, Dict[str, Any]] = {}
    for t in liquid:
        qm = quote_meta.get(t) or {}
        row = analyze(
            t,
            bars.get(t),
            daily.get(t),
            live_price=live.get(t),
            bar_provider=bar_prov.get(t),
            quote_provider=qm.get("provider"),
            quote_latency_ms=qm.get("latency_ms"),
        )
        dvol = float(dvol_map.get(t, 0.0) or 0.0)
        row["dollar_vol"] = round(dvol, 0) if dvol else 0
        floor = min_dollar_volume_for(t, min_dvol) if min_dvol and min_dvol > 0 else 0.0
        row["illiquid"] = bool(floor > 0 and dvol > 0 and dvol < floor)
        apply_stale_guard(row, bar_age_min(bars.get(t)), MAX_BAR_AGE_MIN)
        _apply_one_conflict(row, bars, daily, live, bar_prov, quote_meta)
        by_ticker[t] = row
        # strip heavy chart blob from table payload (kept in by_ticker)
        slim = {k: v for k, v in row.items() if k != "_chart"}
        slim["_rot"] = rotation_score(slim)
        rows.append(slim)

    ledger_new = 0
    if LEDGER_ON:
        try:   # the ledger must never break a scan
            ledger_new = ledger.record(by_ticker, version=APP_VERSION)
        except Exception as e:
            log.warning("ledger record failed: %s", e)

    if actionable_only:
        rows = [r for r in rows if r.get("actionable") or r.get("live_actionable")]
    if grade_min and grade_min.upper() not in ("", "C", "–", "-"):
        gm = grade_min.upper()
        rows = [r for r in rows if _grade_ok(r.get("grade") or "–", gm)]

    # Rotation: gappers / high |dev| / RVOL / $vol / edge first, then hard-cap max_n
    rows.sort(
        key=lambda r: (
            r.get("_rot") or 0,
            r.get("edge") or 0,
            r.get("dollar_vol") or 0,
            GRADE_RANK.get(r.get("grade") or "–", -1),
        ),
        reverse=True,
    )
    rows = rows[:max_n]

    for r in rows:
        r.pop("_rot", None)

    provs = sorted({
        p for r in rows
        for p in (r.get("bar_provider"), r.get("quote_provider"), r.get("provider"))
        if p
    })
    regimes = {}
    for r in rows:
        rg = r.get("regime") or "unknown"
        regimes[rg] = regimes.get(rg, 0) + 1
    meta = {
        "count": len(rows),
        "actionable": sum(1 for r in rows if r.get("actionable")),
        "live_actionable": sum(1 for r in rows if r.get("live_actionable")),
        "setups": sum(1 for r in rows if r.get("signal") in ("SETUP", "TRIGGER", "TAGGED")),
        "grade_a": sum(1 for r in rows if (r.get("grade") or "").startswith("A") or (r.get("grade") or "").startswith("LA")),
        "conflicts": sum(1 for r in rows if r.get("conflict")),
        "stale": sum(1 for r in rows if r.get("stale_bars")),
        "max_bar_age_min": MAX_BAR_AGE_MIN,
        "ledger_new": ledger_new,
        "forward": _forward_brief(),
        "regime_counts": regimes,
        "grade_min": grade_min,
        "pool_scanned": len(tickers),
        "pool_liquid": len(liquid),
        "volume_dropped": vol_dropped,
        "min_dvol": min_dvol,
        "min_dvol_crypto": min_dvol * CRYPTO_DVOL_SHARE if min_dvol and min_dvol > 0 else 0,
        "pool_mult": POOL_MULT,
        "version": APP_VERSION,
        "mode": mode,
        "providers_used": provs,
        "elapsed_sec": round(time.time() - t0, 2),
        "asof": time.strftime("%Y-%m-%d %H:%M:%S"),
        "live": bool(_live_cfg.get("enabled")),
        "engine": "blueline dual VWAP · KER regime · VW-σ adaptive · 10× pool · $vol filter · One conflict",
    }
    _last["ts"] = time.time()
    _last["rows"] = rows
    _last["meta"] = meta
    _last["by_ticker"] = by_ticker
    log.info(
        "scan ok n=%s pool=%s liquid=%s drop_vol=%s A=%s conflicts=%s feeds=%s in %.1fs",
        meta["count"], meta["pool_scanned"], meta["pool_liquid"], vol_dropped,
        meta["grade_a"], meta["conflicts"], provs, meta["elapsed_sec"],
    )
    return {"results": rows, "meta": meta}


_ET = ZoneInfo("America/New_York")
_last_auto_resolve_session: Optional[str] = None
_last_auto_resolve_check: float = 0.0
_auto_resolve_retry_after: float = 0.0
_auto_resolve_gate = threading.Lock()     # startup and the live loop must not both enter
AUTO_RESOLVE_EVERY_S = 600                # routine retries for pending signals
AUTO_RESOLVE_MIN_GAP_S = 60               # never faster than this, even right after the close
AUTO_RESOLVE_BACKOFF_S = 300              # after a resolve raised


def _maybe_auto_resolve_ledger(now_dt: Optional[datetime] = None) -> Optional[Dict[str, int]]:
    """Resolve forward-ledger signals once a day after market close (16:15 ET), or on boot for past sessions.
    Throttled on the same clock as the session logic (now_dt), backs off after failures, and never runs twice at once
    (ledger.resolve is serialized too, for the API endpoint)."""
    global _last_auto_resolve_session, _last_auto_resolve_check, _auto_resolve_retry_after
    if not LEDGER_ON:
        return None
    if not _auto_resolve_gate.acquire(blocking=False):
        return None
    try:
        now_dt = now_dt or datetime.now(_ET)
        now_s = now_dt.timestamp()
        today_str = now_dt.strftime("%Y-%m-%d")
        is_post_close = (now_dt.hour, now_dt.minute) >= (16, 15) or now_dt.weekday() >= 5
        need_close_run = is_post_close and _last_auto_resolve_session != today_str
        elapsed = now_s - _last_auto_resolve_check
        if now_s < _auto_resolve_retry_after:
            return None
        if elapsed < AUTO_RESOLVE_MIN_GAP_S or (not need_close_run and elapsed < AUTO_RESOLVE_EVERY_S):
            return None
        _last_auto_resolve_check = now_s
        try:
            counts = ledger.resolve(now=now_dt)
        except Exception as e:
            _auto_resolve_retry_after = now_s + AUTO_RESOLVE_BACKOFF_S
            log.warning("ledger auto-resolve failed (retry in %ss): %s", AUTO_RESOLVE_BACKOFF_S, e)
            return None
        if is_post_close:
            _last_auto_resolve_session = today_str
        if counts.get("resolved") or counts.get("unresolvable") or counts.get("retry_later"):
            log.info("ledger auto-resolve: %s", counts)
        return counts
    finally:
        _auto_resolve_gate.release()


def _live_loop() -> None:
    log.info("live loop started")
    while True:
        _live_wake.clear()
        with _live_lock:
            if not _live_cfg["enabled"]:
                break
            interval = int(_live_cfg["interval_sec"])
            max_n = int(_live_cfg["max"])
            tickers = _live_cfg.get("tickers")
            actionable_only = bool(_live_cfg.get("actionable_only"))
            mode = str(_live_cfg.get("mode") or "hybrid")
            grade_min = str(_live_cfg.get("grade_min") or "A")
            min_dvol = _live_cfg.get("min_dvol", _DEFAULT_MIN_DVOL)
        try:
            run_scan(
                tickers, max_n, actionable_only, force=True,
                mode=mode, grade_min=grade_min, min_dvol=min_dvol,
            )
        except Exception as e:
            log.exception("live scan failed: %s", e)
        try:
            _maybe_auto_resolve_ledger()
        except Exception as e:
            log.warning("auto-resolve step failed: %s", e)
        _live_wake.wait(interval)
    log.info("live loop stopped")


def _ensure_live_thread() -> None:
    global _live_thread
    if _live_thread and _live_thread.is_alive():
        return
    _live_thread = threading.Thread(target=_live_loop, name="vwap-blue-live", daemon=True)
    _live_thread.start()


def _boot_live_party() -> None:
    if os.environ.get("VWAP_BLUE_LIVE", "1").strip() in ("0", "false", "no"):
        return
    interval = int(os.environ.get("VWAP_BLUE_LIVE_SEC", "45"))
    max_n = int(os.environ.get("VWAP_BLUE_LIVE_MAX", "16"))
    mode = os.environ.get("VWAP_BLUE_MODE", "hybrid")
    grade_min = os.environ.get("VWAP_BLUE_GRADE_MIN", "A").strip().upper() or "A"
    with _live_lock:
        _live_cfg["enabled"] = True
        _live_cfg["interval_sec"] = max(15, min(300, interval))
        _live_cfg["max"] = max(1, min(200, max_n))
        _live_cfg["mode"] = mode if mode in ("hybrid", "rotate", "yfinance") else "hybrid"
        _live_cfg["grade_min"] = grade_min
        _live_cfg["min_dvol"] = _DEFAULT_MIN_DVOL
    log.info(
        "party live ON interval=%ss mode=%s grade_min=%s pool_mult=%s min_dvol=%s",
        _live_cfg["interval_sec"], _live_cfg["mode"], grade_min, POOL_MULT, _DEFAULT_MIN_DVOL,
    )
    # the loop's first pass is the boot scan (and auto-resolve); a second one here ran alongside it
    _ensure_live_thread()


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_boot_live_party, name="vwap-blue-boot", daemon=True).start()


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "app": "vwap_blue",
        "version": APP_VERSION,
        "age": round(time.time() - _last["ts"], 1) if _last["ts"] else None,
        "live": bool(_live_cfg.get("enabled")),
        "count": len(_last.get("rows") or []),
        "mode": _live_cfg.get("mode"),
        "grade_min": _live_cfg.get("grade_min"),
        "providers": get_provider_status(),
    }


@app.get("/api/providers")
def providers():
    return get_provider_status()


@app.post("/api/scan")
def scan_post(body: ScanBody):
    return run_scan(
        body.tickers, body.max, body.actionable_only,
        force=body.force, mode=body.mode, grade_min=body.grade_min,
        min_dvol=body.min_dvol,
    )


@app.get("/api/scan")
def scan_get(
    tickers: Optional[str] = Query(None),
    max: int = Query(16, ge=1, le=200),
    actionable_only: bool = False,
    force: bool = False,
    mode: str = Query("hybrid"),
    grade_min: str = Query("A"),
    min_dvol: Optional[float] = Query(None),
):
    tlist = [x.strip() for x in tickers.split(",")] if tickers else None
    return run_scan(
        tlist, max, actionable_only, force=force, mode=mode,
        grade_min=grade_min, min_dvol=min_dvol,
    )


@app.get("/api/last")
def last():
    return {
        "meta": _last["meta"],
        "results": _last["rows"],
        "age_sec": round(time.time() - _last["ts"], 1) if _last["ts"] else None,
    }


@app.post("/api/live")
def live_set(body: LiveBody):
    with _live_lock:
        _live_cfg["enabled"] = bool(body.enabled)
        _live_cfg["interval_sec"] = int(body.interval_sec)
        _live_cfg["max"] = int(body.max)
        _live_cfg["tickers"] = body.tickers
        _live_cfg["actionable_only"] = bool(body.actionable_only)
        _live_cfg["mode"] = body.mode or "hybrid"
        _live_cfg["grade_min"] = body.grade_min or "A"
        if body.min_dvol is not None:
            _live_cfg["min_dvol"] = body.min_dvol
        enabled = _live_cfg["enabled"]
    # the loop rescans now with these settings (a new loop's first pass does), or stops; results
    # land in /api/last
    _live_wake.set()
    if enabled:
        _ensure_live_thread()
    return {"ok": True, "live": enabled, "cfg": dict(_live_cfg)}


@app.get("/api/live")
def live_get():
    return {"live": bool(_live_cfg.get("enabled")), "cfg": dict(_live_cfg)}


@app.get("/api/quote/{ticker}")
def quote(ticker: str):
    return fetch_quote(ticker.upper().strip())


@app.get("/api/chart/{ticker}")
def chart(ticker: str, mode: str = Query("hybrid"), force: bool = Query(False)):
    t = ticker.upper().strip()
    # prefer last scan chart if fresh
    cached = (_last.get("by_ticker") or {}).get(t)
    if cached and cached.get("_chart") and _last["ts"] and (time.time() - _last["ts"] < 120) and not force:
        payload = build_chart_from_row(cached)
        payload["cached"] = True
        return payload

    bars, daily, bar_prov, live, quote_meta = batch_fetch([t], force=force, mode=mode, with_daily=one_analyze is not None)
    qm = quote_meta.get(t) or {}
    row = analyze(
        t, bars.get(t), daily.get(t),
        live_price=live.get(t),
        bar_provider=bar_prov.get(t),
        quote_provider=qm.get("provider"),
        quote_latency_ms=qm.get("latency_ms"),
    )
    _last.setdefault("by_ticker", {})[t] = row
    payload = build_chart_from_row(row)
    payload["cached"] = False
    if row.get("error"):
        payload["error"] = row["error"]
    return payload


@app.get("/api/critique")
def critique():
    return {
        "version": APP_VERSION,
        "name": "VWAP Blue",
        "steelman": [
            "VWAP One desk layout (scanner left · interactive chart right) with free multi-provider rotate.",
            "Blueline dual VWAP: blue = day session VWAP ± volume-weighted σ · orange = prior-day anchor.",
            "Kaufman Efficiency Ratio regime gate: pure gap-fades suppressed in trend; multi-day reclaim kept.",
            "Adaptive band width (chop widens / trend tightens) inspired by Modern VWAP [GBB].",
            "Desk default grade ≥ A; thin RVOL samples demoted; One opposite-side → conflict demotion.",
            "Pure multi-day reverse in chop is demoted off the A desk (replay: −0.10R n=107). Gap / both kept.",
            "10× universe pool (~480 names) with a $ volume filter ($2M equity session / $0.5M crypto 24h "
            "by default).",
            "Rank by |gap|×RVOL×edge×$vol, show top N; thin tape demoted before the desk list.",
            "Equity signals only in premarket + RTH (Blueline hygiene); crypto trades its whole ET day, and with no "
            "opening gap to fade only multi-day orange reclaims trigger (v1.5.0).",
            "Crypto bars are one venue's 5m candles for 8 prior ET days (paged once, then only the tail is "
            "refreshed), so crypto RVOL has its full 7-day baseline.",
            "Auditable feeds per row; never fabricates prices.",
        ],
        "strawman": [
            "Session replay (replay_sessions.py) is research — live params are not auto-tuned from it.",
            "Crypto has no edge either: its first replay (replay_crypto.py) lost 0.91R per trade on v1.4.1's equity "
            "clock and came out flat on the v1.5.0 24h session, losing again when the day starts at UTC midnight.",
            "The Pages demo grades crypto on Yahoo's 5m bars (zero volume on about half of them), the desk on "
            "exchange candles (OKX first): the two can disagree.",
            "Walk-forward grid exists (walkforward.py) but is research — not auto-tuned live params.",
            "Orange anchor depends on clean multi-session bars — thin free history can mis-anchor.",
            "RVOL still n≤~6 free sessions max on 1m (Yahoo 8d hard-cap).",
            "The $vol filter uses free bar Volume×Close (one venue's for crypto), not exchange ADV.",
            "Full 10× pool fetch is slower on cold cache; hybrid yfinance bulk helps equities.",
            "One conflict uses free delayed bars; disagreement can be noise.",
            "Free APIs delay/disagree; hybrid yfinance vs OKX clocks differ.",
            "Paper book is client-side only (session memory) — not a broker.",
        ],
        "from_blueline": [
            "blue / orange dual VWAP trails",
            "gap dir · first break · fakeout count · confirm · tag · stop",
            "grade A/B/C + late prefix",
            "RVOL vs prior days (n shown) · runway / R multiple",
            "prior RTH close reference",
            "8d Yahoo 1m window for fuller RVOL baselines",
        ],
        "from_modern_vwap": [
            "volume-weighted σ (not close-only)",
            "KER adaptive band mult",
            "regime-gated signal families (fade vs reclaim)",
        ],
        "from_vwap_one": [
            "scanner + chart split layout",
            "free multi-provider rotate + live loop",
            "edge rank · actionable flags · provider provenance",
            "hybrid / rotate / yfinance modes",
            "cross-scanner conflict demotion",
        ],
        "providers": [
            "okx, binance, bybit, coinbase, coingecko",
            "yahoo_chart, stooq, eodhd_demo, yfinance",
        ],
    }


@app.get("/api/ledger")
def ledger_report(model: str = Query("classic"), entry: str = Query("next_open")):
    """Forward record of live grade-A triggers, resolved after each session (see ledger.py)."""
    return ledger.report(model=model, entry_mode=entry)


@app.post("/api/ledger/resolve")
def ledger_resolve():
    """Resolve signals whose session has closed (fetches that session's bars)."""
    counts = ledger.resolve()
    return {"resolve": counts, "report": ledger.report()}


@app.get("/api/universe")
def universe(max: int = Query(480, ge=1, le=600)):
    u = load_universe(max_n=max)
    return {"tickers": u, "count": len(u), "pool_mult": POOL_MULT, "min_dvol": _DEFAULT_MIN_DVOL}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("VWAP_BLUE_PORT", "8791"))
    # Pass app object (not "app:app") so sys.path quirks can't load another app module
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
