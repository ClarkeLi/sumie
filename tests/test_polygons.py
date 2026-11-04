"""Fill-polygon validation regressions. No game, model, or network required."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client'))
from dg_artist import is_simple_polygon, simple_polygon


def contour(points):
    return np.asarray(points, dtype=np.int32).reshape(-1, 1, 2)


class SimplePolygonTests(unittest.TestCase):
    def test_square_is_simple(self):
        square = [[0, 0], [10, 0], [10, 10], [0, 10]]
        self.assertTrue(is_simple_polygon(square))

    def test_bowtie_is_not_simple(self):
        bowtie = [[0, 0], [10, 10], [10, 0], [0, 10]]
        self.assertFalse(is_simple_polygon(bowtie))

    def test_simple_polygon_keeps_a_square(self):
        polygon = simple_polygon(contour([[0, 0], [10, 0], [10, 10], [0, 10]]))
        self.assertIsNotNone(polygon)
        self.assertTrue(is_simple_polygon(polygon))

    def test_simple_polygon_rejects_a_bowtie(self):
        self.assertIsNone(simple_polygon(contour([[0, 0], [10, 10], [10, 0], [0, 10]])))

    def test_three_points_is_too_short(self):
        self.assertFalse(is_simple_polygon([[0, 0], [1, 1]]))


if __name__ == '__main__':
    unittest.main()
