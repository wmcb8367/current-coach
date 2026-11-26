"""Soundings: reading a boat's depth logs, taking the tide out, laying them over a coarse depth source."""

import datetime as dt
import struct

import numpy as np
import pytest

from currentmodel.build import better_depths
from currentmodel.geo import to_latlon
from currentmodel.sonarlog import FOOT_M, LOWRANCE_RADIUS_M, read_csv, read_gpx, read_lowrance, read_nmea, read_sonar_log
from currentmodel.soundings import (
    Soundings,
    depth_source_from_soundings,
    read_hk_predicted_tide,
    reduce_to_mean_sea_level,
    tide_level,
)

LAT, LON = 22.27, 114.30
START = dt.datetime(2026, 11, 20, 2, 0, tzinfo=dt.timezone.utc).timestamp()


def lowrance_log(kind: int, pings, clock: float) -> bytes:
    """A sonar log as the plotters write it: an 8-byte file header, then pings with a header and some echo bytes."""
    header_size, at = (144, {"size": 28, "channel": 32, "clock": 60, "depth": 64, "east": 108, "north": 112, "elapsed": 140}) if kind == 2 else (
        168,
        {"size": 8, "channel": 12, "clock": 40, "depth": 48, "east": 92, "north": 96, "elapsed": 124},
    )
    out = bytearray(struct.pack("<HHHH", kind, 0, 1970, 0))
    for index, (lat, lon, depth_m, elapsed_s, channel) in enumerate(pings):
        echo = bytes(200 + 8 * (index % 3))
        ping = bytearray(header_size)
        struct.pack_into("<H", ping, at["size"], header_size + len(echo))
        struct.pack_into("<H", ping, at["channel"], channel)
        struct.pack_into("<I", ping, at["clock"], int(clock))
        struct.pack_into("<f", ping, at["depth"], depth_m / FOOT_M)
        struct.pack_into("<i", ping, at["east"], int(round(np.radians(lon) * LOWRANCE_RADIUS_M)))
        struct.pack_into("<i", ping, at["north"], int(round(np.log(np.tan(np.radians(lat) / 2 + np.pi / 4)) * LOWRANCE_RADIUS_M)))
        struct.pack_into("<I", ping, at["elapsed"], int(elapsed_s * 1000))
        out += ping + echo
    return bytes(out)


@pytest.mark.parametrize("kind", [2, 3])
def test_a_plotter_sonar_log_gives_one_sounding_per_moment(kind):
    pings = []
    for second in range(20):
        for channel in (0, 2):  # the plotter writes each moment once per sonar channel
            pings.append((LAT + 1e-5 * second, LON + 2e-5 * second, 12.0 + 0.1 * second, float(second), channel))
    pings.append((LAT, LON, 0.0, 20.0, 0))  # no bottom found
    found = read_lowrance(lowrance_log(kind, pings, START))
    assert len(found) == 20
    assert np.allclose(found.depth, 12.0 + 0.1 * np.arange(20), atol=1e-4)
    assert np.allclose(found.lat, LAT + 1e-5 * np.arange(20), atol=2e-6)  # the log keeps positions to about 10 cm
    assert np.allclose(found.lon, LON + 2e-5 * np.arange(20), atol=2e-6)
    assert np.allclose(found.time, START + np.arange(20))


def test_a_channel_without_a_depth_does_not_hide_the_one_that_has_it():
    # The side-scan channel is written first and carries no depth; the main channel at the same moment does.
    pings = []
    for second in range(5):
        pings += [(LAT, LON, 0.0, float(second), 5), (LAT, LON, 8.0 + second, float(second), 0)]
    found = read_lowrance(lowrance_log(2, pings, START))
    assert found.depth.tolist() == pytest.approx([8.0, 9.0, 10.0, 11.0, 12.0])


