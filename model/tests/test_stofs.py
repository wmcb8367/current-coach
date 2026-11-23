"""Reading the global model: file parsing, stitching runs, and interpolating levels.

These tests never touch the network. The file reader is checked against a
netCDF file written by SciPy, and the run stitching against a fake archive.
"""

import datetime as dt

import numpy as np
import pytest
from scipy.io import netcdf_file

from currentmodel import stofs
from currentmodel.geo import to_latlon

HOUR = 3600.0


@pytest.fixture
def local_get(monkeypatch):
    """Serve byte ranges from local files in place of HTTP."""

    def get(url, start=None, end=None, tries=4):
        with open(url, "rb") as f:
            if start is None:
                return f.read()
            f.seek(start)
            return f.read(end - start)

    monkeypatch.setattr(stofs, "_get", get)


def _write_run(path, nodes=5000, records=7):
    rng = np.random.default_rng(0)
    with netcdf_file(path, "w") as f:
        f.createDimension("time", None)
        f.createDimension("node", nodes)
        f.description = "x" * 3000  # a long header, as the real files have
        time = f.createVariable("time", "f8", ("time",))
        time.units = "seconds since 2026-05-26 12:00:00        ! NCDASE - BASE_DATE"
        for name in ("x", "y", "depth"):
            f.createVariable(name, "f8", ("node",))[:] = rng.normal(size=nodes)
        u = f.createVariable("u-vel", "f8", ("time", "node"))
        v = f.createVariable("v-vel", "f8", ("time", "node"))
        for r in range(records):
            time[r] = 1000.0 * HOUR + r * HOUR
            u[r] = rng.normal(size=nodes)
            v[r] = rng.normal(size=nodes)
    with netcdf_file(path, "r", mmap=False) as f:
        return {name: f.variables[name][:].copy() for name in ("x", "depth", "u-vel", "v-vel")}


def test_classic_reader_matches_scipy(tmp_path, local_get):
    path = str(tmp_path / "run.nc")
    truth = _write_run(path)
    handle = stofs._Classic(path)
    base = dt.datetime(2026, 5, 26, 12, tzinfo=dt.timezone.utc).timestamp()
    assert np.allclose(handle.times(), base + (1000 + np.arange(7)) * HOUR)
    assert np.array_equal(handle.fixed("x"), truth["x"])
    assert np.array_equal(handle.fixed("depth"), truth["depth"])
    nodes = np.array([3, 4, 5, 900, 901, 4200, 4999])
    for name in ("u-vel", "v-vel"):
        got = handle.records(name, [0, 2, 6], nodes)
        assert np.array_equal(got, truth[name][[0, 2, 6]][:, nodes])


def test_node_runs_cover_every_node_and_skip_big_gaps():
    nodes = np.concatenate([np.arange(1000, 1200), np.arange(5_000_000, 5_000_300, 3)])
    runs = stofs._runs(nodes)
    assert len(runs) == 2
    assert all(any(lo <= n < hi for lo, hi in runs) for n in nodes)
    assert sum(hi - lo for lo, hi in runs) < 1000


def test_level_field_is_exact_for_a_tilted_surface():
    rng = np.random.default_rng(1)
    x, y = rng.uniform(-20e3, 20e3, 60), rng.uniform(-20e3, 20e3, 60)
    lat, lon = to_latlon(x, y, 22.3, 114.2)
    times = np.arange(0.0, 13 * HOUR, HOUR)
    tide = np.cos(2 * np.pi * times / (12.42 * HOUR))
    level = tide[:, None] * (1.0 + 1e-5 * x + 2e-5 * y)[None, :]
    field = stofs.LevelField(lat, lon, times, level)
    qx, qy = np.array([0.0, 5e3, -7e3]), np.array([0.0, -3e3, 8e3])
    qlat, qlon = to_latlon(qx, qy, 22.3, 114.2)
    at = field.at(qlat, qlon)
    # Exact in space at a data time; the spline follows the tide between hourly values.
    assert np.allclose(at(3 * HOUR), tide[3] * (1.0 + 1e-5 * qx + 2e-5 * qy), atol=1e-6)
    mid = np.cos(2 * np.pi * 4.5 / 12.42) * (1.0 + 1e-5 * qx + 2e-5 * qy)
    assert np.allclose(at(4.5 * HOUR), mid, atol=5e-3)
    # Far outside the data, the nearest point's level is used rather than an extrapolation.
    far_lat, far_lon = to_latlon(np.array([500e3]), np.array([0.0]), 22.3, 114.2)
    assert abs(field.at(far_lat, far_lon)(0.0)[0]) < 1.5


class _FakeRun:
    """Six-hourly runs whose values say which run they came from."""

    def __init__(self, cycle):
        self.cycle = cycle.timestamp()

    def times(self):
        return self.cycle + HOUR * np.arange(stofs.FIRST_RECORD_H, stofs.LAST_RECORD_H + 1)

    def records(self, name, records, nodes):
        hours = self.times()[records] / HOUR
        return np.outer(self.cycle / HOUR * 1000.0 + hours % 1000.0, np.ones(nodes.size))


