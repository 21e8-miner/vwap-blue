# VWAP Blue

**Best-of Blueline + VWAP One** in the VWAP desk layout, with Modern-VWAP-style
adaptive bands and Kaufman Efficiency Ratio regime gates (**v1.2**).

## Share / live demo

| | |
|--|--|
| **Web demo (GitHub Pages)** | **https://21e8-miner.github.io/vwap-blue/** |
| **Source** | https://github.com/21e8-miner/vwap-blue |
| **Related desk** | https://21e8-miner.github.io/blueline-orangeline/ |

The Pages demo is a client-side scanner (Yahoo chart + optional CORS proxy).
Full live desk (1m hybrid feeds, live loop, chart pane) runs locally on `:8791`.

| From | What |
|------|------|
| **VWAP One** | Scanner left · chart right, free multi-provider rotate, live loop, edge rank |
| **Blueline** | Blue day VWAP ± volume-weighted σ · orange prior-day anchor · gap MR · grades |
| **v1.2** | KER regime gate · adaptive σ mult · grade A desk default · universe rotation · One conflict demotion · partial/time-stop backtest · walk-forward grid |
| **v1.3** | **10× scan pool** (~480 names) · session **$ volume filter** ($2M equity / $0.5M crypto) · rank by gap×RVOL×edge×$vol |

## Run (local full desk)

```bash
git clone https://github.com/21e8-miner/vwap-blue.git
cd vwap-blue
python3 -m pip install -r requirements.txt
python3 app.py
# → http://127.0.0.1:8791/
```

Env:
- `VWAP_BLUE_LIVE=0` — disable auto live
- `VWAP_BLUE_LIVE_SEC=45` — scan interval
- `VWAP_BLUE_PORT=8791`
- `VWAP_BLUE_GRADE_MIN=A` — desk grade floor (default A)
- `VWAP_BLUE_POOL_MULT=10` — universe pool multiplier (was effectively ~3× / 48 names)
- `VWAP_BLUE_MIN_DVOL=2000000` — equity session $ volume floor (`0` = off; crypto default $0.5M)

## Thesis

Gap extends away from **blue** (session VWAP, hlc3×volume). After K consecutive
bars **beyond blue by adaptive volume-weighted σ** → **confirm**. Target
**orange** (prior-day-anchored VWAP). Stop = blue ± ATR/σ × adaptive mult.

- **Chop regime** (low KER): favor gap-fades, wider bands  
- **Trend regime** (high KER): suppress pure gap-fades; keep multi-day reclaim  
- **Grade A** requires R:R + RVOL with adequate `rvol_n`  
- **Live actionable** = Grade A/LA only (no thin sample, no One conflict)

## Research tools

```bash
# Session backtest (partial 1R + trail + time-stop)
python3 backtest_today_scans.py
python3 backtest_today_scans.py --blue-only --grade-min A --model partial_trail
python3 backtest_today_scans.py --model classic

# Parameter grid on current free bars
python3 walkforward.py --quick
python3 walkforward.py --max-tickers 20
```

Sample snapshot (session `2026-08-11`, grade ≥ A, `partial_trail`):

| metric | value |
|--------|------:|
| n | 10 |
| win rate | 40% |
| avg R | −0.18 |
| avg MFE R | 1.23 |
| exits | 7 stop / 3 partial target |

Raw JSON: [`research/backtest_2026-08-11.json`](research/backtest_2026-08-11.json),
[`research/walkforward_2026-08-11.json`](research/walkforward_2026-08-11.json).

## History / RVOL

- Free Yahoo **1m** hard-cap is **8 calendar days** per request (`10d` rejects). Default fetch is **`8d`** (was `5d`).
- Coarser bars use a longer free window (`5m`/`15m` → `1mo` via `bars_range_for_interval`).
- RVOL uses **all prior sessions** in the window (cap 7), not a hard 4, and surfaces **`rvol_n`** / **`session_n`** so thin samples stay visible.
- yfinance uses **start/end** clamped to 8d for 1m so requests are not rejected or silently collapsed to period=`5d`.

## Layout

```
index.html          # GitHub Pages / shareable SPA demo
static/index.html   # full local desk UI (served by app.py)
app.py              # FastAPI · live loop · :8791
engine.py           # dual VWAP + KER + grades
providers.py / data.py
backtest_today_scans.py · walkforward.py
research/           # sample backtest / walk-forward JSON
```

Research only. Free feeds delay. Not financial advice.