def test_a_sonar_log_from_a_plotter_without_a_clock_takes_the_time_it_is_given(tmp_path):
    log = lowrance_log(2, [(LAT, LON, 9.0, 0.0, 0), (LAT, LON, 9.5, 2.0, 0)], clock=1234.0)
    assert np.isnan(read_lowrance(log).time).all()
    path = tmp_path / "Sonar0001.sl2"
    path.write_bytes(log)
    assert np.allclose(read_sonar_log(path, start_time=START).time, [START, START + 2.0])
    with pytest.raises(ValueError, match="sonar log"):
        read_lowrance(b"not a log at all")


def test_a_track_with_depths_is_read_from_gpx():
    gpx = b"""<?xml version="1.0"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
 <trk><trkseg>
  <trkpt lat="22.2701" lon="114.3002"><time>2026-11-20T02:00:00Z</time>
   <extensions><gpxtpx:TrackPointExtension><gpxtpx:depth>14.2</gpxtpx:depth></gpxtpx:TrackPointExtension></extensions></trkpt>
  <trkpt lat="22.2702" lon="114.3004"><time>2026-11-20T02:00:10Z</time></trkpt>
  <trkpt lat="22.2703" lon="114.3006"><time>2026-11-20T02:00:20Z</time>
   <extensions><gpxtpx:TrackPointExtension><gpxtpx:depth>14.6</gpxtpx:depth></gpxtpx:TrackPointExtension></extensions></trkpt>
 </trkseg></trk>
</gpx>"""
    found = read_gpx(gpx)
    assert found.depth.tolist() == [14.2, 14.6]
    assert found.lat.tolist() == [22.2701, 22.2703]
    assert found.time.tolist() == [START, START + 20.0]


def test_the_open_archive_table_is_read():
    table = (
        "UNIQUE_ID,FILE_UUID,LON,LAT,DEPTH,TIME,PLATFORM_NAME,PROVIDER\n"
        "X-1,abc,-80.1500,25.7000,6.4,2026-11-20T02:00:00Z,Somebody,Someone\n"
        "X-1,abc,-80.1501,25.7001,0.0,2026-11-20T02:00:05Z,Somebody,Someone\n"
        "X-1,abc,-80.1502,25.7002,6.6,2026-11-20T02:00:10.000Z,Somebody,Someone\n"
    )
    found = read_csv(table)
    assert found.depth.tolist() == [6.4, 6.6]
    assert found.lon.tolist() == [-80.15, -80.1502]
    assert found.time.tolist() == [START, START + 10.0]
    with pytest.raises(ValueError, match="latitude, longitude and depth"):
        read_csv("a,b\n1,2\n")


def sentence(body: str) -> str:
    total = 0
    for letter in body:
        total ^= ord(letter)
    return f"${body}*{total:02X}"


def test_instrument_sentences_pair_each_depth_with_the_last_position():
    lines = [
        sentence("GPRMC,020000,A,2216.200,N,11418.000,E,5.0,90.0,201126,,"),
        sentence("SDDPT,11.5,0.4"),
        sentence("GPGGA,020010,2216.210,N,11418.020,E,1,08,1.0,0.0,M,,M,,"),
        sentence("SDDBT,39.37,f,12.0,M,6.56,F"),
        "$SDDBT,39.37,f,99.0,M,6.56,F*00",  # damaged in the log: the check does not match
    ]
    found = read_nmea("\n".join(lines))
    assert np.allclose(found.depth, [11.9, 12.0])
    assert np.allclose(found.lat, [22.27, 22.27 + 0.01 / 60])
    assert np.allclose(found.lon, [114.30, 114.30 + 0.02 / 60])
    assert found.time.tolist() == [START, START + 10.0]


