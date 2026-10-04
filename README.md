# VWAP Blue

**Best-of Blueline + VWAP One** in the VWAP desk layout, with Modern-VWAP-style
adaptive bands and Kaufman Efficiency Ratio regime gates (**v1.4.1**).

**Research scanner, no demonstrated edge**: replayed on sessions no rule was tuned on, its grade-A
triggers lost money after costs ([honest results](#honest-results)).

## Share / live demo

| | |
|--|--|
| **Web demo (GitHub Pages)** | **https://21e8-miner.github.io/vwap-blue/** |
| **Source** | https://github.com/21e8-miner/vwap-blue |
| **Related desk** | https://21e8-miner.github.io/blueline-orangeline/ |

The Pages demo runs the desk's own engine in the browser: `engine.js` is a line-by-line port of
`engine.py`, and CI runs both on hundreds of synthetic sessions (gap fades, trend days, reclaims,
DST weeks, crypto, broken feeds) and compares every output field (`tests/test_engine_parity.py`).
It pulls 14 days of free 5m Yahoo bars per name through a CORS proxy and applies the desk's scan
pipeline: session $ volume floor → engine → stale-bar guard → grade floor → rotation rank. Unlike
the local desk it scans a smaller pool (≥160 names vs ~480), has no VWAP One cross-check, uses the
last bar's close rather than a live quote, and keeps no forward ledger. `cf-pages/` is an
identical copy for Cloudflare Pages (a test keeps it so).

The full live desk (5m hybrid feeds, live loop, chart pane, forward ledger) runs locally on `:8791`.

| From | What |
|------|------|
| **VWAP One** | Scanner left · chart right, free multi-provider rotate, live loop, edge rank |
| **Blueline** | Blue day VWAP ± volume-weighted σ · orange prior-day anchor · gap MR · grades |
| **v1.2** | KER regime gate · adaptive σ mult · grade A desk default · universe rotation · One conflict demotion · partial/time-stop backtest · walk-forward grid |
| **v1.3** | **10× scan pool** (~480 names) · session **$ volume filter** ($2M equity / $0.5M crypto) · rank by gap×RVOL×edge×$vol |
| **v1.3.1** | Prefix-honest session replay · **mdrev-in-chop demoted** off A desk |
| **v1.4** | **Honest replay**: next-bar-open fills, R **net of costs**, session-clustered CIs · **stale-bar guard** · **forward signal ledger** |
| **v1.4.1** | **Prior-close fix**: gaps were measured from the prior session's 09:30 close instead of its last RTH bar · Pages demo runs the desk engine (`engine.js`, parity-tested) · round-trip cost in R on every plan · blocked fades badge WATCH, not TRIGGER |

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
- `VWAP_BLUE_MAX_BAR_AGE_MIN=15` — while the market is open, a TRIGGER on bars older than this is demoted to WATCH (STALE)
- `VWAP_BLUE_LEDGER=0` — stop recording live grade-A triggers to `data/signals/`

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
# Prefix-honest multi-session replay (matches live 5m desk, ~1mo).
# Fills on the bar after the trigger (+2 bps); prints net-of-cost R with session-clustered CIs.
python3 replay_sessions.py --max-tickers 96 --grade-min A
python3 replay_sessions.py --entry trigger_close          # the old, optimistic trigger-close fill

# Honest stats for a saved replay, no fetching
python3 replay_sessions.py --rescore research/replay_2026-08-12.json

# Forward record of live grade-A triggers (recorded automatically by the desk)
python3 ledger.py resolve    # after the close: resolve the session's signals
python3 ledger.py report     # net R, session-clustered CI, by setup mode x regime

# Same-day leftover-bar check (thin n — do not use for expectancy)
python3 backtest_today_scans.py --blue-only --grade-min A --model classic

# Parameter grid on current free bars (in-sample: picks and scores on the same bars)
python3 walkforward.py --quick
python3 walkforward.py --max-tickers 20
```

### Honest results

Grade ≥ A at the trigger bar · 96 names · 5m bars · held to the session close · R **net of
round-trip costs** (0.08% equity, converted per trade: cost ÷ risk) · ± **session-clustered**
standard error (`honest.py`: setups on the same day share the tape, so ~400 trades from 22
sessions are ~22 independent draws). Verdicts need |t| ≥ 2 across ≥ 10 sessions.

| sessions | role | engine | fill | n | gross R | **net R ± SE** | verdict |
|---|---|---|---|--:|--:|--:|---|
| Jul 14 – Aug 12 | in-sample (rules tuned here), all A | v1.3 ¹ | trigger close | 419 | +0.22 | **+0.13 ± 0.16** | inconclusive |
| Jul 14 – Aug 12 | in-sample, mdrev-in-chop removed | v1.3.1 ¹ | trigger close | 312 | +0.33 | **+0.23 ± 0.18** | inconclusive |
| Sep 3 – Oct 2 | out of sample | v1.3.1 ¹ | next bar open +2 bps | 265 | −0.15 | **−0.27 ± 0.13** | negative (t −2.1) |
| Sep 3 – Oct 2 | out of sample | v1.3.1 ¹ | trigger close | 266 | −0.13 | −0.25 ± 0.13 | inconclusive (t −1.9) |
| **Sep 3 – Oct 2** | **out of sample, prior close fixed** | **v1.4.1** | next bar open +2 bps | 295 | −0.11 | **−0.23 ± 0.12** | inconclusive (t −1.9) |
| Sep 3 – Oct 2 | the same refetched bars, old prior close | v1.4.0 ¹ | next bar open +2 bps | 280 | −0.16 | −0.28 ± 0.12 | negative (t −2.4) |

¹ Measured every gap from the prior session's 09:30 close (fixed in v1.4.1). August's 5m bars
are past the free data window, so its rows cannot be re-run. The last two rows share one fetch;
it reached slightly further back than the morning fetch behind the 265-trade row (19 more trades
on Sep 3–4, 261 of the 265 in common).

What this says:

- **No demonstrated edge.** August's edge was never distinguishable from zero once costs and
  same-day clustering are counted, and the rule set tuned on August lost about 0.5R per trade
  relative to that in September.
- **The prior-close bug mattered for which trades, not for the verdict.** Fixing it changed about
  a quarter of September's trades (58 dropped, 73 added, 222 kept; the median gap on a traded
  setup fell from 2.0% to 1.4%) and moved net R from −0.28 to −0.23 on the same bars: inside the
  noise, still negative. The page and the desk now show the fixed engine.
- **Costs are not small.** At these stop widths a round trip is 0.09–0.12R per trade.
- **Segments flip month to month.** On the v1.3.1 engine mdrev in mixed regimes was August's best
  segment (+0.38R) and September's worst (−0.39R, t −2.6); gap fades in chop went +0.20 → −0.17.
  One-month rules, the v1.3.1 mdrev-in-chop demotion included, fit noise.
- **Exit rankings flip too.** August favoured holding to orange; in September the
  partial/trail exit lost least (−0.15R net vs −0.27R).
- **Fill realism** (next bar open vs trigger close) costs ~0.015R on 5m bars here.

Treat the desk as a research scanner. The forward ledger (`ledger.py`) records every live
grade-A trigger as it is shown and scores it after the close; trust a grade, gate or learned
filter only once months of that record agree.

Re-score any saved replay without refetching: `python3 replay_sessions.py --rescore <file>`.
Raw JSON: [`research/replay_2026-08-12.json`](research/replay_2026-08-12.json) (August,
trigger-close fills) · [`research/replay_2026-10-04.json`](research/replay_2026-10-04.json)
(September, next-bar fills) · [`research/replay_2026-10-04_v1.4.1.json`](research/replay_2026-10-04_v1.4.1.json)
(September, fixed engine; `method.paired_v1_4_0_same_bars` holds the old engine on the same bars).
A replay never overwrites an existing snapshot for the same day.

Same-day leftover-bar scans (`backtest_today_scans.py`) print results on a handful of trades
from one session; they are not expectancy.

## History / RVOL

- Free Yahoo **1m** hard-cap is **8 calendar days** per request (`10d` rejects). Default fetch is **`8d`** (was `5d`).
- Coarser bars use a longer free window (`5m`/`15m` → `1mo` via `bars_range_for_interval`).
- RVOL uses **all prior sessions** in the window (cap 7), not a hard 4, and surfaces **`rvol_n`** / **`session_n`** so thin samples stay visible.
- yfinance uses **start/end** clamped to 8d for 1m so requests are not rejected or silently collapsed to period=`5d`.

## Layout

```
index.html          # GitHub Pages / shareable demo (runs engine.js in the browser)
engine.js           # browser port of engine.py + the desk's scan helpers (parity-tested)
cf-pages/           # identical copy of index.html + engine.js for Cloudflare Pages
static/index.html   # full local desk UI (served by app.py)
app.py              # FastAPI · live loop · :8791
engine.py           # dual VWAP + KER + grades (ENGINE_VERSION)
providers.py / data.py
backtest_today_scans.py · walkforward.py · replay_sessions.py
honest.py           # cost-in-R and session-clustered standard errors (replay + ledger)
ledger.py           # forward signal ledger: record → resolve → report (data/signals/, gitignored)
tests/              # python3 -m unittest discover -s tests (engine parity needs node)
research/           # sample backtest / session-replay JSON
```

Research only. Free feeds delay. Not financial advice.
