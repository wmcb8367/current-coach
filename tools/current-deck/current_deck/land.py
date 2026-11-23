"""Coastline polygons for the map panels.

Default source is Natural Earth 1:10m land + minor islands (public domain),
downloaded once from GitHub and cached in ~/.cache/current-deck. It is coarse
(roughly 1 km), fine for orientation. For a sharper shoreline pass --land with
any GeoJSON of land polygons (for example an OpenStreetMap land-polygon extract).
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

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


def load_land(bbox, source: str = "auto"):
    """Return (rings, note). bbox = (lon_min, lat_min, lon_max, lat_max)."""
    if source in ("none", "off", ""):
        return [], None
    files = []
    if source == "auto":
        for name in NE_FILES:
            p = _fetch(name)
            if p is None:
                return [], ("Coastline download failed (no network?); maps drawn without land. "
                            "Pass --land file.geojson or --land none.")
            files.append(p)
    else:
        files = [Path(source)]
    x0, y0, x1, y1 = bbox
    out = []
    for fp in files:
        with open(fp, encoding="utf-8") as fh:
            gj = json.load(fh)
        for ring in _rings(gj):
            xs = [pt[0] for pt in ring]
            ys = [pt[1] for pt in ring]
            if max(xs) < x0 or min(xs) > x1 or max(ys) < y0 or min(ys) > y1:
                continue
            out.append(ring)
    return out, None
