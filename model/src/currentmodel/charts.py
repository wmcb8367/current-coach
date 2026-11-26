"""Water depths from official electronic navigational charts (S-57 files).

A hydrographic office's chart is drawn from its own surveys, so where one
can be had it beats any global grid inshore. Charts come as "cells": one
``.000`` file per chart, in a format called S-57 that every chart plotter
reads. Some countries give them away (the United States: every NOAA chart,
https://charts.noaa.gov/ENCs/ENCs.shtml), most sell them, and many sell
them only encrypted (S-63), which this cannot read and says so.

What a chart knows about depth, and what is taken from it:

* soundings: the depth figures printed on the chart;
* depth contours: lines of equal depth, used as rows of soundings;
* depth areas: for every patch of water the shallowest and deepest it can
  be (0 to 5 m, 5 to 10 m and so on, and dredged channels with their kept
  depth). No depth the model ends up with may leave that range.

:func:`chart_depth_source` turns one chart or several into a
``depth_source`` for :func:`~currentmodel.venue.build_grid`, laid over a
coarser source such as GEBCO by
:func:`~currentmodel.soundings.depth_source_from_soundings`. Where charts
overlap, the larger-scale (more detailed) one wins inside its own coverage.

Chart depths are measured below the chart's own datum, a low-water level
chosen so that there is nearly always at least that much water. The model
wants depths below mean sea level, so the caller must say how far mean sea
level is above the chart's datum: about 1.3 to 1.45 m in Hong Kong, about
0.34 m at Miami. The tide tables for the place give it.

A chart is not a survey. Its soundings are picked to show the shallowest
spots, because it exists to keep ships off the bottom, so depths built from
it lean shallow. It is licensed data too: a chart that was bought, or a
grid made from one, must not be put in a public repository.

Only the base cell (``.000``) is read. Updates issued since (``.001``,
``.002`` ...) are not applied; they rarely move a depth contour.
"""

from __future__ import annotations

import io
import struct
import warnings
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

from .geo import to_xy
from .soundings import MOST_CELLS, Soundings, depth_source_from_soundings

FIELD_END, UNIT_END = 0x1E, 0x1F
ISOLATED_NODE, CONNECTED_NODE, EDGE = 110, 120, 130
DEPTH_AREA, DEPTH_CONTOUR, DREDGED_AREA, SOUNDING, COVERAGE = 42, 43, 46, 129, 302
DEPTH_FROM, DEPTH_TO, CONTOUR_DEPTH, COVERAGE_KIND = 87, 88, 174, 18
METRES = 1


@dataclass
class Chart:
    """The depth information of one chart cell. Depths are metres below the chart's datum."""

    name: str
    scale: int  # the 50000 of 1:50,000; smaller is more detailed
    sounding_datum: int  # the S-57 code of the datum depths are measured from
    soundings: Soundings
    contours: list[tuple[float, np.ndarray, np.ndarray]] = field(default_factory=list)  # depth, latitudes, longitudes
    areas: list[tuple[float, float, np.ndarray]] = field(default_factory=list)  # shallowest, deepest, boundary
    coverage: list[np.ndarray] = field(default_factory=list)  # boundaries of what the chart covers

    # A boundary is an array of straight pieces, one per row: lat, lon, lat, lon.


def _records(data: bytes):
    """Each record of an ISO 8211 file as a list of (tag, bytes) fields."""
    position = 0
    while position + 24 <= len(data):
        leader = data[position : position + 24]
        try:
            length, base = int(leader[0:5]), int(leader[12:17])
            size_length, size_position, size_tag = int(leader[20:21]), int(leader[21:22]), int(leader[23:24])
        except ValueError:
            raise ValueError("damaged chart file: a record does not start as the format says") from None
        entry = size_tag + size_length + size_position
        fields, at, last = [], position + 24, base
        while at + entry <= position + base and data[at] != FIELD_END:
            tag = data[at : at + size_tag].decode("ascii", "replace")
            size = int(data[at + size_tag : at + size_tag + size_length])
            start = int(data[at + size_tag + size_length : at + entry])
            fields.append((tag, data[position + base + start : position + base + start + size - 1]))
            last = base + start + size
            at += entry
        if length == 0:  # records too long for five digits leave their length out
            length = last
        if length < 24:
            raise ValueError("damaged chart file: a record has no length")
        yield leader[6:7], fields
        position += length


