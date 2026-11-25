"""Keep the current on a map from crossing the coast.

Water does not flow through a shoreline. The physics model knows that: its
current slows and turns along the coast as it nears it. But two of the
things a map is made from do not. The share of the surface water that
follows the wind is one figure for the whole venue, so on its own it points
straight at whatever coast lies downwind. And a worldwide model with points
three kilometres apart has no idea where a venue's shore is.

So every map gets the same rule before it is published. At the shore, the
part of the current that points at the land, or away from it, is removed
and the part that runs along the shore is kept. The rule fades with
distance and is gone ``REACH_M`` from land, which leaves open water as the
model had it.

What that looks like on the water: with the wind blowing onto a shore, the
surface drift slows as it nears the land and what is left of it runs along
the coast.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

REACH_M = 600.0  # two points of a 300 m map: nearer than that, the shore steers the water


class Shore:
    """Where the land is, as seen from each point of a map.

    ``away_x`` and ``away_y`` are the east and north parts of the unit
    vector pointing straight away from the nearest land (towards the water,
    for a point that is itself ashore). ``hold`` is how much of the current
    across the shore is removed there: 1 at the shore, 0 from ``REACH_M`` on.
    """

    def __init__(self, away_x: np.ndarray, away_y: np.ndarray, hold: np.ndarray):
        self.away_x, self.away_y, self.hold = away_x, away_y, hold

    @classmethod
    def from_mask(
        cls,
        water: np.ndarray,
        detail: int,
        step_north_m: float,
        step_east_m: float,
        reach_m: float = REACH_M,
    ) -> "Shore":
        """From a coastline ``detail`` times finer than the map (True for water, row 0 the southernmost).

        ``step_north_m`` and ``step_east_m`` are the distances between the
        coastline's points. Land smaller than one cell of the map is left
        out: the map cannot show the water going round it.
        """
        water = np.asarray(water, dtype=bool)
        patches, count = ndimage.label(~water)
        if count:
            sizes = ndimage.sum_labels(~water, patches, np.arange(1, count + 1))
            small = np.concatenate([[False], sizes < detail * detail])
            water = water | small[patches]
        if water.all() or reach_m <= 0:
            zero = np.zeros(water[::detail, ::detail].shape)
            return cls(zero, zero.copy(), zero.copy())
        steps = (float(step_north_m), float(step_east_m))
        rows, cols = np.indices(water.shape)
        # For a point on the water, the nearest land; for a point ashore, the nearest water.
        to_land, land_at = ndimage.distance_transform_edt(water, sampling=steps, return_indices=True)
        to_water, water_at = ndimage.distance_transform_edt(~water, sampling=steps, return_indices=True)
        north = np.where(water, rows - land_at[0], water_at[0] - rows) * steps[0]
        east = np.where(water, cols - land_at[1], water_at[1] - cols) * steps[1]
        length = np.maximum(np.hypot(north, east), 1e-9)
        # The shoreline lies half a step short of the first point ashore.
        clear = np.maximum(to_land - 0.5 * min(steps), 0.0)
        hold = np.where(water, np.clip(1.0 - clear / reach_m, 0.0, 1.0) ** 2, 1.0)
        pick = (slice(None, None, detail), slice(None, None, detail))
        return cls((east / length)[pick], (north / length)[pick], hold[pick])

    def along(self, u: np.ndarray, v: np.ndarray):
        """The current with the part that crosses the shore taken out. The last two axes are the map's."""
        across = (u * self.away_x + v * self.away_y) * self.hold
        return u - across * self.away_x, v - across * self.away_y
