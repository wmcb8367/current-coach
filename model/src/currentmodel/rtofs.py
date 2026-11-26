"""NOAA's global ocean forecast (RTOFS, HYCOM at 1/12 degree): the ocean currents a tide-and-surge model lacks.

The global model the venues lean on (STOFS-2D) carries tide and storm surge
but no ocean circulation: no Gulf Stream, no Kuroshio, no monsoon drift.
Off Miami the Florida Current's inshore edge is the largest current at the
sea buoy (0.3 kn north on average, over 1.5 kn on some days), and neither
STOFS nor the venue physics model has it. RTOFS does.

NOAA publishes each day's run on AWS (bucket ``noaa-nws-rtofs-pds``, no
key, archive back to at least October 2025): hourly forecasts to six days
ahead. The surface currents sit in the HYCOM "archs" files, a 440 MB
gzipped tar per hour holding one binary ``.a`` file of whole-globe fields
in the order its ``.b`` text file lists them. :func:`surface_current`
streams one of these and keeps only the box asked for; nothing of the
globe is stored. Total surface velocity is the top layer's velocity plus
the barotropic (depth-mean) part, which HYCOM keeps apart.

The grid's coordinates come from one of the small netCDF ``2ds_diag``
files (read once and cached), which also hold the depth-mean current on
their own.
"""

from __future__ import annotations

import datetime as dt
import io
import tarfile
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

BUCKET = "https://noaa-nws-rtofs-pds.s3.amazonaws.com"
IDM, JDM = 4500, 3298
WORDS = IDM * JDM
PADDED = ((WORDS + 4095) // 4096) * 4096


def _day(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y%m%d")


def archs_url(run_day: str, lead_h: int, suffix: str) -> str:
    return f"{BUCKET}/rtofs.{run_day}/rtofs_glo.t00z.f{lead_h:02d}.archs.{suffix}"


def field_index(b_text: str) -> dict[tuple[str, int], int]:
    """Position of each (field, layer) in the ``.a`` file, from the ``.b`` text."""
    lines = b_text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.lstrip().startswith("field")) + 1
    index = {}
    for n, line in enumerate(lines[start:]):
        if "=" not in line:
            continue
        name = line.split("=")[0].strip()
        parts = line.split("=")[1].split()
        index[(name, int(parts[2]))] = n
    return index


@dataclass
class Window:
    """The rows and columns of the global grid that cover a box, with their coordinates."""

    j0: int
    j1: int
    i0: int
    i1: int
    lat: np.ndarray
    lon: np.ndarray


def window(lat_min: float, lat_max: float, lon_min: float, lon_max: float, cache_dir: str | Path | None = None) -> Window:
    """The part of the RTOFS grid inside a box (longitudes -180 to 180)."""
    import h5py

    cache = Path(cache_dir) if cache_dir else None
    path = cache / "rtofs-grid.npz" if cache else None
    if path is not None and path.exists():
        grid = np.load(path)
        lat, lon = grid["lat"], grid["lon"]
    else:
        url = f"{BUCKET}/rtofs.20261125/rtofs_glo_2ds_f000_diag.nc"
        data = urllib.request.urlopen(url, timeout=300).read()
        with h5py.File(io.BytesIO(data)) as f:
            lat, lon = f["Latitude"][:], (f["Longitude"][:] + 180.0) % 360.0 - 180.0
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(path, lat=lat, lon=lon)
    inside = (lat >= lat_min) & (lat <= lat_max) & (lon >= lon_min) & (lon <= lon_max)
    if not inside.any():
        raise ValueError("the box holds no RTOFS grid point")
    j, i = np.where(inside)
    j0, j1, i0, i1 = j.min(), j.max() + 1, i.min(), i.max() + 1
    return Window(int(j0), int(j1), int(i0), int(i1), lat[j0:j1, i0:i1], lon[j0:j1, i0:i1])


def _stream_fields(url: str, wanted: list[int], win: Window) -> dict[int, np.ndarray]:
    """Read the wanted field numbers of a gzipped-tar ``.a`` file as it downloads, keeping only the window."""
    out: dict[int, np.ndarray] = {}
    last = max(wanted)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "currentmodel"}), timeout=600) as response:
        with tarfile.open(fileobj=response, mode="r|gz") as archive:
            for member in archive:
                if not member.name.endswith(".a"):
                    continue
                handle = archive.extractfile(member)
                for n in range(last + 1):
                    raw = handle.read(PADDED * 4)
                    if len(raw) < WORDS * 4:
                        raise ValueError(f"{url} ended early")
                    if n in wanted:
                        values = np.frombuffer(raw[: WORDS * 4], dtype=">f4").reshape(JDM, IDM)
                        part = values[win.j0 : win.j1, win.i0 : win.i1].astype(float)
                        out[n] = np.where(np.abs(part) > 1e20, np.nan, part)
                break
    return out


