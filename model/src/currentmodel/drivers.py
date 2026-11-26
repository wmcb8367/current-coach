"""Which sensors, at which lags, explain what the physics model misses.

This is the middle tier of the venue model. The physics model gives the
current from tide, coast, depths and the forecast wind; whatever it misses
shows as a residual against measurements. Here every sensor gathered for
the venue (:mod:`currentmodel.sensors`) is turned into candidate drivers,
each a simple function of the sensor's past: its value some hours ago, its
mean over the last hours or days, its change. Each candidate is then tested
for how much of the residual it explains on days it was not fitted on.

The test is built to be hard to fool, because with many sensors and lags
something always lines up by chance:

- **Whole days held out.** Readings on one day share that day's weather,
  so they are not independent. Every score is the error on days left out
  of the fit (``groups``), so a driver must work on a new day.
- **A permutation test per candidate.** The candidate's values are shuffled
  between days (keeping each day's values together) and scored again many
  times; the p-value is how often chance did as well.
- **False discovery control.** Benjamini-Hochberg q-values over all
  candidates: a q of 0.1 means about one in ten drivers that good would be
  chance.

The target can be a vector (a current residual, east and north) or a
number (a tide gauge's residual). For a vector each candidate gets a
coefficient for each component, so a driver can push the water in any
direction, not only along itself.

:func:`select` then combines the drivers that pass, one at a time, keeping
each only if it lowers the held-out error further.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import numpy as np

DEFAULT_LAGS_H = (0, 3, 6, 12, 24, 48)
DEFAULT_WINDOWS_H = (1, 6, 24, 72, 168)


@dataclass
class Candidate:
    name: str          # e.g. "ghcnh:CHM00045045/wind_u mean 72h lag 24h"
    series: str        # key in the hourly table
    kind: str          # "value" (mean over window), "change", "sum"
    window_h: float
    lag_h: float
    values: np.ndarray = field(repr=False, default=None)


def _cumulative(series: np.ndarray):
    good = np.isfinite(series)
    filled = np.where(good, series, 0.0)
    return np.concatenate([[0.0], np.cumsum(filled)]), np.concatenate([[0], np.cumsum(good)])


def window_values(table_times: np.ndarray, series: np.ndarray, at: np.ndarray, window_h: float, lag_h: float, kind: str = "value") -> np.ndarray:
    """A trailing-window statistic of an hourly series at times ``at``.

    ``value``: mean over the ``window_h`` hours ending ``lag_h`` hours
    before each time. ``sum``: the total over that window (rain).
    ``change``: the value at the end of the window minus the value at its
    start (a tendency). Windows with under half their hours present give NaN.
    """
    step = table_times[1] - table_times[0]
    sums, counts = _cumulative(series)
    end = np.floor((np.asarray(at) - lag_h * 3600.0 - table_times[0]) / step).astype(int) + 1
    n = max(1, int(round(window_h * 3600.0 / step)))
    start = end - n
    ok = (start >= 0) & (end <= series.size)
    out = np.full(np.shape(at), np.nan)
    s, e = np.clip(start, 0, series.size), np.clip(end, 0, series.size)
    have = counts[e] - counts[s]
    total = sums[e] - sums[s]
    enough = ok & (have >= 0.5 * n)
    if kind == "value":
        out[enough] = total[enough] / have[enough]
    elif kind == "sum":
        out[enough] = total[enough] * n / have[enough]
    elif kind == "change":
        last = np.where(ok, series[np.clip(e - 1, 0, series.size - 1)], np.nan)
        first = np.where(ok, series[np.clip(s, 0, series.size - 1)], np.nan)
        out = last - first
    else:
        raise ValueError(kind)
    return out


def candidates(
    table: Mapping[str, np.ndarray],
    at: np.ndarray,
    lags_h: Sequence[float] = DEFAULT_LAGS_H,
    windows_h: Sequence[float] = DEFAULT_WINDOWS_H,
    min_lag_h: float = 0.0,
    series: Iterable[str] | None = None,
    min_coverage: float = 0.8,
) -> list[Candidate]:
    """Every (sensor, statistic, window, lag) combination, evaluated at ``at``.

    ``min_lag_h`` keeps only what is known that many hours before each
    target time: 24 for a prediction made the evening before.
    Candidates missing at more than ``1 - min_coverage`` of the targets are dropped.
    """
    times = table["times"]
    keys = [k for k in (series or table) if k not in ("times", "failed")]
    out = []
    for key in keys:
        values = np.asarray(table[key], dtype=float)
        rainy = key.endswith("/rain")
        daily = key.endswith("/river") or key.endswith("/rain_daily")
        for window in windows_h:
            if daily and window < 24:
                continue
            for lag in lags_h:
                if lag < min_lag_h:
                    continue
                kinds = ("sum",) if rainy else (("value",) if daily else ("value", "change"))
                for kind in kinds:
                    if kind == "change" and window < 3:
                        continue
                    x = window_values(times, values, at, window, lag, kind)
                    if np.isfinite(x).mean() < min_coverage or np.nanstd(x) == 0:
                        continue
                    label = {"value": "mean", "sum": "total", "change": "change"}[kind]
                    out.append(Candidate(f"{key} {label} {window:g}h lag {lag:g}h", key, kind, window, lag, x))
    return out


def _as_matrix(target) -> np.ndarray:
    y = np.asarray(target, dtype=float)
    return y[:, None] if y.ndim == 1 else y


def cv_error(X: np.ndarray, Y: np.ndarray, groups: np.ndarray, ridge: float = 1e-6, intercept: bool = True) -> float:
    """Root-mean-square (vector) error of a linear fit on held-out groups.

    Each group is predicted from a fit to all the other groups. The fits
    share one pass over the data: every group's sums are taken once and the
    held-out fit subtracts its own, so this costs little more than one fit.
    """
    Y = _as_matrix(Y)
    X = np.asarray(X, dtype=float).reshape(len(Y), -1)
    if intercept:
        X = np.column_stack([np.ones(len(Y)), X])
    if X.shape[1] == 0:
        return float(np.sqrt(np.mean(np.sum(Y**2, axis=1))))
    scale = X.std(axis=0)
    scale[scale == 0] = 1.0
    if intercept:
        scale[0] = 1.0
    X = X / scale
    labels, index = np.unique(groups, return_inverse=True)
    k = X.shape[1]
    G = np.zeros((len(labels), k, k))
    C = np.zeros((len(labels), k, Y.shape[1]))
    np.add.at(G, index, X[:, :, None] * X[:, None, :])
    np.add.at(C, index, X[:, :, None] * Y[:, None, :])
    Gt, Ct = G.sum(axis=0), C.sum(axis=0)
    counts = np.bincount(index, minlength=len(labels))
    penalty = ridge * np.eye(k)
    if intercept:
        penalty[0, 0] = 0.0
    A = Gt[None] - G + penalty[None] * (len(Y) - counts)[:, None, None]
    coef = np.linalg.solve(A + 1e-12 * np.eye(k)[None], Ct[None] - C)
    predicted = np.einsum("nk,nkd->nd", X, coef[index])
    errors = Y - predicted
    return float(np.sqrt(np.mean(np.sum(errors**2, axis=1))))


def _block_shuffle(x: np.ndarray, groups: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Give each group's targets another group's values of x (resampled to the group's size)."""
    labels = np.unique(groups)
    donors = rng.permutation(labels)
    out = np.empty_like(x)
    for g, d in zip(labels, donors):
        mine = np.flatnonzero(groups == g)
        theirs = x[groups == d]
        if g == d or theirs.size == 0:
            out[mine] = x[mine][rng.permutation(mine.size)]
        else:
            out[mine] = theirs[rng.integers(0, theirs.size, mine.size)] if theirs.size != mine.size else theirs
    return out


