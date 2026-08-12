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
| **v1.3.1** | Prefix-honest session replay · **mdrev-in-chop demoted** off A desk |

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
# Prefix-honest multi-session replay (matches live 5m desk, ~1mo)
python3 replay_sessions.py --max-tickers 96 --grade-min A

# Same-day leftover-bar check (thin n — do not use for expectancy)
python3 backtest_today_scans.py --blue-only --grade-min A --model classic

# Parameter grid on current free bars
python3 walkforward.py --quick
python3 walkforward.py --max-tickers 20
```

Honest snapshot (`replay_sessions.py`, 96 names, 5m, 22 sessions
2026-07-14 → 2026-08-12, grade ≥ A **at the trigger bar**, hold to cash close):

| book | n | win | avg R | sum R |
|------|--:|----:|------:|------:|
| all setups · classic | 419 | 44% | **+0.22** | +93 |
| all setups · time-stop 2h | 419 | 44% | **+0.23** | +96 |
| all setups · desk `partial_trail` | 419 | 56% | +0.16 | +67 |
| **gap + both only · time_24** | 246 | 44% | **+0.34** | +83 |
| **gap + both only · classic** | 246 | 44% | **+0.33** | +82 |
| mdrev · chop only · classic | 107 | 38% | **−0.10** | — |

Taking profits earlier (full 1R / 0.75R / giveback-50) **raises win rate and
lowers expectancy** — the right tail that tags orange (~1.7R avg win) is what
pays for the stops. The leak is dead mdrev-in-chop, not slow targets.

Same-day leftover-bar scans (old `backtest_today_scans.py`) can print −0.18R
on n=10. That is not a month of trades.

Raw JSON: [`research/replay_2026-08-12.json`](research/replay_2026-08-12.json).

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
backtest_today_scans.py · walkforward.py · replay_sessions.py
research/           # sample backtest / session-replay JSON
```

Research only. Free feeds delay. Not financial advice.
