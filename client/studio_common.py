"""Small stdlib-only helpers shared by the renderer and the studio server.

Keep this module free of cv2/numpy/torch so the local UI service can import it
without loading the heavy rendering stack.
"""
from __future__ import annotations

import math


def parse_region(value):
    """Parse a normalized canvas rect ``x,y,w,h`` (y-up) into floats."""
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
    else:
        parts = list(value)
    if len(parts) != 4:
        raise ValueError("region must be x,y,w,h")
    try:
        x, y, w, h = (float(part) for part in parts)
    except (TypeError, ValueError):
        raise ValueError("region values must be numbers") from None
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        raise ValueError("region values must be finite")
    if w <= 0 or h <= 0:
        raise ValueError("region width and height must be positive")
    if x < -1e-6 or y < -1e-6 or x + w > 1.000001 or y + h > 1.000001:
        raise ValueError("region must stay inside the canvas (0..1)")
    x, y = max(0.0, x), max(0.0, y)
    return (x, y, min(w, 1.0 - x), min(h, 1.0 - y))


def bounded_number(value, low, high, default=None, name="value"):
    """Return a clamped float, or ``default`` when value is missing/empty."""
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return max(low, min(high, number))


def bounded_int(value, low, high, default=None, name="value"):
    number = bounded_number(value, low, high, default, name)
    return None if number is None else int(round(number))
