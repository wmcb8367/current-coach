import json

import numpy as np

from currentmodel import meters
from currentmodel.baseline import parse_noaa_current_predictions
from currentmodel.geo import KNOT_MS
from currentmodel.sensors import parse_coops

HOUR = 3600.0


def synthetic(days=120, seed=1):
    """A meter whose current is M2 tide + a slow residual that follows the wind at 2%, turned 30 degrees right."""
    rng = np.random.default_rng(seed)
    t = 1.7e9 + np.arange(days * 24) * HOUR
    m2 = np.radians(28.9841042) * (t - 946684800.0) / HOUR
    tide_u, tide_v = 0.3 * np.cos(m2), 0.1 * np.sin(m2)
    # wind: a random walk that changes over days
    wu = np.cumsum(rng.normal(0, 0.4, t.size)); wu = 6 * wu / np.abs(wu).max()
    wv = np.cumsum(rng.normal(0, 0.4, t.size)); wv = 6 * wv / np.abs(wv).max()
    turn = np.radians(-30)  # to the right
    ru = 0.02 * (np.cos(turn) * wu - np.sin(turn) * wv)
    rv = 0.02 * (np.sin(turn) * wu + np.cos(turn) * wv)
    slow = np.convolve(rng.normal(0, 0.02, t.size), np.ones(48) / 48 ** 0.5, "same")  # persistent drift
    u = tide_u + ru + slow + rng.normal(0, 0.01, t.size)
    v = tide_v + rv + rng.normal(0, 0.01, t.size)
    return t, u, v, wu, wv


def test_tidal_split_finds_the_tide():
    t, u, v, *_ = synthetic()
    s = meters.tidal_split(t, u, v)
    assert abs(s.amplitudes_kn["M2"] - np.hypot(0.3, 0.1) / KNOT_MS) < 0.05
    assert s.test_tide_error_kn < s.test_rms_kn


def test_window_mean():
    t = np.arange(10) * HOUR
    x = np.arange(10, dtype=float)
    assert meters.window_mean(t, x, np.array([5 * HOUR]), 3)[0] == 4.0  # hours 3, 4, 5
    x[3:6] = np.nan
    assert np.isnan(meters.window_mean(t, x, np.array([5 * HOUR]), 3)[0])


def test_wind_response_share_and_turn():
    t, u, v, wu, wv = synthetic(days=200)
    s = meters.tidal_split(t, u, v)
    r = meters.wind_response(t, (s.residual_u, s.residual_v), (wu, wv), utc_offset_h=0)
    assert abs(r["share_pct"] - 2.0) < 0.4
    assert abs(r["turn_right_deg"] - 30) < 10


def test_day_ahead_wind_helps_and_tide_only_is_worst():
    t, u, v, wu, wv = synthetic(days=200)
    s = meters.tidal_split(t, u, v)
    out = meters.day_ahead(t, (s.residual_u, s.residual_v), 0, wind=(wu, wv))
    e = out["error_kn"]
    assert out["days"] > 100
    assert e["+ day's wind"] < e["tide only"]
    assert e["last 6 h + wind"] <= e["+ day's wind"] + 1e-3


def test_issue_time_is_the_evening_before():
    day = np.array([20000])
    issued = meters.issue_times(day, utc_offset_h=-5, issue_hour=18)
    # 18:00 local on day 19999 is 23:00 UTC that day
    assert issued[0] == 19999 * 86400 + 23 * HOUR


def test_parse_coops_wind_blows_towards():
    text = json.dumps({"data": [{"t": "2026-11-01 00:00", "s": "5.0", "d": "90.0"}]})  # from the east
    out = parse_coops(text, "wind")
    assert np.isclose(out["wind_u"][1][0], -5.0) and abs(out["wind_v"][1][0]) < 1e-9


def test_parse_noaa_current_predictions_flood_and_ebb():
    text = json.dumps({"current_predictions": {"cp": [
        {"Time": "2026-11-01 00:00", "Velocity_Major": 1.0, "meanFloodDir": 270, "meanEbbDir": 90},
        {"Time": "2026-11-01 00:30", "Velocity_Major": -2.0, "meanFloodDir": 270, "meanEbbDir": 90},
    ]}})
    t, u, v = parse_noaa_current_predictions(text)
    assert t[1] - t[0] == 1800
    assert np.isclose(u[0], -KNOT_MS) and np.isclose(u[1], 2 * KNOT_MS)
