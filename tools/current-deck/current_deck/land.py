"""Coastline polygons for the map panels.

Sources, best first (``--land auto`` tries them in this order):

1. GSHHG full resolution (about 100 m), read from the ``basemap-data-hires`` pip
   package. No network needed once the package is installed. LGPL data.
2. Natural Earth 1:10m land + minor islands (public domain), downloaded once from
   GitHub and cached in ~/.cache/current-deck. Coarse (about 1 km): fine for the
   locator map, too blocky for the zoomed race-area map.

``--land FILE.geojson`` uses your own land polygons instead (for example an
OpenStreetMap land-polygon extract, which is sharper again).

Every ring is clipped to the requested box, so a continent-sized polygon costs
only the handful of points that are actually on the map.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import numpy as np

NE_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
NE_FILES = ["ne_10m_land.geojson", "ne_10m_minor_islands.geojson"]
CACHE = Path(os.environ.get("CURRENT_DECK_CACHE", Path.home() / ".cache" / "current-deck"))


def _rings(geojson: dict):
    feats = geojson.get("features") or [geojson]
    for ft in feats:
        g = ft.get("geometry", ft)
        if not g:
            continue
        if g["type"] == "Polygon":
            yield g["coordinates"][0]
        elif g["type"] == "MultiPolygon":
            for poly in g["coordinates"]:
                yield poly[0]
        elif g["type"] == "GeometryCollection":
            yield from _rings({"features": [{"geometry": x} for x in g["geometries"]]})


def _fetch(name: str) -> Path | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / name
    if p.exists() and p.stat().st_size > 1000:
        return p
    try:
        req = urllib.request.Request(NE_BASE + name, headers={"User-Agent": "current-deck"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        p.write_bytes(data)
        return p
    except Exception:
        return None


def _gshhg_dir() -> Path | None:
    try:
        import importlib.util
        spec = importlib.util.find_spec("mpl_toolkits.basemap_data")
    except Exception:
        return None
    for loc in (spec.submodule_search_locations or []) if spec else []:
        if (Path(loc) / "gshhs_f.dat").exists():
            return Path(loc)
    return None


def _gshhg_rings(folder: Path, bbox):
    """Land polygons (level 1) from basemap's full-resolution GSHHG files that touch bbox."""
    x0, y0, x1, y1 = bbox
    out = []
    with open(folder / "gshhs_f.dat", "rb") as data, open(folder / "gshhsmeta_f.dat") as meta:
        for line in meta:
            p = line.split()
            # level, area, points, south, north, byte offset, byte count, id
            if p[0] != "1" or float(p[4]) < y0 or float(p[3]) > y1:
                continue
            data.seek(int(p[5]))
            ring = np.frombuffer(data.read(int(p[6])), dtype="<f4").reshape(int(p[2]), 2).astype(float)
            if ring[:, 0].max() < x0 or ring[:, 0].min() > x1:
                continue
            out.append(ring)
    return out


def clip_ring(ring, bbox):
    """Sutherland-Hodgman clip of a closed ring to a lon/lat box. Returns an (N,2) array or None."""
    pts = np.asarray(ring, dtype=float)[:, :2]
    x0, y0, x1, y1 = bbox
    for axis, bound, keep_greater in ((0, x0, True), (0, x1, False), (1, y0, True), (1, y1, False)):
        if len(pts) < 3:
            return None
        cur, nxt = pts, np.roll(pts, -1, axis=0)
        inside = cur[:, axis] >= bound if keep_greater else cur[:, axis] <= bound
        inside_n = np.roll(inside, -1)
        denom = nxt[:, axis] - cur[:, axis]
        t = np.divide(bound - cur[:, axis], denom, out=np.zeros(len(cur)), where=denom != 0)
        cross = cur + (nxt - cur) * t[:, None]
        # per edge: emit the current point if inside, then the crossing if the edge changes side
        emit = np.empty((len(cur) * 2, 2))
        emit[0::2], emit[1::2] = cur, cross
        mask = np.empty(len(cur) * 2, dtype=bool)
        mask[0::2], mask[1::2] = inside, inside != inside_n
        pts = emit[mask]
    return pts if len(pts) >= 3 else None


def smooth_ring(pts, rounds: int = 2):
    """Chaikin corner cutting: rounds off the stair-steps of raster-derived coastlines."""
    for _ in range(rounds):
        nxt = np.roll(pts, -1, axis=0)
        out = np.empty((len(pts) * 2, 2))
        out[0::2] = 0.75 * pts + 0.25 * nxt
        out[1::2] = 0.25 * pts + 0.75 * nxt
        pts = out
    return pts


def load_land(bbox, source: str = "auto"):
    """Return (rings, note, source_name). bbox = (lon_min, lat_min, lon_max, lat_max).

    Rings are (N,2) lon/lat arrays clipped to bbox.
    """
    if source in ("none", "off", ""):
        return [], None, "none"
    note, name, smooth = None, source, False
    raw = None
    if source in ("auto", "gshhg"):
        folder = _gshhg_dir()
        if folder is not None:
            raw, name, smooth = _gshhg_rings(folder, bbox), "gshhg-full", True
        elif source == "gshhg":
            raise SystemExit("--land gshhg needs the basemap-data-hires package: pip install basemap-data-hires")
        else:
            note = ("Detailed coastline not installed (pip install basemap-data-hires); "
                    "using coarse Natural Earth land.")
    if raw is None and source in ("auto", "naturalearth"):
        raw, name = [], "naturalearth-10m"
        for fname in NE_FILES:
            p = _fetch(fname)
            if p is None:
                return [], ("Coastline download failed (no network?); maps drawn without land. "
                            "Install basemap-data-hires, or pass --land file.geojson or --land none."), "none"
            with open(p, encoding="utf-8") as fh:
                raw += list(_rings(json.load(fh)))
    if raw is None:
        with open(source, encoding="utf-8") as fh:
            raw = list(_rings(json.load(fh)))
    out = []
    for ring in raw:
        c = clip_ring(ring, bbox)
        if c is not None:
            out.append(smooth_ring(c) if smooth else c)
    return out, note, name
