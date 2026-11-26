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

### Predicting a day ahead

`dayahead.py` builds tomorrow's current the evening before. By then the
global ocean model has forecast the week ahead and several weather models
have forecast the wind, so the whole chain can run on forecasts: read them,
run the venue physics model on them (three minutes for the Hong Kong box),
and add the share of the surface water that follows the wind. The result is
a `LiveModel`, so on the day each reading on the water corrects it.

```
python -m currentmodel.dayahead --lat 22.28 --lon 114.36 --width-km 44 --height-km 38 \
    --out tomorrow --cache cache --state live.json
```

```python
from currentmodel.dayahead import day_ahead
tomorrow = day_ahead(lat, lon, 44e3, 38e3, start, end, cache_dir="cache", state=yesterdays_state)
u, v = tomorrow.velocity(lat, lon, time)     # any place and time of the day ahead
tomorrow.model.observe(drift)                # on the day
```

The wind comes from `forecast_wind`, the mean of several weather models:
by default ECMWF's IFS and AIFS (`ecmwf_wind`, read from the centre's open
data on Google Cloud or AWS) and NOAA's GFS. `open_meteo_wind` reads any
model Open-Meteo carries, among them Germany's ICON, and with `days_before`
what each model forecast one to seven days earlier, which is how the
forecasts were scored. Its free service is for non-commercial use; a paid
key is an argument.

### Publishing maps for the app

The app draws a venue's current as a moving map and lets the sailor switch
between models. It cannot run the models, so `publish.py` runs them each
evening and writes what the app needs: for each model and each UTC day, the
current on a regular grid (300 m by default) at every hour, packed into one
small file (about 0.3 MB), plus a manifest saying which models and days
exist.

```
python -m currentmodel.publish --venue hong-kong --name "Hong Kong" \
    --lat 22.28 --lon 114.36 --width-km 44 --height-km 38 --time-zone Asia/Hong_Kong \
    --out maps --cache cache                      # into a folder
CURRENT_MODELS_UPLOAD_TOKEN=... python -m currentmodel.publish ... --server https://example.com
```

