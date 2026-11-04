"""Raster-to-stroke geometry: centerlines, not outlines of line pixels."""
from __future__ import annotations

import numpy as np


def thin_mask(mask):
    """Zhang–Suen thinning; no opencv-contrib or scikit-image dependency."""
    pixels = np.pad((mask > 0).astype(np.uint8), 1)
    while True:
        changed = False
        for step in (0, 1):
            p = pixels[1:-1, 1:-1]
            neighbors = (
                pixels[:-2, 1:-1], pixels[:-2, 2:], pixels[1:-1, 2:],
                pixels[2:, 2:], pixels[2:, 1:-1], pixels[2:, :-2],
                pixels[1:-1, :-2], pixels[:-2, :-2],
            )
            count = sum(neighbors)
            transitions = sum(((neighbors[i] == 0) & (neighbors[(i + 1) % 8] == 1))
                              for i in range(8))
            n, _, e, _, s, _, w, _ = neighbors
            if step == 0:
                corner = (n * e * s == 0) & (e * s * w == 0)
            else:
                corner = (n * e * w == 0) & (n * s * w == 0)
            remove = (p == 1) & (count >= 2) & (count <= 6) & (transitions == 1) & corner
            if np.any(remove):
                p[remove] = 0
                changed = True
        if not changed:
            return pixels[1:-1, 1:-1] * 255


def trace_centerlines(mask, min_length=3.0):
    """Visit each skeleton edge once, splitting at junctions and retaining loops."""
    skeleton = thin_mask(mask)
    ys, xs = np.where(skeleton > 0)
    points = set(zip(xs.tolist(), ys.tolist()))
    adjacency = {}
    for x, y in sorted(points, key=lambda p: (p[1], p[0])):
        neighbors = []
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0),
                       (-1, -1), (1, -1), (1, 1), (-1, 1)):
            q = (x + dx, y + dy)
            if q not in points:
                continue
            # An orthogonal connection already joins this diagonal. Keeping both
            # creates triangles, duplicate strokes and spurious branch points.
            if dx and dy and ((x + dx, y) in points or (x, y + dy) in points):
                continue
            neighbors.append(q)
        adjacency[(x, y)] = neighbors

    visited = set()
    paths = []

    def edge(a, b):
        return (a, b) if a < b else (b, a)

    def walk(start, following):
        path = [start]
        previous, current = start, following
        visited.add(edge(previous, current))
        while True:
            path.append(current)
            if len(adjacency[current]) != 2:
                break
            choices = [q for q in adjacency[current] if q != previous]
            following = choices[0]
            link = edge(current, following)
            if link in visited:
                break
            visited.add(link)
            previous, current = current, following
            if current == start:
                path.append(current)
                break
        array = np.array(path, dtype=np.float32)
        length = float(np.linalg.norm(np.diff(array, axis=0), axis=1).sum())
        if length >= min_length:
            paths.append(array)

    for p, neighbors in adjacency.items():
        if len(neighbors) == 2:
            continue
        for q in neighbors:
            if edge(p, q) not in visited:
                walk(p, q)
    # Components containing no junction/endpoints are closed loops.
    for p, neighbors in adjacency.items():
        for q in neighbors:
            if edge(p, q) not in visited:
                walk(p, q)
    return paths
