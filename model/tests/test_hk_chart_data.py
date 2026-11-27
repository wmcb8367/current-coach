from pathlib import Path

import numpy as np

from currentmodel.sonarlog import read_sonar_log

DATA = Path(__file__).resolve().parents[1] / "data" / "hong-kong" / "admiralty-937-soundings.csv"


def test_admiralty_937_soundings_load_as_depths_below_mean_sea_level():
    soundings = read_sonar_log(DATA)
    assert len(soundings) == 456
    assert np.all((soundings.lat > 22.2) & (soundings.lat < 22.35))
    assert np.all((soundings.lon > 114.25) & (soundings.lon < 114.40))
    # fathoms and feet, plus 1.4 m from chart datum to mean sea level
    table = np.genfromtxt(DATA, delimiter=",", names=True)
    assert np.allclose(table["depth_m"], (table["fathoms"] * 6 + table["feet"]) * 0.3048 + 1.4, atol=0.06)
    # the race area east of Clear Water Bay is 20 to 30 m deep on the chart
    race = (soundings.lat > 22.25) & (soundings.lat < 22.31) & (soundings.lon > 114.30) & (soundings.lon < 114.38)
    assert 20.0 < np.median(soundings.depth[race]) < 30.0
