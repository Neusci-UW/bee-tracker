// Unit tests for src/core.js. Run: node --test tests/
const test = require('node:test');
const assert = require('node:assert/strict');
const C = require('../src/core.js');

function rng(seed){ let s = seed >>> 0; return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; }; }

test('hungarian matches brute force', () => {
  const r = rng(1);
  const perms = a => a.length <= 1 ? [a] : a.flatMap((x, i) => perms([...a.slice(0, i), ...a.slice(i + 1)]).map(p => [x, ...p]));
  for(let trial = 0; trial < 200; trial++){
    const n = 1 + Math.floor(r() * 5), m = n + Math.floor(r() * 3);
    const cost = Array.from({length: n}, () => Array.from({length: m}, () => Math.round(r() * 100)));
    const a = C.hungarian(cost), got = a.reduce((s, j, i) => s + cost[i][j], 0);
    let best = Infinity;
    perms([...Array(m).keys()]).forEach(p => { best = Math.min(best, cost.reduce((s, row, i) => s + row[p[i]], 0)); });
    assert.equal(got, best);
    assert.equal(new Set(a).size, n);
  }
});

test('assign handles more rows than columns and gating', () => {
  const pairs = C.assign([[1, 1e9], [1e9, 2], [3, 1e9]], 1e9);
  assert.deepEqual(pairs.sort(), [[0, 0], [1, 1]]);
});

