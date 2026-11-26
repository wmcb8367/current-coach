"""Tidal analysis of a sea-level record: what is tide, and what is left over.

A tide gauge measures tide plus everything else: wind piling water against
the coast, air pressure, river floods, the slow coastal current. Fitting
the tidal constituents to the gauge's own record (least squares, nothing
assumed about the place) and subtracting them leaves the *residual*, which
is the part a current model has to explain with weather. Residuals are
decades long at many gauges, which makes them the best long training target
for the driver screening (:mod:`currentmodel.drivers`): far more numbers
than drifts will ever give.

Nodal modulation is ignored, so fit windows of a year or less.
"""

from __future__ import annotations

import numpy as np

# Constituent speeds in degrees per hour.
CONSTITUENTS = {
    "M2": 28.9841042, "S2": 30.0, "N2": 28.4397295, "K2": 30.0821373, "2N2": 27.8953548, "MU2": 27.9682084, "NU2": 28.5125831,
    "L2": 29.5284789, "T2": 29.9589333,
    "K1": 15.0410686, "O1": 13.9430356, "P1": 14.9589314, "Q1": 13.3986609, "J1": 15.5854433, "OO1": 16.1391017, "M1": 14.4920521,
    "M4": 57.9682084, "MS4": 58.9841042, "MN4": 57.4238337, "M3": 43.4761563, "M6": 86.9523127, "MK3": 44.0251729,
    "SA": 0.0410686, "SSA": 0.0821373, "MM": 0.5443747, "MF": 1.0980331,
}


def _design(t: np.ndarray, names) -> np.ndarray:
    hours = (np.asarray(t, dtype=float) - 946684800.0) / 3600.0
    columns = [np.ones_like(hours)]
    for name in names:
        w = np.radians(CONSTITUENTS[name]) * hours
        columns += [np.cos(w), np.sin(w)]
    return np.column_stack(columns)


class TideFit:
    """Tidal constituents fitted to a record; ``predict`` gives the tide at any time."""

    def __init__(self, names, coefficients):
        self.names = list(names)
        self.coefficients = np.asarray(coefficients)

    @classmethod
    def fit(cls, t, level, names=None) -> "TideFit":
        t = np.asarray(t, dtype=float)
        level = np.asarray(level, dtype=float)
        good = np.isfinite(level)
        span_days = (t[good].max() - t[good].min()) / 86400.0 if good.any() else 0.0
        if names is None:
            names = choose_constituents(span_days)
        A = _design(t[good], names)
        coefficients, *_ = np.linalg.lstsq(A, level[good], rcond=None)
        return cls(names, coefficients)

    def predict(self, t) -> np.ndarray:
        return _design(np.atleast_1d(t), self.names) @ self.coefficients

    def amplitudes(self) -> dict[str, float]:
        c = self.coefficients[1:].reshape(-1, 2)
        return {n: float(np.hypot(a, b)) for n, (a, b) in zip(self.names, c)}


def choose_constituents(span_days: float) -> list[str]:
    """The constituents a record of this length can separate (Rayleigh criterion), the larger ones first.

    The table is in order of importance, so when two are too close to tell
    apart (K1 and P1 in under six months) the larger one is kept.
    """
    hours = span_days * 24.0
    chosen: list[str] = []
    for name, speed in CONSTITUENTS.items():
        if hours * speed < 360.0 * 0.9:
            continue  # not even one cycle in the record
        if all(hours * abs(speed - CONSTITUENTS[other]) >= 360.0 for other in chosen):
            chosen.append(name)
    return chosen


def residual(t, level, fit_window_days: float = 365.0) -> tuple[np.ndarray, np.ndarray]:
    """Tide and residual of a record, fitting the constituents over windows of ``fit_window_days``.

    Each window is fitted on its own and predicted only within itself, so a
    long record does not need nodal corrections.
    """
    t = np.asarray(t, dtype=float)
    level = np.asarray(level, dtype=float)
    tide = np.full_like(level, np.nan)
    edges = np.arange(np.nanmin(t), np.nanmax(t) + 1.0, fit_window_days * 86400.0)
    for a in edges:
        inside = (t >= a) & (t < a + fit_window_days * 86400.0)
        if np.isfinite(level[inside]).sum() < 24 * 30:
            continue
        tide[inside] = TideFit.fit(t[inside], level[inside]).predict(t[inside])
    return tide, level - tide
