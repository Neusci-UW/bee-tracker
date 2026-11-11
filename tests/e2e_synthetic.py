#!/usr/bin/env python3
"""End-to-end check: drive bee-tracker.html in headless Chrome on synthetic videos and compare with ground truth.

  python3 tools/make_synthetic.py /tmp/synth
  python3 tests/e2e_synthetic.py /tmp/synth [play|seek] [scenario ...]

Uses the page the way a person would: open the video, drag the arena, type the arena size,
measure the bee size on a frame where the bees are apart, and press Track bees.
"""
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from playwright.sync_api import sync_playwright
from scipy.optimize import linear_sum_assignment

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "bee-tracker.html").as_uri()
FPS = 30
ARENA_BOX = (340, 60, 600, 600)   # matches tools/make_synthetic.py
PX_PER_MM = 600 / 90.0


def load_truth(path):
    truth = defaultdict(dict)  # frame -> bee -> (x, y, moving)
    for row in csv.DictReader(open(path)):
        truth[int(row["frame"])][int(row["bee"])] = (float(row["x_px"]), float(row["y_px"]), int(row["moving"]))
    return truth


def apart_frame(truth, n):
    """A frame where every pair of bees is at least 2.5 body lengths apart."""
    for f in range(60, max(truth), 15):
        pts = list(truth[f].values())
        if all(math.dist(a[:2], b[:2]) > 2.5 * 12 * PX_PER_MM for i, a in enumerate(pts) for b in pts[i + 1:]):
            return f
    return 60


