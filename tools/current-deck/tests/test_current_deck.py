"""Run with:  python -m pytest tests  (from tools/current-deck)."""

import json
import zipfile
from pathlib import Path

import pytest

from current_deck.cli import main
from current_deck.loader import load_csv
from current_deck.render import fade_alpha, make_units

EX = Path(__file__).resolve().parent.parent / "examples"


def test_app_export_schema():
    ms, rep, zone = load_csv(EX / "app_export_sample.csv")
    assert zone == "Asia/Hong_Kong"
    assert rep.time_column == "time_hong_kong"
    assert rep.skipped == {"flagged invalid": 1}
    assert [m.t.strftime("%H:%M") for m in ms] == ["10:05", "10:20", "10:40", "11:25"]
    assert ms[0].toward_deg == pytest.approx(280.0)


def test_coach_log_schema_from_to_toward_and_date_from_filename():
    ms, rep, zone = load_csv(EX / "coach_log_2026_11_20.csv")
    assert zone == "Asia/Hong_Kong"
    assert rep.skipped == {"no current reading": 1}
    assert len(ms) == 5
    first = ms[0]
    assert first.t.isoformat().startswith("2026-11-20T10:52:00+08:00")
    assert first.toward_deg == pytest.approx(230.0)          # from 50 -> toward 230
    assert first.note == "Race 1 pre-start · Start line"
    assert first.wind_from == 90 and first.wind_kt == 12


def test_explicit_mapping_and_units(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("when,y,x,v_ms,set\n2026-11-20T10:00:00+08:00,22.29,114.33,0.5,90\n")
    ms, rep, _ = load_csv(p, venue_tz="Asia/Hong_Kong",
                          colmap={"time": "when", "lat": "y", "lon": "x", "speed": "v_ms"})
    assert ms[0].speed_kt == pytest.approx(0.5 * 3600 / 1852)
    assert ms[0].toward_deg == 90


def test_fade():
    assert fade_alpha(-1, 60, 20) == 0
    assert fade_alpha(10, 60, 20) == 1
    assert fade_alpha(50, 60, 20) == pytest.approx(0.5)
    assert fade_alpha(60, 60, 20) == 0


def test_units():
    ramp = ("#000000", "#FFFFFF")
    u = make_units("m/min", ramp, 0.78)
    assert u.fmt(1.0) == "30.9 m/min"
    assert u.vmax_kt * u.factor == 25                      # 24.1 m/min rounds up to 25
    k = make_units("kt", ramp, 0.78)
    assert k.vmax_kt == 1.0
    assert k.color(0) == "#000000" and k.color(2.0) == "#ffffff"
    assert k.color(0.5) == "#808080"


def test_weak_arrows_keep_a_tail():
    from current_deck.render import BASE_ARROW_IN, HEAD_LEN_IN
    assert BASE_ARROW_IN > HEAD_LEN_IN


def test_end_to_end(tmp_path, capsys):
    out = tmp_path / "deck.pptx"
    rc = main([str(EX / "coach_log_2026_11_20.csv"), str(EX / "app_export_sample.csv"),
               "-o", str(out), "--land", "none", "--step", "5", "--json"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["slides"] == 2 + 3                       # scrubber + day average + hours 10, 11, 12
    assert summary["hours"] == ["2026-11-20 10:00", "2026-11-20 11:00", "2026-11-20 12:00"]
    with zipfile.ZipFile(out) as z:
        assert any(n.endswith(".gif") for n in z.namelist())


def test_explicit_from_mapping_beats_autodetected_toward(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("time_hkt,lat,lon,current_kt,direction_toward_deg,corrected_from\n"
                 "2026-11-20T10:00:00+08:00,22.29,114.33,0.5,90,180\n")
    ms, _, _ = load_csv(p, colmap={"from": "corrected_from"})
    assert ms[0].toward_deg == pytest.approx(0.0)


def test_separate_date_and_time_columns(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("date,time,lat,lon,current_kt,current_from_deg\n"
                 "2026-11-20,10:15,22.29,114.33,0.5,90\n2026-11-21,14:40,22.29,114.33,0.4,80\n")
    ms, rep, _ = load_csv(p, venue_tz="Asia/Hong_Kong")
    assert rep.time_column == "time"
    assert [m.t.strftime("%Y-%m-%d %H:%M") for m in ms] == ["2026-11-20 10:15", "2026-11-21 14:40"]


def test_particle_field_is_bounded_and_blends():
    import numpy as np
    from current_deck.particles import field_at
    # two readings 1 nm apart: 0.4 kt toward 350 and 0.8 kt toward 010; reach 0.6 nm
    ang = np.radians([350.0, 10.0])
    args = (np.array([0.0, 1.0]), np.array([0.0, 0.0]), np.sin(ang), np.cos(ang),
            np.array([0.4, 0.8]), np.array([1.0, 1.0]), 0.6)
    pts = np.array([[0.5, 0.0], [-0.3, 0.0], [3.0, 0.0], [0.0, 0.59]])
    u, v, spd, cov = field_at(pts, *args)
    assert spd[0] == pytest.approx(0.6) and u[0] == pytest.approx(0.0, abs=1e-9) and v[0] == pytest.approx(1.0)
    assert spd[1] == pytest.approx(0.4)                      # only the first reading reaches here
    assert cov[2] == 0 and spd[2] == 0                       # beyond every reach: nothing shown
    assert 0 < cov[3] < 0.01                                 # fades out at the edge of the reach
    # a faded-out reading contributes nothing
    u, v, spd, cov = field_at(pts[:1], *args[:5], np.array([1.0, 0.0]), 0.6)
    assert spd[0] == pytest.approx(0.4)


def test_clip_ring():
    from current_deck.land import clip_ring
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    c = clip_ring(sq, (5, 5, 20, 20))
    assert c[:, 0].min() == 5 and c[:, 0].max() == 10 and c[:, 1].min() == 5
    assert clip_ring(sq, (20, 20, 30, 30)) is None


def test_particle_gif(tmp_path, capsys):
    out = tmp_path / "deck.pptx"
    rc = main([str(EX / "coach_log_2026_11_20.csv"), "-o", str(out), "--land", "none", "--step", "10",
               "--gif-style", "particles", "--reach", "800", "--json"])
    assert rc == 0
    info = json.loads(capsys.readouterr().out)["gif_info"]
    assert info["style"] == "particles" and info["reach_m"] == 800


def test_parse_window():
    import math
    from current_deck.render import fmt_window, parse_window
    assert parse_window("90") == 90 and parse_window("90m") == 90 and parse_window("3h") == 180
    assert parse_window("1h30m") == 90 and math.isinf(parse_window("all"))
    assert fade_alpha(600, math.inf, 20) == 1 and fade_alpha(-1, math.inf, 20) == 0
    assert fmt_window(180) == "3 h" and fmt_window(math.inf) == "all day"
    with pytest.raises(ValueError):
        parse_window("soon")


def test_lifetime_all_and_no_average(tmp_path, capsys):
    out = tmp_path / "deck.pptx"
    rc = main([str(EX / "coach_log_2026_11_20.csv"), "-o", str(out), "--land", "none", "--step", "10",
               "--lifetime", "all", "--no-day-average", "--json"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["slides"] == 1 + 3 and summary["gif_info"]["lifetime_min"] is None


def test_parse_distance():
    from current_deck.render import parse_distance
    assert parse_distance("800") == 800 and parse_distance("1.5km") == 1500
    assert parse_distance("0.5nm") == pytest.approx(926)
    with pytest.raises(ValueError):
        parse_distance("far")
