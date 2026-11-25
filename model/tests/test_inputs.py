import numpy as np
import pytest

from currentmodel.baseline import TidalStreamField
from currentmodel.geo import KNOT_MS, angle_diff_deg, speed_dir_from_uv, to_latlon, to_xy, uv_from_speed_dir
from currentmodel.measurements import drift_from_app_record, drift_sigma, drifts_from_records

LAT0, LON0 = 22.28, 114.17


def test_projection_round_trip():
    lat, lon = to_latlon([0.0, 1500.0, -800.0], [0.0, -2000.0, 600.0], LAT0, LON0)
    x, y = to_xy(lat, lon, LAT0, LON0)
    assert np.allclose(x, [0.0, 1500.0, -800.0], atol=1e-6)
    assert np.allclose(y, [0.0, -2000.0, 600.0], atol=1e-6)


def test_direction_conventions():
    # A stream setting towards the east has only an east component.
    u, v = uv_from_speed_dir(1.0, 90.0)
    assert u == pytest.approx(1.0) and v == pytest.approx(0.0, abs=1e-12)
    speed, direction = speed_dir_from_uv(-1.0, 0.0)
    assert speed == pytest.approx(1.0) and direction == pytest.approx(270.0)
    assert angle_diff_deg(350.0, 10.0) == pytest.approx(-20.0)
    assert angle_diff_deg(10.0, 350.0) == pytest.approx(20.0)


def app_record(**overrides):
    record = {
        "clientId": "abc",
        "timestamp": "2026-11-23T02:00:00.000Z",
        "durationSeconds": 120.0,
        "speedMetersPerMinute": 30.0,       # 0.5 m/s
        "fromDirectionDegrees": 270.0,      # comes from the west, so flows east
        "latitude": LAT0,
        "longitude": LON0,
        "isValid": True,
    }
    record.update(overrides)
    return record


def test_app_record_becomes_velocity_at_the_middle_of_the_drift():
    drift = drift_from_app_record(app_record())
    assert drift.u == pytest.approx(0.5) and drift.v == pytest.approx(0.0, abs=1e-9)
    # 0.5 m/s for 120 s is 60 m; the mid-point is 30 m east of the start fix.
    x, y = to_xy(drift.lat, drift.lon, LAT0, LON0)
    assert x == pytest.approx(30.0, abs=0.01) and y == pytest.approx(0.0, abs=0.01)
    assert drift.time == pytest.approx(1795399200.0 + 60.0)


def test_android_records_are_anchored_at_the_end_of_the_drift():
    # The same drift as the iOS record above, as the Android app sends it:
    # the last fix (60 m east of the start) and the time the drift ended.
    lat_end, lon_end = to_latlon(60.0, 0.0, LAT0, LON0)
    record = app_record(source="current-coach-android", timestamp="2026-11-23T02:02:00.000Z",
                        latitude=float(lat_end), longitude=float(lon_end))
    drift = drift_from_app_record(record)
    ios = drift_from_app_record(app_record())
    x, y = to_xy(drift.lat, drift.lon, LAT0, LON0)
    assert x == pytest.approx(30.0, abs=0.01) and y == pytest.approx(0.0, abs=0.01)
    assert drift.time == pytest.approx(ios.time)
    assert (drift.u, drift.v) == (ios.u, ios.v)


def test_invalid_and_zero_length_records_are_dropped():
    records = [app_record(), app_record(isValid=False), app_record(durationSeconds=0)]
    assert len(drifts_from_records(records)) == 1


def test_short_drifts_are_less_certain():
    assert drift_sigma(30.0) > drift_sigma(120.0)
    assert drift_sigma(120.0, gps_sigma_m=5.0) == pytest.approx(np.sqrt(2) * 5.0 / 120.0)
    # A long drift never claims better than the floor.
    assert drift_sigma(3600.0) == pytest.approx(0.02)


def test_wind_reading_on_a_record_uses_the_from_convention():
    drift = drift_from_app_record(app_record(windSpeedMs=6.0, windFromDegrees=0.0))
    # A northerly blows towards the south.
    assert drift.wind_u == pytest.approx(0.0, abs=1e-9) and drift.wind_v == pytest.approx(-6.0)


