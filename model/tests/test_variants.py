import numpy as np
import pytest

from currentmodel.correction import Conditions
from currentmodel.variants import ScalarPush, WindMemory, score_variants

HOUR = 3600.0


def test_wind_memory_is_zero_in_steady_wind_and_largest_when_a_blow_stops():
    times = np.arange(0, 10 * 86400.0, HOUR)
    wind_v = np.where(times < 5 * 86400.0, -10.0, 0.0)  # five days of northerly, then calm
    memory = WindMemory(Conditions(times, np.zeros_like(times), wind_v), 22.0, 114.0, memory_s=72 * HOUR)
    plain = WindMemory(Conditions(times, np.zeros_like(times), wind_v), 22.0, 114.0, memory_s=72 * HOUR, eased=False)
    assert abs(float(plain.velocity(22.0, 114.0, 4 * 86400.0)[1])) < 1e-9
    steady = memory.velocity(22.0, 114.0, 4 * 86400.0)
    stopped = memory.velocity(22.0, 114.0, 5 * 86400.0 + 7 * HOUR)
    assert abs(float(steady[1])) < 1e-9
    assert float(stopped[1]) < -5.0  # the old wind's direction; a negative weight makes it a return flow
    turned = WindMemory(Conditions(times, np.zeros_like(times), wind_v), 22.0, 114.0, turn_deg=90.0)
    u, v = turned.velocity(22.0, 114.0, 5 * 86400.0 + 7 * HOUR)
    assert float(u) < -5.0 and abs(float(v)) < 1e-6  # a quarter turn clockwise of south is west


def test_scalar_push_uses_the_lagged_value():
    push = ScalarPush(np.array([0.0, HOUR, 2 * HOUR]), np.array([0.0, 1.0, 2.0]), 22.0, 114.0, towards_deg=0.0, lag_s=HOUR)
    u, v = push.velocity(22.0, 114.0, 2 * HOUR)
    assert float(v) == pytest.approx(1.0) and abs(float(u)) < 1e-12


def test_scoring_prefers_the_variant_with_the_ingredient_that_matters():
    rng = np.random.default_rng(3)
    n = 120
    days = np.repeat(np.arange(8), 15)
    times = days * 86400.0 + np.tile(np.arange(15) * 600.0, 8)
    physics = rng.normal(0, 0.05, (n, 2))
    wind = np.repeat(rng.normal(0, 8.0, (8, 2)), 15, axis=0)
    memory = np.repeat(rng.normal(0, 6.0, (8, 2)), 15, axis=0)
    truth = physics + 0.015 * wind - 0.02 * memory + rng.normal(0, 0.02, (n, 2))
    values = {"physics": physics, "wind": wind, "memory": memory, "memory-turned": np.column_stack([-memory[:, 1], memory[:, 0]])}
    rows = {r["variant"]: r for r in score_variants(values, truth[:, 0], truth[:, 1], days, times)}
    assert rows["cc-wind-memory-turned"]["day_ahead_kn"] < 0.8 * rows["cc-wind"]["day_ahead_kn"]
    assert rows["cc-wind-memory"]["weights"]["memory"] < -0.01


def test_sensor_persistence_carries_the_evening_mean_forward():
    from currentmodel.variants import SensorPersistence

    t = np.arange(0, 48) * 3600.0
    u = np.where(t < 20 * 3600, 0.1, 0.3)
    p = SensorPersistence(t, u, np.zeros_like(u), 0.0, 0.0, window_s=4 * 3600, issued=lambda x: np.floor(x / 86400) * 86400 - 3600)
    pu, pv = p.velocity(0.0, 0.0, np.array([30 * 3600.0, 40 * 3600.0]))
    # both are on the second day, predicted from the 4 h before 23:00 on the first: hours 20 to 23 read 0.3
    assert np.allclose(pu, 0.3) and np.allclose(pv, 0.0)


def test_new_variants_are_scored_when_their_ingredients_exist():
    from currentmodel.variants import score_variants

    rng = np.random.default_rng(0)
    n = 80
    days = np.repeat(np.arange(8), 10)
    physics = rng.normal(0, 0.2, (n, 2))
    nearby = rng.normal(0, 0.1, (n, 2))
    u = physics[:, 0] + 0.8 * nearby[:, 0] + rng.normal(0, 0.02, n)
    v = physics[:, 1] + 0.8 * nearby[:, 1] + rng.normal(0, 0.02, n)
    rows = {r["variant"]: r for r in score_variants({"physics": physics, "wind": np.zeros((n, 2)), "nearby": nearby}, u, v, days, np.arange(n) * 600.0)}
    assert rows["cc-nearby-sensor"]["day_ahead_kn"] < rows["cc-wind"]["day_ahead_kn"]
