"""Command line entry point: ``python -m current_deck readings.csv [more.csv] -o deck.pptx``."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .deck import build_deck, hour_groups
from .loader import load_csv, zone_label
from .land import load_land
from .render import LEFT_PANEL, RIGHT_PANEL, build_frame, locator_frame, fmt_window, make_units, parse_distance, parse_window, render_average_gif, render_gif, render_hour_panels
from .style import THEMES

FIELDS = "time, lat, lon, speed, toward, from, direction, note, group, valid, wind_from, wind_speed"


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="current-deck",
        description="Turn current-measurement CSVs into a slide deck: an animated time-scrubber GIF "
                    "on slide 1, then one slide per hour of the day (venue local time).")
    p.add_argument("csv", nargs="+", help="one or more measurement CSV files (merged into one deck)")
    p.add_argument("-o", "--out", help="output .pptx (default: <first csv stem>_currents.pptx next to the csv)")
    p.add_argument("--assets-dir", help="also keep the GIF and hourly PNGs here (default: <out stem>_assets/)")
    p.add_argument("--title", help="deck title (default: built from the file name)")
    p.add_argument("--tz", help="venue IANA time zone, e.g. Asia/Hong_Kong (default: from column names / longitude)")
    p.add_argument("--date", type=date.fromisoformat,
                   help="YYYY-MM-DD for files whose times have no date (default: date in the file name)")
    p.add_argument("--map", action="append", default=[], metavar="FIELD=COLUMN",
                   help=f"force a column for a field; fields: {FIELDS}")
    p.add_argument("--dir-convention", choices=["auto", "toward", "from"], default="auto",
                   help="meaning of a bare 'direction' column (default auto: toward unless the name says 'from')")
    p.add_argument("--speed-units", choices=["auto", "kt", "m/s", "cm/s", "m/min"], default="auto")
    p.add_argument("--keep-invalid", action="store_true", help="keep rows whose is_valid column is false")
    p.add_argument("--theme", choices=sorted(THEMES), default="sailboss",
                   help="sailboss (default, the Luther current-readings look) or m2x (Current Coach app colours)")
    p.add_argument("--units", choices=["kt", "m/min"], default="kt", help="speed units shown on slides (default kt)")
    p.add_argument("--scale-max", type=float,
                   help="speed at the top of the colour gradient, in display units (default: strongest reading rounded up)")
    p.add_argument("--land", default="auto",
                   help="coastline: 'auto' (detailed GSHHG if basemap-data-hires is installed, else Natural Earth), "
                        "'gshhg', 'naturalearth', 'none', or a GeoJSON file of land polygons")
    p.add_argument("--kicker", help="small cyan line above titles (default: date, e.g. '23 November · current readings')")
    p.add_argument("--gif-style", choices=["arrows", "particles"], default="arrows",
                   help="slide-1 animation: one arrow per reading (default), or drifting particles through a "
                        "field interpolated between nearby readings")
    p.add_argument("--reach", default="1000m", metavar="DISTANCE",
                   help="how far from a reading its current may be shown in the particle views: metres (800), "
                        "800m, 1.5km or 0.5nm (default 1000m). Nothing is drawn beyond this distance from every "
                        "live reading. Smaller is stricter; larger joins readings into a wider field.")
    p.add_argument("--lifetime", "--window", dest="lifetime", metavar="TIME",
                   help="how long a reading stays relevant on the scrubber: minutes (90), 90m, 3h, 1h30m, or "
                        "'all' to keep every reading for the rest of the day (default 60m for arrows, 3h for particles)")
    p.add_argument("--day-average", action=argparse.BooleanOptionalAction, default=True,
                   help="add a slide with a looping particle view of all the day's readings averaged together "
                        "(default on; --no-day-average to skip)")
    p.add_argument("--fade", type=float, default=20, help="minutes of fade-out at the end of its life (default 20)")
    p.add_argument("--step", type=float, help="GIF minutes per frame (default: about 150 frames)")
    p.add_argument("--frame-ms", type=int, default=90, help="GIF milliseconds per frame (default 90)")
    p.add_argument("--json", action="store_true", help="print only the JSON summary on stdout")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _default_title(paths, date_label):
    stem = re.sub(r"[_\-. ]*20\d{2}[-_.]?\d{2}[-_.]?\d{2}[_\-. ]*", " ", Path(paths[0]).stem)
    stem = re.sub(r"\s+", " ", stem.replace("_", " ").replace("-", " ")).strip()
    return "Current readings" if len(paths) > 1 or not stem else stem[0].upper() + stem[1:]


def main(argv=None) -> int:
    a = parse_args(argv)
    colmap = {}
    for item in a.map:
        if "=" not in item:
            sys.exit(f"--map needs FIELD=COLUMN, got {item!r}")
        k, v = item.split("=", 1)
        colmap[k.strip()] = v.strip()

    ms, reports, zone = [], [], a.tz
    for path in a.csv:
        got, rep, z = load_csv(path, venue_tz=zone, day=a.date, colmap=colmap,
                               dir_convention=a.dir_convention, speed_units=a.speed_units,
                               keep_invalid=a.keep_invalid)
        zone = zone or z           # first file fixes the venue zone for the rest
        ms += got
        reports.append(rep)
    if not ms:
        sys.exit("No rows with time, position, current speed and direction were found.")
    from .loader import _zone
    tz = _zone(zone)
    ms = sorted((m.__class__(**{**m.__dict__, "t": m.t.astimezone(tz)}) for m in ms), key=lambda m: m.t)

    zl = zone_label(zone, ms[0].t)
    days = sorted({m.t.date() for m in ms})
    date_label = days[0].strftime("%a %d %b %Y") if len(days) == 1 else \
        f"{days[0]:%d %b}–{days[-1]:%d %b %Y}"
    title = a.title or _default_title(a.csv, date_label)

    out = Path(a.out) if a.out else Path(a.csv[0]).with_name(Path(a.csv[0]).stem + "_currents.pptx")
    assets = Path(a.assets_dir) if a.assets_dir else out.with_name(out.stem + "_assets")
    assets.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    th = THEMES[a.theme]
    try:
        a.lifetime = parse_window(a.lifetime) if a.lifetime is not None else \
            (180.0 if a.gif_style == "particles" else 60.0)
    except ValueError as e:
        sys.exit(f"--lifetime: {e}")

    units = make_units(a.units, th.strength, max(m.speed_kt for m in ms), a.scale_max)
    kicker = a.kicker or (f"{days[0].day} {days[0]:%B} · current readings" if len(days) == 1 else "current readings")
    if len(days) > 1:
        sys.stderr.write("note: readings span more than one day; hourly slides are per calendar hour.\n")

    try:
        a.reach = parse_distance(a.reach)
    except ValueError as e:
        sys.exit(f"--reach: {e}")
    zoom = build_frame(ms, RIGHT_PANEL[2] / RIGHT_PANEL[3])
    loc = locator_frame(zoom, LEFT_PANEL[2] / LEFT_PANEL[3])
    land, land_note, land_src = load_land(locator_frame(zoom, 1.0, factor=6).bbox_lonlat(0.2), a.land)
    for r in reports[:1]:
        if land_note:
            r.notes.append(land_note)

    gif = assets / "scrubber.gif"
    ginfo = render_gif(ms, th, units, gif, kicker=kicker, title=title, zone_label=zl, zoom=zoom, land=land,
                       lifetime=a.lifetime, fade=a.fade, step_min=a.step, frame_ms=a.frame_ms,
                       style=a.gif_style, reach_m=a.reach)

    avg, avg_info = None, None
    if a.day_average:
        avg = assets / "day_average.gif"
        avg_info = render_average_gif(ms, th, units, avg, kicker=kicker, title=title, zone_label=zl, zoom=zoom,
                                      land=land, reach_m=a.reach, frame_ms=a.frame_ms)

    panels = {}
    for hour, group in hour_groups(ms):
        pth = assets / f"hour_{hour:%Y%m%d_%H}.png"
        size = render_hour_panels(group, ms, zoom, loc, th, units, land, pth)
        panels[hour] = (pth, size)

    n_slides = build_deck(out, th=th, units=units, gif_path=gif, hour_panels=panels, ms=ms, title=title,
                          kicker=kicker, zone_label=zl, sources=[Path(p).name for p in a.csv],
                          lifetime=a.lifetime, average_gif=avg, lifetime_label=fmt_window(a.lifetime))

    summary = {
        "deck": str(out), "gif": str(gif), "assets_dir": str(assets), "slides": n_slides,
        "timezone": zone, "reach_m": a.reach, "readings_used": len(ms), "units": units.name, "land_source": land_src, "land_polygons": len(land),
        "hours": [f"{h:%Y-%m-%d %H}:00" for h, _ in hour_groups(ms)],
        "gif_info": ginfo, "day_average_gif": str(avg) if avg else None, "day_average_info": avg_info,
        "inputs": [{"file": r.path, "rows": r.rows, "used": r.used, "skipped": r.skipped,
                    "time_column": r.time_column, "columns": r.columns, "notes": r.notes} for r in reports],
    }
    if not a.json:
        for r in reports:
            sk = ", ".join(f"{v} {k}" for k, v in r.skipped.items()) or "none"
            sys.stderr.write(f"{Path(r.path).name}: {r.used}/{r.rows} rows used (skipped: {sk}); "
                             f"time from '{r.time_column}'\n")
            for nte in r.notes:
                sys.stderr.write(f"  note: {nte}\n")
        sys.stderr.write(f"Wrote {out} ({n_slides} slides) and {gif}\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
