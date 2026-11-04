"""Regression tests for tools/compare_canvas.py.

No game or screenshot is required: these cover the coordinate flip, the
brush-width formula and the two operation formats.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from compare_canvas import load_ops, rasterize, thickness  # noqa: E402


def painted(image):
    return image.min(axis=2) < 240


class RasterTests(unittest.TestCase):
    WIDTH = 1000
    HEIGHT = 562
    WORLD = 17.7778

    def line(self, y, size=30):
        return {
            "kind": "line",
            "points": [[0.2, y], [0.8, y]],
            "color": "#000000",
            "size": size,
            "note": "study-color",
        }

    def test_normalized_y_is_flipped_for_the_screen(self):
        low = painted(rasterize([self.line(0.25)], self.WIDTH, self.HEIGHT,
                                self.WORLD, "plan"))
        high = painted(rasterize([self.line(0.75)], self.WIDTH, self.HEIGHT,
                                 self.WORLD, "plan"))
        low_rows = np.nonzero(low.any(axis=1))[0]
        high_rows = np.nonzero(high.any(axis=1))[0]
        # y=0.25 is near the bottom of the game canvas, so it lands low on
        # screen; y=0.75 lands near the top.
        self.assertGreater(low_rows.mean(), self.HEIGHT * 0.60)
        self.assertLess(high_rows.mean(), self.HEIGHT * 0.40)

    def test_thickness_matches_the_game_formula(self):
        expected = max(1, round(30 * self.WIDTH / (100 * self.WORLD)))
        self.assertEqual(thickness(30, self.WIDTH, self.WORLD), expected)
        mask = painted(rasterize([self.line(0.5)], self.WIDTH, self.HEIGHT,
                                 self.WORLD, "plan"))
        # Measure away from the round caps, at x = 60% of the canvas.
        column = mask[:, int(self.WIDTH * 0.6)]
        packed = np.nonzero(column)[0]
        self.assertGreaterEqual(packed.size, expected - 2)
        self.assertLessEqual(packed.size, expected + 2)

    def test_plan_and_live_formats_produce_the_same_pixels(self):
        plan = [self.line(0.5)]
        live = [{
            "type": 0, "color": "#000000", "size": 30, "points": [[0.2, 0.5], [0.8, 0.5]],
        }]
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            live_path = Path(tmp) / "lines.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            live_path.write_text(json.dumps({"module": "SketchbookDrawModule",
                                             "count": 1, "lines": live}), encoding="utf-8")
            plan_ops, plan_source = load_ops(plan_path)
            live_ops, live_source = load_ops(live_path)
        self.assertEqual(plan_source, "plan")
        self.assertEqual(live_source, "live")
        # Plans get executor caps; the live export has no cap elements here, so
        # the interiors must still agree exactly.
        a = painted(rasterize(plan_ops, self.WIDTH, self.HEIGHT, self.WORLD, plan_source))
        b = painted(rasterize(live_ops, self.WIDTH, self.HEIGHT, self.WORLD, live_source))
        centre = slice(int(self.WIDTH * 0.3), int(self.WIDTH * 0.7))
        self.assertTrue(np.array_equal(a[:, centre], b[:, centre]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
