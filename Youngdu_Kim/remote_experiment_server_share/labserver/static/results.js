// Result plots of an experiment run, shared by the experiments tab (index.html) and the
// interactive HTML export of a saved record (labserver/report.py), so both draw the same.
// The page calls resultInit(box) once, showResult({axis, rows}) whenever there is something
// new (the same object may grow), and resultReset() when it shows another run. Rows the
// export builds also carry `point`: every per-point value in the record, shown in a table
// under the plot for the point under the cursor.
"use strict";

const resEl = id => document.getElementById(id);
const RES = {data: null, mapCursor: null, mapPinned: false, mapGeom: null,
             gridSel: null, gridHover: null, gridGeom: null, specRow: null};

const RESULT_HTML = `
<div id="mapView">
<div class="row">show: <select id="mapQty"><option value="db">|S21| dB</option><option value="deg">phase deg</option></select>
  &nbsp;color: <label><input type="checkbox" id="mapAuto" checked> auto</label>
  <input type="number" id="mapMin" step="any" placeholder="min"> <input type="number" id="mapMax" step="any" placeholder="max">
  &nbsp;<span class="muted">hover the map for cuts, click to pin the cursor</span></div>
<canvas id="mapPlot"></canvas>
<div class="row" id="mapReadout"></div>
<div id="cuts"><canvas id="cutF"></canvas><canvas id="cutP"></canvas></div>
</div>
<div id="xyView" hidden>
<div class="row">show: <select id="xyQty"><option value="nw">nW</option><option value="dbm">dBm</option></select>
  &nbsp;<span class="muted">hover for values; the newest point is circled</span></div>
<canvas id="xyPlot"></canvas>
<div class="row" id="xyReadout"></div>
</div>
<div id="gridView" hidden>
<div class="row">show: <select id="gridQty"><option value="gain_db">gain dB</option><option value="snri_db">SNRI dB</option>
  <option value="noise_rise_db">noise rise dB</option><option value="snr_db">SNR dB</option>
  <option value="signal_dbm">signal dBm</option><option value="noise_dbm">noise dBm</option></select>
  &nbsp;color: <label><input type="checkbox" id="gridAuto" checked> auto</label>
  <input type="number" id="gridMin" step="any" placeholder="min"> <input type="number" id="gridMax" step="any" placeholder="max">
  &nbsp;<span class="muted">hover a point for its values and spectrum, click to pin it, click again to follow the newest</span></div>
<canvas id="gridPlot"></canvas>
<div class="row" id="gridReadout"></div>
<canvas id="specPlot"></canvas>
</div>
<div id="spectraView" hidden>
<div class="row">show: <select id="spectraQty"><option value="spectra">spectra</option>
  <option value="y_pump_off">hot-wire Y, pump off</option><option value="y_pump_on">hot-wire Y, pump on</option>
  <option value="gain_db">gain dB</option></select>
  &nbsp;smooth <input type="number" id="spectraSmooth" min="1" step="2" value="1" style="width: 4em"> points
  &nbsp;<span class="muted">hover for values; the averages so far over all rounds</span></div>
<canvas id="spectraPlot"></canvas>
<div class="row" id="spectraReadout"></div>
</div>
<div id="resultPoint"></div>`;

function resultInit(box) {
  box.innerHTML = RESULT_HTML;
  for (const id of ["mapQty", "mapAuto", "mapMin", "mapMax"]) resEl(id).onchange = drawMap;
  for (const id of ["gridQty", "gridAuto", "gridMin", "gridMax"]) resEl(id).onchange = drawGrid;
  resEl("xyQty").onchange = drawResult;
  for (const id of ["spectraQty", "spectraSmooth"]) resEl(id).onchange = () => drawSpectra();
  const map = resEl("mapPlot");
  map.onmousemove = e => { if (!RES.mapPinned) { const c = mapPick(e); if (c) { RES.mapCursor = c; drawMap(); } } };
  map.onmouseleave = () => { if (!RES.mapPinned) { RES.mapCursor = null; drawMap(); } };
  map.onclick = e => { const c = mapPick(e); if (!c) return; RES.mapPinned = !RES.mapPinned; RES.mapCursor = c; drawMap(); };
  const grid = resEl("gridPlot");
  grid.onmousemove = e => { const k = gridPick(e); if (k !== RES.gridHover) { RES.gridHover = k; drawGrid(); } };
  grid.onmouseleave = () => { RES.gridHover = null; drawGrid(); };
  grid.onclick = e => { const k = gridPick(e); if (k === null) return; RES.gridSel = RES.gridSel === k ? null : k; drawGrid(); };
  for (const id of ["cutF", "cutP"]) attachHover(resEl(id), c => { if (c._xy) drawXY(c, ...c._xy); });
  attachHover(resEl("xyPlot"), drawXyResult);
  attachHover(resEl("specPlot"), () => drawSpectrum(RES.specRow));
  attachHover(resEl("spectraPlot"), drawSpectra);
}
function resultReset() {
  Object.assign(RES, {mapCursor: null, mapPinned: false, gridSel: null, gridHover: null, gridQtySet: false});
}
function showResult(data) {
  RES.data = data;
  // grid: only the quantities the run has (axis.quantities, the first is the default)
  const ax = data && data.axis, q = resEl("gridQty");
  if (ax && ax.plot === "grid") {
    const keep = ax.quantities || Object.keys(GRID_QTY);
    for (const o of q.options) o.hidden = !keep.includes(o.value);
    if (!RES.gridQtySet || !keep.includes(q.value)) { q.value = keep[0]; RES.gridQtySet = true; }
  }
  drawResult();
}

