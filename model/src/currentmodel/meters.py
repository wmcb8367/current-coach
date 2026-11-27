"""Learn from a fixed current meter: how much is tide, and what predicts the rest a day ahead.

Where a venue has a current meter with a long record (NOAA's PORTS meters
in the United States, for instance), it is a far better teacher than
drifts: a reading every few minutes for months, against a few dozen drifts
a day on the days a coach goes out. This module asks the questions the
Current Coach models depend on, the same way for any meter:

1. **How much of the current is tide?** :func:`tidal_split` fits the
   tidal constituents to part of the record and predicts the rest. What the
   tide leaves over (the *residual*) is what weather, the ocean current
   offshore and everything else must explain.
2. **What predicts the residual a day ahead?** :func:`day_ahead` scores a
   set of named predictors on whole held-out days, using only what is known
   by the evening before (the meter's own recent residual, other meters,
   the wind of the last days, the tide gauge's surplus) plus the day's wind
   (a perfect forecast; a real one does somewhat worse).
3. **What does a meter nearby say right now?** :func:`nowcast_from`, the
   value of a live sensor some distance away.
4. **How does the water answer the wind?** :func:`wind_response`, the share
   of the wind speed and how far the current turns from it.

Run for a venue whose sensors were found with :mod:`currentmodel.sensors`::

    python -m currentmodel.meters --sensors miami-sensors.json --start 2025-10-01 --end 2026-11-26 \\
        --utc-offset -5 --cache cache --out miami-meters.json

Errors are root-mean-square vector errors in knots over the sailing hours.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from .drivers import cv_error
from .geo import KNOT_MS
from .tides import CONSTITUENTS, TideFit

HOUR = 3600.0
# Long-period constituents describe seasons and weather as much as tide; leave them to the residual.
TIDAL = [name for name in CONSTITUENTS if name not in ("SA", "SSA", "MM", "MF")]


@dataclass
class TidalSplit:
    tide_u: np.ndarray
    tide_v: np.ndarray
    residual_u: np.ndarray
    residual_v: np.ndarray
    mean_u: float                 # the steady flow the fit found, m/s
    mean_v: float
    amplitudes_kn: dict           # main constituents, speed of the tidal ellipse's long axis, roughly
    test_rms_kn: float            # measured current on the held-out part
    test_tide_error_kn: float     # what the tide prediction (with its mean) leaves on the held-out part


def tidal_split(t, u, v, fit_before: float | None = None, names: Sequence[str] = TIDAL) -> TidalSplit:
    """Tide and residual of a current record, the tide fitted before ``fit_before`` only (default: the first 60%)."""
    t, u, v = (np.asarray(a, dtype=float) for a in (t, u, v))
    good = np.isfinite(u) & np.isfinite(v)
    if good.sum() < 24 * 30:
        raise ValueError("a current meter needs at least a month of readings for a tidal fit")
    if fit_before is None:
        fit_before = float(np.quantile(t[good], 0.6))
    train = good & (t < fit_before)
    fu = TideFit.fit(t[train], u[train], list(names))
    fv = TideFit.fit(t[train], v[train], list(names))
    tu, tv = fu.predict(t), fv.predict(t)
    test = good & (t >= fit_before)
    pick = test if test.any() else good
    rms = float(np.sqrt(np.mean(u[pick] ** 2 + v[pick] ** 2)) / KNOT_MS)
    err = float(np.sqrt(np.mean((u[pick] - tu[pick]) ** 2 + (v[pick] - tv[pick]) ** 2)) / KNOT_MS)
    au, av = fu.amplitudes(), fv.amplitudes()
    amps = {n: float(np.hypot(au[n], av[n]) / KNOT_MS) for n in ("M2", "S2", "N2", "K1", "O1") if n in au}
    return TidalSplit(tu, tv, u - tu, v - tv, float(fu.coefficients[0]), float(fv.coefficients[0]), amps, rms, err)


def window_mean(t: np.ndarray, series: np.ndarray, at: np.ndarray, window_h: float, min_share: float = 0.6) -> np.ndarray:
    """Mean of an hourly ``series`` over the ``window_h`` hours up to each time in ``at`` (NaN if mostly missing)."""
    t = np.asarray(t, dtype=float)
    x = np.asarray(series, dtype=float)
    total = np.concatenate([[0.0], np.cumsum(np.nan_to_num(x))])
    count = np.concatenate([[0], np.cumsum(np.isfinite(x))])
    i1 = np.searchsorted(t, at, side="right")
    i0 = np.searchsorted(t, np.asarray(at) - window_h * HOUR, side="right")
    n = count[i1] - count[i0]
    out = np.full(np.shape(at), np.nan)
    ok = n >= max(1, min_share * window_h)
    out[ok] = (total[i1] - total[i0])[ok] / n[ok]
    return out


def sailing_hours(t: np.ndarray, utc_offset_h: float, first: int = 9, last: int = 16) -> tuple[np.ndarray, np.ndarray]:
    """Which hours fall in the sailing day (local ``first`` to ``last`` o'clock), and the local day of each."""
    local = np.asarray(t, dtype=float) + utc_offset_h * HOUR
    hour = np.floor(local / HOUR).astype(int) % 24
    return (hour >= first) & (hour <= last), np.floor(local / 86400.0).astype(int)


def issue_times(day: np.ndarray, utc_offset_h: float, issue_hour: float = 18.0) -> np.ndarray:
    """When the prediction for each local ``day`` is made: ``issue_hour`` local, the evening before."""
    return (np.asarray(day, dtype=float) - 1) * 86400.0 + issue_hour * HOUR - utc_offset_h * HOUR


def eased(recent: np.ndarray, old: np.ndarray, floor_ms: float = 0.5) -> np.ndarray:
    """How far the wind has eased: 0 when the last hours blow as hard as the old mean, 1 in a calm after it."""
    return np.clip(1.0 - np.hypot(*recent.T) / np.maximum(np.hypot(*old.T), floor_ms), 0.0, 1.0)


def predictors(
    t: np.ndarray,
    residual: tuple[np.ndarray, np.ndarray],
    at: np.ndarray,
    issued: np.ndarray,
    wind: tuple[np.ndarray, np.ndarray] | None = None,
    others: Mapping[str, tuple[np.ndarray, np.ndarray]] | None = None,
    surplus: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """The candidate predictors at times ``at`` for predictions issued at ``issued``, each (n, 2).

    Known the evening before: ``recent`` (the meter's residual over the 6 h
    before issue), ``yesterday`` (24 h), the same for each of ``others``,
    ``old wind`` (72 h before issue) and ``sea level`` (the gauge's surplus
    over the tide and its change over a day). The day's own wind:
    ``wind`` (6 h mean up to the time) and ``wind memory`` (the 72 h mean
    scaled by how far the wind has since eased: Willie's pile-up theory).
    """
    ru, rv = residual
    out = {
        "recent": np.column_stack([window_mean(t, ru, issued, 6), window_mean(t, rv, issued, 6)]),
        "yesterday": np.column_stack([window_mean(t, ru, issued, 24), window_mean(t, rv, issued, 24)]),
    }
    for name, (ou, ov) in (others or {}).items():
        out[f"{name} recent"] = np.column_stack([window_mean(t, ou, issued, 6), window_mean(t, ov, issued, 6)])
    if wind is not None:
        wu, wv = wind
        out["old wind"] = np.column_stack([window_mean(t, wu, issued, 72), window_mean(t, wv, issued, 72)])
        w6 = np.column_stack([window_mean(t, wu, at, 6), window_mean(t, wv, at, 6)])
        w72 = np.column_stack([window_mean(t, wu, at, 72), window_mean(t, wv, at, 72)])
        out["wind"] = w6
        out["wind memory"] = w72 * eased(w6, w72)[:, None]
    if surplus is not None:
        now = window_mean(t, surplus, issued, 24)
        out["sea level"] = np.column_stack([now, now - window_mean(t, surplus, issued - 24 * HOUR, 24)])
    return out


DEFAULT_COMBINATIONS = {
    "tide only": [],
    "+ meter's last 6 h": ["recent"],
    "+ meter's last 24 h": ["yesterday"],
    "+ old wind": ["old wind"],
    "+ sea level": ["sea level"],
    "+ day's wind": ["wind"],
    "+ day's wind and memory": ["wind", "wind memory"],
    "last 6 h + wind": ["recent", "wind"],
    "last 6 h + wind + memory": ["recent", "wind", "wind memory"],
    "last 6 h + wind + memory + sea level": ["recent", "wind", "wind memory", "sea level"],
}


def day_ahead(
    t: np.ndarray,
    residual: tuple[np.ndarray, np.ndarray],
    utc_offset_h: float,
    wind: tuple[np.ndarray, np.ndarray] | None = None,
    others: Mapping[str, tuple[np.ndarray, np.ndarray]] | None = None,
    surplus: np.ndarray | None = None,
    combinations: Mapping[str, Sequence[str]] | None = None,
    issue_hour: float = 18.0,
) -> dict:
    """Held-out-day error (kn) of the meter's residual predicted from each combination of predictors.

    Each combination is a linear fit (east and north, so the current may
    set at any angle to its predictors) learned on all the other days. "tide
    only" predicts no residual beyond the steady mean, so it is the error of
    the tidal prediction alone.
    """
    t = np.asarray(t, dtype=float)
    sail, day = sailing_hours(t, utc_offset_h)
    ok = sail & np.isfinite(residual[0]) & np.isfinite(residual[1])
    at, groups = t[ok], day[ok]
    X = predictors(t, residual, at, issue_times(groups, utc_offset_h, issue_hour), wind, others, surplus)
    if combinations is None:
        combinations = dict(DEFAULT_COMBINATIONS)
        for name in others or {}:
            combinations[f"+ {name}'s last 6 h"] = [f"{name} recent"]
            combinations[f"last 6 h + {name} + wind"] = ["recent", f"{name} recent", "wind"]
    combinations = {k: [n for n in v if n in X] for k, v in combinations.items() if all(n in X for n in v)}
    used = sorted({n for names in combinations.values() for n in names})
    keep = np.all([np.isfinite(X[n]).all(axis=1) for n in used], axis=0) if used else np.ones(len(at), bool)
    Y = np.column_stack([residual[0][ok], residual[1][ok]])[keep]
    g = groups[keep]
    rows = {}
    for label, names in combinations.items():
        M = np.column_stack([X[n][keep] for n in names]) if names else np.zeros((len(Y), 0))
        rows[label] = cv_error(M, Y, g) / KNOT_MS
    return {"hours": int(len(Y)), "days": int(len(np.unique(g))), "error_kn": rows}


def nowcast_from(t, target: tuple[np.ndarray, np.ndarray], other: tuple[np.ndarray, np.ndarray], utc_offset_h: float) -> tuple[float, float]:
    """Held-out-day error (kn) of the target's residual without, and then with, another meter's residual at the same hour."""
    sail, day = sailing_hours(np.asarray(t, dtype=float), utc_offset_h)
    Y = np.column_stack(target)
    Z = np.column_stack(other)
    ok = sail & np.isfinite(Y).all(axis=1) & np.isfinite(Z).all(axis=1)
    return cv_error(np.zeros((ok.sum(), 0)), Y[ok], day[ok]) / KNOT_MS, cv_error(Z[ok], Y[ok], day[ok]) / KNOT_MS


def wind_response(t, residual: tuple[np.ndarray, np.ndarray], wind: tuple[np.ndarray, np.ndarray], utc_offset_h: float, window_h: float = 6) -> dict:
    """How the residual answers the wind of the last ``window_h`` hours.

    Fits current = M @ wind and gives M's nearest scaled rotation: the
    share of the wind speed (per cent) and the turn from the wind, positive
    to the right of it (clockwise, as the Earth's rotation turns surface
    water in the northern hemisphere).
    """
    t = np.asarray(t, dtype=float)
    sail, _ = sailing_hours(t, utc_offset_h)
    W = np.column_stack([window_mean(t, wind[0], t, window_h), window_mean(t, wind[1], t, window_h)])
    Y = np.column_stack(residual)
    ok = sail & np.isfinite(Y).all(axis=1) & np.isfinite(W).all(axis=1)
    A = np.column_stack([np.ones(ok.sum()), W[ok]])
    coef, *_ = np.linalg.lstsq(A, Y[ok], rcond=None)
    M = coef[1:].T
    a = 0.5 * (M[0, 0] + M[1, 1])
    b = 0.5 * (M[1, 0] - M[0, 1])
    return {"share_pct": float(100 * np.hypot(a, b)), "turn_right_deg": float(-np.degrees(np.arctan2(b, a))), "matrix": M.tolist()}


def study(table: Mapping[str, np.ndarray], meters: Sequence[str], wind: str | None, gauge: str | None, utc_offset_h: float,
          fit_before: float | None = None) -> dict:
    """All of the above for each meter of an hourly table from :func:`currentmodel.sensors.collect`.

    ``meters`` are sensor keys (``coops:mi0201``), ``wind`` and ``gauge``
    the keys of the wind station and tide gauge to use.
    """
    t = np.asarray(table["times"], dtype=float)
    splits = {m: tidal_split(t, table[f"{m}/current_u"], table[f"{m}/current_v"], fit_before) for m in meters
              if f"{m}/current_u" in table and np.isfinite(table[f"{m}/current_u"]).sum() >= 24 * 30}
    w = (table[f"{wind}/wind_u"], table[f"{wind}/wind_v"]) if wind and f"{wind}/wind_u" in table else None
    surplus = None
    if gauge and f"{gauge}/sea_level" in table:
        level = np.asarray(table[f"{gauge}/sea_level"], dtype=float)
        good = np.isfinite(level)
        surplus = level - TideFit.fit(t[good], level[good]).predict(t)
    out = {}
    for m, s in splits.items():
        res = (s.residual_u, s.residual_v)
        others = {o: (x.residual_u, x.residual_v) for o, x in splits.items() if o != m}
        entry = {
            "tide": {"rms_kn": s.test_rms_kn, "tide_error_kn": s.test_tide_error_kn, "mean_kn": [s.mean_u / KNOT_MS, s.mean_v / KNOT_MS],
                     "amplitudes_kn": s.amplitudes_kn},
            "day_ahead": day_ahead(t, res, utc_offset_h, w, others, surplus),
            "nowcast": {o: nowcast_from(t, res, r, utc_offset_h) for o, r in others.items()},
        }
        if w is not None:
            entry["wind_response"] = wind_response(t, res, w, utc_offset_h)
        out[m] = entry
    return out


def _time(text: str) -> float:
    stamp = dt.datetime.fromisoformat(text)
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)).timestamp()