def test_the_tide_and_the_transducer_come_out_of_a_sounding():
    hours = START + 3600.0 * np.arange(-2, 3)
    level = tide_level(hours, [1.0, 1.5, 2.0, 2.5, 3.0], mean_m=1.4)  # gauge heights above chart datum
    raw = Soundings([LAT, LAT, LAT], [LON, LON, LON], [10.0, 10.0, 10.0], [START, START + 1800.0, START + 9 * 3600.0])
    reduced = reduce_to_mean_sea_level(raw, level, transducer_m=0.3)
    # At START the water stood 0.6 m above mean sea level, half an hour later 0.85 m; the last is outside the series.
    assert np.allclose(reduced.depth, [10.0 + 0.3 - 0.6, 10.0 + 0.3 - 0.85])
    assert len(reduce_to_mean_sea_level(Soundings([LAT], [LON], [5.0]), level)) == 0  # no time, no tide


def test_the_observatory_tide_table_is_hourly_in_hong_kong_time():
    table = "﻿MM,DD,01,02,03,04,05,06,07,08,09,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24\n" + "01,01," + ",".join(f"{0.5 + 0.1 * h:.2f}" for h in range(24)) + "\n"
    times, heights = read_hk_predicted_tide(table, 2026)
    assert len(times) == 24
    assert times[0] == dt.datetime(2025, 12, 31, 17, 0, tzinfo=dt.timezone.utc).timestamp()  # 01:00 on 1 January in Hong Kong
    assert heights[0] == 0.5 and heights[-1] == pytest.approx(2.8)


def flat(depth_m: float):
    return lambda lat, lon: np.full(np.shape(lat), depth_m)


def test_depths_follow_the_soundings_and_fade_to_the_base_away_from_them():
    # A boat sounds 12 m along a 2 km line where the coarse grid says 20 m.
    along = np.linspace(-1000.0, 1000.0, 400)
    lat, lon = to_latlon(along, 0.0 * along, LAT, LON)
    source = depth_source_from_soundings(Soundings(lat, lon, np.full(400, 12.0)), flat(20.0), cell_m=50.0, reach_m=800.0)
    off = np.array([0.0, 100.0, 300.0, 500.0, 700.0, 900.0, 3000.0])
    at_lat, at_lon = to_latlon(0.0 * off, off, LAT, LON)
    depth = source(at_lat, at_lon)
    assert depth[:3] == pytest.approx(12.0, abs=0.05)  # in full out to half the reach
    assert np.all(np.diff(depth) >= -1e-9)  # then deeper and deeper towards the base
    assert 12.5 < depth[3] < depth[4] < 19.5
    assert depth[-2] == pytest.approx(20.0, abs=0.05) and depth[-1] == 20.0


def test_a_slope_the_soundings_found_is_kept_between_tracks():
    # Two tracks 400 m apart, 5 m and 9 m deeper than the grid's 10 m: the water between them is in between.
    along = np.linspace(-1000.0, 1000.0, 400)
    west, east = to_latlon(0.0 * along - 200.0, along, LAT, LON), to_latlon(0.0 * along + 200.0, along, LAT, LON)
    soundings = Soundings(np.concatenate([west[0], east[0]]), np.concatenate([west[1], east[1]]), np.concatenate([np.full(400, 15.0), np.full(400, 19.0)]))
    source = depth_source_from_soundings(soundings, flat(10.0), cell_m=50.0, reach_m=1000.0)
    middle = float(source(*to_latlon(np.array([0.0]), np.array([0.0]), LAT, LON))[0])
    assert 16.0 < middle < 18.0


def test_a_false_echo_among_good_soundings_is_thrown_out():
    rng = np.random.default_rng(3)
    x, y = rng.uniform(-500, 500, 4000), rng.uniform(-500, 500, 4000)
    lat, lon = to_latlon(x, y, LAT, LON)
    depth = np.full(4000, 15.0)
    spike_lat, spike_lon = to_latlon(np.full(5, 12.0), np.full(5, 12.0), LAT, LON)  # five pings locked on at 2 m
    soundings = Soundings(np.concatenate([lat, spike_lat]), np.concatenate([lon, spike_lon]), np.concatenate([depth, np.full(5, 2.0)]))
    source = depth_source_from_soundings(soundings, flat(15.0), cell_m=25.0, reach_m=500.0)
    grid_x, grid_y = np.meshgrid(np.linspace(-400, 400, 81), np.linspace(-400, 400, 81))
    assert source(*to_latlon(grid_x, grid_y, LAT, LON)).min() > 14.5


