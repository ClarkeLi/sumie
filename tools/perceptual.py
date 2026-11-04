#!/usr/bin/env python3
"""Perceptual score of a plan: LPIPS and CLIP similarity between the rendered plan and the source.

No vision model reads a picture here; the networks produce numbers. Lower LPIPS is
closer, higher CLIP is closer. Compare two plans of the same image with the same
canvas, e.g. an `anime_study` plan and an `anime_human` plan.

    python tools/perceptual.py source.png plan_a.json [plan_b.json ...] [--canvas-aspect 1.7778]

Needs `pip install lpips open_clip_torch` (development only, never used while drawing).
Set TORCH_HOME / HF_HOME to keep the downloaded weights on the external volume.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from dg_artist import (build_parser, load_image, load_plan, make_analysis, make_transform,  # noqa: E402
                       render_preview, resolve_device)
from anime_study import subject_mask  # noqa: E402


def render_plan(ops, aspect, path):
    if any(op.kind == "bucket" for op in ops):
        from anime_human import simulate
        picture, _ = simulate(ops, aspect, aspect * 10.0)
        Image.fromarray(picture).save(path)
    else:
        render_preview(ops, aspect, str(path))
    return np.array(Image.open(path).convert("RGB"))


def crop_to_image(render, analysis, aspect, margin=0.04):
    """The part of the canvas the source image occupies, plus the source resized to it."""
    transform, _, _ = make_transform(analysis.width, analysis.height, aspect, margin)
    height, width = render.shape[:2]
    p00, p10 = transform(0, 0), transform(analysis.width, 0)
    p0h = transform(0, analysis.height)
    left, right = p00[0] * width, p10[0] * width
    top, bottom = (1 - p00[1]) * height, (1 - p0h[1]) * height
    x0, x1, y0, y1 = int(round(left)), int(round(right)), int(round(top)), int(round(bottom))
    crop = render[max(0, y0):y1, max(0, x0):x1]
    source = cv2.resize(analysis.image, (crop.shape[1], crop.shape[0]), interpolation=cv2.INTER_AREA)
    return crop, source


class Scorer:
    def __init__(self):
        import lpips
        import open_clip
        import torch
        self.torch = torch
        self.device = resolve_device("auto")
        self.lpips = lpips.LPIPS(net="alex", verbose=False).to(self.device).eval()
        model, _, preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai")
        self.clip = model.to(self.device).eval()
        self.preprocess = preprocess

    def lpips_distance(self, a, b, size=256):
        t = self.torch
        def prep(x):
            x = cv2.resize(x, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32) / 127.5 - 1
            return t.from_numpy(x).permute(2, 0, 1)[None].to(self.device)
        with t.no_grad():
            return float(self.lpips(prep(a), prep(b)).item())

    def clip_similarity(self, a, b):
        t = self.torch
        with t.no_grad():
            feats = []
            for x in (a, b):
                image = self.preprocess(Image.fromarray(x))[None].to(self.device)
                f = self.clip.encode_image(image)
                feats.append(f / f.norm(dim=-1, keepdim=True))
            return float((feats[0] * feats[1]).sum().item())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("image")
    parser.add_argument("plans", nargs="+")
    parser.add_argument("--canvas-aspect", type=float, default=16 / 9)
    parser.add_argument("--max-dim", type=int, default=900)
    args = parser.parse_args(argv)
    image = load_image(args.image, args.max_dim)
    study_args = build_parser().parse_args(["unused", "--style", "anime_study"])
    analysis = make_analysis(image, "anime_study", study_args)
    scorer = Scorer()
    for plan in args.plans:
        ops = load_plan(plan)
        with tempfile.TemporaryDirectory() as folder:
            render = render_plan(ops, args.canvas_aspect, Path(folder) / "r.png")
        crop, source = crop_to_image(render, analysis, args.canvas_aspect)
        print(f"{Path(plan).name:28s} LPIPS {scorer.lpips_distance(crop, source):.3f}  "
              f"CLIP {scorer.clip_similarity(crop, source):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
