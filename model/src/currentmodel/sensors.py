"""Find and read every open sensor around a venue, anywhere in the world.

When a venue is added, :func:`discover` asks each catalogue below which of
its stations lie within reach and writes them to a manifest; :func:`collect`
then reads their records for any period into one hourly table, which is what
the driver screening (:mod:`currentmodel.drivers`) and the models use.
Nothing here is built for one venue: a catalogue that covers only one
country (Hong Kong's Observatory and Marine Department, NOAA's US stations)
simply finds nothing elsewhere.

What each catalogue gives:

=============  =========================================================  ==========================
catalogue      measures                                                   record
=============  =========================================================  ==========================
``ghcnh``      wind, sea-level air pressure, rain at weather stations      decades, hourly, worldwide
``uhslc``      sea level at tide gauges (research and fast delivery)       decades, hourly, worldwide
``ioc``        sea level at tide gauges, near real time                    about 1 to 15 min, worldwide
``coops``      US tide gauges and current meters (NOAA CO-OPS)             minutes, years back
``ndbc``       buoys: wind, pressure, waves, some currents                 last 45 days, worldwide
``glofas``     river discharge at river mouths (GloFAS, past and forecast) daily, worldwide
``hko``        Hong Kong Observatory tide gauges and rain                  latest values
``mardep``     Hong Kong Marine Department tide gauges                     latest values
=============  =========================================================  ==========================

Every series comes back as ``(times, values)`` with times in epoch seconds
(UTC) and values in SI units: metres, m/s (wind and current as east and
north parts), hPa, mm per hour, m3/s. The latest-only feeds have no history
to download, so :func:`record_latest` appends their current values to a
file each time it runs; run it on a schedule from the day a venue is added
and they grow a history.
"""

from __future__ import annotations

import csv
import hashlib
import datetime as dt
import io
import json
import math
import time as _time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

USER_AGENT = "currentmodel-sensors/0.1"
EARTH_KM = 6371.0


@dataclass
class Sensor:
    """One station of one catalogue, with what it measures."""

    catalogue: str
    id: str
    name: str
    lat: float
    lon: float
    measures: list[str]            # e.g. ["wind", "pressure", "rain"], ["sea_level"], ["current"]
    distance_km: float = 0.0
    history: bool = True           # False for feeds that give only the latest value
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.catalogue}:{self.id}"


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(np.asarray(lon2) - lon1) / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: str) -> float:
    text = text.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.timestamp()


class Fetcher:
    """HTTP with a file cache and polite retries. Pass ``cache_dir=None`` to cache nothing."""

    def __init__(self, cache_dir: str | Path | None = None, tries: int = 3, timeout: float = 90.0):
        self.cache = Path(cache_dir) if cache_dir else None
        self.tries, self.timeout = tries, timeout

    def get(self, url: str, max_age_s: float | None = None) -> bytes:
        path = None
        if self.cache:
            name = urllib.parse.quote(url, safe="")[:200] + "-" + hashlib.sha1(url.encode()).hexdigest()[:12]
            path = self.cache / name
            if path.exists() and (max_age_s is None or _time.time() - path.stat().st_mtime < max_age_s):
                return path.read_bytes()
        problem = None
        for attempt in range(self.tries):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = response.read()
                if path:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                return data
            except urllib.error.HTTPError as error:
                problem = error
                if error.code == 404:
                    break
                _time.sleep(2.0 * (attempt + 1) * (5 if error.code == 429 else 1))
            except Exception as error:  # network trouble: try again
                problem = error
                _time.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"could not read {url}: {problem}")


# --------------------------------------------------------------------------- GHCNh (NCEI): weather stations


GHCNH_LIST = "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/doc/ghcnh-station-list.csv"
GHCNH_YEAR = "https://www.ncei.noaa.gov/oa/global-historical-climatology-network/hourly/access/by-year/{year}/psv/GHCNh_{id}_{year}.psv"


