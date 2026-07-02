"""Server-side port of the client's global hex grid (utils/hexgrid.ts).

Pointy-top hexes in axial coordinates (q, r) over the Web-Mercator plane,
anchored to absolute Mercator metres so every lng/lat maps to the same tile
key ("q,r") the app stores in explored_tiles. Only the pieces the server
needs are ported: point -> tile, and the disk of keys around a tile.
"""

from __future__ import annotations

import math

MERC_R = 6378137.0  # Web-Mercator earth radius (metres)

# Hex circumradius in Mercator metres. MUST match HEX_RADIUS_MERCATOR in the
# app's utils/hexgrid.ts or tile keys stop lining up with explored_tiles rows.
HEX_RADIUS_MERCATOR = 300.0


def _to_merc(lng: float, lat: float) -> tuple[float, float]:
    clamped = max(-85.05112878, min(85.05112878, lat))
    x = MERC_R * math.radians(lng)
    y = MERC_R * math.log(math.tan(math.pi / 4 + math.radians(clamped) / 2))
    return x, y


def _js_round(v: float) -> int:
    # JS Math.round rounds .5 up; Python round() rounds .5 to even. Match JS so
    # boundary points land in the same tile the client would put them in.
    return math.floor(v + 0.5)


def _cube_round(x: float, y: float, z: float) -> tuple[int, int]:
    rx, ry, rz = _js_round(x), _js_round(y), _js_round(z)
    dx, dy, dz = abs(rx - x), abs(ry - y), abs(rz - z)
    if dx > dy and dx > dz:
        rx = -ry - rz
    elif dy > dz:
        ry = -rx - rz
    else:
        rz = -rx - ry
    return rx, rz


def tile_at(lng: float, lat: float) -> tuple[int, int]:
    """Axial (q, r) of the tile containing a lng/lat point."""
    x, y = _to_merc(lng, lat)
    size = HEX_RADIUS_MERCATOR
    q = ((math.sqrt(3) / 3) * x - (1 / 3) * y) / size
    r = ((2 / 3) * y) / size
    return _cube_round(q, -q - r, r)


def disk_keys(lng: float, lat: float, radius: int) -> list[str]:
    """Tile keys ("q,r") within `radius` rings of the tile containing the point."""
    cq, cr = tile_at(lng, lat)
    keys: list[str] = []
    for dq in range(-radius, radius + 1):
        for dr in range(max(-radius, -dq - radius), min(radius, -dq + radius) + 1):
            keys.append(f"{cq + dq},{cr + dr}")
    return keys
