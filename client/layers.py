"""Virtual layers: what the plugin offers and how the renderer uses them.

The game has three real sorting layers. The plugin makes more of them out of
(real layer, band of the synchronised sort order); see `VirtualLayers.cs`.
A higher virtual layer is always drawn above a lower one, whatever the order
of drawing. Fills of a real layer lie under every line of that real layer.
"""
from __future__ import annotations

REAL_LAYERS = 3

ROLES = ('sketch', 'flat', 'flat_top', 'shade', 'light', 'ink', 'top')    # bottom to top
DEFAULT_BANDS = 3

# role -> virtual layer for a plugin with n virtual layers (n >= 5). Roles may share a
# layer when there are few; inside one layer the order of drawing decides.
_TABLE = {
    5: (0, 1, 1, 2, 2, 3, 4),
    6: (0, 1, 2, 3, 3, 4, 5),
    7: (0, 1, 2, 3, 4, 5, 6),
    8: (0, 1, 2, 3, 4, 6, 7),
}


def assign(total):
    """Virtual layer of every role of the brush plan; None when there are too few layers."""
    total = int(total)
    if total < 5:
        return None
    row = _TABLE.get(total, (0, 1, 2, 3, 4, 6, 7))
    return dict(zip(ROLES, row))


def resolve(vlayer, bands=DEFAULT_BANDS):
    """(real layer, band) of a virtual layer."""
    real, band = divmod(int(vlayer), int(bands))
    return min(real, REAL_LAYERS - 1), band


def render_key(op, bands=DEFAULT_BANDS):
    """Sort key of an op the way the game draws: real layer, fills under lines, band."""
    if getattr(op, 'vlayer', None) is not None:
        real, band = resolve(op.vlayer, bands)
    else:
        real, band = int(getattr(op, 'layer', 0) or 0), 0
    return real, 0 if op.kind in ('fill', 'bucket', 'realfill') else 1, band


def probe(client):
    """(capabilities or None, reachable): reachable tells an old plugin from no game at all."""
    try:
        info = client.layers()
    except Exception as error:
        return None, getattr(error, 'status', None) is not None      # an HTTP answer = a game is there
    if not isinstance(info, dict) or int(info.get('virtual', 0)) < 5:
        return None, True
    return info, True


def capabilities(client):
    """What the running plugin can do. None = no virtual layers (old plugin, no game)."""
    return probe(client)[0]


def usable(info):
    """Does the plugin report enough layers (and the native pen patch) for the brush plan?"""
    return bool(info) and assign(info.get('virtual', 0)) is not None and bool(info.get('nativePatch', True))
