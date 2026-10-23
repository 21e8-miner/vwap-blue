// Builds figure/index.html (dark) or figure/light.html (--light): "The VWAP balance", an isometric figure
// of the desk's gap-fade mechanism, drawn with the Anatomy kit (figure/kit/, MIT) and driven bar by bar by
// figure/model.mjs on the tape in figure/bars.mjs. figure/parity.mjs proves the model against engine.js.
//
//   node figure/build.mjs            → figure/index.html
//   node figure/build.mjs --light    → figure/light.html
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import * as k from "./kit/iso-kit.mjs";
import { tape } from "./bars.mjs";
import { prep, walk, profile, hhmm, ANCHOR_M, CLOSE_M, OPEN_M, K as K_BARS, SIGMA_MULT } from "./model.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const theme = process.argv.includes("--light") ? "light" : "dark";
const kitScript = () => readFileSync(join(HERE, "kit", "iso-kit.mjs"), "utf8").replace(/^export /gm, "");

// ── truth ────────────────────────────────────────────────────────────────────────────────────────────
const raw = tape();
const bars = prep(raw);
const W = walk(bars);
const STEPS = W.steps;
const BIN = 0.07;                       // one hanger per 7 cents of typical price
const PPD = 100;                        // world units per dollar along the beams
const P_MID = 100.5;
const px = (price) => (price - P_MID) * PPD;
const binX = (key) => px(key * BIN);

const priorBins = profile(bars, W.p0, W.i0, BIN);
const todayBins = profile(bars, W.i0, bars.length, BIN);
const maxStack = Math.max(...[...priorBins.values(), ...todayBins.values()].map((d) => d.reduce((s, x) => s + x.v, 0)));
const HANG = 44;                        // hanger rod length
const SCALE = (HANG - 7) / maxStack;    // disc thickness per share of volume: the tallest stack leaves rod to see
const trigStep = STEPS.findIndex((s) => s.trig != null);
const tagStep = STEPS.findIndex((s) => s.tagged != null);
const openStep = STEPS.findIndex((s) => s.rth);

// ── world ────────────────────────────────────────────────────────────────────────────────────────────
const WIDTH = 600;
const HEIGHT = 400;
const BASE = { x: -130, y: -38, w: 260, d: 82, r: 8 };
const BASE_H = 12;
const FOOT_H = 5;
const RAIL = { x: -100, y: -4.5, w: 200, d: 9, r: 1 };
const RAIL_H = 3;
const CAR = { x: -12, y: -8, w: 24, d: 16, r: 2 };
const CAR_Z = RAIL_H, CAR_TOP = 8;
const COL = { x: -3, y: -3, w: 6, d: 6, r: 1 };
const CAP = { x: -6, y: -4, w: 12, d: 8, r: 1.2 };
const CAP_Z = 108;
const OB = { x: -104, y: -11, w: 208, d: 22, r: 1.5 };     // the two-day (orange) beam
const OB_Z = 112, OB_H = 6, OB_TOP = OB_Z + OB_H;
const PRIOR_Y = -6.5;
const BB = { x: -104, y: 3.5, w: 208, d: 8, r: 1.2 };       // the day (blue) beam
const BB_Z = 80, BB_H = 6, BB_TOP = BB_Z + BB_H;
const TODAY_Y = 7.5;
const DISC_R = 3.0, ROD_R = 0.5, PLATE_R = 3.4, PLATE_H = 0.8;
const TICK_FROM = px(99.5), TICK_TO = px(101.5), TICK_STEP = PPD * 0.05;
// the hanger (stirrup) that carries the day beam, built at x = 0 and moved to blue
const HOOK = { x: -4, y: 0, w: 8, d: 14, r: 1 };
const HOOK_Z = OB_TOP, HOOK_H = 4;
const LEG = { x: -4, y: 11.5, w: 8, d: 2.5, r: 0.6 };
const LEG_Z0 = BB_TOP, LEG_Z1 = OB_TOP;
const FOOT = { x: -4, y: 5.5, w: 8, d: 8.5, r: 0.8 };
const FOOT_Z = BB_TOP, FOOT_T = 3;
const BAR = { x: -21, y: 14, w: 42, d: 2.5, r: 0.6 };         // the caliper bar on the leg's front
const BAR_Z = 87, BAR_H = 3;
const BLADE_W = 1.6, BLADE_Y = 14.3, BLADE_D = 2, BLADE_Z0 = 82, BLADE_Z1 = BAR_Z;
const POST_W = 1.6;
const SPRING_Y = BLADE_Y + BLADE_D / 2, SPRING_Z = 84.5, SPRING_R = 1.15;
// the pointer rod in front of the day beam
const ROD = { x: -103, y: 17.2, w: 206, d: 2.2, r: 1.1 };   // bracket face to bracket face
const ROD_Z = 73;
const BRK_L = { x: -107, y: 11.5, w: 4, d: 9, r: 0.6 };
const BRK_R = { x: 103, y: 11.5, w: 4, d: 9, r: 0.6 };
const BRK_Z = 72, BRK_TOP = BB_TOP + 1;
const SLIDER = { x: -3.5, y: 15.5, w: 7, d: 5.5, r: 1 };
const SLIDER_Z = 71, SLIDER_H = 6.5;
const BLADE = { x: -0.6, y: 17.1, w: 1.2, d: 2.4, r: 0.3 };
const BLADE_TOP = 85.5;
// dials on the base front
const DIAL = [-112, 26], DIAL_R = 11, DIAL_H = 8, DIAL_TOP = DIAL_H;
const DIAL_FROM = -Math.PI * 0.75, DIAL_SPAN = Math.PI * 1.5;
const DRUM = [112, 26], DRUM_R = 7, DRUM_H = 6;
const FLAG = [122.5, 19], FLAG_R = 0.7, FLAG_Z1 = 16, FLAG_Z0 = 2.6;
const FLAG_PLATE = { x: FLAG[0] - 0.4, y: FLAG[1], w: 0.8, d: 6.5, r: 0.2 };
const FLAG_LIFT = 7;
const LABEL = { x: -28, y: 29, w: 56, d: 8, r: 1 };

