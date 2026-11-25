"""Gridded and harmonic current fields, and running the solver into them."""

import numpy as np
import pytest

from currentmodel.fields import CONSTITUENTS, GriddedField, HarmonicField, resolvable, simulate
from currentmodel.geo import to_latlon
from currentmodel.physics import Grid

LAT0, LON0 = 22.3, 114.2
DAY = 86_400.0


def _grid(ny=20, nx=30, dx=200.0, land=None):
    depth = np.full((ny, nx), 10.0)
    if land is not None:
        depth[land] = 0.0
    return Grid(LAT0, LON0, 0.0, 0.0, dx, depth)


def test_gridded_field_is_exact_for_a_flow_linear_in_space_and_time():
    grid = _grid()
    x, y = grid.centres_xy()
    times = np.array([0.0, 600.0, 1200.0])
    u = np.array([1e-4 * x + 2e-4 * y + 1e-3 * t for t in times])
    field = GriddedField(grid, times, u, -u)
    lat, lon = to_latlon(np.array([330.0, 2110.0]), np.array([470.0, 1930.0]), LAT0, LON0)
    got_u, got_v = field.velocity(lat, lon, np.array([200.0, 900.0]))
    want = 1e-4 * np.array([330.0, 2110.0]) + 2e-4 * np.array([470.0, 1930.0]) + 1e-3 * np.array([200.0, 900.0])
    assert np.allclose(got_u, want, atol=1e-6)
    assert np.allclose(got_v, -want, atol=1e-6)
    du, _ = field.rate(lat, lon, np.array([600.0, 600.0]), half_window_s=300.0)
    assert np.allclose(du, 1e-3)


def test_gridded_field_ignores_land_beside_the_coast_and_has_no_answer_ashore():
    land = np.zeros((20, 30), dtype=bool)
    land[:, 15:] = True
    grid = _grid(land=land)
    u = np.where(land, np.nan, 0.5)[None].repeat(2, axis=0)
    field = GriddedField(grid, np.array([0.0, 600.0]), u, u)
    lat, lon = to_latlon(np.array([14.4 * 200.0, 20 * 200.0]), np.array([2000.0, 2000.0]), LAT0, LON0)
    got, _ = field.velocity(lat, lon, 300.0)
    assert got[0] == pytest.approx(0.5)  # between the last wet cell and the shore
    assert np.isnan(got[1])  # well ashore
    assert np.isnan(field.velocity(lat[0], lon[0], 5000.0)[0])  # outside the run


def test_which_constituents_a_record_can_separate():
    assert resolvable(7 * DAY) == ["M2", "K1", "M4", "M6"]
    assert {"M2", "S2", "K1", "O1"} <= set(resolvable(15 * DAY))
    assert "N2" not in resolvable(15 * DAY)
    assert "N2" in resolvable(30 * DAY)
    assert "P1" not in resolvable(30 * DAY)  # needs half a year


def _tidal_field(days, step=1800.0, start=1_790_000_000.0):
    grid = _grid(ny=6, nx=8)
    x, y = grid.centres_xy()
    times = start + np.arange(0.0, days * DAY, step)

    def flow(t):
        w = {n: np.radians(CONSTITUENTS[n]) / 3600.0 for n in ("M2", "S2", "K1")}
        t = np.asarray(t, dtype=float)[..., None, None]
        u = 0.05 + (0.4 + 1e-5 * x) * np.cos(w["M2"] * t) + 0.15 * np.sin(w["S2"] * t + 1.0) + 0.2 * np.cos(w["K1"] * t - 0.5)
        v = -0.02 + 0.1 * np.sin(w["M2"] * t) + (0.05 + 1e-5 * y) * np.cos(w["K1"] * t)
        return u, v

    u, v = flow(times)
    return GriddedField(grid, times, u, v), flow


def test_harmonic_field_recovers_the_tide_and_predicts_beyond_the_run():
    field, flow = _tidal_field(days=30)
    harmonic = HarmonicField.fit(field)
    assert {"M2", "S2", "K1"} <= set(harmonic.names)
    lat, lon = field.grid.centres_latlon()
    later = field.times[-1] + 20 * DAY + 4321.0
    got_u, got_v = harmonic.velocity(lat, lon, later)
    want_u, want_v = flow(later)
    assert np.allclose(got_u, want_u, atol=1e-6)
    assert np.allclose(got_v, want_v, atol=1e-6)
    assert np.allclose(harmonic.ellipse("M2")[0, 0], np.hypot(0.4 + 1e-5 * 0.0, 0.0), atol=1e-3)
    du, _ = harmonic.rate(lat[0, 0], lon[0, 0], later)
    u0, _ = harmonic.velocity(lat[0, 0], lon[0, 0], later - 1.0)
    u1, _ = harmonic.velocity(lat[0, 0], lon[0, 0], later + 1.0)
    assert du == pytest.approx((u1 - u0) / 2.0, rel=1e-4)


def test_harmonic_field_saves_and_loads(tmp_path):
    field, _ = _tidal_field(days=8)
    harmonic = HarmonicField.fit(field)
    harmonic.save(tmp_path / "tide.npz")
    again = HarmonicField.load(tmp_path / "tide.npz")
    lat, lon = field.grid.centres_latlon()
    assert again.names == harmonic.names
    assert np.allclose(again.velocity(lat, lon, 1.8e9)[0], harmonic.velocity(lat, lon, 1.8e9)[0])
    field.save(tmp_path / "run.npz")
    assert np.allclose(GriddedField.load(tmp_path / "run.npz").u, field.u)


def test_simulated_tide_in_a_channel_condenses_into_harmonics():
    # A channel open to the sea at its west end, closed at the east, with an M2 tide outside.
    depth = np.zeros((5, 42))
    depth[1:4, :-1] = 10.0
    grid = Grid(LAT0, LON0, 0.0, 0.0, 1000.0, depth)
    omega = np.radians(CONSTITUENTS["M2"]) / 3600.0
    start = 1_790_000_000.0

    def boundary(lat, lon):
        return lambda t: 0.5 * np.cos(omega * (t - start)) * np.ones(lat.size)

    field = simulate(grid, start, start + 3.5 * DAY, boundary=boundary, sample_s=900.0, spinup_s=2 * DAY, coriolis=False)
    assert field.u.shape == (field.times.size, 5, 42)
    speed_mouth = np.nanmax(np.abs(field.u[:, 2, 2]))
    speed_head = np.nanmax(np.abs(field.u[:, 2, 39]))
    assert 0.2 < speed_mouth < 1.5  # the tide fills and drains the channel through its mouth
    assert speed_head < 0.2 * speed_mouth  # and barely moves at the closed end
    assert np.nanmax(np.abs(field.v)) < 0.02  # along the channel, not across it

    harmonic = HarmonicField.fit(field)
    lat, lon = grid.centres_latlon()
    sample = slice(None, None, 7)
    got, _ = harmonic.velocity(lat[2, 5], lon[2, 5], field.times[sample])
    error = np.sqrt(np.mean((got - field.u[sample, 2, 5]) ** 2))
    assert error < 0.05 * np.sqrt(np.mean(field.u[:, 2, 5] ** 2))