function disc(w, h, list){
  const m = new Uint8Array(w * h);
  list.forEach(([cx, cy, r]) => { for(let y = 0; y < h; y++) for(let x = 0; x < w; x++) if((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r * r) m[y * w + x] = 1; });
  return m;
}

test('findBlobs gives areas and centroids', () => {
  const w = 100, h = 80, fg = disc(w, h, [[20, 20, 6], [70, 50, 9]]);
  const {blobs} = C.findBlobs(fg, w, h, 5);
  assert.equal(blobs.length, 2);
  const b = blobs.sort((a, c) => a.area - c.area);
  assert.ok(Math.abs(b[0].x - 20) < 0.3 && Math.abs(b[0].y - 20) < 0.3);
  assert.ok(Math.abs(b[1].x - 70) < 0.3 && Math.abs(b[1].y - 50) < 0.3);
  assert.ok(Math.abs(b[0].area - Math.PI * 36) < 12);
});

test('splitBlob separates two touching bees', () => {
  const w = 100, h = 60, fg = disc(w, h, [[40, 30, 8], [54, 30, 8]]);
  const {blobs, pix} = C.findBlobs(fg, w, h, 5);
  assert.equal(blobs.length, 1);
  const parts = C.splitBlob(blobs[0], pix, w, 2).sort((a, b) => a.x - b.x);
  assert.equal(parts.length, 2);
  assert.ok(Math.abs(parts[0].x - 40) < 2.5, 'left ' + parts[0].x);
  assert.ok(Math.abs(parts[1].x - 54) < 2.5, 'right ' + parts[1].x);
});

test('open3 removes single-pixel noise but keeps bees', () => {
  const w = 50, h = 50, fg = disc(w, h, [[25, 25, 6]]);
  fg[3 * w + 3] = 1; fg[40 * w + 10] = 1;
  const o = C.open3(fg, w, h);
  const {blobs} = C.findBlobs(o, w, h, 1);
  assert.equal(blobs.length, 1);
});

test('foreground compensates a global brightness change', () => {
  const n = 400, bg = new Uint8Array(n).fill(200), img = new Uint8Array(n).fill(170), mask = new Uint8Array(n).fill(1);
  img[5] = 90; // a dark bee pixel, in a frame that is 30 levels darker overall
  const {fg, offset} = C.foreground(img, bg, mask, 'dark', 40, C.maskedMedian(bg, mask));
  assert.equal(offset, 30);
  assert.equal(fg.reduce((a, b) => a + b, 0), 1);
});

test('tracker keeps identities through a near crossing', () => {
  const tr = C.createTracker({n: 2, maxSpeed: 200, gate0: 10, reacquireAfter: 1});
  const r = rng(7);
  let swaps = 0, aIsFirst = null;
  for(let i = 0; i <= 60; i++){
    const t = i / 15;
    const a = {x: 10 + 20 * t, y: 50 + 0.0 * t, area: 50}, b = {x: 90 - 20 * t, y: 56, area: 50};
    const dets = [a, b].map(d => ({...d, x: d.x + (r() - 0.5), y: d.y + (r() - 0.5)}));
    if(r() < 0.5) dets.reverse();
    const {rows} = tr.step(t, dets);
    const firstIsA = Math.abs(rows[0].y - 50) < 2;
    if(aIsFirst === null) aIsFirst = firstIsA; else if(firstIsA !== aIsFirst) swaps++;
  }
  assert.equal(swaps, 0);
});

test('tracker holds a lost bee and reacquires it', () => {
  const tr = C.createTracker({n: 1, maxSpeed: 20, gate0: 5, reacquireAfter: 1});
  tr.step(0, [{x: 10, y: 10, area: 30}]);
  let out = tr.step(0.1, []);
  assert.equal(out.rows[0].det, false);
  out = tr.step(0.2, [{x: 80, y: 80, area: 30}]);     // too far: not matched yet
  assert.equal(out.rows[0].det, false);
  out = tr.step(1.5, [{x: 80, y: 80, area: 30}]);     // lost > 1 s: reacquired, flagged
  assert.equal(out.rows[0].det, true);
  assert.equal(out.rows[0].reacq, true);
});

test('metrics: straight walk at constant speed with a zone', () => {
  const t = [], x = [], y = [], det = [];
  for(let i = 0; i <= 100; i++){ t.push(i / 10); x.push(10 + 5 * i / 10 * 10); y.push(50); det.push(true); } // 50 px/s for 10 s
  const zone = {name: 'Right', shape: {type: 'rect', x: 260, y: 0, w: 1000, h: 100}};
  const m = C.metrics({t, x, y, det}, {scale: {sx: 0.1, sy: 0.1}, smooth: 0.3, maxGap: 0.5, moveThr: 1, zones: [zone]});
  assert.ok(Math.abs(m.distance - 50) < 0.6, 'distance ' + m.distance);     // 500 px * 0.1 mm/px
  assert.ok(Math.abs(m.meanSpeed - 5) < 0.1);
  assert.equal(m.fracMoving, 1);
  assert.ok(Math.abs(m.zones[0].time - 5) < 0.25, 'zone time ' + m.zones[0].time);
  assert.equal(m.zones[0].visits, 1);
  assert.equal(m.runs.moving.length, 1);
});

test('metrics: gaps split segments and stillness is not moving', () => {
  const t = [], x = [], y = [], det = [];
  const r = rng(3);
  for(let i = 0; i < 200; i++){ t.push(i / 20); x.push(100 + (r() - 0.5) * 0.4); y.push(100 + (r() - 0.5) * 0.4); det.push(!(i >= 80 && i < 120)); }
  const m = C.metrics({t, x, y, det}, {scale: null, smooth: 0.3, maxGap: 0.5, moveThr: 2, zones: []});
  assert.ok(m.fracMoving < 0.05, 'jitter counted as moving: ' + m.fracMoving);
  assert.ok(Math.abs(m.coverage - 0.8) < 1e-9);
  assert.ok(m.tracked < 8);
});

test('procGeometry round-trips coordinates', () => {
  const g = C.procGeometry({type: 'ellipse', x: 100.5, y: 50, w: 800, h: 600}, 1280, 720, 400);
  assert.ok(g.w <= 400 && g.h <= 400);
  const [px, py] = C.toProc(g, 500, 350), [vx, vy] = C.toVideo(g, px, py);
  assert.ok(Math.abs(vx - 500) < 1e-9 && Math.abs(vy - 350) < 1e-9);
});

test('lightConditions splits a trial by LED state', () => {
  const ev = [
    {t: 2, output: 'ao0', peak: 2, segs: [[0.5, 5.5, 2]]},          // ao0 on 2.5-7.5
    {t: 10, output: 'ao0', peak: 3, segs: [[0, 1, 3]]},              // ao0 on 10-11
    {t: 6, output: 'ao1', peak: 1.5, segs: [[0, 0.01, 1.5], [0.1, 0.11, 1.5], [3.99, 4, 1.5]]}, // a pulse train 6-10
  ];
  const c = C.lightConditions(ev, 15);
  assert.deepEqual(c.map(x => [x.t0, x.t1, x.key]), [
    [0, 2.5, 'No stimulus'], [2.5, 6, 'ao0 2 V'], [6, 7.5, 'ao0 2 V + ao1 1.5 V'], [7.5, 10, 'ao1 1.5 V'],
    [10, 11, 'ao0 3 V'], [11, 15, 'No stimulus']]);
  assert.equal(C.conditionAt(c, 6.2), 'ao0 2 V + ao1 1.5 V');
  assert.equal(C.conditionAt(c, 14.99), 'No stimulus');
  assert.equal(C.conditionAt(c, 20), null);
});

test('metrics can be restricted to some times', () => {
  const t = [], x = [], y = [], det = [];
  for(let i = 0; i <= 200; i++){ t.push(i / 20); x.push(i < 100 ? 100 : 100 + (i - 100) * 2); y.push(50); det.push(true); }  // still 0-5 s, then 40 px/s
  const base = {scale: null, smooth: 0, maxGap: 0.5, moveThr: 5, zones: []};
  const first = C.metrics({t, x, y, det}, {...base, include: s => s < 5});
  const second = C.metrics({t, x, y, det}, {...base, include: s => s >= 5});
  assert.ok(Math.abs(first.tracked - 5) < 1e-9 && first.distance === 0 && first.fracMoving === 0);
  assert.ok(Math.abs(second.meanSpeed - 40) < 1e-6 && second.fracMoving === 1);
});

test('lightChanges finds when a light in view turns on and off, and latency is measured', () => {
  const t = [], v = [];
  for(let i = 0; i < 300; i++){ const s = i / 30; t.push(s); v.push(((s >= 2.1 && s < 4.1) || (s >= 6.1 && s < 7.1) ? 180 : 40) + (i % 3)); }
  const L = C.lightChanges(t, v, 12);
  assert.equal(L.onsets.length, 2); assert.equal(L.offsets.length, 2);
  assert.ok(Math.abs(L.onsets[0] - 2.1) < 0.04 && Math.abs(L.onsets[1] - 6.1) < 0.04);
  const lat = C.matchLatency([2.0, 6.0, 9.0], L.onsets, 1);
  assert.equal(lat.length, 2); assert.ok(lat.every(d => Math.abs(d - 0.1) < 0.04));
  assert.equal(C.lightChanges(t, t.map(() => 50), 12), null);   // no light change in view
});

test('LED formulas: grammar, and clear errors instead of running code', () => {
  const f = C.parseFormula('2.5 + 2*sin(2*pi*t) - -1^2 + (t<1)*3 + clamp(t, 0, 1) + mod(-1, 3) + round(0.5) + d');
  assert.ok(Math.abs(f(0.25, 10) - (2.5 + 2 + 1 + 3 + 0.25 + 2 + 1 + 10)) < 1e-12);
  assert.equal(C.parseFormula('2^3^2')(0, 1), 512);                    // right to left, like maths
  for(const [src, msg] of [['', /empty/], ['2+', /ends too early/], ['alert(1)', /doesn’t know/], ['sin(1, 2)', /takes 1 value/],
    ['t; 1', /character/], ['(t', /missing “\)”/], ['t t', /extra/], ['constructor', /doesn’t know/]])
    assert.throws(() => C.parseFormula(src), msg);
});

