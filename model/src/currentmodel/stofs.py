"""NOAA's global ocean model (STOFS-2D-Global) for any venue in the world.

STOFS-2D-Global is NOAA's operational tide and storm-surge model. It runs
four times a day on one unstructured mesh covering every coast on Earth and
publishes its output as open data on AWS (bucket ``noaa-gestofs-pds``), with
no account or key. Each run holds 6 hours of analysis and a 180-hour
forecast, hourly, of:

* depth-averaged currents driven by tide, wind and air pressure,
* water level from the same run, and
* water level from tide alone.

The mesh is fine along United States coasts (about 100 to 250 m) and coarse
elsewhere (2 to 4 km), so this module is used two ways:

* as a current model in its own right (:func:`currents`), good where the
  mesh is fine and a rough first guess elsewhere, and
* as the open-sea water levels that drive our own fine physics model of a
  venue (:func:`levels`), which is how a detailed model can be set up
  anywhere without local tide data.

The files are 20 to 40 GB each, so nothing is downloaded whole: only the
bytes for the nodes near the venue are read, with HTTP range requests.

STOFS output is a product of NOAA/NOS and is in the public domain.
"""

from __future__ import annotations

import datetime as dt
import http.client
import io
import re
import time as _time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
from scipy.interpolate import CubicSpline
from scipy.spatial import Delaunay, cKDTree

from .baseline import TidalStreamField
from .geo import to_xy

BUCKET = "https://noaa-gestofs-pds.s3.amazonaws.com"
HOUR = 3600.0
FIRST_RECORD_H = -5  # each run starts 5 hours before its nominal time
LAST_RECORD_H = 180
FILL = -9000.0

PRODUCTS = {
    # name: (file names to try in order, variables)
    "currents": (("fields.cwl.vel",), ("u-vel", "v-vel")),
    "level": (("fields.cwl.noanomaly", "fields.cwl"), ("zeta",)),
    "tide_level": (("fields.htp",), ("zeta",)),
}


def _get(url: str, start: int | None = None, end: int | None = None, tries: int = 4) -> bytes:
    headers = {"User-Agent": "currentmodel"}
    if start is not None:
        headers["Range"] = f"bytes={start}-{end - 1}"
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code in (403, 404):
                raise FileNotFoundError(url) from error
            if attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
            if attempt == tries - 1:
                raise
        _time.sleep(2.0**attempt)
    raise AssertionError("unreachable")


def _runs(nodes: np.ndarray, request_cost_bytes: int = 262_144, width: int = 8) -> list[tuple[int, int]]:
    """Group sorted node numbers into stretches worth one request each.

    A venue's nodes sit in the file in clusters. Reading across a gap wastes
    bytes; stopping at it costs another request. Pick the split that is
    cheapest overall, counting a request as ``request_cost_bytes``.
    """
    best, best_cost = None, np.inf
    for gap in (256, 2048, 16_384, 131_072, np.inf):
        breaks = np.nonzero(np.diff(nodes) > gap)[0]
        starts = np.concatenate([[0], breaks + 1])
        ends = np.concatenate([breaks, [nodes.size - 1]])
        runs = [(int(nodes[a]), int(nodes[b]) + 1) for a, b in zip(starts, ends)]
        cost = len(runs) * request_cost_bytes + width * sum(hi - lo for lo, hi in runs)
        if cost < best_cost:
            best, best_cost = runs, cost
    return best


def _base_time(units: str) -> float:
    match = re.search(r"since\s+(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)", units)
    if not match:
        raise ValueError(f"cannot read the time base from {units!r}")
    stamp = dt.datetime.strptime(" ".join(match.groups()), "%Y-%m-%d %H:%M:%S")
    return stamp.replace(tzinfo=dt.timezone.utc).timestamp()


# ------------------------------------------------------------ file formats

_NC_TYPES = {1: ("i1", 1), 2: ("S1", 1), 3: (">i2", 2), 4: (">i4", 4), 5: (">f4", 4), 6: (">f8", 8)}


