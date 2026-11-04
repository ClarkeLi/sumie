"""Natural edges where the picture is cut off by the frame.

A figure that is cut off at the edge of the source image ends there in a straight
line. Copied faithfully, the drawing would end in a straight line too, which nobody
who draws freely does. This module finds those cuts and lets the figure fade out
instead:

  * `soften` replaces the straight edge by a curve that bows into the figure and is
    a little irregular, and clips the subject mask to it;
  * `fray` ends the lines that cross the curve at scattered places and lets some of
    them trail off in short, thinner pieces ("lost lines");
  * `FrameCut.band` marks the transition zone, where the drawing must not put ink or a
    dark border: colour ends there against an invisible, wavy wall.

Everything is deterministic for a seed. Without a cut nothing changes.
"""
from __future__ import annotations

import dataclasses
import math

import cv2
import numpy as np

MERGE_GAP = 0.06         # gaps shorter than this share of the side are bridged
MIN_SPAN = 0.03          # a cut shorter than this share of the side is not a cut
BAND_PX = 2              # how close to the edge subject pixels count as touching it
BOW = 0.05               # how far the curve bows into the figure (share of its size)
BAND = 0.045             # width of the transition zone (share of the figure's size)


@dataclasses.dataclass
class FrameCut:
    spans: list                       # [(side, start, end)] along the side, in pixels
    scale: float                      # long side of the subject's bounding box, px
    band: np.ndarray | None = None    # bool mask: transition zone inside the figure
    curve: np.ndarray | None = None   # bool mask: the new edge of the figure

    @property
    def active(self):
        return bool(self.spans)


def _subject_scale(subject):
    ys, xs = np.nonzero(subject)
    if len(xs) == 0:
        return 1.0
    return float(max(xs.max() - xs.min(), ys.max() - ys.min(), 1))


def detect(subject):
    """Find the stretches of the frame edge where the subject touches it."""
    h, w = subject.shape[:2]
    on = subject > 0
    sides = {
        'top': on[:BAND_PX + 1, :].any(axis=0),
        'bottom': on[-(BAND_PX + 1):, :].any(axis=0),
        'left': on[:, :BAND_PX + 1].any(axis=1),
        'right': on[:, -(BAND_PX + 1):].any(axis=1),
    }
    spans = []
    for side, line in sides.items():
        length = len(line)
        runs, i = [], 0
        while i < length:
            if line[i]:
                j = i
                while j + 1 < length and line[j + 1]:
                    j += 1
                runs.append([i, j])
                i = j + 1
            else:
                i += 1
        merged = []                                   # a short gap does not end a cut
        for run in runs:
            if merged and run[0] - merged[-1][1] <= MERGE_GAP * length:
                merged[-1][1] = run[1]
            else:
                merged.append(run)
        spans += [(side, a, b) for a, b in merged if (b - a + 1) >= MIN_SPAN * length]
    return FrameCut(spans=spans, scale=_subject_scale(subject))


def _edge_point(side, along, depth, h, w):
    """Point `depth` pixels inside the frame, at position `along` on the given side."""
    if side == 'bottom':
        return along, (h - 1) - depth
    if side == 'top':
        return along, depth
    if side == 'left':
        return depth, along
    return (w - 1) - depth, along


