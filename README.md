# Bee Tracker

Track free-moving bees in an arena from a video recording. You draw the arena, and the tracker finds each bee's centroid frame by frame. It reports distance walked, speed, time spent moving, time in zones you draw, time near the wall, and distance to the nearest other bee. Results export as CSV, or as a project that [PER Scorer](https://github.com/kyobisk/per-scorer) opens.

Like PER Scorer, it is a single self-contained page (`bee-tracker.html`). It runs in the browser, uploads nothing, and needs no install. It reuses PER Scorer's look, video loading, frame stepping, file saving and rank tests.

## Use

1. Open `bee-tracker.html` in Chrome or Edge, then open a video (MP4/H.264 is safest).
2. Press **Draw arena** and drag a box around the arena floor (round or rectangular). Enter the arena's real **width in mm** to set the scale.
3. Set **Bees in arena**.
4. Pause on a frame where every bee is visible and apart. Press **Test on this frame**: detected pixels show in magenta, with a cross on each bee. Move the **threshold** until each bee is one solid blob and nothing else lights up.
5. Press **Measure bee size**. This is needed to split bees that touch.
6. Optional: add zones (**Add zone**) and an **edge band** in mm (time near the wall, i.e. thigmotaxis). Zones can be changed after tracking, and the results update without re-tracking. To remove the arena, a zone or the light check, click its outline on the video, then **Remove** (or press Delete).
7. Press **Track bees**. Keep the tab visible while it runs, because browsers pause video in hidden tabs.
8. Export **Tracks CSV** (every sample), **Summary CSV** (one row per bee plus an "all" row per recording, across all recordings), or **Export to PER Scorer**.

Use **New recording** for each video. It copies the arena, zones and settings, which suits a fixed camera. Give each recording a **Group** to compare groups under **Compare recordings**.

## Desktop app (.exe / .dmg)

`BeeTracker.exe` (Windows) and `BeeTracker.dmg` (macOS) hold the page and the LED helper in one program, so nothing else needs installing. Start it and the page opens in a Chrome or Edge window, already connected to the helper. A small Bee Tracker window shows the DAQ and has **Open Bee Tracker**, **LEDs off** and **Quit**. Keep it open while you work. Quitting turns both LEDs off.

- **LEDs need Windows** with NI's NI-DAQmx driver (ni.com) and the USB-6003 plugged in before the app starts. Without them the LEDs run in simulated mode and everything else works. NI-DAQmx has no macOS driver, so on a Mac the LEDs are always simulated.
- **First start:** the apps aren't code-signed. On Windows, choose *More info → Run anyway*. On a Mac, drag Bee Tracker to Applications, open it, then allow it under *System Settings → Privacy & Security → Open Anyway*.
- Settings are kept per address. The app's page (`http://127.0.0.1:8765/`) doesn't see settings saved while using `bee-tracker.html` as a file, so save and reopen the project file to move between them.
- Get the builds from the **Desktop app** workflow on GitHub (Actions → Desktop app → Run workflow, or push a tag such as `v1.0` to attach them to a release). To build one yourself: `pip install pyinstaller numpy` (plus `nidaqmx` on Windows), then `python tools/package.py`. PyInstaller can't cross-build, so build the .exe on Windows and the .dmg on a Mac.

## LED protocols

With the camera, each trial can play an LED protocol on the two analog outputs (ao0, ao1) of an NI USB-6003. The page sends the protocol to the **LED helper**, a small program that has to be running on the same computer; the page can't start it. `bee-tracker.html` on its own has no helper: use the desktop app, or run `py helper\led_helper.py` and type the code it prints.

A protocol is a list of **segments**. Each has a length, an optional **condition label**, and a function for each LED:

| Function | Settings |
|---|---|
| Off, Constant | level (V) |
| Ramp | from, to (V), straight over the segment |
| Exponential | from, to (V), time constant τ (s) |
| Sine | frequency (Hz), midpoint and ± amplitude (V), phase (°) |
| Pulse train | rate (Hz), duty cycle (%), on and off levels (V) |
| Single pulse | delay and width (s), level (V) |
| Random levels | min and max (V), hold time (s), seed (the same seed gives the same levels) |
| Formula | any formula of `t` (s into the segment) and `d` (segment length), e.g. `2.5 + 2*sin(2*pi*t)` or `3*(t < 0.5)`; `sin cos tan exp log sqrt abs floor ceil round min max mod clamp`, `pi`, `e`, `^` and comparisons work |

The protocol can repeat, and the segment order can be shuffled within each repeat (the order is saved with the trial). The preview shows both outputs; a segment that goes outside the helper's 0–5 V, or whose formula doesn't work, is marked and can't be played. The helper builds the waveform at 1000 levels per second and plays it on the DAQ's own clock, so pulse trains are exact up to about 100 Hz.

Results, the **LED conditions CSV** and PER Scorer behaviors are grouped by condition: a segment's label, or what its LEDs do when it has none (e.g. `Blue 2 V`). The **LED log CSV** lists every segment each LED played, with its function. Protocols made before functions existed open as constant segments.

## Recording setup (matters more than any setting)

- **Fixed camera, looking straight down**, on a stand, not handheld. Turn off auto-exposure, auto-white-balance and autofocus if the camera allows it. The tracker compensates for overall brightness changes, but not for changes in one region of the frame.
- **Even, diffuse light** with no hard shadows. A shadow is a dark blob that moves with the bee.
- **Plain background that contrasts with the bees**, e.g. white paper or white acrylic under dark bees. Avoid glare off the lid.
- **Lid on the arena.** Bees on the walls or lid, or flying, give bad tracks.
- **Record a few seconds of the empty arena,** or make sure every bee moves. The background is the per-pixel median of frames across the analysis range, so a bee that sits still for most of the recording becomes part of the background and is missed.
- Note the arena's inner dimensions in mm.

