"""Scoring predictions against drifts.

The headline number is the vector error in knots. Skill compares a model with
the baseline: 0 means no better than the baseline, 1 means perfect.

Validation holds out whole days. Drifts from one session share the same tide
and weather, so holding out single drifts at random would let the model see
the answer through their neighbours and flatter it.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .baseline import TidalStreamField
from .correction import Conditions, Hyper, VenueCorrection
from .geo import KNOT_MS, angle_diff_deg, speed_dir_from_uv
from .measurements import Drift, as_arrays

MIN_SPEED_FOR_DIRECTION_KN = 0.3


def score(pred_u, pred_v, obs_u, obs_v) -> dict[str, float]:
    """Error metrics of predicted against observed currents (inputs in m/s)."""
    pred_u, pred_v, obs_u, obs_v = (np.asarray(a, dtype=float) for a in (pred_u, pred_v, obs_u, obs_v))
    ok = ~(np.isnan(pred_u) | np.isnan(pred_v))
    pred_u, pred_v, obs_u, obs_v = pred_u[ok], pred_v[ok], obs_u[ok], obs_v[ok]
    if pred_u.size == 0:
        return {"n": 0}
    pred_speed, pred_dir = speed_dir_from_uv(pred_u, pred_v)
    obs_speed, obs_dir = speed_dir_from_uv(obs_u, obs_v)
    vector_error = np.hypot(pred_u - obs_u, pred_v - obs_v)
    result = {
        "n": int(pred_u.size),
        "vector_rmse_kn": float(np.sqrt(np.mean(vector_error**2)) / KNOT_MS),
        "speed_bias_kn": float(np.mean(pred_speed - obs_speed) / KNOT_MS),
        "speed_mae_kn": float(np.mean(np.abs(pred_speed - obs_speed)) / KNOT_MS),
    }
    moving = obs_speed >= MIN_SPEED_FOR_DIRECTION_KN * KNOT_MS
    if moving.any():
        result["direction_mae_deg"] = float(np.mean(np.abs(angle_diff_deg(pred_dir[moving], obs_dir[moving]))))
    return result


def score_baseline(baseline: TidalStreamField, drifts: Sequence[Drift]) -> dict[str, float]:
    """How well the uncorrected baseline matches the drifts."""
    cols = as_arrays(drifts)
    u, v = baseline.velocity(cols["lat"], cols["lon"], cols["time"])
    return score(u, v, cols["u"], cols["v"])


def day_labels(drifts: Sequence[Drift], utc_offset_hours: float = 8.0) -> np.ndarray:
    """Local calendar day of each drift, as an integer day number. Default is Hong Kong time."""
    times = np.array([d.time for d in drifts], dtype=float)
    return np.floor((times + utc_offset_hours * 3600.0) / 86_400.0).astype(int)


def cross_validate_by_day(
    baseline: TidalStreamField,
    drifts: Sequence[Drift],
    conditions: Conditions | None = None,
    hyper: Hyper | None = None,
    utc_offset_hours: float = 8.0,
    optimise: bool | None = None,
) -> dict:
    """Leave-one-day-out validation of the venue correction.

    Each day's drifts are predicted by a model fitted to all the other days.
    Returns the baseline's metrics, the model's metrics, the skill, and how
    often the truth fell inside the model's own two-sigma error bar.
    """
    drifts = list(drifts)
    days = day_labels(drifts, utc_offset_hours)
    if np.unique(days).size < 2:
        raise ValueError("need drifts from at least two days")
    cols = as_arrays(drifts)
    pred_u = np.full(len(drifts), np.nan)
    pred_v = np.full(len(drifts), np.nan)
    sigma = np.full(len(drifts), np.nan)
    for day in np.unique(days):
        held = days == day
        train = [d for d, h in zip(drifts, held) if not h]
        try:
            model = VenueCorrection(baseline, hyper).fit(train, conditions, optimise=optimise)
        except ValueError:
            continue
        p = model.predict(cols["lat"][held], cols["lon"][held], cols["time"][held], conditions)
        pred_u[held], pred_v[held] = p.u, p.v
        # Predicting a new drift also carries the scatter no term explains.
        sigma[held] = np.sqrt(p.sigma**2 + model.hyper.nugget**2)

    base_u, base_v = baseline.velocity(cols["lat"], cols["lon"], cols["time"])
    scored = ~np.isnan(pred_u)
    base = score(base_u[scored], base_v[scored], cols["u"][scored], cols["v"][scored])
    model_score = score(pred_u[scored], pred_v[scored], cols["u"][scored], cols["v"][scored])
    result = {"baseline": base, "model": model_score, "days": int(np.unique(days).size)}
    if base.get("n"):
        result["skill"] = 1.0 - model_score["vector_rmse_kn"] / base["vector_rmse_kn"]
        # Compare each error with the model's uncertainty plus the drift's own error.
        total = np.sqrt(sigma[scored] ** 2 + cols["sigma"][scored] ** 2)
        error = np.hypot(pred_u[scored] - cols["u"][scored], pred_v[scored] - cols["v"][scored])
        # For two independent components, 86.5% of errors fall within 2 sigma.
        result["within_2_sigma"] = float(np.mean(error <= 2.0 * total))
    return result
