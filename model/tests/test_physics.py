"""The shallow-water solver against cases with textbook answers."""

import numpy as np
import pytest

from currentmodel.physics import G, RHO_AIR, RHO_WATER, WIND_DRAG, EARTH_ROTATION, Grid, ShallowWater

M2 = 2 * np.pi / (12.4206012 * 3600.0)


def basin(nx, ny, dx, depth, lat0=0.0):
    return Grid(lat0, 114.0, 0.0, 0.0, dx, np.full((ny, nx), float(depth)))


def test_still_water_over_rough_bottom_stays_still():
    rng = np.random.default_rng(0)
    depth = rng.uniform(3.0, 40.0, size=(20, 30))
    depth[5:9, 10:14] = 0.0  # an island
    solver = ShallowWater(Grid(22.0, 114.0, 0.0, 0.0, 200.0, depth), open_boundary=False)
    solver.run(3600.0)
    assert np.abs(solver.eta).max() == 0.0 and np.abs(solver.u).max() == 0.0


def test_seiche_period_matches_merian_formula():
    length, depth, dx = 10_000.0, 10.0, 250.0
    nx = int(length / dx)
    solver = ShallowWater(
        basin(nx, 3, dx, depth), manning=0.0, viscosity=0.0, advection=False, open_boundary=False, coriolis=False
    )
    x = dx * (np.arange(nx) + 0.5)
    solver.eta[:] = 0.01 * np.cos(np.pi * x / length)[None, :]
    series = []
    solver.run(3 * 2100.0, sample_every=solver.dt, on_sample=lambda s: series.append((s.time, s.eta[1, 0])))
    times, level = np.array(series).T
    # Time between successive downward zero crossings is one period.
    down = np.nonzero((level[:-1] > 0) & (level[1:] <= 0))[0]
    period = np.mean(np.diff(times[down]))
    assert period == pytest.approx(2 * length / np.sqrt(G * depth), rel=0.01)


def test_closed_basin_conserves_water():
    rng = np.random.default_rng(1)
    depth = rng.uniform(4.0, 30.0, size=(25, 25))
    depth[10:13, 8:15] = 0.0
    solver = ShallowWater(Grid(22.0, 114.0, 0.0, 0.0, 300.0, depth), open_boundary=False)
    solver.eta[:] = np.where(solver.interior, 0.05 * rng.standard_normal(depth.shape), 0.0)
    before = solver.volume()
    solver.set_wind(lambda t: (8.0, -3.0))
    solver.run(6 * 3600.0)
    assert solver.volume() == pytest.approx(before, abs=1e-6 * solver.grid.dx**2)
    assert np.abs(solver.u).max() > 0.0


def test_wind_piles_water_up_by_the_expected_slope():
    depth, dx, nx, wind = 5.0, 500.0, 40, 10.0
    solver = ShallowWater(basin(nx, 3, dx, depth), open_boundary=False, coriolis=False, min_depth=1.0)
    solver.set_wind(lambda t: (wind, 0.0))
    solver.start(0.0, ramp_s=6 * 3600.0)
    solver.run(36 * 3600.0)
    slope = (solver.eta[1, -1] - solver.eta[1, 0]) / (dx * (nx - 1))
    expected = RHO_AIR * WIND_DRAG * wind**2 / (RHO_WATER * G * depth)
    assert slope == pytest.approx(expected, rel=0.03)
    assert np.abs(solver.u).max() < 0.005  # steady state: the slope balances the wind, nothing flows


def test_tide_is_amplified_at_the_head_of_a_channel():
    # A channel open to the sea at the west end and closed at the east end
    # resonates: the linear, frictionless tide at the head is 1 / cos(k L) of
    # the tide at the mouth.
    length, depth, dx, amplitude = 50_000.0, 10.0, 500.0, 0.05
    nx = int(length / dx) + 1
    grid = basin(nx, 3, dx, depth)
    grid.depth[0, :] = grid.depth[-1, :] = 0.0   # walls along both sides
    grid.depth[:, -1] = 0.0                       # closed head
    solver = ShallowWater(grid, manning=0.004, viscosity=0.0, advection=False, coriolis=False)
    solver.set_boundary(lambda t: amplitude * np.cos(M2 * t))
    period = 2 * np.pi / M2
    solver.start(0.0, ramp_s=2 * period)
    solver.run(8 * period)
    times, head = [], []
    solver.run(12 * period, sample_every=300.0, on_sample=lambda s: (times.append(s.time), head.append(s.eta[1, -2])))
    # Fit the tidal frequency, so the channel's own slowly decaying seiche is not counted.
    times = np.array(times)
    design = np.column_stack([np.cos(M2 * times), np.sin(M2 * times), np.ones_like(times)])
    cos_part, sin_part, _ = np.linalg.lstsq(design, np.array(head), rcond=None)[0]
    channel_length = dx * (nx - 1.5)  # from the centre of the open cell to the wall
    expected = amplitude / np.cos(M2 * channel_length / np.sqrt(G * depth))
    assert np.hypot(cos_part, sin_part) == pytest.approx(expected, rel=0.01)


def test_coriolis_turns_flow_to_the_right_in_the_north():
    # With gravity switched off nothing but the Earth's rotation acts, so a
    # uniform flow turns in an inertial circle with period 2 pi / f.
    lat = 45.0
    solver = ShallowWater(
        basin(21, 21, 1000.0, 50.0, lat0=lat),
        manning=0.0, viscosity=0.0, advection=False, open_boundary=False, gravity=1e-9, cfl=1e-5,
    )
    solver.u[:] = 0.1
    f = 2 * EARTH_ROTATION * np.sin(np.radians(lat))
    solver.dt = 60.0
    solver.run(0.25 * 2 * np.pi / f)
    u, v = solver.centre_velocity()
    assert u[10, 10] == pytest.approx(0.0, abs=0.004)
    assert v[10, 10] == pytest.approx(-0.1, abs=0.004)


def test_flow_speeds_up_through_a_narrows():
    # Tide through a strait that narrows in the middle: continuity alone says
    # the flow in the gap must be faster than in the wide sections.
    ny, nx, dx = 21, 60, 200.0
    depth = np.full((ny, nx), 12.0)
    depth[0, :] = depth[-1, :] = 0.0
    depth[1:7, 25:35] = 0.0
    depth[14:20, 25:35] = 0.0
    solver = ShallowWater(Grid(22.0, 114.0, 0.0, 0.0, dx, depth))
    lat, lon = solver.boundary_latlon()
    west = lon < lon.mean()
    # A small head difference between the ends, swinging with the tide.
    solver.set_boundary(lambda t: np.where(west, 0.03, -0.03) * np.cos(M2 * t))
    solver.start(0.0, ramp_s=3 * 3600.0)
    solver.run(9 * 3600.0)
    u, v = solver.centre_velocity()
    speed = np.hypot(u, v)
    gap = np.nanmean(speed[8:13, 28:32])
    wide = np.nanmean(speed[2:19, 8:14])
    assert gap > 1.8 * wide
