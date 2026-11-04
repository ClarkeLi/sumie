#!/usr/bin/env python3
"""Time-budgeted painter-style renderer for Draw & Guess Bridge.

Five styles:
  anime_study  - sparse subject study, staged and individually paced strokes
  sketch       - loose line work + hatching, few color accents
  anime_flat   - layered source-color shapes + single centerline ink pass
  impasto      - layered curved brush strokes
  watercolor   - light irregular washes + hatching + broken lines

The renderer talks to the BepInEx bridge over HTTP. It never uses the mouse.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Must be set before numpy/cv2/torch load their thread pools.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "2")

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# Planning is mostly serial; wide cv2/BLAS pools only spin cores (fan noise).
cv2.setNumThreads(2)

try:
    from dg_bridge import BridgeClient, BridgeError
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dg_bridge import BridgeClient, BridgeError

try:
    from studio_common import parse_region
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from studio_common import parse_region

from artist_geometry import trace_centerlines
import imagesize
import pacing
import planlog


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

@dataclass
class Op:
    kind: str                       # line | fill | realfill | disc | bucket
    points: list[list[float]]       # normalized 0..1
    color: str = "#000000"
    size: int = 4
    layer: int = 0
    phase: str = ""
    cost: float = 1.0
    fallback: list[list[float]] | None = None
    note: str = ""
    group: int | None = None  # atomic study boundary segments + closed fill
    dur: float | None = None    # anime_human style: seconds the pen is down
    pause: float | None = None  # anime_human style: seconds between the previous stroke and this one
    expect: float | None = None # anime_human style bucket: predicted filled area (share of the canvas)
    pen: list | None = None     # anime_human style line: [t_ms, x, y, pressure] as the pen reports it
    alt: list | None = None     # bucket: other places to click if the first one pours far too little
    vlayer: int | None = None   # virtual layer (see layers.py); the plugin picks real layer and sort order

    def to_json(self):
        data = {
            "kind": self.kind,
            "points": self.points,
            "color": self.color,
            "size": self.size,
            "layer": self.layer,
            "phase": self.phase,
            "cost": round(self.cost, 3),
            "note": self.note,
        }
        if self.group is not None:
            data["group"] = self.group
        for name in ("dur", "pause", "expect"):
            if getattr(self, name) is not None:
                data[name] = round(getattr(self, name), 4)
        if self.pen:
            data["pen"] = self.pen
        if self.alt:
            data["alt"] = self.alt
        if self.vlayer is not None:
            data["vlayer"] = self.vlayer
        if self.fallback:
            data["fallback"] = self.fallback
        return data


def load_plan(path):
    """Load operations written by --plan-out (keeps notes/groups for study)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("plan file must contain a JSON array of operations")
    ops = []
    for item in data:
        kind = item.get("kind")
        if not kind:
            kind = {0: "line", 1: "disc", 3: "fill"}.get(int(item.get("type", 0)), "line")
        ops.append(Op(
            kind=kind,
            points=[[float(p[0]), float(p[1])] for p in item.get("points", [])],
            color=item.get("color", "#000000"),
            size=int(item.get("size", 4)),
            layer=int(item.get("layer", 0)),
            phase=item.get("phase", ""),
            cost=float(item.get("cost", 1.0)),
            fallback=item.get("fallback"),
            note=item.get("note", ""),
            group=item.get("group"),
            dur=item.get("dur"),
            pause=item.get("pause"),
            expect=item.get("expect"),
            pen=item.get("pen"),
            alt=item.get("alt"),
            vlayer=item.get("vlayer"),
        ))
    return ops


def write_progress(path, **fields):
    """Atomically publish job progress for the studio server (best effort)."""
    if not path:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")
        os.replace(str(temporary), str(target))
    except OSError:
        pass


@dataclass
class Budget:
    seconds: float
    usable_seconds: float
    units: float
    online: bool
    source: str
    layers: int = 1
    peak: float = 0.0        # longest the round's timer ever ran (what the plan is made for)
    remaining: float = 0.0   # what was left when the budget was read

    def summary(self):
        return (f"source={self.source} time={self.seconds:.1f}s usable={self.usable_seconds:.1f}s "
                f"units={int(self.units)} online={self.online} layers={self.layers}")


@dataclass
class Analysis:
    image: np.ndarray
    width: int
    height: int
    labels: np.ndarray
    quantized: np.ndarray
    palette: list[tuple[int, int, int]]
    edges: np.ndarray
    gray: np.ndarray
    cache: dict = field(default_factory=dict, repr=False, compare=False)


# ---------------------------------------------------------------------------
# image helpers
# ---------------------------------------------------------------------------

def hex_color(rgb) -> str:
    r, g, b = [max(0, min(255, int(round(float(v))))) for v in rgb[:3]]
    return f"#{r:02X}{g:02X}{b:02X}"


def parse_hex(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def mix_white(rgb, amount=0.25):
    return tuple(int(round(c * (1 - amount) + 255 * amount)) for c in rgb[:3])


def darken(rgb, factor=0.42):
    return tuple(int(round(c * factor)) for c in rgb[:3])


def load_image(path: str | Path, max_dim: int | None = 900) -> np.ndarray:
    image, _ = imagesize.load_scaled(path, max_dim)
    return np.array(image, dtype=np.uint8)


def quantize_image(rgb: np.ndarray, colors: int):
    pil = Image.fromarray(rgb)
    q = pil.quantize(colors=max(2, min(256, int(colors))),
                     method=Image.Quantize.MEDIANCUT,
                     dither=Image.Dither.NONE)
    palette_data = q.getpalette() or []
    palette = []
    for i in range(256):
        base = i * 3
        if base + 2 < len(palette_data):
            palette.append(tuple(int(v) for v in palette_data[base:base + 3]))
        else:
            palette.append((0, 0, 0))
    labels = np.array(q, dtype=np.uint8)
    return labels, np.array(q.convert("RGB"), dtype=np.uint8), palette


def edge_map(rgb: np.ndarray, strength: float = 1.0) -> np.ndarray:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    smooth = cv2.bilateralFilter(gray, 7, 50, 50)
    # Detect dark ridges, not both sides of every ridge as Canny does. This mask
    # is thinned to stroke centers downstream, including in --lineart off mode.
    edges = cv2.adaptiveThreshold(smooth, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                  cv2.THRESH_BINARY_INV, 15,
                                  max(2.0, 8.0 / max(0.4, strength)))
    # remove isolated noise
    count, labels, stats, _ = cv2.connectedComponentsWithStats(edges, 8)
    clean = np.zeros_like(edges)
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] >= 8:
            clean[labels == i] = 255
    return clean


_LINEART_CACHE: dict = {}
_MODEL_CACHE: dict = {}


def resolve_device(name: str | None) -> str:
    import torch
    if name and name != "auto":
        return name
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def anime2sketch_edges(image: np.ndarray, model_path: str, device: str = "auto",
                       threshold: int = 45) -> np.ndarray:
    """Run Anime2Sketch on the image and return a binary line-art mask."""
    device = resolve_device(device)
    key = (id(image), model_path, device, int(threshold))
    cached = _LINEART_CACHE.get(key)
    if cached is not None:
        return cached

    import torch
    from anime2sketch_net import load_checkpoint
    torch.set_num_threads(2)

    model_key = (model_path, device)
    model = _MODEL_CACHE.get(model_key)
    if model is None:
        planlog.note(f"loading Anime2Sketch weights ({Path(model_path).name}) on {device}")
        model = load_checkpoint(model_path, device)
        _MODEL_CACHE[model_key] = model
    planlog.note(f"Anime2Sketch inference 512x512 on {device}")

    pil = Image.fromarray(image)
    orig_size = pil.size
    work = pil.resize((512, 512), Image.BICUBIC)
    array = np.asarray(work, dtype=np.float32) / 127.5 - 1.0
    tensor = torch.from_numpy(array).permute(2, 0, 1).unsqueeze(0).to(device)
    with torch.no_grad():
        output = model(tensor)
    line = output[0, 0].detach().cpu().clamp(-1, 1).numpy()
    planlog.note("Anime2Sketch: cleaning line mask")
    line = ((line + 1.0) * 127.5).clip(0, 255).astype(np.uint8)
    line = cv2.resize(line, orig_size, interpolation=cv2.INTER_AREA)

    cutoff = max(1, 255 - int(threshold))
    mask = np.where(line < cutoff, 255, 0).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    keep = stats[:, cv2.CC_STAT_AREA] >= 5
    keep[0] = False
    clean = np.where(keep[labels], 255, 0).astype(np.uint8)
    _LINEART_CACHE[key] = clean
    return clean


def make_transform(width: int, height: int, canvas_aspect: float, margin: float = 0.04):
    image_aspect = width / max(1, height)
    fit_w = 1.0 - 2.0 * margin
    fit_h = 1.0 - 2.0 * margin
    if image_aspect > canvas_aspect:
        fit_h = fit_w * canvas_aspect / image_aspect
    else:
        fit_w = fit_h * image_aspect / canvas_aspect
    origin_x = (1.0 - fit_w) * 0.5
    origin_y = (1.0 - fit_h) * 0.5

    def transform(x: float, y: float):
        nx = origin_x + (x / width) * fit_w
        ny = origin_y + (1.0 - y / height) * fit_h
        return [round(float(nx), 5), round(float(ny), 5)]

    def rect():
        return [transform(0, 0), transform(width, 0), transform(width, height), transform(0, height)]

    return transform, (fit_w, fit_h), rect


