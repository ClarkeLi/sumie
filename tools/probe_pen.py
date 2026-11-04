#!/usr/bin/env python3
"""Probe the game's virtual pen tablet (needs the plugin with /api/v1/stroke/pen).

Draws one test stroke in the Sketchbook through the game's own pen-pressure code
and compares the BrushSize the game stored with the one the pressure model predicts.

    python tools/probe_pen.py [--keep]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from dg_bridge import BridgeClient, BridgeError  # noqa: E402
from motor import FPS, game_brush_size  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="do not undo the test stroke")
    parser.add_argument("--url", default=None)
    args = parser.parse_args(argv)
    client = BridgeClient(args.url) if args.url else BridgeClient()

    try:
        status = client.pen_status()
    except BridgeError as error:
        print(f"pen endpoint unavailable ({error}). Restart the game so the new plugin loads.")
        return 2
    print("pen status:", status)
    if not status.get("usePenPressure"):
        print("Switch on 'Stiftdruckerkennung' in the game settings first.")
        return 2

    duration = 1.6
    t = np.linspace(0, duration, int(duration * FPS))
    pressure = 0.15 + 0.6 * np.sin(np.pi * t / duration)
    expected = game_brush_size(list(pressure))
    x = np.linspace(0.25, 0.75, 9)
    y = 0.5 + 0.08 * np.sin(np.linspace(0, 2 * np.pi, 9))
    samples = [[int(tt * 1000), float(xx), float(yy), float(np.interp(tt, t, pressure))]
               for tt, xx, yy in zip(np.linspace(0, duration, 9), x, y)]
    before = client.state().get("lines", 0)
    result = client.pen_stroke(samples)
    print("result:", result)
    after = client.state().get("lines", 0)
    ok = result.get("created") == 1 and after == before + 1
    print(f"elements: {before} -> {after}")
    fps = result.get("frames", 0) / duration
    print(f"game frame rate while drawing: about {fps:.0f} fps  (use --game-fps {fps:.0f})")
    expected = game_brush_size(list(np.interp(np.arange(0, duration, 1.0 / fps), t, pressure)))
    if "brushSize" in result:
        print(f"BrushSize stored by the game: {result['brushSize']}  model predicts: {expected}")
    if ok and not args.keep:
        client.undo()
        print("test stroke undone")
    print("PEN OK" if ok else "PEN DID NOT DRAW (the game ignored the virtual pen)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
