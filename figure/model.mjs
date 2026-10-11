// The gap-fade path of VWAP Blue's engine (engine.py _day_start / _step_bar / _resolve_open, engine.js
// stepBar), as a pure per-bar model for the figure. figure/parity.mjs proves it against engine.js on the
// page's own bars. Everything the figure moves or prints comes from here.

export const ANCHOR_M = 4 * 60;
export const OPEN_M = 9 * 60 + 30;
export const CLOSE_M = 16 * 60;
export const K = 2;
export const ATR_MULT = 0.35;
export const GAP_MIN = 0.35;
export const SIGMA_MULT = 0.15;
export const KER_LOOKBACK = 20;
export const KER_TREND = 0.55;
export const KER_CHOP = 0.30;
export const LATE_CUT_M = 14 * 60 + 30;

export const tp = (b) => (b.h + b.l + b.c) / 3;

export function prep(raw, etOffsetMin = 240) {
  return raw.map((r) => {
    const local = r.ts - etOffsetMin * 60000;
    const mins = Math.floor((local % 86400000) / 60000);
    const d = new Date(local).toISOString().slice(0, 10);
    return { ts: r.ts, d, mins, o: r.o, h: r.h, l: r.l, c: r.c, v: Math.max(0, r.v), hlReal: r.h > r.l };
  });
}

export function ker(bars, end, lookback = KER_LOOKBACK) {
  if (end < 1 || lookback < 3) return null;
  const start = Math.max(0, end - lookback + 1);
  if (end - start + 1 < 5) return null;
  const closes = [];
  for (let i = start; i <= end; i++) if (bars[i].c && bars[i].c > 0) closes.push(bars[i].c);
  if (closes.length < 5) return null;
  const net = Math.abs(closes[closes.length - 1] - closes[0]);
  let path = 0;
  for (let i = 1; i < closes.length; i++) path += Math.abs(closes[i] - closes[i - 1]);
  if (path <= 0) return 0;
  return Math.max(0, Math.min(1, net / path));
}

export const adaptiveSigmaMult = (k, base = 1) => (k == null ? base : base * (1 / Math.max(0.35, 0.5 + k)));
export const regimeOf = (k) => (k == null ? "unknown" : k >= KER_TREND ? "trend" : k <= KER_CHOP ? "chop" : "mixed");

export function atr(bars, end, period = 14) {
  const trs = [];
  let prev = null;
  for (let i = Math.max(0, end - 40); i <= end; i++) {
    const b = bars[i];
    if (!b.hlReal) continue;
    trs.push(prev == null ? b.h - b.l : Math.max(b.h - b.l, Math.abs(b.h - prev), Math.abs(b.l - prev)));
    prev = b.c;
  }
  if (trs.length < period) return null;
  let a = 0;
  for (let i = 0; i < period; i++) a += trs[i];
  a /= period;
  for (let i = period; i < trs.length; i++) a = (a * (period - 1) + trs[i]) / period;
  return a;
}

export function priorRthClose(bars, i0) {
  if (i0 <= 0) return bars[0].c;
  const prior = bars[i0 - 1].d;
  for (let i = i0 - 1; i >= 0; i--) { const b = bars[i]; if (b.d !== prior) break; if (b.mins >= OPEN_M && b.mins < CLOSE_M) return b.c; }
  for (let i = i0 - 1; i >= 0; i--) { const b = bars[i]; if (b.d !== prior) break; if (b.mins < CLOSE_M) return b.c; }
  return bars[i0 - 1].c;
}

