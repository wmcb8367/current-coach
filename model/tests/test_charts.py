"""Electronic charts: reading an S-57 cell and turning its depths into a depth source.

The chart read here is written by this file, byte for byte as the standard
lays a cell out (an ISO 8211 file: a first record that describes the fields,
then one record per node, edge and chart object).
"""

import struct
import zipfile

import numpy as np
import pytest

from currentmodel.charts import Chart, _inside, chart_depth_source, read_chart, read_charts

LAT, LON = 22.25, 114.30
FIELD_END, UNIT_END = b"\x1e", b"\x1f"

DESCRIPTIONS = [
    ("0001", b"0500;&   ", "ISO 8211 Record Identifier", "", "(b12)"),
    ("DSID", b"1600;&   ", "Data set identification field", "RCNM!RCID!EXPP!INTU!DSNM!EDTN!UPDN!UADT!ISDT!STED!PRSP!PSDN!PRED!PROF!AGEN!COMT", "(b11,b14,2b11,3A,2A(8),R(4),b11,2A,b11,b12,A)"),
    ("DSSI", b"1600;&   ", "Data set structure information field", "DSTR!AALL!NALL!NOMR!NOCR!NOGR!NOLR!NOIN!NOCN!NOED!NOFA", "(3b11,8b14)"),
    ("DSPM", b"1600;&   ", "Data set parameter field", "RCNM!RCID!HDAT!VDAT!SDAT!CSCL!DUNI!HUNI!PUNI!COUN!COMF!SOMF!COMT", "(b11,b14,3b11,b14,4b11,2b14,A)"),
    ("VRID", b"1100;&   ", "Vector record identifier field", "RCNM!RCID!RVER!RUIN", "(b11,b14,b12,b11)"),
    ("VRPT", b"2600;&   ", "Vector record pointer field", "*NAME!ORNT!USAG!TOPI!MASK", "(B(40),4b11)"),
    ("SG2D", b"2200;&   ", "2-D coordinate field", "*YCOO!XCOO", "(2b24)"),
    ("SG3D", b"2200;&   ", "3-D coordinate (sounding array) field", "*YCOO!XCOO!VE3D", "(3b24)"),
    ("FRID", b"1100;&   ", "Feature record identifier field", "RCNM!RCID!PRIM!GRUP!OBJL!RVER!RUIN", "(b11,b14,2b11,2b12,b11)"),
    ("FOID", b"1100;&   ", "Feature object identifier field", "AGEN!FIDN!FIDS", "(b12,b14,b12)"),
    ("ATTF", b"2600;&-A ", "Feature record attribute field", "*ATTL!ATVL", "(b12,A)"),
    ("FSPT", b"2600;&   ", "Feature record to spatial record pointer field", "*NAME!ORNT!USAG!MASK", "(B(40),3b11)"),
]
TREE = "0001DSIDDSIDDSSI0001DSPM0001VRIDVRIDVRPTVRIDSG2DVRIDSG3D0001FRIDFRIDFOIDFRIDATTFFRIDFSPT"
NODE_ALONE, NODE, EDGE, OBJECT = 110, 120, 130, 100
NONE = 255


def record(fields, describing: bool = False) -> bytes:
    directory, area = b"", b""
    for tag, body in fields:
        directory += tag.encode() + b"%05d%05d" % (len(body), len(area))
        area += body
    base = 24 + len(directory) + 1
    leader = b"%05d" % (base + len(area)) + (b"3LE1 09" if describing else b" D     ") + b"%05d" % base + (b" ! " if describing else b"   ") + b"5504"
    return leader + directory + FIELD_END + area


def first_record() -> bytes:
    fields = [("0000", b"0000;&   " + UNIT_END + TREE.encode() + FIELD_END)]
    for tag, controls, title, names, layout in DESCRIPTIONS:
        fields.append((tag, controls + title.encode() + UNIT_END + names.encode() + UNIT_END + layout.encode() + FIELD_END))
    return record(fields, describing=True)


