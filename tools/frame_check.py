#!/usr/bin/env python3
"""Does the drawing end in a straight line where the source picture was cut off?

No vision model: the plan is rendered, the painted/inked area is found, and for every
side of the source image the profile of where the drawing stops is measured. A cut
copied from the source shows up as many columns (rows) that stop at the same pixel.

    python tools/frame_check.py source.png plan.json [plan2.json ...]

Output per side: `flat` = share of the drawing's extent along that side that stops
within 2 px of one position (0 = natural, > 0.4 = a straight edge), and `stop` = how
far from the picture's edge the drawing stops, as a share of the picture size.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "tools"))

from dg_artist import build_parser, load_image, load_plan, make_analysis  # noqa: E402
from perceptual import crop_to_image, render_plan  # noqa: E402


def drawn_mask(crop):
    return crop.min(axis=2) < 235


def side_profile(mask, side):
    """Per position along the side: distance (px) from the edge to the first drawn pixel."""
    m = mask
    if side in ("left", "right"):
        m = mask.T
    if side in ("bottom", "right"):
        m = m[::-1]
    # m: rows run from the edge inwards
    has = m.any(axis=0)
    first = np.where(has, np.argmax(m, axis=0), -1)
    return first, has


def flatness(first, has):
    values = first[has]
    if len(values) < 10:
        return 0.0
    counts = np.bincount(values)
    window = np.convolve(counts, np.ones(5), mode="same")      # within 2 px of one position
    return float(window.max()) / len(values)


def check(image_path, plan_path, aspect=16 / 9, max_dim=900):
    image = load_image(image_path, max_dim)
    analysis = make_analysis(image, "anime_study",
                             build_parser().parse_args(["unused", "--style", "anime_study"]))
    from anime_study import subject_mask
    subject = subject_mask(analysis) > 0
    ops = load_plan(plan_path)
    with tempfile.TemporaryDirectory() as folder:
        render = render_plan(ops, aspect, Path(folder) / "r.png")
    crop, _ = crop_to_image(render, analysis, aspect)
    mask = drawn_mask(crop)
    h, w = mask.shape
    result = {}
    for side in ("top", "bottom", "left", "right"):
        src_first, src_has = side_profile(
            np.array(__import__("cv2").resize(subject.astype(np.uint8), (w, h),
                                              interpolation=__import__("cv2").INTER_NEAREST), bool), side)
        touching = src_has & (src_first <= 2)                 # where the source touches this edge
        if touching.mean() < 0.03:
            continue                                          # that side is not cut
        first, has = side_profile(mask, side)
        sel = has & touching
        size = h if side in ("top", "bottom") else w
        result[side] = {"cut_share": round(float(touching.mean()), 2),
                        "flat": round(flatness(first, sel), 2),
                        "stop": round(float(np.median(first[sel])) / size, 3) if sel.any() else None}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("image")
    parser.add_argument("plans", nargs="+")
    parser.add_argument("--canvas-aspect", type=float, default=16 / 9)
    args = parser.parse_args(argv)
    worst = 0.0
    for plan in args.plans:
        result = check(args.image, plan, args.canvas_aspect)
        print(Path(plan).name)
        if not result:
            print("   the picture is not cut off by its frame")
        for side, data in result.items():
            print(f"   {side:6s} cut over {data['cut_share']:.0%} of the side  flat {data['flat']:.2f}  stop {data['stop']}")
            worst = max(worst, data["flat"])
    return 1 if worst > 0.4 else 0


if __name__ == "__main__":
    raise SystemExit(main())
