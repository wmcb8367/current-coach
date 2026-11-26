"""The Current Coach models: named variants, each testing one idea about what moves the water.

Every variant is the venue's physics model plus a few more ingredients, each
with a weight (see :mod:`currentmodel.live`). They differ in which ideas
they carry, so scoring them side by side against readings says which ideas
earn their place at a venue:

``cc-physics``
    The physics model alone: tide, coast, depths, the global model's
    coastal drift and the wind's push inside the venue.
``cc-wind``
    Plus the surface water following the wind of the last six hours (the
    default until now).
``cc-wind-memory``
    Plus the wind of the last few days. After days of wind blowing one way,
    water has piled up downwind; when the wind eases it flows back. This
    adds the mean wind of the last three days, scaled by how far the wind
    has since eased (none in a steady blow, all of it in a calm after one).
    Its weight comes from the readings: positive if the water keeps going
    with the old wind, negative if it returns. ``cc-wind-memory-turned``
    adds a copy turned a quarter turn, so the return can set across the old
    wind as the Earth's rotation turns it.
``cc-wind-turned``
    ``cc-wind`` plus a copy of the wind turned a quarter turn to the right,
    so the fit can set the surface water at an angle to the wind, as the
    Earth's rotation does (the Miami current meters answer the wind 15 to
    40 degrees to its right).
``cc-nearby-sensor``
    ``cc-wind`` plus what a current meter nearby measured over the hours
    before the prediction, beyond its own tide: a slow ocean current or a
    weather-driven flow that is already running. Only where a venue has a
    current meter (Miami's Government Cut meters).
``cc-sea-level``
    Plus the coastal tide gauges: how far their level stands above or below
    the predicted tide and how fast that is changing. A falling surplus means
    water draining away along the coast.
``cc-all``
    Everything above.

Each can also run live, tuned reading by reading on the day (the tuner's
"today's pattern" carries what the ingredients miss). Wanted ingredients
that a venue lacks (no tide gauge) are left out of that venue's variants.

:func:`score_variants` scores them on precomputed ingredient values at
each reading, two ways: the day ahead (weights learned on the other days
only, which is what the evening-before prediction would have been), and
live on the day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from .correction import Conditions
from .geo import KNOT_MS
from .live import LiveTuner

HOUR = 3600.0


class WindMemory:
    """The wind of the last days, as far as it has since eased, times a fraction, optionally turned.

    With ``eased`` (the default) this is the mean wind over ``memory_s``
    times how much weaker the last ``recent_s`` have been, from 0 (as
    strong) to 1 (calm): zero in steady wind and largest the day a long blow
    stops. Without it, it is the old mean minus the recent mean, which is
    also set off by a change of direction. ``turn_deg`` turns the vector
    clockwise (90 sets the water to the right of the old wind). Outside the
    wind record the answer is NaN.

    Tried against 96 Hong Kong readings (Nov 2026), the eased form scored
    better than the plain difference, and 72 hours better than 48 or 96.
    """

    def __init__(self, conditions: Conditions, lat0: float, lon0: float, memory_s: float = 72 * HOUR,
                 recent_s: float = 6 * HOUR, fraction: float = 1.0, turn_deg: float = 0.0, eased: bool = True):
        self.eased = bool(eased)
        self.conditions, self.memory_s, self.recent_s = conditions, float(memory_s), float(recent_s)
        self.fraction, self.lat0, self.lon0 = float(fraction), float(lat0), float(lon0)
        self.turn = np.radians(turn_deg)

    def _mean(self, time, window_s):
        back = np.linspace(-window_s, 0.0, max(2, int(window_s // 1800) + 1))
        u, v = self.conditions.wind(np.asarray(time, dtype=float)[..., None] + back)
        return u.mean(axis=-1), v.mean(axis=-1)

    def velocity(self, lat, lon, time):
        lat, lon, time = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (lat, lon, time)))
        mu, mv = self._mean(time, self.memory_s)
        ru, rv = self._mean(time, self.recent_s)
        if self.eased:
            fall = np.clip(1.0 - np.hypot(ru, rv) / np.maximum(np.hypot(mu, mv), 0.1), 0.0, 1.0)
            du, dv = mu * fall, mv * fall
        else:
            du, dv = mu - ru, mv - rv
        c, s = np.cos(self.turn), np.sin(self.turn)
        u, v = c * du + s * dv, -s * du + c * dv
        inside = (time - self.memory_s >= self.conditions.times[0]) & (time <= self.conditions.times[-1])
        return np.where(inside, self.fraction * u, np.nan), np.where(inside, self.fraction * v, np.nan)


class ScalarPush:
    """A sensor's value times a fixed direction: one number (a gauge's surplus) as a current toward east or north.

    ``series`` is (times, values); ``lag_s`` uses the value that long
    before, so a day-ahead variant uses only what was known the evening before.
    """

    def __init__(self, times, values, lat0: float, lon0: float, towards_deg: float = 90.0, lag_s: float = 0.0, scale: float = 1.0):
        good = np.isfinite(values)
        self.times, self.values = np.asarray(times)[good], np.asarray(values)[good]
        self.lat0, self.lon0, self.lag_s, self.scale = float(lat0), float(lon0), float(lag_s), float(scale)
        self.east, self.north = np.sin(np.radians(towards_deg)), np.cos(np.radians(towards_deg))

    def velocity(self, lat, lon, time):
        lat, lon, time = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (lat, lon, time)))
        at = time - self.lag_s
        x = np.interp(at, self.times, self.values) * self.scale
        x = np.where((at >= self.times[0]) & (at <= self.times[-1]), x, np.nan)
        return x * self.east, x * self.north


class SensorPersistence:
    """A current meter's recent flow beyond its tide, carried forward as a steady current over the venue.

    ``times``, ``u`` and ``v`` are the meter's residual (measured minus its
    own tidal fit, :func:`currentmodel.meters.tidal_split`). At each time the
    answer is the mean over the ``window_s`` before ``issued(time)``: for a
    day-ahead prediction, the evening before; by default the time itself
    (live).
    """

    def __init__(self, times, u, v, lat0: float, lon0: float, window_s: float = 6 * HOUR, issued=None):
        good = np.isfinite(u) & np.isfinite(v)
        self.times = np.asarray(times, dtype=float)[good]
        self.u, self.v = np.asarray(u, dtype=float)[good], np.asarray(v, dtype=float)[good]
        self.lat0, self.lon0, self.window_s = float(lat0), float(lon0), float(window_s)
        self.issued = issued or (lambda t: t)
        self._cu = np.concatenate([[0.0], np.cumsum(self.u)])
        self._cv = np.concatenate([[0.0], np.cumsum(self.v)])

    def velocity(self, lat, lon, time):
        lat, lon, time = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (lat, lon, time)))
        end = np.asarray(self.issued(time), dtype=float)
        i1 = np.searchsorted(self.times, end, side="right")
        i0 = np.searchsorted(self.times, end - self.window_s, side="right")
        n = i1 - i0
        expected = max(1.0, 0.5 * self.window_s / max(np.median(np.diff(self.times)) if self.times.size > 1 else HOUR, 1.0))
        ok = n >= expected
        with np.errstate(invalid="ignore", divide="ignore"):
            u = (self._cu[i1] - self._cu[i0]) / n
            v = (self._cv[i1] - self._cv[i0]) / n
        return np.where(ok, u, np.nan), np.where(ok, v, np.nan)


@dataclass
class Ingredient:
    name: str
    weight: float      # where the weight starts
    sd: float          # how unsure that start is


# Starting weights: the physics model as built, 1.5% of the wind (what the
# readings preferred), and nothing of the new ideas until readings call for it.
INGREDIENTS = {
    "physics": Ingredient("physics", 1.0, 0.3),
    "wind": Ingredient("wind", 0.015, 0.008),
    "memory": Ingredient("memory", 0.0, 0.03),
    "memory-turned": Ingredient("memory-turned", 0.0, 0.03),
    "wind-turned": Ingredient("wind-turned", 0.0, 0.008),
    "nearby": Ingredient("nearby", 0.0, 1.0),        # share of the nearby meter's recent non-tidal current
    "level": Ingredient("level", 0.0, 1.0),          # per metre of gauge surplus, in m/s, east and north copies
    "level-north": Ingredient("level-north", 0.0, 1.0),
    "level-change": Ingredient("level-change", 0.0, 1.0),
    "level-change-north": Ingredient("level-change-north", 0.0, 1.0),
}

VARIANTS = {
    "cc-physics": ["physics"],
    "cc-wind": ["physics", "wind"],
    "cc-wind-memory": ["physics", "wind", "memory"],
    "cc-wind-memory-turned": ["physics", "wind", "memory", "memory-turned"],
    "cc-wind-turned": ["physics", "wind", "wind-turned"],
    "cc-nearby-sensor": ["physics", "wind", "nearby"],
    "cc-sea-level": ["physics", "wind", "level", "level-north", "level-change", "level-change-north"],
    "cc-all": ["physics", "wind", "memory", "level", "level-north", "level-change", "level-change-north"],
}


def fit_weights(values: np.ndarray, u: np.ndarray, v: np.ndarray, prior: np.ndarray, prior_sd: np.ndarray, noise: float) -> np.ndarray:
    """Most likely weights given readings and a Gaussian start (the same start the live tuner has).

    ``values`` is (readings, ingredients, 2): each ingredient's east and north value at each reading.
    """
    A = np.concatenate([values[:, :, 0], values[:, :, 1]], axis=0)
    b = np.concatenate([u, v])
    precision = np.diag(1.0 / prior_sd**2)
    lhs = A.T @ A / noise**2 + precision
    rhs = A.T @ b / noise**2 + precision @ prior
    return np.linalg.solve(lhs, rhs)


def score_variants(
    values: Mapping[str, np.ndarray],
    u: np.ndarray,
    v: np.ndarray,
    days: np.ndarray,
    times: np.ndarray,
    x: np.ndarray | None = None,
    y: np.ndarray | None = None,
    sigma: np.ndarray | None = None,
    variants: Mapping[str, Sequence[str]] = VARIANTS,
    noise: float = 0.15 * KNOT_MS,
) -> list[dict]:
    """Vector error (kn) of each variant against readings: the day ahead and live on the day.

    ``values[name]`` is (readings, 2), the ingredient's east and north value
    at each reading. Day ahead: each day is predicted with weights fitted to
    all the other days, nothing from the day itself. Live: readings are
    replayed in time order through a :class:`LiveTuner` started from the
    same weights, each predicted before it is seen.
    """
    n = len(u)
    x = np.zeros(n) if x is None else np.asarray(x)
    y = np.zeros(n) if y is None else np.asarray(y)
    sigma = np.zeros(n) if sigma is None else np.asarray(sigma)
    rows = []
    for variant, names in variants.items():
        names = [m for m in names if m in values]
        V = np.stack([np.nan_to_num(np.asarray(values[m], dtype=float)) for m in names], axis=1)  # (n, k, 2)
        prior = np.array([INGREDIENTS[m].weight for m in names])
        sd = np.array([INGREDIENTS[m].sd for m in names])
        ahead = np.zeros((n, 2))
        for day in np.unique(days):
            test = days == day
            w = fit_weights(V[~test], u[~test], v[~test], prior, sd, noise)
            ahead[test] = np.einsum("nkc,k->nc", V[test], w)
        err_ahead = np.hypot(ahead[:, 0] - u, ahead[:, 1] - v)
        tuner = LiveTuner(names, prior, sd)
        live = np.zeros((n, 2))
        for i in np.argsort(times):
            pu, pv, _ = tuner.predict(V[i], times[i], x[i], y[i])
            live[i] = pu, pv
            tuner.update(V[i], times[i], u[i], v[i], sigma[i], x[i], y[i])
        err_live = np.hypot(live[:, 0] - u, live[:, 1] - v)
        w_all = fit_weights(V, u, v, prior, sd, noise)
        per_day = {int(d): float(np.sqrt(np.mean(err_ahead[days == d] ** 2)) / KNOT_MS) for d in np.unique(days)}
        rows.append({
            "variant": variant,
            "ingredients": names,
            "day_ahead_kn": float(np.sqrt(np.mean(err_ahead**2)) / KNOT_MS),
            "live_kn": float(np.sqrt(np.mean(err_live**2)) / KNOT_MS),
            "weights": dict(zip(names, w_all.tolist())),
            "day_ahead_by_day_kn": per_day,
            "day_ahead_errors_kn": (err_ahead / KNOT_MS).tolist(),
        })
    return rows
