"""`anime_human`: the picture of `anime_study`, drawn the way an illustrator works.

`build_study` decides WHAT to draw (line art, colour areas, brush masses, shading).
This module decides HOW a person would draw it with a pen and a paint bucket:

  layer 1  line art, part by part, thin, each stroke with its own pen pressure;
  layer 0  "flatting": for every colour area the border is traced in the area's own
           colour where no line exists yet, then the bucket is poured; small or thin
           areas are dabbed with the pen instead; brush masses and shading follow;
  layer 1  the finer details last.

The game's bucket only sees the lines of the current layer (LayerDrawModule limits
the fill to it), so the ink never gets in its way. bucket.py holds an offline copy of
the fill rules; every pour is checked against it before it is planned, and the
executor checks the real fill area again and repairs a leak from the lines the game
really has.
"""
from __future__ import annotations

import os
import random

import cv2
import numpy as np

import brush_fill
import bucket
import layers as layer_map
import planlog
from motor import Motor, sample_stroke

REQUEST_ALLOWANCE = 0.06
BUCKET_SECONDS = 0.18
OVERSHOOT_CELLS = 3.0
PEN_DOWN_MS = 70
INK_OVERSHOOT = float(os.environ.get('HAND_INK_OVERSHOOT', 6.0))  # line ends run a little past the join
SIZE_STEPS = (3, 5, 8, 12, 18, 26, 35)   # what a hand without pressure sets on the slider


# ---------------------------------------------------------------------------
# small geometry helpers
# ---------------------------------------------------------------------------

def _length(path):
    path = np.asarray(path, float)
    return float(np.linalg.norm(np.diff(path, axis=0), axis=1).sum()) if len(path) > 1 else 0.0


def _simplify(points, eps, closed=False):
    pts = np.asarray(points, np.float32).reshape(-1, 1, 2)
    out = cv2.approxPolyDP(pts, eps, closed).reshape(-1, 2)
    return out.astype(float) if len(out) >= 2 else np.asarray(points, float)


def _resample(path, step):
    path = np.asarray(path, float)
    if len(path) < 2:
        return path
    seg = np.linalg.norm(np.diff(path, axis=0), axis=1)
    arc = np.concatenate(([0.0], np.cumsum(seg)))
    if arc[-1] < step:
        return path
    n = max(2, int(arc[-1] / step) + 1)
    t = np.linspace(0.0, arc[-1], n)
    return np.column_stack((np.interp(t, arc, path[:, 0]), np.interp(t, arc, path[:, 1])))


def _smooth_path(path, window=5):
    path = np.asarray(path, float)
    if len(path) < window + 2:
        return path
    half = window // 2
    kernel = np.ones(window) / window
    out = path.copy()
    for axis in (0, 1):
        padded = np.pad(path[:, axis], half, mode='edge')
        out[:, axis] = np.convolve(padded, kernel, mode='valid')
    out[0], out[-1] = path[0], path[-1]
    return out


def _extend(path, at_start, distance):
    if distance <= 0 or len(path) < 2:
        return path
    a, b = (path[0], path[min(2, len(path) - 1)]) if at_start else (path[-1], path[max(-3, -len(path))])
    d = a - b
    n = float(np.hypot(*d))
    if n < 1e-9:
        return path
    tip = a + d / n * distance
    return np.vstack((tip, path)) if at_start else np.vstack((path, tip))


def estimate_seconds(length, scale, motor):
    d = motor.cal["stroke_duration"]
    return d["a"] * max(length / scale, 0.01) ** d["b"] + 0.7 + REQUEST_ALLOWANCE


def chain(items, start=None):
    """Order strokes [(path, tag)] so each starts near the pen.

    Horizontal-ish strokes go left to right, others top to bottom (image y grows
    downward), unless the other end is clearly closer to where the pen is.
    """
    todo = list(range(len(items)))
    out = []
    pen = np.asarray(start if start is not None else (0.0, 0.0), float)
    while todo:
        best = None
        for i in todo:
            p = items[i][0]
            delta = p[-1] - p[0]
            habit_forward = (delta[0] >= 0) if abs(delta[0]) > abs(delta[1]) else (delta[1] >= 0)
            span = _length(p)
            for flip in (False, True):
                a = p[-1] if flip else p[0]
                forward = habit_forward != flip
                cost = float(np.hypot(*(a - pen))) + (0.0 if forward else 0.18 * span)
                if best is None or cost < best[0]:
                    best = (cost, i, flip)
        _, i, flip = best
        todo.remove(i)
        p = items[i][0][::-1] if flip else items[i][0]
        out.append((p, items[i][1]))
        pen = p[-1]
    return out


