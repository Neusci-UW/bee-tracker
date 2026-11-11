#!/usr/bin/env python3
"""Record the narrated walkthrough video from the real page, with a simulated camera of three bees.

  python3 tools/walkthrough.py                       Kokoro voice af_heart (as PER Scorer's walkthrough)
  python3 tools/walkthrough.py --voice af_bella --speed 1.05

Writes walkthrough/bee-tracker-walkthrough.mp4 (video with the voice-over), walkthrough/captions.vtt and
walkthrough/chapters.vtt (used by bee-walkthrough.html), and the screenshots in tutorial/img/.

Each line of SCRIPT is spoken while its action runs; the next line starts when both are done, so the video always
fits the narration. Re-run it after the page changes. Needs (once, from the repository root):
  python3 -m venv .venv && .venv/bin/pip install playwright kokoro-onnx soundfile imageio-ffmpeg
  .venv/bin/playwright install chromium        (or use an installed Chrome: --chrome)
  mkdir -p .models && cd .models
  curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx
  curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
"""
from __future__ import annotations

import argparse, hashlib, json, shutil, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT, IMG, CACHE = ROOT / "walkthrough", ROOT / "tutorial" / "img", ROOT / "build" / "narration"
W, H = 1280, 800

# Spoken wording where the caption reads badly aloud.
SPOKEN = {"CSV": "C.S.V.", "LEDs": "L.E.D.s", "LED": "L.E.D.", "DAQ": "dack", "PER Scorer": "P.E.R. Scorer"}

# (chapter or None, caption, action, extra seconds to linger after the line). Actions are methods of Demo below.
SCRIPT = [
    ("Welcome", "Bee Tracker follows every bee in an arena, live from a camera or from a saved video, and turns the tracks into distances, speeds, zone times and social contact.", "intro", 0.5),
    (None, "This demo uses a simulated camera with three bees, so you can follow along without any hardware.", "hide_card", 0),
    ("Start a session", "Start with the session details: who you are, and what the experiment is.", "session", 0),
    (None, "Then press Start camera. If you have more than one camera, choose it first.", "camera", 0.5),
    ("Draw the arena", "Press Draw arena, and drag a box around the arena floor.", "arena", 0.3),
    (None, "Enter the arena's real width in millimetres. That sets the scale, so distances come out in millimetres.", "width", 0),
    (None, "Then tell it how many bees are in the arena.", "bees", 0),
    ("Add zones", "Zones mark places you care about, like a feeder. Press Add zone, and drag a box.", "zone", 0.3),
    (None, "Click a zone's outline to select it. You can rename it, drag it into place, or resize it with the handles.", "edit_zone", 0.3),
    (None, "An edge band measures time near the wall. Here, ten millimetres.", "edge", 0.3),
    ("Set up detection", "Now the Detect tab. With the bees in the arena, press Set up detection.", "detect", 0),
    (None, "It learns the background while the bees move, then works out the threshold, and the size of one bee.", "detect_wait", 0.3),
    (None, "It found all three bees. The magenta shows what it detects. If it ever gets something wrong, adjust it under Advanced.", "advanced", 0.5),
    ("LEDs", "The LEDs tab plays a light protocol on the DAQ with each trial. The desktop app connects to it for you.", "leds", 0.3),
    (None, "Each segment sets what both LEDs do. Here: dark, then blue on, dark again, then a green flicker.", "led_list", 0.3),
    (None, "Test LEDs turns each one on at five volts for a second, so you can check the wiring.", "led_test", 0.5),
    ("Run a trial", "Everything for a trial sits under the video. Set the length, here thirty seconds, and press Start trial.", "trial", 0),
    (None, "Each bee gets a number and a coloured track. The red timer shows how far the trial has got.", None, 1),
    (None, "The LED protocol plays on the DAQ's own clock, lined up with the camera frames.", None, 1),
    (None, "Keep this tab in front while it tracks: browsers slow down tabs that are hidden.", None, 1),
    (None, "When the trial ends, the results fill in straight away.", "trial_wait", 0.5),
    ("Read the results", "Each bee gets a row: distance walked, speed, time moving, time in each zone, and time near the wall.", "results", 0.5),
    (None, "With more than one bee, Near bee is the share of time within ten millimetres of another bee, with the number of encounters. The pair table breaks it down.", "social", 0.5),
    (None, "Beside the map of every track, two charts follow the trial over time: distance from the food, and distance between the bees. The shaded bands are the LED conditions.", "charts", 0.3),
    (None, "Point at a chart, and the map shows where each bee was at that moment.", "sweep", 0.3),
    ("Save your data", "The Data tab saves the tracks and summaries as CSV files, or exports to PER Scorer. Save the project file at the end of each session.", "data", 0.5),
    (None, "Each new trial becomes its own recording, ready to compare between groups.", "outro", 0),
    ("Recap", "That's it: draw the arena, set up detection, start the trial, and read the results.", "recap", 2.5),
]