def make_region_transform(width: int, height: int, canvas_aspect: float,
                          cell=(0.0, 0.0, 1.0, 1.0), margin: float = 0.04):
    """Fit an image into a normalized canvas cell (x, y, w, h); y points up."""
    cell_x, cell_y, cell_w, cell_h = cell
    image_aspect = width / max(1, height)
    avail_w = max(0.02, cell_w * (1.0 - 2.0 * margin))
    avail_h = max(0.02, cell_h * (1.0 - 2.0 * margin))

    fit_w = avail_w
    fit_h = fit_w * canvas_aspect / image_aspect
    if fit_h > avail_h:
        fit_h = avail_h
        fit_w = fit_h * image_aspect / canvas_aspect

    origin_x = cell_x + (cell_w - fit_w) * 0.5
    origin_y = cell_y + (cell_h - fit_h) * 0.5

    def transform(x: float, y: float):
        nx = origin_x + (x / width) * fit_w
        ny = origin_y + (1.0 - y / height) * fit_h
        return [round(float(nx), 5), round(float(ny), 5)]

    def rect():
        return [transform(0, 0), transform(width, 0), transform(width, height), transform(0, height)]

    return transform, (fit_w, fit_h), rect


def contour_length(contour) -> float:
    if contour is None or len(contour) < 2:
        return 0.0
    pts = contour.reshape(-1, 2).astype(np.float32)
    return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))


def simplify_contour(contour, epsilon: float):
    approx = cv2.approxPolyDP(contour, max(0.3, epsilon), True)
    return approx.reshape(-1, 2)


def _segments_intersect(a, b, c, d, epsilon: float = 1e-9) -> bool:
    """True when two non-adjacent segments properly cross.

    The game triangulates fill polygons; PIL (used by the preview and by
    tools/compare_canvas.py) fills them with an even-odd scanline. A proper
    self-crossing therefore becomes a large filled blob in the game while the
    preview leaves the loop empty.
    """
    def cross(u, v, w):
        return ((v[0] - u[0]) * (w[1] - u[1]) -
                (v[1] - u[1]) * (w[0] - u[0]))

    d1 = cross(c, d, a)
    d2 = cross(c, d, b)
    d3 = cross(a, b, c)
    d4 = cross(a, b, d)
    # Only proper crossings create a second loop that the game fills as a
    # blob while the preview leaves it empty. Vertices that merely touch are
    # common in simplified contours and render the same in both.
    return (((d1 > epsilon and d2 < -epsilon) or (d1 < -epsilon and d2 > epsilon)) and
            ((d3 > epsilon and d4 < -epsilon) or (d3 < -epsilon and d4 > epsilon)))


def is_simple_polygon(points) -> bool:
    pts = [(float(p[0]), float(p[1])) for p in points]
    n = len(pts)
    if n < 3:
        return False
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if _segments_intersect(a, b, pts[j], pts[(j + 1) % n]):
                return False
    return True


def simple_polygon(contour, epsilons=(1.2, 0.6, 0.3)):
    """Simplify a contour and return only a polygon both renderers fill alike.

    Large simplification can fold a narrow neck into a crossing. Retry with a
    tighter epsilon before giving up; a skipped fill is much better than a
    dark blob covering a light area in the game.
    """
    for epsilon in epsilons:
        polygon = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2)
        if len(polygon) >= 3 and is_simple_polygon(polygon):
            return polygon
    return None


def closed(points: list[list[float]]):
    if len(points) < 2:
        return points
    if points[0] != points[-1]:
        points = points + [points[0]]
    return points


def color_at(rgb: np.ndarray, x: float, y: float):
    h, w = rgb.shape[:2]
    xi = int(max(0, min(w - 1, x)))
    yi = int(max(0, min(h - 1, y)))
    return tuple(int(v) for v in rgb[yi, xi])


def sample_contour_color(rgb, contour, samples=9):
    if contour is None or len(contour) == 0:
        return (0, 0, 0)
    pts = contour.reshape(-1, 2)
    idx = np.linspace(0, len(pts) - 1, min(samples, len(pts))).astype(int)
    colors = [color_at(rgb, float(pts[i][0]), float(pts[i][1])) for i in idx]
    return tuple(int(np.median([c[k] for c in colors])) for k in range(3))


# ---------------------------------------------------------------------------
# regions
# ---------------------------------------------------------------------------

@dataclass
class Region:
    mask: np.ndarray
    contour: np.ndarray
    holes: list[np.ndarray]
    color: tuple[int, int, int]
    area: float
    bbox: tuple[int, int, int, int]
    seed: tuple[float, float]
    background: bool
    edge_density: float