class Cell:
    """A chart cell being written. Positions are degrees, depths metres."""

    def __init__(self, name: str, scale: int):
        self.count = 1
        text = lambda value: value.encode() + UNIT_END
        dsid = struct.pack("<BIBB", 10, 1, 1, 5) + text(name + ".000") + text("1") + text("0") + b"20260101" + b"20260101" + b"03.1" + struct.pack("<B", 1) + text("") + text("2.0") + struct.pack("<BH", 1, 550) + text("")
        dssi = struct.pack("<BBB8I", 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0)
        dspm = struct.pack("<BIBBBIBBBBII", 20, 1, 2, 3, 23, scale, 1, 1, 1, 1, 10_000_000, 10) + text("")
        self.records = [first_record(), self._record([("DSID", dsid + FIELD_END), ("DSSI", dssi + FIELD_END)]), self._record([("DSPM", dspm + FIELD_END)])]
        self.objects = 0

    def _record(self, fields) -> bytes:
        self.count += 1
        return record([("0001", struct.pack("<H", self.count) + FIELD_END)] + fields)

    @staticmethod
    def _name(kind: int, number: int) -> bytes:
        return struct.pack("<BI", kind, number)

    @staticmethod
    def _places(points) -> bytes:
        return b"".join(struct.pack("<ii", round(lat * 1e7), round(lon * 1e7)) for lat, lon in points)

    def node(self, number: int, lat: float, lon: float) -> None:
        self.records.append(self._record([("VRID", struct.pack("<BIHB", NODE, number, 1, 1) + FIELD_END), ("SG2D", self._places([(lat, lon)]) + FIELD_END)]))

    def edge(self, number: int, start: int, end: int, between) -> None:
        ends = self._name(NODE, start) + bytes([NONE, NONE, 1, NONE]) + self._name(NODE, end) + bytes([NONE, NONE, 2, NONE])
        self.records.append(self._record([("VRID", struct.pack("<BIHB", EDGE, number, 1, 1) + FIELD_END), ("VRPT", ends + FIELD_END), ("SG2D", self._places(between) + FIELD_END)]))

    def soundings(self, number: int, spots) -> None:
        depths = b"".join(struct.pack("<iii", round(lat * 1e7), round(lon * 1e7), round(depth * 10)) for lat, lon, depth in spots)
        self.records.append(self._record([("VRID", struct.pack("<BIHB", NODE_ALONE, number, 1, 1) + FIELD_END), ("SG3D", depths + FIELD_END)]))

    def object(self, what: int, shape: int, attributes: dict, parts) -> None:
        """``parts`` are (kind, number, direction, use): direction 1 forward or 2 reversed, use 1 outer edge or 2 a hole's."""
        self.objects += 1
        frid = struct.pack("<BIBBHHB", OBJECT, self.objects, shape, 1 if what == 42 else 2, what, 1, 1)
        foid = struct.pack("<HIH", 550, self.objects, 1)
        attf = b"".join(struct.pack("<H", code) + str(value).encode() + UNIT_END for code, value in attributes.items())
        fspt = b"".join(self._name(kind, number) + bytes([direction, use, NONE]) for kind, number, direction, use in parts)
        fields = [("FRID", frid + FIELD_END), ("FOID", foid + FIELD_END)] + ([("ATTF", attf + FIELD_END)] if attf else []) + [("FSPT", fspt + FIELD_END)]
        self.records.append(self._record(fields))

    def bytes(self) -> bytes:
        return b"".join(self.records)


def square(half: float):
    """Corners of a square around the test position, from the south-west one clockwise."""
    return [(LAT - half, LON - half), (LAT + half, LON - half), (LAT + half, LON + half), (LAT - half, LON + half)]


def harbour_chart() -> bytes:
    """1:20,000. A basin 10 to 20 m deep, about 2.2 km across, with a 5 to 10 m bank in the middle inside a 10 m contour."""
    cell = Cell("HARBOUR", 20000)
    outer, bank = square(0.010), square(0.004)
    cell.node(1, *outer[0])
    cell.node(2, *bank[0])
    cell.edge(1, 1, 1, outer[1:])
    cell.edge(2, 2, 2, bank[1:])
    spots = [(LAT, LON, 7.5)] + [(LAT + a, LON + b, 15.0) for a in (-0.007, 0.007) for b in (-0.007, 0.007)]
    cell.soundings(1, spots)
    cell.object(302, 3, {18: 1}, [(EDGE, 1, 1, 1)])  # what the chart covers
    cell.object(42, 3, {87: 10, 88: 20}, [(EDGE, 1, 1, 1), (EDGE, 2, 2, 2)])  # the basin, with the bank as a hole
    cell.object(42, 3, {87: 5, 88: 10}, [(EDGE, 2, 1, 1)])  # the bank
    cell.object(43, 2, {174: 10}, [(EDGE, 2, 1, NONE)])  # the 10 m contour round the bank
    cell.object(129, 1, {}, [(NODE_ALONE, 1, NONE, NONE)])
    return cell.bytes()


