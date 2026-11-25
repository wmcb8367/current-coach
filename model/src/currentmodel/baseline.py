"""Layer 1: the tidal baseline.

A baseline is any current prediction given at fixed points and regular times,
such as the Hong Kong Hydrographic Office tidal stream predictions (about
1,200 points every 15 minutes). :class:`TidalStreamField` interpolates it to
any position and time so it can be compared with drifts and corrected.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from .geo import KNOT_MS, to_xy, uv_from_speed_dir
from .measurements import _parse_time


@dataclass
class TidalStreamField:
    """Current predictions at scattered points and common times.

    Parameters
    ----------
    lat, lon:
        Point positions, shape (P,).
    times:
        Increasing prediction times in epoch seconds UTC, shape (T,).
    u, v:
        Velocity components in m/s, shape (T, P).
    neighbours:
        Number of nearest points blended at each query position.
    max_distance_m:
        Queries farther than this from every point are outside coverage and
        return NaN.
    """

    lat: np.ndarray
    lon: np.ndarray
    times: np.ndarray
    u: np.ndarray
    v: np.ndarray
    neighbours: int = 4
    max_distance_m: float = 2_000.0

    def __post_init__(self) -> None:
        self.lat = np.asarray(self.lat, dtype=float)
        self.lon = np.asarray(self.lon, dtype=float)
        self.times = np.asarray(self.times, dtype=float)
        self.u = np.asarray(self.u, dtype=float)
        self.v = np.asarray(self.v, dtype=float)
        points, steps = self.lat.size, self.times.size
        if self.u.shape != (steps, points) or self.v.shape != (steps, points):
            raise ValueError(f"u and v must have shape ({steps}, {points})")
        if steps < 2 or np.any(np.diff(self.times) <= 0):
            raise ValueError("times must be increasing and hold at least two steps")
        self.lat0 = float(self.lat.mean())
        self.lon0 = float(self.lon.mean())
        x, y = to_xy(self.lat, self.lon, self.lat0, self.lon0)
        self._tree = cKDTree(np.column_stack([x, y]))
        self._k = min(self.neighbours, points)

    # ------------------------------------------------------------------ queries

    def _space_weights(self, lat, lon):
        x, y = to_xy(lat, lon, self.lat0, self.lon0)
        dist, idx = self._tree.query(np.column_stack([x, y]), k=self._k)
        if self._k == 1:
            dist, idx = dist[:, None], idx[:, None]
        # Inverse-distance-squared weights. The 25 m floor stops a query that
        # lands on a point from ignoring its neighbours entirely.
        weights = 1.0 / np.maximum(dist, 25.0) ** 2
        weights /= weights.sum(axis=1, keepdims=True)
        covered = dist[:, 0] <= self.max_distance_m
        return idx, weights, covered

    def velocity(self, lat, lon, time):
        """Baseline (u, v) in m/s at each query position and time.

        The three arguments broadcast against each other. Positions outside
        coverage and times outside the prediction window return NaN.
        """
        lat, lon, time = np.broadcast_arrays(
            np.asarray(lat, dtype=float), np.asarray(lon, dtype=float), np.asarray(time, dtype=float)
        )
        shape = lat.shape
        lat, lon, time = lat.ravel(), lon.ravel(), time.ravel()

        idx, weights, covered = self._space_weights(lat, lon)
        step = np.clip(np.searchsorted(self.times, time, side="right") - 1, 0, self.times.size - 2)
        t0, t1 = self.times[step], self.times[step + 1]
        frac = (time - t0) / (t1 - t0)
        inside = covered & (time >= self.times[0]) & (time <= self.times[-1])

        rows0, rows1 = step[:, None], step[:, None] + 1
        out = []
        for field in (self.u, self.v):
            at0 = (field[rows0, idx] * weights).sum(axis=1)
            at1 = (field[rows1, idx] * weights).sum(axis=1)
            value = (1.0 - frac) * at0 + frac * at1
            out.append(np.where(inside, value, np.nan).reshape(shape))
        return out[0], out[1]

    def rate(self, lat, lon, time, half_window_s: float = 900.0):
        """Time derivative of the baseline velocity, in m/s per second.

        Used by the correction model to represent a baseline that turns early
        or late: b(t + tau) is approximately b(t) + tau * db/dt.
        """
        time = np.asarray(time, dtype=float)
        lo = np.maximum(time - half_window_s, self.times[0])
        hi = np.minimum(time + half_window_s, self.times[-1])
        u0, v0 = self.velocity(lat, lon, lo)
        u1, v1 = self.velocity(lat, lon, hi)
        span = np.where(hi > lo, hi - lo, np.nan)
        return (u1 - u0) / span, (v1 - v0) / span

    # ------------------------------------------------------------------ loading

    @classmethod
    def from_long_csv(cls, path: str | Path, **kwargs) -> "TidalStreamField":
        """Load predictions from a tidy CSV, one row per point and time.

        Required columns: ``lat``, ``lon``, ``time`` (ISO 8601 or epoch
        seconds, UTC), ``speed_kn`` and ``dir_to_deg`` (direction the stream
        sets towards, degrees true). Every point must have the same times.

        This is the package's own interchange format. Source-specific
        importers, such as the one for the Hong Kong Hydrographic Office
        download, should convert into it.
        """
        points: dict[tuple[float, float], int] = {}
        times: dict[float, int] = {}
        rows = []
        with open(path, newline="") as handle:
            for row in csv.DictReader(handle):
                key = (round(float(row["lat"]), 6), round(float(row["lon"]), 6))
                stamp = _parse_time(row["time"]) if not _is_number(row["time"]) else float(row["time"])
                p = points.setdefault(key, len(points))
                t = times.setdefault(stamp, len(times))
                rows.append((t, p, float(row["speed_kn"]), float(row["dir_to_deg"])))
        order = np.argsort(list(times))
        rank = np.empty(len(times), dtype=int)
        rank[order] = np.arange(len(times))
        u = np.full((len(times), len(points)), np.nan)
        v = np.full_like(u, np.nan)
        for t, p, speed_kn, dir_to in rows:
            uu, vv = uv_from_speed_dir(speed_kn * KNOT_MS, dir_to)
            u[rank[t], p], v[rank[t], p] = uu, vv
        if np.isnan(u).any():
            raise ValueError("every point must have a value at every time")
        latlon = np.array(list(points))
        return cls(latlon[:, 0], latlon[:, 1], np.sort(list(times)), u, v, **kwargs)


HK_TIDAL_STREAM_URL = "https://current.hydro.gov.hk/data/all_points_static/All_Points_{first}_{last}_{mode}.zip"


def hk_tidal_stream_url(year: int, month: int, surface: bool = True) -> str:
    """Where the Hong Kong Hydrographic Office keeps one month of predictions for all points.

    ``surface`` picks the surface layer flow; otherwise the depth average.
    Months from about a year back to a year ahead are published.
    """
    first = dt.date(year, month, 1)
    last = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return HK_TIDAL_STREAM_URL.format(first=first, last=last, mode="Surface" if surface else "Average")


def load_hk_tidal_streams(*paths: str | Path, **kwargs) -> TidalStreamField:
    """Load Hong Kong Hydrographic Office tidal stream predictions.

    Each path is a monthly "all points" download (the ``.zip`` as served, or
    the ``.csv`` inside it); give several consecutive months to cover a
    longer period. Rows are one point and one day, with a speed in knots and
    the direction the stream sets towards for every quarter hour, in Hong
    Kong time. Tide only: no wind, rain or river flow.

    Source: Hydrographic Office, Marine Department, HKSAR Government.
    """
    hong_kong = dt.timezone(dt.timedelta(hours=8))
    by_point: dict[tuple[float, float], dict[float, tuple[np.ndarray, np.ndarray]]] = {}
    for path in paths:
        path = Path(path)
        if path.suffix.lower() == ".zip":
            archive = zipfile.ZipFile(path)
            handle = io.TextIOWrapper(archive.open(archive.namelist()[0]), encoding="utf-8-sig", newline="")
        else:
            handle = open(path, newline="", encoding="utf-8-sig")
        with handle:
            reader = csv.reader(handle)
            header = next(reader)
            slots = [i for i, name in enumerate(header) if name.endswith("-KNOT")]
            minutes = np.array([int(header[i][:2]) * 60 + int(header[i][2:4]) for i in slots])
            for row in reader:
                day = dt.datetime.strptime(row[3], "%Y-%m-%d").replace(tzinfo=hong_kong).timestamp()
                speed = np.array([float(row[i] or "nan") for i in slots]) * KNOT_MS
                toward = np.array([float(row[i + 1] or "nan") for i in slots])
                key = (float(row[0]), float(row[1]))
                by_point.setdefault(key, {})[day] = uv_from_speed_dir(speed, toward) + (minutes,)
    if not by_point:
        raise ValueError("no predictions in the files given")
    days = sorted(set.intersection(*(set(v) for v in by_point.values())))
    whole = [k for k, v in by_point.items() if all(d in v for d in days)]
    step = minutes * 60.0
    times = np.concatenate([d + step for d in days])
    u = np.column_stack([np.concatenate([by_point[k][d][0] for d in days]) for k in whole])
    v = np.column_stack([np.concatenate([by_point[k][d][1] for d in days]) for k in whole])
    ok = ~(np.isnan(u).any(axis=0) | np.isnan(v).any(axis=0))
    latlon = np.array(whole)[ok]
    return TidalStreamField(latlon[:, 0], latlon[:, 1], times, u[:, ok], v[:, ok], **kwargs)


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True
