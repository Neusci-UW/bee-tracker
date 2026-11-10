#!/usr/bin/env python3
"""Bee Tracker + LED helper: live trials (fake camera) that play an LED protocol made in the page.

  python3 tests/e2e_led_helper.py /tmp/synth

The helper runs with --simulated (no DAQ). The protocol is set in the page's segment editor:
off 2 s, ao0 ("Blue") 2 V 2 s, off 2 s, ao1 ("Green") 5 Hz pulses at 2 V 2 s ("Green flicker"), off 2 s,
repeated twice (20 s).
Trial 1 ends with the protocol; trial 2 is cut short at 5 s and must turn the LEDs off.
"""
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_live_camera import make_y4m, page_to_screen  # noqa: E402
from e2e_synthetic import PAGE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PORT, CODE = 8765, "424242"


def helper_status():
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/status", headers={"X-Led-Code": CODE})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def main():
    synth = Path(sys.argv[1])
    tmp = Path(tempfile.mkdtemp())
    y4m = tmp / "cam.y4m"
    make_y4m(synth / "three_bees.mp4", y4m)
    srv = subprocess.Popen([sys.executable, str(ROOT / "helper" / "led_helper.py"), "--simulated", "--port", str(PORT), "--code", CODE],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    banner = srv.stdout.readline()
    assert banner.startswith("LED helper ready"), banner + srv.stdout.read()
    errors, out = [], {"helper_banner": banner.strip()}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True, args=[
                "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", f"--use-file-for-fake-video-capture={y4m}"])
            ctx = browser.new_context(viewport={"width": 1500, "height": 1100}, accept_downloads=True)
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(PAGE); page.evaluate("localStorage.clear()"); page.reload()
            # connect: wrong code first
            page.fill("#ag-code", "111111"); page.press("#ag-code", "Enter")
            page.click("#ag-connect"); page.wait_for_function("document.getElementById('ag-msg').textContent.length > 15")
            out["wrong_code_msg"] = page.inner_text("#ag-msg")
            page.fill("#ag-code", CODE); page.press("#ag-code", "Enter")
            page.click("#ag-connect"); page.wait_for_function("document.getElementById('ag-state').textContent === 'connected'", timeout=15000)
            out["connect_msg"] = page.inner_text("#ag-msg")
            # protocol through the segment editor
            seg = lambda i: page.locator("#ledSegs .ledseg").nth(i)
            page.fill("#led-n0", "Blue"); page.press("#led-n0", "Tab")
            page.fill("#led-n1", "Green"); page.press("#led-n1", "Tab")
            for i in range(5):
                inp = seg(i).locator("input[data-k=dur]")
                inp.fill("2"); inp.press("Tab")
            seg(3).locator("select[data-o=ao1]").select_option("pulses")
            for k, v in (("freq", "5"), ("on", "2")):
                inp = seg(3).locator(f"input[data-o=ao1][data-p={k}]"); inp.fill(v); inp.press("Tab")
            inp = seg(3).locator("input[data-k=label]"); inp.fill("Green flicker"); inp.press("Tab")
            page.fill("#led-rep", "2"); page.press("#led-rep", "Tab")
            out["protocol_summary"] = page.inner_text("#ledSum")
            bad = seg(1).locator("input[data-o=ao0][data-p=v]")
            bad.fill("7"); bad.press("Tab")
            out["limit_msg"] = page.inner_text("#ledSum")
            bad = seg(1).locator("input[data-o=ao0][data-p=v]")
            bad.fill("2"); bad.press("Tab")
            # Test LEDs button plays 2 x 1 s on the helper
            run_before = (helper_status().get("run") or {}).get("id", 0)
            page.click("#ledTest"); page.wait_for_timeout(500)
            st = helper_status(); out["test_button_played"] = st["run"]["id"] == run_before + 1 and st["running"]
            page.click("#ledOff"); page.wait_for_timeout(300)
            out["off_button_stopped"] = not helper_status()["running"]
            # camera, arena, background, bee size
            page.click("#bCamOn"); page.wait_for_function("window.__beeTracker.videoReady", timeout=30000)
            a, b = page_to_screen(page, [[170, 30], [470, 330]])
            page.click("#bArena"); page.mouse.move(*a); page.mouse.down(); page.mouse.move(*b, steps=5); page.mouse.up()
            page.fill("#a-w", "90"); page.press("#a-w", "Enter")
            page.fill("#r-n", "3"); page.press("#r-n", "Enter")
            page.click("#bBgLearn"); page.wait_for_timeout(500); page.wait_for_function("!window.__beeTracker.busy", timeout=60000)
            for _ in range(40):
                page.click("#bMeasure"); page.wait_for_timeout(250); page.wait_for_function("!window.__beeTracker.busy")
                if "median of 3 blob" in page.inner_text("#detMsg"):
                    break
            assert page.is_checked("#ag-run") and page.is_checked("#ag-end")
            # trial 1: no fixed length, ends with the protocol
            page.fill("#l-dur", ""); page.press("#l-dur", "Enter")
            t0 = time.time()
            page.click("#bRun"); page.wait_for_timeout(3500)
            out["during"] = page.inner_text("#runMsg")
            out["helper_running_during"] = helper_status()["running"]
            page.wait_for_function("!window.__beeTracker.busy", timeout=90000, polling=500)
            out["trial1_wall_s"] = round(time.time() - t0, 1)
            page.wait_for_timeout(800)
            out["trial1_msg"] = page.inner_text("#runMsg")
            state = page.evaluate("JSON.parse(JSON.stringify(window.__beeTracker.state))")
            r1 = [r for r in state["recs"] if r["result"]][-1]
            out["trial1_s"] = r1["result"]["diag"]["t1"]
            out["trial1_events"] = [(e["output"], e["peak"], e["t"], e["dur"]) for e in r1["result"]["stim"]["events"]]
            out["trial1_started_at"] = r1["result"]["stim"]["startedAt"]
            out["offset_ms"], out["rtt_ms"] = r1["result"]["stim"]["offsetMs"], r1["result"]["stim"]["rttMs"]
            out["led_table"] = page.inner_text("#ledRes")
            out["led_csv"] = page.evaluate("window.__beeTracker.ledCSV()").splitlines()[:3]
            out["led_log"] = page.evaluate("window.__beeTracker.stimCSV()").splitlines()[:3]
            out["tracks_has_led"] = "led" in page.evaluate("window.__beeTracker.tracksCSV()").splitlines()[0].split(",")
            out["per_behaviors"] = [(b["name"], b["role"]) for b in page.evaluate("window.__beeTracker.perScorerProject()")["behaviors"]]
            out["helper_running_after_1"] = helper_status()["running"]
            page.locator("#resPanel").screenshot(path=str(tmp / "results.png"))
            page.screenshot(path=str(tmp / "page.png"), full_page=True)
            # trial 2: cut short at 5 s
            page.fill("#l-dur", "5"); page.press("#l-dur", "Enter")
            page.click("#bRun"); page.wait_for_timeout(1000)
            page.wait_for_function("!window.__beeTracker.busy", timeout=60000, polling=500)
            page.wait_for_timeout(500)
            out["trial2_msg"] = page.inner_text("#runMsg")
            state = page.evaluate("JSON.parse(JSON.stringify(window.__beeTracker.state))")
            r2 = [r for r in state["recs"] if r["result"]][-1]
            out["trial2_events"] = [(e["output"], e["peak"], e["t"], e["dur"]) for e in r2["result"]["stim"]["events"]]
            out["helper_running_after_2"] = helper_status()["running"]
            out["recordings"] = [r["label"] for r in state["recs"]]
            out["screenshots"] = [str(tmp / "results.png"), str(tmp / "page.png")]
            browser.close()
    finally:
        srv.terminate()
        srv.wait(5)
    out["page_errors"] = errors
    print(json.dumps(out, indent=1))
    ev = out["trial1_events"]
    s0 = out["trial1_started_at"]
    assert [(o, v) for o, v, _, _ in ev] == [("Blue", 2), ("Green", 2), ("Blue", 2), ("Green", 2)], ev
    assert [round(t - s0, 3) for _, _, t, _ in ev] == [2, 6, 12, 16] and all(d == 2 for *_, d in ev)
    assert 0 <= s0 < 1.0
    assert abs(out["trial1_s"] - (s0 + 20)) < 0.25, out["trial1_s"]
    assert out["test_button_played"] and out["off_button_stopped"] and out["helper_running_during"]
    assert not out["helper_running_after_1"] and not out["helper_running_after_2"]
    assert len(out["trial2_events"]) == 1 and out["trial2_events"][0][0] == "Blue"
    assert "Wrong code" in out["wrong_code_msg"] and "outside" in out["limit_msg"]
    assert out["tracks_has_led"] and not errors
    assert any(line.startswith("Green flicker") for line in out["led_table"].splitlines()), out["led_table"]


if __name__ == "__main__":
    main()