test('LED functions: levels, problems and names', () => {
  const at = (f, t, d) => C.ledEval(f)(t, d ?? 1);
  assert.equal(at(C.ledFn('ramp', {from: 1, to: 3}), 0.5), 2);
  assert.equal(at(C.ledFn('pulses', {freq: 10, duty: 30, on: 3, off: 0.5}), 0.02), 3);
  assert.equal(at(C.ledFn('pulses', {freq: 10, duty: 30, on: 3, off: 0.5}), 0.03), 0.5);
  assert.equal(at(C.ledFn('pulse', {delay: 0.2, width: 0.1, v: 2}), 0.3), 0);      // edge on a sample time: off, as in the helper
  const r = C.ledFn('random', {min: 1, max: 3, hold: 0.25, seed: 7});
  assert.equal(at(r, 0.1), at(r, 0.2)); assert.notEqual(at(r, 0.1), at(r, 0.3));
  assert.deepEqual(C.ledFnProblems(C.ledFn('pulses', {duty: 100})), ['The duty cycle must be between 0 and 100%.']);
  assert.match(C.ledFnProblems(C.ledFn('formula', {expr: 'sin('}))[0], /ends too early/);
  const rg = C.ledRange(C.ledFn('sine', {freq: 1, mid: 2, amp: 1}), 1, 1000);
  assert.ok(Math.abs(rg.lo - 1) < 1e-4 && Math.abs(rg.hi - 3) < 1e-4);
  assert.ok(isNaN(C.ledRange(C.ledFn('formula', {expr: 'sqrt(-1)'}), 1, 1000).lo));
  const names = {ao0: 'Blue', ao1: 'Green'};
  assert.equal(C.ledConditionName({ao0: C.ledFn('const', {v: 2}), ao1: C.ledFn('off')}, names), 'Blue 2 V');
  assert.equal(C.ledConditionName({ao0: C.ledFn('off'), ao1: C.ledFn('const', {v: 0})}, names), 'No stimulus');
  assert.equal(C.ledConditionName({label: ' Flicker ', ao0: C.ledFn('pulses'), ao1: C.ledFn('off')}, names), 'Flicker');
});

