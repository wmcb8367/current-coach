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

For tomorrow's wind other models do better than GFS. Against 16 months of
measurements at Waglan Island, off Hong Kong, forecasts made a day ahead by
the European centre's two models (IFS and the machine-learned AIFS) and by
Germany's ICON were each about a fifth closer than GFS, and the mean of
several was closer than any one (see the README). :func:`ecmwf_wind` reads
IFS and AIFS from the centre's open data, :func:`open_meteo_wind`
reads any model Open-Meteo carries, and :func:`forecast_wind` returns their
mean.
"""

from __future__ import annotations

import datetime as dt
import json
import time as _time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

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


def _noaa_ranges(url: str, wanted: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[int, int | None]]:
    return wanted_ranges(_get(url + ".idx").decode(), wanted)


def _newest_cycle(url_of, read_ranges, wanted, last: float, step_hours: int) -> float | None:
    """Start of the newest run that is published far enough ahead to be used, or None."""
    cycle = np.floor(_time.time() / CYCLE_S) * CYCLE_S
    for _ in range(12):
        ahead = min(max((last - cycle) / 3600.0, 0.0), 120.0)
        hour = int(ahead // max(step_hours, 6) * max(step_hours, 6))
        try:
            read_ranges(url_of(float(cycle), hour), wanted)
            return float(cycle)
        except (FileNotFoundError, ValueError):
            cycle -= CYCLE_S
    return None


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
    read_ranges: Callable | None = None,
) -> tuple[np.ndarray, dict[tuple[str, str], np.ndarray]]:
    """Fields of a six-hourly weather model at one position, as time series.

    ``url_of(cycle, hour)`` gives the GRIB file of forecast hour ``hour`` of
    the run started at ``cycle`` (epoch seconds). ``read_ranges(url, wanted)``
    gives the byte range of each wanted field in it; the default reads the
    NOAA index at that URL plus ``.idx``. Each time is taken from the latest
    run that covers it, so the series is the model's best estimate, and
    times ahead of the newest published run come from that run's forecast.
    With ``issued_before`` only runs started at or before that time are used
    and later times come from the last such run's forecast: the series is
    then what was known at that moment, for testing forecasts (a run is
    published some hours after it starts, so take that delay off the
    time). Returns the times found and one array per wanted (name, level).
    """
    if step_hours not in (1, 2, 3, 6):
        raise ValueError("step_hours must be 1, 2, 3 or 6")
    first = np.floor(start / CYCLE_S) * CYCLE_S
    last = np.ceil(end / CYCLE_S) * CYCLE_S
    wanted_times = np.arange(first, last + 1, step_hours * 3600.0)
    read_ranges = read_ranges or _noaa_ranges
    final_cycle = None if issued_before is None else np.floor(issued_before / CYCLE_S) * CYCLE_S
    if final_cycle is None and last > _time.time() - 2 * CYCLE_S:
        # The period reaches the present: runs not yet published cannot serve their own hours.
        final_cycle = _newest_cycle(url_of, read_ranges, wanted, last, step_hours)
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
            ranges = read_ranges(url, wanted)
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


# ------------------------------------------------------------ other models

# The same files on two clouds; the second is tried when the first refuses.
ECMWF_MIRRORS = ("https://storage.googleapis.com/ecmwf-open-data", "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com")
ECMWF_STEP_H = {"ifs": 3, "aifs-single": 6}
ECMWF_FIELDS = (("10u", "sfc"), ("10v", "sfc"))
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_PAST_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
OPEN_METEO_PAID = "https://customer-api.open-meteo.com/v1/forecast"
BEST_DAY_AHEAD = ("ecmwf_ifs025", "ecmwf_aifs025_single", "icon_global")


def ecmwf_ranges(index_text: str, wanted: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[int, int | None]]:
    """Byte range of each (param, level type) pair in an ECMWF open-data file, from the text of its ``.index``."""
    found = {}
    for line in index_text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = (row.get("param"), row.get("levtype"))
        if key in wanted and key not in found and "number" not in row:
            found[key] = (int(row["_offset"]), int(row["_offset"]) + int(row["_length"]))
    missing = [f"{n} at {l}" for n, l in wanted if (n, l) not in found]
    if missing:
        raise ValueError(f"no {', '.join(missing)} in the index")
    return found


def _ecmwf_url(model: str, mirror: str = ECMWF_MIRRORS[0], stream: str = "oper") -> Callable[[float, int], str]:
    def url(cycle: float, hour: int) -> str:
        stamp = dt.datetime.fromtimestamp(cycle, dt.timezone.utc)
        return f"{mirror}/{stamp:%Y%m%d}/{stamp:%H}z/{model}/0p25/{stream}/{stamp:%Y%m%d%H}0000-{hour}h-{stream}-fc.grib2"

    return url


def _ecmwf_read_ranges(url: str, wanted: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[int, int | None]]:
    return ecmwf_ranges(_get(url[: -len("grib2")] + "index").decode(), wanted)


def ecmwf_wind(
    lat: float,
    lon: float,
    start: float,
    end: float,
    model: str = "ifs",
    cache_dir: str | Path | None = None,
    issued_before: float | None = None,
) -> Conditions:
    """The 10 metre wind at (lat, lon) from the European centre's open data, as :class:`Conditions`.

    ``model`` is ``"ifs"`` (the physical model, every 3 hours) or
    ``"aifs-single"`` (the machine-learned one, every 6 hours). Both run four
    times a day on a quarter degree grid and are kept on AWS without a key
    (https://registry.opendata.aws/ecmwf-forecasts), free to use with
    attribution (CC BY 4.0). A run is published about 8 hours (IFS) or 6
    hours (AIFS) after it starts. Otherwise as :func:`gfs_wind`.
    """
    if model not in ECMWF_STEP_H:
        raise ValueError(f"model must be one of {sorted(ECMWF_STEP_H)}")
    step = ECMWF_STEP_H[model]
    cache_file = Path(cache_dir) / f"ecmwf_{model}_wind_{lat:.3f}_{lon:.3f}_{step}h.npz" if cache_dir else None
    wanted = list(ECMWF_FIELDS)
    problem: Exception | None = None
    for mirror in ECMWF_MIRRORS:
        try:
            times, series = point_series(
                _ecmwf_url(model, mirror), wanted, lat, lon, start, end, step, cache_file,
                issued_before=issued_before, read_ranges=_ecmwf_read_ranges,
            )
            return Conditions(times, series[wanted[0]], series[wanted[1]])
        except (FileNotFoundError, OSError) as error:
            problem = error
    raise FileNotFoundError(f"no ECMWF {model} wind for this period ({problem})")


def open_meteo_wind(
    lat: float,
    lon: float,
    start: float,
    end: float,
    models: Iterable[str] = BEST_DAY_AHEAD,
    days_before: int = 0,
    api_key: str | None = None,
) -> dict[str, Conditions]:
    """The 10 metre wind at the nearest sea point from each named weather model, through Open-Meteo.

    One request returns hourly wind from any of the models Open-Meteo
    carries (https://open-meteo.com/en/docs: ``ecmwf_ifs025``,
    ``ecmwf_aifs025_single``, ``icon_global``, ``gfs_global``,
    ``ukmo_global_deterministic_10km`` and more), each from its latest run.
    ``days_before`` of 1 to 7 gives instead what each model forecast that
    many days earlier, from the service's archive of past runs: use it to
    test forecasts. Only the coordinates are sent. The free service is for
    non-commercial use; pass ``api_key`` to use a paid plan. Models with
    nothing for the period are left out.
    """
    models = list(models)
    if not 0 <= days_before <= 7:
        raise ValueError("days_before must be between 0 and 7")
    suffix = f"_previous_day{days_before}" if days_before else ""
    day = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")
    query = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": f"wind_speed_10m{suffix},wind_direction_10m{suffix}",
        "models": ",".join(models),
        "start_date": day(start),
        "end_date": day(end),
        "wind_speed_unit": "ms",
        "timezone": "GMT",
        "cell_selection": "sea",
    }
    base = OPEN_METEO_PAST_RUNS if days_before else OPEN_METEO
    if api_key:
        query["apikey"] = api_key
        base = OPEN_METEO_PAID if not days_before else base.replace("https://", "https://customer-")
    hourly = json.loads(_get(base + "?" + urllib.parse.urlencode(query)).decode()).get("hourly", {})
    return open_meteo_series(hourly, models, suffix)


def open_meteo_series(hourly: dict, models: list[str], suffix: str = "") -> dict[str, Conditions]:
    """:class:`Conditions` per model from the ``hourly`` block of an Open-Meteo answer."""
    stamps = hourly.get("time", [])
    times = np.array([dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() for s in stamps])
    out = {}
    for model in models:
        tag = f"_{model}" if len(models) > 1 else ""
        speed = hourly.get(f"wind_speed_10m{suffix}{tag}")
        direction = hourly.get(f"wind_direction_10m{suffix}{tag}")
        if speed is None or direction is None:
            continue
        speed = np.array([np.nan if x is None else x for x in speed], dtype=float)
        direction = np.radians(np.array([np.nan if x is None else x for x in direction], dtype=float))
        ok = np.isfinite(speed) & np.isfinite(direction)
        if ok.sum() < 2:
            continue
        # Direction is where the wind comes from; Conditions hold where it blows towards.
        out[model] = Conditions(times[ok], -speed[ok] * np.sin(direction[ok]), -speed[ok] * np.cos(direction[ok]))
    return out


def blend_wind(records: Iterable[Conditions], step_s: float = 3600.0) -> Conditions:
    """The mean of several wind records over the hours they all cover.

    Different models miss in different ways, so their mean is closer to the
    measured wind than any one of them.
    """
    records = list(records)
    if not records:
        raise ValueError("nothing to blend")
    first = max(r.times[0] for r in records)
    last = min(r.times[-1] for r in records)
    if last <= first:
        raise ValueError("the wind records do not overlap")
    times = np.arange(first, last + 1.0, step_s)
    parts = [r.wind(times) for r in records]
    return Conditions(times, np.mean([p[0] for p in parts], axis=0), np.mean([p[1] for p in parts], axis=0))


def forecast_wind(
    lat: float,
    lon: float,
    start: float,
    end: float,
    sources: Iterable[str] = ("ifs", "aifs-single", "gfs"),
    cache_dir: str | Path | None = None,
    issued_before: float | None = None,
    log: Callable[[str], None] | None = None,
) -> Conditions:
    """The wind to predict tomorrow's current with: the mean of several weather models.

    ``sources`` name what to blend: ``"ifs"`` and ``"aifs-single"``
    (:func:`ecmwf_wind`), ``"gfs"`` (:func:`gfs_wind`), or a model name for
    :func:`open_meteo_wind` such as ``"icon_global"``. A source that cannot
    be reached is left out, as long as one remains. ``issued_before`` is
    for testing and applies to the first three; the usual publication delay
    of each (8, 6 and 5 hours) is taken off it.
    """
    log = log or (lambda message: None)
    delay_s = {"ifs": 8 * 3600.0, "aifs-single": 6 * 3600.0, "gfs": 5 * 3600.0}
    found = []
    for source in sources:
        issued = None if issued_before is None else issued_before - delay_s.get(source, 0.0)
        try:
            if source in ECMWF_STEP_H:
                found.append(ecmwf_wind(lat, lon, start, end, source, cache_dir, issued))
            elif source == "gfs":
                found.append(gfs_wind(lat, lon, start, end, 3, cache_dir, issued))
            elif issued_before is not None:
                raise ValueError("issued_before is not supported through Open-Meteo; use open_meteo_wind(days_before=...)")
            else:
                found.extend(open_meteo_wind(lat, lon, start, end, [source]).values())
        except (FileNotFoundError, OSError, ValueError) as problem:
            log(f"  wind from {source} not available ({problem})")
    if not found:
        raise FileNotFoundError("no wind forecast could be read")
    return found[0] if len(found) == 1 else blend_wind(found)
