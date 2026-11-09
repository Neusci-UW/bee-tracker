#!/usr/bin/env python3
"""Make synthetic arena videos of walking "bees" with known positions, for testing the tracker.

Each scenario writes <name>.mp4 and <name>_truth.csv (frame, t_s, bee, x_px, y_px, moving).
Bees walk in bouts (10-30 mm/s) separated by pauses, turn gradually, and slide along the wall.

  python3 tools/make_synthetic.py OUT_DIR
"""
import csv
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

W, H, FPS = 1280, 720, 30
ARENA_MM = 90.0                     # petri dish diameter
CX, CY, R_PX = 640, 360, 300        # arena on the image
PX_PER_MM = 2 * R_PX / ARENA_MM
BEE_L, BEE_W = 12.0, 4.5            # mm


def walk(rng, n_frames, start):
    """One bee's path in mm (arena-centred)."""
    x, y = start
    heading = rng.uniform(0, 2 * math.pi)
    moving, left = False, rng.uniform(0.5, 2)
    speed = 0.0
    out = []
    r_max = ARENA_MM / 2 - BEE_L / 2
    for _ in range(n_frames):
        dt = 1 / FPS
        left -= dt
        if left <= 0:
            moving = not moving
            left = rng.uniform(1.5, 5) if moving else rng.uniform(0.5, 3)
            speed = rng.uniform(10, 30) if moving else 0.0
        if moving:
            heading += rng.normal(0, 1.2) * math.sqrt(dt)
            nx, ny = x + speed * dt * math.cos(heading), y + speed * dt * math.sin(heading)
            r = math.hypot(nx, ny)
            if r > r_max:  # follow the wall
                nx, ny = nx * r_max / r, ny * r_max / r
                heading = math.atan2(ny, nx) + math.pi / 2 * (1 if rng.random() < 0.5 else -1)
            x, y = nx, ny
        out.append((x, y, heading, moving))
    return out


def render(paths, n_frames, out_mp4, truth_csv, flicker=0.0, noise=2.0, crf=23, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    # paper background with gentle vignette and texture, dish rim, dark surround
    base = 205 - 25 * (((xx - W / 2) / W) ** 2 + ((yy - H / 2) / H) ** 2) * 2
    base += rng.normal(0, 3, (H, W)).astype(np.float32)
    rr = np.hypot(xx - CX, yy - CY)
    base[rr > R_PX + 14] = 70
    rim = (rr > R_PX) & (rr <= R_PX + 14)
    base[rim] = 150
    proc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-r", str(FPS),
                             "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(crf), "-g", "60", str(out_mp4)], stdin=subprocess.PIPE)
    with open(truth_csv, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["frame", "t_s", "bee", "x_px", "y_px", "moving"])
        for f in range(n_frames):
            img = base.copy()
            for b, path in enumerate(paths):
                x, y, hd, mv = path[f]
                px, py = CX + x * PX_PER_MM, CY + y * PX_PER_MM
                a, bb = BEE_L / 2 * PX_PER_MM, BEE_W / 2 * PX_PER_MM
                c, s = math.cos(hd), math.sin(hd)
                x0, x1 = int(px - a - 2), int(px + a + 3)
                y0, y1 = int(py - a - 2), int(py + a + 3)
                sub_x, sub_y = xx[y0:y1, x0:x1] - px, yy[y0:y1, x0:x1] - py
                u, v = sub_x * c + sub_y * s, -sub_x * s + sub_y * c
                body = (u / a) ** 2 + (v / bb) ** 2 <= 1
                stripes = (np.cos(u / a * 9) > 0.3) & body & (u < 0)
                patch = img[y0:y1, x0:x1]
                patch[body] = 45
                patch[stripes] = 95       # abdomen bands
                wr.writerow([f, round(f / FPS, 4), b + 1, round(px, 2), round(py, 2), int(mv)])
            gain = 1 + flicker * math.sin(2 * math.pi * f / FPS * 0.7) + (flicker * rng.normal(0, 0.3) if flicker else 0)
            frame = img * gain + rng.normal(0, noise, (H, W))
            proc.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
    proc.stdin.close()
    proc.wait()


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "synthetic")
    out.mkdir(parents=True, exist_ok=True)
    scenarios = [
        ("one_bee", 1, 60, dict()),
        ("three_bees", 3, 60, dict()),
        ("three_bees_flicker", 3, 60, dict(flicker=0.08, noise=5.0, crf=30)),
    ]
    for i, (name, n, secs, kw) in enumerate(scenarios):
        rng = np.random.default_rng(100 + i)
        n_frames = secs * FPS
        starts = [(rng.uniform(-25, 25), rng.uniform(-25, 25)) for _ in range(n)]
        paths = [walk(rng, n_frames, s) for s in starts]
        render(paths, n_frames, out / f"{name}.mp4", out / f"{name}_truth.csv", seed=i, **kw)
        print("wrote", out / f"{name}.mp4")
    print(f"arena: centre ({CX},{CY}) px, radius {R_PX} px = {ARENA_MM} mm diameter; box x={CX-R_PX} y={CY-R_PX} w=h={2*R_PX}")


if __name__ == "__main__":
    main()
