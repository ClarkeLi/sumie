#!/usr/bin/env python3
"""Fit the hand-motion statistics used by the `anime_human` style from Quick, Draw! data.

    python tools/calibrate_motor.py DIR_WITH_NDJSON --out client/calibration/quickdraw_motor.json
    python tools/calibrate_motor.py --download cat face house --workdir /path/to/scratch

The raw Quick, Draw! files (https://github.com/googlecreativelab/quickdraw-dataset)
hold real doodles with a timestamp for every point. Only the summary statistics
are written to the repository; the downloaded ndjson stays in the work directory.
All lengths are relative to the longer side of the drawing's bounding box, so the
numbers transfer to any canvas size.
"""
from __future__ import annotations

import argparse
import json
import math
import urllib.request
from pathlib import Path

import numpy as np

URL = "https://storage.googleapis.com/quickdraw_dataset/full/raw/{}.ndjson"
PROFILE_BINS = 20


def download(names, workdir: Path, megabytes: int):
    workdir.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = workdir / f"{name}.ndjson"
        if target.exists():
            continue
        request = urllib.request.Request(
            URL.format(name), headers={"Range": f"bytes=0-{megabytes * 1_000_000}"})
        with urllib.request.urlopen(request, timeout=120) as response:
            target.write_bytes(response.read())
        print(f"downloaded {name}: {target.stat().st_size / 1e6:.1f} MB")


def read_drawings(path: Path, limit: int):
    count = 0
    with open(path, "rb") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                break                      # the range download cuts the last line
            if not item.get("recognized"):
                continue
            yield item["drawing"]
            count += 1
            if count >= limit:
                return


def lognormal_fit(values):
    logs = np.log(np.maximum(np.asarray(values, float), 1e-4))
    return {"mu": round(float(logs.mean()), 4), "sigma": round(float(logs.std()), 4)}


def analyse(files, per_file: int):
    durations, lengths, speeds = [], [], []
    pauses, pause_gaps = [], []
    strokes_per_drawing = []
    profiles = []
    curve_k, curve_v = [], []
    for path in files:
        for drawing in read_drawings(path, per_file):
            xs = np.concatenate([np.asarray(s[0], float) for s in drawing])
            ys = np.concatenate([np.asarray(s[1], float) for s in drawing])
            scale = max(np.ptp(xs), np.ptp(ys))
            if scale < 20 or len(drawing) > 40:
                continue
            strokes_per_drawing.append(len(drawing))
            prev_end, prev_xy = None, (0.0, 0.0)
            for stroke in drawing:
                x = np.asarray(stroke[0], float) / scale
                y = np.asarray(stroke[1], float) / scale
                t = np.asarray(stroke[2], float) / 1000.0
                if len(x) < 4:
                    continue
                step = np.hypot(np.diff(x), np.diff(y))
                length = float(step.sum())
                duration = float(t[-1] - t[0])
                if duration < 0.05 or length < 0.02:
                    continue
                durations.append(duration)
                lengths.append(length)
                speeds.append(length / duration)
                if prev_end is not None:
                    gap = float(t[0] - prev_end)
                    if 0.0 < gap < 30.0:
                        pauses.append(gap)
                        pause_gaps.append(math.hypot(x[0] - prev_xy[0], y[0] - prev_xy[1]))
                prev_end, prev_xy = float(t[-1]), (x[-1], y[-1])
                # speed profile on a normalised time axis
                dt = np.diff(t)
                valid = dt > 1e-4
                if valid.sum() >= 6:
                    v = np.where(valid, step / np.maximum(dt, 1e-4), 0.0)
                    tm = (t[:-1] + t[1:]) / 2.0
                    u = (tm - t[0]) / duration
                    grid = np.linspace(0.04, 0.96, PROFILE_BINS)
                    prof = np.interp(grid, u, v)
                    mean = prof.mean()
                    if mean > 0:
                        profiles.append(prof / mean)
                # curvature vs speed (turning angle per unit length)
                if len(x) >= 8:
                    ang = np.unwrap(np.arctan2(np.diff(y), np.diff(x)))
                    k = np.abs(np.diff(ang)) / np.maximum(step[1:], 1e-4)
                    v = step[1:] / np.maximum(dt[1:], 1e-3)
                    ok = (k > 0.5) & (k < 80) & (v > 0.0) & (dt[1:] > 1e-3)
                    curve_k.extend(k[ok].tolist())
                    curve_v.extend(v[ok].tolist())
    durations = np.asarray(durations)
    lengths = np.asarray(lengths)
    speeds = np.asarray(speeds)
    # duration = a * length^b (log-log regression), residual spread
    b, a_log = np.polyfit(np.log(lengths), np.log(durations), 1)
    residual = np.log(durations) - (a_log + b * np.log(lengths))
    curve_k = np.asarray(curve_k)
    curve_v = np.asarray(curve_v)
    beta = -1.0 / 3.0
    if len(curve_k) > 500:
        slope, _ = np.polyfit(np.log(curve_k), np.log(curve_v), 1)
        beta = float(np.clip(slope, -0.8, -0.05))
    profile = np.mean(profiles, axis=0)
    profile_sd = np.std(profiles, axis=0)
    pauses = np.asarray(pauses)
    return {
        "source": "Google Quick, Draw! raw ndjson (CC BY 4.0), recognized drawings only",
        "samples": {"strokes": int(len(durations)), "pauses": int(len(pauses)),
                    "drawings": int(len(strokes_per_drawing))},
        "length_unit": "fraction of the drawing's longer bounding-box side",
        "stroke_duration": {
            "a": round(float(math.exp(a_log)), 4), "b": round(float(b), 4),
            "sigma": round(float(residual.std()), 4),
            "median_s": round(float(np.median(durations)), 3)},
        "speed": lognormal_fit(speeds),
        "stroke_length": lognormal_fit(lengths),
        "pause": lognormal_fit(pauses),
        "pause_vs_travel": {
            "slope": round(float(np.polyfit(pause_gaps, pauses, 1)[0]), 3),
            "intercept": round(float(np.polyfit(pause_gaps, pauses, 1)[1]), 3)},
        "strokes_per_drawing": {
            "mean": round(float(np.mean(strokes_per_drawing)), 2),
            "median": float(np.median(strokes_per_drawing)),
            "p90": float(np.percentile(strokes_per_drawing, 90))},
        "power_law_beta": round(beta, 3),
        "speed_profile": {"mean": [round(float(v), 4) for v in profile],
                          "sd": [round(float(v), 4) for v in profile_sd]},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", nargs="?", help="directory with *.ndjson files")
    parser.add_argument("--download", nargs="+", metavar="CLASS",
                        help="fetch the first --megabytes of these classes first")
    parser.add_argument("--workdir", default="quickdraw-raw")
    parser.add_argument("--megabytes", type=int, default=25)
    parser.add_argument("--per-file", type=int, default=4000)
    parser.add_argument("--out", default="client/calibration/quickdraw_motor.json")
    args = parser.parse_args()
    folder = Path(args.source or args.workdir)
    if args.download:
        download(args.download, folder, args.megabytes)
    files = sorted(folder.glob("*.ndjson"))
    if not files:
        raise SystemExit(f"no *.ndjson files in {folder}")
    result = analyse(files, args.per_file)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}: {result['samples']}")
    print(f"  duration = {result['stroke_duration']['a']} * len^{result['stroke_duration']['b']}"
          f"  median {result['stroke_duration']['median_s']}s  beta {result['power_law_beta']}")


if __name__ == "__main__":
    main()
