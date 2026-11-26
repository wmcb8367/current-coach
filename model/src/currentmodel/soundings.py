"""Water depths from soundings: a boat's own depth sounder, a chart, an open archive.

GEBCO's grid (:mod:`currentmodel.depths`) is the only depth data that is
free everywhere, and inshore most of it is estimated from satellite gravity
rather than sounded. Soundings are the cure, and they all arrive the same
way: a position, a depth and usually a time. This module holds them
(:class:`Soundings`), takes the tide out of them
(:func:`reduce_to_mean_sea_level`) and lays them over a coarser depth
source (:func:`depth_source_from_soundings`) so the result plugs into
:func:`~currentmodel.venue.build_grid` like any other ``depth_source``.

Where they come from:

* a boat's own sounder, logged by its chart plotter
  (:mod:`currentmodel.sonarlog`). Free, and it covers exactly the water the
  boat sails on;
* an electronic chart (:mod:`currentmodel.charts`), free in some countries;
* the IHO's crowdsourced bathymetry archive, hosted by NOAA
  (https://noaa-dcdb-bathymetry-pds.s3.amazonaws.com/docs/readme.html):
  public domain soundings from ordinary vessels, as CSV files that
  :func:`~currentmodel.sonarlog.read_csv` reads. It publishes data inside a
  country's waters only where that country agreed, so it is rich on United
  States coasts and empty in Hong Kong.

How the overlay works. Soundings are sorted into cells and each cell keeps
its median, which throws out the odd false echo. What matters is the
*difference* between that median and the coarse source at the same spot:
the difference is spread into the gaps between tracks, from near to far,
kept in full out to half of ``reach_m`` from the nearest sounding and faded
to nothing at ``reach_m``. So the result follows the soundings where there
are some, keeps the shape they found a little beyond them, and is the
coarse source unchanged everywhere else.

Depths here are metres below mean sea level, positive down, which is what
the physics model wants. A sounder measures from its transducer to the
bottom at whatever the tide was doing, so raw logs must be reduced first.
"""

from __future__ import annotations

import datetime as dt
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

import numpy as np
from scipy import ndimage
from scipy.interpolate import RegularGridInterpolator

from .geo import to_latlon, to_xy

HKO_TIDES = "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
HONG_KONG_TIME = dt.timezone(dt.timedelta(hours=8))
MOST_CELLS = 6_000_000


@dataclass
class Soundings:
    """Depths at positions. ``time`` is NaN where a sounding has none."""

    lat: np.ndarray
    lon: np.ndarray
    depth: np.ndarray  # metres, positive down
    time: np.ndarray | None = None  # seconds since the Unix epoch, UTC

    def __post_init__(self) -> None:
        self.lat = np.asarray(self.lat, dtype=float).ravel()
        self.lon = np.asarray(self.lon, dtype=float).ravel()
        self.depth = np.asarray(self.depth, dtype=float).ravel()
        time = np.full(self.lat.shape, np.nan) if self.time is None else np.asarray(self.time, dtype=float).ravel()
        self.time = np.broadcast_to(time, self.lat.shape).copy() if time.size == 1 else time
        if not (self.lat.size == self.lon.size == self.depth.size == self.time.size):
            raise ValueError("positions, depths and times must be the same length")

    def __len__(self) -> int:
        return self.lat.size

    def keep(self, which: np.ndarray) -> "Soundings":
        return Soundings(self.lat[which], self.lon[which], self.depth[which], self.time[which])

    def within(self, south: float, north: float, west: float, east: float) -> "Soundings":
        return self.keep((self.lat >= south) & (self.lat <= north) & (self.lon >= west) & (self.lon <= east))

    @classmethod
    def join(cls, parts: Iterable["Soundings"]) -> "Soundings":
        parts = list(parts)
        if not parts:
            return cls(np.empty(0), np.empty(0), np.empty(0), np.empty(0))
        return cls(*(np.concatenate([getattr(p, name) for p in parts]) for name in ("lat", "lon", "depth", "time")))


