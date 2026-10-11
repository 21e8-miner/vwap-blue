// A seeded synthetic two-session tape for the figure: one prior session (orange's seed) and a gap-up
// day that fades through blue. Deterministic, so the built page and the parity check see the same bars.
// Times are ET; the engine's prepBars reads them from epoch ms.

const ET_OFFSET_MIN = 4 * 60;   // EDT in October
const OPEN_MIN = 9 * 60 + 30;

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const etMs = (y, mo, d, mins) => Date.UTC(y, mo - 1, d, 0, (mins + ET_OFFSET_MIN));

function session(rnd, day, start, { open, drift, noise, volume, premarket = true }) {
  const bars = [];
  let c = open;
  const first = premarket ? 4 * 60 : 9 * 60 + 30;
  for (let m = first; m < 16 * 60; m += 5) {
    const rth = m >= 9 * 60 + 30;
    const t = rth ? (m - 570) / 390 : 0;
    const d = rth ? drift(t) : 0;
    const n = (rnd() - 0.5) * 2 * noise * (rth ? 1 : 0.35);
    const o = c;
    c = +(o + d + n).toFixed(2);
    const wick = noise * (rth ? 0.9 : 0.3);
    const h = +(Math.max(o, c) + rnd() * wick).toFixed(2);
    const l = +(Math.min(o, c) - rnd() * wick).toFixed(2);
    const v = Math.round(rth ? volume(t) * (0.75 + rnd() * 0.5) : 3000 + rnd() * 4000);
    bars.push({ ts: etMs(day[0], day[1], day[2], m), o, h, l, c, v });
  }
  return bars;
}

export function tape(seed = 7) {
  const rnd = mulberry32(seed);
  const prior = session(rnd, [2026, 10, 8], 0, {
    open: 99.85,
    drift: (t) => (t < 0.5 ? 0.008 : -0.003),          // a quiet up-and-flat day that closes near 100.0
    noise: 0.04,
    volume: (t) => 60000 * Math.exp(-4 * t) + 24000 + 40000 * Math.max(0, t - 0.85) * 6,
  });
  const today = session(rnd, [2026, 10, 9], 0, {
    open: 100.85,                                        // about +0.85 % on the prior close
    drift: (t) => {
      if (t < 0.08) return 0.055;                        // the first 30 minutes push higher on heavy volume
      if (t < 0.5) return -0.042;                        // then the gap fades through blue
      return 0.003;                                      // and the afternoon drifts
    },
    noise: 0.045,
    volume: (t) => 90000 * Math.exp(-5 * t) + 28000 + 50000 * Math.max(0, t - 0.85) * 6,
  });
  // the gap day's premarket is a quiet, thin drift up to the open (100.62 → 100.82): a 24-hour tape
  // would show the gap there already, and the desk takes signals in the premarket too, but this
  // session's point is the regular-hours fade
  const pre = today.filter((b) => b.ts < etMs(2026, 10, 9, OPEN_MIN));
  pre.forEach((b, j) => {
    const c = +(100.62 + (0.2 * (j + 1)) / pre.length).toFixed(2);
    const o = j ? pre[j - 1].c : 100.6;
    b.o = o; b.c = c; b.h = +(Math.max(o, c) + 0.01).toFixed(2); b.l = +(Math.min(o, c) - 0.01).toFixed(2);
    b.v = 3000 + Math.round(rnd() * 4000);
  });
  return [...prior, ...today];
}
