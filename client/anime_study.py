"""Human-paced anime study: curved brush lines and thick-brush colour.

No polygon fill, image upload, background rectangle, black region fences or
uniform hatching. Everything is an ordinary line/disc the game brush can draw.

The plan is built from the round timer: a short round gets few colours and a
loose block-in, a long one gets more colour masses, shading and interior ink.
Colour masses are scrubbed in with overlapping thick brush strokes, never
filled, and every stroke is a curved, slightly wobbly path.
"""
from __future__ import annotations

import dataclasses
import math
import random
import time

import cv2
import numpy as np

import planlog
from artist_geometry import trace_centerlines

MAX_COLORS = 10
MAX_STROKES = 900
REQUEST_ALLOWANCE = 0.06  # estimate, not a promise about bridge/network latency
COLOR_PAUSE = 0.10
BRUSH_PAUSE = 0.05
MIN_BRUSH = 2
MAX_BRUSH = 35
# A human drags the brush for a while, lifts, looks, and starts again. These
# are estimates used for planning; the real deadline is enforced in execute.
MIN_STROKE_SECONDS = 0.30


# ---------------------------------------------------------------------------
# timing and planning
# ---------------------------------------------------------------------------

def stroke_delay(op, args):
    if op.dur is not None:               # anime_human style: the motor model has the timing
        return (op.pause or 0.0) + op.dur
    points = np.asarray(op.points, dtype=float)
    length = float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())
    return args.study_pause + min(0.85, length * args.study_speed)


def stroke_seconds(op, args):
    return REQUEST_ALLOWANCE + stroke_delay(op, args)


def plan_seconds(ops, args, color=None, size=None):
    total = 0.0
    for op in ops:
        if op.dur is None and op.color != color:
            total += REQUEST_ALLOWANCE + COLOR_PAUSE
            color = op.color
        if op.dur is None and op.kind in ('line', 'disc') and op.size != size:
            total += REQUEST_ALLOWANCE + BRUSH_PAUSE
            size = op.size
        total += stroke_seconds(op, args)
    return total


def complexity(seconds, args):
    """Choose palette and detail from the actual remaining round time."""
    if args.colors is not None:
        colors = int(args.colors)
    elif seconds < 30:
        colors = 3
    elif seconds < 60:
        colors = 4
    elif seconds < 120:
        colors = 5
    elif seconds < 200:
        colors = 7
    elif seconds < 320:
        colors = 8
    else:
        colors = 8
    return max(2, min(MAX_COLORS, colors))


def detail_level(seconds):
    if seconds < 40:
        return 0
    if seconds < 80:
        return 1
    if seconds < 140:
        return 2
    if seconds < 280:
        return 3
    return 4


def operation_groups(ops):
    groups = []
    for op in ops:
        if op.group is not None and groups and groups[-1][0].group == op.group:
            groups[-1].append(op)
        else:
            groups.append([op])
    return groups


def fit_plan(ops, seconds, args, units=float('inf')):
    """Keep complete groups, spend time by phase, never pad with fake strokes."""
    cap = min(MAX_STROKES,
              args.max_strokes if args.max_strokes is not None else MAX_STROKES,
              int(units) if math.isfinite(units) else MAX_STROKES)
    if cap <= 0 or seconds <= 0:
        return []
    if len(ops) <= cap and plan_seconds(ops, args) <= seconds:
        return ops

    if seconds < 40:
        shares = (0.30, 0.54, 0.00, 0.16)
    elif seconds < 90:
        shares = (0.26, 0.56, 0.04, 0.14)
    elif seconds < 170:
        shares = (0.24, 0.56, 0.08, 0.12)
    else:
        shares = (0.20, 0.54, 0.12, 0.14)

    phases = ('sketch', 'color', 'shade', 'detail')
    available = {p: operation_groups([o for o in ops if o.phase == p]) for p in phases}
    selected = {p: [] for p in phases}
    counts = {p: 0 for p in phases}
    times = {p: 0.0 for p in phases}

    def cost_of(group):
        return sum(stroke_seconds(o, args) for o in group)

    def add(phase):
        group = available[phase].pop(0)
        selected[phase].extend(group)
        counts[phase] += len(group)
        times[phase] += cost_of(group)

    def protected(group):
        return False

    for phase, share in zip(phases, shares):
        phase_cap = max(1, int(cap * share))
        phase_time = seconds * share
        while available[phase]:
            group = available[phase][0]
            cost = cost_of(group)
            if (counts[phase] + len(group) > phase_cap or times[phase] + cost > phase_time or
                    sum(counts.values()) + len(group) > cap):
                break
            add(phase)

    # Spend what is left where it shows first: colour, then structure.
    changed = True
    while changed:
        changed = False
        for phase in ('color', 'sketch', 'detail', 'shade'):
            if not available[phase]:
                continue
            group = available[phase][0]
            cost = cost_of(group)
            if (sum(counts.values()) + len(group) <= cap and
                    sum(times.values()) + cost <= seconds):
                add(phase)
                changed = True

    result = [o for phase in phases for o in selected[phase]]

    # HUD transitions are not free; drop optional work before core drawing.
    while result and plan_seconds(result, args) > seconds:
        dropped = False
        for phase in ('detail', 'shade', 'color', 'sketch'):
            groups = operation_groups(selected[phase])
            for index in range(len(groups) - 1, -1, -1):
                if protected(groups[index]):
                    continue
                del groups[index]
                selected[phase] = [o for group in groups for o in group]
                dropped = True
                break
            if dropped:
                break
        if not dropped:
            break
        result = [o for phase in phases for o in selected[phase]]
    return result


# ---------------------------------------------------------------------------
# image helpers
# ---------------------------------------------------------------------------

def flat_background_mask(image):
    """Exact subject mask for a flat (paper/solid) background, else None.

    Illustrations pasted from a clipboard usually sit on pure white. GrabCut
    only guesses there and loses dark clothing and hair that touch the frame
    (30% of the figure on a real test image), while a distance-from-background
    test is exact. Returns None when the border is not a single flat colour so
    the caller falls back to GrabCut.
    """
    h, w = image.shape[:2]
    lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB).astype(np.float32)
    border = np.concatenate((lab[0], lab[-1], lab[:, 0], lab[:, -1]))
    bg = np.median(border, axis=0)
    spread = np.linalg.norm(border - bg, axis=1)
    # Most of the frame edge must be the background colour; the figure may
    # touch the edge in a few places.
    if float(np.mean(spread < 10)) < 0.62:
        return None
    mask = (np.linalg.norm(lab - bg, axis=2) > 24).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(mask)
    for contour in contours:
        if cv2.contourArea(contour) > w * h * 0.004:
            cv2.drawContours(filled, [contour], -1, 255, -1)
    coverage = np.count_nonzero(filled) / float(w * h)
    if not 0.05 < coverage < 0.90:
        return None
    return filled