def approach_chart() -> bytes:
    """1:100,000. Everything for 5 km around, 20 to 50 m, with a 45 m sounding where the harbour chart knows better."""
    cell = Cell("APPROACH", 100000)
    outer = square(0.05)
    cell.node(1, *outer[0])
    cell.edge(1, 1, 1, outer[1:])
    cell.soundings(1, [(LAT, LON, 45.0), (LAT + 0.03, LON, 30.0)])
    cell.object(302, 3, {18: 1}, [(EDGE, 1, 1, 1)])
    cell.object(42, 3, {87: 20, 88: 50}, [(EDGE, 1, 1, 1)])
    cell.object(129, 1, {}, [(NODE_ALONE, 1, NONE, NONE)])
    return cell.bytes()


def flat(depth_m: float):
    return lambda lat, lon: np.full(np.shape(lat), depth_m)


def test_a_chart_cell_gives_up_its_soundings_contours_and_depth_areas():
    chart = read_chart(harbour_chart(), "HARBOUR")
    assert (chart.scale, chart.sounding_datum) == (20000, 23)
    assert sorted(chart.soundings.depth.tolist()) == [7.5, 15.0, 15.0, 15.0, 15.0]
    assert chart.soundings.lat[0] == pytest.approx(LAT) and chart.soundings.lon[0] == pytest.approx(LON)
    (depth, lat, lon), = chart.contours
    assert depth == 10.0
    assert len(lat) == 5 and (lat[0], lon[0]) == (lat[-1], lon[-1])  # the ring closes on its node
    assert np.allclose(sorted(set(np.round(lat - LAT, 6))), [-0.004, 0.004])
    assert [(low, high, len(edge)) for low, high, edge in chart.areas] == [(10.0, 20.0, 8), (5.0, 10.0, 4)]
    assert len(chart.coverage) == 1 and len(chart.coverage[0]) == 4


def test_a_point_is_inside_a_boundary_if_it_crosses_it_an_odd_number_of_times():
    def pieces(half):
        corners = np.array([(-half, -half), (half, -half), (half, half), (-half, half), (-half, -half)])
        return np.hstack([corners[:-1], corners[1:]])  # x, y, x, y

    ring = np.concatenate([pieces(40.0), pieces(15.0)])[::-1]  # a square with a square hole, in no particular order
    inside = _inside(ring[:, [0, 2]], ring[:, [1, 3]], -50.0, -50.0, 10.0, 11, 11)
    centres = -50.0 + 10.0 * np.arange(11)
    x, y = np.meshgrid(centres, centres)
    expected = (np.maximum(np.abs(x), np.abs(y)) < 40.0) & (np.maximum(np.abs(x), np.abs(y)) > 15.0)
    edge = np.maximum(np.abs(x), np.abs(y)) == 40.0  # centres exactly on the boundary may go either way
    assert np.array_equal(inside[~edge], expected[~edge])


def test_chart_depths_become_depths_below_mean_sea_level_over_the_base():
    chart = read_chart(harbour_chart(), "HARBOUR")
    source = chart_depth_source(chart, flat(30.0), mean_sea_level_above_datum_m=1.4, cell_m=50.0, reach_m=600.0)
    lat = np.array([LAT, LAT + 0.007, LAT, LAT + 0.2])
    lon = np.array([LON, LON + 0.007, LON + 0.004, LON])
    on_bank, in_basin, on_contour, far_away = source(lat, lon)
    assert on_bank == pytest.approx(7.5 + 1.4, abs=0.05)
    assert in_basin == pytest.approx(15.0 + 1.4, abs=0.05)
    assert on_contour == pytest.approx(10.0 + 1.4, abs=0.3)
    assert far_away == 30.0
    # Everywhere in the basin the depth stays inside what its depth area allows, soundings or not.
    across = np.linspace(-0.0095, 0.0095, 60)
    grid_lat, grid_lon = np.meshgrid(LAT + across, LON + across, indexing="ij")
    depth = source(grid_lat, grid_lon)
    ring = np.maximum(np.abs(grid_lat - LAT), np.abs(grid_lon - LON))
    assert depth[ring < 0.0035].min() >= 5.0 + 1.4 - 1e-9 and depth[ring < 0.0035].max() <= 10.0 + 1.4 + 1e-9
    assert depth[ring > 0.0045].min() >= 10.0 + 1.4 - 1e-9 and depth[ring > 0.0045].max() <= 20.0 + 1.4 + 1e-9


