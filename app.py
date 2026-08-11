"""VWAP Blue — Blueline dual-VWAP engine in the VWAP One desk layout. Port 8791."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from data import batch_fetch, get_provider_status, load_universe, rotation_score
from engine import analyze, build_chart_from_row
from providers import fetch_quote

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vwap_blue")

ROOT = Path(__file__).resolve().parent
APP_VERSION = "1.2.0-blue"
app = FastAPI(title="VWAP Blue", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

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
}
_live_thread: Optional[threading.Thread] = None

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
    max: int = Field(default=16, ge=1, le=80)
    actionable_only: bool = False
    force: bool = False
    mode: str = Field(default="hybrid")
    grade_min: str = Field(default="A")


class LiveBody(BaseModel):
    enabled: bool = True
    interval_sec: int = Field(default=45, ge=15, le=300)
    max: int = Field(default=16, ge=1, le=80)
    tickers: Optional[List[str]] = None
    actionable_only: bool = False
    mode: str = "hybrid"
    grade_min: str = "A"


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


def run_scan(
    tickers: Optional[List[str]] = None,
    max_n: int = 16,
    actionable_only: bool = False,
    force: bool = False,
    mode: str = "hybrid",
    grade_min: str = "A",
) -> Dict[str, Any]:
    t0 = time.time()
    user_supplied = bool(tickers)
    if not tickers:
        # Scan a wider pool, then rotate down to max_n by gap/RVOL/edge
        pool_n = max(48, max_n * 3)
        tickers = load_universe(max_n=pool_n)
    else:
        tickers = [t.strip().upper() for t in tickers if t.strip()][: max(max_n, 40)]

    mode = (mode or "hybrid").lower()
    if mode not in ("rotate", "hybrid", "yfinance"):
        mode = "hybrid"

    bars, daily, bar_prov, live, quote_meta = batch_fetch(
        tickers, force=force, mode=mode,
    )

    rows: List[Dict[str, Any]] = []
    by_ticker: Dict[str, Dict[str, Any]] = {}
    for t in tickers:
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
        _apply_one_conflict(row, bars, daily, live, bar_prov, quote_meta)
        by_ticker[t] = row
        # strip heavy chart blob from table payload (kept in by_ticker)
        slim = {k: v for k, v in row.items() if k != "_chart"}
        slim["_rot"] = rotation_score(slim)
        rows.append(slim)

    if actionable_only:
        rows = [r for r in rows if r.get("actionable") or r.get("live_actionable")]
    if grade_min and grade_min.upper() not in ("", "C", "–", "-"):
        gm = grade_min.upper()
        rows = [r for r in rows if _grade_ok(r.get("grade") or "–", gm)]

    # Rotation: gappers / high |dev| / RVOL / edge first, then hard-cap max_n for auto universe
    rows.sort(
        key=lambda r: (
            r.get("_rot") or 0,
            r.get("edge") or 0,
            GRADE_RANK.get(r.get("grade") or "–", -1),
        ),
        reverse=True,
    )
    if not user_supplied:
        rows = rows[:max_n]
    else:
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
        "regime_counts": regimes,
        "grade_min": grade_min,
        "pool_scanned": len(tickers),
        "version": APP_VERSION,
        "mode": mode,
        "providers_used": provs,
        "elapsed_sec": round(time.time() - t0, 2),
        "asof": time.strftime("%Y-%m-%d %H:%M:%S"),
        "live": bool(_live_cfg.get("enabled")),
        "engine": "blueline dual VWAP · KER regime · VW-σ adaptive · One conflict",
    }
    _last["ts"] = time.time()
    _last["rows"] = rows
    _last["meta"] = meta
    _last["by_ticker"] = by_ticker
    log.info(
        "scan ok n=%s A=%s conflicts=%s regimes=%s feeds=%s in %.2ss",
        meta["count"], meta["grade_a"], meta["conflicts"], regimes, provs, meta["elapsed_sec"],
    )
    return {"results": rows, "meta": meta}


def _live_loop() -> None:
    log.info("live loop started")
    while True:
        with _live_lock:
            if not _live_cfg["enabled"]:
                break
            interval = int(_live_cfg["interval_sec"])
            max_n = int(_live_cfg["max"])
            tickers = _live_cfg.get("tickers")
            actionable_only = bool(_live_cfg.get("actionable_only"))
            mode = str(_live_cfg.get("mode") or "hybrid")
            grade_min = str(_live_cfg.get("grade_min") or "A")
        try:
            run_scan(tickers, max_n, actionable_only, force=True, mode=mode, grade_min=grade_min)
        except Exception as e:
            log.exception("live scan failed: %s", e)
        for _ in range(interval):
            with _live_lock:
                if not _live_cfg["enabled"]:
                    break
            time.sleep(1)
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
        _live_cfg["max"] = max(1, min(80, max_n))
        _live_cfg["mode"] = mode if mode in ("hybrid", "rotate", "yfinance") else "hybrid"
        _live_cfg["grade_min"] = grade_min
    log.info(
        "party live ON interval=%ss mode=%s grade_min=%s",
        _live_cfg["interval_sec"], _live_cfg["mode"], grade_min,
    )
    _ensure_live_thread()
    try:
        run_scan(
            None, _live_cfg["max"], False, force=True,
            mode=_live_cfg["mode"], grade_min=_live_cfg["grade_min"],
        )
    except Exception as e:
        log.warning("boot scan: %s", e)


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
    )


@app.get("/api/scan")
def scan_get(
    tickers: Optional[str] = Query(None),
    max: int = Query(16, ge=1, le=80),
    actionable_only: bool = False,
    force: bool = False,
    mode: str = Query("hybrid"),
    grade_min: str = Query("A"),
):
    tlist = [x.strip() for x in tickers.split(",")] if tickers else None
    return run_scan(tlist, max, actionable_only, force=force, mode=mode, grade_min=grade_min)


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
        enabled = _live_cfg["enabled"]
        mode = _live_cfg["mode"]
        grade_min = _live_cfg["grade_min"]
    if enabled:
        _ensure_live_thread()
        try:
            run_scan(body.tickers, body.max, body.actionable_only, force=True, mode=mode, grade_min=grade_min)
        except Exception as e:
            log.warning("immediate live scan: %s", e)
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

    bars, daily, bar_prov, live, quote_meta = batch_fetch([t], force=force, mode=mode)
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
            "Universe rotation: scan wide pool, rank by |gap|×RVOL×edge, show top N.",
            "Signals only in premarket + RTH (Blueline hygiene); crypto 24/7 path kept separate.",
            "Auditable feeds per row; never fabricates prices.",
        ],
        "strawman": [
            "Walk-forward grid exists (walkforward.py) but is research — not auto-tuned live params.",
            "Orange anchor depends on clean multi-session bars — thin free history can mis-anchor.",
            "RVOL still n≤~6 free sessions max on 1m (Yahoo 8d hard-cap).",
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


@app.get("/api/universe")
def universe(max: int = Query(40, ge=1, le=80)):
    return {"tickers": load_universe(max_n=max)}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("VWAP_BLUE_PORT", "8791"))
    # Pass app object (not "app:app") so sys.path quirks can't load another app module
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
