"""Forecast wind from several weather models and the day-ahead model, tested without a network."""

import json

import numpy as np
import pytest

from currentmodel import dayahead, wind
from currentmodel.correction import Conditions
from currentmodel.geo import KNOT_MS
from currentmodel.measurements import Drift
from currentmodel.models import FunctionModel

HOUR = 3600.0
DAY0 = 1_795_000_000 // 21_600 * 21_600.0  # a model run time


def steady(u, v, start=DAY0, hours=48):
    times = start + HOUR * np.arange(hours + 1)
    return Conditions(times, np.full(times.size, float(u)), np.full(times.size, float(v)))


# ------------------------------------------------------------------- readers


def test_ecmwf_index_gives_the_byte_range_of_each_field():
    lines = [
        {"param": "2t", "levtype": "sfc", "_offset": 0, "_length": 50},
        {"param": "10u", "levtype": "sfc", "number": "3", "_offset": 50, "_length": 10},  # an ensemble member: not wanted
        {"param": "10u", "levtype": "sfc", "_offset": 100, "_length": 40},
        {"param": "10v", "levtype": "sfc", "_offset": 300, "_length": 60},
    ]
    found = wind.ecmwf_ranges("\n".join(json.dumps(line) for line in lines), list(wind.ECMWF_FIELDS))
    assert found == {("10u", "sfc"): (100, 140), ("10v", "sfc"): (300, 360)}
    with pytest.raises(ValueError, match="10v at sfc"):
        wind.ecmwf_ranges(json.dumps(lines[2]), list(wind.ECMWF_FIELDS))


def test_ecmwf_files_are_named_by_run_and_forecast_hour():
    url = wind._ecmwf_url("aifs-single")(DAY0, 24)
    stamp = "2026111806"
    assert url.endswith(f"/20261118/06z/aifs-single/0p25/oper/{stamp}0000-24h-oper-fc.grib2")


def test_ecmwf_reader_moves_to_the_second_mirror_when_the_first_refuses(monkeypatch):
    asked = []

    def series(url_of, wanted, *args, **options):
        asked.append(url_of(DAY0, 0))
        if len(asked) == 1:
            raise OSError("slow down")
        return np.array([DAY0, DAY0 + HOUR]), {wanted[0]: np.array([1.0, 2.0]), wanted[1]: np.array([3.0, 4.0])}

    monkeypatch.setattr(wind, "point_series", series)
    record = wind.ecmwf_wind(22.0, 114.0, DAY0, DAY0 + HOUR)
    assert asked[0].startswith(wind.ECMWF_MIRRORS[0]) and asked[1].startswith(wind.ECMWF_MIRRORS[1])
    assert np.allclose(record.wind_u, [1.0, 2.0]) and np.allclose(record.wind_v, [3.0, 4.0])


def test_open_meteo_answer_becomes_one_record_per_model():
    hourly = {
        "time": ["2026-11-18T06:00", "2026-11-18T07:00", "2026-11-18T08:00"],
        "wind_speed_10m_icon_global": [5.0, 5.0, 5.0],
        "wind_direction_10m_icon_global": [0.0, 90.0, 180.0],  # from north, east, south
        "wind_speed_10m_ecmwf_ifs025": [4.0, None, 6.0],
        "wind_direction_10m_ecmwf_ifs025": [270.0, None, 270.0],
        "wind_speed_10m_kma_gdps": [None, None, None],
        "wind_direction_10m_kma_gdps": [None, None, None],
    }
    found = wind.open_meteo_series(hourly, ["icon_global", "ecmwf_ifs025", "kma_gdps"])
    assert set(found) == {"icon_global", "ecmwf_ifs025"}  # a model with nothing is left out
    icon = found["icon_global"]
    assert icon.times[0] == DAY0
    assert np.allclose(icon.wind_u, [0.0, -5.0, 0.0], atol=1e-9) and np.allclose(icon.wind_v, [-5.0, 0.0, 5.0], atol=1e-9)
    assert np.allclose(found["ecmwf_ifs025"].wind_u, [4.0, 6.0])  # a westerly blows towards the east; the gap is dropped


