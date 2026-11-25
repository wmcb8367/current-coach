"""Publish the models as maps the app can draw.

The app shows a venue's current as an animated map and lets the sailor
switch between models. It cannot run the models, so each evening this module
runs them and writes what the app needs: for every model and every day, the
current on a regular grid of latitude and longitude at each hour, packed
small; and one manifest that says which models and days exist.

Each day's file is built the evening before from forecasts, the same way
:mod:`currentmodel.dayahead` does. That is what makes the app's validation
honest: when a reading is compared with a model, it is compared with what
the model said before the day began.

The models published:

``day-ahead``   the venue physics model run on the forecasts, plus the share
                of the surface water that follows the wind (the default)
``physics``     the venue physics model alone: tide, weather-driven level,
                coastal drift, local wind on the whole water column
``tide``        the venue physics model with the tide only
``global``      NOAA's global ocean model as published
``wind``        the wind-following share alone, the same everywhere

File format (little-endian), one file per model and UTC day::

    bytes 0-3    "CCM1"
    4-5          rows (south to north)        uint16
    6-7          columns (west to east)       uint16
    8-9          frames                       uint16
    10-11        flags; bit 0: the rest is raw DEFLATE   uint16
    12-15        metres per second per unit   float32
    16-          for each frame: east-going plane, then north-going plane,
                 each rows x columns int16, row 0 the southernmost;
                 -32768 where there is no water or no answer

Frames are hourly from 00:00 to 24:00 UTC (25 of them), so a file answers
for any time of its day on its own.

Run it each evening, to a folder or to the server that hands the maps to
the app::

    python -m currentmodel.publish --venue hong-kong --name "Hong Kong" \\
        --lat 22.28 --lon 114.36 --width-km 44 --height-km 38 --out maps --cache cache

    CURRENT_MODELS_UPLOAD_TOKEN=... python -m currentmodel.publish ... --server https://example.com

``--days 2026-11-23,2026-11-24`` builds past days instead, each from what
had been issued the evening before it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import struct
import time as _time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np

from .build import SPINUP_S, build_suite
from .correction import Conditions
from .dayahead import WIND_AVERAGE_S, WIND_FRACTION, WIND_SOURCES, assemble
from .models import WindDrift
from .wind import forecast_wind

HOUR_S = 3600.0
DAY_S = 86400.0
MAGIC = b"CCM1"
MISSING = -32768
SCALE = 0.005  # m/s per unit: steps of 0.01 kn, a tenth of what a reading can resolve, and a third the size of finer steps
FRAMES_PER_DAY = 25
FLAG_DEFLATE = 1
KEEP_DAYS = 10
LEAD_IN_S = 12 * HOUR_S
API_PATH = "/api/current-models"

ATTRIBUTION = [
    "NOAA STOFS-2D-Global ocean model",
    "ECMWF open data (IFS, AIFS), CC BY 4.0",
    "NOAA GFS weather model",
    "GEBCO 2026 Grid depths",
    "Coastline from Mapzen terrain tiles",
]


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    summary: str
    factors: tuple[str, ...]


MODELS = (
    ModelInfo(
        "day-ahead",
        "Physics + wind",
        "Our physics model of the venue run on the evening's forecasts, plus the surface water the wind pushes along.",
        ("tide", "coastline and depths", "weather-driven sea level", "coastal drift", "forecast wind"),
    ),
    ModelInfo(
        "physics",
        "Physics only",
        "Our physics model of the venue without the wind-pushed surface layer.",
        ("tide", "coastline and depths", "weather-driven sea level", "coastal drift"),
    ),
    ModelInfo(
        "tide",
        "Tide only",
        "Our physics model driven by the tide alone. What a tide atlas would show.",
        ("tide", "coastline and depths"),
    ),
    ModelInfo(
        "global",
        "NOAA global",
        "NOAA's worldwide ocean model as published, about 3 km between points outside the United States.",
        ("tide", "wind", "air pressure"),
    ),
    ModelInfo(
        "wind",
        "Wind drift only",
        "Two percent of the forecast wind, the same everywhere. The simplest model that follows the wind.",
        ("forecast wind",),
    ),
)
DEFAULT_MODEL = "day-ahead"


@dataclass(frozen=True)
class MapGrid:
    """A regular grid of latitude and longitude: cell centres from south-west to north-east."""

    south: float
    north: float
    west: float
    east: float
    rows: int
    cols: int

    @classmethod
    def around(cls, lat: float, lon: float, width_m: float, height_m: float, step_m: float = 300.0) -> "MapGrid":
        half_lat = 0.5 * height_m / 111_320.0
        half_lon = 0.5 * width_m / (111_320.0 * np.cos(np.radians(lat)))
        rows = max(2, int(round(height_m / step_m)) + 1)
        cols = max(2, int(round(width_m / step_m)) + 1)
        return cls(lat - half_lat, lat + half_lat, lon - half_lon, lon + half_lon, rows, cols)

    def points(self):
        """Latitude and longitude of every point, each of shape (rows, cols)."""
        lats = np.linspace(self.south, self.north, self.rows)
        lons = np.linspace(self.west, self.east, self.cols)
        return np.meshgrid(lats, lons, indexing="ij")

    def as_dict(self) -> dict:
        return {
            "south": round(self.south, 6), "north": round(self.north, 6),
            "west": round(self.west, 6), "east": round(self.east, 6),
            "rows": self.rows, "cols": self.cols,
        }


def day_start(day: str) -> float:
    """Epoch seconds of 00:00 UTC on ``day`` (YYYY-MM-DD)."""
    return dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()


def day_name(time: float) -> str:
    return dt.datetime.fromtimestamp(time, dt.timezone.utc).strftime("%Y-%m-%d")


def frame_times(day: str) -> np.ndarray:
    return day_start(day) + HOUR_S * np.arange(FRAMES_PER_DAY)


def sample(model, grid: MapGrid, times: Sequence[float], water: np.ndarray | None = None):
    """The model's current on the map grid at each time: two arrays (times, rows, cols) in m/s, NaN where none."""
    lats, lons = grid.points()
    u = np.full((len(times), grid.rows, grid.cols), np.nan)
    v = np.full_like(u, np.nan)
    for index, time in enumerate(times):
        a, b = model.velocity(lats, lons, np.full(lats.shape, float(time)))
        u[index], v[index] = a, b
    if water is not None:
        u[:, ~water] = np.nan
        v[:, ~water] = np.nan
    return u, v


