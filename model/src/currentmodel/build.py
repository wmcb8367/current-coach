"""Build the standard set of current models for a venue anywhere in the world.

From a position and a box size this fetches open data and returns a
:class:`~currentmodel.models.ModelSuite` holding:

``global``
    NOAA's global ocean model, as published. Tide, wind and air pressure.
    Fine along United States coasts, coarse (2 to 4 km) elsewhere.

``venue-tide``
    Our physics model on the venue's own coastline and depths, driven at
    its open edges by the global model's tide. Tide only.

``venue-weather``
    The same physics model driven by the global model's full water level
    and current, which adds what wind and air pressure are doing to the sea
    and the drift along the coast. The wind's push on the water inside the
    venue is added from NOAA's weather model, and rivers if given.

Depths come from GEBCO where it can be reached (see
:mod:`currentmodel.depths`), otherwise from the global terrain tiles.
Electronic charts and the depth logs of a team's own boats, where there are
any, are laid over that (:func:`better_depths`). The open edges take both
the level and the current of the global model.

Official national predictions, where a country publishes them, are loaded
separately (see :mod:`currentmodel.baseline`) and added to the suite with
``suite.add``. Nothing here has seen a measurement: use
:meth:`~currentmodel.models.ModelSuite.score` with drifts to find out which
model is right for the venue, and ``fit`` to correct it.

Command line::

    python -m currentmodel.build --lat 22.27 --lon 114.17 --width-km 44 --height-km 34 --out hong-kong

With the boats' own soundings, reduced with the predicted tide at Tai Miu Wan::

    python -m currentmodel.build ... --depth-logs logs/*.sl2 --tide-station TMW --transducer 0.4
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time as _time
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from . import stofs as stofs_module
from .correction import Conditions
from .depths import gebco_depths
from .fields import GriddedField, edge_flow, simulate
from .geo import KNOT_MS
from .models import LUNAR_DAY_S, ModelSuite
from .physics import Grid
from .sonarlog import read_sonar_log
from .soundings import Soundings, depth_source_from_soundings, hk_predicted_tide, reduce_to_mean_sea_level
from .venue import build_grid
from .wind import forecast_wind, gfs_wind

SPINUP_S = 12 * 3600.0
MARGIN_M = 15_000.0


def better_depths(
    base: Callable | None,
    charts: Iterable[str | Path] = (),
    mean_sea_level_above_datum_m: float = 0.0,
    depth_logs: Iterable[str | Path] = (),
    level: Callable | None = None,
    transducer_m: float = 0.0,
    cell_m: float = 50.0,
    reach_m: float = 1000.0,
    log: Callable[[str], None] | None = None,
) -> Callable | None:
    """``base`` with charts laid over it, and the boats' own soundings over those.

    ``charts`` are electronic chart files, folders or zips
    (:mod:`currentmodel.charts`) and ``mean_sea_level_above_datum_m`` how far
    mean sea level is above their datum. ``depth_logs`` are files a sounder
    or plotter recorded (:mod:`currentmodel.sonarlog`). ``level(time)`` is
    the water level above mean sea level when they were recorded and
    ``transducer_m`` the depth of the transducer below the waterline; without
    ``level`` the logs are taken as depths below mean sea level already.
    """
    log = log or (lambda message: None)
    source = base
    charts = list(charts)
    if charts:
        from .charts import chart_depth_source, read_charts

        cells = read_charts(charts)
        log(f"depths from {len(cells)} chart cells: " + ", ".join(f"{c.name} 1:{c.scale:,}" for c in cells))
        source = chart_depth_source(cells, source, mean_sea_level_above_datum_m, cell_m=cell_m, reach_m=reach_m)
    logs = [read_sonar_log(path) for path in depth_logs]
    if logs:
        soundings = Soundings.join(logs)
        if level is not None:
            raw = len(soundings)
            soundings = reduce_to_mean_sea_level(soundings, level, transducer_m)
            if len(soundings) < raw:
                log(f"  {raw - len(soundings)} soundings left out: no time, or outside the tide series")
        log(f"depths from {len(soundings)} soundings in {len(logs)} logs")
        if len(soundings):
            source = depth_source_from_soundings(soundings, source, cell_m=cell_m, reach_m=reach_m)
    return source


def build_suite(
    lat: float,
    lon: float,
    width_m: float,
    height_m: float,
    start: float,
    end: float,
    dx: float = 200.0,
    cache_dir: str | Path | None = None,
    wind: Conditions | str | None = "gfs",
    rivers: Iterable[tuple[float, float, float]] = (),
    depth_source: Callable | str | None = "gebco",
    refine_depths: Callable[[Callable | None], Callable | None] | None = None,
    models: Iterable[str] = ("global", "venue-tide", "venue-weather"),
    log: Callable[[str], None] | None = None,
    edge_currents: bool = True,
    issued_before: float | None = None,
    **solver_options,
) -> tuple[ModelSuite, Grid]:
    """Build the models for the box centred on (lat, lon) from ``start`` to ``end`` (epoch seconds).

    The period can lie in the past (the archive goes back about a year) or
    run up to 7 days ahead. ``dx`` is the cell size of the venue physics
    model; halving it costs eight times the computing.

    ``depth_source`` is ``"gebco"`` (the default: GEBCO's grid, falling back
    to the terrain tiles if it cannot be reached), ``None`` for the tiles
    alone, or a function giving better depths as in
    :func:`~currentmodel.venue.build_grid`. ``refine_depths`` is handed
    whichever of those was used and returns a better one: the place for
    charts and the boats' own soundings (:func:`better_depths`). ``wind`` drives ``venue-weather``
    inside the venue: ``"gfs"`` fetches NOAA's weather model at the centre,
    ``"forecast"`` the mean of several weather models
    (:func:`~currentmodel.wind.forecast_wind`, steadier for the days ahead),
    ``None`` leaves it out, or pass a record of your own. ``rivers`` are
    (lat, lon, m3/s). With ``edge_currents`` the open edges take the global
    model's current as well as its level, which lets waves leave the venue
    and brings the coastal drift in.

    ``issued_before`` (epoch seconds) builds from what had been issued by
    then and nothing later: the way to test, after the fact, a prediction
    made the evening before.
    """
    log = log or (lambda message: None)
    models = tuple(models)
    cache = Path(cache_dir) if cache_dir else None
    source = stofs_module.Stofs(cache / "stofs" if cache else None)
    radius = 0.5 * float(np.hypot(width_m, height_m)) + MARGIN_M
    suite = ModelSuite()
    physics = [name for name in ("venue-tide", "venue-weather") if name in models]

    if depth_source == "gebco":
        reach_lat = 0.5 * height_m / 111_000.0 + 0.05
        reach_lon = 0.5 * width_m / (111_000.0 * np.cos(np.radians(lat))) + 0.05
        try:
            log("fetching depths from GEBCO")
            depth_source = gebco_depths(lat - reach_lat, lat + reach_lat, lon - reach_lon, lon + reach_lon, cache / "gebco" if cache else None)
        except Exception as problem:  # no network, or the service is down: the tiles still give a grid
            log(f"  GEBCO not available ({problem}); using the terrain tiles' depths")
            depth_source = None
    if refine_depths is not None:
        depth_source = refine_depths(depth_source)
    log("building the venue grid")
    grid = build_grid(lat, lon, width_m, height_m, dx=dx, depth_source=depth_source, cache_dir=cache / "tiles" if cache else None)

    global_currents = None
    if "global" in models or (edge_currents and physics):
        log("reading the global model's currents")
        global_currents = stofs_module.currents(
            source, lat, lon, radius, start - (SPINUP_S if edge_currents and physics else 0.0), end, issued_before=issued_before
        )
    if "global" in models:
        suite.add("global", global_currents, ["tide", "wind", "air pressure"], "NOAA global ocean model (STOFS-2D-Global) as published")
    if isinstance(wind, str):
        if wind not in ("gfs", "forecast"):
            raise ValueError('wind must be "gfs", "forecast", a record of the wind or None')
        kind, wind = wind, None
        if "venue-weather" in models:
            try:
                if kind == "gfs":
                    log("reading the wind from NOAA's weather model")
                    wind = gfs_wind(
                        lat, lon, start - SPINUP_S, end, step_hours=1, cache_dir=cache / "gfs" if cache else None, issued_before=issued_before
                    )
                else:
                    log("reading the wind from several weather models")
                    wind = forecast_wind(
                        lat, lon, start - SPINUP_S, end, cache_dir=cache / "wind" if cache else None, issued_before=issued_before, log=log
                    )
            except Exception as problem:
                log(f"  wind not available ({problem}); venue-weather runs without local wind")
    wind_function = (lambda t: wind.wind(t)) if wind is not None else None
    for name, tide_only in (("venue-tide", True), ("venue-weather", False)):
        if name not in models:
            continue
        log(f"reading the global model's {'tide' if tide_only else 'water level'} for the open edges")
        levels = stofs_module.levels(source, lat, lon, radius, start - SPINUP_S, end, tide_only=tide_only, issued_before=issued_before)
        log(f"running the venue physics model: {name}")
        began = _time.time()
        flow = None
        if edge_currents:
            # For the tide-only model the global current is stripped to its tide, to match the tide-only level.
            flow = edge_flow(global_currents, start - SPINUP_S, end, remove_mean_over_s=LUNAR_DAY_S if tide_only else None)
        field = simulate(
            grid,
            start,
            end,
            boundary=levels.at,
            boundary_flow=flow,
            wind=None if tide_only else wind_function,
            rivers=() if tide_only else rivers,
            spinup_s=SPINUP_S,
            **solver_options,
        )
        log(f"  done in {_time.time() - began:.0f} s")
        if tide_only:
            factors = ["tide", "coastline", "depths"]
            description = "venue physics model, tide only"
        else:
            factors = ["tide", "weather-driven sea level", "coastline", "depths"]
            factors += ["coastal drift"] if edge_currents else []
            factors += ["local wind"] if wind is not None else []
            factors += ["rivers"] if rivers else []
            description = "venue physics model, tide plus weather"
        suite.add(name, field, factors, description)
    return suite, grid


def summarise(suite: ModelSuite, grid: Grid, start: float, end: float) -> dict:
    """Speeds of each model over the wet cells of the grid, in knots."""
    lat, lon = grid.centres_latlon()
    lat, lon = lat[grid.wet][::7], lon[grid.wet][::7]
    times = np.arange(start, end, 3600.0)
    out = {}
    for entry in suite.entries:
        speeds = []
        for t in times:
            u, v = entry.model.velocity(lat, lon, np.full(lat.size, t))
            speeds.append(np.hypot(u, v))
        speeds = np.concatenate(speeds) / KNOT_MS
        covered = ~np.isnan(speeds)
        out[entry.name] = {
            "factors": list(entry.factors),
            "coverage_of_water": float(covered.mean()),
            "median_kn": float(np.nanmedian(speeds)) if covered.any() else None,
            "p90_kn": float(np.nanpercentile(speeds, 90)) if covered.any() else None,
            "p99_kn": float(np.nanpercentile(speeds, 99)) if covered.any() else None,
        }
    return out


def _parse_time(text: str) -> float:
    stamp = dt.datetime.fromisoformat(text)
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)).timestamp()


def _hk_tide(station: str, depth_logs: Iterable[str | Path]) -> Callable:
    """The predicted tide at a Hong Kong station for every year the logs cover."""
    times = Soundings.join(read_sonar_log(path) for path in depth_logs).time
    times = times[~np.isnan(times)]
    if times.size == 0:
        raise SystemExit("the depth logs carry no times, so the tide cannot be taken out of them")
    years = sorted({dt.datetime.fromtimestamp(t, dt.timezone.utc).year for t in (times.min() - 86400.0, times.max() + 86400.0)})
    tables = [hk_predicted_tide(year, station) for year in range(years[0], years[-1] + 1)]

    def level(at) -> np.ndarray:
        found = np.full(np.shape(at), np.nan)
        for table in tables:
            found = np.where(np.isnan(found), table(at), found)
        return found

    return level


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the current models for a venue anywhere in the world.")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--width-km", type=float, required=True, help="east-west size of the venue box")
    parser.add_argument("--height-km", type=float, required=True, help="north-south size of the venue box")
    parser.add_argument("--dx", type=float, default=200.0, help="cell size of the venue physics model in metres")
    parser.add_argument("--start", default=None, help="start time (ISO, UTC); default is the start of today")
    parser.add_argument("--hours", type=float, default=48.0)
    parser.add_argument("--out", required=True, help="folder for the results")
    parser.add_argument("--cache", default=None, help="folder to keep downloaded data in")
    parser.add_argument("--depths", choices=("gebco", "tiles"), default="gebco", help="where water depths come from")
    parser.add_argument("--charts", nargs="*", default=[], help="electronic chart cells (.000), folders or zips of them, laid over those depths")
    parser.add_argument("--chart-datum", type=float, default=0.0, help="how far mean sea level is above the charts' datum, in metres")
    parser.add_argument("--depth-logs", nargs="*", default=[], help="depth logs from the boats' own sounders (.sl2 .sl3 .gpx .csv .nmea)")
    parser.add_argument("--tide-station", default=None, help="Hong Kong Observatory tide station (TMW, QUB ...) to take the tide out of the logs with")
    parser.add_argument("--transducer", type=float, default=0.0, help="depth of the sounder's transducer below the waterline, in metres")
    parser.add_argument("--no-wind", action="store_true", help="leave the wind out of venue-weather")
    args = parser.parse_args(argv)

    if args.start:
        start = _parse_time(args.start)
    else:
        start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    end = start + args.hours * 3600.0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    say = lambda m: print(m, flush=True)
    refine = None
    if args.charts or args.depth_logs:
        level = None
        if args.depth_logs and args.tide_station:
            level = _hk_tide(args.tide_station, args.depth_logs)
        elif args.depth_logs:
            say("no --tide-station: taking the depth logs as depths below mean sea level already")
        refine = lambda base: better_depths(base, args.charts, args.chart_datum, args.depth_logs, level, args.transducer, cell_m=min(args.dx, 50.0), log=say)
    suite, grid = build_suite(
        args.lat,
        args.lon,
        args.width_km * 1e3,
        args.height_km * 1e3,
        start,
        end,
        dx=args.dx,
        cache_dir=args.cache,
        depth_source="gebco" if args.depths == "gebco" else None,
        refine_depths=refine,
        wind=None if args.no_wind else "gfs",
        log=say,
    )
    for entry in suite.entries:
        if isinstance(entry.model, GriddedField):
            entry.model.save(out / f"{entry.name}.npz")
        else:
            m = entry.model
            np.savez_compressed(out / f"{entry.name}.npz", lat=m.lat, lon=m.lon, times=m.times, u=m.u, v=m.v)
    summary = {
        "lat": args.lat,
        "lon": args.lon,
        "start": start,
        "end": end,
        "grid": {"rows": grid.shape[0], "columns": grid.shape[1], "dx": grid.dx, "water_fraction": float(grid.wet.mean())},
        "models": summarise(suite, grid, start, end),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary["models"], indent=2))


if __name__ == "__main__":
    main()
