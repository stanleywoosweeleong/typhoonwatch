#!/usr/bin/env node
/*
 * squall.js — run Sumatra Squall Watch's OWN scoring engine on the Actions runner.
 *
 * WHY THIS EXISTS
 * TyphoonWatch reads a 24 h pressure change, which is blind to a Sumatra squall:
 * a line of storms tens of km wide that forms at night over the Strait and is
 * gone by mid-morning. On 2026-09-26 the page read 转干 for Penang and Alor Setar
 * while the west coast was flooding. The squall setup index already exists, in
 * the SumatraSquall app. Re-writing it here would give two copies that drift, so
 * this script loads the engine straight out of that app's published index.html
 * (between its /*ENGINE-START*​/ and /*ENGINE-END*​/ markers) together with its
 * data constants and parsers, and runs them unchanged. Every threshold, weight,
 * grid point and model id therefore comes from SumatraSquall itself.
 *
 * Networking stays in fetch_typhoon.py (one User-Agent, one retry policy):
 *
 *   node scripts/squall.js plan    <SumatraSquall index.html>
 *        -> {version, stale_h, urls:{grid:[[id,url]..], upper:[..], towns:[..]}}
 *   node scripts/squall.js analyze <SumatraSquall index.html>   < raw.json
 *        raw = {now: unix s, grid:{id,json}, upper:{id,json}|null, towns:{id,json}|null}
 *        -> the compact block stored as `squall` in data/typhoon.json
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

function load(path) {
  const html = fs.readFileSync(path, 'utf8');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  if (!scripts.length) throw new Error('no inline script in SumatraSquall index.html');
  const js = scripts[scripts.length - 1][1];
  const cut = (a, b) => {
    const i = js.indexOf(a), j = i < 0 ? -1 : js.indexOf(b, i);
    if (i < 0 || j < 0) throw new Error('SumatraSquall index.html: marker not found: ' + a + ' … ' + b);
    return js.slice(i, j);
  };
  const consts = ['VERSION', 'MYT', 'STALE_H'].map(k => {
    const m = js.match(new RegExp('const ' + k + '\\s*=\\s*([^;]+);'));
    if (!m) throw new Error('SumatraSquall index.html: const ' + k + ' not found');
    return 'const ' + k + ' = ' + m[1] + ';';
  }).join('\n');
  const src = consts + '\n' +
    cut('/*ENGINE-START*/', '/*ENGINE-END*/') + '\n' +
    cut('const MODELS', 'async function loadModel') + '\n' +
    ';({VERSION, MYT, STALE_H, TOWNS, GPTS, THR, W8, LEVELS, ESSENTIAL, MODELS, SURF_VARS, UPPER_VARS,' +
    ' GKEYS, TVARS, TKEYS, omURL, parseMulti, buildD, mergeGrid, pick, nullVars, analyzeNight, nightMorning})';
  // a fresh context: no fetch, no DOM — the engine is pure and must stay so
  return vm.runInContext(src, vm.createContext({}), { filename: 'SumatraSquall/index.html' });
}

function plan(E) {
  const M = E.MODELS.ecmwf;
  if (!M) throw new Error('SumatraSquall has no "ecmwf" model entry');
  const gridVars = M.upper ? E.SURF_VARS : E.SURF_VARS.concat(E.UPPER_VARS);
  return {
    version: E.VERSION, stale_h: E.STALE_H,
    urls: {
      grid: M.grid.map(id => [id, E.omURL(E.GPTS, gridVars, id, 'nearest')]),
      upper: (M.upper || []).map(id => [id, E.omURL(E.GPTS, E.UPPER_VARS, id, 'nearest')]),
      towns: M.towns.map(id => [id, E.omURL(E.TOWNS, E.TVARS, id, null)]),
    },
  };
}

const iso = s => (s == null ? null : new Date(s * 1000).toISOString().replace(/\.\d+Z$/, 'Z'));
const r1 = x => (x == null ? null : Math.round(x * 10) / 10);

