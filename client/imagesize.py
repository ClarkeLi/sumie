"""Working size for planning, chosen from the time the round leaves.

Measured on an Apple M4 (anime_study, a 2400 px source resized down):

    max side   400   560   720   900  1200  1800
    plan [s]   6.4   8.5  10.5  11.6  15.8  34
    (other styles at 900 px: flat 5.7, sketch 8.6, impasto 9.3, watercolor 8.1)

Two fixed model passes cost about 3 s regardless of size (Anime2Sketch always
runs at 512 px, anime-seg at 1024 px); the rest grows like side**1.6. Colour
error, SSIM and silhouette IoU stop improving between 720 and 960, so a larger
working image only buys planning time. The size is therefore the largest one,
capped at 960, whose planning time stays under PLAN_SHARE of the round.
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps

MIN_SIDE = 480
MAX_SIDE = 960
PLAN_SHARE = 0.08          # planning may use this much of the round
FIXED_SECONDS = 4.0        # model passes + I/O at 900 px for anime_study
VARIABLE_SECONDS = 7.6     # size dependent part at 900 px
REFERENCE_SIDE = 900.0
EXPONENT = 1.6
# Measured planning time at 900 px relative to anime_study (11.6 s): the other
# styles skip the segmentation network or the stroke merging.
STYLE_COST = {'anime_study': 1.0, 'anime_human': 1.1, 'anime_flat': 0.5, 'sketch': 0.75,
              'impasto': 0.8, 'watercolor': 0.7}


def planning_seconds(side, style='anime_study'):
    """Estimated planning time for a working image with this longest side."""
    factor = STYLE_COST.get(style, 1.0)
    return factor * (FIXED_SECONDS + VARIABLE_SECONDS * (side / REFERENCE_SIDE) ** EXPONENT)


def auto_max_dim(seconds, style='anime_study'):
    """Largest working side (multiple of 20) that plans within PLAN_SHARE."""
    factor = STYLE_COST.get(style, 1.0)
    budget = float(seconds) * PLAN_SHARE / factor - FIXED_SECONDS
    if budget <= 0:
        return MIN_SIDE
    side = REFERENCE_SIDE * (budget / VARIABLE_SECONDS) ** (1.0 / EXPONENT)
    return max(MIN_SIDE, min(MAX_SIDE, int(side // 20 * 20)))


def load_scaled(source, max_dim):
    """Decode an image, upright, on white, no larger than max_dim on the long side.

    The image is shrunk before it is composited, so a 12000 px photo never has to
    exist as a full-size RGBA array. Smaller images are never enlarged.
    Returns (RGB PIL image, original (width, height)).
    """
    image = Image.open(source)
    original = image.size
    if max_dim and max(image.size) > max_dim:
        try:
            image.draft('RGB', (max_dim, max_dim))     # JPEG: decode at 1/2, 1/4, 1/8
        except Exception:
            pass
    image = ImageOps.exif_transpose(image)
    if image.mode in ('P', '1', 'LA', 'PA'):
        image = image.convert('RGBA')                   # palette modes resize badly
    if max_dim and max(image.size) > max_dim:
        image.thumbnail((max_dim, max_dim), Image.LANCZOS, reducing_gap=2.0)
    rgba = image.convert('RGBA')
    background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
    background.alpha_composite(rgba)
    return background.convert('RGB'), original


def describe(original, size, max_dim, auto):
    ow, oh = original
    w, h = size
    note = 'auto' if auto else 'fixed'
    if (ow, oh) == (w, h):
        return f'{w}x{h} px (kept, limit {max_dim} {note})'
    return f'{ow}x{oh} -> {w}x{h} px (limit {max_dim} {note})'