// mouse position on a canvas in c.hover (null outside), then redraw(c)
function attachHover(c, redraw) {
  c.hover = null;
  c.addEventListener("mousemove", e => { const r = c.getBoundingClientRect(); c.hover = {x: e.clientX - r.left, y: e.clientY - r.top}; redraw(c); });
  c.addEventListener("mouseleave", () => { c.hover = null; redraw(c); });
}

// ---------------------------------------------------------------- plot helpers
function ticks(lo, hi, n) {
  const span = hi - lo || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map(m => m * mag).find(s => s >= raw);
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) out.push(t);
  return out;
}
function label(v) {
  const a = Math.abs(v);
  return a !== 0 && (a < 1e-3 || a >= 1e5) ? v.toExponential(2) : String(+v.toPrecision(6));
}
function prepCanvas(c) {
  const dpr = window.devicePixelRatio || 1, W = c.clientWidth, H = c.clientHeight;
  if (c.width !== W * dpr || c.height !== H * dpr) { c.width = W * dpr; c.height = H * dpr; }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = "#fff"; g.fillRect(0, 0, W, H);
  g.font = "12px Courier New"; g.fillStyle = "#000"; g.strokeStyle = "#000"; g.lineWidth = 1;
  return [g, W, H];
}
function edges(v) {           // cell edges around sorted centers
  if (v.length === 1) return [v[0] - 0.5, v[0] + 0.5];
  const e = [v[0] - (v[1] - v[0]) / 2];
  for (let i = 0; i < v.length - 1; i++) e.push((v[i] + v[i + 1]) / 2);
  return [...e, v.at(-1) + (v.at(-1) - v.at(-2)) / 2];
}
// text in a white box at the top right inside the frame (the value under the cursor)
function hoverText(g, text, right, top) {
  const w = g.measureText(text).width;
  g.fillStyle = "#fff"; g.fillRect(right - w - 8, top + 2, w + 6, 16);
  g.fillStyle = "#000"; g.textAlign = "right"; g.fillText(text, right - 5, top + 14);
}
// line plot; mark: index circled in red; with c.hover (attachHover) the nearest point is
// marked and its values shown. Returns that index (undefined without a hover).
function drawXY(c, xs, ys, xLabel, yLabel, title, mark, dots) {
  c._xy = [xs, ys, xLabel, yLabel, title, mark, dots];
  const [g, W, H] = prepCanvas(c);
  if (!xs || !xs.length) { g.fillText("no data", 20, 30); return undefined; }
  const L = 80, R = 12, T = 22, B = 36, pw = W - L - R, ph = H - T - B;
  let x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (x1 === x0) { x0 -= 0.5; x1 += 0.5; }
  const pad = (y1 - y0) * 0.05 || Math.abs(y0) * 0.05 || 1; y0 -= pad; y1 += pad;
  const X = v => L + (v - x0) / (x1 - x0) * pw, Y = v => T + (1 - (v - y0) / (y1 - y0)) * ph;
  g.strokeStyle = "#ddd"; g.beginPath(); g.textAlign = "center";
  for (const t of ticks(x0, x1, 6)) { g.moveTo(X(t), T); g.lineTo(X(t), T + ph); g.fillText(label(t), X(t), T + ph + 14); }
  g.textAlign = "right";
  for (const t of ticks(y0, y1, 5)) { g.moveTo(L, Y(t)); g.lineTo(L + pw, Y(t)); g.fillText(label(t), L - 4, Y(t) + 4); }
  g.stroke(); g.strokeStyle = "#000"; g.strokeRect(L, T, pw, ph);
  g.textAlign = "center"; g.fillText(xLabel, L + pw / 2, H - 4);
  g.save(); g.translate(12, T + ph / 2); g.rotate(-Math.PI / 2); g.fillText(yLabel, 0, 0); g.restore();
  g.textAlign = "left"; g.fillText(title, L, T - 7);
  g.save(); g.beginPath(); g.rect(L, T, pw, ph); g.clip();
  g.beginPath();
  for (let k = 0; k < xs.length; k++) { const px = X(xs[k]), py = Y(ys[k]); k ? g.lineTo(px, py) : g.moveTo(px, py); }
  g.stroke();
  if (dots) for (let k = 0; k < xs.length; k++) { g.beginPath(); g.arc(X(xs[k]), Y(ys[k]), 2, 0, 2 * Math.PI); g.fill(); }
  if (mark !== undefined) {
    g.strokeStyle = "#d00"; g.lineWidth = 2; g.beginPath(); g.arc(X(xs[mark]), Y(ys[mark]), 4, 0, 2 * Math.PI); g.stroke();
  }
  let hit;
  if (c.hover && c.hover.x >= L && c.hover.x <= L + pw) {
    // nearest in x; of points at the same x (e.g. the passes of a sweep), the nearest in y
    const dx = k => Math.abs(X(xs[k]) - c.hover.x), dy = k => Math.abs(Y(ys[k]) - c.hover.y);
    hit = 0;
    for (let k = 1; k < xs.length; k++)
      if (dx(k) < dx(hit) - 0.5 || (Math.abs(dx(k) - dx(hit)) <= 0.5 && dy(k) < dy(hit))) hit = k;
    g.strokeStyle = "#000"; g.lineWidth = 1; g.setLineDash([3, 3]);
    g.beginPath(); g.moveTo(X(xs[hit]), T); g.lineTo(X(xs[hit]), T + ph); g.stroke(); g.setLineDash([]);
    g.beginPath(); g.arc(X(xs[hit]), Y(ys[hit]), 5, 0, 2 * Math.PI); g.stroke();
  }
  g.restore();
  if (hit !== undefined) hoverText(g, `${label(xs[hit])}, ${label(ys[hit])}`, L + pw, T);
  return hit;
}