def pack(u: np.ndarray, v: np.ndarray, compress: bool = True) -> bytes:
    """One day's file from arrays of shape (frames, rows, cols) in m/s."""
    if u.shape != v.shape or u.ndim != 3:
        raise ValueError("u and v must both have shape (frames, rows, cols)")
    frames, rows, cols = u.shape
    planes = np.stack([u, v], axis=1)  # frame, (u, v), row, col
    missing = ~np.isfinite(planes)
    units = np.clip(np.rint(np.where(missing, 0.0, planes) / SCALE), MISSING + 1, 32767).astype("<i2")
    units[missing] = MISSING
    body = units.tobytes()
    if compress:
        packer = zlib.compressobj(9, zlib.DEFLATED, -15)
        body = packer.compress(body) + packer.flush()
    header = MAGIC + struct.pack("<HHHHf", rows, cols, frames, FLAG_DEFLATE if compress else 0, SCALE)
    return header + body


def unpack(data: bytes):
    """The arrays (frames, rows, cols) in m/s, NaN where missing, from one day's file."""
    if data[:4] != MAGIC:
        raise ValueError("not a current map file")
    rows, cols, frames, flags, scale = struct.unpack("<HHHHf", data[4:16])
    body = data[16:]
    if flags & FLAG_DEFLATE:
        body = zlib.decompress(body, -15)
    units = np.frombuffer(body, dtype="<i2").reshape(frames, 2, rows, cols)
    values = np.where(units == MISSING, np.nan, units * float(scale))
    return values[:, 0], values[:, 1]


