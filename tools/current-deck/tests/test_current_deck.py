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
    u = make_units("m/min")
    assert u.fmt(1.0) == "30.9 m/min"
    assert make_units("kt").cls(0.6) == 2


def test_end_to_end(tmp_path, capsys):
    out = tmp_path / "deck.pptx"
    rc = main([str(EX / "coach_log_2026_11_20.csv"), str(EX / "app_export_sample.csv"),
               "-o", str(out), "--land", "none", "--step", "5", "--json"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["slides"] == 1 + 3                       # GIF + hours 10, 11, 12
    assert summary["hours"] == ["2026-11-20 10:00", "2026-11-20 11:00", "2026-11-20 12:00"]
    with zipfile.ZipFile(out) as z:
        assert any(n.endswith(".gif") for n in z.namelist())