def small_field():
    lat, lon = to_latlon([0.0, 1000.0, 0.0, 1000.0], [0.0, 0.0, 1000.0, 1000.0], LAT0, LON0)
    times = np.array([0.0, 900.0, 1800.0])
    u = np.array([[0.0, 0.0, 0.0, 0.0], [0.2, 0.4, 0.2, 0.4], [0.4, 0.8, 0.4, 0.8]])
    return TidalStreamField(lat, lon, times, u, np.zeros_like(u), max_distance_m=1500.0)


def test_baseline_interpolates_in_time_and_space():
    field = small_field()
    lat, lon = to_latlon(0.0, 0.0, LAT0, LON0)
    u, _ = field.velocity(lat, lon, 450.0)
    # Half-way between the first two steps, dominated by the point it sits on.
    assert 0.1 <= float(u) <= 0.12
    mid_lat, mid_lon = to_latlon(500.0, 500.0, LAT0, LON0)
    u, _ = field.velocity(mid_lat, mid_lon, 900.0)
    assert float(u) == pytest.approx(0.3)


def test_baseline_reports_no_value_outside_coverage():
    field = small_field()
    far_lat, far_lon = to_latlon(9000.0, 9000.0, LAT0, LON0)
    assert np.isnan(field.velocity(far_lat, far_lon, 900.0)[0])
    lat, lon = to_latlon(0.0, 0.0, LAT0, LON0)
    assert np.isnan(field.velocity(lat, lon, 5000.0)[0])


def test_baseline_rate_matches_the_slope():
    field = small_field()
    lat, lon = to_latlon(500.0, 500.0, LAT0, LON0)
    du, dv = field.rate(lat, lon, 900.0)
    assert float(du) == pytest.approx(0.3 / 900.0) and float(dv) == pytest.approx(0.0)


def test_long_csv_round_trip(tmp_path):
    path = tmp_path / "streams.csv"
    rows = ["lat,lon,time,speed_kn,dir_to_deg"]
    for lat, lon in ((22.28, 114.17), (22.29, 114.18)):
        for hour, speed in ((0, 0.5), (1, 1.0)):
            rows.append(f"{lat},{lon},2026-11-23T0{hour}:00:00Z,{speed},90")
    path.write_text("\n".join(rows))
    field = TidalStreamField.from_long_csv(path)
    u, v = field.velocity(22.28, 114.17, field.times[0] + 1800.0)
    assert float(u) == pytest.approx(0.75 * KNOT_MS, rel=1e-6)
    assert float(v) == pytest.approx(0.0, abs=1e-9)


def test_hong_kong_tidal_stream_file_is_read_in_hong_kong_time(tmp_path):
    import datetime as dt

    from currentmodel.baseline import hk_tidal_stream_url, load_hk_tidal_streams
    from currentmodel.geo import KNOT_MS

    slots = [f"{h:02d}{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]
    header = "LATITUDE,LONGITUDE,MODE,DATE," + ",".join(f"{s}-KNOT,{s}-DEG" for s in slots)
    lines = [header]
    for lat, lon, toward in ((22.27, 114.32, 90), (22.28, 114.33, 180)):
        for day in ("2026-11-01", "2026-11-02"):
            lines.append(f"{lat},{lon},S,{day}," + ",".join(f"1.00,{toward}" for _ in slots))
    path = tmp_path / "All_Points.csv"
    path.write_text("\n".join(lines) + "\n")
    field = load_hk_tidal_streams(path)
    # Midnight in Hong Kong on 1 November is 16:00 UTC the day before.
    assert field.times[0] == dt.datetime(2026, 10, 31, 16, tzinfo=dt.timezone.utc).timestamp()
    assert field.times.size == 192 and np.all(np.diff(field.times) == 900.0)
    u, v = field.velocity(22.27, 114.32, field.times[10])
    assert u == pytest.approx(KNOT_MS, rel=1e-2) and abs(v) < 0.02 * KNOT_MS
    assert hk_tidal_stream_url(2026, 11).endswith("All_Points_2026-11-01_2026-11-30_Surface.zip")
    assert hk_tidal_stream_url(2026, 2, surface=False).endswith("All_Points_2026-02-01_2026-02-28_Average.zip")
