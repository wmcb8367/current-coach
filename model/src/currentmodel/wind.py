"""Wind for any venue, from NOAA's global weather model (GFS).

The drift correction learns how much of the surface current follows the
wind, which needs to know the wind at the time of every drift, including
drifts whose owner did not write it down. GFS covers the world on a quarter
degree grid and is kept on AWS Open Data, without a key, for years back:

    https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.fFFF

Each file is large, but an index beside it says where every field starts,
so only the two 10 metre wind fields are fetched (about a megabyte each).
Decoding them needs the ``eccodes`` package (``pip install currentmodel[data]``).

A quarter degree is about 25 km: this is the wind over open water near the
venue, not the bends and holes a shoreline puts in it. A reading taken on the
water always wins over it (see :class:`~currentmodel.correction.Conditions`
and the drift's own ``wind_u``, ``wind_v``).
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .correction import Conditions
from .stofs import _get

BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
CYCLE_S = 6 * 3600
FIELDS = ("UGRD", "VGRD")
LEVEL = "10 m above ground"


def _url(cycle: float, hour: int) -> str:
    stamp = dt.datetime.fromtimestamp(cycle, dt.timezone.utc)
    return f"{BUCKET}/gfs.{stamp:%Y%m%d}/{stamp:%H}/atmos/gfs.t{stamp:%H}z.pgrb2.0p25.f{hour:03d}"


def field_ranges(index_text: str, level: str = LEVEL, names: tuple[str, ...] = FIELDS) -> dict[str, tuple[int, int | None]]:
    """Byte range of each named field in a GRIB file, from the text of its ``.idx``.

    Index lines read ``number:start byte:date:name:level:forecast:``. A field
    ends where the next one starts; the end is ``None`` for the last field.
    """
    rows = [line.split(":") for line in index_text.splitlines() if line.strip()]
    starts = [int(row[1]) for row in rows]
    found = {}
    for i, row in enumerate(rows):
        if row[3] in names and row[4] == level and row[3] not in found:
            following = [s for s in starts[i + 1 :] if s > starts[i]]
            found[row[3]] = (starts[i], following[0] if following else None)
    missing = [n for n in names if n not in found]
    if missing:
        raise ValueError(f"no {', '.join(missing)} at {level} in the index")
    return found


def _value_at(message: bytes, lat: float, lon: float) -> float:
    """The field at a position: the four surrounding grid points, weighted by nearness."""
    import eccodes

    handle = eccodes.codes_new_from_message(message)
    try:
        near = eccodes.codes_grib_find_nearest(handle, lat, lon % 360.0, npoints=4)
    finally:
        eccodes.codes_release(handle)
    values = np.array([p.value for p in near], dtype=float)
    weights = 1.0 / np.maximum(np.array([p.distance for p in near], dtype=float), 1e-3)
    return float(np.sum(values * weights) / np.sum(weights))


def _wind_of(cycle: float, hour: int, lat: float, lon: float) -> tuple[float, float]:
    url = _url(cycle, hour)
    ranges = field_ranges(_get(url + ".idx").decode())
    out = []
    for name in FIELDS:
        start, end = ranges[name]
        out.append(_value_at(_get(url, start, end if end is not None else start + 4_000_000), lat, lon))
    return out[0], out[1]


def gfs_wind(
    lat: float,
    lon: float,
    start: float,
    end: float,
    step_hours: int = 3,
    cache_dir: str | Path | None = None,
) -> Conditions:
    """The 10 metre wind at (lat, lon) from ``start`` to ``end`` (epoch seconds), as :class:`Conditions`.

    Uses each six-hourly run's analysis and its first forecast hours, so the
    series is the model's best estimate at each time rather than one long
    forecast. Runs that are missing, or not out yet, are skipped and the
    series is interpolated across them. With ``cache_dir`` each value is
    kept, so a later call only fetches what is new.
    """
    if step_hours not in (1, 2, 3, 6):
        raise ValueError("step_hours must be 1, 2, 3 or 6")
    first = np.floor(start / CYCLE_S) * CYCLE_S
    last = np.ceil(end / CYCLE_S) * CYCLE_S
    wanted = [(cycle, hour) for cycle in np.arange(first, last + 1, CYCLE_S) for hour in range(0, 6, step_hours)]
    wanted = [(c, h) for c, h in wanted if c + h * 3600 <= last]

    cache_file = Path(cache_dir) / f"gfs_wind_{lat:.3f}_{lon:.3f}.npz" if cache_dir else None
    known: dict[float, tuple[float, float]] = {}
    if cache_file is not None and cache_file.exists():
        saved = np.load(cache_file)
        known = {float(t): (float(u), float(v)) for t, u, v in zip(saved["times"], saved["wind_u"], saved["wind_v"])}

    def fetch(item):
        cycle, hour = item
        try:
            return cycle + hour * 3600.0, _wind_of(cycle, hour, lat, lon)
        except FileNotFoundError:
            return cycle + hour * 3600.0, None

    todo = [item for item in wanted if item[0] + item[1] * 3600.0 not in known]
    with ThreadPoolExecutor(8) as pool:
        for time, value in pool.map(fetch, todo):
            if value is not None:
                known[time] = value
    if cache_file is not None and known:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        times = np.array(sorted(known))
        np.savez(cache_file, times=times, wind_u=[known[t][0] for t in times], wind_v=[known[t][1] for t in times])

    times = np.array(sorted(t for t in known if first <= t <= last))
    if times.size < 2:
        raise FileNotFoundError("no GFS wind found for this period")
    return Conditions(times, np.array([known[t][0] for t in times]), np.array([known[t][1] for t in times]))