// ---------------------------------------------------------------- which plot
// sweeps that record one number per point (axis.plot = "xy", e.g. the Y factor measurement)
// get a plot of it vs the swept setting; sweeps over a grid of two settings with a spectrum
// at each point ("grid", the TWPA pump sweep) get a map of one number and the spectrum;
// the VNA sweeps get the 2D map
function drawResult() {
  const plot = RES.data && RES.data.axis ? RES.data.axis.plot : null;
  const xy = plot === "xy", grid = plot === "grid", spectra = plot === "spectra";
  resEl("mapView").hidden = xy || grid || spectra || plot === "none"; resEl("xyView").hidden = !xy;   // "none": no plot
  resEl("gridView").hidden = !grid; resEl("spectraView").hidden = !spectra;
  if (plot === "none") { showPoint(null); return; }
  xy ? drawXyResult() : grid ? drawGrid() : spectra ? drawSpectra() : drawMap();
}

// every value the record has for one point (rows from the export only)
function fmtPoint(key, v) {
  if (v === null || v === undefined) return "-";
  if (typeof v !== "number") return String(v);
  if (/_time$/.test(key) && v > 1e9) {         // epoch s -> local YYYY-MM-DD HH:MM:SS.mmm
    const d = new Date(v * 1000), z = (n, w = 2) => String(n).padStart(w, "0");
    return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())} ${z(d.getHours())}:${z(d.getMinutes())}:`
      + `${z(d.getSeconds())}.${z(d.getMilliseconds(), 3)}`;
  }
  if (/_hz$/.test(key)) return `${+(v / 1e9).toPrecision(10)} GHz`;
  if (/_w$/.test(key) && v > 0) return `${label(v)} W (${(10 * Math.log10(v / 1e-3)).toFixed(3)} dBm)`;
  return label(v);
}
function showPoint(row, what) {
  const box = resEl("resultPoint"); box.innerHTML = "";
  if (!row || !row.point) return;
  const cells = document.createElement("div"); cells.className = "cells";
  for (const [k, v] of Object.entries(row.point)) {
    const c = document.createElement("div"), name = document.createElement("span");
    name.textContent = k; c.append(name, fmtPoint(k, v)); cells.append(c);
  }
  const cap = document.createElement("div");
  cap.textContent = `${what}: point ${row.point_index + 1} of ${RES.data.rows.length} (in the order measured), as saved`;
  box.append(cap, cells);
}

// ---------------------------------------------------------------- XY (one number per point)
function drawXyResult() {
  const ax = RES.data.axis, rows = RES.data.rows, dbm = resEl("xyQty").value === "dbm";
  if (!rows.length) {
    const [g] = prepCanvas(resEl("xyPlot")); g.fillText("waiting for the first point...", 20, 30);
    resEl("xyReadout").textContent = ""; showPoint(null); return;
  }
  const val = w => dbm ? 10 * Math.log10(Math.max(w, 1e-30) / 1e-3) : w / 1e-9;
  const unit = dbm ? "dBm" : "nW";
  const hit = drawXY(resEl("xyPlot"), rows.map(r => r[ax.x_key]), rows.map(r => val(r[ax.y_key])),
                     `${ax.x_label} [${ax.x_unit}]`, `${ax.y_label} [${unit}]`,
                     (ax.title ? ax.title + "; " : "") + "in the order measured", rows.length - 1, true);
  const r = rows[hit ?? rows.length - 1], what = hit === undefined ? "newest" : "cursor";
  resEl("xyReadout").textContent = `${what}: ${ax.x_label} = ${r[ax.x_key]} ${ax.x_unit}`
    + (r.pass ? ` (pass ${r.pass + 1})` : "") + `, ${ax.y_label} = ${label(val(r[ax.y_key]))} ${unit}`
    + (dbm || r.power_std_w === undefined ? "" : ` (sample std ${label(val(r.power_std_w))} nW)`)
    + (r.traces ? `, ${r.traces} trace(s)` : "");
  showPoint(r, what);
}

// ---------------------------------------------------------------- 2D map
// x = frequency, y = the swept setting (axis.y_key: VNA power, flux bias, ...),
// gray scale (dark = low), like the notebook's Greys_r
function mapData() {
  const data = RES.data;
  if (!data || !data.axis || !data.rows.length) return null;
  const ax = data.axis, key = ax.y_key || "power_dbm";
  resEl("mapQty").hidden = !!ax.no_phase;        // SA sweeps: power only, no phase
  if (ax.no_phase) resEl("mapQty").value = "db";
  const deg = resEl("mapQty").value === "deg";
  const rows = [...data.rows].sort((a, b) => a[key] - b[key]);
  // stepping the warm attenuator scales S21 itself (it is in the measured path): add it back
  // so the map shows the qubit, not the attenuation; the saved data stays as measured.
  // axis.z_label / z_label_att name the quantity when it is not the VNA's S21
  const att = key === "attenuation_db" && !deg;
  return {f: ax.x.map(v => v / 1e9), p: rows.map(r => r[key]), rows, key,
          pLabel: ax.y_label || "VNA power", pUnit: ax.y_unit ?? "dBm",
          z: rows.map(r => deg ? r.deg : att ? r.s_db.map(v => v + r[key]) : r.s_db),
          zLabel: deg ? "phase [deg]" : att ? (ax.z_label_att || "|S21| + attenuation [dB]") : (ax.z_label || "|S21| [dB]")};
}
function mapCursorCell(d) {   // {i: row, j: column}; default = newest row, at the dip of the lowest row
  let i, j;
  if (RES.mapCursor) {
    i = d.p.reduce((best, v, k) => Math.abs(v - RES.mapCursor.p) < Math.abs(d.p[best] - RES.mapCursor.p) ? k : best, 0);
    j = RES.mapCursor.j;
  } else {
    i = d.p.indexOf(RES.data.rows.at(-1)[d.key]);
    const low = d.rows[0].s_db;
    j = low.indexOf(Math.min(...low));
  }
  return {i, j: Math.min(Math.max(j, 0), d.f.length - 1)};
}
function drawMap() {
  const [g, W, H] = prepCanvas(resEl("mapPlot"));
  const d = mapData();
  RES.mapGeom = null;
  if (!d) {
    g.fillText(RES.data ? "waiting for the first point..." : "no result yet", 20, 30);
    resEl("mapReadout").textContent = ""; drawXY(resEl("cutF")); drawXY(resEl("cutP")); showPoint(null);
    return;
  }
  const L = 80, R = 95, T = 10, B = 40, pw = W - L - R, ph = H - T - B;
  const fe = edges(d.f), pe = edges(d.p), nf = d.f.length, np = d.p.length;
  let z0 = Infinity, z1 = -Infinity;
  if (resEl("mapAuto").checked) { for (const row of d.z) for (const v of row) { if (v < z0) z0 = v; if (v > z1) z1 = v; } }
  else { z0 = parseFloat(resEl("mapMin").value); z1 = parseFloat(resEl("mapMax").value); }
  if (!(z1 > z0)) z1 = z0 + 1;
  const X = v => L + (v - fe[0]) / (fe.at(-1) - fe[0]) * pw, Y = v => T + (1 - (v - pe[0]) / (pe.at(-1) - pe[0])) * ph;
  const gray = v => Math.round(255 * Math.min(1, Math.max(0, (v - z0) / (z1 - z0))));

  const off = document.createElement("canvas"); off.width = nf; off.height = np;
  const img = off.getContext("2d").createImageData(nf, np);
  for (let i = 0; i < np; i++) for (let j = 0; j < nf; j++) {
    const k = (i * nf + j) * 4, c = gray(d.z[i][j]);
    img.data[k] = img.data[k + 1] = img.data[k + 2] = c; img.data[k + 3] = 255;
  }
  off.getContext("2d").putImageData(img, 0, 0);
  g.imageSmoothingEnabled = false;
  for (let i = 0; i < np; i++) {     // each row gets the band between its neighbours (steps can differ)
    const top = Y(pe[i + 1]), bottom = Y(pe[i]);
    g.drawImage(off, 0, i, nf, 1, L, top, pw, Math.max(1, bottom - top + 0.5));
  }
  g.strokeRect(L, T, pw, ph);
  g.textAlign = "center";
  for (const t of ticks(fe[0], fe.at(-1), 8)) { g.fillText(label(t), X(t), T + ph + 14); g.beginPath(); g.moveTo(X(t), T + ph); g.lineTo(X(t), T + ph + 4); g.stroke(); }
  g.fillText("frequency [GHz]", L + pw / 2, H - 6);
  g.textAlign = "right";
  for (const t of ticks(pe[0], pe.at(-1), 8)) { g.fillText(label(t), L - 6, Y(t) + 4); g.beginPath(); g.moveTo(L - 4, Y(t)); g.lineTo(L, Y(t)); g.stroke(); }
  g.save(); g.translate(14, T + ph / 2); g.rotate(-Math.PI / 2); g.textAlign = "center"; g.fillText(`${d.pLabel} [${d.pUnit}]`, 0, 0); g.restore();
  // color bar
  const cx = L + pw + 14, cw = 14;
  for (let y = 0; y < ph; y++) { const c = Math.round(255 * (1 - y / ph)); g.fillStyle = `rgb(${c},${c},${c})`; g.fillRect(cx, T + y, cw, 1.5); }
  g.fillStyle = "#000"; g.strokeRect(cx, T, cw, ph); g.textAlign = "left";
  for (const t of ticks(z0, z1, 6)) g.fillText(label(t), cx + cw + 4, T + (1 - (t - z0) / (z1 - z0)) * ph + 4);
  g.save(); g.translate(W - 6, T + ph / 2); g.rotate(-Math.PI / 2); g.textAlign = "center"; g.fillText(d.zLabel, 0, 0); g.restore();

  const {i, j} = mapCursorCell(d);
  g.strokeStyle = "#d00"; g.setLineDash(RES.mapPinned ? [] : [4, 3]); g.beginPath();
  g.moveTo(X(d.f[j]), T); g.lineTo(X(d.f[j]), T + ph); g.moveTo(L, Y(d.p[i])); g.lineTo(L + pw, Y(d.p[i])); g.stroke();
  g.setLineDash([]);
  RES.mapGeom = {L, T, pw, ph, fe, pe, d};
  const what = RES.mapPinned ? "pinned" : "cursor";
  resEl("mapReadout").textContent = `${what}: f = ${d.f[j].toFixed(6)} GHz, ${d.pLabel} = ${d.p[i]} ${d.pUnit} `
    + `(${d.rows[i].averages} traces), ${d.zLabel} = ${label(d.z[i][j])}`;
  drawXY(resEl("cutF"), d.f, d.z[i], "frequency [GHz]", d.zLabel, `at ${d.p[i]} ${d.pUnit}`, j, false);
  drawXY(resEl("cutP"), d.p, d.z.map(r => r[j]), `${d.pLabel} [${d.pUnit}]`, d.zLabel, `at ${d.f[j].toFixed(6)} GHz`, i, true);
  showPoint(d.rows[i], what);
}
function mapPick(e) {
  if (!RES.mapGeom) return null;
  const r = resEl("mapPlot").getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  const {L, T, pw, ph, fe, pe, d} = RES.mapGeom;
  if (x < L || x > L + pw || y < T || y > T + ph) return null;
  const f = fe[0] + (x - L) / pw * (fe.at(-1) - fe[0]), p = pe.at(-1) - (y - T) / ph * (pe.at(-1) - pe[0]);
  let j = 0, i = 0;
  for (let k = 1; k < d.f.length; k++) if (Math.abs(d.f[k] - f) < Math.abs(d.f[j] - f)) j = k;
  for (let k = 1; k < d.p.length; k++) if (Math.abs(d.p[k] - p) < Math.abs(d.p[i] - p)) i = k;
  return {p: d.p[i], j};
}

// ---------------------------------------------------------------- grid result
// x, y = two swept settings (axis.x_key / y_key), one number per point (gridQty) as a gray
// map (dark = low) or, with one value on an axis, a line; below it the spectrum of the point
// under the cursor (else the pinned one, else the newest) over the pump-off reference, with
// the noise region and signal window. The reference is one spectrum (axis.ref, the TWPA pump
// sweep) or each point's own (row.ref_trace_dbm, axis.per_point_ref: the TWPA signal sweep,
// whose rows also carry their SA center, sa_center_hz)
const GRID_QTY = {gain_db: "gain [dB]", snri_db: "SNRI [dB]", noise_rise_db: "noise rise [dB]",
                  snr_db: "SNR [dB]", signal_dbm: "signal [dBm]", noise_dbm: "noise [dBm]"};
function gridData() {
  const ax = RES.data.axis, rows = RES.data.rows, key = resEl("gridQty").value, xs = ax.x_scale || 1;
  const xv = rows.map(r => +(r[ax.x_key] / xs).toPrecision(12)), yv = rows.map(r => r[ax.y_key]);
  const uniq = v => [...new Set(v)].sort((a, b) => a - b);
  return {ax, rows, key, xv, yv, z: rows.map(r => r[key] ?? null), xu: uniq(xv), yu: uniq(yv), zLabel: GRID_QTY[key],
          xLabel: `${ax.x_label} [${ax.x_unit}]`, yLabel: `${ax.y_label} [${ax.y_unit}]`};
}
function fmtVal(v, unit) { return v === null || v === undefined ? "-" : `${label(+v.toFixed(3))} ${unit}`; }
function drawGrid() {
  RES.gridGeom = null;
  if (!RES.data.rows.length) {
    const [g] = prepCanvas(resEl("gridPlot")); g.fillText("waiting for the first point (after the pump-off reference)...", 20, 30);
    resEl("gridReadout").textContent = ""; drawSpectrum(null); showPoint(null); return;
  }
  const d = gridData(), shown = RES.gridHover ?? RES.gridSel ?? d.rows.length - 1;
  const what = RES.gridHover !== null ? "cursor" : RES.gridSel !== null ? "pinned" : "newest";
  if (d.xu.length === 1 || d.yu.length === 1) {      // one value on an axis: a line vs the other one
    const alongX = d.xu.length > 1, av = alongX ? d.xv : d.yv;
    const idx = d.rows.map((_, k) => k).filter(k => d.z[k] !== null).sort((a, b) => av[a] - av[b]);
    drawXY(resEl("gridPlot"), idx.map(k => av[k]), idx.map(k => d.z[k]), alongX ? d.xLabel : d.yLabel, d.zLabel,
           (alongX ? `at ${d.yu[0]} ${d.ax.y_unit}` : `at ${d.xu[0]} ${d.ax.x_unit}`) + (d.ax.title ? "; " + d.ax.title : ""),
           idx.indexOf(shown) >= 0 ? idx.indexOf(shown) : undefined, true);
    RES.gridGeom = {line: true, L: 80, pw: resEl("gridPlot").clientWidth - 92, idx, av};
  } else drawGridMap(d, shown);
  const r = d.rows[shown], own = r.ref_snr_db !== undefined;
  const ref = own ? {snr_db: r.ref_snr_db, signal_dbm: r.ref_signal_dbm, noise_dbm: r.ref_noise_dbm} : d.ax.ref;
  resEl("gridReadout").textContent = `${what}: ${d.ax.x_label} ${d.xv[shown]} ${d.ax.x_unit}, `
    + `${d.ax.y_label} ${d.yv[shown]} ${d.ax.y_unit}: `
    + (r.gain_db !== undefined ? `gain ${fmtVal(r.gain_db, "dB")}, SNRI ${fmtVal(r.snri_db, "dB")}, `
       + `noise rise ${fmtVal(r.noise_rise_db, "dB")}, ` : "")
    + `SNR ${fmtVal(r.snr_db, "dB")} (signal ${fmtVal(r.signal_dbm, "dBm")}, noise ${fmtVal(r.noise_dbm, "dBm")})`
    + (ref ? `  |  pump-off ${own ? "at this point" : "reference"}: SNR ${fmtVal(ref.snr_db, "dB")} `
       + `(signal ${fmtVal(ref.signal_dbm, "dBm")}, noise ${fmtVal(ref.noise_dbm, "dBm")})` : "");
  drawSpectrum(r);
  showPoint(r, what);
}
function drawGridMap(d, shown) {
  const [g, W, H] = prepCanvas(resEl("gridPlot"));
  const L = 80, R = 95, T = 22, B = 40, pw = W - L - R, ph = H - T - B;
  const xe = edges(d.xu), ye = edges(d.yu);
  let z0 = Infinity, z1 = -Infinity;
  if (resEl("gridAuto").checked) { for (const v of d.z) if (v !== null) { if (v < z0) z0 = v; if (v > z1) z1 = v; } }
  else { z0 = parseFloat(resEl("gridMin").value); z1 = parseFloat(resEl("gridMax").value); }
  if (!isFinite(z0)) z0 = 0;
  if (!(z1 > z0)) z1 = z0 + 1;
  const X = v => L + (v - xe[0]) / (xe.at(-1) - xe[0]) * pw, Y = v => T + (1 - (v - ye[0]) / (ye.at(-1) - ye[0])) * ph;
  const cell = k => {
    const i = d.xu.indexOf(d.xv[k]), j = d.yu.indexOf(d.yv[k]);
    return [X(xe[i]), Y(ye[j + 1]), X(xe[i + 1]) - X(xe[i]), Y(ye[j]) - Y(ye[j + 1])];
  };
  g.fillStyle = "#dde8f0"; g.fillRect(L, T, pw, ph);         // not measured (yet)
  d.rows.forEach((_, k) => {
    const v = d.z[k];
    if (v === null) g.fillStyle = "#f4c0c0";                   // undefined: the tone is not above the noise
    else { const c = Math.round(255 * Math.min(1, Math.max(0, (v - z0) / (z1 - z0)))); g.fillStyle = `rgb(${c},${c},${c})`; }
    const [x, y, w, h] = cell(k); g.fillRect(x, y, w + 0.5, h + 0.5);
  });
  g.fillStyle = "#000"; g.strokeStyle = "#000"; g.strokeRect(L, T, pw, ph);
  g.textAlign = "center";
  for (const t of ticks(xe[0], xe.at(-1), 8)) { g.fillText(label(t), X(t), T + ph + 14); g.beginPath(); g.moveTo(X(t), T + ph); g.lineTo(X(t), T + ph + 4); g.stroke(); }
  g.fillText(d.xLabel, L + pw / 2, H - 6);
  g.textAlign = "left"; if (d.ax.title) g.fillText(d.ax.title, L, T - 7);
  g.textAlign = "right";
  for (const t of ticks(ye[0], ye.at(-1), 8)) { g.fillText(label(t), L - 6, Y(t) + 4); g.beginPath(); g.moveTo(L - 4, Y(t)); g.lineTo(L, Y(t)); g.stroke(); }
  g.save(); g.translate(14, T + ph / 2); g.rotate(-Math.PI / 2); g.textAlign = "center"; g.fillText(d.yLabel, 0, 0); g.restore();
  const cx = L + pw + 14, cw = 14;            // color bar
  for (let y = 0; y < ph; y++) { const c = Math.round(255 * (1 - y / ph)); g.fillStyle = `rgb(${c},${c},${c})`; g.fillRect(cx, T + y, cw, 1.5); }
  g.fillStyle = "#000"; g.strokeRect(cx, T, cw, ph); g.textAlign = "left";
  for (const t of ticks(z0, z1, 6)) g.fillText(label(t), cx + cw + 4, T + (1 - (t - z0) / (z1 - z0)) * ph + 4);
  g.save(); g.translate(W - 6, T + ph / 2); g.rotate(-Math.PI / 2); g.textAlign = "center"; g.fillText(d.zLabel, 0, 0); g.restore();
  if (RES.gridSel !== null && RES.gridSel !== shown) {        // the pinned point while another is hovered
    const [x, y, w, h] = cell(RES.gridSel);
    g.strokeStyle = "#d00"; g.lineWidth = 1; g.strokeRect(x + 1, y + 1, w - 2, h - 2);
  }
  const [x, y, w, h] = cell(shown);
  g.strokeStyle = "#d00"; g.lineWidth = 2; g.setLineDash(shown === RES.gridSel ? [] : [4, 3]); g.strokeRect(x + 1, y + 1, w - 2, h - 2);
  g.setLineDash([]); g.lineWidth = 1;
  RES.gridGeom = {L, T, pw, ph, xe, ye, d};
}
function drawSpectrum(r) {
  RES.specRow = r;
  const c = resEl("specPlot"), [g, W, H] = prepCanvas(c);
  const ax = RES.data && RES.data.axis;
  const own = r && r.trace_dbm, pointRef = r && r.ref_trace_dbm;
  const refTr = pointRef || (ax && ax.ref && ax.ref.trace_dbm);
  if (!ax || !ax.f || !(refTr || own)) {
    g.fillText(!ax || !ax.per_point_ref ? "spectrum: waiting for the pump-off reference..."
               : r ? "spectrum: not saved for this run" : "spectrum: waiting for the first point...", 20, 30);
    return;
  }
  // the bins are the same offsets from the SA center at every point; the center may move
  const fsa = (r && r.sa_center_hz) ?? ax.f_sa, shift = fsa - ax.f_sa;
  const xs = ax.f.map(f => (f - ax.f_sa) / 1e6);
  const named = [...(refTr ? [["pump off", refTr, "#999"]] : []), ...(own ? [["this point", own, "#000"]] : [])];
  const traces = named.map(t => t[1]);
  const L = 80, R = 12, T = 22, B = 36, pw = W - L - R, ph = H - T - B;
  const x0 = xs[0], x1 = xs.at(-1);
  let y0 = Infinity, y1 = -Infinity;
  for (const tr of traces) for (const v of tr) { if (v < y0) y0 = v; if (v > y1) y1 = v; }
  const pad = (y1 - y0) * 0.05 || 1; y0 -= pad; y1 += pad;
  const X = v => L + (v - x0) / (x1 - x0) * pw, Y = v => T + (1 - (v - y0) / (y1 - y0)) * ph;
  const lo = ax.noise_inner_hz / 1e6, hi = ax.noise_outer_hz / 1e6, fp = ((ax.f_signal ?? ax.f_probe) - ax.f_sa) / 1e6, win = ax.window_hz / 1e6;
  g.fillStyle = "#dde8f0";                                      // noise region, both sides
  for (const [a, b] of [[-hi, -lo], [lo, hi]]) g.fillRect(X(Math.max(a, x0)), T, X(Math.min(b, x1)) - X(Math.max(a, x0)), ph);
  g.fillStyle = "#f4d4d4"; g.fillRect(X(fp - win), T, Math.max(1, X(fp + win) - X(fp - win)), ph);    // signal window
  g.fillStyle = "#000"; g.strokeStyle = "#ddd"; g.beginPath(); g.textAlign = "center";
  for (const t of ticks(x0, x1, 8)) { g.moveTo(X(t), T); g.lineTo(X(t), T + ph); g.fillText(label(t), X(t), T + ph + 14); }
  g.textAlign = "right";
  for (const t of ticks(y0, y1, 5)) { g.moveTo(L, Y(t)); g.lineTo(L + pw, Y(t)); g.fillText(label(t), L - 4, Y(t) + 4); }
  g.stroke(); g.strokeStyle = "#000"; g.strokeRect(L, T, pw, ph);
  g.textAlign = "center"; g.fillText(`frequency - SA frequency (${+(fsa / 1e9).toPrecision(12)} GHz) [MHz]`, L + pw / 2, H - 4);
  g.save(); g.translate(12, T + ph / 2); g.rotate(-Math.PI / 2); g.fillText("power per bin [dBm]", 0, 0); g.restore();
  g.textAlign = "left";
  const at = r ? `${+(r[ax.x_key] / (ax.x_scale || 1)).toPrecision(12)} ${ax.x_unit} ${r[ax.y_key]} ${ax.y_unit}` : "";
  const what = [...(refTr ? [pointRef ? "gray: pump off, same point" : "gray: pump off"] : []),
                ...(own ? [`black: ${at}`] : r ? [`(the spectrum at ${at} was not saved)`] : [])];
  g.fillText(what.join(", ") + "; blue: noise, red: signal window", L, T - 7);
  g.save(); g.beginPath(); g.rect(L, T, pw, ph); g.clip();
  for (const [, tr, color] of named) {
    g.strokeStyle = color; g.beginPath();
    tr.forEach((v, k) => k ? g.lineTo(X(xs[k]), Y(v)) : g.moveTo(X(xs[k]), Y(v))); g.stroke();
  }
  let hit;
  if (c.hover && c.hover.x >= L && c.hover.x <= L + pw) {       // the bin under the cursor
    hit = 0;
    for (let k = 1; k < xs.length; k++) if (Math.abs(X(xs[k]) - c.hover.x) < Math.abs(X(xs[hit]) - c.hover.x)) hit = k;
    g.strokeStyle = "#000"; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X(xs[hit]), T); g.lineTo(X(xs[hit]), T + ph); g.stroke();
    g.setLineDash([]);
  }
  g.restore();
  if (hit !== undefined)
    hoverText(g, `${label(xs[hit])} MHz (${+((ax.f[hit] + shift) / 1e9).toPrecision(10)} GHz): `
              + named.map(([name, tr]) => `${name} ${tr[hit]} dBm`).join(", "), L + pw, T);
}
function gridPick(e) {           // index of the row under the mouse, or null
  if (!RES.gridGeom) return null;
  const r = resEl("gridPlot").getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  if (RES.gridGeom.line) {       // nearest point along the line
    const {L, pw, idx, av} = RES.gridGeom;
    if (!idx.length) return null;
    const lo = Math.min(...idx.map(k => av[k])), hi = Math.max(...idx.map(k => av[k]));
    const v = hi > lo ? lo + (x - L) / pw * (hi - lo) : lo;
    return idx.reduce((best, k) => Math.abs(av[k] - v) < Math.abs(av[best] - v) ? k : best, idx[0]);
  }
  const {L, T, pw, ph, xe, ye, d} = RES.gridGeom;
  if (x < L || x > L + pw || y < T || y > T + ph) return null;
  const xv = xe[0] + (x - L) / pw * (xe.at(-1) - xe[0]), yv = ye.at(-1) - (y - T) / ph * (ye.at(-1) - ye[0]);
  const i = xe.findIndex((e, k) => k < xe.length - 1 && xv >= e && xv <= xe[k + 1]);
  const j = ye.findIndex((e, k) => k < ye.length - 1 && yv >= e && yv <= ye[k + 1]);
  const k = d.rows.findIndex((_, k) => d.xv[k] === d.xu[i] && d.yv[k] === d.yu[j]);
  return k >= 0 ? k : null;
}

// ---------------------------------------------------------------- spectra (TWPA noise spectrum, TWPA VNA gain)
// axis.f, axis.spectra {label: [dB(m)]} (each state averaged so far), axis.spectra_label (the
// y label, default dBm) and axis.derived {y_pump_off, y_pump_on, gain_db: [value or null]};
// one plot of the chosen quantity, with a running mean. Quantities the run has no data for
// are hidden.
const SPECTRA_QTY = {spectra: "power [dBm]", y_pump_off: "Y, pump off", y_pump_on: "Y, pump on", gain_db: "gain [dB]"};
const SPECTRA_COLORS = ["#1f77b4", "#ff7f0e", "#d62728", "#2ca02c"];
function runningMean(v, n) {
  if (n <= 1) return v;
  const h = Math.floor(n / 2);
  return v.map((_, k) => {
    let s = 0, c = 0;
    for (let j = Math.max(0, k - h); j <= Math.min(v.length - 1, k + h); j++) if (v[j] !== null) { s += v[j]; c++; }
    return c ? s / c : null;
  });
}
function drawSpectra(c = resEl("spectraPlot")) {
  const ax = RES.data && RES.data.axis, sel = resEl("spectraQty");
  for (const o of sel.options) o.hidden = o.value !== "spectra" && !(ax && ax.derived && o.value in ax.derived);
  if (sel.selectedOptions[0].hidden) sel.value = "spectra";
  const qty = sel.value, yLabel = qty === "spectra" ? (ax && ax.spectra_label) || SPECTRA_QTY.spectra : SPECTRA_QTY[qty];
  const [g, W, H] = prepCanvas(c);
  const n = Math.max(1, parseInt(resEl("spectraSmooth").value) || 1);
  let series = [];
  if (ax && ax.f) {
    if (qty === "spectra")      // smoothed in W, shown in dBm
      series = Object.entries(ax.spectra || {}).map(([name, ys], k) => [name,
        runningMean(ys.map(v => 10 ** (v / 10)), n).map(w => 10 * Math.log10(w)), SPECTRA_COLORS[k % 4]]);
    else if (ax.derived && ax.derived[qty]) series = [[SPECTRA_QTY[qty], runningMean(ax.derived[qty], n), "#000"]];
  }
  resEl("spectraReadout").textContent = "";
  if (!series.length) {
    g.fillText(ax && ax.f ? "waiting for the states this needs..." : "waiting for the first spectrum...", 20, 30);
    showPoint(null); return;
  }
  const fs = ax.f.map(f => f / 1e9);
  const L = 80, R = 12, T = 22, B = 36, pw = W - L - R, ph = H - T - B;
  let y0 = Infinity, y1 = -Infinity;
  for (const [, ys] of series) for (const v of ys) if (v !== null && isFinite(v)) { if (v < y0) y0 = v; if (v > y1) y1 = v; }
  if (!(y1 > y0)) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.05; y0 -= pad; y1 += pad;
  const x0 = fs[0], x1 = fs.at(-1);
  const X = v => L + (v - x0) / (x1 - x0) * pw, Y = v => T + (1 - (v - y0) / (y1 - y0)) * ph;
  g.strokeStyle = "#ddd"; g.beginPath(); g.textAlign = "center";
  for (const t of ticks(x0, x1, 8)) { g.moveTo(X(t), T); g.lineTo(X(t), T + ph); g.fillText(label(t), X(t), T + ph + 14); }
  g.textAlign = "right";
  for (const t of ticks(y0, y1, 5)) { g.moveTo(L, Y(t)); g.lineTo(L + pw, Y(t)); g.fillText(label(t), L - 4, Y(t) + 4); }
  g.stroke(); g.strokeStyle = "#000"; g.strokeRect(L, T, pw, ph);
  g.textAlign = "center"; g.fillText("frequency [GHz]", L + pw / 2, H - 4);
  g.save(); g.translate(12, T + ph / 2); g.rotate(-Math.PI / 2); g.fillText(yLabel, 0, 0); g.restore();
  g.textAlign = "left"; g.fillText(ax.title || "", L, T - 7);
  g.save(); g.beginPath(); g.rect(L, T, pw, ph); g.clip();
  for (const [, ys, color] of series) {
    g.strokeStyle = color; g.beginPath(); let on = false;
    ys.forEach((v, k) => { if (v === null || !isFinite(v)) { on = false; return; } on ? g.lineTo(X(fs[k]), Y(v)) : g.moveTo(X(fs[k]), Y(v)); on = true; });
    g.stroke();
  }
  let hit;
  if (c.hover && c.hover.x >= L && c.hover.x <= L + pw) {
    hit = 0;
    for (let k = 1; k < fs.length; k++) if (Math.abs(X(fs[k]) - c.hover.x) < Math.abs(X(fs[hit]) - c.hover.x)) hit = k;
    g.strokeStyle = "#000"; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X(fs[hit]), T); g.lineTo(X(fs[hit]), T + ph); g.stroke();
    g.setLineDash([]);
  }
  g.restore();
  if (series.length > 1) {      // legend
    g.textAlign = "left";
    series.forEach(([name, , color], k) => { g.fillStyle = color; g.fillText(name, L + 8 + (k % 2) * 220, T + 16 + Math.floor(k / 2) * 14); });
    g.fillStyle = "#000";
  }
  if (hit !== undefined)
    resEl("spectraReadout").textContent = `${+fs[hit].toPrecision(10)} GHz: `
      + series.map(([name, ys]) => `${name} ${ys[hit] === null ? "-" : label(+ys[hit].toFixed(4))}`).join(", ");
  const rows = RES.data.rows;
  showPoint(rows.length ? rows.at(-1) : null, "newest");
}