const P = k.fitProjection(
  [...k.boxCorners(BASE, -BASE_H - FOOT_H, 0), [HOOK.x, HOOK.y, HOOK_Z + HOOK_H + 2], [OB.x + OB.w + 4, OB.y + OB.d, OB_TOP + 2], [OB.x - 4, OB.y, OB_TOP + 2]],
  WIDTH,
  HEIGHT,
  { pad: 26, azimuth: 46 },
);

const at = (point) => k.iso(point, P);
const S = (paths, style) => k.solidSvg(paths, style);
const L = (d, style) => k.lineSvg(d, style);
const D = (points, style) => k.dotsSvg(points, style);
const byDepth = (points) => [...points].sort((a, b) => k.depthOf([a[0], a[1], 0], P) - k.depthOf([b[0], b[1], 0], P));

// ── shared helpers: these run in the build and again in the page (kk is the kit either way) ──────────
const kk = k;
function stackSvg(kk, P, x, y, zTop, hang, discs, discR, rodR, plateR, plateH, lit) {
  // one hanger: a rod from the beam's underside, a plate at its end, slotted discs stacked from the plate up
  const z0 = zTop - hang;
  const out = [];
  out.push(kk.solidSvg(kk.cylinder(x, y, rodR, z0 + plateH, hang - plateH, P, 8), { tone: "mid", crease: "none" }));
  out.push(kk.solidSvg(kk.cylinder(x, y, plateR, z0, plateH, P, 14), { tone: "mid", crease: "none" }));
  let z = z0 + plateH;
  for (let i = 0; i < discs.length; i++) {
    const h = discs[i];
    out.push(kk.solidSvg(kk.cylinder(x, y, discR, z, h, P, 12), { tone: "hi", crease: h > 1.2 ? "lo" : "none" }));
    z += h;
  }
  if (discs.length) out.push(kk.lineSvg(kk.ring(x, y, rodR + 0.35, z, P, 10), { tone: "lo" }));
  return out.join("");
}
function jawsSvg(kk, P, bandX, bar, barZ, bladeW, bladeY, bladeD, z0, z1, postW, springY, springZ, springR) {
  // the two sprung blades hanging from the caliper bar, inner faces at ±bandX
  const out = [];
  const gap = Math.max(0, bandX);
  for (const s of [-1, 1]) {
    const x0 = s < 0 ? -gap - bladeW : gap;
    const postX = s < 0 ? bar.x : bar.x + bar.w - postW;
    const from = s < 0 ? postX + postW : x0 + bladeW;
    const to = s < 0 ? x0 : postX;
    const c = kk.coil(from, to, [springY, springZ], springR, 5, "x", P, 12);
    out.push(kk.lineSvg(c.back, { tone: "lo" }));
    out.push(kk.solidSvg(kk.slabOf({ x: x0, y: bladeY, w: bladeW, d: bladeD, r: 0.3 }, z0, z1 - z0, P, 2, 0.3), { tone: "hi", crease: "lo" }));
    out.push(kk.lineSvg(c.front, { tone: "mid" }));
  }
  return out.join("");
}
function needleD(kk, P, dial, r, top, from, span, value) {
  const a = from + span * value;
  return kk.lineOnTop([dial[0] - Math.sin(a) * 3.2, dial[1] + Math.cos(a) * 3.2], [dial[0] + Math.sin(a) * (r - 3.6), dial[1] - Math.cos(a) * (r - 3.6)], top + 0.2, P);
}

