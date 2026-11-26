import json

import numpy as np
import pytest

from currentmodel import publish
from currentmodel.publish import Folder, MapGrid, merge_index, merge_manifest, pack, sample, unpack


class Steady:
    """A current that is the same everywhere and grows by the hour, with a patch of land."""

    lat0, lon0 = 22.0, 114.0

    def velocity(self, lat, lon, time):
        lat = np.asarray(lat, dtype=float)
        hours = (np.asarray(time, dtype=float) % 86400.0) / 3600.0
        u = np.where(lat > 22.05, np.nan, 0.1 + 0.01 * hours)
        return u, -0.5 * u


def test_map_grid_covers_the_box_and_counts_points():
    grid = MapGrid.around(22.0, 114.0, 30_000.0, 15_000.0, step_m=300.0)
    assert (grid.rows, grid.cols) == (51, 101)
    lats, lons = grid.points()
    assert lats.shape == (51, 101)
    assert lats[0, 0] == pytest.approx(grid.south) and lats[-1, 0] == pytest.approx(grid.north)
    assert lons[0, 0] == pytest.approx(grid.west) and lons[0, -1] == pytest.approx(grid.east)
    assert (grid.north - grid.south) * 111_320.0 == pytest.approx(15_000.0)


def test_pack_and_unpack_agree_to_half_a_step():
    rng = np.random.default_rng(1)
    u = rng.normal(0.0, 0.4, (25, 12, 9))
    v = rng.normal(0.0, 0.4, (25, 12, 9))
    u[:, 3:5, 2] = np.nan
    v[:, 3:5, 2] = np.nan
    for compress in (True, False):
        data = pack(u, v, compress=compress)
        assert data[:4] == b"CCM1"
        a, b = unpack(data)
        assert np.isnan(a[:, 3:5, 2]).all() and np.isnan(b[:, 3:5, 2]).all()
        assert np.nanmax(np.abs(a - u)) <= publish.SCALE / 2 + 1e-9
        assert np.nanmax(np.abs(b - v)) <= publish.SCALE / 2 + 1e-9
    assert len(pack(u, v, compress=False)) == 16 + 25 * 2 * 12 * 9 * 2


def test_pack_keeps_extreme_values_in_range_and_rejects_bad_shapes():
    u = np.full((1, 2, 2), 999.0)
    a, b = unpack(pack(u, -u))
    assert a.max() == pytest.approx(32767 * publish.SCALE) and b.min() == pytest.approx(-32767 * publish.SCALE)
    with pytest.raises(ValueError):
        pack(np.zeros((2, 2)), np.zeros((2, 2)))
    with pytest.raises(ValueError):
        unpack(b"nope" + bytes(20))


def test_sample_follows_the_model_and_the_water_mask():
    grid = MapGrid(22.0, 22.1, 114.0, 114.1, 5, 4)
    times = publish.frame_times("2026-11-26")
    assert times.size == 25 and times[-1] - times[0] == 86400.0
    u, v = sample(Steady(), grid, times)
    assert u.shape == (25, 5, 4)
    assert u[3, 0, 0] == pytest.approx(0.13) and v[3, 0, 0] == pytest.approx(-0.065)
    assert np.isnan(u[:, 3:, :]).all() and np.isfinite(u[:, :3, :]).all()
    water = np.zeros((5, 4), dtype=bool)
    water[0, :] = True
    u, _ = sample(Steady(), grid, times, water)
    assert np.isfinite(u[:, 0, :]).all() and np.isnan(u[:, 1:, :]).all()


def test_the_coastline_packs_into_the_manifest_and_back(monkeypatch):
    grid = MapGrid(22.0, 22.1, 114.0, 114.1, 5, 4)

    def fake_elevation(lat, lon, zoom, cache_dir):
        assert zoom == 12
        return np.where(lon > 114.05, 12.0, 0.0)  # land to the east

    monkeypatch.setattr(publish, "elevation", fake_elevation)
    water = publish.water_mask(grid, detail=4)
    assert water.shape == (17, 13)
    assert water[:, 0].all() and not water[:, -1].any()
    entry = publish.pack_mask(water, 4)
    assert (entry["detail"], entry["rows"], entry["cols"]) == (4, 17, 13)
    assert isinstance(entry["data"], str)
    assert (publish.unpack_mask(entry) == water).all()
    json.dumps(entry)


