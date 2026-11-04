#!/usr/bin/env python3
"""Parameter sweep for `anime_human`: plan with several settings, score each plan perceptually.

    python tools/sweep.py image.png --time 180 --shares 0.3,0.6 0.4,0.6 --skill 1.6 2.0

Each setting plans the image (dry run), then LPIPS / CLIP / coverage are printed.
Development tool; it needs lpips and open_clip_torch (see tools/perceptual.py).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "tools"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("image")
    parser.add_argument("--time", type=float, default=180)
    parser.add_argument("--shares", nargs="+", default=["0.24,0.80"], help="sketch,flats pairs")
    parser.add_argument("--skill", nargs="+", default=["1.6"])
    parser.add_argument("--style", default="anime_human")
    args = parser.parse_args(argv)

    from perceptual import Scorer, crop_to_image, render_plan
    from dg_artist import build_parser, load_image, load_plan, make_analysis
    image = load_image(args.image, 900)
    analysis = make_analysis(image, "anime_study", build_parser().parse_args(["unused", "--style", "anime_study"]))
    scorer = Scorer()
    with tempfile.TemporaryDirectory() as folder:
        for shares in args.shares:
            for skill in args.skill:
                plan = Path(folder) / "plan.json"
                env = dict(os.environ, HAND_SHARES=shares)
                subprocess.run([sys.executable, str(ROOT / "client" / "dg_artist.py"), args.image,
                                "--style", args.style, "--skill", skill, "--time", str(args.time),
                                "--dry-run", "--plan-out", str(plan)], env=env, capture_output=True)
                ops = load_plan(plan)
                crop, source = crop_to_image(render_plan(ops, 16 / 9, Path(folder) / "r.png"), analysis, 16 / 9)
                pours = sum(1 for o in ops if o.kind == "bucket")
                ink = sum(1 for o in ops if o.note in ("human-contour", "human-inner"))
                print(f"shares {shares:10s} skill {skill:4s} ops {len(ops):4d} ink {ink:3d} pours {pours:3d}  "
                      f"LPIPS {scorer.lpips_distance(crop, source):.3f}  CLIP {scorer.clip_similarity(crop, source):.3f}",
                      flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
