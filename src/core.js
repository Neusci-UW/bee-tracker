/* Bee Tracker core: image processing, detection, tracking and metrics.
   Pure functions with no DOM access, so they run in the page and in Node tests.
   Coordinates: "proc" = processing image (arena crop, downscaled); "video" = source video pixels. */
const BeeCore = (() => {
'use strict';

/* ---------- shapes (video pixel coords) ---------- */
// shape: {type:'ellipse'|'rect', x, y, w, h} — bounding box of the arena or zone
function inside(s, x, y){
  if(!s) return false;
  if(s.type === 'rect') return x >= s.x && x <= s.x + s.w && y >= s.y && y <= s.y + s.h;
  const rx = s.w / 2, ry = s.h / 2; if(rx <= 0 || ry <= 0) return false;
  const dx = (x - (s.x + rx)) / rx, dy = (y - (s.y + ry)) / ry;
  return dx * dx + dy * dy <= 1;
}
// The shape shrunk by d on every side (for an "edge band" inside the arena wall).
function inset(s, d){
  return {type: s.type, x: s.x + d, y: s.y + d, w: Math.max(0, s.w - 2 * d), h: Math.max(0, s.h - 2 * d)};
}

/* ---------- processing geometry ---------- */
// Crop the arena bounding box out of the video and scale it so its longer side is at most procMax.
function procGeometry(arena, vw, vh, procMax){
  const x0 = Math.max(0, Math.floor(arena.x)), y0 = Math.max(0, Math.floor(arena.y));
  const x1 = Math.min(vw, Math.ceil(arena.x + arena.w)), y1 = Math.min(vh, Math.ceil(arena.y + arena.h));
  const cw = Math.max(1, x1 - x0), ch = Math.max(1, y1 - y0);
  const s = Math.min(1, procMax / Math.max(cw, ch));
  const w = Math.max(1, Math.round(cw * s)), h = Math.max(1, Math.round(ch * s));
  return {cx: x0, cy: y0, cw, ch, w, h, sx: w / cw, sy: h / ch};
}
const toVideo = (g, x, y) => [g.cx + x / g.sx, g.cy + y / g.sy];
const toProc = (g, x, y) => [(x - g.cx) * g.sx, (y - g.cy) * g.sy];

function shapeMask(shape, g){
  const m = new Uint8Array(g.w * g.h);
  for(let y = 0; y < g.h; y++) for(let x = 0; x < g.w; x++){
    const [vx, vy] = toVideo(g, x + 0.5, y + 0.5);
    if(inside(shape, vx, vy)) m[y * g.w + x] = 1;
  }
  return m;
}

/* ---------- pixels ---------- */
function toLuma(rgba, n){
  const out = new Uint8Array(n);
  for(let i = 0, j = 0; i < n; i++, j += 4) out[i] = (rgba[j] * 77 + rgba[j + 1] * 150 + rgba[j + 2] * 29) >> 8;
  return out;
}
function maskedMedian(img, mask){
  const h = new Uint32Array(256); let n = 0;
  for(let i = 0; i < img.length; i++) if(mask[i]){ h[img[i]]++; n++; }
  if(!n) return 0;
  let c = 0; const half = n / 2;
  for(let v = 0; v < 256; v++){ c += h[v]; if(c >= half) return v; }
  return 255;
}
// Per-pixel median of a stack of frames: removes bees that move during the recording.
function medianBackground(frames){
  const K = frames.length, n = frames[0].length, out = new Uint8Array(n), buf = new Uint8Array(K);
  for(let i = 0; i < n; i++){
    for(let k = 0; k < K; k++) buf[k] = frames[k][i];
    buf.sort(); out[i] = K % 2 ? buf[K >> 1] : (buf[(K >> 1) - 1] + buf[K >> 1] + 1) >> 1;
  }
  return out;
}
// Pixels that differ from the background by at least thr, after matching overall brightness
// (compensates auto-exposure and flicker). polarity: 'dark' (bees darker than background), 'light', 'both'.
function foreground(img, bg, mask, polarity, thr, bgMed){
  const off = bgMed == null ? 0 : bgMed - maskedMedian(img, mask);
  const n = img.length, fg = new Uint8Array(n);
  for(let i = 0; i < n; i++){
    if(!mask[i]) continue;
    const d = img[i] + off - bg[i];
    const v = polarity === 'dark' ? -d : polarity === 'light' ? d : (d < 0 ? -d : d);
    if(v >= thr) fg[i] = 1;
  }
  return {fg, offset: off};
}
// 3x3 binary opening (erode then dilate): removes isolated noise pixels and thin specks.
function open3(m, w, h){
  const a = new Uint8Array(m.length), b = new Uint8Array(m.length);
  const pass = (src, dst, erode, horiz) => {
    for(let y = 0; y < h; y++) for(let x = 0; x < w; x++){
      const i = y * w + x;
      let p, q;
      if(horiz){ p = x > 0 ? src[i - 1] : (erode ? 0 : 0); q = x < w - 1 ? src[i + 1] : 0; }
      else { p = y > 0 ? src[i - w] : 0; q = y < h - 1 ? src[i + w] : 0; }
      dst[i] = erode ? (src[i] & p & q) : (src[i] | p | q);
    }
  };
  pass(m, a, true, true); pass(a, b, true, false);   // erode
  pass(b, a, false, true); pass(a, b, false, false); // dilate
  return b;
}

/* ---------- blobs ---------- */
// 8-connected components. Each blob's pixels are stored contiguously in pix[start..end).
function findBlobs(fg, w, h, minArea){
  const lab = new Uint8Array(fg.length), pix = new Int32Array(fg.length), out = [];
  let tail = 0;
  for(let s = 0; s < fg.length; s++){
    if(!fg[s] || lab[s]) continue;
    const start = tail; let head = tail; pix[tail++] = s; lab[s] = 1;
    let sx = 0, sy = 0;
    while(head < tail){
      const p = pix[head++], px = p % w, py = (p - px) / w;
      sx += px; sy += py;
      for(let dy = -1; dy <= 1; dy++){
        const yy = py + dy; if(yy < 0 || yy >= h) continue;
        for(let dx = -1; dx <= 1; dx++){
          const xx = px + dx; if(xx < 0 || xx >= w) continue;
          const q = yy * w + xx; if(fg[q] && !lab[q]){ lab[q] = 1; pix[tail++] = q; }
        }
      }
    }
    const area = tail - start;
    if(area >= minArea) out.push({area, x: sx / area + 0.5, y: sy / area + 0.5, start, end: tail});
    else tail = start; // reuse the space
  }
  return {blobs: out, pix};
}
// Split one blob into k parts with k-means on its pixel coordinates (for touching bees).
function splitBlob(blob, pix, w, k, seeds){
  const n = blob.end - blob.start;
  const X = new Float32Array(n), Y = new Float32Array(n);
  let mx = 0, my = 0;
  for(let i = 0; i < n; i++){ const p = pix[blob.start + i]; X[i] = p % w + 0.5; Y[i] = Math.floor(p / w) + 0.5; mx += X[i]; my += Y[i]; }
  mx /= n; my /= n;
  let c;
  if(seeds && seeds.length === k) c = seeds.map(s => [s[0], s[1]]);
  else {
    // seeds spread along the blob's long axis
    let sxx = 0, syy = 0, sxy = 0;
    for(let i = 0; i < n; i++){ const dx = X[i] - mx, dy = Y[i] - my; sxx += dx * dx; syy += dy * dy; sxy += dx * dy; }
    const th = 0.5 * Math.atan2(2 * sxy, sxx - syy), ux = Math.cos(th), uy = Math.sin(th);
    const proj = Array.from({length: n}, (_, i) => (X[i] - mx) * ux + (Y[i] - my) * uy).sort((a, b) => a - b);
    c = Array.from({length: k}, (_, j) => { const q = proj[Math.min(n - 1, Math.floor((j + 0.5) / k * n))]; return [mx + q * ux, my + q * uy]; });
  }
  const asg = new Int32Array(n);
  for(let it = 0; it < 12; it++){
    const sum = c.map(() => [0, 0, 0]);
    for(let i = 0; i < n; i++){
      let best = 0, bd = Infinity;
      for(let j = 0; j < k; j++){ const d = (X[i] - c[j][0]) ** 2 + (Y[i] - c[j][1]) ** 2; if(d < bd){ bd = d; best = j; } }
      asg[i] = best; sum[best][0] += X[i]; sum[best][1] += Y[i]; sum[best][2]++;
    }
    let moved = 0;
    for(let j = 0; j < k; j++) if(sum[j][2]){ const nx = sum[j][0] / sum[j][2], ny = sum[j][1] / sum[j][2]; moved += Math.abs(nx - c[j][0]) + Math.abs(ny - c[j][1]); c[j] = [nx, ny]; }
    if(moved < 0.05) break;
  }
  const area = new Array(k).fill(0); for(let i = 0; i < n; i++) area[asg[i]]++;
  return c.map((p, j) => ({x: p[0], y: p[1], area: area[j]})).filter(d => d.area > 0);
}

/* ---------- detection ---------- */
// opt: {polarity, thr, open, beeArea, minFrac, maxFrac, n}
// predictions: [[x,y],...] proc coords of where tracked bees are expected (to judge how many share a merged blob)
function detect(img, bg, mask, g, opt, bgMed, predictions){
  const {fg} = foreground(img, bg, mask, opt.polarity, opt.thr, bgMed);
  const m = opt.open ? open3(fg, g.w, g.h) : fg;
  const minArea = opt.beeArea ? Math.max(2, opt.beeArea * opt.minFrac) : 4;
  const {blobs, pix} = findBlobs(m, g.w, g.h, minArea);
  const dets = [];
  blobs.sort((a, b) => b.area - a.area);
  for(const b of blobs){
    let k = 1;
    if(opt.beeArea && opt.n > 1 && b.area > opt.beeArea * opt.maxFrac){
      k = Math.round(b.area / opt.beeArea);
      // bees predicted to be inside this blob's extent also count
      if(predictions && predictions.length){
        const r = Math.sqrt(b.area / Math.PI) + Math.sqrt(opt.beeArea / Math.PI);
        const near = predictions.filter(p => (p[0] - b.x) ** 2 + (p[1] - b.y) ** 2 <= r * r);
        k = Math.max(k, near.length);
        if(near.length === k && k > 1){
          splitBlob(b, pix, g.w, k, near).forEach(d => dets.push({...d, merged: true}));
          continue;
        }
      }
      k = Math.max(2, Math.min(opt.n, k));
      splitBlob(b, pix, g.w, k).forEach(d => dets.push({...d, merged: true}));
      continue;
    }
    dets.push({x: b.x, y: b.y, area: b.area, merged: false});
  }
  return {dets, fg: m, blobs};
}
// Typical single-bee area from blobs on one frame (median of blob areas).
function estimateBeeArea(blobs){
  if(!blobs.length) return null;
  const a = blobs.map(b => b.area).sort((x, y) => x - y);
  return a[Math.floor((a.length - 1) / 2)];
}

/* ---------- assignment ---------- */
// Hungarian algorithm (min-cost). cost: n x m with n <= m. Returns column for each row.
function hungarian(cost){
  const n = cost.length, m = cost[0].length;
  const u = new Float64Array(n + 1), v = new Float64Array(m + 1), p = new Int32Array(m + 1), way = new Int32Array(m + 1);
  for(let i = 1; i <= n; i++){
    p[0] = i; let j0 = 0;
    const minv = new Float64Array(m + 1).fill(Infinity), used = new Uint8Array(m + 1);
    do{
      used[j0] = 1; const i0 = p[j0]; let delta = Infinity, j1 = 0;
      for(let j = 1; j <= m; j++) if(!used[j]){
        const cur = cost[i0 - 1][j - 1] - u[i0] - v[j];
        if(cur < minv[j]){ minv[j] = cur; way[j] = j0; }
        if(minv[j] < delta){ delta = minv[j]; j1 = j; }
      }
      for(let j = 0; j <= m; j++){ if(used[j]){ u[p[j]] += delta; v[j] -= delta; } else minv[j] -= delta; }
      j0 = j1;
    } while(p[j0] !== 0);
    do{ const j1 = way[j0]; p[j0] = p[j1]; j0 = j1; } while(j0);
  }
  const ans = new Int32Array(n).fill(-1);
  for(let j = 1; j <= m; j++) if(p[j]) ans[p[j] - 1] = j - 1;
  return ans;
}
// Returns [row, col] pairs with cost < big.
function assign(cost, big){
  const n = cost.length; if(!n) return []; const m = cost[0].length; if(!m) return [];
  const pairs = [];
  if(n <= m){ hungarian(cost).forEach((j, i) => { if(j >= 0 && cost[i][j] < big) pairs.push([i, j]); }); }
  else {
    const t = Array.from({length: m}, (_, j) => Array.from({length: n}, (_, i) => cost[i][j]));
    hungarian(t).forEach((i, j) => { if(i >= 0 && cost[i][j] < big) pairs.push([i, j]); });
  }
  return pairs;
}

/* ---------- tracking ---------- */
// opt: {n: number of bees, maxSpeed: proc px per s, gate0: proc px, reacquireAfter: s}
function createTracker(opt){
  const BIG = 1e9, tracks = [];
  let nextId = 1;
  function predict(tr, t){ const dt = Math.min(t - tr.tSeen, 0.5); return [tr.x + tr.vx * dt, tr.y + tr.vy * dt]; }
  function predictions(t){ return tracks.map(tr => predict(tr, t)); }
  function step(t, dets){
    const res = new Map();
    const cost = tracks.map(tr => {
      const [px, py] = predict(tr, t), gate = opt.gate0 + opt.maxSpeed * Math.max(0, t - tr.tSeen);
      return dets.map(d => { const c = Math.hypot(d.x - px, d.y - py); return c <= gate ? c : BIG; });
    });
    const pairs = tracks.length && dets.length ? assign(cost, BIG) : [];
    const usedT = new Set(), usedD = new Set();
    const update = (tr, d, reacq) => {
      const dt = t - tr.tSeen;
      // A position split out of a merged blob is rough, so the bee keeps its previous velocity through contact.
      if(reacq || dt > 0.5){ tr.vx = 0; tr.vy = 0; }
      else if(dt > 0 && !d.merged){ tr.vx = 0.5 * (d.x - tr.x) / dt + 0.5 * tr.vx; tr.vy = 0.5 * (d.y - tr.y) / dt + 0.5 * tr.vy; }
      tr.x = d.x; tr.y = d.y; tr.tSeen = t;
      res.set(tr.id, {x: d.x, y: d.y, area: d.area, det: true, merged: !!d.merged, reacq: !!reacq});
    };
    pairs.forEach(([i, j]) => { usedT.add(i); usedD.add(j); update(tracks[i], dets[j], false); });
    let freeD = dets.map((d, j) => j).filter(j => !usedD.has(j)).sort((a, b) => dets[b].area - dets[a].area);
    // new bees appear until all n are being tracked
    while(tracks.length < opt.n && freeD.length){
      const j = freeD.shift(), d = dets[j];
      const tr = {id: nextId++, x: d.x, y: d.y, vx: 0, vy: 0, tSeen: t};
      tracks.push(tr); usedT.add(tracks.length - 1);
      res.set(tr.id, {x: d.x, y: d.y, area: d.area, det: true, merged: !!d.merged, reacq: false});
    }
    // a bee lost for a while is matched to the nearest leftover blob (identity uncertain)
    tracks.forEach((tr, i) => {
      if(usedT.has(i) || !freeD.length || t - tr.tSeen < opt.reacquireAfter) return;
      let best = -1, bd = Infinity;
      freeD.forEach((j, k) => { const d = Math.hypot(dets[j].x - tr.x, dets[j].y - tr.y); if(d < bd){ bd = d; best = k; } });
      const j = freeD.splice(best, 1)[0]; usedT.add(i); update(tr, dets[j], true);
    });
    return {rows: tracks.map(tr => res.get(tr.id) || {x: null, y: null, area: 0, det: false, merged: false, reacq: false}),
      ids: tracks.map(tr => tr.id), extra: freeD.length};
  }
  return {step, predictions, get count(){ return tracks.length; }};
}

/* ---------- metrics ---------- */
// samples: {t:[], x:[], y:[], det:[]} in video px for one bee. opt: {scale:{sx,sy}|null (mm per px),
// smooth: s, maxGap: s, moveThr: units/s, zones:[{name, shape}], minBout: s}
function segments(t, det, maxGap){
  const segs = []; let cur = null;
  for(let i = 0; i < t.length; i++){
    if(!det[i]){ continue; }
    if(cur && t[i] - t[cur[cur.length - 1]] <= maxGap) cur.push(i);
    else { cur = [i]; segs.push(cur); }
  }
  return segs;
}
function smoothSeg(t, v, seg, half){
  const out = new Array(seg.length);
  let lo = 0, hi = 0, sum = 0;
  for(let k = 0; k < seg.length; k++){
    const tk = t[seg[k]];
    while(hi < seg.length && t[seg[hi]] <= tk + half + 1e-9){ sum += v[seg[hi]]; hi++; }
    while(t[seg[lo]] < tk - half - 1e-9){ sum -= v[seg[lo]]; lo++; }
    out[k] = sum / (hi - lo);
  }
  return out;
}
function runsFrom(steps, minBout, mergeGap){
  // steps: [{t0, t1, on}] in time order -> merged on-runs [[t0,t1]]
  const runs = [];
  steps.forEach(s => {
    if(!s.on) return;
    const last = runs[runs.length - 1];
    if(last && s.t0 - last[1] <= mergeGap) last[1] = s.t1; else runs.push([s.t0, s.t1]);
  });
  return runs.filter(r => r[1] - r[0] >= minBout);
}
function metrics(sm, opt){
  // opt.include(t) (optional): count only steps that start at times it accepts, e.g. one LED condition.
  const {t, det} = sm, sc = opt.scale, ux = sc ? sc.sx : 1, uy = sc ? sc.sy : 1, inc = opt.include || null;
  const segs = segments(t, det, opt.maxGap);
  const zones = opt.zones || [];
  let dist = 0, tracked = 0, moving = 0;
  const zt = zones.map(() => 0), visits = zones.map(() => 0), speeds = [];
  const mvSteps = [], zSteps = zones.map(() => []);
  const sx = new Array(t.length).fill(null), sy = new Array(t.length).fill(null), spd = new Array(t.length).fill(null);
  segs.forEach(seg => {
    const xs = smoothSeg(t, sm.x, seg, opt.smooth / 2), ys = smoothSeg(t, sm.y, seg, opt.smooth / 2);
    seg.forEach((i, k) => { sx[i] = xs[k]; sy[i] = ys[k]; });
    const inZ = zones.map(() => false);
    for(let k = 0; k < seg.length; k++){
      const zNow = zones.map(z => z.test ? z.test(xs[k], ys[k]) : inside(z.shape, xs[k], ys[k]));
      const counted = !inc || inc(t[seg[k]]);
      zNow.forEach((v, z) => { if(v && !inZ[z] && counted) visits[z]++; inZ[z] = v; });
      if(k === seg.length - 1) break;
      const dt = t[seg[k + 1]] - t[seg[k]]; if(dt <= 0 || !counted) continue;
      const d = Math.hypot((xs[k + 1] - xs[k]) * ux, (ys[k + 1] - ys[k]) * uy), v = d / dt;
      dist += d; tracked += dt; speeds.push(v); spd[seg[k]] = v;
      const on = v >= opt.moveThr; if(on) moving += dt;
      mvSteps.push({t0: t[seg[k]], t1: t[seg[k + 1]], on});
      zNow.forEach((inz, z) => { if(inz) zt[z] += dt; zSteps[z].push({t0: t[seg[k]], t1: t[seg[k + 1]], on: inz}); });
    }
  });
  const nDet = det.reduce((a, b) => a + (b ? 1 : 0), 0);
  const sorted = speeds.slice().sort((a, b) => a - b);
  const span = t.length > 1 ? t[t.length - 1] - t[0] : 0;
  const mb = opt.minBout ?? 0.3, mg = opt.mergeGap ?? 0.3;
  return {
    span, tracked, coverage: t.length ? nDet / t.length : 0, distance: dist,
    meanSpeed: tracked > 0 ? dist / tracked : null,
    p95Speed: sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(0.95 * (sorted.length - 1)))] : null,
    fracMoving: tracked > 0 ? moving / tracked : null, movingTime: moving,
    zones: zones.map((z, i) => ({name: z.name, time: zt[i], frac: tracked > 0 ? zt[i] / tracked : null, visits: visits[i]})),
    runs: {moving: runsFrom(mvSteps, mb, mg), zones: zSteps.map(s => runsFrom(s, mb, mg))},
    smooth: {x: sx, y: sy, speed: spd},
  };
}
// Mean distance from each bee to its nearest neighbour, over samples where both are detected.
function nearestNeighbour(tracks, scale){
  const ux = scale ? scale.sx : 1, uy = scale ? scale.sy : 1, n = tracks.length;
  if(n < 2) return tracks.map(() => null);
  const sum = new Array(n).fill(0), cnt = new Array(n).fill(0), len = tracks[0].x.length;
  for(let i = 0; i < len; i++) for(let a = 0; a < n; a++){
    if(!tracks[a].det[i]) continue;
    let best = Infinity;
    for(let b = 0; b < n; b++) if(b !== a && tracks[b].det[i]) best = Math.min(best, Math.hypot((tracks[a].x[i] - tracks[b].x[i]) * ux, (tracks[a].y[i] - tracks[b].y[i]) * uy));
    if(best < Infinity){ sum[a] += best; cnt[a]++; }
  }
  return sum.map((s, a) => cnt[a] ? s / cnt[a] : null);
}
/* ---------- LEDs (stimuli logged by Argus) ---------- */
const fmtLevel = v => (Math.round(v * 100) / 100) + ' V';
// Split [0, tEnd] into stretches by which LED outputs are on, and at what level.
// events: [{t, output, peak, segs: [[start_s, end_s, volts], ...]}] with t in trial seconds.
// A stimulus counts as "on" from its first to its last nonzero stretch (so a pulse train is one block).
function lightConditions(events, tEnd){
  const spans = [];
  events.forEach(e => {
    if(!e.segs || !e.segs.length) return;
    const a = e.t + e.segs[0][0], b = e.t + e.segs[e.segs.length - 1][1];
    if(b > a) spans.push({a, b, out: e.output, key: e.output + ' ' + fmtLevel(e.peak)});
  });
  const cuts = new Set([0, tEnd]);
  spans.forEach(s => { [s.a, s.b].forEach(v => { if(v > 0 && v < tEnd) cuts.add(v); }); });
  const pts = [...cuts].sort((x, y) => x - y), out = [];
  for(let i = 0; i < pts.length - 1; i++){
    const a = pts[i], b = pts[i + 1], m = (a + b) / 2, byOut = {};
    spans.forEach(s => { if(s.a <= m && m < s.b && (!byOut[s.out] || s.a > byOut[s.out].a)) byOut[s.out] = s; });
    const key = Object.keys(byOut).sort().map(o => byOut[o].key).join(' + ') || 'No stimulus';
    const last = out[out.length - 1];
    if(last && last.key === key) last.t1 = b; else out.push({t0: a, t1: b, key});
  }
  return out;
}
function conditionAt(intervals, t){
  let lo = 0, hi = intervals.length - 1;
  while(lo <= hi){ const m = (lo + hi) >> 1, c = intervals[m]; if(t < c.t0) hi = m - 1; else if(t >= c.t1) lo = m + 1; else return c.key; }
  return null;
}
// Times a light in the camera's "light check" box turned on/off, from its brightness per sample.
// Needs a clear difference between dark and lit (minContrast grey levels between the 10th and 90th percentiles).
function lightChanges(t, v, minContrast){
  const ok = v.map((x, i) => [t[i], x]).filter(p => p[1] != null && isFinite(p[1]));
  if(ok.length < 4) return null;
  const s = ok.map(p => p[1]).sort((a, b) => a - b), q = p => s[Math.min(s.length - 1, Math.floor(p * (s.length - 1)))];
  const lo = q(0.1), hi = q(0.9); if(hi - lo < (minContrast ?? 12)) return null;
  const mid = (lo + hi) / 2, hyst = (hi - lo) * 0.1, onsets = [], offsets = [];
  let on = ok[0][1] > mid;
  for(let i = 1; i < ok.length; i++){
    const [t1, x1] = ok[i], [t0, x0] = ok[i - 1];
    const cross = x0 === x1 ? t1 : t0 + (mid - x0) / (x1 - x0) * (t1 - t0);
    if(!on && x1 > mid + hyst){ on = true; onsets.push(Math.max(t0, Math.min(t1, cross))); }
    else if(on && x1 < mid - hyst){ on = false; offsets.push(Math.max(t0, Math.min(t1, cross))); }
  }
  return {onsets, offsets, lo, hi};
}
// For each commanded onset, the delay until the camera saw the light come on (within -0.2 .. maxLag s).
function matchLatency(ref, cam, maxLag){
  const out = [];
  ref.forEach(r => { const c = cam.find(x => x >= r - 0.2 && x <= r + (maxLag ?? 1)); if(c != null) out.push(c - r); });
  return out;
}

// Count separate merge episodes (runs of merged samples).
function episodes(flags){ let n = 0, prev = false; flags.forEach(f => { if(f && !prev) n++; prev = !!f; }); return n; }

return {inside, inset, procGeometry, toVideo, toProc, shapeMask, toLuma, maskedMedian, medianBackground, foreground, open3,
  findBlobs, splitBlob, detect, estimateBeeArea, hungarian, assign, createTracker, segments, metrics, nearestNeighbour, episodes,
  lightConditions, conditionAt, lightChanges, matchLatency, fmtLevel};
})();
if(typeof module !== 'undefined') module.exports = BeeCore;
