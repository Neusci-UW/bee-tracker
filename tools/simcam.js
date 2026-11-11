// A simulated top-down arena camera, for demos, the walkthrough recording and speed tests. Injected before the page
// loads, it replaces the browser's camera with a drawn round arena on a light floor and N dark bees that walk, pause,
// turn and bump into each other, with sensor noise and slight flicker.
// Settings (set on window before this runs): __simW, __simH (default 1280x720), __simBees (3), __simSeed (7).
// The bees' true positions are kept in window.__simTruth ({t, p: performance.now(), xy: [[x, y], ...]} per frame),
// and the arena's box in window.__simArena.
(() => {
  const W = +(window.__simW || 1280), H = +(window.__simH || 720), N = +(window.__simBees || 3), FPS = 30;
  const cx = W / 2, cy = H / 2, R = H * 0.42, bee = H / 720;            // a bee is about 26x14 px at 720p
  const cv = document.createElement('canvas'); cv.width = W; cv.height = H; const g = cv.getContext('2d');
  // a fixed noise tile drawn at a random offset each frame: cheap sensor noise
  const nt = document.createElement('canvas'); nt.width = nt.height = 256; const ng = nt.getContext('2d'), id = ng.createImageData(256, 256);
  for(let i = 0; i < id.data.length; i += 4){ const v = Math.random() * 255; id.data[i] = id.data[i + 1] = id.data[i + 2] = v; id.data[i + 3] = 14; }
  ng.putImageData(id, 0, 0);
  const pat = g.createPattern(nt, 'repeat');
  let seed = +(window.__simSeed || 7); const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const bees = Array.from({length: N}, (_, k) => {
    const a = k / N * 6.28, r = R * 0.5;
    return {x: cx + r * Math.cos(a), y: cy + r * Math.sin(a), h: rnd() * 6.28, v: 0, rest: rnd() * 2, walk: 0};
  });
  const truth = []; window.__simTruth = truth; let t = 0;
  function step(dt){
    bees.forEach(b => {
      if(b.rest > 0){ b.rest -= dt; b.v = 0; if(b.rest <= 0) b.walk = 1 + rnd() * 4; }
      else { b.walk -= dt; b.v = (70 + 60 * rnd()) * bee; b.h += (rnd() - 0.5) * 12 * dt; if(b.walk <= 0) b.rest = rnd() < 0.3 ? 1 + rnd() * 3 : 0.2; }
      b.x += Math.cos(b.h) * b.v * dt; b.y += Math.sin(b.h) * b.v * dt;
      const dx = b.x - cx, dy = b.y - cy, d = Math.hypot(dx, dy), lim = R - 16 * bee;
      if(d > lim){ b.x = cx + dx / d * lim; b.y = cy + dy / d * lim; b.h = Math.atan2(-dy, -dx) + (rnd() - 0.5) * 2; }
    });
  }
  function paint(){
    const dt = 1 / FPS; t += dt; step(dt);
    const fl = Math.round(2 * Math.sin(t * 7));                          // slight flicker
    g.fillStyle = `rgb(${120 + fl},${118 + fl},${112 + fl})`; g.fillRect(0, 0, W, H);
    g.fillStyle = `rgb(${226 + fl},${224 + fl},${218 + fl})`; g.beginPath(); g.arc(cx, cy, R, 0, 7); g.fill();
    g.strokeStyle = '#8a8478'; g.lineWidth = 6 * bee; g.stroke();
    g.fillStyle = 'rgba(0,0,0,0.05)'; g.beginPath(); g.arc(cx + 6, cy + 6, R - 3, 0, 7); g.arc(cx, cy, R - 12 * bee, 0, 7, true); g.fill();
    bees.forEach(b => {
      g.save(); g.translate(b.x, b.y); g.rotate(b.h);
      g.fillStyle = 'rgba(200,200,205,0.55)'; g.beginPath(); g.ellipse(-2 * bee, -7 * bee, 9 * bee, 4 * bee, -0.4, 0, 7); g.ellipse(-2 * bee, 7 * bee, 9 * bee, 4 * bee, 0.4, 0, 7); g.fill();
      g.fillStyle = '#3a2a14'; g.beginPath(); g.ellipse(0, 0, 13 * bee, 7 * bee, 0, 0, 7); g.fill();
      g.fillStyle = '#d9a514'; for(const s of [-5, 1]) g.fillRect(s * bee, -6 * bee, 2.5 * bee, 12 * bee);
      g.fillStyle = '#1c140a'; g.beginPath(); g.arc(13 * bee, 0, 5 * bee, 0, 7); g.fill();
      g.restore();
    });
    g.save(); g.translate(-Math.random() * 256, -Math.random() * 256); g.fillStyle = pat; g.fillRect(0, 0, W + 256, H + 256); g.restore();
    truth.push({t, p: performance.now(), xy: bees.map(b => [Math.round(b.x * 10) / 10, Math.round(b.y * 10) / 10])});
    if(truth.length > 20000) truth.shift();
  }
  setInterval(paint, 1000 / FPS); paint();
  const stream = cv.captureStream(FPS), label = `Simulated arena camera (${N} bees)`;
  stream.getVideoTracks().forEach(tr => Object.defineProperty(tr, 'label', {value: label}));
  navigator.mediaDevices.getUserMedia = async () => stream;
  navigator.mediaDevices.enumerateDevices = async () => [{kind: 'videoinput', deviceId: 'sim', label, groupId: ''}];
  window.__simCanvas = cv; window.__simArena = {x: cx - R, y: cy - R, w: 2 * R, h: 2 * R};
})();