def merge_manifest(old: Mapping | None, new: Mapping, keep_days: int = KEEP_DAYS, today: str | None = None) -> dict:
    """The new manifest with the days of the old one that it does not replace.

    Days more than ``keep_days`` before ``today`` drop out: the app
    validates against the last week, and the store should not grow for ever.
    """
    merged = json.loads(json.dumps(new))
    before = {model["id"]: model for model in (old or {}).get("models", [])}
    first = day_name(day_start(today or day_name(_time.time())) - keep_days * DAY_S)
    for model in merged["models"]:
        days = {entry["day"]: entry for entry in before.get(model["id"], {}).get("days", [])}
        days.update({entry["day"]: entry for entry in model["days"]})
        model["days"] = [days[day] for day in sorted(days) if day >= first]
    return merged


def merge_index(old: Mapping | None, venue: Mapping) -> dict:
    """The list of venues with this one added or replaced."""
    venues = [entry for entry in (old or {}).get("venues", []) if entry.get("id") != venue["id"]]
    return {"version": 1, "venues": sorted(venues + [dict(venue)], key=lambda entry: entry["id"])}


class Folder:
    """Publish into a folder on disk, laid out as the server lays it out."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def get(self, path: str) -> bytes | None:
        file = self.root / path
        return file.read_bytes() if file.exists() else None

    def put(self, path: str, data: bytes) -> None:
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)


class Server:
    """Publish to the server that hands the maps to the app. ``token`` is its publishing token."""

    def __init__(self, base_url: str, token: str, tries: int = 4):
        self.base = base_url.rstrip("/") + API_PATH
        self.token = token
        self.tries = tries

    def _request(self, method: str, path: str, data: bytes | None = None) -> bytes | None:
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json" if path.endswith(".json") else "application/octet-stream"
        for attempt in range(self.tries):
            request = urllib.request.Request(f"{self.base}/{path}", data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return response.read()
            except urllib.error.HTTPError as problem:
                if problem.code == 404 and method == "GET":
                    return None
                if problem.code < 500 or attempt == self.tries - 1:
                    raise RuntimeError(f"{method} {path}: the server answered {problem.code}") from None
            except (urllib.error.URLError, TimeoutError):
                if attempt == self.tries - 1:
                    raise
            _time.sleep(2.0 * 2**attempt)
        return None

    def get(self, path: str) -> bytes | None:
        return self._request("GET", path)

    def put(self, path: str, data: bytes) -> None:
        self._request("PUT", path, data)


def models_for(suite, wind: Conditions, lat: float, lon: float) -> dict:
    """The published models from a built suite and the wind forecast."""
    return {
        "day-ahead": assemble(suite["venue-weather"], wind, lat, lon),
        "physics": suite["venue-weather"],
        "tide": suite["venue-tide"],
        "global": suite["global"],
        "wind": WindDrift(wind, lat, lon, WIND_FRACTION, average_s=WIND_AVERAGE_S),
    }


def issue_hour_utc(lon: float) -> int:
    """The UTC hour that is about 18:00 by the sun at this longitude: the evening's build."""
    return int(round(18.0 - lon / 15.0)) % 24


def issued_for(day: str, lon: float) -> float:
    """When the forecast for UTC day ``day`` is built: the venue's evening, on the UTC day before."""
    return day_start(day) - DAY_S + issue_hour_utc(lon) * HOUR_S


