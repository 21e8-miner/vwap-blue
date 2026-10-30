/* contract.js — the options equivalent of a VWAP Blue bracket, from a Moontower option chain.
   A long plan becomes a call vertical: buy the call struck nearest the entry, sell the call at the
   first strike at or beyond the target (a reclaim's target often sits inside one strike step). A short plan becomes the mirror put vertical. The expiry is the first one that
   ends after the session (a day trade wants the shortest tenor that is not expiring today). Prices
   are the chain's end-of-day quotes: debit = long ask − short bid (worst), mid − mid (fair); the
   spread's value at the target is the strike width, so its reward:risk is (width − debit) / debit.
   Pure, so tests/test_contract.py can run it under node; the page loads it as window.VWAPContract. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.VWAPContract = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const num = x => (x == null || Number.isNaN(+x) ? null : +x);

  // first listed expiry strictly after `today` (YYYY-MM-DD, ET); null when the chain has none
  function pickExpiry(chain, today) {
    const after = [...new Set(chain.map(r => r.expiry_date).filter(e => e && e > today))].sort();
    return after.length ? after[0] : null;
  }

  function nearestStrike(rows, price) {
    let best = null;
    for (const r of rows) {
      const s = num(r.strike);
      if (s == null) continue;
      if (!best || Math.abs(s - price) < Math.abs(num(best.strike) - price) ||
          (Math.abs(s - price) === Math.abs(num(best.strike) - price) && s < num(best.strike))) best = r;
    }
    return best;
  }

  // plan: { side: "long"|"short", entry, stop, target }; chain: /v1/optionchain rows for one ticker
  function bracketContract(chain, plan, today) {
    if (!plan || !plan.side || plan.entry == null || plan.target == null) return { error: "no plan" };
    if (!chain || !chain.length) return { error: "no chain" };
    const expiry = pickExpiry(chain, today);
    if (!expiry) return { error: "no expiry after today" };
    const rows = chain.filter(r => r.expiry_date === expiry);
    // long leg at the strike nearest the entry; short leg at the first strike at or beyond the target in
    // the trade's direction (a reclaim's target is often inside one strike step), else the nearest
    const long = nearestStrike(rows, +plan.entry);
    const beyond = rows.filter(r => num(r.strike) != null && (plan.side === "long" ? num(r.strike) >= +plan.target : num(r.strike) <= +plan.target));
    const short = beyond.length ? nearestStrike(beyond, +plan.target) : nearestStrike(rows, +plan.target);
    if (!long || !short) return { error: "no strikes" };
    const call = plan.side === "long", leg = call ? "call" : "put";
    const longAsk = num(long[`${leg}_ask_price`]), longMid = num(long[`${leg}_mid_price`]), longBid = num(long[`${leg}_bid_price`]);
    const shortBid = num(short[`${leg}_bid_price`]), shortMid = num(short[`${leg}_mid_price`]);
    const width = Math.abs(num(short.strike) - num(long.strike));
    const sameStrike = num(short.strike) === num(long.strike);
    const debitAsk = longAsk != null && shortBid != null ? +(longAsk - shortBid).toFixed(2) : null;
    const debitMid = longMid != null && shortMid != null ? +(longMid - shortMid).toFixed(2) : null;
    const rr = d => (d != null && d > 0 && width > 0 ? +((width - d) / d).toFixed(2) : null);
    return {
      expiry, dte: num(long.dte), trade_date: long.trade_date || null, spot: num(long.spot_price),
      kind: call ? "call vertical" : "put vertical", side: plan.side,
      long: { strike: num(long.strike), bid: longBid, mid: longMid, ask: longAsk, delta: num(long[`${leg}_delta`]), iv: num(long[`${leg}_mid_iv`]),
              volume: num(long[`${leg}_volume`]), open_interest: num(long[`${leg}_open_interest`]) },
      short: { strike: num(short.strike), bid: shortBid, mid: shortMid, delta: num(short[`${leg}_delta`]) },
      width, same_strike: sameStrike,
      debit_ask: sameStrike ? null : debitAsk, debit_mid: sameStrike ? null : debitMid,
      rr_ask: sameStrike ? null : rr(debitAsk), rr_mid: sameStrike ? null : rr(debitMid),
      stock_rr: plan.stop != null && plan.entry !== plan.stop ? +(Math.abs(plan.target - plan.entry) / Math.abs(plan.entry - plan.stop)).toFixed(2) : null,
      fill: fillability(longBid, longAsk, sameStrike ? longAsk : debitAsk, num(long[`${leg}_open_interest`]), num(long[`${leg}_volume`])),
    };
  }

  // can the spread be filled near its quote? the long leg's market as a share of what you pay, and whether anyone trades it
  function fillability(bid, ask, cost, openInterest, volume, maxSpreadShare = 1 / 3) {
    if (bid == null || ask == null || cost == null || cost <= 0) return { ok: null, why: "no quote" };
    const spread = +(ask - bid).toFixed(2), share = +(spread / cost).toFixed(2);
    if (ask <= 0 || bid <= 0) return { ok: false, spread, share, why: "no bid" };
    if (share > maxSpreadShare) return { ok: false, spread, share, why: `market ${spread.toFixed(2)} wide on a ${cost.toFixed(2)} cost` };
    if ((openInterest || 0) === 0 && (volume || 0) === 0) return { ok: false, spread, share, why: "no open interest, no volume" };
    return { ok: true, spread, share, why: "" };
  }

  // a stock position's index hedge from /v1/hedge-ratios: beta-weighted dollars of the hedge ticker, as shares
  // hr: the endpoint's row (beta, correlation, vol_core, vol_hedge, idio_vol_remaining for its notional);
  // shares: the core position; price, hedgePrice: the two prices
  function indexHedge(hr, shares, price, hedgePrice) {
    const beta = Number(hr && hr.beta);
    if (!Number.isFinite(beta) || !shares || !price || !hedgePrice) return null;
    const notional = shares * price, hedgeDollars = -beta * notional;
    return { beta: +beta.toFixed(2), correlation: hr.correlation == null ? null : +(+hr.correlation).toFixed(2),
             notional: +notional.toFixed(0), hedge_dollars: +hedgeDollars.toFixed(0),
             hedge_shares: +(hedgeDollars / hedgePrice).toFixed(1),
             idio_share: hr.idio_vol_remaining != null && hr.notional_position ? +(hr.idio_vol_remaining / hr.notional_position).toFixed(3) : null };
  }

  // keep only what the page needs, so a day's chains fit in localStorage
  function trimChain(chain, today, maxDte = 45, band = 0.15) {
    const keys = ["ticker", "trade_date", "expiry_date", "dte", "strike", "spot_price",
      "call_bid_price", "call_mid_price", "call_ask_price", "call_delta", "call_mid_iv", "call_volume", "call_open_interest",
      "put_bid_price", "put_mid_price", "put_ask_price", "put_delta", "put_mid_iv", "put_volume", "put_open_interest"];
    return chain.filter(r => r.expiry_date > today && (num(r.dte) == null || num(r.dte) <= maxDte) &&
                             (num(r.spot_price) == null || Math.abs(num(r.strike) / num(r.spot_price) - 1) <= band))
                .map(r => Object.fromEntries(keys.map(k => [k, r[k]])));
  }

  return { pickExpiry, nearestStrike, bracketContract, trimChain, fillability, indexHedge };
});
