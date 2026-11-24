"""Water depths for any venue from GEBCO, the public global chart of the sea floor.

The terrain tiles :mod:`currentmodel.venue` starts from hold real soundings
on much of the United States coast and little elsewhere: east of Hong Kong's
Clearwater Bay they give 5 m where the chart shows 25 m. GEBCO's grid (one
value every 15 arc-seconds, about 450 m, revised yearly) is better there and
covers the world. It is still coarse and, close inshore, mostly estimated
from satellite gravity rather than sounded, so a national chart or survey
remains better where one can be had; anything that answers
``depth_source(lat, lon)`` plugs into :func:`~currentmodel.venue.build_grid`
the same way.

GEBCO serves boxes on request through its download site
(https://download.gebco.net): ask, wait a few seconds, fetch a zip holding a
small netCDF file. :func:`gebco_depths` does that and keeps the file.
The grid is free to use with attribution: "GEBCO Compilation Group (2026)
GEBCO 2026 Grid".
"""

from __future__ import annotations

import io
import json
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

import numpy as np
from scipy import ndimage
from scipy.interpolate import RegularGridInterpolator
from scipy.io import netcdf_file

GEBCO = "https://download.gebco.net/api"
GEBCO_GRID = 1  # the global grid
GEBCO_BATHYMETRY = 1
GEBCO_NETCDF = 1


def depth_source_from_grid(lat: np.ndarray, lon: np.ndarray, elevation: np.ndarray, shallowest_m: float = 1.0) -> Callable:
    """A ``depth_source`` from a grid of heights (metres, sea floor negative).

    Depths are interpolated between grid points. Points the grid calls land
    take the nearest water depth, because a 450 m grid's coastline is not the
    venue's: the venue grid decides what is water from finer data.
    """
    depth = np.where(elevation < 0, -np.asarray(elevation, dtype=float), np.nan)
    known = ~np.isnan(depth)
    if not known.any():
        raise ValueError("the grid holds no water")
    nearest = ndimage.distance_transform_edt(~known, return_distances=False, return_indices=True)
    filled = np.maximum(depth[nearest[0], nearest[1]], shallowest_m)
    interpolate = RegularGridInterpolator((lat, lon), filled, bounds_error=False, fill_value=np.nan)

    def source(at_lat, at_lon):
        at_lat, at_lon = np.asarray(at_lat, dtype=float), np.asarray(at_lon, dtype=float)
        return interpolate(np.column_stack([at_lat.ravel(), at_lon.ravel()])).reshape(at_lat.shape)

    return source


def read_gebco(path_or_bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Latitudes, longitudes and heights from a GEBCO netCDF subset (or the zip it comes in)."""
    data = Path(path_or_bytes).read_bytes() if isinstance(path_or_bytes, (str, Path)) else path_or_bytes
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if n.endswith(".nc") and "tid" not in n]
            if not names:
                raise ValueError("no depth grid in the GEBCO download")
            data = archive.read(names[0])
    with netcdf_file(io.BytesIO(data), "r", mmap=False) as file:
        return (
            file.variables["lat"][:].astype(float),
            file.variables["lon"][:].astype(float),
            file.variables["elevation"][:].astype(float),
        )


def _call(url: str, body: dict | None = None, timeout: float = 60.0) -> bytes:
    request = urllib.request.Request(url, method="POST" if body is not None else "GET")
    if body is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(body).encode()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def gebco_depths(
    south: float,
    north: float,
    west: float,
    east: float,
    cache_dir: str | Path | None = None,
    wait_s: float = 180.0,
) -> Callable:
    """A ``depth_source`` for the box, from GEBCO's download service.

    Ask for a box a few kilometres wider than the venue. With ``cache_dir``
    the download is kept and reused.
    """
    cache = Path(cache_dir) / f"gebco_{south:.3f}_{north:.3f}_{west:.3f}_{east:.3f}.zip" if cache_dir else None
    if cache is not None and cache.exists():
        return depth_source_from_grid(*read_gebco(cache))
    basket = {
        "id": "0",
        "email": None,
        "submission_date": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "processing_status": "new",
        "items": [
            {
                "id": 0,
                "grid_id": GEBCO_GRID,
                "data_source_ids": [GEBCO_BATHYMETRY],
                "formats": [GEBCO_NETCDF],
                "left": west,
                "right": east,
                "top": north,
                "bottom": south,
            }
        ],
    }
    ticket = json.loads(_call(f"{GEBCO}/queue", basket))["basketId"]
    deadline = time.time() + wait_s
    while json.loads(_call(f"{GEBCO}/queue/status/{ticket}")).get("status") != "finished":
        if time.time() > deadline:
            raise TimeoutError("GEBCO did not prepare the depths in time")
        time.sleep(5.0)
    data = _call(f"{GEBCO}/queue/download/{ticket}", timeout=300.0)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    return depth_source_from_grid(*read_gebco(data))