// ── static art ───────────────────────────────────────────────────────────────────────────────────────
function base() {
  const out = [`<path class="iso-halo" d="${k.haloOf(BASE, -BASE_H - FOOT_H, BASE_H, P)}"/>`];
  for (const [x, y] of byDepth(k.corners(BASE, 24))) {
    out.push(S(k.cylinder(x, y, 7, -BASE_H - FOOT_H, 1.6, P, 28), { tone: "lo" }));
    out.push(S(k.cylinder(x, y, 5.4, -BASE_H - FOOT_H + 1.6, FOOT_H - 1.6, P, 28), { tone: "mid" }));
  }
  out.push(S(k.slabOf(BASE, -BASE_H, BASE_H, P, 8, 1.6), { tone: "mid" }));
  out.push(L(k.planOutline(k.insetPlan(BASE, 5), 0, P), { tone: "lo" }));
  const screws = k.corners(BASE, 10);
  out.push(L(screws.map(([x, y]) => k.ring(x, y, 2.2, 0, P, 16)).join(""), { tone: "lo" }));
  out.push(D(screws.map(([x, y]) => at([x, y, 0])), { size: 0.5 }));
  // label plate with three rule lines (they stand for printed text)
  out.push(S(k.slabOf(LABEL, 0, 0.8, P, 4), { tone: "lo", crease: "none" }));
  out.push(L(k.planOutline(k.insetPlan(LABEL, 1.4), 0.8, P, 3), { tone: "faint" }));
  for (const [i, len] of [[0, 30], [1, 22], [2, 26]].values()) out.push(L(k.lineOnTop([LABEL.x + 6, LABEL.y + 2.2 + i * 1.8], [LABEL.x + 6 + len, LABEL.y + 2.2 + i * 1.8], 0.8, P), { tone: "lo", free: true }));
  out.push(D([at([LABEL.x + 2.2, LABEL.y + 4, 0.8]), at([LABEL.x + LABEL.w - 2.2, LABEL.y + 4, 0.8])], { size: 0.42 }));
  // the bench's grid of tapped holes, either side of the rail
  const holes = [];
  for (const y of [-24, -15, 14, 23]) for (let x = -90; x <= 90; x += 12) holes.push(k.ring(x, y, 0.9, 0, P, 12));
  out.push(L(holes.join(""), { tone: "lo" }));
  return out.join("");
}

function rail() {
  const out = [];
  out.push(S(k.slabOf(RAIL, 0, RAIL_H, P, 3, 0.4), { tone: "mid" }));
  out.push(L(k.lineOnTop([RAIL.x + 4, 0], [RAIL.x + RAIL.w - 4, 0], RAIL_H, P), { tone: "faint" }));
  for (const x of [RAIL.x - 5, RAIL.x + RAIL.w]) {
    out.push(S(k.slabOf({ x, y: -8, w: 5, d: 16, r: 1.2 }, 0, 7, P, 3, 0.5), { tone: "mid" }));
    out.push(D([at([x + 2.5, -5, 7]), at([x + 2.5, 5, 7])], { size: 0.45 }));
  }
  out.push(D([RAIL.x + 10, RAIL.x + RAIL.w - 10].map((x) => at([x, RAIL.y + 1.6, RAIL_H])), { size: 0.4 }));
  return out.join("");
}

function carriage() {
  // the column that carries the two-day beam, on a carriage that slides to the orange anchor
  const out = [`<path class="iso-halo" d="${k.haloOf(k.insetPlan(CAR, 2), CAR_Z, 0.1, P)}" style="filter:blur(3px)"/>`];
  out.push(S(k.slabOf(CAR, CAR_Z, CAR_TOP - CAR_Z, P, 4, 0.5), { tone: "mid" }));
  for (const [x, y] of byDepth(k.corners(CAR, 3))) out.push(S(k.slabOf({ x: x - 1.6, y: y - 1.6, w: 3.2, d: 3.2, r: 0.6 }, CAR_TOP, 1.4, P, 2), { tone: "mid", crease: "none" }));
  out.push(D(k.corners(CAR, 3).map(([x, y]) => at([x, y, CAR_TOP + 1.4])), { size: 0.4, tone: "lo" }));
  out.push(S(k.slabOf(k.insetPlan(COL, -1.5), CAR_TOP, 3, P, 3, 0.4), { tone: "mid" }));
  out.push(S(k.slabOf(COL, CAR_TOP + 3, CAP_Z - CAR_TOP - 3, P, 3), { tone: "mid", crease: "none", inner: L([30, 60].map((z) => k.sideSeam(COL, z, P, 3)).join(""), { tone: "faint" }) }));
  out.push(S(k.slabOf(CAP, CAP_Z, OB_Z - CAP_Z, P, 3, 0.4), { tone: "mid" }));
  out.push(L(k.lineOnTop([CAP.x + 1, 0], [CAP.x + CAP.w - 1, 0], OB_Z, P), { tone: "hi" }));
  return out.join("");
}

