"""Drift measurements as recorded by the Current Coach app.

The app stores one record per drift: the start fix, the start time, the
duration, the speed in metres per minute and the direction the current comes
*from*. This module converts those records into what the model needs: a
velocity vector placed at the middle of the drift, in space and in time, with
an error estimate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .geo import EARTH_RADIUS_M, uv_from_speed_dir

DEFAULT_GPS_SIGMA_M = 5.0
MIN_SIGMA_MS = 0.02


@dataclass(frozen=True)
class Drift:
    """One drift measurement, normalised for modelling."""

    id: str
    user_id: str
    time: float          # mid-time of the drift, epoch seconds UTC
    lat: float           # mid-point of the drift
    lon: float
    u: float             # m/s towards east
    v: float             # m/s towards north
    sigma: float         # 1-sigma error of each velocity component, m/s
    duration_s: float
    wind_u: float | None = None   # m/s, direction the wind blows towards
    wind_v: float | None = None
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def speed(self) -> float:
        return float(np.hypot(self.u, self.v))


def drift_sigma(duration_s: float, gps_sigma_m: float = DEFAULT_GPS_SIGMA_M) -> float:
    """Error of a drift velocity component from GPS error at both ends.

    The displacement is the difference of two fixes, so its error is
    sqrt(2) times the error of one fix, and the velocity error is that divided
    by the duration. A floor keeps long drifts from claiming perfect accuracy:
    windage and the drifter's own behaviour never average out.
    """
    if duration_s <= 0:
        return float("inf")
    return max(MIN_SIGMA_MS, float(np.sqrt(2.0) * gps_sigma_m / duration_s))


def _parse_time(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def drift_from_app_record(record: dict, default_user: str = "unknown") -> Drift | None:
    """Build a :class:`Drift` from one item of the app's sync payload.

    Accepts the field names the iOS app uploads (``timestamp``,
    ``durationSeconds``, ``speedMetersPerMinute``, ``fromDirectionDegrees``,
    ``latitude``, ``longitude``, ``isValid``) plus the optional fields this
    package proposes (``userId``, ``gpsAccuracyMeters``, ``windSpeedMs``,
    ``windFromDegrees``). Returns ``None`` for drifts the app marked invalid.
    """
    if record.get("isValid") is False:
        return None
    duration = float(record["durationSeconds"])
    if duration <= 0:
        return None
    speed = float(record["speedMetersPerMinute"]) / 60.0
    dir_to = (float(record["fromDirectionDegrees"]) + 180.0) % 360.0
    u, v = uv_from_speed_dir(speed, dir_to)

    # The app anchors the record at the start fix and start time. The drift
    # samples the water between start and end, so move both to the middle.
    lat0 = float(record["latitude"])
    lon0 = float(record["longitude"])
    half = 0.5 * duration
    lat = lat0 + np.degrees(float(v) * half / EARTH_RADIUS_M)
    lon = lon0 + np.degrees(float(u) * half / (EARTH_RADIUS_M * np.cos(np.radians(lat0))))
    time = _parse_time(record["timestamp"]) + half

    gps_sigma = float(record.get("gpsAccuracyMeters") or DEFAULT_GPS_SIGMA_M)
    wind_u = wind_v = None
    if record.get("windSpeedMs") is not None and record.get("windFromDegrees") is not None:
        wind_to = (float(record["windFromDegrees"]) + 180.0) % 360.0
        wu, wv = uv_from_speed_dir(float(record["windSpeedMs"]), wind_to)
        wind_u, wind_v = float(wu), float(wv)

    return Drift(
        id=str(record.get("clientId") or record.get("id") or ""),
        user_id=str(record.get("userId") or default_user),
        time=time,
        lat=float(lat),
        lon=float(lon),
        u=float(u),
        v=float(v),
        sigma=drift_sigma(duration, gps_sigma),
        duration_s=duration,
        wind_u=wind_u,
        wind_v=wind_v,
        extra={k: record[k] for k in ("confidence", "source") if k in record},
    )


def load_drifts(path: str | Path, default_user: str = "unknown") -> list[Drift]:
    """Load drifts from a JSON file: a list of app records, or ``{"measurements": [...]}``."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data.get("measurements", [])
    return drifts_from_records(data, default_user)


def drifts_from_records(records: Iterable[dict], default_user: str = "unknown") -> list[Drift]:
    drifts = (drift_from_app_record(r, default_user) for r in records)
    return [d for d in drifts if d is not None]


def as_arrays(drifts: Sequence[Drift]) -> dict[str, np.ndarray]:
    """Column arrays for a list of drifts."""
    return {
        "time": np.array([d.time for d in drifts], dtype=float),
        "lat": np.array([d.lat for d in drifts], dtype=float),
        "lon": np.array([d.lon for d in drifts], dtype=float),
        "u": np.array([d.u for d in drifts], dtype=float),
        "v": np.array([d.v for d in drifts], dtype=float),
        "sigma": np.array([d.sigma for d in drifts], dtype=float),
        "user": np.array([d.user_id for d in drifts], dtype=object),
    }
