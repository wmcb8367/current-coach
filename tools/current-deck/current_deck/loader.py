"""Load current measurements from CSV files with differing schemas.

Columns are matched by name (case and punctuation insensitive). Anything the
auto-detection gets wrong can be forced with ``--map field=column``.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

KT_PER_MS = 3600.0 / 1852.0

# Field -> candidate column names, best first (already normalised).
CANDIDATES = {
    "lat": ["latitude", "lat", "lat_deg", "latitude_deg", "y"],
    "lon": ["longitude", "lon", "lng", "long", "lon_deg", "longitude_deg", "x"],
    "speed": [
        "current_kt", "current_knots", "current_speed_kt", "current_speed_knots",
        "speed_knots", "speed_kt", "speed_kts", "current_speed", "drift_kt",
        "current_ms", "current_m_s", "speed_ms", "speed_m_s", "speed_cm_s",
        "speed_cms", "speed_m_min", "speed_meters_per_minute",
        "current", "speed", "spd",
    ],
    "toward": [
        "current_toward_deg", "direction_toward_deg", "current_set_deg", "set_deg",
        "current_to_deg", "toward_deg", "flow_direction", "flow_dir", "set",
        "direction_to", "dir_to", "toward",
    ],
    "from": [
        "current_from_deg", "direction_from_deg", "from_deg", "from_direction",
        "direction_from", "dir_from", "from",
    ],
    "direction": ["current_direction", "current_dir", "direction_deg", "direction", "dir", "heading"],
    "note": ["position_note", "note", "notes", "location", "position", "label", "name", "mark"],
    "group": ["race", "leg", "session"],
    "valid": ["is_valid", "valid"],
    "wind_from": ["wind_from_deg", "wind_direction", "wind_dir", "twd"],
    "wind_speed": ["wind_kt", "wind_knots", "wind_speed", "tws"],
}

# Time-zone hints found in column names (``time_hkt``, ``time_hong_kong`` ...).
TZ_HINTS = {
    "utc": "UTC", "gmt": "UTC", "z": "UTC",
    "hkt": "Asia/Hong_Kong", "hong_kong": "Asia/Hong_Kong", "hk": "Asia/Hong_Kong",
    "pacific": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "eastern": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
    "central": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
    "mountain": "America/Denver",
    "bst": "Europe/London", "uk": "Europe/London", "london": "Europe/London",
    "cet": "Europe/Paris", "cest": "Europe/Paris",
    "aest": "Australia/Sydney", "aedt": "Australia/Sydney", "sydney": "Australia/Sydney",
    "nzst": "Pacific/Auckland", "nzdt": "Pacific/Auckland",
    "jst": "Asia/Tokyo", "sgt": "Asia/Singapore",
}
TIME_WORDS = ("time", "timestamp", "datetime", "date_time", "ts")


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


@dataclass
class Measurement:
    t: datetime            # aware, in the venue time zone
    lat: float
    lon: float
    speed_kt: float
    toward_deg: float      # direction the water flows toward, degrees true
    note: str = ""
    wind_from: float | None = None
    wind_kt: float | None = None
    source: str = ""

    @property
    def from_deg(self) -> float:
        return (self.toward_deg + 180.0) % 360.0


@dataclass
class LoadReport:
    path: str
    rows: int = 0
    used: int = 0
    skipped: dict = field(default_factory=dict)
    columns: dict = field(default_factory=dict)
    time_column: str = ""
    notes: list = field(default_factory=list)

    def skip(self, reason: str):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _num(v) -> float | None:
    if v is None:
        return None
    v = str(v).strip()
    if v == "" or v.lower() in ("nan", "na", "n/a", "none", "null", "-"):
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return None if math.isnan(f) else f


def _truthy(v) -> bool | None:
    if v is None or str(v).strip() == "":
        return None
    return str(v).strip().lower() in ("1", "true", "t", "yes", "y")


def _pick(cols: dict, fieldname: str) -> str | None:
    for cand in CANDIDATES[fieldname]:
        if cand in cols:
            return cols[cand]
    return None


def _tz_from_name(n: str) -> str | None:
    parts = n.split("_")
    joined = "_".join(parts)
    for hint, zone in TZ_HINTS.items():
        if "_" in hint and hint in joined:
            return zone
    for p in parts:
        if p in TZ_HINTS and p not in ("z",):
            return TZ_HINTS[p]
    return None


def _time_columns(cols: dict) -> list[tuple[str, str | None]]:
    """[(original column, zone hint or None)] for columns that look like times."""
    out = []
    for n, orig in cols.items():
        if any(w == n or n.startswith(w + "_") or n.endswith("_" + w) for w in TIME_WORDS) or n == "utc":
            out.append((orig, _tz_from_name(n)))
    return out


def _parse_dt(s: str, day: date | None, tz: tzinfo | None) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" UTC", "+00:00"))
    except ValueError:
        dt = None
    if dt is None:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?", s)
        if m:
            if day is None:
                raise ValueError(f"Time '{s}' has no date. Pass --date YYYY-MM-DD.")
            dt = datetime.combine(day, time(int(m[1]), int(m[2]), int(m[3] or 0)))
        else:
            for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
                        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or timezone.utc)
    return dt


def date_from_filename(path: str) -> date | None:
    m = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", Path(path).name)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def guess_zone(candidates: list[str], lon: float | None) -> str | None:
    """Pick the named zone whose UTC offset best matches the longitude."""
    zones = [z for z in candidates if z and z != "UTC"]
    if not zones:
        return None
    if lon is None or len(zones) == 1:
        return zones[0]
    now = datetime.now(timezone.utc)
    return min(zones, key=lambda z: abs(ZoneInfo(z).utcoffset(now).total_seconds() / 3600 - lon / 15))


def load_csv(path: str, *, venue_tz: str | None = None, day: date | None = None,
             colmap: dict | None = None, dir_convention: str = "auto",
             speed_units: str = "auto", keep_invalid: bool = False):
    """Return (measurements, report, venue_zone_name)."""
    colmap = colmap or {}
    rep = LoadReport(path=str(path))
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    rep.rows = len(rows)
    if not rows:
        return [], rep, venue_tz
    cols = {norm(c): c for c in rows[0].keys() if c}

    def col(fieldname):
        if fieldname in colmap:
            want = colmap[fieldname]
            for c in rows[0].keys():
                if c == want or norm(c) == norm(want):
                    return c
            raise SystemExit(f"--map {fieldname}={want}: no such column in {path}")
        return _pick(cols, fieldname)

    c = {k: col(k) for k in CANDIDATES}
    # wind columns must not be picked up as current columns and vice versa
    for k in ("speed", "toward", "from", "direction"):
        if c[k] and "wind" in norm(c[k]) and k not in colmap:
            c[k] = None
    rep.columns = {k: v for k, v in c.items() if v}
    if not c["lat"] or not c["lon"]:
        raise SystemExit(f"{path}: could not find latitude/longitude columns (use --map lat=.. --map lon=..)")
    if not c["speed"]:
        raise SystemExit(f"{path}: could not find a current speed column (use --map speed=..)")
    if not (c["toward"] or c["from"] or c["direction"]):
        raise SystemExit(f"{path}: could not find a current direction column (use --map toward=.. or --map from=..)")

    # ---- time column and zone
    tcols = _time_columns(cols)
    # a separate date column supplies the day for clock-only times
    date_col = next((cols[n] for n in ("date", "local_date", "date_local", "utc_date", "date_utc") if n in cols), None)
    if not tcols and date_col:
        tcols = [(date_col, None)]
    if "time" in colmap:
        tcols = [(col("time"), _tz_from_name(norm(colmap["time"])))]
    if not tcols:
        raise SystemExit(f"{path}: could not find a time column (use --map time=..)")
    first_lon = next((_num(r.get(c["lon"])) for r in rows if _num(r.get(c["lon"])) is not None), None)
    zone = venue_tz or guess_zone([z for _, z in tcols], first_lon)
    if zone is None:
        if first_lon is not None:
            zone = f"UTC{round(first_lon / 15):+d}"
            rep.notes.append(f"No venue time zone given or named in the columns; using {zone} from longitude. Pass --tz to set it.")
        else:
            zone = "UTC"
    tz = _zone(zone)
    # Prefer a column already in the venue zone, else one with full ISO stamps, else UTC.
    tcols.sort(key=lambda tc: 0 if tc[1] == zone else 1 if tc[1] == "UTC" else 2 if tc[1] is None else 3)
    tcol, thint = tcols[0]
    rep.time_column = tcol
    naive_tz = _zone(thint) if thint else tz
    if day is None:
        day = date_from_filename(path)

    # ---- speed units
    sname = norm(c["speed"])
    factor = 1.0
    units = speed_units
    if units == "auto":
        if re.search(r"(^|_)(cm_?s|cms)($|_)", sname):
            units = "cm/s"
        elif re.search(r"m_?min|meters_per_minute", sname):
            units = "m/min"
        elif re.search(r"(^|_)(m_?s|mps)($|_)", sname):
            units = "m/s"
        else:
            units = "kt"
    factor = {"kt": 1.0, "m/s": KT_PER_MS, "cm/s": KT_PER_MS / 100, "m/min": KT_PER_MS / 60}[units]
    if units != "kt":
        rep.notes.append(f"Speed column '{c['speed']}' read as {units} and converted to knots.")

    # ---- direction convention
    # an explicit --map wins over autodetected direction columns
    if "from" in colmap:
        c["toward"] = None
    elif "direction" in colmap:
        c["toward"] = c["from"] = None
    if c["toward"]:
        dcol, conv = c["toward"], "toward"
    elif c["from"]:
        dcol, conv = c["from"], "from"
    else:
        dcol = c["direction"]
        conv = dir_convention if dir_convention != "auto" else "toward"
        if dir_convention == "auto":
            rep.notes.append(f"Direction column '{dcol}' does not say from/toward; assumed 'toward' (oceanographic set). Use --dir-convention from if wrong.")
    if dir_convention in ("from", "toward") and dcol != c["direction"]:
        conv = dir_convention

    out = []
    for r in rows:
        if c["valid"] and not keep_invalid and _truthy(r.get(c["valid"])) is False:
            rep.skip("flagged invalid")
            continue
        spd = _num(r.get(c["speed"]))
        d = _num(r.get(dcol))
        lat, lon = _num(r.get(c["lat"])), _num(r.get(c["lon"]))
        if spd is None or d is None:
            rep.skip("no current reading")
            continue
        if lat is None or lon is None:
            rep.skip("no position")
            continue
        rday = day
        if date_col and date_col != tcol:
            try:
                rday = date.fromisoformat((r.get(date_col) or "").strip()[:10])
            except ValueError:
                rday = day
        t = _parse_dt(r.get(tcol, ""), rday, naive_tz)
        if t is None:
            rep.skip("unreadable time")
            continue
        toward = d % 360 if conv == "toward" else (d + 180) % 360
        note = " · ".join(x for x in (
            (r.get(c["group"]) or "").strip() if c["group"] else "",
            (r.get(c["note"]) or "").strip() if c["note"] else "",
        ) if x)
        out.append(Measurement(
            t=t.astimezone(tz), lat=lat, lon=lon, speed_kt=spd * factor, toward_deg=toward,
            note=note,
            wind_from=_num(r.get(c["wind_from"])) if c["wind_from"] else None,
            wind_kt=_num(r.get(c["wind_speed"])) if c["wind_speed"] else None,
            source=Path(path).name,
        ))
    out.sort(key=lambda m: m.t)
    rep.used = len(out)
    return out, rep, zone


def _zone(name: str) -> tzinfo:
    m = re.fullmatch(r"UTC([+-]\d{1,2})(?::?(\d{2}))?", name)
    if m:
        h = int(m[1])
        mins = int(m[2] or 0) * (1 if h >= 0 else -1)
        return timezone(timedelta(hours=h, minutes=mins), name)
    return ZoneInfo(name)


def zone_label(zone: str, sample: datetime | None) -> str:
    """Short label such as 'HKT' or 'UTC+08:00'."""
    nice = {"Asia/Hong_Kong": "HKT"}
    if zone in nice:
        return nice[zone]
    if sample is not None:
        abbr = sample.tzname() or ""
        if abbr and not abbr.startswith(("+", "-")):
            return abbr
    return zone