function beam(plan, z, h, tickEdge, lit) {
  const out = [];
  out.push(S(k.slabOf(plan, z, h, P, 4, 0.8), { tone: "hi", crease: "lo", inner: L(k.sideSeam(plan, z + 1.2, P, 4), { tone: "faint" }) }));
  const ticks = k.topTicks(TICK_FROM, TICK_TO, TICK_STEP, 5, tickEdge, z + h, [1.6, 3], P);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  for (const x of [plan.x - 3, plan.x + plan.w - 1]) {
    out.push(S(k.slabOf({ x, y: plan.y - 1.5, w: 4, d: plan.d + 3, r: 1 }, z - 1, h + 2, P, 3, 0.4), { tone: "mid" }));
    out.push(D([at([x + 2, plan.y - 1.5 + 2, z + h + 1]), at([x + 2, plan.y + plan.d + 1.5 - 2, z + h + 1])], { size: 0.42 }));
  }
  return out.join("");
}

function lane(bins, y, zTop, upTo) {
  // hangers sorted far to near (ascending x is nearer at this azimuth)
  const keys = [...bins.keys()].sort((a, b) => a - b);
  return keys.map((key) => {
    const discs = bins.get(key).filter((d) => upTo == null || d.i <= upTo).map((d) => d.v * SCALE);
    return stackSvg(kk, P, binX(key), y, zTop, HANG, discs, DISC_R, ROD_R, PLATE_R, PLATE_H, false);
  }).join("");
}

function hanger() {
  // the stirrup that hangs the day beam from the two-day beam; its foot is the day's balance point
  const out = [];
  out.push(S(k.slabOf(HOOK, HOOK_Z, HOOK_H, P, 3, 0.5), { tone: "mid" }));
  out.push(D([at([HOOK.x + 2, HOOK.y + 3, HOOK_Z + HOOK_H]), at([HOOK.x + HOOK.w - 2, HOOK.y + 3, HOOK_Z + HOOK_H])], { size: 0.42 }));
  out.push(S(k.slabOf(LEG, LEG_Z0, LEG_Z1 - LEG_Z0, P, 2, 0.3), { tone: "mid", crease: "none", inner: L(k.sideSeam(LEG, 92, P, 2), { tone: "faint" }) }));
  out.push(S(k.slabOf(FOOT, FOOT_Z, FOOT_T, P, 3, 0.4), { tone: "mid" }));
  out.push(L(k.lineOnTop([0, FOOT.y + 1], [0, FOOT.y + FOOT.d - 1], FOOT_Z + FOOT_T, P), { tone: "hi" }));
  out.push(S(k.slabOf(BAR, BAR_Z, BAR_H, P, 3, 0.4), { tone: "mid" }));
  out.push(D([at([BAR.x + 3, BAR.y + 1.25, BAR_Z + BAR_H]), at([BAR.x + BAR.w - 3, BAR.y + 1.25, BAR_Z + BAR_H])], { size: 0.4 }));
  for (const x of [BAR.x, BAR.x + BAR.w - POST_W]) out.push(S(k.slabOf({ x, y: BAR.y, w: POST_W, d: BAR.d, r: 0.4 }, BLADE_Z0, BAR_Z - BLADE_Z0, P, 2), { tone: "mid", crease: "none" }));
  return out.join("");
}

function pointerRod() {
  const out = [];
  for (const brk of [BRK_L, BRK_R]) {
    out.push(S(k.slabOf(brk, BRK_Z, BRK_TOP - BRK_Z, P, 3, 0.4), { tone: "mid" }));
    out.push(L(k.sideRing([brk.x + brk.w, ROD.y + ROD.d / 2, ROD_Z + ROD.d / 2], ROD.d / 2 + 0.5, "x", P, 14), { tone: "lo" }));
  }
  out.push(S(k.slabOf(ROD, ROD_Z, ROD.d, P, 3), { tone: "mid", crease: "none" }));
  return out.join("");
}

function slider() {
  const out = [];
  out.push(S(k.slabOf(SLIDER, SLIDER_Z, SLIDER_H, P, 3, 0.5), { tone: "hi", lit: true, crease: "lo" }));
  out.push(L(k.sideRing([SLIDER.x + SLIDER.w, ROD.y + ROD.d / 2, ROD_Z + ROD.d / 2], ROD.d / 2 + 0.4, "x", P, 14), { tone: "lo" }));
  out.push(L(k.knurl(0, SLIDER.y + SLIDER.d + 0.2, 1.2, SLIDER_Z + 1, SLIDER_Z + 4, 8, P, 0.2), { tone: "lo" }));
  out.push(S(k.cylinder(0, SLIDER.y + SLIDER.d + 0.4, 1.2, SLIDER_Z + 1, 3, P, 12), { tone: "mid" }));
  out.push(S(k.slabOf(BLADE, SLIDER_Z + SLIDER_H, BLADE_TOP - SLIDER_Z - SLIDER_H, P, 2, 0.3), { tone: "hi", lit: true, crease: "lo" }));
  return out.join("");
}

