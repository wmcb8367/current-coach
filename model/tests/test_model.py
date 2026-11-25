"""The model against a simulated venue with known answers."""

import numpy as np
import pytest

from currentmodel import VenueCorrection, cross_validate_by_day, drifts_from_records, score_baseline
from currentmodel.geo import KNOT_MS, to_latlon
from currentmodel.pack import build_pack
from currentmodel.synthetic import (
    BASELINE_LATE_S,
    BASELINE_ROTATION_DEG,
    BASELINE_SCALE,
    LAT0,
    LON0,
    OUTFLOW,
    WIND_FACTOR,
    make_app_records,
    make_world,
)


@pytest.fixture(scope="module")
def world():
    return make_world()


@pytest.fixture(scope="module")
def records(world):
    return make_app_records(world)


@pytest.fixture(scope="module")
def drifts(records):
    return drifts_from_records(records)


@pytest.fixture(scope="module")
def model(world, drifts):
    return VenueCorrection(world.baseline).fit(drifts, world.conditions)


def test_correction_beats_the_baseline_on_held_out_days(world, drifts):
    result = cross_validate_by_day(world.baseline, drifts, world.conditions)
    assert result["days"] == 12
    assert result["skill"] > 0.25
    assert result["model"]["direction_mae_deg"] < result["baseline"]["direction_mae_deg"]


def test_error_bars_are_honest(world, drifts):
    result = cross_validate_by_day(world.baseline, drifts, world.conditions)
    # 86% of errors should fall inside 2 sigma. Far below means overconfident.
    assert 0.78 <= result["within_2_sigma"] <= 0.97


def test_model_recovers_what_was_wrong_with_the_baseline(model):
    lat, lon = to_latlon(-1500.0, -700.0, LAT0, LON0)  # open water, away from the headland
    summary = model.venue_summary(lat, lon)
    assert summary["speed_scale"] == pytest.approx(1.0 / BASELINE_SCALE, abs=0.12)
    assert summary["rotation_deg"] == pytest.approx(BASELINE_ROTATION_DEG, abs=5.0)
    assert summary["baseline_late_min"] == pytest.approx(BASELINE_LATE_S / 60.0, abs=6.0)
    assert summary["wind_factor"] == pytest.approx(WIND_FACTOR, abs=0.004)
    assert summary["mean_u"] == pytest.approx(OUTFLOW[0], abs=0.015)


def test_model_finds_the_headland_the_baseline_smooths_out(model):
    open_water = model.venue_summary(*to_latlon(-1500.0, -700.0, LAT0, LON0))
    headland = model.venue_summary(*to_latlon(300.0, 500.0, LAT0, LON0))
    assert headland["speed_scale"] > open_water["speed_scale"] + 0.3


def test_uncertainty_grows_away_from_the_drifts(world, model, drifts):
    time = drifts[0].time
    near = model.predict(drifts[0].lat, drifts[0].lon, time, world.conditions)
    far_lat, far_lon = to_latlon(-2600.0, 1900.0, LAT0, LON0)
    far = model.predict(far_lat, far_lon, time, world.conditions)
    assert float(far.sigma) > float(near.sigma)


def test_prediction_outside_coverage_is_empty(world, model):
    lat, lon = to_latlon(20_000.0, 0.0, LAT0, LON0)
    p = model.predict(lat, lon, world.start + 86_400.0, world.conditions)
    assert np.isnan(p.u) and np.isnan(p.sigma)


def test_fake_drifts_are_flagged_and_do_not_wreck_the_model(world, records):
    rng = np.random.default_rng(7)
    fakes = []
    for i, base in enumerate(rng.choice(records, size=8, replace=False)):
        fake = dict(base, clientId=f"fake-{i}")
        fake["speedMetersPerMinute"] = 2.0 * KNOT_MS * 60.0
        fake["fromDirectionDegrees"] = float(rng.uniform(0.0, 360.0))
        fakes.append(fake)
    clean = drifts_from_records(records)
    mixed = drifts_from_records(records + fakes)

    fitted = VenueCorrection(world.baseline).fit(mixed, world.conditions)
    flagged = {drift_id for drift_id, _ in fitted.suspect_drifts()}
    assert sum(f"fake-{i}" in flagged for i in range(8)) >= 7
    assert sum(not name.startswith("fake") for name in flagged) <= 4

    # Score both models on the honest drifts at truth: the fakes should cost little.
    cols_lat = np.array([d.lat for d in clean])
    cols_lon = np.array([d.lon for d in clean])
    cols_time = np.array([d.time for d in clean])
    truth_u, truth_v = world.truth(cols_lat, cols_lon, cols_time)
    reference = VenueCorrection(world.baseline).fit(clean, world.conditions)

    def error(fit):
        p = fit.predict(cols_lat, cols_lon, cols_time, world.conditions)
        return float(np.sqrt(np.mean((p.u - truth_u) ** 2 + (p.v - truth_v) ** 2)))

    assert error(fitted) < 1.25 * error(reference)


def test_personal_offset_is_learned_and_kept_out_of_the_shared_model(world, records):
    # Give one coach a drifter that reads 0.10 m/s too far east every time.
    biased = []
    for record in records:
        drift = drifts_from_records([record])[0]
        if drift.user_id != "coach-b":
            biased.append(drift)
            continue
        biased.append(type(drift)(**{**drift.__dict__, "u": drift.u + 0.10}))
    assert any(d.user_id == "coach-b" for d in biased)

    fitted = VenueCorrection(world.baseline).fit(biased, world.conditions)
    sample = next(d for d in biased if d.user_id == "coach-b")
    shared = fitted.predict(sample.lat, sample.lon, sample.time, world.conditions)
    personal = fitted.predict(sample.lat, sample.lon, sample.time, world.conditions, user="coach-b")
    other = fitted.predict(sample.lat, sample.lon, sample.time, world.conditions, user="coach-a")
    # The model can tell the coaches apart: the gap between them is the bias.
    assert float(personal.u - other.u) == pytest.approx(0.10, abs=0.04)
    # It cannot know which of them is right, so the shared model sits between.
    assert float(other.u) < float(shared.u) < float(personal.u)


def test_few_drifts_still_give_a_usable_correction(world, records):
    few = drifts_from_records(records[:10])
    fitted = VenueCorrection(world.baseline).fit(few, world.conditions)
    p = fitted.predict(few[0].lat, few[0].lon, few[0].time, world.conditions)
    assert np.isfinite(p.u) and np.isfinite(p.sigma) and float(p.sigma) > 0.0


def test_baseline_score_reports_knots(world, drifts):
    result = score_baseline(world.baseline, drifts)
    assert result["n"] == len(drifts)
    assert 0.1 < result["vector_rmse_kn"] < 1.0


def test_pack_is_plain_json_with_gaps_outside_coverage(world, model):
    import json

    lats = np.linspace(LAT0 - 0.01, LAT0 + 0.01, 3)
    lons = np.array([LON0, LON0 + 0.5])  # the second column is far outside coverage
    pack = build_pack(model, "simulated", lats, lons, [world.start + 86_400.0], world.conditions)
    frame = json.loads(json.dumps(pack))["frames"][0]
    assert len(frame["speedKnots"]) == 3 and len(frame["speedKnots"][0]) == 2
    assert frame["speedKnots"][1][0] is not None and frame["speedKnots"][1][1] is None
    assert frame["time"].endswith("Z")