def test_the_more_detailed_chart_wins_where_charts_overlap(tmp_path):
    with zipfile.ZipFile(tmp_path / "charts.zip", "w") as archive:  # the way NOAA hands charts out
        archive.writestr("ENC_ROOT/APPROACH/APPROACH.000", approach_chart())
        archive.writestr("ENC_ROOT/HARBOUR/HARBOUR.000", harbour_chart())
        archive.writestr("ENC_ROOT/HARBOUR/HARBOUR.001", b"an update, which is not applied")
        archive.writestr("ENC_ROOT/CATALOG.031", b"the list of what is in the archive, which is not an update")
    with pytest.warns(UserWarning, match="1 chart update"):
        charts = read_charts([tmp_path / "charts.zip"])
    assert [c.name for c in charts] == ["APPROACH", "HARBOUR"]
    source = chart_depth_source(charts, flat(30.0), cell_m=50.0, reach_m=600.0)
    on_bank, approach, beyond = source(np.array([LAT, LAT + 0.03, LAT + 0.2]), np.array([LON, LON, LON]))
    assert on_bank == pytest.approx(7.5, abs=0.05)  # not the approach chart's 45 m
    assert approach == pytest.approx(30.0, abs=0.05)
    assert beyond == 30.0
    # Outside the harbour chart the approach chart's depth area still holds: 20 to 50 m, so a 12 m base is raised.
    shallow_base = chart_depth_source(charts, flat(12.0), cell_m=50.0, reach_m=600.0)
    assert shallow_base(np.array([LAT - 0.03]), np.array([LON - 0.03]))[0] == 20.0


def test_an_encrypted_or_damaged_chart_says_what_is_wrong(tmp_path):
    path = tmp_path / "HK400001.000"
    path.write_bytes(bytes(range(256)) * 4)
    with pytest.raises(ValueError, match="encrypted"):
        read_chart(path)
    with pytest.raises(ValueError, match="no charts"):
        chart_depth_source([], flat(10.0))
    assert isinstance(read_chart(approach_chart()), Chart)


def test_a_folder_of_charts_is_read_like_the_archive(tmp_path):
    for name, cell in (("APPROACH", approach_chart()), ("HARBOUR", harbour_chart())):
        (tmp_path / "ENC_ROOT" / name).mkdir(parents=True)
        (tmp_path / "ENC_ROOT" / name / f"{name}.000").write_bytes(cell)
    (tmp_path / "ENC_ROOT" / "CATALOG.031").write_bytes(b"the list of what is in the folder")
    (tmp_path / "ENC_ROOT" / "HARBOUR" / "HARBOUR.002").write_bytes(b"an update")
    with pytest.warns(UserWarning, match="1 chart update"):
        assert [c.name for c in read_charts([tmp_path])] == ["APPROACH", "HARBOUR"]


def island_chart() -> bytes:
    """Charted 2 km square with water only in its middle 0.8 km (a lagoon); the rest of it is land."""
    cell = Cell("LAGOON", 12000)
    outer, lagoon = square(0.010), square(0.004)
    cell.node(1, *outer[0])
    cell.node(2, *lagoon[0])
    cell.edge(1, 1, 1, outer[1:])
    cell.edge(2, 2, 2, lagoon[1:])
    cell.soundings(1, [(LAT, LON, 3.0)])
    cell.object(302, 3, {18: 1}, [(EDGE, 1, 1, 1)])
    cell.object(42, 3, {87: 2, 88: 5}, [(EDGE, 2, 1, 1)])
    return cell.bytes()


def test_charts_say_where_water_and_land_are():
    source = chart_depth_source(read_chart(island_chart(), "LAGOON"), flat(30.0), cell_m=50.0)
    lat = np.array([LAT, LAT + 0.007, LAT + 0.2])
    lon = np.array([LON, LON, LON])
    lagoon, land, beyond = source.water(lat, lon)
    assert lagoon == 1.0 and land == 0.0 and np.isnan(beyond)