## How it works

1. **Crop and downscale:** the arena's bounding box is cropped and scaled so its longer side is 480 px (adjustable).
2. **Background:** the per-pixel median of 25 frames spread over the analysis range.
3. **Foreground:** pixels darker (or lighter) than the background by at least the threshold. The overall brightness is matched first, to absorb flicker and exposure drift. A 3×3 opening removes specks.
4. **Blobs:** 8-connected components, keeping those at least 0.3× the measured bee size. A blob larger than 1.6× a bee is treated as touching bees: it is split by k-means on its pixels into as many parts as bees are predicted inside it, and flagged **merged**.
5. **Tracking:** each bee's position is predicted from its velocity. Bees are matched to blobs by minimum total distance (Hungarian algorithm), within a gate of one body length plus the max walking speed × time since the bee was last seen. A bee missing for over 1 s is matched to the nearest leftover blob and flagged **reacquired**.
6. **Metrics:** positions are smoothed (moving average, 0.2 s by default) within stretches of continuous detection. Gaps longer than 0.5 s split a track and are not counted.

Frames are read either by playing the video and taking frames as they are shown (`requestVideoFrameCallback`), which is fast but can skip a few frames, or by seeking to every sample, which is slower but never skips. Every sample stores the frame's actual timestamp, so variable-frame-rate phone video is timed correctly.

## Validation on synthetic videos

`tools/make_synthetic.py` renders 60 s videos of bee-shaped ellipses walking in bouts (10–30 mm/s) in a 90 mm dish, with the true positions saved. `tests/e2e_synthetic.py` drives the page in headless Chrome the way a person would: it opens the video, drags the arena, measures bee size, tracks, and compares the output with the truth. Results (15 samples/s, playing at 2×, defaults otherwise):

| Video | Samples (of 900) | Position error, median / 95th pct | Identity switches | Distance vs truth | Time moving vs truth |
|---|---|---|---|---|---|
| 1 bee | 873 | 0.11 / 0.13 mm | 0 | −6.2% | 60% vs 58% |
| 3 bees (9.6 s of bee contact) | 874 | 0.11 / 0.27 mm | 8, all during merges | −6.7% (all bees) | 69% vs 65% |
| 3 bees, flicker ±8%, noise, heavy compression (18 s of contact) | 875 | 0.12 / 0.93 mm | 20, all during merges | −8.0% (all bees) | 68% vs 65% |

What this does and doesn't show:

- **Synthetic videos are the easy case.** They have uniform shapes, a clean background, no shadows, no legs or wings, and no bees on the walls. The numbers are an upper bound on performance with real footage. Validate on real recordings by hand-scoring a few clips (for example, zone entries in PER Scorer) and comparing.
- **Identity is lost when bees touch.** Every identity switch happened within 1 s of a merge, and here the bees pass through each other, which is the worst case. With several unmarked bees, per-bee rows are not reliable; use the **All bees** row, which doesn't depend on identity. For per-bee data, mark bees with paint, or run one bee per arena.
- **Distance is about 6–8% low,** and it depends on the settings. Two-thirds of the shortfall comes from sampling at 15 Hz instead of 30 fps; the rest is smoothing. With smoothing off it matched the truth at the same sample times (704 vs 703 mm), but real footage has jitter that smoothing removes. Keep samples per second and smoothing the same across anything you compare. The summary CSV records both.
- **Playing skips about 3% of frames** in headless Chrome. Seeking gave 900 of 900 samples on the one-bee video and took 39 s for 60 s of video. Speed on long phone videos (long keyframe intervals, 4K) is untested.

## Export to PER Scorer

Each tracked bee becomes a PER Scorer bee. **Moving** and **In \<zone\>** become start/stop behaviors, with bouts shorter than 0.3 s dropped and gaps under 0.3 s merged. Open the file with **Open project…** in PER Scorer to use its time budget, plots and group tests. A few caveats:

- PER Scorer measures proportions from 0:00 to the end of tracking, so untracked time counts as "not doing" a behavior.
- The behaviors overlap (a bee can be moving and in a zone at once), so PER Scorer's transition matrix isn't meaningful for them.
- There is no PER here, so PER Scorer's PER columns stay empty.

## Develop

```
src/core.js         detection, tracking and metrics (pure functions, also run by Node tests)
src/page.html       the page; /*@CORE@*/ is replaced with core.js
tools/build.py      writes bee-tracker.html  ← edit src/, then run this
app/bee_tracker_app.py  the desktop app: LED helper + page server + launcher
tools/package.py        builds dist/BeeTracker.exe or dist/BeeTracker.dmg
tools/make_synthetic.py
tests/core.test.cjs                 node --test tests/core.test.cjs
tests/e2e_synthetic.py              python3 tests/e2e_synthetic.py /tmp/synth [play|seek]
tests/e2e_per_export.py             python3 tests/e2e_per_export.py /tmp/synth /path/to/per-scorer
```

The end-to-end tests need `pip install playwright scipy numpy`, Google Chrome, and ffmpeg (to make the videos).

## Known limits

- Detection is simple background subtraction. It fails with a moving camera, changing lighting in part of the frame, or bees the same brightness as the background.
- When bees touch, positions come from splitting one blob and are rough (up to about 3 mm off in the tests).
- Bees on the arena wall seen from above are foreshortened. The edge band counts them as near the wall, but distance along the wall is underestimated.
- Results and settings are kept in browser storage. Save the project file, since long multi-bee recordings may not fit in storage.
- Links to videos on other sites only work if the site allows cross-origin reading (CORS). Otherwise, download the file and open it.