def test_without_a_base_there_is_no_depth_away_from_the_soundings():
    along = np.linspace(-500.0, 500.0, 200)
    lat, lon = to_latlon(along, 0.0 * along, LAT, LON)
    source = depth_source_from_soundings(Soundings(lat, lon, np.full(200, 8.0)), cell_m=50.0, reach_m=400.0)
    near, far = source(*to_latlon(np.array([0.0, 0.0]), np.array([100.0, 2000.0]), LAT, LON))
    assert near == pytest.approx(8.0, abs=0.01) and np.isnan(far)
    with pytest.raises(ValueError, match="no soundings"):
        depth_source_from_soundings(Soundings([], [], []))


def test_limits_hold_the_result():
    along = np.linspace(-500.0, 500.0, 200)
    lat, lon = to_latlon(along, 0.0 * along, LAT, LON)
    limits = lambda at_lat, at_lon: (np.full(np.shape(at_lat), 9.0), np.full(np.shape(at_lat), np.nan))
    source = depth_source_from_soundings(Soundings(lat, lon, np.full(200, 4.0)), flat(6.0), cell_m=50.0, reach_m=400.0, limits=limits)
    assert source(np.array([LAT]), np.array([LON]))[0] == 9.0


def test_depth_logs_are_reduced_and_laid_over_the_base(tmp_path):
    along = np.linspace(-500.0, 500.0, 101)
    lat, lon = to_latlon(along, 0.0 * along, LAT, LON)
    rows = ["lat,lon,depth,time"] + [f"{a:.7f},{b:.7f},9.0,{START + 10.0 * i}" for i, (a, b) in enumerate(zip(lat, lon))]
    rows.append(f"{LAT:.7f},{LON:.7f},9.0,")  # one sounding without a time
    log = tmp_path / "coach-boat.csv"
    log.write_text("\n".join(rows))
    level = tide_level([START - 3600.0, START + 7200.0], [0.8, 0.8], mean_m=0.0)  # the water stood 0.8 m above mean sea level
    said = []
    source = better_depths(flat(20.0), depth_logs=[log], level=level, transducer_m=0.5, log=said.append)
    assert source(np.array([LAT]), np.array([LON]))[0] == pytest.approx(9.0 + 0.5 - 0.8, abs=0.01)
    assert any("1 soundings left out" in line for line in said)
    assert better_depths(None) is None


def test_where_the_base_has_no_depth_the_result_has_none_away_from_the_soundings():
    # The base covers only the water west of the track; the venue's own fallback needs to see the gap to the east.
    def west_only(lat, lon):
        return np.where(np.asarray(lon) < LON, 20.0, np.nan)

    along = np.linspace(-500.0, 500.0, 200)
    tracks = [to_latlon(0.0 * along + east, along, LAT, LON) for east in (300.0, 3000.0)]  # both east of the base's edge
    soundings = Soundings(np.concatenate([t[0] for t in tracks]), np.concatenate([t[1] for t in tracks]), np.full(400, 8.0))
    source = depth_source_from_soundings(soundings, west_only, cell_m=50.0, reach_m=400.0)
    east_x = np.array([300.0, 650.0, 1650.0, 6000.0])
    on_track, fading, between_tracks, far = source(*to_latlon(east_x, 0.0 * east_x, LAT, LON))
    assert on_track == pytest.approx(8.0, abs=0.01)
    assert fading == pytest.approx(8.0, abs=0.01)  # no base to fade to, so the soundings in full
    assert np.isnan(between_tracks) and np.isnan(far)
    assert source(*to_latlon(np.array([-900.0]), np.array([0.0]), LAT, LON))[0] == 20.0  # the base, where it has one
