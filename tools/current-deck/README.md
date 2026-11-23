# current-deck

Turns current-measurement CSVs (Current Coach app exports, coach-boat logs, or anything similar) into a slide deck:

1. **Slide 1, an animated GIF time scrubber.** Each current arrow appears at its reading time and fades out an hour later, while a playhead runs along a timeline of the day.
2. **Slide 2, the all-day average.** A looping particle view of every reading of the day averaged together.
3. **One slide per hour of the day** (venue local time). Each has a locator map with the race area boxed and a zoomed map of that hour's readings. Arrows are coloured on one continuous strength gradient (periwinkle and blue for weak, through green and yellow, to orange and magenta for strong, shown in a slim key on the map) and labelled `HH:MM · speed`. Every arrow keeps a visible tail: length is a fixed base plus an amount proportional to speed.

The look copies the SailBoss current-readings slide made for Luther's coach boat (black page, dark green map panels, cyan kicker, lime wordmark, white-dotted arrows). Pass `--theme m2x` to use the Current Coach app colours instead.

Arrows always point **the way the water flows** (set, degrees true). Files that give "from" directions are flipped (toward = from + 180).

## Quick start

```bash
cd tools/current-deck
python3 -m pip install -r requirements.txt        # or: pip install .  (adds a `current-deck` command)

python3 -m current_deck path/to/readings.csv -o out/readings.pptx
```

This writes:

- `out/readings.pptx`: the deck, with the GIF on slide 1 and per-reading detail in each slide's speaker notes
- `out/readings_assets/scrubber.gif`: the animation on its own
- `out/readings_assets/day_average.gif`: the all-day average animation
- `out/readings_assets/hour_YYYYMMDD_HH.png`: the map panels for each hour

It also prints a JSON summary on stdout (deck path, slide count, time zone, rows used and skipped per file, and which column was used for each field). Progress notes go to stderr. Add `--json` to print only the JSON.

Several CSVs from the same venue can be merged into one deck: `python3 -m current_deck a.csv b.csv -o day1.pptx`.

Python 3.11 or newer. IBM Plex fonts are bundled for the images. The deck's text boxes ask for IBM Plex Mono and IBM Plex Sans, so install the fonts from `current_deck/fonts/` on the presenting computer for an exact match. Otherwise PowerPoint and Keynote substitute a similar font.

## Notes for agents

- Run from `tools/current-deck` (or after `pip install .`). The command needs no prompts and no API keys.
- Read the JSON on stdout. Check `inputs[].used`, `inputs[].skipped` and `inputs[].notes`, and tell the user about skipped rows or assumptions.
- The coastline comes from the `basemap-data-hires` package in `requirements.txt`, so install the requirements first. `land_source` in the JSON says which coastline was used. See Coastline below.
- Rendering takes about 10 to 30 seconds, mostly for the GIF. Use `--step 5` for a quicker, choppier GIF.

## The two dials

These two settings control how much the particle views are allowed to infer. Both take plain values.

| Setting | What it does | Examples |
|---|---|---|
| `--lifetime` (or `--window`) | How long a reading stays relevant on the scrubber after it was taken | `--lifetime 45m`, `--lifetime 3h`, `--lifetime all` (never expires) |
| `--reach` | How far from a reading its current may be shown | `--reach 500m`, `--reach 2km`, `--reach 0.5nm` |

Smaller values are stricter and show only what was measured there and then. Larger values join readings into a wider, longer-lived field, at the cost of blending readings taken further apart in space or time.

```bash
# strict: each reading counts for 45 minutes within 500 m
python3 -m current_deck day1.csv --gif-style particles --lifetime 45m --reach 500m -o strict.pptx
# generous: readings stay for the rest of the day and reach 2 km
python3 -m current_deck day1.csv --gif-style particles --lifetime all --reach 2km -o generous.pptx
```

## All-day average slide

Slide 2 is a looping particle view of every reading of the day at once (`day_average.gif` in the assets folder). Where readings overlap, speed and direction are averaged, whatever the time between them, so it shows the day's typical flow and hides how the tide changed. `--reach` applies here too. `--no-day-average` leaves the slide out.

## Particle animation

`--gif-style particles` swaps the arrows on slide 1 for drifting particles with trails, in the manner of a weather-map wind layer. The hourly slides still use arrows.

The particles follow a field interpolated from the readings that are live at that moment on the scrubber. It is deliberately conservative so it doesn't suggest measurements that were never taken:

- A reading only affects water within `--reach` of where it was taken (default 1000 m). Its weight falls smoothly to zero at that distance.
- Beyond the reach of every live reading nothing is drawn. A faint dotted circle marks each reading's reach.
- Where the reaches of two or more live readings overlap, speed is the weighted mean and direction is the weighted mean of unit vectors (so 350° and 010° average to 000°).
- A reading stops contributing when it fades off the scrubber (`--lifetime`, `--fade`).

Particle speed on screen is scaled for legibility, not real time. Colour is the interpolated speed on the same gradient as the key.

## Coastline

`--land auto` uses the first of these that is available:

