"""Unit tests for the local studio service's pure helpers.

These cover request validation, option clamping and renderer argv building
without starting the HTTP server, a renderer subprocess or any heavy import.
"""
import base64
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
from studio_common import bounded_int, bounded_number, parse_region  # noqa: E402
from studio_server import (BadRequest, build_artist_argv, decode_image,  # noqa: E402
                           normalize_options, redact_argv, sniff_image)


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


class RegionTests(unittest.TestCase):
    def test_parses_normalized_rect(self):
        self.assertEqual(parse_region("0.1,0.2,0.3,0.4"), (0.1, 0.2, 0.3, 0.4))
        self.assertEqual(parse_region([0.05, 0.1, 0.9, 0.8]),
                         (0.05, 0.1, 0.9, 0.8))

    def test_rejects_bad_shapes(self):
        for value in ("0.1,0.2,0.3", "0.1,0.2,0,0.4", "a,b,c,d",
                      "0.8,0.1,0.4,0.2", "-0.1,0,0.2,0.2"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_region(value)

    def test_rejects_non_finite(self):
        with self.assertRaises(ValueError):
            parse_region("0.1,0.2,nan,0.3")

    def test_clamps_roundoff_at_the_edge(self):
        x, y, w, _h = parse_region("1.0,0.0,1e-7,1.0")
        self.assertEqual((x, y), (1.0, 0.0))
        self.assertGreaterEqual(w, 0)


class BoundTests(unittest.TestCase):
    def test_bounded_number(self):
        self.assertEqual(bounded_number(None, 0, 10, 5), 5)
        self.assertEqual(bounded_number("", 0, 10, 5), 5)
        self.assertEqual(bounded_number("99", 0, 10), 10)
        self.assertEqual(bounded_number(-3, 0, 10), 0)
        with self.assertRaises(ValueError):
            bounded_number("abc", 0, 10)

    def test_bounded_int_rounds(self):
        self.assertEqual(bounded_int(2.6, 1, 5), 3)
        self.assertIsNone(bounded_int(None, 1, 5, None))


class MaxDimTests(unittest.TestCase):
    def test_default_is_automatic_and_not_sent(self):
        options = normalize_options({})
        self.assertIsNone(options["max_dim"])
        argv = build_artist_argv("py", "a.py", "in.png", options)
        self.assertNotIn("--max-dim", argv)

    def test_explicit_value_is_clamped_and_sent(self):
        options = normalize_options({"max_dim": 5000})
        self.assertEqual(options["max_dim"], 2000)
        argv = build_artist_argv("py", "a.py", "in.png", options)
        self.assertEqual(argv[argv.index("--max-dim") + 1], "2000")


class PlanProgressTests(unittest.TestCase):
    def test_argv_carries_the_plan_progress_file(self):
        argv = build_artist_argv("py", "a.py", "in.png", normalize_options({}),
                                 plan_progress="job/plan_progress.json")
        self.assertEqual(argv[argv.index("--plan-progress-file") + 1], "job/plan_progress.json")
        self.assertNotIn("--plan-progress-file",
                         build_artist_argv("py", "a.py", "in.png", normalize_options({})))


class PaceTests(unittest.TestCase):
    def test_pace_is_validated_and_passed_to_every_style(self):
        for style in ("anime_study", "anime_flat", "sketch"):
            options = normalize_options({"style": style, "pace": "instant"})
            argv = build_artist_argv("py", "a.py", "in.png", options)
            self.assertEqual(argv[argv.index("--pace") + 1], "instant")
        argv = build_artist_argv("py", "a.py", "in.png", normalize_options({}))
        self.assertEqual(argv[argv.index("--pace") + 1], "slow")
        with self.assertRaises(BadRequest):
            normalize_options({"pace": "warp"})


class OptionsTests(unittest.TestCase):
    def test_defaults(self):
        options = normalize_options({})
        self.assertEqual(options["style"], "anime_study")
        self.assertIsNone(options["time"])
        self.assertEqual(options["default_time"], 180)
        self.assertIsNone(options["colors"])
        self.assertEqual(options["study_pause"], 0.26)
        self.assertEqual(options["study_speed"], 0.90)
        self.assertEqual(options["pace"], "slow")
        self.assertAlmostEqual(options["canvas_aspect"], 16 / 9)

    def test_clamps_and_keeps_explicit_values(self):
        options = normalize_options({
            "style": "anime_study", "time": "30", "colors": 99,
            "max_strokes": 0, "seed": -4, "margin": 0.9,
            "canvas_aspect": 2.0, "force": 1,
        })
        self.assertEqual(options["time"], 30)
        self.assertEqual(options["colors"], 64)
        self.assertEqual(options["max_strokes"], 1)
        self.assertEqual(options["seed"], 0)
        self.assertEqual(options["margin"], 0.30)
        self.assertTrue(options["force"])

    def test_study_fields_dropped_for_other_styles(self):
        options = normalize_options({"style": "anime_flat"})
        self.assertNotIn("study_pause", options)
        self.assertNotIn("study_speed", options)

    def test_rejects_unknown_enum_values(self):
        for raw in ({"style": "oil"}, {"lineart": "magic"},
                    {"lineart_device": "tpu"}):
            with self.subTest(raw=raw):
                with self.assertRaises(BadRequest):
                    normalize_options(raw)


class ArgvTests(unittest.TestCase):
    def options(self, **overrides):
        raw = {"style": "anime_study", "time": None, "colors": 6,
               "max_strokes": 288, "seed": 7, "margin": 0.04, "max_dim": 900,
               "study_pause": 0.26, "study_speed": 0.9, "lineart": "auto",
               "lineart_device": "mps", "canvas_aspect": 16 / 9}
        raw.update(overrides)
        return normalize_options(raw)

    def test_auto_time_omits_time_flag(self):
        argv = build_artist_argv("py", "artist.py", "in.png", self.options())
        self.assertNotIn("--time", argv)
        self.assertIn("--no-clear", argv)
        self.assertIn("anime_study", argv)

    def test_explicit_plan_time_is_passed(self):
        argv = build_artist_argv("py", "artist.py", "in.png", self.options(),
                                 time_value=42.5)
        index = argv.index("--time")
        self.assertEqual(argv[index + 1], "42.5")

    def test_plan_and_region_flags(self):
        argv = build_artist_argv(
            "py", "artist.py", "in.png", self.options(), clear=True,
            region=(0.1, 0.2, 0.3, 0.4), plan_in="plan.json",
            plan_out="out.json", preview="preview.png",
            progress="progress.json", stages="stages", dry_run=True)
        self.assertIn("--clear", argv)
        self.assertNotIn("--no-clear", argv)
        self.assertEqual(argv[argv.index("--region") + 1],
                         "0.100000,0.200000,0.300000,0.400000")
        self.assertEqual(argv[argv.index("--plan-in") + 1], "plan.json")
        self.assertIn("--plan-out", argv)
        self.assertIn("--preview", argv)
        self.assertIn("--progress-file", argv)
        self.assertIn("--study-stage-dir", argv)
        self.assertIn("--dry-run", argv)

    def test_token_and_redaction(self):
        argv = build_artist_argv("py", "artist.py", "in.png", self.options(),
                                 token="secret")
        self.assertEqual(argv[argv.index("--token") + 1], "secret")
        redacted = redact_argv(argv)
        self.assertEqual(redacted[redacted.index("--token") + 1], "***")
        self.assertEqual(argv[argv.index("--token") + 1], "secret")


class ImageTests(unittest.TestCase):
    def test_sniff_known_formats(self):
        self.assertEqual(sniff_image(PNG_1PX), "png")
        self.assertEqual(sniff_image(b"\xff\xd8\xff\xe0rest"), "jpg")
        self.assertEqual(sniff_image(b"RIFF\0\0\0\0WEBPVP8 "), "webp")
        self.assertEqual(sniff_image(b"GIF89a...."), "gif")
        self.assertEqual(sniff_image(b"BM...."), "bmp")
        self.assertIsNone(sniff_image(b"not an image"))

    def test_decode_data_url(self):
        raw, ext = decode_image("data:image/png;base64," +
                                base64.b64encode(PNG_1PX).decode())
        self.assertEqual(raw, PNG_1PX)
        self.assertEqual(ext, "png")

    def test_decode_rejects_bad_input(self):
        for value in (None, "", "data:text/plain;base64,aGVsbG8=",
                      "data:image/png,aGVsbG8=", "!!!", "aGVsbG8="):
            with self.subTest(value=value):
                with self.assertRaises(BadRequest):
                    decode_image(value)


class JsonShapeTests(unittest.TestCase):
    def test_options_are_json_serialisable(self):
        json.dumps(normalize_options({"style": "watercolor"}))


class HumanOptionArgv(unittest.TestCase):
    def test_human_and_style_options_reach_the_renderer(self):
        options = normalize_options({"style": "anime_human", "borders": False, "colour": "bucket",
                                     "soften_frame": "off", "game_fps": 144, "line_color": "#202020",
                                     "fill_mode": "polygon", "fill_white": True, "base_fill": False,
                                     "outlines": False, "lineart_threshold": 60, "max_regions": 300})
        argv = build_artist_argv("py", "a.py", "in.png", options)
        for flag in ("--borders", "--colour", "--soften-frame", "--game-fps", "--line-color",
                     "--fill-mode", "--fill-white", "--no-base-fill", "--no-outlines",
                     "--lineart-threshold", "--max-regions"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--borders") + 1], "off")

    def test_defaults_add_no_flags_and_bad_values_are_rejected(self):
        argv = build_artist_argv("py", "a.py", "in.png", normalize_options({"style": "anime_human"}))
        for flag in ("--borders", "--soften-frame", "--no-outlines", "--fill-white"):
            self.assertNotIn(flag, argv)
        with self.assertRaises(BadRequest):
            normalize_options({"line_color": "red"})
        with self.assertRaises(BadRequest):
            normalize_options({"soften_frame": "maybe"})


if __name__ == "__main__":
    unittest.main()
