"""Hand-motion and pen-pressure model for the `anime_human` style.

The game's own pen support (LinePressure.cs) does not give a stroke a varying
width that other players see: the live thickness follows the pressure, but the
element that is finished and synchronised has ONE BrushSize, the smoothed
pressure of the last frames. So a tablet player's strokes differ from each
other in width, not along their length. This module reproduces exactly that:
it simulates the pressure the pen would report at 60 Hz and runs it through a
port of the game's smoothing to get the BrushSize the game would have stored.

Timing comes from statistics measured on real doodles (client/calibration/
quickdraw_motor.json, made by tools/calibrate_motor.py).
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

import numpy as np

CALIBRATION = Path(__file__).resolve().parent / "calibration" / "quickdraw_motor.json"

# LinePressure.cs / DrawInput.cs constants
FPS = 60.0
PEN_MIN = 2
PEN_MAX = 35
SMOOTHING = 10
DROP = 5
CUTOFF = 0.05
PRESSURE_FLOOR = 0.052          # just above the cutoff: the thinnest line the game can store is 3
MIN_STEP_WORLD = 0.015          # the game only records a point after this much movement

# pressure a hand typically uses per kind of stroke (fraction of the sensor range)
ROLE_PRESSURE = {
    # light touch: the game ignores readings under 0.05, so a drawing hand sits just above it
    "construction": (0.065, 0.11),
    "contour": (0.07, 0.17),
    "inner": (0.065, 0.14),
    "detail": (0.062, 0.105),
    "accent": (0.18, 0.34),
    "gap_close": (0.07, 0.13),
    "shade": (0.062, 0.10),
    "fill": (0.30, 0.55),           # a broad sweep with a thick brush: the pressure is searched for the size
}
ROLE_TIME = {"construction": 0.8, "contour": 1.0, "inner": 0.95, "detail": 0.85,
             "accent": 1.2, "gap_close": 0.7, "shade": 1.05, "fill": 0.75}

_DEFAULTS = {
    "stroke_duration": {"a": 0.885, "b": 0.616, "sigma": 0.62},
    "pause": {"mu": -0.79, "sigma": 0.72},
    "pause_vs_travel": {"slope": 0.5, "intercept": 0.44},
    "power_law_beta": -0.416,
    "speed_profile": {"mean": [0.63, 0.82, 0.98, 1.08, 1.13, 1.15, 1.15, 1.13, 1.1,
                               1.07, 1.04, 1.0, 0.96, 0.92, 0.88, 0.84, 0.8, 0.75,
                               0.7, 0.63]},
}


def load_calibration(path=CALIBRATION):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    merged.update(data)
    return merged


# ---------------------------------------------------------------------------
# the game's pressure pipeline
# ---------------------------------------------------------------------------

def game_brush_size(pressures):
    """BrushSize the game stores for a stroke with these per-frame pressures.

    Port of LinePressure.Update/Finish with SmoothedValue(10).SmoothValue(5):
    the mean of the readings from the 5th to the 9th newest frame, so the last
    four frames before the pen lifts never count.
    """
    values = [0] * SMOOTHING
    index = consider = 0
    for p in pressures:
        if p > CUTOFF:
            values[index] = int(PEN_MIN + (PEN_MAX - PEN_MIN) * min(1.0, p))
            index = (index + 1) % SMOOTHING
            consider = min(consider + 1, SMOOTHING)
    if consider == 0:
        return None
    if DROP >= consider:
        return int(sum(values[:consider]) / consider)
    total = 0
    for i in range(consider - DROP):
        total += values[(index - DROP - i) % consider]
    return total // (consider - DROP)


def _smooth(values, window):
    """Moving average that keeps the length, whatever the input size."""
    values = np.asarray(values, float)
    half = min(window // 2, max(0, len(values) - 1))
    if half == 0:
        return values
    padded = np.pad(values, half, mode="edge")
    return np.convolve(padded, np.ones(2 * half + 1) / (2 * half + 1), mode="valid")


class Motor:
    """Draws strokes the way one person's hand would, reproducibly per seed."""

    def __init__(self, seed=0, calibration=None, skill=1.0, pressure_scale=1.0, fps=FPS):
        self.rng = random.Random(seed)
        self.np_rng = np.random.default_rng(seed)
        self.cal = calibration or load_calibration()
        self.skill = skill
        self.fps = float(fps)        # the game's frame rate: LinePressure reads once per frame
        self.pressure_scale = pressure_scale
        # the hand's habit for the whole drawing: how hard this person presses today
        self.habit = math.exp(self.rng.gauss(0.0, 0.12))
        self.drift = 0.0

    # --- timing --------------------------------------------------------

    def stroke_duration(self, length_rel, role="contour"):
        d = self.cal["stroke_duration"]
        base = d["a"] * max(length_rel, 0.01) ** d["b"]
        noise = math.exp(self.rng.gauss(0.0, d["sigma"] * 0.7))
        seconds = base * noise * ROLE_TIME.get(role, 1.0) / self.skill
        return float(min(3.6, max(0.16, seconds)))

    def pause(self, travel_rel, think=0.0):
        """Time between lifting the pen and touching down again."""
        c = self.cal
        base = c["pause_vs_travel"]["intercept"] + c["pause_vs_travel"]["slope"] * travel_rel
        noise = math.exp(self.rng.gauss(0.0, c["pause"]["sigma"] * 0.6))
        return float(max(0.08, (base * 0.7 * noise + think) / self.skill ** 0.9))

    def pause_continue(self):
        """Lift between two strokes of the same shape: the hand barely leaves the paper."""
        return float(max(0.06, math.exp(self.rng.gauss(math.log(0.16), 0.4)) / self.skill ** 0.9))

    def think(self, scale=1.0):
        """A longer stop to look at the drawing (switching part or colour)."""
        return scale * math.exp(self.rng.gauss(math.log(0.9), 0.5))

    # --- speed along the stroke ----------------------------------------

    def timeline(self, path, duration):
        """Per-point times for a path: bell-shaped speed, slower in tight bends."""
        path = np.asarray(path, float)
        n = len(path)
        if n < 2:
            return np.zeros(n)
        step = np.linalg.norm(np.diff(path, axis=0), axis=1)
        arc = np.concatenate(([0.0], np.cumsum(step)))
        total = max(arc[-1], 1e-9)
        u = arc / total
        profile = np.asarray(self.cal["speed_profile"]["mean"], float)
        grid = np.linspace(0.04, 0.96, len(profile))
        speed = np.interp(u, grid, profile)
        # two-thirds power law: v ~ curvature^beta
        heading = np.unwrap(np.arctan2(np.diff(path[:, 1]), np.diff(path[:, 0])))
        turn = np.abs(np.diff(heading, prepend=heading[0]))
        kappa = np.concatenate((turn, [turn[-1]])) / np.maximum(
            np.concatenate((step, [step[-1]])), 1e-9)
        kappa = _smooth(kappa, 5)
        reference = float(np.mean(kappa)) + 1e-9
        speed = speed * ((kappa / reference + 0.35) ** self.cal["power_law_beta"])
        speed = np.maximum(speed, 0.08)
        dt = np.concatenate(([0.0], step / ((speed[:-1] + speed[1:]) / 2.0)))
        t = np.cumsum(dt)
        return t * (duration / max(t[-1], 1e-9))

    # --- pressure -------------------------------------------------------

    def pressure_curve(self, times, role="contour", speeds=None, raw=False):
        """Pen pressure at every frame of a stroke lasting times[-1] seconds.

        With `raw` only the unit-scale shape is returned (see pressure_for_size)."""
        duration = float(times[-1]) if len(times) else 0.1
        frames = max(2, int(round(duration * self.fps)))
        t = np.linspace(0.0, duration, frames)
        lo, hi = ROLE_PRESSURE.get(role, ROLE_PRESSURE["contour"])
        self.drift = 0.85 * self.drift + self.rng.gauss(0.0, 0.07)
        base = self.rng.uniform(lo, hi) * self.habit * math.exp(self.drift)
        base = max(0.07, base * self.pressure_scale)
        attack = self.rng.uniform(0.04, 0.10)
        release = min(duration * 0.4, self.rng.uniform(0.07, 0.15))
        envelope = (1.0 - np.exp(-t / attack)) * 0.85 + 0.15
        tail = np.clip((duration - t) / max(release, 1e-3), 0.0, 1.0)
        envelope *= 0.35 + 0.65 * tail
        # slower segments press a bit harder, fast flicks go lighter
        if speeds is not None and len(speeds) > 2:
            v = np.interp(t, times, speeds)
            v = v / max(float(np.mean(v)), 1e-9)
            envelope *= 1.0 + 0.16 * np.clip(1.0 - v, -1.0, 1.0)
        noise = _smooth(self.np_rng.normal(0.0, 0.05, frames), 7)
        if raw:
            return envelope * (1.0 + noise * 2.0)
        curve = np.clip(base * envelope * (1.0 + noise * 2.0), 0.0, 1.0)
        peak = float(curve.max())
        if peak < 0.075:                 # the game ignores a pen that never presses past 0.05
            curve = curve * (0.075 / max(peak, 1e-6))
        # A stroke the game records starts where the pressure first passes 0.05 and
        # ends where it falls back. A hand that wants the line to reach its corners
        # keeps just above that from touch-down to lift, so the geometry survives.
        return np.maximum(curve, 0.062)

    def pressure_for_size(self, times, role="contour", speeds=None, target=6, spread=0.7):
        """Pressure curve that makes the game store about `target` (+/- a hand's jitter).

        The shape (soft touch-down, speed coupling, release, tremor) is drawn once
        and scaled; the scale is searched against the game's own smoothing, because
        the width the game keeps comes from the last frames, where the release is.
        Returns (curve, brush_size).
        """
        want = int(min(PEN_MAX, max(PEN_MIN, round(target + self.rng.gauss(0.0, spread)))))
        shape = self.pressure_curve(times, role, speeds, raw=True)
        best = None
        for base in np.linspace(0.0515, 2.5, 220):
            curve = np.clip(shape * base, PRESSURE_FLOOR, 1.0)
            size = game_brush_size(curve)
            if size is None:
                continue
            if best is None or abs(size - want) < abs(best[1] - want):
                best = (curve, size)
            if size >= want:
                break
        return best if best else (np.maximum(shape * 0.07, PRESSURE_FLOOR), PEN_MIN)

    def brush_size(self, times, role="contour", speeds=None, curve=None):
        if curve is None:
            curve = self.pressure_curve(times, role, speeds)
        size = game_brush_size(curve)
        return PEN_MIN if size is None else int(min(PEN_MAX, max(PEN_MIN, size)))


def sample_stroke(path_world, times, min_step=MIN_STEP_WORLD, with_times=False, fps=FPS):
    """Points the game would record: 60 Hz frames, only after `min_step` of movement.

    With `with_times` also returns the time (seconds) of every recorded point.
    """
    path_world = np.asarray(path_world, float)
    if len(path_world) < 2:
        return (path_world, np.zeros(len(path_world))) if with_times else path_world
    frames = np.arange(0.0, times[-1], 1.0 / fps)
    frames = np.append(frames, times[-1])
    x = np.interp(frames, times, path_world[:, 0])
    y = np.interp(frames, times, path_world[:, 1])
    keep = [0]
    for i in range(1, len(frames)):
        if math.hypot(x[i] - x[keep[-1]], y[i] - y[keep[-1]]) >= min_step:
            keep.append(i)
    if keep[-1] != len(frames) - 1:
        keep.append(len(frames) - 1)
    points = np.column_stack((x[keep], y[keep]))
    return (points, frames[keep]) if with_times else points
