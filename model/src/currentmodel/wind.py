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
    found = wanted_ranges(index_text, [(name, level) for name in names])
    return {name: found[(name, level)] for name in names}


def wanted_ranges(index_text: str, wanted: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[int, int | None]]:
    """Byte range of each (name, level) pair in a GRIB file, from the text of its ``.idx``."""
    rows = [line.split(":") for line in index_text.splitlines() if line.strip()]
    starts = [int(row[1]) for row in rows]
    found = {}
    for i, row in enumerate(rows):
        key = (row[3], row[4])
        if key in wanted and key not in found:
            following = [s for s in starts[i + 1 :] if s > starts[i]]
            found[key] = (starts[i], following[0] if following else None)
    missing = [f"{n} at {l}" for n, l in wanted if (n, l) not in found]
    if missing:
        raise ValueError(f"no {', '.join(missing)} in the index")
    return found


def _value_at(message: bytes, lat: float, lon: float, circular: bool = False) -> float:
    """The field at a position: the surrounding grid points that hold a value, weighted by nearness.

    Points on land carry no value in sea-only products and are left out.
    ``circular`` averages a direction in degrees the right way round.
    """
    import eccodes

    handle = eccodes.codes_new_from_message(message)
    try:
        missing = eccodes.codes_get(handle, "missingValue")
        near = eccodes.codes_grib_find_nearest(handle, lat, lon % 360.0, npoints=4)
    finally:
        eccodes.codes_release(handle)
    values = np.array([p.value for p in near], dtype=float)
    weights = 1.0 / np.maximum(np.array([p.distance for p in near], dtype=float), 1e-3)
    ok = np.isfinite(values) & (values != missing) & (np.abs(values) < 9e3)
    if not ok.any():
        return float("nan")
    values, weights = values[ok], weights[ok]
    if circular:
        angle = np.radians(values)
        return float(np.degrees(np.arctan2(np.sum(weights * np.sin(angle)), np.sum(weights * np.cos(angle)))) % 360.0)
    return float(np.sum(values * weights) / np.sum(weights))


def point_series(
    url_of,
    wanted: list[tuple[str, str]],
    lat: float,
    lon: float,
    start: float,
    end: float,
    step_hours: int = 3,
    cache_file: Path | None = None,
    circular: tuple[str, ...] = (),
    issued_before: float | None = None,
) -> tuple[np.ndarray, dict[tuple[str, str], np.ndarray]]:
    """Fields of a six-hourly NOAA model at one position, as time series.

    ``url_of(cycle, hour)`` gives the GRIB file of forecast hour ``hour`` of
    the run started at ``cycle`` (epoch seconds); its index is at that URL
    plus ``.idx``. Each time is taken from the latest run that covers it, so
    the series is the model's best estimate. With ``issued_before`` only
    runs started at or before that time are used and later times come from
    the last such run's forecast: the series is then what was known at that
    moment, for testing forecasts. Returns the times found and one array per
    wanted (name, level).
    """
    if step_hours not in (1, 2, 3, 6):
        raise ValueError("step_hours must be 1, 2, 3 or 6")
    first = np.floor(start / CYCLE_S) * CYCLE_S
    last = np.ceil(end / CYCLE_S) * CYCLE_S
    wanted_times = np.arange(first, last + 1, step_hours * 3600.0)
    final_cycle = None if issued_before is None else np.floor(issued_before / CYCLE_S) * CYCLE_S
    jobs = []
    for time in wanted_times:
        cycle = np.floor(time / CYCLE_S) * CYCLE_S
        if final_cycle is not None:
            cycle = min(cycle, final_cycle)
        jobs.append((float(time), float(cycle), int(round((time - cycle) / 3600.0))))

    names = [f"{n}|{l}" for n, l in wanted]
    known: dict[tuple[float, float], list[float]] = {}
    if cache_file is not None and cache_file.exists():
        saved = np.load(cache_file, allow_pickle=False)
        if list(saved["names"]) == names:
            known = {(float(t), float(c)): list(row) for t, c, row in zip(saved["times"], saved["cycles"], saved["values"])}

    def fetch(job):
        time, cycle, hour = job
        url = url_of(cycle, hour)
        try:
            ranges = wanted_ranges(_get(url + ".idx").decode(), wanted)
            row = []
            for key in wanted:
                a, b = ranges[key]
                row.append(_value_at(_get(url, a, b if b is not None else a + 4_000_000), lat, lon, key[0] in circular))
            return (time, cycle), row
        except (FileNotFoundError, ValueError):
            return (time, cycle), None

    todo = [job for job in jobs if (job[0], job[1]) not in known]
    with ThreadPoolExecutor(8) as pool:
        for key, row in pool.map(fetch, todo):
            if row is not None:
                known[key] = row
    if cache_file is not None and known:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        keys = sorted(known)
        np.savez(cache_file, names=names, times=[k[0] for k in keys], cycles=[k[1] for k in keys], values=[known[k] for k in keys])

    have = [(t, c) for t, c, _ in jobs if (t, c) in known]
    if len(have) < 2:
        raise FileNotFoundError("nothing found for this period")
    times = np.array([t for t, _ in have])
    values = np.array([known[k] for k in have], dtype=float)
    return times, {key: values[:, i] for i, key in enumerate(wanted)}


def gfs_wind(
    lat: float,
    lon: float,
    start: float,
    end: float,
    step_hours: int = 3,
    cache_dir: str | Path | None = None,
    issued_before: float | None = None,
) -> Conditions:
    """The 10 metre wind at (lat, lon) from ``start`` to ``end`` (epoch seconds), as :class:`Conditions`.

    Uses each six-hourly run's analysis and its first forecast hours, so the
    series is the model's best estimate at each time rather than one long
    forecast, and continues with the latest run's forecast (up to 16 days)
    beyond it. Runs that are missing are skipped and the series is
    interpolated across them. ``issued_before`` (epoch seconds) limits it to
    what had been issued by then, to test a forecast after the fact. With
    ``cache_dir`` each value is kept, so a later call only fetches what is new.
    """
    cache_file = Path(cache_dir) / f"gfs_wind_{lat:.3f}_{lon:.3f}_{step_hours}h.npz" if cache_dir else None
    wanted = [(name, LEVEL) for name in FIELDS]
    times, series = point_series(_url, wanted, lat, lon, start, end, step_hours, cache_file, issued_before=issued_before)
    return Conditions(times, series[wanted[0]], series[wanted[1]])
