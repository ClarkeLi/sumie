#!/usr/bin/env python3
"""Numeric report for a `anime_human` plan: does it move and press like a person?

No vision model is involved. The plan's stroke durations, speeds and pauses are
compared with the Quick, Draw! statistics in client/calibration (pauses: 1-D earth
mover's distance in log space, 0 = identical distribution; stroke time: bias and
spread of log(duration) around the measured duration-length law), the pen-pressure
widths are summarised, and the bucket model checks every pour for leaks.

    python tools/human_eval.py plan.json [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from motor import load_calibration  # noqa: E402
from dg_artist import Op, load_plan  # noqa: E402
from anime_human import simulate  # noqa: E402

REQUEST_ALLOWANCE = 0.06


def emd_log(sample, mu, sigma, seed=1, n=4000):
    """Earth mover's distance between log(sample) and a log-normal reference."""
    sample = np.log(np.maximum(np.asarray(sample, float), 1e-3))
    reference = np.random.default_rng(seed).normal(mu, sigma, n)
    q = np.linspace(0.02, 0.98, 49)
    return float(np.mean(np.abs(np.quantile(sample, q) - np.quantile(reference, q))))


def evaluate(ops, aspect=16 / 9, world_width=None):
    cal = load_calibration()
    world_width = world_width or aspect * 10.0
    lines = [o for o in ops if o.kind == "line" and len(o.points) >= 2 and o.dur]
    buckets = [o for o in ops if o.kind == "bucket"]
    allpts = np.vstack([np.asarray(o.points) for o in lines]) if lines else np.zeros((1, 2))
    span = max(np.ptp(allpts[:, 0]) * world_width, np.ptp(allpts[:, 1]) * world_width / aspect, 1e-6)
    lengths = np.array([np.linalg.norm(np.diff(np.asarray(o.points) * [world_width, world_width / aspect],
                                               axis=0), axis=1).sum() for o in lines]) / span
    durations = np.array([o.dur for o in lines])
    pauses = np.array([o.pause for o in ops if o.pause])
    sizes = np.array([o.size for o in lines])
    d = cal["stroke_duration"]
    residual = np.log(durations) - (np.log(d["a"]) + d["b"] * np.log(np.maximum(lengths, 0.01)))
    total = float(sum((o.dur or 0) + (o.pause or 0) + REQUEST_ALLOWANCE for o in ops))
    _, report = simulate(ops, aspect, world_width, width=640)
    pours = [r for r in report if r.get("filled")]
    ratios = [r["area"] / r["expect"] for r in pours if r.get("expect")]
    result = {
        "ops": len(ops),
        "kinds": {"line": len(lines), "bucket": len(buckets)},
        "colours": len({o.color for o in ops}),
        "brush_min": int(sizes.min()) if len(sizes) else 0,
        "brush_median": int(np.median(sizes)) if len(sizes) else 0,
        "brush_max": int(sizes.max()) if len(sizes) else 0,
        "brush_spread": round(float(sizes.std() / max(sizes.mean(), 1e-6)), 3) if len(sizes) else 0.0,
        "estimated_seconds": round(total, 2),
        "strokes_per_minute": round(len(lines) / max(total, 1e-6) * 60.0, 1),
        "emd_pause": round(emd_log(pauses, cal["pause"]["mu"], cal["pause"]["sigma"]), 3) if len(pauses) else None,
        "duration_bias": round(float(residual.mean()), 3),
        "duration_spread": round(float(residual.std()), 3),
        "fill_strokes": sum(1 for o in lines if o.note == "human-fill"),
        "virtual_layers": len({o.vlayer for o in ops if o.vlayer is not None}),
        "pours": len(pours),
        "predicted_leaks": sum(1 for r in pours if r.get("leaked")),
        "pour_model_ratio": round(float(np.mean(ratios)), 3) if ratios else None,
    }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plan")
    parser.add_argument("--canvas-aspect", type=float, default=16 / 9)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    ops = load_plan(args.plan)
    report = evaluate(ops, args.canvas_aspect)
    if args.json:
        print(json.dumps(report))
    else:
        for key, value in report.items():
            print(f"{key:20s} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
