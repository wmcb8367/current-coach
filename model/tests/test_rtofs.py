import numpy as np

from currentmodel.rtofs import RtofsCurrent, Window, field_index

B = """header
   22    'iversn' = hycom version number x10
field       time step  model day  k  dens        min              max
montg1   =   82774800  45986.000  6 34.000  -5.1615223E+01   1.8645725E+01
srfhgt   =   82774800  45986.000  0  0.000  -1.9114664E+01   1.4731049E+01
u_btrop  =   82774800  45986.000  0  0.000  -1.1315573E+00   1.4668964E+00
v_btrop  =   82774800  45986.000  0  0.000  -1.3399433E+00   1.1317792E+00
u-vel.   =   82774800  45986.000  1 17.000  -1.7983497E+00   1.9974805E+00
v-vel.   =   82774800  45986.000  1 17.000  -1.6862942E+00   2.0937371E+00
"""


def test_fields_are_found_in_the_order_the_b_file_lists_them():
    index = field_index(B)
    assert index[("u_btrop", 0)] == 2 and index[("v-vel.", 1)] == 5


def test_rtofs_current_interpolates_between_daily_snapshots():
    lat, lon = np.meshgrid([25.0, 25.1], [-80.1, -80.0], indexing="ij")
    win = Window(0, 2, 0, 2, lat, lon)
    a = (0.0, np.full((2, 2), 0.2), np.zeros((2, 2)))
    b = (86400.0, np.full((2, 2), 0.4), np.full((2, 2), 0.1))
    model = RtofsCurrent(win, [b, a], 25.0, -80.0)
    u, v = model.velocity(np.array([25.02, 25.02, 25.02]), np.array([-80.09, -80.09, -80.09]), np.array([43200.0, 0.0, 2e5]))
    assert np.allclose(u[:2], [0.3, 0.2]) and np.isclose(v[0], 0.05)
    assert np.isnan(u[2])