def main(argv: list[str] | None = None) -> None:
    from .sensors import Fetcher, collect

    parser = argparse.ArgumentParser(description="What a venue's current meters say about tide, wind and the day ahead.")
    parser.add_argument("--sensors", required=True, help="the venue's sensor manifest from currentmodel.sensors")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--utc-offset", type=float, required=True, help="local standard time minus UTC, hours (sailing hours are 9 to 16 local)")
    parser.add_argument("--wind", default=None, help="sensor key of the wind station (default: nearest with wind)")
    parser.add_argument("--gauge", default=None, help="sensor key of the tide gauge (default: nearest)")
    parser.add_argument("--max-km", type=float, default=30.0, help="use current meters within this distance")
    parser.add_argument("--cache", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    manifest = json.load(open(args.sensors))
    sensors = manifest["sensors"]
    key = lambda s: f"{s['catalogue']}:{s['id']}"
    meters = [key(s) for s in sensors if "current" in s["measures"] and s["distance_km"] <= args.max_km and s["catalogue"] == "coops"]
    gauge = args.gauge or next((key(s) for s in sorted(sensors, key=lambda s: s["distance_km"]) if s["catalogue"] == "coops" and "sea_level" in s["measures"]), None)
    wind = args.wind or gauge  # NOAA tide gauges usually carry a weather station
    if not meters:
        raise SystemExit("no current meter with a record within reach of this venue")
    table = collect(manifest, _time(args.start), _time(args.end), Fetcher(args.cache), only=meters + [k for k in (gauge, wind) if k],
                    log=lambda m: print(m, flush=True))
    result = study(table, meters, wind, gauge, args.utc_offset)
    with open(args.out, "w") as handle:
        json.dump(result, handle, indent=1)
    for m, entry in result.items():
        print(f"\n{m}: current {entry['tide']['rms_kn']:.2f} kn rms, tide prediction leaves {entry['tide']['tide_error_kn']:.2f} kn")
        for label, err in entry["day_ahead"]["error_kn"].items():
            print(f"  {err:.3f}  {label}")


if __name__ == "__main__":
    main()