# Overlay for the recording only: a visible cursor, click ripples, key caps and title cards.
OVERLAY = r"""
addEventListener('DOMContentLoaded', () => {
  const css = document.createElement('style');
  css.textContent = `#wt-cur{position:fixed;left:0;top:0;width:22px;height:22px;z-index:99999;pointer-events:none;transform:translate(-100px,-100px)}
  .wt-rip{position:fixed;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;border:3px solid #f0a92e;z-index:99998;pointer-events:none;animation:wtr .5s ease-out forwards}
  @keyframes wtr{from{transform:scale(.3);opacity:1}to{transform:scale(1.4);opacity:0}}
  #wt-card{position:fixed;inset:0;z-index:99990;display:none;place-items:center;background:rgba(18,23,28,.88);color:#fff;font-family:"IBM Plex Sans",sans-serif}
  #wt-card .in{max-width:760px;padding:0 40px}#wt-card .eb{color:#f0a92e;font:600 14px "IBM Plex Mono",monospace;letter-spacing:.12em;text-transform:uppercase}
  #wt-card h2{font-size:44px;margin:10px 0 14px;line-height:1.1}#wt-card p,#wt-card li{font-size:20px;line-height:1.5;color:#d5dde3}#wt-card ol{padding-left:26px;margin:0}`;
  document.head.appendChild(css);
  const cur = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); cur.id = 'wt-cur'; cur.setAttribute('viewBox', '0 0 22 22');
  cur.innerHTML = '<path d="M3 2 L3 18 L7.5 14 L10.5 20.5 L13.3 19.3 L10.4 12.8 L16.5 12.8 Z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/>';
  document.body.appendChild(cur);
  addEventListener('mousemove', e => { cur.style.transform = `translate(${e.clientX - 3}px,${e.clientY - 2}px)`; }, true);
  addEventListener('mousedown', e => { const r = document.createElement('div'); r.className = 'wt-rip'; r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px'; document.body.appendChild(r); setTimeout(() => r.remove(), 600); }, true);
  const card = document.createElement('div'); card.id = 'wt-card'; document.body.appendChild(card);
  window.__card = html => { card.innerHTML = html ? `<div class="in">${html}</div>` : ''; card.style.display = html ? 'grid' : 'none'; };
});
"""
INTRO = '<div class="eb">Bee Tracker · Walkthrough</div><h2>Tracking bees, start to finish</h2><p>Set up a session, draw the arena and zones, let Bee Tracker set up detection, run a trial with LEDs, then read and save the results.</p>'
RECAP = ('<div class="eb">Recap</div><h2>Every trial</h2><ol><li>Session details, then Start camera</li><li>Draw the arena, enter its width in mm, set the number of bees</li>'
         '<li>Add zones and an edge band</li><li>Detect tab: Set up detection</li><li>LEDs tab: connect and test (optional)</li><li>Set the length, Start trial</li>'
         '<li>Read the results and charts</li><li>Data tab: save CSVs and the project file</li></ol>')


