"""Tuning a model while the readings come in.

A venue's model is a few ingredients (the steady drift of the global model,
the wind, a tide model) added together with weights. :class:`LiveTuner` keeps
two things up to date as each reading arrives:

the weights
    How much of each ingredient the readings at this venue call for. They
    start from sensible values for anywhere (all of the drift, two percent of
    the wind) and move slowly, so that after a few days they are the venue's
    own and one odd day cannot drag them away.

today's pattern
    What the water is doing today that no ingredient explains: readings from
    the last hours, each speaking for the water within a kilometre or two of
    where it was taken and fading as it ages. Three drifts showing a strong
    stream along one side of the course raise the prediction there, at once,
    and leave the far side alone.

A reading costs a handful of multiplications for the weights (a Kalman filter
step) and one small matrix solve for the pattern (as many rows as there are
recent readings), so the tuning can run on the phone the moment a drift ends,
or on the server as readings sync. The whole state is the weights and the
recent readings (:meth:`LiveTuner.state`), small enough to send to every
phone at a venue. Replaying stored readings through a fresh tuner gives the
same state again, which is how a nightly job rebuilds or checks it.

A reading that disagrees wildly with what the tuner expects is not believed
outright: beyond ``gate`` standard deviations its weight is cut, so one bad
drift or a made-up one cannot move the model far.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np

from .geo import KNOT_MS, to_xy
from .measurements import Drift

DAY_S = 86_400.0


@dataclass
class LiveTuner:
    """Weights of a model's ingredients, and today's pattern, updated reading by reading.

    Parameters
    ----------
    names:
        The ingredients, in the order their values are passed in.
    weights, weight_sd:
        Where each weight starts and how unsure that start is.
    weight_wander_per_day:
        How far a weight may wander in a day, as a fraction of ``weight_sd``.
        Zero freezes the weights once learned.
    pattern_sd:
        Typical size of what the ingredients leave unexplained, per
        component, in m/s. Zero turns today's pattern off.
    pattern_reach_m:
        How far from a reading its news carries: at this distance it counts
        for 60%, at twice this for 14%. ``None`` makes it one offset for the
        whole venue.
    pattern_memory_s:
        How long a reading's news lasts: after this long it counts for a third.
    noise:
        Scatter of a single reading that nothing explains, per component, in
        m/s; added to the reading's own error.
    gate:
        A reading further than this many standard deviations from what was
        expected has its weight cut in proportion.
    max_recent:
        Most readings kept for today's pattern; the oldest go first.
    """

    names: Sequence[str]
    weights: Sequence[float]
    weight_sd: Sequence[float]
    weight_wander_per_day: float = 0.1
    pattern_sd: float = 0.15 * KNOT_MS
    pattern_reach_m: float | None = 1500.0
    pattern_memory_s: float = 12 * 3600.0
    noise: float = 0.15 * KNOT_MS
    gate: float = 3.0
    max_recent: int = 200
    time: float | None = None
    readings: int = 0
    mean: np.ndarray = field(default=None, repr=False)
    cov: np.ndarray = field(default=None, repr=False)
    recent: list = field(default_factory=list, repr=False)  # (time, x, y, u, v, variance, values)

    def __post_init__(self) -> None:
        self.names = tuple(self.names)
        self._prior_sd = np.asarray(self.weight_sd, dtype=float)
        if len(self.names) != len(self.weights) or len(self.names) != self._prior_sd.size:
            raise ValueError("need one starting weight and one uncertainty per ingredient")
        if self.mean is None:
            self.mean = np.asarray(self.weights, dtype=float).copy()
            self.cov = np.diag(self._prior_sd**2)
        self.mean, self.cov = np.asarray(self.mean, dtype=float), np.asarray(self.cov, dtype=float)
        self._solved = None

    # ------------------------------------------------------------------ state

    @property
    def current_weights(self) -> dict[str, float]:
        return dict(zip(self.names, self.mean.tolist()))

    def state(self) -> dict:
        """Everything needed to carry on later, as plain numbers."""
        return {
            "names": list(self.names),
            "time": self.time,
            "readings": self.readings,
            "mean": self.mean.tolist(),
            "cov": self.cov.tolist(),
            "recent": [[t, x, y, u, v, var, np.asarray(values).tolist()] for t, x, y, u, v, var, values in self.recent],
        }

    def restore(self, state: Mapping) -> "LiveTuner":
        """Carry on from a saved :meth:`state`."""
        if list(state["names"]) != list(self.names):
            raise ValueError("the saved state is for different ingredients")
        self.time, self.readings = state["time"], int(state["readings"])
        self.mean, self.cov = np.asarray(state["mean"], dtype=float), np.asarray(state["cov"], dtype=float)
        self.recent = [(t, x, y, u, v, var, np.asarray(values, dtype=float)) for t, x, y, u, v, var, values in state["recent"]]
        self._solved = None
        return self

    # ---------------------------------------------------------------- pattern

    def _kernel(self, t1, x1, y1, t2, x2, y2) -> np.ndarray:
        fade = np.exp(-np.abs(np.subtract.outer(t1, t2)) / self.pattern_memory_s)
        if self.pattern_reach_m is not None:
            apart = np.subtract.outer(x1, x2) ** 2 + np.subtract.outer(y1, y2) ** 2
            fade = fade * np.exp(-apart / (2.0 * self.pattern_reach_m**2))
        return self.pattern_sd**2 * fade

    def _solve(self):
        """What the recent readings leave over once the weights have had their say, ready for predicting."""
        if self._solved is None and self.recent and self.pattern_sd > 0:
            t, x, y, u, v, var = (np.array([r[i] for r in self.recent], dtype=float) for i in range(6))
            values = np.array([r[6] for r in self.recent], dtype=float)  # (readings, ingredients, 2)
            left = np.column_stack([u, v]) - np.einsum("k,nkc->nc", self.mean, values)
            inverse = np.linalg.inv(self._kernel(t, x, y, t, x, y) + np.diag(var))
            self._solved = (t, x, y, inverse, inverse @ left)
        return self._solved

    def pattern(self, time, x, y):
        """Today's pattern (towards east, towards north, variance) at each time and place, in m/s."""
        time, x, y = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (time, x, y)))
        solved = self._solve()
        if solved is None:
            return np.zeros(time.shape), np.zeros(time.shape), np.full(time.shape, self.pattern_sd**2)
        t, px, py, inverse, answer = solved
        link = self._kernel(time.ravel(), x.ravel(), y.ravel(), t, px, py)
        variance = self.pattern_sd**2 - np.einsum("ij,jk,ik->i", link, inverse, link)
        return (link @ answer[:, 0]).reshape(time.shape), (link @ answer[:, 1]).reshape(time.shape), np.maximum(variance, 0.0).reshape(time.shape)

    # ------------------------------------------------------------------ steps

    def _values(self, values) -> np.ndarray:
        return np.nan_to_num(np.asarray(values, dtype=float).reshape(len(self.names), 2))

    def _wandered(self, time: float | None) -> np.ndarray:
        if time is None or self.time is None or time <= self.time:
            return self.cov
        return self.cov + np.diag((self.weight_wander_per_day * self._prior_sd) ** 2 * (time - self.time) / DAY_S)

    def predict(self, values, time: float, x: float = 0.0, y: float = 0.0) -> tuple[float, float, float]:
        """The current expected at a time and place where the ingredients have ``values``.

        ``values`` holds (towards east, towards north) in m/s for each
        ingredient, in the order of ``names``; ``x`` and ``y`` are metres
        east and north of the venue's centre. Returns the current and its
        one-sigma uncertainty per component, all in m/s.
        """
        values = self._values(values)
        pu, pv, pvar = self.pattern(time, x, y)
        u, v = self.mean @ values + np.array([float(pu), float(pv)])
        spread = values.T @ self._wandered(time) @ values
        return float(u), float(v), float(np.sqrt(0.5 * np.trace(spread) + float(pvar)))

    def update(self, values, time: float, u: float, v: float, sigma: float = 0.0, x: float = 0.0, y: float = 0.0) -> dict:
        """Take in one reading. Returns what was expected and how far off it was."""
        values = self._values(values)
        expected_u, expected_v, expected_sd = self.predict(values, time, x, y)
        miss = np.array([u - expected_u, v - expected_v])
        own = sigma**2 + self.noise**2
        surprise = float(np.hypot(*miss) / np.sqrt(2.0 * (expected_sd**2 + own)))
        trust = 1.0 if surprise <= self.gate else (self.gate / surprise) ** 2

        # The weights: a Kalman step in which today's pattern counts as noise.
        cov = self._wandered(time)
        design = values.T  # rows: east, north
        scatter = (own + self.pattern_sd**2) / trust
        gain = cov @ design.T @ np.linalg.inv(design @ cov @ design.T + scatter * np.eye(2))
        self.mean = self.mean + gain @ (np.array([u, v]) - design @ self.mean)
        keep = np.eye(self.mean.size) - gain @ design
        self.cov = keep @ cov @ keep.T + gain @ gain.T * scatter

        # Today's pattern: remember the reading; drop what is too old to matter.
        self.time = time if self.time is None else max(time, self.time)
        self.recent.append((float(time), float(x), float(y), float(u), float(v), own / trust, values))
        oldest = self.time - 5.0 * self.pattern_memory_s
        self.recent = [r for r in self.recent if r[0] >= oldest][-self.max_recent :]
        self._solved = None
        self.readings += 1
        return {
            "expected_u": expected_u,
            "expected_v": expected_v,
            "miss": float(np.hypot(*miss)),
            "surprise": surprise,
            "trust": float(trust),
        }


