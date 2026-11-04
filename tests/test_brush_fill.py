"""Colouring with the brush: the planner for one area and the brush plan of anime_human."""
import random
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'client'))
sys.path.insert(0, str(ROOT / 'tests'))
import anime_human as human
import brush_fill as bf
import bucket
import layers
from anime_study import plan_seconds
from dg_artist import Budget, build_ops, build_parser, make_analysis, make_transform

GRID = bucket.Grid(17.7778, 10.0)


class FakeOp:
    pass


def fake_stroke(cells, size, role, part):
    op = FakeOp()
    op.points = GRID.to_normalised(np.asarray(cells)).tolist()
    op.size, op.dur, op.pause = size, 0.15, 0.05
    return op


def paint(need, allowed=None, seed=1):
    painter = bf.Painter(GRID, fake_stroke, random.Random(seed), 300)
    state = painter.prepare(need, need if allowed is None else allowed, 1)
    ops = painter.run(state, ('coarse', 'fine'), 1e9)
    cover = np.zeros((GRID.H * bf.HI, GRID.W * bf.HI), bool)
    for op in ops:
        px = (GRID.to_cells(np.asarray(op.points)) + 0.5) * bf.HI - 0.5
        cover |= bf.footprint(cover.shape, px, op.size)
    return ops, cover, bf.upsample(need)


def shape(draw):
    mask = np.zeros((GRID.H, GRID.W), np.uint8)
    draw(mask)
    return mask > 0


class AreaPlanner(unittest.TestCase):
    def check(self, need, strokes, cover_min=0.93):
        ops, cover, big = paint(need)
        self.assertGreaterEqual((cover & big).sum() / big.sum(), cover_min)
        self.assertLessEqual(max(o.size for o in ops), 35)
        self.assertLessEqual(len(ops), strokes)
        # nothing but a thin fringe (under the line art) leaves the area
        self.assertLess((cover & ~cv2.dilate(big.astype(np.uint8), bf.disk(3)).astype(bool)).sum(),
                        0.01 * big.sum())
        return ops

    def test_a_disc_is_one_edge_stroke_and_a_few_sweeps(self):
        ops = self.check(shape(lambda m: cv2.circle(m, (300, 250), 110, 1, -1)), 12)
        self.assertEqual(max(o.size for o in ops), 35)

    def test_an_l_shape(self):
        def draw(m):
            cv2.rectangle(m, (100, 100), (300, 140), 1, -1)
            cv2.rectangle(m, (100, 100), (140, 300), 1, -1)
        self.check(shape(draw), 12)

    def test_a_ring_is_painted_round_not_across(self):
        def draw(m):
            cv2.circle(m, (300, 250), 110, 1, -1)
            cv2.circle(m, (300, 250), 70, 0, -1)
        self.check(shape(draw), 60, 0.9)

    def test_a_thin_strip_gets_a_brush_that_fits_it(self):
        ops = self.check(shape(lambda m: cv2.rectangle(m, (100, 240), (400, 255), 1, -1)), 8)
        self.assertLessEqual(max(o.size for o in ops), 35)
        self.assertGreaterEqual(min(o.size for o in ops), 12)       # 30 px high on the fine raster

    def test_the_brush_stays_inside_what_is_allowed(self):
        need = shape(lambda m: cv2.circle(m, (300, 250), 60, 1, -1))
        allowed = need.copy()
        ops, cover, big = paint(need, allowed)
        outside = cover & ~cv2.dilate(bf.upsample(allowed).astype(np.uint8), bf.disk(3)).astype(bool)
        self.assertLess(outside.sum(), 0.01 * big.sum())

    def test_room_to_overshoot_allows_a_bigger_brush(self):
        need = shape(lambda m: cv2.rectangle(m, (100, 240), (400, 248), 1, -1))      # 16 px high
        allowed = shape(lambda m: cv2.rectangle(m, (100, 215), (400, 280), 1, -1))
        tight, _, _ = paint(need)
        loose, cover, big = paint(need, allowed)
        self.assertLess(max(o.size for o in tight), 24)
        self.assertEqual(max(o.size for o in loose), 35)
        self.assertGreaterEqual((cover & big).sum() / big.sum(), 0.93)

    def test_same_seed_same_strokes(self):
        need = shape(lambda m: cv2.circle(m, (300, 250), 90, 1, -1))
        a, _, _ = paint(need, seed=4)
        b, _, _ = paint(need, seed=4)
        self.assertEqual([o.points for o in a], [o.points for o in b])

    def test_a_stroke_that_adds_nothing_is_dropped(self):
        need = shape(lambda m: cv2.circle(m, (300, 250), 60, 1, -1))
        painter = bf.Painter(GRID, fake_stroke, random.Random(1), 300)
        state = painter.prepare(need, need, 1)
        painter.run(state, ('coarse', 'fine'), 1e9)
        again = painter.run(state, ('coarse', 'fine'), 1e9)
        self.assertEqual(again, [])

    def test_budget_stops_the_planner(self):
        need = shape(lambda m: cv2.circle(m, (300, 250), 150, 1, -1))
        painter = bf.Painter(GRID, fake_stroke, random.Random(1), 300)
        state = painter.prepare(need, need, 1)
        ops = painter.run(state, ('coarse', 'fine'), 0.5)
        full, _, _ = paint(need)
        self.assertLess(len(ops), len(full))