def run(page, video, n, method, t_apart):
    page.goto(PAGE)
    page.evaluate("localStorage.clear()")
    page.reload()
    page.set_input_files("#vidFile", str(video))
    page.wait_for_function("window.__beeTracker.videoReady", timeout=30000)
    page.wait_for_timeout(300)
    # drag the arena box
    page.click("#bArena")
    box = page.evaluate("""([x, y, w, h]) => {
        const ov = document.getElementById('ov'), v = document.getElementById('vid'), b = ov.getBoundingClientRect();
        const k = Math.min(b.width / v.videoWidth, b.height / v.videoHeight), ox = (b.width - v.videoWidth * k) / 2, oy = (b.height - v.videoHeight * k) / 2;
        return [b.left + ox + x * k, b.top + oy + y * k, b.left + ox + (x + w) * k, b.top + oy + (y + h) * k];
    }""", list(ARENA_BOX))
    page.mouse.move(box[0], box[1]); page.mouse.down(); page.mouse.move((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, steps=4)
    page.mouse.move(box[2], box[3], steps=4); page.mouse.up()
    page.fill("#a-w", "90"); page.press("#a-w", "Enter")
    page.fill("#r-n", str(n)); page.press("#r-n", "Enter")
    page.click(".sidetabs [data-tab=detect]"); page.click("#dtAdv")          # these controls are under Detect → Advanced
    page.select_option("#t-method", method)
    page.evaluate(f"document.getElementById('vid').currentTime = {t_apart}")
    page.wait_for_timeout(500)
    page.click("#bMeasure")
    page.wait_for_function("!window.__beeTracker.busy && document.getElementById('detMsg').textContent.includes('One bee')", timeout=120000)
    det_msg = page.inner_text("#detMsg")
    page.click("#bRun")
    page.wait_for_timeout(500)
    page.wait_for_function("!window.__beeTracker.busy", timeout=900000, polling=1000)
    run_msg = page.inner_text("#runMsg")
    state = page.evaluate("JSON.parse(JSON.stringify(window.__beeTracker.state))")
    metrics = page.evaluate("""(() => { const m = window.__beeTracker.recMetrics(); return m && {bees: m.bees.map(b => ({distance: b.distance, fracMoving: b.fracMoving, coverage: b.coverage, merges: b.merges, reacq: b.reacq})), pooled: m.pooled}; })()""")
    return state, metrics, det_msg, run_msg


def evaluate(state, metrics, truth, n):
    rec = next(r for r in state["recs"] if r["id"] == state["current"])
    R = rec["result"]
    if not R:
        return {"error": "no result"}
    T = R["t"]
    tracks = R["tracks"]
    errs, last_id, swaps, found, lone = [], [None] * n, 0, 0, 0
    merged_any = [any(tracks[k]["mer"][i] or tracks[k]["rea"][i] for k in range(n)) for i in range(len(T))]
    near_merge = lambda i: any(merged_any[max(0, i - 15):i + 16])
    for i, t in enumerate(T):
        f = round(t * FPS)
        if f not in truth:
            continue
        gt = truth[f]
        gids = sorted(gt)
        det = [k for k in range(n) if tracks[k]["det"][i]]
        found += len(det)
        if not det:
            continue
        cost = np.array([[math.dist((tracks[k]["x"][i], tracks[k]["y"][i]), gt[g][:2]) for g in gids] for k in det])
        rows, cols = linear_sum_assignment(cost)
        for r_, c_ in zip(rows, cols):
            k, g = det[r_], gids[c_]
            errs.append(cost[r_, c_] / PX_PER_MM)
            if last_id[k] is not None and last_id[k] != g:
                swaps += 1
                lone += 0 if near_merge(i) else 1
            last_id[k] = g
    # ground-truth totals at full frame rate
    frames = sorted(truth)
    f0, f1 = round(R["diag"]["t0"] * FPS), round(R["diag"]["t1"] * FPS)
    sel = [f for f in frames if f0 <= f < f1]
    gt_dist, gt_moving = 0.0, 0
    for b in truth[sel[0]]:
        for a, c in zip(sel, sel[1:]):
            gt_dist += math.dist(truth[a][b][:2], truth[c][b][:2]) / PX_PER_MM
        gt_moving += sum(truth[f][b][2] for f in sel)
    gt_frac = gt_moving / (len(sel) * n)
    contacts = sum(1 for f in sel for i_, a in enumerate(truth[f].values()) for b in list(truth[f].values())[i_ + 1:] if math.dist(a[:2], b[:2]) < 12 * PX_PER_MM) / FPS
    e = np.array(errs)
    P = metrics["pooled"] if n > 1 else metrics["bees"][0]
    return {
        "samples": len(T), "expected": R["diag"]["expected"], "seconds": R["diag"]["seconds"],
        "found_frac": found / (len(T) * n),
        "err_mm_median": float(np.median(e)), "err_mm_p95": float(np.percentile(e, 95)), "err_mm_max": float(e.max()),
        "id_switches": swaps, "id_switches_not_near_merge": lone, "bee_contact_s": round(contacts, 1),
        "merges": sum(b["merges"] for b in metrics["bees"]), "reacquired": sum(b["reacq"] for b in metrics["bees"]),
        "distance_mm": P["distance"], "gt_distance_mm": gt_dist, "distance_err_pct": 100 * (P["distance"] - gt_dist) / gt_dist,
        "moving_frac": P["fracMoving"], "gt_moving_frac": gt_frac,
    }


def main():
    synth = Path(sys.argv[1])
    method = sys.argv[2] if len(sys.argv) > 2 else "play"
    names = sys.argv[3:] or ["one_bee", "three_bees", "three_bees_flicker"]
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("pageerror", lambda e: print("PAGE ERROR:", e))
        for name in names:
            truth = load_truth(synth / f"{name}_truth.csv")
            n = len(truth[0])
            fa = apart_frame(truth, n)
            state, metrics, det_msg, run_msg = run(page, synth / f"{name}.mp4", n, method, fa / FPS)
            res = evaluate(state, metrics, truth, n)
            res["detect_msg"], res["run_msg"] = det_msg, run_msg
            out[name] = res
            print(name, json.dumps(res, indent=1))
        browser.close()
    (ROOT / "tests" / f"e2e_results_{method}.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
