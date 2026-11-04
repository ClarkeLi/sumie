#!/usr/bin/env python3
"""Compare a stroke plan (or /api/v1/lines) with a real game screenshot.

No vision model is involved. The plan is rasterised with the same thickness
formula the game uses, aligned to the canvas rectangle inside the screenshot,
then compared pixel by pixel.

    python3 tools/compare_canvas.py SHOT.png PLAN.json --rect 0,95,1920,1080

The rectangle is ``x,y,width,height`` of the game canvas inside the
screenshot, in screenshot pixels. Use ``--exclude x,y,w,h`` (repeatable) for
overlapping game UI (toolbar, undo buttons, notifications).

Outputs IoU / recall / precision of the painted pixels, the mean colour error
where both images are painted, and the fraction of game-painted pixels the
plan did not predict (white gaps).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

DEFAULT_WORLD_WIDTH = 17.7778


def load_ops(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    source = "plan"
    if isinstance(data, dict):
        payload = data.get("result", data)
        if isinstance(payload, dict) and "lines" in payload:
            data = payload["lines"]
            source = "live"
    ops = []
    for item in data:
        if "kind" in item:
            ops.append({
                "kind": item["kind"],
                "points": item["points"],
                "color": item.get("color", "#000000"),
                "size": int(item.get("size", 4)),
                "note": item.get("note", ""),
                "fallback": item.get("fallback"),
            })
            continue
        kind = {0: "line", 1: "disc", 3: "fill"}.get(int(item.get("type", 0)), "line")
        ops.append({
            "kind": kind,
            "points": item.get("points", []),
            "color": item.get("color", "#000000"),
            "size": int(item.get("size", 4)),
            "note": "",
            "fallback": None,
        })
    return ops, source


def thickness(size: int, width: int, world_width: float) -> int:
    return max(1, int(round(size * width / (100.0 * world_width))))


def rasterize(ops, width: int, height: int, world_width: float, source: str):
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    def point(p):
        # Normalized game coordinates have y=0 at the bottom of the surface.
        return (float(p[0]) * width, (1.0 - float(p[1])) * height)

    for op in ops:
        if op["kind"] == "fill" and len(op["points"]) >= 3:
            draw.polygon([point(p) for p in op["points"]], fill=op["color"])
        elif op["kind"] == "realfill" and op["fallback"]:
            draw.polygon([point(p) for p in op["fallback"]], fill=op["color"])

    for op in ops:
        if op["kind"] == "line" and len(op["points"]) >= 2:
            width_px = thickness(op["size"], width, world_width)
            draw.line([point(p) for p in op["points"]], fill=op["color"],
                      width=width_px, joint="curve")
            # execute_study() adds a same-colour disc at both ends of thick
            # lines. Live /lines already contains those discs, so only mirror
            # the executor when we are looking at a plan JSON.
            if source == "plan" and op["size"] >= 8:
                radius = width_px / 2.0
                for cap in (op["points"][0], op["points"][-1]):
                    x, y = point(cap)
                    draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                                 fill=op["color"])
        elif op["kind"] == "disc" and op["points"]:
            width_px = thickness(op["size"], width, world_width)
            x, y = point(op["points"][0])
            radius = width_px / 2.0
            draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                         fill=op["color"])
    return np.asarray(image)


def parse_rect(text: str):
    parts = [int(round(float(v))) for v in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("expected x,y,width,height")
    return tuple(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("screenshot", help="PNG/JPEG captured while the game is frontmost")
    parser.add_argument("ops", help="plan JSON from --plan-out, or /api/v1/lines JSON")
    parser.add_argument("--rect", required=True, type=parse_rect,
                        help="canvas rectangle inside the screenshot: x,y,width,height")
    parser.add_argument("--exclude", action="append", default=[], type=parse_rect,
                        metavar="X,Y,W,H", help="ignore this rectangle (game UI); repeatable")
    parser.add_argument("--world-width", type=float, default=DEFAULT_WORLD_WIDTH,
                        help="canvas world width used by the game (state.canvas.width)")
    parser.add_argument("--threshold", type=int, default=240,
                        help="pixel is considered painted below this grey level")
    parser.add_argument("--render", default=None, help="write prediction | screenshot | diff")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    ops, source = load_ops(Path(args.ops))
    if not ops:
        print("no operations in " + args.ops, file=sys.stderr)
        return 1

    shot_full = np.asarray(Image.open(args.screenshot).convert("RGB"))
    x0, y0, width, height = args.rect
    if x0 < 0 or y0 < 0 or x0 + width > shot_full.shape[1] or y0 + height > shot_full.shape[0]:
        print("--rect is outside the screenshot", file=sys.stderr)
        return 2
    shot = shot_full[y0:y0 + height, x0:x0 + width]

    prediction = rasterize(ops, width, height, args.world_width, source)

    valid = np.ones((height, width), bool)
    for ex, ey, ew, eh in args.exclude:
        ex, ey = ex - x0, ey - y0
        valid[max(0, ey):max(0, ey + eh), max(0, ex):max(0, ex + ew)] = False

    predicted = (prediction.min(axis=2) < args.threshold) & valid
    actual = (shot.min(axis=2) < args.threshold) & valid

    intersection = int(np.count_nonzero(predicted & actual))
    union = int(np.count_nonzero(predicted | actual))
    actual_count = int(np.count_nonzero(actual))
    predicted_count = int(np.count_nonzero(predicted))
    both = predicted & actual
    colour_error = (float(np.abs(prediction[both].astype(float)
                                 - shot[both].astype(float)).mean())
                    if np.any(both) else None)

    report = {
        "ops": len(ops),
        "ops_source": source,
        "screenshot": str(args.screenshot),
        "rect": [x0, y0, width, height],
        "predicted_pixels": predicted_count,
        "game_painted_pixels": actual_count,
        "iou": round(intersection / union, 4) if union else None,
        "precision": round(intersection / predicted_count, 4) if predicted_count else None,
        "recall": round(intersection / actual_count, 4) if actual_count else None,
        "unpredicted_game_pixels": round(1.0 - intersection / actual_count, 4)
        if actual_count else None,
        "mean_colour_error_both": round(colour_error, 2) if colour_error is not None else None,
        "note": "recall<1 means the game painted pixels the plan did not predict "
                "(usually anti-aliasing, status UI, or a stale/partial drawing); "
                "precision<1 means predicted paint is missing in the game.",
    }

    if args.render:
        diff = np.full_like(shot, 255)
        diff[actual & ~predicted] = (255, 90, 90)     # game only
        diff[~actual & predicted] = (90, 120, 255)    # prediction only
        diff[both] = (90, 190, 120)                   # agreement
        side = np.concatenate([prediction, shot, diff], axis=1)
        Image.fromarray(side).save(args.render)
        report["render"] = str(args.render)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for key, value in report.items():
            print(f"{key:<28} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