def segmented_subject(image):
    """Character mask from the anime segmentation network, or None.

    Cheap sanity checks keep a failed network run from becoming a wrong plan:
    the character must be a plausible share of the frame and not a rectangle.
    """
    try:
        import anime_seg
    except ImportError:
        return None
    if not anime_seg.available():
        return None
    try:
        alpha = anime_seg.foreground_alpha(image)
    except Exception:
        return None
    h, w = image.shape[:2]
    mask = (alpha > 0.5).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    count, comps, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros_like(mask)
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] >= w * h * 0.004:
            keep[comps == i] = 255
    # seal interior holes (white sleeves, hair gaps) so they are painted
    contours, _ = cv2.findContours(keep, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(keep)
    cv2.drawContours(filled, contours, -1, 255, -1)
    coverage = np.count_nonzero(filled) / float(w * h)
    if not 0.05 < coverage < 0.92:
        return None
    return filled


def subject_mask(analysis):
    from dg_artist import grabcut_subject
    image = analysis.image
    h, w = image.shape[:2]
    cv2.setRNGSeed(7)
    seg = segmented_subject(image)
    if seg is not None:
        return seg
    flat = flat_background_mask(image)
    if flat is not None:
        return flat
    mask = grabcut_subject(analysis)
    if mask is not None:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        largest = max(contours, key=cv2.contourArea) if contours else None
        coverage = np.count_nonzero(mask) / (w * h)
        rectangular = False
        if largest is not None:
            _, _, bw, bh = cv2.boundingRect(largest)
            rectangular = cv2.contourArea(largest) / max(1, bw * bh) > 0.93
        if not 0.06 < coverage < 0.85 or rectangular:
            mask = None
    if mask is None:
        lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB).astype(np.float32)
        border = np.concatenate((lab[0], lab[-1], lab[:, 0], lab[:, -1]))
        bg = np.median(border, axis=0)
        if np.median(np.linalg.norm(border - bg, axis=1)) > 24:
            return np.zeros((h, w), np.uint8)
        mask = (np.linalg.norm(lab - bg, axis=2) > 28).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        mask[:] = 0
        for contour in contours:
            if cv2.contourArea(contour) > w * h * 0.015:
                cv2.drawContours(mask, [contour], -1, 255, -1)
        if np.count_nonzero(mask) > w * h * 0.85:
            mask[:] = 0
    return mask


def resample(path, step):
    path = np.asarray(path, dtype=float)
    if len(path) < 2:
        return path
    lengths = np.linalg.norm(np.diff(path, axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(lengths)))
    total = float(cumulative[-1])
    if total < step:
        return path
    count = max(2, int(total / step) + 1)
    sample = np.linspace(0.0, total, count)
    x = np.interp(sample, cumulative, path[:, 0])
    y = np.interp(sample, cumulative, path[:, 1])
    return np.column_stack((x, y))


