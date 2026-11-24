"""The model suite: several models for one venue, ranked by drifts they have not seen."""

import numpy as np
import pytest

from currentmodel import synthetic
from currentmodel.geo import to_latlon, to_xy
from currentmodel.measurements import drifts_from_records
from currentmodel.models import Blend, FunctionModel, ModelSuite, format_scores


@pytest.fixture(scope="module")
def venue():
    world = synthetic.make_world(days=30)
    drifts = drifts_from_records(synthetic.make_app_records(world))

    def tide(lat, lon, time):
        x, y = to_xy(lat, lon, synthetic.LAT0, synthetic.LON0)
        su, sv = synthetic._stream(x, y, headland=True)
        s = synthetic.tide_signal(np.asarray(time, dtype=float))
        return su * s, sv * s

    def tide_and_wind(lat, lon, time):
        u, v = tide(lat, lon, time)
        wu, wv = world.conditions.wind(time)
        return u + synthetic.WIND_FACTOR * wu, v + synthetic.WIND_FACTOR * wv

    def mistimed(lat, lon, time):
        return tide(lat, lon, np.asarray(time, dtype=float) + 3.1 * 3600.0)  # a quarter of a tide out

    suite = ModelSuite(utc_offset_hours=8.0)
    suite.add("official", world.baseline, ["tide"], "coarse published tidal streams")
    suite.add("physics-tide", FunctionModel(tide, synthetic.LAT0, synthetic.LON0), ["tide", "bathymetry"])
    suite.add(
        "physics-tide-wind",
        FunctionModel(tide_and_wind, synthetic.LAT0, synthetic.LON0),
        ["tide", "bathymetry", "wind"],
    )
    suite.add("mistimed", FunctionModel(mistimed, synthetic.LAT0, synthetic.LON0), ["tide"])
    return world, drifts, suite


def test_models_are_ranked_by_what_they_include(venue):
    world, drifts, suite = venue
    rows = suite.score(drifts, world.conditions)
    raw = {r["name"]: r["raw"]["vector_rmse_kn"] for r in rows}
    # Adding the wind to the right tide helps; a tide a quarter-cycle out is the worst on offer.
    assert raw["physics-tide-wind"] < min(raw["physics-tide"], raw["official"])
    assert raw["mistimed"] > max(raw["physics-tide"], raw["official"])
    assert rows[-1]["name"] == "mistimed"
    assert rows[0]["name"] in ("physics-tide-wind", "blend")
    assert "physics-tide-wind" in format_scores(rows)


def test_drift_correction_helps_every_imperfect_model(venue):
    world, drifts, suite = venue
    rows = {r["name"]: r for r in suite.score(drifts, world.conditions)}
    for name in ("official", "physics-tide"):
        assert rows[name]["corrected"]["vector_rmse_kn"] < 0.85 * rows[name]["raw"]["vector_rmse_kn"]
    # A better model still ends up better after correction: the correction is not a substitute for physics.
    assert rows["physics-tide-wind"]["corrected"]["vector_rmse_kn"] <= rows["official"]["corrected"]["vector_rmse_kn"]
    for row in rows.values():
        assert 0.7 < row["within_2_sigma"] <= 1.0


def test_blend_leans_on_the_good_models_and_ignores_the_wrong_one(venue):
    world, drifts, suite = venue
    rows = {r["name"]: r for r in suite.score(drifts, world.conditions)}
    weights = rows["blend"]["weights"]
    assert weights["mistimed"] < 0.15
    assert weights["physics-tide-wind"] > 0.5
    best_single = min(r["raw"]["vector_rmse_kn"] for n, r in rows.items() if n != "blend")
    assert rows["blend"]["raw"]["vector_rmse_kn"] < best_single + 0.02  # weights fitted on other days only