test('LED sequence repeats and shuffles each repeat the same way for the same seed', () => {
  const led = {repeats: 3, shuffle: true, segs: [0, 1, 2, 3].map(k => ({label: 'S' + k, dur: 1}))};
  const a = C.ledSequence(led, 99).map(s => s.index), b = C.ledSequence(led, 99).map(s => s.index);
  assert.deepEqual(a, b);
  for(let r = 0; r < 3; r++) assert.deepEqual(a.slice(r * 4, r * 4 + 4).sort(), [0, 1, 2, 3]);
  assert.notDeepEqual(a, C.ledSequence(led, 100).map(s => s.index));
  assert.deepEqual(C.ledSequence(Object.assign({}, led, {shuffle: false}), 99).map(s => s.index), [0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 2, 3]);
});

test('ledPlayed logs conditions, events and onsets on the trial clock', () => {
  const off = C.ledFn('off'), names = {ao0: 'Blue', ao1: 'Green'};
  const seq = C.ledSequence({repeats: 1, segs: [
    {label: '', dur: 1, ao0: off, ao1: off},
    {label: 'Flicker', dur: 1, ao0: C.ledFn('pulses', {freq: 4, duty: 50, on: 2, off: 0}), ao1: off},
    {label: '', dur: 1, ao0: C.ledFn('const', {v: 1}), ao1: C.ledFn('ramp', {from: 0, to: 2})}]}, 1);
  const p = C.ledPlayed(seq, [0, 1, 2], 0.5, 3.2, 1000, names);
  assert.deepEqual(p.conditions.map(c => [c.t0, c.t1, c.key]),
    [[0, 1.5, 'No stimulus'], [1.5, 2.5, 'Flicker'], [2.5, 3.2, 'Blue 1 V + Green ramp 0–2 V']]);
  assert.deepEqual(p.events.map(e => [e.output, e.t, e.dur, e.label, e.peak]),
    [['Blue', 1.5, 1, 'Flicker', 2], ['Blue', 2.5, 0.7, 'Blue 1 V + Green ramp 0–2 V', 1], ['Green', 2.5, 0.7, 'Blue 1 V + Green ramp 0–2 V', 1.398]]);
  assert.deepEqual(p.onsets, [1.5, 1.75, 2, 2.25, 2.5, 2.501]);             // 4 pulses, Blue on again, Green leaves 0 V one sample in
  assert.equal(C.conditionAt(p.conditions, 1.6), 'Flicker');
});
