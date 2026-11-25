"""Layers 2 to 4: corrections to the baseline learned from drift measurements.

The correction is a Gaussian process on the residual, measured minus baseline.
Instead of a generic kernel in space and time, the residual is written as a
sum of physically meaningful terms whose coefficients vary smoothly in space:

    residual(x, t) = A(x) b(x, t)            baseline too weak, too strong or rotated
                   + S(x) db/dt(x, t)        baseline turns early or late
                   + W(x) wind(t)            wind-driven surface flow
                   + c(x)                    steady flow the tide model lacks
                   + offset(user)            one user's drifter and technique
                   + noise

Here b is the baseline velocity. Each coefficient field is the sum of a
venue-wide constant and a local part with a spatial length scale, so the model
behaves like a simple bias correction when drifts are few and resolves local
structure as they accumulate. Because every term is linear in its
coefficients, the posterior is exact and comes with an uncertainty.

Far from any drift the local parts fall back to zero and the prediction
returns to the venue-wide correction of the baseline, with a wide error bar.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Sequence

import numpy as np
from scipy.linalg import cho_factor, cho_solve
from scipy.optimize import minimize
from scipy.spatial.distance import cdist

from .baseline import TidalStreamField
from .geo import to_xy
from .measurements import Drift, as_arrays

# The time-shift feature is db/dt multiplied by this many seconds, so a
# coefficient of 1 means the baseline is 30 minutes out.
SHIFT_UNIT_S = 1800.0
# Chi-square thresholds with two degrees of freedom (the two velocity components).
CHI2_99 = 9.21
CHI2_999 = 13.82

GROUPS = ("tide", "shift", "wind", "mean")


@dataclass(frozen=True)
class Hyper:
    """Prior scales of the correction terms. Units are noted per field."""

    length_m: float = 600.0       # spatial scale of local structure
    tide_global: float = 0.25     # fraction of baseline speed
    tide_local: float = 0.20
    shift_global: float = 0.7     # units of SHIFT_UNIT_S
    shift_local: float = 0.3
    wind_global: float = 0.015    # fraction of wind speed
    wind_local: float = 0.008
    mean_global: float = 0.05     # m/s
    mean_local: float = 0.04
    user: float = 0.03            # m/s, per-user offset
    nugget: float = 0.04          # m/s, scatter no term explains

    def scaled(self, log_factors: np.ndarray) -> "Hyper":
        names = [f.name for f in fields(self)]
        values = {n: getattr(self, n) * float(np.exp(x)) for n, x in zip(names, log_factors)}
        return replace(self, **values)


@dataclass
class Conditions:
    """Venue-wide wind time series, the input to the wind term.

    ``wind_u`` and ``wind_v`` are in m/s and point the way the wind blows
    towards, so a northerly wind of 5 m/s is (0, -5).
    """

    times: np.ndarray
    wind_u: np.ndarray
    wind_v: np.ndarray

    def wind(self, time):
        time = np.asarray(time, dtype=float)
        return (np.interp(time, self.times, self.wind_u), np.interp(time, self.times, self.wind_v))


@dataclass
class Prediction:
    u: np.ndarray            # corrected current, m/s
    v: np.ndarray
    sigma: np.ndarray        # 1-sigma uncertainty of each component, m/s
    baseline_u: np.ndarray
    baseline_v: np.ndarray


class VenueCorrection:
    """Correction of a baseline at one venue, fitted to drifts.

    Typical use::

        model = VenueCorrection(baseline).fit(drifts, conditions)
        p = model.predict(lat, lon, time, conditions)
    """

    def __init__(self, baseline: TidalStreamField, hyper: Hyper | None = None):
        self.baseline = baseline
        self.hyper = hyper or Hyper()
        self._fitted = False

    # ------------------------------------------------------------- features

    def _features(self, lat, lon, time, wind_u, wind_v) -> dict[str, np.ndarray]:
        bu, bv = self.baseline.velocity(lat, lon, time)
        ru, rv = self.baseline.rate(lat, lon, time)
        return {
            "tide": np.column_stack([bu, bv]),
            "shift": np.column_stack([ru, rv]) * SHIFT_UNIT_S,
            "wind": np.column_stack([wind_u, wind_v]),
            "mean": np.ones((np.size(bu), 1)),
        }

    def _kernel(self, fa, xa, ua, fb, xb, ub, hyper: Hyper) -> np.ndarray:
        near = np.exp(-0.5 * cdist(xa, xb, "sqeuclidean") / hyper.length_m**2)
        k = np.zeros((xa.shape[0], xb.shape[0]))
        for group in GROUPS:
            if group == "wind" and not self._use_wind:
                continue
            whole = getattr(hyper, f"{group}_global") ** 2
            local = getattr(hyper, f"{group}_local") ** 2
            k += (whole + local * near) * (fa[group] @ fb[group].T)
        if ua is not None and ub is not None:
            k += hyper.user**2 * (ua[:, None] == ub[None, :])
        return k

    def _wind_for(self, time, drift_wind_u, drift_wind_v, conditions: Conditions | None):
        """Wind per query: the drift's own reading if present, else the venue series, else calm."""
        time = np.asarray(time, dtype=float)
        if conditions is not None:
            wu, wv = conditions.wind(time)
        else:
            wu, wv = np.zeros_like(time), np.zeros_like(time)
        if drift_wind_u is not None:
            own = ~np.isnan(drift_wind_u)
            wu = np.where(own, drift_wind_u, wu)
            wv = np.where(own, drift_wind_v, wv)
        return wu, wv

    # ------------------------------------------------------------------ fit

    def fit(
        self,
        drifts: Sequence[Drift],
        conditions: Conditions | None = None,
        optimise: bool | None = None,
        robust_rounds: int = 3,
    ) -> "VenueCorrection":
        """Fit the correction to drifts.

        ``optimise`` tunes the prior scales to the data by maximising the
        marginal likelihood with the defaults as a prior. It defaults to on
        when there are at least 30 drifts; below that the defaults stand.
        ``robust_rounds`` is the number of passes that down-weight drifts the
        rest of the data contradicts.
        """
        cols = as_arrays(drifts)
        own_u = np.array([np.nan if d.wind_u is None else d.wind_u for d in drifts], dtype=float)
        own_v = np.array([np.nan if d.wind_v is None else d.wind_v for d in drifts], dtype=float)
        bu, bv = self.baseline.velocity(cols["lat"], cols["lon"], cols["time"])
        keep = ~np.isnan(bu)
        if keep.sum() < 3:
            raise ValueError("need at least three drifts inside the baseline's coverage")
        self.dropped_outside_coverage = int((~keep).sum())
        self._ids = [d.id for d, k in zip(drifts, keep) if k]

        lat, lon, time = cols["lat"][keep], cols["lon"][keep], cols["time"][keep]
        self._use_wind = conditions is not None or bool(np.any(~np.isnan(own_u)))
        wu, wv = self._wind_for(time, own_u[keep], own_v[keep], conditions)

        self._x = np.column_stack(to_xy(lat, lon, self.baseline.lat0, self.baseline.lon0))
        self._f = self._features(lat, lon, time, wu, wv)
        self._user = cols["user"][keep]
        self._y = np.column_stack([cols["u"][keep] - bu[keep], cols["v"][keep] - bv[keep]])
        self._meas_var = cols["sigma"][keep] ** 2
        self._inflate = np.ones(keep.sum())

        if optimise is None:
            optimise = keep.sum() >= 30
        # Down-weight outliers under the default scales first, so that a few
        # bad drifts cannot talk the optimiser into a large noise level.
        self._solve()
        self._reweight(robust_rounds)
        if optimise:
            self.hyper = self._optimise(self.hyper)
            self._solve()
            self._reweight(robust_rounds)
        self._fitted = True
        return self

    def _reweight(self, rounds: int) -> None:
        """Inflate the noise of drifts the other drifts contradict."""
        for _ in range(rounds):
            inflate = np.maximum(1.0, self._raw_chi2() / CHI2_99)
            if np.allclose(inflate, self._inflate, rtol=0.05):
                break
            self._inflate = inflate
            self._solve()

    def _raw_chi2(self) -> np.ndarray:
        # A drift whose noise was inflated by k has had its chi-square divided
        # by about k; multiply back to score it as an ordinary drift.
        return self.leave_one_out_chi2() * self._inflate

    def _noise(self, hyper: Hyper) -> np.ndarray:
        return (self._meas_var + hyper.nugget**2) * self._inflate

    def _train_kernel(self, hyper: Hyper) -> np.ndarray:
        k = self._kernel(self._f, self._x, self._user, self._f, self._x, self._user, hyper)
        k[np.diag_indices_from(k)] += self._noise(hyper)
        return k

    def _solve(self) -> None:
        self._chol = cho_factor(self._train_kernel(self.hyper), lower=True)
        self._alpha = cho_solve(self._chol, self._y)

    def _optimise(self, prior: Hyper) -> Hyper:
        count = len(fields(prior))
        spread = np.full(count, 1.0)
        spread[0] = 0.7  # the length scale is the least identifiable term

        def objective(log_factors):
            hyper = prior.scaled(log_factors)
            try:
                chol = cho_factor(self._train_kernel(hyper), lower=True)
            except np.linalg.LinAlgError:
                return 1e12
            alpha = cho_solve(chol, self._y)
            log_det = 2.0 * np.log(np.diag(chol[0])).sum()
            data = 0.5 * float(np.sum(self._y * alpha)) + self._y.shape[1] * 0.5 * log_det
            return data + 0.5 * float(np.sum((log_factors / spread) ** 2))

        result = minimize(
            objective,
            np.zeros(count),
            method="L-BFGS-B",
            bounds=[(-2.5, 2.5)] * count,
            options={"maxiter": 60},
        )
        return prior.scaled(result.x)

    # -------------------------------------------------------------- predict

    def predict(
        self,
        lat,
        lon,
        time,
        conditions: Conditions | None = None,
        user: str | None = None,
    ) -> Prediction:
        """Corrected current and its uncertainty at each position and time.

        With ``user`` set, that user's personal offset is included; without
        it the prediction is the shared venue model.
        """
        if not self._fitted:
            raise RuntimeError("fit the model first")
        lat, lon, time = np.broadcast_arrays(
            np.asarray(lat, dtype=float), np.asarray(lon, dtype=float), np.asarray(time, dtype=float)
        )
        shape = lat.shape
        lat, lon, time = lat.ravel(), lon.ravel(), time.ravel()
        bu, bv = self.baseline.velocity(lat, lon, time)
        inside = ~np.isnan(bu)

        u = np.full(lat.size, np.nan)
        v = np.full(lat.size, np.nan)
        sigma = np.full(lat.size, np.nan)
        if inside.any():
            wu, wv = self._wind_for(time[inside], None, None, conditions)
            x = np.column_stack(to_xy(lat[inside], lon[inside], self.baseline.lat0, self.baseline.lon0))
            f = self._features(lat[inside], lon[inside], time[inside], wu, wv)
            users = None if user is None else np.full(x.shape[0], user, dtype=object)
            train_users = None if user is None else self._user
            k_star = self._kernel(f, x, users, self._f, self._x, train_users, self.hyper)
            mean = k_star @ self._alpha
            prior_var = self._prior_variance(f, user is not None)
            explained = np.einsum("ij,ji->i", k_star, cho_solve(self._chol, k_star.T))
            u[inside] = bu[inside] + mean[:, 0]
            v[inside] = bv[inside] + mean[:, 1]
            sigma[inside] = np.sqrt(np.maximum(prior_var - explained, 0.0))
        return Prediction(
            u.reshape(shape), v.reshape(shape), sigma.reshape(shape), bu.reshape(shape), bv.reshape(shape)
        )

    def _prior_variance(self, f: dict[str, np.ndarray], with_user: bool) -> np.ndarray:
        var = np.zeros(f["mean"].shape[0])
        for group in GROUPS:
            if group == "wind" and not self._use_wind:
                continue
            scale = getattr(self.hyper, f"{group}_global") ** 2 + getattr(self.hyper, f"{group}_local") ** 2
            var += scale * np.sum(f[group] ** 2, axis=1)
        if with_user:
            var += self.hyper.user**2
        return var

    # ---------------------------------------------------------- diagnostics

    def leave_one_out_chi2(self) -> np.ndarray:
        """How surprising each drift is given all the others.

        For each drift this is the squared distance between its measured
        residual and the model's prediction of it from every other drift,
        scaled by the predicted variance. Under the model it follows a
        chi-square distribution with two degrees of freedom, so values above
        about 13.8 occur by chance once in a thousand drifts.
        """
        inverse_diag = np.diag(cho_solve(self._chol, np.eye(self._y.shape[0])))
        loo_residual = self._alpha / inverse_diag[:, None]
        return np.sum(loo_residual**2, axis=1) * inverse_diag

    def suspect_drifts(self, threshold: float = CHI2_999) -> list[tuple[str, float]]:
        """Drift ids the rest of the data contradicts, most suspicious first."""
        chi2 = self._raw_chi2()
        order = np.argsort(-chi2)
        return [(self._ids[i], float(chi2[i])) for i in order if chi2[i] > threshold]

    def _term(self, group: str, lat=None, lon=None) -> np.ndarray:
        """Posterior mean of one term, evaluated for the conditions of every fitted drift.

        Without a position each drift is evaluated where it was taken. With
        one, the coefficients at that position are applied to the tide, wind
        and so on that the drifts saw, which describes the correction there
        across the conditions sampled so far. Shape (drifts, 2).
        """
        if lat is None:
            near = np.exp(-0.5 * cdist(self._x, self._x, "sqeuclidean") / self.hyper.length_m**2)
        else:
            x = np.column_stack(to_xy([lat], [lon], self.baseline.lat0, self.baseline.lon0))
            near = np.exp(-0.5 * cdist(x, self._x, "sqeuclidean") / self.hyper.length_m**2)
        whole = getattr(self.hyper, f"{group}_global") ** 2
        local = getattr(self.hyper, f"{group}_local") ** 2
        f = self._f[group]
        return ((whole + local * near) * (f @ f.T)) @ self._alpha

    def venue_summary(self, lat=None, lon=None) -> dict[str, float]:
        """The correction in plain quantities, venue-wide or at one position.

        Each quantity is measured along the flow that actually occurred in
        the fitted drifts, because a stream that runs back and forth along a
        channel says nothing about the correction across it.

        ``speed_scale`` above 1 means the baseline is too weak.
        ``rotation_deg`` is how far the true stream is rotated clockwise from
        the baseline. ``baseline_late_min`` above 0 means the baseline turns
        late. ``mean_u`` and ``mean_v`` are the steady flow in m/s the
        baseline lacks. ``wind_factor`` is the downwind surface current as a
        fraction of wind speed.
        """
        if not self._fitted:
            raise RuntimeError("fit the model first")
        tide = self._f["tide"]
        corrected = tide + self._term("tide", lat, lon)
        weight = np.sum(tide**2, axis=1)
        cross = tide[:, 0] * corrected[:, 1] - tide[:, 1] * corrected[:, 0]
        dot = np.sum(tide * corrected, axis=1)
        scale = float(np.sqrt(np.sum(corrected**2) / np.sum(weight)))
        # atan2 of the summed cross and dot products is the mean turn from
        # baseline to corrected, weighted by speed. Counter-clockwise is positive
        # with east and north axes, so negate for a compass (clockwise) angle.
        rotation = float(-np.degrees(np.arctan2(cross.sum(), dot.sum())))

        rate = self._f["shift"]
        shift = self._term("shift", lat, lon)
        late_s = SHIFT_UNIT_S * float(np.sum(shift * rate) / np.sum(rate**2)) / scale
        mean = self._term("mean", lat, lon).mean(axis=0)
        summary = {
            "speed_scale": scale,
            "rotation_deg": rotation,
            "baseline_late_min": late_s / 60.0,
            "mean_u": float(mean[0]),
            "mean_v": float(mean[1]),
        }
        if self._use_wind:
            wind = self._f["wind"]
            summary["wind_factor"] = float(np.sum(self._term("wind", lat, lon) * wind) / np.sum(wind**2))
        return summary