class Demo:
    """The actions in SCRIPT, done the way a person would: the cursor moves to each control and clicks or types."""
    def __init__(self, page, helper_url, code):
        self.p, self.helper_url, self.code = page, helper_url, code

    # --- helpers ---
    def at(self, sel):
        el = self.p.locator(sel).first; el.scroll_into_view_if_needed(); b = el.bounding_box()
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    def move(self, x, y, steps=18): self.p.mouse.move(x, y, steps=steps)
    def click(self, sel, pause=0.25):
        self.move(*self.at(sel)); time.sleep(pause); self.p.mouse.down(); self.p.mouse.up(); time.sleep(0.2)
    def type(self, sel, text, enter=False):
        self.click(sel); self.p.keyboard.press("ControlOrMeta+a"); self.p.keyboard.type(text, delay=70)
        self.p.keyboard.press("Enter" if enter else "Tab"); time.sleep(0.2)
    def tab(self, name): self.click(f".sidetabs [data-tab={name}]", 0.15)
    def video(self, fx, fy):
        """Screen point at a fraction of the camera image."""
        box = self.p.locator("#stage canvas").bounding_box(); vw, vh = self.p.evaluate("[document.getElementById('vid').videoWidth, document.getElementById('vid').videoHeight]")
        k = min(box["width"] / vw, box["height"] / vh)
        return box["x"] + (box["width"] - vw * k) / 2 + fx * vw * k, box["y"] + (box["height"] - vh * k) / 2 + fy * vh * k
    def drag(self, a, b, steps=30):
        self.move(*a); time.sleep(0.2); self.p.mouse.down(); self.p.mouse.move(*b, steps=steps); time.sleep(0.15); self.p.mouse.up(); time.sleep(0.3)
    def idle(self, timeout=90): self.p.wait_for_function("!window.__beeTracker.busy", timeout=timeout * 1000)
    def shot(self, sel, name, pad=6):
        el = self.p.locator(sel).first; el.scroll_into_view_if_needed(); b = el.bounding_box()
        self.p.evaluate("document.getElementById('wt-cur').style.visibility = 'hidden'")
        self.p.screenshot(path=str(IMG / f"{name}.png"), clip={"x": max(0, b["x"] - pad), "y": max(0, b["y"] - pad), "width": b["width"] + 2 * pad, "height": b["height"] + 2 * pad})
        self.p.evaluate("document.getElementById('wt-cur').style.visibility = ''")
    def scroll_to(self, sel, block="start"):
        self.p.evaluate(f"document.querySelector({json.dumps(sel)}).scrollIntoView({{behavior: 'smooth', block: '{block}'}})"); time.sleep(0.9)

    # --- the actions ---
    def intro(self): self.p.evaluate(f"window.__card({json.dumps(INTRO)})"); self.move(W * 0.7, H * 0.6, 5)
    def hide_card(self): time.sleep(2.5); self.p.evaluate("window.__card(null)")
    def session(self):
        self.type("#m-exp", "AB"); self.type("#m-exp-id", "Feeder test, day 1")
    def camera(self):
        self.click("#bCamOn"); self.p.wait_for_function("window.__beeTracker.videoReady", timeout=30000); time.sleep(0.5)
    def arena(self):
        self.click("#bArena"); a = self.p.evaluate("window.__simArena"); vw, vh = W_CAM, H_CAM
        self.drag(self.video(a["x"] / vw, a["y"] / vh), self.video((a["x"] + a["w"]) / vw, (a["y"] + a["h"]) / vh))
    def width(self): self.tab("setup"); self.type("#a-w", "100")
    def bees(self): self.type("#r-n", "3")
    def zone(self):
        self.p.select_option("#zoneShape", "ellipse"); self.click("#bZone")
        self.drag(self.video(0.66, 0.16), self.video(0.78, 0.36)); self.p.keyboard.press("Escape")
    def edit_zone(self):
        z = self.p.evaluate("(s => s.recs.find(r => r.id === s.current).cfg.zones[0].shape)(window.__beeTracker.state)")
        self.move(*self.video(z["x"] / W_CAM, (z["y"] + z["h"] / 2) / H_CAM)); time.sleep(0.2); self.p.mouse.down(); self.p.mouse.up(); time.sleep(0.4)
        self.type("#selNm", "Food", enter=True)
        cx, cy = (z["x"] + z["w"] / 2) / W_CAM, (z["y"] + z["h"] / 2) / H_CAM
        self.drag(self.video(cx, cy), self.video(cx - 0.02, cy + 0.04), steps=25)
        self.shot("#selBar", "selbar")
    def edge(self):
        self.type("#a-edge", "10"); self.click("#selNone"); self.shot(".panel[aria-label='Arena and zones']", "zones"); self.shot(".panel[aria-label=Video]", "video")
    def detect(self):
        self.tab("detect"); self.p.evaluate("const e = document.getElementById('c-learn'); e.value = 5; e.dispatchEvent(new Event('change'))")
        self.click("#bAuto")
    def detect_wait(self): time.sleep(0.5); self.idle(); time.sleep(0.3); self.shot(".panel[aria-label=Detection]", "detect")
    def advanced(self):
        time.sleep(1.5); self.click("#dtAdv"); time.sleep(0.4); self.shot(".panel[aria-label=Detection]", "advanced"); time.sleep(1.2); self.click("#dtAuto")
    def leds(self):
        self.tab("leds")
        self.p.evaluate("""([url, code]) => {
          const S = window.__beeTracker.state, c = S.recs.find(r => r.id === S.current).cfg, F = BeeCore.ledFn;
          c.led = {names: {ao0: 'Blue', ao1: 'Green'}, repeats: 1, shuffle: false, segs: [
            {label: 'Dark', dur: 8, ao0: F('off'), ao1: F('off')}, {label: 'Blue', dur: 6, ao0: F('const', {v: 2}), ao1: F('off')},
            {label: 'Dark', dur: 6, ao0: F('off'), ao1: F('off')}, {label: 'Green flicker', dur: 6, ao0: F('off'), ao1: F('pulses', {freq: 5, duty: 50, on: 2, off: 0})},
            {label: 'Dark', dur: 4, ao0: F('off'), ao1: F('off')}]};
          const set = (id, v) => { const el = document.getElementById(id); el.value = v; el.dispatchEvent(new Event('change')); };
          set('led-rep', 1); set('ag-url', url); set('ag-code', code); }""", [self.helper_url, self.code])
        self.click("#ag-connect"); self.p.wait_for_function("document.getElementById('ag-state').textContent === 'connected'", timeout=10000)
    def led_list(self):
        for i in (1, 2, 4): self.move(*self.at(f"#ledList li:nth-child({i})"), steps=12); time.sleep(0.5)
        self.shot(".panel[aria-label=LEDs]", "leds")
    def led_test(self): self.click("#ledTest")
    def trial(self):
        self.p.evaluate("const s = document.getElementById('l-save'); if(s.checked){ s.checked = false; s.dispatchEvent(new Event('change')); }")
        self.type("#l-dur", "30"); self.click("#bRun"); time.sleep(4); self.shot(".trialbar", "trialbar"); self.move(*self.video(0.85, 0.8), 10); time.sleep(4); self.shot("#stage", "live", 0)
    def trial_wait(self):
        self.p.wait_for_function("document.getElementById('runMsg').textContent.startsWith('Trial done')", timeout=90000); time.sleep(0.5)
    def results(self): self.scroll_to("#resPanel"); self.move(*self.at("#resTbl tbody tr:nth-child(1) td:nth-child(4)"), 15); self.shot("#resPanel .tbl-wrap", "results", 2)
    def social(self):
        self.move(*self.at("#resTbl thead th:nth-last-child(5)"), 15); time.sleep(1); self.move(*self.at("#pairRes summary"), 15); self.shot("#pairRes", "pairs")
    def charts(self): self.scroll_to("#viz", "center"); self.shot("#viz", "charts")
    def sweep(self):
        b = self.p.locator("#zdChart svg").bounding_box(); y = b["y"] + b["height"] * 0.5
        self.move(b["x"] + b["width"] * 0.1, y, 10)
        for k in range(1, 41): self.p.mouse.move(b["x"] + b["width"] * (0.1 + 0.85 * k / 40), y); time.sleep(0.08)
        time.sleep(0.6); self.shot("#viz", "cursor")
    def data(self):
        self.p.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})"); time.sleep(0.8); self.tab("data")
        for sel in ("#xTracks", "#xSummary", "#xPer", "#xJson"): self.move(*self.at(sel), 12); time.sleep(0.45)
        self.shot(".panel[aria-label=Data]", "data")
    def outro(self): self.tab("setup"); self.move(*self.at("#recs"), 12)
    def recap(self): self.p.evaluate(f"window.__card({json.dumps(RECAP)})")


