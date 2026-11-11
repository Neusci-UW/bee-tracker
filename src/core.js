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

// Detection settings worked out from one frame and its background (processing image, inside the arena mask):
// the camera noise, which way the bees differ from the background, the threshold that finds the n bees with the most
// margin, and the size of one bee. fixed: any of {polarity, thr, open} the user set, which are kept as they are.
function autoDetect(img, bg, mask, bgMed, w, h, n, fixed){
  fixed = fixed || {};
  const off = bgMed == null ? 0 : bgMed - maskedMedian(img, mask);
  const d = new Int16Array(img.length), hist = new Uint32Array(256); let cnt = 0;
  for(let i = 0; i < img.length; i++) if(mask[i]){ const v = img[i] + off - bg[i]; d[i] = v; hist[Math.min(255, Math.abs(v))]++; cnt++; }
  if(!cnt) return {error: 'The arena has no pixels on the video.'};
  let c = 0, mad = 0; for(let v = 0; v < 256; v++){ c += hist[v]; if(c >= cnt / 2){ mad = v; break; } }
  const noise = 1.4826 * Math.max(1, mad), base = Math.max(8, Math.min(100, Math.round(5 * noise)));
  let dark = 0, light = 0;
  for(let i = 0; i < d.length; i++) if(mask[i]){ if(d[i] <= -base) dark++; else if(d[i] >= base) light++; }
  const polarity = fixed.polarity || (dark > 3 * light ? 'dark' : light > 3 * dark ? 'light' : 'both');
  const blobsAt = (thr, open) => {
    const fg = new Uint8Array(d.length);
    for(let i = 0; i < d.length; i++) if(mask[i]){ const v = polarity === 'dark' ? -d[i] : polarity === 'light' ? d[i] : Math.abs(d[i]); if(v >= thr) fg[i] = 1; }
    return findBlobs(open ? open3(fg, w, h) : fg, w, h, 4).blobs;
  };
  const ts = [];
  if(fixed.thr) ts.push(fixed.thr); else for(let t = base; t <= 120; t = Math.max(t + 1, Math.round(t * 1.12))) ts.push(t);
  const scan = open => ts.map(t => ({t, blobs: blobsAt(t, open)}));
  let open = fixed.open ?? true, runs = scan(open);
  if(fixed.open == null && runs.every(x => !x.blobs.length)){ open = false; runs = scan(open); }     // bees too small to survive the opening
  // the middle of the first run of thresholds that find exactly n blobs, else the closest count (lowest threshold)
  const first = runs.findIndex(x => x.blobs.length === n);
  let best;
  if(first >= 0){ let last = first; while(last + 1 < runs.length && runs[last + 1].blobs.length === n) last++; best = runs[(first + last) >> 1]; }
  else best = runs.reduce((a, x) => Math.abs(x.blobs.length - n) < Math.abs(a.blobs.length - n) ? x : a);
  return {polarity, thr: best.t, open, beeArea: estimateBeeArea(best.blobs), found: best.blobs.length, noise: +noise.toFixed(1), blobs: best.blobs};
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
/* ---------- distances over time and social contact ---------- */
// Distance from a point (video px) to a zone, in scale units: to its edge (0 inside) or to its centre.
// Ellipse edges use the radial projection, which is exact for circles.
function zoneDistance(s, x, y, toEdge, scale){
  const ux = scale ? scale.sx : 1, uy = scale ? scale.sy : 1;
  const dx = (x - s.x - s.w / 2) * ux, dy = (y - s.y - s.h / 2) * uy;
  if(!toEdge) return Math.hypot(dx, dy);
  const hw = s.w / 2 * ux, hh = s.h / 2 * uy;
  if(s.type === 'rect') return Math.hypot(Math.max(0, Math.abs(dx) - hw), Math.max(0, Math.abs(dy) - hh));
  const r = Math.hypot(dx / hw, dy / hh);
  return r <= 1 ? 0 : Math.hypot(dx, dy) * (1 - 1 / r);
}
// Centre-to-centre distance of every pair of bees per sample (null where either is missing). xs[k][i], ys[k][i] in video px.
function pairDistances(xs, ys, scale){
  const ux = scale ? scale.sx : 1, uy = scale ? scale.sy : 1, out = [];
  for(let a = 0; a < xs.length; a++) for(let b = a + 1; b < xs.length; b++)
    out.push({a, b, d: xs[a].map((x, i) => x == null || xs[b][i] == null ? null : Math.hypot((x - xs[b][i]) * ux, (ys[a][i] - ys[b][i]) * uy))});
  return out;
}
// Social contact: how long each bee is within thr of any other bee, and each pair's time close, encounters (runs of
// being close, merged and filtered like other bouts) and first contact. Steps longer than maxGap are not counted.
function social(t, pairs, n, thr, maxGap, minBout, mergeGap){
  const mk = () => ({close: 0, measured: 0, steps: []});
  const bees = Array.from({length: n}, mk), prs = pairs.map(p => Object.assign(mk(), {a: p.a, b: p.b, first: null}));
  for(let i = 0; i < t.length - 1; i++){
    const dt = t[i + 1] - t[i]; if(!(dt > 0) || dt > maxGap) continue;
    const near = new Array(n).fill(null);
    pairs.forEach((p, j) => {
      const d = p.d[i]; if(d == null) return;
      const on = d < thr, q = prs[j];
      q.measured += dt; q.steps.push({t0: t[i], t1: t[i + 1], on});
      if(on){ q.close += dt; if(q.first == null) q.first = t[i]; }
      near[p.a] = near[p.a] || on; near[p.b] = near[p.b] || on;
    });
    near.forEach((on, k) => { if(on == null) return; const B = bees[k]; B.measured += dt; if(on) B.close += dt; B.steps.push({t0: t[i], t1: t[i + 1], on}); });
  }
  const fin = o => {
    o.runs = runsFrom(o.steps, minBout, mergeGap); delete o.steps;
    o.frac = o.measured > 0 ? o.close / o.measured : null; o.encounters = o.runs.length;
    o.meanEncounter = o.runs.length ? o.runs.reduce((a, r) => a + r[1] - r[0], 0) / o.runs.length : null;
    return o;
  };
  return {bees: bees.map(fin), pairs: prs.map(fin)};
}
// Moves between arena areas (zones marked as arenas, e.g. two linked arenas). area[i]: the index of the area the bee
// is in at sample i, -1 in none, null if not seen. Each change of area is a crossing; the time from last being seen in
// one area to first being seen in the next is time in transit (in a tunnel neither camera sees, say).
function crossings(t, area){
  const out = []; let last = null, lastT = null;
  for(let i = 0; i < t.length; i++){
    const a = area[i]; if(a == null || a < 0) continue;
    if(last != null && a !== last) out.push({from: last, to: a, t0: lastT, t1: t[i]});
    last = a; lastT = t[i];
  }
  return {list: out, n: out.length, transit: out.reduce((x, c) => x + c.t1 - c.t0, 0), first: out.length ? out[0] : null};
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

/* ---------- LED protocol: segments, each output following a function of time ---------- */
// A protocol is segments of {label, dur (s), ao0: fn, ao1: fn}; fn = {fn: name, ...params}, with t = seconds into the segment.
// helper/led_helper.py builds the DAQ's waveform from the same definitions: change both together.
const LED_FNS = {
  off:     {name: 'Off', params: []},
  const:   {name: 'Constant', params: [['v', 'V', 2]]},
  ramp:    {name: 'Ramp', params: [['from', 'from V', 0], ['to', 'to V', 4]]},
  exp:     {name: 'Exponential', params: [['from', 'from V', 4], ['to', 'to V', 0], ['tau', 'τ s', 2]]},
  sine:    {name: 'Sine', params: [['freq', 'Hz', 0.5], ['mid', 'mid V', 2.5], ['amp', '± V', 2], ['phase', 'phase °', 0]]},
  pulses:  {name: 'Pulse train', params: [['freq', 'Hz', 10], ['duty', 'duty %', 50], ['on', 'on V', 3], ['off', 'off V', 0]]},
  pulse:   {name: 'Single pulse', params: [['delay', 'delay s', 1], ['width', 'width s', 0.5], ['v', 'V', 3]]},
  random:  {name: 'Random levels', params: [['min', 'min V', 0], ['max', 'max V', 3], ['hold', 'hold s', 0.5], ['seed', 'seed', 1]]},
  formula: {name: 'Formula', params: [['expr', 'f(t) =', '2.5 + 2*sin(2*pi*t)']]},
};
function ledFn(name, params){
  const f = {fn: Object.hasOwn(LED_FNS, name) ? name : 'off'};
  LED_FNS[f.fn].params.forEach(([k, , d]) => { f[k] = params && params[k] != null ? params[k] : d; });
  return f;
}

// Formulas: numbers, t (s into the segment), d (segment length), pi, e; + - * / ^, comparisons (1 or 0), and
// sin cos tan abs sqrt exp log floor ceil round min max mod clamp. Parsed, never run as code.
const FORMULA_FNS = {sin: [1, Math.sin], cos: [1, Math.cos], tan: [1, Math.tan], abs: [1, Math.abs], sqrt: [1, Math.sqrt],
  exp: [1, Math.exp], log: [1, Math.log], floor: [1, Math.floor], ceil: [1, Math.ceil], round: [1, x => Math.floor(x + 0.5)],
  min: [2, Math.min], max: [2, Math.max], mod: [2, (a, b) => a - b * Math.floor(a / b)], clamp: [3, (x, a, b) => Math.min(Math.max(x, a), b)]};
function parseFormula(src){
  src = String(src == null ? '' : src);
  if(!src.trim()) throw new Error('The formula is empty.');
  if(src.length > 300) throw new Error('The formula is longer than 300 characters.');
  const toks = [], re = /\s*(?:(\d+\.?\d*(?:e[+-]?\d+)?|\.\d+(?:e[+-]?\d+)?)|([a-z_]\w*)|(<=|>=|[-+*/^(),<>]))/iy;
  let m;
  re.lastIndex = 0;
  while(re.lastIndex < src.length){
    const at = re.lastIndex;
    if(!(m = re.exec(src))){ if(/^\s*$/.test(src.slice(at))) break; throw new Error(`The formula has a character it can’t use: “${src.slice(at).trim()[0]}”.`); }
    toks.push(m[1] != null ? {n: +m[1]} : m[2] != null ? {id: m[2].toLowerCase()} : {op: m[3]});
  }
  let i = 0;
  const peek = () => toks[i], isOp = o => toks[i] && toks[i].op === o;
  const need = o => { if(!isOp(o)) throw new Error(`The formula is missing “${o}”.`); i++; };
  function cmp(){
    const a = sum();
    if(toks[i] && ['<', '<=', '>', '>='].includes(toks[i].op)){
      const o = toks[i++].op, b = sum();
      return o === '<' ? (t, d) => +(a(t, d) < b(t, d)) : o === '<=' ? (t, d) => +(a(t, d) <= b(t, d))
        : o === '>' ? (t, d) => +(a(t, d) > b(t, d)) : (t, d) => +(a(t, d) >= b(t, d));
    }
    return a;
  }
  function sum(){
    let a = prod();
    while(isOp('+') || isOp('-')){ const o = toks[i++].op, x = a, b = prod(); a = o === '+' ? (t, d) => x(t, d) + b(t, d) : (t, d) => x(t, d) - b(t, d); }
    return a;
  }
  function prod(){
    let a = unary();
    while(isOp('*') || isOp('/')){ const o = toks[i++].op, x = a, b = unary(); a = o === '*' ? (t, d) => x(t, d) * b(t, d) : (t, d) => x(t, d) / b(t, d); }
    return a;
  }
  function unary(){
    if(isOp('-')){ i++; const a = unary(); return (t, d) => -a(t, d); }
    if(isOp('+')){ i++; return unary(); }
    const a = atom();
    if(isOp('^')){ i++; const b = unary(); return (t, d) => Math.pow(a(t, d), b(t, d)); }
    return a;
  }
  function atom(){
    const k = peek();
    if(!k) throw new Error('The formula ends too early.');
    i++;
    if(k.n != null){ const v = k.n; return () => v; }
    if(k.op === '('){ const a = cmp(); need(')'); return a; }
    if(k.id != null){
      if(k.id === 't') return t => t;
      if(k.id === 'd') return (t, d) => d;
      if(k.id === 'pi') return () => Math.PI;
      if(k.id === 'e') return () => Math.E;
      const f = Object.hasOwn(FORMULA_FNS, k.id) && FORMULA_FNS[k.id];
      if(!f) throw new Error(`The formula uses “${k.id}”, which it doesn’t know. Use t, d, pi, e, or a function such as sin(…).`);
      need('(');
      const args = [cmp()];
      while(isOp(',')){ i++; args.push(cmp()); }
      need(')');
      if(args.length !== f[0]) throw new Error(`${k.id}(…) takes ${f[0]} value${f[0] === 1 ? '' : 's'}.`);
      const g = f[1];
      return args.length === 1 ? (t, d) => g(args[0](t, d)) : args.length === 2 ? (t, d) => g(args[0](t, d), args[1](t, d))
        : (t, d) => g(args[0](t, d), args[1](t, d), args[2](t, d));
    }
    throw new Error(`The formula has “${k.op}” where a number or name should be.`);
  }
  const f = cmp();
  if(i < toks.length) throw new Error(`The formula has something extra at “${toks[i].op || toks[i].id || toks[i].n}”.`);
  return f;
}
// Random levels: the same hash as the helper's, so the page knows exactly what was played.
function hash01(seed, k){
  let h = Math.imul(seed ^ 0x9E3779B9, 0x85EBCA6B) ^ Math.imul(k + 1, 0xC2B2AE35);
  h ^= h >>> 16; h = Math.imul(h, 0x85EBCA6B); h ^= h >>> 13; h = Math.imul(h, 0xC2B2AE35); h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}
// Problems with one output's settings, as sentences (empty if fine).
function ledFnProblems(f){
  if(!f || !Object.hasOwn(LED_FNS, f.fn)) return ['Unknown function.'];
  const def = LED_FNS[f.fn];
  if(f.fn === 'formula'){ try{ parseFormula(f.expr); return []; }catch(e){ return [e.message]; } }
  const p = [];
  def.params.forEach(([k, label]) => { if(!(typeof f[k] === 'number' && isFinite(f[k]))) p.push(`${label.replace(/ (V|s|Hz|%|°)$/, '')} must be a number.`); });
  if(p.length) return p;
  if(f.fn === 'exp' && !(f.tau > 0)) p.push('τ must be more than 0 s.');
  if(f.fn === 'pulses' && !(f.freq > 0)) p.push('The pulse rate must be more than 0 Hz.');
  if(f.fn === 'pulses' && !(f.duty > 0 && f.duty < 100)) p.push('The duty cycle must be between 0 and 100%.');
  if(f.fn === 'sine' && f.freq < 0) p.push('The frequency can’t be negative.');
  if(f.fn === 'pulse' && (f.delay < 0 || !(f.width > 0))) p.push('The pulse needs a delay of 0 s or more and a width over 0 s.');
  if(f.fn === 'random' && !(f.hold >= 0.001)) p.push('Each random level must hold for at least 0.001 s.');
  if(f.fn === 'random' && !(Number.isInteger(f.seed) && f.seed >= 0 && f.seed < 2 ** 31)) p.push('The seed must be a whole number from 0.');
  return p;
}
// A function of (t, d) for one output's settings; call ledFnProblems first.
function ledEval(f){
  switch(f && f.fn){
    case 'const': return () => f.v;
    case 'ramp': return (t, d) => f.from + (f.to - f.from) * t / d;
    case 'exp': return t => f.to + (f.from - f.to) * Math.exp(-t / f.tau);
    case 'sine': return t => f.mid + f.amp * Math.sin(2 * Math.PI * f.freq * t + f.phase * Math.PI / 180);
    case 'pulses': return t => { const x = t * f.freq; return x - Math.floor(x) < f.duty / 100 - 1e-9 ? f.on : f.off; };
    case 'pulse': return t => t >= f.delay - 1e-9 && t < f.delay + f.width - 1e-9 ? f.v : 0;
    case 'random': return t => f.min + (f.max - f.min) * hash01(f.seed, Math.floor(t / f.hold + 1e-9));
    case 'formula': return parseFormula(f.expr);
    default: return () => 0;
  }
}
// (Edges get 1e-9 s of slack, so one that falls on a sample time, like 0.2 + 0.1 s, lands the same way every time.)
// Samples per segment, as the helper counts them.
const ledSamples = (dur, rate) => Math.max(1, Math.round(dur * rate));
// Lowest and highest level of one output over a segment, at the DAQ's sample times (NaN if a formula breaks).
function ledRange(f, dur, rate){
  const g = ledEval(f), n = ledSamples(dur, rate);
  let lo = Infinity, hi = -Infinity;
  for(let j = 0; j < n; j++){ const v = g(j / rate, dur); if(!isFinite(v)) return {lo: NaN, hi: NaN, at: j / rate}; if(v < lo) lo = v; if(v > hi) hi = v; }
  return {lo, hi};
}
// Short description of what one output does, e.g. "2 V", "ramp 0–4 V", "10 Hz pulses 3 V".
function ledDescribe(f){
  const v = x => (Math.round(x * 100) / 100);
  switch(f && f.fn){
    case 'const': return v(f.v) + ' V';
    case 'ramp': return `ramp ${v(f.from)}–${v(f.to)} V`;
    case 'exp': return `exp ${v(f.from)}→${v(f.to)} V, τ ${v(f.tau)} s`;
    case 'sine': return `sine ${v(f.freq)} Hz, ${v(f.mid)} ± ${v(f.amp)} V` + (f.phase ? `, ${v(f.phase)}°` : '');
    case 'pulses': return `${v(f.freq)} Hz pulses ${v(f.on)} V, ${v(f.duty)}%` + (f.off ? ` (off ${v(f.off)} V)` : '');
    case 'pulse': return `pulse ${v(f.v)} V at ${v(f.delay)}–${v(f.delay + f.width)} s`;
    case 'random': return `random ${v(f.min)}–${v(f.max)} V every ${v(f.hold)} s`;
    case 'formula': return 'f(t) = ' + String(f.expr).trim();
    default: return 'off';
  }
}
const ledIsOff = f => !f || f.fn === 'off' || (f.fn === 'const' && f.v === 0);
// The condition a segment counts as: its label, or else what the LEDs do ("Blue 2 V + Green sine 1 Hz, …").
function ledConditionName(seg, names){
  const lab = String(seg.label || '').trim();
  if(lab) return lab;
  const on = ['ao0', 'ao1'].filter(o => !ledIsOff(seg[o])).map(o => `${(names && names[o]) || o} ${ledDescribe(seg[o])}`);
  return on.join(' + ') || 'No stimulus';
}
// Segments in play order: the protocol `repeats` times, shuffled within each repeat when asked (seeded, so it can be logged).
function ledSequence(led, seed){
  const segs = led.segs || [], out = [];
  let s = (seed >>> 0) || 1;
  const rnd = () => { s ^= s << 13; s >>>= 0; s ^= s >>> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; };
  for(let r = 0; r < Math.max(1, led.repeats | 0); r++){
    const idx = segs.map((_, k) => k);
    if(led.shuffle) for(let k = idx.length - 1; k > 0; k--){ const j = Math.floor(rnd() * (k + 1)); [idx[k], idx[j]] = [idx[j], idx[k]]; }
    idx.forEach(k => out.push(Object.assign({index: k, repeat: r + 1}, segs[k])));
  }
  return out;
}
// What was played, on the trial clock: conditions over [0, tEnd] (gaps are "No stimulus"),
// one event per segment and lit output, and the times an output went from 0 V to on (for the light check).
// starts: each segment's start in s after the protocol began (from the helper); t0: protocol start in trial s.
function ledPlayed(seq, starts, t0, tEnd, rate, names){
  const conditions = [], events = [], onsets = [];
  const add = (a, b, key) => { if(b <= a) return; const last = conditions[conditions.length - 1];
    if(last && last.key === key && Math.abs(last.t1 - a) < 1e-6) last.t1 = b; else conditions.push({t0: a, t1: b, key}); };
  let prevEnd = 0;
  const lastOn = {ao0: false, ao1: false};
  seq.forEach((s, i) => {
    const a = t0 + starts[i], b = a + ledSamples(s.dur, rate) / rate;
    if(a >= tEnd) return;
    const key = ledConditionName(s, names), end = Math.min(b, tEnd);
    add(Math.max(prevEnd, 0), Math.max(0, a), 'No stimulus');
    add(Math.max(a, 0), end, key);
    prevEnd = end;
    ['ao0', 'ao1'].forEach(o => {
      const f = s[o], g = ledEval(f), n = Math.min(ledSamples(s.dur, rate), Math.ceil((end - a) * rate - 1e-6));
      if(ledIsOff(f)){ lastOn[o] = false; return; }
      let lo = Infinity, hi = -Infinity;
      for(let j = 0; j < n; j++){
        const v = g(j / rate, s.dur), on = v > 0;
        if(on && !lastOn[o] && onsets.length < 20000) onsets.push(+(a + j / rate).toFixed(4));
        lastOn[o] = on; if(v < lo) lo = v; if(v > hi) hi = v;
      }
      if(n > 0) events.push({t: +a.toFixed(4), dur: +(end - a).toFixed(4), output: (names && names[o]) || o, channel: o, segment: s.index + 1, repeat: s.repeat,
        label: key, fn: ledDescribe(f), peak: +hi.toFixed(4), low: +lo.toFixed(4), segs: [[0, +(end - a).toFixed(4), +hi.toFixed(4)]]});
    });
  });
  add(Math.max(prevEnd, 0), tEnd, 'No stimulus');
  events.sort((x, y) => x.t - y.t || x.channel.localeCompare(y.channel)).forEach((e, k) => { e.seq = k + 1; });
  return {conditions, events, onsets: onsets.sort((x, y) => x - y)};
}

// Count separate merge episodes (runs of merged samples).
function episodes(flags){ let n = 0, prev = false; flags.forEach(f => { if(f && !prev) n++; prev = !!f; }); return n; }

return {inside, inset, procGeometry, toVideo, toProc, shapeMask, toLuma, maskedMedian, medianBackground, foreground, open3,
  findBlobs, splitBlob, detect, estimateBeeArea, autoDetect, hungarian, assign, createTracker, segments, metrics, nearestNeighbour, episodes,
  zoneDistance, pairDistances, social, crossings,
  lightConditions, conditionAt, lightChanges, matchLatency, fmtLevel,
  LED_FNS, ledFn, parseFormula, hash01, ledFnProblems, ledEval, ledSamples, ledRange, ledDescribe, ledIsOff, ledConditionName, ledSequence, ledPlayed};
})();
if(typeof module !== 'undefined') module.exports = BeeCore;
