"""A suite of current models for one venue, scored against the same drifts.

No single model is best everywhere. A national tide service is hard to beat
inside its coverage; a global model is all there is on a coast nobody has
modelled; a physics model of the venue resolves channels the global one
cannot; a tide-only model is steadier than one that also tries to follow the
weather and gets it wrong. So every venue holds several models behind one
interface, each labelled with the factors it includes, and the drifts decide
between them.

A current model is anything with:

* ``velocity(lat, lon, time)`` returning (u, v) in m/s, NaN where it has no
  answer, with u towards east, v towards north and time in epoch seconds,
* ``rate(lat, lon, time)`` returning the time derivative of that, and
* ``lat0`` and ``lon0``, a reference position near the venue.

:class:`~currentmodel.baseline.TidalStreamField` (official predictions, or
the global model's nodes), :class:`~currentmodel.fields.GriddedField` (a
physics run) and :class:`~currentmodel.fields.HarmonicField` (a physics tide
model) all qualify, and so does a :class:`Blend` of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence

import numpy as np
from scipy.optimize import nnls

from .correction import Conditions, Hyper, VenueCorrection
from .measurements import Drift, as_arrays
from .scoring import cross_validate_by_day, day_labels, score


class CurrentModel(Protocol):
    lat0: float
    lon0: float

    def velocity(self, lat, lon, time): ...

    def rate(self, lat, lon, time): ...


@dataclass
class FunctionModel:
    """Wrap a plain function ``f(lat, lon, time) -> (u, v)`` as a current model."""

    function: Callable
    lat0: float
    lon0: float

    def velocity(self, lat, lon, time):
        return self.function(lat, lon, time)

    def rate(self, lat, lon, time, half_window_s: float = 900.0):
        time = np.asarray(time, dtype=float)
        u0, v0 = self.function(lat, lon, time - half_window_s)
        u1, v1 = self.function(lat, lon, time + half_window_s)
        return (u1 - u0) / (2 * half_window_s), (v1 - v0) / (2 * half_window_s)


class Blend:
    """A weighted sum of models. It has an answer only where every member has one."""

    def __init__(self, models: Sequence[CurrentModel], weights: Sequence[float], names: Sequence[str] | None = None):
        if len(models) != len(weights) or not models:
            raise ValueError("need one weight per model")
        self.models, self.weights = list(models), np.asarray(weights, dtype=float)
        self.names = list(names) if names else [f"model {i}" for i in range(len(models))]
        self.lat0, self.lon0 = models[0].lat0, models[0].lon0

    @classmethod
    def fit(cls, models: Sequence[CurrentModel], drifts: Sequence[Drift], names: Sequence[str] | None = None) -> "Blend":
        """Weights, none negative, that best reproduce the drifts.

        The weights need not sum to one, so a blend can also rescale: a model
        whose pattern is right and whose strength is 20% low gets 1.2.
        """
        cols = as_arrays(drifts)
        columns = []
        for model in models:
            u, v = model.velocity(cols["lat"], cols["lon"], cols["time"])
            columns.append(np.concatenate([u, v]))
        design = np.column_stack(columns)
        target = np.concatenate([cols["u"], cols["v"]])
        ok = ~np.isnan(design).any(axis=1)
        if ok.sum() < 2 * len(models):
            raise ValueError("too few drifts inside every model to fit a blend")
        weights, _ = nnls(design[ok], target[ok])
        return cls(models, weights, names)

    def _combine(self, parts):
        u = sum(w * p[0] for w, p in zip(self.weights, parts))
        v = sum(w * p[1] for w, p in zip(self.weights, parts))
        return u, v

    def velocity(self, lat, lon, time):
        return self._combine([m.velocity(lat, lon, time) for m in self.models])

    def rate(self, lat, lon, time):
        return self._combine([m.rate(lat, lon, time) for m in self.models])


@dataclass
class Entry:
    name: str
    model: CurrentModel
    factors: tuple[str, ...] = ()
    description: str = ""


@dataclass
class ModelSuite:
    """The models on offer at one venue.

    ``utc_offset_hours`` sets where one sailing day ends and the next begins
    when drifts are held out by day. Left unset, it is taken from the venue's
    longitude.
    """

    utc_offset_hours: float | None = None
    entries: list[Entry] = field(default_factory=list)

    def add(self, name: str, model: CurrentModel, factors: Sequence[str] = (), description: str = "") -> None:
        if any(e.name == name for e in self.entries):
            raise ValueError(f"there is already a model called {name!r}")
        self.entries.append(Entry(name, model, tuple(factors), description))

    def __getitem__(self, name: str) -> CurrentModel:
        for entry in self.entries:
            if entry.name == name:
                return entry.model
        raise KeyError(name)

    def _offset(self) -> float:
        if self.utc_offset_hours is not None:
            return self.utc_offset_hours
        return float(np.round(self.entries[0].model.lon0 / 15.0))

    def score(
        self,
        drifts: Sequence[Drift],
        conditions: Conditions | None = None,
        hyper: Hyper | None = None,
        blend: bool = True,
        optimise: bool | None = False,
    ) -> list[dict]:
        """Rank every model by how well it predicts drifts it has not seen.

        Each model is scored twice on the same drifts: as it stands
        (``raw``), and with the drift correction fitted on the other days
        (``corrected``), which needs drifts from two or more days. With
        ``blend`` a weighted mix of the models is scored as well, its weights
        also fitted on the other days only. Rows come back best first, by
        corrected error where there is one. Errors are vector errors in
        knots.

        Only drifts inside every model's coverage are used, so the numbers
        are comparable; ``n`` says how many that was.
        """
        if not self.entries:
            raise ValueError("the suite has no models")
        drifts = list(drifts)
        cols = as_arrays(drifts)
        predictions = {e.name: e.model.velocity(cols["lat"], cols["lon"], cols["time"]) for e in self.entries}
        common = np.ones(len(drifts), dtype=bool)
        for u, v in predictions.values():
            common &= ~(np.isnan(u) | np.isnan(v))
        if not common.any():
            raise ValueError("no drift lies inside every model's coverage")
        kept = [d for d, keep in zip(drifts, common) if keep]
        offset = self._offset()
        several_days = np.unique(day_labels(kept, offset)).size >= 2

        rows = []
        for entry in self.entries:
            u, v = predictions[entry.name]
            row = {
                "name": entry.name,
                "factors": list(entry.factors),
                "description": entry.description,
                "n": int(common.sum()),
                "raw": score(u[common], v[common], cols["u"][common], cols["v"][common]),
                "corrected": None,
            }
            if several_days:
                cv = cross_validate_by_day(entry.model, kept, conditions, hyper, offset, optimise)
                row["corrected"] = cv["model"]
                row["within_2_sigma"] = cv.get("within_2_sigma")
            rows.append(row)

        if blend and several_days and len(self.entries) > 1:
            models = [e.model for e in self.entries]
            names = [e.name for e in self.entries]
            cv = cross_validate_by_day(
                None, kept, conditions, hyper, offset, optimise, baseline_for=lambda train: Blend.fit(models, train, names)
            )
            weights = Blend.fit(models, kept, names).weights
            rows.append(
                {
                    "name": "blend",
                    "factors": sorted({f for e in self.entries for f in e.factors}),
                    "description": "weighted mix: " + ", ".join(f"{w:.2f} x {n}" for n, w in zip(names, weights) if w > 1e-6),
                    "n": int(common.sum()),
                    "raw": cv["baseline"],
                    "corrected": cv["model"],
                    "within_2_sigma": cv.get("within_2_sigma"),
                    "weights": dict(zip(names, weights.tolist())),
                }
            )
        rows.sort(key=lambda r: (r["corrected"] or r["raw"])["vector_rmse_kn"])
        return rows

    def fit(
        self,
        name: str,
        drifts: Sequence[Drift],
        conditions: Conditions | None = None,
        hyper: Hyper | None = None,
        optimise: bool | None = None,
    ) -> VenueCorrection:
        """The chosen model with the drift correction fitted to all drifts, ready to serve.

        ``name`` may be ``"blend"`` for the weighted mix of every model.
        """
        if name == "blend":
            base = Blend.fit([e.model for e in self.entries], drifts, [e.name for e in self.entries])
        else:
            base = self[name]
        return VenueCorrection(base, hyper).fit(drifts, conditions, optimise=optimise)


def format_scores(rows: Sequence[dict]) -> str:
    """A plain-text table of :meth:`ModelSuite.score` rows."""
    lines = [f"{'model':<24}{'factors':<34}{'as is':>8}{'with drifts':>13}", "-" * 79]
    for row in rows:
        corrected = f"{row['corrected']['vector_rmse_kn']:.2f} kn" if row["corrected"] else "n/a"
        lines.append(
            f"{row['name']:<24}{', '.join(row['factors'])[:32]:<34}{row['raw']['vector_rmse_kn']:>5.2f} kn{corrected:>13}"
        )
    return "\n".join(lines)
