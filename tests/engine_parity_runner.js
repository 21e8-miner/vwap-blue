#!/usr/bin/env node
// Runs engine.js on the cases tests/test_engine_parity.py writes to stdin and prints JSON:
// {version, rows, grade_ok, geom_ok, looks_crypto}. Each row also carries the desk's scan fields (dollar_vol,
// illiquid, rotation score) computed the way app.run_scan does after the stale guard.
const E = require("../engine.js");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", chunk => { input += chunk; });
process.stdin.on("end", () => {
  const { cases, grades, floors, geometry, min_dvol: minDvol, tickers } = JSON.parse(input);
  const rows = cases.map(c => {
    const bars = E.prepBars(c.raw);
    const row = E.analyze(c.ticker, bars, { nowMs: c.now_ms });
    E.applyStaleGuard(row, E.barAgeMin(bars, c.now_ms), c.max_age_min);
    const dvol = E.sessionDollarVolume(bars, c.ticker);
    row.dollar_vol = dvol ? Math.round(dvol) : 0;
    row.illiquid = minDvol > 0 && dvol > 0 && dvol < E.dollarVolumeFloor(c.ticker, minDvol);
    row.rot = E.rotationScore(row);
    const markers = row._chart ? row._chart.markers : null;
    delete row._chart;
    return { ...row, markers };
  });
  const gradeOk = grades.map(g => floors.map(f => E.gradeOk(g, f)));
  const geomOk = geometry.map(([side, entry, stop, target]) => E.geomOk(side, entry, stop, target));
  const looksCrypto = tickers.map(E.looksCrypto);
  process.stdout.write(JSON.stringify({ version: E.VERSION, rows, grade_ok: gradeOk, geom_ok: geomOk,
                                        looks_crypto: looksCrypto }));
});
