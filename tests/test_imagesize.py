"""Working-size choice and fast, safe decoding of large photos."""
import io
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client'))
import imagesize
from imagesize import MAX_SIDE, MIN_SIDE, auto_max_dim, load_scaled, planning_seconds


class AutoSizeTests(unittest.TestCase):
    def test_bounds_and_granularity(self):
        for seconds in (1, 10, 30, 60, 120, 180, 600, 5000):
            for style in imagesize.STYLE_COST:
                side = auto_max_dim(seconds, style)
                self.assertGreaterEqual(side, MIN_SIDE)
                self.assertLessEqual(side, MAX_SIDE)
                self.assertEqual(side % 20, 0)

    def test_longer_rounds_never_get_a_smaller_image(self):
        sides = [auto_max_dim(s) for s in (20, 40, 60, 90, 120, 180, 300, 600)]
        self.assertEqual(sides, sorted(sides))
        self.assertLess(sides[0], sides[-1])

    def test_short_round_is_small_and_long_round_is_capped(self):
        self.assertEqual(auto_max_dim(15), MIN_SIDE)
        self.assertEqual(auto_max_dim(3600), MAX_SIDE)

    def test_planning_fits_its_share_of_the_round(self):
        for seconds in (120, 180, 300):
            side = auto_max_dim(seconds)
            self.assertLessEqual(planning_seconds(side), seconds * imagesize.PLAN_SHARE + 1.0)

    def test_estimate_matches_the_measured_m4_numbers(self):
        # measured: 400 px 6.4 s, 900 px 11.6 s, 1800 px 34 s (anime_study)
        self.assertAlmostEqual(planning_seconds(900), 11.6, delta=0.5)
        self.assertAlmostEqual(planning_seconds(400), 6.4, delta=1.5)

    def test_cheaper_styles_afford_a_bigger_image(self):
        self.assertGreaterEqual(auto_max_dim(100, 'anime_flat'), auto_max_dim(100, 'anime_study'))


class LoadScaledTests(unittest.TestCase):
    def save(self, image, name, **kw):
        path = Path(self.tmp.name) / name
        image.save(path, **kw)
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_large_image_is_shrunk_keeping_aspect(self):
        path = self.save(Image.new('RGB', (4000, 3000), (10, 120, 200)), 'big.png')
        image, original = load_scaled(path, 800)
        self.assertEqual(original, (4000, 3000))
        self.assertEqual(max(image.size), 800)
        self.assertAlmostEqual(image.size[0] / image.size[1], 4 / 3, places=2)

    def test_small_image_is_never_enlarged(self):
        path = self.save(Image.new('RGB', (300, 200), 'white'), 'small.png')
        image, original = load_scaled(path, 900)
        self.assertEqual(image.size, (300, 200))

    def test_jpeg_uses_fast_draft_decode_and_still_lands_at_the_limit(self):
        path = self.save(Image.new('RGB', (6000, 4000), (200, 30, 30)), 'photo.jpg', quality=85)
        image, original = load_scaled(path, 700)
        self.assertEqual(original, (6000, 4000))
        self.assertLessEqual(max(image.size), 700)
        self.assertGreaterEqual(max(image.size), 560)
        self.assertLess(abs(int(np.array(image)[..., 0].mean()) - 200), 6)

    def test_transparency_is_composited_on_white(self):
        rgba = Image.new('RGBA', (100, 100), (255, 0, 0, 0))
        path = self.save(rgba, 'clear.png')
        image, _ = load_scaled(path, 900)
        self.assertEqual(image.getpixel((50, 50)), (255, 255, 255))

    def test_palette_png_with_transparency_works(self):
        base = Image.new('RGBA', (80, 80), (0, 0, 255, 255))
        base.putpixel((0, 0), (0, 0, 0, 0))
        path = self.save(base.convert('P'), 'pal.png')
        image, _ = load_scaled(path, 40)
        self.assertEqual(image.mode, 'RGB')

    def test_exif_rotation_is_applied(self):
        source = Image.new('RGB', (200, 100), 'white')
        exif = Image.Exif()
        exif[0x0112] = 6                       # rotate 90 degrees clockwise
        path = self.save(source, 'rot.jpg', exif=exif)
        image, _ = load_scaled(path, 900)
        self.assertEqual(image.size, (100, 200))

    def test_no_limit_keeps_full_size(self):
        path = self.save(Image.new('RGB', (1500, 900), 'white'), 'full.png')
        image, _ = load_scaled(path, None)
        self.assertEqual(image.size, (1500, 900))


if __name__ == '__main__':
    unittest.main()
