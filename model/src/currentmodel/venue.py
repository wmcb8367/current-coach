"""Build a venue grid anywhere in the world from open global elevation data.

The source is the Terrain Tiles dataset on AWS Open Data: a global elevation
mosaic in web-map tiles that combines land surveys with ocean bathymetry
(GEBCO and ETOPO1 offshore, finer sources near some coasts). It needs no
account or key.

The tiles answer two questions at different quality:

* Where is the coast? At zoom 12 (about 30 m) the land surveys draw it well
  everywhere.
* How deep is the water? Near some coasts (much of the United States) zoom 12
  carries surveyed depths. Elsewhere it reads exactly zero over water and the
  only depths are the global ocean grids at zoom 10 and below, which are
  smooth: channels and shoals narrower than about a kilometre are missing.

So the grid takes the coastline from the fine tiles and the depth from the
best level that has one. Where better local depths exist (charts, surveys),
pass them as ``depth_source`` and they replace the global ones; nothing else
in the package changes.

Attribution for the tiles is listed at
https://github.com/tilezen/joerd/blob/master/docs/attribution.md
"""

from __future__ import annotations

import io
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np
from scipy import ndimage

from .geo import to_latlon
from .physics import Grid

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE_SIZE = 256


def _fetch_tile(z: int, x: int, y: int, cache: Path | None) -> np.ndarray:
    from PIL import Image  # optional dependency, needed only to build grids

    path = cache / f"terrarium_{z}_{x}_{y}.png" if cache else None
    if path is not None and path.exists():
        data = path.read_bytes()
    else:
        request = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": "currentmodel"})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=float)
    # Terrarium encoding: metres above sea level, negative under water.
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def _pixel(lat, lon, zoom: int):
    """Global web-mercator pixel coordinates (column, row) at a zoom level."""
    scale = TILE_SIZE * 2**zoom
    col = (np.asarray(lon) + 180.0) / 360.0 * scale
    sin_lat = np.sin(np.radians(lat))
    row = (0.5 - np.log((1 + sin_lat) / (1 - sin_lat)) / (4 * np.pi)) * scale
    return col, row