Six models are published: `day-ahead` (physics run on the forecasts plus the
wind-following surface layer; the default), `physics`, `tide`, `global`
(NOAA's model as published), `wind` (2% of the forecast wind alone) and
`wind-memory` (Current Coach's `cc-wind-memory`: day-ahead plus the water
that flows back after days of wind ease, weights from the Hong Kong
readings; see the fourth round below). The
simple ones are there on purpose: a model has earned its place only if it
beats them against readings.

No published map sends water through the coast. The wind-following layer is
one figure for the whole venue and the global model does not know this
coast, so on their own they point straight at whatever land lies downstream.
Before a day is packed, `shore.py` takes out the part of every model's
current that crosses the shore: all of it at the shore, less further off,
none beyond 600 m. Open water is left as the model had it. On the first
Hong Kong readings this moved no model's score by more than 0.002 kn, and
cut the current pointing into land at the shoreline by about three quarters
(for the default model, from 0.09 kn to 0.02 kn on a windy day).

Without `--days` it is the evening's run: tomorrow and the day after, from
the forecasts in hand, and today as well the first time. With
`--days 2026-11-23,2026-11-24` each past day is built from what had been
issued the evening before it (18:00 by the sun at the venue). Either way a
day's map is what the model said before the day began, so when the server
scores a model against readings it scores a real prediction. The manifest
keeps ten days.

With `--server` the files go to the portal (`PUT /api/current-models/...`
with the publishing token), which stores them privately and serves them only
to accounts allowed to see them. `.github/workflows/current-models.yml` does
this every evening; add a venue by adding an entry to its list. The file
format is described at the top of `publish.py`; the app reads it in
`CurrentCoach/CurrentModels/CurrentField.swift` and the portal in
`lib/current-models/field.ts`.

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

### Fourth round: sensors around the venue, and the Current Coach variants

The aim is a three-tier model: the physics model on the best depths (tier
1); every open sensor around the venue, searched for what explains the
physics model's misses (tier 2); and each user's readings tuning their own
copy on the day (tier 3, `live.py`). This round built tier 2 and a set of
named variants, each carrying one idea, to be scored side by side.

**Finding the sensors (`sensors.py`).** `discover(lat, lon)` asks every
catalogue which of its stations lie within reach and writes a manifest;
`collect(manifest, start, end)` reads them all onto one hourly table. It is
the same step for any venue:

```
python -m currentmodel.sensors --lat 22.28 --lon 114.33 --out hong-kong-sensors.json --cache cache
```

Catalogues: weather stations worldwide (NCEI GHCNh: wind, pressure, rain),
tide gauges worldwide (UHSLC hourly for decades, IOC near real time), NOAA
CO-OPS tide gauges and current meters (US), NDBC buoys, river discharge
from GloFAS (the largest rivers within reach, found by probing a grid),
and for Hong Kong the Observatory's and Marine Department's gauges and the
Observatory's daily rain. Feeds that give only their latest value grow a
history with `record_latest`, run on a schedule. Around the Hong Kong race
area it found 48 sensors: 19 tide gauges (3 with history), 24 weather
stations (only Waglan Island reports wind and pressure through this
archive), 4 rivers including the Pearl River (mean 7,100 m3/s), and no
current meter: no fixed current meter in Hong Kong publishes open data.

**Tide gauges as a long target (`tides.py`).** A gauge's level minus its
own fitted tide is the residual: what wind, pressure and rivers did to the
sea. At Quarry Bay it is 13 cm (standard deviation, Jan 2025 to Nov 2026),
mostly slow (11 cm in the daily mean). It is 16,000 hours of target where
the drifts are 96 readings.

**Screening drivers (`drivers.py`).** Every sensor becomes candidate
drivers (its mean over 6 hours to 7 days, its change, lagged 0 to 72
hours), each tested alone on held-out weeks or days, with a permutation
test that shuffles whole days and Benjamini-Hochberg q-values over all
candidates.

- Quarry Bay residual a day ahead (2,734 times, 99 weeks): yesterday's
  level alone takes the error from 13.0 to 9.5 cm. On top of it, what the
  wind did (north-south wind over the last one to two days and how it has
  changed) brings 9.0 cm with what is known the evening before and 7.8 cm
  with the day's wind known (a perfect forecast). Rain and river discharge
  added nothing measurable. The wind's history and its easing move the sea,
  which is the mechanism behind the pile-up idea.
- What physics plus wind miss in the 96 readings (304 candidates, 8 days):
  the best candidates are again changes in wind over one to four days and
  the slope between Shek Pik and Quarry Bay, each cutting the error by
  about 12%, but none survives the correction for testing so many
  (q 0.57). Eight days cannot single out a driver among hundreds.

**The variants (`variants.py`).** Scored on 96 readings, 15 to 26 Nov 2026.
Day ahead: each day predicted with weights learned on the other days only.
Live: tuned reading by reading.

| Variant | Ideas | Day ahead | Live |
| --- | --- | --- | --- |
| no current | | 0.346 kn | |
| `cc-physics` | physics model | 0.265 kn | 0.177 kn |
| `cc-wind` | + surface water following the wind of the last 6 h | 0.228 kn | 0.165 kn |
| `cc-wind-memory` | + return flow after a blow eases | **0.219 kn** | **0.162 kn** |
| `cc-wind-memory-turned` | + that return turned by the Earth's rotation | 0.226 kn | 0.163 kn |
| `cc-sea-level` | + tide gauge surplus and its change | 0.313 kn | 0.177 kn |
| `cc-all` | all of the above | 0.290 kn | 0.176 kn |

- The wind memory term is the mean wind of the last three days, scaled by
  how far the wind has since eased. Its fitted weight is negative with any
  one day left out (-0.011 to -0.045; leaving out 26 Nov weakens it most,
  so that day carries much of the evidence): after a blow, the water comes back against the old wind,
  at about 3% of the old wind's speed once it has fully eased. On 26 Nov
  (light wind after a week of 15 to 23 kn north-easterlies) the readings
  ran 0.20 kn toward 66 degrees, against the light wind; it cut that day's
  error from 0.29 to 0.26 kn, and 21 Nov's from 0.22 to 0.18. It made 18
  Nov worse (0.19 to 0.29), a day that followed only a light easterly.
  Resampling whole days, it beats `cc-wind` in 81% of cases: likely, not
  yet established. Two forms were tried (the plain difference of old and
  recent wind scored 0.254) and three memories (48, 72, 96 h); the best is
  kept, so the 0.219 is slightly flattering.
- The tide gauge terms make the day-ahead prediction worse: six more
  weights than eight days can pin down.
- Physics plus 1.5% of the six-hour wind with nothing fitted scores 0.205
  kn on all 96 readings (0.19 on the first 81); 26 Nov is the hardest day
  so far (0.29).

**More variants (same 96 readings, same day-ahead test).** Each adds one
idea to `cc-wind-memory`; "better" is the share of whole-day resamples in
which it beats `cc-wind-memory`.

| Variant | Idea | Day ahead | Live | Better |
| --- | --- | --- | --- | --- |
| `cc-wind-memory` | (reference) | 0.219 kn | 0.162 kn | |
| `cc-tide-timing` | physics tide shifted in time | 0.219 kn | 0.162 kn | 94%, gain under 0.001 kn |
| `cc-coastal-drift` | + 0.22 of the global model's 25-hour mean drift | 0.216 kn | 0.162 kn | 100%, gain 0.004 kn |
| `cc-boat-windage` | each boat its own share of the wind | 0.214 kn | 0.159 kn | 99%, gain 0.006 kn |
| score-weighted ensemble | each day, the variants weighted by their errors on earlier days | 0.218 kn | | |

- The tide here is about 0.03 kn at the race area, so its timing cannot
  matter to these readings: the fit asks for 12 minutes earlier and gains
  nothing. It belongs in the set for venues with real tidal streams.
- The global model's steady drift helps a little at a fifth of its
  strength: the physics run already carries most of the coastal current
  through its open edges.
- Per-boat windage is the clearest of the small gains. The coach boat's
  drifter goes with 0.9% of the wind and the race log's with 1.5% (the
  second boat's 2.6% rests on one day). How far a drifter is blown is a
  property of the drifter, so it is learned per user in their personal
  model, and the shared model should be fitted on water, not windage.
- The ensemble does no better than its best member: with eight days, its
  weights are mostly noise.
- All these gains are under 0.01 kn, a twentieth of the error that is
  left. The leftover is mostly what differs from day to day and place to
  place within the course, not a missing venue-wide factor.

What this data can and cannot support: 96 readings on 8 days, all near the
middle of the day in one 7 by 5 km area. Each day is close to one sample of
the weather, so the honest sample size is 8. That is enough to rank a model
with one extra weight and not enough to choose among hundreds of sensor
signals; the long gauge record carries that search until there are more
days of readings. Each new day of readings is worth more than more
readings on the same day, and days after a change of wind are worth most.

### Third round: one day ahead

The question was how good the prediction can be the evening before, and
which wind forecast serves it best.

**Wind forecasts.** Past forecasts of 14 weather models (Open-Meteo's
archive of earlier runs) were scored against the anemometer on Waglan
Island, the open-water station 10 km south of the race area: 2,328 times
from three-hourly reports, January 2025 to November 2026. The measure is the
vector error of the six-hour mean wind, which is what drives the current,
after one scale factor per model fitted on other months (the anemometer
stands 60 m above the sea). Seven models had forecasts for the whole period.

| Six-hour mean wind, error in kn | latest run | 1 day ahead | 2 days | 3 days | off by over 10 kn, 1 day ahead |
|---|---|---|---|---|---|
| GFS (NOAA) | 5.1 | 6.0 | 6.6 | 6.9 | 8.4% of the time |
| ICON (Germany) | 4.6 | 5.1 | 5.6 | 6.2 | 3.0% |
| ECMWF IFS | 4.3 | 5.0 | 5.7 | 5.9 | 3.6% |
| ECMWF AIFS (machine-learned) | 4.9 | 5.0 | 5.3 | 5.4 | 2.4% |
| mean of IFS and AIFS | 4.4 | 4.8 | 5.2 | 5.3 | 2.1% |
| mean of IFS, AIFS and GFS (the default) | 4.3 | 4.9 | 5.4 | 5.5 | 1.8% |
| mean of IFS, AIFS and ICON | 4.2 | 4.6 | 5.0 | 5.3 | 1.3% |

- The three leaders a day ahead are IFS, AIFS and ICON; GFS is a fifth
  worse and is badly wrong more than twice as often. Canada's GEM, Japan's
  GSM and China's model score near GFS (5.8, 6.0, 5.8 a day ahead).
- A mean of models beats each of its members. The best mean a day ahead is
  closer to the measured wind than GFS's latest run.
- AIFS loses the least with lead time, so it matters more two and three
  days out.
- When the three leaders disagree most (the top third of their spread) the
  mean's error is 5.2 kn against 4.3 kn otherwise: a mild warning sign.
- The same ranking holds in the north-east monsoon months alone and in
  September to November 2026 alone.

**The current, the evening before.** For each of the seven days of
readings the whole chain was rebuilt from what had been issued by 18:00
local time the day before (`issued_before`): the global model's forecast at
the open edges, forecast wind inside the physics model, and 2% of the
forecast six-hour mean wind added. Nothing was fitted to the readings.

| Against the 81 readings | error, kn |
|---|---|
| No current | 0.36 |
| Built after the fact from best estimates (second round) | 0.19 |
| Day ahead, GFS wind | 0.19 |
| Day ahead, mean of IFS, AIFS and GFS (the default) | 0.20 |
| Day ahead, mean of IFS and AIFS | 0.22 |
| On the day, each reading correcting the next | 0.16 |

- A day of lead costs nothing measurable. The physics run on forecasts
  differs from the run on best estimates by 0.02 kn at the readings.
- These seven days do not show the better wind forecasts giving a better
  current: GFS did as well as any. The difference comes from two days with
  12 readings (18 and 21 November) on which the measured current was weak
  and did not follow the wind, so the source forecasting less wind scored
  better; Waglan measured 13 and 16 kn on 19 and 21 November, where the
  ECMWF models said 12 and 11 and GFS said 7. A 5 kn wind error is about
  0.1 kn of current, so the wind forecast is a small part of the 0.19 kn on
  an ordinary day and matters on the days a forecast is badly wrong. The
  default takes the mean of three for that reason; more days of readings
  will settle it, and the source is one argument.
- What to carry from one day to the next: little. Weights retuned by a few
  readings made the next day worse (0.19 to 0.22 kn with the live tuner's
  usual freedom), so the day-ahead model holds them five times tighter; with
  that, carrying them scored 0.19. What the water did yesterday beyond the
  model (the pattern) did not help the next day at any memory from 12 hours
  to 10 days.
- The six-hour mean remains the best window with forecast wind (3 to 6
  hours, against none, 12 or 24). The readings preferred 1.5% of the wind
  to 2% with every source, by 0.01 to 0.03 kn; the tuner moves towards it.
- On the day, the first reading is predicted no better than the evening
  before (0.24 kn), and every later reading is (0.19 to 0.15 kn).

`wind.py` had a fault this round uncovered: asked for hours beyond the
newest published run it returned nothing, so a build for tomorrow ran with
the wind frozen at its last value. It now continues with the newest run's
forecast, as `stofs.py` already did.

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
to `*.amazonaws.com`, `depths.py` to `download.gebco.net`, `ecmwf_wind` to
`storage.googleapis.com` and `open_meteo_wind` to `api.open-meteo.com` and
`previous-runs-api.open-meteo.com`.

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
- Wind forecasts: ECMWF open data (IFS and AIFS), CC BY 4.0; other models
  through Open-Meteo (https://open-meteo.com, CC BY 4.0, free service for
  non-commercial use), among them ICON from Deutscher Wetterdienst.
- Wind measurements used for scoring: Waglan Island, Hong Kong Observatory,
  from NOAA's Global Historical Climatology Network hourly.
- Depths: GEBCO Compilation Group (2026) GEBCO 2026 Grid.
- Coastline, and depths where GEBCO is not used: Terrain Tiles on AWS Open Data, compiled by Mapzen
  from sources listed at
  https://github.com/tilezen/joerd/blob/master/docs/attribution.md
- Tidal stream predictions for Hong Kong, when used: Hydrographic Office,
  Marine Department, HKSAR Government, through DATA.GOV.HK.

None of this is to be used for navigation.
