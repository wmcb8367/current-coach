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
:mod:`currentmodel.depths`), otherwise from the global terrain tiles. The
open edges take both the level and the current of the global model.

Official national predictions, where a country publishes them, are loaded
separately (see :mod:`currentmodel.baseline`) and added to the suite with
``suite.add``. Nothing here has seen a measurement: use
:meth:`~currentmodel.models.ModelSuite.score` with drifts to find out which
model is right for the venue, and ``fit`` to correct it.

Command line::

    python -m currentmodel.build --lat 22.27 --lon 114.17 --width-km 44 --height-km 34 --out hong-kong
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
from .venue import build_grid
from .wind import gfs_wind

SPINUP_S = 12 * 3600.0
MARGIN_M = 15_000.0


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
    models: Iterable[str] = ("global", "venue-tide", "venue-weather"),
    log: Callable[[str], None] | None = None,
    edge_currents: bool = True,
    **solver_options,
) -> tuple[ModelSuite, Grid]:
    """Build the models for the box centred on (lat, lon) from ``start`` to ``end`` (epoch seconds).

    The period can lie in the past (the archive goes back about a year) or
    run up to 7 days ahead. ``dx`` is the cell size of the venue physics
    model; halving it costs eight times the computing.

    ``depth_source`` is ``"gebco"`` (the default: GEBCO's grid, falling back
    to the terrain tiles if it cannot be reached), ``None`` for the tiles
    alone, or a function giving better depths as in
    :func:`~currentmodel.venue.build_grid`. ``wind`` drives ``venue-weather``
    inside the venue: ``"gfs"`` fetches NOAA's weather model at the centre,
    ``None`` leaves it out, or pass a record of your own. ``rivers`` are
    (lat, lon, m3/s). With ``edge_currents`` the open edges take the global
    model's current as well as its level, which lets waves leave the venue
    and brings the coastal drift in.
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
    log("building the venue grid")
    grid = build_grid(lat, lon, width_m, height_m, dx=dx, depth_source=depth_source, cache_dir=cache / "tiles" if cache else None)

    global_currents = None
    if "global" in models or (edge_currents and physics):
        log("reading the global model's currents")
        global_currents = stofs_module.currents(source, lat, lon, radius, start - (SPINUP_S if edge_currents and physics else 0.0), end)
    if "global" in models:
        suite.add("global", global_currents, ["tide", "wind", "air pressure"], "NOAA global ocean model (STOFS-2D-Global) as published")
    if wind == "gfs":
        wind = None
        if "venue-weather" in models:
            try:
                log("reading the wind from NOAA's weather model")
                wind = gfs_wind(lat, lon, start - SPINUP_S, end, step_hours=1, cache_dir=cache / "gfs" if cache else None)
            except Exception as problem:
                log(f"  wind not available ({problem}); venue-weather runs without local wind")
    wind_function = (lambda t: wind.wind(t)) if wind is not None else None
    for name, tide_only in (("venue-tide", True), ("venue-weather", False)):
        if name not in models:
            continue
        log(f"reading the global model's {'tide' if tide_only else 'water level'} for the open edges")
        levels = stofs_module.levels(source, lat, lon, radius, start - SPINUP_S, end, tide_only=tide_only)
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
    parser.add_argument("--no-wind", action="store_true", help="leave the wind out of venue-weather")
    args = parser.parse_args(argv)

    if args.start:
        start = _parse_time(args.start)
    else:
        start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    end = start + args.hours * 3600.0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
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
        wind=None if args.no_wind else "gfs",
        log=lambda m: print(m, flush=True),
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