def cluster_parts(centres, count, seed):
    """Group stroke centres into body parts by position; clusters ordered top first."""
    n = len(centres)
    if n <= 6 or count < 2:
        return [list(range(n))]
    data = np.asarray(centres, np.float32)
    count = min(count, n)
    cv2.setRNGSeed(seed)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    _, labels, centers = cv2.kmeans(data, count, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
    groups = [[i for i in range(n) if labels[i, 0] == k] for k in range(count)]
    order = sorted(range(count), key=lambda k: (centers[k][1], centers[k][0]))
    return [groups[k] for k in order if groups[k]]


def quantize_size(size):
    """The slider position a hand without pen pressure would use."""
    return min(SIZE_STEPS, key=lambda s: abs(s - size))


# ---------------------------------------------------------------------------
# the hand
# ---------------------------------------------------------------------------

class Human:
    """Turns a path into an Op the way this person's hand would draw it."""

    def __init__(self, Op, motor, grid, world_w, world_h, ink, scale_world):
        self.Op, self.motor, self.grid = Op, motor, grid
        self.world_w, self.world_h, self.ink = world_w, world_h, ink
        self.scale_world = scale_world        # length of the subject's long side, world units
        self.last_end = None                  # normalised canvas coords of the pen
        self.last_part = None
        self.last_color = None
        self.bands = layer_map.DEFAULT_BANDS

    def _travel(self, norm):
        if self.last_end is None:
            return 0.0
        return float(np.hypot((norm[0] - self.last_end[0]) * self.world_w,
                              (norm[1] - self.last_end[1]) * self.world_h)) / self.scale_world

    def stroke(self, cells, role, note, phase, target_size, layer=0, color=None, part=None,
               think=0.0, continuing=False, speed=1.0, vlayer=None, scrub=False):
        """Op for a path given in grid-cell coordinates, drawn at about `target_size`."""
        if vlayer is not None:
            layer = layer_map.resolve(vlayer, self.bands)[0]
        cells = np.asarray(cells, float)
        if len(cells) < 2:
            return None
        norm = self.grid.to_normalised(cells)
        world = np.column_stack((norm[:, 0] * self.world_w, norm[:, 1] * self.world_h))
        duration = self.motor.stroke_duration(_length(world) / self.scale_world, role) / speed
        times = self.motor.timeline(world, duration)
        step = np.linalg.norm(np.diff(world, axis=0), axis=1)
        speeds = step / np.maximum(np.diff(times), 1e-4)
        speeds = np.concatenate((speeds[:1], speeds)) if len(speeds) else None
        curve, size = self.motor.pressure_for_size(times, role, speeds, target_size)
        kept, kept_t = sample_stroke(world, times, with_times=True, fps=self.motor.fps)
        recorded = _simplify(kept, 0.012)
        if len(recorded) < 2:
            return None
        points = [[float(x / self.world_w), float(y / self.world_h)] for x, y in recorded]
        frame_t = np.linspace(0.0, float(times[-1]), len(curve))
        pen = [[int(round(t * 1000.0)) + PEN_DOWN_MS, round(float(x / self.world_w), 5),
                round(float(y / self.world_h), 5), round(float(np.interp(t, frame_t, curve)), 3)]
               for (x, y), t in zip(kept, kept_t)]
        # the pen touches down and rests a moment before it moves: the game starts the
        # line a frame or two after the press, and would otherwise cut the first stretch off
        pen.insert(0, [0, pen[0][1], pen[0][2], pen[0][3]])
        duration += PEN_DOWN_MS / 1000.0
        if part is not None and self.last_part is not None and part != self.last_part:
            think += self.motor.think()
        if color is not None and self.last_color is not None and color != self.last_color:
            think += 0.25                                   # picking another colour
        if self.last_end is None:
            pause = 0.4
        elif continuing:
            # scrubbing an area: the hand hardly leaves the paper between two sweeps
            pause = (0.4 * self.motor.pause_continue() + 0.15 * self._travel(norm[0])) if scrub \
                else self.motor.pause_continue() + 0.3 * self._travel(norm[0])
        else:
            pause = self.motor.pause(self._travel(norm[0]), think)
        op = self.Op('line', points, color or self.ink, size, layer, phase, 1.0, note=note)
        op.dur, op.pause, op.pen = duration, pause, pen
        op.vlayer = vlayer
        self.last_end = norm[-1]
        if part is not None:
            self.last_part = part
        if color is not None:
            self.last_color = color
        return op

    def pour(self, seed_cell, color_hex, expect, phase, layer=0, part=None, think_scale=0.5, alt=()):
        norm = self.grid.to_normalised(np.array([seed_cell], float))[0]
        think = self.motor.think(think_scale)
        if self.last_color is not None and color_hex != self.last_color:
            think += 0.35
        op = self.Op('bucket', [[float(norm[0]), float(norm[1])]], color_hex, 0, layer, phase, 1.0,
                     note='human-bucket')
        op.dur, op.pause = BUCKET_SECONDS, self.motor.pause(self._travel(norm), think)
        op.expect = expect
        if alt:
            op.alt = [[float(a[0]), float(a[1])] for a in self.grid.to_normalised(np.array(alt, float))]
        self.last_end = norm
        self.last_color = color_hex
        if part is not None:
            self.last_part = part
        return op


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

INK_NOTES = ('study-structure', 'study-centerline')
BORDER_SIZE = int(os.environ.get('HAND_BORDER_SIZE', 4))      # pen size of the area borders and closing strokes
OUTLINE_LIMIT = 0.62              # the outlines of the colour areas are done by this share of the round
BORDER_DARKNESS = float(os.environ.get('HAND_BORDER_DARK', 0.62))   # border colour = area colour x this
BRUSH_COARSE, BRUSH_FINE, BRUSH_BORDERS = 0.72, 0.84, 0.90   # brush colouring: sweeps, rims, borders done by
INK_SHARE = 0.38                  # line art (outline + details) share of the round
SHARES = {'sketch': None, 'flats': 0.88}               # sketch: share for the outline lines, None = derived
if os.environ.get('HAND_SHARES'):                      # tuning: sketch,flats
    SHARES['sketch'], SHARES['flats'] = (float(v) for v in os.environ['HAND_SHARES'].split(','))


def build_human(analysis, budget, args, transform):
    from dg_artist import Op, hex_color, parse_hex
    from anime_study import build_study, humanize
    from study_lineart import limit_length

    debug = bool(os.environ.get('HAND_DEBUG'))
    # brush colouring only: the bucket needs closed areas, so it always gets its borders
    area_borders = getattr(args, 'borders', 'on') != 'off'

    def line_color(fill_hex):
        """Border and closing strokes: the area's colour, darker, so they read as line work."""
        return hex_color(tuple(v * BORDER_DARKNESS for v in parse_hex(fill_hex)))

    plan = build_study(analysis, budget, args, transform, fit=False)
    if not plan:
        return []
    w, h = analysis.width, analysis.height
    usable = max(0.0, budget.usable_seconds - min(10.0, budget.usable_seconds * 0.15))
    rng = random.Random(args.seed)
    motor = Motor(seed=args.seed, skill=getattr(args, 'human_skill', 1.0),
                  pressure_scale=getattr(args, 'human_pressure', 1.0),
                  fps=getattr(args, 'game_fps', 240.0))
    world_w = float(getattr(args, 'canvas_world_width', 17.7778) or 17.7778)
    aspect = float(getattr(args, 'canvas_aspect', None) or 16 / 9)
    world_h = world_w / aspect
    grid = bucket.Grid(world_w, world_h)
    layered = budget.layers >= 2
    subject = analysis.cache['study_subject']
    ys, xs = np.where(subject > 0)
    scale_px = float(max(xs.max() - xs.min(), ys.max() - ys.min(), 1))
    src = np.float32([[0, 0], [w, 0], [0, h]])
    dst = np.float32(grid.to_cells([transform(float(x), float(y)) for x, y in src]))
    M = cv2.getAffineTransform(src, dst)
    scale_cells = scale_px * float(np.hypot(M[0, 0], M[1, 0]))
    cut = analysis.cache.get('frame_cut')
    band_g = None
    if cut is not None and cut.band is not None:
        band_g = cv2.warpAffine(cut.band.astype(np.uint8), M, (grid.W, grid.H),
                                flags=cv2.INTER_NEAREST) > 0

    def edge_color(path_cells, fill_hex):
        """Where the figure fades out at a cut of the frame there is no dark border: the
        colour ends against an invisible wall, in its own colour."""
        if band_g is not None:
            idx = np.clip(np.round(np.asarray(path_cells, float)).astype(int), 0,
                          [grid.W - 1, grid.H - 1])
            if band_g[idx[:, 1], idx[:, 0]].mean() > 0.5:
                return fill_hex
        return line_color(fill_hex)

    human = Human(Op, motor, grid, world_w, world_h, args.line_color or '#34333D',
                  scale_cells * grid.density)
    area_to_norm = 1.0 / float(grid.W * grid.H)

    ink_lines = [o for o in plan if o.kind == 'line' and o.note in INK_NOTES]
    fills = [o for o in plan if o.kind == 'fill']
    masses = [o for o in plan if o.kind == 'line' and o.note == 'study-color']
    shades = [o for o in plan if o.kind == 'line' and o.note == 'study-shade']
    taken = {id(o) for o in ink_lines + fills + masses + shades}
    masses += [o for o in plan if o.kind == 'line' and id(o) not in taken]   # touch-ups
    # One layer for ink and colour: the line art itself closes most areas, so only the
    # borders it lacks have to be drawn. (Two layers would need every border twice.)
    ink_layer = colour_layer = 0
    roles = getattr(args, 'layer_roles', None) if getattr(args, 'colour', 'bucket') == 'brush' else None
    brush = roles is not None
    human.bands = int(getattr(args, 'layer_bands', layer_map.DEFAULT_BANDS) or layer_map.DEFAULT_BANDS)
    ink_v = roles['ink'] if brush else None
    border_v = ink_v                      # borders and gap closers go on the ink layer in brush mode
    planlog.note(f'{len(ink_lines)} lines, {len(fills)} areas, {len(masses)} brush strokes, '
                 f'{len(shades)} shading strokes in the base plan')

    ops = []

    def total(extra=()):
        return sum((o.pause or 0.0) + (o.dur or 0.0) + REQUEST_ALLOWANCE for o in list(ops) + list(extra))

    def cells_of(op):
        return grid.to_cells(np.asarray(op.points, float))

    def brush_ops(source_ops, phase, role, cap, part_base, vlayer=None, zone=None):
        """Pen strokes for the plan's own brush work (thick scrubs, shading).

        With a `zone` (bool grid mask) the strokes are cut to the stretches where the whole
        brush stays inside it: they lie under the line art and must not smear onto white."""
        if not source_ops:
            return []
        centres = [cells_of(o).mean(axis=0) for o in source_ops]
        made = []
        for pi, members in enumerate(cluster_parts(centres, max(2, round(len(source_ops) / 18)),
                                                    args.seed)):
            for path, op0 in chain([(cells_of(source_ops[i]), source_ops[i]) for i in members]):
                pieces = [path]
                if zone is not None:
                    pieces = [np.asarray(q, float) for q in brush_fill.clip_path(
                        path, op0.size, zone, px_per_size=0.5) if len(q) >= 3]
                for piece in pieces:
                    op = human.stroke(piece, role, f'human-{role}', phase, op0.size, layer=colour_layer,
                                      color=op0.color, part=part_base + pi, vlayer=vlayer)
                    if op is None:
                        continue
                    if total(made) + op.dur + op.pause > cap:
                        return made
                    made.append(op)
        return made

    def ink_ops(source_ops, phase, cap, vlayer=None):
        """Line art by part: head first, long outlines before short details."""
        if not source_ops:
            return []
        order = sorted(range(len(source_ops)), key=lambda i: -_length(cells_of(source_ops[i])))
        keep, spent = [], 0.0
        for i in order:                                   # drop the shortest if time runs out
            cost = estimate_seconds(_length(cells_of(source_ops[i])), scale_cells, motor)
            if spent + cost > cap:
                continue
            keep.append(i)
            spent += cost
        if not keep:
            return []
        centres = [cells_of(source_ops[i]).mean(axis=0) for i in keep]
        made, pen = [], None
        for pi, members in enumerate(cluster_parts(centres, int(np.clip(round(len(keep) / 14), 2, 5)),
                                                   args.seed)):
            group = [(cells_of(source_ops[keep[m]]), source_ops[keep[m]]) for m in members]
            for path, op0 in chain(group, pen):
                path = _extend(_extend(path, True, INK_OVERSHOOT), False, INK_OVERSHOOT)
                role = 'contour' if op0.size >= 4 else 'inner'
                op = human.stroke(path, role, f'human-{role}', phase, max(3, op0.size),
                                  layer=ink_layer, part=(200 + pi) if phase == 'sketch' else 400,
                                  vlayer=vlayer)
                if op is not None:
                    made.append(op)
                pen = path[-1]
        return made

    sketch_src = [o for o in ink_lines if o.phase == 'sketch']
    detail_src = [o for o in ink_lines if o.phase != 'sketch']

    # --- 1. line art (layer 1) ------------------------------------------------------
    planlog.stage('lines')
    # Lines are the skeleton: a picture of colour blobs without them does not stand up.
    # Two thirds of the line time go to the outline and structure first; the rest is kept
    # for the interior details (eyes, folds, strands) that are drawn after the colour.
    sketch_share = SHARES['sketch'] if SHARES['sketch'] is not None else INK_SHARE * 0.65
    detail_reserve = usable * (INK_SHARE - sketch_share)
    ops += ink_ops(sketch_src, 'sketch', usable * sketch_share, ink_v)
    planlog.note(f'{len(ops)} line-art strokes')

    # --- 2. flatting: border in the area's own colour where no line exists, then pour ----
    planlog.stage('fills', detail='flatting')
    walls = np.zeros((grid.H, grid.W), np.uint8)               # bucket walls of the colour layer
    for op in ops:
        if op.kind == 'line' and (brush or op.layer == colour_layer):
            bucket.stroke_wall(grid, walls, op.points, op.size)
    flats_limit = usable * SHARES['flats'] - detail_reserve
    poured = dabbed = skipped = 0
    ratios = []

    def outline_pieces(loop, covered):
        idx = np.clip(np.round(loop).astype(int), 0, [grid.W - 1, grid.H - 1])
        free_pt = ~covered[idx[:, 1], idx[:, 0]]
        n = len(loop)
        if free_pt.all():
            return [np.vstack((loop, loop[1:6]))], OVERSHOOT_CELLS
        runs, i = [], 0
        while i < n:
            if free_pt[i]:
                j = i
                while j + 1 < n and free_pt[j + 1]:
                    j += 1
                runs.append([i, j])
                i = j + 1
            else:
                i += 1
        merged = []
        for run in runs:
            if merged and run[0] - merged[-1][1] <= 12:
                merged[-1][1] = run[1]
            else:
                merged.append(run)
        return [loop[a:b + 1] for a, b in merged if b - a >= 1], OVERSHOOT_CELLS * 2

    def border_strokes(loop, color, part):
        pieces, overshoot = outline_pieces(loop, cv2.dilate(walls, np.ones((5, 5), np.uint8)) > 0)
        made = []
        for piece in pieces:
            if _length(piece) < 3.0:
                continue
            for part_path in (limit_length(piece, 1.1 * scale_cells) if len(piece) > 8 else [piece]):
                path = _resample(_extend(_extend(part_path, True, overshoot), False, overshoot), 2.0)
                wobble = humanize(path, rng, amplitude=0.25, smooth=5, step=2.0, overshoot=0.0)
                drawn = np.asarray(wobble if len(wobble) > 2 else path, float)
                op = human.stroke(drawn, 'detail', 'human-border', 'color', BORDER_SIZE, layer=colour_layer,
                                  color=edge_color(drawn, color), part=part,
                                  continuing=bool(made), speed=1.35, vlayer=border_v)
                if op is not None:
                    made.append(op)
        return made

    def plug_strokes(trial_walls, seed, box, color, attempts, part):
        """Short strokes across the narrowest gap of the escape route."""
        made = []
        for _ in range(attempts):
            cell = bucket.region_at(trial_walls, seed)
            if cell is not None and not bucket.touches_border(cell):
                return cell, made
            plug = bucket.plug_leak(trial_walls, seed, box)
            if plug is None:
                break
            a, b = plug
            direction = (b - a) / max(1e-6, float(np.hypot(*(b - a))))
            line = np.array([a - direction * 1.5, (a + b) / 2, b + direction * 1.5])
            op = human.stroke(line, 'gap_close', 'human-gap', 'color', BORDER_SIZE, layer=colour_layer,
                              color=edge_color(line, color), part=part, continuing=True)
            if op is None:
                break
            made.append(op)
            bucket.stroke_wall(grid, trial_walls, op.points, op.size)
        return bucket.region_at(trial_walls, seed), made

    def dab_strokes(mask, color, part):
        """A thin or tiny area: one pen stroke along its long axis."""
        ys_m, xs_m = np.nonzero(mask)
        pts = np.column_stack((xs_m, ys_m)).astype(np.float32)
        if len(pts) < 3:
            return []
        vx, vy, x0, y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
        proj = (pts[:, 0] - x0) * vx + (pts[:, 1] - y0) * vy
        radius = float(cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 3).max())
        a = np.array([x0 + vx * proj.min(), y0 + vy * proj.min()])
        b = np.array([x0 + vx * proj.max(), y0 + vy * proj.max()])
        size = int(np.clip(round(4.0 * max(radius, 1.0)), 3, 35))
        mid = (a + b) / 2 + np.array([-vy, vx]) * rng.uniform(-0.6, 0.6)
        op = human.stroke(np.array([a, mid, b]), 'inner', 'human-dab', 'color', size,
                          layer=colour_layer, color=color, part=part)
        return [op] if op is not None else []

    if debug:
        areas = []
        for fill in fills:
            m = np.zeros((grid.H, grid.W), np.uint8)
            cv2.fillPoly(m, [np.round(cells_of(fill)).astype(np.int32).reshape(-1, 1, 2)], 1)
            areas.append(int(m.sum()))
        cum = np.cumsum(areas) / max(1, sum(areas))
        print(f'[human] {len(fills)} areas; cumulative share after 10/20/30/40/60: ' + ' '.join(f'{cum[min(k, len(cum)) - 1]:.2f}' for k in (10, 20, 30, 40, 60)))
    pending = []                       # areas whose outline is done, waiting for the bucket

    def outline_pass(first, limit):
        """Give every colour area its line work (biggest first); the pours come afterwards.

        The skeleton first: an area that has no colour yet still has its outline, so
        nothing is left as a hole. Returns the index of the next area."""
        nonlocal dabbed, skipped
        for index in range(first, len(fills)):
            fill = fills[index]
            if total() > limit:
                return index
            cells = cells_of(fill)
            if len(cells) < 4:
                continue
            color = fill.color
            mask = np.zeros((grid.H, grid.W), np.uint8)
            cv2.fillPoly(mask, [np.round(cells).astype(np.int32).reshape(-1, 1, 2)], 1)
            area = int(mask.sum())
            if area < 6:
                continue
            part = 600 + index // 8
            depth = float(cv2.distanceTransform(mask, cv2.DIST_L2, 3).max())
            if depth < 2.2 or area < 40:
                made = dab_strokes(mask > 0, color, part)
                for op in made:
                    bucket.stroke_wall(grid, walls, op.points, op.size)
                ops.extend(made)
                dabbed += 1
                continue
            loop = _resample(_smooth_path(_resample(_simplify(cells, 1.4), 2.5), 5), 2.0)
            seed, _ = bucket.seed_point(mask > 0)
            ys_m, xs_m = np.nonzero(mask)
            box = (int(xs_m.min()), int(ys_m.min()), int(xs_m.max()), int(ys_m.max()))

            def fits(c):
                return c is not None and not bucket.touches_border(c) and \
                    0.75 * area <= np.count_nonzero(c) <= 1.35 * area + 40

            # cheapest first: lines and earlier areas may already close it; then a short
            # stroke across a gap; only then trace the border
            trial_walls = walls.copy()
            trial = []
            cell = bucket.region_at(trial_walls, seed)
            if not fits(cell):
                cell, trial = plug_strokes(trial_walls, seed, box, color, 3, part)
            if not fits(cell):
                trial_walls = walls.copy()
                trial = border_strokes(loop, color, part)
                for op in trial:
                    bucket.stroke_wall(grid, trial_walls, op.points, op.size)
                cell, extra = plug_strokes(trial_walls, seed, box, color, 12, part)
                trial += extra
            if cell is None or bucket.touches_border(cell):
                # still open: go round the whole border once, ignoring what already covers it
                full = []
                for part_path in limit_length(np.vstack((loop, loop[1:6])), 1.1 * scale_cells):
                    path = _resample(_extend(_extend(part_path, True, OVERSHOOT_CELLS), False,
                                             OVERSHOOT_CELLS), 2.0)
                    op = human.stroke(path, 'detail', 'human-border', 'color', BORDER_SIZE, layer=colour_layer,
                                      color=edge_color(path, color), part=part,
                                      continuing=bool(full), speed=1.35)
                    if op is not None:
                        full.append(op)
                        bucket.stroke_wall(grid, trial_walls, op.points, op.size)
                trial += full
                cell, extra = plug_strokes(trial_walls, seed, box, color, 8, part)
                trial += extra
            ops.extend(trial)                       # whatever happens, the outline stays drawn
            for op in trial:
                bucket.stroke_wall(grid, walls, op.points, op.size)
            if cell is None or bucket.touches_border(cell) or np.count_nonzero(cell) > 1.35 * area + 40:
                skipped += 1
                if debug:
                    print(f'[human] area {area} color {color} left open '
                          f'(cell {0 if cell is None else int(np.count_nonzero(cell))})')
                continue
            ratios.append(np.count_nonzero(cell) / area)
            pending.append({'index': index, 'mask': mask > 0, 'area': area, 'color': color,
                            'seed': seed, 'box': box, 'part': part, 'done': False})
        return len(fills)

    def pour_pass(limit):
        """Pour the outlined areas, biggest first, against the lines that now really exist."""
        nonlocal poured, skipped
        for st in sorted(pending, key=lambda r: -r['area']):
            if st['done']:
                continue
            if total() > limit:
                return
            st['done'] = True
            mask, area, color, seed, part = st['mask'], st['area'], st['color'], st['seed'], st['part']
            cell = bucket.region_at(walls, seed)
            if cell is None or bucket.touches_border(cell):
                cell, extra = plug_strokes(walls, seed, st['box'], color, 3, part)
                ops.extend(extra)
            if cell is None or bucket.touches_border(cell) or np.count_nonzero(cell) > 1.35 * area + 40:
                skipped += 1
                continue
            cands = bucket.seed_candidates(cell, 3)
            seed = cands[0][0] if cands else seed          # the deepest point of the real cell
            pours = [(seed, int(np.count_nonzero(cell)), [c[0] for c in cands[1:]])]
            covered_by = cell.copy()
            for _extra in range(8):                     # the area was cut into pieces by lines: pour each
                rest = mask & ~(cv2.dilate(covered_by.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0)
                rest = cv2.morphologyEx(rest.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)) > 0
                count, comp, stats, _ = cv2.connectedComponentsWithStats(rest.astype(np.uint8), 8)
                if count < 2:
                    break
                k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                if stats[k, cv2.CC_STAT_AREA] < max(14, 0.04 * area):
                    break
                seed_k, _ = bucket.seed_point(comp == k)
                piece = bucket.region_at(walls, seed_k)
                covered_by |= (comp == k)                # whatever happens, do not try this piece again
                if piece is not None and not bucket.touches_border(piece) and \
                        np.count_nonzero(piece) <= 1.35 * stats[k, cv2.CC_STAT_AREA] + 40:
                    cands_k = bucket.seed_candidates(piece, 3)
                    pours.append((cands_k[0][0] if cands_k else seed_k, int(np.count_nonzero(piece)),
                                  [c[0] for c in cands_k[1:]]))
                    covered_by |= piece
            for seed_p, count_p, alt_p in pours:
                ops.append(human.pour(seed_p, color, count_p * area_to_norm, 'color',
                                      layer=colour_layer, part=part, think_scale=0.2, alt=alt_p))
            poured += len(pours)

    def brush_flow():
        """Colour with the brush: sweeps, then rims and corners, then dark borders where no line is.

        Colour goes on its own virtual layers under the ink, so it can run over the line art
        without hiding it (and a gap in the lines costs nothing: there is no bucket to leak)."""
        nonlocal dabbed
        planlog.stage('fills', detail='brush')
        masks, areas = [], []
        for fill in fills:
            m = np.zeros((grid.H, grid.W), np.uint8)
            cv2.fillPoly(m, [np.round(cells_of(fill)).astype(np.int32).reshape(-1, 1, 2)], 1)
            masks.append(m > 0)
            areas.append(int(m.sum()))
        total_area = max(1, sum(areas))
        upper = [a < 0.02 * total_area for a in areas]              # small areas lie on top of big ones
        ink_zone = np.zeros((grid.H, grid.W), np.uint8)
        for op in list(ops) + list(detail_src):
            if op.kind == 'line' and len(op.points) > 1:
                cv2.polylines(ink_zone, [np.round(cells_of(op)).astype(np.int32).reshape(-1, 1, 2)], False, 1,
                              max(1, int(round(op.size / 2.0 - 0.5))))
        ink_zone = ink_zone > 0
        cur = {'color': '#000000', 'v': roles['flat'], 'count': 0}

        def fill_stroke(cells, size, role, part):
            op = human.stroke(cells, role, 'human-fill', 'color', size, layer=colour_layer, color=cur['color'],
                              part=part, continuing=cur['count'] > 0, speed=2.2, vlayer=cur['v'], scrub=True)
            if op is not None:
                cur['count'] += 1
            return op

        painter = brush_fill.Painter(grid, fill_stroke, rng, scale_cells)
        painter.debug = debug
        # A brush may run over the line art and over every area that is painted later (that
        # colour covers it), never over bare paper or an area that is already finished.
        order = paint_order(masks, areas, [f.color for f in fills])
        position = {i: k for k, i in enumerate(order)}
        states, later = {}, np.zeros((grid.H, grid.W), bool)
        reach_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (37, 37))
        for i in reversed(order):
            if areas[i] >= 6:
                near = cv2.dilate(masks[i].astype(np.uint8), reach_kernel) > 0
                core = (cv2.erode(masks[i].astype(np.uint8), np.ones((3, 3), np.uint8)) > 0) & ~ink_zone
                if core.sum() < 0.5 * areas[i]:            # thin shape: the edge is all there is
                    core = masks[i] & ~ink_zone
                states[i] = painter.prepare(core, masks[i] | ((ink_zone | later) & near),
                                            600 + position[i] // 8, tight_cells=masks[i] | (ink_zone & near))
            if debug and i < 12:
                print(f'[human]   area {i}: {areas[i] / total_area:.3f} of the area sum, '
                      f'{np.count_nonzero(masks[i] & later) / max(1, areas[i]):.2f} of it lies under later areas')
            later |= masks[i]
        painted = [i for i in order if states.get(i) is not None]

        def paint_pass(order, limit, rounds, target_big, target_small, tight):
            """One pass over the areas. Returns the number of areas left out for lack of time."""
            area_left = max(1, sum(areas[i] for i in order))
            left_out = 0
            for i in order:
                st = states[i]
                if total() > limit:
                    left_out += 1
                    continue
                if tight:
                    painter.tighten(st)
                cur.update(color=fills[i].color, count=0, v=roles['flat_top'] if upper[i] else roles['flat'])
                painter.spent = 0.0
                allowance = max(1.2, 1.6 * (limit - total()) * areas[i] / area_left)
                made = painter.run(st, ('coarse', 'fine') if not tight else ('fine',), allowance,
                                   target=target_small if areas[i] < 0.01 * total_area else target_big,
                                   rounds=rounds)
                ops.extend(made)
                area_left -= areas[i]
                if debug:
                    print(f'[human]   {"refine" if tight else "area"} {i} share {areas[i] / total_area:.3f} '
                          f'{"top" if upper[i] else "flat"}: {len(made)} strokes {painter.spent:.1f}s '
                          f'cover {painter.share(st):.2f}')
            return left_out

        # first every area gets its colour roughly (big brush, few strokes), then the edges are refined
        unpainted = paint_pass(painted, usable * BRUSH_COARSE, 1, 0.80, 0.70, False)
        missing = {i: (1.0 - painter.share(states[i])) * areas[i] for i in painted if painter.share(states[i]) < 0.96}
        by_colour = {}
        for i in missing:
            by_colour[fills[i].color] = by_colour.get(fills[i].color, 0.0) + missing[i]
        refine = sorted(missing, key=lambda i: (-by_colour[fills[i].color], fills[i].color, -missing[i]))
        paint_pass(refine, usable * BRUSH_FINE, 3,
                   brush_fill.COVER_TARGET, 0.88, True)
        states = {i: states[i] for i in painted}
        shares = [painter.share(st) for st in states.values()]
        planlog.note(f'brush colour: {len(states)} areas, mean cover {np.mean(shares) if shares else 0:.3f}, '
                     f'{unpainted} areas without colour')
        if debug:
            print(f'[human] brush: {len(fills)} areas, {len(states)} prepared, {unpainted} without colour after '
                  f'the sweeps, cover mean {np.mean(shares) if shares else 0:.3f} '
                  f'min {np.min(shares) if shares else 0:.3f}; t={total():.0f}s of {usable:.0f}s')
        # borders: where no line exists the area gets a dark edge of its own colour
        planlog.stage('fills', detail='borders')
        for op in ops:
            if op.kind == 'line' and op.vlayer == ink_v:
                bucket.stroke_wall(grid, walls, op.points, op.size)
        done = 0
        for i, fill in enumerate(fills):
            if not area_borders or total() > usable * BRUSH_BORDERS:
                break
            cells = cells_of(fill)
            if len(cells) < 4 or areas[i] < 40:
                continue
            loop = _resample(_smooth_path(_resample(_simplify(cells, 1.4), 2.5), 5), 2.0)
            made = border_strokes(loop, fill.color, 650 + i // 8)
            for op in made:
                bucket.stroke_wall(grid, walls, op.points, op.size)
            ops.extend(made)
            done += 1
        dabbed = done
        zone = np.zeros((grid.H, grid.W), bool)
        for m in masks:
            zone |= m
        return len(fills), zone | ink_zone

    # the skeleton of the colour areas first, then the bucket (or the brush)
    if brush:
        reached, shade_zone = brush_flow()
        planlog.note(f'brush colouring done, {dabbed} areas bordered')
    else:
        reached = outline_pass(0, usable * OUTLINE_LIMIT)
        pour_pass(usable * SHARES['flats'] - detail_reserve)
        planlog.note(f'{poured} pours, {dabbed} dabs, {skipped} areas skipped, '
                     f'{len(fills) - reached} areas without outline')
        shade_zone = None

    # --- 3. brush masses and shading (thick strokes, high pressure) ---------------------
    planlog.stage('shade')
    ops += brush_ops(masses, 'color', 'accent', usable * 0.88, 700,
                     roles['light'] if brush else None, shade_zone)
    ops += brush_ops(shades, 'shade', 'accent', usable * 0.92, 800,
                     roles['shade'] if brush else None, shade_zone)

    # --- 4. the finer line art last ------------------------------------------------------
    planlog.stage('fit', detail='details')
    ops += ink_ops(detail_src, 'detail', max(detail_reserve, usable * 0.96 - total()), ink_v)
    if not brush:
        if total() < usable * 0.90 and reached < len(fills):   # time left: outline and colour the rest
            reached = outline_pass(reached, usable * 0.90)
        pour_pass(usable * 0.96)

    ops = fit_time(ops, usable)
    planlog.note(f'{len(ops)} operations, {sum(1 for o in ops if o.kind == "bucket")} buckets')
    if debug:
        if ratios:
            print(f'[human] cell/polygon area {np.round(np.percentile(ratios, [0, 25, 50, 75, 100]), 2)}')
        print(f'[human] areas {len(fills)}: outlined {reached}, outline-only {sum(1 for r in pending if not r["done"])}, poured areas {sum(1 for r in pending if r["done"])}, open/leaky {skipped}')
        tot = sum(int(np.count_nonzero(cv2.fillPoly(np.zeros((grid.H, grid.W), np.uint8), [np.round(cells_of(f)).astype(np.int32).reshape(-1, 1, 2)], 1))) for f in fills) or 1
        bare = sum(int(np.count_nonzero(cv2.fillPoly(np.zeros((grid.H, grid.W), np.uint8), [np.round(cells_of(f)).astype(np.int32).reshape(-1, 1, 2)], 1))) for f in fills[reached:])
        print(f'[human] share of the colour areas with neither outline nor colour: {bare / tot:.2f}')
        print(f'[human] total {total():.0f}s of {usable:.0f}s; poured {poured} dabbed {dabbed} '
              f'skipped {skipped}')
    return ops


def paint_order(masks, areas, colours):
    """Order of the colour areas: the painter's order, except that areas of one colour go together.

    An area that overlaps an earlier one must stay after it (it is painted on top); everything
    else may move, so that the hand picks each colour once instead of over and over."""
    n = len(masks)
    boxes = []
    for m in masks:
        ys, xs = np.nonzero(m)
        boxes.append((xs.min(), ys.min(), xs.max(), ys.max()) if len(xs) else (0, 0, -1, -1))
    before = [set() for _ in range(n)]
    for j in range(n):
        for i in range(j):
            a, b = boxes[i], boxes[j]
            if a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]:
                continue
            if np.count_nonzero(masks[i] & masks[j]) > 0.03 * max(1, min(areas[i], areas[j])):
                before[j].add(i)
    done, order, colour = set(), [], None
    remaining = list(range(n))
    while remaining:
        ready = [i for i in remaining if before[i] <= done]
        same = [i for i in ready if colours[i] == colour]
        pick = (same or ready)[0]
        remaining.remove(pick)
        done.add(pick)
        order.append(pick)
        colour = colours[pick]
    return order


def fit_time(ops, usable):
    """Cut the tail of a plan that cannot finish; stretch the pauses of one that is early."""
    def total(items):
        return sum((o.pause or 0.0) + (o.dur or 0.0) + REQUEST_ALLOWANCE for o in items)

    while len(ops) > 1 and total(ops) > usable:
        ops = ops[:-1]
    t = total(ops)
    if t > 0 and t < 0.6 * usable:
        # a person with time to spare looks at the drawing longer; the pen does not slow down
        want = 0.88 * usable
        pauses = sum(o.pause or 0.0 for o in ops)
        if pauses > 0:
            factor = min(2.4, 1.0 + (want - t) / pauses)
            for o in ops:
                o.pause *= factor
    return ops


# ---------------------------------------------------------------------------
# offline rendering (preview, tests): the same model the planner used
# ---------------------------------------------------------------------------

def _stroke(canvas, pts, thickness, color):
    """One polyline with flat ends and round joins (what the game draws)."""
    half = max(thickness, 1.0) / 2.0
    for a, b in zip(pts[:-1], pts[1:]):
        d = b - a
        n = float(np.hypot(*d))
        if n < 1e-6:
            continue
        normal = np.array([-d[1], d[0]]) / n * half
        quad = np.array([a + normal, b + normal, b - normal, a - normal])
        cv2.fillConvexPoly(canvas, np.round(quad).astype(np.int32), color, cv2.LINE_AA)
    for p in pts[1:-1]:                                   # round joins
        cv2.circle(canvas, (int(round(p[0])), int(round(p[1]))), int(round(half)), color, -1, cv2.LINE_AA)


def _slot(op, bands):
    """(real layer, band) of an op: virtual layers map onto real ones, the rest keeps its layer."""
    if getattr(op, 'vlayer', None) is not None:
        return layer_map.resolve(op.vlayer, bands)
    return int(getattr(op, 'layer', 0) or 0), 0


def simulate(ops, canvas_aspect, world_width, width=1280, bands=layer_map.DEFAULT_BANDS):
    """Replay a plan: every real layer has its own bucket walls; layers stack 0, 1, 2 ...

    Within a real layer the fills are under the lines (the game's _FILL sorting layer) and
    the lines follow their band (virtual layer), then the order of drawing.
    """
    height = int(round(width / canvas_aspect))
    grid = bucket.Grid(world_width, world_width / canvas_aspect)
    layers = sorted({_slot(o, bands)[0] for o in ops} | {0})
    fills = {k: np.full((height, width, 3), 255, np.uint8) for k in layers}
    painted = {k: np.zeros((height, width), bool) for k in layers}
    lines = {k: [] for k in layers}
    report = []
    for index, op in enumerate(ops):
        layer, band = _slot(op, bands)
        if op.kind == 'line':
            lines[layer].append((band, index, op))
        elif op.kind == 'bucket':
            blocked = bucket.barrier(grid, [o for _, _, o in lines[layer]])
            cell = np.round(grid.to_cells(op.points)[0]).astype(int)
            region = bucket.region_at(blocked, cell)
            if region is None:
                report.append({'filled': False, 'reason': 'seed on a line'})
                continue
            fraction = float(np.count_nonzero(region)) / region.size
            leaked = bucket.touches_border(region)
            report.append({'filled': True, 'area': fraction, 'leaked': leaked,
                           'expect': getattr(op, 'expect', None)})
            mask = cv2.resize(region.astype(np.uint8), (width, height), interpolation=cv2.INTER_NEAREST)
            mask = cv2.dilate(mask, np.ones((3, 3), np.uint8)) > 0
            fills[layer][mask] = tuple(int(op.color[i:i + 2], 16) for i in (1, 3, 5))
            painted[layer] |= mask
    # Lines are drawn the way the game draws them: exact width (size / 100 world units),
    # flat ends, round joins, at 3x and scaled down so thin lines are anti-aliased.
    ss = 3
    canvas = np.full((height * ss, width * ss, 3), 255, np.uint8)
    for layer in layers:
        mask = cv2.resize(painted[layer].astype(np.uint8), (width * ss, height * ss),
                          interpolation=cv2.INTER_NEAREST) > 0
        fill_big = cv2.resize(fills[layer], (width * ss, height * ss), interpolation=cv2.INTER_NEAREST)
        canvas[mask] = fill_big[mask]
        for _, _, op in sorted(lines[layer], key=lambda item: item[:2]):
            pts = np.column_stack((np.asarray(op.points)[:, 0] * width * ss,
                                   (1 - np.asarray(op.points)[:, 1]) * height * ss))
            color = tuple(int(op.color[i:i + 2], 16) for i in (1, 3, 5))
            _stroke(canvas, pts, op.size / 100.0 / world_width * width * ss, color)
    canvas = cv2.resize(canvas, (width, height), interpolation=cv2.INTER_AREA)
    return canvas, report
