"""Time safety and pacing shared by every drawing style.

Two modes:
  slow     (default) strokes are spaced like a person drawing. The spacing is
           recomputed before every stroke from the time that is really left, so
           a late start or a slow bridge makes the hand faster instead of making
           the drawing run past the end of the round.
  instant  everything is sent back to back in batches, no pauses.

Safety rules that hold in both modes:
  * a reserve is kept back from the round (12 %, at least 3 s, at most 20 s);
  * request latency is measured and multiplied by a margin before it is trusted;
  * the live round timer is re-read while drawing and can only move the
    deadline earlier;
  * when even the fastest hand cannot finish, the tail of the plan (the least
    important strokes) is dropped, so the picture is complete but simpler.
"""
from __future__ import annotations

SPEED_FLOOR = 0.40        # slow mode never compresses its pauses below 40 %
LATENCY_MARGIN = 1.30     # trust measured request time only with 30 % extra
RECHECK_EVERY = 6.0       # seconds between re-reads of the live round timer
MAX_DELAY = 1.2           # longest pause slow mode inserts between batches


ROUND_RESERVE = 30.0      # seconds left for the player at the end of a round read from the timer


def reserve_seconds(remaining, fixed=False):
    """Time kept back for slow requests, planning drift and a late timer.

    `fixed`: the human styles on a round timer always leave ROUND_RESERVE seconds
    (less only in a very short round) for the player to adjust.
    """
    if fixed:
        return min(ROUND_RESERVE, 0.4 * float(remaining))
    return min(20.0, max(3.0, float(remaining) * 0.12))


def usable_seconds(remaining, fixed=False):
    return max(0.0, float(remaining) - reserve_seconds(remaining, fixed))


class LatencyTracker:
    """Smoothed request time relative to what the planner assumed."""

    def __init__(self, assumed):
        self.assumed = max(1e-6, float(assumed))
        self.value = self.assumed

    def add(self, seconds):
        self.value = 0.7 * self.value + 0.3 * max(0.0, float(seconds))

    def factor(self):
        """>= LATENCY_MARGIN, grows when the bridge is slower than assumed."""
        return LATENCY_MARGIN * max(1.0, self.value / self.assumed)


def pace_scale(spare, natural):
    """Share of the natural pauses that fit into the spare time (0..1)."""
    if natural <= 1e-9:
        return 1.0
    return max(0.0, min(1.0, spare / natural))


def tighten_deadline(client, args, deadline, now):
    """Re-read the live round timer; the deadline can only move earlier."""
    if getattr(args, 'dry_run', False) or getattr(args, 'time', None) is not None:
        return deadline
    try:
        state = client.round()
        remaining = float(((state or {}).get('recommended') or {}).get('remaining', 0))
    except Exception:
        return deadline
    if remaining <= 0:
        return deadline
    return min(deadline, now + usable_seconds(remaining, getattr(args, 'round_reserve', False)))
