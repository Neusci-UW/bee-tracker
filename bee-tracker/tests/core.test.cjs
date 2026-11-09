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