function kerDial() {
  const out = [];
  const [x, y] = DIAL;
  out.push(S(k.slabOf({ x: x - 4, y: y - DIAL_R - 7, w: 8, d: 8, r: 1.2 }, 0, 4, P, 3, 0.4), { tone: "mid" }));
  out.push(S(k.cylinder(x, y, DIAL_R, 0, DIAL_H, P, 64, 0.8), { tone: "hi", crease: "lo" }));
  out.push(L(k.knurl(x, y, DIAL_R, DIAL_TOP - 3, DIAL_TOP, 72, P, 0.3), { tone: "lo" }));
  out.push(L(k.sideArc(x, y, DIAL_R, DIAL_TOP - 3, P, 40), { tone: "faint" }));
  out.push(L(k.ring(x, y, DIAL_R - 2, DIAL_TOP, P, 64), { tone: "lo" }));
  const ticks = k.radialTicks(x, y, DIAL_R - 2.6, 20, 5, DIAL_TOP, [1.2, 2.6], P, DIAL_FROM, DIAL_SPAN);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  out.push(S(k.slabOf({ x: x + DIAL_R - 1, y: y - 2, w: 4.5, d: 4, r: 1 }, 2, 4, P, 3), { tone: "mid" }));
  out.push(S(k.slabOf({ x: x + DIAL_R + 3, y: y - 2.8, w: 2.4, d: 5.6, r: 1 }, 1.4, 5.2, P, 3), { tone: "mid" }));
  out.push(L(k.ring(x, y, 1.5, DIAL_TOP, P, 16), { tone: "mid" }));
  out.push(k.faceTextSvg(k.topMatrix([x, y + 6.2], DIAL_TOP, P, "x"), "KER", { size: 2.2, tone: "lo", anchor: "middle" }));
  return out.join("");
}

function counter() {
  const out = [];
  const [x, y] = DRUM;
  out.push(S(k.slabOf({ x: x - DRUM_R - 2, y: y - DRUM_R - 2, w: DRUM_R * 2 + 4, d: DRUM_R * 2 + 4, r: 2 }, 0, 1.2, P, 4, 0.3), { tone: "mid" }));
  out.push(S(k.cylinder(x, y, DRUM_R, 1.2, DRUM_H, P, 48, 0.6), { tone: "hi", crease: "lo" }));
  out.push(L(k.sideArc(x, y, DRUM_R, 1.2 + DRUM_H - 1.8, P, 30), { tone: "faint" }));
  const ticks = k.radialTicks(x, y, DRUM_R - 1.4, 3, 1, 1.2 + DRUM_H, [1, 2.2], P, -Math.PI * 2 / 3, Math.PI * 4 / 3);
  out.push(L(ticks.major, { tone: "mid" }));
  out.push(L(k.ring(x, y, 1.2, 1.2 + DRUM_H, P, 14), { tone: "mid" }));
  // the trip flag: a post in the housing, its plate rises when the latch trips
  out.push(S(k.cylinder(FLAG[0], FLAG[1], FLAG_R + 0.8, 1.2, 1.4, P, 14), { tone: "mid", crease: "none" }));
  out.push(S(k.cylinder(FLAG[0], FLAG[1], FLAG_R, 2.6, FLAG_Z1 - 2.6, P, 12), { tone: "mid", crease: "none" }));
  return out.join("");
}

const counterD = (run) => needleD(kk, P, DRUM, DRUM_R, 1.2 + DRUM_H, -Math.PI * 2 / 3, Math.PI * 4 / 3, run / 2);

// ── first frame ──────────────────────────────────────────────────────────────────────────────────────
const first = STEPS[openStep];
const startStep = openStep;
const s0 = STEPS[startStep];
const flagPlate = () => S(k.slabOf(FLAG_PLATE, FLAG_Z0, 5.5, P, 2), { tone: "hi", crease: "none" });

const svg = k.figureSvg({
  width: WIDTH,
  height: HEIGHT,
  label:
    "A two-beam balance on a bench. Every five-minute bar hangs a slotted weight, as thick as its volume, under a beam at the bar's typical price. The day beam hangs from the longer two-day beam by a sliding foot that settles under the day's weights, which is the blue VWAP; the two-day beam, carrying yesterday's weights too, balances on a column that slides to the orange anchor. Spring jaws either side of the foot open to the adaptive band, a dial shows the Kaufman efficiency ratio that widens them, and a pointer on a rod in front of the day beam follows the latest close; when two closes in a row sit beyond a jaw, a counter trips a flag.",
  body: [
    base(),
    rail(),
    `<g data-part="column" transform="${k.translateAlong(P, [px(s0.orange), 0, 0])}">${carriage()}</g>`,
    `<g data-part="prior">${lane(priorBins, PRIOR_Y, OB_Z, null)}</g>`,
    `<g data-part="ob">${beam(OB, OB_Z, OB_H, OB.y + 0.8, false)}</g>`,
    `<g data-part="today">${lane(todayBins, TODAY_Y, BB_Z, s0.i)}</g>`,
    `<g data-part="bb">${beam(BB, BB_Z, BB_H, BB.y + 0.8, false)}</g>`,
    `<g data-part="hanger" transform="${k.translateAlong(P, [px(s0.blue), 0, 0])}">${hanger()}<g data-part="jaws">${jawsSvg(kk, P, s0.band * PPD, BAR, BAR_Z, BLADE_W, BLADE_Y, BLADE_D, BLADE_Z0, BLADE_Z1, POST_W, SPRING_Y, SPRING_Z, SPRING_R)}</g></g>`,
    `<g data-part="rod">${pointerRod()}</g>`,
    `<g data-part="slider" transform="${k.translateAlong(P, [px(s0.close), 0, 0])}">${slider()}</g>`,
    kerDial(),
    `<path data-part="needle" class="iso-line" data-tone="lit" data-free="start" d="${needleD(kk, P, DIAL, DIAL_R, DIAL_TOP, DIAL_FROM, DIAL_SPAN, s0.ker ?? 0)}" style="stroke-width:1"/>`,
    counter(),
    `<path data-part="count" class="iso-line" data-tone="hi" data-free="start" d="${counterD(s0.run)}" style="stroke-width:0.9"/>`,
    `<g data-part="flag">${flagPlate()}</g>`,
  ],
});