def benjamini_hochberg(p: np.ndarray) -> np.ndarray:
    p = np.asarray(p, dtype=float)
    order = np.argsort(p)
    ranked = p[order] * len(p) / (np.arange(len(p)) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty_like(q)
    out[order] = np.minimum(q, 1.0)
    return out


@dataclass
class Screened:
    candidate: Candidate
    error: float          # held-out error with this driver alone
    gain: float           # baseline error minus error (positive is better)
    p: float
    q: float = 1.0
    n: int = 0


def screen(
    target,
    cands: Sequence[Candidate],
    groups: np.ndarray,
    permutations: int = 200,
    seed: int = 0,
    base: np.ndarray | None = None,
) -> tuple[float, list[Screened]]:
    """Score each candidate alone against the target on held-out groups.

    Returns the baseline held-out error (an intercept only, or ``base``
    columns if given) and the candidates ranked by gain, with permutation
    p-values and Benjamini-Hochberg q-values. Rows where a candidate is
    missing are left out of its own test, and the baseline is recomputed on
    the same rows.
    """
    Y = _as_matrix(target)
    groups = np.asarray(groups)
    base = np.zeros((len(Y), 0)) if base is None else np.asarray(base).reshape(len(Y), -1)
    rng = np.random.default_rng(seed)
    baseline = cv_error(base, Y, groups)
    rows = []
    for c in cands:
        ok = np.isfinite(c.values) & np.isfinite(Y).all(axis=1) & np.isfinite(base).all(axis=1)
        if ok.sum() < 8 or len(np.unique(groups[ok])) < 3:
            continue
        x, y, g, b = c.values[ok], Y[ok], groups[ok], base[ok]
        b0 = cv_error(b, y, g)
        e = cv_error(np.column_stack([b, x]), y, g)
        null = np.array([cv_error(np.column_stack([b, _block_shuffle(x, g, rng)]), y, g) for _ in range(permutations)])
        p = (1 + np.sum(null <= e)) / (permutations + 1)
        rows.append(Screened(c, e, b0 - e, float(p), n=int(ok.sum())))
    if rows:
        q = benjamini_hochberg(np.array([r.p for r in rows]))
        for r, qq in zip(rows, q):
            r.q = float(qq)
    rows.sort(key=lambda r: -r.gain)
    return baseline, rows


def select(target, cands: Sequence[Candidate], groups: np.ndarray, max_terms: int = 4, min_gain: float = 0.0, base=None) -> tuple[list[Candidate], list[float]]:
    """Forward selection on held-out error: add the candidate that helps most, while it helps by more than ``min_gain``."""
    Y = _as_matrix(target)
    groups = np.asarray(groups)
    base = np.zeros((len(Y), 0)) if base is None else np.asarray(base).reshape(len(Y), -1)
    ok = np.isfinite(Y).all(axis=1)
    chosen: list[Candidate] = []
    errors = [cv_error(base[ok], Y[ok], groups[ok])]
    pool = list(cands)
    while pool and len(chosen) < max_terms:
        best = None
        for c in pool:
            cols = [k.values for k in chosen] + [c.values]
            X = np.column_stack([base] + [np.asarray(v)[:, None] for v in cols])
            good = ok & np.isfinite(X).all(axis=1)
            if good.sum() < max(8, 2 * X.shape[1]):
                continue
            e = cv_error(X[good], Y[good], groups[good])
            e0 = cv_error(np.column_stack([base] + [np.asarray(k.values)[:, None] for k in chosen])[good], Y[good], groups[good])
            if best is None or e - e0 < best[0]:
                best = (e - e0, c, e)
        if best is None or -best[0] <= min_gain:
            break
        chosen.append(best[1])
        errors.append(best[2])
        pool = [c for c in pool if c.series != best[1].series or c.window_h != best[1].window_h]
    return chosen, errors


def format_screen(baseline: float, rows: Sequence[Screened], top: int = 15, unit: str = "") -> str:
    lines = [f"held-out error with no driver: {baseline:.4f}{unit}", f"{'driver':58s} {'error':>8s} {'gain':>8s} {'p':>6s} {'q':>6s} {'n':>5s}"]
    for r in rows[:top]:
        lines.append(f"{r.candidate.name[:58]:58s} {r.error:8.4f} {r.gain:8.4f} {r.p:6.3f} {r.q:6.3f} {r.n:5d}")
    return "\n".join(lines)