def test_serving_model_can_be_any_entry_or_the_blend(venue):
    world, drifts, suite = venue
    lat, lon = to_latlon(np.array([-400.0]), np.array([300.0]), synthetic.LAT0, synthetic.LON0)
    time = np.array([drifts[10].time])
    truth = world.truth(lat, lon, time)
    for name in ("official", "blend"):
        p = suite.fit(name, drifts, world.conditions, optimise=False).predict(lat, lon, time, world.conditions)
        assert np.hypot(p.u - truth[0], p.v - truth[1])[0] < 0.12
        assert p.sigma[0] > 0


def test_only_drifts_every_model_covers_are_scored(venue):
    world, drifts, suite = venue
    narrow = ModelSuite(utc_offset_hours=8.0)
    narrow.add("official", world.baseline, ["tide"])

    def west_only(lat, lon, time):
        u, v = world.baseline.velocity(lat, lon, time)
        inside = np.asarray(lon) < synthetic.LON0
        return np.where(inside, u, np.nan), np.where(inside, v, np.nan)

    narrow.add("west", FunctionModel(west_only, synthetic.LAT0, synthetic.LON0), ["tide"])
    rows = narrow.score(drifts, world.conditions, blend=False)
    assert 0 < rows[0]["n"] < len(drifts)
    assert rows[0]["n"] == rows[1]["n"]


def test_blend_can_rescale_a_model_that_runs_weak():
    world = synthetic.make_world(days=10)
    drifts = drifts_from_records(synthetic.make_app_records(world, sessions=6))
    weak = FunctionModel(lambda la, lo, t: tuple(0.5 * c for c in world.truth(la, lo, t)), synthetic.LAT0, synthetic.LON0)
    assert Blend.fit([weak], drifts).weights[0] == pytest.approx(2.0, abs=0.1)


def test_subtidal_keeps_the_steady_drift_and_drops_the_tide():
    from currentmodel.models import LUNAR_DAY_S, Subtidal

    def tide_and_drift(lat, lon, time):
        time = np.asarray(time, dtype=float)
        covered = (time >= 0.0) & (time <= 5 * 86_400.0)
        swing = 0.5 * np.sin(2 * np.pi * time / (LUNAR_DAY_S / 2))
        return np.where(covered, 0.1 + swing, np.nan), np.where(covered, -0.05 + 0.3 * swing, np.nan)

    steady = Subtidal(FunctionModel(tide_and_drift, 22.3, 114.3))
    u, v = steady.velocity(22.3, 114.3, np.array([2.0, 2.5, 3.0]) * 86_400.0)
    assert np.allclose(u, 0.1, atol=1e-3) and np.allclose(v, -0.05, atol=1e-3)
    # Too close to the end of the record for a whole lunar day: no answer.
    u, _ = steady.velocity(22.3, 114.3, 4.9 * 86_400.0)
    assert np.isnan(u)


def test_a_blend_finds_how_much_of_the_wind_the_surface_follows():
    from currentmodel.correction import Conditions
    from currentmodel.measurements import Drift
    from currentmodel.models import Blend, WindDrift

    times = np.arange(0.0, 6 * 86_400.0, 3600.0)
    conditions = Conditions(times, 8.0 * np.cos(times / 40_000.0), 6.0 * np.sin(times / 55_000.0))
    steady = FunctionModel(lambda lat, lon, t: (np.full(np.shape(t), -0.10), np.full(np.shape(t), -0.05)), 22.3, 114.3)
    wind = WindDrift(conditions, 22.3, 114.3)
    when = np.linspace(3600.0, 5 * 86_400.0, 40)
    wind_u, wind_v = conditions.wind(when)
    drifts = [
        Drift(str(i), "a", t, 22.3, 114.3, -0.10 + 0.03 * wu, -0.05 + 0.03 * wv, 0.02, 200.0)
        for i, (t, wu, wv) in enumerate(zip(when, wind_u, wind_v))
    ]
    blend = Blend.fit([steady, wind], drifts, ["steady", "wind"])
    # The steady flow is taken whole and the wind model, written for 2%, is scaled to 3%.
    assert blend.weights == pytest.approx([1.0, 1.5], abs=1e-6)
    assert np.isnan(wind.velocity(22.3, 114.3, times[-1] + 10.0)[0])