// ── live data ────────────────────────────────────────────────────────────────────────────────────────
const stepData = STEPS.map((s) => ({
  m: s.mins, c: s.close, b: s.blue, o: s.orange, k: s.ker, band: s.band, run: s.run, beyond: s.beyond,
  trig: s.trig != null, entry: s.entry, stop: s.stopPx, target: s.target, R: s.R, tagged: s.tagged != null,
  stopped: s.stopped != null, blocked: s.regimeBlock, regime: s.regime, rth: s.rth, fake: s.fake,
}));
const todayDiscs = STEPS.map((s) => {
  const b = bars[s.i];
  const ok = b.mins >= ANCHOR_M && b.mins < CLOSE_M && b.v > 0;
  return ok ? [Math.round(((b.h + b.l + b.c) / 3) / BIN), b.v * SCALE] : null;
});

const readoutOf = (s) => {
  const t = hhmm(s.mins);
  const nums = `blue ${s.blue.toFixed(2)} · orange ${s.orange.toFixed(2)} · KER ${s.ker == null ? "–" : s.ker.toFixed(2)}`;
  let phase;
  if (!s.rth) phase = "premarket";
  else if (s.tagged != null) phase = "TAGGED ✓ orange";
  else if (s.stopped != null) phase = "STOPPED ✕";
  else if (s.trig != null) phase = s.regimeBlock ? "blocked · trend" : "TRIGGER ▼";
  else if (s.beyond) phase = `beyond ${s.run}/${K_BARS}`;
  else if (s.fake) phase = `fakeout ×${s.fake}`;
  else phase = `gap ▲ ${W.gapPct.toFixed(2)}%`;
  return `${t} · ${nums} · ${phase}`;
};

