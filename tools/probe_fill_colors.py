#!/usr/bin/env python3
"""Check that paint-bucket fills keep their colour when the active colour changes later.

Draws four outlined squares with the virtual pen (when the game has pen pressure on),
pours each in a different colour, changes the active colour in between, then reads the
canvas back. Needs the plugin build that leaves the fill's hover state (OnPointerExit)
and parks the virtual pen off the canvas.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from dg_bridge import BridgeClient  # noqa: E402


def main():
    client = BridgeClient()
    client.clear()
    colors = ["#34333D", "#FAE7D9", "#6E6076", "#F2CE95"]
    for index, color in enumerate(colors):
        x = 0.25 + 0.15 * index
        square = [[x - .06, .44], [x + .06, .44], [x + .06, .56], [x - .06, .56], [x - .06, .44]]
        client.color("#000000")
        try:
            client.pen_stroke([[i * 150, p[0], p[1], 0.2] for i, p in enumerate(square)])
        except Exception:
            client.draw_strokes([{"type": "line", "points": square, "color": "#000000", "size": 5, "layer": 0}])
        client.fill([[x, 0.5]], color=color)
        client.color("#FF00FF")             # a later colour change must not touch the fills
    fills = [line["color"] for line in client.lines(100000)["lines"] if line["type"] == 3]
    client.clear()
    print("poured:", colors)
    print("canvas:", fills)
    ok = [c.upper() for c in colors] == [c.upper() for c in fills]
    print("FILL COLOURS OK" if ok else "FILL COLOURS CHANGED AFTER THE POUR")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