def ghcnh_stations(text: str, lat: float, lon: float, radius_km: float) -> list[Sensor]:
    found = []
    for row in csv.reader(io.StringIO(text)):
        try:
            slat, slon = float(row[1]), float(row[2])
        except (ValueError, IndexError):
            continue
        km = float(distance_km(lat, lon, slat, slon))
        if km <= radius_km:
            found.append(Sensor("ghcnh", row[0], row[5].strip().title(), slat, slon, ["wind", "pressure", "rain"], km,
                                extra={"elevation_m": float(row[3]) if row[3] else None}))
    return found


def parse_ghcnh(text: str) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Wind (east, north in m/s, the way it blows towards), sea-level pressure (hPa) and rain (mm) from a GHCNh file."""
    rows = list(csv.reader(io.StringIO(text), delimiter="|"))
    if len(rows) < 2:
        return {}
    head = {name: i for i, name in enumerate(rows[0])}

    def number(row, name):
        i = head.get(name)
        if i is None or i >= len(row) or not row[i].strip():
            return math.nan
        try:
            return float(row[i])
        except ValueError:
            return math.nan

    times, wu, wv, slp, rain = [], [], [], [], []
    for row in rows[1:]:
        if len(row) < len(head) // 2:
            continue
        times.append(_epoch(row[head["DATE"]]))
        speed, frm = number(row, "wind_speed"), number(row, "wind_direction")
        if frm == 0 and speed == 0:
            frm = 0.0
        towards = math.radians((frm + 180.0) % 360.0) if not math.isnan(frm) else math.nan
        wu.append(speed * math.sin(towards))
        wv.append(speed * math.cos(towards))
        slp.append(number(row, "sea_level_pressure"))
        rain.append(number(row, "precipitation"))
    t = np.asarray(times)
    order = np.argsort(t)
    t = t[order]
    out = {"wind_u": (t, np.asarray(wu)[order]), "wind_v": (t, np.asarray(wv)[order]),
           "pressure": (t, np.asarray(slp)[order]), "rain": (t, np.asarray(rain)[order])}
    return {k: (tt, vv) for k, (tt, vv) in out.items() if np.isfinite(vv).any()}


# --------------------------------------------------------------------------- UHSLC: tide gauges, hourly, decades


UHSLC_ERDDAP = "https://uhslc.soest.hawaii.edu/erddap/tabledap/global_hourly_fast.csv"


def uhslc_stations(text: str, lat: float, lon: float, radius_km: float) -> list[Sensor]:
    found = []
    for row in list(csv.reader(io.StringIO(text)))[2:]:
        try:
            slat, slon = float(row[2]), float(row[3])
        except (ValueError, IndexError):
            continue
        slon = slon - 360.0 if slon > 180.0 else slon
        km = float(distance_km(lat, lon, slat, slon))
        if km <= radius_km:
            found.append(Sensor("uhslc", row[0], row[1], slat, slon, ["sea_level"], km))
    return found


def parse_erddap_level(text: str, scale: float = 0.001) -> tuple[np.ndarray, np.ndarray]:
    rows = list(csv.reader(io.StringIO(text)))[2:]
    t = np.array([_epoch(r[0]) for r in rows])
    v = np.array([float(r[1]) * scale if r[1] not in ("", "NaN") else math.nan for r in rows])
    return t, v


# --------------------------------------------------------------------------- IOC sea level monitoring: near real time


IOC_BASE = "https://www.ioc-sealevelmonitoring.org/service.php"


def ioc_stations(text: str, lat: float, lon: float, radius_km: float) -> list[Sensor]:
    found, seen = [], set()
    for s in json.loads(text):
        try:
            slat, slon = float(s["Lat"]), float(s["Lon"])
        except (TypeError, ValueError, KeyError):
            continue
        km = float(distance_km(lat, lon, slat, slon))
        if km <= radius_km and s["Code"] not in seen:
            seen.add(s["Code"])
            found.append(Sensor("ioc", s["Code"], s.get("Location") or s["Code"], slat, slon, ["sea_level"], km,
                                extra={"country": s.get("country"), "sensor": s.get("sensor")}))
    return found


def parse_ioc(text: str) -> tuple[np.ndarray, np.ndarray]:
    items = json.loads(text) if text.strip() else []
    if not items:
        return np.zeros(0), np.zeros(0)
    # A station can report from several sensors: keep the commonest one.
    sensors = [i.get("sensor") for i in items]
    main = max(set(sensors), key=sensors.count)
    items = [i for i in items if i.get("sensor") == main]
    t = np.array([_epoch(i["stime"]) for i in items])
    v = np.array([float(i["slevel"]) if i["slevel"] is not None else math.nan for i in items])
    return t, v


# --------------------------------------------------------------------------- NOAA CO-OPS (US): tide gauges and current meters


COOPS_STATIONS = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type={kind}"
COOPS_DATA = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


def coops_stations(texts: dict[str, str], lat: float, lon: float, radius_km: float) -> list[Sensor]:
    found = []
    for kind, text in texts.items():
        for s in json.loads(text).get("stations", []):
            km = float(distance_km(lat, lon, float(s["lat"]), float(s["lng"])))
            if km <= radius_km:
                measures = ["current"] if kind == "currents" else ["sea_level"]
                found.append(Sensor("coops", str(s["id"]), s.get("name", ""), float(s["lat"]), float(s["lng"]), measures, km,
                                    extra={"kind": kind}))
    return found


def parse_coops(text: str, kind: str) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    data = json.loads(text).get("data", [])
    t = np.array([_epoch(d["t"]) for d in data])

    def number(d, name):
        try:
            return float(d.get(name))
        except (TypeError, ValueError):
            return math.nan

    if kind == "wind":  # metric: m/s, direction it blows from
        speed = np.array([number(d, "s") for d in data])
        towards = np.radians((np.array([number(d, "d") for d in data]) + 180.0) % 360.0)
        return {"wind_u": (t, speed * np.sin(towards)), "wind_v": (t, speed * np.cos(towards))}
    if kind == "air_pressure":
        return {"pressure": (t, np.array([number(d, "v") for d in data]))}
    if kind == "currents":
        speed = np.array([float(d["s"]) * 0.514444 if d.get("s") not in (None, "") else math.nan for d in data])  # knots
        towards = np.radians([float(d["d"]) if d.get("d") not in (None, "") else math.nan for d in data])
        return {"current_u": (t, speed * np.sin(towards)), "current_v": (t, speed * np.cos(towards))}
    return {"sea_level": (t, np.array([float(d["v"]) if d.get("v") not in (None, "") else math.nan for d in data]))}


# --------------------------------------------------------------------------- NDBC buoys


NDBC_ACTIVE = "https://www.ndbc.noaa.gov/activestations.xml"
NDBC_REALTIME = "https://www.ndbc.noaa.gov/data/realtime2/{id}.txt"


def ndbc_stations(text: str, lat: float, lon: float, radius_km: float) -> list[Sensor]:
    import xml.etree.ElementTree as ET

    found = []
    for s in ET.fromstring(text):
        try:
            slat, slon = float(s.get("lat")), float(s.get("lon"))
        except (TypeError, ValueError):
            continue
        km = float(distance_km(lat, lon, slat, slon))
        if km <= radius_km:
            measures = ["wind", "pressure"] if s.get("met") == "y" else []
            measures += ["current"] if s.get("currents") == "y" else []
            measures += ["waves"] if s.get("waterquality") == "y" or s.get("met") == "y" else []
            found.append(Sensor("ndbc", s.get("id"), s.get("name", ""), slat, slon, measures or ["other"], km))
    return found


def parse_ndbc(text: str) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 3:
        return {}
    head = lines[0].lstrip("#").split()
    rows = [l.split() for l in lines[2:]]
    col = {name: i for i, name in enumerate(head)}

    def values(name):
        return np.array([float(r[col[name]]) if r[col[name]] != "MM" else math.nan for r in rows])

    t = np.array([dt.datetime(int(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4]), tzinfo=dt.timezone.utc).timestamp() for r in rows])
    out = {}
    if "WSPD" in col and "WDIR" in col:
        speed, towards = values("WSPD"), np.radians((values("WDIR") + 180.0) % 360.0)
        out["wind_u"], out["wind_v"] = (t, speed * np.sin(towards)), (t, speed * np.cos(towards))
    if "PRES" in col:
        out["pressure"] = (t, values("PRES"))
    if "WVHT" in col:
        out["wave_height"] = (t, values("WVHT"))
    order = np.argsort(t)
    return {k: (tt[order], vv[order]) for k, (tt, vv) in out.items()}


# --------------------------------------------------------------------------- GloFAS river discharge (through Open-Meteo)


GLOFAS = "https://flood-api.open-meteo.com/v1/flood"


def glofas_probe_points(lat: float, lon: float, radius_km: float, spacing_km: float = 10.0) -> list[tuple[float, float]]:
    """Points on a grid within reach, at which to ask for river discharge."""
    steps = int(radius_km // spacing_km)
    points = []
    for i in range(-steps, steps + 1):
        for j in range(-steps, steps + 1):
            plat = lat + i * spacing_km / 111.0
            plon = lon + j * spacing_km / (111.0 * math.cos(math.radians(lat)))
            if distance_km(lat, lon, plat, plon) <= radius_km:
                points.append((round(plat, 3), round(plon, 3)))
    return points


def parse_glofas(text: str) -> list[dict]:
    data = json.loads(text)
    data = data if isinstance(data, list) else [data]
    out = []
    for item in data:
        daily = item.get("daily", {})
        t = np.array([_epoch(day + "T12:00:00") for day in daily.get("time", [])])
        q = np.array([math.nan if x is None else float(x) for x in daily.get("river_discharge", [])])
        out.append({"lat": item.get("latitude"), "lon": item.get("longitude"), "times": t, "discharge": q})
    return out


# --------------------------------------------------------------------------- Hong Kong: Observatory and Marine Department


HKO_TIDE = "https://data.weather.gov.hk/weatherAPI/hko_data/tide/ALL_en.csv"
HKO_RAIN_DAILY = "https://data.weather.gov.hk/weatherAPI/cis/csvfile/{station}/ALL/daily_{station}_RF_ALL.csv"
MARDEP_TIDE = "https://tide1.hydro.gov.hk/hotide/OpenData/All_en.csv"

# Approximate positions (to about a kilometre) of the gauges these feeds name; the feeds give names only.
HK_TIDE_GAUGES = {
    "Quarry Bay": (22.291, 114.213), "Shek Pik": (22.220, 113.894), "Tsim Bei Tsui": (22.487, 114.014),
    "Tai Miu Wan": (22.269, 114.289), "Tai Po Kau": (22.442, 114.184), "Tai O": (22.257, 113.867),
    "Waglan Island": (22.183, 114.303), "Kwai Chung": (22.323, 114.123), "Ma Wan": (22.364, 114.071),
    "Cheung Chau": (22.214, 114.029), "Ko Lau Wan": (22.459, 114.361), "Sha Kiu Tau": (22.293, 114.338),
    "Lok On Pai": (22.360, 113.983), "Tai Mei Tuk": (22.475, 114.233), "Chi Ma Wan": (22.239, 114.000),
}
HK_BOX = (22.1, 22.6, 113.8, 114.5)


def parse_hk_latest_tide(text: str, catalogue: str) -> list[dict]:
    """Rows of a latest-tide feed: station, time (epoch, from Hong Kong time), height above Chart Datum (m)."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    out = []
    for row in rows[1:]:
        if len(row) < 4:
            continue
        name = row[0].strip()
        try:
            height = float(row[3])
        except ValueError:
            continue
        stamp = dt.datetime.fromisoformat(f"{row[1].strip()}T{row[2].strip()}").replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
        out.append({"catalogue": catalogue, "station": name, "time": stamp.timestamp(), "sea_level": height})
    return out