def surface_current(run_day: str, lead_h: int, win: Window, cache_dir: str | Path | None = None) -> tuple[np.ndarray, np.ndarray]:
    """East and north surface current (m/s) on the window, from the run of ``run_day`` (``YYYYMMDD``, 00 UTC) ``lead_h`` hours on."""
    cache = Path(cache_dir) if cache_dir else None
    key = f"rtofs-{run_day}-f{lead_h:02d}-{win.j0}-{win.j1}-{win.i0}-{win.i1}.npz"
    if cache is not None and (cache / key).exists():
        saved = np.load(cache / key)
        return saved["u"], saved["v"]
    b_text = urllib.request.urlopen(archs_url(run_day, lead_h, "b"), timeout=120).read().decode()
    index = field_index(b_text)
    names = {"u": ("u-vel.", 1), "v": ("v-vel.", 1), "ub": ("u_btrop", 0), "vb": ("v_btrop", 0)}
    numbers = {k: index[n] for k, n in names.items()}
    fields = _stream_fields(archs_url(run_day, lead_h, "a.tgz"), list(numbers.values()), win)
    u = fields[numbers["u"]] + fields[numbers["ub"]]
    v = fields[numbers["v"]] + fields[numbers["vb"]]
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache / key, u=u, v=v)
    return u, v


class RtofsCurrent:
    """RTOFS surface current as a model: daily snapshots, interpolated in time, nearest grid point in space.

    ``snapshots`` is a list of (time, u, v) on ``win``. Between snapshots
    the current is interpolated linearly; outside them it is NaN.
    """

    def __init__(self, win: Window, snapshots, lat0: float, lon0: float):
        from scipy.spatial import cKDTree

        snapshots = sorted(snapshots, key=lambda s: s[0])
        self.times = np.array([s[0] for s in snapshots], dtype=float)
        self.u = np.stack([s[1].ravel() for s in snapshots])
        self.v = np.stack([s[2].ravel() for s in snapshots])
        good = np.isfinite(self.u).all(axis=0) & np.isfinite(self.v).all(axis=0)
        self.points = np.where(good)[0]
        self.tree = cKDTree(np.column_stack([win.lat.ravel()[good], win.lon.ravel()[good] * np.cos(np.radians(lat0))]))
        self.lat0, self.lon0 = float(lat0), float(lon0)

    def velocity(self, lat, lon, time):
        lat, lon, time = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (lat, lon, time)))
        _, k = self.tree.query(np.column_stack([lat.ravel(), lon.ravel() * np.cos(np.radians(self.lat0))]))
        p = self.points[k]
        t = time.ravel()
        step = np.clip(np.searchsorted(self.times, t) - 1, 0, max(len(self.times) - 2, 0))
        if len(self.times) == 1:
            frac = np.zeros_like(t)
            nxt = step
        else:
            frac = np.clip((t - self.times[step]) / (self.times[step + 1] - self.times[step]), 0.0, 1.0)
            nxt = step + 1
        inside = (t >= self.times[0]) & (t <= self.times[-1])
        u = (1 - frac) * self.u[step, p] + frac * self.u[nxt, p]
        v = (1 - frac) * self.v[step, p] + frac * self.v[nxt, p]
        return np.where(inside, u, np.nan).reshape(lat.shape), np.where(inside, v, np.nan).reshape(lat.shape)