W_CAM, H_CAM = 1280, 720


def speak(lines, voice, speed):
    """One WAV per line (cached by its text), with its length in seconds."""
    import soundfile
    from kokoro_onnx import Kokoro
    models = ROOT / ".models"
    kokoro = Kokoro(str(models / "kokoro-v1.0.int8.onnx"), str(models / "voices-v1.0.bin"))
    CACHE.mkdir(parents=True, exist_ok=True); out = []
    for text in lines:
        said = text
        for a, b in SPOKEN.items(): said = said.replace(a, b)
        f = CACHE / (hashlib.sha1(f"{voice}|{speed}|{said}".encode()).hexdigest()[:16] + ".wav")
        if not f.exists():
            audio, sr = kokoro.create(said, voice=voice, speed=speed, lang="en-us")
            soundfile.write(f, audio, sr)
        info = soundfile.info(str(f)); out.append((f, info.frames / info.samplerate))
    return out


def vtt_time(s): h, s = divmod(s, 3600); m, s = divmod(s, 60); return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="af_heart"); ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--chrome", action="store_true", help="use the installed Google Chrome instead of Playwright's Chromium")
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright
    sys.path.insert(0, str(ROOT / "helper")); import led_helper as L
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    clips = speak([line for _, line, _, _ in SCRIPT], args.voice, args.speed)
    print(f"{len(clips)} lines, {sum(d for _, d in clips):.0f} s of speech", flush=True)
    subprocess.run([sys.executable, str(ROOT / "tools" / "build.py")], check=True)

    helper = L.Helper(L.SimulatedOutput(), 10.0, 0.0, code="424242"); srv = helper.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    OUT.mkdir(exist_ok=True); IMG.mkdir(parents=True, exist_ok=True)
    timeline = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome" if args.chrome else None, headless=True)
        ctx = b.new_context(viewport={"width": W, "height": H}, device_scale_factor=2, record_video_dir=tmp, record_video_size={"width": W, "height": H}, color_scheme="light")
        ctx.add_init_script(f"window.__simW = {W_CAM}; window.__simH = {H_CAM}; window.__simBees = 3; window.__simSeed = 11;" + (ROOT / "tools" / "simcam.js").read_text())
        ctx.add_init_script(OVERLAY)
        page = ctx.new_page(); t0 = time.monotonic(); errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto((ROOT / "bee-tracker.html").as_uri()); page.evaluate("localStorage.clear()"); page.reload(); page.wait_for_timeout(600)
        demo = Demo(page, f"http://127.0.0.1:{srv.server_address[1]}", "424242")
        for (chapter, line, action, linger), (wav, dur) in zip(SCRIPT, clips):
            start = time.monotonic() - t0
            if action: getattr(demo, action)()
            done = time.monotonic() - t0
            time.sleep(max(0.0, start + dur + 0.35 - done) + linger)
            timeline.append({"chapter": chapter, "line": line, "wav": str(wav), "start": start, "end": time.monotonic() - t0})
            print(f"  {start:6.1f}s  {line[:70]}", flush=True)
        page.wait_for_timeout(300); video_path = page.video.path(); ctx.close(); b.close()
        if errors: print("page errors:", errors)
        raw = Path(tmp) / "raw.webm"; shutil.copy(video_path, raw)
        # voice-over: each line at its start time
        inputs, filters = [], []
        for i, ev in enumerate(timeline):
            inputs += ["-i", ev["wav"]]; filters.append(f"[{i + 1}:a]adelay={int(ev['start'] * 1000)}:all=1[a{i}]")
        mix = "".join(f"[a{i}]" for i in range(len(timeline))) + f"amix=inputs={len(timeline)}:normalize=0[voice]"
        mp4 = OUT / "bee-tracker-walkthrough.mp4"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(raw), *inputs, "-filter_complex", ";".join(filters + [mix]),
                        "-map", "0:v", "-map", "[voice]", "-c:v", "libx264", "-preset", "slow", "-crf", "30", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-shortest", str(mp4)], check=True)
    total = timeline[-1]["end"]
    cues = ["WEBVTT", ""] + [f"{vtt_time(ev['start'])} --> {vtt_time(ev['end'])}\n{ev['line']}\n" for ev in timeline]
    (OUT / "captions.vtt").write_text("\n".join(cues), encoding="utf-8")
    chaps = [ev for ev in timeline if ev["chapter"]]
    (OUT / "chapters.vtt").write_text("\n".join(["WEBVTT", ""] + [f"{vtt_time(c['start'])} --> {vtt_time(chaps[i + 1]['start'] if i + 1 < len(chaps) else total)}\n{c['chapter']}\n"
                                                                    for i, c in enumerate(chaps)]), encoding="utf-8")
    print(f"wrote {mp4.relative_to(ROOT)} ({mp4.stat().st_size / 1e6:.1f} MB, {total / 60:.1f} min), captions, chapters and {len(list(IMG.glob('*.png')))} screenshots")


if __name__ == "__main__":
    main()
