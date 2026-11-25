"""Wind, waves and depths from public sources, tested without a network."""

import io

import numpy as np
import pytest
from scipy.io import netcdf_file

from currentmodel import wind
from currentmodel.depths import depth_source_from_grid, read_gebco
from currentmodel.fields import edge_flow
from currentmodel.models import FunctionModel
from currentmodel.waves import Waves, stokes_speed

HOUR = 3600.0
DAY0 = 1_795_000_000 // 21_600 * 21_600.0  # a model run time


@pytest.fixture
def fake_model(monkeypatch):
    """A weather model whose value at any hour is that hour's number, plus 0.5 per hour of forecast."""
    asked = []

    def get(url, start=None, end=None):
        if url.endswith(".idx"):
            return b"1:0:d=x:UGRD:10 m above ground:anl:\n2:100:d=x:VGRD:10 m above ground:anl:\n3:200:d=x:TMP:surface:anl:\n"
        cycle, hour = url.split("|")
        if float(cycle) > DAY0 + 30 * HOUR:
            raise FileNotFoundError(url)  # runs after this one have not been made yet
        asked.append((float(cycle), int(hour)))
        return f"{(float(cycle) - DAY0) / HOUR + int(hour)}|{hour}|{start}".encode()

    def value_at(message, lat, lon, circular=False):
        valid_hour, forecast_hour, start = message.decode().split("|")
        return float(valid_hour) + 0.5 * float(forecast_hour) + (1000.0 if start == "100" else 0.0)

    monkeypatch.setattr(wind, "_get", get)
    monkeypatch.setattr(wind, "_value_at", value_at)
    return asked


def _series(**options):
    wanted = [("UGRD", wind.LEVEL), ("VGRD", wind.LEVEL)]
    times, values = wind.point_series(lambda cycle, hour: f"{cycle}|{hour}", wanted, 22.0, 114.0, DAY0, DAY0 + 24 * HOUR, **options)
    return times, values[wanted[0]], values[wanted[1]]


def test_each_time_comes_from_the_latest_run_that_covers_it(fake_model):
    times, u, v = _series(step_hours=3)
    assert np.allclose(times, DAY0 + 3 * HOUR * np.arange(9))
    # Forecast hours are 0 or 3 only, so the values are the hour plus 0 or 1.5.
    assert np.allclose(u, [0, 4.5, 6, 10.5, 12, 16.5, 18, 22.5, 24])
    assert np.allclose(v, u + 1000.0)
    assert {hour for _, hour in fake_model} == {0, 3}


def test_a_forecast_as_issued_uses_no_later_run(fake_model):
    times, u, _ = _series(step_hours=6, issued_before=DAY0 + 7 * HOUR)
    assert max(cycle for cycle, _ in fake_model) == DAY0 + 6 * HOUR
    # After the 06:00 run everything is its forecast: 12:00 is 6 hours ahead, 24:00 is 18 hours ahead.
    assert np.allclose(u, [0, 6, 12 + 3, 18 + 6, 24 + 9])


def test_runs_not_yet_made_are_filled_from_what_exists(fake_model):
    wanted = [("UGRD", wind.LEVEL)]
    times, values = wind.point_series(lambda c, h: f"{c}|{h}", wanted, 22.0, 114.0, DAY0 + 24 * HOUR, DAY0 + 48 * HOUR, step_hours=6)
    assert times.max() == DAY0 + 30 * HOUR  # nothing invented beyond the last run


def test_values_are_kept_between_calls(fake_model, tmp_path):
    _series(step_hours=3, cache_file=tmp_path / "wind.npz")
    first = len(fake_model)
    _series(step_hours=3, cache_file=tmp_path / "wind.npz")
    assert len(fake_model) == first


def test_index_lookup_reports_what_is_missing():
    with pytest.raises(ValueError, match="HTSGW at surface"):
        wind.wanted_ranges("1:0:d=x:UGRD:10 m above ground:anl:\n", [("HTSGW", "surface")])


def test_stokes_drift_grows_with_height_and_falls_with_period():
    assert stokes_speed(2.0, 6.0) == pytest.approx(4 * stokes_speed(1.0, 6.0))
    assert stokes_speed(1.0, 4.0) > 3 * stokes_speed(1.0, 6.0)
    assert stokes_speed(1.0, 0.0) == 0.0
    # A 1.75 m, 6 second wind sea: a few hundredths of a metre per second.
    assert 0.03 < stokes_speed(1.75, 6.0) < 0.06


def test_wave_drift_goes_the_way_the_waves_travel():
    nan = np.array([np.nan, np.nan])
    two = lambda value: np.array([value, value], dtype=float)
    sea = Waves(np.array([0.0, HOUR]), two(1.5), two(5.0), two(90.0), two(1.5), two(5.0), two(90.0), nan, nan, nan)
    u, v = sea.stokes(np.array([1800.0, 2 * HOUR]))
    assert u[0] < 0 and v[0] == pytest.approx(0.0, abs=1e-9)  # waves from the east push west
    assert np.isnan(u[1])


def _gebco_bytes():
    buffer = io.BytesIO()
    with netcdf_file(buffer, "w") as file:
        file.createDimension("lat", 3)
        file.createDimension("lon", 4)
        lat = file.createVariable("lat", "d", ("lat",))
        lon = file.createVariable("lon", "d", ("lon",))
        elevation = file.createVariable("elevation", "h", ("lat", "lon"))
        lat[:] = [22.0, 22.1, 22.2]
        lon[:] = [114.0, 114.1, 114.2, 114.3]
        elevation[:] = [[-10, -20, -30, 5], [-10, -20, -30, 50], [-10, -20, -30, 100]]
        file.flush()
        return buffer.getvalue()


def test_gebco_file_becomes_a_depth_source():
    source = depth_source_from_grid(*read_gebco(_gebco_bytes()))
    depth = source(np.array([22.1, 22.1, 22.1]), np.array([114.0, 114.15, 114.3]))
    assert depth[0] == pytest.approx(10.0)
    assert depth[1] == pytest.approx(25.0)
    assert depth[2] == pytest.approx(30.0)  # the grid calls this land; the nearest water stands in
    assert np.isnan(source(np.array([30.0]), np.array([114.0]))[0])


def test_edge_flow_follows_the_wider_model_and_can_keep_only_its_tide():
    period = 12.42 * HOUR
    wider = FunctionModel(
        lambda lat, lon, t: (0.2 + 0.5 * np.sin(2 * np.pi * t / period) + 0.0 * lat, np.where(lon > 114.05, np.nan, 0.1) + 0.0 * t),
        22.0,
        114.0,
    )
    lat, lon = np.array([22.0, 22.0, 22.0]), np.array([114.00, 114.02, 114.10])
    flow = edge_flow(wider, 0.0, 4 * 86_400.0)(lat, lon)
    u, v = flow(1.5 * 86_400.0 + 450.0)
    assert u == pytest.approx(0.2 + 0.5 * np.sin(2 * np.pi * (1.5 * 86_400.0 + 450.0) / period), abs=1e-3)
    assert v == pytest.approx(0.1)  # the cell the wider model does not reach takes its neighbour's value
    tide = edge_flow(wider, 0.0, 4 * 86_400.0, remove_mean_over_s=2 * period)(lat, lon)
    u, v = tide(2.0 * 86_400.0)
    assert u == pytest.approx(0.5 * np.sin(2 * np.pi * 2.0 * 86_400.0 / period), abs=0.01)
    assert v == pytest.approx(0.0, abs=1e-9)