/** The whole tape, stepped: one frozen state per bar of the focus day, as the desk would have seen it. */
export function walk(bars) {
  const d0 = bars[bars.length - 1].d;
  const i0 = bars.findIndex((b) => b.d === d0);
  const p0 = bars.findIndex((b) => b.d === bars[i0 - 1].d);
  const priorClose = priorRthClose(bars, i0);
  const openIdx = bars.findIndex((b, i) => i >= i0 && b.mins >= OPEN_M);
  const ob = bars[openIdx];
  const gapPct = ((ob.o ?? ob.c) - priorClose) / priorClose * 100;
  const direction = gapPct >= 0 ? 1 : -1;
  const devOk = Math.abs(gapPct) >= GAP_MIN;

  const acc = { bp: 0, bv: 0, bp2: 0, op: 0, ov: 0 };
  for (let i = p0; i < i0; i++) {
    const b = bars[i];
    if (b.mins >= ANCHOR_M && b.mins < CLOSE_M && b.v > 0) { acc.op += tp(b) * b.v; acc.ov += b.v; }
  }
  const st = { phase: "SIDE", run: 0, fake: 0, firstBreak: null, trig: null, tagged: null, stopped: null,
    entry: null, stopPx: null, target: null, riskPct: null, runwayPct: null, R: null, noRunway: false,
    blue: null, sigma: null, orange: acc.ov > 0 ? acc.op / acc.ov : null, late: false };
  const steps = [];
  for (let i = i0; i < bars.length; i++) {
    const b = bars[i];
    const a = atr(bars, i);
    if (b.mins >= ANCHOR_M && b.mins < CLOSE_M && b.v > 0) {
      const t = tp(b);
      acc.bp += t * b.v; acc.bv += b.v; acc.bp2 += t * t * b.v; acc.op += t * b.v; acc.ov += b.v;
    }
    if (acc.bv > 0) { st.blue = acc.bp / acc.bv; st.sigma = Math.sqrt(Math.max(acc.bp2 / acc.bv - st.blue * st.blue, 0)); }
    if (acc.ov > 0) st.orange = acc.op / acc.ov;
    const kerNow = ker(bars, i);
    const adapt = adaptiveSigmaMult(kerNow, 1);
    let band = 0;
    if (st.sigma != null && st.sigma > 0) band = st.sigma * SIGMA_MULT * adapt;
    let beyond = false;
    const eligible = devOk && st.blue != null && b.mins < CLOSE_M;
    if (eligible) {
      if (st.trig == null) {
        beyond = direction > 0 ? b.c < st.blue - band : b.c > st.blue + band;
        if (beyond) {
          if (st.phase !== "BEYOND") { st.phase = "BEYOND"; st.run = 1; if (st.firstBreak == null) st.firstBreak = i; }
          else st.run += 1;
          if (st.run >= K) {
            st.trig = i; st.entry = b.c;
            let buf = 0;
            if (ATR_MULT > 0 && a) buf = a * ATR_MULT * adapt;
            else if (st.sigma != null) buf = st.sigma * 0.35 * adapt;
            st.stopPx = st.blue + direction * buf;
            st.target = st.orange;
            st.riskPct = st.entry ? Math.abs(st.entry - st.stopPx) / st.entry * 100 : null;
            st.late = b.mins >= LATE_CUT_M;
            if (st.target != null && st.entry) {
              st.runwayPct = direction > 0 ? (st.entry - st.target) / st.entry * 100 : (st.target - st.entry) / st.entry * 100;
              st.noRunway = st.runwayPct <= 0;
              if (st.riskPct && st.riskPct > 0 && !st.noRunway) st.R = st.runwayPct / st.riskPct;
            }
            resolveOpen(bars, i, st, direction);
          }
        } else {
          if (st.phase === "BEYOND") st.fake += 1;
          st.phase = "SIDE"; st.run = 0;
        }
      } else if (st.tagged == null && st.stopped == null) {
        resolveOpen(bars, i, st, direction);
      }
    }
    // the desk's regime gate (engine.js analyze): a pure gap fade whose plan is still open is blocked while
    // the last 20 closes trend (KER ≥ 0.55); the row then shows WATCH and no plan
    const regimeBlock = st.trig != null && st.tagged == null && st.stopped == null && regimeOf(kerNow) === "trend";
    steps.push({
      i, mins: b.mins, regimeBlock, close: b.c, high: b.h, low: b.l, volume: b.v, tp: tp(b), rth: b.mins >= OPEN_M,
      blue: st.blue, sigma: st.sigma, orange: st.orange, ker: kerNow, adapt, band, regime: regimeOf(kerNow),
      beyond, run: st.trig == null ? st.run : K, fake: st.fake,
      trig: st.trig, entry: st.entry, stopPx: st.stopPx, target: st.target, R: st.R, late: st.late,
      tagged: st.tagged, stopped: st.stopped, atr: a,
    });
  }
  return { i0, p0, priorClose, gapPct, direction, devOk, steps, priorMass: acc.ov && steps.length ? null : null };
}

function resolveOpen(bars, i, st, direction) {
  const b = bars[i];
  if (st.orange != null && b.hlReal) {
    const hit = direction > 0 ? b.l <= st.orange : b.h >= st.orange;
    if (hit) { st.tagged = i; return; }
  }
  if (st.stopPx != null && st.trig != null && i > st.trig) {
    const out = direction > 0 ? b.c > st.stopPx : b.c < st.stopPx;
    if (out) st.stopped = i;
  }
}

/** Volume by price bin: the slotted weights on each hanger, in bar order (first bar at the bottom). */
export function profile(bars, from, to, binWidth) {
  const bins = new Map();
  for (let i = from; i < to; i++) {
    const b = bars[i];
    if (!(b.mins >= ANCHOR_M && b.mins < CLOSE_M && b.v > 0)) continue;
    const key = Math.round(tp(b) / binWidth);
    if (!bins.has(key)) bins.set(key, []);
    bins.get(key).push({ i, v: b.v });
  }
  return bins;
}

export const hhmm = (mins) => `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;