def _manifest(days, bytes_=10):
    return {"models": [{"id": "day-ahead", "days": [{"day": d, "kind": "forecast", "issued": 1, "bytes": bytes_} for d in days]}]}


def test_merge_manifest_keeps_old_days_replaces_same_days_and_drops_stale_ones():
    old = _manifest(["2026-11-10", "2026-11-24", "2026-11-25"])
    new = _manifest(["2026-11-25", "2026-11-26"], bytes_=20)
    merged = merge_manifest(old, new, keep_days=10, today="2026-11-25")
    days = merged["models"][0]["days"]
    assert [d["day"] for d in days] == ["2026-11-24", "2026-11-25", "2026-11-26"]
    assert [d["bytes"] for d in days] == [10, 20, 20]
    assert merge_manifest(None, new, today="2026-11-25")["models"][0]["days"] == new["models"][0]["days"]


def test_merge_index_adds_and_replaces_a_venue():
    index = merge_index(None, {"id": "hong-kong", "name": "Hong Kong"})
    index = merge_index(index, {"id": "biscayne", "name": "Biscayne Bay"})
    index = merge_index(index, {"id": "hong-kong", "name": "Hong Kong waters"})
    assert [v["id"] for v in index["venues"]] == ["biscayne", "hong-kong"]
    assert index["venues"][1]["name"] == "Hong Kong waters"


def test_issue_time_is_the_venue_evening_before_the_day():
    assert publish.issue_hour_utc(114.36) == 10          # 18:00 in Hong Kong
    assert publish.issue_hour_utc(-80.2) == 23           # about 18:00 in Miami
    assert publish.issued_for("2026-11-26", 114.36) == publish.day_start("2026-11-25") + 10 * 3600.0


class _Suite(dict):
    pass


def _fake_build(monkeypatch, calls):
    from currentmodel.correction import Conditions

    def fake_wind(lat, lon, start, end, sources, cache, issued_before, log):
        times = np.arange(start, end + 1, 3600.0)
        return Conditions(times, np.full(times.size, 5.0), np.zeros(times.size))

    def fake_suite(lat, lon, width_m, height_m, start, end, **options):
        calls.append({"start": start, "end": end, **options})
        return _Suite({"global": Steady(), "venue-tide": Steady(), "venue-weather": Steady()}), None

    monkeypatch.setattr(publish, "forecast_wind", fake_wind)
    monkeypatch.setattr(publish, "build_suite", fake_suite)
    monkeypatch.setattr(publish, "elevation", lambda lat, lon, zoom, cache_dir: np.where(lat > 22.05, 5.0, 0.0))


def test_evening_run_publishes_today_once_then_only_the_days_ahead(tmp_path, monkeypatch):
    calls = []
    _fake_build(monkeypatch, calls)
    target = Folder(tmp_path)
    now = publish.day_start("2026-11-25") + 10 * 3600.0
    manifest = publish.run(target, "hong-kong", "Hong Kong", 22.0, 114.0, 20_000.0, 20_000.0, now=now, step_m=1000.0)

    assert manifest["default"] == "day-ahead"
    assert [m["id"] for m in manifest["models"]] == ["day-ahead", "physics", "tide", "global", "wind", "wind-memory"]
    days = manifest["models"][0]["days"]
    assert [(d["day"], d["kind"]) for d in days] == [("2026-11-25", "analysis"), ("2026-11-26", "forecast"), ("2026-11-27", "forecast")]
    assert calls[0]["issued_before"] is None
    assert calls[0]["start"] == publish.day_start("2026-11-25") - 12 * 3600.0
    assert calls[0]["end"] == publish.day_start("2026-11-28")
    venue = manifest["venue"]
    assert venue["id"] == "hong-kong" and venue["rows"] == 21 and venue["cols"] == 21

    u, v = unpack((tmp_path / "hong-kong" / "physics" / "2026-11-26.bin").read_bytes())
    assert u.shape == (25, 21, 21)
    assert u[6, 0, 0] == pytest.approx(0.16, abs=publish.SCALE) and v[6, 0, 0] == pytest.approx(-0.08, abs=publish.SCALE)
    assert np.isnan(u[:, -1, :]).all()  # the land of the physics model masks every model
    wind_u, _ = unpack((tmp_path / "hong-kong" / "wind" / "2026-11-26.bin").read_bytes())
    assert np.isnan(wind_u[:, -1, :]).all() and np.isfinite(wind_u[:, 0, :]).all()
    both, _ = unpack((tmp_path / "hong-kong" / "day-ahead" / "2026-11-26.bin").read_bytes())
    assert both[6, 0, 0] == pytest.approx(u[6, 0, 0] + wind_u[6, 0, 0], abs=1.5 * publish.SCALE)
    listed = json.loads((tmp_path / "index.json").read_text())["venues"][0]
    assert listed["id"] == "hong-kong" and "mask" not in listed
    coast = publish.unpack_mask(venue["mask"])
    assert coast.shape == (81, 81) and coast[0].all() and not coast[-1].any()

    later = publish.run(target, "hong-kong", "Hong Kong", 22.0, 114.0, 20_000.0, 20_000.0, now=now + 86400.0, step_m=1000.0)
    days = later["models"][0]["days"]
    assert [(d["day"], d["kind"]) for d in days] == [
        ("2026-11-25", "analysis"), ("2026-11-26", "forecast"), ("2026-11-27", "forecast"), ("2026-11-28", "forecast"),
    ]
    assert days[2]["issued"] == int(now + 86400.0)  # the newer forecast for the 27th replaced the older one
    assert days[1]["issued"] == int(now)            # the 26th keeps the forecast made before it began


