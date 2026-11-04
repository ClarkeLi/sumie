"""Regression tests for doubled strokes and the flat painter's data contracts.

Run: python tests/test_artist_geometry.py
These tests need no game, checkpoint or network.
"""
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from artist_geometry import thin_mask, trace_centerlines
from dg_artist import (Budget, build_anime_flat, build_parser, color_detail_candidates,
                       edge_map, make_analysis, make_transform, summarize_ops)


class CenterlineTests(unittest.TestCase):
    def test_thick_line_becomes_one_open_stroke(self):
        mask = np.zeros((32, 64), np.uint8)
        cv2.line(mask, (8, 16), (55, 16), 255, 5)
        paths = trace_centerlines(mask)
        self.assertEqual(len(paths), 1)
        self.assertGreater(np.ptp(paths[0][:, 0]), 40)
        self.assertLessEqual(np.ptp(paths[0][:, 1]), 1)
        self.assertFalse(np.array_equal(paths[0][0], paths[0][-1]))

    def test_closed_loop_is_traced_once(self):
        mask = np.zeros((32, 32), np.uint8)
        cv2.rectangle(mask, (6, 6), (25, 25), 255, 1)
        paths = trace_centerlines(mask)
        self.assertEqual(len(paths), 1)
        self.assertTrue(np.array_equal(paths[0][0], paths[0][-1]))
        edges = [tuple(sorted((tuple(a), tuple(b))))
                 for a, b in zip(paths[0][:-1], paths[0][1:])]
        self.assertEqual(len(edges), len(set(edges)))

    def test_cross_has_four_nonduplicated_branches(self):
        mask = np.zeros((32, 32), np.uint8)
        cv2.line(mask, (4, 16), (28, 16), 255, 1)
        cv2.line(mask, (16, 4), (16, 28), 255, 1)
        paths = trace_centerlines(mask)
        self.assertEqual(len(paths), 4)
        links = []
        for path in paths:
            links.extend(tuple(sorted((tuple(a), tuple(b))))
                         for a, b in zip(path[:-1], path[1:]))
        self.assertEqual(len(links), len(set(links)))

    def test_nearby_lines_remain_separate(self):
        mask = np.zeros((32, 64), np.uint8)
        mask[10, 5:58] = 255
        mask[14, 5:58] = 255
        self.assertEqual(len(trace_centerlines(mask)), 2)

    def test_empty_mask_and_idempotence(self):
        self.assertEqual(trace_centerlines(np.zeros((20, 20), np.uint8)), [])
        mask = np.zeros((32, 64), np.uint8)
        cv2.line(mask, (8, 16), (55, 16), 255, 5)
        skeleton = thin_mask(mask)
        np.testing.assert_array_equal(skeleton, thin_mask(skeleton))

    def test_classical_dark_ridge_is_not_two_canny_sides(self):
        image = np.full((40, 80, 3), 255, np.uint8)
        cv2.line(image, (10, 20), (70, 20), (0, 0, 0), 3)
        self.assertEqual(len(trace_centerlines(edge_map(image))), 1)


class FlatPainterTests(unittest.TestCase):
    def setUp(self):
        self.image = np.full((80, 80, 3), 235, np.uint8)
        cv2.circle(self.image, (40, 40), 29, (210, 80, 75), -1)
        cv2.circle(self.image, (40, 40), 15, (60, 145, 200), -1)
        self.edges = np.zeros((80, 80), np.uint8)
        cv2.line(self.edges, (10, 8), (65, 8), 255, 3)
        self.transform, _, _ = make_transform(80, 80, 1, margin=0)

    def plan(self, units=1000, mode="auto", limit=None, edges=None):
        argv = ["unused.png", "--fill-mode", mode, "--colors", "16"]
        if limit is not None:
            argv += ["--max-strokes", str(limit)]
        args = build_parser().parse_args(argv)
        analysis = make_analysis(self.image, "anime_flat", args,
                                 self.edges if edges is None else edges)
        return build_anime_flat(analysis, Budget(60, 55, units, False, "test", 3),
                                args, self.transform)

    def test_no_region_fences_or_random_hatching_in_auto_mode(self):
        ops = self.plan()
        self.assertTrue(any(o.kind == "line" for o in ops))
        self.assertTrue(all(o.note == "centerline" for o in ops if o.kind == "line"))
        self.assertFalse(any(o.phase in ("shade", "hatch", "outline") for o in ops))
        self.assertFalse(any(o.kind == "realfill" for o in ops))

    def test_polygon_points_are_not_fill_seeds(self):
        for op in self.plan(mode="polygon"):
            if op.kind == "fill":
                self.assertGreaterEqual(len(op.points), 3)

    def test_explicit_real_fill_has_closed_source_colored_fence(self):
        ops = self.plan(mode="real")
        fills = [(i, o) for i, o in enumerate(ops) if o.kind == "realfill"]
        self.assertTrue(fills)
        for i, fill in fills:
            fence = ops[i - 1]
            self.assertEqual(fence.note, "fill-fence")
            self.assertEqual(fence.color, fill.color)
            self.assertEqual(fence.points[0], fence.points[-1])
            self.assertEqual(len(fill.points), 1)
            self.assertGreaterEqual(len(fill.fallback), 3)

    def test_budgets_and_stroke_cap(self):
        for mode in ("auto", "real", "polygon"):
            for units in (10, 30, 66, 198, 607, 2200):
                with self.subTest(mode=mode, units=units):
                    self.assertLessEqual(summarize_ops(self.plan(units, mode))[1], units + 1e-6)
        for limit in (0, 1, 5, 20):
            self.assertLessEqual(len(self.plan(limit=limit)), limit)

    def test_local_palette_keeps_rare_small_colors(self):
        image = np.full((80, 80, 3), 235, np.uint8)
        image[20:25, 20:25] = (35, 145, 120)
        edges = np.zeros((80, 80), np.uint8)
        edges[19:26, 19:26] = 255
        args = build_parser().parse_args(["unused.png"])
        analysis = make_analysis(image, "anime_flat", args, edges)
        painted = np.full_like(image, 235)
        candidates = color_detail_candidates(analysis, painted)
        self.assertTrue(any(np.abs(color.astype(int) - (35, 145, 120)).max() < 20
                            for _, _, _, _, color in candidates))

    def test_nested_color_islands_are_restored(self):
        ops = self.plan(edges=np.zeros((80, 80), np.uint8))
        canvas = Image.new("RGB", (80, 80), "white")
        draw = ImageDraw.Draw(canvas)
        for op in ops:
            if op.kind == "fill":
                draw.polygon([(p[0] * 80, (1 - p[1]) * 80) for p in op.points], op.color)
        for x, y in ((40, 40), (40, 18), (5, 5)):
            delta = np.abs(np.array(canvas.getpixel((x, y)), dtype=int) - self.image[y, x])
            self.assertLess(int(delta.max()), 18)


if __name__ == "__main__":
    unittest.main()
