# VWAP Blue

**Best-of Blueline + VWAP One** in the VWAP desk layout, with Modern-VWAP-style
adaptive bands and Kaufman Efficiency Ratio regime gates (**v1.7.0**).

**Research scanner, no demonstrated edge**: replayed on sessions no rule was tuned on, its grade-A
triggers lost money after costs on US equities and showed no edge on crypto
([honest results](#honest-results)).

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
the local desk it scans a smaller pool (≥160 names vs ~480), grades crypto on Yahoo's bars rather
than exchange candles, has no VWAP One cross-check, uses the last bar's close rather than a live
quote, and keeps no forward ledger. `cf-pages/` is an identical copy for Cloudflare Pages (a test
keeps it so).

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
| **v1.5.0** | **Crypto gets a 24h session**: blue, orange, RVOL and signals use the whole ET day, the prior close is the prior day's last bar, and there is no gap fade (a 24/7 market has no opening gap) · first crypto replay (`replay_crypto.py`) · the session replay grades every bar as the desk saw it and closes equity trades by 16:00 (`causal_check.py` re-runs its old end-of-day trigger search) |
| **v1.6.0** | **mdrev gets a real target**: the first level beyond entry among today's VWAP, today's extreme before the reclaim, the prior session's regular-hours extreme and close (`engine._md_target`); with none, the plan has no target and cannot grade A. v1.5.x stretched a fixed max(1.5 × risk, 0.4%), so every such R:R read 1.50 and the R:R gate never applied · the rule and a missing-RVOL gate are engine options (`md_target`, `rvol_missing`), compared in `md_target_check.py` · parity now covers every option |
| **v1.7.0** | **Grade A means a multi-day reclaim.** On 59 replayed sessions the gap fade's direction call did worse than its mirror (−0.21R vs −0.09R) while the reclaim's was the only one with a hint (+0.33R ± 0.19), so a fade, alone or with a reclaim, is capped at grade B (`fade_max_grade`); the desk's default floor moves to B, so the board still shows the fades · the **Record** column shows every row's class as it replayed (`evidence.py`; the capped fades' record is the paired v1.6.0 replay's on the same bars, marked A→B, since no grade-A replay takes them any more) · the forward ledger records the capped fades too, reported beside grade A |

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
- `VWAP_BLUE_GRADE_MIN=B` — desk grade floor (default B since v1.7.0: gap fades show at B, reclaims at A)
- `VWAP_BLUE_POOL_MULT=10` — universe pool multiplier (was effectively ~3× / 48 names)
- `VWAP_BLUE_MIN_DVOL=2000000` — equity $ volume floor (`0` = off), on the larger of a stock's ET day
  so far and its prior session. Crypto gets a quarter of it ($0.5M at the default) on its last 24
  hours of volume. A day so far is minutes old early on, after midnight ET for crypto and in premarket
  for a stock once it first trades: on the day so far alone, 9.5% of the desk's stocks cleared $2M at
  09:00 ET and 9% of its crypto names at 00:55. The desk's $M box and `min_dvol` work the same way
- `VWAP_BLUE_YF_RPS=20` — pace of the yfinance bulk download, in requests a second (`0` = unpaced; yfinance
  alone sends ~45 a second). A chunk Yahoo rate-limits (HTTP 429) pauses it 15 s and its names are asked
  for again at half the pace
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
# Prefix-honest multi-session replay (matches live 5m desk, ~1mo): every bar graded as the desk saw it,
# fills on the bar after the trigger (+2 bps), equities out by the 16:00 close; prints net-of-cost R
# with session-clustered CIs.
python3 replay_sessions.py --max-tickers 96 --grade-min A
python3 replay_sessions.py --range-60d --save-bars data/backtests/eq_60d.pkl   # 60 days of 5m bars (Yahoo chart API), ~59 sessions
python3 replay_sessions.py --save-bars data/backtests/equity_bars.pkl  # keep the fetch: Yahoo's window moves
python3 replay_sessions.py --bars data/backtests/equity_bars.pkl       # replay those bars again
python3 replay_sessions.py --entry trigger_close          # the old, optimistic trigger-close fill

# Honest stats for a saved replay, no fetching
python3 replay_sessions.py --rescore research/replay_2026-08-12.json

# The crypto names (the replay above takes the first N names of universe.txt: all equities)
python3 replay_crypto.py --save-bars data/backtests/crypto_bars.pkl
python3 replay_crypto.py --bars data/backtests/crypto_bars.pkl --utc-days   # crypto's day from 00:00 UTC
python3 replay_crypto.py --source coinbase                                  # Coinbase bars instead of Yahoo
python3 replay_crypto.py --source binance-vision --days 365 --jobs 8 --save-bars data/backtests/crypto_bv.pkl
#   a year of Binance spot 5m bars from data.binance.vision (no key; checksum-verified; cached in data/binance_vision/)
# bars cached before the Yahoo symbol map hold other tokens under ARB/TON/JUP-USD: add
#   --exclude ARB-USD,TON-USD,JUP-USD

# The replay's old end-of-day trigger search on the same bars: what it missed, how it scored
python3 causal_check.py --bars data/backtests/equity_bars.pkl

# What each setup class did on the replays the page cites → research/evidence_v<engine>.json, pasted
# into the page as its Record column (the page test recomputes it from the linked files)
python3 evidence.py --embed index.html cf-pages/index.html

# mdrev target rules and the missing-RVOL gate on the same bars, plus a 60-day holdout
python3 md_target_check.py --fetch-holdout data/backtests/holdout_60d.pkl
python3 md_target_check.py --bars data/backtests/equity_bars.pkl --holdout data/backtests/holdout_60d.pkl

# Breakout vs fade at the engine's own gap states (first band break, fakeout): same events, both
# directions, same exits, net of costs, session-clustered (60 days of 5m bars, first 96 equities)
python3 direction_check.py

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

Grade ≥ A at the trigger bar · 5m bars · R **net of round-trip costs** (0.08% equity, 0.15% crypto,
converted per trade: cost ÷ risk) · ± **session-clustered** standard error (`honest.py`: setups on
the same day share the tape, so ~400 trades from 22 sessions are ~22 independent draws). Verdicts
need ≥ 10 sessions and |t| past Student's t with (sessions − 1) degrees of freedom at two-sided 5%,
never less than 2: 2.26 at 10 sessions, 2.09 at 21, 2.00 at 61 (before Oct 23 the bar was a flat 2;
no verdict in these tables changes). Every replay also scores a **mirror**: the same bracket the
other way from the same bar, so *trade − mirror* shows what the trigger's direction adds after the
stop, target and costs, which the mirror shares.

Since Oct 4 the replay grades each bar as the desk showed it then, on the bars up to that one, and
trades the first tradeable trigger (*bar by bar*); before, it took each session's trigger from the
end-of-day row (*end of day*). Equities now exit by the 16:00 close (*16:00*), not at the last
after-hours bar, ~19:55 ET (*after hours*). Crypto holds to its ET day's last bar either way.

#### US equities (the first 96 names of `universe.txt`)

| sessions | role | engine | triggers · exit | fill | n | gross R | **net R ± SE** | verdict |
|---|---|---|---|---|--:|--:|--:|---|
| Jul 14 – Aug 12 | in-sample (rules tuned here), all A | v1.3 ¹ | end of day · after hours | trigger close | 419 | +0.22 | **+0.13 ± 0.16** | inconclusive |
| Jul 14 – Aug 12 | in-sample, mdrev-in-chop removed | v1.3.1 ¹ | end of day · after hours | trigger close | 312 | +0.33 | **+0.23 ± 0.18** | inconclusive |
| Sep 3 – Oct 2 | out of sample | v1.3.1 ¹ | end of day · after hours | next bar open +2 bps | 265 | −0.15 | **−0.27 ± 0.13** | negative (t −2.1) |
| Sep 3 – Oct 2 | out of sample | v1.3.1 ¹ | end of day · after hours | trigger close | 266 | −0.13 | −0.25 ± 0.13 | inconclusive (t −1.9) |
| Sep 3 – Oct 2 | out of sample, prior close fixed | v1.4.1 | end of day · after hours | next bar open +2 bps | 295 | −0.11 | **−0.23 ± 0.12** | inconclusive (t −1.9) |
| Sep 3 – Oct 2 | the same refetched bars, old prior close | v1.4.0 ¹ | end of day · after hours | next bar open +2 bps | 280 | −0.16 | −0.28 ± 0.12 | negative (t −2.4) |
| Sep 3 – Oct 2 | out of sample, refetched | v1.5.0 ² | end of day · after hours | next bar open +2 bps | 292 | −0.10 | **−0.22 ± 0.12** | inconclusive (t −1.9) |
| Sep 3 – Oct 2 | the same bars | v1.5.0 | bar by bar · after hours | next bar open +2 bps | 431 | −0.14 | −0.23 ± 0.07 | negative (t −3.4) |
| **Sep 3 – Oct 2** | **out of sample, refetched, all 96 names** ³ | **v1.5.0** | **bar by bar · 16:00** | next bar open +2 bps | 428 | −0.08 | **−0.17 ± 0.06** | negative (t −2.8) |
| Sep 3 – Oct 2 | the same bars | v1.5.0 | bar by bar · after hours | next bar open +2 bps | 428 | −0.11 | −0.20 ± 0.07 | negative (t −3.0) |
| Sep 3 – Oct 2 | the same bars | v1.5.0 | end of day · 16:00 | next bar open +2 bps | 289 | −0.04 | −0.16 ± 0.10 | inconclusive (t −1.5) |
| **Jul 28 – Oct 19** | **60 days of 5m bars, all 96 names** ⁴ | **v1.6.0** | **bar by bar · 16:00** | next bar open +2 bps | 654 | −0.05 | **−0.16 ± 0.08** | negative (t −2.1) |
| Jul 28 – Oct 19 | the same bars | v1.5.0 | bar by bar · 16:00 | next bar open +2 bps | 1,094 | −0.04 | −0.13 ± 0.05 | negative (t −2.5) |
| **Jul 28 – Oct 19** | **the same bars; grade A = reclaims only** | **v1.7.0** | **bar by bar · 16:00** | next bar open +2 bps | 174 | +0.12 | **+0.05 ± 0.13** | inconclusive (t 0.4) |
| Jul 28 – Oct 19 | the same bars; the fades v1.7.0 grades B | v1.6.0 ⁵ | bar by bar · 16:00 | next bar open +2 bps | 480 | −0.11 | −0.23 ± 0.09 | negative (t −2.5) |

¹ Measured every gap from the prior session's 09:30 close (fixed in v1.4.1). August's 5m bars
are past the free data window, so its rows cannot be re-run. The v1.4.1 and v1.4.0 rows share one
fetch; it reached slightly further back than the morning fetch behind the 265-trade row (19 more
trades on Sep 3–4, 261 of the 265 in common).
² v1.5.0 changed only crypto: on one fetch v1.4.1 and v1.5.0 produce the same 292 equity trades,
field for field. That fetch starts later on Sep 2 than the v1.4.1 row's (Sep 2 is the first
replayed day's prior session and part of the early RVOL baselines), so 285 of the 295 recur.
³ The fetch `replay_sessions.py --max-tickers 96` makes, taken Oct 4 at 15:07 ET and replayed from
that cache (`--bars`): Yahoo's 5m window is the last 32 days, so later that day a fetch had no
regular-session bars from Sep 2, the first replayed day's prior session (and after 20:00 ET none
from Sep 2 at all). Same window as the rows above, with two differences. All 96 names: the Oct 4 files
above replay only the 92 that traded in the morning v1.4.0 run, never PINS, DUOL, HIMS or ACHR (5
trades here). And this fetch starts Sep 2 at 15:05 ET instead of 12:45, which moves orange's seed on
Sep 3 and an RVOL baseline on Sep 4: on the 92 names, bar by bar and after hours, it gives 423 trades
at −0.21R against 431 at −0.23R, the same trades from Sep 5 on. The old replay (end of day · after
hours) on these bars: 289 trades, −0.18R ± 0.12 (t −1.5).

⁴ Yahoo's chart API serves 60 days of 5m bars (`range=60d`); the desk's own fetch asks for 32.
Fetched Oct 19 in the evening (`md_target_check.py --fetch-holdout`), so it overlaps the September
rows above and adds Jul 28 – Aug 29 and Oct 5 – 19. Paired on these bars, v1.6.0 keeps 627 of
v1.5.0's trades, drops 467 (multi-day reclaims with no real level beyond entry) and adds 27; the
per-session difference is −0.03R ± 0.05 (t −0.6).

⁵ The 480 v1.6.0 grade-A trades that v1.7.0 grades B: the gap fades alone and with a reclaim. Their
direction call did worse than their mirror, −0.11R ± 0.17; the 174 reclaims' did better, +0.33R ± 0.19
(`method.paired_v1_6_0_same_bars` in the v1.7.0 file).

What this says:

- **v1.7.0 changes what grade A means, not what the engine finds.** Every row v1.6.0 showed still
  shows at the desk's new default floor of B; the grade now says which class has any evidence behind
  it. Grade A, the reclaims: +0.05R ± 0.13 on 54 sessions, inconclusive, with the only positive
  direction call. Grade B, the fades: −0.23R ± 0.09, negative, and worse than their mirror. A
  premarket or cost gate was tried and withdrawn: the premarket triggers *are* the reclaims, and the
  trades with the highest cost in R had the best direction.
- **The mdrev target was a placeholder; a real one changes nothing measurable.** Every mdrev
  plan in v1.5.x had R:R 1.50 by construction (232 of 232 in September). `md_target_check.py`
  replays five real-level rules on the September bars and on 38 other sessions: none is better
  than the stretch in both, and the stretch itself is flat (−0.13R in September, +0.02R
  elsewhere). v1.6.0 takes the nearest level on principle, so that R:R and its gate mean
  something, not because it scored best. Making a missing RVOL count as thin halved the trades
  without helping, so it stays an option, off.
- **The losses are the gap fades.** On 59 sessions, v1.6.0's gap fades lost −0.21R ± 0.11
  (t −1.9, 388 trades) and gap + reclaim setups −0.34R ± 0.14 (t −2.4, 92), while multi-day
  reclaims made +0.05R ± 0.13 (174); v1.5.0 on the same bars splits the same way. With the
  breakout-vs-fade check below finding no edge in the other direction either, the opening gap
  is where this engine loses, and what a forward test should watch.
- **The direction call adds nothing measurable; the bracket loses either way.** On the same 59
  sessions the mirror (each trade's bracket the other way, same bar, same costs) lost −0.16R
  ± 0.06, as much as the trades (−0.16R): trade − mirror = +0.01R ± 0.11 (t 0.0). By setup,
  multi-day reclaims are the only hint of a direction worth keeping (+0.05R vs −0.28R mirrored,
  difference +0.33R ± 0.19, t 1.7, not significant); gap fades did worse than their mirror
  (−0.21R vs −0.09R, t −0.7), and gap + reclaim setups lost either way (−0.34R vs −0.26R).
- **No demonstrated edge.** August's edge was never distinguishable from zero once costs and
  same-day clustering are counted, and the rule set tuned on August lost about 0.5R per trade
  relative to that in September.
- **Graded bar by bar, as the desk showed them, September's triggers clearly lost.** The old
  end-of-day search leaked: premarket fades were searched with the side the 09:30 open fixed
  later, and an end-of-day gap trigger hid an earlier multi-day reverse the desk had shown live.
  On the same bars the bar-by-bar search adds 147 trades (129 of them premarket multi-day
  reverses) and drops 8, at about the same mean (−0.17R vs −0.16R with the same 16:00 exit), now
  distinguishable from zero (t −2.8 vs −1.5). `causal_check.py` re-runs the old search on a
  cached fetch.
- **Equities now exit by the 16:00 close.** Held to the last after-hours bar, 116 of the 428
  trades end differently: thin after-hours prints fill their stops and targets, or they ride the
  evening. Here that cost about 0.03R per trade (−0.20R vs −0.17R). The forward ledger resolves
  with the same 16:00 exit.
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

#### Crypto (the 48 crypto names of `universe.txt`; this fetch had the right Yahoo 5m bars for 36)

No replay before v1.5.0 included crypto, yet the page graded it. `replay_crypto.py` runs the same
replay on Yahoo's 5m bars (the page's feed; about half its 5m crypto bars report zero volume),
complete ET days only, on sessions no rule was ever tuned on. Under ARB-USD, TON-USD and JUP-USD
Yahoo lists other tokens (ARbit, TON Token, a second Jupiter), so those are left out; a first
version of these results included them (4 v1.5.0 trades: 149 instead of 145, ± 0.12 instead of 0.13).

| sessions | engine · crypto session | bars | n | gross R | **net R ± SE** | verdict |
|---|---|---|--:|--:|--:|---|
| Sep 5 – Oct 3 | v1.4.1 · equity clock: 00:00–16:00 ET, gap from the prior 15:55 close · end-of-day trigger search | Yahoo | 196 | −0.40 | **−0.91 ± 0.17** | negative (t −5.3) |
| Sep 5 – Oct 3 | v1.4.1, graded bar by bar (the replay's search since Oct 4) | Yahoo | 245 | −0.24 | −0.75 ± 0.16 | negative (t −4.8) |
| **Sep 5 – Oct 3** | **v1.5.0 · the whole ET day, no gap fade** | Yahoo | 145 | +0.08 | **+0.01 ± 0.13** | inconclusive (t 0.0) |
| Sep 5 – Oct 3 | v1.5.0 rules with the day starting 00:00 UTC | Yahoo | 150 | −0.28 | −0.35 ± 0.10 | negative (t −3.4) |
| Sep 5 – Oct 3 | v1.4.1 with only its gap fade switched off | Yahoo | 137 | +0.09 | +0.01 ± 0.11 | inconclusive (t 0.1) |
| Sep 5 – Oct 3 | v1.4.1 · end-of-day trigger search | Coinbase, 43 names | 257 | −0.53 | −0.98 ± 0.12 | negative (t −8.3) |
| Sep 5 – Oct 3 | v1.5.0 | Coinbase, 43 names | 128 | +0.18 | +0.13 ± 0.17 | inconclusive (t 0.7) |
| **Sep 20 – Oct 18** | **v1.6.0 · mdrev to the nearest real level** | Yahoo, refetched Oct 19 | 76 | +0.38 | **+0.31 ± 0.22** | inconclusive (t 1.4) |
| Sep 20 – Oct 18 | v1.5.0 on the same bars | Yahoo | 180 | +0.30 | +0.24 ± 0.16 | inconclusive (t 1.6) |
| **Oct 23 – Oct 22 (a year)** | **v1.6.0**, 47 names ⁵ | Binance archive (real volume) | 661 | −0.07 | **−0.14 ± 0.06** | negative (t −2.3) |

⁵ `replay_crypto.py --source binance-vision --days 365`: Binance spot 5m klines from data.binance.vision
(checksum-verified archive files, the venue's own volume; Yahoo prints zero on about half its 5m crypto
bars). All 48 crypto names except MKR-USD, which has no USDT pair there. 365 complete UTC days ending
Oct 22, 2026, replayed on the engine's ET calendar day; 244 of the 365 sessions had a grade-A trade.

All Yahoo rows replay one fetch of 36 names; the two Coinbase rows share another (on Coinbase,
ARB-USD and TON-USD are Arbitrum and Toncoin). Without a gap fade no crypto trigger depends on later
bars, so the v1.5.0 rows (and v1.4.1 without its fade) come out the same under either trigger
search; the two v1.4.1 rows marked end-of-day can differ (the Yahoo one, graded bar by bar, is the
row below it).

What this says:

- **No edge on crypto in either version.** As graded through v1.4.1, crypto's grade-A triggers
  lost about 0.9R per trade after costs, on Yahoo and Coinbase bars alike, and still −0.75R graded
  bar by bar.
- **v1.4.1's crypto trades were an artifact of the equity clock.** 184 of 196 were gap fades and
  135 fired in the hour after midnight ET, minutes after blue reset, fading the move since the prior
  15:55 ET close on stops a median 0.43% of price wide: the 0.15% round trip alone cost 0.51R.
- **v1.5.0 gives crypto the whole ET day and no gap fade.** A 24/7 market has no opening gap (a
  ≥ 0.15% step between consecutive 5m alt bars is routine), so only the multi-day reverse trades:
  +0.01R ± 0.13 on the same bars. The +0.91R ± 0.20 paired change is the fades going away, not the
  longer day: v1.4.1 with only its gap fade switched off scores the same.
- **Over a year, crypto's multi-day reclaims lose.** On Binance's own bars, v1.6.0's grade-A
  crypto triggers (all multi-day reclaims) lost −0.14R ± 0.06 per trade (t −2.3, 661 trades,
  244 sessions): the one-month Yahoo windows above (+0.01R, then +0.31R) were noise around a
  negative mean. Longs did the damage (−0.27R ± 0.09, t −2.8, 299 trades); shorts were flat
  (−0.03R ± 0.08). The mirror lost −0.06R and trade − mirror is −0.07R ± 0.12: the direction
  call adds nothing here either. Monthly means ran from −0.39R to +0.11R with no run of two
  positive months, which is why a month at a time never settled it.
- **Even the break-even is fragile.** Starting the day at 00:00 UTC instead of 00:00 ET changes
  which reclaim is the day's first and where orange is anchored, and the same rules lose −0.35R
  (t −3.4). ET was chosen before either day was replayed, because the replay, the ledger and
  every label already run on it; the UTC row is a sensitivity check, not a selection. Coinbase bars
  (full volume) give +0.13R ± 0.17, and a Coinbase fetch that began at noon on Sep 4 rather than
  midnight gave −0.09R: half a day of history moves this result by 0.2R.
- **Coverage.** This fetch found no Yahoo 5m bars under 9 of the 48 symbols (MATIC, SUI, APT, UNI,
  PEPE, TAO, IMX, GRT, STX) and other tokens under 3 (ARB, TON, JUP), left out above. Yahoo lists all
  12 under other symbols (SUI20947-USD, ARB11841-USD, GRAM-USD for Toncoin, ...; MATIC is now
  POL-USD), which the desk and the page now ask for (`providers.YAHOO_CRYPTO_SYMBOLS`). The local
  desk grades crypto on exchange candles instead (see [History / RVOL](#history--rvol)).

Treat the desk as a research scanner. The forward ledger (`ledger.py`) records every live
grade-A trigger as it is shown and scores it after the close; trust a grade, gate or learned
filter only once months of that record agree.

Re-score any saved replay without refetching: `python3 replay_sessions.py --rescore <file>`.
Raw JSON: [`research/replay_2026-08-12.json`](research/replay_2026-08-12.json) (August,
trigger-close fills) · [`research/replay_2026-10-04.json`](research/replay_2026-10-04.json)
(September, next-bar fills) · [`research/replay_2026-10-04_v1.4.1.json`](research/replay_2026-10-04_v1.4.1.json)
(September, fixed engine; `method.paired_v1_4_0_same_bars` holds the old engine on the same bars) ·
[`research/replay_2026-10-04_v1.5.0.json`](research/replay_2026-10-04_v1.5.0.json) (September
refetched; `method.paired_v1_4_1_same_bars`, `method.causal_check`) ·
[`research/replay_2026-10-04_v1.5.0_causal.json`](research/replay_2026-10-04_v1.5.0_causal.json)
(September, bar by bar with the 16:00 exit, all 96 names; `method.causal_check`: the old end-of-day
search on the same bars) · crypto:
[`research/replay_crypto_2026-10-04_v1.4.1.json`](research/replay_crypto_2026-10-04_v1.4.1.json)
and [`research/replay_crypto_2026-10-04_v1.5.0.json`](research/replay_crypto_2026-10-04_v1.5.0.json)
(`method.paired_v1_4_1_same_bars`, `method.causal_check`, `method.sensitivity`: UTC day, no gap
fade, Coinbase bars) · v1.6.0: [`research/replay_2026-10-19_v1.6.0.json`](research/replay_2026-10-19_v1.6.0.json)
and [`research/replay_crypto_2026-10-19_v1.6.0.json`](research/replay_crypto_2026-10-19_v1.6.0.json)
(`method.paired_v1_5_0_same_bars`) · v1.7.0: [`research/replay_2026-10-19_v1.7.0.json`](research/replay_2026-10-19_v1.7.0.json)
(reclaims only on the 60-day bars, `method.paired_v1_6_0_same_bars` with the capped fades) and
[`research/replay_crypto_2026-10-22_365d_binance_v1.7.0.json`](research/replay_crypto_2026-10-22_365d_binance_v1.7.0.json)
(the same crypto trades as v1.6.0: crypto has no gap fade) · the target rules:
[`research/md_target_check_2026-10-19.json`](research/md_target_check_2026-10-19.json). A replay
never overwrites an existing snapshot for the same day.

#### Breakout or fade? (`direction_check.py`, Jul 28 – Oct 19, 59 sessions)

At the engine's first band break (2,782 events) and its first fakeout (903), the same event was
traded both ways: with the gap's extension (breakout) and against it (fade), next-bar fill, exits
at 30 / 60 min, the close, or a 1 ATR stop / 1.5 ATR target. **Neither direction wins.** The paired
breakout-minus-fade difference is inside the noise in 31 of 32 comparisons (|t| ≤ 1.8), and both
sides lose after costs: a round trip at a one-ATR 5m stop costs about 0.16R. The one exception,
holding a breakout to the close after a fakeout in a trend or mixed regime (+1.38R vs the fade,
t 2.1, 232 events), is what one test in 32 shows by chance. It is recorded here as a hypothesis
for the forward ledger, not a rule. Raw numbers:
[`research/direction_check_2026-10-19.json`](research/direction_check_2026-10-19.json).

Same-day leftover-bar scans (`backtest_today_scans.py`) print results on a handful of trades
from one session; they are not expectancy.

## History / RVOL

- Free Yahoo **1m** hard-cap is **8 calendar days** per request (`10d` rejects). Default fetch is **`8d`** (was `5d`).
- Coarser bars use a longer free window (`5m`/`15m` → `1mo` via `bars_range_for_interval`).
- RVOL uses **all prior sessions** in the window (cap 7), not a hard 4, and surfaces **`rvol_n`** / **`session_n`** so thin samples stay visible.
- yfinance uses **start/end** clamped to 8d for 1m so requests are not rejected or silently collapsed to period=`5d`.
- **Crypto** (desk and ledger): one exchange's 5m candles (OKX, then Binance, Bybit, Coinbase), paged
  back to ET midnight 8 days ago (`providers.CRYPTO_HISTORY_DAYS`), so RVOL has its 7 prior
  sessions. Each venue call returns only 200–350 bars, about a day; before, crypto RVOL never had a
  baseline. The series is cached per coin and each scan fetches only its tail, from the same venue:
  a series is never stitched from two venues' books. If that venue fails, its cache is served
  for up to 5 minutes, then the next venue builds a new series. Yahoo, under its own symbols, is the
  fallback over the same window. The ledger asks for more days when a pending crypto session is
  older, and leaves a session pending until the feed has a bar from the next ET day.
- A venue that has no such coin (OKX has no RUNE, Coinbase no TRX, Yahoo no SUI-USD) is skipped for
  that coin only. It used to go into the 45 s cooldown meant for a failing provider, and every later
  coin in the scan lost that venue: two back-to-back scans got bars for 34, then 29, of the desk's
  42 crypto names, from three or four different feeds.

## Layout

```
index.html          # GitHub Pages / shareable demo (runs engine.js in the browser)
engine.js           # browser port of engine.py + the desk's scan helpers (parity-tested)
cf-pages/           # identical copy of index.html + engine.js for Cloudflare Pages
static/index.html   # full local desk UI (served by app.py)
app.py              # FastAPI · live loop · :8791
engine.py           # dual VWAP + KER + grades (ENGINE_VERSION)
providers.py / data.py   # free feed rotation; crypto history pager + per-coin cache
backtest_today_scans.py · walkforward.py · replay_sessions.py
replay_crypto.py    # the same replay on the crypto names (Yahoo, Coinbase or Binance archive bars, paired engines)
binance_vision.py   # Binance's public bulk kline archive: monthly/daily zips, checksums, cache
causal_check.py     # the replay's old end-of-day trigger search, against its bar-by-bar one
evidence.py         # per-class record (n, net R, direction over mirror) from the banner's replays → page + /api/evidence
md_target_check.py  # mdrev target rules + missing-RVOL gate, same bars and a 60-day holdout
direction_check.py  # breakout vs fade at the gap states (first break, fakeout), paired, net of costs
honest.py           # cost-in-R and session-clustered standard errors (replay + ledger)
ledger.py           # forward signal ledger: record → resolve → report (data/signals/, gitignored)
tests/              # python3 -m unittest discover -s tests (engine parity needs node)
research/           # sample backtest / session-replay JSON
```

Research only. Free feeds delay. Not financial advice.