def _attributes(body: bytes) -> dict[int, str]:
    found, at = {}, 0
    while at + 2 <= len(body):
        code = body[at] | body[at + 1] << 8
        end = body.find(bytes([UNIT_END]), at + 2)
        end = len(body) if end < 0 else end
        found[code] = body[at + 2 : end].decode("latin-1").strip()
        at = end + 1
    return found


def _number(text: str | None) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return float("nan")


def _pointers(body: bytes, size: int) -> list[tuple[int, int, int]]:
    """(kind of record, its number, the byte after the name) for each pointer in a field."""
    return [(body[at], int.from_bytes(body[at + 1 : at + 5], "little"), body[at + 7] if size == 9 else 0) for at in range(0, len(body) - size + 1, size)]


def read_chart(source: str | Path | bytes, name: str | None = None) -> Chart:
    """The depths of one chart cell, from its ``.000`` file or that file's bytes."""
    if isinstance(source, (str, Path)):
        name = name or Path(source).stem
        data = Path(source).read_bytes()
    else:
        data = bytes(source)
    name = name or "chart"
    if len(data) < 24 or not data[0:5].isdigit() or data[6:7] != b"L":
        raise ValueError(
            f"{name}: not a readable chart file. Charts sold encrypted (S-63, delivered with a PERMIT.TXT) "
            "cannot be read; an unencrypted S-57 cell is needed."
        )
    scale, datum, units, xy_factor, depth_factor = 0, 0, METRES, 1e7, 10.0
    nodes: dict[int, tuple[float, float]] = {}
    sounded: dict[int, np.ndarray] = {}
    edges: dict[int, tuple[list[int], np.ndarray]] = {}
    features: list[tuple[int, dict[int, str], list[tuple[int, int, int]]]] = []
    for kind, fields in _records(data):
        if kind == b"L":
            continue
        tags = dict(fields)
        if "DSPM" in tags and len(tags["DSPM"]) >= 24:
            datum, scale, units = tags["DSPM"][7], struct.unpack_from("<I", tags["DSPM"], 8)[0], tags["DSPM"][12]
            xy_factor, depth_factor = (float(v) or 1.0 for v in struct.unpack_from("<II", tags["DSPM"], 16))
        elif "VRID" in tags:
            record, number = tags["VRID"][0], struct.unpack_from("<I", tags["VRID"], 1)[0]
            flat = np.concatenate([np.frombuffer(body[: len(body) // 8 * 8], "<i4") for tag, body in fields if tag == "SG2D"] or [np.empty(0, "<i4")])
            points = flat.reshape(-1, 2)[:, ::-1] / xy_factor  # lon, lat
            if record == EDGE:
                ends = [0, 0]
                for tag, body in fields:
                    if tag == "VRPT":
                        for _, node, role in _pointers(body, 9):
                            if role in (1, 2):
                                ends[role - 1] = node
                edges[number] = (ends, points)
            elif len(points):
                nodes[number if record == CONNECTED_NODE else -number] = (points[0, 1], points[0, 0])
            deep = [np.frombuffer(body[: len(body) // 12 * 12], "<i4").reshape(-1, 3) for tag, body in fields if tag == "SG3D"]
            if deep:
                found = np.concatenate(deep).astype(float)
                sounded[number if record == CONNECTED_NODE else -number] = np.column_stack([found[:, 0] / xy_factor, found[:, 1] / xy_factor, found[:, 2] / depth_factor])
        elif "FRID" in tags:
            what = struct.unpack_from("<H", tags["FRID"], 7)[0]
            if what in (DEPTH_AREA, DEPTH_CONTOUR, DREDGED_AREA, SOUNDING, COVERAGE):
                pointers = [p for tag, body in fields if tag == "FSPT" for p in _pointers(body, 8)]
                features.append((what, _attributes(tags.get("ATTF", b"")), pointers))
    if units != METRES:
        raise ValueError(f"{name}: depths are not in metres (unit code {units}); only metric charts are read")

    def line(number: int) -> np.ndarray:
        """An edge with its end nodes, as rows of lat, lon."""
        ends, middle = edges[number]
        start = [nodes[ends[0]]] if ends[0] in nodes else []
        stop = [nodes[ends[1]]] if ends[1] in nodes else []
        return np.array(start + [(lat, lon) for lon, lat in middle] + stop, dtype=float).reshape(-1, 2)

    def boundary(pointers) -> np.ndarray:
        pieces = [line(number) for record, number, _ in pointers if record == EDGE and number in edges]
        pieces = [np.hstack([p[:-1], p[1:]]) for p in pieces if len(p) > 1]
        return np.concatenate(pieces) if pieces else np.empty((0, 4))

    chart = Chart(name, int(scale), int(datum), Soundings(np.empty(0), np.empty(0), np.empty(0)))
    spots = []
    for what, attributes, pointers in features:
        if what == SOUNDING:
            for record, number, _ in pointers:
                key = number if record == CONNECTED_NODE else -number
                if key in sounded:
                    spots.append(sounded[key])
        elif what == DEPTH_CONTOUR:
            depth = _number(attributes.get(CONTOUR_DEPTH))
            for record, number, _ in pointers:
                if record == EDGE and number in edges and not np.isnan(depth):
                    path = line(number)
                    chart.contours.append((depth, path[:, 0], path[:, 1]))
        elif what in (DEPTH_AREA, DREDGED_AREA):
            low, high = _number(attributes.get(DEPTH_FROM)), _number(attributes.get(DEPTH_TO))
            if what == DREDGED_AREA and np.isnan(high):
                high = low  # a dredged channel is kept at one depth
            edge = boundary(pointers)
            if len(edge) and not (np.isnan(low) and np.isnan(high)):
                chart.areas.append((low, high, edge))
        elif what == COVERAGE and attributes.get(COVERAGE_KIND, "1") == "1":
            edge = boundary(pointers)
            if len(edge):
                chart.coverage.append(edge)
    if spots:
        every = np.concatenate(spots)
        chart.soundings = Soundings(every[:, 0], every[:, 1], every[:, 2])
    return chart


def read_charts(paths: Iterable[str | Path]) -> list[Chart]:
    """Every chart cell in the given files, folders and zip archives (as NOAA delivers them)."""
    charts, updates = [], 0
    for path in map(Path, paths):
        if path.is_dir():
            files = sorted(path.rglob("*"))
            updates += sum(1 for f in files if f.suffix[1:].isdigit() and f.suffix != ".000" and len(f.suffix) == 4)
            charts += [read_chart(f) for f in files if f.suffix == ".000"]
        elif path.suffix.lower() == ".zip":
            with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
                for entry in sorted(archive.namelist()):
                    stem, _, ending = entry.rsplit("/", 1)[-1].rpartition(".")
                    if ending == "000":
                        charts.append(read_chart(archive.read(entry), stem))
                    elif len(ending) == 3 and ending.isdigit():
                        updates += 1
        else:
            charts.append(read_chart(path))
    if updates:
        warnings.warn(f"{updates} chart update files were not applied; depths are as the base cells were issued", stacklevel=2)
    return charts


def _inside(boundary_x: np.ndarray, boundary_y: np.ndarray, x0: float, y0: float, cell: float, rows: int, columns: int) -> np.ndarray:
    """Which cell centres of a grid lie inside a boundary, holes and all.

    ``boundary_x`` and ``boundary_y`` hold the two ends of each straight
    piece. A point is inside if a line from it to the far west crosses the
    boundary an odd number of times, so the pieces need no order.
    """
    low, high = np.minimum(boundary_y[:, 0], boundary_y[:, 1]), np.maximum(boundary_y[:, 0], boundary_y[:, 1])
    first = np.clip(np.ceil((low - y0) / cell), 0, rows).astype(int)
    stop = np.clip(np.ceil((high - y0) / cell), 0, rows).astype(int)  # rows whose centre is from low up to, not including, high
    count = np.maximum(stop - first, 0)
    piece = np.repeat(np.arange(len(count)), count)
    if piece.size == 0:
        return np.zeros((rows, columns), dtype=bool)
    row = np.arange(piece.size) - np.repeat(np.cumsum(count) - count, count) + first[piece]
    y = y0 + cell * row
    rise = boundary_y[piece, 1] - boundary_y[piece, 0]
    cross = boundary_x[piece, 0] + (y - boundary_y[piece, 0]) * (boundary_x[piece, 1] - boundary_x[piece, 0]) / rise
    column = np.clip(np.ceil((cross - x0) / cell), 0, columns).astype(int)
    flips = np.zeros((rows, columns + 1), dtype=np.int32)
    np.add.at(flips, (row, column), 1)
    return (np.cumsum(flips, axis=1)[:, :columns] % 2).astype(bool)


def _fill(target: np.ndarray, boundary: np.ndarray, value, free: np.ndarray | None, frame) -> None:
    """Set the cells inside a boundary (rows of lat, lon, lat, lon), working only on the part of the grid it spans."""
    lat0, lon0, x0, y0, cell = frame
    x_a, y_a = to_xy(boundary[:, 0], boundary[:, 1], lat0, lon0)
    x_b, y_b = to_xy(boundary[:, 2], boundary[:, 3], lat0, lon0)
    rows, columns = target.shape
    r0 = int(np.clip(np.floor((min(y_a.min(), y_b.min()) - y0) / cell), 0, rows))
    r1 = int(np.clip(np.ceil((max(y_a.max(), y_b.max()) - y0) / cell) + 1, 0, rows))
    c0 = int(np.clip(np.floor((min(x_a.min(), x_b.min()) - x0) / cell), 0, columns))
    c1 = int(np.clip(np.ceil((max(x_a.max(), x_b.max()) - x0) / cell) + 1, 0, columns))
    if r1 <= r0 or c1 <= c0:
        return
    mask = _inside(np.column_stack([x_a, x_b]), np.column_stack([y_a, y_b]), x0 + cell * c0, y0 + cell * r0, cell, r1 - r0, c1 - c0)
    if free is not None:
        mask &= free[r0:r1, c0:c1]
    target[r0:r1, c0:c1][mask] = value


def _along(lat: np.ndarray, lon: np.ndarray, spacing_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Points every ``spacing_m`` along a line, so a long straight contour is not just its two ends."""
    if len(lat) < 2:
        return lat, lon
    x, y = to_xy(lat, lon, float(lat[0]), float(lon[0]))
    travelled = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
    if travelled[-1] <= 0.0:
        return lat[:1], lon[:1]
    marks = np.linspace(0.0, travelled[-1], int(np.ceil(travelled[-1] / spacing_m)) + 1)
    return np.interp(marks, travelled, lat), np.interp(marks, travelled, lon)


def chart_depth_source(
    charts: Chart | Sequence[Chart],
    base: Callable | None = None,
    mean_sea_level_above_datum_m: float = 0.0,
    cell_m: float = 50.0,
    reach_m: float = 1000.0,
    shallowest_m: float = 1.0,
) -> Callable:
    """A ``depth_source`` from charts, laid over ``base`` (GEBCO, usually).

    ``mean_sea_level_above_datum_m`` is added to every chart depth to make
    it a depth below mean sea level; see the top of this module. ``cell_m``
    should be about the venue's cell size or a little finer.
    """
    charts = sorted([charts] if isinstance(charts, Chart) else list(charts), key=lambda c: c.scale or 10**9)
    if not charts:
        raise ValueError("no charts")
    rise = float(mean_sea_level_above_datum_m)
    every_lat = np.concatenate([np.concatenate([b[:, [0, 2]].ravel() for _, _, b in c.areas] + [b[:, [0, 2]].ravel() for b in c.coverage] + [c.soundings.lat] + [lat for _, lat, _ in c.contours]) for c in charts])
    every_lon = np.concatenate([np.concatenate([b[:, [1, 3]].ravel() for _, _, b in c.areas] + [b[:, [1, 3]].ravel() for b in c.coverage] + [c.soundings.lon] + [lon for _, _, lon in c.contours]) for c in charts])
    if every_lat.size == 0:
        raise ValueError("the charts hold no depth information")
    lat0, lon0 = 0.5 * (every_lat.min() + every_lat.max()), 0.5 * (every_lon.min() + every_lon.max())
    x, y = to_xy(every_lat, every_lon, lat0, lon0)
    width, height = x.max() - x.min() + 2 * cell_m, y.max() - y.min() + 2 * cell_m
    cell = max(float(cell_m), float(np.sqrt(width * height / MOST_CELLS)))
    x0, y0 = x.min() - cell, y.min() - cell
    rows, columns = int(np.ceil(height / cell)) + 1, int(np.ceil(width / cell)) + 1
    frame = (lat0, lon0, x0, y0, cell)

    def cell_of(lat, lon):
        at_x, at_y = to_xy(lat, lon, lat0, lon0)
        row, column = np.rint((at_y - y0) / cell).astype(int), np.rint((at_x - x0) / cell).astype(int)
        return row, column, (row >= 0) & (row < rows) & (column >= 0) & (column < columns)

    low, high = np.full((rows, columns), np.nan), np.full((rows, columns), np.nan)
    taken = np.zeros((rows, columns), dtype=bool)  # already covered by a more detailed chart
    points = []
    for chart in charts:
        free = ~taken
        lines = [(depth, *_along(lat, lon, cell)) for depth, lat, lon in chart.contours]
        lats = [chart.soundings.lat] + [lat for _, lat, _ in lines]
        lons = [chart.soundings.lon] + [lon for _, _, lon in lines]
        depths = [chart.soundings.depth] + [np.full(len(lat), depth) for depth, lat, _ in lines]
        lat, lon, depth = np.concatenate(lats), np.concatenate(lons), np.concatenate(depths)
        row, column, on_grid = cell_of(lat, lon)
        keep = on_grid.copy()
        keep[on_grid] = free[row[on_grid], column[on_grid]]
        points.append(Soundings(lat[keep], lon[keep], depth[keep] + rise))
        for shallow, deep, edge in chart.areas:
            if not np.isnan(shallow):
                _fill(low, edge, shallow + rise, free, frame)
            if not np.isnan(deep):
                _fill(high, edge, deep + rise, free, frame)
        covered = np.zeros((rows, columns), dtype=bool)
        for edge in chart.coverage or [b for _, _, b in chart.areas]:
            _fill(covered, edge, True, None, frame)
        taken |= covered

    def limits(at_lat, at_lon):
        at_lat, at_lon = np.asarray(at_lat, dtype=float), np.asarray(at_lon, dtype=float)
        row, column, on_grid = cell_of(at_lat, at_lon)
        shallow, deep = np.full(at_lat.shape, np.nan), np.full(at_lat.shape, np.nan)
        shallow[on_grid], deep[on_grid] = low[row[on_grid], column[on_grid]], high[row[on_grid], column[on_grid]]
        return shallow, deep

    found = Soundings.join(points)
    if len(found) == 0:
        if base is None:
            raise ValueError("the charts hold no soundings or contours, and there is no base to hold between their depth areas")

        def source(at_lat, at_lon):
            under = np.asarray(base(at_lat, at_lon), dtype=float)
            shallow, deep = limits(at_lat, at_lon)
            under = np.where(np.isnan(shallow), under, np.maximum(under, shallow))
            under = np.where(np.isnan(deep), under, np.minimum(under, deep))
            return np.where(np.isnan(under), np.nan, np.maximum(under, shallowest_m))

        return source
    return depth_source_from_soundings(found, base, cell_m=cell, reach_m=reach_m, limits=limits, shallowest_m=shallowest_m)