class _Classic:
    """A netCDF classic file read over HTTP: fixed layout, so any slice is one range request."""

    def __init__(self, url: str, workers: int = 16):
        self.url, self.workers = url, workers
        size = 1 << 16
        while True:
            self._buf, self._p = _get(url, 0, size), 4
            try:
                self._parse()
                break
            except IndexError:
                size *= 4

    def _int(self, wide: bool = False) -> int:
        n = 8 if wide else 4
        if self._p + n > len(self._buf):
            raise IndexError
        self._p += n
        return int.from_bytes(self._buf[self._p - n : self._p], "big")

    def _name(self) -> str:
        n = self._int()
        self._p += (n + 3) // 4 * 4
        return self._buf[self._p - (n + 3) // 4 * 4 :][:n].decode()

    def _attributes(self) -> dict:
        self._int()
        out = {}
        for _ in range(self._int()):
            name, kind, count = self._name(), self._int(), self._int()
            dtype, width = _NC_TYPES[kind]
            raw = self._buf[self._p : self._p + count * width]
            self._p += (count * width + 3) // 4 * 4
            if self._p > len(self._buf):
                raise IndexError
            out[name] = raw.decode(errors="replace") if kind == 2 else np.frombuffer(raw, dtype)
        return out

    def _parse(self) -> None:
        version = self._buf[3]
        if self._buf[:3] != b"CDF" or version not in (1, 2):
            raise ValueError("not a netCDF classic file")
        self.records_count = self._int()
        self._int()
        dims = [(self._name(), self._int()) for _ in range(self._int())]
        self._attributes()
        self._int()
        self.vars, self.record_size = {}, 0
        for _ in range(self._int()):
            name = self._name()
            ids = [self._int() for _ in range(self._int())]
            attributes, kind, size = self._attributes(), self._int(), self._int()
            begin = self._int(wide=version == 2)
            is_record = bool(ids) and dims[ids[0]][1] == 0
            self.vars[name] = (kind, size, begin, is_record, attributes)
            if is_record:
                self.record_size += size

    def times(self) -> np.ndarray:
        kind, size, begin, _, attributes = self.vars["time"]
        first = np.frombuffer(_get(self.url, begin, begin + 8), ">f8")[0]
        return _base_time(attributes["units"]) + first + HOUR * np.arange(self.records_count)

    def fixed(self, name: str) -> np.ndarray:
        kind, size, begin, _, _ = self.vars[name]
        return np.frombuffer(_get(self.url, begin, begin + size), _NC_TYPES[kind][0])

    def records(self, name: str, records: Sequence[int], nodes: np.ndarray) -> np.ndarray:
        kind, _, begin, _, _ = self.vars[name]
        dtype, width = _NC_TYPES[kind]
        runs = _runs(nodes, width=width)
        picks = [nodes[(nodes >= lo) & (nodes < hi)] - lo for lo, hi in runs]

        def one(job: tuple[int, int]) -> np.ndarray:
            record, run = job
            lo, hi = runs[run]
            start = begin + record * self.record_size + lo * width
            return np.frombuffer(_get(self.url, start, start + (hi - lo) * width), dtype)[picks[run]]

        jobs = [(record, run) for record in records for run in range(len(runs))]
        with ThreadPoolExecutor(self.workers) as pool:
            parts = list(pool.map(one, jobs))
        rows = [np.concatenate(parts[i : i + len(runs)]) for i in range(0, len(parts), len(runs))]
        return np.array(rows, dtype=float)


class _RangeFile(io.RawIOBase):
    """A read-only file object over HTTP range requests, for the HDF5 library."""

    def __init__(self, url: str, size: int, block: int = 1 << 18):
        self.url, self.size, self.block, self.pos, self.cache = url, size, block, 0, {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def readinto(self, target) -> int:
        n = min(len(target), self.size - self.pos)
        if n <= 0:
            return 0
        first, last = self.pos // self.block, (self.pos + n - 1) // self.block
        missing = [i for i in range(first, last + 1) if i not in self.cache]
        if missing:
            lo, hi = missing[0], missing[-1]
            data = _get(self.url, lo * self.block, min((hi + 1) * self.block, self.size))
            for i in range(lo, hi + 1):
                self.cache[i] = data[(i - lo) * self.block : (i - lo + 1) * self.block]
        joined = b"".join(self.cache[i] for i in range(first, last + 1))
        offset = self.pos - first * self.block
        target[:n] = joined[offset : offset + n]
        self.pos += n
        if len(self.cache) > 256:  # keep memory bounded; metadata is re-read if needed
            self.cache.clear()
        return n


class _Hdf5:
    """A netCDF-4 (HDF5) file read over HTTP. Older runs in the archive use this format."""

    def __init__(self, url: str, size: int):
        import h5py  # optional dependency, needed only for runs stored as netCDF-4

        self._file = h5py.File(_RangeFile(url, size), "r")

    def times(self) -> np.ndarray:
        time = self._file["time"]
        units = time.attrs["units"]
        return _base_time(units.decode() if isinstance(units, bytes) else str(units)) + np.asarray(time[:], dtype=float)

    def fixed(self, name: str) -> np.ndarray:
        return np.asarray(self._file[name][:])

    def records(self, name: str, records: Sequence[int], nodes: np.ndarray) -> np.ndarray:
        # Chunks are half a million nodes wide, so nearby stretches share one anyway.
        data, runs = self._file[name], _runs(nodes, request_cost_bytes=4_000_000)
        out = np.empty((len(records), nodes.size))
        for i, record in enumerate(records):
            out[i] = np.concatenate([data[record, lo:hi][nodes[(nodes >= lo) & (nodes < hi)] - lo] for lo, hi in runs])
        return out


def _open(url: str):
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "currentmodel"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            size = int(response.headers["Content-Length"])
    except urllib.error.HTTPError as error:
        raise FileNotFoundError(url) from error
    return _Classic(url) if _get(url, 0, 4)[:3] == b"CDF" else _Hdf5(url, size)


# ----------------------------------------------------------------- reading


def _as_cycle(when: float | dt.datetime) -> dt.datetime:
    if not isinstance(when, dt.datetime):
        when = dt.datetime.fromtimestamp(float(when), dt.timezone.utc)
    when = when.astimezone(dt.timezone.utc) if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
    return when.replace(hour=when.hour // 6 * 6, minute=0, second=0, microsecond=0)


@dataclass
class Series:
    """Model output at chosen mesh nodes: ``values[name]`` has shape (times, nodes)."""

    times: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    depth: np.ndarray
    values: dict[str, np.ndarray]


class Stofs:
    """Reads STOFS-2D-Global output near a venue.

    ``cache_dir`` keeps the mesh (node positions and depths, about 150 MB)
    so that it is downloaded once, not once per venue.
    """

    def __init__(self, cache_dir: str | Path | None = None, bucket: str = BUCKET):
        self.bucket = bucket
        self.cache = Path(cache_dir) if cache_dir else None
        self._mesh: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self._latest: dt.datetime | None = None

    # ------------------------------------------------------------- catalogue

    def url(self, name: str, cycle: dt.datetime) -> str:
        return f"{self.bucket}/stofs_2d_glo.{cycle:%Y%m%d}/stofs_2d_glo.t{cycle:%H}z.{name}.nc"

    def _open_product(self, product: str, cycle: dt.datetime):
        for name in PRODUCTS[product][0]:
            try:
                handle = _open(self.url(name, cycle))
            except FileNotFoundError:
                continue
            if set(PRODUCTS[product][1]) <= set(getattr(handle, "vars", None) or handle._file.keys()):
                return handle
        raise FileNotFoundError(f"no {product} file for the {cycle:%Y-%m-%d %H}Z run")

    def latest_cycle(self) -> dt.datetime:
        """The newest run whose current fields are published."""
        if self._latest is None:
            cycle = _as_cycle(dt.datetime.now(dt.timezone.utc))
            for _ in range(12):
                try:
                    _get(self.url("fields.cwl.vel", cycle), 0, 4)
                    self._latest = cycle
                    break
                except FileNotFoundError:
                    cycle -= dt.timedelta(hours=6)
            else:
                raise FileNotFoundError("no recent STOFS run found")
        return self._latest

    # ------------------------------------------------------------------ mesh

    def mesh(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Latitude, longitude and depth (m, positive down) of every mesh node."""
        if self._mesh is None:
            path = self.cache / "stofs_mesh.npz" if self.cache else None
            if path is not None and path.exists():
                with np.load(path) as saved:
                    self._mesh = (saved["lat"], saved["lon"], saved["depth"])
            else:
                handle = _open(self.url("fields.cwl.vel", self.latest_cycle()))
                lat, lon, depth = (handle.fixed(n).astype("f4") for n in ("y", "x", "depth"))
                self._mesh = (lat, lon, depth)
                if path is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    np.savez(path, lat=lat, lon=lon, depth=depth)
        return self._mesh

    def nodes_near(self, lat: float, lon: float, radius_m: float) -> np.ndarray:
        """Numbers of the mesh nodes within ``radius_m`` of a position, ascending."""
        node_lat, node_lon, _ = self.mesh()
        reach = radius_m / 111_000.0
        box = np.nonzero(
            (np.abs(node_lat - lat) < reach)
            & (np.abs((node_lon - lon + 180.0) % 360.0 - 180.0) < reach / max(np.cos(np.radians(lat)), 0.05))
        )[0]
        x, y = to_xy(node_lat[box], node_lon[box], lat, lon)
        return box[np.hypot(x, y) <= radius_m]

    # ---------------------------------------------------------------- output

    def read(
        self,
        product: str,
        nodes: np.ndarray,
        start: float,
        end: float,
        hours_per_run: int = 6,
    ) -> Series:
        """Output of ``product`` at ``nodes`` from ``start`` to ``end`` (epoch seconds).

        Past hours are stitched from successive runs, each contributing its
        freshest ``hours_per_run`` hours; hours beyond the newest run come
        from its forecast. A larger ``hours_per_run`` (a multiple of 6, up to
        180) reads fewer files and leans on older forecasts, which costs
        nothing for the tide-only product.
        """
        if product not in PRODUCTS:
            raise ValueError(f"product must be one of {sorted(PRODUCTS)}")
        if hours_per_run % 6 or not 6 <= hours_per_run <= LAST_RECORD_H:
            raise ValueError("hours_per_run must be a multiple of 6 between 6 and 180")
        nodes = np.sort(np.asarray(nodes))
        step = dt.timedelta(hours=hours_per_run)
        ceiling = _as_cycle(end) + dt.timedelta(hours=6)
        cycle = min(self.latest_cycle(), ceiling)
        if end > cycle.timestamp() + LAST_RECORD_H * HOUR:
            raise ValueError("the end time is beyond the newest forecast")
        cycles = [cycle]
        while cycles[-1].timestamp() + FIRST_RECORD_H * HOUR > start:
            cycles.append(cycles[-1] - step)
        cycles.reverse()

        # Runs that are missing from the archive are bridged by the forecast of the one before.
        runs = []
        for cycle in cycles:
            try:
                runs.append((cycle, self._open_product(product, cycle)))
            except FileNotFoundError:
                continue
        times, values = [], {name: [] for name in PRODUCTS[product][1]}
        covered = -np.inf
        for i, (cycle, handle) in enumerate(runs):
            run_times = handle.times()
            # Leave to the next run the hours it will cover more freshly.
            upto = runs[i + 1][0].timestamp() + (FIRST_RECORD_H - 1) * HOUR if i + 1 < len(runs) else np.inf
            pick = np.nonzero(
                (run_times > covered) & (run_times >= start - HOUR) & (run_times <= min(upto, end + HOUR))
            )[0]
            if pick.size == 0:
                continue
            for name in values:
                values[name].append(handle.records(name, pick.tolist(), nodes))
            times.append(run_times[pick])
            covered = run_times[pick[-1]]
        if not times:
            raise FileNotFoundError("no STOFS output found for this period")
        node_lat, node_lon, depth = self.mesh()
        merged = {name: np.concatenate(parts) for name, parts in values.items()}
        merged = {name: np.where(block < FILL, np.nan, block) for name, block in merged.items()}
        return Series(np.concatenate(times), node_lat[nodes], node_lon[nodes], depth[nodes], merged)


# ------------------------------------------------------------ what we use


def currents(
    stofs: Stofs,
    lat: float,
    lon: float,
    radius_m: float,
    start: float,
    end: float,
    sample_s: float = 900.0,
    hours_per_run: int = 6,
) -> TidalStreamField:
    """The global model's currents around a venue, as a model the suite can use.

    Hourly output is resampled in time with a spline (straight lines between
    hourly values would clip the tide's peaks by a few percent). In space the
    field is interpolated between mesh nodes, out to twice the local node
    spacing.
    """
    series = stofs.read("currents", stofs.nodes_near(lat, lon, radius_m), start, end, hours_per_run)
    u, v = series.values["u-vel"], series.values["v-vel"]
    wet = ~(np.isnan(u).any(axis=0) | np.isnan(v).any(axis=0))
    if wet.sum() < 3:
        raise ValueError("the global model has fewer than 3 wet nodes here")
    u, v, node_lat, node_lon = u[:, wet], v[:, wet], series.lat[wet], series.lon[wet]
    times = np.arange(series.times[0], series.times[-1] + 1e-6, sample_s)
    u = CubicSpline(series.times, u, axis=0)(times)
    v = CubicSpline(series.times, v, axis=0)(times)
    x, y = to_xy(node_lat, node_lon, lat, lon)
    spacing = float(np.median(cKDTree(np.column_stack([x, y])).query(np.column_stack([x, y]), k=2)[0][:, 1]))
    return TidalStreamField(node_lat, node_lon, times, u, v, neighbours=3, max_distance_m=2.0 * spacing)


@dataclass
class LevelField:
    """Water level over time at scattered points, for driving a venue model's open edges."""

    lat: np.ndarray
    lon: np.ndarray
    times: np.ndarray
    level: np.ndarray  # (times, points), metres

    def __post_init__(self) -> None:
        good = ~np.isnan(self.level).any(axis=0)
        if good.sum() < 3:
            raise ValueError("need water levels at 3 or more points")
        self.lat, self.lon, self.level = self.lat[good], self.lon[good], self.level[:, good]

    def at(self, lat, lon) -> Callable[[float], np.ndarray]:
        """A function of time giving the level at each requested position.

        Linear between the surrounding three points in space (nearest point
        outside them), smooth in time.
        """
        lat, lon = np.atleast_1d(lat).astype(float), np.atleast_1d(lon).astype(float)
        lat0, lon0 = float(np.mean(self.lat)), float(np.mean(self.lon))
        points = np.column_stack(to_xy(self.lat, self.lon, lat0, lon0))
        query = np.column_stack(to_xy(lat, lon, lat0, lon0))
        mesh = Delaunay(points)
        simplex = mesh.find_simplex(query)
        weights = np.zeros((query.shape[0], points.shape[0]))
        inside = simplex >= 0
        transform = mesh.transform[simplex[inside]]
        bary = np.einsum("nij,nj->ni", transform[:, :2], query[inside] - transform[:, 2])
        bary = np.column_stack([bary, 1.0 - bary.sum(axis=1)])
        weights[np.nonzero(inside)[0][:, None], mesh.simplices[simplex[inside]]] = bary
        outside = np.nonzero(~inside)[0]
        weights[outside, cKDTree(points).query(query[outside])[1]] = 1.0
        spline = CubicSpline(self.times, self.level @ weights.T, axis=0)
        first, last = self.times[0], self.times[-1]
        return lambda t: spline(min(max(t, first), last))


def levels(
    stofs: Stofs,
    lat: float,
    lon: float,
    radius_m: float,
    start: float,
    end: float,
    tide_only: bool = False,
    hours_per_run: int | None = None,
) -> LevelField:
    """Water levels around a venue from the global model, to drive a venue model.

    ``tide_only`` gives the astronomical tide alone. Otherwise the weather's
    effect on the sea surface is added: the difference between the global
    model's full water level and its tide, reduced at each hour to a tilted
    plane across the venue. The plane keeps what a venue can use (the sea
    standing higher or lower, and tilting, under wind and air pressure) and
    drops the node-to-node differences of a mesh that is kilometres coarse,
    which would otherwise drive currents of their own at the open edges.
    """
    nodes = stofs.nodes_near(lat, lon, radius_m)
    tide = stofs.read("tide_level", nodes, start, end, LAST_RECORD_H if hours_per_run is None else hours_per_run)
    field = LevelField(tide.lat, tide.lon, tide.times, tide.values["zeta"])
    if tide_only:
        return field
    full = stofs.read("level", nodes, start, end, 6 if hours_per_run is None else hours_per_run)
    common, in_tide, in_full = np.intersect1d(tide.times, full.times, return_indices=True)
    surge = full.values["zeta"][in_full] - tide.values["zeta"][in_tide]
    good = ~np.isnan(surge).any(axis=0)
    x, y = to_xy(full.lat[good], full.lon[good], lat, lon)
    plane = np.column_stack([np.ones(x.size), x, y])
    coefficients = np.linalg.lstsq(plane, surge[:, good].T, rcond=None)[0]  # (3, times)
    fx, fy = to_xy(field.lat, field.lon, lat, lon)
    smooth = (np.column_stack([np.ones(fx.size), fx, fy]) @ coefficients).T
    keep = np.isin(field.times, common)
    return LevelField(field.lat, field.lon, field.times[keep], field.level[keep] + smooth)