class _FakeStofs(stofs.Stofs):
    def __init__(self, latest, missing=()):
        super().__init__()
        self._latest, self.missing, self.opened = latest, set(missing), []
        self._mesh = (np.linspace(22.0, 22.5, 50), np.linspace(114.0, 114.5, 50), np.full(50, 10.0))

    def _open_product(self, product, cycle):
        if cycle in self.missing:
            raise FileNotFoundError
        self.opened.append(cycle)
        return _FakeRun(cycle)


def _utc(*args):
    return dt.datetime(*args, tzinfo=dt.timezone.utc)


def test_past_hours_come_from_the_freshest_run_and_the_future_from_the_forecast():
    fake = _FakeStofs(latest=_utc(2026, 11, 22, 12))
    start, end = _utc(2026, 11, 21, 20).timestamp(), _utc(2026, 11, 23, 6).timestamp()
    series = fake.read("currents", np.arange(5), start, end)
    assert np.allclose(np.diff(series.times), HOUR)  # every hour once, in order
    assert series.times[0] <= start and series.times[-1] >= end
    source = np.floor(series.values["u-vel"][:, 0] / 1000.0) * HOUR  # the run each hour came from
    age = (series.times - source) / HOUR
    past = series.times <= _utc(2026, 11, 22, 12).timestamp()
    assert age[past].max() <= 0 and age[past].min() >= -5  # analysis hours of the run that covers them
    assert np.all(source[~past] == _utc(2026, 11, 22, 12).timestamp())  # then the newest forecast


def test_fewer_files_are_read_when_older_forecasts_are_acceptable():
    start, end = _utc(2026, 11, 10, 0).timestamp(), _utc(2026, 11, 22, 0).timestamp()
    fresh = _FakeStofs(latest=_utc(2026, 11, 22, 12))
    fresh.read("tide_level", np.arange(5), start, end, hours_per_run=6)
    coarse = _FakeStofs(latest=_utc(2026, 11, 22, 12))
    series = coarse.read("tide_level", np.arange(5), start, end, hours_per_run=180)
    assert len(coarse.opened) <= 3 < len(fresh.opened)
    assert np.allclose(np.diff(series.times), HOUR)


def test_a_missing_run_is_bridged_by_the_one_before():
    fake = _FakeStofs(latest=_utc(2026, 11, 22, 12), missing=[_utc(2026, 11, 22, 0)])
    series = fake.read("currents", np.arange(5), _utc(2026, 11, 21, 12).timestamp(), _utc(2026, 11, 22, 12).timestamp())
    assert np.allclose(np.diff(series.times), HOUR)


def test_currents_become_a_model_with_dry_nodes_dropped():
    class Dry(_FakeRun):
        def records(self, name, records, nodes):
            out = np.sin(self.times()[records] / (12.42 * HOUR) * 2 * np.pi)[:, None] * np.linspace(0.1, 0.5, nodes.size)
            out[:, 0] = -99999.0  # a node the global model has dry
            return out

    class Fake(_FakeStofs):
        def _open_product(self, product, cycle):
            return Dry(cycle)

    fake = Fake(latest=_utc(2026, 11, 22, 12))
    start = _utc(2026, 11, 22, 0).timestamp()
    model = stofs.currents(fake, 22.25, 114.25, 60e3, start, start + 12 * HOUR)
    assert model.u.shape[1] == 49  # the dry node is gone
    assert model.times[1] - model.times[0] == 900.0
    u, v = model.velocity(22.25, 114.25, start + 6.2 * HOUR)
    assert np.isfinite(u) and np.isfinite(v)


def test_weather_levels_keep_the_tilt_of_the_surge_and_drop_node_noise():
    rng = np.random.default_rng(3)
    noise = rng.normal(0.0, 0.05, 50)

    class Run(_FakeRun):
        def __init__(self, cycle, product, mesh):
            super().__init__(cycle)
            self.product, self.mesh = product, mesh

        def records(self, name, records, nodes):
            hours = (self.times()[records] - _utc(2026, 11, 22, 0).timestamp()) / HOUR
            tide = np.outer(np.cos(2 * np.pi * hours / 12.42), np.ones(nodes.size))
            if self.product == "tide_level":
                return tide
            x, y = stofs.to_xy(self.mesh[0][nodes], self.mesh[1][nodes], 22.25, 114.25)
            return tide + 0.2 + 2e-6 * x - 1e-6 * y + noise[nodes]  # the sea raised and tilted, plus mesh noise

    class Fake(_FakeStofs):
        def _open_product(self, product, cycle):
            return Run(cycle, product, self._mesh)

    fake = Fake(latest=_utc(2026, 11, 22, 12))
    fake._mesh = (rng.uniform(22.0, 22.5, 50), rng.uniform(114.0, 114.5, 50), np.full(50, 10.0))
    start = _utc(2026, 11, 22, 0).timestamp()
    tide = stofs.levels(fake, 22.25, 114.25, 80e3, start, start + 12 * HOUR, tide_only=True)
    weather = stofs.levels(fake, 22.25, 114.25, 80e3, start, start + 12 * HOUR)
    surge = weather.level - tide.level[np.isin(tide.times, weather.times)]
    x, y = stofs.to_xy(weather.lat, weather.lon, 22.25, 114.25)
    wanted = 0.2 + 2e-6 * x - 1e-6 * y
    assert np.abs(surge - wanted).max() < 0.03  # the plane, with the 5 cm node noise averaged away
    assert np.abs(surge - surge[0]).max() < 1e-9  # and steady, as the made-up surge is
