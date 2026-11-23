"""Currents on a venue grid: running the physics model and keeping its output.

:func:`simulate` runs the solver over a period and returns a
:class:`GriddedField`, a current model the rest of the package can query at
any position and time inside the run.

:class:`HarmonicField` condenses a tide-only run into tidal constituents per
cell. It is the cheap, long-lived form of a tide model: a month of
simulation becomes a few numbers per cell that predict the tide for any
date without running the solver again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

from .physics import Grid, ShallowWater

# Tidal constituent speeds in degrees per hour, most important first.
CONSTITUENTS = {
    "M2": 28.9841042,
    "K1": 15.0410686,
    "O1": 13.9430356,
    "S2": 30.0,
    "N2": 28.4397295,
    "M4": 57.9682084,
    "P1": 14.9589314,
    "K2": 30.0821373,
    "MS4": 58.9841042,
    "Q1": 13.3986609,
    "M6": 86.9523127,
}


def _corners(grid: Grid, valid: np.ndarray, lat, lon):
    """Rows, columns and weights of the four cells around each position.

    Weights are bilinear over the wet cells only, so the flow beside a coast
    is not dragged towards zero by the land. All four weights are zero where
    there is no wet cell.
    """
    row, col = grid.index(np.asarray(lat, dtype=float), np.asarray(lon, dtype=float))
    ny, nx = grid.shape
    inside = (row >= -0.5) & (row <= ny - 0.5) & (col >= -0.5) & (col <= nx - 0.5)
    row, col = np.clip(row, 0, ny - 1), np.clip(col, 0, nx - 1)
    r0 = np.minimum(np.floor(row).astype(int), ny - 2)
    c0 = np.minimum(np.floor(col).astype(int), nx - 2)
    fr, fc = row - r0, col - c0
    rows = np.stack([r0, r0, r0 + 1, r0 + 1])
    cols = np.stack([c0, c0 + 1, c0, c0 + 1])
    weights = np.stack([(1 - fr) * (1 - fc), (1 - fr) * fc, fr * (1 - fc), fr * fc])
    weights = weights * valid[rows, cols] * inside
    total = weights.sum(axis=0)
    return rows, cols, np.divide(weights, total, out=np.zeros_like(weights), where=total > 1e-9), total > 1e-9


@dataclass
class GriddedField:
    """Currents on a grid at a sequence of times.

    ``u`` and ``v`` have shape (times, rows, columns) in m/s, NaN on land.
    """

    grid: Grid
    times: np.ndarray
    u: np.ndarray
    v: np.ndarray

    def __post_init__(self) -> None:
        self.times = np.asarray(self.times, dtype=float)
        if self.u.shape != (self.times.size, *self.grid.shape) or self.v.shape != self.u.shape:
            raise ValueError("u and v must have shape (times, rows, columns)")
        self._valid = ~np.isnan(self.u[0])

    @property
    def lat0(self) -> float:
        return self.grid.lat0

    @property
    def lon0(self) -> float:
        return self.grid.lon0

    def velocity(self, lat, lon, time):
        """Current (u, v) in m/s at each position and time; NaN on land or outside the run."""
        lat, lon, time = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (lat, lon, time)))
        shape = lat.shape
        lat, lon, time = lat.ravel(), lon.ravel(), time.ravel()
        rows, cols, weights, found = _corners(self.grid, self._valid, lat, lon)
        found &= (time >= self.times[0]) & (time <= self.times[-1])
        step = np.clip(np.searchsorted(self.times, time, side="right") - 1, 0, self.times.size - 2)
        frac = np.clip((time - self.times[step]) / (self.times[step + 1] - self.times[step]), 0.0, 1.0)
        out = []
        for field in (self.u, self.v):
            at0 = np.nansum(field[step, rows, cols] * weights, axis=0)
            at1 = np.nansum(field[step + 1, rows, cols] * weights, axis=0)
            out.append(np.where(found, (1 - frac) * at0 + frac * at1, np.nan).reshape(shape))
        return out[0], out[1]

    def rate(self, lat, lon, time, half_window_s: float = 900.0):
        """Time derivative of the velocity in m/s per second."""
        time = np.asarray(time, dtype=float)
        lo = np.maximum(time - half_window_s, self.times[0])
        hi = np.minimum(time + half_window_s, self.times[-1])
        u0, v0 = self.velocity(lat, lon, lo)
        u1, v1 = self.velocity(lat, lon, hi)
        span = np.where(hi > lo, hi - lo, np.nan)
        return (u1 - u0) / span, (v1 - v0) / span

    def save(self, path: str | Path) -> None:
        g = self.grid
        np.savez_compressed(
            path, lat0=g.lat0, lon0=g.lon0, x0=g.x0, y0=g.y0, dx=g.dx, depth=g.depth, times=self.times, u=self.u, v=self.v
        )

    @classmethod
    def load(cls, path: str | Path) -> "GriddedField":
        with np.load(path) as f:
            grid = Grid(float(f["lat0"]), float(f["lon0"]), float(f["x0"]), float(f["y0"]), float(f["dx"]), f["depth"])
            return cls(grid, f["times"], f["u"], f["v"])


def simulate(
    grid: Grid,
    start: float,
    end: float,
    boundary: Callable[[np.ndarray, np.ndarray], Callable[[float], np.ndarray]] | None = None,
    wind: Callable[[float], tuple[float, float]] | None = None,
    rivers: Iterable[tuple[float, float, float]] = (),
    sample_s: float = 900.0,
    spinup_s: float = 12 * 3600.0,
    progress: Callable[[float], None] | None = None,
    **solver_options,
) -> GriddedField:
    """Run the physics model from ``start`` to ``end`` and keep the currents.

    ``boundary`` supplies the water level along the open sea: given the
    positions of the open cells it returns a function of time, which is what
    :meth:`currentmodel.stofs.LevelField.at` does. ``wind`` is a function of
    time returning the wind in m/s as (towards east, towards north).
    ``rivers`` are (lat, lon, discharge in m3/s).

    The run starts ``spinup_s`` earlier from still water so that start-up
    waves have died away by ``start``; the boundary data must reach back that
    far. ``progress`` is called with the fraction done.
    """
    solver = ShallowWater(grid, **solver_options)
    if boundary is not None:
        solver.set_boundary(boundary(*solver.boundary_latlon()))
    if wind is not None:
        solver.set_wind(wind)
    for lat, lon, discharge in rivers:
        solver.add_river(lat, lon, discharge)
    solver.start(start - spinup_s, ramp_s=0.5 * spinup_s)
    solver.run(start)

    times, us, vs = [], [], []

    def keep(s: ShallowWater) -> None:
        u, v = s.centre_velocity()
        times.append(s.time)
        us.append(u.astype("f4"))
        vs.append(v.astype("f4"))
        if progress is not None:
            progress((s.time - start) / (end - start))

    keep(solver)
    solver.run(end, sample_every=sample_s, on_sample=keep)
    return GriddedField(grid, np.array(times), np.array(us), np.array(vs))


# ---------------------------------------------------------------- harmonics


def resolvable(duration_s: float, names: Sequence[str] | None = None) -> list[str]:
    """Constituents a record of this length can tell apart (Rayleigh criterion).

    Two constituents are separable when they drift a full cycle apart over
    the record. A week separates the twice-daily from the daily tide; about
    15 days separates M2 from S2 and K1 from O1; a month adds N2.
    """
    hours = duration_s / 3600.0
    chosen: list[str] = []
    for name in names or CONSTITUENTS:
        speed = CONSTITUENTS[name]
        if speed * hours < 360.0:
            continue
        if all(abs(speed - CONSTITUENTS[other]) * hours >= 360.0 for other in chosen):
            chosen.append(name)
    return chosen


@dataclass
class HarmonicField:
    """Tidal currents as constituents per grid cell.

    The velocity is ``mean + sum_k (a_k cos(w_k (t - epoch)) + b_k sin(w_k (t - epoch)))``
    for each component. The slow 18.6-year modulation of the tide is not
    modelled, which costs a few percent a year away from the fitted period;
    refit yearly.
    """

    grid: Grid
    names: list[str]
    epoch: float
    coefficients_u: np.ndarray  # (1 + 2 * constituents, rows, columns), NaN on land
    coefficients_v: np.ndarray

    def __post_init__(self) -> None:
        self._valid = ~np.isnan(self.coefficients_u[0])

    @property
    def lat0(self) -> float:
        return self.grid.lat0

    @property
    def lon0(self) -> float:
        return self.grid.lon0

    @staticmethod
    def _basis(names: Sequence[str], epoch: float, time: np.ndarray, derivative: bool = False) -> np.ndarray:
        omega = np.radians([CONSTITUENTS[n] for n in names]) / 3600.0
        phase = np.outer(time - epoch, omega)
        if derivative:
            return np.column_stack([np.zeros(time.size), -np.sin(phase) * omega, np.cos(phase) * omega])
        return np.column_stack([np.ones(time.size), np.cos(phase), np.sin(phase)])

    @classmethod
    def fit(cls, field: GriddedField, names: Sequence[str] | None = None) -> "HarmonicField":
        """Least-squares tidal analysis of a simulated field, cell by cell."""
        duration = field.times[-1] - field.times[0]
        names = resolvable(duration, names)
        if not names:
            raise ValueError("the run is too short to fit any tidal constituent")
        epoch = float(field.times[0])
        basis = cls._basis(names, epoch, field.times)
        wet = ~np.isnan(field.u[0])
        coefficients = []
        for data in (field.u, field.v):
            solved = np.linalg.lstsq(basis, data[:, wet].astype(float), rcond=None)[0]
            full = np.full((basis.shape[1], *field.grid.shape), np.nan)
            full[:, wet] = solved
            coefficients.append(full)
        return cls(field.grid, list(names), epoch, coefficients[0], coefficients[1])

    def _evaluate(self, lat, lon, time, derivative: bool):
        lat, lon, time = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (lat, lon, time)))
        shape = lat.shape
        lat, lon, time = lat.ravel(), lon.ravel(), time.ravel()
        rows, cols, weights, found = _corners(self.grid, self._valid, lat, lon)
        basis = self._basis(self.names, self.epoch, time, derivative)
        out = []
        for coefficients in (self.coefficients_u, self.coefficients_v):
            local = np.nansum(coefficients[:, rows, cols] * weights, axis=1)  # (terms, positions)
            out.append(np.where(found, np.einsum("nk,kn->n", basis, local), np.nan).reshape(shape))
        return out[0], out[1]

    def velocity(self, lat, lon, time):
        """Tidal current (u, v) in m/s at each position, for any time."""
        return self._evaluate(lat, lon, time, derivative=False)

    def rate(self, lat, lon, time, half_window_s: float = 900.0):
        """Time derivative of the velocity in m/s per second (exact, from the harmonics)."""
        return self._evaluate(lat, lon, time, derivative=True)

    def ellipse(self, name: str):
        """Strongest current of one constituent in each cell, in m/s."""
        k = self.names.index(name)
        n = len(self.names)
        au, bu = self.coefficients_u[1 + k], self.coefficients_u[1 + n + k]
        av, bv = self.coefficients_v[1 + k], self.coefficients_v[1 + n + k]
        total = au**2 + bu**2 + av**2 + bv**2
        cross = au * bv - av * bu
        return np.sqrt(0.5 * (total + np.sqrt(np.maximum(total**2 - 4.0 * cross**2, 0.0))))

    def save(self, path: str | Path) -> None:
        g = self.grid
        np.savez_compressed(
            path,
            lat0=g.lat0,
            lon0=g.lon0,
            x0=g.x0,
            y0=g.y0,
            dx=g.dx,
            depth=g.depth,
            names=np.array(self.names),
            epoch=self.epoch,
            coefficients_u=self.coefficients_u,
            coefficients_v=self.coefficients_v,
        )

    @classmethod
    def load(cls, path: str | Path) -> "HarmonicField":
        with np.load(path) as f:
            grid = Grid(float(f["lat0"]), float(f["lon0"]), float(f["x0"]), float(f["y0"]), float(f["dx"]), f["depth"])
            return cls(grid, [str(n) for n in f["names"]], float(f["epoch"]), f["coefficients_u"], f["coefficients_v"])
