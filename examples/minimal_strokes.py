"""Minimal image -> strokes demo: Canny edges -> polylines -> JSON for POST /api/v1/strokes.

usage: python examples/minimal_strokes.py image.png > strokes.json
       python client/dg_bridge.py draw-json strokes.json   (needs the game running)
"""
import json, sys
import cv2

img = cv2.imread(sys.argv[1], cv2.IMREAD_GRAYSCALE)
h, w = img.shape
scale = 480 / max(h, w)
img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
h, w = img.shape
edges = cv2.Canny(cv2.GaussianBlur(img, (3, 3), 0), 60, 140)
contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)

CANVAS_ASPECT = 16 / 9                  # the game canvas is 17.78 x 10 world units
fit_h = 0.92
fit_w = fit_h * (w / h) / CANVAS_ASPECT
x0, y0 = (1 - fit_w) / 2, (1 - fit_h) / 2

strokes = []
for c in contours:
    if len(c) < 25:                     # drop specks
        continue
    c = cv2.approxPolyDP(c, 1.2, False)[:, 0, :]
    pts = [[round(x0 + px / w * fit_w, 4), round(y0 + (1 - py / h) * fit_h, 4)] for px, py in c]
    strokes.append({"type": "line", "color": "#202020", "size": 4, "points": pts})

print(f"{len(strokes)} strokes", file=sys.stderr)
json.dump({"clear": True, "strokes": strokes}, sys.stdout)