const live = `
const P = ${JSON.stringify(P)};
const STEPS = ${JSON.stringify(stepData)};
const DISCS = ${JSON.stringify(todayDiscs)};
const PPD = ${PPD}, P_MID = ${P_MID}, BIN = ${BIN}, K_BARS = ${K_BARS}, GAP = ${W.gapPct.toFixed(2)};
const TODAY_Y = ${TODAY_Y}, BB_Z = ${BB_Z}, HANG = ${HANG}, DISC_R = ${DISC_R}, ROD_R = ${ROD_R}, PLATE_R = ${PLATE_R}, PLATE_H = ${PLATE_H};
const BAR = ${JSON.stringify(BAR)}, BAR_Z = ${BAR_Z}, BLADE_W = ${BLADE_W}, BLADE_Y = ${BLADE_Y}, BLADE_D = ${BLADE_D}, BLADE_Z0 = ${BLADE_Z0}, BLADE_Z1 = ${BLADE_Z1}, POST_W = ${POST_W};
const SPRING_Y = ${SPRING_Y}, SPRING_Z = ${SPRING_Z}, SPRING_R = ${SPRING_R};
const DIAL = ${JSON.stringify(DIAL)}, DIAL_R = ${DIAL_R}, DIAL_TOP = ${DIAL_TOP}, DIAL_FROM = ${DIAL_FROM}, DIAL_SPAN = ${DIAL_SPAN};
const DRUM = ${JSON.stringify(DRUM)}, DRUM_R = ${DRUM_R}, DRUM_TOP = ${1.2 + DRUM_H}, FLAG_LIFT = ${FLAG_LIFT};
const OPEN_STEP = ${openStep}, TRIG_STEP = ${trigStep}, TAG_STEP = ${tagStep}, START = ${startStep};
const kk = { cylinder, solidSvg, ring, lineSvg, coil, slabOf, lineOnTop };
const px = (price) => (price - P_MID) * PPD;
const binX = (key) => px(key * BIN);
${stackSvg.toString()}
${jawsSvg.toString()}
${needleD.toString()}
const hhmm = (m) => String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
const stage = document.querySelector(".iso-stage");
const svg = stage.querySelector("svg");
const q = (name) => svg.querySelector('[data-part="' + name + '"]');
const column = q("column"), today = q("today"), hanger = q("hanger"), jaws = q("jaws"), slider = q("slider");
const needle = q("needle"), count = q("count"), flag = q("flag");
const readout = document.querySelector("[data-readout]");
const still = matchMedia("(prefers-reduced-motion: reduce)");
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const LAST = STEPS.length - 1;

function todaySvg(upTo) {
  const bins = new Map();
  for (let i = 0; i <= upTo; i++) { const d = DISCS[i]; if (!d) continue; if (!bins.has(d[0])) bins.set(d[0], []); bins.get(d[0]).push(d[1]); }
  const keys = [...bins.keys()].sort((a, b) => a - b);
  return keys.map((key) => stackSvg(kk, P, binX(key), TODAY_Y, BB_Z, HANG, bins.get(key), DISC_R, ROD_R, PLATE_R, PLATE_H, false)).join("");
}
function phaseOf(s) {
  if (!s.rth) return "premarket";
  if (s.tagged) return "TAGGED ✓ orange";
  if (s.stopped) return "STOPPED ✕";
  if (s.trig) return s.blocked ? "blocked · trend" : "TRIGGER ▼";
  if (s.beyond) return "beyond " + s.run + "/" + K_BARS;
  if (s.fake) return "fakeout ×" + s.fake;
  return "gap ▲ " + GAP + "%";
}
const readoutOf = (s) => hhmm(s.m) + " · blue " + s.b.toFixed(2) + " · orange " + s.o.toFixed(2) + " · KER " + (s.k == null ? "–" : s.k.toFixed(2)) + " · " + phaseOf(s);
const describe = (s) => hhmm(s.m) + ": close " + s.c.toFixed(2) + ", blue VWAP " + s.b.toFixed(2) + ", orange anchor " + s.o.toFixed(2) + ", efficiency ratio " + (s.k == null ? "unknown" : s.k.toFixed(2)) + ", " + phaseOf(s);

// the tour: the premarket flies by, regular hours walk, the trigger and the tag hold
const TOUR = [];
{
  let t = 0;
  for (let i = 0; i <= LAST; i++) {
    let dwell = i < OPEN_STEP ? 0.028 : 0.17;
    if (i === OPEN_STEP) dwell = 0.9;
    if (i === TRIG_STEP) dwell = 2.2;
    if (i === TAG_STEP) dwell = 2.2;
    if (i === LAST) dwell = 2.6;
    TOUR.push([t, i]); t += dwell;
  }
  TOUR.len = t;
}
const tourAt = (clock) => { const c = clock % TOUR.len; let step = 0; for (const [t, i] of TOUR) { if (t <= c) step = i; else break; } return step; };

let shown = START, target = START;
let ox = px(STEPS[START].o), bx = px(STEPS[START].b), cx = px(STEPS[START].c), band = STEPS[START].band * PPD, ker = STEPS[START].k ?? 0, run = STEPS[START].run, lift = 0;
let frame = 0, last = 0, clock = 0, touring = !still.matches, visible = false, idleTimer = 0, lastDrawn = -1, lastRead = "";

function draw() {
  const s = STEPS[shown];
  if (shown !== lastDrawn) { today.innerHTML = todaySvg(shown); lastDrawn = shown; }
  column.setAttribute("transform", translateAlong(P, [ox, 0, 0]));
  hanger.setAttribute("transform", translateAlong(P, [bx, 0, 0]));
  jaws.innerHTML = jawsSvg(kk, P, band, BAR, BAR_Z, BLADE_W, BLADE_Y, BLADE_D, BLADE_Z0, BLADE_Z1, POST_W, SPRING_Y, SPRING_Z, SPRING_R);
  slider.setAttribute("transform", translateAlong(P, [cx, 0, 0]));
  needle.setAttribute("d", needleD(kk, P, DIAL, DIAL_R, DIAL_TOP, DIAL_FROM, DIAL_SPAN, ker));
  count.setAttribute("d", needleD(kk, P, DRUM, DRUM_R, DRUM_TOP, -Math.PI * 2 / 3, Math.PI * 4 / 3, run / 2));
  flag.setAttribute("transform", translateAlong(P, [0, 0, lift * FLAG_LIFT]));
  const text = readoutOf(s);
  if (text !== lastRead) { readout.textContent = text; lastRead = text; }
}
function tick(now) {
  const dt = last ? Math.min((now - last) / 1000, 1 / 30) : 1 / 60;
  last = now;
  const calm = still.matches;
  if (touring) { clock += dt; target = tourAt(clock); }
  shown = target;
  const s = STEPS[shown];
  const want = { ox: px(s.o), bx: px(s.b), cx: px(s.c), band: s.band * PPD, ker: s.k ?? ker, run: s.run, lift: s.trig ? 1 : 0 };
  const ease = (v, w, tau) => (calm ? w : Math.abs(w - v) < 0.01 ? w : v + (w - v) * (1 - Math.exp(-dt / tau)));
  ox = ease(ox, want.ox, 0.32); bx = ease(bx, want.bx, 0.26); cx = ease(cx, want.cx, 0.18); band = ease(band, want.band, 0.3);
  ker = ease(ker, want.ker, 0.35); run = ease(run, want.run, 0.12); lift = ease(lift, want.lift, 0.2);
  draw();
  const settled = !touring && ox === want.ox && bx === want.bx && cx === want.cx && band === want.band && ker === want.ker && run === want.run && lift === want.lift;
  frame = visible && !settled ? requestAnimationFrame(tick) : 0;
}
const run_ = () => { if (!frame && visible) { last = 0; frame = requestAnimationFrame(tick); } };
const steer = (next, announce = true) => {
  touring = false; target = clamp(Math.round(next), 0, LAST);
  stage.setAttribute("aria-valuenow", String(target));
  if (announce) stage.setAttribute("aria-valuetext", describe(STEPS[target]));
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { touring = !still.matches; clock = TOUR[target][0]; run_(); }, 3600);
  run_();
};
new IntersectionObserver((entries) => { visible = entries[entries.length - 1].isIntersecting; if (visible) run_(); }, { rootMargin: "120px 0px" }).observe(stage);
const fromShare = (share) => (share < 0.1 ? share / 0.1 * OPEN_STEP : OPEN_STEP + (share - 0.1) / 0.9 * (LAST - OPEN_STEP));
stage.addEventListener("pointermove", (event) => {
  const box = svg.getBoundingClientRect();
  const share = clamp(((event.clientX - box.left) / box.width - 0.16) / 0.68, 0, 1);
  steer(fromShare(share), false);
});
stage.addEventListener("pointerdown", (event) => {
  const box = svg.getBoundingClientRect();
  const share = clamp(((event.clientX - box.left) / box.width - 0.16) / 0.68, 0, 1);
  steer(fromShare(share));
});
stage.addEventListener("pointerleave", () => steer(target, false));
stage.addEventListener("keydown", (event) => {
  const here = touring ? shown : target;
  const big = event.shiftKey ? 6 : 1;
  let to = null;
  if (event.key === "ArrowRight" || event.key === "ArrowUp") to = here + big;
  if (event.key === "ArrowLeft" || event.key === "ArrowDown") to = here - big;
  if (event.key === "Home") to = OPEN_STEP;
  if (event.key === "End") to = LAST;
  if (event.key === "t" || event.key === "T") to = TRIG_STEP;
  if (to === null) return;
  event.preventDefault();
  steer(to);
});
stage.addEventListener("focus", () => stage.setAttribute("aria-valuetext", describe(STEPS[shown])));
if (still.matches) touring = false;
draw();
`;

