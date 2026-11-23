"""A depth-averaged shallow-water solver for venue-scale currents.

This is the physics model: given water depths on a grid, it computes how the
tide entering at the open edges, the wind on the surface and rivers flowing in
move water through the venue. Channels speed up, shoals slow down and
headlands shed eddies because the equations make them, not because anyone
measured them there.

Equations (depth-averaged, on a plane):

    du/dt + u du/dx + v du/dy - f v = -g d(eta)/dx - Cd |u| u / H + tau_x / (rho H) + nu lap(u)
    dv/dt + u dv/dx + v dv/dy + f u = -g d(eta)/dy - Cd |u| v / H + tau_y / (rho H) + nu lap(v)
    d(eta)/dt + d(H u)/dx + d(H v)/dy = river inflow

where eta is the surface height, H the total depth, f the Coriolis parameter,
Cd bottom friction from Manning's n, and tau the wind stress.

Numerics: a regular Arakawa C grid (surface height at cell centres, velocity
on cell faces), explicit forward-backward time stepping, first-order upwind
advection and friction treated implicitly. This is the classic scheme for
tidal models. It is simple and robust rather than state of the art: coasts
are stair-stepped, there is no wetting and drying (shallow cells are deepened
to ``min_depth``), and the result is the depth-averaged flow, not the surface
layer. The correction layer exists to learn what these simplifications miss.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy import ndimage

from .geo import to_latlon, to_xy

G = 9.81
RHO_WATER = 1025.0
RHO_AIR = 1.225
WIND_DRAG = 1.3e-3
EARTH_ROTATION = 7.2921e-5


@dataclass
class Grid:
    """Water depths on a regular grid in local metres.

    ``depth`` has shape (rows north, columns east), in metres below mean sea
    level, positive down. Zero, negative or NaN means land. Cell (0, 0) is the
    south-west corner, centred at local (x0, y0) relative to (lat0, lon0).
    """

    lat0: float
    lon0: float
    x0: float
    y0: float
    dx: float
    depth: np.ndarray

    def __post_init__(self) -> None:
        self.depth = np.where(np.isnan(self.depth), 0.0, np.asarray(self.depth, dtype=float))

    @property
    def shape(self) -> tuple[int, int]:
        return self.depth.shape

    @property
    def wet(self) -> np.ndarray:
        return self.depth > 0.0

    def centres_xy(self):
        ny, nx = self.shape
        return np.meshgrid(self.x0 + self.dx * np.arange(nx), self.y0 + self.dx * np.arange(ny))

    def centres_latlon(self):
        x, y = self.centres_xy()
        return to_latlon(x, y, self.lat0, self.lon0)

    def index(self, lat, lon):
        """Fractional (row, column) of a position."""
        x, y = to_xy(lat, lon, self.lat0, self.lon0)
        return (y - self.y0) / self.dx, (x - self.x0) / self.dx


class ShallowWater:
    """Time-stepping solver on a :class:`Grid`.

    Parameters
    ----------
    manning:
        Manning's roughness n. 0.025 is typical for sand and mud. 0 turns
        bottom friction off.
    viscosity:
        Horizontal eddy viscosity in m2/s.
    min_depth:
        Wet cells shallower than this are deepened to it, because the solver
        has no wetting and drying.
    open_boundary:
        If true, wet cells on the edge of the grid are open sea whose surface
        height is prescribed by ``boundary``. If false the grid is a closed
        basin.
    edge_zone_cells:
        Width of the zone beside the open sea in which the momentum-carrying
        (advection) terms fade to zero. Prescribing the surface height at an
        open edge leaves the flow along it unconstrained, and with advection
        that grows into jets; fading it out there is the usual cure.
    """

    def __init__(
        self,
        grid: Grid,
        manning: float = 0.025,
        viscosity: float = 2.0,
        min_depth: float = 2.0,
        advection: bool = True,
        open_boundary: bool = True,
        coriolis: bool = True,
        gravity: float = G,
        cfl: float = 0.6,
        edge_zone_cells: int = 10,
    ):
        self.grid = grid
        self.manning = manning
        self.viscosity = viscosity
        self.min_depth = min_depth
        self.advection = advection
        self.gravity = gravity
        ny, nx = grid.shape
        if ny < 3 or nx < 3:
            raise ValueError("the grid needs at least 3 by 3 cells")
        wet = grid.wet
        self.h = np.where(wet, np.maximum(grid.depth, min_depth), 0.0)

        edge = np.zeros_like(wet)
        edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
        self.boundary_cells = wet & edge if open_boundary else np.zeros_like(wet)
        self.interior = wet & ~self.boundary_cells

        # A face carries flow only between two wet cells, and not between two
        # open-sea cells: water enters and leaves across the edge, not along it.
        sea = self.boundary_cells
        self._umask = wet[:, :-1] & wet[:, 1:] & ~(sea[:, :-1] & sea[:, 1:])
        self._vmask = wet[:-1, :] & wet[1:, :] & ~(sea[:-1, :] & sea[1:, :])
        if sea.any() and edge_zone_cells > 0:
            fade = np.clip((ndimage.distance_transform_edt(~sea) - 1.0) / edge_zone_cells, 0.0, 1.0)
        else:
            fade = np.ones((ny, nx))
        self._fade_u = np.minimum(fade[:, :-1], fade[:, 1:])
        self._fade_v = np.minimum(fade[:-1, :], fade[1:, :])
        self._hu = 0.5 * (self.h[:, :-1] + self.h[:, 1:])
        self._hv = 0.5 * (self.h[:-1, :] + self.h[1:, :])

        self.f = 2.0 * EARTH_ROTATION * np.sin(np.radians(grid.lat0)) if coriolis else 0.0
        fastest = np.sqrt(max(gravity, 1e-12) * (self.h.max() + 3.0))
        self.dt = cfl * grid.dx / (np.sqrt(2.0) * fastest)

        self.eta = np.zeros((ny, nx))
        self.u = np.zeros((ny, nx + 1))
        self.v = np.zeros((ny + 1, nx))
        self.time = 0.0
        self._source = np.zeros((ny, nx))
        self._boundary: Callable[[float], np.ndarray | float] | None = None
        self._wind: Callable[[float], tuple[float, float]] | None = None
        self._ramp_s = 0.0
        self._start = 0.0

    # ---------------------------------------------------------------- forcing

    def boundary_latlon(self):
        """Positions of the open-sea cells, in the order ``boundary`` values are expected."""
        lat, lon = self.grid.centres_latlon()
        return lat[self.boundary_cells], lon[self.boundary_cells]

    def set_boundary(self, surface_height: Callable[[float], np.ndarray | float]) -> None:
        """Surface height in metres at the open-sea cells as a function of time."""
        self._boundary = surface_height

    def set_wind(self, wind: Callable[[float], tuple[float, float]]) -> None:
        """Wind in m/s as (towards east, towards north) as a function of time."""
        self._wind = wind

    def add_river(self, lat: float, lon: float, discharge_m3s: float) -> None:
        """Steady inflow at the wet interior cell nearest a position."""
        row, col = self.grid.index(lat, lon)
        rows, cols = np.nonzero(self.interior)
        nearest = np.argmin((rows - row) ** 2 + (cols - col) ** 2)
        self._source[rows[nearest], cols[nearest]] += discharge_m3s / self.grid.dx**2

    def start(self, time: float, ramp_s: float = 0.0) -> None:
        """Set the clock. Forcing is eased in over ``ramp_s`` seconds to limit start-up waves."""
        self.time = self._start = float(time)
        self._ramp_s = ramp_s

    # ------------------------------------------------------------------- step

    def step(self) -> None:
        dt, dx, g = self.dt, self.grid.dx, self.gravity
        eta, u, v = self.eta, self.u, self.v
        t_new = self.time + dt
        ramp = 1.0 if self._ramp_s <= 0 else min(1.0, (t_new - self._start) / self._ramp_s)

        # Continuity: move water with the current velocities.
        depth_u = np.maximum(self._hu + 0.5 * (eta[:, :-1] + eta[:, 1:]), 0.5 * self.min_depth)
        depth_v = np.maximum(self._hv + 0.5 * (eta[:-1, :] + eta[1:, :]), 0.5 * self.min_depth)
        flux_x = np.zeros_like(u)
        flux_y = np.zeros_like(v)
        flux_x[:, 1:-1] = depth_u * u[:, 1:-1]
        flux_y[1:-1, :] = depth_v * v[1:-1, :]
        divergence = (flux_x[:, 1:] - flux_x[:, :-1] + flux_y[1:, :] - flux_y[:-1, :]) / dx
        eta = np.where(self.interior, eta + dt * (self._source * ramp - divergence), 0.0)
        if self._boundary is not None and self.boundary_cells.any():
            eta[self.boundary_cells] = ramp * np.asarray(self._boundary(t_new))

        stress_x = stress_y = 0.0
        if self._wind is not None:
            wind_u, wind_v = self._wind(t_new)
            drag = RHO_AIR * WIND_DRAG * np.hypot(wind_u, wind_v) * ramp
            stress_x, stress_y = drag * wind_u, drag * wind_v

        # Momentum east, using the new surface. Friction is implicit so it can never reverse the flow.
        ui = u[:, 1:-1]
        v_at_u = 0.25 * (v[:-1, :-1] + v[1:, :-1] + v[:-1, 1:] + v[1:, 1:])
        rhs = -g * (eta[:, 1:] - eta[:, :-1]) / dx + self.f * v_at_u + stress_x / (RHO_WATER * depth_u)
        if self.advection or self.viscosity:
            padded = np.pad(ui, ((1, 1), (0, 0)), mode="edge")
            if self.advection:
                along = np.where(ui > 0, ui - u[:, :-2], u[:, 2:] - ui)
                across = np.where(v_at_u > 0, ui - padded[:-2], padded[2:] - ui)
                rhs -= self._fade_u * (ui * along + v_at_u * across) / dx
            if self.viscosity:
                rhs += self.viscosity * (u[:, 2:] + u[:, :-2] + padded[2:] + padded[:-2] - 4.0 * ui) / dx**2
        ui_new = (ui + dt * rhs) / (1.0 + dt * self._drag(depth_u) * np.hypot(ui, v_at_u) / depth_u)
        u_new = np.zeros_like(u)
        u_new[:, 1:-1] = np.where(self._umask, ui_new, 0.0)
        # Edge faces copy their neighbour so averages at the open sea are not dragged to zero.
        u_new[:, 0] = np.where(self.boundary_cells[:, 0], u_new[:, 1], 0.0)
        u_new[:, -1] = np.where(self.boundary_cells[:, -1], u_new[:, -2], 0.0)

        # Momentum north, using the new east velocity so the Coriolis terms stay stable.
        vi = v[1:-1, :]
        u_at_v = 0.25 * (u_new[:-1, :-1] + u_new[:-1, 1:] + u_new[1:, :-1] + u_new[1:, 1:])
        rhs = -g * (eta[1:, :] - eta[:-1, :]) / dx - self.f * u_at_v + stress_y / (RHO_WATER * depth_v)
        if self.advection or self.viscosity:
            padded = np.pad(vi, ((0, 0), (1, 1)), mode="edge")
            if self.advection:
                along = np.where(vi > 0, vi - v[:-2, :], v[2:, :] - vi)
                across = np.where(u_at_v > 0, vi - padded[:, :-2], padded[:, 2:] - vi)
                rhs -= self._fade_v * (vi * along + u_at_v * across) / dx
            if self.viscosity:
                rhs += self.viscosity * (v[2:, :] + v[:-2, :] + padded[:, 2:] + padded[:, :-2] - 4.0 * vi) / dx**2
        vi_new = (vi + dt * rhs) / (1.0 + dt * self._drag(depth_v) * np.hypot(u_at_v, vi) / depth_v)
        v_new = np.zeros_like(v)
        v_new[1:-1, :] = np.where(self._vmask, vi_new, 0.0)
        v_new[0, :] = np.where(self.boundary_cells[0, :], v_new[1, :], 0.0)
        v_new[-1, :] = np.where(self.boundary_cells[-1, :], v_new[-2, :], 0.0)

        self.eta, self.u, self.v, self.time = eta, u_new, v_new, t_new

    def _drag(self, depth: np.ndarray) -> np.ndarray | float:
        if self.manning <= 0:
            return 0.0
        return self.gravity * self.manning**2 / np.cbrt(depth)

    # -------------------------------------------------------------------- run

    def centre_velocity(self):
        """Velocity (u, v) at cell centres; NaN on land."""
        u = 0.5 * (self.u[:, :-1] + self.u[:, 1:])
        v = 0.5 * (self.v[:-1, :] + self.v[1:, :])
        wet = self.grid.wet
        return np.where(wet, u, np.nan), np.where(wet, v, np.nan)

    def volume(self) -> float:
        """Water volume above the still level in the interior, in m3."""
        return float(np.sum(self.eta[self.interior]) * self.grid.dx**2)

    def run(
        self,
        until: float,
        sample_every: float | None = None,
        on_sample: Callable[["ShallowWater"], None] | None = None,
    ) -> None:
        """Advance to time ``until``, calling ``on_sample`` every ``sample_every`` seconds."""
        next_sample = self.time + sample_every if sample_every else np.inf
        while self.time < until - 1e-9:
            self.step()
            if not np.isfinite(self.eta).all():
                raise FloatingPointError("the solver became unstable; lower cfl or raise viscosity")
            if self.time >= next_sample - 1e-9:
                if on_sample is not None:
                    on_sample(self)
                next_sample += sample_every
