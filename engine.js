/*
 * VWAP Blue engine for the static demo: a line-by-line port of engine.py (analyze, the stale-bar
 * guard) plus the scan helpers the desk uses around it (data.py session $ volume and rotation
 * score, app.py grade floor, honest.py cost in R).
 *
 * The replay and the forward ledger evaluate engine.py, so this file must grade the same bars the
 * same way. tests/test_engine_parity.py runs both engines on identical bars and compares every
 * field; change the two together.
 *
 * Browser: <script src="engine.js"> defines window.VWAPBlue.  Node: require("./engine.js").
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VWAPBlue = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const VERSION = "1.5.0";   // engine.ENGINE_VERSION
  const RTH_OPEN_M = 9 * 60 + 30;
  const RTH_CLOSE_M = 16 * 60;
  const DEFAULT_ANCHOR_M = 4 * 60;
  const DEFAULT_K = 2;
  const DEFAULT_ATR_MULT = 0.35;
  const DEFAULT_GAP_MIN = 0.35;
  const DEFAULT_RMIN = 1.0;
  const DEFAULT_RVOL_MIN = 1.2;
  const LATE_CUT_M = 14 * 60 + 30;
  // crypto: the whole ET day is the session and there is no gap fade (engine.CRYPTO_CLOSE_M)
  const CRYPTO_CLOSE_M = 24 * 60;
  const CRYPTO_LATE_CUT_M = CRYPTO_CLOSE_M - 90;
  const DEFAULT_MD_MIN_EXT = 8;
  const DEFAULT_MD_MIN_DIST = 0.25;
  const DEFAULT_SIGMA_MULT = 0.15;
  const DEFAULT_KER_LOOKBACK = 20;
  const DEFAULT_KER_TREND = 0.55;
  const DEFAULT_KER_CHOP = 0.30;
  const DEFAULT_RVOL_MIN_N = 2;
  const RVOL_MAX_PRIORS = 7;
  const RVOL_MIN_PRIORS = 2;
  const DEFAULT_MAX_BAR_AGE_MIN = 15.0;
  const COST = { equity: 0.0008, crypto: 0.0015 };   // round trip, fraction of price (honest.py)
  const GRADE_RANK = { A: 5, LA: 4, B: 3, LB: 2, C: 1, "✕": 0, "–": -1 };

  // ── US/Eastern clock (engine._to_et) ─────────────────────────────────────
  // US DST switches on a whole UTC hour, so one offset per UTC hour is exact.
  const ET_FMT = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hourCycle: "h23",
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
  const offsetCache = new Map();
  function etOffsetMs(ms) {
    const hour = Math.floor(ms / 3600000);
    let off = offsetCache.get(hour);
    if (off === undefined) {
      const p = {};
      for (const part of ET_FMT.formatToParts(new Date(hour * 3600000))) p[part.type] = part.value;
      off = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute) - hour * 3600000;
      offsetCache.set(hour, off);
    }
    return off;
  }
  function etParts(ms) {
    const d = new Date(ms + etOffsetMs(ms));
    return {
      y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, day: d.getUTCDate(),
      h: d.getUTCHours(), mi: d.getUTCMinutes(),
      wd: (d.getUTCDay() + 6) % 7,   // Monday = 0, as Python's weekday()
    };
  }
  const pad2 = x => String(x).padStart(2, "0");

  // Python round(x, nd) for display fields (differs only on exact binary ties).
  function pyRound(x, nd) { return Number(Number(x).toFixed(nd)); }
  const roundPx = x => pyRound(x, x < 10 ? 4 : 2);

  function isCrypto(ticker) {
    const t = String(ticker || "").toUpperCase();
    if (t.endsWith("-USD") || t.endsWith("-USDT") || t.endsWith("-USDC")) return true;
    const bare = t.replace(/-/g, "");
    return (bare.endsWith("USDT") || bare.endsWith("USDC")) && bare.length >= 6;
  }

  function sessionLabel(crypto, nowMs) {
    if (crypto) return { session_state: "crypto", rth_open: true, session_label: "24/7 crypto" };
    const p = etParts(nowMs);
    const mins = p.h * 60 + p.mi;
    const isWd = p.wd < 5;
    const rth = isWd && RTH_OPEN_M <= mins && mins < RTH_CLOSE_M;
    let label;
    if (!isWd) label = "weekend";
    else if (mins < RTH_OPEN_M) label = "premarket";
    else if (mins >= RTH_CLOSE_M) label = "afterhours";
    else label = "rth";
    return {
      session_state: rth ? "open" : "closed",
      rth_open: rth,
      session_label: label,
      signal_window: isWd && mins < RTH_CLOSE_M,
    };
  }

  /** raw: [{ts (ms, bar open), o, h, l, c, v}] oldest first → engine bars (engine._prep_bars). */
  function prepBars(raw) {
    const bars = [];
    for (const r of raw || []) {
      if (r == null || r.o == null || r.h == null || r.l == null || r.c == null) continue;
      const o = +r.o, h = +r.h, l = +r.l, c = +r.c;
      if (Number.isNaN(o) || Number.isNaN(h) || Number.isNaN(l) || Number.isNaN(c)) continue;
      let v = r.v == null ? 0 : +r.v;
      if (Number.isNaN(v)) v = 0;
      const p = etParts(+r.ts);
      bars.push({
        ts: +r.ts,
        d: `${p.y}-${pad2(p.mo)}-${pad2(p.day)}`,
        mins: p.h * 60 + p.mi,
        o, h, l, c, v: Math.max(0, v),
        hlReal: h > l,
        time: `${pad2(p.mo)}-${pad2(p.day)} ${pad2(p.h)}:${pad2(p.mi)}`,
      });
    }
    return bars;
  }

  function sessions(bars) {
    const seen = new Set(), out = [];
    for (const b of bars) if (!seen.has(b.d)) { seen.add(b.d); out.push(b.d); }
    return out;
  }
  function firstIdx(bars, day) {
    for (let i = 0; i < bars.length; i++) if (bars[i].d === day) return i;
    return -1;
  }

  /**
   * Close of the prior session's last RTH bar (else its last bar before 16:00, else its last bar).
   * Crypto passes its 24h window (0, 24:00): the prior day's last bar.
   */
  function priorRthClose(bars, i0, openM = RTH_OPEN_M, closeM = RTH_CLOSE_M) {
    if (i0 <= 0) return bars[0].c;
    const priorDay = bars[i0 - 1].d;
    for (let i = i0 - 1; i >= 0; i--) {
      const b = bars[i];
      if (b.d !== priorDay) break;
      if (openM <= b.mins && b.mins < closeM) return b.c;
    }
    for (let i = i0 - 1; i >= 0; i--) {
      const b = bars[i];
      if (b.d !== priorDay) break;
      if (b.mins < closeM) return b.c;
    }
    return bars[i0 - 1].c;
  }

  function atr(bars, end, period = 14) {
    const trs = [];
    let prevC = null;
    for (let i = Math.max(0, end - 40); i <= end; i++) {
      const b = bars[i];
      if (!b.hlReal) continue;
      if (prevC == null) trs.push(b.h - b.l);
      else trs.push(Math.max(b.h - b.l, Math.abs(b.h - prevC), Math.abs(b.l - prevC)));
      prevC = b.c;
    }
    if (trs.length < period) return null;
    let a = 0;
    for (let i = 0; i < period; i++) a += trs[i];
    a /= period;
    for (let i = period; i < trs.length; i++) a = (a * (period - 1) + trs[i]) / period;
    return a;
  }

  /** Kaufman Efficiency Ratio on closes: |net move| / Σ|bar moves|. */
  function ker(bars, end, lookback = DEFAULT_KER_LOOKBACK) {
    if (end < 1 || lookback < 3) return null;
    const start = Math.max(0, end - lookback + 1);
    if (end - start + 1 < 5) return null;
    const closes = [];
    for (let i = start; i <= end; i++) if (bars[i].c && bars[i].c > 0) closes.push(bars[i].c);
    if (closes.length < 5) return null;
    const net = Math.abs(closes[closes.length - 1] - closes[0]);
    let path = 0;
    for (let i = 1; i < closes.length; i++) path += Math.abs(closes[i] - closes[i - 1]);
    if (path <= 0) return 0.0;
    return Math.max(0.0, Math.min(1.0, net / path));
  }

  function adaptiveSigmaMult(k, base = 1.0) {
    if (k == null) return base;
    return base * (1.0 / Math.max(0.35, 0.5 + k));
  }

  function regimeFromKer(k, trendTh = DEFAULT_KER_TREND, chopTh = DEFAULT_KER_CHOP) {
    if (k == null) return "unknown";
    if (k >= trendTh) return "trend";
    if (k <= chopTh) return "chop";
    return "mixed";
  }

  const tp = b => (b.h + b.l + b.c) / 3.0;

  function resolveDay(bars, i0, iN, p0, o) {
    const anchor = o.anchor_mins, openM = o.open_mins, closeM = o.close_mins;
    const acc = { bp: 0, bv: 0, bp2: 0, op: 0, ov: 0, vol: 0, trapV: 0 };
    for (let i = p0; i < i0; i++) {
      const b = bars[i];
      if (b.mins >= anchor && b.mins < closeM && b.v > 0) {
        const t = tp(b);
        acc.op += t * b.v;
        acc.ov += b.v;
      }
    }
    const priorClose = priorRthClose(bars, i0, openM, closeM);
    let openIdx = null;
    for (let i = i0; i <= iN; i++) if (bars[i].mins >= openM) { openIdx = i; break; }
    let gapPct, gapProvisional;
    if (openIdx != null) {
      const ob = bars[openIdx];
      gapPct = ((ob.o != null ? ob.o : ob.c) - priorClose) / priorClose * 100.0;
      gapProvisional = false;
    } else {
      gapPct = (bars[iN].c - priorClose) / priorClose * 100.0;
      gapProvisional = true;
    }
    const direction = gapPct >= 0 ? 1 : -1;
    const devOk = !!o.gap_fade && Math.abs(gapPct) >= o.gap_min;

    const st = {
      phase: "SIDE", run: 0, fake: 0, firstBreak: null, trig: null, tagged: null, stopped: null,
      entry: null, stopPx: null, target: null, riskPct: null, runwayPct: null, R: null, noRunway: false,
      blue: null, sigma: null, orange: null, blueTrail: [], orangeTrail: [], sigTrail: [], atr: null,
      late: false, _i0: i0,
      md_ext_side: 0, md_ext_run: 0, md_ext_extreme: null, md_max_dist: 0.0, md_trig: null,
      md_side: null, md_entry: null, md_stop: null, md_target: null, md_R: null, setup_mode: null,
    };

    for (let i = i0; i <= iN; i++) stepBar(bars, i, acc, st, direction, devOk, o);

    if (st.trig == null && st.md_trig != null) {
      // gap path never armed but the multi-day reverse did: promote the MD plan
      st.trig = st.md_trig;
      st.entry = st.md_entry;
      st.stopPx = st.md_stop;
      st.target = st.md_target;
      st.R = st.md_R;
      st.setup_mode = "mdrev";
      if (st.entry && st.stopPx) st.riskPct = Math.abs(st.entry - st.stopPx) / st.entry * 100.0;
      if (st.entry && st.target != null) {
        if (st.md_side === "long") st.runwayPct = (st.target - st.entry) / st.entry * 100.0;
        else st.runwayPct = (st.entry - st.target) / st.entry * 100.0;
        st.noRunway = st.runwayPct != null && st.runwayPct <= 0;
      }
      resolveMdFrom(bars, st.md_trig, iN, st);
    } else if (st.trig != null && st.md_trig != null) {
      const gapSide = direction > 0 ? "short" : "long";
      st.setup_mode = st.md_side === gapSide ? "both" : "gap";
    } else if (st.trig != null) {
      st.setup_mode = "gap";
    }

    return {
      acc, st, gap_pct: gapPct, gap_provisional: gapProvisional, dir: direction, dev_ok: devOk,
      prior_close: priorClose, open_idx: openIdx, md_side: st.md_side, setup_mode: st.setup_mode,
    };
  }

  function stepBar(bars, i, acc, st, direction, devOk, o) {
    const b = bars[i];
    const anchor = o.anchor_mins, closeM = o.close_mins;
    if (b.mins >= anchor && b.mins < closeM && b.v > 0) {
      const t = tp(b);
      acc.bp += t * b.v;
      acc.bv += b.v;
      acc.bp2 += t * t * b.v;
      acc.op += t * b.v;
      acc.ov += b.v;
    }
    if (b.mins < closeM) acc.vol += b.v;

    if (acc.bv > 0) {
      st.blue = acc.bp / acc.bv;
      st.sigma = Math.sqrt(Math.max(acc.bp2 / acc.bv - st.blue * st.blue, 0.0));
    }
    if (acc.ov > 0) st.orange = acc.op / acc.ov;

    st.blueTrail.push(st.blue);
    st.orangeTrail.push(st.orange);
    st.sigTrail.push(st.sigma);
    st.atr = atr(bars, i);

    // multi-day reverse: tracked whenever orange exists (no gap required)
    if (st.orange != null && b.mins < closeM && st.md_trig == null) trackMdReverse(bars, i, st, o);

    // Blueline gap path: signals only pre + RTH when the gap is armed
    const eligible = devOk && st.blue != null && b.mins < closeM;
    if (!eligible) return;

    if ((direction > 0 && b.c > st.blue) || (direction < 0 && b.c < st.blue)) acc.trapV += b.v;

    if (st.trig == null) {
      // adaptive beyond-threshold: volume-weighted σ × KER multiplier (chop wider, trend tighter)
      const kerNow = ker(bars, i, Math.trunc(o.ker_lookback));
      const adapt = adaptiveSigmaMult(kerNow, 1.0);
      let band = 0.0;
      if (st.sigma != null && st.sigma > 0) band = st.sigma * +o.sigma_mult * adapt;
      const beyond = direction > 0 ? b.c < st.blue - band : b.c > st.blue + band;
      if (beyond) {
        if (st.phase !== "BEYOND") {
          st.phase = "BEYOND";
          st.run = 1;
          if (st.firstBreak == null) st.firstBreak = i;
        } else {
          st.run += 1;
        }
        if (st.run >= o.K) {
          st.trig = i;
          st.entry = b.c;
          st._ker_at_trig = kerNow;
          st._adapt_at_trig = adapt;
          let buf = 0.0;
          if (o.atr_mult > 0 && st.atr) buf = st.atr * o.atr_mult * adapt;
          else if (st.sigma != null) buf = st.sigma * 0.35 * adapt;
          st.stopPx = st.blue + direction * buf;
          st.target = st.orange;
          st.riskPct = st.entry ? Math.abs(st.entry - st.stopPx) / st.entry * 100.0 : null;
          st.late = b.mins >= o.late_cut;
          if (st.target != null && st.entry) {
            if (direction > 0) st.runwayPct = (st.entry - st.target) / st.entry * 100.0;
            else st.runwayPct = (st.target - st.entry) / st.entry * 100.0;
            st.noRunway = st.runwayPct <= 0;
            if (st.riskPct && st.riskPct > 0 && !st.noRunway) st.R = st.runwayPct / st.riskPct;
          }
          resolveOpen(bars, i, st, direction);
        }
      } else {
        if (st.phase === "BEYOND") st.fake += 1;
        st.phase = "SIDE";
        st.run = 0;
      }
    } else if (st.tagged == null && st.stopped == null) {
      resolveOpen(bars, i, st, direction);
    }
  }

  /** Multi-day VWAP reverse: extend beyond orange, then reclaim it. */
  function trackMdReverse(bars, i, st, o) {
    const b = bars[i];
    const orange = st.orange;
    if (orange == null || orange <= 0) return;
    const minExt = Math.trunc(o.md_min_ext);
    const minDist = +o.md_min_dist;
    const side = b.c < orange ? -1 : (b.c > orange ? 1 : 0);
    const ext = st.md_ext_side || 0;
    const run = st.md_ext_run || 0;

    // reclaim: was extended on one side, now closes on the other side of orange
    if (ext !== 0 && side === -ext && run >= minExt && (st.md_max_dist || 0) >= minDist) {
      const mdSide = ext < 0 ? "long" : "short";
      const entry = b.c;
      let buf = 0.0;
      if ((o.atr_mult || 0) > 0 && st.atr) buf = st.atr * +o.atr_mult;
      else if (st.sigma != null) buf = st.sigma * 0.25;
      const extreme = st.md_ext_extreme;
      let stop, target;
      if (mdSide === "long") {
        stop = (extreme != null ? extreme : entry) - buf;
        if (st.blue != null && st.blue > entry) target = st.blue;
        else target = entry + Math.max(Math.abs(entry - stop) * 1.5, entry * 0.004);
      } else {
        stop = (extreme != null ? extreme : entry) + buf;
        if (st.blue != null && st.blue < entry) target = st.blue;
        else target = entry - Math.max(Math.abs(stop - entry) * 1.5, entry * 0.004);
      }
      const risk = Math.abs(entry - stop);
      const runway = target != null ? Math.abs(target - entry) : 0.0;
      st.md_trig = i;
      st.md_side = mdSide;
      st.md_entry = entry;
      st.md_stop = stop;
      st.md_target = target;
      st.md_R = risk > 0 ? runway / risk : null;
      st.late = b.mins >= o.late_cut;
      return;
    }

    // still / newly extended
    if (side !== 0 && (ext === 0 || ext === side)) {
      if (ext !== side) {
        st.md_ext_side = side;
        st.md_ext_run = 1;
        st.md_ext_extreme = side < 0 ? b.l : b.h;
        st.md_max_dist = Math.abs(b.c - orange) / orange * 100.0;
      } else {
        st.md_ext_run = run + 1;
        const prev = st.md_ext_extreme;
        if (side < 0) st.md_ext_extreme = prev == null ? b.l : Math.min(prev, b.l);
        else st.md_ext_extreme = prev == null ? b.h : Math.max(prev, b.h);
        const dist = Math.abs(b.c - orange) / orange * 100.0;
        st.md_max_dist = Math.max(st.md_max_dist || 0.0, dist);
      }
      return;
    }

    // lost the extension without a reclaim (oscillating on orange): soft reset
    if (side === 0 || (ext !== 0 && side !== ext && run < minExt)) {
      st.md_ext_side = side;
      st.md_ext_run = side !== 0 ? 1 : 0;
      if (side !== 0) {
        st.md_ext_extreme = side < 0 ? b.l : b.h;
        st.md_max_dist = Math.abs(b.c - orange) / orange * 100.0;
      } else {
        st.md_ext_extreme = null;
        st.md_max_dist = 0.0;
      }
    }
  }

  function resolveOpen(bars, i, st, direction) {
    const b = bars[i];
    const orange = st.orange;
    if (orange != null && b.hlReal) {
      const hit = direction > 0 ? b.l <= orange : b.h >= orange;
      if (hit) { st.tagged = i; return; }
    }
    if (st.stopPx != null && st.trig != null && i > st.trig) {
      const out = direction > 0 ? b.c > st.stopPx : b.c < st.stopPx;
      if (out) st.stopped = i;
    }
  }

  function resolveMdFrom(bars, iStart, iN, st) {
    const mdSide = st.md_side;
    if (!mdSide) return;
    for (let i = iStart; i <= iN; i++) {
      const b = bars[i];
      if (st.tagged != null || st.stopped != null) return;
      const tgt = st.target || st.md_target;
      const stop = st.stopPx || st.md_stop;
      if (tgt != null && b.hlReal) {
        const hit = mdSide === "long" ? b.h >= tgt : b.l <= tgt;
        if (hit) { st.tagged = i; return; }
      }
      if (stop != null && i > iStart) {
        const out = mdSide === "long" ? b.c < stop : b.c > stop;
        if (out) { st.stopped = i; return; }
      }
    }
  }

  function stateText(S, o) {
    const st = S.st;
    const mode = st.setup_mode;
    if (mode === "mdrev" || (!S.dev_ok && st.md_trig != null)) {
      const arrow = st.md_side === "long" ? "▲" : "▼";
      if (st.stopped != null) return { txt: "MD REV STOPPED ✕", cls: "err" };
      if (st.tagged != null) return { txt: `MD REV TAGGED ✓ ${arrow}`, cls: "tag" };
      if (st.trig != null || st.md_trig != null) {
        return { txt: `MD REV ${arrow} · reclaim orange${st.late ? " LATE" : ""}`, cls: "conf" };
      }
      return { txt: "MD REV SETUP", cls: "break" };
    }
    if (st.md_trig == null && (st.md_ext_run || 0) >= Math.floor(o.md_min_ext / 2)) {
      if ((st.md_ext_side || 0) < 0) return { txt: `MD EXT ▼ orange ${st.md_ext_run}b`, cls: "break" };
      if ((st.md_ext_side || 0) > 0) return { txt: `MD EXT ▲ orange ${st.md_ext_run}b`, cls: "break" };
    }
    if (!S.dev_ok) return { txt: S.gap_provisional ? "NO GAP (pre)" : "NO GAP", cls: "none" };
    if (st.trig == null) {
      if (st.firstBreak == null) {
        let gap = S.dir > 0 ? "GAP ▲ · above blue" : "GAP ▼ · below blue";
        if (S.gap_provisional) gap += " (pre)";
        return { txt: gap, cls: "gap" };
      }
      if (st.phase === "BEYOND") return { txt: `FIRST BREAK ${st.run}/${o.K}`, cls: "break" };
      return { txt: `FAKEOUT ×${st.fake}`, cls: "fake" };
    }
    if (st.noRunway) return { txt: "CONFIRMED · NO RUNWAY", cls: "none" };
    if (st.tagged != null) {
      let base = S.dir > 0 ? "TAGGED ✓ short" : "TAGGED ✓ long";
      if (mode === "both") base = "MD+GAP " + base;
      return { txt: base, cls: "tag" };
    }
    if (st.stopped != null) return { txt: "STOPPED ✕", cls: "err" };
    let conf = S.dir > 0 ? "CONFIRMED ▼" : "CONFIRMED ▲";
    if (mode === "both") conf = "MD+GAP " + conf;
    if (st.fake > 0) conf = "FSB " + conf;
    return { txt: conf + (st.late ? " LATE" : ""), cls: "conf" };
  }

  function gradeOf(S, o) {
    const st = S.st;
    const mode = st.setup_mode;
    if (S.error) return "–";
    if (!S.dev_ok && mode !== "mdrev" && st.md_trig == null) return "–";
    if (st.trig == null && st.md_trig == null) {
      if ((st.md_ext_run || 0) >= o.md_min_ext) return "C";   // extension building: soft watch
      return S.dev_ok ? "C" : "–";
    }
    if (st.stopped != null) return "✕";
    let g;
    if (st.noRunway || st.R == null) {
      g = "B";
    } else {
      const gR = st.R >= o.Rmin;
      const rvol = S.rvol;
      const gV = rvol == null ? true : rvol >= o.rvol_min;
      const thin = rvol != null && Math.trunc(S.rvol_n || 0) < Math.trunc(o.rvol_min_n);
      g = gR && gV && !thin ? "A" : "B";
      if (mode === "both" && g === "B" && gR && !thin) g = "A";
      if (S.regime === "trend" && mode === "gap" && g === "A") g = "B";
      if (S.regime === "chop" && mode === "both" && g === "B" && gR && gV && !thin) g = "A";
    }
    if (st.late) g = "L" + g;
    return g;
  }

  function signalBadge(grade, stateCls, st) {
    if (st.tagged != null) return "TAGGED";
    if (st.stopped != null) return "STOPPED";
    // no-runway / inverted geometry is not a live trigger, nor is a fade the regime or trend-day guard blocked
    if (st.noRunway || st.bad_geom || st.regime_block || st.trend_block) return "WATCH";
    if (st.trig != null) return "TRIGGER";
    if (stateCls === "break") return "SETUP";
    if (stateCls === "fake") return "FAKE";
    if (stateCls === "gap") return "WATCH";
    return "FLAT";
  }

  /** Long needs stop < entry < target; short needs target < entry < stop. */
  function geomOk(side, entry, stop, target) {
    if (entry == null || stop == null || target == null) return false;
    const e = +entry, s = +stop, t = +target;
    if ([e, s, t].some(Number.isNaN)) return false;
    if (e <= 0) return false;
    if (side === "long") return s < e && e < t;
    if (side === "short") return t < e && e < s;
    return false;
  }

  /** Block a gap fade when the session has already continued hard in the gap direction. */
  function dayTrendBlocksFade(bars, i0, iN, gapPct, side, minCont = 0.6, openM = RTH_OPEN_M) {
    if (Math.abs(gapPct) < 0.35) return false;
    let openPx = null;
    for (let i = i0; i <= iN; i++) {
      if (bars[i].mins >= openM) { openPx = bars[i].o ? bars[i].o : bars[i].c; break; }
    }
    if (openPx == null || openPx <= 0) return false;
    const dayMove = (bars[iN].c - openPx) / openPx * 100.0;
    if (side === "long" && gapPct < 0 && dayMove <= -minCont) return true;
    if (side === "short" && gapPct > 0 && dayMove >= minCont) return true;
    if (side === "long" && gapPct < 0 && dayMove < gapPct) return true;
    if (side === "short" && gapPct > 0 && dayMove > gapPct) return true;
    return false;
  }

  /** Relative cumulative volume vs prior sessions at the same minute: [rvol, n baselines]. */
  function rvolOf(bars, days, iN, accVol, closeM = RTH_CLOSE_M) {
    if (days.length < 3 || accVol <= 0) return [null, 0];
    const lastMins = bars[iN].mins;
    const priors = days.slice(0, -1).slice(-RVOL_MAX_PRIORS);
    const bases = [];
    for (const pd of priors) {
      let cum = 0.0;
      for (const b of bars) {
        if (b.d !== pd) continue;
        if (b.mins <= lastMins && b.mins < closeM) cum += b.v;
      }
      if (cum > 0) bases.push(cum);
    }
    const n = bases.length;
    if (n < RVOL_MIN_PRIORS) return [null, n];
    let sum = 0;
    for (const x of bases) sum += x;
    return [accVol / (sum / n), n];
  }

  /** Composite 0–100 rank (higher = more interesting). */
  function edgeOf(grade, S, stateCls) {
    const st = S.st;
    let score = ({ A: 40, LA: 32, B: 22, LB: 16, C: 8, "✕": 2, "–": 0 })[grade] || 0;
    if (stateCls === "conf") score += 20;
    else if (stateCls === "break") score += 14;
    else if (stateCls === "tag") score += 18;
    else if (stateCls === "fake") score += 6;
    if (st.R != null) score += Math.min(20, Math.trunc(st.R * 8));
    if (S.rvol != null) score += Math.min(12, Math.trunc(Math.max(0, (S.rvol - 1.0) * 10)));
    score += Math.min(10, Math.trunc(Math.abs(S.gap_pct || 0) * 2));
    if (st.setup_mode === "mdrev") {
      score += 12;
      score += Math.min(10, Math.trunc((st.md_max_dist || 0) * 2));
    } else if (st.setup_mode === "both") {
      score += 16;
    } else if ((st.md_ext_run || 0) >= DEFAULT_MD_MIN_EXT) {
      score += 6;
    }
    const reg = S.regime, mode = st.setup_mode;
    if (reg === "chop" && (mode === "gap" || mode === "both")) score += 8;
    else if (reg === "trend" && mode === "mdrev") score += 8;
    else if (reg === "trend" && mode === "gap") score -= 14;
    if (S.conflict) score -= 22;
    if (st.late) score -= 8;
    if (st.stopped != null) score = Math.min(score, 15);
    return Math.max(0, Math.min(100, score));
  }

  /** Round-trip cost in R for a plan (honest.cost_r). */
  function costR(ticker, entry, stop) {
    const t = String(ticker || "").toUpperCase();
    const crypto = t.endsWith("-USD") || t.endsWith("-USDT") || t.endsWith("-USDC");
    const riskPct = entry ? Math.abs(entry - stop) / entry * 100.0 : 0.0;
    return riskPct > 0 ? COST[crypto ? "crypto" : "equity"] * 100.0 / riskPct : Infinity;
  }

  /** engine._plan_cost_r: null without a plan or with a zero-width stop. */
  function planCostR(ticker, entry, stop) {
    if (!entry || !stop) return null;
    const c = costR(ticker, entry, stop);
    return Number.isFinite(c) ? pyRound(c, 3) : null;
  }

  const DEFAULT_OPTS = {
    K: DEFAULT_K, atr_mult: DEFAULT_ATR_MULT, Rmin: DEFAULT_RMIN, rvol_min: DEFAULT_RVOL_MIN,
    late_cut: LATE_CUT_M, md_min_ext: DEFAULT_MD_MIN_EXT, md_min_dist: DEFAULT_MD_MIN_DIST,
    sigma_mult: DEFAULT_SIGMA_MULT, ker_lookback: DEFAULT_KER_LOOKBACK, ker_trend: DEFAULT_KER_TREND,
    ker_chop: DEFAULT_KER_CHOP, rvol_min_n: DEFAULT_RVOL_MIN_N, regime_gate: true,
  };

  /**
   * engine.analyze on prepared bars (prepBars). ctx: {nowMs, provider, opts}.
   * Returns the same row fields as engine.py plus _chart for the demo's chart pane.
   */
  function analyze(ticker, barsIn, ctx = {}) {
    const t = String(ticker).toUpperCase().trim();
    const crypto = isCrypto(t);
    const sess = sessionLabel(crypto, ctx.nowMs != null ? ctx.nowMs : Date.now());
    const o = {
      ...DEFAULT_OPTS,
      anchor_mins: crypto ? 0 : DEFAULT_ANCHOR_M,
      open_mins: crypto ? 0 : RTH_OPEN_M,
      close_mins: crypto ? CRYPTO_CLOSE_M : RTH_CLOSE_M,
      gap_fade: !crypto,
      gap_min: crypto ? 0.15 : DEFAULT_GAP_MIN,
      late_cut: crypto ? CRYPTO_LATE_CUT_M : LATE_CUT_M,
      ...(ctx.opts || {}),
    };
    const base = {
      ticker: t, provider: ctx.provider || null, bar_provider: ctx.provider || null,
      quote_provider: null, quote_latency_ms: null, ...sess, is_crypto: crypto,
    };
    let bars = barsIn || [];
    if (bars.length < 20) return { ...base, error: "insufficient bars", edge: 0, signal: "FLAT", grade: "–" };

    let days = sessions(bars);
    if (days.length < 2) {
      if (crypto && bars.length >= 40) {
        // crypto single continuous session: synthesize a prior window (engine.py does the same)
        const mid = Math.floor(bars.length / 2);
        bars = bars.map((b, i) => ({ ...b, d: i < mid ? "D0" : "D1", mins: (i % 390) + RTH_OPEN_M }));
        days = sessions(bars);
      } else {
        return { ...base, error: "need ≥2 sessions for orange anchor", edge: 0, signal: "FLAT", grade: "–" };
      }
    }

    const d0 = days[days.length - 1], d1 = days[days.length - 2];
    const i0 = firstIdx(bars, d0), p0 = firstIdx(bars, d1), iN = bars.length - 1;
    if (i0 < 1 || p0 < 0) return { ...base, error: "anchor bars missing", edge: 0, signal: "FLAT", grade: "–" };

    const resolved = resolveDay(bars, i0, iN, p0, o);
    const st = resolved.st;
    const price = bars[iN].c;

    const [rvolVal, rvolN] = rvolOf(bars, days, iN, resolved.acc.vol, o.close_mins);
    const kerVal = ker(bars, iN, Math.trunc(o.ker_lookback));
    const regime = regimeFromKer(kerVal, +o.ker_trend, +o.ker_chop);
    const adaptNow = adaptiveSigmaMult(kerVal, 1.0);

    // regime gate: pure gap fade suppressed in a trend; keep the multi-day reclaim
    let regimeBlock = false;
    const openPlan = st.tagged == null && st.stopped == null;
    if (o.regime_gate && regime === "trend" && st.setup_mode === "gap" && openPlan) {
      regimeBlock = true;
      st.regime_block = true;
    } else if (o.regime_gate && regime === "trend" && st.setup_mode === "both" && st.md_trig != null && openPlan) {
      st.trig = st.md_trig;
      st.entry = st.md_entry;
      st.stopPx = st.md_stop;
      st.target = st.md_target;
      st.R = st.md_R;
      st.setup_mode = "mdrev";
      if (st.entry && st.stopPx) st.riskPct = Math.abs(st.entry - st.stopPx) / st.entry * 100.0;
      st.regime_downgrade = "both→mdrev";
    }

    const S = {
      st, dir: resolved.dir, dev_ok: resolved.dev_ok, gap_pct: resolved.gap_pct,
      gap_provisional: resolved.gap_provisional, prior_close: resolved.prior_close,
      open_idx: resolved.open_idx, acc: resolved.acc, rvol: rvolVal, rvol_n: rvolN,
      session_n: days.length, ker: kerVal, regime, conflict: false,
    };
    let state = stateText(S, o);
    let grade = gradeOf(S, o);
    let signal = signalBadge(grade, state.cls, st);
    let edge = edgeOf(grade, S, state.cls);

    const blue = st.blue, orange = st.orange, sigma = st.sigma;
    const dBluePct = blue ? (price - blue) / blue * 100.0 : null;
    const devSigma = blue && sigma && sigma > 0 ? (price - blue) / sigma : null;
    let toOrange = null;
    if (orange != null) {
      if (st.setup_mode === "mdrev" && st.md_side === "long") toOrange = (price - orange) / price * 100.0;
      else if (st.setup_mode === "mdrev" && st.md_side === "short") toOrange = (orange - price) / price * 100.0;
      else if (resolved.dev_ok) {
        toOrange = resolved.dir > 0 ? (price - orange) / price * 100.0 : (orange - price) / price * 100.0;
      }
    }

    // trade direction: gap up fades short, gap down fades long; the MD plan uses its reclaim side
    const side = st.setup_mode === "mdrev" && st.md_side ? st.md_side : (resolved.dir > 0 ? "short" : "long");

    // hard guards: geometry, and no fading into a continuing trend day
    let badGeom = false;
    if (st.entry != null && st.stopPx != null && st.target != null && !geomOk(side, st.entry, st.stopPx, st.target)) {
      badGeom = true;
      st.bad_geom = true;
      st.noRunway = true;
    }
    let trendBlock = false;
    if ((st.setup_mode === "gap" || st.setup_mode === "both" || st.setup_mode == null) && st.trig != null) {
      if (dayTrendBlocksFade(bars, i0, iN, resolved.gap_pct, side, 0.6, o.open_mins)) trendBlock = true;
    }

    // scrub untradeable levels on open plans; TAGGED / STOPPED keep theirs for audit
    if ((st.noRunway || badGeom || trendBlock || regimeBlock) && st.tagged == null && st.stopped == null) {
      st.entry = st.stopPx = st.target = null;
      st.R = st.riskPct = st.runwayPct = null;
      if (trendBlock) st.trend_block = true;
      if (regimeBlock) st.regime_block = true;
    }

    if (regimeBlock) {
      state = { txt: "REGIME BLOCK · trend tape no gap-fade", cls: "none" };
      grade = "–";
      signal = signalBadge(grade, state.cls, st);
      edge = Math.max(0, edgeOf(grade, S, state.cls) - 12);
    } else if (trendBlock) {
      state = { txt: `TREND BLOCK · no fade ${side}`, cls: "none" };
      grade = "–";
      signal = signalBadge(grade, state.cls, st);
      edge = Math.max(0, edgeOf(grade, S, state.cls) - 18);
    } else if (badGeom || st.noRunway) {
      state = { txt: "CONFIRMED · NO RUNWAY", cls: "none" };
      if (st.trig != null && grade !== "✕" && grade !== "–") grade = "B";
      signal = signalBadge(grade, state.cls, st);
      edge = Math.max(0, edgeOf(grade, S, state.cls) - 10);
    }

    // thin RVOL: demote a live TRIGGER to WATCH
    if (signal === "TRIGGER" && rvolVal != null && rvolN < Math.trunc(o.rvol_min_n)) {
      signal = "WATCH";
      st.thin_rvol = true;
      if (grade === "A" || grade === "LA") grade = grade === "A" ? "B" : "LB";
      edge = Math.max(0, edge - 8);
    }

    // replay 2026-08-12: pure mdrev in chop off the A desk (engine.py carries the v1.4 caveat)
    if (st.setup_mode === "mdrev" && regime === "chop" && (grade === "A" || grade === "LA")) {
      grade = grade === "A" ? "B" : "LB";
      st.mdrev_chop_demote = true;
      edge = Math.max(0, edge - 10);
    }

    const hasTrig = st.trig != null || st.md_trig != null;
    const planOk = geomOk(side, st.entry, st.stopPx, st.target);
    const actionable = ["A", "LA", "B", "LB"].includes(grade) && hasTrig && st.stopped == null &&
      st.tagged == null && planOk && !st.noRunway && !trendBlock && !regimeBlock && !st.thin_rvol;
    const liveActionable = actionable && (grade === "A" || grade === "LA") &&
      ["rth", "premarket", "24/7 crypto"].includes(sess.session_label);

    const rel = idx => (idx == null ? null : idx - i0);
    const pc = resolved.prior_close;

    return {
      ...base,
      price: roundPx(price),
      blue: blue ? roundPx(blue) : null,
      orange: orange ? roundPx(orange) : null,
      sigma: sigma != null ? pyRound(sigma, 6) : null,
      dist_std: devSigma != null ? pyRound(devSigma, 2) : null,
      d_blue_pct: dBluePct != null ? pyRound(dBluePct, 3) : null,
      to_orange_pct: toOrange != null ? pyRound(toOrange, 3) : null,
      gap_pct: pyRound(resolved.gap_pct, 3),
      gap_provisional: resolved.gap_provisional,
      prior_close: roundPx(pc),
      dir: resolved.dir,
      side,
      grade,
      state: state.txt,
      state_cls: state.cls,
      signal,
      edge,
      entry: st.entry ? roundPx(st.entry) : null,
      stop: st.stopPx ? roundPx(st.stopPx) : null,
      target: st.target ? roundPx(st.target) : null,
      rr: st.R != null ? pyRound(st.R, 2) : null,
      risk_pct: st.riskPct != null ? pyRound(st.riskPct, 3) : null,
      runway_pct: st.runwayPct != null ? pyRound(st.runwayPct, 3) : null,
      cost_r: planCostR(t, st.entry, st.stopPx),
      rvol: rvolVal != null ? pyRound(rvolVal, 3) : null,
      rvol_n: Math.trunc(rvolN || 0),
      session_n: days.length,
      fakes: st.fake,
      late: st.late,
      no_runway: !!st.noRunway,
      bad_geom: !!st.bad_geom,
      trend_block: !!st.trend_block,
      regime_block: !!st.regime_block,
      thin_rvol: !!st.thin_rvol,
      actionable,
      live_actionable: liveActionable,
      focus_day: d0,
      prior_day: d1,
      note: state.txt,
      setup_mode: st.setup_mode,
      md_side: st.md_side,
      md_ext_run: st.md_ext_run || 0,
      md_max_dist: st.md_max_dist ? pyRound(st.md_max_dist, 3) : null,
      ker: kerVal != null ? pyRound(kerVal, 3) : null,
      regime,
      adapt_mult: pyRound(adaptNow, 3),
      conflict: false,
      _chart: {
        bars: bars.slice(i0, iN + 1),
        blue_trail: st.blueTrail,
        orange_trail: st.orangeTrail,
        sig_trail: st.sigTrail,
        prior_close: pc,
        markers: {
          firstBreak: rel(st.firstBreak),
          trig: rel(st.trig != null ? st.trig : st.md_trig),
          tagged: rel(st.tagged),
          stopped: rel(st.stopped),
          mdTrig: rel(st.md_trig),
          openIdx: resolved.open_idx != null ? resolved.open_idx - i0 : null,
        },
        levels: { entry: st.entry, stop: st.stopPx, target: st.target, blue, orange },
        dir: resolved.dir,
        side,
        grade,
        state: state.txt,
        setup_mode: st.setup_mode,
      },
    };
  }

  /** Minutes since the newest bar started (engine.bar_age_min). */
  function barAgeMin(bars, nowMs) {
    if (!bars || !bars.length) return null;
    return Math.max(0.0, (nowMs - bars[bars.length - 1].ts) / 60000.0);
  }

  /**
   * engine.apply_stale_guard: while the market is open (RTH or 24/7 crypto) a newest bar older than
   * maxAgeMin demotes TRIGGER → WATCH and clears live_actionable. Premarket is exempt.
   */
  function applyStaleGuard(row, ageMin, maxAgeMin = DEFAULT_MAX_BAR_AGE_MIN) {
    row.bar_age_min = ageMin != null ? pyRound(ageMin, 1) : null;
    const openNow = row.session_label === "rth" || row.session_label === "24/7 crypto";
    const stale = !!(openNow && ageMin != null && ageMin > maxAgeMin);
    row.stale_bars = stale;
    if (stale) {
      row.live_actionable = false;
      if (row.signal === "TRIGGER") row.signal = "WATCH";
      row.note = ((row.note || "") + ` · STALE BARS ${ageMin.toFixed(0)}m`).replace(/^[ ·]+/, "");
      row.edge = Math.max(0, Math.trunc(row.edge || 0) - 15);
    }
    return stale;
  }

  /** Chart series for a row's _chart (engine.build_chart_from_row). */
  function chartFromRow(row) {
    const ch = row._chart || {};
    const bars = ch.bars || [];
    const r4 = x => (x == null ? null : pyRound(x, 4));
    const series = bars.map((b, i) => {
      const blue = ch.blue_trail[i], orange = ch.orange_trail[i], sig = ch.sig_trail[i];
      const band = blue != null && sig != null;
      return {
        t: b.ts, time: b.time, open: b.o, high: b.h, low: b.l, price: b.c, volume: b.v,
        blue: r4(blue), orange: r4(orange),
        upper: band ? r4(blue + sig) : null, lower: band ? r4(blue - sig) : null,
      };
    });
    return {
      ticker: row.ticker, series, levels: ch.levels || {}, markers: ch.markers || {},
      prior_close: ch.prior_close, dir: ch.dir, side: ch.side, grade: row.grade, state: row.state,
      price: row.price, blue: row.blue, orange: row.orange, gap_pct: row.gap_pct, entry: row.entry,
      stop: row.stop, target: row.target, rr: row.rr, cost_r: row.cost_r, rvol: row.rvol,
      rvol_n: row.rvol_n, session_n: row.session_n, signal: row.signal, edge: row.edge,
      provider: row.provider, regime: row.regime, ker: row.ker, setup_mode: row.setup_mode,
    };
  }

  // ── scan helpers around the engine (data.py / app.py) ────────────────────

  /** providers.looks_crypto: the desk's crypto routing and liquidity floor (bare BTC/ETH/SOL included). */
  function looksCrypto(ticker) {
    const t = String(ticker || "").toUpperCase().replace(/\//g, "-");
    if (t.endsWith("-USD") || t.endsWith("-USDT") || t.endsWith("-USDC")) return true;
    const bare = t.replace(/-/g, "");
    if ((bare.endsWith("USDT") || bare.endsWith("USDC") || bare.endsWith("BUSD")) && bare.length >= 6) return true;
    if (["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD", "BNBUSD"].includes(bare)) return true;
    return bare.endsWith("USDT") || ["BTC", "ETH", "SOL"].includes(bare);
  }

  /**
   * Σ close × volume (data.session_dollar_volume): over the newest ET calendar day, or for crypto over
   * the 24 hours to the newest bar (a 24/7 market's ET day is only minutes old after midnight).
   */
  function sessionDollarVolume(bars, ticker) {
    if (!bars || !bars.length) return 0.0;
    const last = bars[bars.length - 1];
    const crypto = ticker != null && looksCrypto(ticker);
    let s = 0.0;
    for (const b of bars) {
      if (crypto ? b.ts > last.ts - 86400000 : b.d === last.d) s += (b.c || 0) * (b.v || 0);
    }
    return s;
  }

  // data.EQUITY_MIN_DVOL / CRYPTO_DVOL_SHARE
  const EQUITY_MIN_DVOL = 2000000.0;
  const CRYPTO_DVOL_SHARE = 0.25;

  /** data.min_dollar_volume_for: the desk's floor for equities, a quarter of it for crypto (24h volume). */
  function dollarVolumeFloor(ticker, floor) {
    const f = floor == null ? EQUITY_MIN_DVOL : Math.max(0.0, +floor);
    return looksCrypto(ticker) ? f * CRYPTO_DVOL_SHARE : f;
  }

  /** Desk ranking for a wide pool (data.rotation_score). */
  function rotationScore(row) {
    if (!row || typeof row !== "object") return -1e9;
    if (row.error) return -1e6;
    const gap = Math.abs(+row.gap_pct || 0);
    const dBlue = Math.abs(+row.d_blue_pct || 0);
    const rvol = +row.rvol || 1.0;
    const edge = +row.edge || 0;
    const gboost = ({ A: 25, LA: 18, B: 8, LB: 5, C: 2, "✕": 0, "–": 0 })[row.grade || "–"] || 0;
    let score = gap * 3.0 + dBlue * 1.5 + Math.max(0.0, rvol - 0.8) * 12.0 + edge * 0.35 + gboost;
    const dvol = +row.dollar_vol || 0;
    if (dvol > 0) score += Math.min(18.0, Math.log10(dvol + 1.0) * 2.2);
    if (row.live_actionable) score += 20;
    else if (row.actionable) score += 10;
    if (row.setup_mode === "both") score += 12;
    else if (row.setup_mode === "mdrev") score += 6;
    if (row.regime === "chop" && (row.setup_mode === "gap" || row.setup_mode === "both")) score += 8;
    if (row.regime === "trend" && row.setup_mode === "mdrev") score += 8;
    if (row.conflict) score -= 30;
    if (row.regime_block || row.trend_block) score -= 40;
    if (row.thin_rvol) score -= 10;
    if ((row.rvol_n || 0) < 2 && row.rvol != null) score -= 6;
    if (row.illiquid) score -= 25;
    return score;
  }

  /** Desk grade floor (app._grade_ok); the A floor also accepts late A. */
  function gradeOk(grade, floor) {
    const g = grade || "–";
    const f = String(floor || "C").toUpperCase();
    if (f === "A" && (g === "A" || g === "LA")) return true;
    if (f === "B" && ["A", "LA", "B", "LB"].includes(g)) return true;
    return (g in GRADE_RANK ? GRADE_RANK[g] : -1) >= (f in GRADE_RANK ? GRADE_RANK[f] : 1);
  }

  return {
    analyze, prepBars, sessionLabel, barAgeMin, applyStaleGuard, chartFromRow, costR, isCrypto,
    looksCrypto, sessionDollarVolume, dollarVolumeFloor, rotationScore, gradeOk, geomOk, etParts,
    VERSION, GRADE_RANK, DEFAULT_MAX_BAR_AGE_MIN, COST,
  };
});
