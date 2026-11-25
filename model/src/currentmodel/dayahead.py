"""Tomorrow's current, built the evening before.

Everything a current model needs for the next day exists as a forecast by
the evening: the global ocean model (four runs a day, a week ahead), and the
wind from several weather models. :func:`day_ahead` reads them, runs the
venue physics model on them (minutes for a venue), and adds the part of the
surface water that follows the wind. The result is a
:class:`~currentmodel.live.LiveModel`: it answers for any place and time of
the day ahead, and on the day itself each reading on the water corrects it.

What the first tests showed (Hong Kong, seven days of readings; the README
has the numbers):

* Built from forecasts issued the evening before, the model scored the same
  against the readings as when built afterwards from the best estimates.
* For the wind a day ahead, the European centre's two models are each
  about a fifth closer to the measured wind than GFS, and a mean of models
  is badly wrong far less often than any one. Seven days of current
  readings could not tell the wind sources apart (GFS did as well as any),
  so the default here is the mean of the three that can be read directly.
* Between days the weights should move slowly. Letting a few readings pull
  them made the next day worse, so they start tight here; and what the
  water did today beyond the model does not carry over the night.
* On the day, the first reading is worth the most: predictions for the
  rest of the day improve from the second reading on.

Run it each evening::

    python -m currentmodel.dayahead --lat 22.28 --lon 114.36 --width-km 44 --height-km 38 \\
        --out tomorrow --cache cache --state live.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .build import SPINUP_S, build_suite
from .correction import Conditions
from .fields import GriddedField
from .live import LiveModel
from .models import WindDrift
from .physics import Grid
from .wind import forecast_wind

HOUR_S = 3600.0
WIND_SOURCES = ("ifs", "aifs-single", "gfs")
WIND_FRACTION = 0.02
WIND_AVERAGE_S = 6 * HOUR_S
# How unsure the starting weights are, as a share of each: small, so that days of readings move them, not one.
WEIGHT_SD_SHARE = 0.1


@dataclass
class DayAhead:
    """A prediction for the day ahead and what it was built from."""

    model: LiveModel      # the prediction; feed it readings on the day with ``observe``
    physics: GriddedField  # the venue physics model's run on the forecasts
    wind: Conditions      # the wind forecast, one series for the venue
    grid: Grid
    start: float
    end: float

    def velocity(self, lat, lon, time):
        return self.model.velocity(lat, lon, time)


def assemble(
    physics,
    wind: Conditions,
    lat: float,
    lon: float,
    wind_fraction: float = WIND_FRACTION,
    wind_average_s: float = WIND_AVERAGE_S,
    state: Mapping | None = None,
    **tuner_options,
) -> LiveModel:
    """The day-ahead model from a physics run and a wind forecast.

    ``state`` is yesterday's :meth:`~currentmodel.live.LiveTuner.state`: the
    weights learned so far carry over, and so do the latest readings, which
    fade within hours.
    """
    model = LiveModel(
        {"physics": physics, "wind": WindDrift(wind, lat, lon, 1.0, average_s=wind_average_s)},
        weights=[1.0, wind_fraction],
        weight_sd=[WEIGHT_SD_SHARE, WEIGHT_SD_SHARE * wind_fraction],
        **tuner_options,
    )
    if state:
        model.tuner.restore(state)
    return model


def day_ahead(
    lat: float,
    lon: float,
    width_m: float,
    height_m: float,
    start: float,
    end: float,
    cache_dir: str | Path | None = None,
    wind: Conditions | Iterable[str] = WIND_SOURCES,
    wind_fraction: float = WIND_FRACTION,
    state: Mapping | None = None,
    lead_in_s: float = 12 * HOUR_S,
    issued_before: float | None = None,
    log: Callable[[str], None] | None = None,
    **build_options,
) -> DayAhead:
    """The current over the box centred on (lat, lon) from ``start`` to ``end``, from forecasts.

    ``start`` and ``end`` (epoch seconds) bound the hours wanted, up to a few
    days ahead. ``wind`` names the weather models to blend
    (:func:`~currentmodel.wind.forecast_wind`) or is a wind record of your
    own; GFS stands in if none of the named ones can be read. The physics
    model starts ``lead_in_s`` before ``start``, after its spin-up, so that
    it has settled. ``state`` carries the tuning over from earlier days.
    ``issued_before`` builds from what was known at that time, for testing
    after the fact. Other options go to :func:`~currentmodel.build.build_suite`.
    """
    log = log or (lambda message: None)
    cache = Path(cache_dir) if cache_dir else None
    run_start = start - lead_in_s
    if not isinstance(wind, Conditions):
        sources = tuple(wind)
        log(f"reading the wind forecast ({', '.join(sources)})")
        first = run_start - SPINUP_S - WIND_AVERAGE_S
        try:
            wind = forecast_wind(lat, lon, first, end, sources, cache / "wind" if cache else None, issued_before, log)
        except FileNotFoundError:
            log("  none of them could be read; using GFS")
            wind = forecast_wind(lat, lon, first, end, ("gfs",), cache / "wind" if cache else None, issued_before, log)
    suite, grid = build_suite(
        lat, lon, width_m, height_m, run_start, end,
        cache_dir=cache, wind=wind, models=("venue-weather",), log=log, issued_before=issued_before, **build_options,
    )
    physics = suite["venue-weather"]
    return DayAhead(assemble(physics, wind, lat, lon, wind_fraction, state=state), physics, wind, grid, start, end)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Predict the current for the day ahead at a venue anywhere in the world.")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--width-km", type=float, required=True, help="east-west size of the venue box")
    parser.add_argument("--height-km", type=float, required=True, help="north-south size of the venue box")
    parser.add_argument("--start", default=None, help="first hour wanted (ISO, UTC); default is now")
    parser.add_argument("--hours", type=float, default=36.0)
    parser.add_argument("--dx", type=float, default=200.0, help="cell size of the venue physics model in metres")
    parser.add_argument("--wind", default=",".join(WIND_SOURCES), help="weather models to blend, comma separated")
    parser.add_argument("--state", default=None, help="file holding the tuning from earlier days (read if it exists, then written)")
    parser.add_argument("--out", required=True, help="folder for the results")
    parser.add_argument("--cache", default=None, help="folder to keep downloaded data in")
    args = parser.parse_args(argv)

    if args.start:
        stamp = dt.datetime.fromisoformat(args.start)
        start = (stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)).timestamp()
    else:
        start = _time.time() // HOUR_S * HOUR_S
    end = start + args.hours * HOUR_S
    state_file = Path(args.state) if args.state else None
    state = json.loads(state_file.read_text()) if state_file and state_file.exists() else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result = day_ahead(
        args.lat, args.lon, args.width_km * 1000.0, args.height_km * 1000.0, start, end,
        cache_dir=args.cache, wind=[name for name in args.wind.split(",") if name], state=state, dx=args.dx, log=print,
    )
    result.physics.save(out / "venue-weather.npz")
    (out / "wind.json").write_text(
        json.dumps({"times": result.wind.times.tolist(), "wind_u": result.wind.wind_u.tolist(), "wind_v": result.wind.wind_v.tolist()})
    )
    tuned = result.model.tuner.state()
    (out / "live.json").write_text(json.dumps(tuned))
    if state_file:
        state_file.write_text(json.dumps(tuned))
    weights = result.model.tuner.current_weights
    print(f"day ahead built for {dt.datetime.fromtimestamp(start, dt.timezone.utc):%Y-%m-%d %H:%M} UTC, {args.hours:.0f} h; weights {weights}")


if __name__ == "__main__":
    main()