class Helpers(unittest.TestCase):
    def test_clip_path_keeps_only_what_fits(self):
        zone = np.zeros((200, 400), bool)
        zone[50:150, 100:300] = True
        path = np.column_stack((np.linspace(0, 399, 200), np.full(200, 100.0)))
        pieces = bf.clip_path(path, 20, zone)
        self.assertEqual(len(pieces), 1)
        self.assertGreater(pieces[0][:, 0].min(), 100)
        self.assertLess(pieces[0][:, 0].max(), 300)

    def test_pieces_at_a_fork_continue_straight(self):
        trunk = np.column_stack((np.linspace(0, 50, 20), np.zeros(20)))
        straight = np.column_stack((np.linspace(50, 100, 20), np.zeros(20)))
        side = np.column_stack((np.full(20, 50.0), np.linspace(0, 50, 20)))
        merged = bf.merge_at_junctions([trunk, straight, side])
        self.assertEqual(len(merged), 2)
        self.assertEqual(sorted(len(p) for p in merged), [20, 39])

    def test_sweeps_connect_into_a_zigzag(self):
        mask = np.zeros((300, 500), bool)
        mask[50:250, 50:450] = True
        paths = bf.sweep_paths(mask, 0.0, 30.0, random.Random(1))
        self.assertLessEqual(len(paths), 3)
        self.assertGreater(max(len(p) for p in paths), 8)

    def test_footprint_is_as_wide_as_the_size(self):
        fp = bf.footprint((100, 200), np.array([[20.0, 50.0], [180.0, 50.0]]), 20)
        self.assertAlmostEqual(int(fp[:, 100].sum()), 20, delta=1)


