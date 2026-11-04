#!/usr/bin/env python3
"""Numeric quality report for a study plan or the live canvas.

No vision model is involved: strokes are rasterised with the same
approximation as the preview, then compared against the source image.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from dg_artist import (Budget, Op, build_parser, load_image, make_analysis,
                       make_region_transform, make_transform, render_preview)
from anime_study import (plan_seconds, subject_mask)
from studio_common import parse_region


def load_ops(path: Path) -> list[Op]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "result" in data:
        lines = data["result"].get("lines", [])
        return [Op({0: "line", 1: "disc", 3: "fill"}.get(line["type"], "line"),
                   [list(p) for p in line["points"]], line["color"],
                   line["size"], 0, "canvas") for line in lines]
    return [Op(item["kind"], [list(p) for p in item["points"]], item["color"],
               item["size"], int(item.get("layer", 0)), item.get("phase", ""))
            for item in data]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="source image")
    parser.add_argument("ops", help="plan JSON or /api/v1/lines response")
    parser.add_argument("--render", default=None, help="write the rasterised PNG")
    parser.add_argument("--canvas-aspect", type=float, default=16 / 9)
    parser.add_argument("--region", default=None,
                        help="normalized canvas rect x,y,w,h used to place the image")
    parser.add_argument("--margin", type=float, default=0.04,
                        help="image margin used by the plan")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    region = None
    if args.region:
        try:
            region = parse_region(args.region)
        except ValueError as exc:
            parser.error(str(exc))

    ops = load_ops(Path(args.ops))
    if not ops:
        print("no operations", file=sys.stderr)
        return 1

    image = load_image(args.image, 900)
    study_args = build_parser().parse_args(["unused", "--style", "anime_study"])
    analysis = make_analysis(image, "anime_study", study_args)
    subject = subject_mask(analysis)
    if region:
        transform, _, _ = make_region_transform(
            analysis.width, analysis.height, args.canvas_aspect,
            cell=region, margin=args.margin)
    else:
        transform, _, _ = make_transform(analysis.width, analysis.height,
                                         args.canvas_aspect, args.margin)

    render_path = args.render or str(Path(args.ops).with_suffix(".render.png"))
    if any(op.kind == "bucket" for op in ops):       # bucket plans: replay the game's fill rules
        from anime_human import simulate
        picture, _ = simulate(ops, args.canvas_aspect, args.canvas_aspect * 10.0)
        Image.fromarray(picture).save(render_path)
    else:
        render_preview(ops, args.canvas_aspect, render_path)
    preview = np.array(Image.open(render_path).convert("RGB"))
    height, width = preview.shape[:2]
    fit_w = min(width - 36, (height - 36) * args.canvas_aspect)
    fit_h = fit_w / args.canvas_aspect
    origin_x, origin_y = (width - fit_w) / 2, (height - fit_h) / 2
    p00 = transform(0, 0)
    p10 = transform(analysis.width, 0)
    p0h = transform(0, analysis.height)
    left = origin_x + p00[0] * fit_w
    right = origin_x + p10[0] * fit_w
    top = origin_y + (1.0 - p00[1]) * fit_h
    bottom = origin_y + (1.0 - p0h[1]) * fit_h
    src_w = max(1, int(round(right - left)))
    src_h = max(1, int(round(bottom - top)))
    src = cv2.resize(analysis.image, (src_w, src_h), interpolation=cv2.INTER_AREA)
    mask = cv2.resize(subject, (src.shape[1], src.shape[0]),
                      interpolation=cv2.INTER_NEAREST) > 0
    roi = preview[int(round(top)):int(round(top)) + src.shape[0],
                  int(round(left)):int(round(left)) + src.shape[1]]
    if roi.shape[:2] != src.shape[:2]:
        roi = cv2.resize(roi, (src.shape[1], src.shape[0]))
    white = np.all(roi > 242, axis=2)
    painted = (~white) & mask
    error = np.abs(roi.astype(float) - src.astype(float)).mean(axis=2)

    chords = []
    thickness = []
    for op in ops:
        if op.kind != "line" or len(op.points) < 2:
            continue
        points = np.asarray(op.points, float)
        length = float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())
        chord = float(np.linalg.norm(points[-1] - points[0]))
        if chord > 1e-6:
            chords.append(length / chord)
        thickness.append(op.size)

    report = {
        "ops": len(ops),
        "kinds": {kind: sum(1 for op in ops if op.kind == kind)
                  for kind in ("line", "disc", "fill", "bucket")},
        "colours": len({op.color for op in ops}),
        "brush_min": min(thickness) if thickness else 0,
        "brush_median": int(np.median(thickness)) if thickness else 0,
        "brush_max": max(thickness) if thickness else 0,
        "arc_ratio_mean": round(float(np.mean(chords)), 3) if chords else None,
        "two_point_long": sum(1 for op in ops
                              if op.kind == "line" and len(op.points) < 3 and
                              np.linalg.norm(np.diff(np.asarray(op.points, float),
                                                     axis=0), axis=1).sum() > 0.02),
        "subject_coverage": round(float(painted[mask].mean()), 3) if mask.any() else 0.0,
        "colour_error_painted": round(float(error[painted].mean()), 2) if painted.any() else None,
        "bleed_outside_subject": round(float(((~white) & ~mask)[~mask].mean()), 4) if (~mask).any() else 0.0,
        "estimated_seconds": round(plan_seconds(ops, study_args), 2),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for key, value in report.items():
            print(f"{key:24s} {value}")
        print(f"{'render':24s} {render_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
