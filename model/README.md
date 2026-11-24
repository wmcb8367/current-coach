# Current model

The models behind Current Coach's current forecasts. For any venue in the
world it builds several current models from open data, each including
different factors, and uses the drift measurements the app collects to rank
them, correct them and put an uncertainty on the result.

Background, data sources and the plan are in the project document
"Hong Kong current model: research and plan".

## The models

Every model answers the same question, `velocity(lat, lon, time)`, so they
can be compared, swapped and blended (`models.py`).

| Model | Factors | Where it works | Source |
| --- | --- | --- | --- |
| `global` | tide, wind, air pressure | everywhere; fine (100 to 250 m) on United States coasts, coarse (2 to 4 km) elsewhere | NOAA STOFS-2D-Global, read straight from NOAA's open data (`stofs.py`) |
| `venue-tide` | tide, coastline, depths | everywhere | our physics model (`physics.py`) on the venue's coast and depths (`venue.py`, `depths.py`), driven at its open edges by the global model's tide |
| `venue-weather` | tide, weather-driven sea level, the drift along the coast, wind on the venue's own water, coastline, depths, optionally rivers | everywhere | the same physics model, with the global model's full level and current at the open edges and NOAA's wind on the surface |
| official | tide | where a national service publishes tidal streams, such as Hong Kong | loaded from the service's files (`baseline.py`) |
| steady drift of a model | whatever keeps the water moving once the tide is averaged out: the season's coastal current, wind over days, rivers | wherever the parent model covers | `Subtidal` in `models.py`; add it to a tide model with `Blend` |
| `WindDrift` | the surface water going downwind at a fraction of the wind speed | everywhere | `models.py`, with wind from `wind.py`; give it a weight with `Blend` |
| any of the above, tuned live | weights for the venue plus what today's readings show, updated by every reading | wherever there are drifts | `LiveModel` in `live.py` |
| any of the above, corrected | adds what the drifts show; needs many days of drifts | wherever there are drifts | `correction.py` |
| `blend` | everything its members include | wherever two or more models overlap | weighted mix fitted to drifts (`models.py`) |

`build.py` builds the first three for any position:

```
python -m currentmodel.build --lat 22.27 --lon 114.17 --width-km 44 --height-km 34 --out hong-kong
```