def test_open_meteo_single_model_has_plain_names():
    hourly = {"time": ["2026-11-18T06:00", "2026-11-18T07:00"], "wind_speed_10m_previous_day1": [3.0, 3.0], "wind_direction_10m_previous_day1": [180.0, 180.0]}
    found = wind.open_meteo_series(hourly, ["gfs_global"], "_previous_day1")
    assert np.allclose(found["gfs_global"].wind_v, [3.0, 3.0])


def test_blend_is_the_mean_over_the_hours_all_cover():
    a = steady(4.0, 0.0, DAY0, 24)
    b = steady(0.0, 2.0, DAY0 + 6 * HOUR, 24)
    both = wind.blend_wind([a, b])
    assert both.times[0] == DAY0 + 6 * HOUR and both.times[-1] == DAY0 + 24 * HOUR
    assert np.allclose(both.wind_u, 2.0) and np.allclose(both.wind_v, 1.0)
    with pytest.raises(ValueError):
        wind.blend_wind([a, steady(1.0, 1.0, DAY0 + 100 * HOUR, 5)])


def test_forecast_wind_leaves_out_a_source_that_fails(monkeypatch):
    issued = {}

    def ecmwf(lat, lon, start, end, model="ifs", cache_dir=None, issued_before=None):
        issued[model] = issued_before
        if model == "ifs":
            raise FileNotFoundError("not published")
        return steady(6.0, 0.0)

    def gfs(lat, lon, start, end, step_hours=3, cache_dir=None, issued_before=None):
        issued["gfs"] = issued_before
        return steady(2.0, 0.0)

    monkeypatch.setattr(wind, "ecmwf_wind", ecmwf)
    monkeypatch.setattr(wind, "gfs_wind", gfs)
    said = []
    record = wind.forecast_wind(22.0, 114.0, DAY0, DAY0 + 24 * HOUR, ("ifs", "aifs-single", "gfs"), issued_before=DAY0 + 12 * HOUR, log=said.append)
    assert np.allclose(record.wind_u, 4.0)  # the mean of the two that answered
    assert any("ifs" in line for line in said)
    # Each run is published hours after it starts, which is taken off the issue time.
    assert issued == {"ifs": DAY0 + 4 * HOUR, "aifs-single": DAY0 + 6 * HOUR, "gfs": DAY0 + 7 * HOUR}
    def none(*args, **options):
        raise FileNotFoundError("none")

    monkeypatch.setattr(wind, "ecmwf_wind", none)
    monkeypatch.setattr(wind, "gfs_wind", none)
    with pytest.raises(FileNotFoundError):
        wind.forecast_wind(22.0, 114.0, DAY0, DAY0 + 24 * HOUR)


def test_hours_ahead_of_the_newest_run_come_from_its_forecast(monkeypatch):
    """Asked in real time, the runs for tomorrow do not exist yet: the newest published run answers."""
    newest = DAY0 + 6 * HOUR
    asked = []

    def get(url, start=None, end=None):
        cycle, hour = url.removesuffix(".idx").split("|")
        if float(cycle) > newest:
            raise FileNotFoundError(url)
        if url.endswith(".idx"):
            return b"1:0:d=x:UGRD:10 m above ground:anl:\n2:100:d=x:VGRD:10 m above ground:anl:\n"
        asked.append((float(cycle), int(hour)))
        return f"{(float(cycle) - DAY0) / HOUR + int(hour)}".encode()

    monkeypatch.setattr(wind, "_get", get)
    monkeypatch.setattr(wind, "_value_at", lambda message, lat, lon, circular=False: float(message.decode()))
    monkeypatch.setattr(wind._time, "time", lambda: DAY0 + 14 * HOUR)  # two runs later than the newest published
    wanted = [("UGRD", wind.LEVEL)]
    times, values = wind.point_series(lambda c, h: f"{c}|{h}", wanted, 22.0, 114.0, DAY0, DAY0 + 36 * HOUR, step_hours=6)
    assert np.allclose(times, DAY0 + 6 * HOUR * np.arange(7))  # the whole period, a day beyond the newest run
    assert np.allclose(values[wanted[0]], [0, 6, 12, 18, 24, 30, 36])
    assert max(cycle for cycle, _ in asked) == newest and (newest, 30) in asked


