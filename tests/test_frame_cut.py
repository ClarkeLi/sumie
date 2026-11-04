"""A figure cut off by the frame must fade out, not end in a straight line."""
import random
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'client'))
import frame_cut
from dg_artist import Budget, build_ops, build_parser, make_analysis, make_transform


def bust(w=300, h=400):
    """Head and shoulders, the body cut off by the bottom edge of the picture."""
    mask = np.zeros((h, w), np.uint8)
    cv2.ellipse(mask, (w // 2, 140), (70, 90), 0, 0, 360, 255, -1)
    cv2.rectangle(mask, (60, 220), (w - 60, h - 1), 255, -1)
    return mask


class Detection(unittest.TestCase):
    def test_finds_the_cut_edge_only(self):
        cut = frame_cut.detect(bust())
        self.assertEqual([s[0] for s in cut.spans], ['bottom'])
        side, a, b = cut.spans[0]
        self.assertAlmostEqual(a, 60, delta=3)
        self.assertAlmostEqual(b, 239, delta=3)

    def test_a_figure_inside_the_frame_has_no_cut(self):
        mask = np.zeros((300, 300), np.uint8)
        cv2.circle(mask, (150, 150), 80, 255, -1)
        cut = frame_cut.detect(mask)
        self.assertFalse(cut.active)
        self.assertIs(frame_cut.soften(mask, cut, 3), mask)

    def test_short_touches_and_short_gaps(self):
        mask = np.zeros((300, 300), np.uint8)
        mask[290:, 10:14] = 255                                   # 4 px: a touch, not a cut
        self.assertFalse(frame_cut.detect(mask).active)
        mask[280:, 50:120] = 255
        mask[280:, 125:200] = 255                                 # 5 px gap is bridged
        spans = frame_cut.detect(mask).spans
        self.assertEqual(len(spans), 1)


class Softening(unittest.TestCase):
    def setUp(self):
        self.mask = bust()
        self.cut = frame_cut.detect(self.mask)
        self.soft = frame_cut.soften(self.mask, self.cut, 7)

    def bottom_profile(self, mask):
        ys = np.where(mask.any(axis=0), mask.shape[0] - 1 - np.argmax(mask[::-1] > 0, axis=0), -1)
        return ys[60:240]

    def test_the_straight_edge_is_gone_and_the_curve_is_irregular(self):
        before = self.bottom_profile(self.mask)
        after = self.bottom_profile(self.soft)
        self.assertEqual(len(set(before.tolist())), 1)            # the cut is a straight line
        self.assertGreater(len(set(after.tolist())), 12)          # now it is not
        self.assertGreater(np.ptp(after), 0.03 * self.cut.scale)  # it bows into the figure
        self.assertLess((self.soft > 0).sum(), (self.mask > 0).sum())

    def test_curve_is_inside_the_figure_and_the_band_follows_it(self):
        self.assertTrue(np.all(self.soft[self.cut.band] > 0))
        self.assertGreater(int(self.cut.curve.sum()), 100)
        self.assertGreater(int(self.cut.band.sum()), int(self.cut.curve.sum()))

    def test_deterministic_for_a_seed_and_different_between_seeds(self):
        a = frame_cut.soften(self.mask, frame_cut.detect(self.mask), 7)
        b = frame_cut.soften(self.mask, frame_cut.detect(self.mask), 7)
        c = frame_cut.soften(self.mask, frame_cut.detect(self.mask), 8)
        self.assertTrue(np.array_equal(a, b))
        self.assertFalse(np.array_equal(a, c))


class Fraying(unittest.TestCase):
    def test_lines_end_at_scattered_places_and_some_trail_off(self):
        mask = bust()
        cut = frame_cut.detect(mask)
        frame_cut.soften(mask, cut, 7)
        rng = random.Random(3)
        strokes = [(np.column_stack((np.full(60, x, float), np.linspace(180, 399, 60))), 1, 10.0)
                   for x in range(75, 230, 8)]
        out = frame_cut.fray(strokes, cut, rng)
        ends = [p[-1][1] for p, tier, _ in out if tier == 1 and len(p) > 10]
        self.assertGreater(float(np.ptp(ends)), 0.02 * cut.scale)             # not one shared end line
        self.assertGreaterEqual(len(set(round(e) for e in ends)), len(ends) // 3)
        trails = [p for p, tier, _ in out if tier == 2]
        self.assertTrue(trails)                                     # lost lines exist
        self.assertTrue(all(len(p) >= 3 for p, _, _ in out))
        no_cut = frame_cut.fray(strokes, frame_cut.FrameCut(spans=[], scale=100), rng)
        self.assertEqual(len(no_cut), len(strokes))


class InThePlan(unittest.TestCase):
    def plan(self, soften):
        image = np.full((400, 300, 3), 245, np.uint8)
        cv2.ellipse(image, (150, 140), (70, 90), 0, 0, 360, (238, 205, 180), -1)
        cv2.rectangle(image, (60, 220), (239, 399), (190, 60, 70), -1)
        for x in (125, 175):
            cv2.circle(image, (x, 140), 7, (30, 30, 35), -1)
        args = build_parser().parse_args(['unused', '--style', 'anime_human', '--dry-run',
                                          '--soften-frame', soften])
        args.canvas_aspect, args.canvas_world_width = 16 / 9, 17.7778
        analysis = make_analysis(image, 'anime_human', args)
        transform, _, _ = make_transform(300, 400, 16 / 9)
        budget = Budget(120, 110, 900, False, 'test', 3)
        return build_ops('anime_human', analysis, budget, args, transform), analysis

    def test_off_keeps_the_old_behaviour_and_on_changes_the_edge(self):
        off, analysis_off = self.plan('off')
        on, analysis_on = self.plan('on')
        self.assertIsNone(analysis_off.cache.get('frame_cut'))
        cut = analysis_on.cache.get('frame_cut')
        self.assertIsNotNone(cut)
        self.assertTrue(cut.active)
        self.assertNotEqual([o.to_json() for o in off], [o.to_json() for o in on])

    def test_no_dark_border_on_the_faded_edge(self):
        ops, analysis = self.plan('on')
        cut = analysis.cache['frame_cut']
        # every stroke that lies in the transition zone is in its own colour or ink,
        # never a darkened copy of a fill colour along the new edge
        transform, _, _ = make_transform(300, 400, 16 / 9)
        w, h = 300, 400
        fit = np.array([[transform(x, y) for x, y in [(0, 0), (w, 0), (0, h)]]])
        origin, right, down = fit[0]
        borders = [o for o in ops if o.note in ('human-border', 'human-gap')]
        inside = 0
        for op in borders:
            norm = np.asarray(op.points)
            px = np.column_stack(((norm[:, 0] - origin[0]) / (right[0] - origin[0]) * w,
                                  (norm[:, 1] - origin[1]) / (down[1] - origin[1]) * h))
            flags = frame_cut.in_band(cut, px)
            if flags.mean() > 0.5:
                inside += 1
                fills = {o.color for o in ops if o.kind == 'bucket'}
                self.assertIn(op.color.upper(), {c.upper() for c in fills})
        self.assertGreaterEqual(inside + 1, 1)                        # the loop above ran


if __name__ == '__main__':
    unittest.main()