def tide_level(times: Sequence[float], heights: Sequence[float], mean_m: float | None = None) -> Callable:
    """The water level above mean sea level at any time, from a tide gauge or a tide table.

    ``heights`` are in the gauge's own datum (chart datum, usually), and
    ``mean_m`` is mean sea level in that datum. Left out, it is taken as the
    mean of the series, which is right to a few centimetres for a table
    covering a year and wrong for a few days of readings. Times outside the
    series give NaN.
    """
    times, heights = np.asarray(times, dtype=float), np.asarray(heights, dtype=float)
    good = ~np.isnan(heights)
    times, heights = times[good], heights[good]
    order = np.argsort(times)
    times, heights = times[order], heights[order]
    mean = float(heights.mean()) if mean_m is None else float(mean_m)

    def level(at) -> np.ndarray:
        return np.interp(np.asarray(at, dtype=float), times, heights - mean, left=np.nan, right=np.nan)

    return level


def reduce_to_mean_sea_level(soundings: Soundings, level: Callable, transducer_m: float = 0.0) -> Soundings:
    """Sounder readings turned into depths below mean sea level.

    ``level(time)`` is the water level above mean sea level when each
    sounding was taken (see :func:`tide_level`) and ``transducer_m`` how far
    the transducer sits below the waterline. Soundings without a time, or
    from a time the tide series does not cover, are dropped: compare the
    lengths to see how many.
    """
    tide = np.asarray(level(soundings.time), dtype=float)
    known = ~np.isnan(tide)
    kept = soundings.keep(known)
    kept.depth = kept.depth + transducer_m - tide[known]
    return kept


def read_hk_predicted_tide(text: str, year: int) -> tuple[np.ndarray, np.ndarray]:
    """Times and heights (metres above chart datum) from a Hong Kong Observatory hourly tide table.

    One line per day: month, day, then the height at 01:00, 02:00 ... 24:00
    Hong Kong time.
    """
    times, heights = [], []
    for line in text.lstrip("﻿").splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        midnight = dt.datetime(year, int(cells[0]), int(cells[1]), tzinfo=HONG_KONG_TIME).timestamp()
        for hour, cell in enumerate(cells[2:26], start=1):
            try:
                height = float(cell)
            except ValueError:
                continue
            times.append(midnight + 3600.0 * hour)
            heights.append(height)
    if not times:
        raise ValueError("no tide heights found in the table")
    return np.array(times), np.array(heights)


def hk_predicted_tide(year: int, station: str = "TMW", timeout: float = 60.0) -> Callable:
    """The predicted water level above mean sea level at a Hong Kong tide station, for one year.

    From the Hong Kong Observatory's open data (hourly predictions; free to
    use with attribution). ``TMW`` is Tai Miu Wan at the mouth of Clear
    Water Bay, ``QUB`` Quarry Bay in the harbour, ``WAG`` Waglan Island.
    These are predictions, so a surge or a set-down on the day (a few tenths
    of a metre at most outside typhoons) is not in them.
    """
    url = f"{HKO_TIDES}?dataType=HHOT&station={station}&year={year}&rformat=csv"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return tide_level(*read_hk_predicted_tide(response.read().decode("utf-8", "replace"), year))