def hk_sensors(lat: float, lon: float, radius_km: float) -> list[Sensor]:
    if not (HK_BOX[0] - 1 <= lat <= HK_BOX[1] + 1 and HK_BOX[2] - 1 <= lon <= HK_BOX[3] + 1):
        return []
    found = []
    for name, (slat, slon) in HK_TIDE_GAUGES.items():
        km = float(distance_km(lat, lon, slat, slon))
        if km <= radius_km:
            found.append(Sensor("hk-tide", name, name, slat, slon, ["sea_level"], km, history=False))
    found.append(Sensor("hko-rain", "HKO", "Hong Kong Observatory daily rain", 22.302, 114.174, ["rain"],
                        float(distance_km(lat, lon, 22.302, 114.174))))
    return found


# --------------------------------------------------------------------------- discovery and collection


DEFAULT_RADIUS_KM = {"ghcnh": 60.0, "uhslc": 150.0, "ioc": 150.0, "coops": 60.0, "ndbc": 150.0, "glofas": 120.0, "hk": 80.0}


def discover(
    lat: float,
    lon: float,
    fetcher: Fetcher | None = None,
    radius_km: dict | None = None,
    catalogues: Sequence[str] = ("ghcnh", "uhslc", "ioc", "coops", "ndbc", "glofas", "hk"),
    log: Callable[[str], None] | None = None,
) -> dict:
    """Every sensor within reach of (lat, lon), as a manifest (a JSON-ready dict).

    A catalogue that cannot be reached is recorded under ``unreachable``
    and the rest carry on, so a venue's manifest says plainly what was
    looked for and not found.
    """
    fetcher = fetcher or Fetcher()
    reach = {**DEFAULT_RADIUS_KM, **(radius_km or {})}
    log = log or (lambda m: None)
    sensors: list[Sensor] = []
    unreachable = {}
    for name in catalogues:
        try:
            log(f"asking {name}")
            if name == "ghcnh":
                sensors += ghcnh_stations(fetcher.get(GHCNH_LIST, max_age_s=30 * 86400).decode(), lat, lon, reach[name])
            elif name == "uhslc":
                url = UHSLC_ERDDAP + "?uhslc_id,station_name,latitude,longitude&distinct()"
                sensors += uhslc_stations(fetcher.get(url, max_age_s=30 * 86400).decode(), lat, lon, reach[name])
            elif name == "ioc":
                url = IOC_BASE + "?query=stationlist&showall=all"
                sensors += ioc_stations(fetcher.get(url, max_age_s=7 * 86400).decode(), lat, lon, reach[name])
            elif name == "coops":
                texts = {k: fetcher.get(COOPS_STATIONS.format(kind=k), max_age_s=30 * 86400).decode() for k in ("waterlevels", "currents")}
                sensors += coops_stations(texts, lat, lon, reach[name])
            elif name == "ndbc":
                sensors += ndbc_stations(fetcher.get(NDBC_ACTIVE, max_age_s=7 * 86400).decode(), lat, lon, reach[name])
            elif name == "glofas":
                sensors += discover_rivers(lat, lon, reach[name], fetcher)
            elif name == "hk":
                sensors += hk_sensors(lat, lon, reach[name])
        except Exception as problem:
            unreachable[name] = str(problem)[:300]
            log(f"  {name} not reachable: {problem}")
    sensors.sort(key=lambda s: (s.measures[0], s.distance_km))
    return {
        "lat": lat,
        "lon": lon,
        "made": _iso(_time.time()),
        "sensors": [asdict(s) for s in sensors],
        "unreachable": unreachable,
    }


