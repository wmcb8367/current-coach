"""Waves for any venue, from NOAA's global wave model (GFS-Wave).

Waves carry the surface water along with them (Stokes drift), on top of
what the wind does directly. The two differ most where the fetch differs:
an onshore wind arrives with a sea that has built over hundreds of miles,
an offshore wind of the same strength with almost none. The wave model
knows the fetch; the wind alone does not.

Files are on AWS Open Data beside the weather model's, without a key:

    https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.YYYYMMDD/HH/wave/gridded/gfswave.tHHz.global.0p16.fFFF.grib2

on a sixth of a degree (about 18 km), so this is the sea state off the
venue, not behind its headlands. Needs ``eccodes``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .geo import uv_from_speed_dir
from .wind import BUCKET, point_series

G = 9.81
SURFACE = "surface"
SWELL = "1 in sequence"
WANTED = [
    ("HTSGW", SURFACE), ("PERPW", SURFACE), ("DIRPW", SURFACE),
    ("WVHGT", SURFACE), ("WVPER", SURFACE), ("WVDIR", SURFACE),
    ("SWELL", SWELL), ("SWPER", SWELL), ("SWDIR", SWELL),
]


def _url(cycle: float, hour: int) -> str:
    stamp = dt.datetime.fromtimestamp(cycle, dt.timezone.utc)
    return f"{BUCKET}/gfs.{stamp:%Y%m%d}/{stamp:%H}/wave/gridded/gfswave.t{stamp:%H}z.global.0p16.f{hour:03d}.grib2"


def stokes_speed(height_m, period_s):
    """Surface Stokes drift in m/s of deep-water waves of a given significant height and period.

    For one wave train of height H and period T it is 2 pi^3 H^2 / (g T^3);
    a significant height stands for a train of height Hs / sqrt(2). A real
    sea has shorter waves riding on the main ones that add to it, so treat
    this as the right shape with a strength to be fitted.
    """
    height_m, period_s = np.asarray(height_m, dtype=float), np.asarray(period_s, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        speed = np.pi**3 * height_m**2 / (G * period_s**3)
    return np.where(np.isfinite(speed) & (period_s > 0), speed, 0.0)


@dataclass
class Waves:
    """Sea state over time at one position. Directions are where the waves come *from*."""

    times: np.ndarray
    height: np.ndarray        # significant height of the whole sea, m
    period: np.ndarray        # peak period, s
    direction: np.ndarray
    sea_height: np.ndarray    # the part the local wind is raising
    sea_period: np.ndarray
    sea_direction: np.ndarray
    swell_height: np.ndarray  # the largest swell arriving from elsewhere
    swell_period: np.ndarray
    swell_direction: np.ndarray

    def _drift(self, height, period, direction):
        speed = stokes_speed(np.nan_to_num(height), np.where(np.isnan(period), 1.0, period))
        return uv_from_speed_dir(speed, (np.nan_to_num(direction) + 180.0) % 360.0)

    def stokes(self, time, part: str = "both"):
        """Surface Stokes drift (u, v) in m/s at each time: ``"sea"``, ``"swell"`` or ``"both"``."""
        time = np.asarray(time, dtype=float)
        u = np.zeros_like(self.times)
        v = np.zeros_like(self.times)
        if part in ("sea", "both"):
            du, dv = self._drift(self.sea_height, self.sea_period, self.sea_direction)
            u, v = u + du, v + dv
        if part in ("swell", "both"):
            du, dv = self._drift(self.swell_height, self.swell_period, self.swell_direction)
            u, v = u + du, v + dv
        inside = (time >= self.times[0]) & (time <= self.times[-1])
        return (
            np.where(inside, np.interp(time, self.times, u), np.nan),
            np.where(inside, np.interp(time, self.times, v), np.nan),
        )


def gfs_waves(
    lat: float,
    lon: float,
    start: float,
    end: float,
    step_hours: int = 3,
    cache_dir: str | Path | None = None,
    issued_before: float | None = None,
) -> Waves:
    """The sea state at (lat, lon) from ``start`` to ``end`` (epoch seconds).

    The position should be in open water: a point the wave model counts as
    land has no value. ``issued_before`` works as in :func:`~currentmodel.wind.gfs_wind`.
    """
    cache_file = Path(cache_dir) / f"gfs_waves_{lat:.3f}_{lon:.3f}_{step_hours}h.npz" if cache_dir else None
    circular = ("DIRPW", "WVDIR", "SWDIR")
    times, s = point_series(_url, WANTED, lat, lon, start, end, step_hours, cache_file, circular, issued_before)
    return Waves(times, *(s[key] for key in WANTED))
