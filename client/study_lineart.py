"""Clean line drawing for anime_study.

The raw skeleton of a line-art model is a bundle of short fragments split at
every junction. A person draws a few long, confident curves instead, so this
module joins fragments that continue each other, cuts the result only at real
corners, ranks the strokes by importance and returns them in drawing order:
outline first, then the main inner lines, then fine detail.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

import planlog
from artist_geometry import trace_centerlines


def _length(path):
    return float(np.linalg.norm(np.diff(path, axis=0), axis=1).sum()) if len(path) > 1 else 0.0


def _outward(path, at_start, k=5):
    n = len(path)
    k = min(k, n - 1)
    d = (path[0] - path[k]) if at_start else (path[-1] - path[-1 - k])
    norm = float(np.hypot(*d))
    return d / norm if norm > 1e-6 else d


def _near_pairs(points, radius):
    """All index pairs (i < j) closer than `radius`, via a grid of cell size radius.

    scipy.spatial would do this, but merely importing it starts a pool of
    spinning BLAS threads that burns seconds of CPU for a 0.2 s import.
    """
    cells = {}
    keys = np.floor(points / radius).astype(np.int64)
    for index, (cx, cy) in enumerate(keys.tolist()):
        cells.setdefault((cx, cy), []).append(index)
    out_a, out_b = [], []
    for (cx, cy), members in cells.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                other = cells.get((cx + dx, cy + dy))
                if other is None:
                    continue
                for i in members:
                    for j in other:
                        if j > i:
                            out_a.append(i)
                            out_b.append(j)
    if not out_a:
        return np.empty((0, 2), np.int64)
    a = np.asarray(out_a)
    b = np.asarray(out_b)
    close = np.linalg.norm(points[a] - points[b], axis=1) <= radius
    return np.column_stack((a[close], b[close]))


def merge_paths(paths, max_gap, max_turn_deg=48.0, max_rounds=600):
    """Greedily join end points that continue each other smoothly.

    Only end points closer than `max_gap` can be joined, so the candidate pairs
    come from a grid hash and are scored once, vectorised. They are then taken
    best first from a heap. A join just retires its two end points and links
    the paths; no distance matrix is rebuilt and no point array is copied until
    the final chains are assembled.
    """
    paths = [np.asarray(p, float) for p in paths if len(p) >= 2]
    n = len(paths)
    if n < 2:
        return paths
    cos_limit = math.cos(math.radians(max_turn_deg))
    ends = np.empty((2 * n, 2))
    tans = np.empty((2 * n, 2))
    for i, p in enumerate(paths):
        ends[2 * i], ends[2 * i + 1] = p[0], p[-1]
        tans[2 * i], tans[2 * i + 1] = _outward(p, True), _outward(p, False)
    pairs = _near_pairs(ends, max_gap)
    heap = []
    if len(pairs):
        a, b = pairs[:, 0], pairs[:, 1]
        delta = ends[b] - ends[a]
        dist = np.linalg.norm(delta, axis=1)
        direction = delta / np.maximum(dist[:, None], 1e-6)
        along_a = np.einsum('ij,ij->i', direction, tans[a])
        along_b = -np.einsum('ij,ij->i', direction, tans[b])
        opposed = -np.einsum('ij,ij->i', tans[a], tans[b])
        ok = ((along_a > cos_limit * 0.8) & (along_b > cos_limit * 0.8) &
              (opposed > cos_limit))
        score = dist + 8.0 * (2.0 - along_a - along_b)
        heap = sorted(zip(score[ok].tolist(), a[ok].tolist(), b[ok].tolist()))

    dead = np.zeros(2 * n, bool)
    link = {}
    partner = np.arange(2 * n) ^ 1          # the other end of the same chain
    joined = 0
    for _, i, j in heap:
        if joined >= max_rounds:
            break
        if dead[i] or dead[j] or partner[i] == j:
            continue                         # retired, or it would close a loop
        dead[i] = dead[j] = True
        link[i], link[j] = j, i
        far_i, far_j = partner[i], partner[j]
        partner[far_i], partner[far_j] = far_j, far_i
        joined += 1

    # Assemble the chains by walking from every free end point.
    seen = np.zeros(n, bool)
    chains = []
    for start in range(2 * n):
        if dead[start] or seen[start // 2]:
            continue
        pieces = []
        e = start
        while True:
            k = e // 2
            seen[k] = True
            pieces.append(paths[k] if e % 2 == 0 else paths[k][::-1])
            far = e ^ 1
            if far not in link:
                break
            e = link[far]
        chains.append(np.vstack(pieces) if len(pieces) > 1 else pieces[0])
    return chains


def split_at_corners(path, corner_deg=75.0, step=2.0, window=3):
    """Cut a path where the pen would really turn, nowhere else."""
    from anime_study import resample
    pts = resample(path, step)
    n = len(pts)
    if n < 2 * window + 3:
        return [pts]
    cuts = []
    for i in range(window, n - window):
        a = pts[i] - pts[i - window]
        b = pts[i + window] - pts[i]
        na, nb = np.hypot(*a), np.hypot(*b)
        if na < 1e-6 or nb < 1e-6:
            continue
        turn = math.degrees(math.acos(float(np.clip(a @ b / (na * nb), -1, 1))))
        if turn > corner_deg:
            cuts.append(i)
    if not cuts:
        return [pts]
    keep, last = [], -999
    for i in cuts:
        if i - last > window * 2:
            keep.append(i)
            last = i
    pieces, start = [], 0
    for i in keep:
        pieces.append(pts[start:i + 1])
        start = i
    pieces.append(pts[start:])
    return [p for p in pieces if len(p) >= 3]


def limit_length(path, max_len):
    """Split an over-long curve into pen-length strokes that overlap a little."""
    total = _length(path)
    if total <= max_len:
        return [path]
    parts = max(2, int(math.ceil(total / max_len)))
    target = total / parts
    out, start, acc = [], 0, 0.0
    for i in range(1, len(path)):
        acc += float(np.hypot(*(path[i] - path[i - 1])))
        if acc >= target and i - start >= 3:
            out.append(path[start:i + 1])
            start, acc = max(start, i - 1), 0.0
    if len(path) - start >= 3:
        out.append(path[start:])
    return out


def boundary_gap_paths(subject, ink_mask, min_len, skip=None):
    """Outline pieces of the subject that the line model left open.

    `skip` (bool mask) marks the faded-out edge of a figure cut off by the frame: no
    outline is made there."""
    contours, _ = cv2.findContours(subject, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return []
    covered = cv2.dilate(ink_mask, np.ones((7, 7), np.uint8)) > 0
    out = []
    for contour in contours:
        pts = contour.reshape(-1, 2)
        gap = np.array([not covered[y, x] and not (skip is not None and skip[y, x])
                        for x, y in pts])
        i, n = 0, len(pts)
        while i < n:
            if gap[i]:
                j = i
                while j < n and gap[j]:
                    j += 1
                run = pts[max(0, i - 2):min(n, j + 2)].astype(float)
                if _length(run) >= min_len:
                    out.append(run)
                i = j
            else:
                i += 1
    return out


def nearest_order(paths, start=None):
    """Draw neighbours after each other; flip a stroke so it starts near the pen."""
    todo = list(range(len(paths)))
    if not todo:
        return []
    pen = np.asarray(start if start is not None else (1e9, 1e9), float)
    if start is None:
        # people begin top-left: take the stroke whose end is nearest that corner
        corner = np.array([0.0, 0.0])
        best = min(todo, key=lambda i: min(np.hypot(*(paths[i][0] - corner)),
                                            np.hypot(*(paths[i][-1] - corner))))
        todo.remove(best)
        p = paths[best]
        if np.hypot(*(p[-1] - corner)) < np.hypot(*(p[0] - corner)):
            p = p[::-1]
        ordered = [p]
        pen = p[-1]
    else:
        ordered = []
    while todo:
        best, flip, bd = None, False, 1e18
        for i in todo:
            d0 = float(np.hypot(*(paths[i][0] - pen)))
            d1 = float(np.hypot(*(paths[i][-1] - pen)))
            d, f = (d0, False) if d0 <= d1 else (d1, True)
            if d < bd:
                best, flip, bd = i, f, d
        todo.remove(best)
        p = paths[best][::-1] if flip else paths[best]
        ordered.append(p)
        pen = p[-1]
    return ordered


def extract_lineart(edges, subject, gray, rng, max_dim, skip=None):
    """Return [(path, tier)] with tier 0 outline, 1 main line, 2 detail."""
    from anime_study import humanize
    h, w = subject.shape[:2]
    near = cv2.dilate(subject, np.ones((5, 5), np.uint8))
    mask = cv2.bitwise_and(edges, near)
    count, comps, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    small = max(6, int(max_dim * 0.012) ** 2 // 2)
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] < small:
            mask[comps == i] = 0
    min_len = max(5.0, max_dim * 0.018)
    planlog.note('thinning and tracing line pixels')
    raw = trace_centerlines(mask, min_length=2.0)
    raw += boundary_gap_paths(subject, mask, min_len, skip)
    planlog.note(f'joining {len(raw)} fragments into long curves')
    merged = merge_paths(raw, max_gap=max(4.0, max_dim * 0.018))
    inside = (subject > 0).astype(np.uint8)
    dist = cv2.distanceTransform(inside, cv2.DIST_L2, 3)
    edge_band = max(2.5, max_dim * 0.008)
    dark = cv2.GaussianBlur(gray, (0, 0), 1.2)
    out = []
    max_len = max_dim * 0.24
    for mi, path in enumerate(merged):
        if mi % 8 == 0:
            planlog.note(f'smoothing curve {mi + 1}/{len(merged)}', (mi + 1) / max(1, len(merged)))
        for piece in split_at_corners(path):
            for part in limit_length(piece, max_len):
                length = _length(part)
                if length < min_len:
                    continue
                xs = np.clip(np.round(part[:, 0]).astype(int), 0, w - 1)
                ys = np.clip(np.round(part[:, 1]).astype(int), 0, h - 1)
                on_edge = float(np.median(dist[ys, xs])) < edge_band
                tone = 1.0 - float(dark[ys, xs].mean()) / 255.0
                importance = length * (0.6 + tone)
                tier = 0 if on_edge and length >= min_len * 1.5 else (
                    1 if length >= max_dim * 0.07 else 2)
                amp = 0.45 if tier < 2 else 0.8
                smooth = humanize(part, rng, amplitude=amp, smooth=7, step=1.8,
                                  overshoot=0.8 if tier < 2 else 1.4)
                if len(smooth) >= 3:
                    out.append((smooth, tier, importance))
    return out
