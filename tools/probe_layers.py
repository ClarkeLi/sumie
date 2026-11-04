#!/usr/bin/env python3
"""Check the plugin's virtual layers in the running game (Sketchbook or a round).

For every pair of neighbouring virtual layers (v, v+1) a thick bar of layer v+1 is
drawn first and a crossing bar of layer v after it. The crossing must show the colour
of v+1: a higher layer lies above a lower one whatever the order of drawing. It runs
once with direct strokes and once through the virtual pen. Reads the result from a
screenshot of the game window.
"""
from __future__ import annotations

import colorsys
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))

from dg_bridge import BridgeClient  # noqa: E402
import layers  # noqa: E402

SIZE = 35
HALF = 0.055


def palette(count):
    out = []
    for i in range(count):
        r, g, b = colorsys.hsv_to_rgb((i * 0.381966) % 1.0, 0.85, 0.95)
        out.append((int(r * 255), int(g * 255), int(b * 255)))
    return out


def hex_of(rgb):
    return "#%02X%02X%02X" % rgb


def bar(client, native, points, color, vlayer):
    if native:
        n = 14
        samples = []
        for i in range(n):
            f = i / (n - 1)
            samples.append([i * 40, points[0][0] + (points[1][0] - points[0][0]) * f,
                            points[0][1] + (points[1][1] - points[0][1]) * f, 1.0])
        client.color(color)
        result = client.pen_stroke(samples, vlayer=vlayer)
        if int(result.get("created", 0)) != 1:
            raise RuntimeError(f"pen stroke created {result.get('created')} elements: {result}")
    else:
        client.draw_strokes([{"type": "line", "points": points, "color": color, "size": SIZE,
                              "vlayer": vlayer}])


def grab(client, path):
    path.unlink(missing_ok=True)
    client.screenshot(path)
    for _ in range(40):
        if path.exists() and path.stat().st_size > 0:
            time.sleep(0.2)
            return np.asarray(Image.open(path).convert("RGB"))
        time.sleep(0.1)
    raise RuntimeError("no screenshot")


def run(client, native, total, tmp):
    client.clear()
    time.sleep(0.5)
    cols = 4
    colours = palette(2 * (total - 1))
    centres = []
    for k in range(total - 1):
        cx = 0.14 + 0.24 * (k % cols)
        cy = 0.80 - 0.30 * (k // cols)
        centres.append((cx, cy))
        up, low = colours[2 * k], colours[2 * k + 1]
        bar(client, native, [[cx - HALF * 0.56, cy], [cx + HALF * 0.56, cy]], hex_of(up), k + 1)
        bar(client, native, [[cx, cy - HALF], [cx, cy + HALF]], hex_of(low), k)
    time.sleep(0.6)
    image = grab(client, tmp)
    failures = []
    for k in range(total - 1):
        up = np.array(colours[2 * k])
        low = np.array(colours[2 * k + 1])
        m_up = (np.abs(image.astype(int) - up).sum(axis=2) < 6)
        m_low = (np.abs(image.astype(int) - low).sum(axis=2) < 6)
        if not m_up.any() or not m_low.any():
            failures.append((k, "bar missing in the screenshot"))
            continue
        # the crossing = overlap of both bars' bounding boxes
        yu, xu = np.nonzero(m_up)
        yl, xl = np.nonzero(m_low)
        x0, x1 = max(xu.min(), xl.min()), min(xu.max(), xl.max())
        y0, y1 = max(yu.min(), yl.min()), min(yu.max(), yl.max())
        cell_up = int(m_up[y0:y1 + 1, x0:x1 + 1].sum())
        cell_low = int(m_low[y0:y1 + 1, x0:x1 + 1].sum())
        area = max(1, (x1 - x0 + 1) * (y1 - y0 + 1))
        good = cell_up > 0.6 * area and cell_low < 0.2 * area
        if not good:
            failures.append((k, f"crossing shows upper {cell_up / area:.2f} / lower {cell_low / area:.2f}"))
    return failures


def main():
    client = BridgeClient()
    info = layers.capabilities(client)
    if not info:
        print("the plugin reports no virtual layers (old build, or no game)")
        return 2
    print("plugin:", info)
    total = int(info["virtual"])
    tmp = Path(tempfile.gettempdir()) / "probe_layers.png"
    exit_code = 0
    for native in (False, True):
        if native and not info.get("nativePatch"):
            print("native pen patch is not active; skipping the pen run")
            continue
        label = "pen" if native else "direct"
        failures = run(client, native, total, tmp)
        if failures:
            exit_code = 1
            print(f"{label}: FAILED", failures)
        else:
            print(f"{label}: {total - 1} crossings ordered correctly")
    lines = client.lines(1000).get("lines", [])
    print("sample of what the game stored (layer, sortingOrder):",
          [(l.get("layer"), l.get("sortingOrder")) for l in lines[:6]])
    client.clear()
    print("LAYERS OK" if exit_code == 0 else "LAYERS BROKEN")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