def build_days(
    lat: float,
    lon: float,
    width_m: float,
    height_m: float,
    days: Sequence[str],
    grid: MapGrid,
    issued_before: float | None = None,
    cache_dir: str | Path | None = None,
    wind_sources: Sequence[str] = WIND_SOURCES,
    log: Callable[[str], None] | None = None,
    **build_options,
) -> dict[str, dict[str, bytes]]:
    """Each model's file for each of ``days`` (consecutive UTC days), from one run of the models.

    Returns ``{model id: {day: file bytes}}``. With ``issued_before`` only
    forecasts issued by then are used.
    """
    log = log or (lambda message: None)
    cache = Path(cache_dir) if cache_dir else None
    start = day_start(days[0]) - LEAD_IN_S
    end = day_start(days[-1]) + DAY_S
    log(f"reading the wind forecast ({', '.join(wind_sources)})")
    first = start - SPINUP_S - WIND_AVERAGE_S
    try:
        wind = forecast_wind(lat, lon, first, end, tuple(wind_sources), cache / "wind" if cache else None, issued_before, log)
    except FileNotFoundError:
        log("  none of them could be read; using GFS")
        wind = forecast_wind(lat, lon, first, end, ("gfs",), cache / "wind" if cache else None, issued_before, log)
    suite, _ = build_suite(
        lat, lon, width_m, height_m, start, end,
        cache_dir=cache, wind=wind, models=("global", "venue-tide", "venue-weather"),
        log=log, issued_before=issued_before, **build_options,
    )
    models = models_for(suite, wind, lat, lon)
    lats, lons = grid.points()
    probe = day_start(days[0]) + 12 * HOUR_S
    water = np.isfinite(suite["venue-weather"].velocity(lats, lons, np.full(lats.shape, probe))[0])
    files: dict[str, dict[str, bytes]] = {info.id: {} for info in MODELS}
    for day in days:
        for info in MODELS:
            u, v = sample(models[info.id], grid, frame_times(day), water)
            files[info.id][day] = pack(u, v)
    return files


def manifest_for(venue: Mapping, grid: MapGrid, files: Mapping[str, Mapping[str, bytes]], kinds: Mapping[str, str], issued: Mapping[str, float]) -> dict:
    return {
        "version": 1,
        "venue": {**venue, **grid.as_dict()},
        "frameSeconds": int(HOUR_S),
        "framesPerDay": FRAMES_PER_DAY,
        "updated": int(_time.time()),
        "default": DEFAULT_MODEL,
        "models": [
            {
                "id": info.id, "name": info.name, "summary": info.summary, "factors": list(info.factors),
                "days": [
                    {"day": day, "kind": kinds[day], "issued": int(issued[day]), "bytes": len(data)}
                    for day, data in sorted(files[info.id].items())
                ],
            }
            for info in MODELS
        ],
        "attribution": ATTRIBUTION,
    }


def publish(target, venue: Mapping, manifest: Mapping, files: Mapping[str, Mapping[str, bytes]], log: Callable[[str], None] | None = None) -> dict:
    """Write the day files, then the venue's manifest and the list of venues. Returns the merged manifest."""
    log = log or (lambda message: None)
    for model, by_day in files.items():
        for day, data in sorted(by_day.items()):
            target.put(f"{venue['id']}/{model}/{day}.bin", data)
            log(f"  {model} {day}: {len(data) / 1000:.0f} kB")
    path = f"{venue['id']}/manifest.json"
    old = target.get(path)
    merged = merge_manifest(json.loads(old) if old else None, manifest)
    target.put(path, json.dumps(merged, separators=(",", ":")).encode())
    old = target.get("index.json")
    index = merge_index(json.loads(old) if old else None, merged["venue"])
    target.put("index.json", json.dumps(index, separators=(",", ":")).encode())
    return merged