def catmull_rom(points, step=1.0):
    """Sample a centripetal-ish Catmull-Rom curve through the control points."""
    points = np.asarray(points, dtype=float)
    if len(points) < 3:
        return points
    extended = np.vstack((2 * points[0] - points[1], points,
                          2 * points[-1] - points[-2]))
    output = [points[0]]
    for i in range(1, len(extended) - 2):
        p0, p1, p2, p3 = extended[i - 1], extended[i], extended[i + 1], extended[i + 2]
        span = float(np.linalg.norm(p2 - p1))
        count = max(2, int(span / max(0.4, step)) + 1)
        for t in np.linspace(0.0, 1.0, count, endpoint=False)[1:]:
            t2, t3 = t * t, t * t * t
            output.append(0.5 * ((2 * p1) + (-p0 + p2) * t +
                                 (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2 +
                                 (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    output.append(points[-1])
    return np.asarray(output, dtype=float)


def _extend_end(points, at_start, distance):
    if len(points) < 2 or distance <= 0:
        return points
    if at_start:
        direction = points[0] - points[1]
        anchor = points[0]
        points = np.vstack((anchor + direction / max(1e-6, np.linalg.norm(direction)) * distance,
                            points))
    else:
        direction = points[-1] - points[-2]
        anchor = points[-1]
        points = np.vstack((points,
                            anchor + direction / max(1e-6, np.linalg.norm(direction)) * distance))
    return points


def humanize(path, rng, amplitude=1.0, smooth=5, step=1.6, overshoot=0.0):
    """Smooth a traced edge, add a small hand wobble, return dense curve points."""
    points = resample(path, step)
    if len(points) < 3:
        return points
    window = min(smooth, len(points) // 2 * 2 + 1)
    if window >= 3:
        kernel = np.ones(window, dtype=float) / window
        for axis in (0, 1):
            padded = np.pad(points[:, axis], (window // 2, window // 2), mode='edge')
            points[:, axis] = np.convolve(padded, kernel, mode='valid')
    if overshoot > 0:
        points = _extend_end(points, True, rng.uniform(0, overshoot))
        points = _extend_end(points, False, rng.uniform(0, overshoot))
    tangents = np.gradient(points, axis=0)
    norm = np.linalg.norm(tangents, axis=1)
    norm[norm < 1e-6] = 1.0
    normals = np.column_stack((-tangents[:, 1] / norm, tangents[:, 0] / norm))
    length = np.concatenate(([0.0], np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))))
    total = max(1e-6, length[-1])
    phase = rng.uniform(0, math.pi * 2)
    phase2 = rng.uniform(0, math.pi * 2)
    taper = np.sin(np.pi * np.clip(length / total, 0, 1))
    # Two low-frequency bows plus a fine tremor. The amplitude scales with the
    # stroke length so a short dab stays crisp and a long drag visibly bends.
    scale = max(0.55, min(2.6, total * 0.055))
    wobble = amplitude * scale * (0.72 * np.sin(length * 0.085 + phase) +
                                  0.32 * np.sin(length * 0.26 + phase2) +
                                  0.14 * np.sin(length * 0.74 + phase * 1.7)) * taper
    points = points + normals * wobble[:, None]
    return catmull_rom(points, step=max(0.6, step * 0.55))


def human_paths(path, rng, max_length, amplitude=1.0, max_points=90):
    """Split a smoothed path at natural brush lengths."""
    points = humanize(path, rng, amplitude=amplitude)
    if len(points) < 2:
        return []
    pieces = []
    start = 0
    while start < len(points) - 1:
        distance = 0.0
        end = start + 1
        while end < len(points):
            distance += float(np.linalg.norm(points[end] - points[end - 1]))
            end += 1
            if distance >= max_length:
                break
        part = points[start:end]
        if len(part) >= 2:
            if len(part) > max_points:
                idx = np.linspace(0, len(part) - 1, max_points).astype(int)
                part = part[idx]
            if np.linalg.norm(part[-1] - part[0]) < 1.0:
                start = end - 1
                continue
            pieces.append(part)
        start = max(start + 1, end - 1)
    return pieces


def source_brush_size(source_px, width, fit_w, world_width):
    if fit_w <= 1e-6 or world_width <= 1e-6:
        return 20
    size = source_px * 100.0 * fit_w * world_width / max(1.0, width)
    return int(np.clip(round(size), MIN_BRUSH, MAX_BRUSH))


def size_to_px(size, width, fit_w, world_width):
    return size * max(1.0, width) / (100.0 * max(1e-6, fit_w) * max(1e-6, world_width))


def render_records(records, shape):
    """Approximate canvas for painted strokes: round brush, round caps."""
    canvas = np.full((shape[0], shape[1], 3), 255, np.uint8)
    for path, px, color in records:
        pts = np.round(np.asarray(path, float)).astype(np.int32).reshape(-1, 1, 2)
        thickness = max(1, int(round(px)))
        c = tuple(int(v) for v in color)
        cv2.polylines(canvas, [pts], False, c, thickness, cv2.LINE_AA)
        if thickness >= 4:
            for end in (pts[0, 0], pts[-1, 0]):
                cv2.circle(canvas, (int(end[0]), int(end[1])), thickness // 2, c, -1, cv2.LINE_AA)
    return canvas


def transform_fit_width(transform, width):
    dx = transform(1.0, 0.0)[0] - transform(0.0, 0.0)[0]
    return max(1e-6, dx * width)


def region_angle(mask):
    ys, xs = np.nonzero(mask)
    if len(xs) < 8:
        return None
    xs = xs.astype(float)
    ys = ys.astype(float)
    cov = np.cov(np.vstack((xs, ys)))
    values, vectors = np.linalg.eigh(cov)
    vector = vectors[:, int(np.argmax(values))]
    return math.atan2(vector[1], vector[0])


# ---------------------------------------------------------------------------
# drawing primitives
# ---------------------------------------------------------------------------

def bowed_path(start, direction, length, bend, rng, step=1.4):
    """A gently bowed brush drag, not a ruler line."""
    normal = np.array([-math.sin(direction), math.cos(direction)])
    unit = np.array([math.cos(direction), math.sin(direction)])
    p0 = np.asarray(start, dtype=float)
    p2 = p0 + unit * float(length)
    p1 = p0 + unit * (float(length) * 0.5) + normal * float(bend)
    t = np.linspace(0.0, 1.0, max(4, int(length / step) + 1))[:, None]
    path = (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t ** 2 * p2
    return humanize(path, rng, amplitude=min(1.3, 0.35 + length * 0.012),
                    step=step, overshoot=min(2.5, length * 0.06))


def clip_to_mask(path, mask, margin=2):
    """Keep the run of points that stays near the colour mass."""
    allowed = cv2.dilate(mask, np.ones((margin * 2 + 1, margin * 2 + 1), np.uint8))
    h, w = mask.shape[:2]
    keep = []
    for x, y in path:
        ix = int(round(x))
        iy = int(round(y))
        if 0 <= ix < w and 0 <= iy < h and allowed[iy, ix]:
            keep.append((x, y))
        elif keep:
            break
    if len(keep) < 3:
        # The whole drag fell outside the colour mass; do not paint it.
        return np.zeros((0, 2), dtype=float)
    return np.asarray(keep, dtype=float)


def sweep_order(points, rng):
    """Nearest-neighbour sweep: paint one area, then the next; never a raster."""
    if len(points) < 3:
        return list(points)
    remaining = list(points)
    current = remaining.pop(rng.randrange(len(remaining)))
    order = [current]
    while remaining:
        sample = remaining if len(remaining) < 64 else [
            remaining[rng.randrange(len(remaining))] for _ in range(48)]
        best = min(sample, key=lambda p: (p[0] - current[0]) ** 2 + (p[1] - current[1]) ** 2)
        remaining.remove(best)
        order.append(best)
        current = best
    return order


def _project_mask(mask, angle):
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    ca, sa = math.cos(angle), math.sin(angle)
    u = xs * ca + ys * sa
    v = -xs * sa + ys * ca
    return xs, ys, u, v, ca, sa


def _serpentine_bands(mask, angle, spacing, min_run):
    """Parallel runs across the colour mass, longest run per band."""
    projected = _project_mask(mask, angle)
    if projected is None:
        return [], 1.0, 0.0
    _, _, u, v, ca, sa = projected
    vmin, vmax = float(v.min()), float(v.max())
    span = max(1e-3, vmax - vmin)
    count = max(1, int(math.ceil(span / spacing)))
    band_span = span / count
    bands = []
    for index in range(count):
        v0 = vmin + (index + 0.5) * band_span
        selected = np.abs(v - v0) <= band_span * 0.55
        if not np.any(selected):
            continue
        us = np.sort(u[selected])
        breaks = np.nonzero(np.diff(us) > max(2.0, spacing * 1.1))[0]
        starts = np.r_[0, breaks + 1]
        ends = np.r_[breaks, len(us) - 1]
        runs = [(float(us[s]), float(us[e])) for s, e in zip(starts, ends)
                if us[e] - us[s] >= min_run]
        if not runs:
            continue
        main = max(runs, key=lambda run: run[1] - run[0])
        bands.append((v0, main[0], main[1]))
    return bands, ca, sa


def serpentine_paths(mask, brush_px, rng, max_chunks=6, bands_per_chunk=None):
    """Cover a colour mass with a few long back-and-forth brush drags.

    This is how a person fills with a thick marker: one continuous scribble
    sweeps across the mass, turns at the edge, and comes back a brush-width
    lower. Instead of dozens of isolated dabs it produces long strokes with
    many folds, so the paint reads as one deliberate action.
    """
    if max_chunks <= 0 and not bands_per_chunk:
        return []
    angle = region_angle(mask)
    if angle is None:
        angle = rng.uniform(0, math.pi)
    angle += rng.uniform(-0.20, 0.20)
    spacing = brush_px * rng.uniform(0.66, 0.80)
    bands, ca, sa = _serpentine_bands(mask, angle, spacing, min_run=brush_px * 0.7)
    if not bands:
        return []
    inset = brush_px * 0.16
    bands = [(v0, u0 + inset, u1 - inset) for v0, u0, u1 in bands
             if u1 - u0 > inset * 2.4]
    if not bands:
        return []
    if bands_per_chunk:
        per_chunk = max(2, int(bands_per_chunk))
    else:
        per_chunk = max(2, int(math.ceil(len(bands) / max(1.0, float(max_chunks)))))
    chunks = []
    current = []
    direction = 1 if rng.random() < 0.5 else -1
    phase = rng.uniform(0, math.pi * 2)
    turn = brush_px * 0.08
    for index, (v0, u0, u1) in enumerate(bands):
        start_u, end_u = (u0, u1) if direction > 0 else (u1, u0)
        count = max(3, int(abs(end_u - start_u) / max(2.0, brush_px * 0.30)))
        t = np.linspace(0.0, 1.0, count)
        amplitude = min(spacing * 0.20, brush_px * 0.16)
        vv = v0 + amplitude * np.sin(t * math.pi * rng.uniform(1.2, 2.3) + phase)
        uu = start_u + (end_u - start_u) * t
        if current:
            last_u, last_v = current[-1]
            side = 1.0 if last_u >= (u0 + u1) * 0.5 else -1.0
            current.append((last_u + side * turn, last_v))
        current.extend(zip(uu.tolist(), vv.tolist()))
        direction *= -1
        if index % per_chunk == per_chunk - 1:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    paths = []
    for chunk in chunks:
        points = np.asarray(chunk, dtype=float)
        x = points[:, 0] * ca - points[:, 1] * sa
        y = points[:, 0] * sa + points[:, 1] * ca
        path = humanize(np.column_stack((x, y)), rng, amplitude=1.0,
                        step=2.0, overshoot=brush_px * 0.18)
        if len(path) >= 3:
            paths.append(path)
    return paths if bands_per_chunk else paths[:max(1, max_chunks)]


def paint_region(mask, color, brush_px, max_ops, rng, transform, width, fit_w,
                 world_width, phase='color', target=0.88, record=None):
    """Cover a colour mass with long thick brush scribbles, never a fill."""
    from dg_artist import Op, hex_color
    area = int(np.count_nonzero(mask))
    if area < 5 or max_ops <= 0:
        return []
    brush_px = max(2.0, float(brush_px))
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    max_dist = float(distance.max())
    size = source_brush_size(brush_px, width, fit_w, world_width)

    if max_dist < max(1.5, brush_px * 0.42):
        # Too thin for a scrubbing stroke: one curved drag along the ridge.
        paths = trace_centerlines(mask, min_length=3.0)
        paths.sort(key=lambda p: -np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
        ops = []
        for path in paths[:max(1, min(2, max_ops))]:
            if len(path) < 2:
                continue
            points = humanize(path, rng, amplitude=0.7, overshoot=1.5)
            if len(points) < 2:
                continue
            size_thin = source_brush_size(max(2.0, min(brush_px, max_dist * 2.4)),
                                          width, fit_w, world_width)
            pts = [transform(float(x), float(y)) for x, y in points]
            if record is not None:
                record.append((points, size_to_px(size_thin, width, fit_w, world_width), color))
            ops.append(Op('line', pts, hex_color(color), size_thin, 0, phase, 1,
                          note='study-color'))
        return ops

    def to_op(path):
        pts = [transform(float(x), float(y)) for x, y in path]
        if record is not None:
            record.append((path, size_to_px(size, width, fit_w, world_width), color))
        return Op('line', pts, hex_color(color), size, 0, phase, 1, note='study-color')

    # Keep the brush centre a little inside the colour mass: the brush half
    # width still paints the rim, but the thick strokes stop bleeding outside.
    shrink = max(1, int(round(brush_px * 0.24)))
    centre = cv2.erode(mask, np.ones((shrink * 2 + 1, shrink * 2 + 1), np.uint8))
    if np.count_nonzero(centre) < area * 0.35:
        centre = mask
    paths = serpentine_paths(centre, brush_px, rng, max_ops)
    ops = [to_op(path) for path in paths]

    # The main scribble can miss small lobes and the rim. Fill what is still
    # bare with one shorter cross pass instead of a cloud of little dabs.
    coverage = np.zeros_like(mask)
    for path in paths:
        cv2.polylines(coverage, [np.round(path).astype(np.int32)], False, 255,
                      int(max(2, brush_px * 0.9)), cv2.LINE_AA)
    uncovered = cv2.bitwise_and(mask, cv2.bitwise_not(coverage))
    if (len(ops) < max_ops and
            np.count_nonzero(uncovered) > area * max(0.08, 1.0 - target)):
        for path in serpentine_paths(uncovered, max(3.0, brush_px * 0.72), rng,
                                      max(1, max_ops - len(ops))):
            ops.append(to_op(path))
    return ops


# ---------------------------------------------------------------------------
# subject planning
# ---------------------------------------------------------------------------

def study_palette(source, subject, count):
    """Cluster the subject in Lab. Returns (labels, colours); labels 255 = outside."""
    h, w = subject.shape[:2]
    lab = cv2.cvtColor(source.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB)
    inside = lab[subject > 0]
    if len(inside) == 0:
        return np.full((h, w), 255, np.uint8), []
    step = max(1, len(inside) // 20000)
    sample = np.ascontiguousarray(inside[::step], dtype=np.float32)
    k = int(max(2, min(count, len(sample))))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 24, 0.4)
    cv2.setRNGSeed(11)
    _, _, centers = cv2.kmeans(sample, k, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
    centers = list(centers)
    population = []

    def assign(cs):
        stack = np.stack([np.linalg.norm(lab - c, axis=2) for c in cs], axis=2)
        return np.argmin(stack, axis=2)

    labels = assign(centers)
    # Two clusters closer than a visible step are one paint, not two.
    while len(centers) > 2:
        counts = [int(np.count_nonzero((labels == i) & (subject > 0))) for i in range(len(centers))]
        best = None
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                d = float(np.linalg.norm(centers[i] - centers[j]))
                if d < 13 and (best is None or d < best[0]):
                    best = (d, i, j)
        if best is None:
            break
        _, i, j = best
        drop = i if counts[i] < counts[j] else j
        del centers[drop]
        labels = assign(centers)
    colours = []
    for i in range(len(centers)):
        pixels = source[(labels == i) & (subject > 0)]
        colours.append(tuple(int(v) for v in np.median(pixels, axis=0)) if len(pixels) else (128, 128, 128))
    out = labels.astype(np.uint8)
    out[subject == 0] = 255
    return out, colours


def paint_mass(mask, color, brush_px, max_ops, rng, transform, width, fit_w,
               world_width, phase='color', record=None, note='study-color'):
    """Big brush first, then half, then a small brush for what is left.

    The brush centre stays half a brush inside the mass, so the big strokes do
    not spill over the edge; the smaller passes close the rim. This is how a
    person blocks in a shape, then tidies its border.
    """
    from dg_artist import Op, hex_color
    area = int(np.count_nonzero(mask))
    if area < 5 or max_ops <= 0:
        return []
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    if float(distance.max()) < max(1.5, brush_px * 0.42):
        return paint_region(mask, color, brush_px, max_ops, rng, transform, width,
                            fit_w, world_width, phase=phase, record=record)
    covered = np.zeros_like(mask)
    ops = []
    passes = ((1.0, 0.55), (0.5, 0.30), (0.28, 0.15))
    for index, (scale, share) in enumerate(passes):
        left = max_ops - len(ops)
        if left <= 0:
            break
        b = max(2.5, brush_px * scale)
        if index and b >= brush_px:
            continue
        todo = cv2.bitwise_and(mask, cv2.bitwise_not(covered))
        todo = cv2.morphologyEx(todo, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        if np.count_nonzero(todo) < max(8, area * (0.03 if index else 0.0)):
            continue
        r = max(1, int(round(b * 0.42)))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r * 2 + 1, r * 2 + 1))
        inner = cv2.bitwise_and(cv2.erode(mask, kernel), cv2.dilate(todo, kernel))
        if np.count_nonzero(inner) < 5:
            inner = todo
        quota = left if index == len(passes) - 1 else max(1, int(round(max_ops * share)))
        size = source_brush_size(b, width, fit_w, world_width)
        for path in serpentine_paths(inner, b, rng, min(quota, left)):
            pts = [transform(float(x), float(y)) for x, y in path]
            if record is not None:
                record.append((path, size_to_px(size, width, fit_w, world_width), color))
            ops.append(Op('line', pts, hex_color(color), size, 0, phase, 1, note=note))
            cv2.polylines(covered, [np.round(path).astype(np.int32).reshape(-1, 1, 2)],
                          False, 255, int(max(2, b)), cv2.LINE_AA)
    return ops


def fill_polygon(mask, rng):
    """Simple polygon for one colour mass, or None.

    A person lassoes a shape with a few corners, not with 200 points, so the
    outline is simplified hard; it is only used if both the game and the
    preview would fill it identically (no self crossing).
    """
    from dg_artist import simple_polygon
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    scale = max(mask.shape[:2])
    for eps in (0.005, 0.0035, 0.0025):
        polygon = simple_polygon(contour, epsilons=(max(1.0, scale * eps),))
        if polygon is not None and len(polygon) >= 3:
            return polygon
    return None


def soften_wanted(args):
    value = getattr(args, 'soften_frame', 'auto')
    if value == 'auto':
        return getattr(args, 'style', '') == 'anime_human'
    return value in (True, 'on')


def build_study(analysis, budget, args, transform, fit=True):
    """Line drawing first, then flat colour fills, a little shading, last accents.

    Layer 1 holds the ink and layer 0 the colour, so the flat fills sit under
    the line drawing and nothing has to be redrawn after colouring.
    """
    from dg_artist import Op, closed, hex_color
    from study_lineart import extract_lineart, nearest_order
    w, h = analysis.width, analysis.height
    seconds = budget.seconds
    level = detail_level(seconds)
    colors = complexity(seconds, args)
    planlog.stage('segment')
    subject = subject_mask(analysis)
    analysis.cache['study_subject'] = subject
    cut = None
    if soften_wanted(args):
        # a figure cut off by the frame must not end in a straight line
        from frame_cut import detect, soften
        cut = detect(subject)
        if cut.active:
            subject = soften(subject, cut, args.seed)
            analysis.cache['study_subject'] = subject
            planlog.note(f'{len(cut.spans)} cut edges softened')
    analysis.cache['frame_cut'] = cut if cut is not None and cut.active else None
    planlog.note(f'subject {100.0 * np.count_nonzero(subject) / subject.size:.0f}% of the frame')
    if not np.any(subject):
        return []
    world_width = float(getattr(args, 'canvas_world_width', 17.7778) or 17.7778)
    fit_w = transform_fit_width(transform, w)
    rng = random.Random(args.seed)
    ink = args.line_color or '#34333D'
    max_dim = float(max(w, h))
    layered = budget.layers >= 2
    ink_layer = 1 if layered else 0
    avg = max(MIN_STROKE_SECONDS, args.study_pause + 0.08 + REQUEST_ALLOWANCE)
    usable = max(0.0, budget.usable_seconds - min(12.0, budget.usable_seconds * 0.18))
    cap = min(MAX_STROKES, int(budget.units))

    # --- 1. line drawing ------------------------------------------------------
    planlog.stage('lines')
    edges = cv2.bitwise_and(analysis.edges, cv2.dilate(subject, np.ones((3, 3), np.uint8)))
    strokes = extract_lineart(edges, subject, analysis.gray, rng, max_dim,
                              skip=cut.band if cut is not None and cut.active else None)
    if cut is not None and cut.active:
        from frame_cut import fray
        strokes = fray(strokes, cut, rng)
    line_budget = max(8, int(usable * 0.52 / avg))
    strokes.sort(key=lambda s: (s[1], -s[2]))
    chosen = []
    for tier in (0, 1, 2):
        pool = [s for s in strokes if s[1] == tier]
        room = line_budget - len(chosen)
        share = room if tier < 2 else max(0, room)
        chosen += pool[:max(0, min(share, len(pool)))]
    main = [s for s in chosen if s[1] < 2]
    detail = [s for s in chosen if s[1] == 2]

    def line_ops(items, phase, note):
        order = nearest_order([s[0] for s in items])
        tiers = {id(s[0]): s[1] for s in items}
        result = []
        for path in order:
            tier = next((s[1] for s in items if s[0] is path or
                         (len(s[0]) == len(path) and np.allclose(s[0][::-1], path))), 1)
            px = (3.6, 2.8, 2.0)[tier] * (0.9 + rng.random() * 0.25)
            size = source_brush_size(px, w, fit_w, world_width)
            pts = [transform(float(x), float(y)) for x, y in path]
            result.append(Op('line', pts, ink, size, ink_layer, phase, 1, note=note))
        return result

    sketch_ops = line_ops(main, 'sketch', 'study-structure')
    detail_ops = line_ops(detail, 'detail', 'study-centerline')
    planlog.note(f'{len(sketch_ops) + len(detail_ops)} strokes kept of {len(strokes)} traced')

    # --- 2. flat colour fills ---------------------------------------------------
    planlog.stage('palette', detail='flattening colours')
    source = cv2.bilateralFilter(cv2.pyrMeanShiftFiltering(analysis.image, sp=5, sr=18), 7, 24, 5)
    thin_ink = ((analysis.gray < 100) & (edges > 0)).astype(np.uint8) * 255
    source = cv2.inpaint(source, thin_ink, 2, cv2.INPAINT_TELEA)
    labels, palette = study_palette(source, subject, max(2, colors - 1))
    # smooth the label map so regions are calm shapes, not confetti
    if palette:
        k = len(palette)
        votes = np.stack([cv2.GaussianBlur((labels == i).astype(np.float32), (0, 0), 1.6)
                          for i in range(k)], axis=2)
        smooth = np.argmax(votes, axis=2).astype(np.uint8)
        smooth[subject == 0] = 255
        labels = smooth
    planlog.note(f'{len(palette)} colours')
    area_total = max(1, int(np.count_nonzero(subject)))
    min_area = max(24, int(area_total * 0.0012 * (1.4 - 0.2 * level)))
    fill_ops, brush_masses = [], []
    candidates = []
    for label, c in enumerate(palette):
        if max(c) > 246 and min(c) > 238:
            continue                       # paper white stays blank
        m = ((labels == label) & (subject > 0)).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        count, comps, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        for i in range(1, count):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area >= min_area:
                candidates.append((area, (comps == i).astype(np.uint8) * 255, c))
    def outer_solid(part):
        solid = np.zeros_like(part)
        cs, hier = cv2.findContours(part, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
        for ci, cnt in enumerate(cs):
            if hier[0][ci][3] < 0:
                cv2.drawContours(solid, [cnt], -1, 255, -1)
        return solid

    # Paint by the size of the filled outline: a face nested in a body or hair
    # ring then lands on top of it instead of being buried.
    candidates = [(area, part, c, outer_solid(part)) for area, part, c in candidates]
    candidates.sort(key=lambda r: -int(np.count_nonzero(r[3])))
    fill_cap = min(len(candidates), int(usable * 0.22 / avg) + 6, cap)
    planlog.stage('fills', detail=f'{min(fill_cap, len(candidates))} colour masses')
    for fi, (area, part, c, solid) in enumerate(candidates[:fill_cap]):
        planlog.note(f'{fi + 1}/{min(fill_cap, len(candidates))} masses, {len(fill_ops)} polygons',
                     (fi + 1) / max(1, min(fill_cap, len(candidates))))
        hole = (solid > 0) & (part == 0)
        # only a hole that shows paper must stay open; other colours go on top later
        if np.count_nonzero(hole & (subject == 0)) > 0.01 * area:
            brush_masses.append((area, part, c))
            continue
        grown = cv2.dilate(solid, np.ones((3, 3), np.uint8))
        grown[subject == 0] = 0
        grown = cv2.bitwise_or(grown, solid)
        polygon = fill_polygon(grown, rng)
        if polygon is None:
            brush_masses.append((area, part, c))
            continue
        pts = [transform(float(x), float(y)) for x, y in polygon]
        fill_ops.append(Op('fill', closed(pts), hex_color(c), 1, 0, 'color', 1, note='study-fill'))
        filled = np.zeros_like(part)
        cv2.fillPoly(filled, [polygon.astype(np.int32)], 255)
    color_ops = list(fill_ops)
    brush_budget = max(0, int(usable * 0.16 / avg))
    for area, part, c in brush_masses:
        left = brush_budget - len(color_ops) + len(fill_ops)
        if left <= 0:
            break
        brush = float(np.clip(math.sqrt(area) / 3.1, 5.5, 18.0))
        cap_ops = int(np.clip(round(left * 0.5), 1, max(3, int(area / max(1.0, brush * brush * 0.5)))))
        color_ops.extend(paint_mass(part, c, brush, cap_ops, rng, transform, w, fit_w, world_width))
    for op in color_ops:
        op.layer = 0

    # --- 3. shading on the biggest masses ---------------------------------------
    planlog.stage('shade')
    shade_ops = []
    shade_budget = max(0, int(usable * 0.16 / avg)) if level >= 1 else 0
    for area, part, c, _ in sorted(candidates, key=lambda r: -r[0])[:5]:
        if shade_budget - len(shade_ops) <= 0:
            break
        g = analysis.gray[part > 0]
        if len(g) < 40:
            continue
        shadow = ((part > 0) & (analysis.gray < min(150, float(np.percentile(g, 30))))).astype(np.uint8) * 255
        shadow = cv2.morphologyEx(shadow, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        if np.count_nonzero(shadow) < 40:
            continue
        dark = tuple(int(round(v * 0.76)) for v in c)
        got = paint_mass(shadow, dark, max(3.5, math.sqrt(area) / 6.0),
                         max(1, (shade_budget - len(shade_ops)) // 2), rng, transform, w, fit_w,
                         world_width, phase='shade', note='study-shade')
        for op in got:
            op.layer = 0
        shade_ops.extend(got)

    # With a single layer the colour would bury the line drawing, so the ink is
    # drawn after the fills instead of before them.
    if not layered:
        for op in sketch_ops:
            op.phase = 'detail'
        detail_ops = sketch_ops + detail_ops
        sketch_ops = []
    ops = sketch_ops + color_ops + shade_ops + detail_ops
    if not fit:
        return ops
    return fit_plan(ops, usable, args, budget.units)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

def op_requests(op):
    """Bridge requests for one op; thick drags get round-cap discs."""
    if op.kind == 'bucket':
        return [{'type': 'bucket', 'points': op.points, 'color': op.color}]
    kind = 'fill' if op.kind == 'fill' else op.kind
    requests = [{'type': kind, 'points': op.points, 'color': op.color,
                 'size': op.size, 'layer': op.layer}]

    # The game polyline has flat butt ends: a thick drag would read as a
    # rectangle. A disc of the same brush size has exactly the stroke's
    # diameter, so two dabs round the start and end like a real brush.
    if op.kind == 'line' and op.dur is None and op.size >= 8 and len(op.points) >= 2:
        requests.append({'type': 'disc', 'points': [op.points[-1]],
                         'color': op.color, 'size': op.size, 'layer': op.layer})
        requests.append({'type': 'disc', 'points': [op.points[0]],
                         'color': op.color, 'size': op.size, 'layer': op.layer})
    if getattr(op, 'vlayer', None) is not None:
        for request in requests:
            request['vlayer'] = op.vlayer
    return requests


LEAK_FACTOR = 1.8


class FillStats:
    def __init__(self):
        self.poured = self.leaked = self.missed = 0
        self.pen_strokes = self.pen_size_error = self.repairs = self.retries = 0
        self.log = []                     # (expected, actual, undone) of every pour
        self.pen_failures, self.pen_streak = [], 0


def resolve_pen(client, args):
    """--pen auto: draw through the virtual pen when the game has pen pressure on."""
    if getattr(args, 'pen', 'direct') != 'auto':
        return
    try:
        status = client.pen_status()
        args.pen = 'native' if status.get('usePenPressure') else 'direct'
        args.pressure_off = not status.get('usePenPressure')
        if args.pen == 'native' and getattr(args, 'colour', 'bucket') == 'brush' \
                and not getattr(args, 'native_layers', True):
            args.pen = 'direct'             # the plugin cannot place pen strokes on virtual layers
    except Exception:
        args.pen = 'direct'                 # older plugin without the pen endpoint
        args.pressure_off = True


HUMAN_SPEED_FLOOR = 0.30       # a drawing hand can be about three times faster than its natural pace
PEN_DOWN_REST_MS = 70           # the pen rests this long after touch-down (see anime_human.PEN_DOWN_MS)


def scaled_pen(samples, scale):
    """The pen samples at `scale` of their natural duration (the touch-down rest stays)."""
    if scale >= 0.999 or len(samples) < 3:
        return samples
    out = []
    for t, x, y, p in samples:
        moved = max(0.0, t - PEN_DOWN_REST_MS) * scale
        out.append([int(round(min(t, PEN_DOWN_REST_MS) + moved)), x, y, p])
    for i in range(1, len(out)):                    # strictly increasing, every segment >= 4 ms
        out[i][0] = max(out[i][0], out[i - 1][0] + 4)
    return out


def native_pen(client, op, args, stats, scale=1.0):
    """Draw a line through the game's virtual pen. False = use the direct path instead."""
    if op.kind != 'line' or not op.pen or getattr(args, 'pen', 'direct') != 'native' \
            or getattr(args, 'pen_disabled', False):
        return False
    result = {}
    for attempt in range(2):
        try:
            result = client.pen_stroke(scaled_pen(op.pen, scale), force=args.force,
                                       vlayer=getattr(op, 'vlayer', None))
        except Exception as error:
            result = {'error': str(error)}
        created = int(result.get('created', 0))
        if created <= 1 or attempt:
            break
        # more than one element: the pen press did something else (somebody picked a
        # tool in the game while it was drawing). Take it back, pick the brush, once more.
        for _ in range(created):
            try:
                client.undo()
            except Exception:
                break
        leave_fill_tool(client)
    if int(result.get('created', 0)) != 1:
        stats.pen_failures.append(result)
        stats.pen_streak += 1
        if stats.pen_streak >= 3:
            args.pen_disabled = True        # the game keeps ignoring the pen; carry on directly
        return False
    stats.pen_streak = 0
    stats.pen_strokes += 1
    if 'brushSize' in result:
        stats.pen_size_error += abs(int(result['brushSize']) - int(op.size))
    return True


def repair_and_pour(client, op, args, stats, attempts=3):
    """The fill flooded: undo it, find the gap in the lines the game really has, close it, pour again.

    The strokes in the game differ a little from the plan (the game simplifies the
    points it records), so a gap the offline model did not see can exist.
    """
    import bucket
    world_w = float(getattr(args, 'canvas_world_width', 17.7778) or 17.7778)
    aspect = float(getattr(args, 'canvas_aspect', None) or 16 / 9)
    grid = bucket.Grid(world_w, world_w / aspect)
    seed = np.round(grid.to_cells(op.points)[0]).astype(int)
    radius = int(1.6 * math.sqrt(max(op.expect or 0.01, 1e-4) * grid.W * grid.H / math.pi)) + 12
    box = (seed[0] - radius, seed[1] - radius, seed[0] + radius, seed[1] + radius)
    for _ in range(attempts):
        lines = client.lines(100000)
        blocked = np.zeros((grid.H, grid.W), np.uint8)
        for item in lines.get('lines', []):
            if item['type'] == 0 and len(item['points']) > 1 and item.get('layer', 0) == (op.layer or 0):
                bucket.stroke_wall(grid, blocked, item['points'], item['size'])
        plug = bucket.plug_leak(blocked, tuple(seed), box)
        if plug is None:
            return False
        a, b = plug
        direction = (b - a) / max(1e-6, float(np.hypot(*(b - a))))
        pts = grid.to_normalised(np.array([a - direction * 1.5, b + direction * 1.5]))
        client.draw_strokes([{'type': 'line', 'points': pts.tolist(), 'color': op.color,
                              'size': 4, 'layer': op.layer or 0}], clear=False, force=args.force)
        stats.repairs += 1
        result = client.fill(op.points, color=op.color, force=args.force)
        leave_fill_tool(client)
        regions = result.get('regions') or []
        area = float(regions[-1].get('area', 0.0)) if regions else None
        if int(result.get('filled', 0)) > 0 and area is not None and \
                area <= (op.expect or 0.0) * LEAK_FACTOR + 0.004:
            stats.log[-1] = stats.log[-1] + ('repaired',)
            stats.poured += 1
            return True
        client.undo()
    return False


def leave_fill_tool(client):
    """Switch the active tool back to the brush after a pour.

    The game keeps a new fill "selectable" until another tool is picked, and a selectable
    fill is repainted with the active colour by the next pen press. Picking the brush
    ends that, like a person does when they go back to drawing.
    """
    try:
        client.tool('Brush')
    except Exception:
        pass


UNDERFILL = 0.35               # a pour this much smaller than predicted missed its region


def _pour_once(client, op, point, args):
    """One bucket click; returns (area share of the canvas or None, filled?)."""
    result = client.fill([point], color=op.color, force=args.force)
    leave_fill_tool(client)
    regions = result.get('regions') or []
    area = float(regions[-1].get('area', 0.0)) if regions else None
    return area, int(result.get('filled', 0)) > 0


def send_bucket(client, op, args, stats):
    """Pour one region and check it against the model.

    Far too big = it leaked: undo, find the gap in the lines the game really has, close
    it, pour again. Far too small = the click landed in a pocket next to a wall: undo and
    click somewhere else in the region (the planner stored a few places)."""
    expect = op.expect
    area, filled = _pour_once(client, op, op.points[0], args)
    if not filled:
        stats.missed += 1
        return 0, 1
    stats.log.append((expect, area))
    if area is not None and expect is not None and area > expect * LEAK_FACTOR + 0.004:
        client.undo()
        stats.log[-1] += ('undone',)
        if repair_and_pour(client, op, args, stats):
            return 1, 0
        stats.leaked += 1
        return 0, 1
    if area is not None and expect is not None and expect > 0.0015 and area < UNDERFILL * expect:
        for point in (op.alt or []):
            client.undo()
            stats.retries += 1
            area, filled = _pour_once(client, op, point, args)
            if filled and area is not None and area >= 0.6 * expect and \
                    area <= expect * LEAK_FACTOR + 0.004:
                stats.log[-1] += ('retried',)
                stats.poured += 1
                return 1, 0
        if area is not None and area < UNDERFILL * expect:
            client.undo()                          # nothing better: do not leave a speck
            stats.log[-1] += ('undone-small',)
            stats.missed += 1
            return 0, 1
    stats.poured += 1
    return 1, 0


def cost_arrays(ops, args):
    """Per-op (fixed, flexible) seconds. Fixed = requests that cannot shrink
    (the setters and the draw call); flexible = the pauses a hand may shorten."""
    fixed, flex = [], []
    color = size = None
    layer = 0
    for op in ops:
        f = REQUEST_ALLOWANCE * len(op_requests(op))
        x = stroke_delay(op, args)
        if op.vlayer is None and op.layer != layer:
            f += REQUEST_ALLOWANCE
            layer = op.layer
        if op.dur is None and op.color != color:
            f += REQUEST_ALLOWANCE
            x += COLOR_PAUSE
            color = op.color
        if op.dur is None and op.kind in ('line', 'disc') and op.size != size:
            f += REQUEST_ALLOWANCE
            x += BRUSH_PAUSE
            size = op.size
        fixed.append(f)
        flex.append(x)
    return fixed, flex


FLAT_PHASE_ORDER = {'sketch': 0, 'color': 1, 'shade': 2, 'detail': 3}
FLAT_INSET_PX = 1          # at FLAT_RASTER_WIDTH; larger values open seams between colours
FLAT_RASTER_WIDTH = 1280


def inset_fill(op, aspect, inset=None):
    """Shrink one fill polygon a few pixels so it does not bury the sketch.

    Returns replacement fills (the eroded mass can split in two) or, if the
    shape is too small to survive, the original fill unchanged.
    """
    from dg_artist import closed, simple_polygon
    inset = FLAT_INSET_PX if inset is None else inset
    w = FLAT_RASTER_WIDTH
    h = max(2, int(round(w / aspect)))
    pts = np.array([[p[0] * w, (1.0 - p[1]) * h] for p in op.points], np.float32)
    mask = np.zeros((h, w), np.uint8)
    cv2.fillPoly(mask, [np.round(pts).astype(np.int32)], 255)
    area = float(np.count_nonzero(mask))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * inset + 1, 2 * inset + 1))
    eroded = cv2.erode(mask, kernel)
    if np.count_nonzero(eroded) < 0.2 * area:
        return [op]
    contours, _ = cv2.findContours(eroded, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = []
    for contour in contours:
        if cv2.contourArea(contour) < 0.02 * area:
            continue
        polygon = simple_polygon(contour, epsilons=(max(1.0, w * 0.0035), max(1.0, w * 0.002)))
        if polygon is None or len(polygon) < 3:
            continue
        ring = [[float(x) / w, 1.0 - float(y) / h] for x, y in polygon]
        out.append(dataclasses.replace(op, points=closed(ring)))
    return out or [op]


def adapt_layers(ops, total_layers, aspect=16 / 9):
    """Fit a plan to the layers the game really has.

    Plans are built for three layers (colour under ink), but some modules, such
    as the relay round, have a single canvas and reject layer switches. There
    the order stays a person's: sketch, then colour, then shading, then the
    final line work. Colour would bury the sketch, so each flat fill is shrunk
    a few pixels first, like colouring inside the lines; the closing line work
    goes over the rest.
    """
    top = max(0, int(total_layers) - 1)
    if top >= max((op.layer for op in ops), default=0) or any(op.vlayer is not None for op in ops):
        return ops
    ordered = sorted(ops, key=lambda op: FLAT_PHASE_ORDER.get(op.phase, 9))
    result = []
    for op in ordered:
        if op.kind == 'fill' and top == 0:
            result.extend(inset_fill(op, aspect))
        else:
            result.append(op)
    return [dataclasses.replace(op, layer=min(op.layer, top)) for op in result]


def execute_instant(client, ops, deadline, args, progress_path, start):
    """Send everything back to back in batches; stop before the deadline."""
    from dg_artist import write_progress
    from pacing import LATENCY_MARGIN
    stats = FillStats()
    batch_size = max(1, int(getattr(args, 'batch', 20) or 20))
    total = len(ops)
    sent = applied = failed = 0
    latency = 0.25
    i = 0
    layer = 0
    while i < total:
        group = [ops[i]]
        while (group[0].kind != 'bucket' and len(group) < batch_size and i + len(group) < total and
               ops[i + len(group)].kind != 'bucket' and
               ops[i + len(group)].layer == group[0].layer and
               ops[i + len(group)].vlayer == group[0].vlayer):
            group.append(ops[i + len(group)])
        if time.monotonic() + latency * LATENCY_MARGIN > deadline:
            break
        if group[0].vlayer is None and group[0].layer != layer:
            client.layer(group[0].layer)
            layer = group[0].layer
        requests = [r for op in group for r in op_requests(op)]
        t0 = time.monotonic()
        try:
            if group[0].kind == 'bucket':
                ok, bad = send_bucket(client, group[0], args, stats)
                applied += ok
                failed += bad
            else:
                result = client.draw_strokes(requests, clear=False, force=args.force)
                applied += int(result.get('applied', 0))
                failed += int(result.get('failed', 0))
        except Exception:
            failed += len(requests)
        latency = max(0.05, 0.6 * latency + 0.4 * (time.monotonic() - t0))
        sent += len(group)
        i += len(group)
        write_progress(progress_path, status='running', sent=sent, ops=total,
                       applied=applied, failed=failed,
                       elapsed=round(time.monotonic() - start, 2), phase=group[-1].phase)
    args.fill_stats = stats
    return sent, applied, failed


def execute_study(client, ops, budget, args):
    from dg_bridge import BridgeError
    from dg_artist import write_progress
    from pacing import (LatencyTracker, RECHECK_EVERY, SPEED_FLOOR, pace_scale,
                        tighten_deadline)
    start = time.monotonic()
    deadline = getattr(args, 'study_deadline', start + budget.usable_seconds)
    state = client.state()
    if not state.get('available'):
        raise BridgeError(state.get('hint', 'no drawing module'))
    if not state.get('canDraw') and not args.force:
        raise BridgeError('The game reports canDraw=false.')
    deadline = tighten_deadline(client, args, deadline, time.monotonic())
    layered = 'layers' in state
    aspect = (getattr(args, 'canvas_aspect', None) or
              (state.get('canvas') or {}).get('aspect') or 16 / 9)
    ops = adapt_layers(ops, (state.get('layers') or {}).get('total', 1) if layered else 1,
                       float(aspect))
    if ops and any(o.dur is not None for o in ops):
        if any(o.vlayer is not None for o in ops):
            args.colour = 'brush'           # a plan made for virtual layers (also when loaded with --plan-in)
            if not hasattr(args, 'native_layers'):
                from layers import capabilities
                info = capabilities(client)
                if not info:
                    raise BridgeError('This plan uses virtual layers, but the plugin offers none '
                                      '(old plugin, or the game was not restarted). Plan again with --colour bucket.')
                args.native_layers = bool(info.get('nativePatch'))
        resolve_pen(client, args)
        if getattr(args, 'pressure_off', False):
            # no pen pressure in this game: a hand moves the size slider, it cannot vary it per stroke
            from anime_human import quantize_size
            for o in ops:
                if o.kind == 'line' and o.dur is not None:
                    o.size = quantize_size(o.size)
    else:
        ops = fit_plan(ops, max(0, deadline - time.monotonic()), args, budget.units)
    progress_path = getattr(args, 'progress_file', None)
    floor = HUMAN_SPEED_FLOOR if any(o.dur is not None for o in ops) else SPEED_FLOOR
    total = len(ops)
    write_progress(progress_path, status='running', sent=0, ops=total,
                   applied=0, failed=0, elapsed=0.0, phase='')
    if ops and time.monotonic() + stroke_seconds(ops[0], args) <= deadline:
        if args.clear:
            client.clear(force=args.force)
        if layered and time.monotonic() < deadline:
            client.layer(0)
    if getattr(args, 'pace', 'slow') == 'instant':
        sent, applied, failed = execute_instant(client, ops, deadline, args,
                                                progress_path, start)
        return finish_study(progress_path, total, sent, applied, failed, start,
                            getattr(args, 'fill_stats', None))

    fixed, flex = cost_arrays(ops, args)
    tail_fixed = np.concatenate((np.cumsum(fixed[::-1])[::-1], [0.0])) if ops else np.zeros(1)
    tail_flex = np.concatenate((np.cumsum(flex[::-1])[::-1], [0.0])) if ops else np.zeros(1)
    tracker = LatencyTracker(REQUEST_ALLOWANCE)
    limit = total                      # ops at or after `limit` were dropped to fit
    applied = failed = sent = 0
    current_color = current_size = None
    current_layer = 0
    pause_rng = random.Random(args.seed + 17)
    stats = FillStats()
    hesitation_at = pause_rng.randint(18, 34)
    last_check = time.monotonic()
    index = 0
    while index < limit:
        op = ops[index]
        now = time.monotonic()
        if now - last_check >= RECHECK_EVERY:
            deadline = tighten_deadline(client, args, deadline, now)
            last_check = now
        # Time left for pauses = what remains minus the unavoidable request time
        # of this and every later stroke (measured latency, with a margin).
        # Behind schedule: shorten the pauses. Still short at the floor: drop
        # the last strokes, which are the least important ones.
        factor = tracker.factor()
        while limit > index:
            need_fixed = (tail_fixed[index] - tail_fixed[limit]) * factor
            need_flex = tail_flex[index] - tail_flex[limit]
            scale = pace_scale(deadline - now - need_fixed, need_flex)
            if scale >= floor or limit - index <= 1:
                break
            limit -= 1
        if limit <= index:
            break
        need_fixed = (tail_fixed[index] - tail_fixed[limit]) * factor
        if now + fixed[index] * factor > deadline:
            break
        scale = pace_scale(deadline - now - need_fixed, tail_flex[index] - tail_flex[limit])
        delay = stroke_delay(op, args) * scale
        t_before = time.monotonic()
        try:
            if op.vlayer is None and op.layer != current_layer:
                client.layer(op.layer)
                current_layer = op.layer
            native_line = (op.kind == 'line' and op.pen and getattr(args, 'pen', 'direct') == 'native'
                           and not getattr(args, 'pen_disabled', False))
            if (op.dur is None or native_line) and op.color != current_color:
                if time.monotonic() + REQUEST_ALLOWANCE + COLOR_PAUSE * scale > deadline:
                    break
                client.color(op.color)
                current_color = op.color
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(COLOR_PAUSE * scale, remaining))
            if op.dur is None and op.kind in ('line', 'disc') and op.size != current_size:
                if time.monotonic() + REQUEST_ALLOWANCE + BRUSH_PAUSE * scale > deadline:
                    break
                client.brush(op.size)
                current_size = op.size
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(BRUSH_PAUSE * scale, remaining))
            if time.monotonic() + REQUEST_ALLOWANCE + delay > deadline:
                break
            if op.dur is not None:
                # The pen is down for the stroke's duration and the stroke reaches
                # the other players when it lifts, so wait first and send afterwards.
                # A native pen stroke takes its own time in the game: only the pause.
                native = (op.kind == 'line' and op.pen and getattr(args, 'pen', 'direct') == 'native'
                          and not getattr(args, 'pen_disabled', False))
                wait = (op.pause or 0.0) * scale if native else delay
                time.sleep(min(wait, max(0.0, deadline - time.monotonic())))
            t_draw = time.monotonic()
            if op.kind == 'bucket':
                ok, bad = send_bucket(client, op, args, stats)
                applied += ok
                failed += bad
            elif native_pen(client, op, args, stats, scale):
                applied += 1
            else:
                result = client.draw_strokes(op_requests(op), clear=False, force=args.force)
                tracker.add((time.monotonic() - t_draw) / max(1, len(op_requests(op))))
                applied_now = int(result.get('applied', 0))
                failed_now = int(result.get('failed', 0))
                applied += int(applied_now > 0)
                failed += int(bool(failed_now) or applied_now == 0)
        except Exception as error:
            failed += 1
            stats.last_error = f'{type(error).__name__}: {error}'
        sent += 1
        index += 1
        write_progress(progress_path, status='running', sent=sent, ops=total,
                       applied=applied, failed=failed,
                       elapsed=round(time.monotonic() - start, 2), phase=op.phase)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Humans pause unevenly: now and then they stop to look at the drawing.
        # The look is a luxury and only happens when the schedule is comfortable.
        pause = 0.0 if op.dur is not None else delay
        if op.dur is None and sent >= hesitation_at:
            extra = pause_rng.uniform(0.25, 0.85)
            hesitation_at = sent + pause_rng.randint(22, 48)
            if scale >= 0.999 and remaining > (tail_flex[index] - tail_flex[limit]) + extra + \
                    (tail_fixed[index] - tail_fixed[limit]) * factor + 2.0:
                pause += extra
        time.sleep(min(pause, remaining))
    args.fill_stats = stats
    return finish_study(progress_path, total, sent, applied, failed, start,
                        getattr(args, 'fill_stats', None))


def finish_study(progress_path, total, sent, applied, failed, start, fills=None):
    from dg_artist import write_progress
    result = {'ops': total, 'sent': sent, 'applied': applied, 'failed': failed,
              'real_fills': fills.poured if fills else 0, 'fallbacks': 0,
              'leaked_fills': fills.leaked if fills else 0,
              'pen_strokes': fills.pen_strokes if fills else 0,
              'repairs': fills.repairs if fills else 0,
              'retries': fills.retries if fills else 0,
              'pen_failures': fills.pen_failures[:5] if fills else [],
              'last_error': getattr(fills, 'last_error', '') if fills else '',
              'pen_size_error': fills.pen_size_error if fills else 0,
              'pour_log': [[round(e or 0, 4), round(a or 0, 4)] + list(r) for e, a, *r in fills.log] if fills else [],
              'elapsed': round(time.monotonic() - start, 2)}
    write_progress(progress_path,
                   status='done' if sent >= total else 'stopped',
                   sent=sent, ops=total, applied=applied, failed=failed,
                   elapsed=result['elapsed'], phase='done')
    return result
