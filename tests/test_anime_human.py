"""Offline tests for the `anime_human` style: pen pressure, motor model, bucket model, plan."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'client'))
sys.path.insert(0, str(ROOT / 'tools'))
import bucket
import anime_human as human
import motor
from anime_study import execute_study, plan_seconds, stroke_delay
from dg_artist import (Budget, Op, build_ops, build_parser, load_plan, make_analysis,
                       make_transform)


def square_op(cx, cy, half, size=8, gap=None):
    """Closed (or, with `gap`, open: two strokes) square outline, normalised coordinates."""
    c = [[cx - half, cy - half], [cx + half, cy - half], [cx + half, cy + half],
         [cx - half, cy + half], [cx - half, cy - half]]
    pts = []
    for (x0, y0), (x1, y1) in zip(c[:-1], c[1:]):
        for t in np.linspace(0, 1, 40, endpoint=False):
            pts.append([x0 + (x1 - x0) * t, y0 + (y1 - y0) * t])
    pts.append(c[-1])
    if gap:
        start, end = gap
        return [Op('line', pts[:start], '#000000', size), Op('line', pts[end:], '#000000', size)]
    return [Op('line', pts, '#000000', size)]


class PressurePipeline(unittest.TestCase):
    def test_constant_pressure_gives_the_linear_brush_size(self):
        # LinePressure: size = 2 + 33 * pressure, truncated
        self.assertEqual(motor.game_brush_size([0.5] * 30), 18)
        self.assertEqual(motor.game_brush_size([1.0] * 30), 35)
        self.assertEqual(motor.game_brush_size([0.2] * 30), 8)

    def test_last_frames_before_the_lift_do_not_count(self):
        # SmoothValue(drop=5): the tail of the stroke (lifting the pen) is ignored
        press = [0.6] * 20 + [0.2, 0.15, 0.1, 0.08]
        self.assertEqual(motor.game_brush_size(press), motor.game_brush_size([0.6] * 20))

    def test_short_stroke_averages_everything_and_weak_pressure_is_ignored(self):
        self.assertEqual(motor.game_brush_size([0.5, 0.5, 0.5]), 18)
        self.assertIsNone(motor.game_brush_size([0.01, 0.04, 0.0]))
        self.assertEqual(motor.game_brush_size([0.0, 0.5, 0.5, 0.5]), 18)

    def test_brush_sizes_stay_in_range_and_differ_between_strokes(self):
        m = motor.Motor(seed=3)
        sizes = []
        for i in range(120):
            role = ('contour', 'inner', 'detail', 'accent', 'gap_close', 'shade')[i % 6]
            path = np.column_stack((np.linspace(0, 3, 30), np.sin(np.linspace(0, 3, 30))))
            t = m.timeline(path, m.stroke_duration(0.3, role))
            sizes.append(m.brush_size(t, role))
        self.assertTrue(all(motor.PEN_MIN <= s <= motor.PEN_MAX for s in sizes))
        self.assertGreater(len(set(sizes)), 7)
        accent = np.mean([s for i, s in enumerate(sizes) if i % 6 == 3])
        inner = np.mean([s for i, s in enumerate(sizes) if i % 6 == 1])
        self.assertGreater(float(accent), float(inner))

    def test_same_seed_same_hand(self):
        def run(seed):
            m = motor.Motor(seed=seed)
            path = np.column_stack((np.linspace(0, 2, 20), np.linspace(0, 1, 20)))
            t = m.timeline(path, 0.8)
            return m.brush_size(t, 'contour'), round(float(t[-1]), 6)
        self.assertEqual(run(5), run(5))


class PressureTargets(unittest.TestCase):
    def test_curve_hits_the_target_size_and_never_drops_under_the_game_cutoff(self):
        m = motor.Motor(seed=4, fps=240)
        path = np.column_stack((np.linspace(0, 2, 40), np.sin(np.linspace(0, 2, 40))))
        for target in (3, 4, 6, 10, 18, 26):
            t = m.timeline(path, 0.7)
            curve, size = m.pressure_for_size(t, 'contour', None, target, spread=0.0)
            self.assertGreater(float(curve.min()), motor.CUTOFF)          # the game would split the stroke
            self.assertEqual(size, motor.game_brush_size(list(curve)))
            self.assertLessEqual(abs(size - target), 1)

    def test_thinnest_line_is_three(self):
        m = motor.Motor(seed=4, fps=240)
        t = m.timeline(np.column_stack((np.linspace(0, 1, 20), np.zeros(20))), 0.4)
        _, size = m.pressure_for_size(t, 'inner', None, 2, spread=0.0)
        self.assertEqual(size, 3)


class MotorModel(unittest.TestCase):
    def test_timeline_is_monotonic_bell_shaped_and_exact(self):
        m = motor.Motor(seed=1)
        path = np.column_stack((np.linspace(0, 4, 60), np.zeros(60)))
        t = m.timeline(path, 1.2)
        self.assertTrue(np.all(np.diff(t) >= 0))
        self.assertAlmostEqual(float(t[-1]), 1.2, places=6)
        speed = np.diff(path[:, 0]) / np.maximum(np.diff(t), 1e-9)
        mid = speed[len(speed) // 3: 2 * len(speed) // 3].mean()
        self.assertGreater(mid, speed[:4].mean())
        self.assertGreater(mid, speed[-4:].mean())

    def test_tight_bends_are_slower(self):
        m = motor.Motor(seed=1)
        straight = np.column_stack((np.linspace(0, 2, 40), np.zeros(40)))
        a = np.linspace(0, np.pi, 40)
        bend = np.column_stack((2 + np.sin(a) * 0.15, 0.15 - np.cos(a) * 0.15))
        path = np.vstack((straight, bend[1:]))
        t = m.timeline(path, 2.0)
        speed = np.linalg.norm(np.diff(path, axis=0), axis=1) / np.maximum(np.diff(t), 1e-9)
        self.assertLess(speed[45:70].mean(), speed[10:35].mean())

    def test_recorded_points_are_spaced_like_the_game(self):
        path = np.column_stack((np.linspace(0, 5, 300), np.zeros(300)))
        t = np.linspace(0, 1.0, 300)
        pts = motor.sample_stroke(path, t)
        gaps = np.linalg.norm(np.diff(pts, axis=0), axis=1)
        self.assertTrue(np.all(gaps[:-1] >= motor.MIN_STEP_WORLD - 1e-9))
        self.assertAlmostEqual(float(pts[-1][0]), 5.0, places=6)

    def test_durations_follow_the_measured_law(self):
        m = motor.Motor(seed=2)
        short = np.median([m.stroke_duration(0.05) for _ in range(300)])
        long = np.median([m.stroke_duration(0.8) for _ in range(300)])
        self.assertLess(short, long)
        self.assertTrue(0.16 <= short and long <= 3.6)

    def test_calibration_file_is_present_and_sane(self):
        data = json.loads(motor.CALIBRATION.read_text())
        self.assertGreater(data['samples']['strokes'], 10000)
        self.assertTrue(0.3 < data['stroke_duration']['b'] < 0.9)
        self.assertEqual(len(data['speed_profile']['mean']), 20)


class BucketModel(unittest.TestCase):
    def setUp(self):
        self.grid = bucket.Grid(17.7778, 10.0)

    def test_grid_round_trip(self):
        pts = np.array([[0.1, 0.2], [0.9, 0.7]])
        back = self.grid.to_normalised(self.grid.to_cells(pts))
        np.testing.assert_allclose(back, pts, atol=1e-9)

    def test_closed_outline_holds_the_fill_and_open_one_leaks(self):
        closed = bucket.barrier(self.grid, square_op(0.5, 0.5, 0.2))
        seed = tuple(np.round(self.grid.to_cells([[0.5, 0.5]])[0]).astype(int))
        region = bucket.region_at(closed, seed)
        self.assertFalse(bucket.touches_border(region))
        expected = (0.4 * self.grid.world_w / self.grid.density) * (0.4 * self.grid.world_h / self.grid.density)
        self.assertAlmostEqual(np.count_nonzero(region), expected, delta=expected * 0.1)
        opened = bucket.barrier(self.grid, square_op(0.5, 0.5, 0.2, gap=(30, 50)))
        self.assertTrue(bucket.touches_border(bucket.region_at(opened, seed)))

    def test_discs_do_not_block_and_a_seed_on_a_wall_fills_nothing(self):
        disc = Op('disc', [[0.5, 0.5]], '#000', 30)
        self.assertEqual(int(bucket.barrier(self.grid, [disc]).sum()), 0)
        wall = bucket.barrier(self.grid, square_op(0.5, 0.5, 0.2))
        on_wall = tuple(np.round(self.grid.to_cells([[0.3, 0.5]])[0]).astype(int))
        self.assertIsNone(bucket.region_at(wall, on_wall) if wall[on_wall[1], on_wall[0]] else None)

    def test_inscribed_seed_is_inside(self):
        region = np.zeros((100, 100), bool)
        region[20:80, 30:70] = True
        (x, y), depth = bucket.seed_point(region)
        self.assertTrue(region[y, x])
        self.assertGreater(depth, 15)

    def test_plug_closes_a_leak(self):
        ops = square_op(0.5, 0.5, 0.2, gap=(18, 22))
        blocked = bucket.barrier(self.grid, ops)
        seed = tuple(np.round(self.grid.to_cells([[0.5, 0.5]])[0]).astype(int))
        cells = self.grid.to_cells(np.array([[0.3, 0.3], [0.7, 0.7]]))
        box = (int(cells[:, 0].min()), int(cells[:, 1].min()), int(cells[:, 0].max()), int(cells[:, 1].max()))
        plugged = None
        for _ in range(3):
            plug = bucket.plug_leak(blocked, seed, box)
            if plug is None:
                break
            a, b = plug
            cv2.line(blocked, tuple(int(v) for v in np.round(a)), tuple(int(v) for v in np.round(b)), 255, 2)
            plugged = bucket.region_at(blocked, seed)
            if not bucket.touches_border(plugged):
                break
        self.assertIsNotNone(plugged)
        self.assertFalse(bucket.touches_border(plugged))
        self.assertIsNone(bucket.plug_leak(blocked, seed, box))       # closed: nothing to plug


class Simulation(unittest.TestCase):
    def test_lines_have_the_games_width_and_flat_ends(self):
        op = Op('line', [[0.3, 0.5], [0.7, 0.5]], '#000000', 10)
        canvas, _ = human.simulate([op], 16 / 9, 17.7778, width=1280)
        dark = canvas.max(axis=2) < 128
        ys, xs = np.nonzero(dark)
        width_px = 10 / 100 / 17.7778 * 1280                       # 0.72 px per size unit
        self.assertAlmostEqual(float(dark[:, 640].sum()), width_px, delta=1.5)
        self.assertAlmostEqual(float(xs.max() - xs.min() + 1), 0.4 * 1280, delta=2)   # no round caps

    def test_seed_candidates_are_deep_and_apart(self):
        region = np.zeros((120, 200), bool)
        region[20:100, 20:180] = True
        found = bucket.seed_candidates(region, 3)
        self.assertGreaterEqual(len(found), 2)
        self.assertGreater(found[0][1], 30)
        a, b = np.array(found[0][0]), np.array(found[1][0])
        self.assertGreater(float(np.hypot(*(a - b))), 20)


class HandHelpers(unittest.TestCase):
    def test_chain_prefers_left_to_right_and_top_to_bottom(self):
        horizontal = np.array([[100.0, 10.0], [10.0, 10.0]])          # drawn right to left
        vertical = np.array([[10.0, 90.0], [10.0, 20.0]])             # drawn bottom to top
        out = human.chain([(horizontal, 1), (vertical, 1)], start=(55.0, 15.0))
        for path, _ in out:
            d = path[-1] - path[0]
            self.assertTrue(d[0] > 0 if abs(d[0]) > abs(d[1]) else d[1] > 0)

    def test_chain_takes_the_closer_end_when_the_habit_is_far_worse(self):
        path = np.array([[500.0, 10.0], [10.0, 10.0]])
        out = human.chain([(path, 1)], start=(505.0, 10.0))
        self.assertLess(out[0][0][0][0], 600)                         # keeps one stroke either way
        self.assertEqual(len(out), 1)

    def test_fit_time_cuts_the_tail_and_stretches_pauses_not_strokes(self):
        def mk(n, dur, pause):
            ops = []
            for _ in range(n):
                o = Op('line', [[0, 0], [0.1, 0.1]], '#000', 8)
                o.dur, o.pause = dur, pause
                ops.append(o)
            return ops
        cut = human.fit_time(mk(100, 0.5, 0.5), 30)
        self.assertLess(len(cut), 100)
        self.assertLessEqual(sum(o.dur + o.pause + human.REQUEST_ALLOWANCE for o in cut), 30)
        slow = human.fit_time(mk(20, 0.5, 0.5), 60)
        self.assertEqual(len(slow), 20)
        self.assertGreater(slow[0].pause, 0.5)
        self.assertLessEqual(slow[0].dur, 0.5 * 1.25 + 1e-9)


class HandPlan(unittest.TestCase):
    def setUp(self):
        img = np.full((300, 220, 3), 246, np.uint8)
        cv2.ellipse(img, (110, 190), (80, 100), 0, 0, 360, (190, 60, 70), -1)          # body
        cv2.circle(img, (110, 90), 55, (238, 205, 180), -1)                              # face
        cv2.ellipse(img, (110, 55), (58, 36), 0, 180, 360, (50, 40, 110), -1)            # hair
        for x in (92, 128):
            cv2.circle(img, (x, 95), 7, (30, 30, 35), -1)
        self.image = img
        self.args = build_parser().parse_args(['unused', '--style', 'anime_human', '--dry-run'])
        self.transform, _, _ = make_transform(220, 300, 16 / 9)
        self.args.canvas_aspect = 16 / 9
        self.args.canvas_world_width = 17.7778

    def plan(self, seconds=90):
        analysis = make_analysis(self.image, 'anime_human', self.args)
        budget = Budget(seconds, seconds * 0.92, 900, False, 'test', 3)
        return build_ops('anime_human', analysis, budget, self.args, self.transform), budget

    def test_plan_is_lines_and_buckets_with_pressure_widths(self):
        ops, _ = self.plan()
        self.assertTrue(ops)
        self.assertEqual({o.kind for o in ops}, {'line', 'bucket'})
        self.assertGreaterEqual(sum(o.kind == 'bucket' for o in ops), 2)
        sizes = [o.size for o in ops if o.kind == 'line']
        self.assertTrue(all(motor.PEN_MIN <= s <= motor.PEN_MAX for s in sizes))
        self.assertGreaterEqual(len(set(sizes)), 2)
        self.assertTrue(all(o.dur is not None and o.pause is not None for o in ops))
        self.assertTrue(all(0.0 <= p[0] <= 1.0 and 0.0 <= p[1] <= 1.0
                            for o in ops for p in o.points))

    def test_no_fill_polygons_and_every_pour_is_closed(self):
        ops, _ = self.plan()
        self.assertFalse(any(o.kind in ('fill', 'realfill') for o in ops))
        _, report = human.simulate(ops, 16 / 9, 17.7778, width=640)
        self.assertTrue(report)
        for item in report:
            self.assertTrue(item['filled'])
            self.assertFalse(item['leaked'])
            self.assertAlmostEqual(item['area'] / item['expect'], 1.0, delta=0.05)

    def test_lines_are_drawn_before_the_pour_that_needs_them(self):
        ops, _ = self.plan()
        first_pour = next(i for i, o in enumerate(ops) if o.kind == 'bucket')
        self.assertGreater(first_pour, 0)
        self.assertEqual(ops[0].kind, 'line')

    def test_skeleton_first_then_colour(self):
        ops, _ = self.plan(120)
        first_pour = next(i for i, o in enumerate(ops) if o.kind == 'bucket')
        lines_before = sum(1 for o in ops[:first_pour] if o.kind == 'line')
        lines_total = sum(1 for o in ops if o.kind == 'line')
        # the line work (outline, structure, the outlines of the colour areas) comes first
        self.assertGreaterEqual(lines_before, 0.45 * lines_total)
        # and no pour happens while a colour area still has no line round it at all:
        # the border strokes of the areas that get poured precede their pour
        borders = [i for i, o in enumerate(ops) if o.note == 'human-border']
        if borders:
            self.assertLess(min(borders), first_pour)

    def test_plan_fits_the_time_and_is_deterministic(self):
        for seconds in (30, 90, 180):
            ops, budget = self.plan(seconds)
            self.assertLessEqual(plan_seconds(ops, self.args), budget.usable_seconds + 1e-6)
        a, _ = self.plan(90)
        b, _ = self.plan(90)
        self.assertEqual([o.to_json() for o in a], [o.to_json() for o in b])

    def test_plan_round_trips_through_json(self):
        ops, _ = self.plan(60)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'plan.json'
            path.write_text(json.dumps([o.to_json() for o in ops]))
            again = load_plan(path)
        self.assertEqual(len(again), len(ops))
        self.assertEqual([o.kind for o in again], [o.kind for o in ops])
        self.assertAlmostEqual(again[0].dur, ops[0].dur, places=3)
        self.assertEqual(again[-1].expect is None, ops[-1].expect is None)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Bridge:
    def __init__(self, clock, area_factor=1.0):
        self.clock, self.area_factor = clock, area_factor
        self.strokes, self.fills, self.undone, self.colors, self.brushes = [], [], 0, [], []
        self.expect = []

    def state(self):
        return {'available': True, 'canDraw': True, 'layers': {'total': 3}}

    def round(self):
        return {}

    def clear(self, **kwargs):
        pass

    def layer(self, value):
        pass

    def color(self, value):
        self.colors.append(value)

    def brush(self, value):
        self.brushes.append(value)

    def draw_strokes(self, strokes, **_):
        self.strokes.extend(strokes)
        self.clock.now += 0.02
        return {'applied': len(strokes), 'failed': 0}

    def fill(self, points, color=None, force=False, space='normalized'):
        self.fills.append((points, color))
        self.clock.now += 0.02
        factor = self.area_factor if len(self.fills) == 1 else getattr(self, 'second_factor', self.area_factor)
        return {'filled': 1, 'failed': 0, 'regions': [{'area': self.expect[0] * factor}]}

    lines_result = {'lines': []}

    def lines(self, limit=0):
        return self.lines_result

    def undo(self):
        self.undone += 1

    def tool(self, name):
        self.tools = getattr(self, 'tools', []) + [name]

    pen_result = {'created': 1, 'brushSize': 9}

    def pen_stroke(self, samples, force=False, **_):
        self.pen_calls = getattr(self, 'pen_calls', 0) + 1
        self.clock.now += samples[-1][0] / 1000.0
        return dict(self.pen_result)


class HandExecution(unittest.TestCase):
    def setUp(self):
        self.args = build_parser().parse_args(['unused', '--style', 'anime_human', '--dry-run'])
        self.args.study_deadline = 1e9
        self.args.pace = 'slow'
        self.args.pen = 'direct'

    def run_plan(self, area_factor):
        ops = []
        for i in range(3):
            o = Op('line', [[0.1 * i, 0.2], [0.1 * i + 0.1, 0.3], [0.1 * i + 0.2, 0.2]], '#34333D', 7 + i)
            o.dur, o.pause = 0.4, 0.5
            ops.append(o)
        b = Op('bucket', [[0.2, 0.25]], '#ff0000', 0, 0, 'color')
        b.dur, b.pause, b.expect = 0.18, 0.7, 0.02
        ops.append(b)
        clock = Clock()
        bridge = Bridge(clock, area_factor)
        bridge.expect = [0.02]
        budget = Budget(100, 90, 900, False, 'test', 3)
        with patch('anime_study.time.monotonic', clock.monotonic), \
                patch('anime_study.time.sleep', clock.sleep):
            result = execute_study(bridge, ops, budget, self.args)
        return bridge, result, clock

    def test_each_stroke_keeps_its_own_pressure_size_without_brush_requests(self):
        bridge, result, clock = self.run_plan(1.0)
        self.assertEqual([s['size'] for s in bridge.strokes], [7, 8, 9])
        self.assertEqual(bridge.brushes, [])
        self.assertEqual(bridge.colors, [])
        self.assertEqual(result['real_fills'], 1)
        self.assertEqual(bridge.undone, 0)
        self.assertEqual(bridge.tools, ['Brush'])          # back to the brush after the pour
        # waited for the pen-down time before each stroke reached the game
        self.assertGreater(clock.now, 3 * 0.4)

    def test_a_pour_that_floods_far_more_than_planned_is_undone(self):
        bridge, result, _ = self.run_plan(6.0)
        self.assertEqual(bridge.undone, 1)
        self.assertEqual(result['leaked_fills'], 1)
        self.assertEqual(result['real_fills'], 0)

    def native_plan(self):
        ops = []
        for i in range(3):
            o = Op('line', [[0.1 * i, 0.2], [0.1 * i + 0.1, 0.3], [0.1 * i + 0.2, 0.2]], '#34333D', 9)
            o.dur, o.pause = 0.4, 0.5
            o.pen = [[0, 0.1 * i, 0.2, 0.3], [400, 0.1 * i + 0.2, 0.2, 0.4]]
            ops.append(o)
        return ops

    def run_native(self, pen_result):
        self.args.pen = 'native'
        clock = Clock()
        bridge = Bridge(clock)
        bridge.pen_result = pen_result
        budget = Budget(100, 90, 900, False, 'test', 3)
        with patch('anime_study.time.monotonic', clock.monotonic), \
                patch('anime_study.time.sleep', clock.sleep):
            result = execute_study(bridge, self.native_plan(), budget, self.args)
        return bridge, result

    def test_a_pour_that_comes_out_far_too_small_is_retried_somewhere_else(self):
        clock = Clock()
        bridge = Bridge(clock)
        sizes = [0.0001, 0.0094]                        # the first click lands in a pocket
        def fill(points, color=None, force=False, space='normalized'):
            bridge.fills.append((points, color))
            return {'filled': 1, 'failed': 0, 'regions': [{'area': sizes[len(bridge.fills) - 1]}]}
        bridge.fill = fill
        b = Op('bucket', [[0.5, 0.5]], '#e9e8e5', 0, 0, 'color')
        b.dur, b.pause, b.expect, b.alt = 0.18, 0.7, 0.0095, [[0.55, 0.52]]
        budget = Budget(100, 90, 900, False, 'test', 3)
        with patch('anime_study.time.monotonic', clock.monotonic), \
                patch('anime_study.time.sleep', clock.sleep):
            result = execute_study(bridge, [b], budget, self.args)
        self.assertEqual(result['retries'], 1)
        self.assertEqual(result['real_fills'], 1)
        self.assertEqual(bridge.undone, 1)                         # the speck was taken back
        self.assertEqual(bridge.fills[1][0], [[0.55, 0.52]])

    def test_a_pour_that_stays_too_small_everywhere_leaves_no_speck(self):
        clock = Clock()
        bridge = Bridge(clock)
        bridge.fill = lambda points, color=None, force=False, space='normalized': \
            {'filled': 1, 'failed': 0, 'regions': [{'area': 0.0001}]}
        b = Op('bucket', [[0.5, 0.5]], '#e9e8e5', 0, 0, 'color')
        b.dur, b.pause, b.expect, b.alt = 0.18, 0.7, 0.0095, [[0.55, 0.52]]
        budget = Budget(100, 90, 900, False, 'test', 3)
        with patch('anime_study.time.monotonic', clock.monotonic), \
                patch('anime_study.time.sleep', clock.sleep):
            result = execute_study(bridge, [b], budget, self.args)
        self.assertEqual(result['real_fills'], 0)
        self.assertEqual(bridge.undone, 2)                         # first speck + the retry's

    def test_a_leak_is_repaired_from_the_lines_the_game_really_has(self):
        gap = square_op(0.5, 0.5, 0.2, gap=(18, 22))
        clock = Clock()
        bridge = Bridge(clock, area_factor=40.0)
        bridge.second_factor = 1.0
        bridge.expect = [0.02]
        bridge.lines_result = {'lines': [{'type': 0, 'size': o.size, 'points': o.points} for o in gap]}
        b = Op('bucket', [[0.5, 0.5]], '#ff0000', 0, 0, 'color')
        b.dur, b.pause, b.expect = 0.18, 0.7, 0.02
        budget = Budget(100, 90, 900, False, 'test', 3)
        self.args.canvas_world_width, self.args.canvas_aspect = 17.7778, 16 / 9
        with patch('anime_study.time.monotonic', clock.monotonic), \
                patch('anime_study.time.sleep', clock.sleep):
            result = execute_study(bridge, [b], budget, self.args)
        self.assertEqual(result['repairs'], 1)
        self.assertEqual(result['real_fills'], 1)
        self.assertEqual(result['leaked_fills'], 0)
        self.assertEqual(len(bridge.fills), 2)
        self.assertEqual(len(bridge.strokes), 1)                       # the closing stroke

    def test_native_pen_draws_through_the_game_and_skips_direct_strokes(self):
        bridge, result = self.run_native({'created': 1, 'brushSize': 9})
        self.assertEqual(bridge.pen_calls, 3)
        self.assertEqual(bridge.strokes, [])
        self.assertEqual(result['pen_strokes'], 3)
        self.assertEqual(result['pen_size_error'], 0)
        self.assertEqual(result['applied'], 3)

    def test_native_pen_falls_back_to_direct_when_the_game_ignores_it(self):
        bridge, result = self.run_native({'created': 0})
        self.assertEqual(bridge.pen_calls, 3)          # three misses in a row, then gave up
        self.assertEqual(len(bridge.strokes), 3)
        self.assertEqual(result['pen_strokes'], 0)
        self.assertEqual(result['applied'], 3)

    def test_without_pen_pressure_the_sizes_are_slider_steps(self):
        from anime_human import SIZE_STEPS
        self.args.pen = 'auto'                      # the mock bridge has no pen endpoint: no pressure
        bridge, result, _ = self.run_plan(1.0)
        self.assertTrue(all(s['size'] in SIZE_STEPS for s in bridge.strokes))
        self.assertEqual(bridge.brushes, [])

    def squeeze_plan(self, native):
        ops = []
        for i in range(20):
            o = Op('line', [[0.02 * i, 0.2], [0.02 * i + 0.05, 0.3]], '#34333D', 4)
            o.dur, o.pause = 1.0, 1.0
            o.pen = [[0, 0.1, 0.2, 0.3], [70, 0.1, 0.2, 0.3], [1070, 0.2, 0.3, 0.3]]
            ops.append(o)
        self.args.pen = 'native' if native else 'direct'
        clock = Clock()
        bridge = Bridge(clock)
        self.args.study_deadline = 24.0                 # the plan wants 40 s, only 24 are left
        budget = Budget(100, 90, 900, False, 'test', 3)
        with patch('anime_study.time.monotonic', clock.monotonic), \
                patch('anime_study.time.sleep', clock.sleep):
            result = execute_study(bridge, ops, budget, self.args)
        return bridge, result, clock

    def test_less_time_than_planned_means_the_same_drawing_drawn_faster(self):
        for native in (False, True):
            bridge, result, clock = self.squeeze_plan(native)
            self.assertEqual(result['sent'], 20)                       # nothing dropped
            self.assertLessEqual(clock.now, 24.0 + 0.5)                # and done in the time left
            self.assertGreater(clock.now, 12.0)                        # not needlessly hurried
            if native:
                self.assertEqual(bridge.pen_calls, 20)

    def test_scaled_pen_compresses_the_stroke_but_keeps_the_touch_down_rest(self):
        from anime_study import scaled_pen
        samples = [[0, .1, .2, .3], [70, .1, .2, .3], [570, .2, .3, .3], [1070, .3, .3, .3]]
        out = scaled_pen(samples, 0.5)
        self.assertEqual([s[0] for s in out], [0, 70, 320, 570])
        self.assertEqual(scaled_pen(samples, 1.0), samples)
        self.assertEqual([s[1:] for s in out], [s[1:] for s in samples])
        very_short = scaled_pen(samples, 0.01)
        self.assertTrue(all(b[0] - a[0] >= 4 for a, b in zip(very_short, very_short[1:])))

    def test_delay_uses_the_motor_timing(self):
        o = Op('line', [[0, 0], [1, 1]], '#000', 8)
        o.dur, o.pause = 0.3, 0.7
        self.assertAlmostEqual(stroke_delay(o, self.args), 1.0)


if __name__ == '__main__':
    unittest.main()
