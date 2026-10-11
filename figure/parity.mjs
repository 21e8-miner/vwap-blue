// Proves figure/model.mjs against engine.js on the figure's own tape: at every bar of the focus day the
// desk's row (engine.analyze on the bars up to that one) must say what the model's step says.
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { tape } from "./bars.mjs";
import { prep, walk } from "./model.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const E = createRequire(import.meta.url)(join(HERE, "..", "engine.js"));

const raw = tape();
const bars = prep(raw);
const W = walk(bars);
const close = (a, b, eps = 1e-9) => (a == null && b == null) || (a != null && b != null && Math.abs(a - b) <= eps);
let bad = 0;
const fields = [];
for (const s of W.steps) {
  const row = E.analyze("TEST", E.prepBars(raw.slice(0, s.i + 1)), { nowMs: raw[s.i].ts + 300000 });
  // the row rounds for display: prices to 2 decimals, sigma to 6, ker and the adaptive multiplier to 3
  const checks = [
    ["blue", s.blue, row.blue, 0.0051], ["orange", s.orange, row.orange, 0.0051], ["sigma", s.sigma, row.sigma, 1e-6],
    ["ker", s.ker, row.ker, 5.1e-4], ["adapt", s.adapt, row.adapt_mult, 5.1e-4], ["entry", s.entry, row.entry, 0.0051],
    ["stop", s.stopPx, row.stop, 0.0051], ["target", s.target, row.target, 0.0051], ["rr", s.R, row.rr, 0.051],
  ];
  if (!!row.regime_block !== s.regimeBlock) { bad++; if (bad < 12) console.log("regime_block", s.i, s.regimeBlock, row.regime_block); }
  for (const [name, mine, theirs, eps] of checks) {
    if (theirs === undefined) { if (!fields.includes(name)) fields.push(name); continue; }
    if (s.regimeBlock && ["entry", "stop", "target", "rr"].includes(name)) { if (theirs != null) { bad++; console.log("blocked plan shown", s.i, name); } continue; }
    if (!close(mine, theirs, eps)) { bad++; if (bad < 12) console.log("mismatch", s.i, name, mine, theirs); }
  }
  const trigState = s.trig != null ? (s.tagged != null ? "TAGGED" : s.stopped != null ? "STOPPED" : "TRIGGER") : null;
  if (trigState && row.signal !== trigState && !(trigState === "TRIGGER" && row.signal === "WATCH" && s.regimeBlock)) {
    bad++; if (bad < 12) console.log("signal", s.i, trigState, row.signal, row.state);
  }
}
if (fields.length) console.log("row fields not present (skipped):", fields.join(", "));
const t = W.steps.find((s) => s.trig != null);
const tg = W.steps.find((s) => s.tagged != null), sp = W.steps.find((s) => s.stopped != null);
if (t) console.log(`trigger ${t.i} at ${String(Math.floor(t.mins/60)).padStart(2,"0")}:${String(t.mins%60).padStart(2,"0")} entry ${t.entry.toFixed(2)} stop ${t.stopPx.toFixed(2)} target ${t.target.toFixed(2)} R ${t.R?.toFixed(2)} ker ${t.ker?.toFixed(2)} band ${t.band.toFixed(3)}`);
if (tg) console.log(`tagged at bar ${tg.i} (${tg.mins})`); if (sp) console.log(`stopped at bar ${sp.i} (${sp.mins})`);
if (process.argv.includes("--story")) for (const s of W.steps) if (s.mins % 30 === 0 || s.i === t?.i) console.log(s.mins, s.close.toFixed(2), "blue", s.blue?.toFixed(2), "orange", s.orange?.toFixed(2), "sig", s.sigma?.toFixed(3), "ker", s.ker?.toFixed(2), "band", s.band.toFixed(3), s.beyond ? "BEYOND " + s.run : "", s.trig != null ? "TRIG" : "", s.tagged != null ? "TAG" : "", s.stopped != null ? "STOP" : "");
console.log(`gap ${W.gapPct.toFixed(2)}% dir ${W.direction} armed ${W.devOk} · bars ${W.steps.length} · trigger ${t ? t.i : "none"} · ${bad} mismatches`);
process.exit(bad ? 1 : 0);