def discover_rivers(lat: float, lon: float, radius_km: float, fetcher: Fetcher, min_mean_m3s: float = 50.0, keep: int = 6) -> list[Sensor]:
    """The largest rivers reaching the sea within reach, from GloFAS's discharge on a probe grid."""
    points = glofas_probe_points(lat, lon, radius_km, spacing_km=max(5.0, radius_km / 8))
    found = []
    for i in range(0, len(points), 50):
        chunk = points[i : i + 50]
        url = GLOFAS + "?" + urllib.parse.urlencode(
            {"latitude": ",".join(str(p[0]) for p in chunk), "longitude": ",".join(str(p[1]) for p in chunk),
             "daily": "river_discharge", "past_days": 92, "forecast_days": 1}
        )
        for item in parse_glofas(fetcher.get(url, max_age_s=30 * 86400).decode()):
            mean = float(np.nanmean(item["discharge"])) if np.isfinite(item["discharge"]).any() else 0.0
            if mean >= min_mean_m3s:
                found.append((mean, item["lat"], item["lon"]))
    found.sort(reverse=True)
    sensors, used = [], []
    for mean, rlat, rlon in found:
        if any(distance_km(rlat, rlon, a, b) < 8.0 for a, b in used):
            continue  # the same river seen at a neighbouring probe
        used.append((rlat, rlon))
        sensors.append(Sensor("glofas", f"{rlat:.3f},{rlon:.3f}", f"river at {rlat:.2f} N {rlon:.2f} E (mean {mean:.0f} m3/s)",
                              rlat, rlon, ["river"], float(distance_km(lat, lon, rlat, rlon)), extra={"mean_m3s": mean}))
        if len(sensors) >= keep:
            break
    return sensors