The period can be in the past (NOAA's archive goes back about a year) or up
to 7 days ahead. A tide-only physics run of a month can be condensed into
tidal constituents per cell (`HarmonicField` in `fields.py`), which then
predicts the tide for any date without running the solver again.

### The physics model

`physics.py` solves the depth-averaged shallow-water equations on a regular
grid: the tide entering at the open edges, wind on the surface, rivers,
bottom friction, the Earth's rotation and the momentum of the flow itself.
Channels speed up and headlands shed eddies because the equations make them.
It is checked against seven textbook cases with known answers
(`tests/test_physics.py`), including resonance in a channel and the slope
wind sets up on a closed basin.

It is deliberately simple: stair-stepped coasts, no drying flats, and the
depth-averaged flow rather than the surface layer. The live tuning is
there to learn what that misses.

The open edges take both the level and the current of the global model
(Flather's condition, `edge_flow` in `fields.py`). Waves made inside the
venue leave instead of bouncing off the edge, and the current along the
coast outside comes in. For the tide-only model the global current is
stripped to its tide first.

### Depths and coastline

`venue.py` builds the grid. The coastline comes from the global terrain
tiles on AWS Open Data and is good everywhere (about 30 m). Depths come
from GEBCO's 2026 grid (`depths.py`), one value every 450 m for the whole
world, fetched for the venue's box on request. If GEBCO cannot be reached
the tiles' own depths are used, which are surveyed on much of the United
States coast and poor elsewhere: east of Clearwater Bay they give 5 m where
GEBCO and the chart give about 25 m, and that alone made the old physics
tide there wrong.

GEBCO is still coarse. In Hong Kong three quarters of its inshore cells
are estimated from satellite gravity rather than sounded, and channels
narrower than a kilometre are blurred. Better depths (a national chart, a
survey, boats' own depth sounders) plug in through `depth_source` and
nothing else changes. Hong Kong's electronic charts are free but need a
registration and a licence agreement, so they are not wired in.

### The drift correction

`correction.py` fits a Gaussian process to measured minus modelled current.
It learns where a model is too weak, rotated or turns late, the wind-driven
part of the surface current, any steady flow the model lacks, and a
per-user offset for one user's drifter and technique. Each term is a
venue-wide constant plus a local part, so with a handful of drifts it is a
simple bias correction and with many it resolves local structure. Every
prediction comes with an uncertainty that grows away from the drifts, and
every drift is scored against all the others (`suspect_drifts`), which is
the basis for screening bad or fake data in a shared model.

### Tuning live

`live.py` keeps a model up to date as readings arrive. A venue's model is a
few ingredients with weights, for instance the physics model and the wind.
Each new reading does two things:

- It nudges the weights (a Kalman filter step on two or three numbers).
  They start from values that are sensible anywhere (all of the model, 2%
  of the wind) and settle on the venue's own over days.
- It joins today's pattern: the readings of the last hours, each speaking
  for the water within a kilometre or two of where it was taken and fading
  over about half a day. A stream found along one side of the course shows
  in the prediction there at once and leaves the far side alone.

A reading costs under a few milliseconds, so this can run on the phone the
moment a drift ends or on the server as drifts sync. The whole state is the
weights and the recent readings, 7 kB of JSON with 40 recent readings
(`LiveTuner.state()`); replaying stored readings rebuilds it exactly, which
is what a nightly job would do. A reading far from what was expected has
its influence cut (`gate`), so a bad or made-up drift cannot move the model
far: a 12 knot reading moved the prediction by under a tenth of a knot in
the tests. `score_live` replays drifts and scores each prediction made
before its own reading, which is the error a user would have seen.

```python
live = LiveModel({"physics": venue_weather, "wind": WindDrift(wind, lat, lon, 1.0, average_s=6 * 3600)},
                 weights=[1.0, 0.02], weight_sd=[0.5, 0.01])
live.observe(drift)                    # each time a drift ends
u, v = live.velocity(lat, lon, now)    # the tuned current, anywhere in the venue
```

### Choosing between models

`ModelSuite.score(drifts)` scores each model on drifts it has not seen,
holding out whole days, both as it stands and with the correction, and adds
a blend whose weights are fitted on the other days only. The best row is the
venue's default; the rest stay selectable. `pack.py` writes a chosen model's
field on a grid as JSON for the app.

## What is real and what is simulated

Real: the global model's currents and water levels, the coastline and
depths, the physics runs driven by them, and the wind from NOAA's weather
model (`wind.py`). These have been built for Hong Kong and for Biscayne Bay.

Still simulated: the tests of the correction, the scoring and the blend,
which use a venue (`synthetic.py`) whose answer is known.

### Second round: what improved the models and what did not

Every idea was scored against the same 81 readings (described in the next
section) on days it had not seen, and the physics tide against Hong Kong's
official tidal streams at about 500 points (17 to 19 November 2026).

| Model | Error against the readings |
| --- | --- |
| no current at all | 0.36 kn |
| old `venue-weather` as built | 0.31 kn |
| `global` as published | 0.30 kn |
| new `venue-weather` as built (GEBCO depths, global currents at the edges, wind inside) | 0.27 kn |
| best of the first round: steady drift plus wind, weights from the other days | 0.23 kn |
| new `venue-weather` plus 2% of the wind of the last six hours, nothing fitted | 0.19 kn |
| the same, tuned live, each reading predicted from the readings before it | 0.16 kn |

Readings taken minutes apart at the same spot differ by 0.10 to 0.15 knots,
so that is about the floor.

What helped:

- **Depths.** With GEBCO depths the physics tide in the race area agrees
  with the official one (correlation 0.59, up from 0.28; amplitude 0.04
  against 0.07 knots, where the old depths gave 0.11).
- **Open edges that take the global model's current.** Agreement with the
  official tide over the whole model area rose from 0.42 to 0.56 before
  the new depths and 0.60 with them. The physics tide now beats the global
  model's own tide in every region checked (0.60 against 0.55 overall).
- **Live tuning.** With the first round's ingredients it cut the error from
  0.23 to 0.185 knots; most of that is today's pattern, the rest the
  weights. On 23 November the south of the course ran at 0.7 knots and the
  north at 0.2 all day, and nothing but the readings themselves knew.
- **The new physics as the base.** Tuned live it gives 0.16 knots against
  0.185 with the global model's steady drift as the base. Resampling the
  readings, the gain is 0.02 knots (0.01 to 0.035); resampling whole days
  it holds in three cases out of four, so it is likely and not yet certain.
  Its fitted weight is 1.0: its strength is right as built, where the
  global drift needed 0.7.
- **The wind of the last few hours** instead of the wind at that moment:
  0.220 against 0.231 knots (`average_s` of `WindDrift`). Small.
- **Forecasts.** With wind and global currents as forecast one, two and
  three evenings before the day, the error is the same as with after-the-fact
  values (0.22 knots). Five evenings before it rises to 0.26, as the
  forecast wind direction drifts 30 degrees off.

What did not help (each tried, none kept):

- Turning the wind-driven current left or right of the wind (best angle 0).
- A wind term that grows with the square of the wind speed.
- Shelter from land upwind, and scaling the wind term by water depth.
- Wave-driven (Stokes) drift from NOAA's wave model, sea and swell.
- NOAA's ocean circulation model (RTOFS) as the steady drift.
- Bottom friction and eddy viscosity of the physics model: the tide barely
  changes over the plausible range.
- Either tide model as an extra ingredient next to drift and wind. The tide
  in the race area is about 0.1 knots and all readings are from the middle
  of the day.
- Hong Kong's official tidal streams as the model for these readings:
  0.38 knots (depth average) and 0.43 (surface layer), against 0.36 for no
  current at all. They are the reference for the tide, and the tide is not
  what moved these drifters.
- Rain could not be tested: 11 mm fell in the nine days.

Also seen: two boats drift at 1.5 to 1.7% of the wind speed and the third
at over 3% (six readings on one day). What is being timed matters, and the
app should record it.

Known wrong: the tide in the Tathong Channel is a third of the official
strength (0.07 against 0.20 knots) with either set of depths. The harbour
behind it is too coarsely drawn at 200 m cells and GEBCO's depths there are
estimates.

### First round: comparison with measured current

These are the numbers before the second round's changes.

81 readings from three boats on seven days, 15 to 24 November 2026, in the
race area east of Clearwater Bay, Hong Kong. Two more were set aside (one
at 12 knots, one 14 seconds long) and six that appeared in two files were
counted once (`drop_repeats`). Mean measured speed 0.31 knots. Vector
error of each model as built:

| Model | Error |
| --- | --- |
| `venue-tide` | 0.39 kn |
| no current at all | 0.36 kn |
| `venue-weather` | 0.31 kn |
| `global` | 0.30 kn |
| steady drift of `global` (`Subtidal`) | 0.29 kn |
| 2% of the wind, nothing else (`WindDrift`) | 0.24 kn |
| blend of steady drift and wind, weights from the other days | 0.23 kn |

- The readings follow the wind. On both race days the water went
  downwind, west-south-west under an easterly and south under a
  northerly. A blend of 0.8 of the global model's steady drift and 1.6% of
  the wind speed, with both numbers fitted on the other six days, predicts
  a held-out day to 0.23 knots, with no bias in speed and directions
  within 17 degrees.
- How much of that is water and how much is the drifter being blown along
  is not known. It decides what the number means for a keelboat.
- The tide-only physics model is worse than assuming no current, and
  adding either tide model to the blend gains nothing. All seven days were
  sampled around the middle of the day; these readings cannot yet say
  what the tide does there.
- The drift correction (`correction.py`) made the blend worse on held-out
  days (0.28 knots). It has more freedom than seven days can pin down,
  and needs to start simpler when days are few.
- The wind from NOAA's weather model matched 43 winds logged on the water
  to 2 knots and 9 degrees on average.
- The screen for bad readings picked out the 12 knot reading by itself.
- Two boats out on the same day differ: on 24 November one averaged 0.46
  knots and the other 0.25.

Seven days in one season and one wind regime is a small sample.

What the first runs showed before any readings (48 hours from 22 November 2026, 200 m cells):

- In open water the tide-only venue model agrees with the global model that
  drives it: correlation 0.80 in Hong Kong and 0.85 in Biscayne Bay, with a
  difference of 0.2 to 0.3 knots. That checks the plumbing, not the truth.
- Near shore the two differ, as they should, since that is where the global
  model has no detail. Which is right there is for the drifts to say.
- In Hong Kong the venue model moves almost no water through Victoria
  Harbour, where a knot or so is expected at spring tides. The inshore
  depths from open data are rough and the narrow eastern entrance is poorly
  drawn, so treat the harbour as wrong until better depths are in.
- `venue-weather` differs from `venue-tide` by about 0.2 knots and agrees
  less well with the global model (0.60 against 0.75 in a Hong Kong test).
  The global model's full water level and its tide product do not share
  quite the same tide, and the difference drives current at the open edges.
  It stays in the suite as a candidate; do not prefer it without drifts
  that say so.

Still to build:

- An export of synced drifts from the backend (script written, in the
  backend repository; readings so far came as spreadsheets, read by
  `load_readings_csv`).
- The live tuning in the app and on the server: the state is a small JSON
  document per venue, and the update is short enough to port to Swift.
- Readings early and late in the day and on spring tides, to test the tide.
- The Hong Kong Observatory's ten-minute wind (Waglan Island is the nearest
  open-water station) as the live wind in place of the weather model's.
- Finer cells and charted depths for the harbour and the Tathong Channel.
- Wind varying across the venue (`wind.py` gives one series per venue), a
  source of river flow, and drying flats.

## Run it

```
cd model
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,data]"
python -m currentmodel.demo
pytest
```

The tests need no network. `build.py`, `wind.py` and `waves.py` need access
to `*.amazonaws.com`, and `depths.py` to `download.gebco.net`.

## Conventions

- Velocities are metres per second, `u` towards east and `v` towards north.
- Directions in this package are the bearing the water flows *towards*. The
  app stores the bearing it comes *from*; `measurements.py` converts.
- Times are seconds since the Unix epoch, UTC.
- A drift is placed at the middle of its track and the middle of its
  duration, not at the start fix the app records.

## Attribution

- Global currents and water levels: NOAA/NOS STOFS-2D-Global, public domain.
- Wind and waves: NOAA/NCEP Global Forecast System and GFS-Wave, public domain.
- Depths: GEBCO Compilation Group (2026) GEBCO 2026 Grid.
- Coastline, and depths where GEBCO is not used: Terrain Tiles on AWS Open Data, compiled by Mapzen
  from sources listed at
  https://github.com/tilezen/joerd/blob/master/docs/attribution.md
- Tidal stream predictions for Hong Kong, when used: Hydrographic Office,
  Marine Department, HKSAR Government, through DATA.GOV.HK.

None of this is to be used for navigation.