def extract_regions(analysis: Analysis, min_area_frac: float = 0.0007) -> list[Region]:
    rgb = analysis.image
    labels = analysis.labels
    h, w = labels.shape[:2]
    boundary = np.zeros((h, w), np.uint8)

    # label discontinuities
    diff_x = np.zeros((h, w), np.uint8)
    diff_y = np.zeros((h, w), np.uint8)
    diff_x[:, 1:] = ((labels[:, 1:] != labels[:, :-1]) * 255).astype(np.uint8)
    diff_y[1:, :] = ((labels[1:, :] != labels[:-1, :]) * 255).astype(np.uint8)
    boundary = cv2.bitwise_or(diff_x, diff_y)
    boundary = cv2.bitwise_or(boundary, cv2.dilate(analysis.edges, np.ones((2, 2), np.uint8)))
    boundary = cv2.dilate(boundary, np.ones((2, 2), np.uint8))

    free = 255 - boundary
    count, cc, stats, _ = cv2.connectedComponentsWithStats(free, 8)
    min_area = max(18.0, w * h * min_area_frac)
    regions: list[Region] = []

    for i in range(1, count):
        area = float(stats[i, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        x, y, bw, bh = (int(stats[i, k]) for k in range(4))
        mask = (cc == i).astype(np.uint8)
        values, counts = np.unique(labels[mask > 0], return_counts=True)
        if len(values) == 0:
            continue
        color = analysis.palette[int(values[int(np.argmax(counts))])]

        contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        main_index = max(range(len(contours)), key=lambda j: cv2.contourArea(contours[j]))
        main = contours[main_index]
        if len(main) < 3:
            continue
        holes = [c for j, c in enumerate(contours) if j != main_index and cv2.contourArea(c) >= 8]

        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
        sy, sx = np.unravel_index(int(np.argmax(dist)), dist.shape)
        if dist[sy, sx] < 1.0:
            continue

        bbox = (x, y, bw, bh)
        background = (x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1 or area > 0.12 * w * h)
        edge_roi = analysis.edges[y:y + bh, x:x + bw]
        edge_density = float(np.count_nonzero(edge_roi)) / max(1.0, float(bw * bh))

        regions.append(Region(
            mask=mask, contour=main, holes=holes, color=color, area=area,
            bbox=bbox, seed=(float(sx), float(sy)), background=background,
            edge_density=edge_density,
        ))

    regions.sort(key=lambda r: -r.area)
    return regions


def _regions_from_label_map(analysis: Analysis, labels: np.ndarray,
                            min_area: float) -> list[Region]:
    """Turn a per-pixel label map into Region objects (one per label)."""
    rgb = analysis.image
    h, w = rgb.shape[:2]
    regions: list[Region] = []
    for label in np.unique(labels):
        if label < 0:
            continue
        mask = (labels == label).astype(np.uint8)
        area = float(np.count_nonzero(mask))
        if area < min_area:
            continue
        ys, xs = np.where(mask > 0)
        if len(xs) == 0:
            continue
        color = tuple(int(v) for v in np.median(rgb[ys, xs], axis=0))
        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
        sy, sx = np.unravel_index(int(np.argmax(dist)), dist.shape)
        if dist[sy, sx] < 0.8:
            continue
        contours, _ = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        main_index = max(range(len(contours)), key=lambda j: cv2.contourArea(contours[j]))
        main = contours[main_index]
        if len(main) < 3:
            continue
        holes = [c for j, c in enumerate(contours) if j != main_index and cv2.contourArea(c) >= 6]
        x, y, bw, bh = cv2.boundingRect(main)
        background = (x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1
                      or area > 0.12 * w * h)
        edge_roi = analysis.edges[y:y + bh, x:x + bw]
        edge_density = float(np.count_nonzero(edge_roi)) / max(1.0, float(bw * bh))
        regions.append(Region(
            mask=mask, contour=main, holes=holes, color=color, area=area,
            bbox=(x, y, bw, bh), seed=(float(sx), float(sy)),
            background=background, edge_density=edge_density,
        ))
    regions.sort(key=lambda r: -r.area)
    return regions


def extract_regions_meanshift(analysis: Analysis, k: int = 800,
                              min_size: int = 60,
                              min_area_frac: float = 0.0002) -> list[Region]:
    """Segmentation for OpenCV builds without ximgproc.

    Mean-shift removes hair/tree texture, then median-cut quantisation gives
    flat colour patches. Adjacent patches close in Lab are merged, and small
    components are absorbed by their most similar neighbour so the result
    covers the whole image instead of leaving holes. This is the fallback for
    `createGraphSegmentation`, which is missing from many pip/conda builds.
    """
    rgb = analysis.image
    h, w = rgb.shape[:2]
    colors = max(10, min(24, int(round(k / 50.0))))
    merge_dist = 18.0 if k <= 500 else (14.0 if k <= 800 else (12.0 if k <= 1100 else 9.0))
    sp = max(6, min(24, int(round(min(w, h) / 35.0))))
    sr = max(20, min(40, int(round(min(w, h) / 14.0))))
    smooth = cv2.pyrMeanShiftFiltering(rgb, sp, sr, maxLevel=1)
    lab = cv2.cvtColor(smooth, cv2.COLOR_RGB2LAB).astype(np.float32)
    quantized = np.array(Image.fromarray(smooth).quantize(
        colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))

    comp_id = np.full((h, w), -1, np.int32)
    comp_area: list[int] = []
    comp_lab: list[np.ndarray] = []
    for label in np.unique(quantized):
        count, cc, stats, _ = cv2.connectedComponentsWithStats(
            (quantized == label).astype(np.uint8), 8)
        for i in range(1, count):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < 12:
                continue
            mask = cc == i
            ys, xs = np.nonzero(mask)
            comp_id[mask] = len(comp_area)
            comp_area.append(area)
            comp_lab.append(lab[ys, xs].mean(axis=0))
    n = len(comp_area)
    if n == 0:
        return extract_regions(analysis, min_area_frac=min_area_frac)

    parent = np.arange(n, dtype=np.int32)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = int(parent[a])
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    pairs: dict[tuple[int, int], int] = {}
    for dy, dx in ((0, 1), (1, 0)):
        a = comp_id[:h - dy, :w - dx]
        b = comp_id[dy:, dx:]
        valid = (a >= 0) & (b >= 0) & (a != b)
        if not valid.any():
            continue
        lo = np.minimum(a[valid], b[valid]).astype(np.int64)
        hi = np.maximum(a[valid], b[valid]).astype(np.int64)
        key = lo * n + hi
        uniq, counts = np.unique(key, return_counts=True)
        for value, count in zip(uniq, counts):
            pair = (int(value // n), int(value % n))
            pairs[pair] = pairs.get(pair, 0) + int(count)

    for a, b in pairs:
        if float(np.linalg.norm(comp_lab[a] - comp_lab[b])) < merge_dist:
            union(a, b)

    min_area = max(40.0, w * h * min_area_frac)
    for a in range(n):
        if comp_area[a] >= min_area:
            continue
        best, best_dist = None, 1e9
        for x, y in pairs:
            if x != a and y != a:
                continue
            other = y if x == a else x
            distance = float(np.linalg.norm(comp_lab[a] - comp_lab[other]))
            if distance < best_dist:
                best, best_dist = other, distance
        if best is not None and best_dist < 60.0:
            union(a, best)

    roots = np.array([find(i) for i in range(n)], dtype=np.int32)
    unique_roots, inverse = np.unique(roots, return_inverse=True)
    labels = np.where(comp_id >= 0, inverse[np.maximum(comp_id, 0)], -1)
    return _regions_from_label_map(analysis, labels, min_area)


def extract_regions_graph(analysis: Analysis, k: int = 500, min_size: int = 60,
                          min_area_frac: float = 0.0002) -> list[Region]:
    """Region segmentation that tiles the whole image, so the flat-fill pass
    has no accidental white holes. Uses OpenCV graph segmentation when the
    build provides it and the mean-shift fallback otherwise."""
    key = ("regions", int(k), int(min_size), float(min_area_frac))
    cached = analysis.cache.get(key)
    if cached is not None:
        return cached

    rgb = analysis.image
    h, w = rgb.shape[:2]
    min_area = max(12.0, w * h * min_area_frac)
    try:
        gs = cv2.ximgproc.segmentation.createGraphSegmentation()
        gs.setSigma(0.8)
        gs.setK(float(k))
        gs.setMinSize(int(min_size))
        regions = _regions_from_label_map(analysis, gs.processImage(rgb), min_area)
    except Exception:
        regions = extract_regions_meanshift(analysis, k=k, min_size=min_size,
                                            min_area_frac=min_area_frac)
    analysis.cache[key] = regions
    return regions


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------

STUDY_STYLES = ("anime_study", "anime_human")

STYLE_MAX_UNITS = {
    "sketch": 1600.0,
    "anime_flat": 2200.0,
    "anime_study": 900.0,
    "anime_human": 900.0,
    "impasto": 3000.0,
    "watercolor": 2000.0,
}

STYLE_DEFAULT_COLORS = {
    "sketch": 8,
    "anime_flat": 40,
    "anime_study": 6,
    "anime_human": 6,
    "impasto": 24,
    "watercolor": 8,
}

GRID_STYLES = ["sketch", "anime_flat", "impasto", "watercolor"]
# normalized canvas cells, y points up: top-left, top-right, bottom-left, bottom-right
GRID_CELLS = [
    (0.0, 0.5, 0.5, 0.5),
    (0.5, 0.5, 0.5, 0.5),
    (0.0, 0.0, 0.5, 0.5),
    (0.5, 0.0, 0.5, 0.5),
]


def make_analysis(image, style: str, args, edges_override=None) -> Analysis:
    colors = args.colors or STYLE_DEFAULT_COLORS[style]
    labels, quantized, palette = quantize_image(image, colors)
    edges = edges_override if edges_override is not None else edge_map(
        image, 1.0 if style != "sketch" else 1.25)
    return Analysis(
        image=image,
        width=image.shape[1],
        height=image.shape[0],
        labels=labels,
        quantized=quantized,
        palette=palette,
        edges=edges,
        gray=cv2.cvtColor(image, cv2.COLOR_RGB2GRAY),
        cache={"ai_lineart": edges_override is not None},
    )


def resolve_budget(client: BridgeClient, style: str, args) -> Budget:
    try:
        state = {} if args.dry_run else client.state()
    except Exception:
        state = {}
    round_state = {}
    try:
        round_state = {} if args.dry_run else client.round()
    except Exception:
        pass

    remaining = None
    peak = 0.0
    source = "default"
    recommended = (round_state or {}).get("recommended") if isinstance(round_state, dict) else None
    if recommended and float(recommended.get("remaining", 0)) > 0:
        remaining = float(recommended["remaining"])
        source = "round"
        # The plan is made for the round's full length, never less: if only 120 s of a
        # 180 s round are left the drawing is the same one, drawn faster.
        peak = max(float(recommended.get("peak") or 0), float(recommended.get("total") or 0),
                   remaining + float(recommended.get("passed") or 0), remaining)
    if args.time is not None:
        remaining = float(args.time)
        source = "cli"
        peak = 0.0
    if remaining is None:
        remaining = float(args.default_time)
        source = "sketchbook-default"
    horizon = max(remaining, peak)
    # Study styles on a live round timer keep a fixed 30 s for the player at the end.
    args.round_reserve = bool(source == "round" and style in STUDY_STYLES)

    module = str(state.get("module") or "")
    online = bool(module) and module not in ("SketchbookDrawModule", "TestDrawModule")
    # Keep a reserve for slow requests, planning drift and a late round timer.
    usable = pacing.usable_seconds(horizon, args.round_reserve)
    args.budget_anchor = time.monotonic()
    rate = float(args.rate) if args.rate else (11.0 if online else 65.0)
    units = usable * rate
    units = min(units, STYLE_MAX_UNITS.get(style, 2000.0))
    if args.budget is not None:
        units = float(args.budget)
        source = "cli-budget"

    # Offline planning has no game to ask; the game normally has three layers.
    layers = 3 if args.dry_run and style in STUDY_STYLES else 1
    if isinstance(state.get("layers"), dict):
        layers = max(1, int(state["layers"].get("total", 1)))

    return Budget(seconds=horizon, usable_seconds=usable,
                  units=max(0.0 if style in STUDY_STYLES else 10.0, units),
                  online=online, source=source, layers=layers, peak=peak, remaining=remaining)


def grabcut_subject(analysis: Analysis):
    """GrabCut with a generous central rectangle is a robust foreground mask for
    portraits/illustrations, and much less fragile than touching-the-border tests."""
    rgb = analysis.image
    h, w = rgb.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    rect = (max(1, int(w * 0.06)), max(1, int(h * 0.04)),
            max(2, int(w * 0.88)), max(2, int(h * 0.92)))
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(rgb, mask, rect, bgd, fgd, 4, cv2.GC_INIT_WITH_RECT)
    except Exception:
        return None
    fg = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(fg)
    if contours:
        cv2.drawContours(filled, contours, -1, 255, -1)
    coverage = np.count_nonzero(filled) / max(1, w * h)
    if coverage < 0.06 or coverage > 0.96:
        return None
    return filled


def build_subject_mask(analysis: Analysis, k: int = 750, min_size: int = 60):
    regions = extract_regions_graph(analysis, k=k, min_size=min_size)
    mask = grabcut_subject(analysis)
    if mask is None:
        mask = np.zeros((analysis.height, analysis.width), np.uint8)
        for region in regions:
            if not region.background:
                mask |= region.mask
    else:
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
        # include graph regions that mostly sit inside the GrabCut foreground
        for region in regions:
            if region.background:
                continue
            overlap = int(np.count_nonzero(region.mask & mask))
            if overlap > region.area * 0.35:
                mask |= region.mask
    if np.count_nonzero(mask) < (analysis.width * analysis.height) * 0.05:
        mask[:] = 1
    return mask, regions


def smooth_closed(points, iterations: int = 1):
    pts = [tuple(p) for p in points]
    if len(pts) < 3:
        return [list(p) for p in pts]
    for _ in range(max(0, iterations)):
        out = []
        for i in range(len(pts)):
            p = pts[i]
            q = pts[(i + 1) % len(pts)]
            out.append((p[0] * 0.75 + q[0] * 0.25, p[1] * 0.75 + q[1] * 0.25))
            out.append((p[0] * 0.25 + q[0] * 0.75, p[1] * 0.25 + q[1] * 0.75))
        pts = out
    pts.append(pts[0])
    return [[float(x), float(y)] for x, y in pts]


def contour_inside_fraction(contour, mask, samples=9) -> float:
    if contour is None or len(contour) == 0:
        return 0.0
    pts = contour.reshape(-1, 2)
    idx = np.linspace(0, len(pts) - 1, min(samples, len(pts))).astype(int)
    inside = 0
    for i in idx:
        x = int(max(0, min(mask.shape[1] - 1, pts[i][0])))
        y = int(max(0, min(mask.shape[0] - 1, pts[i][1])))
        if mask[y, x]:
            inside += 1
    return inside / max(1, len(idx))


# ---------------------------------------------------------------------------
# style: sketch
# ---------------------------------------------------------------------------

def build_sketch(analysis: Analysis, budget: Budget, args, transform) -> list[Op]:
    w, h = analysis.width, analysis.height
    subject, regions = build_subject_mask(analysis, k=args.seg_k or 750,
                                          min_size=args.seg_min_size)
    ops = centerline_ops(analysis, budget.units * 0.62, transform, size=1)
    rng = random.Random(args.seed)

    # hatching: only on the subject, in the darker half-tones
    hatch_budget = budget.units * 0.24
    used_hatch = 0.0
    gray = analysis.gray
    shadow_pct = 42 if budget.units < 800 else (52 if budget.units < 1500 else 60)
    shadow = ((gray < np.percentile(gray, shadow_pct)) & (subject > 0)).astype(np.uint8)
    shadow = cv2.morphologyEx(shadow, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    spacing_factor = 1.0 if budget.units < 800 else (0.75 if budget.units < 1500 else 0.55)
    spacing = max(4, int(min(w, h) * 0.022 * spacing_factor))
    skip_chance = 0.25 if budget.units < 800 else (0.16 if budget.units < 1500 else 0.10)
    for y in range(0, h, spacing):
        for x in range(0, w, spacing):
            if used_hatch >= hatch_budget:
                break
            jx = x + rng.randint(-spacing // 2, spacing // 2)
            jy = y + rng.randint(-spacing // 2, spacing // 2)
            if jx < 0 or jy < 0 or jx >= w or jy >= h or shadow[jy, jx] == 0:
                continue
            if rng.random() < skip_chance:
                continue
            angle = math.radians(44 + rng.uniform(-14, 14))
            length = rng.uniform(5.0, 11.0) * max(0.8, min(w, h) / 500.0)
            x2 = jx + math.cos(angle) * length
            y2 = jy + math.sin(angle) * length
            pts = [transform(float(jx), float(jy)), transform(float(x2), float(y2))]
            color = darken(color_at(analysis.image, jx, jy), 0.45)
            ops.append(Op("line", pts, hex_color(color), 2, 0, phase="hatch",
                          cost=0.55, note="hatch"))
            used_hatch += 0.55

    # a few color accents on large saturated regions
    accent_budget = budget.units * 0.10
    used_accent = 0.0
    for region in regions:
        if used_accent >= accent_budget:
            break
        if region.background or region.area < w * h * 0.005:
            continue
        color = region.color
        if max(color) - min(color) < 18:
            continue
        ys, xs = np.where(region.mask > 0)
        if len(xs) == 0:
            continue
        for _ in range(3):
            i = rng.randrange(len(xs))
            x, y = int(xs[i]), int(ys[i])
            length = rng.uniform(10, 26)
            angle = math.radians(rng.uniform(-30, 30))
            pts = [transform(x, y), transform(x + math.cos(angle) * length, y + math.sin(angle) * length)]
            ops.append(Op("line", pts, hex_color(mix_white(color, 0.10)), 5,
                          phase="accent", cost=1.1, note="accent"))
            used_accent += 1.1
    return ops


# ---------------------------------------------------------------------------
# style: anime flat (real fill)
# ---------------------------------------------------------------------------

def centerline_ops(analysis: Analysis, units: float, transform, layer=0,
                   color=None, size=1, ink_percentile=25, ink_scale=0.70,
                   ink_lo=24, ink_hi=135) -> list[Op]:
    paths = analysis.cache.get("centerlines")
    if paths is None:
        paths = trace_centerlines(analysis.edges, min_length=3.0)
        analysis.cache["centerlines"] = paths
    candidates = []
    for path in paths:
        length = float(np.linalg.norm(np.diff(path, axis=0), axis=1).sum())
        # Keep short facial marks, but discard single-pixel thinning spurs.
        if length < 4.0:
            continue
        approx = cv2.approxPolyDP(path.reshape(-1, 1, 2), 0.45, False).reshape(-1, 2)
        if len(approx) < 2:
            continue
        xy = path.astype(int)
        samples = analysis.image[xy[:, 1], xy[:, 0]]
        luminance = analysis.gray[xy[:, 1], xy[:, 0]]
        ink = np.percentile(samples, ink_percentile, axis=0)
        # Source-colored ink avoids turning a soft colored edge into black wire.
        ink = tuple(int(v) for v in np.clip(ink * ink_scale, ink_lo, ink_hi))
        score = math.sqrt(length) * (1.0 + (255 - float(np.mean(luminance))) / 255.0)
        candidates.append((score, approx, hex_color(ink)))
    candidates.sort(key=lambda item: -item[0])
    ops = []
    for _, path, ink in candidates[:max(0, int(units))]:
        pts = [transform(float(x), float(y)) for x, y in path]
        ops.append(Op("line", pts, color or ink, size, layer, phase="ink",
                      cost=1.0, note="centerline"))
    return ops


def color_detail_candidates(analysis: Analysis, painted):
    fine = cv2.bilateralFilter(analysis.image, 5, 12, 2)
    near_ink = cv2.dilate(analysis.edges, np.ones((5, 5), np.uint8))
    details = []
    # Local palettes preserve rare colors such as an iris; a global palette
    # otherwise spends those slots on the much larger background and hair.
    for ty in range(0, analysis.height, 48):
        for tx in range(0, analysis.width, 48):
            tile = fine[ty:ty + 48, tx:tx + 48]
            labels, rgb, _ = quantize_image(tile, 24)
            near = near_ink[ty:ty + 48, tx:tx + 48]
            for label in np.unique(labels):
                count, components, stats, _ = cv2.connectedComponentsWithStats(
                    (labels == label).astype(np.uint8), connectivity=8)
                for i in range(1, count):
                    x, y, bw, bh, area = (int(v) for v in stats[i])
                    if not 3 <= area <= 160:
                        continue
                    part = (components[y:y + bh, x:x + bw] == i).astype(np.uint8) * 255
                    if np.mean(near[y:y + bh, x:x + bw][part > 0] > 0) < 0.60:
                        continue
                    color = rgb[y:y + bh, x:x + bw][part > 0][0]
                    existing = painted[ty + y:ty + y + bh, tx + x:tx + x + bw][part > 0]
                    error = float(np.abs(existing.astype(float) - color).mean())
                    if error < 8:
                        continue
                    contours, _ = cv2.findContours(part, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    if not contours:
                        continue
                    polygon = simple_polygon(max(contours, key=cv2.contourArea),
                                             epsilons=(0.3, 0.15))
                    if polygon is not None:
                        details.append((error * math.sqrt(area), tx + x, ty + y,
                                        polygon, color))
    return sorted(details, key=lambda item: -item[0])


def flat_palette_size(budget: Budget, args):
    return args.colors or (16 if budget.units < 400 else (24 if budget.units < 1000 else 40))


def build_anime_flat(analysis: Analysis, budget: Budget, args, transform) -> list[Op]:
    # Color regions are NOT ink boundaries. Paint them without black fences;
    # the model's line centers provide the only ink pass, with no hatch padding.
    w, h = analysis.width, analysis.height
    fill_layer = 0
    ink_layer = 1 if budget.layers >= 2 else 0
    fill_units = budget.units * 0.70
    real_fill = args.fill_mode == "real"
    region_cost = 9.35 if real_fill else 2.2
    colors = flat_palette_size(budget, args)
    source = analysis.image
    if analysis.cache.get("ai_lineart"):
        # Remove the source's thin ink from the color field so it isn't drawn
        # again beside the model stroke. Broad dark shapes remain colored.
        ink_mask = ((analysis.edges > 0) & (analysis.gray < 100)).astype(np.uint8) * 255
        source = cv2.inpaint(source, ink_mask, 2, cv2.INPAINT_TELEA)
    smooth = cv2.pyrMeanShiftFiltering(source, sp=5, sr=18)
    smooth = cv2.bilateralFilter(smooth, 7, 24, 5)
    labels, quantized, _ = quantize_image(smooth, colors)
    dominant = int(np.bincount(labels.ravel(), minlength=256).argmax())
    base_color = tuple(int(c) for c in quantized[labels == dominant][0])
    ops = [Op("fill", transform_rect((w, h), transform), hex_color(base_color),
              1, fill_layer, phase="color", cost=2.2, note="color-base")]
    used = 2.2
    painted = np.empty_like(source)
    painted[:] = base_color
    candidates = []
    for label in np.unique(labels):
        mask = (labels == label).astype(np.uint8)
        count, components, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for i in range(1, count):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < 8:
                continue
            x, y, bw, bh, _ = stats[i]
            part = (components[y:y + bh, x:x + bw] == i).astype(np.uint8) * 255
            contours, _ = cv2.findContours(part, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                continue
            outer_area = cv2.contourArea(max(contours, key=cv2.contourArea))
            expanded = cv2.dilate(np.pad(part, 2), np.ones((3, 3), np.uint8),
                                  iterations=2)
            contours, _ = cv2.findContours(expanded, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contour = max(contours, key=cv2.contourArea) - np.array([[[2, 2]]])
            color = tuple(int(v) for v in quantized[y:y + bh, x:x + bw][part > 0][0])
            candidates.append((outer_area, int(x), int(y), contour, color))
    # A hole's components have smaller outer areas than its enclosing ring.
    # Painting outer contours in that order restores nested colors without
    # drawing an ink boundary or hundreds of artificial hole-closing strokes.
    candidates.sort(key=lambda item: -item[0])
    # Very small patches read as fragments at game resolution. The preview
    # can show them, but the game's fill outline amplifies every boundary.
    region_limit = min(args.max_regions, 500)
    skipped = 0
    for _, x, y, contour, color in candidates[:region_limit]:
        if used + region_cost > fill_units:
            break
        polygon = simple_polygon(contour)
        if polygon is None:
            skipped += 1
            continue
        polygon[:, 0] = np.clip(polygon[:, 0] + x, 0, w)
        polygon[:, 1] = np.clip(polygon[:, 1] + y, 0, h)
        pts = [transform(float(px), float(py)) for px, py in polygon]
        # transform() rounds to 5 decimals; near-duplicate vertices can cross
        # only after rounding. Validate exactly what the bridge will receive.
        if not is_simple_polygon(pts):
            skipped += 1
            continue
        cv2.fillPoly(painted, [polygon.astype(np.int32)], color)
        if real_fill:
            mask = np.zeros((h + 1, w + 1), np.uint8)
            cv2.fillPoly(mask, [polygon.astype(np.int32)], 255)
            _, _, _, seed = cv2.minMaxLoc(cv2.distanceTransform(mask, cv2.DIST_L2, 3))
            # Real fill still needs a fence, but it matches the region's color
            # rather than imposing a black outline on every shadow boundary.
            ops.append(Op("line", closed(pts), hex_color(color), 2, fill_layer,
                          phase="color", cost=1.35, note="fill-fence"))
            ops.append(Op("realfill", [transform(*seed)], hex_color(color),
                          1, fill_layer, phase="color", cost=8.0,
                          fallback=closed(pts), note="color-region"))
        else:
            ops.append(Op("fill", pts, hex_color(color), 1, fill_layer,
                          phase="color", cost=2.2, note="color-region"))
        used += region_cost
    if skipped:
        print(f"flat: skipped {skipped} self-intersecting fill(s)")
    # Small colored features (irises, hair ornaments, folds) disappear in a
    # global palette. Restore source-color islands near the extracted ink, not
    # extra black contours. Leave the existing ink budget intact.
    ink_ops = centerline_ops(
        analysis, min(max(0.0, budget.units - used), 180.0), transform,
        ink_layer, args.line_color, ink_percentile=42, ink_scale=0.82,
        ink_lo=36, ink_hi=170)
    detail_allowance = min(budget.units * 0.10,
                           max(0.0, budget.units - used - len(ink_ops)))
    if not real_fill and detail_allowance >= 2.2:
        details = color_detail_candidates(analysis, painted)
        for _, x, y, polygon, color in details[:min(80, int(detail_allowance / 2.2))]:
            pts = [transform(float(px + x), float(py + y)) for px, py in polygon]
            if not is_simple_polygon(pts):
                continue
            ops.append(Op("fill", pts, hex_color(color), 1, fill_layer,
                          phase="color", cost=2.2, note="color-detail"))
            used += 2.2
    ops += ink_ops
    if args.max_strokes is not None:
        ops = ops[:max(0, args.max_strokes)]
    return ops


# ---------------------------------------------------------------------------
# style: impasto
# ---------------------------------------------------------------------------

def build_impasto(analysis: Analysis, budget: Budget, args, transform) -> list[Op]:
    w, h = analysis.width, analysis.height
    k = args.seg_k if args.seg_k else 1200
    regions = extract_regions_graph(analysis, k=k, min_size=args.seg_min_size)
    subject, _ = build_subject_mask(analysis, k=k, min_size=args.seg_min_size)
    layers = budget.layers
    paint_layer = 1 if layers >= 2 else 0
    detail_layer = 2 if layers >= 3 else paint_layer
    scale = (0.92 * 1300.0) / max(w, h)
    ops: list[Op] = []
    rng = random.Random(args.seed)

    # 1. broad region blocks: opaque base coverage without thousands of fills
    max_blocks = max(10, min(args.max_regions, int(budget.units * 0.22 // 2.0)))
    blocks = []
    canvas = np.full((h, w, 3), 255, np.uint8)
    for region in regions[:max_blocks]:
        color = region.color
        if max(color) > 248 and min(color) > 244:
            continue
        approx = simplify_contour(region.contour, 1.2)
        if len(approx) < 3:
            continue
        pts = [transform(float(p[0]), float(p[1])) for p in approx]
        if len(pts) < 3:
            continue
        ops.append(Op("fill", closed(pts), hex_color(color), 1,
                      paint_layer, phase="block", cost=2.0, note="impasto-block"))
        cv2.fillPoly(canvas, [approx.reshape(-1, 1, 2).astype(np.int32)], color)
        blocks.append(region)
        if len(ops) * 2.0 > budget.units * 0.22:
            break

    # 2. curved brush strokes over the blocks
    rgbf = analysis.image.astype(np.float32)
    gray = cv2.cvtColor(analysis.image, cv2.COLOR_RGB2GRAY)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    base_radius = max(3.0, min(w, h) / 32.0)
    layer_count = 3 if budget.units > 700 else 2
    radii = [base_radius * (0.62 ** i) for i in range(layer_count)]
    radii = [max(2.0, r) for r in radii]
    strokes_budget = budget.units * 0.62
    used_strokes = 0.0
    candidates = []
    canvas_f = canvas.astype(np.float32)

    for li, radius in enumerate(radii):
        sigma = max(0.8, radius * 0.6)
        blurred = cv2.GaussianBlur(rgbf, (0, 0), sigma)
        step = max(3, int(round(radius * 0.85)))
        ox = rng.randint(0, step - 1) if step > 1 else 0
        oy = rng.randint(0, step - 1) if step > 1 else 0
        for y in range(oy, h, step):
            for x in range(ox, w, step):
                px = min(w - 1, max(0, float(x) + rng.uniform(-step * 0.3, step * 0.3)))
                py = min(h - 1, max(0, float(y) + rng.uniform(-step * 0.3, step * 0.3)))
                ix, iy = int(px), int(py)
                target = blurred[iy, ix]
                # skip near-white paper unless we are in the background skirt
                error = float(np.mean(np.abs(target - canvas_f[iy, ix])))
                threshold = (11.0 if li == 0 else (8.0 if li == 1 else 6.0))
                if error < threshold:
                    continue
                gradient = abs(float(gx[iy, ix])) + abs(float(gy[iy, ix]))
                if li > 0 and gradient < (10.0 if li == 1 else 18.0):
                    continue
                points = trace_brush_stroke(gx, gy, blurred, px, py, radius, rng)
                if len(points) < 2:
                    continue
                mid = points[len(points) // 2]
                color = color_at(blurred.astype(np.uint8), float(mid[0]), float(mid[1]))
                cv2.line(canvas, (int(points[0][0]), int(points[0][1])),
                         (int(points[-1][0]), int(points[-1][1])),
                         tuple(int(c) for c in color), int(max(1, radius * 0.85)), cv2.LINE_AA)
                norm = [transform(float(p[0]), float(p[1])) for p in points]
                size = int(max(2, min(35, round(radius * scale * 0.95))))
                cost = 0.75 + len(norm) / 80.0
                candidates.append((li, Op("line", norm, hex_color(color), size,
                                          phase="coarse" if li == 0 else "fine",
                                          cost=cost, note=f"impasto r={radius:.1f}")))
                if len(candidates) > max(200, int(budget.units * 2)):
                    break
            if len(candidates) > max(200, int(budget.units * 2)):
                break
        if len(candidates) > max(200, int(budget.units * 2)):
            break

    candidates.sort(key=lambda item: item[0])
    for _, op in candidates:
        if used_strokes + op.cost > strokes_budget:
            break
        ops.append(op)
        used_strokes += op.cost

    # 3. subject silhouette and key form lines
    if args.outlines:
        outline_budget = budget.units * 0.10
        used_outline = 0.0
        contours, _ = cv2.findContours(subject, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            main = max(contours, key=cv2.contourArea)
            approx = simplify_contour(main, 1.6)
            if len(approx) >= 3:
                pts = smooth_closed([transform(float(p[0]), float(p[1])) for p in approx], 1)
                for s2 in range(0, max(1, len(pts) - 1), 10):
                    seg = pts[s2:min(len(pts), s2 + 10)]
                    if len(seg) >= 2 and used_outline < outline_budget:
                        ops.append(Op("line", seg, "#333333", 2, detail_layer,
                                      phase="outline", cost=1.0, note="impasto-outline"))
                        used_outline += 1.0

        major = [r for r in regions if not r.background and r.area >= w * h * 0.003]
        for region in sorted(major, key=lambda r: -r.area)[:16]:
            if used_outline >= outline_budget:
                break
            approx = simplify_contour(region.contour, 2.0)
            if len(approx) < 3:
                continue
            pts = smooth_closed([transform(float(p[0]), float(p[1])) for p in approx], 1)
            seg = pts[:max(2, len(pts) // 2)]
            ops.append(Op("line", seg, hex_color(darken(region.color, 0.55)), 2,
                          detail_layer, phase="outline", cost=1.0, note="impasto-form"))
            used_outline += 1.0
    return ops


def trace_brush_stroke(gx, gy, blurred, start_x, start_y, radius, rng):
    h, w = gx.shape
    points = [(start_x, start_y)]
    x, y = start_x, start_y
    angle = rng.uniform(0, math.pi * 2)
    steps = max(3, int(radius * 1.8))
    for _ in range(steps):
        ix = int(max(0, min(w - 1, x)))
        iy = int(max(0, min(h - 1, y)))
        gxv = float(gx[iy, ix])
        gyv = float(gy[iy, ix])
        if abs(gxv) + abs(gyv) > 2.0:
            tangent = math.atan2(gxv, -gyv)  # perpendicular to gradient
            # keep direction continuous
            while tangent - angle > math.pi / 2:
                tangent -= math.pi
            while angle - tangent > math.pi / 2:
                tangent += math.pi
            angle = tangent + rng.uniform(-0.18, 0.18)
        else:
            angle += rng.uniform(-0.25, 0.25)
        step = max(1.2, radius * 0.42)
        x += math.cos(angle) * step
        y += math.sin(angle) * step
        if x < 0 or y < 0 or x >= w or y >= h:
            break
        points.append((x, y))
    return points


def transform_rect(size, transform):
    w, h = size
    return [transform(0, 0), transform(w, 0), transform(w, h), transform(0, h)]


# ---------------------------------------------------------------------------
# style: watercolor
# ---------------------------------------------------------------------------

def build_watercolor(analysis: Analysis, budget: Budget, args, transform) -> list[Op]:
    w, h = analysis.width, analysis.height
    k = args.seg_k if args.seg_k else (1000 if budget.units < 900 else 800)
    subject, regions = build_subject_mask(analysis, k=k, min_size=args.seg_min_size)
    layers = budget.layers
    wash_layer = 1 if layers >= 2 else 0
    detail_layer = 2 if layers >= 3 else wash_layer
    ops: list[Op] = []
    rng = random.Random(args.seed)
    used = 0.0

    # pale but saturated washes; keep the paper visible at edges
    candidates = [r for r in regions if not r.background and r.area >= w * h * 0.0012]
    candidates += [r for r in regions if (not r.background and r.edge_density > 0.22
                                          and r.area > w * h * 0.0004)]
    seen = set()
    selected = []
    for region in sorted(candidates, key=lambda r: (0 if r.edge_density > 0.2 else 1, -r.area)):
        key = (int(region.seed[0]) << 16) ^ int(region.seed[1]) ^ int(region.area)
        if key in seen:
            continue
        seen.add(key)
        selected.append(region)
        wash_cap = 64 if budget.units < 800 else (96 if budget.units < 1500 else 128)
        if len(selected) >= min(args.max_regions, wash_cap):
            break

    wash_budget = budget.units * 0.48
    for region in selected:
        if used >= wash_budget:
            break
        color = region.color
        if max(color) > 248 and min(color) > 242:
            continue
        mask = cv2.erode(region.mask, np.ones((3, 3), np.uint8), iterations=1)
        if np.count_nonzero(mask) < 24:
            mask = region.mask
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        approx = simplify_contour(max(contours, key=cv2.contourArea), 1.6)
        if len(approx) < 3:
            continue
        pts = [transform(float(p[0]), float(p[1])) for p in approx]
        pts = [[p[0] + rng.uniform(-0.0015, 0.0015), p[1] + rng.uniform(-0.0015, 0.0015)] for p in pts]
        ops.append(Op("fill", closed(pts), hex_color(mix_white(color, 0.10)), 1,
                      wash_layer, phase="wash", cost=2.0, note="watercolor-wash"))
        used += 2.0

    # shadow hatching
    hatch_budget = budget.units * 0.20
    used_hatch = 0.0
    spacing_factor = 1.45 if budget.units < 800 else (1.1 if budget.units < 1500 else 0.85)
    spacing = max(6, int(min(w, h) * 0.022 * spacing_factor))
    dark_cutoff = 170 if budget.units < 800 else (184 if budget.units < 1500 else 198)
    skip_chance = 0.45 if budget.units < 800 else (0.34 if budget.units < 1500 else 0.24)
    for y in range(0, h, spacing):
        for x in range(0, w, spacing):
            if used_hatch >= hatch_budget:
                break
            jx = x + rng.randint(-spacing // 2, spacing // 2)
            jy = y + rng.randint(-spacing // 2, spacing // 2)
            if jx < 0 or jy < 0 or jx >= w or jy >= h or not subject[jy, jx]:
                continue
            if analysis.gray[jy, jx] > dark_cutoff or rng.random() < skip_chance:
                continue
            angle = math.radians(46 + rng.uniform(-10, 10))
            length = rng.uniform(5, 11) * max(0.8, min(w, h) / 500.0)
            pts = [transform(jx, jy), transform(jx + math.cos(angle) * length,
                                               jy + math.sin(angle) * length)]
            color = darken(color_at(analysis.image, jx, jy), 0.20)
            ops.append(Op("line", pts, hex_color(color), 1, detail_layer,
                          phase="hatch", cost=0.55, note="watercolor-hatch"))
            used_hatch += 0.55

    # The same centerline tracer is used here; region boundaries are not ink.
    ops += centerline_ops(analysis, budget.units * 0.22, transform,
                          detail_layer, size=1)
    return ops


# ---------------------------------------------------------------------------
# planning / preview
# ---------------------------------------------------------------------------

def resolve_colour(client, args):
    """Decide between brush and bucket colouring and read the layer set-up of the plugin."""
    import layers as layer_map
    want = getattr(args, "colour", "auto")
    if want == "bucket":
        args.layer_roles = None
        return
    info, reachable = layer_map.probe(client)
    if not info and args.dry_run and not reachable:
        # offline planning (no game): assume the layers the current plugin builds
        info = {"virtual": args.virtual_layers, "bands": 3, "nativePatch": True}
    ok = layer_map.usable(info)
    if want == "brush" and not ok:
        print("virtual layers are not available (plugin too old, or the game was not restarted); "
              "colouring with the bucket instead", file=sys.stderr)
    args.colour = "brush" if ok else "bucket"
    args.layer_roles = layer_map.assign(info["virtual"]) if ok else None
    args.layer_bands = int((info or {}).get("bands", 3) or 3)
    args.native_layers = bool((info or {}).get("nativePatch", False)) if ok else True


def build_ops(style: str, analysis: Analysis, budget: Budget, args, transform) -> list[Op]:
    if style == "anime_study":
        from anime_study import build_study
        return build_study(analysis, budget, args, transform)
    if style == "anime_human":
        from anime_human import build_human
        return build_human(analysis, budget, args, transform)
    if style == "sketch":
        return build_sketch(analysis, budget, args, transform)
    if style == "anime_flat":
        return build_anime_flat(analysis, budget, args, transform)
    if style == "impasto":
        return build_impasto(analysis, budget, args, transform)
    if style == "watercolor":
        return build_watercolor(analysis, budget, args, transform)
    raise ValueError(f"unknown style {style}")


def build_grid_ops(image, styles: list[str], base_budget: Budget, args,
                   canvas_aspect: float, lineart_edges=None):
    """Four-cell sheet: the same image rendered in four styles, one per cell."""
    share = max(10.0, base_budget.units / 4.0)
    cell_budget = Budget(seconds=base_budget.seconds,
                         usable_seconds=base_budget.usable_seconds,
                         units=share,
                         online=base_budget.online,
                         source=base_budget.source,
                         layers=base_budget.layers)
    # dividers sit outside the cell margins, so fills never cover them
    ops = [
        Op("line", [[0.5, 0.025], [0.5, 0.975]], "#9AA0A6", 2, 0, phase="grid", cost=0.0),
        Op("line", [[0.025, 0.5], [0.975, 0.5]], "#9AA0A6", 2, 0, phase="grid", cost=0.0),
    ]
    stats = []
    for style, cell in zip(styles, GRID_CELLS):
        analysis = make_analysis(image, style, args, lineart_edges)
        transform, fit, _ = make_region_transform(analysis.width, analysis.height,
                                                  canvas_aspect, cell, args.margin)
        part = build_ops(style, analysis, cell_budget, args, transform)
        ops.extend(part)
        stats.append((style, fit, len(part), summarize_ops(part)))
    return ops, stats, cell_budget


def summarize_ops(ops: list[Op]):
    counts = {}
    cost = 0.0
    for op in ops:
        counts[op.kind] = counts.get(op.kind, 0) + 1
        cost += op.cost
    return counts, cost


def render_preview(ops: list[Op], canvas_aspect: float, output: str, width=1280,
                   canvas_width=None):
    height = int(round(width / canvas_aspect))
    # No extra inset: the plan already includes the configured margin, and the
    # game draws the normalized plan edge-to-edge. An 18 px preview border made
    # the preview ~3% smaller and misaligned with the real canvas.
    supersample = 3
    image = Image.new("RGB", (width * supersample, height * supersample), "white")
    draw = ImageDraw.Draw(image)
    fit_w = min(width, height * canvas_aspect)
    fit_h = fit_w / canvas_aspect
    ox, oy = (width - fit_w) / 2, (height - fit_h) / 2
    world_width = canvas_width or canvas_aspect * 10.0

    def point(p):
        return ((ox + p[0] * fit_w) * supersample,
                (oy + (1.0 - p[1]) * fit_h) * supersample)

    def thickness(op):
        return max(1, int(round(op.size * fit_w / (100 * world_width) * supersample)))

    # Approximation only: native sorting-layer assets and real-fill topology
    # aren't available here. A preview is not an in-game screenshot.
    # Higher layers draw on top; inside a layer the plan order is kept.
    ops = sorted(ops, key=lambda o: (o.layer, o.vlayer % 3 if o.vlayer is not None else 0))
    for op in ops:
        if op.kind == "fill" and len(op.points) >= 3:
            polygon = [point(p) for p in op.points]
            draw.polygon(polygon, fill=op.color)
            # The game also strokes the fill outline with the same colour.
            draw.line(polygon + [polygon[0]], fill=op.color,
                      width=max(1, thickness(op)), joint="curve")
        elif op.kind == "realfill" and op.fallback:
            draw.polygon([point(p) for p in op.fallback], fill=op.color)

    for op in ops:
        if op.kind == "line" and len(op.points) >= 2:
            draw.line([point(p) for p in op.points], fill=op.color,
                      width=thickness(op), joint="curve")
            # Study strokes get round cap dabs in the game; mirror that here.
            if op.size >= 8 and op.note.startswith("study-"):
                r = thickness(op) / 2
                for cap in (op.points[0], op.points[-1]):
                    x, y = point(cap)
                    draw.ellipse([x - r, y - r, x + r, y + r], fill=op.color)
        elif op.kind == "disc" and op.points:
            x, y = point(op.points[0])
            r = thickness(op) / 2
            draw.ellipse([x - r, y - r, x + r, y + r], fill=op.color)

    image.resize((width, height), Image.Resampling.LANCZOS).save(output)
    return output


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

def execute_plan(client: BridgeClient, ops: list[Op], budget: Budget, args) -> dict:
    if args.style in STUDY_STYLES and not args.grid:
        from anime_study import execute_study
        return execute_study(client, ops, budget, args)
    state = client.state()
    if not state.get("available"):
        raise BridgeError(state.get("hint", "no drawing module"))
    if not state.get("canDraw") and not args.force:
        raise BridgeError("The game reports canDraw=false. Use --force to try anyway.")

    max_layer = max(0, int((state.get("layers") or {}).get("total", 1)) - 1)
    start = time.monotonic()
    # The budget clock started when the budget was read, so planning time that
    # has already passed is not available a second time.
    anchor = getattr(args, "budget_anchor", start)
    deadline = anchor + budget.usable_seconds
    deadline = pacing.tighten_deadline(client, args, deadline, start)
    instant = getattr(args, "pace", "slow") == "instant"
    applied = failed = real_fills = fallbacks = sent = 0
    current_layer = None
    progress_path = getattr(args, "progress_file", None)
    write_progress(progress_path, status="running", sent=0, ops=len(ops),
                   applied=0, failed=0, elapsed=0.0)

    if args.clear:
        client.clear(force=args.force)

    # Group the plan into send units that keep its order: runs of drawable ops
    # on one layer, and every real fill on its own.
    batch_cap = max(1, int(args.batch))
    drawable = sum(1 for op in ops if op.kind != "realfill")
    if not instant and drawable:
        # about 0.5 s of drawing per request keeps slow mode looking steady
        wanted = max(1, int(drawable / max(1.0, budget.usable_seconds / 0.5)))
        batch_cap = min(batch_cap, wanted)
    units = []
    for op in ops:
        layer = max(0, min(max_layer, op.layer))
        if op.kind == "realfill":
            units.append(("realfill", layer, [op]))
        elif units and units[-1][0] == "draw" and units[-1][1] == layer and len(units[-1][2]) < batch_cap:
            units[-1][2].append(op)
        else:
            units.append(("draw", layer, [op]))

    latency = 0.25          # measured per request; starts pessimistic
    def spend(unit):
        return len(unit[2]) if unit[0] == "draw" else 1

    for position, (kind, layer, group) in enumerate(units):
        now = time.monotonic()
        if now + latency * pacing.LATENCY_MARGIN > deadline:
            print(f"  out of time after {sent} ops; stopping")
            break
        if current_layer != layer:
            try:
                client.layer(layer)
                current_layer = layer
            except Exception:
                pass
        t0 = time.monotonic()
        if kind == "realfill":
            op = group[0]
            try:
                client.color(op.color)
                result = client.fill(op.points, color=op.color, force=args.force)
                if int(result.get("filled", 0)):
                    applied += 1
                    real_fills += 1
                else:
                    raise BridgeError("fill reported nothing filled")
            except Exception as exc:
                print(f"  real fill failed: {exc}", file=sys.stderr)
                failed += 1
                if op.fallback:
                    try:
                        client.draw_strokes([{"type": "fill", "color": op.color,
                                              "points": op.fallback}],
                                            clear=False, force=args.force)
                        fallbacks += 1
                        applied += 1
                        failed -= 1
                    except Exception:
                        pass
        else:
            strokes = [{"type": "line" if op.kind == "line" else ("fill" if op.kind == "fill" else "disc"),
                        "color": op.color, "size": op.size, "points": op.points} for op in group]
            try:
                result = client.draw_strokes(strokes, clear=False, force=args.force)
                applied += int(result.get("applied", 0))
                failed += int(result.get("failed", 0))
            except Exception as exc:
                failed += len(group)
                print(f"  batch failed: {exc}", file=sys.stderr)
        took = time.monotonic() - t0
        latency = 0.6 * latency + 0.4 * took
        sent += len(group)
        write_progress(progress_path, status="running", sent=sent, ops=len(ops),
                       applied=applied, failed=failed,
                       elapsed=round(time.monotonic() - start, 2))
        if instant:
            continue
        # Slow mode: spread the remaining sends over the remaining time, keeping
        # the time they themselves need. Late -> the gap shrinks towards zero.
        left_units = len(units) - position - 1
        if left_units <= 0:
            break
        spare = deadline - time.monotonic() - left_units * latency * pacing.LATENCY_MARGIN
        gap = max(0.0, min(pacing.MAX_DELAY, spare / left_units * 0.9))
        time.sleep(min(gap, max(0.0, deadline - time.monotonic())))

    elapsed = time.monotonic() - start
    result = {
        "ops": len(ops),
        "applied": applied,
        "failed": failed,
        "real_fills": real_fills,
        "fallbacks": fallbacks,
        "elapsed": round(elapsed, 2),
    }
    write_progress(progress_path,
                   status="done" if sent >= len(ops) else "stopped",
                   sent=sent, ops=len(ops), applied=applied,
                   failed=failed, elapsed=result["elapsed"])
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="Time-budgeted painter renderer for Draw & Guess Bridge")
    parser.add_argument("image", help="input image (PNG/JPG/WebP)")
    parser.add_argument("--style", default="anime_flat",
                        choices=["sketch", "anime_flat", "anime_study", "anime_human", "impasto", "watercolor"])
    parser.add_argument("--pressure", dest="human_pressure", type=float, default=1.0,
                        help="anime_human style: multiply the simulated pen pressure (wider/narrower lines)")
    parser.add_argument("--pen", choices=["auto", "direct", "native"], default="auto",
                        help="anime_human style: 'auto' uses the virtual pen when the game has pen pressure "
                             "switched on and sends finished strokes otherwise; 'direct' sends finished strokes; 'native' replays each "
                             "stroke through a virtual pen tablet so the game's own pressure code "
                             "runs (needs the game focused or its background input enabled)")
    parser.add_argument("--colour", "--color-mode", dest="colour", choices=["auto", "brush", "bucket"],
                        default="auto",
                        help="anime_human style: colour with the brush on virtual layers under the line art "
                             "('brush'), or with the paint bucket ('bucket'); auto = brush when the plugin "
                             "offers enough layers")
    parser.add_argument("--borders", choices=["on", "off"], default="on",
                        help="anime_human style, brush colouring: after the colour, trace a dark border "
                             "round each colour area where the line art has none ('on'), or leave the "
                             "colour edges as they are ('off'); the bucket always needs its borders")
    parser.add_argument("--virtual-layers", type=int, default=9,
                        help="layers assumed when planning offline (--dry-run, --plan-in); a live run asks the plugin")
    parser.add_argument("--soften-frame", choices=["auto", "on", "off"], default="auto",
                        help="a figure cut off by the image frame fades out instead of ending in a "
                             "straight line (auto: on for anime_human, off for anime_study)")
    parser.add_argument("--game-fps", type=float, default=240.0,
                        help="anime_human style: the game's frame rate (tools/probe_pen.py prints it); "
                             "pen pressure is read once per frame, so it sets which part of the "
                             "stroke decides the width")
    parser.add_argument("--skill", dest="human_skill", type=float, default=2.3,
                        help="anime_human style: >1 draws faster and more fluently, <1 slower")
    parser.add_argument("--grid", action="store_true",
                        help="draw a 2x2 sheet with one style per cell (uses --grid-styles)")
    parser.add_argument("--grid-styles", default=",".join(GRID_STYLES),
                        help="four comma-separated styles in order: top-left, top-right, "
                             "bottom-left, bottom-right")
    parser.add_argument("--time", type=float, default=None,
                        help="remaining seconds; default: read the live timer, else --default-time")
    parser.add_argument("--default-time", type=float, default=180.0,
                        help="fallback seconds when no round timer exists (Sketchbook)")
    parser.add_argument("--budget", type=float, default=None,
                        help="override weighted operation budget (units)")
    parser.add_argument("--rate", type=float, default=None,
                        help="operation units per second; default 65 offline / 11 online")
    parser.add_argument("--colors", type=int, default=None,
                        help="palette size; flat defaults to 16/24/40 with budget; "
                             "study 3-8 by remaining time (cap 10), sketch 8, "
                             "impasto 24, water 8")
    parser.add_argument("--max-strokes", type=int, default=None)
    parser.add_argument("--max-regions", type=int, default=600)
    parser.add_argument("--seg-k", type=int, default=None,
                        help="graph segmentation k (higher = fewer, larger regions)")
    parser.add_argument("--seg-min-size", type=int, default=60)
    parser.add_argument("--line-color", default=None, help="anime-flat outline color")
    parser.add_argument("--fill-mode", default="auto", choices=["auto", "real", "polygon"])
    parser.add_argument("--fill-white", action="store_true", help="also fill near-white regions")
    parser.add_argument("--base-fill", action=argparse.BooleanOptionalAction, default=True,
                        help="impasto base wash")
    parser.add_argument("--outlines", action=argparse.BooleanOptionalAction, default=True,
                        help="add outline strokes to impasto")
    parser.add_argument("--margin", type=float, default=0.04)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-dim", type=int, default=None,
                        help="longest side of the working image; default: chosen from the "
                             "time left (480-960) so planning stays fast on large photos")
    parser.add_argument("--clear", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--study-pause", type=float, default=0.26,
                        help="anime_study minimum pause between individual strokes (seconds)")
    parser.add_argument("--study-speed", type=float, default=0.90,
                        help="anime_study extra seconds per normalized path length")
    parser.add_argument("--pace", default="slow", choices=["slow", "instant"],
                        help="slow (default): draw like a person, spacing strokes over the "
                             "available time and speeding up when late; instant: send "
                             "everything back to back")
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--region", default=None,
                        help="draw inside a normalized canvas rect x,y,w,h (y-up); "
                             "default: fit the whole canvas")
    parser.add_argument("--plan-in", default=None,
                        help="execute/render a plan written by --plan-out instead of planning")
    parser.add_argument("--plan-progress-file", default=None,
                        help="write JSON planning steps (model inference, tracing, ...) for local UIs")
    parser.add_argument("--progress-file", default=None,
                        help="write JSON progress while executing (for local UIs)")
    parser.add_argument("--study-stage-dir", default=None,
                        help="write anime_study sketch/color/shade/detail cumulative previews")
    parser.add_argument("--preview", default=None, help="write a preview PNG before executing")
    parser.add_argument("--plan-out", default=None, help="write the planned operations as JSON")
    parser.add_argument("--dry-run", action="store_true", help="only analyze/plan/preview")
    parser.add_argument("--url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--canvas-aspect", type=float, default=None,
                        help="override canvas aspect ratio for preview/planning")
    parser.add_argument("--lineart", default="auto", choices=["auto", "off", "anime2sketch"],
                        help="line extraction: auto uses Anime2Sketch when the model exists")
    parser.add_argument("--lineart-model", default="models/anime2sketch/netG.pth",
                        help="Anime2Sketch checkpoint path")
    parser.add_argument("--lineart-device", default="auto", choices=["auto", "cuda", "mps", "cpu"])
    parser.add_argument("--lineart-threshold", type=int, default=45,
                        help="line darkness threshold (higher = fewer, darker lines)")
    return parser


def main(argv=None):
    args = None
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles default to cp1252
        except (AttributeError, ValueError, OSError):
            pass
    try:
        return _main(argv)
    except SystemExit:
        planlog.finish(False, 'cancelled')
        raise
    except Exception as exc:
        planlog.finish(False, str(exc) or exc.__class__.__name__)
        raise


def _round_seconds(client, args):
    """Seconds the round has left, else the default; never raises."""
    if not args.dry_run:
        try:
            recommended = (client.round() or {}).get("recommended") or {}
            remaining = float(recommended.get("remaining") or 0)
            if remaining > 0:
                return remaining
        except Exception:
            pass
    return float(args.default_time)


def _main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.study_pause < 0 or args.study_speed < 0:
        raise SystemExit("study timing parameters must be nonnegative")
    region = None
    if args.region:
        try:
            region = parse_region(args.region)
        except ValueError as exc:
            parser.error(str(exc))
    if args.grid and region:
        parser.error("--region cannot be combined with --grid")
    if args.grid and args.plan_in:
        parser.error("--plan-in cannot be combined with --grid")
    random.seed(args.seed)
    np.random.seed(args.seed)
    if args.plan_in:
        steps = [("plan", "读取计划"), ("preview", "渲染预览")]
    elif args.style in STUDY_STYLES and not args.grid:
        steps = [("bridge", "读取回合时间"), ("load", "读取图片"),
                 ("lineart", "Anime2Sketch 线稿推理"), ("segment", "anime-seg 主体分割"),
                 ("lines", "整理线稿笔画"), ("palette", "提取调色板"),
                 ("fills", "规划填色多边形"), ("shade", "规划阴影笔触"),
                 ("fit", "按时间裁剪计划"), ("preview", "渲染预览")]
    else:
        steps = [("bridge", "读取回合时间"), ("load", "读取图片"),
                 ("lineart", "Anime2Sketch 线稿推理"), ("plan", "规划笔画"),
                 ("preview", "渲染预览")]
    planlog.start(getattr(args, "plan_progress_file", None), steps)

    client = BridgeClient(args.url) if args.url else BridgeClient()
    if args.token:
        client.token = args.token

    if not args.plan_in:
        planlog.stage("bridge", detail="offline" if args.dry_run else "")
    try:
        state = {} if args.dry_run else client.state()
    except Exception:
        if not args.dry_run:
            raise
        state = {}
        print("bridge unavailable; planning offline", file=sys.stderr)
    canvas = state.get("canvas") or {}
    canvas_aspect = args.canvas_aspect or float(canvas.get("aspect") or 16 / 9)
    args.canvas_world_width = float(canvas.get("width") or canvas_aspect * 10.0)
    study_budget = None
    if args.style == "anime_human" and not args.plan_in:
        resolve_colour(client, args)
    if args.style in STUDY_STYLES and not args.grid:
        planning_start = time.monotonic()
        study_budget = resolve_budget(client, args.style, args)
        args.study_deadline = planning_start + study_budget.usable_seconds

    plan_ops = None
    if args.plan_in:
        planlog.stage("plan", detail=str(args.plan_in))
        try:
            plan_ops = load_plan(args.plan_in)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"could not load plan: {exc}", file=sys.stderr)
            return 1
        if not plan_ops:
            print(f"plan is empty: {args.plan_in}", file=sys.stderr)
            return 1
        image = None
    else:
        planlog.stage("load", detail=str(args.image))
        auto_dim = args.max_dim is None
        if auto_dim:
            # Planning time is part of the round: a long round affords a bigger
            # working image, a short one gets a small, fast one.
            horizon = (args.time if args.time is not None
                       else (study_budget.seconds if study_budget is not None else None))
            if horizon is None:
                horizon = _round_seconds(client, args)
            args.max_dim = imagesize.auto_max_dim(horizon, args.style)
        image = load_image(args.image, args.max_dim)
        try:
            with Image.open(args.image) as header:      # size only, no decode
                original = header.size
        except OSError:
            original = (image.shape[1], image.shape[0])
        info = imagesize.describe(original, (image.shape[1], image.shape[0]),
                                  args.max_dim, auto_dim)
        print(f"image: {info}")
        planlog.note(info)

    lineart_edges = None
    if image is not None and args.lineart != "off":
        model_path = Path(args.lineart_model)
        if not model_path.is_absolute():
            model_path = Path(__file__).resolve().parent.parent / model_path
        if args.lineart == "anime2sketch" or model_path.exists():
            planlog.stage("lineart", detail="Anime2Sketch")
            if not model_path.exists():
                print(f"lineart model not found: {model_path}", file=sys.stderr)
                return 1
            try:
                lineart_edges = anime2sketch_edges(image, str(model_path),
                                                   args.lineart_device,
                                                   args.lineart_threshold)
                print(f"lineart: Anime2Sketch  device={resolve_device(args.lineart_device)}  "
                      f"threshold={args.lineart_threshold}")
            except Exception as exc:
                if args.lineart == "anime2sketch":
                    print(f"lineart failed: {exc}", file=sys.stderr)
                    return 1
                print(f"lineart: Anime2Sketch unavailable ({exc}); using classical edges")
                planlog.note("unavailable, using classical edges")
    else:
        planlog.skip("lineart", "off")

    if args.grid:
        styles = [s.strip() for s in args.grid_styles.split(",") if s.strip()]
        invalid = [s for s in styles if s not in GRID_STYLES]
        if len(styles) != 4 or invalid:
            print(f"--grid needs exactly four valid styles, got: {args.grid_styles}",
                  file=sys.stderr)
            return 2
        # the largest style cap gives the total sheet budget; cells split it evenly
        budget = resolve_budget(client, max(styles, key=lambda s: STYLE_MAX_UNITS[s]), args)
        print(f"image: {image.shape[1]}x{image.shape[0]}  style=grid  "
              f"cells={','.join(styles)}")
        print(f"canvas aspect={canvas_aspect:.3f}  grid=2x2  {budget.summary()}")
        ops, grid_stats, cell_budget = build_grid_ops(image, styles, budget, args,
                                                      canvas_aspect, lineart_edges)
        print(f"grid budget: {cell_budget.units:.0f} units per cell")
        for style, fit, n, (counts, cost) in grid_stats:
            print(f"  {style:<10} fit={fit[0]:.3f}x{fit[1]:.3f}  "
                  f"{n} ops  cost={cost:.1f}/{cell_budget.units:.0f}  {counts}")
    elif plan_ops is not None:
        budget = study_budget or resolve_budget(client, args.style, args)
        ops = plan_ops
        print(f"plan: {args.plan_in}  style={args.style}  ops={len(ops)}")
        print(f"canvas aspect={canvas_aspect:.3f}  {budget.summary()}")
        if study_budget is not None:
            from anime_study import fit_plan, plan_seconds
            if args.style != "anime_human":      # the hand plan already fits its own time budget
                ops = fit_plan(ops, max(0, args.study_deadline - time.monotonic()),
                               args, budget.units)
            print(f"study: loaded {len(ops)} ops  "
                  f"estimated execution={plan_seconds(ops, args):.2f}s")
    else:
        budget = study_budget or resolve_budget(client, args.style, args)
        analysis = make_analysis(image, args.style, args, lineart_edges)
        if region:
            transform, fit, rect = make_region_transform(
                analysis.width, analysis.height, canvas_aspect,
                cell=region, margin=args.margin)
        else:
            transform, fit, rect = make_transform(analysis.width, analysis.height,
                                                   canvas_aspect, args.margin)
        colors = (flat_palette_size(budget, args) if args.style == "anime_flat"
                  else args.colors or STYLE_DEFAULT_COLORS[args.style])
        print(f"image: {analysis.width}x{analysis.height}  style={args.style}  "
              f"colors={colors}")
        print(f"canvas aspect={canvas_aspect:.3f}  fit={fit[0]:.3f}x{fit[1]:.3f}  "
              f"{budget.summary()}")
        if region:
            print(f"region: x={region[0]:.3f} y={region[1]:.3f} "
                  f"w={region[2]:.3f} h={region[3]:.3f}")
        if args.style not in STUDY_STYLES:
            planlog.stage("plan", detail=args.style)
        ops = build_ops(args.style, analysis, budget, args, transform)
        if study_budget is not None:
            planlog.stage("fit", detail=f"{len(ops)} ops")
            from anime_study import fit_plan, plan_seconds
            if args.style != "anime_human":      # the hand plan already fits its own time budget
                ops = fit_plan(ops, max(0, args.study_deadline - time.monotonic()),
                               args, budget.units)
            print(f"study: planning={time.monotonic() - planning_start:.2f}s "
                  f"estimated execution={plan_seconds(ops, args):.2f}s "
                  f"palette={len(set(o.color for o in ops))}")

    counts, cost = summarize_ops(ops)
    print(f"planned: {len(ops)} ops  cost={cost:.1f}/{budget.units:.1f} units  {counts}")

    if args.plan_out:
        Path(args.plan_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.plan_out).write_text(json.dumps([op.to_json() for op in ops], ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"plan written: {args.plan_out}")

    if args.preview:
        planlog.stage("preview", detail=f"{len(ops)} ops")
        Path(args.preview).parent.mkdir(parents=True, exist_ok=True)
        if args.style == "anime_human" or any(o.kind == "bucket" for o in ops):
            from anime_human import simulate
            picture, bucket_report = simulate(
                ops, canvas_aspect, float(canvas.get("width") or canvas_aspect * 10.0),
                bands=int(getattr(args, "layer_bands", 3) or 3))
            Image.fromarray(picture).save(args.preview)
            leaks = sum(1 for r in bucket_report if r.get("leaked"))
            print(f"bucket simulation: {len(bucket_report)} pours, {leaks} predicted leaks")
        else:
            render_preview(ops, canvas_aspect, args.preview,
                           canvas_width=float(canvas.get("width") or canvas_aspect * 10.0))
        print(f"preview written: {args.preview}")

    if args.study_stage_dir and study_budget is not None:
        directory = Path(args.study_stage_dir)
        directory.mkdir(parents=True, exist_ok=True)
        phases = ("sketch", "color", "shade", "detail")
        for i, phase in enumerate(phases):
            render_preview([o for o in ops if o.phase in phases[:i + 1]],
                           canvas_aspect, str(directory / f"{i + 1}-{phase}.png"),
                           canvas_width=float(canvas.get("width") or canvas_aspect * 10.0))

    planlog.finish(True)
    if args.dry_run:
        return 0

    if not ops:
        print("nothing to draw", file=sys.stderr)
        return 1

    if not state.get("available"):
        print(f"game is not in a drawable state: {state.get('hint', '')}", file=sys.stderr)
        return 1

    print("drawing...")
    result = execute_plan(client, ops, budget, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