def soften(subject, cut, seed=0):
    """Clip the subject to curves that replace the straight cut. Returns the new mask."""
    if not cut.active:
        return subject
    h, w = subject.shape[:2]
    rng = np.random.default_rng(seed)
    depth_max = BOW * cut.scale
    removed = np.zeros((h, w), np.uint8)
    for side, a, b in cut.spans:
        length = max(b - a, 1)
        steps = max(24, int(length / 4))
        t = np.linspace(0.0, 1.0, steps)
        bow = np.sin(math.pi * t) ** 0.7 * depth_max * rng.uniform(0.75, 1.15)
        wobble = np.zeros(steps)
        for freq, amp in ((rng.uniform(1.0, 2.0), rng.uniform(0.18, 0.32)),
                          (rng.uniform(2.5, 4.0), rng.uniform(0.12, 0.22)),
                          (rng.uniform(5.0, 8.0), rng.uniform(0.05, 0.11)),
                          (rng.uniform(9.0, 14.0), rng.uniform(0.02, 0.05))):   # slow waves down to a tremor
            wobble += amp * depth_max * np.sin(2 * math.pi * freq * t + rng.uniform(0, 2 * math.pi))
        depth = np.clip(bow + wobble * np.sin(math.pi * t), 0.0, None)
        curve = [_edge_point(side, a + (b - a) * tt, d, h, w) for tt, d in zip(t, depth)]
        edge = [_edge_point(side, a + (b - a) * tt, -1.0, h, w) for tt in t[::-1]]
        polygon = np.round(np.array(curve + edge)).astype(np.int32).reshape(-1, 1, 2)
        cv2.fillPoly(removed, [polygon], 1)
    clipped = subject.copy()
    clipped[removed > 0] = 0
    # one connected body: keep the largest piece and any piece that is not a sliver
    count, labels, stats, _ = cv2.connectedComponentsWithStats((clipped > 0).astype(np.uint8), 8)
    if count > 1:
        biggest = int(stats[1:, cv2.CC_STAT_AREA].max())
        for i in range(1, count):
            if stats[i, cv2.CC_STAT_AREA] < 0.02 * biggest:
                clipped[labels == i] = 0
    inside = clipped > 0
    outline = cv2.morphologyEx(inside.astype(np.uint8), cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
    near_removed = cv2.dilate(removed, np.ones((5, 5), np.uint8)) > 0
    cut.curve = outline & near_removed
    distance = cv2.distanceTransform((~cut.curve).astype(np.uint8), cv2.DIST_L2, 3)
    cut.band = inside & (distance < BAND * cut.scale)
    return clipped


def in_band(cut, points):
    """Boolean per (x, y) point: is it inside the transition zone?"""
    if cut is None or cut.band is None:
        return np.zeros(len(points), bool)
    h, w = cut.band.shape
    idx = np.clip(np.round(np.asarray(points, float)).astype(int), 0, [w - 1, h - 1])
    return cut.band[idx[:, 1], idx[:, 0]]


def fray(strokes, cut, rng):
    """End the lines that run into the transition zone at scattered places; let some trail off.

    `strokes` is [(path, tier, importance)] with paths in image pixels (x, y).
    """
    if cut is None or not cut.active or cut.band is None:
        return strokes
    out = []
    band_px = BAND * cut.scale
    for path, tier, importance in strokes:
        path = np.asarray(path, float)
        flags = in_band(cut, path)
        if not flags.any():
            out.append((path, tier, importance))
            continue
        first = int(np.argmax(flags))                    # where the line enters the zone
        inside = int(flags.sum())
        cut_at = first + int(rng.uniform(0.05, 0.95) * inside)
        kept = path[:max(cut_at, 0)]
        if len(kept) >= 3:
            out.append((kept, tier, importance))
        if len(kept) >= 3 and rng.random() < 0.45:       # a lost line: short pieces with gaps
            direction = kept[-1] - kept[max(0, len(kept) - 4)]
            norm = float(np.hypot(*direction))
            if norm > 1e-6:
                direction /= norm
                start = kept[-1].copy()
                length = band_px * rng.uniform(0.45, 0.8)
                for _ in range(rng.randint(1, 3)):
                    gap = band_px * rng.uniform(0.12, 0.25)
                    a = start + direction * gap
                    b = a + direction * length
                    steps = max(3, int(length / 2.0))
                    piece = np.linspace(a, b, steps)
                    if in_band(cut, piece).mean() < 0.6:
                        break
                    out.append((piece, 2, importance * 0.3))
                    start = b
                    length *= rng.uniform(0.45, 0.7)
    return out
