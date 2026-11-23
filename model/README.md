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
| `venue-tide` | tide, coastline, depths | everywhere | our physics model (`physics.py`) on the venue's coast and depths (`venue.py`), driven at its open edges by the global model's tide |
| `venue-weather` | tide, weather-driven sea level, coastline, depths, optionally local wind and rivers | everywhere; experimental | the same physics model, with the global model's weather-driven rise and tilt of the sea added at the open edges |
| official | tide | where a national service publishes tidal streams, such as Hong Kong | loaded from the service's files (`baseline.py`) |
| any of the above, corrected | adds what the drifts show | wherever there are drifts | `correction.py` |
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
depth-averaged flow rather than the surface layer. The drift correction is
there to learn what that misses.

### Depths and coastline

`venue.py` builds the grid from the global terrain tiles on AWS Open Data.
The coastline is good everywhere (about 30 m). Depths are only as good as
the open data for that coast: surveyed on much of the United States coast,
and elsewhere a smooth global grid that misses channels and shoals narrower
than about a kilometre. Close to shore the builder discards the global
grid's numbers and uses the nearest open-water depth, which keeps harbours
open and is a guess. Hong Kong is in the second group. Better depths
(charts, surveys, a boat's own depth sounder) plug in through
`depth_source` and nothing else changes.

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

### Choosing between models

`ModelSuite.score(drifts)` scores each model on drifts it has not seen,
holding out whole days, both as it stands and with the correction, and adds
a blend whose weights are fitted on the other days only. The best row is the
venue's default; the rest stay selectable. `pack.py` writes a chosen model's
field on a grid as JSON for the app.

## What is real and what is simulated

Real: the global model's currents and water levels, the coastline and
depths, and the physics runs driven by them. These have been built for Hong
Kong and for Biscayne Bay.

Not yet real: any comparison with measured current. No drifts and no
official tidal streams are connected, so nothing here says how accurate any
model is at a real venue. The correction, the scoring and the blend are
tested on a simulated venue (`synthetic.py`) whose answer is known.

What the runs do show (48 hours from 22 November 2026, 200 m cells):

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

- An export of synced drifts from the backend, to score the models.
- An importer for the Hong Kong Hydrographic Office tidal streams, and
  importers for Hong Kong Observatory wind, rain and observed tide.
- A source of gridded wind (for the local wind term) and of river flow.
- Better Hong Kong depths.
- Open edges that also use the global model's currents, and drying flats.

## Run it

```
cd model
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,data]"
python -m currentmodel.demo
pytest
```

The tests need no network. `build.py` needs access to `*.amazonaws.com`.

## Conventions

- Velocities are metres per second, `u` towards east and `v` towards north.
- Directions in this package are the bearing the water flows *towards*. The
  app stores the bearing it comes *from*; `measurements.py` converts.
- Times are seconds since the Unix epoch, UTC.
- A drift is placed at the middle of its track and the middle of its
  duration, not at the start fix the app records.

## Attribution

- Global currents and water levels: NOAA/NOS STOFS-2D-Global, public domain.
- Coastline and depths: Terrain Tiles on AWS Open Data, compiled by Mapzen
  from sources listed at
  https://github.com/tilezen/joerd/blob/master/docs/attribution.md
- Tidal stream predictions for Hong Kong, when used: Hydrographic Office,
  Marine Department, HKSAR Government, through DATA.GOV.HK.

None of this is to be used for navigation.
