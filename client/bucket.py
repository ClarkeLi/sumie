"""Offline model of the game's paint bucket (ShapeFiller + CollisionMatrix).

The game fills the region around a seed point that is closed by existing lines.
Polylines block on a grid of 0.02 world units, as a wall of half the brush width
times 0.2 on each side; discs do not block at all; fill polygons add their edges.
This module rasterises the planned strokes onto the same grid, so the planner can
see which regions a bucket would fill, which ones leak to the outside, and in
what order strokes close a region.

Coordinates are normalised canvas coordinates (0..1, y up), like Op.points.
"""
from __future__ import annotations

import cv2
import numpy as np

DENSITY = 0.02            # world units per collision cell (desktop)
WALL = 0.2                # blocked half-width = brush size / 100 * WALL


class Grid:
    def __init__(self, world_w, world_h, density=DENSITY):
        self.world_w = float(world_w)
        self.world_h = float(world_h)
        self.density = density
        self.W = int(np.ceil(self.world_w / density)) + 1
        self.H = int(np.ceil(self.world_h / density)) + 1

    def to_cells(self, points):
        """Normalised (y up) -> cell coordinates (row 0 = top, for OpenCV)."""
        p = np.asarray(points, float)
        x = p[:, 0] * self.world_w / self.density
        y = (1.0 - p[:, 1]) * self.world_h / self.density
        return np.column_stack((x, y))

    def to_normalised(self, cells):
        c = np.asarray(cells, float)
        return np.column_stack((c[:, 0] * self.density / self.world_w,
                                1.0 - c[:, 1] * self.density / self.world_h))


GAME_SIMPLIFY = 0.01       # world units: the game simplifies every line it takes in (LineRenderSimplify)


def _game_simplify(cells, eps):
    """Ramer-Douglas-Peucker like Unity's LineUtility.Simplify; closed loops are split in two
    first (a polyline whose ends meet has no chord to measure against)."""
    cells = np.asarray(cells, np.float32)
    if len(cells) < 4:
        return cells
    def rdp(part):
        return cv2.approxPolyDP(part.reshape(-1, 1, 2), eps, False).reshape(-1, 2)
    if float(np.hypot(*(cells[0] - cells[-1]))) < max(2.0, 2 * eps):
        far = int(np.argmax(np.linalg.norm(cells - cells[0], axis=1)))
        return np.vstack((rdp(cells[:far + 1]), rdp(cells[far:])[1:]))
    return rdp(cells)


def stroke_wall(grid, mask, points, size):
    """Draw the blocking wall of one line into `mask` (255 = blocked)."""
    if len(points) < 2:
        return
    cells = _game_simplify(grid.to_cells(points), GAME_SIMPLIFY / grid.density)    # as the game stores it
    cells = np.round(cells).astype(np.int32).reshape(-1, 1, 2)
    width = max(1, int(round(2.0 * size / 100.0 * WALL / grid.density)))
    cv2.polylines(mask, [cells], False, 255, width, cv2.LINE_8)


def barrier(grid, ops, upto=None):
    """Blocked cells for the line ops (list order = drawing order)."""
    mask = np.zeros((grid.H, grid.W), np.uint8)
    for op in ops[:upto]:
        if op.kind == "line":
            stroke_wall(grid, mask, op.points, op.size)
    return mask


def free_cells(blocked):
    """Labelled free regions (4-connected, as the hull walker cannot cross corners)."""
    free = (blocked == 0).astype(np.uint8)
    count, labels = cv2.connectedComponents(free, connectivity=4)
    return count, labels