1. **GSHHG full resolution** (about 100 m), read from the `basemap-data-hires` pip package listed in `requirements.txt`. It needs no network once installed. The package is about 90 MB.
2. **Natural Earth 1:10m**, downloaded once from GitHub and cached in `~/.cache/current-deck`. It is coarse (about 1 km) and looks blocky on the zoomed map. A note in the JSON says when this fallback was used.

`--land FILE.geojson` takes your own land polygons, for example an OpenStreetMap extract, which is sharper again. `--land none` draws no land.

## Input columns

Column names are matched case-insensitively, ignoring punctuation. Any field can be forced with `--map field=column`.

| Field | Recognised names (first match wins) |
|---|---|
| time | any column named like `time`, `time_*`, `*_time`, `timestamp`, `datetime`. A zone in the name (`time_hkt`, `time_hong_kong`, `time_utc`, `time_pacific`, ...) is used for times that carry no offset. |
| lat / lon | `latitude`, `lat` / `longitude`, `lon`, `lng`, `long` |
| speed | `current_kt`, `speed_knots`, `speed_kt`, `current_speed`, ... Units come from the name: `*_ms`/`*_m_s` = m/s, `*_cm_s` = cm/s, `*_m_min` = m/min, anything else = knots. Override with `--speed-units`. |
| direction | toward: `direction_toward_deg`, `current_toward_deg`, `set`, `set_deg`, ...; from: `current_from_deg`, `direction_from_deg`, `from_deg`, .... A bare `direction` column is read as "toward" unless you pass `--dir-convention from`. |
| note | `position_note`, `note`, `location`, `label`, ... joined with `race`/`leg` when present |
| valid | `is_valid`. Rows marked false are skipped unless `--keep-invalid` is given. |
| wind (optional) | `wind_from_deg`/`wind_direction`, `wind_kt`/`wind_speed`. These go in the speaker notes. |

Rows with no current speed or direction, or no position, are skipped and counted in the summary.

### Time zone and date

Slides show the venue's local time. The zone is decided in this order:

1. `--tz Area/City` (for example `--tz Asia/Hong_Kong`)
2. A zone named in a time column. When several are named (as in the app export, with `time_utc`, `time_hong_kong` and `time_pacific`), the one closest to the readings' longitude wins.
3. A fixed UTC offset estimated from longitude, with a warning.

Times with no date (such as `10:45` in a coach log) take the date in the file name (`..._2026_11_23.csv`), or `--date YYYY-MM-DD`.

## Options

```
--title TEXT            deck title (default: from the file name)
--kicker TEXT           small cyan line above titles (default: "23 November · current readings")
--tz ZONE               venue time zone (IANA name)
--date YYYY-MM-DD       date for files whose times have no date
--map FIELD=COLUMN      force a column (repeatable)
--theme sailboss|m2x    SailBoss (default) or Current Coach app colours
--units kt|m/min        speed units on the slides (default kt; the Current Coach app works in m/min)
--scale-max N           speed at the top of the colour gradient (default: strongest reading rounded up)
--land auto|gshhg|naturalearth|none|FILE   coastline source (see Coastline below)
--gif-style arrows|particles   slide-1 animation style (default arrows)
--reach DISTANCE        how far a reading's current may be shown in the particle views: 800, 800m, 1.5km, 0.5nm (default 1000m)
--lifetime TIME         how long a reading stays on the scrubber: 90, 90m, 3h, 1h30m, or all (default 60m for arrows, 3h for particles)
--no-day-average        leave out the all-day average slide
--fade MIN              minutes of fade-out at the end of that time (default 20)
--step MIN              GIF minutes per frame (default: about 150 frames)
--frame-ms MS           GIF milliseconds per frame (default 90)
--assets-dir DIR        where the GIF and PNGs go (default: <deck>_assets)
--json                  print only the JSON summary
```

## Examples

`examples/` has two synthetic files in the two formats seen so far:

- `app_export_sample.csv`: Current Coach export (`time_utc`/`time_hong_kong`, `speed_knots`, `direction_from_deg`/`direction_toward_deg`, `is_valid`)
- `coach_log_2026_11_20.csv`: hand-kept coach-boat log (`time_hkt` with no date, `current_kt`, `current_from_deg`, wind, some rows with no current)

```bash
python3 -m current_deck examples/coach_log_2026_11_20.csv examples/app_export_sample.csv -o /tmp/demo.pptx
```

Don't commit real client readings here, because this repository is public.

## Tests

```bash
python3 -m pip install pytest
python3 -m pytest tests
```

## Layout of the code

- `current_deck/loader.py`: CSV reading, column matching, time zones, units, from/toward
- `current_deck/render.py`: map panels, strength colours, label placement, the scrubber GIF
- `current_deck/land.py`: coastline sources, clipping and smoothing
- `current_deck/particles.py`: bounded interpolation and the particle animation
- `current_deck/deck.py`: the PPTX (python-pptx)
- `current_deck/style.py`: themes and fonts
- `current_deck/cli.py`: command line and JSON summary

The loader's `Measurement` (time, lat, lon, speed in knots, toward degrees) is the hand-off point for wiring this into the Current Coach app or its backend later. The app's `TideMeasurement` maps directly: `speedMetersPerMinute × 60 / 1852` gives knots, and `fromDirection + 180` gives toward.
