"""Particle view of the current: a bounded, time-aware interpolation of the
readings, drawn as drifting particles with trails.

Honesty rules for the field:

* A reading only influences water within ``reach_nm`` of where it was taken.
  Its weight falls smoothly to zero at that distance, and there are no
  particles anywhere beyond the reach of every live reading.
* A reading only counts while it is "live" on the scrubber (same lifetime and
  fade as the arrows), so old readings do not prop up the field.
* Where the reaches of two or more live readings overlap, speed is the weighted
  mean and direction is the weighted mean of unit vectors (so 350 and 010
  average to 000, not 180). This matches the Current Coach app's interpolator,
  except that the app's inverse-distance weights are replaced by a kernel that
  is exactly zero outside the reach.
"""

from __future__ import annotations

import math

import numpy as np
from matplotlib.collections import LineCollection
from matplotlib.colors import to_rgb

from .loader import Measurement

M_PER_NM = 1852.0


def kernel(d, reach):
    """Compact weight: 1 at the reading, 0 at and beyond `reach`."""
    q = np.clip(d / reach, 0.0, 1.0)
    return (1.0 - q * q) ** 2


def field_at(pts, rx, ry, ru, rv, rspd, ralpha, reach):
    """Interpolated current at pts (N,2).

    rx, ry: reading positions; ru, rv: unit vectors toward; rspd: speed in kt;
    ralpha: 0..1 time weight of each reading. Returns (ux, uy, speed, coverage),
    where coverage is 0 outside the reach of every live reading.
    """
    n = len(pts)
    if len(rx) == 0:
        z = np.zeros(n)
        return z, z, z, z
    d = np.hypot(pts[:, 0:1] - rx[None, :], pts[:, 1:2] - ry[None, :])
    w = kernel(d, reach) * ralpha[None, :]
    sw = w.sum(axis=1)
    ok = sw > 1e-9
    safe = np.where(ok, sw, 1.0)
    u = (w * ru[None, :]).sum(axis=1) / safe
    v = (w * rv[None, :]).sum(axis=1) / safe
    spd = (w * rspd[None, :]).sum(axis=1) / safe
    norm = np.hypot(u, v)
    norm = np.where(norm > 1e-9, norm, 1.0)
    cov = np.where(ok, w.max(axis=1), 0.0)
    return np.where(ok, u / norm, 0.0), np.where(ok, v / norm, 0.0), np.where(ok, spd, 0.0), cov


