"""A simulated venue for testing the model before real data is connected.

The simulated truth is a tidal stream through a channel that speeds up off a
headland, plus a wind-driven surface current and a steady outflow. The
simulated "official baseline" has the faults a real one is expected to have:
it is too weak, slightly rotated, turns late, is given only at coarse points
that smooth the headland away, and knows nothing about wind or outflow.

None of the numbers here describe a real place. They only give the model
something with known answers to recover.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .baseline import TidalStreamField
from .correction import Conditions
from .geo import speed_dir_from_uv, to_latlon, to_xy

LAT0, LON0 = 22.28, 114.17
HOUR = 3600.0
# Period in hours, relative amplitude, phase in radians: M2, S2, K1, O1.
CONSTITUENTS = ((12.4206, 1.00, 0.3), (12.0, 0.40, 1.1), (23.9345, 0.95, 2.0), (25.8193, 0.80, 0.4))

BASELINE_SCALE = 0.8          # baseline speed as a fraction of the truth
BASELINE_ROTATION_DEG = 8.0   # truth is rotated this far clockwise from the baseline
BASELINE_LATE_S = 20 * 60.0   # baseline turns this late
WIND_FACTOR = 0.02            # surface current as a fraction of wind speed
OUTFLOW = (-0.06, 0.01)       # steady flow in m/s


def tide_signal(time):
    """Dimensionless tidal stream strength, roughly within -1 to 1."""
    time = np.asarray(time, dtype=float)
    total = sum(a * np.cos(2 * np.pi * time / (p * HOUR) + ph) for p, a, ph in CONSTITUENTS)
    return total / sum(a for _, a, _ in CONSTITUENTS) * 1.6


def _stream(x, y, headland: bool):
    """Peak tidal stream (u, v) in m/s at local position (x, y)."""
    bend = np.radians(12.0) * np.tanh(x / 1500.0)
    speed = 0.45 * (1.0 - 0.35 * np.tanh(np.abs(y) / 1200.0) ** 2)
    if headland:
        speed = speed * (1.0 + 0.7 * np.exp(-((x - 300.0) ** 2 + (y - 500.0) ** 2) / (2 * 350.0**2)))
    return speed * np.cos(bend), speed * np.sin(bend)


@dataclass
class World:
    baseline: TidalStreamField
    conditions: Conditions
    start: float
    end: float

    def truth(self, lat, lon, time):
        """True surface current (u, v) in m/s."""
        x, y = to_xy(lat, lon, LAT0, LON0)
        su, sv = _stream(x, y, headland=True)
        s = tide_signal(time)
        wu, wv = self.conditions.wind(time)
        return (su * s + WIND_FACTOR * wu + OUTFLOW[0], sv * s + WIND_FACTOR * wv + OUTFLOW[1])


def make_world(days: int = 30, start: float = 1_790_000_000.0, seed: int = 0) -> World:
    """Build the simulated truth, its faulty baseline and a wind record."""
    rng = np.random.default_rng(seed)
    end = start + days * 86_400.0

    # Baseline points on a 700 m grid, every 15 minutes.
    gx, gy = np.meshgrid(np.arange(-2800.0, 2801.0, 700.0), np.arange(-2100.0, 2101.0, 700.0))
    gx, gy = gx.ravel(), gy.ravel()
    lat, lon = to_latlon(gx, gy, LAT0, LON0)
    times = np.arange(start, end + 1.0, 900.0)
    su, sv = _stream(gx, gy, headland=False)
    turn = np.radians(BASELINE_ROTATION_DEG)  # counter-clockwise from the truth
    bu = BASELINE_SCALE * (su * np.cos(turn) - sv * np.sin(turn))
    bv = BASELINE_SCALE * (su * np.sin(turn) + sv * np.cos(turn))
    signal = tide_signal(times - BASELINE_LATE_S)[:, None]
    baseline = TidalStreamField(lat, lon, times, signal * bu[None, :], signal * bv[None, :])

    # Wind: a slowly varying vector, a few m/s, changing over a day or two.
    hours = np.arange(start, end + 1.0, HOUR)
    smooth = np.ones(36) / 36.0
    # Averaging 36 unit normals divides their spread by 6; the factor 24 gives about 4 m/s.
    wind_u = 24.0 * np.convolve(rng.standard_normal(hours.size + 35), smooth, "valid")
    wind_v = 24.0 * np.convolve(rng.standard_normal(hours.size + 35), smooth, "valid")
    return World(baseline, Conditions(hours, wind_u, wind_v), start, end)


def make_app_records(
    world: World,
    sessions: int = 12,
    drifts_per_session: int = 16,
    users: tuple[str, ...] = ("coach-a", "coach-b", "coach-c"),
    gps_sigma_m: float = 4.0,
    scatter_ms: float = 0.03,
    seed: int = 1,
) -> list[dict]:
    """Simulated drifts in the format the iOS app uploads.

    Each session is one training day: a few hours of drifts clustered around
    that day's race area. Every record is anchored at the start fix and start
    time, with speed in metres per minute and the direction the current comes
    from, exactly as the app stores them.
    """
    rng = np.random.default_rng(seed)
    total_days = int((world.end - world.start) // 86_400)
    day_numbers = np.sort(rng.choice(np.arange(1, total_days - 1), size=sessions, replace=False))
    records = []
    for day in day_numbers:
        centre = rng.uniform([-1200.0, -700.0], [1400.0, 1000.0])
        session_start = world.start + day * 86_400.0 + rng.uniform(1.0, 9.0) * HOUR
        user = users[int(rng.integers(len(users)))]
        for _ in range(drifts_per_session):
            start_time = session_start + rng.uniform(0.0, 3.0) * HOUR
            duration = float(rng.uniform(45.0, 180.0))
            x, y = centre + rng.normal(0.0, 350.0, size=2)
            mid_lat, mid_lon = to_latlon(x, y, LAT0, LON0)
            u, v = world.truth(mid_lat, mid_lon, start_time + duration / 2)
            # GPS error at both ends plus scatter from windage and small eddies.
            noise = np.sqrt(2.0) * gps_sigma_m / duration
            u = float(u) + rng.normal(0.0, np.hypot(noise, scatter_ms))
            v = float(v) + rng.normal(0.0, np.hypot(noise, scatter_ms))
            start_lat, start_lon = to_latlon(x - u * duration / 2, y - v * duration / 2, LAT0, LON0)
            speed, dir_to = speed_dir_from_uv(u, v)
            records.append(
                {
                    "clientId": f"sim-{len(records):04d}",
                    "userId": user,
                    "timestamp": float(start_time),
                    "durationSeconds": duration,
                    "speedMetersPerMinute": float(speed) * 60.0,
                    "fromDirectionDegrees": float((dir_to + 180.0) % 360.0),
                    "latitude": float(start_lat),
                    "longitude": float(start_lon),
                    "gpsAccuracyMeters": gps_sigma_m,
                    "isValid": True,
                    "source": "simulated",
                }
            )
    return records