class LiveModel:
    """A current model that tunes itself to readings: ingredients plus a :class:`LiveTuner`.

    It answers ``velocity`` like any other model, with the weights and
    today's pattern as they stand, and :meth:`observe` feeds it a drift.
    Typical use::

        live = LiveModel({"drift": Subtidal(global_model), "wind": WindDrift(wind, lat, lon, 1.0)},
                         weights=[1.0, 0.02], weight_sd=[0.5, 0.01])
        for drift in todays_drifts:
            live.observe(drift)
        u, v = live.velocity(lat, lon, now)
    """

    def __init__(self, ingredients: Mapping[str, object], weights: Sequence[float], weight_sd: Sequence[float], **tuner_options):
        self.ingredients = dict(ingredients)
        self.tuner = LiveTuner(tuple(self.ingredients), weights, weight_sd, **tuner_options)
        first = next(iter(self.ingredients.values()))
        self.lat0, self.lon0 = first.lat0, first.lon0

    def _values(self, lat, lon, time):
        return [m.velocity(lat, lon, time) for m in self.ingredients.values()]

    def observe(self, drift: Drift) -> dict:
        """Take in one drift; returns the tuner's report on it."""
        values = [(float(u), float(v)) for u, v in self._values(drift.lat, drift.lon, drift.time)]
        x, y = to_xy(drift.lat, drift.lon, self.lat0, self.lon0)
        return self.tuner.update(values, drift.time, drift.u, drift.v, drift.sigma, float(x), float(y))

    def replay(self, drifts: Sequence[Drift]) -> list[dict]:
        """Take in many drifts, oldest first."""
        return [self.observe(d) for d in sorted(drifts, key=lambda d: d.time)]

    def velocity(self, lat, lon, time):
        lat, lon, time = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (lat, lon, time)))
        u, v = np.zeros(lat.shape), np.zeros(lat.shape)
        for weight, (part_u, part_v) in zip(self.tuner.mean, self._values(lat, lon, time)):
            u, v = u + weight * np.nan_to_num(part_u), v + weight * np.nan_to_num(part_v)
        x, y = to_xy(lat, lon, self.lat0, self.lon0)
        pattern_u, pattern_v, _ = self.tuner.pattern(time, x, y)
        return u + pattern_u, v + pattern_v

    def rate(self, lat, lon, time, half_window_s: float = 900.0):
        time = np.asarray(time, dtype=float)
        u0, v0 = self.velocity(lat, lon, time - half_window_s)
        u1, v1 = self.velocity(lat, lon, time + half_window_s)
        return (u1 - u0) / (2 * half_window_s), (v1 - v0) / (2 * half_window_s)
