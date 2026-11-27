import numpy as np

from currentmodel.baseline import TidalStreamField
from currentmodel.corrections import StationCorrected, fit_station_corrections

HOUR = 3600.0
M2 = 2 * np.pi / (12.4206 * HOUR)


class Channel:
    """An east-west tidal stream everywhere, 0.5 m/s, peaking at time 0."""

    def velocity(self, lat, lon, time):
        lat, lon, time = np.broadcast_arrays(*(np.asarray(a, dtype=float) for a in (lat, lon, time)))
        return 0.5 * np.cos(M2 * time), np.zeros(lat.shape)

    def level(self, lat, lon, time):
        return np.zeros(np.broadcast(lat, lon, time).shape)


def reference(gain=2.0, late_s=1800.0):
    t = np.arange(0, 3 * 86400, 1800.0)
    u = gain * 0.5 * np.cos(M2 * (t - late_s))
    return TidalStreamField(np.array([25.76]), np.array([-80.13]), t, u[:, None], np.zeros((t.size, 1)))


def test_fit_finds_the_gain_and_how_early_the_model_is():
    (c,) = fit_station_corrections(Channel(), reference(), 6 * HOUR, 2.5 * 86400, ["cut"])
    assert abs(c.gain - 2.0) < 0.05
    assert abs(c.shift_s - 1800.0) <= 600.0
    assert c.name == "cut"


def test_correction_applies_at_the_station_and_fades_away_from_it():
    fits = fit_station_corrections(Channel(), reference(), 6 * HOUR, 2.5 * 86400)
    model = StationCorrected(Channel(), fits, radius_m=500.0)
    t = np.arange(12 * HOUR, 36 * HOUR, 600.0)
    at, _ = model.velocity(25.76, -80.13, t)
    want = reference().u[:, 0]
    assert np.max(np.abs(at - np.interp(t, reference().times, want))) < 0.15
    far, _ = model.velocity(25.80, -80.13, t)  # 4.4 km north
    assert np.allclose(far, Channel().velocity(25.80, -80.13, t)[0], atol=1e-6)
    gain, shift = model.gain_and_shift(np.array([25.76, 25.7645]), np.array([-80.13, -80.13]))
    assert gain[0] > gain[1] > 1.0 and shift[0] > shift[1] > 0.0
