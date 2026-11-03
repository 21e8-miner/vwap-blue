# External research scan, 2026-11-02

Four parallel surveys (GitHub repos, published gap/ORB/momentum evidence, minute-bar data vendors,
decision-time tape filters). What they say about VWAP Blue's own findings, and what to test next.

## What the outside evidence says about our results

- **Gap fades losing (−0.25R) is the base rate, not bad luck.** Aiche–Cohen–Griskin (2023, ~2,600 US
  stocks 2010–19): open-to-close drift *continues* the gap, +0.3–0.6% for full gaps. SPY fill rates
  fall from 78% (gaps 0.1–0.25%) to 26% (1–2%). NQ: gaps > 0.7× ATR14 fill 26%, > 1.2× fill 8%.
  Every open-source VWAP-band fade that charged costs came out flat or negative (FX study −0.33R/trade).
- **The continuation lead (gap > implied move: fade −0.42R, mirror +0.24R) matches the one published
  lift that survives costs:** Zarattini–Barbon–Aziz 2024, 7,000 stocks 2016–23: unfiltered 5-min ORB
  ≈ 0 net; by *opening relative volume* −0.02R (RVOL < 1) → +0.08R (> 1) → +0.38R (> 30). "Gap larger
  than the options expected" and "opening volume larger than usual" are the same object. No published
  out-of-sample test of this after 2023; replications of the QQQ version die at ~2.2¢/share slippage.
- **The month's P&L being index beta is the literature's market-intraday-momentum effect.** Gao–Han–Li–Zhou
  2018: SPY first half-hour predicts the last, R² 1.4% OOS, but 3.3% in the high-vol tercile and ~0 in
  the low-vol tercile. Baltussen et al. 2021: present only when dealers are net short gamma. Packaged
  versions ("Beat the Market") run Sharpe 0.4 out of sample since 2024.
- **Stock-level winners do not carry into the last half hour** (Baltussen–Da–Soebhag 2025: end-of-day
  reversal, driven by intraday losers, t > 10). Continuation trades should be flat by ~15:25, not 16:00.
- **Earnings-day continuation is mostly gone for mega-caps after 2016** (Christensen et al. 2026), but
  the open carries only ~71% of the shock in names with little after-hours trading (Santosh 2016).

## Five replays to run, in order

1. **RVOL-gated continuation (the ZBA rule on our bars).** RVOL = first-5-min volume / 14-day mean of
   that bar; |gap| ≥ 0.7× ATR14; direction = first 5-min candle; entry stop at its high/low until
   10:30; stop 0.10× ATR14; flat 15:25. Report by RVOL bucket (<1, 1–3, 3–10, ≥10) with the mirror
   fade as control. Needs the 14-day first-bar volume history: we have it in the saved bars.
2. **Gap/implied × RVOL.** Partition the existing gap trades by gap/implied daily move {<0.5, 0.5–1,
   1–2, >2} crossed with RVOL {<1, 1–3, >3}. Tests whether our +0.24R lead is the RVOL effect in disguise.
3. **Opening-vol gate + SPY sign.** σ₁ = SPY realized vol 09:30–10:00 ranked vs trailing 60 days;
   m₁₀ = SPY 10:00 / prior close − 1. Hypothesis: the hedged-vs-unhedged gap in grade A (+0.23R → −0.07R)
   concentrates in the top σ₁ tercile and in trades against the sign of m₁₀.
4. **Breadth at the trigger.** AD_t = share of the 485 names above vs below prior close at the trigger
   bar; |AD| ≥ 0.6 = one-sided tape. Hypothesis: counter-trend reclaims/fades on |AD| ≥ 0.6 bars are
   the beta losers. Free from bars we already hold.
5. **Small-gap, low-RVOL fade only.** |gap| 0.15–0.5× ATR14, RVOL < 1, fade to prior close, stop
   0.5× ATR beyond the open, flat 12:00. If the fade cannot win here, retire it.

## Data: breaking the 60-day ceiling

Every result above is on ~21 sessions; the literature's effects are 1–3% R², which needs hundreds.
- **Alpaca Basic, $0**: historical SIP 1-minute bars back to 2016 with 4:00 pre-market, 200 req/min,
  10k bars/page. IEX feed is ~2.5% of volume: never use it for RVOL or $-vol floors. No licence text
  found on publishing derived research.
- **FirstRate Data (perpetual, explicit "published research" permission, delisted names, pre-market)**
  — bundle price hidden on page (~$400 per snippet); **MarketParquet $79 one-time** (1m from 2000,
  pre-market, delisted; licence says personal/research, no explicit publishing clause).
- **Databento**: $125 free credits probably cover 500 names × 2y of 1-min; redistribution allowed after
  24h for most datasets. XNAS.ITCH is Nasdaq-only volume.
- **Avoid Massive/Polygon individual plans** for an open-source project: display-only, no derived works,
  delete on termination. Stooq is now behind a JS proof-of-work; Yahoo is rate-limiting yfinance.
- Gotchas: use unadjusted/split-only prices (dividend-adjusted intraday breaks $-vol and gap %);
  pre-market bars exist only when a trade printed; prior session's last RTH bar is the 15:55 5-min bar.

## Method borrowings from GitHub

- Placebo controls and a slippage sweep (giovannibrusco/zarattini-2023-orb-qqq): rerun grade A with a
  random filter of the same selection rate; report the slippage at which net R crosses zero.
- Top-k concentration (dantecoderr/orb-strategy-research): share of net R from the best 10 trades.
- Sealed holdout with a peek counter (yelgabo/btest); Deflated Sharpe across filter configs
  (francesco-nicolo/intraday-momentum-replication); re-centred bootstrap of mean R
  (The-Pranay-Pandey/vwap-mean-reversion).
- Nothing found that already does session-clustered SEs or beta-neutral intraday attribution:
  our replay is ahead of the open-source field on method, behind on data.
