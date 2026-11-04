#!/usr/bin/env python3
"""Compare what the game really shows with what the plan's simulation predicted.

    python tools/compare_actual.py plan.json screenshot.png [--canvas-aspect 1.7778] [--save-dir DIR]

The simulation (`anime_human.simulate`) is aligned to the screenshot by the bounding
box of the dark line work, then both are compared as masks:

  hollow   share of the simulated paint that is white in the game (the bucket or a
           stroke did less than planned);
  extra    share of the game's paint where the simulation has none;
  ink      ratio of dark-pixel area in the game to the simulation (line weight);
  thick    median line thickness, game vs simulation, in screenshot pixels.

No vision model; the aligned pictures can be saved for the user to look at.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from dg_artist import load_plan  # noqa: E402
from anime_human import simulate  # noqa: E402


def ink_mask(image):
    return image.max(axis=2) < 110


def bbox(mask, low=0.4, high=99.6):
    ys, xs = np.nonzero(mask)
    return (np.percentile(xs, low), np.percentile(xs, high),
            np.percentile(ys, low), np.percentile(ys, high))


def thickness(mask):
    """Median stroke thickness: twice the distance to the background along the skeleton."""
    dist = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 3)
    thin = cv2.ximgproc.thinning(mask.astype(np.uint8) * 255) > 0 if hasattr(cv2, "ximgproc") else \
        (dist >= cv2.dilate(dist, np.ones((3, 3), np.uint8)))
    values = dist[thin & mask]
    return float(2.0 * np.median(values)) if len(values) else 0.0


def compare(plan_path, shot_path, aspect=16 / 9, world_width=None, save_dir=None):
    world_width = world_width or aspect * 10.0
    ops = load_plan(plan_path)
    sim, _ = simulate(ops, aspect, world_width, width=1280)
    shot = cv2.cvtColor(cv2.imread(str(shot_path)), cv2.COLOR_BGR2RGB)
    h, w = shot.shape[:2]
    # the toolbar and buttons of the game are dark too: look only at the middle of the window
    region = np.zeros(shot.shape[:2], bool)
    region[int(0.09 * h):int(0.93 * h), int(0.12 * w):int(0.89 * w)] = True
    sim_ink = ink_mask(sim)
    shot_ink = ink_mask(shot) & region
    sx0, sx1, sy0, sy1 = bbox(sim_ink)
    gx0, gx1, gy0, gy1 = bbox(shot_ink)
    scale_x = (gx1 - gx0) / max(sx1 - sx0, 1)
    scale_y = (gy1 - gy0) / max(sy1 - sy0, 1)
    scale = (scale_x + scale_y) / 2.0
    # map the screenshot into the simulation's frame
    matrix = np.float32([[1 / scale, 0, sx0 - gx0 / scale], [0, 1 / scale, sy0 - gy0 / scale]])
    aligned = cv2.warpAffine(shot, matrix, (sim.shape[1], sim.shape[0]), flags=cv2.INTER_AREA,
                             borderValue=(255, 255, 255))
    valid = cv2.warpAffine(region.astype(np.uint8) * 255, matrix, (sim.shape[1], sim.shape[0])) > 0
    sim_paint = (sim.min(axis=2) < 240) & valid
    got_paint = (aligned.min(axis=2) < 240) & valid
    near = cv2.dilate(sim_paint.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0   # 2 px of tolerance
    hollow = sim_paint & ~(cv2.dilate(got_paint.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0)
    extra = got_paint & ~near
    got_ink = ink_mask(aligned) & valid
    result = {
        "scale_x": round(scale_x, 3), "scale_y": round(scale_y, 3),
        "hollow": round(float(hollow.sum()) / max(1, sim_paint.sum()), 3),
        "extra": round(float(extra.sum()) / max(1, got_paint.sum()), 3),
        "ink_ratio": round(float(got_ink.sum()) / max(1, (sim_ink & valid).sum()), 2),
        "thick_game": round(thickness(got_ink), 2),
        "thick_sim": round(thickness(sim_ink & valid), 2),
    }
    if save_dir:
        out = Path(save_dir)
        out.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out / "sim.png"), cv2.cvtColor(sim, cv2.COLOR_RGB2BGR))
        cv2.imwrite(str(out / "game_aligned.png"), cv2.cvtColor(aligned, cv2.COLOR_RGB2BGR))
        diff = np.full(sim.shape, 255, np.uint8)
        diff[sim_paint & got_paint] = (190, 190, 190)
        diff[hollow] = (255, 0, 0)          # planned but missing
        diff[extra] = (0, 0, 255)           # in the game but not planned
        cv2.imwrite(str(out / "diff.png"), cv2.cvtColor(diff, cv2.COLOR_RGB2BGR))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plan")
    parser.add_argument("screenshot")
    parser.add_argument("--canvas-aspect", type=float, default=16 / 9)
    parser.add_argument("--save-dir", default=None)
    args = parser.parse_args(argv)
    for key, value in compare(args.plan, args.screenshot, args.canvas_aspect,
                              save_dir=args.save_dir).items():
        print(f"{key:10s} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
