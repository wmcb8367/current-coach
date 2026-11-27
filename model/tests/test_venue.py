"""Building a venue grid from elevation tiles, with the tiles replaced by a made-up coast."""

import numpy as np

from currentmodel import venue
from currentmodel.geo import to_xy

LAT0, LON0 = 22.3, 114.2


def _fake_tiles(soundings_in_fine_tiles):
    """Land east of x = 3 km, a pond ashore, sea elsewhere, 20 m deep away from the coast."""

    def elevation(lat, lon, zoom=12, cache_dir=None):
        x, y = to_xy(lat, lon, LAT0, LON0)
        land = x > 3000.0
        pond = np.hypot(x - 4500.0, y) < 300.0
        true_depth = np.clip(2.0 + (3000.0 - x) / 100.0, 2.0, 20.0)
        if zoom >= 11:
            water = -true_depth if soundings_in_fine_tiles else np.zeros_like(x)
            return np.where(land & ~pond, 30.0, water)
        # The coarse grid puts the coast 800 m too far west and ramps up to it.
        return np.clip((x - 2200.0) / 50.0, -20.0, 30.0)

    return elevation


def test_coast_comes_from_fine_tiles_and_depth_from_beyond_the_coarse_ramp(monkeypatch):
    monkeypatch.setattr(venue, "elevation", _fake_tiles(soundings_in_fine_tiles=False))
    grid = venue.build_grid(LAT0, LON0, 12_000.0, 6_000.0, dx=100.0)
    x, y = grid.centres_xy()
    assert grid.wet[(x < 2900.0)].all()  # sea right up to the true coast
    assert not grid.wet[(x > 3100.0)].any()  # the pond ashore is not part of the sea
    assert np.allclose(grid.depth[x < 0.0], 20.0)
    # The strip the coarse grid calls land or shallow takes the open-water depth, not the ramp.
    assert grid.depth[(x > 1300.0) & (x < 2900.0)].min() > 15.0


def test_surveyed_depths_in_fine_tiles_are_used_as_they_are(monkeypatch):
    monkeypatch.setattr(venue, "elevation", _fake_tiles(soundings_in_fine_tiles=True))
    grid = venue.build_grid(LAT0, LON0, 12_000.0, 6_000.0, dx=100.0)
    x, y = grid.centres_xy()
    near_shore = (x > 2500.0) & (x < 2900.0)
    assert np.allclose(grid.depth[near_shore], np.clip(2.0 + (3000.0 - x[near_shore]) / 100.0, 2.0, 20.0), atol=0.6)


def test_better_local_depths_replace_the_global_ones(monkeypatch):
    monkeypatch.setattr(venue, "elevation", _fake_tiles(soundings_in_fine_tiles=False))

    def chart(lat, lon):
        x, y = to_xy(lat, lon, LAT0, LON0)
        return np.where(np.abs(y) < 500.0, 35.0, np.nan)  # a dredged channel the global grid lacks

    grid = venue.build_grid(LAT0, LON0, 12_000.0, 6_000.0, dx=100.0, depth_source=chart)
    x, y = grid.centres_xy()
    assert np.allclose(grid.depth[(np.abs(y) < 400.0) & grid.wet], 35.0)
    assert np.allclose(grid.depth[(y > 1000.0) & (x < 0.0)], 20.0)


def test_a_channel_meeting_only_at_corners_is_opened():
    from currentmodel.venue import open_diagonals

    water = np.eye(5, dtype=bool)  # a staircase from corner to corner
    opened = open_diagonals(water)
    assert opened[water].all()
    from scipy import ndimage

    labels, count = ndimage.label(opened)  # four-neighbour connection
    assert count == 1
    assert opened.sum() == 9
