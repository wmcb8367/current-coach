# current-deck

Turns current-measurement CSVs (Current Coach app exports, coach-boat logs, or anything similar) into a slide deck:

1. **Slide 1, an animated GIF time scrubber.** Each current arrow appears at its reading time and fades out an hour later, while a playhead runs along a timeline of the day.
2. **One slide per hour of the day** (venue local time). Each has a locator map with the race area boxed and a zoomed map of that hour's readings. Arrows are coloured by strength and labelled `HH:MM · speed`.

The look copies the SailBoss current-readings slide made for Luther's coach boat (black page, dark green map panels, cyan kicker, lime wordmark, strength-coloured arrows). Pass `--theme m2x` to use the Current Coach app colours instead.

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
- `out/readings_assets/hour_YYYYMMDD_HH.png`: the map panels for each hour

It also prints a JSON summary on stdout (deck path, slide count, time zone, rows used and skipped per file, and which column was used for each field). Progress notes go to stderr. Add `--json` to print only the JSON.

Several CSVs from the same venue can be merged into one deck: `python3 -m current_deck a.csv b.csv -o day1.pptx`.

Python 3.11 or newer. IBM Plex fonts are bundled for the images. The deck's text boxes ask for IBM Plex Mono and IBM Plex Sans, so install the fonts from `current_deck/fonts/` on the presenting computer for an exact match. Otherwise PowerPoint and Keynote substitute a similar font.

## Notes for agents

- Run from `tools/current-deck` (or after `pip install .`). The command needs no prompts and no API keys.
- Read the JSON on stdout. Check `inputs[].used`, `inputs[].skipped` and `inputs[].notes`, and tell the user about skipped rows or assumptions.
- The coastline is downloaded once from GitHub (Natural Earth 1:10m, public domain) and cached in `~/.cache/current-deck`. With no network the maps are drawn without land and a note says so. Use `--land none` to skip it, or `--land shore.geojson` for a sharper shoreline.
- Rendering takes about 10 to 30 seconds, mostly for the GIF. Use `--step 5` for a quicker, choppier GIF.

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
--bins A,B,C            strength colour thresholds in display units (default 0.25,0.5,0.75 kt or 8,15,23 m/min)
--land auto|none|FILE   coastline source
--lifetime MIN          minutes an arrow stays on the GIF (default 60)
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
- `current_deck/land.py`: coastline download, cache and clipping
- `current_deck/deck.py`: the PPTX (python-pptx)
- `current_deck/style.py`: themes and fonts
- `current_deck/cli.py`: command line and JSON summary

The loader's `Measurement` (time, lat, lon, speed in knots, toward degrees) is the hand-off point for wiring this into the Current Coach app or its backend later. The app's `TideMeasurement` maps directly: `speedMetersPerMinute × 60 / 1852` gives knots, and `fromDirection + 180` gives toward.
