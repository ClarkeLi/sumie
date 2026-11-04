"""Offline study planner / bridge pacing regressions. No game or model required."""
import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client'))
from dg_artist import (Budget, Op, GRID_STYLES, build_parser, build_ops,
                       make_analysis, make_transform, execute_plan, resolve_budget, main)
from anime_study import (MAX_STROKES, bowed_path, complexity, detail_level,
                         humanize, subject_mask, stroke_seconds,
                         stroke_delay, fit_plan, plan_seconds)


def arc_ratio(op):
    points = np.asarray(op.points, dtype=float)
    if len(points) < 2:
        return 1.0
    length = float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())
    chord = float(np.linalg.norm(points[-1] - points[0]))
    return length / max(chord, 1e-9)



def busy_scene(w=640, h=400):
    """Deterministic, detail-rich picture (no image file needed)."""
    rng = np.random.default_rng(5)
    img = np.full((h, w, 3), (236, 232, 226), np.uint8)
    for _ in range(42):
        color = tuple(int(v) for v in rng.integers(20, 235, 3))
        x, y = int(rng.integers(40, w - 40)), int(rng.integers(30, h - 30))
        r = int(rng.integers(14, 48))
        if rng.random() < .5:
            cv2.circle(img, (x, y), r, color, -1)
        else:
            cv2.rectangle(img, (x - r, y - r // 2), (x + r, y + r // 2), color, -1)
        cv2.circle(img, (x, y), max(3, r // 4), (25, 25, 30), -1)
    for _ in range(26):
        points = rng.integers(20, [w - 20, h - 20], (4, 2))
        cv2.polylines(img, [points.astype(np.int32)], False, (30, 30, 40), 2)
    return img

class Clock:
    def __init__(self):
        self.now = 0.0
        self.pauses = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


class Bridge:
    def __init__(self, clock=None, latency=0.02):
        self.clock, self.latency = clock, latency
        self.colors, self.brushes, self.events = [], [], []
        self.batches, self.clears, self.layers, self.timestamps = [], 0, [], []

    def state(self):
        return {'available': True, 'canDraw': True, 'layers': {'total': 3}}

    def round(self):
        return {}

    def clear(self, **kwargs):
        self.clears += 1

    def layer(self, value):
        self.layers.append(value)

    def color(self, value):
        self.colors.append(value)
        self.events.append(('color', value))
        if self.clock:
            self.clock.now += self.latency

    def brush(self, value):
        self.brushes.append(value)
        self.events.append(('brush', value))
        if self.clock:
            self.clock.now += self.latency

    def draw_strokes(self, strokes, **kwargs):
        self.events.append(('draw', strokes[0]['color']))
        self.batches.append(strokes)
        if self.clock:
            self.timestamps.append(self.clock.now)
            self.clock.now += self.latency
        return {'applied': len(strokes), 'failed': 0}


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.image = np.full((160, 120, 3), 245, np.uint8)
        cv2.ellipse(self.image, (60, 78), (37, 67), 0, 0, 360, (105, 160, 85), -1)
        cv2.circle(self.image, (60, 45), 20, (245, 207, 174), -1)
        for x in (52, 68):
            cv2.ellipse(self.image, (x, 42), (5, 4), 0, 0, 360, (20, 30, 35), -1)
        self.args = build_parser().parse_args(['unused', '--style', 'anime_study', '--dry-run'])
        self.transform, _, _ = make_transform(120, 160, 1)

    def plan_layers(self, layers):
        self.args.colors = 9
        self.args.max_strokes = None
        analysis = make_analysis(self.image, 'anime_study', self.args)
        budget = Budget(60, 55, 280, False, 'test', layers)
        return build_ops('anime_study', analysis, budget, self.args, self.transform), budget

    def plan(self, colors=9, seconds=60, units=280, cap=None, image=None):
        self.args.colors = colors
        self.args.max_strokes = cap
        analysis = make_analysis(self.image if image is None else image,
                                 'anime_study', self.args)
        budget = Budget(seconds, seconds * 0.92, units, False, 'test', 3)
        return build_ops('anime_study', analysis, budget, self.args, self.transform), budget

    # -- plan shape ---------------------------------------------------------

    def test_only_brush_fill_and_disc_without_fences(self):
        for seconds in (20, 60, 180, 400):
            ops, _ = self.plan(seconds=seconds)
            self.assertTrue(ops)
            self.assertFalse(any(op.kind == 'realfill' for op in ops))
            self.assertFalse(any('fence' in op.note for op in ops))
            self.assertTrue(all(op.kind in ('line', 'disc', 'fill') for op in ops))
            for op in ops:
                if op.kind == 'fill':
                    from dg_artist import is_simple_polygon
                    self.assertTrue(is_simple_polygon(op.points), 'self-crossing fill')

    def test_colour_is_flat_fills_and_line_drawing_is_long_strokes(self):
        ops, _ = self.plan(seconds=180)
        fills = [op for op in ops if op.kind == 'fill']
        lines = [op for op in ops if op.kind == 'line' and op.layer == 1]
        self.assertGreaterEqual(len(fills), 2)
        self.assertGreaterEqual(len(lines), 6)
        self.assertTrue(all(2 <= op.size <= 35 for op in ops if op.kind == 'line'))
        # fills are lassoed with a handful of corners, not traced point by point
        self.assertLess(np.mean([len(op.points) for op in fills]), 40)
        # the line drawing has curves, not two-point ruler lines
        self.assertTrue(all(len(op.points) >= 3 for op in lines))

    def test_strokes_are_curved_not_straight(self):
        ops, _ = self.plan(seconds=180)
        colour = [op for op in ops if op.note in ('study-color', 'study-shade', 'study-touch')]
        self.assertGreater(np.mean([arc_ratio(op) for op in colour]), 1.05)
        for op in ops:
            if op.kind != 'line':
                continue
            points = np.asarray(op.points, dtype=float)
            length = float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())
            # a long stroke made of two endpoints is the generated line to avoid
            if length > 0.02:
                self.assertGreaterEqual(len(points), 4, op.note)
            segments = np.linalg.norm(np.diff(points, axis=0), axis=1)
            self.assertLessEqual(float(segments.max()), 0.02, op.note)

    def test_bowed_path_has_visible_curvature_and_wobble(self):
        rng = __import__('random').Random(3)
        path = bowed_path((10.0, 10.0), 0.2, 40.0, 28.0, rng)
        length = float(np.linalg.norm(np.diff(path, axis=0), axis=1).sum())
        chord = float(np.linalg.norm(path[-1] - path[0]))
        self.assertGreater(length / chord, 1.08)
        traced = humanize(np.array([[0, 0], [0, 40], [40, 40]], float),
                          rng, amplitude=1.5)
        self.assertGreater(len(traced), 20)

    def test_colours_include_ink_and_respect_cap(self):
        for colors in (2, 3, 9, 12):
            ops, _ = self.plan(colors=colors)
            self.assertTrue(ops)
            base = {op.color for op in ops if op.note == 'study-color'}
            self.assertLessEqual(len(base), min(12, colors))
            self.assertLessEqual(len({op.color for op in ops}), 12)
            self.assertIn('#34333D', {op.color for op in ops if op.note.startswith('study-ink') or op.note in ('study-facial', 'study-structure', 'study-centerline')})

    def test_budget_fits_and_uses_the_available_time(self):
        for seconds in (30, 60, 180, 300):
            ops, budget = self.plan(seconds=seconds, units=2000)
            self.assertLessEqual(len(ops), MAX_STROKES)
            estimated = plan_seconds(ops, self.args)
            self.assertLessEqual(estimated, budget.usable_seconds + 1e-6)
        example = busy_scene()
        for seconds in (60, 180):
            ops, budget = self.plan(seconds=seconds, units=2000, image=example)
            estimated = plan_seconds(ops, self.args)
            self.assertLessEqual(estimated, budget.usable_seconds + 1e-6)
            # a detailed picture has enough content to fill most of the round
            self.assertGreater(estimated, budget.usable_seconds * 0.55,
                               f'{seconds}s plan leaves too much time unused')

    def test_stroke_cap_is_respected(self):
        for cap in (0, 3, 50):
            ops, _ = self.plan(seconds=60, units=800, cap=cap)
            self.assertLessEqual(len(ops), cap)

    def test_complexity_grows_with_remaining_time(self):
        self.assertLess(complexity(20, self.args), complexity(200, self.args))
        self.assertLessEqual(complexity(100000, self.args), 8)
        self.assertLessEqual(detail_level(30), detail_level(300))
        short, _ = self.plan(seconds=30, units=2000)
        long, _ = self.plan(seconds=300, units=2000)
        self.assertLess(len(short), len(long))

    def test_order_and_layer_contract(self):
        ops, _ = self.plan()
        phases = ['sketch', 'color', 'shade', 'detail']
        ranks = [phases.index(op.phase) for op in ops]
        self.assertEqual(ranks, sorted(ranks))
        # ink on the upper layer, flat colour below it
        self.assertTrue(all(op.layer == 1 for op in ops if op.phase == 'sketch'))
        self.assertTrue(all(op.layer == 0 for op in ops if op.kind == 'fill'))
        self.assertFalse(any(op.phase == 'hatch' for op in ops))
        # a single-layer canvas draws the ink last so the colour cannot bury it
        one, _ = self.plan_layers(1)
        self.assertTrue(all(op.layer == 0 for op in one))
        order = [op.kind for op in one]
        self.assertLess(order.index('fill'), len(order) - 1 - order[::-1].index('line'))

    def test_uncertain_mask_never_falls_back_to_full_frame(self):
        analysis = make_analysis(np.full_like(self.image, 150), 'anime_study', self.args)
        with patch('dg_artist.grabcut_subject', return_value=np.full((160, 120), 255, np.uint8)):
            self.assertEqual(np.count_nonzero(subject_mask(analysis)), 0)

    def test_no_eye_cells_in_the_plan(self):
        # an arc pair with a solid dot reads as a cell, not an eye
        for seconds in (30, 180):
            ops, _ = self.plan(seconds=seconds, units=2000)
            self.assertFalse(any(op.note == 'study-eye' or op.group == 900000 for op in ops))

    def test_flat_background_mask_keeps_dark_figure_that_touches_the_frame(self):
        from anime_study import flat_background_mask
        image = np.full((200, 160, 3), 255, np.uint8)
        cv2.rectangle(image, (30, 20), (120, 90), (235, 190, 190), -1)
        # navy clothing running off the bottom edge: GrabCut tends to drop it
        cv2.rectangle(image, (10, 100), (150, 199), (30, 22, 92), -1)
        mask = flat_background_mask(image)
        self.assertIsNotNone(mask)
        self.assertTrue(mask[150, 80] > 0)
        self.assertTrue(mask[50, 70] > 0)
        self.assertEqual(int(mask[5, 5]), 0)

    def test_flat_background_mask_declines_a_busy_border(self):
        from anime_study import flat_background_mask
        rng = np.random.default_rng(3)
        image = rng.integers(0, 255, (120, 100, 3), dtype=np.uint8)
        self.assertIsNone(flat_background_mask(image))

    def test_adaptive_hue_families_keep_pink_hair_and_purple_clothes(self):
        image = np.full((160, 120, 3), 250, np.uint8)
        cv2.ellipse(image, (60, 47), (30, 35), 0, 0, 360, (235, 85, 190), -1)
        cv2.rectangle(image, (34, 75), (86, 142), (105, 55, 175), -1)
        self.args.colors = None
        analysis = make_analysis(image, 'anime_study', self.args)
        ops = build_ops('anime_study', analysis, Budget(180, 165, 280, False, 'test', 3),
                        self.args, self.transform)
        rgb = np.asarray([[tuple(int(o.color[i:i+2], 16) for i in (1, 3, 5))
                           for o in ops if o.kind == 'fill']], np.uint8)
        self.assertTrue(rgb.size)
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[0]
        self.assertTrue(any(145 <= h <= 179 and s > 70 for h, s, v in hsv), hsv)
        self.assertTrue(any(120 <= h < 145 and s > 70 for h, s, v in hsv), hsv)

    # -- execution ----------------------------------------------------------

    def test_single_send_pacing_and_deadline(self):
        clock = Clock()
        bridge = Bridge(clock)
        args = self.args
        args.study_deadline = 0.9
        ops = [Op('line', [[0, 0], [.1, .1]], size=2, phase='sketch') for _ in range(10)]
        with patch('anime_study.time.monotonic', clock.monotonic), patch('anime_study.time.sleep', clock.sleep):
            result = execute_plan(bridge, ops, Budget(1, 1, 10, False, 'test'), args)
        self.assertGreater(result['sent'], 0)
        self.assertLess(result['sent'], len(ops))
        self.assertTrue(all(len(batch) == 1 for batch in bridge.batches))
        self.assertTrue(all(t < args.study_deadline for t in bridge.timestamps))
        self.assertLessEqual(clock.now, args.study_deadline)
        self.assertTrue(clock.pauses)
        self.assertTrue(all(b-a >= args.study_pause for a, b in zip(bridge.timestamps, bridge.timestamps[1:])))
        self.assertEqual(bridge.colors, ['#000000'])
        self.assertEqual(bridge.brushes, [2])
        self.assertEqual(bridge.layers, [0])
        self.assertGreater(stroke_delay(Op('line', [[0, 0], [1, 1]]), args),
                           stroke_delay(ops[0], args))

    def test_single_canvas_module_never_switches_layers(self):
        """Relay rounds reject layer calls (HTTP 409) and report no layers."""
        from anime_study import adapt_layers

        class Flat(Bridge):
            def state(self):
                return {'available': True, 'canDraw': True}

            def layer(self, value):
                raise AssertionError('layer switch on a single-canvas module')

        for pace in ('slow', 'instant'):
            clock = Clock()
            bridge = Flat(clock)
            args = self.args
            args.pace = pace
            args.study_deadline = 30
            ops = [Op('line', [[0, 0], [.1, .1]], size=2, layer=1, phase='sketch'),
                   Op('fill', [[0, 0], [.2, 0], [.2, .2]], color='#ff0000', layer=0, phase='color')]
            with patch('anime_study.time.monotonic', clock.monotonic), patch('anime_study.time.sleep', clock.sleep):
                result = execute_plan(bridge, ops, Budget(30, 30, 100, False, 'test', 3), args)
            self.assertEqual(result['sent'], 2, pace)
            self.assertGreaterEqual(result['sent'], 2, pace)

        # a person's order on one canvas: sketch, colour, shading, closing lines
        big = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8], [0.2, 0.2]]
        plan = [Op('line', [[0, 0], [.1, .1]], layer=1, phase='detail'),
                Op('fill', big, color='#ff0000', layer=0, phase='color'),
                Op('line', [[0, 0], [.2, .1]], layer=1, phase='sketch'),
                Op('line', [[.3, .3], [.5, .4]], color='#aa0000', size=20, layer=0, phase='shade')]
        flat = adapt_layers(plan, 1, 16 / 9)
        self.assertEqual([op.phase for op in flat], ['sketch', 'color', 'shade', 'detail'])
        self.assertTrue(all(op.layer == 0 for op in flat))
        self.assertEqual(plan[0].layer, 1)             # the input plan is untouched
        self.assertIs(adapt_layers(plan, 3), plan)

    def test_flat_fill_is_inset_so_it_does_not_bury_the_sketch(self):
        from anime_study import adapt_layers
        from dg_artist import is_simple_polygon
        big = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8], [0.2, 0.2]]
        fill = Op('fill', big, color='#ff0000', layer=0, phase='color')
        ink = Op('line', [[0, 0], [.1, .1]], layer=1, phase='sketch')
        out = [op for op in adapt_layers([ink, fill], 1, 16 / 9) if op.kind == 'fill']
        self.assertTrue(out)
        xs = [p[0] for op in out for p in op.points]
        ys = [p[1] for op in out for p in op.points]
        self.assertGreater(min(xs), 0.2)
        self.assertLess(max(xs), 0.8)
        self.assertGreater(min(ys), 0.2)
        self.assertLess(max(ys), 0.8)
        self.assertTrue(all(is_simple_polygon(op.points[:-1]) for op in out))
        # a sliver too small to shrink is kept as it was
        sliver = Op('fill', [[.5, .5], [.502, .5], [.502, .502], [.5, .502], [.5, .5]], layer=0, phase='color')
        kept = [op for op in adapt_layers([ink, sliver], 1, 16 / 9) if op.kind == 'fill']
        self.assertEqual(kept[0].points, sliver.points)

    def test_hud_setters_precede_drawing_and_only_change_when_needed(self):
        clock = Clock()
        bridge = Bridge(clock)
        self.args.study_deadline = 20
        ops = [Op('line', [[0, 0], [.1, .1]], '#112233', 2, phase='sketch'),
               Op('line', [[0, 0], [.1, .1]], '#112233', 2, phase='sketch'),
               Op('disc', [[.2, .2]], '#445566', 8, phase='sketch')]
        self.assertGreater(plan_seconds(ops, self.args), sum(stroke_seconds(o, self.args) for o in ops))
        with patch('anime_study.time.monotonic', clock.monotonic), patch('anime_study.time.sleep', clock.sleep):
            execute_plan(bridge, ops, Budget(20, 20, 30, False, 'test'), self.args)
        self.assertEqual([e[0] for e in bridge.events],
                         ['color', 'brush', 'draw', 'draw', 'color', 'brush', 'draw'])
        self.assertEqual(bridge.colors, ['#112233', '#445566'])
        self.assertEqual(bridge.brushes, [2, 8])

    def test_hud_setter_overrun_does_not_send_draw_or_another_setter(self):
        clock = Clock()
        bridge = Bridge(clock, latency=20)
        self.args.study_deadline = 2
        ops = [Op('line', [[0, 0], [.1, .1]], size=2, phase='sketch')]
        with patch('anime_study.time.monotonic', clock.monotonic), patch('anime_study.time.sleep', clock.sleep):
            execute_plan(bridge, ops, Budget(3, 2, 20, False, 'test'), self.args)
        self.assertEqual(bridge.colors, ['#000000'])
        self.assertFalse(bridge.brushes)
        self.assertFalse(bridge.batches)
        self.assertFalse(clock.pauses)

    def test_expired_planning_deadline_never_clears_or_sends(self):
        clock = Clock()
        clock.now = 20
        self.args.study_deadline = 10
        bridge = Bridge(clock)
        with patch('anime_study.time.monotonic', clock.monotonic), patch('anime_study.time.sleep', clock.sleep):
            execute_plan(bridge, [Op('line', [[0, 0], [.1, .1]], phase='sketch')],
                         Budget(60, 55, 280, False, 'test'), self.args)
        self.assertEqual(bridge.clears, 0)
        self.assertEqual(bridge.batches, [])

    def test_slow_synchronous_request_no_further_calls_or_sleep(self):
        clock = Clock()
        bridge = Bridge(clock, latency=20)
        self.args.study_deadline = 2
        ops = [Op('line', [[0, 0], [.1, .1]], size=2, phase='sketch')] * 4
        with patch('anime_study.time.monotonic', clock.monotonic), patch('anime_study.time.sleep', clock.sleep), \
             patch.object(bridge, 'color'), patch.object(bridge, 'brush'):
            execute_plan(bridge, ops, Budget(3, 2, 20, False, 'test'), self.args)
        self.assertEqual(len(bridge.batches), 1)
        self.assertEqual(clock.pauses, [.10, .05])

    def test_thick_strokes_get_round_cap_discs(self):
        clock = Clock()
        bridge = Bridge(clock)
        self.args.study_deadline = 30
        ops = [Op('line', [[0, 0], [.1, .1], [.2, .2]], '#112233', 30, phase='sketch'),
               Op('line', [[0, 0], [.1, .1]], '#112233', 3, phase='sketch')]
        with patch('anime_study.time.monotonic', clock.monotonic), \
             patch('anime_study.time.sleep', clock.sleep):
            execute_plan(bridge, ops, Budget(30, 30, 30, False, 'test'), self.args)
        self.assertEqual([len(batch) for batch in bridge.batches], [3, 1])
        line, end_cap, start_cap = bridge.batches[0]
        self.assertEqual(line['type'], 'line')
        self.assertEqual(end_cap['type'], 'disc')
        self.assertEqual(end_cap['points'], [[.2, .2]])
        self.assertEqual(start_cap['points'], [[0, 0]])
        self.assertEqual({cap['size'] for cap in (end_cap, start_cap)}, {30})

    def test_dry_run_budget_never_queries_bridge_and_tiny_time_is_safe(self):
        self.args.time = 1
        bridge = Bridge()
        with patch.object(bridge, 'state', side_effect=AssertionError('network')), \
             patch.object(bridge, 'round', side_effect=AssertionError('network')):
            budget = resolve_budget(bridge, 'anime_study', self.args)
        self.assertEqual(budget.usable_seconds, 0)
        self.assertEqual(GRID_STYLES, ['sketch', 'anime_flat', 'impasto', 'watercolor'])

    def test_live_round_timer_sets_the_budget(self):
        args = build_parser().parse_args(['unused', '--style', 'anime_study'])
        bridge = Bridge()

        def round_state():
            return {'recommended': {'remaining': 42.0}}

        bridge.round = round_state
        budget = resolve_budget(bridge, 'anime_study', args)
        self.assertEqual(budget.seconds, 42.0)
        self.assertEqual(budget.source, 'round')
        # a study style on a live timer always leaves 30 s (at most 40 % of a short round)
        self.assertAlmostEqual(budget.usable_seconds, 42.0 - 42.0 * 0.4)
        self.assertLess(complexity(budget.seconds, args), complexity(240, args))

    def test_plan_is_made_for_the_whole_round_even_when_part_of_it_has_passed(self):
        args = build_parser().parse_args(['unused', '--style', 'anime_study'])
        bridge = Bridge()
        bridge.round = lambda: {'recommended': {'remaining': 120.0, 'total': 180.0, 'passed': 60.0}}
        budget = resolve_budget(bridge, 'anime_study', args)
        self.assertEqual(budget.seconds, 180.0)                 # planned for 180 s ...
        self.assertAlmostEqual(budget.usable_seconds, 150.0)    # ... leaving the 30 s
        self.assertEqual(budget.remaining, 120.0)
        # more time than the plan was made for only ever adds
        bridge.round = lambda: {'recommended': {'remaining': 200.0, 'total': 180.0, 'passed': 0.0}}
        self.assertEqual(resolve_budget(bridge, 'anime_study', args).seconds, 200.0)
        # an explicit --time keeps its old meaning
        args = build_parser().parse_args(['unused', '--style', 'anime_study', '--time', '90'])
        budget = resolve_budget(bridge, 'anime_study', args)
        self.assertEqual(budget.seconds, 90.0)
        self.assertAlmostEqual(budget.usable_seconds, 90.0 - 90.0 * 0.12)

    def test_deadline_follows_the_timer_that_is_left_not_the_plan(self):
        from pacing import tighten_deadline
        args = build_parser().parse_args(['unused', '--style', 'anime_study'])
        bridge = Bridge()
        bridge.round = lambda: {'recommended': {'remaining': 180.0, 'total': 180.0, 'passed': 0.0}}
        resolve_budget(bridge, 'anime_study', args)             # plan for 180 s
        bridge.round = lambda: {'recommended': {'remaining': 120.0, 'total': 180.0, 'passed': 60.0}}
        deadline = tighten_deadline(bridge, args, 1000.0 + 150.0, 1000.0)
        self.assertAlmostEqual(deadline - 1000.0, 90.0)         # 120 s left, 30 s kept

    def test_main_counts_model_planning_time_before_execution_budget(self):
        clock = Clock()
        def slow_edges(*args):
            clock.now += 20
            return np.zeros(self.image.shape[:2], np.uint8)
        captured = []
        def capture_fit(ops, seconds, args, units):
            captured.append(seconds)
            return fit_plan(ops, seconds, args, units)
        import tempfile
        # main() checks that the weights file exists; the model itself is stubbed
        with tempfile.NamedTemporaryFile(suffix='.pth') as weights, \
             patch('dg_artist.time.monotonic', clock.monotonic), \
             patch('dg_artist.load_image', return_value=self.image), \
             patch('dg_artist.anime2sketch_edges', side_effect=slow_edges), \
             patch('dg_artist.resolve_device', return_value='cpu'), \
             patch('anime_study.fit_plan', side_effect=capture_fit):
            result = main(['unused', '--style', 'anime_study', '--dry-run',
                           '--time', '10', '--lineart', 'anime2sketch',
                           '--lineart-model', weights.name])
        self.assertEqual(result, 0)
        self.assertEqual(captured[-1], 0)

    def test_old_style_instant_pace_sends_one_batch_without_pauses(self):
        args = build_parser().parse_args(['unused', '--style', 'anime_flat', '--no-clear',
                                          '--pace', 'instant'])
        bridge = Bridge()
        ops = [Op('line', [[0, 0], [.1, .1]], phase='ink')] * 4
        with patch('dg_artist.time.sleep', side_effect=AssertionError('instant must not sleep')):
            execute_plan(bridge, ops, Budget(60, 55, 10, False, 'test'), args)
        self.assertEqual([len(b) for b in bridge.batches], [4])

    def test_old_style_slow_pace_spreads_batches_and_ends_before_deadline(self):
        clock = Clock()
        bridge = Bridge(clock, latency=0.1)
        args = build_parser().parse_args(['unused', '--style', 'anime_flat', '--no-clear'])
        args.budget_anchor = 0.0
        ops = [Op('line', [[0, 0], [.1, .1]], phase='ink') for _ in range(200)]
        with patch('dg_artist.time.monotonic', clock.monotonic), \
             patch('dg_artist.time.sleep', clock.sleep):
            result = execute_plan(bridge, ops, Budget(60, 50, 10, False, 'test'), args)
        self.assertEqual(result['applied'], 200)
        self.assertTrue(clock.pauses)
        self.assertLess(clock.now, 50)          # finished inside the usable time
        self.assertGreater(clock.now, 10)       # and actually spread out, not instant

    def test_old_style_late_start_rushes_and_never_passes_deadline(self):
        clock = Clock()
        bridge = Bridge(clock, latency=0.3)
        args = build_parser().parse_args(['unused', '--style', 'anime_flat', '--no-clear'])
        args.budget_anchor = 0.0
        clock.now = 45                           # planning ate most of the budget
        ops = [Op('line', [[0, 0], [.1, .1]], phase='ink') for _ in range(400)]
        with patch('dg_artist.time.monotonic', clock.monotonic), \
             patch('dg_artist.time.sleep', clock.sleep):
            execute_plan(bridge, ops, Budget(60, 50, 10, False, 'test'), args)
        self.assertLessEqual(clock.now, 50)

    def study_run(self, ops, deadline, pace='slow', latency=0.02, start=0.0):
        clock = Clock()
        clock.now = start
        bridge = Bridge(clock, latency=latency)
        args = build_parser().parse_args(['unused', '--style', 'anime_study', '--pace', pace])
        args.study_deadline = deadline
        with patch('anime_study.time.monotonic', clock.monotonic), \
             patch('anime_study.time.sleep', clock.sleep):
            result = execute_plan(bridge, ops, Budget(deadline, deadline, 900, False, 'test'), args)
        return result, clock, bridge

    def many_ops(self, n):
        return [Op('line', [[i / 400, 0], [i / 400 + .05, .05], [i / 400 + .1, .02]],
                   '#112233' if i % 7 else '#445566', 3 + (i % 3), phase='sketch') for i in range(n)]

    def test_reserve_rule(self):
        from pacing import reserve_seconds, usable_seconds
        self.assertEqual(reserve_seconds(10), 3.0)
        self.assertAlmostEqual(reserve_seconds(100), 12.0)
        self.assertEqual(reserve_seconds(1000), 20.0)
        self.assertEqual(usable_seconds(2), 0.0)
        self.assertAlmostEqual(usable_seconds(180), 180 - 20.0 if 180 * .12 > 20 else 180 - 180 * .12)

    def test_study_slow_never_passes_deadline_even_with_a_late_start(self):
        ops = self.many_ops(120)
        full, clock_full, _ = self.study_run(ops, 400)
        natural_end = clock_full.now
        # the same plan with a third of the natural time must be compressed, not overrun
        tight = natural_end * 0.5
        result, clock, bridge = self.study_run(ops, tight)
        self.assertLessEqual(clock.now, tight + 1e-6)
        self.assertGreater(result['sent'], len(ops) * 0.35)

    def test_study_slow_speeds_up_when_behind_schedule(self):
        ops = self.many_ops(60)
        _, clock_full, bridge_full = self.study_run(ops, 400)
        natural = clock_full.now
        gaps_full = np.diff(bridge_full.timestamps)

        # The round timer turns out shorter than planned once drawing has begun.
        clock = Clock()
        bridge = Bridge(clock, latency=0.02)
        limit = natural * 0.75

        def round_state():
            if clock.now < 6:
                return {}
            return {'recommended': {'remaining': (limit - clock.now) / 0.88}}

        bridge.round = round_state
        args = build_parser().parse_args(['unused', '--style', 'anime_study'])
        args.study_deadline = 400
        with patch('anime_study.time.monotonic', clock.monotonic), \
             patch('anime_study.time.sleep', clock.sleep):
            result = execute_plan(bridge, ops, Budget(400, 400, 900, False, 'test'), args)
        self.assertLessEqual(clock.now, limit + 1e-6)
        late = np.diff([t for t in bridge.timestamps if t > 6])
        self.assertLess(np.median(late), np.median(gaps_full))
        self.assertGreater(result['sent'], len(ops) * 0.5)

    def test_study_drops_tail_not_head_when_even_the_fast_hand_is_too_slow(self):
        ops = self.many_ops(150)
        result, clock, bridge = self.study_run(ops, 20)
        self.assertLessEqual(clock.now, 20 + 1e-6)
        self.assertGreater(result['sent'], 0)
        self.assertLess(result['sent'], len(ops))
        sent_points = [b[0]['points'][0] for b in bridge.batches]
        self.assertEqual(sent_points, [op.points[0] for op in ops[:len(sent_points)]])

    def test_study_slow_survives_a_slow_bridge(self):
        ops = self.many_ops(80)
        result, clock, _ = self.study_run(ops, 60, latency=0.5)
        self.assertLessEqual(clock.now, 60 + 1e-6)

    def test_study_instant_has_no_pauses_and_batches(self):
        ops = self.many_ops(100)
        result, clock, bridge = self.study_run(ops, 400, pace='instant')
        self.assertFalse(clock.pauses)
        self.assertEqual(result['sent'], 100)
        self.assertLess(len(bridge.batches), 100)
        self.assertLess(clock.now, 5)

    def test_study_instant_still_stops_at_the_deadline(self):
        ops = self.many_ops(100)
        result, clock, _ = self.study_run(ops, 0.3, pace='instant', latency=0.2)
        self.assertLessEqual(clock.now, 0.3 + 1e-6)
        self.assertLess(result['sent'], 100)

    def test_live_round_timer_is_reread_and_can_only_pull_the_deadline_in(self):
        from pacing import tighten_deadline
        args = build_parser().parse_args(['unused', '--style', 'anime_study'])

        class Round:
            def __init__(self, remaining):
                self.remaining = remaining
            def round(self):
                return {'recommended': {'remaining': self.remaining}}

        # 120 s really left -> usable 120 - 14.4 = 105.6 from now (t=10)
        self.assertAlmostEqual(tighten_deadline(Round(120), args, 170.0, 10.0), 10 + 105.6)
        # a longer timer never moves the deadline later
        self.assertEqual(tighten_deadline(Round(500), args, 50.0, 10.0), 50.0)
        # no round / explicit --time / errors leave it alone
        self.assertEqual(tighten_deadline(Round(0), args, 50.0, 10.0), 50.0)
        args.time = 30
        self.assertEqual(tighten_deadline(Round(5), args, 50.0, 10.0), 50.0)

        class Broken:
            def round(self):
                raise RuntimeError('down')
        args.time = None
        self.assertEqual(tighten_deadline(Broken(), args, 50.0, 10.0), 50.0)


if __name__ == '__main__':
    unittest.main()
