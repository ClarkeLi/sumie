#!/usr/bin/env python3
"""Perceptual + behavioural report for a study plan. Numbers only, no vision model.

Perceptual: compare the rasterised plan with the source after the blur a viewer
applies at normal distance (Lab dE, SSIM, edge F-score, silhouette IoU, bleed).
Behaviour: stroke count/length distribution, brush sizes, colour switches, and
how much of the drawing is ink vs paint, to judge "hand drawn" plausibility.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "tools"))

from dg_artist import (build_parser, load_image, make_analysis, make_transform,
                       render_preview)
from anime_study import subject_mask
from study_metrics import load_ops


def ssim(a, b):
    a = a.astype(np.float64); b = b.astype(np.float64)
    c1, c2 = 6.5025, 58.5225
    k = (7, 7)
    mu_a = cv2.blur(a, k); mu_b = cv2.blur(b, k)
    va = cv2.blur(a * a, k) - mu_a ** 2
    vb = cv2.blur(b * b, k) - mu_b ** 2
    cov = cv2.blur(a * b, k) - mu_a * mu_b
    s = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (va + vb + c2))
    return s


def evaluate(image_path, ops_path, canvas_aspect=16 / 9, margin=0.04, render=None):
    ops = load_ops(Path(ops_path))
    notes = [d.get('note', '') for d in json.loads(Path(ops_path).read_text(encoding='utf-8'))]
    for op, note in zip(ops, notes):
        op.note = note
    image = load_image(image_path, 900)
    args = build_parser().parse_args(["unused", "--style", "anime_study"])
    analysis = make_analysis(image, "anime_study", args)
    subject = subject_mask(analysis)
    transform, _, _ = make_transform(analysis.width, analysis.height, canvas_aspect, margin)
    out = render or str(Path(ops_path).with_suffix(".eval.png"))
    render_preview(ops, canvas_aspect, out)
    pv = np.array(Image.open(out).convert("RGB"))
    H, W = pv.shape[:2]
    fit_w = min(W - 36, (H - 36) * canvas_aspect)
    fit_h = fit_w / canvas_aspect
    ox, oy = (W - fit_w) / 2, (H - fit_h) / 2
    p00 = transform(0, 0); p10 = transform(analysis.width, 0); p0h = transform(0, analysis.height)
    left = ox + p00[0] * fit_w; right = ox + p10[0] * fit_w
    top = oy + (1 - p00[1]) * fit_h; bottom = oy + (1 - p0h[1]) * fit_h
    sw, sh = max(1, int(round(right - left))), max(1, int(round(bottom - top)))
    src = cv2.resize(analysis.image, (sw, sh), interpolation=cv2.INTER_AREA)
    msk = cv2.resize(subject, (sw, sh), interpolation=cv2.INTER_NEAREST) > 0
    t0, l0 = int(round(top)), int(round(left))
    roi = pv[t0:t0 + sh, l0:l0 + sw]
    if roi.shape[:2] != src.shape[:2]:
        roi = cv2.resize(roi, (sw, sh))

    sigma = max(2.0, min(sw, sh) / 60.0)
    blur = lambda x: cv2.GaussianBlur(x, (0, 0), sigma)
    lab = lambda x: cv2.cvtColor(blur(x).astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
    dE = np.linalg.norm(lab(roi) - lab(src), axis=2)
    gs = cv2.cvtColor(blur(src), cv2.COLOR_RGB2GRAY)
    gr = cv2.cvtColor(blur(roi), cv2.COLOR_RGB2GRAY)
    s = ssim(gs, gr)

    white = np.all(roi > 242, axis=2)
    painted = (~white)
    inter = (painted & msk).sum(); union = (painted | msk).sum()
    # edge agreement on the unblurred structure: dilate by 2% of the short side
    es = cv2.Canny(cv2.cvtColor(src, cv2.COLOR_RGB2GRAY), 60, 140)
    er = cv2.Canny(cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY), 40, 110)
    tol = max(2, int(min(sw, sh) * 0.02)) | 1
    k = np.ones((tol, tol), np.uint8)
    prec = float((cv2.bitwise_and(er, cv2.dilate(es, k)) > 0).sum()) / max(1, (er > 0).sum())
    rec = float((cv2.bitwise_and(es, cv2.dilate(er, k)) > 0).sum()) / max(1, (es > 0).sum())

    # behaviour
    lens, sizes, switches, ink, paint = [], [], 0, 0.0, 0.0
    prev = None
    for op in ops:
        if op.kind != "line" or len(op.points) < 2:
            continue
        pts = np.asarray(op.points, float)
        L = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
        lens.append(L); sizes.append(op.size)
        if op.color != prev:
            switches += 1; prev = op.color
        if op.note in ("study-color", "study-shade", "study-touch"):
            paint += L * op.size
        else:
            ink += L * op.size
    lens = np.array(lens) if lens else np.zeros(1)
    return {
        "ops": len(ops),
        "dE_blur_subject": round(float(dE[msk].mean()), 2),
        "dE_blur_all": round(float(dE.mean()), 2),
        "ssim_subject": round(float(s[msk].mean()), 3),
        "silhouette_iou": round(float(inter) / max(1, union), 3),
        "bleed_pct": round(float((painted & ~msk).sum()) / max(1, (~msk).sum()) * 100, 2),
        "hole_pct": round(float((white & msk).sum()) / max(1, msk.sum()) * 100, 2),
        "edge_precision": round(prec, 3),
        "edge_recall": round(rec, 3),
        "stroke_len_median": round(float(np.median(lens)), 3),
        "stroke_len_p90": round(float(np.percentile(lens, 90)), 3),
        "colour_switches": switches,
        "paint_to_ink": round(paint / max(1e-9, ink), 2),
        "render": out,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("image"); p.add_argument("ops")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    r = evaluate(a.image, a.ops)
    if a.json:
        print(json.dumps(r))
    else:
        for k, v in r.items():
            print(f"{k:20s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