def region_at(blocked, seed_cell):
    """Boolean mask of the region a bucket at `seed_cell` (x, y) would fill."""
    x, y = int(seed_cell[0]), int(seed_cell[1])
    if not (0 <= x < blocked.shape[1] and 0 <= y < blocked.shape[0]) or blocked[y, x]:
        return None
    flood = blocked.copy()
    # the mask border must block too, or two regions could meet around the edge
    padded = np.full((flood.shape[0] + 2, flood.shape[1] + 2), 255, np.uint8)
    padded[1:-1, 1:-1] = flood
    cv2.floodFill(np.zeros_like(flood), padded, (x, y), 1, flags=4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
    region = (padded[1:-1, 1:-1] == 255) & (flood == 0)
    return region


def touches_border(region):
    return bool(region[0].any() or region[-1].any() or region[:, 0].any() or region[:, -1].any())


def seed_point(region):
    """Cell farthest from the region's boundary (inscribed-circle centre)."""
    dist = cv2.distanceTransform(region.astype(np.uint8), cv2.DIST_L2, 3)
    y, x = np.unravel_index(int(np.argmax(dist)), dist.shape)
    return (int(x), int(y)), float(dist[y, x])


def plug_leak(blocked, seed, box, pad=24):
    """Find where the fill from `seed` escapes its area and return a short plug line.

    `box` = (x0, y0, x1, y1) of the area the fill is meant to stay in. The escape
    route is the cheapest path (wide corridors cost less) from the seed to the edge
    of that box plus `pad` cells; the plug crosses the narrowest point of the route
    between the nearest walls on both sides. Returns ((x1, y1), (x2, y2)) in cell
    coordinates, or None if the fill is closed or no wall is near the neck.
    """
    import heapq
    H, W = blocked.shape
    x0, y0, x1, y1 = box
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(W - 1, x1 + pad), min(H - 1, y1 + pad)
    crop = blocked[y0:y1 + 1, x0:x1 + 1]
    free = crop == 0
    h, w = free.shape
    sx, sy = int(seed[0]) - x0, int(seed[1]) - y0
    if not (0 <= sx < w and 0 <= sy < h) or not free[sy, sx]:
        return None
    wide = cv2.distanceTransform(free.astype(np.uint8), cv2.DIST_L2, 3)
    cost = 1.0 / (wide + 0.3)
    best = np.full((h, w), np.inf)
    best[sy, sx] = 0.0
    parent = {}
    heap = [(0.0, sx, sy)]
    end = None
    while heap:
        d, x, y = heapq.heappop(heap)
        if d > best[y, x]:
            continue
        if x in (0, w - 1) or y in (0, h - 1):
            end = (x, y)
            break
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and free[ny, nx]:
                nd = d + cost[ny, nx]
                if nd < best[ny, nx]:
                    best[ny, nx] = nd
                    parent[(nx, ny)] = (x, y)
                    heapq.heappush(heap, (nd, nx, ny))
    if end is None:
        return None
    route = [end]
    while route[-1] in parent:
        route.append(parent[route[-1]])
    neck = min(route, key=lambda p: wide[p[1], p[0]])
    px, py = neck
    reach = int(wide[py, px]) + 8
    ya, yb = max(0, py - reach), min(h, py + reach + 1)
    xa, xb = max(0, px - reach), min(w, px + reach + 1)
    wy, wx = np.nonzero(~free[ya:yb, xa:xb])
    if len(wx) == 0:
        return None
    wx, wy = wx + xa, wy + ya
    dist = np.hypot(wx - px, wy - py)
    first = int(np.argmin(dist))
    v = np.array([wx[first] - px, wy[first] - py], float)
    opposite = (wx - px) * v[0] + (wy - py) * v[1] <= 0
    if not opposite.any():
        return None
    second = int(np.argmin(np.where(opposite, dist, np.inf)))
    a = np.array([wx[first], wy[first]], float) + (x0, y0)
    b = np.array([wx[second], wy[second]], float) + (x0, y0)
    return a, b


def seed_candidates(region, count=3, spacing=8):
    """Up to `count` seed cells inside `region`, deepest first, spread apart.

    Returns [((x, y), depth)]. A bucket click is only as safe as its distance to the
    nearest wall: the game rounds the click to its own grid, so a seed that sits one
    cell from a wall can land on the wrong side of it. Several candidates let the
    executor try another place when a pour comes out far too small.
    """
    dist = cv2.distanceTransform(region.astype(np.uint8), cv2.DIST_L2, 3)
    out = []
    for _ in range(count):
        y, x = np.unravel_index(int(np.argmax(dist)), dist.shape)
        depth = float(dist[y, x])
        if depth < 1.0:
            break
        out.append(((int(x), int(y)), depth))
        radius = int(max(spacing, 0.9 * depth))
        cv2.circle(dist, (int(x), int(y)), radius, 0, -1)
    return out
