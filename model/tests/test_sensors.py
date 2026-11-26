import json

import numpy as np
import pytest

from currentmodel import drivers, sensors, tides


def test_weather_station_file_gives_wind_towards_and_pressure():
    text = (
        "STATION|Station_name|DATE|wind_direction|wind_speed|sea_level_pressure|precipitation\n"
        "X|A|2026-11-24T15:00:00|90|10.0|1016.3|\n"
        "X|A|2026-11-24T12:00:00|0|5.0|1017.0|0.4\n"
    )
    parts = sensors.parse_ghcnh(text)
    t, u = parts["wind_u"]
    assert t[0] < t[1]
    assert u[1] == pytest.approx(-10.0)          # from the east: blows west
    assert parts["wind_v"][1][0] == pytest.approx(-5.0)  # from the north: blows south
    assert parts["pressure"][1][1] == pytest.approx(1016.3)
    assert np.nansum(parts["rain"][1]) == pytest.approx(0.4)


def test_station_lists_are_cut_to_the_reach():
    ghcnh = "GHCN_ID,LATITUDE,LONGITUDE,ELEVATION,STATE,NAME\nNEAR,22.18,114.30,60,,WAGLAN\nFAR,40.0,-70.0,1,,BOSTON\n"
    found = sensors.ghcnh_stations(ghcnh, 22.28, 114.33, 60)
    assert [s.id for s in found] == ["NEAR"] and 10 < found[0].distance_km < 13
    ioc = json.dumps([{"Code": "quar", "Location": "Quarry Bay", "Lat": "22.29", "Lon": "114.21"},
                      {"Code": "quar", "Location": "Quarry Bay", "Lat": "22.29", "Lon": "114.21"},
                      {"Code": "x", "Location": "far", "Lat": "0", "Lon": "0"}])
    assert [s.id for s in sensors.ioc_stations(ioc, 22.28, 114.33, 150)] == ["quar"]


def test_hong_kong_feeds_only_near_hong_kong():
    assert sensors.hk_sensors(25.76, -80.19, 80) == []
    names = {s.id for s in sensors.hk_sensors(22.28, 114.33, 15)}
    assert "Tai Miu Wan" in names and "Shek Pik" not in names


def test_latest_tide_feed_is_read_in_hong_kong_time():
    rows = sensors.parse_hk_latest_tide("﻿Tide Station,Date,Time,Height(m)\nQuarry Bay,2026-11-26,06:40,0.80\nWaglan Island,2026-11-26,06:40,----\n", "hko")
    assert len(rows) == 1 and rows[0]["sea_level"] == 0.8
    import datetime as dt
    assert rows[0]["time"] == dt.datetime(2026, 11, 25, 22, 40, tzinfo=dt.timezone.utc).timestamp()


def test_hourly_grid_averages_and_sums():
    grid = np.arange(0.0, 6 * 3600.0, 3600.0)
    t = np.array([100.0, 200.0, 7300.0])
    assert sensors.to_hourly(t, np.array([1.0, 3.0, 5.0]), grid)[0] == pytest.approx(2.0)
    assert sensors.to_hourly(t, np.array([1.0, 3.0, 5.0]), grid, how="sum")[0] == pytest.approx(4.0)
    assert np.isnan(sensors.to_hourly(t, np.array([1.0, 3.0, 5.0]), grid, how="sum")[1])


def test_tidal_fit_recovers_constituents_and_leaves_the_surge():
    t = np.arange(0, 90 * 86400.0, 3600.0)
    hours = (t - 946684800.0) / 3600.0
    tide = 0.8 * np.cos(np.radians(28.9841042) * hours - 1.0) + 0.4 * np.cos(np.radians(15.0410686) * hours)
    surge = 0.2 * np.exp(-(((t - 40 * 86400.0) / (2 * 86400.0)) ** 2))
    fitted, left = tides.residual(t, 1.0 + tide + surge, fit_window_days=90)
    assert np.nanmax(np.abs(left - surge - np.nanmean(left - surge))) < 0.05
    amps = tides.TideFit.fit(t, tide).amplitudes()
    assert amps["M2"] == pytest.approx(0.8, abs=0.01) and amps["K1"] == pytest.approx(0.4, abs=0.01)


def test_window_statistics_look_only_backwards():
    times = np.arange(0.0, 100 * 3600.0, 3600.0)
    series = np.arange(100.0)
    at = np.array([50 * 3600.0])
    assert drivers.window_values(times, series, at, 1, 0)[0] == 50.0
    assert drivers.window_values(times, series, at, 24, 24)[0] == pytest.approx(np.mean(np.arange(3, 27)))
    assert drivers.window_values(times, series, at, 6, 0, "change")[0] == pytest.approx(5.0)
    assert drivers.window_values(times, series, at, 3, 0, "sum")[0] == pytest.approx(49 + 50 + 48)


def test_screening_finds_a_lagged_driver_and_rejects_noise():
    rng = np.random.default_rng(1)
    times = np.arange(0.0, 60 * 86400.0, 3600.0)
    wind = np.convolve(rng.normal(0, 1, times.size), np.ones(24) / 24, "same")
    noise = np.convolve(rng.normal(0, 1, times.size), np.ones(24) / 24, "same")
    at = np.sort(rng.uniform(10 * 86400.0, 59 * 86400.0, 300))
    days = (at // 86400).astype(int)
    driver = drivers.window_values(times, wind, at, 24, 24)
    target = np.column_stack([2.0 * driver + rng.normal(0, 0.05, at.size), rng.normal(0, 0.05, at.size)])
    cands = drivers.candidates({"times": times, "wind": wind, "noise": noise}, at, lags_h=(0, 24), windows_h=(1, 24))
    base, rows = drivers.screen(target, cands, days, permutations=200)
    assert rows[0].candidate.name == "wind mean 24h lag 24h"
    assert rows[0].q < 0.05
    assert all(r.q > 0.05 or r.candidate.series == "wind" for r in rows)
    chosen, errors = drivers.select(target, cands, days, max_terms=3, min_gain=0.002)
    assert chosen[0].name == "wind mean 24h lag 24h" and errors[-1] < 0.5 * errors[0]


def test_record_latest_does_not_repeat_unchanged_values(tmp_path, monkeypatch):
    feed = b"Tide Station,Date,Time,Height(m)\nQuarry Bay,2026-11-26,06:40,0.80\n"

    class Fake(sensors.Fetcher):
        def get(self, url, max_age_s=None):
            return feed

    path = tmp_path / "latest.csv"
    assert sensors.record_latest(path, 22.28, 114.33, Fake()) == 2  # one row from each of the two feeds
    assert sensors.record_latest(path, 22.28, 114.33, Fake()) == 0
