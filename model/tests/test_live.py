"""Live tuning: weights and today's pattern, reading by reading."""

import json

import numpy as np
import pytest

from currentmodel.geo import KNOT_MS
from currentmodel.live import LiveModel, LiveTuner
from currentmodel.measurements import Drift
from currentmodel.models import FunctionModel

HOUR = 3600.0


def _readings(rng, n, weights, offset=(0.0, 0.0), noise=0.01):
    """Readings of ``weights`` times two ingredients that vary from reading to reading."""
    values = rng.normal(0.0, [[0.1], [5.0]], size=(n, 2, 2))  # a current of 0.1 m/s, a wind of 5 m/s
    truth = np.einsum("k,nkc->nc", weights, values) + np.asarray(offset)
    return values, truth + rng.normal(0.0, noise, size=(n, 2))


def test_weights_settle_on_the_truth():
    rng = np.random.default_rng(1)
    values, seen = _readings(rng, 60, [0.7, 0.015])
    tuner = LiveTuner(["drift", "wind"], [1.0, 0.02], [0.5, 0.01], pattern_sd=0.0, noise=0.01)
    for k in range(60):
        tuner.update(values[k], k * 600.0, *seen[k])
    assert tuner.current_weights["drift"] == pytest.approx(0.7, abs=0.05)
    assert tuner.current_weights["wind"] == pytest.approx(0.015, abs=0.001)
    assert tuner.readings == 60


def test_with_no_readings_the_answer_is_the_starting_weights():
    tuner = LiveTuner(["drift", "wind"], [1.0, 0.02], [0.5, 0.01])
    u, v, sd = tuner.predict([(0.1, 0.0), (0.0, -5.0)], 0.0)
    assert (u, v) == pytest.approx((0.1, -0.1))
    assert sd > 0.05  # and it says how unsure it is


def test_todays_pattern_is_local_and_fades():
    tuner = LiveTuner(["drift"], [1.0], [1e-6], noise=0.02)
    nothing = [(0.0, 0.0)]
    for k in range(4):  # four drifts in one spot find 0.2 m/s to the east that no ingredient has
        tuner.update(nothing, k * 600.0, 0.2, 0.0, x=0.0, y=0.0)
    now = 4 * 600.0
    here, _, sure = tuner.predict(nothing, now, 0.0, 0.0)
    near, _, _ = tuner.predict(nothing, now, 1500.0, 0.0)
    far, _, unsure = tuner.predict(nothing, now, 6000.0, 0.0)
    assert here == pytest.approx(0.2, abs=0.03)
    assert 0.3 * here < near < 0.8 * here
    assert abs(far) < 0.01
    assert sure < 0.5 * unsure
    later, _, _ = tuner.predict(nothing, now + 12 * HOUR, 0.0, 0.0)
    next_week, _, _ = tuner.predict(nothing, now + 7 * 24 * HOUR, 0.0, 0.0)
    assert 0.2 * here < later < 0.5 * here
    assert abs(next_week) < 0.001


def test_one_offset_for_the_whole_venue_when_reach_is_none():
    tuner = LiveTuner(["drift"], [1.0], [1e-6], noise=0.02, pattern_reach_m=None)
    for k in range(4):
        tuner.update([(0.0, 0.0)], k * 600.0, 0.2, 0.0, x=0.0, y=0.0)
    far, _, _ = tuner.predict([(0.0, 0.0)], 2400.0, 6000.0, 0.0)
    assert far == pytest.approx(0.2, abs=0.03)


def test_a_wild_reading_is_not_believed():
    def tuned(with_wild):
        rng = np.random.default_rng(2)
        values, seen = _readings(rng, 30, [1.0, 0.02])
        tuner = LiveTuner(["drift", "wind"], [1.0, 0.02], [0.5, 0.01], noise=0.02)
        report = None
        for k in range(30):
            tuner.update(values[k], k * 600.0, *seen[k], x=0.0, y=0.0)
            if with_wild and k == 20:
                report = tuner.update(values[k], k * 600.0 + 60.0, 12 * KNOT_MS, 0.0, x=0.0, y=0.0)
        return tuner, report

    clean, _ = tuned(False)
    hit, report = tuned(True)
    assert report["surprise"] > 10 and report["trust"] < 0.05
    assert hit.mean == pytest.approx(clean.mean, abs=0.05 * np.abs(clean.mean).max() + 0.002)
    u_clean, _, _ = clean.predict([(0.1, 0.0), (0.0, 0.0)], 30 * 600.0)
    u_hit, _, _ = hit.predict([(0.1, 0.0), (0.0, 0.0)], 30 * 600.0)
    assert abs(u_hit - u_clean) < 0.1 * KNOT_MS  # a 12 knot reading moved the answer by under a tenth of a knot


