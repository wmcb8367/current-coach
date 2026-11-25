"""Drift measurements as recorded by the Current Coach app.

The app stores one record per drift: the start fix, the start time, the
duration, the speed in metres per minute and the direction the current comes
*from*. This module converts those records into what the model needs: a
velocity vector placed at the middle of the drift, in space and in time, with
an error estimate.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .geo import EARTH_RADIUS_M, KNOT_MS, uv_from_speed_dir

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


# Apps whose records carry the fix and the time at which the drift ended.
STAMPED_AT_END = frozenset({"current-coach-android"})


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

    # The iOS app anchors the record at the start fix and start time, the
    # Android app at the end fix and end time. The drift samples the water
    # between the two, so move place and time half a drift toward the middle.
    lat0 = float(record["latitude"])
    lon0 = float(record["longitude"])
    half = 0.5 * duration * (-1.0 if record.get("source") in STAMPED_AT_END else 1.0)
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


# Column names seen in spreadsheets of readings, by what they hold.
_CSV_COLUMNS = {
    "id": ("id", "client_id", "clientid"),
    "time": ("time_utc", "utc", "measured_at_utc", "timestamp", "time", "time_hong_kong", "time_hkt", "time_local"),
    "speed_kn": ("speed_knots", "current_kt", "current_kn", "current_knots", "speed_kt", "speed_kn"),
    "from_deg": (
        "flow_from_degrees", "current_from_deg", "direction_from_deg", "direction_from_degrees",
        "from_true_deg", "from_direction_degrees", "from_deg",
    ),
    "to_deg": (
        "set_toward_degrees", "direction_toward_deg", "direction_toward_degrees", "toward_true_deg",
        "set_deg", "current_to_deg", "to_deg",
    ),
    "lat": ("latitude", "lat"),
    "lon": ("longitude", "lon", "lng"),
    "duration": ("duration_seconds", "duration_s"),
    "valid": ("is_valid", "valid"),
    "confidence": ("confidence_percent", "confidence"),
    "wind_from": ("wind_from_deg", "wind_from_degrees", "twd"),
    "wind_kn": ("wind_kt", "wind_kn", "wind_knots", "tws"),
}
_ZONES = {"UTC": 0.0, "GMT": 0.0, "Z": 0.0, "HKT": 8.0, "PST": -8.0, "PDT": -7.0, "EST": -5.0, "EDT": -4.0}


def _csv_time(text: str, date: str | None, utc_offset_hours: float | None) -> float:
    """Epoch seconds of a time written as ISO, as ``2026-11-15 12:18:00 HKT``, or as ``12:18`` on ``date``."""
    text = text.strip()
    parts = text.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].upper() in _ZONES:
        text, utc_offset_hours = parts[0], _ZONES[parts[1].upper()]
    if "-" not in text and "/" not in text:
        if date is None:
            raise ValueError(f"the time {text!r} has no date: pass date='YYYY-MM-DD'")
        text = f"{date} {text}"
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        if utc_offset_hours is None:
            raise ValueError(f"the time {text!r} has no time zone: pass utc_offset_hours")
        stamp = stamp.replace(tzinfo=timezone(timedelta(hours=utc_offset_hours)))
    return stamp.timestamp()


def load_readings_csv(
    path: str | Path,
    user: str = "unknown",
    date: str | None = None,
    utc_offset_hours: float | None = None,
    default_duration_s: float = 180.0,
) -> list[Drift]:
    """Load current readings from a spreadsheet saved as CSV.

    For readings kept outside the app, or exported from it as a table. The
    columns are found by name: a time, a speed in knots, the direction the
    current comes *from* or sets *towards*, latitude and longitude, and
    optionally duration, validity, confidence and the wind noted at the time
    (from direction and knots). Rows without a position or a current are
    skipped, as are rows marked invalid.

    ``date`` (``YYYY-MM-DD``) and ``utc_offset_hours`` are needed when the
    file gives only a time of day. With a duration column the position is
    taken as the start fix, as in the app, and the reading moved to the
    middle of the drift. Without one the position is used as it stands and
    ``default_duration_s`` sets the error estimate.
    """
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return []
    header = {name.strip().lower(): name for name in rows[0]}
    column = {key: next((header[n] for n in names if n in header), None) for key, names in _CSV_COLUMNS.items()}
    for needed in ("time", "speed_kn", "lat", "lon"):
        if column[needed] is None:
            raise ValueError(f"no {needed} column in {path}; expected one of {_CSV_COLUMNS[needed]}")
    if column["from_deg"] is None and column["to_deg"] is None:
        raise ValueError(f"no current direction column in {path}")

    def cell(row, key):
        text = (row.get(column[key]) or "").strip() if column[key] else ""
        return text or None

    drifts = []
    stem = Path(path).stem
    for number, row in enumerate(rows, start=2):
        speed, lat, lon = cell(row, "speed_kn"), cell(row, "lat"), cell(row, "lon")
        direction = cell(row, "from_deg") or cell(row, "to_deg")
        if None in (speed, lat, lon, direction) or (cell(row, "valid") or "true").lower() in ("false", "0", "no"):
            continue
        dir_to = (float(direction) + 180.0) % 360.0 if cell(row, "from_deg") else float(direction) % 360.0
        u, v = uv_from_speed_dir(float(speed) * KNOT_MS, dir_to)
        duration = float(cell(row, "duration") or 0.0)
        half = 0.5 * duration
        lat0, lon0 = float(lat), float(lon)
        wind_u = wind_v = None
        if cell(row, "wind_from") and cell(row, "wind_kn"):
            wu, wv = uv_from_speed_dir(float(cell(row, "wind_kn")) * KNOT_MS, (float(cell(row, "wind_from")) + 180.0) % 360.0)
            wind_u, wind_v = float(wu), float(wv)
        extra = {"row": number}
        if cell(row, "confidence"):
            extra["confidence"] = float(cell(row, "confidence"))
        drifts.append(
            Drift(
                id=cell(row, "id") or f"{stem}:{number}",
                user_id=user,
                time=_csv_time(cell(row, "time"), date, utc_offset_hours) + half,
                lat=float(lat0 + np.degrees(float(v) * half / EARTH_RADIUS_M)),
                lon=float(lon0 + np.degrees(float(u) * half / (EARTH_RADIUS_M * np.cos(np.radians(lat0))))),
                u=float(u),
                v=float(v),
                sigma=drift_sigma(duration or default_duration_s),
                duration_s=duration or default_duration_s,
                wind_u=wind_u,
                wind_v=wind_v,
                extra=extra,
            )
        )
    return drifts


def drop_repeats(drifts: Sequence[Drift], within_s: float = 5.0, within_m: float = 30.0) -> list[Drift]:
    """The drifts with repeats of the same reading removed, in time order.

    The same reading often arrives twice, in two exports or two pulls, with
    its numbers rounded differently. Two drifts are the same reading when
    their times, positions and velocities all agree closely; the first one
    met is kept.
    """
    kept: list[Drift] = []
    for d in sorted(drifts, key=lambda d: d.time):
        repeat = False
        for other in reversed(kept):
            if d.time - other.time > within_s:
                break
            apart = EARTH_RADIUS_M * np.hypot(
                np.radians(d.lat - other.lat), np.radians(d.lon - other.lon) * np.cos(np.radians(d.lat))
            )
            if apart <= within_m and np.hypot(d.u - other.u, d.v - other.v) <= 0.01:
                repeat = True
                break
        if not repeat:
            kept.append(d)
    return kept


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