def fetch(sensor: Sensor | dict, start: float, end: float, fetcher: Fetcher | None = None) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """The record of one sensor from ``start`` to ``end``, as {quantity: (times, values)}."""
    s = Sensor(**sensor) if isinstance(sensor, dict) else sensor
    fetcher = fetcher or Fetcher()
    out: dict[str, list] = {}

    def add(parts: dict):
        for k, (t, v) in parts.items():
            out.setdefault(k, []).append((t, v))

    if s.catalogue == "ghcnh":
        for year in range(_year(start), _year(end) + 1):
            try:
                add(parse_ghcnh(fetcher.get(GHCNH_YEAR.format(year=year, id=s.id), max_age_s=None if year < _year(_time.time()) else 6 * 3600).decode()))
            except RuntimeError:
                continue
    elif s.catalogue == "uhslc":
        url = UHSLC_ERDDAP + "?" + urllib.parse.quote(f"time,sea_level&uhslc_id={s.id}&time>={_iso(start)}&time<={_iso(end)}", safe="=&,><")
        add({"sea_level": parse_erddap_level(fetcher.get(url, max_age_s=None if end < _time.time() - 86400 * 60 else 6 * 3600).decode())})
    elif s.catalogue == "ioc":
        step = 30 * 86400.0
        for a in np.arange(start, end, step):
            b = min(end, a + step)
            url = IOC_BASE + "?" + urllib.parse.urlencode({"query": "data", "code": s.id, "timestart": _iso(a)[:-1], "timestop": _iso(b)[:-1], "format": "json"})
            add({"sea_level": parse_ioc(fetcher.get(url, max_age_s=None if b < _time.time() - 86400 else 1800).decode())})
    elif s.catalogue == "coops":
        kind = s.extra.get("kind", "waterlevels")
        product = "currents" if kind == "currents" else "water_level"
        step = 30 * 86400.0
        for a in np.arange(start, end, step):
            b = min(end, a + step)
            query = {"product": product, "station": s.id, "begin_date": _coops_date(a), "end_date": _coops_date(b),
                     "time_zone": "gmt", "units": "english" if kind == "currents" else "metric", "format": "json", "application": USER_AGENT}
            if product == "water_level":
                query["datum"] = "MSL"
            add(parse_coops(fetcher.get(COOPS_DATA + "?" + urllib.parse.urlencode(query), max_age_s=None if b < _time.time() - 86400 else 1800).decode(), kind))
            if kind == "waterlevels":
                # Many water level stations also carry a weather station: read its wind and pressure where it has them.
                for met in ("wind", "air_pressure"):
                    query.update(product=met, units="metric")
                    query.pop("datum", None)
                    try:
                        text = fetcher.get(COOPS_DATA + "?" + urllib.parse.urlencode(query), max_age_s=None if b < _time.time() - 86400 else 1800).decode()
                        add(parse_coops(text, met))
                    except (RuntimeError, ValueError, KeyError):
                        continue
    elif s.catalogue == "ndbc":
        add(parse_ndbc(fetcher.get(NDBC_REALTIME.format(id=s.id), max_age_s=3600).decode()))
    elif s.catalogue == "glofas":
        url = GLOFAS + "?" + urllib.parse.urlencode(
            {"latitude": s.lat, "longitude": s.lon, "daily": "river_discharge",
             "start_date": _day(start), "end_date": _day(min(end, _time.time() + 30 * 86400))}
        )
        for item in parse_glofas(fetcher.get(url, max_age_s=6 * 3600).decode()):
            add({"river": (item["times"], item["discharge"])})
    elif s.catalogue == "hko-rain":
        add({"rain_daily": parse_hko_daily(fetcher.get(HKO_RAIN_DAILY.format(station=s.id), max_age_s=6 * 3600).decode("utf-8", "replace"))})
    elif not s.history:
        return {}
    merged = {}
    for k, parts in out.items():
        t = np.concatenate([p[0] for p in parts]) if parts else np.zeros(0)
        v = np.concatenate([p[1] for p in parts]) if parts else np.zeros(0)
        keep = (t >= start) & (t <= end)
        t, v = t[keep], v[keep]
        t, first = np.unique(t, return_index=True)
        merged[k] = (t, v[first])
    return merged


