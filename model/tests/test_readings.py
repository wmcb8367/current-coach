"""Readings from a spreadsheet, and the wind index reader."""

import datetime as dt

import numpy as np
import pytest

from currentmodel.geo import KNOT_MS, speed_dir_from_uv
from currentmodel.measurements import load_readings_csv
from currentmodel.wind import field_ranges


def test_app_style_table_is_read_like_the_app_record(tmp_path):
    path = tmp_path / "readings.csv"
    path.write_text(
        "time_hong_kong,speed_knots,set_toward_degrees,flow_from_degrees,latitude,longitude,confidence_percent,is_valid,duration_seconds\n"
        "2026-11-15 12:18:00 HKT,0.5,270.0,90.0,22.27,114.32,93.8,true,200\n"
        "2026-11-15 12:30:00 HKT,0.5,270.0,90.0,22.27,114.32,93.8,false,200\n"
    )
    drifts = load_readings_csv(path, user="coach")
    assert len(drifts) == 1  # the invalid row is dropped
    d = drifts[0]
    speed, to = speed_dir_from_uv(d.u, d.v)
    assert speed / KNOT_MS == pytest.approx(0.5)
    assert to == pytest.approx(270.0)
    # 12:18 in Hong Kong is 04:18 UTC, and the reading sits at the middle of the drift.
    start = dt.datetime(2026, 11, 15, 4, 18, tzinfo=dt.timezone.utc).timestamp()
    assert d.time == pytest.approx(start + 100.0)
    assert d.lon < 114.32 and d.lat == pytest.approx(22.27, abs=1e-6)
    assert d.user_id == "coach" and d.extra["confidence"] == 93.8


def test_hand_log_with_time_of_day_and_wind(tmp_path):
    path = tmp_path / "log.csv"
    path.write_text(
        "time_hkt,race,lat,lon,wind_from_deg,wind_kt,current_kt,current_from_deg\n"
        "10:45,pre-start,,,95,12,,\n"
        "10:55,start,22.278,114.323,,,0.31,47.1\n"
        "11:18,left,22.307,114.334,90,12,0.1,147.5\n"
    )
    with pytest.raises(ValueError):
        load_readings_csv(path)
    drifts = load_readings_csv(path, date="2026-11-23", utc_offset_hours=8)
    assert len(drifts) == 2  # the row without a current is skipped
    assert drifts[0].time == dt.datetime(2026, 11, 23, 2, 55, tzinfo=dt.timezone.utc).timestamp()
    assert drifts[0].lat == 22.278 and drifts[0].wind_u is None
    # An easterly wind blows towards the west.
    assert drifts[1].wind_u == pytest.approx(-12 * KNOT_MS) and drifts[1].wind_v == pytest.approx(0.0, abs=1e-9)
    _, to = speed_dir_from_uv(drifts[1].u, drifts[1].v)
    assert to == pytest.approx(327.5)


def test_wind_fields_are_found_in_a_grib_index():
    index = (
        "1:0:d=2026112300:PRMSL:mean sea level:anl:\n"
        "2:1000:d=2026112300:UGRD:10 m above ground:anl:\n"
        "3:2500:d=2026112300:VGRD:10 m above ground:anl:\n"
        "4:4000:d=2026112300:UGRD:100 m above ground:anl:\n"
    )
    assert field_ranges(index) == {"UGRD": (1000, 2500), "VGRD": (2500, 4000)}
    with pytest.raises(ValueError):
        field_ranges(index, level="80 m above ground")