def elevation(lat, lon, zoom: int = 12, cache_dir: str | Path | None = None) -> np.ndarray:
    """Elevation in metres (negative under water) at each position, bilinearly sampled."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    col, row = _pixel(lat, lon, zoom)
    col, row = col - 0.5, row - 0.5  # pixel centres
    cache = Path(cache_dir) if cache_dir else None
    x_min, x_max = int(np.floor(col.min())) // TILE_SIZE, int(np.floor(col.max()) + 1) // TILE_SIZE
    y_min, y_max = int(np.floor(row.min())) // TILE_SIZE, int(np.floor(row.max()) + 1) // TILE_SIZE
    count = (x_max - x_min + 1) * (y_max - y_min + 1)
    if count > 400:
        raise ValueError(f"the area needs {count} tiles at zoom {zoom}; lower the zoom or shrink the area")
    mosaic = np.vstack(
        [np.hstack([_fetch_tile(zoom, x, y, cache) for x in range(x_min, x_max + 1)]) for y in range(y_min, y_max + 1)]
    )
    c = col - x_min * TILE_SIZE
    r = row - y_min * TILE_SIZE
    c0 = np.clip(np.floor(c).astype(int), 0, mosaic.shape[1] - 2)
    r0 = np.clip(np.floor(r).astype(int), 0, mosaic.shape[0] - 2)
    fc, fr = c - c0, r - r0
    return (
        mosaic[r0, c0] * (1 - fr) * (1 - fc)
        + mosaic[r0, c0 + 1] * (1 - fr) * fc
        + mosaic[r0 + 1, c0] * fr * (1 - fc)
        + mosaic[r0 + 1, c0 + 1] * fr * fc
    )


def build_grid(
    lat: float,
    lon: float,
    width_m: float,
    height_m: float,
    dx: float = 100.0,
    coast_zoom: int = 12,
    depth_zoom: int = 10,
    depth_source: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None,
    land_above_m: float = 0.5,
    shallowest_m: float = 1.0,
    coast_taper_m: float = 1000.0,
    cache_dir: str | Path | None = None,
) -> Grid:
    """A venue grid centred on (lat, lon), from global terrain tiles.

    ``width_m`` is the east-west extent and ``height_m`` the north-south
    extent. The coast comes from ``coast_zoom``; depths come from the same
    tiles where they hold any, otherwise from ``depth_zoom``.

    ``depth_source(lat, lon)`` may return better depths in metres, positive
    down, with NaN where it has none. If it also has ``water(lat, lon)``
    (chart depths do: 1 water, 0 land, NaN unknown), that sets the coastline
    wherever it knows, in place of the tiles.

    The coarse ocean grids ramp from their depths up to the land heights
    beside them, so within about a kilometre of their own idea of the coast
    their numbers are an artefact of that ramp, not soundings. Those cells,
    and water the coarse grid calls land (narrow harbours and inlets), take
    the nearest depth from beyond ``coast_taper_m`` instead. That is a guess:
    it keeps harbours open, and it erases real shallows close to shore.
    Water cut off from the edge of the grid is dropped.
    """
    nx = int(round(width_m / dx))
    ny = int(round(height_m / dx))
    x0 = -0.5 * dx * (nx - 1)
    y0 = -0.5 * dx * (ny - 1)
    x, y = np.meshgrid(x0 + dx * np.arange(nx), y0 + dx * np.arange(ny))
    cell_lat, cell_lon = to_latlon(x, y, lat, lon)

    fine = elevation(cell_lat, cell_lon, coast_zoom, cache_dir)
    water = fine <= land_above_m
    charted = getattr(depth_source, "water", None)
    if charted is not None:
        # Where charts cover the venue they say what is water: they keep cuts and harbours the tiles close.
        verdict = np.asarray(charted(cell_lat, cell_lon), dtype=float)
        water = np.where(np.isnan(verdict), water, verdict > 0.5)
        water = open_diagonals(water)
    water = _open_water(water)

    # Fine tiles without soundings read zero over water, with specks of noise
    # below it. Trust their depths only where most of the water has one.
    sounded = water & (fine < -land_above_m)
    if sounded.sum() > 0.5 * water.sum():
        depth = np.where(sounded, -fine, np.nan)
    else:
        depth = np.full(fine.shape, np.nan)
    if np.isnan(depth[water]).any():
        coarse = -elevation(cell_lat, cell_lon, depth_zoom, cache_dir)
        clear = ndimage.distance_transform_edt(coarse > 0.0) * dx >= coast_taper_m
        depth = np.where(np.isnan(depth) & clear, coarse, depth)
    if depth_source is not None:
        better = np.asarray(depth_source(cell_lat, cell_lon), dtype=float)
        depth = np.where(np.isnan(better), depth, better)
    known = water & ~np.isnan(depth)
    if not known.any():
        raise ValueError("no water depths found for this area")
    nearest = ndimage.distance_transform_edt(~known, return_distances=False, return_indices=True)
    depth = np.maximum(depth[nearest[0], nearest[1]], shallowest_m)
    return Grid(lat, lon, x0, y0, dx, np.where(water, depth, 0.0))


def open_diagonals(water: np.ndarray) -> np.ndarray:
    """Join water cells that touch only at a corner, by wetting one of the two land cells beside them.

    The solver passes water only across cell faces, so a narrow channel
    running at an angle to the grid (a staircase of cells meeting at their
    corners) would carry nothing. The land cell wetted is the first of the
    pair; repeated until no such corner remains.
    """
    water = np.asarray(water, dtype=bool).copy()
    for _ in range(10):
        a, b, c, d = water[:-1, :-1], water[:-1, 1:], water[1:, :-1], water[1:, 1:]
        main = a & d & ~b & ~c        # water at top-left and bottom-right only
        anti = b & c & ~a & ~d        # water at top-right and bottom-left only
        if not (main.any() or anti.any()):
            break
        water[:-1, 1:] |= main        # wet the top-right cell of each
        water[:-1, :-1] |= anti       # wet the top-left cell of each
    return water


def _open_water(water: np.ndarray) -> np.ndarray:
    """Keep only water connected to the edge of the grid (the sea), not ponds."""
    labels, count = ndimage.label(water)
    if count == 0:
        raise ValueError("the area has no water")
    edge = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    touching = np.unique(edge[edge > 0])
    if touching.size == 0:
        touching = [1 + int(np.argmax(ndimage.sum_labels(water, labels, np.arange(1, count + 1))))]
    return np.isin(labels, touching)