def test_no_published_map_sends_water_through_the_coast(tmp_path, monkeypatch):
    _fake_build(monkeypatch, [])
    now = publish.day_start("2026-11-25") + 10 * 3600.0
    publish.run(Folder(tmp_path), "hong-kong", "Hong Kong", 22.0, 114.0, 6_000.0, 12_000.0, now=now, step_m=300.0)
    # The land lies north of 22.05; the models set south of east at every point.
    for model in ("day-ahead", "physics", "tide", "global"):
        u, v = unpack((tmp_path / "hong-kong" / model / "2026-11-26.bin").read_bytes())
        wet = np.flatnonzero(np.isfinite(u[6, :, 0]))
        last, open_water = wet[-1], wet[0]
        assert abs(v[6, last, 0]) < 0.75 * abs(v[6, open_water, 0]), model
        assert u[6, last, 0] == pytest.approx(u[6, open_water, 0], abs=publish.SCALE), model


def test_past_days_are_built_from_what_was_issued_the_evening_before(tmp_path, monkeypatch):
    calls = []
    _fake_build(monkeypatch, calls)
    now = publish.day_start("2026-11-25") + 10 * 3600.0
    manifest = publish.run(
        Folder(tmp_path), "hong-kong", "Hong Kong", 22.0, 114.36, 20_000.0, 10_000.0,
        days=["2026-11-23", "2026-11-24"], now=now, step_m=1000.0,
    )
    assert [c["issued_before"] for c in calls] == [
        publish.day_start("2026-11-22") + 10 * 3600.0, publish.day_start("2026-11-23") + 10 * 3600.0,
    ]
    days = manifest["models"][2]["days"]
    assert [(d["day"], d["kind"]) for d in days] == [("2026-11-23", "forecast"), ("2026-11-24", "forecast")]
    assert (tmp_path / "hong-kong" / "tide" / "2026-11-24.bin").exists()


def test_server_sends_the_token_and_treats_a_missing_file_as_none(monkeypatch):
    import io
    import urllib.error

    seen = []

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(request, timeout):
        seen.append((request.get_method(), request.full_url, request.headers.get("Authorization"), request.data))
        if request.get_method() == "GET":
            raise urllib.error.HTTPError(request.full_url, 404, "Not found", None, None)
        return Answer(b'{"ok":true}')

    monkeypatch.setattr(publish.urllib.request, "urlopen", fake_open)
    server = publish.Server("https://example.com/", "secret-token")
    assert server.get("hong-kong/manifest.json") is None
    server.put("hong-kong/tide/2026-11-26.bin", b"abc")
    assert seen[0][:3] == ("GET", "https://example.com/api/current-models/hong-kong/manifest.json", "Bearer secret-token")
    assert seen[1] == ("PUT", "https://example.com/api/current-models/hong-kong/tide/2026-11-26.bin", "Bearer secret-token", b"abc")

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", None, None)

    monkeypatch.setattr(publish.urllib.request, "urlopen", refuse)
    with pytest.raises(RuntimeError, match="401"):
        server.put("index.json", b"{}")
