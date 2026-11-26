"""Read the depths a boat's own sounder recorded.

Most chart plotters can log what the sounder sees to a memory card, and
that log is the one free source of real soundings on the water a team
actually sails. This is also how the commercial "community" charts are
made: boats upload these same files. Four kinds are read here:

``.sl2`` and ``.sl3``
    Sonar logs from Lowrance, Simrad and B&G plotters. Each ping carries the
    depth, the position and the time.
``.gpx``
    A track with a depth on each point, as Garmin plotters and several apps
    save them.
``.csv``
    Any table with latitude, longitude and depth columns, and a time column
    if there is one. The files of the IHO crowdsourced bathymetry archive
    are this.
NMEA 0183 text (``.nmea``, ``.log``, ``.txt``)
    What a data logger on the boat's instrument network records: position
    and time sentences with depth sentences among them.

All of them give :class:`~currentmodel.soundings.Soundings` with depths as
the sounder reported them: from the transducer (or wherever the instrument
was set to measure from) to the bottom, at the tide of the moment. Pass them
through :func:`~currentmodel.soundings.reduce_to_mean_sea_level` before use.

The sonar log layouts are not published by the maker. They follow the
open-source readers ``sonarlight`` and ``sllib`` (both MIT licensed) and the
OpenStreetMap wiki's description of the format.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import numpy as np

from .soundings import Soundings

FOOT_M = 0.3048
LOWRANCE_RADIUS_M = 6356752.3142  # the sphere the plotters project positions on
# Byte offsets inside a ping's header: its size, depth (feet), east, north, clock (s) and time into the log (ms).
SL2 = {"size": 28, "depth": 64, "east": 108, "north": 112, "clock": 60, "elapsed": 140, "header": 144}
SL3 = {"size": 8, "depth": 48, "east": 92, "north": 96, "clock": 40, "elapsed": 124, "header": 128}
EARLIEST_CLOCK = dt.datetime(2005, 1, 1, tzinfo=dt.timezone.utc).timestamp()


def read_sonar_log(path: str | Path, start_time: float | None = None) -> Soundings:
    """Soundings from one log file of any kind listed above, chosen by its extension."""
    path = Path(path)
    kind = path.suffix.lower()
    if kind in (".sl2", ".sl3"):
        return read_lowrance(path.read_bytes(), start_time)
    if kind == ".gpx":
        return read_gpx(path.read_bytes())
    if kind == ".csv":
        return read_csv(path.read_text(encoding="utf-8", errors="replace"))
    if kind in (".nmea", ".log", ".txt"):
        return read_nmea(path.read_text(encoding="ascii", errors="replace"))
    raise ValueError(f"{path.name}: not a kind of depth log this reads (.sl2, .sl3, .gpx, .csv, .nmea)")


def read_lowrance(data: bytes, start_time: float | None = None) -> Soundings:
    """Soundings from the bytes of a ``.sl2`` or ``.sl3`` sonar log.

    A log holds several pings for each moment (one per sonar channel), all
    with the same depth; one is kept. The time of a ping is the plotter's
    clock at the start of the log plus the time into the log. Where the
    plotter had no clock set, give ``start_time`` (Unix seconds, UTC) or the
    soundings come back without times.
    """
    if len(data) < 8:
        raise ValueError("not a sonar log: too short")
    kind = int.from_bytes(data[0:2], "little")
    if kind not in (2, 3):
        raise ValueError("not a .sl2 or .sl3 sonar log")
    at = SL2 if kind == 2 else SL3
    view = np.frombuffer(data, dtype=np.uint8)
    starts, position = [], 8
    while position + at["header"] <= len(data):
        size = int.from_bytes(data[position + at["size"] : position + at["size"] + 2], "little")
        if size < at["header"]:
            break
        starts.append(position)
        position += size
    if not starts:
        raise ValueError("the sonar log holds no pings")
    starts = np.array(starts)

    def field(name: str, kind: str) -> np.ndarray:
        offsets = starts[:, None] + at[name] + np.arange(4)[None, :]
        return view[offsets].copy().view(kind).ravel()

    depth = field("depth", "<f4").astype(float) * FOOT_M
    east, north = field("east", "<i4").astype(float), field("north", "<i4").astype(float)
    elapsed = field("elapsed", "<u4").astype(float) / 1000.0
    clock = float(field("clock", "<u4")[0])
    lon = np.degrees(east / LOWRANCE_RADIUS_M)
    lat = np.degrees(2.0 * np.arctan(np.exp(north / LOWRANCE_RADIUS_M)) - 0.5 * np.pi)
    if start_time is None and clock >= EARLIEST_CLOCK:
        start_time = clock
    time = np.full(depth.shape, np.nan) if start_time is None else start_time + elapsed
    good = np.isfinite(depth) & (depth > 0.0) & ((east != 0.0) | (north != 0.0)) & (np.abs(lat) < 89.0)
    _, first = np.unique(elapsed, return_index=True)
    one_each = np.zeros(depth.shape, dtype=bool)
    one_each[first] = True
    return Soundings(lat, lon, depth, time).keep(good & one_each)


def _when(text: str) -> float:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        stamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return float("nan")
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)).timestamp()


def read_gpx(data: bytes) -> Soundings:
    """Soundings from a GPX track whose points carry a depth in metres."""
    lat, lon, depth, time = [], [], [], []
    for _, node in ElementTree.iterparse(io.BytesIO(data)):
        if node.tag.rsplit("}", 1)[-1] not in ("trkpt", "wpt", "rtept"):
            continue
        found, stamp = None, float("nan")
        for part in node.iter():
            name = part.tag.rsplit("}", 1)[-1].lower()
            if name in ("depth", "waterdepth") and part.text:
                try:
                    found = float(part.text)
                except ValueError:
                    pass
            elif name == "time" and part.text:
                stamp = _when(part.text)
        if found is not None and found > 0.0:
            lat.append(float(node.get("lat")))
            lon.append(float(node.get("lon")))
            depth.append(found)
            time.append(stamp)
        node.clear()
    return Soundings(lat, lon, depth, time)


def read_csv(text: str) -> Soundings:
    """Soundings from a table with latitude, longitude and depth columns (metres), and a time column if any."""
    rows = csv.reader(io.StringIO(text.lstrip("﻿")))
    header = [name.strip().lower() for name in next(rows, [])]

    def column(*names: str) -> int | None:
        return next((header.index(name) for name in names if name in header), None)

    at_lat, at_lon = column("lat", "latitude", "y"), column("lon", "longitude", "lng", "long", "x")
    at_depth, at_time = column("depth", "depth_m", "depth (m)", "depth(m)"), column("time", "timestamp", "datetime", "date")
    if at_lat is None or at_lon is None or at_depth is None:
        raise ValueError("the table needs latitude, longitude and depth columns")
    lat, lon, depth, time = [], [], [], []
    for cells in rows:
        try:
            values = float(cells[at_lat]), float(cells[at_lon]), float(cells[at_depth])
        except (ValueError, IndexError):
            continue
        lat.append(values[0])
        lon.append(values[1])
        depth.append(values[2])
        time.append(_when(cells[at_time]) if at_time is not None and at_time < len(cells) else float("nan"))
    found = Soundings(lat, lon, depth, time)
    return found.keep(found.depth > 0.0)


def _degrees(value: str, side: str) -> float:
    whole = int(float(value) // 100)
    degrees = whole + (float(value) - 100 * whole) / 60.0
    return -degrees if side in ("S", "W") else degrees


def _checked(line: str) -> list[str] | None:
    line = line.strip()
    start = line.find("$")
    if start < 0:
        return None
    body, star, check = line[start + 1 :].partition("*")
    if star:
        total = 0
        for letter in body:
            total ^= ord(letter)
        try:
            if total != int(check[:2], 16):
                return None
        except ValueError:
            return None
    return body.split(",")


def read_nmea(text: str) -> Soundings:
    """Soundings from logged NMEA 0183 sentences.

    Each depth (DPT or DBT) takes the last position before it. Times come
    from RMC sentences, which carry the date; a log without them gives
    soundings without times. DPT depths include the offset the instrument
    was set up with, so they may already be from the waterline or the keel.
    """
    lat, lon, depth, time = [], [], [], []
    here, day, now = None, None, float("nan")
    for line in text.splitlines():
        cells = _checked(line)
        if not cells or len(cells[0]) < 5:
            continue
        kind = cells[0][2:5]
        try:
            if kind == "RMC" and len(cells) > 9 and cells[2] == "A":
                here = _degrees(cells[3], cells[4]), _degrees(cells[5], cells[6])
                clock, date = cells[1], cells[9]
                day = dt.datetime(2000 + int(date[4:6]), int(date[2:4]), int(date[0:2]), tzinfo=dt.timezone.utc).timestamp()
                now = day + 3600 * int(clock[0:2]) + 60 * int(clock[2:4]) + float(clock[4:])
            elif kind == "GGA" and len(cells) > 6 and cells[6] not in ("", "0"):
                here = _degrees(cells[2], cells[3]), _degrees(cells[4], cells[5])
                if day is not None:
                    clock = cells[1]
                    stamp = day + 3600 * int(clock[0:2]) + 60 * int(clock[2:4]) + float(clock[4:])
                    now = stamp + 86400.0 if stamp < now - 43200.0 else stamp
            elif kind == "DPT" and len(cells) > 1 and here is not None:
                found = float(cells[1]) + (float(cells[2]) if len(cells) > 2 and cells[2] else 0.0)
            elif kind == "DBT" and len(cells) > 3 and here is not None:
                found = float(cells[3]) if cells[3] else float(cells[1]) * FOOT_M
            else:
                continue
        except (ValueError, IndexError):
            continue
        if kind in ("DPT", "DBT") and found > 0.0:
            lat.append(here[0])
            lon.append(here[1])
            depth.append(found)
            time.append(now)
    return Soundings(lat, lon, depth, time)