# ---------------------------------------------------------------- day ahead


def test_day_ahead_model_is_physics_plus_a_share_of_the_mean_wind():
    physics = FunctionModel(lambda lat, lon, time: (np.full(np.shape(lat), 0.10), np.zeros(np.shape(lat))), 22.0, 114.0)
    model = dayahead.assemble(physics, steady(0.0, -8.0), 22.0, 114.0)
    u, v = model.velocity(22.0, 114.0, DAY0 + 12 * HOUR)
    assert u == pytest.approx(0.10) and v == pytest.approx(-0.02 * 8.0)
    assert model.tuner.current_weights == {"physics": 1.0, "wind": 0.02}


def test_weights_move_slowly_between_days_and_state_carries_over():
    physics = FunctionModel(lambda lat, lon, time: (np.full(np.shape(lat), 0.10), np.zeros(np.shape(lat))), 22.0, 114.0)
    record = steady(0.0, -8.0)
    today = dayahead.assemble(physics, record, 22.0, 114.0)
    # Three readings in which the water follows the wind at 4% instead of 2%.
    for i in range(3):
        today.observe(Drift(id=f"d{i}", user_id="a", lat=22.0, lon=114.0, time=DAY0 + (10 + i) * HOUR, duration_s=300.0, u=0.10, v=-0.04 * 8.0, sigma=0.03 * KNOT_MS))
    learned = today.tuner.current_weights["wind"]
    assert 0.02 < learned < 0.025  # pulled towards the readings, but three of them do not rewrite the model
    tomorrow = dayahead.assemble(physics, record, 22.0, 114.0, state=json.loads(json.dumps(today.tuner.state())))
    assert tomorrow.tuner.current_weights["wind"] == pytest.approx(learned)
    # A day later today's pattern has faded: the prediction is close to the weights alone.
    u, v = tomorrow.velocity(22.0, 114.0, DAY0 + 36 * HOUR)
    assert v == pytest.approx(-learned * 8.0, abs=0.02)


def test_day_ahead_reads_the_wind_then_runs_the_physics(monkeypatch):
    calls = {}

    def fake_wind(lat, lon, start, end, sources, cache_dir=None, issued_before=None, log=None):
        calls.setdefault("wind", []).append((tuple(sources), start, end, issued_before))
        if tuple(sources) != ("gfs",):
            raise FileNotFoundError("not reachable")
        return steady(3.0, 0.0, start, int((end - start) / HOUR))

    class Suite(dict):
        pass

    def fake_build(lat, lon, width_m, height_m, start, end, **options):
        calls["build"] = dict(start=start, end=end, **options)
        return Suite({"venue-weather": FunctionModel(lambda la, lo, t: (np.zeros(np.shape(la)), np.full(np.shape(la), 0.2)), lat, lon)}), "grid"

    monkeypatch.setattr(dayahead, "forecast_wind", fake_wind)
    monkeypatch.setattr(dayahead, "build_suite", fake_build)
    start, end = DAY0 + 24 * HOUR, DAY0 + 48 * HOUR
    result = dayahead.day_ahead(22.0, 114.0, 20e3, 20e3, start, end, issued_before=DAY0 + 10 * HOUR, dx=400.0)
    # The named models failed, so GFS stood in; the wind reaches back far enough for spin-up and the six-hour mean.
    assert [c[0] for c in calls["wind"]] == [dayahead.WIND_SOURCES, ("gfs",)]
    assert calls["wind"][-1][1] == start - 12 * HOUR - dayahead.SPINUP_S - dayahead.WIND_AVERAGE_S
    assert calls["build"]["start"] == start - 12 * HOUR and calls["build"]["models"] == ("venue-weather",)
    assert calls["build"]["issued_before"] == DAY0 + 10 * HOUR and calls["build"]["dx"] == 400.0
    assert calls["build"]["wind"] is result.wind
    u, v = result.velocity(22.0, 114.0, start + 6 * HOUR)
    assert u == pytest.approx(0.02 * 3.0) and v == pytest.approx(0.2)