class BrushPlan(unittest.TestCase):
    def setUp(self):
        img = np.full((300, 220, 3), 246, np.uint8)
        cv2.ellipse(img, (110, 190), (80, 100), 0, 0, 360, (190, 60, 70), -1)
        cv2.circle(img, (110, 90), 55, (238, 205, 180), -1)
        cv2.ellipse(img, (110, 55), (58, 36), 0, 180, 360, (50, 40, 110), -1)
        for x in (92, 128):
            cv2.circle(img, (x, 95), 7, (30, 30, 35), -1)
        self.image = img
        self.args = build_parser().parse_args(['unused', '--style', 'anime_human', '--dry-run',
                                               '--colour', 'brush'])
        self.args.layer_roles = layers.assign(9)
        self.args.layer_bands = 3
        self.transform, _, _ = make_transform(220, 300, 16 / 9)
        self.args.canvas_aspect = 16 / 9
        self.args.canvas_world_width = 17.7778

    def plan(self, seconds=120):
        analysis = make_analysis(self.image, 'anime_human', self.args)
        budget = Budget(seconds, seconds * 0.92, 900, False, 'test', 3)
        return build_ops('anime_human', analysis, budget, self.args, self.transform), budget

    def test_colour_is_brush_strokes_on_layers_under_the_ink(self):
        ops, _ = self.plan()
        roles = self.args.layer_roles
        self.assertFalse(any(o.kind == 'bucket' for o in ops))
        self.assertTrue(all(o.vlayer is not None for o in ops))
        fills = [o for o in ops if o.note == 'human-fill']
        self.assertGreater(len(fills), 3)
        self.assertTrue(all(o.vlayer in (roles['flat'], roles['flat_top']) for o in fills))
        ink = [o for o in ops if o.note in ('human-contour', 'human-inner', 'human-border')]
        self.assertTrue(ink)
        self.assertTrue(all(o.vlayer == roles['ink'] for o in ink))
        self.assertGreaterEqual(max(o.size for o in fills), 26)

    def test_borders_off_leaves_the_colour_edges_untraced(self):
        on, _ = self.plan()
        self.args.borders = 'off'
        off, _ = self.plan()
        self.assertGreater(sum(1 for o in on if o.note == 'human-border'), 0)
        self.assertEqual(sum(1 for o in off if o.note == 'human-border'), 0)
        # the line art before the colour and the colour itself stay
        self.assertGreater(sum(1 for o in off if o.note in ('human-contour', 'human-inner')), 5)
        self.assertGreater(sum(1 for o in off if o.note == 'human-fill'), 3)

    def test_the_line_art_comes_first(self):
        ops, _ = self.plan()
        first_fill = next(i for i, o in enumerate(ops) if o.note == 'human-fill')
        self.assertGreater(sum(1 for o in ops[:first_fill] if o.note.startswith('human-')), 5)
        self.assertEqual(ops[0].vlayer, self.args.layer_roles['ink'])

    def test_the_picture_has_colour_and_the_ink_stays_visible(self):
        ops, _ = self.plan()
        picture, _ = human.simulate(ops, 16 / 9, 17.7778, width=640)
        ink_only, _ = human.simulate([o for o in ops if o.vlayer == self.args.layer_roles['ink']],
                                     16 / 9, 17.7778, width=640)
        painted = (picture.min(axis=2) < 235).mean()
        self.assertGreater(painted, 0.10)
        ink_pixels = ink_only.min(axis=2) < 120
        # the colour lies under the line art: every dark pixel of the ink survives
        self.assertGreater((picture.min(axis=2)[ink_pixels] < 120).mean(), 0.97)

    def test_the_figure_is_painted_without_holes(self):
        ops, _ = self.plan(180)
        picture, _ = human.simulate(ops, 16 / 9, 17.7778, width=640)
        h, w = picture.shape[:2]
        window = picture[int(h * 0.35):int(h * 0.75), int(w * 0.42):int(w * 0.58)]    # the middle of the figure
        self.assertLess((window.min(axis=2) > 240).mean(), 0.10)

    def test_plan_fits_the_time_and_is_deterministic(self):
        for seconds in (45, 120, 180):
            ops, budget = self.plan(seconds)
            self.assertLessEqual(plan_seconds(ops, self.args), budget.usable_seconds + 1e-6)
        a, _ = self.plan(90)
        b, _ = self.plan(90)
        self.assertEqual([o.to_json() for o in a], [o.to_json() for o in b])

    def test_bucket_mode_is_untouched(self):
        self.args.colour, self.args.layer_roles = 'bucket', None
        ops, _ = self.plan()
        self.assertTrue(any(o.kind == 'bucket' for o in ops))
        self.assertTrue(all(o.vlayer is None for o in ops))

    def test_too_few_layers_means_the_bucket(self):
        self.args.layer_roles = layers.assign(4)
        ops, _ = self.plan()
        self.assertTrue(any(o.kind == 'bucket' for o in ops))


if __name__ == '__main__':
    unittest.main()
