"""Venue packs: the corrected field on a grid, in a form the app can draw.

A pack is plain JSON. Speeds are in knots and directions are the bearing the
current flows towards, matching what the app shows. Positions outside the
baseline's coverage are null.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from .correction import Conditions, VenueCorrection
from .geo import KNOT_MS, speed_dir_from_uv

ATTRIBUTION = (
    "Tidal stream baseline: Hydrographic Office, Marine Department, HKSAR Government, via DATA.GOV.HK. "
    "Not to be used for navigation."
)


def build_pack(
    model: VenueCorrection,
    venue: str,
    lats,
    lons,
    times,
    conditions: Conditions | None = None,
    attribution: str = ATTRIBUTION,
) -> dict:
    """Predict on the grid ``lats`` by ``lons`` at each of ``times`` (epoch seconds UTC)."""
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    times = np.asarray(times, dtype=float)
    grid_lat, grid_lon = np.meshgrid(lats, lons, indexing="ij")
    frames = []
    for stamp in times:
        p = model.predict(grid_lat, grid_lon, stamp, conditions)
        speed, direction = speed_dir_from_uv(p.u, p.v)
        frames.append(
            {
                "time": datetime.fromtimestamp(float(stamp), tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "speedKnots": _rounded(speed / KNOT_MS, 2),
                "directionToDegrees": _rounded(direction, 0),
                "sigmaKnots": _rounded(p.sigma / KNOT_MS, 2),
            }
        )
    return {
        "venue": venue,
        "attribution": attribution,
        "lats": [round(float(v), 6) for v in lats],
        "lons": [round(float(v), 6) for v in lons],
        "order": "row per latitude, column per longitude",
        "frames": frames,
    }


def _rounded(values: np.ndarray, digits: int) -> list:
    return [[None if np.isnan(v) else round(float(v), digits) for v in row] for row in values]
