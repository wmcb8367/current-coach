# Current model

The model behind Current Coach's current forecasts. It starts from an official
tidal stream prediction and corrects it with the drift measurements the app
collects.

Background, data sources and the plan are in the project document
"Hong Kong current model: research and plan".

## How it works

The prediction at a position and time is a sum of layers:

1. **Tidal baseline** (`baseline.py`). Predictions at fixed points and regular
   times, interpolated to any position and time. For Hong Kong this is the
   Hydrographic Office tidal stream prediction: about 1,200 points every
   15 minutes.
2. **Conditions correction.** The wind-driven part of the surface current,
   learned from drifts and a wind record.
3. **Venue correction** (`correction.py`). A Gaussian process fitted to
   measured minus baseline. It learns where the baseline is too weak, rotated
   or turns late, and any steady flow the baseline lacks. Every prediction
   comes with an uncertainty that grows away from the drifts.
4. **Personal offset.** A per-user term that absorbs one user's drifter and
   technique. It is applied only when predicting for that user.

Layers 2 to 4 are one model. Each term's coefficients are a venue-wide
constant plus a local part, so with a handful of drifts the model is a simple
bias correction and with many it resolves local structure.

`scoring.py` measures accuracy by holding out whole days. `pack.py` writes the
corrected field on a grid as JSON for the app. The model also scores every
drift against all the others (`suspect_drifts`), which is the basis for
screening bad or fake data in a shared model.

## What is real and what is simulated

No real data is connected yet. The tests and the demo run on a simulated venue
(`synthetic.py`) whose baseline has known faults, to check that the model
recovers them. The numbers the demo prints say the method works on a problem
with a known answer. They say nothing yet about accuracy in Hong Kong.

Still to build:

- An importer that converts the Hydrographic Office download into the tidy
  CSV format `TidalStreamField.from_long_csv` reads.
- An export of synced drifts from the backend.
- Importers for Hong Kong Observatory wind, rain and observed tide.

## Run it

```
cd model
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m currentmodel.demo
pytest
```

## Conventions

- Velocities are metres per second, `u` towards east and `v` towards north.
- Directions in this package are the bearing the water flows *towards*. The
  app stores the bearing it comes *from*; `measurements.py` converts.
- Times are seconds since the Unix epoch, UTC.
- A drift is placed at the middle of its track and the middle of its
  duration, not at the start fix the app records.

## Attribution

Tidal stream baseline data for Hong Kong comes from the Hydrographic Office,
Marine Department, HKSAR Government, through DATA.GOV.HK. It is not to be used
for navigation.
