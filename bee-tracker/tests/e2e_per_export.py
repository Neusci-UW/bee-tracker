#!/usr/bin/env python3
"""Check that "Export to PER Scorer" produces a project PER Scorer opens without errors.

  python3 tests/e2e_per_export.py /tmp/synth /path/to/per-scorer
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_synthetic import FPS, apart_frame, load_truth, run  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def main():
    synth, per_dir = Path(sys.argv[1]), Path(sys.argv[2])
    truth = load_truth(synth / "three_bees_truth.csv")
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        run(page, synth / "three_bees.mp4", 3, "play", apart_frame(truth, 3) / FPS)
        # add a zone so zone bouts are exported too
        page.evaluate("""(() => { const s = window.__beeTracker.state, r = s.recs.find(x => x.id === s.current);
            r.cfg.zones.push({id: 'z1', name: 'Left half', shape: {type: 'rect', x: 340, y: 60, w: 300, h: 600}}); r.cfg.edgeMm = 10; })()""")
        proj = page.evaluate("window.__beeTracker.perScorerProject()")
        f = Path(tempfile.mkdtemp()) / "per_project.json"
        f.write_text(json.dumps(proj))
        page.goto((per_dir / "per-scorer.html").as_uri())
        page.evaluate("localStorage.clear()")
        page.reload()
        page.set_input_files("#jsonFile", str(f))
        page.wait_for_timeout(1500)
        n_bees = page.locator("#bees .bee").count()
        all_rows = page.locator("#allTbl tbody tr").count()
        behaviors = [b["name"] for b in proj["behaviors"]]
        events = sum(len(b["events"]) for b in proj["bees"])
        page.locator("#analysis").screenshot(path=str(Path(__file__).resolve().parent / "per_export_analysis.png"))
        browser.close()
    print(json.dumps({"bees_in_per_scorer": n_bees, "rows_in_all_bees_table": all_rows, "behaviors": behaviors, "events": events, "page_errors": errors}, indent=1))
    assert n_bees == 3 and not errors and events > 0


if __name__ == "__main__":
    main()