def _cell_medians(cell: np.ndarray, depth: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    order = np.lexsort((depth, cell))
    cell, depth = cell[order], depth[order]
    cells, first, count = np.unique(cell, return_index=True, return_counts=True)
    low, high = first + (count - 1) // 2, first + count // 2
    return cells, 0.5 * (depth[low] + depth[high])


def _spread(values: np.ndarray, has: np.ndarray, widest: float) -> np.ndarray:
    """Fill the gaps between the cells that have a value, nearest data first."""
    filled = np.where(has, values, np.nan)
    data, weight = np.where(has, values, 0.0), has.astype(float)
    sigma = 1.0
    while np.isnan(filled).any() and sigma <= max(widest, 1.0):
        share = ndimage.gaussian_filter(weight, sigma, mode="constant")
        near = ndimage.gaussian_filter(data, sigma, mode="constant")
        fill = np.isnan(filled) & (share > 1e-3)
        filled[fill] = near[fill] / share[fill]
        sigma *= 2.0
    if np.isnan(filled).any():
        nearest = ndimage.distance_transform_edt(np.isnan(filled), return_distances=False, return_indices=True)
        filled = filled[nearest[0], nearest[1]]
    return filled


def _without_spikes(values: np.ndarray, has: np.ndarray, least_m: float = 2.0) -> np.ndarray:
    """Cells whose value agrees with their neighbours'. A cell alone is kept: nothing says it is wrong."""
    kernel = np.ones((5, 5))
    kernel[2, 2] = 0.0
    others = ndimage.convolve(has.astype(float), kernel, mode="constant")
    around = ndimage.convolve(np.where(has, values, 0.0), kernel, mode="constant") / np.maximum(others, 1.0)
    judged = has & (others >= 3)
    if not judged.any():
        return has
    off = np.abs(values - around)
    usual = 1.4826 * float(np.median(off[judged]))
    return has & ~(judged & (off > max(5.0 * usual, least_m)))


def depth_source_from_soundings(
    soundings: Soundings,
    base: Callable | None = None,
    cell_m: float = 50.0,
    reach_m: float = 1000.0,
    limits: Callable | None = None,
    shallowest_m: float = 1.0,
) -> Callable:
    """A ``depth_source`` that follows the soundings and is ``base`` away from them.

    ``soundings`` are depths below mean sea level. ``base(lat, lon)`` is the
    coarse source they correct, usually GEBCO; without one the result is NaN
    further than ``reach_m`` from a sounding. ``cell_m`` is the size of the
    cells soundings are sorted into, and should be about the spacing along a
    track or the venue's cell size, whichever is larger.

    ``limits(lat, lon)`` may return the shallowest and deepest the water can
    be at each point, NaN where it does not know (a chart's depth areas do):
    the result is held between them.
    """
    good = ~(np.isnan(soundings.lat) | np.isnan(soundings.lon) | np.isnan(soundings.depth))
    soundings = soundings.keep(good)
    if len(soundings) == 0:
        raise ValueError("no soundings")
    lat0, lon0 = float(soundings.lat.mean()), float(soundings.lon.mean())
    x, y = to_xy(soundings.lat, soundings.lon, lat0, lon0)
    width = x.max() - x.min() + 2.0 * reach_m
    height = y.max() - y.min() + 2.0 * reach_m
    cell = max(float(cell_m), float(np.sqrt(width * height / MOST_CELLS)))
    columns, rows = int(np.ceil(width / cell)) + 1, int(np.ceil(height / cell)) + 1
    x0, y0 = x.min() - reach_m, y.min() - reach_m
    grid_x, grid_y = x0 + cell * np.arange(columns), y0 + cell * np.arange(rows)

    column = np.clip(np.rint((x - x0) / cell).astype(int), 0, columns - 1)
    row = np.clip(np.rint((y - y0) / cell).astype(int), 0, rows - 1)
    cells, medians = _cell_medians(row * columns + column, soundings.depth)
    sounded = np.full(rows * columns, np.nan)
    sounded[cells] = medians
    sounded = sounded.reshape(rows, columns)
    has = ~np.isnan(sounded)

    under = None
    if base is not None:
        cell_lat, cell_lon = to_latlon(*np.meshgrid(grid_x, grid_y), lat0, lon0)
        under = np.asarray(base(cell_lat, cell_lon), dtype=float)
        if np.isnan(under).all():
            under = None
        elif np.isnan(under).any():
            nearest = ndimage.distance_transform_edt(np.isnan(under), return_distances=False, return_indices=True)
            under = under[nearest[0], nearest[1]]
    values = sounded if under is None else sounded - under
    has = _without_spikes(np.where(has, values, 0.0), has)
    spread = _spread(values, has, reach_m / cell)
    away = ndimage.distance_transform_edt(~has) * cell
    trust = np.clip(2.0 * (1.0 - away / reach_m), 0.0, 1.0)
    trust = trust * trust * (3.0 - 2.0 * trust)
    if under is None:
        depth = np.where(trust > 0.0, spread, np.nan)
    else:
        depth = under + trust * spread
    interpolate = RegularGridInterpolator((grid_y, grid_x), depth, bounds_error=False, fill_value=np.nan)

    def source(at_lat, at_lon) -> np.ndarray:
        at_lat, at_lon = np.asarray(at_lat, dtype=float), np.asarray(at_lon, dtype=float)
        at_x, at_y = to_xy(at_lat, at_lon, lat0, lon0)
        found = interpolate(np.column_stack([at_y.ravel(), at_x.ravel()])).reshape(at_lat.shape)
        if base is not None:
            outside = np.isnan(found)
            if outside.any():
                found = np.where(outside, np.asarray(base(at_lat, at_lon), dtype=float), found)
        if limits is not None:
            low, high = limits(at_lat, at_lon)
            found = np.where(np.isnan(low), found, np.maximum(found, low))
            found = np.where(np.isnan(high), found, np.minimum(found, high))
        return np.where(np.isnan(found), np.nan, np.maximum(found, shallowest_m))

    return source