class ParticleField:
    """Windy-style particles advected through the bounded field."""

    def __init__(self, frame, units, *, reach_m: float = 1000.0, n: int = 2000, trail: int = 8,
                 life: int = 90, seed: int = 7, px_per_frame: float = 0.015, per_reading: int = 110,
                 speed_scale: float = 1.0):
        self.f, self.units = frame, units
        self.reach = reach_m / M_PER_NM
        self.n, self.trail, self.life, self.per_reading = n, trail, life, per_reading
        self.rng = np.random.default_rng(seed)
        # distance moved per frame by a current at the top of the colour scale. Movement is strictly
        # proportional to speed, so a current twice as strong drifts twice as far per frame.
        self.move = px_per_frame * speed_scale * frame.width / units.vmax_kt
        self.pos = np.full((n, 2), np.nan)
        self.hist = np.full((n, trail, 2), np.nan)
        self.spd_hist = np.zeros((n, trail))
        self.cov_hist = np.zeros((n, trail))
        self.age = self.rng.integers(0, life, n)
        stops = np.array([to_rgb(c) for c in units.ramp])
        t = np.linspace(0, 1, 256)
        x = t * (len(stops) - 1)
        i = np.minimum(x.astype(int), len(stops) - 2)
        self.lut = stops[i] + (stops[i + 1] - stops[i]) * (x - i)[:, None]

    def _readings(self, shown):
        f = self.f
        xy = np.array([f.xy(m.lat, m.lon) for m, _ in shown]) if shown else np.zeros((0, 2))
        ang = np.radians([m.toward_deg for m, _ in shown])
        return (xy[:, 0] if shown else np.zeros(0), xy[:, 1] if shown else np.zeros(0), np.sin(ang), np.cos(ang),
                np.array([m.speed_kt for m, _ in shown]), np.array([a for _, a in shown]))

    def _spawn(self, idx, rx, ry, ralpha):
        k = len(idx)
        if k == 0:
            return
        if len(rx) == 0:
            self.pos[idx] = np.nan
        else:
            pick = self.rng.choice(len(rx), size=k, p=ralpha / ralpha.sum())
            r = self.reach * np.sqrt(self.rng.random(k))
            th = self.rng.random(k) * 2 * math.pi
            self.pos[idx, 0] = rx[pick] + r * np.cos(th)
            self.pos[idx, 1] = ry[pick] + r * np.sin(th)
        self.hist[idx] = np.nan
        self.age[idx] = self.rng.integers(0, self.life // 3, k)

    def step(self, shown: list[tuple[Measurement, float]]):
        """Advance one frame. `shown` = [(reading, alpha)] for the live readings."""
        rx, ry, ru, rv, rspd, ralpha = self._readings(shown)
        # keep the density per reading steady: only as many particles as the live readings call for
        target = int(min(self.n, round(self.per_reading * float(ralpha.sum())))) if len(rx) else 0
        expired = ~np.isnan(self.pos[:, 0]) & (self.age >= self.life)
        self.pos[expired] = np.nan
        self.hist[expired] = np.nan
        free = np.flatnonzero(np.isnan(self.pos[:, 0]))
        need = max(0, target - (self.n - len(free)))
        self._spawn(free[:need], rx, ry, ralpha)
        live = ~np.isnan(self.pos[:, 0])
        u = np.zeros(self.n)
        v = np.zeros(self.n)
        spd = np.zeros(self.n)
        cov = np.zeros(self.n)
        if live.any():
            u[live], v[live], spd[live], cov[live] = field_at(self.pos[live], rx, ry, ru, rv, rspd, ralpha, self.reach)
        out = live & (cov < 0.02)            # drifted past the reach of every live reading
        self.pos[out] = np.nan
        self.pos[:, 0] += u * spd * self.move
        self.pos[:, 1] += v * spd * self.move
        self.hist = np.roll(self.hist, -1, axis=1)
        self.spd_hist = np.roll(self.spd_hist, -1, axis=1)
        self.cov_hist = np.roll(self.cov_hist, -1, axis=1)
        self.hist[:, -1] = self.pos
        self.spd_hist[:, -1] = spd
        self.cov_hist[:, -1] = cov
        self.hist[out] = np.nan
        self.age += 1

    def draw(self, ax, fs: float = 1.0):
        """Return a LineCollection of fading trails, coloured by interpolated speed."""
        a, b = self.hist[:, :-1], self.hist[:, 1:]
        ok = ~(np.isnan(a[..., 0]) | np.isnan(b[..., 0]))
        segs = np.stack([a[ok], b[ok]], axis=1)
        frac = np.clip(self.spd_hist[:, 1:][ok] / self.units.vmax_kt, 0, 1)
        rgb = self.lut[(frac * 255).astype(int)]
        along = np.broadcast_to(np.linspace(0.0, 1.0, self.trail - 1) ** 0.7 * 0.9 + 0.1, ok.shape)[ok]      # head brightest
        life = np.broadcast_to(np.clip(np.minimum(self.age / 6.0, (self.life - self.age) / 14.0), 0, 1)[:, None], ok.shape)[ok]
        alpha = np.clip(along * life * np.sqrt(self.cov_hist[:, 1:][ok]), 0, 1) * 0.95
        lc = LineCollection(segs, colors=np.column_stack([rgb, alpha]), linewidths=1.25 * fs, zorder=3,
                            capstyle="round")
        ax.add_collection(lc)
        return lc


def reach_outline(ax, frame, shown, reach_nm, color, fs=1.0):
    """Faint dotted circles marking how far each live reading is allowed to reach."""
    from matplotlib.patches import Circle
    arts = []
    for m, al in shown:
        c = Circle(frame.xy(m.lat, m.lon), reach_nm, fill=False, ec=color, lw=0.7 * fs, ls=(0, (1, 4)),
                   alpha=0.45 * al, zorder=2)
        ax.add_patch(c)
        arts.append(c)
    return arts
