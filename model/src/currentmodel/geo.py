"""Coordinate and direction helpers.

Conventions used throughout the package:

* Velocities are (u, v) in metres per second, u towards east and v towards north.
* A "to" direction is the compass bearing the water flows towards, in degrees
  clockwise from true north. The app stores the opposite ("from") direction.
* Positions inside a venue are (x, y) metres east and north of the venue origin.
* Times are seconds since the Unix epoch, UTC.
"""

from __future__ import annotations

import numpy as np

EARTH_RADIUS_M = 6_371_000.0
KNOT_MS = 1852.0 / 3600.0


def to_xy(lat, lon, lat0: float, lon0: float):
    """Project latitude and longitude to local metres around (lat0, lon0).

    An equirectangular projection is accurate to well under a metre across a
    venue a few tens of kilometres wide, which is far below GPS error.
    """
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    x = np.radians(lon - lon0) * EARTH_RADIUS_M * np.cos(np.radians(lat0))
    y = np.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def to_latlon(x, y, lat0: float, lon0: float):
    """Inverse of :func:`to_xy`."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    lat = lat0 + np.degrees(y / EARTH_RADIUS_M)
    lon = lon0 + np.degrees(x / (EARTH_RADIUS_M * np.cos(np.radians(lat0))))
    return lat, lon


def uv_from_speed_dir(speed_ms, dir_to_deg):
    """Velocity components from a speed and the direction the flow goes towards."""
    theta = np.radians(np.asarray(dir_to_deg, dtype=float))
    speed_ms = np.asarray(speed_ms, dtype=float)
    return speed_ms * np.sin(theta), speed_ms * np.cos(theta)


def speed_dir_from_uv(u, v):
    """Speed and "to" direction (degrees, 0 to 360) from velocity components."""
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    return np.hypot(u, v), np.degrees(np.arctan2(u, v)) % 360.0


def angle_diff_deg(a, b):
    """Signed smallest difference a - b between two bearings, in (-180, 180]."""
    d = (np.asarray(a, dtype=float) - np.asarray(b, dtype=float) + 180.0) % 360.0 - 180.0
    return np.where(d == -180.0, 180.0, d)