def test_state_survives_saving_and_replaying():
    rng = np.random.default_rng(3)
    values, seen = _readings(rng, 25, [0.8, 0.018], offset=(0.05, 0.0))
    where = rng.uniform(-2000.0, 2000.0, size=(25, 2))
    make = lambda: LiveTuner(["drift", "wind"], [1.0, 0.02], [0.5, 0.01])
    straight = make()
    for k in range(25):
        straight.update(values[k], k * 900.0, *seen[k], x=where[k, 0], y=where[k, 1])
    # Stop after ten readings, save as text, carry on in a fresh tuner.
    first = make()
    for k in range(10):
        first.update(values[k], k * 900.0, *seen[k], x=where[k, 0], y=where[k, 1])
    resumed = make().restore(json.loads(json.dumps(first.state())))
    for k in range(10, 25):
        resumed.update(values[k], k * 900.0, *seen[k], x=where[k, 0], y=where[k, 1])
    probe = ([(0.1, 0.05), (3.0, -4.0)], 25 * 900.0, 500.0, -300.0)
    assert resumed.predict(*probe) == pytest.approx(straight.predict(*probe), rel=1e-9)


def test_old_readings_are_dropped_from_the_pattern():
    tuner = LiveTuner(["drift"], [1.0], [0.1], pattern_memory_s=HOUR, max_recent=50)
    for k in range(200):
        tuner.update([(0.1, 0.0)], k * 600.0, 0.1, 0.0)
    assert len(tuner.recent) <= 50
    assert min(r[0] for r in tuner.recent) >= 199 * 600.0 - 5 * HOUR


def test_live_model_answers_like_any_model_and_learns_from_drifts():
    drift_model = FunctionModel(lambda lat, lon, t: (np.full(np.shape(lat), 0.10), np.zeros(np.shape(lat))), 22.28, 114.36)
    wind_model = FunctionModel(lambda lat, lon, t: (np.zeros(np.shape(lat)), np.full(np.shape(lat), -6.0)), 22.28, 114.36)
    live = LiveModel({"drift": drift_model, "wind": wind_model}, weights=[1.0, 0.02], weight_sd=[0.5, 0.01], noise=0.01)
    u, v = live.velocity(22.28, 114.36, 0.0)
    assert (float(u), float(v)) == pytest.approx((0.10, -0.12))
    # The water here really takes 3% of the wind.
    drifts = [Drift(str(k), "a", k * 600.0, 22.28 + 0.002 * (k % 5), 114.36, 0.10, -0.18, 0.02, 180.0) for k in range(30)]
    reports = live.replay(drifts)
    assert reports[-1]["miss"] < 0.3 * reports[0]["miss"]
    u, v = live.velocity(np.array([22.28, 22.285]), np.array([114.36, 114.36]), 30 * 600.0)
    assert v == pytest.approx([-0.18, -0.18], abs=0.01)
    assert live.tuner.current_weights["wind"] > 0.024


def test_score_live_never_lets_a_drift_predict_itself():
    from currentmodel.scoring import score_live

    still = FunctionModel(lambda lat, lon, t: (np.zeros(np.shape(lat)), np.zeros(np.shape(lat))), 22.28, 114.36)
    live = LiveModel({"nothing": still}, weights=[1.0], weight_sd=[1e-6], noise=0.01)
    # One spot, a steady 0.2 m/s that the model knows nothing of.
    drifts = [Drift(str(k), "a", k * 600.0, 22.28, 114.36, 0.2, 0.0, 0.02, 180.0) for k in range(12)]
    result = score_live(live, drifts)
    # The first drift is missed in full; the later ones are caught, so the error falls well below 0.2 m/s.
    assert 0.2 * 0.2 < result["vector_rmse_kn"] * KNOT_MS < 0.5 * 0.2
    assert live.tuner.readings == 12