const body = k.plateHtml({
  fig: "Fig 1",
  title: "The VWAP balance",
  hint: "Hover to scrub · ← →",
  readout: readoutOf(s0),
  keys: [
    { mark: "lit", label: "Pointer: the latest 5-minute close" },
    { mark: "raised", label: "Slotted weights: one per bar, thick as its volume, hung at its typical price" },
    { mark: "edge", label: "Hanger foot: the day's balance point, blue VWAP · column: the two-day balance point, orange" },
  ],
  caption:
    `A two-beam balance, driven by the desk's engine on a synthetic gap day. Each bar hangs a weight as thick as its volume at its typical price, so the day beam's balance point is the blue VWAP and the two-day beam, carrying yesterday's weights, balances on the column at the orange anchor. The jaws open to ${SIGMA_MULT} σ of the hanging weights, wider when the KER dial reads chop; after ${K_BARS} closes in a row beyond the jaw the counter trips and the pointer's trade targets the column. On replay these fades lost money after costs: the figure shows the mechanism, not an edge.`,
  body: `<div class="iso-stage" tabindex="0" role="slider" aria-label="The VWAP balance: arrow keys step through the session bar by bar, Home is the open, End the close, T the trigger" aria-valuemin="0" aria-valuemax="${STEPS.length - 1}" aria-valuenow="${startStep}">${svg}</div>`,
});

const PHONE_CSS = `<style>@media (max-width:560px){.iso-plate-corner[data-corner="readout"]{white-space:normal;text-align:right;max-width:78%;line-height:1.5}.iso-plate-corner[data-corner="hint"]{bottom:34px}}</style>`;
const out = theme === "light" ? "light.html" : "index.html";
writeFileSync(join(HERE, out), k.pageHtml({ title: "The VWAP balance · VWAP Blue", theme, body: PHONE_CSS + body, script: kitScript() + live, width: 680 }));
const solids = (svg.match(/class="iso-solid/g) || []).length;
const paths = (svg.match(/<path/g) || []).length;
console.log(`wrote figure/${out} · ${solids} solids · ${paths} paths · trigger ${hhmm(STEPS[trigStep].mins)} · tag ${hhmm(STEPS[tagStep].mins)} · max stack ${(maxStack * SCALE).toFixed(1)}`);
