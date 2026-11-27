"""Correct a physics model's tidal currents where official predictions exist.

A depth-averaged model on 75 to 150 m cells gets a bay's tide heights right
(Biscayne Bay: within about 10% of NOAA's range and 20 minutes of its
times) but cannot resolve a cut that is one or two cells wide: Government
Cut runs at 55 to 70% of NOAA's predicted strength and peaks 20 to 40
minutes early. Where an agency publishes harmonic current predictions (NOAA's
``currents_predictions`` in US ports), those predictions are good in exactly
the places the model is weakest.

:func:`fit_station_corrections` compares the model's tide with the
predictions at each station over the run (both are known in advance, so this
is fair for a forecast) and finds a gain and a time shift along the
station's main flow axis. :class:`StationCorrected` applies them near each
station, fading to no correction a few hundred metres away, so the open bay
keeps the model's own currents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .geo import to_xy


@dataclass
class StationCorrection:
    """How to correct the model near one station: multiply by ``gain`` and take the value from ``shift_s`` earlier."""

    lat: float
    lon: float
    gain: float
    shift_s: float
    name: str = ""


def _axis(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Unit vector along the main axis of a flow (principal component)."""
    good = np.isfinite(u) & np.isfinite(v)
    m = np.cov(np.vstack([u[good], v[good]]))
    w, vec = np.linalg.eigh(m)
    return vec[:, np.argmax(w)]


def fit_station_corrections(
    model,
    reference,
    start: float,
    end: float,
    names: Sequence[str] | None = None,
    step_s: float = 600.0,
    max_shift_s: float = 3 * 3600.0,
    gain_range: tuple[float, float] = (0.5, 3.0),
) -> list[StationCorrection]:
    """A gain and time shift for each point of ``reference`` (a :class:`~currentmodel.baseline.TidalStreamField`).

    The shift is the one that best lines the model's flow along the
    station's axis up with the reference (positive: the model is early); the
    gain is the ratio of their spreads at that shift. Stations where the
    model has no water or the fit is poor (correlation under 0.7) are left
    out.
    """
    times = np.arange(start, end + 1.0, step_s)
    shifts = np.arange(-max_shift_s, max_shift_s + 1.0, step_s)
    out = []
    for p in range(len(reference.lat)):
        ref_u = np.interp(times, reference.times, reference.u[:, p], left=np.nan, right=np.nan)
        ref_v = np.interp(times, reference.times, reference.v[:, p], left=np.nan, right=np.nan)
        if not np.isfinite(ref_u).any():
            continue
        axis = _axis(ref_u, ref_v)
        ref = ref_u * axis[0] + ref_v * axis[1]
        lat = np.full(times.size, reference.lat[p])
        lon = np.full(times.size, reference.lon[p])
        best = None
        for shift in shifts:
            mu, mv = model.velocity(lat, lon, times - shift)
            m = mu * axis[0] + mv * axis[1]
            good = np.isfinite(m) & np.isfinite(ref)
            if good.sum() < 24 or np.std(m[good]) == 0:
                continue
            c = np.corrcoef(m[good], ref[good])[0, 1]
            if best is None or c > best[0]:
                best = (c, shift, np.std(ref[good]) / np.std(m[good]))
        if best is None or best[0] < 0.7:
            continue
        gain = float(np.clip(best[2], *gain_range))
        out.append(StationCorrection(float(reference.lat[p]), float(reference.lon[p]), gain, float(best[1]), names[p] if names else ""))
    return out


class StationCorrected:
    """A model with :class:`StationCorrection` s applied near their stations.

    Each station's weight falls off as a Gaussian of distance with scale
    ``radius_m``; where weights overlap they are averaged, and where they sum
    to less than one the rest is the uncorrected model.
    """

    def __init__(self, model, corrections: Sequence[StationCorrection], radius_m: float = 600.0):
        self.model = model
        self.corrections = list(corrections)
        self.radius_m = float(radius_m)
        if self.corrections:
            self.lat0 = float(np.mean([c.lat for c in self.corrections]))
            self.lon0 = float(np.mean([c.lon for c in self.corrections]))
            self._x, self._y = to_xy(np.array([c.lat for c in self.corrections]), np.array([c.lon for c in self.corrections]), self.lat0, self.lon0)

    def gain_and_shift(self, lat, lon):
        lat, lon = np.broadcast_arrays(np.asarray(lat, dtype=float), np.asarray(lon, dtype=float))
        if not self.corrections:
            return np.ones(lat.shape), np.zeros(lat.shape)
        x, y = to_xy(lat.ravel(), lon.ravel(), self.lat0, self.lon0)
        d2 = (x[:, None] - self._x[None, :]) ** 2 + (y[:, None] - self._y[None, :]) ** 2
        w = np.exp(-0.5 * d2 / self.radius_m**2)
        total = w.sum(axis=1)
        share = np.minimum(total, 1.0)
        mean = lambda values: np.where(total > 0, (w * values[None, :]).sum(axis=1) / np.maximum(total, 1e-300), 0.0)
        gains = np.array([c.gain for c in self.corrections])
        shifts = np.array([c.shift_s for c in self.corrections])
        gain = 1.0 + share * (mean(gains) - 1.0)
        shift = share * mean(shifts)
        return gain.reshape(lat.shape), shift.reshape(lat.shape)

    def velocity(self, lat, lon, time):
        lat, lon, time = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (lat, lon, time)))
        gain, shift = self.gain_and_shift(lat, lon)
        u, v = self.model.velocity(lat, lon, time - shift)
        return gain * u, gain * v

    def level(self, lat, lon, time):
        return self.model.level(lat, lon, time)

    def __getattr__(self, name):
        if name == "model":
            raise AttributeError(name)
        return getattr(self.model, name)
