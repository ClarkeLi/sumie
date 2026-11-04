"""Line-art path merging: grid-hash neighbour search and greedy joining."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client'))
from study_lineart import _near_pairs, merge_paths


def _line(x0, y0, x1, y1, n=10):
    return np.column_stack((np.linspace(x0, x1, n), np.linspace(y0, y1, n)))


class NearPairsTest(unittest.TestCase):
    def test_matches_brute_force(self):
        rng = np.random.default_rng(3)
        points = rng.uniform(0, 60, size=(300, 2))
        radius = 4.0
        got = {tuple(sorted(map(int, p))) for p in _near_pairs(points, radius)}
        want = set()
        for i in range(len(points)):
            for j in range(i + 1, len(points)):
                if np.linalg.norm(points[i] - points[j]) <= radius:
                    want.add((i, j))
        self.assertEqual(got, want)

    def test_empty_when_far_apart(self):
        points = np.array([[0.0, 0.0], [50.0, 50.0]])
        self.assertEqual(len(_near_pairs(points, 3.0)), 0)


class MergePathsTest(unittest.TestCase):
    def test_joins_collinear_fragments(self):
        merged = merge_paths([_line(0, 0, 10, 0), _line(12, 0, 22, 0)], max_gap=4)
        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(abs(merged[0][0][0] - merged[0][-1][0]), 22, delta=0.5)

    def test_keeps_distant_fragments_apart(self):
        merged = merge_paths([_line(0, 0, 10, 0), _line(0, 40, 10, 40)], max_gap=4)
        self.assertEqual(len(merged), 2)

    def test_does_not_join_sharp_turn(self):
        merged = merge_paths([_line(0, 0, 10, 0), _line(11, 0, 11, 10)], max_gap=4, max_turn_deg=40)
        self.assertEqual(len(merged), 2)

    def test_chain_of_many_fragments_is_one_curve(self):
        pieces = [_line(i * 12, 0, i * 12 + 10, 0) for i in range(8)]
        merged = merge_paths(pieces, max_gap=4)
        self.assertEqual(len(merged), 1)
        self.assertGreaterEqual(len(merged[0]), 8 * 10)


if __name__ == '__main__':
    unittest.main()
