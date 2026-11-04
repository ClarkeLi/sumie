"""Colouring with the brush instead of the paint bucket.

A person with a pen-tablet fills an area by sweeping a thick brush back and forth, then
goes round the rim with a smaller one and dabs the corners. This module plans exactly that
for one colour area: zigzag strokes at the biggest brush that fits, rim strokes at 18, 12 ...
and corner strokes at 8, 5, 3, until the area is covered.

The geometry works on a raster that is twice as fine as the bucket model's grid, so that a
brush of size s is s pixels wide (the game draws size / 100 world units, a cell is 0.02 world).
Everything it plans is measured with the same footprint (flat ends, round joins) that
`anime_human.simulate` draws, so plan and result agree.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from artist_geometry import trace_centerlines

HI = 2                              # raster factor over the bucket grid
SIZES = (35, 22, 12, 6, 3)          # the brush sizes of the hand, big to small
HATCH_SIZES = (35, 22)              # these sweep the area; the smaller ones follow rims and corners
SPACING = 0.86                      # hatch line distance as a share of the brush width
MIN_GAIN = 0.04                     # a stroke must add this share of its own footprint
COVER_TARGET = 0.93
SECONDS_PER_REQUEST = 0.06           # what a request costs besides the pen time (as in anime_human)


def disk(radius):
    r = max(0, int(math.ceil(radius)))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def upsample(mask, factor=HI):
    h, w = mask.shape
    return cv2.resize(mask.astype(np.uint8), (w * factor, h * factor), interpolation=cv2.INTER_NEAREST) > 0


def footprint(shape, path_px, size, origin=(0, 0)):
    """Boolean mask of a stroke: flat ends, round joins, `size` pixels wide."""
    canvas = np.zeros(shape, np.uint8)
    pts = np.asarray(path_px, float) - np.asarray(origin, float)
    half = max(size, 1.0) / 2.0
    for a, b in zip(pts[:-1], pts[1:]):
        d = b - a
        n = float(np.hypot(*d))
        if n < 1e-6:
            continue
        normal = np.array([-d[1], d[0]]) / n * half
        quad = np.array([a + normal, b + normal, b - normal, a - normal])
        cv2.fillConvexPoly(canvas, np.round(quad).astype(np.int32), 255)
    for p in pts[1:-1]:
        cv2.circle(canvas, (int(round(p[0])), int(round(p[1]))), int(round(half)), 255, -1)
    return canvas > 0


def clip_path(path_px, size, zone, origin=(0, 0), px_per_size=1.0):
    """Pieces of a brush path that stay inside `zone` (the whole footprint inside it)."""
    pts = np.asarray(path_px, float)
    if len(pts) < 2:
        return []
    inner = cv2.erode(zone.astype(np.uint8), disk(size * px_per_size / 2.0)) > 0
    dense = [pts[0]]
    for a, b in zip(pts[:-1], pts[1:]):
        n = max(1, int(math.hypot(*(b - a)) / 2.0))
        for k in range(1, n + 1):
            dense.append(a + (b - a) * k / n)
    dense = np.array(dense)
    idx = np.round(dense - np.asarray(origin, float)).astype(int)
    h, w = inner.shape
    inside = np.zeros(len(dense), bool)
    ok = (idx[:, 0] >= 0) & (idx[:, 0] < w) & (idx[:, 1] >= 0) & (idx[:, 1] < h)
    inside[ok] = inner[idx[ok, 1], idx[ok, 0]]
    pieces, start = [], None
    for i, flag in enumerate(inside):
        if flag and start is None:
            start = i
        if (not flag or i == len(inside) - 1) and start is not None:
            end = i if flag else i - 1
            if end - start >= 2:
                pieces.append(dense[start:end + 1])
            start = None
    return pieces


# ---------------------------------------------------------------------------
# the sweeps
# ---------------------------------------------------------------------------

def _rotate(mask, degrees):
    h, w = mask.shape
    size = int(math.ceil(math.hypot(w, h))) + 2
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), degrees, 1.0)
    M[0, 2] += size / 2.0 - w / 2.0
    M[1, 2] += size / 2.0 - h / 2.0
    rotated = cv2.warpAffine(mask.astype(np.uint8), M, (size, size), flags=cv2.INTER_NEAREST) > 0
    return rotated, cv2.invertAffineTransform(M)


def _runs(row):
    if not row.any():
        return []
    edges = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
    starts, ends = np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0] - 1
    return list(zip(starts.tolist(), ends.tolist()))


def sweep_paths(centres, angle, spacing, rng, min_run=2):
    """Zigzag paths (in `centres` pixel coordinates) over a mask of allowed brush centres.

    Lines run along `angle` (degrees); neighbouring lines are joined into one stroke where the
    turn stays inside the mask, so a hand's back-and-forth shows up as long strokes.
    """
    rotated, back = _rotate(centres, angle)
    soft = cv2.dilate(rotated.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0   # a turn may graze the rim
    ys = np.nonzero(rotated.any(axis=1))[0]
    if len(ys) == 0:
        return []
    first = ys[0] + rng.uniform(0.0, spacing * 0.4)
    rows = np.arange(first, ys[-1] + 1, spacing)
    strokes = []                      # each: dict(points=[...], row=k)
    open_strokes = []
    forward = rng.random() < 0.5
    for k, y in enumerate(rows):
        yi = int(round(y))
        if yi >= rotated.shape[0]:
            break
        runs = [r for r in _runs(rotated[yi]) if r[1] - r[0] >= min_run]
        now_open = []
        used = set()
        for x0, x1 in runs:
            best = None
            for si, s in enumerate(open_strokes):
                if si in used:
                    continue
                px, py = s['points'][-1]
                for end, other in ((x0, x1), (x1, x0)):
                    d = math.hypot(end - px, y - py)
                    if d > spacing * 2.6:
                        continue
                    line = np.linspace((px, py), (end, y), max(2, int(d / 2) + 1))
                    li = np.round(line).astype(int)
                    if not soft[np.clip(li[:, 1], 0, soft.shape[0] - 1),
                                np.clip(li[:, 0], 0, soft.shape[1] - 1)].all():
                        continue
                    if best is None or d < best[0]:
                        best = (d, si, end, other)
            if best:
                _, si, end, other = best
                used.add(si)
                s = open_strokes[si]
                s['points'] += [(end, y), (other, y)]
                s['row'] = k
                now_open.append(s)
            else:
                a, b = (x0, x1) if forward else (x1, x0)
                s = {'points': [(a, y), (b, y)], 'row': k}
                strokes.append(s)
                now_open.append(s)
            forward = not forward
        open_strokes = now_open
    paths = []
    for s in strokes:
        pts = cv2.transform(np.array(s['points'], np.float32).reshape(-1, 1, 2), back).reshape(-1, 2)
        paths.append(pts.astype(float))
    return paths


def rim_paths(band, min_len):
    """Centre lines of a band of brush centres: along rims, thin shapes and corners.

    The band is the set of places where a brush of this size fits and would still add colour.
    A hand follows such a band with one stroke, not round its edge twice."""
    if not band.any():
        return []
    band = cv2.morphologyEx(band.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)) > 0
    paths = []
    for path in trace_centerlines(band, min_length=min_len):
        if len(path) >= 2:
            paths.append(np.asarray(path, float))
    return paths


def _direction(path, at_start, span=6):
    pts = np.asarray(path, float)
    a, b = (pts[0], pts[min(span, len(pts) - 1)]) if at_start else (pts[-1], pts[max(-1 - span, -len(pts))])
    d = a - b                              # pointing out of the path
    n = float(np.hypot(*d))
    return d / n if n > 1e-9 else np.zeros(2)


def merge_at_junctions(paths, max_turn=45.0, snap=2.5):
    """Join skeleton pieces that meet at a junction and continue nearly straight.

    A hand paints a strand and its fork in a few strokes, not one per skeleton edge."""
    paths = [np.asarray(p, float) for p in paths]
    cos_limit = math.cos(math.radians(max_turn))
    changed = True
    while changed:
        changed = False
        best = None
        for i, a in enumerate(paths):
            for ai in (True, False):
                pa, da = (a[0] if ai else a[-1]), _direction(a, ai)
                for j in range(i + 1, len(paths)):
                    b = paths[j]
                    for bi in (True, False):
                        pb = b[0] if bi else b[-1]
                        if np.hypot(*(pa - pb)) > snap:
                            continue
                        straight = -float(np.dot(da, _direction(b, bi)))      # 1 = straight on
                        if straight >= cos_limit and (best is None or straight > best[0]):
                            best = (straight, i, ai, j, bi)
        if best:
            _, i, ai, j, bi = best
            a = paths[i] if not ai else paths[i][::-1]          # a ends at the junction
            b = paths[j] if bi else paths[j][::-1]              # b starts at the junction
            merged = np.vstack((a, b[1:]))
            paths = [p for k, p in enumerate(paths) if k not in (i, j)] + [merged]
            changed = True
    return paths


def medial_paths(rem, min_len):
    """Centre lines through what is left, each with the width of the brush it needs.

    Returns [(path, needed_width_px)]: strands get one stroke along their length."""
    smooth = cv2.morphologyEx(rem.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if not smooth.any():
        return []
    dist = cv2.distanceTransform(smooth, cv2.DIST_L2, 3)
    out = []
    for path in merge_at_junctions(trace_centerlines(smooth > 0, min_length=min_len)):
        if len(path) < 2:
            continue
        idx = np.round(path).astype(int)
        radius = float(np.percentile(dist[np.clip(idx[:, 1], 0, dist.shape[0] - 1),
                                           np.clip(idx[:, 0], 0, dist.shape[1] - 1)], 85))
        out.append((path, 2.0 * radius))
    return out


# ---------------------------------------------------------------------------
# one area
# ---------------------------------------------------------------------------

class Painter:
    """Plans the brush strokes for colour areas. `stroke(cells, size, role)` makes an Op or None."""

    def __init__(self, grid, stroke, rng, scale_cells):
        self.grid, self.stroke, self.rng, self.scale_cells = grid, stroke, rng, scale_cells
        self.spent = 0.0
        self.debug = False

    # conversions between bucket cells and hi-res pixels (pixel centres line up with cell centres)
    def to_px(self, cells, origin):
        c = np.asarray(cells, float)
        return (c + 0.5) * HI - 0.5 - np.asarray(origin, float)

    def to_cells(self, px, origin):
        p = np.asarray(px, float) + np.asarray(origin, float)
        return (p + 0.5) / HI - 0.5

    def op_path_px(self, op, origin):
        return self.to_px(self.grid.to_cells(np.asarray(op.points, float)), origin)

    def sweep(self, st, size, max_seconds):
        """Hatch strokes of one size over the rest of the area; returns the new ops."""
        need, allowed, covered = st['need'], st['allowed'], st['covered']
        radius = size / 2.0
        centres = cv2.erode(allowed.astype(np.uint8), disk(radius)) > 0
        rem = need & ~covered
        if not rem.any() or not centres.any():
            return []
        # keep only centres that can still add colour (and, if asked, only within the focus)
        centres &= cv2.dilate(rem.astype(np.uint8), disk(radius)) > 0
        if st.get('focus') is not None:
            centres &= st['focus']
        made = []
        for path in sweep_paths(centres, st['angle'], SPACING * size, self.rng):
            if _length_px(path) < 1.5 * size:          # a short run is part of a strand: the strokes along it do that
                continue
            made += self._draw(path, size, st)
            if self.spent > max_seconds:
                break
        return made

    def rim(self, st, size, max_seconds):
        need, allowed, covered = st['need'], st['allowed'], st['covered']
        radius = size / 2.0
        centres = cv2.erode(allowed.astype(np.uint8), disk(radius)) > 0
        rem = need & ~covered
        if not rem.any() or not centres.any():
            return []
        band = centres & (cv2.dilate(rem.astype(np.uint8), disk(radius)) > 0)
        made = []
        for path in rim_paths(band, min_len=max(4.0, 1.0 * size)):
            made += self._draw(path, size, st)
            if self.spent > max_seconds:
                break
        return made

    def _draw(self, path_px, size, st):
        """Make the strokes for a pixel path; skip those that add nothing."""
        need, covered, origin = st['need'], st['covered'], st['origin']
        cap = 1.4 * self.scale_cells * HI                     # one stroke is at most this long
        pieces, run, acc = [], [path_px[0]], 0.0
        for a, b in zip(path_px[:-1], path_px[1:]):
            run.append(b)
            acc += float(np.hypot(*(b - a)))
            if acc >= cap:
                pieces.append(np.array(run))
                run, acc = [b], 0.0
        if len(run) >= 2:
            pieces.append(np.array(run))
        made = []
        for piece in pieces:
            piece = _soften(piece, size)
            op = self.stroke(self.to_cells(piece, origin), size, 'fill', st['part'])
            if op is None:
                continue
            fp = footprint(need.shape, self.op_path_px(op, origin), op.size)
            gain = int(np.count_nonzero(fp & need & ~covered))
            if gain < max(8, MIN_GAIN * int(np.count_nonzero(fp))):
                continue
            covered |= fp
            self.spent += (op.dur or 0.0) + (op.pause or 0.0) + SECONDS_PER_REQUEST
            made.append(op)
        return made

    def prepare(self, need_cells, allowed_cells, part, angle=None, tight_cells=None):
        """State of one area for `run`; None when there is nothing to paint."""
        ys, xs = np.nonzero(allowed_cells | need_cells)
        if len(xs) == 0:
            return None
        pad = int(math.ceil(35 / HI)) + 2
        x0, x1 = max(0, xs.min() - pad), min(allowed_cells.shape[1], xs.max() + pad + 1)
        y0, y1 = max(0, ys.min() - pad), min(allowed_cells.shape[0], ys.max() + pad + 1)
        need = upsample(need_cells[y0:y1, x0:x1])
        if not need.any():
            return None
        allowed = upsample(allowed_cells[y0:y1, x0:x1]) | need
        return {'need': need, 'allowed': allowed, 'tight': upsample(tight_cells[y0:y1, x0:x1]) | need
                if tight_cells is not None else allowed, 'covered': np.zeros_like(need),
                'origin': (x0 * HI, y0 * HI), 'part': part, 'total': int(np.count_nonzero(need)),
                'angle': _long_axis(need) if angle is None else angle}

    def tighten(self, st):
        """From now on the brush stays within the area and the line art: what is painted after
        the neighbours is not allowed to cover them."""
        st['allowed'] = st['tight']
        st['soft'] = None

    def share(self, st):
        return 1.0 - np.count_nonzero(st['need'] & ~st['covered']) / max(1, st['total'])

    def medial(self, st, max_seconds):
        """Strokes along what is left, each with the brush that fits it. Returns the new ops."""
        need, allowed, covered = st['need'], st['allowed'], st['covered']
        rem = need & ~covered
        made = []
        for path, width in sorted(medial_paths(rem, min_len=6.0), key=lambda item: -_length_px(item[0])):
            wanted = min((x for x in SIZES if x >= width + 3), default=SIZES[0])
            for size in (x for x in SIZES if x <= wanted):
                if self._inside(path, size, allowed, st):
                    made += self._draw(path, size, st)
                    break
            if self.spent > max_seconds:
                break
        return made

    def _inside(self, path_px, size, allowed, st):
        """Does a brush of this size stay inside the allowed place all along the path?"""
        if st.get('soft') is None:                     # 2 px over the rim lies under the ink line
            st['soft'] = cv2.dilate(allowed.astype(np.uint8), disk(2)) > 0
        fp = footprint(allowed.shape, path_px, size)
        outside = np.count_nonzero(fp & ~st['soft'])
        return outside <= 0.03 * max(1, np.count_nonzero(fp))

    def run(self, st, passes, max_seconds, target=COVER_TARGET, rounds=3):
        """Plan strokes for an area. passes: 'coarse' sweeps, 'fine' strokes along the rest.

        Returns the new ops; `self.spent` counts their seconds (the caller resets it)."""
        done = (1 - target) * st['total']
        ops = []
        if 'coarse' in passes:
            # Where a brush fits with room to spare the hand first goes round the edge, then
            # sweeps back and forth inside it.
            size = SIZES[0]
            rem = st['need'] & ~st['covered']
            centres = cv2.erode(st['allowed'].astype(np.uint8), disk(size / 2.0)) > 0
            centres &= cv2.dilate(rem.astype(np.uint8), disk(size / 2.0)) > 0
            fat = cv2.morphologyEx(centres.astype(np.uint8), cv2.MORPH_OPEN, disk(5)) > 0
            made = []
            if fat.any():
                contours, _ = cv2.findContours(fat.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
                for contour in sorted(contours, key=lambda c: -len(c)):
                    pts = contour.reshape(-1, 2).astype(float)
                    if len(pts) < 3 * size:
                        continue
                    made += self._draw(np.vstack((pts, pts[1:6])), size, st)
                st['focus'] = fat
                made += self.sweep(st, size, max_seconds)
                st['focus'] = None
            if self.debug:
                print(f'[brush]   edge + sweep: {len(made)} strokes, uncovered '
                      f'{np.count_nonzero(st["need"] & ~st["covered"]) / st["total"]:.3f}')
            ops += made
        if 'fine' in passes:
            for _round in range(rounds):
                if np.count_nonzero(st['need'] & ~st['covered']) <= done or self.spent > max_seconds:
                    break
                made = self.medial(st, max_seconds)
                if self.debug:
                    print(f'[brush]     along-strokes round {_round}: {len(made)} strokes, uncovered '
                          f'{np.count_nonzero(st["need"] & ~st["covered"]) / st["total"]:.3f}')
                if not made:
                    break
                ops += made
        return ops


def _length_px(path):
    return float(np.hypot(*np.diff(np.asarray(path, float), axis=0).T).sum())


def _soften(px, size):
    """Round the corners of a zigzag a hand cannot turn on a point."""
    if len(px) < 4:
        return px
    dense = [px[0]]
    for a, b in zip(px[:-1], px[1:]):
        n = max(1, int(math.hypot(*(b - a)) / 3.0))
        for k in range(1, n + 1):
            dense.append(a + (b - a) * k / n)
    dense = np.array(dense)
    window = int(max(3, min(size * 0.5, len(dense) // 3)) // 2 * 2 + 1)
    half = window // 2
    kernel = np.ones(window) / window
    out = dense.copy()
    for axis in (0, 1):
        padded = np.pad(dense[:, axis], half, mode='edge')
        out[:, axis] = np.convolve(padded, kernel, mode='valid')
    out[0], out[-1] = dense[0], dense[-1]
    return out


def _long_axis(mask):
    """Direction (degrees) of the long axis of a mask: the hand sweeps along the area."""
    ys, xs = np.nonzero(mask)
    if len(xs) < 5:
        return 0.0
    pts = np.column_stack((xs, ys)).astype(np.float32)
    vx, vy, _, _ = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
    return math.degrees(math.atan2(vy, vx))
