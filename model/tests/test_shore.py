import numpy as np
import pytest

from currentmodel.shore import REACH_M, Shore

DETAIL = 4
STEP_M = 75.0  # between the coastline's points; the map's points are 300 m apart


def coast_to_the_north(rows=41, cols=41, land_from=32):
    water = np.ones((rows, cols), dtype=bool)
    water[land_from:] = False
    return water


def test_at_the_shore_the_current_runs_along_it():
    shore = Shore.from_mask(coast_to_the_north(), DETAIL, STEP_M, STEP_M)
    assert shore.hold.shape == (11, 11)
    # The map row at fine row 28 is the last on the water, 300 m from the first row ashore.
    u = np.full((11, 11), 0.2)
    v = np.full((11, 11), 0.3)   # setting north-east, onto the land
    along_u, along_v = shore.along(u, v)
    assert np.allclose(along_u, 0.2)                      # the part along the shore is untouched
    assert along_v[8, 5] == pytest.approx(0.0)            # a point ashore: nothing crosses
    assert 0.0 < along_v[7, 5] < 0.3                      # 300 m off: part of it is held back
    assert along_v[7, 5] < along_v[6, 5] <= 0.3           # less so further out
    assert along_v[0, 5] == pytest.approx(0.3)            # open water is as the model had it


def test_the_rule_is_gone_beyond_its_reach_and_works_both_ways():
    shore = Shore.from_mask(coast_to_the_north(), DETAIL, STEP_M, STEP_M)
    metres_off = (32 - 0.5 - np.arange(0, 41, DETAIL)) * STEP_M
    assert np.all(shore.hold[metres_off >= REACH_M] == 0.0)
    assert np.all(np.diff(shore.hold[:9, 0]) >= 0.0)      # stronger towards the land
    assert np.allclose(shore.away_y[:8], -1.0) and np.allclose(shore.away_x[:8], 0.0)
    # Water setting off the land is held back as much as water setting onto it.
    onto = shore.along(np.zeros((11, 11)), np.full((11, 11), 0.3))[1][7, 5]
    off = shore.along(np.zeros((11, 11)), np.full((11, 11), -0.3))[1][7, 5]
    assert off == pytest.approx(-onto)


def test_a_point_ashore_is_turned_by_the_nearest_water():
    water = np.ones((41, 41), dtype=bool)
    water[:, 30:] = False                                  # land to the east
    shore = Shore.from_mask(water, DETAIL, STEP_M, STEP_M)
    assert shore.hold[5, 8] == 1.0
    assert shore.away_x[5, 8] == pytest.approx(-1.0) and shore.away_y[5, 8] == pytest.approx(0.0)
    u, v = shore.along(np.full((11, 11), 0.4), np.full((11, 11), 0.1))
    assert u[5, 8] == pytest.approx(0.0) and v[5, 8] == pytest.approx(0.1)


def test_a_rock_smaller_than_a_map_cell_is_left_out():
    water = np.ones((41, 41), dtype=bool)
    water[20:22, 20:22] = False                            # 150 m across
    shore = Shore.from_mask(water, DETAIL, STEP_M, STEP_M)
    assert not shore.hold.any()
    water[18:24, 18:24] = False                            # 450 m across: an island
    assert Shore.from_mask(water, DETAIL, STEP_M, STEP_M).hold.any()


def test_no_land_and_missing_values():
    shore = Shore.from_mask(np.ones((41, 41), dtype=bool), DETAIL, STEP_M, STEP_M)
    u = np.random.default_rng(1).normal(size=(3, 11, 11))
    assert np.array_equal(shore.along(u, -u)[0], u)
    shore = Shore.from_mask(coast_to_the_north(), DETAIL, STEP_M, STEP_M)
    u[:, 9:] = np.nan
    held_u, held_v = shore.along(u, u)                     # works on every frame at once
    assert np.isnan(held_u[:, 9:]).all() and np.isfinite(held_u[:, :9]).all() and held_v.shape == u.shape