def run(
    target,
    venue_id: str,
    name: str,
    lat: float,
    lon: float,
    width_m: float,
    height_m: float,
    days: Sequence[str] | None = None,
    now: float | None = None,
    step_m: float = 300.0,
    time_zone: str | None = None,
    cache_dir: str | Path | None = None,
    log: Callable[[str], None] | None = None,
    **build_options,
) -> dict:
    """Build and publish a venue.

    Without ``days`` this is the evening's run: tomorrow and the day after
    from the forecasts in hand now (and today too, the first time, marked as
    built after the fact). With ``days`` each past day is built from what
    had been issued the evening before it.
    """
    log = log or (lambda message: None)
    now = _time.time() if now is None else now
    grid = MapGrid.around(lat, lon, width_m, height_m, step_m)
    venue = {"id": venue_id, "name": name, "lat": lat, "lon": lon}
    if time_zone:
        venue["timeZone"] = time_zone
    common = dict(cache_dir=cache_dir, log=log, **build_options)
    files: dict[str, dict[str, bytes]] = {info.id: {} for info in MODELS}
    kinds: dict[str, str] = {}
    issued: dict[str, float] = {}
    if days:
        for day in days:
            at = issued_for(day, lon)
            log(f"{day}: from forecasts issued before {dt.datetime.fromtimestamp(at, dt.timezone.utc):%Y-%m-%d %H:%M} UTC")
            built = build_days(lat, lon, width_m, height_m, [day], grid, issued_before=min(at, now), **common)
            for model, by_day in built.items():
                files[model].update(by_day)
            kinds[day], issued[day] = "forecast", min(at, now)
    else:
        today = day_name(now)
        ahead = [day_name(day_start(today) + DAY_S), day_name(day_start(today) + 2 * DAY_S)]
        old = target.get(f"{venue_id}/manifest.json")
        have = {entry["day"] for model in (json.loads(old)["models"] if old else []) for entry in model["days"]}
        wanted = ([] if today in have else [today]) + ahead
        log(f"building {', '.join(wanted)} from the forecasts in hand")
        files = build_days(lat, lon, width_m, height_m, wanted, grid, **common)
        for day in wanted:
            kinds[day], issued[day] = ("analysis" if day == today else "forecast"), now
    return publish(target, venue, manifest_for(venue, grid, files, kinds, issued), files, log)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build a venue's current models and publish them as maps for the app.")
    parser.add_argument("--venue", required=True, help="short id, lower case letters, digits and dashes (hong-kong)")
    parser.add_argument("--name", required=True, help="the venue's name as the app shows it")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--width-km", type=float, required=True)
    parser.add_argument("--height-km", type=float, required=True)
    parser.add_argument("--time-zone", default=None, help="the venue's time zone (Asia/Hong_Kong)")
    parser.add_argument("--days", default=None, help="past UTC days to build, comma separated (YYYY-MM-DD); default is the evening's run")
    parser.add_argument("--step-m", type=float, default=300.0, help="spacing of the published map in metres")
    parser.add_argument("--dx", type=float, default=200.0, help="cell size of the venue physics model in metres")
    parser.add_argument("--out", default=None, help="folder to publish into")
    parser.add_argument("--server", default=None, help="server to publish to; its token is read from CURRENT_MODELS_UPLOAD_TOKEN")
    parser.add_argument("--cache", default=None, help="folder to keep downloaded data in")
    args = parser.parse_args(argv)

    if bool(args.out) == bool(args.server):
        parser.error("give either --out or --server")
    if args.server:
        token = os.environ.get("CURRENT_MODELS_UPLOAD_TOKEN", "")
        if not token:
            parser.error("CURRENT_MODELS_UPLOAD_TOKEN is not set")
        target = Server(args.server, token)
    else:
        target = Folder(args.out)
    days = [day.strip() for day in args.days.split(",") if day.strip()] if args.days else None
    manifest = run(
        target, args.venue, args.name, args.lat, args.lon, args.width_km * 1000.0, args.height_km * 1000.0,
        days=days, step_m=args.step_m, time_zone=args.time_zone, cache_dir=args.cache, dx=args.dx, log=print,
    )
    count = sum(len(model["days"]) for model in manifest["models"])
    print(f"published {args.venue}: {len(manifest['models'])} models, {count} day files in the manifest")


if __name__ == "__main__":
    main()