function pack(A) {
  const p = A.parts || {}, out = {};
  for (const k of Object.keys(p)) {
    const q = p[k], o = { w: q.w };
    if (q.missing) o.missing = true;
    else { o.score = Math.round(q.score * 100) / 100; o.pts = r1(q.pts); }
    if (k === 'steer' && q.st) { o.from = Math.round(q.st.from); o.ms = r1(q.st.spd); }
    if (k === 'cape' && q.c != null) o.cape = Math.round(q.c);
    if (k === 'conv' && q.mean != null) o.conv = Math.round(q.mean * 1e7) / 100;   // ×10⁻⁵ s⁻¹
    if (k === 'rain' && q.frac != null) { o.frac = Math.round(q.frac * 100) / 100; o.max = r1(q.max); }
    if (k === 'gust' && q.g != null) { o.kmh = Math.round(q.g); o.town = q.town && q.town.id; o.at = iso(q.ts); }
    if (k === 'season') o.phase = q.phase;
    out[k] = o;
  }
  const towns = {};
  for (const r of A.towns || []) {
    const lag = r.lag == null ? null : r.lag.upwind ? { upwind: true } : { h: r1(r.lag.h), km: Math.round(r.lag.km) };
    towns[r.town.id] = { first: iso(r.first), rain: r1(r.rain), max: r1(r.maxP), gust: r.gust == null ? null : Math.round(r.gust), lag };
  }
  return {
    m0: iso(A.M0), score: A.score, level: A.level, gate: !!A.gate, blocked: A.blocked || null,
    lacking: A.lacking || [], missing: A.missing || [], parts: out,
    earliest: A.earliest ? { id: A.earliest.town.id, at: iso(A.earliest.first) } : null, towns,
  };
}

function analyze(E, raw) {
  const M = E.MODELS.ecmwf;
  if (!raw.grid) throw new Error('no grid data');
  const G = E.parseMulti(raw.grid.json, E.GPTS.length, M.upper ? E.pick(E.GKEYS, E.SURF_VARS) : E.GKEYS);
  let upperId = null;
  if (M.upper) {
    if (raw.upper) { E.mergeGrid(G, E.parseMulti(raw.upper.json, E.GPTS.length, E.pick(E.GKEYS, E.UPPER_VARS))); upperId = raw.upper.id; }
    else for (const v of E.UPPER_VARS) G.series[E.GKEYS[v]] = E.GPTS.map(() => G.times.map(() => null));   // as the app does: empty, never invented
  }
  const t = raw.towns ? E.parseMulti(raw.towns.json, E.TOWNS.length, E.TKEYS) : null;
  const D = E.buildD('ecmwf', raw.grid.id, raw.now * 1000, G, t);
  const empty = E.nullVars(G.series, E.GKEYS).concat(t ? E.nullVars(t.series, E.TKEYS).map(v => v + ' (towns)') : []);
  return {
    source: 'SumatraSquall ' + E.VERSION,
    app_url: 'https://stanleywoosweeleong.github.io/SumatraSquall/',
    at: iso(raw.now), stale_h: E.STALE_H,
    models: { grid: raw.grid.id, upper: upperId, towns: raw.towns ? raw.towns.id : null },
    empty,
    levels: E.LEVELS.map(x => [x[0], x[1]]),
    weights: Object.assign({}, E.W8),
    sig: { rain: E.THR.sigRain, gust: E.THR.sigGust },   // a town "signal", as the app defines it
    town_meta: E.TOWNS.map(x => ({ id: x.id, g: x.g, lat: x.lat, lon: x.lon, zh: x.zh, en: x.en })),
    // tonight (or the night still under way) and the one after; the page picks
    // whichever has not finished, so a file read at 14:00 never shows last night
    nights: [0, 1].map(w => pack(E.analyzeNight(D, E.nightMorning(raw.now, w)))),
  };
}

function main() {
  const [mode, path] = process.argv.slice(2);
  if (!mode || !path) throw new Error('usage: squall.js plan|analyze <SumatraSquall index.html>');
  const E = load(path);
  if (mode === 'plan') return plan(E);
  if (mode === 'analyze') return analyze(E, JSON.parse(fs.readFileSync(0, 'utf8')));
  throw new Error('unknown mode ' + mode);
}

try { process.stdout.write(JSON.stringify(main())); }
catch (e) { process.stderr.write(String(e && e.message || e) + '\n'); process.exit(1); }