def parse_hko_daily(text: str) -> tuple[np.ndarray, np.ndarray]:
    """A Hong Kong Observatory daily climate file (year, month, day, value): values at noon, Hong Kong time."""
    t, v = [], []
    for row in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(row) < 4 or not row[0].strip().isdigit():
            continue
        try:
            day = dt.datetime(int(row[0]), int(row[1]), int(row[2]), 12, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        except ValueError:
            continue
        text_value = row[3].strip()
        value = 0.0 if text_value.lower() == "trace" else (float(text_value) if _is_number(text_value) else math.nan)
        t.append(day.timestamp())
        v.append(value)
    return np.array(t), np.array(v)


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _year(t: float) -> int:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).year


def _day(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")


def _coops_date(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y%m%d %H:%M")


def to_hourly(times: np.ndarray, values: np.ndarray, grid: np.ndarray, how: str = "mean", max_gap_s: float = 3 * 3600) -> np.ndarray:
    """Put a series on an hourly grid: the mean of samples within each hour (``sum`` for rain), gaps left empty.

    Hours with no sample are filled by linear interpolation when the gap is
    shorter than ``max_gap_s``; a daily series is spread over its day.
    """
    out = np.full(grid.shape, np.nan)
    good = np.isfinite(values)
    times, values = times[good], values[good]
    if times.size == 0:
        return out
    step = grid[1] - grid[0] if grid.size > 1 else 3600.0
    index = np.floor((times - grid[0]) / step).astype(int)
    inside = (index >= 0) & (index < grid.size)
    sums = np.bincount(index[inside], weights=values[inside], minlength=grid.size)
    counts = np.bincount(index[inside], minlength=grid.size)
    have = counts > 0
    out[have] = sums[have] if how == "sum" else sums[have] / counts[have]
    if how != "sum" and times.size > 1:
        filled = np.interp(grid, times, values)
        if not have.all():
            fill = ~have & (_nearest_gap(grid, times) <= max_gap_s / 2)
            out[fill] = filled[fill]
    return out


def _nearest_gap(grid, times):
    pos = np.clip(np.searchsorted(times, grid), 1, times.size - 1)
    return np.minimum(np.abs(grid - times[pos - 1]), np.abs(times[pos] - grid))


def collect(
    manifest: dict,
    start: float,
    end: float,
    fetcher: Fetcher | None = None,
    step_s: float = 3600.0,
    log: Callable[[str], None] | None = None,
    only: Iterable[str] | None = None,
) -> dict:
    """Every sensor of a manifest on one hourly grid: {"times": array, "<catalogue:id>/<quantity>": array}.

    Sensors that fail to load are listed under ``"failed"``. ``only``
    limits the read to some sensor keys.
    """
    log = log or (lambda m: None)
    fetcher = fetcher or Fetcher()
    grid = np.arange(np.floor(start / step_s) * step_s, end, step_s)
    table = {"times": grid}
    failed = {}
    wanted = set(only) if only else None
    for item in manifest["sensors"]:
        s = Sensor(**item)
        if wanted is not None and s.key not in wanted:
            continue
        if not s.history:
            continue
        try:
            log(f"reading {s.key} {s.name}")
            parts = fetch(s, start, end, fetcher)
        except Exception as problem:
            failed[s.key] = str(problem)[:300]
            continue
        for quantity, (t, v) in parts.items():
            how = "sum" if quantity == "rain" else "mean"
            series = to_hourly(t, v, grid, how=how, max_gap_s=(36 * 3600 if quantity in ("river", "rain_daily") else 3 * 3600))
            if np.isfinite(series).sum() > 0:
                table[f"{s.key}/{quantity}"] = series
    table["failed"] = failed
    return table


def record_latest(path: str | Path, lat: float, lon: float, fetcher: Fetcher | None = None) -> int:
    """Append the latest values of feeds that keep no history (Hong Kong's live tide gauges) to a CSV; returns rows added.

    Run it every 10 minutes from the day a venue is added and those feeds
    grow a record. Rows already in the file are not added again.
    """
    fetcher = fetcher or Fetcher()
    rows = []
    if HK_BOX[0] - 1 <= lat <= HK_BOX[1] + 1 and HK_BOX[2] - 1 <= lon <= HK_BOX[3] + 1:
        for url, name in ((HKO_TIDE, "hko"), (MARDEP_TIDE, "mardep")):
            try:
                rows += parse_hk_latest_tide(fetcher.get(url, max_age_s=0).decode("utf-8", "replace"), name)
            except Exception:
                continue
    path = Path(path)
    seen = set()
    if path.exists():
        with open(path, newline="") as handle:
            seen = {(r["catalogue"], r["station"], r["time"]) for r in csv.DictReader(handle)}
    new = [r for r in rows if (r["catalogue"], r["station"], f"{r['time']:.0f}") not in seen]
    if new:
        exists = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["catalogue", "station", "time", "sea_level"])
            if not exists:
                writer.writeheader()
            for r in new:
                writer.writerow({**r, "time": f"{r['time']:.0f}"})
    return len(new)


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Find every open sensor around a venue and write a manifest.")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--out", required=True, help="manifest file (JSON)")
    parser.add_argument("--cache", default=None)
    args = parser.parse_args(argv)
    manifest = discover(args.lat, args.lon, Fetcher(args.cache), log=print)
    Path(args.out).write_text(json.dumps(manifest, indent=1))
    by_kind: dict[str, int] = {}
    for s in manifest["sensors"]:
        for m in s["measures"]:
            by_kind[m] = by_kind.get(m, 0) + 1
    print(f"{len(manifest['sensors'])} sensors: " + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items())))
    if manifest["unreachable"]:
        print("not reachable: " + ", ".join(manifest["unreachable"]))


if __name__ == "__main__":
    main()
