"""Tests for parser.py - shape parsing and coordinate conversion."""

from kicad_jlcimport.easyeda.ee_types import EEFootprint, EESymbol
from kicad_jlcimport.easyeda.parser import (
    MILS_TO_MM_DIVISOR,
    _find_svg_path,
    _parse_solid_region,
    _parse_svg_arc_path,
    _parse_svg_path_with_arcs,
    _parse_svg_polygon,
    _parse_sym_path,
    compute_arc_midpoint,
    mil_to_mm,
    parse_footprint_shapes,
    parse_symbol_shapes,
)


class TestMilToMm:
    def test_zero(self):
        assert mil_to_mm(0) == 0

    def test_positive(self):
        result = mil_to_mm(100)
        assert abs(result - 100 / MILS_TO_MM_DIVISOR) < 1e-10

    def test_negative(self):
        result = mil_to_mm(-50)
        assert result < 0
        assert abs(result - (-50 / MILS_TO_MM_DIVISOR)) < 1e-10


class TestParseSvgArcPath:
    def test_valid_arc(self):
        path = "M 100 200 A 50 50 0 0 1 150 250"
        result = _parse_svg_arc_path(path)
        assert result is not None
        sx, sy, rx, ry, large_arc, sweep, ex, ey = result
        assert sx == 100.0
        assert sy == 200.0
        assert rx == 50.0
        assert ry == 50.0
        assert large_arc == 0
        assert sweep == 1
        assert ex == 150.0
        assert ey == 250.0

    def test_valid_arc_with_commas(self):
        path = "M100,200A50,50,0,1,0,150,250"
        result = _parse_svg_arc_path(path)
        assert result is not None
        sx, sy, rx, ry, large_arc, sweep, ex, ey = result
        assert sx == 100.0
        assert sy == 200.0
        assert large_arc == 1
        assert sweep == 0

    def test_valid_arc_negative_coords(self):
        path = "M -10.5 -20.3 A 30 30 0 0 1 -5.5 -15.3"
        result = _parse_svg_arc_path(path)
        assert result is not None
        sx, sy, _, _, _, _, ex, ey = result
        assert sx == -10.5
        assert sy == -20.3
        assert ex == -5.5
        assert ey == -15.3

    def test_invalid_no_arc(self):
        assert _parse_svg_arc_path("M 100 200 L 150 250") is None

    def test_invalid_empty(self):
        assert _parse_svg_arc_path("") is None

    def test_invalid_malformed(self):
        assert _parse_svg_arc_path("M abc A def") is None

    def test_invalid_zero_radius(self):
        assert _parse_svg_arc_path("M 100 200 A 0 50 0 0 1 150 250") is None
        assert _parse_svg_arc_path("M 100 200 A 50 0 0 0 1 150 250") is None

    def test_invalid_negative_radius(self):
        assert _parse_svg_arc_path("M 100 200 A -5 50 0 0 1 150 250") is None


class TestFindSvgPath:
    def test_finds_path(self):
        parts = ["ARC", "2", "3", "id123", "M 100 200 A 50 50 0 0 1 150 250"]
        assert _find_svg_path(parts, start=3) == "M 100 200 A 50 50 0 0 1 150 250"

    def test_strips_whitespace(self):
        parts = ["ARC", "2", "3", "  M100,200A50,50  "]
        assert _find_svg_path(parts, start=1) == "M100,200A50,50"

    def test_returns_empty_when_not_found(self):
        parts = ["ARC", "2", "3", "no_path_here"]
        assert _find_svg_path(parts, start=1) == ""

    def test_returns_empty_for_empty_parts(self):
        assert _find_svg_path([], start=0) == ""


class TestParseFootprintShapes:
    def test_empty_shapes(self):
        fp = parse_footprint_shapes([], 0, 0)
        assert isinstance(fp, EEFootprint)
        assert fp.pads == []
        assert fp.tracks == []

    def test_parse_rect_pad(self):
        shape = "PAD~RECT~400~300~10~10~1~~1~0~~~0~id1"
        fp = parse_footprint_shapes([shape], 400, 300)
        assert len(fp.pads) == 1
        pad = fp.pads[0]
        assert pad.shape == "RECT"
        assert pad.number == "1"
        assert abs(pad.x) < 0.01  # origin-corrected
        assert abs(pad.y) < 0.01

    def test_parse_oval_pad(self):
        shape = "PAD~OVAL~400~300~5~10~1~~2~3~~~0~id2"
        fp = parse_footprint_shapes([shape], 400, 300)
        assert len(fp.pads) == 1
        assert fp.pads[0].shape == "OVAL"
        assert fp.pads[0].number == "2"

    def test_parse_polygon_pad(self):
        """POLYGON pad should store center-relative polygon_points in mm."""
        # Pad at (400, 300) with a triangle polygon: (390,290) (410,290) (400,310)
        shape = "PAD~POLYGON~400~300~20~20~1~~1~0~390 290 410 290 400 310~0~id3"
        fp = parse_footprint_shapes([shape], 400, 300)
        assert len(fp.pads) == 1
        pad = fp.pads[0]
        assert pad.shape == "POLYGON"
        # 3 vertices * 2 coords = 6 floats
        assert len(pad.polygon_points) == 6
        # First vertex: (390-400, 290-300) = (-10, -10) in mils -> mm
        assert abs(pad.polygon_points[0] - mil_to_mm(-10)) < 1e-6
        assert abs(pad.polygon_points[1] - mil_to_mm(-10)) < 1e-6

    def test_parse_polygon_pad_svg_path_with_arcs(self):
        """POLYGON pad with SVG path containing arcs should be parsed correctly."""
        # Rounded rectangle centered at (400, 300) with arc corners
        svg = (
            "M 390 290 L 410 290 A 5 5 0 0 1 415 295 "
            "L 415 305 A 5 5 0 0 1 410 310 "
            "L 390 310 A 5 5 0 0 1 385 305 "
            "L 385 295 A 5 5 0 0 1 390 290 Z"
        )
        shape = f"PAD~POLYGON~400~300~30~20~1~~1~0~{svg}~0~id4"
        fp = parse_footprint_shapes([shape], 400, 300)
        assert len(fp.pads) == 1
        pad = fp.pads[0]
        assert pad.shape == "POLYGON"
        # Arc parser generates many points (4 arcs × 8+ segments each + line endpoints)
        assert len(pad.polygon_points) > 16

    def test_parse_polygon_pad_empty_coords(self):
        """POLYGON pad with empty polygon string gets empty polygon_points."""
        shape = "PAD~POLYGON~400~300~20~20~1~~1~0~~0~id3"
        fp = parse_footprint_shapes([shape], 400, 300)
        assert len(fp.pads) == 1
        assert fp.pads[0].polygon_points == []

    def test_parse_track(self):
        shape = "TRACK~2~3~id1~100 100 200 200"
        fp = parse_footprint_shapes([shape], 0, 0)
        assert len(fp.tracks) == 1
        track = fp.tracks[0]
        assert track.layer == "F.SilkS"
        assert len(track.points) == 2

    def test_parse_circle(self):
        shape = "CIRCLE~100~100~10~1~3~id1"
        fp = parse_footprint_shapes([shape], 0, 0)
        assert len(fp.circles) == 1
        circle = fp.circles[0]
        assert circle.layer == "F.SilkS"

    def test_skip_decorative_circle(self):
        shape = "CIRCLE~100~100~10~1~100~id1"
        fp = parse_footprint_shapes([shape], 0, 0)
        assert len(fp.circles) == 0

    def test_skip_component_marking_circle(self):
        """Layer 101 (Component Marking Layer) circles should be filtered."""
        shape = "CIRCLE~100~100~10~1~101~id1~0~~"
        fp = parse_footprint_shapes([shape], 0, 0)
        assert len(fp.circles) == 0

    def test_parse_hole(self):
        shape = "HOLE~200~200~5~id1"
        fp = parse_footprint_shapes([shape], 0, 0)
        assert len(fp.holes) == 1
        hole = fp.holes[0]
        assert hole.radius == mil_to_mm(5)

    def test_origin_offset_applied(self):
        shape = "PAD~RECT~400~300~10~10~1~~1~0~~~0~id1"
        fp = parse_footprint_shapes([shape], 200, 100)
        pad = fp.pads[0]
        # Pad at (400,300) with origin (200,100) => offset (200, 200) in mils
        expected_x = mil_to_mm(400) - mil_to_mm(200)
        expected_y = mil_to_mm(300) - mil_to_mm(100)
        assert abs(pad.x - expected_x) < 0.001
        assert abs(pad.y - expected_y) < 0.001

    def test_parse_text_plus_as_tracks(self):
        """TEXT '+' produces two track segments (vertical + horizontal lines)."""
        shape = "TEXT~L~100~100~0.8~0~0~3~~4~+~M 98 95 L 98 105 M 93 100 L 103 100~~id1~~0~pinpart"
        fp = parse_footprint_shapes([shape], 0, 0)
        assert len(fp.tracks) == 2
        assert fp.tracks[0].layer == "F.SilkS"
        assert fp.tracks[1].layer == "F.SilkS"
        assert len(fp.tracks[0].points) == 2
        assert len(fp.tracks[1].points) == 2

    def test_parse_text_minus_as_track(self):
        """TEXT '-' produces a single track segment (one horizontal line)."""
        shape = "TEXT~L~100~100~0.8~0~0~3~~4~-~M 93 100 L 103 100~~id1~~0~pinpart"
        fp = parse_footprint_shapes([shape], 0, 0)
        assert len(fp.tracks) == 1
        assert fp.tracks[0].layer == "F.SilkS"
        assert len(fp.tracks[0].points) == 2

    def test_parse_text_stroke_width(self):
        """TEXT stroke width is converted from mils to mm."""
        shape = "TEXT~L~100~100~0.8~0~0~3~~4~+~M 98 95 L 98 105 M 93 100 L 103 100~~id1~~0~pinpart"
        fp = parse_footprint_shapes([shape], 0, 0)
        assert abs(fp.tracks[0].width - mil_to_mm(0.8)) < 1e-6

    def test_parse_text_unknown_layer_skipped(self):
        """TEXT on unmapped layer produces no tracks."""
        shape = "TEXT~L~100~100~0.8~0~0~999~~4~+~M 98 95 L 98 105~~id1~~0~pinpart"
        fp = parse_footprint_shapes([shape], 0, 0)
        assert len(fp.tracks) == 0

    def test_parse_text_empty_path_skipped(self):
        """TEXT with no SVG path produces no tracks."""
        shape = "TEXT~L~100~100~0.8~0~0~3~~4~+~~~id1~~0~pinpart"
        fp = parse_footprint_shapes([shape], 0, 0)
        assert len(fp.tracks) == 0

    def test_parse_text_origin_offset_applied(self):
        """TEXT track coordinates should have origin offset applied."""
        shape = "TEXT~L~100~100~0.8~0~0~3~~4~-~M 100 100 L 110 100~~id1~~0~pinpart"
        fp = parse_footprint_shapes([shape], 50, 50)
        assert len(fp.tracks) == 1
        x0, y0 = fp.tracks[0].points[0]
        x1, y1 = fp.tracks[0].points[1]
        # After origin offset: (100-50)=50 mils, (110-50)=60 mils
        assert abs(x0 - mil_to_mm(50)) < 1e-6
        assert abs(y0 - mil_to_mm(50)) < 1e-6
        assert abs(x1 - mil_to_mm(60)) < 1e-6
        assert abs(y1 - mil_to_mm(50)) < 1e-6


class TestParseSymbolShapes:
    def test_empty_shapes(self):
        sym = parse_symbol_shapes([], 0, 0)
        assert isinstance(sym, EESymbol)
        assert sym.pins == []
        assert sym.rectangles == []

    def test_parse_rectangle(self):
        shape = "R~100~100~0~0~50~30~0~0~0~1~0~id1"
        sym = parse_symbol_shapes([shape], 0, 0)
        assert len(sym.rectangles) == 1
        rect = sym.rectangles[0]
        assert rect.width == mil_to_mm(50)

    def test_parse_circle(self):
        shape = "E~100~100~20~20~0~0"
        sym = parse_symbol_shapes([shape], 0, 0)
        assert len(sym.circles) == 1
        assert sym.circles[0].radius == mil_to_mm(20)

    def test_parse_polyline(self):
        shape = "PL~100~100~200~200~0~3"
        sym = parse_symbol_shapes([shape], 0, 0)
        assert len(sym.polylines) == 1
        assert len(sym.polylines[0].points) == 2
        assert sym.polylines[0].closed is False

    def test_parse_polyline_space_separated(self):
        """Test polyline with space-separated coordinates (e.g. C100072 capacitor)."""
        shapes = [
            "PL~13 -8 13 8~#880000~1~0~none~rep2~0",
            "PL~17 -8 17 8~#880000~1~0~none~rep3~0",
            "PL~10 0 13 0~#880000~1~0~none~rep6~0",
            "PL~17 0 20 0~#880000~1~0~none~rep7~0",
        ]
        sym = parse_symbol_shapes(shapes, 15, 0)
        assert len(sym.polylines) == 4
        # First polyline: vertical line at x=13, from y=-8 to y=8
        pl = sym.polylines[0]
        assert len(pl.points) == 2
        assert pl.points[0][0] == mil_to_mm(13 - 15)  # x relative to origin
        assert pl.points[0][1] == -mil_to_mm(-8)  # y inverted
        assert pl.points[1][0] == mil_to_mm(13 - 15)
        assert pl.closed is False

    def test_parse_polygon(self):
        shape = "PG~100~100~200~200~100~200~0~3"
        sym = parse_symbol_shapes([shape], 0, 0)
        assert len(sym.polylines) == 1
        assert sym.polylines[0].closed is True
        assert sym.polylines[0].fill is True

    def test_parse_pin(self):
        shape = (
            "P~show~0~1~400~300~0~id1^^section1^^M400,300h10^^1~0~0~0~TestPin~start~~~#0000FF^^1~0~0~0~1~end~~~#0000FF"
        )
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        pin = sym.pins[0]
        assert pin.number == "1"
        assert pin.electrical_type == "unspecified"

    def test_parse_pin_horizontal_right(self):
        """Test horizontal pin extending right (h +N) -> KiCad 0°."""
        shape = "P~show~0~1~400~300~0~id1^^400~300^^M 400 300 h 10~#880000^^1~0~0~0~1~start~~#0000FF"
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        assert sym.pins[0].rotation == 0

    def test_parse_pin_horizontal_left(self):
        """Test horizontal pin extending left (h -N) -> KiCad 180°."""
        shape = "P~show~0~1~400~300~180~id1^^400~300^^M 400 300 h -10~#880000^^1~0~0~0~1~end~~#0000FF"
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        assert sym.pins[0].rotation == 180

    def test_parse_pin_vertical_up(self):
        """Test vertical pin extending up (v -N in SVG) -> KiCad 90°."""
        shape = "P~show~0~1~400~300~270~id1^^400~300^^M 400 300 v -10~#880000^^1~0~0~0~1~start~~#0000FF"
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        assert sym.pins[0].rotation == 90

    def test_parse_pin_vertical_down(self):
        """Test vertical pin extending down (v +N in SVG) -> KiCad 270°."""
        shape = "P~show~0~1~400~300~90~id1^^400~300^^M 400 300 v 10~#880000^^1~0~0~0~1~start~~#0000FF"
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        assert sym.pins[0].rotation == 270

    def test_parse_pin_body_to_pin_horizontal_right(self):
        """Test body-to-pin path (h +N, M starts at body edge) -> KiCad 180°."""
        # Pin position is (420,300), but path starts at body edge (410,300)
        # Path goes body→pin, so direction flips: h+ becomes 180° not 0°
        shape = "P~show~0~2~420~300~0~id1^^420~300^^M410,300 h10~#880000^^0~406~300~0~2~end~~~#800^^0~414~296~0~2~start~~~#800"
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        assert sym.pins[0].rotation == 180

    def test_parse_pin_body_to_pin_horizontal_left(self):
        """Test body-to-pin path (h -N, M starts at body edge) -> KiCad 0°."""
        # Pin position is (380,300), but path starts at body edge (390,300)
        # Path goes body→pin, so direction flips: h- becomes 0° not 180°
        shape = "P~show~0~1~380~300~180~id1^^380~300^^M390,300 h-10~#880000^^0~394~300~0~1~start~~~#800^^0~386~296~0~1~end~~~#800"
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        assert sym.pins[0].rotation == 0

    def test_parse_pin_body_to_pin_vertical_down(self):
        """Test body-to-pin path (v +N, M starts at body edge) -> KiCad 90°."""
        # Pin position is (400,340), but path starts at body edge (400,330)
        # Path goes body→pin downward, so direction flips: v+ becomes 90° not 270°
        shape = "P~show~0~1~400~340~270~id1^^400~340^^M400,330 v10~#880000^^1~0~0~0~1~start~~#0000FF"
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        assert sym.pins[0].rotation == 90

    def test_parse_pin_body_to_pin_vertical_up(self):
        """Test body-to-pin path (v -N, M starts at body edge) -> KiCad 270°."""
        # Pin position is (400,260), but path starts at body edge (400,270)
        # Path goes body→pin upward, so direction flips: v- becomes 270° not 90°
        shape = "P~show~0~1~400~260~90~id1^^400~260^^M400,270 v-10~#880000^^1~0~0~0~1~start~~#0000FF"
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        assert sym.pins[0].rotation == 270

    def test_parse_pin_empty_rotation(self):
        """Pin with empty rotation field should default to 0, not be dropped."""
        # Reproduces C6188 pin 4 (TAB) which has no rotation value
        shape = "P~show~0~4~450~290~~gge45~0^^450~290^^M 450 290 h -20~#880000^^1~428~293~0~TAB~end~~~#0000FF^^1~435~289~0~4~start~~~#0000FF"
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        pin = sym.pins[0]
        assert pin.number == "4"
        assert pin.name == "TAB"

    def test_parse_pin_display_number_differs_from_spice_index(self):
        """Display pin number from section 4 should override SPICE index."""
        # Reproduces C6188 pin 1 where SPICE index=1 but display number=3
        shape = "P~show~0~1~350~300~180~gge24~0^^350~300^^M 350 300 h 20~#880000^^1~372~303~0~In~start~~~#0000FF^^1~365~299~0~3~end~~~#0000FF"
        sym = parse_symbol_shapes([shape], 400, 300)
        assert len(sym.pins) == 1
        pin = sym.pins[0]
        assert pin.number == "3"
        assert pin.name == "In"

    def test_y_inversion_for_symbols(self):
        shape = "E~100~200~10~10~0~0"
        sym = parse_symbol_shapes([shape], 0, 0)
        circle = sym.circles[0]
        # Y should be inverted: -(200 - 0) in mils -> mm
        assert circle.cy == -mil_to_mm(200)


class TestComputeArcMidpoint:
    def test_semicircle(self):
        # A half-circle from (0, -1) to (0, 1) with radius 1
        mid = compute_arc_midpoint((0, -1), (0, 1), 1, 1, 0, 1)
        # Midpoint should be at approximately (1, 0) or (-1, 0)
        assert abs(mid[0] ** 2 + mid[1] ** 2 - 1) < 0.1  # on circle of radius 1

    def test_coincident_points(self):
        mid = compute_arc_midpoint((5, 5), (5, 5), 10, 10, 0, 1)
        assert mid == (5, 5)

    def test_large_arc_flag(self):
        # Use radius > chord/2 so center is off-chord and flags diverge
        mid0 = compute_arc_midpoint((0, 0), (2, 0), 2, 2, 0, 1)
        mid1 = compute_arc_midpoint((0, 0), (2, 0), 2, 2, 1, 1)
        # Different large_arc flags should give different midpoints
        assert mid0 != mid1


class TestParseSvgPolygon:
    """Test SVG polygon path parsing with different coordinate formats."""

    def test_space_separated_coordinates(self):
        """Test parsing paths with space-separated coordinates (e.g. 'M 390 304 L 397 304')."""
        path = "M 390 304 L 397 304 L 397 305 L 390 305 Z"
        points = _parse_svg_polygon(path)
        assert len(points) == 4
        # Points are converted from mils to mm
        assert points[0] == (mil_to_mm(390), mil_to_mm(304))
        assert points[1] == (mil_to_mm(397), mil_to_mm(304))
        assert points[2] == (mil_to_mm(397), mil_to_mm(305))
        assert points[3] == (mil_to_mm(390), mil_to_mm(305))

    def test_comma_separated_coordinates(self):
        """Test parsing paths with comma-separated coordinates (e.g. 'M414,286L414,314')."""
        path = "M414,286L414,314L417,310L419,304L420,298L419,294Z"
        points = _parse_svg_polygon(path)
        assert len(points) == 6
        assert points[0] == (mil_to_mm(414), mil_to_mm(286))
        assert points[1] == (mil_to_mm(414), mil_to_mm(314))
        assert points[2] == (mil_to_mm(417), mil_to_mm(310))
        assert points[3] == (mil_to_mm(419), mil_to_mm(304))
        assert points[4] == (mil_to_mm(420), mil_to_mm(298))
        assert points[5] == (mil_to_mm(419), mil_to_mm(294))

    def test_mixed_separators(self):
        """Test paths that mix commas and spaces."""
        path = "M 100,200 L 150 250 L200,300Z"
        points = _parse_svg_polygon(path)
        assert len(points) == 3
        assert points[0] == (mil_to_mm(100), mil_to_mm(200))
        assert points[1] == (mil_to_mm(150), mil_to_mm(250))
        assert points[2] == (mil_to_mm(200), mil_to_mm(300))

    def test_empty_path(self):
        """Test empty path returns empty list."""
        assert _parse_svg_polygon("") == []

    def test_path_without_z(self):
        """Test path without closing Z command."""
        path = "M100,100L200,200"
        points = _parse_svg_polygon(path)
        assert len(points) == 2


class TestParseSolidRegion:
    """Test SOLIDREGION shape parsing."""

    def test_silkscreen_solid_region_space_separated(self):
        """Test parsing silkscreen solid region with space-separated path."""
        shape = "SOLIDREGION~3~~M 390 304 L 397 304 L 397 305 L 390 305 Z ~solid~gge104~~~~0"
        parts = shape.split("~")
        region = _parse_solid_region(parts)
        assert region is not None
        assert region.layer == "F.SilkS"
        assert len(region.points) == 4
        assert region.region_type == "solid"

    def test_silkscreen_solid_region_comma_separated(self):
        """Test parsing silkscreen solid region with comma-separated path (no spaces)."""
        shape = "SOLIDREGION~3~~M414,286L414,314L417,310L419,304L420,298L419,294Z~solid~gge14~~~~0"
        parts = shape.split("~")
        region = _parse_solid_region(parts)
        assert region is not None
        assert region.layer == "F.SilkS"
        assert len(region.points) == 6
        assert region.region_type == "solid"

    def test_npth_region(self):
        """Test parsing NPTH (edge cuts) region."""
        shape = "SOLIDREGION~99~~M 100 100 L 200 100 L 200 200 L 100 200 Z~npth~gge1~~~~0"
        parts = shape.split("~")
        region = _parse_solid_region(parts)
        assert region is not None
        assert region.layer == "Edge.Cuts"
        assert region.region_type == "npth"

    def test_fab_layer_imported(self):
        """Test that fab layer (12) solid regions are imported (e.g., polarity marks)."""
        shape = "SOLIDREGION~12~~M 390 304 L 397 304 L 397 305 L 390 305 Z ~solid~gge104~~~~0"
        parts = shape.split("~")
        region = _parse_solid_region(parts)
        assert region is not None
        assert region.layer == "F.Fab"
        assert len(region.points) == 4

    def test_path_starting_with_m_digit(self):
        """Test detection of paths starting with M followed immediately by digit."""
        shape = "SOLIDREGION~3~~M100,100L200,200L100,200Z~solid~gge1~~~~0"
        parts = shape.split("~")
        region = _parse_solid_region(parts)
        assert region is not None
        assert len(region.points) == 3

    def test_path_starting_with_m_negative(self):
        """Test detection of paths starting with M followed by negative number."""
        shape = "SOLIDREGION~3~~M-100,-100L-200,-200L-100,-200Z~solid~gge1~~~~0"
        parts = shape.split("~")
        region = _parse_solid_region(parts)
        assert region is not None
        assert len(region.points) == 3


class TestParseSvgPathWithArcs:
    """Test _parse_svg_path_with_arcs general SVG path walker."""

    def test_full_circle_two_arcs(self):
        """Two arcs forming a full circle (C427602 pin 1 dot)."""
        path = (
            "M 3979.0547 2997.751 A 0.6969 0.6969 0 1 1 3980.4484 2997.751 A 0.6969 0.6969 0 1 1 3979.0547 2997.751 Z"
        )
        points = _parse_svg_path_with_arcs(path)
        # Two 180-degree arcs = full circle, should have many points
        assert len(points) >= 16
        # Check bounding box is roughly circular with expected center/radius
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        expected_cx = mil_to_mm((3979.0547 + 3980.4484) / 2)
        expected_cy = mil_to_mm(2997.751)
        assert abs(cx - expected_cx) < 0.01
        assert abs(cy - expected_cy) < 0.01
        diameter = max(xs) - min(xs)
        expected_diameter = mil_to_mm(0.6969 * 2)
        assert abs(diameter - expected_diameter) < 0.01

    def test_single_arc_d_shape(self):
        """Single arc D-shape (semicircle polarity mark)."""
        # Fabricated D-shape: start at top, arc 180 degrees to bottom, line back
        path = "M 100 90 A 10 10 0 0 1 100 110 L 100 90 Z"
        points = _parse_svg_path_with_arcs(path)
        assert len(points) >= 3
        # All arc points should be on one side (x >= 100 for sweep=1)
        xs = [p[0] for p in points]
        # Center of arc is at (100, 100), radius 10 — arc goes rightward
        assert max(xs) > mil_to_mm(100)

    def test_rounded_rectangle_c395958(self):
        """Rounded rectangle with 4 corner arcs + lines (C395958 layer 12)."""
        path = (
            "M 3960.3278 2985.4332 L 3960.3278 3048.7126 L 4040.0363 3048.7126"
            " L 4040.0363 2985.4332 A 0.5 0.5 0 0 1 4041.0363 2985.4332"
            " L 4041.0363 3049.2126 A 0.5 0.5 0 0 1 4040.5363 3049.7126"
            " L 3959.8278 3049.7126 A 0.5 0.5 0 0 1 3959.3278 3049.2126"
            " L 3959.3278 2985.4332 A 0.5 0.5 0 0 1 3960.3278 2985.4332 Z"
        )
        points = _parse_svg_path_with_arcs(path)
        # 8 L/M points + 4 arcs * 16 segments each = lots of points
        assert len(points) >= 20
        # Bounding box should be roughly 81.7 x 64.3 mils
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)
        expected_w = mil_to_mm(4041.0363 - 3959.3278)
        expected_h = mil_to_mm(3049.7126 - 2985.4332)
        assert abs(width - expected_w) < 0.15
        assert abs(height - expected_h) < 0.15

    def test_empty_path(self):
        """Empty path returns empty list."""
        assert _parse_svg_path_with_arcs("") == []

    def test_no_arcs_still_works(self):
        """Path with only M and L commands (no arcs) still produces points."""
        path = "M 100 100 L 200 100 L 200 200 L 100 200 Z"
        points = _parse_svg_path_with_arcs(path)
        assert len(points) == 4
        assert points[0] == (mil_to_mm(100), mil_to_mm(100))
        assert points[1] == (mil_to_mm(200), mil_to_mm(100))

    def test_horizontal_and_vertical_lineto(self):
        """H and V commands produce correct points."""
        path = "M 100 100 H 200 V 200 H 100 V 100 Z"
        points = _parse_svg_path_with_arcs(path)
        assert len(points) == 5
        assert points[0] == (mil_to_mm(100), mil_to_mm(100))
        assert points[1] == (mil_to_mm(200), mil_to_mm(100))  # H 200
        assert points[2] == (mil_to_mm(200), mil_to_mm(200))  # V 200
        assert points[3] == (mil_to_mm(100), mil_to_mm(200))  # H 100
        assert points[4] == (mil_to_mm(100), mil_to_mm(100))  # V 100

    def test_relative_commands_match_absolute(self):
        """Lowercase m/l/h/v commands produce same result as absolute equivalent."""
        abs_path = "M 100 100 L 200 100 L 200 200 L 100 200 Z"
        rel_path = "M 100 100 l 100 0 l 0 100 l -100 0 Z"
        abs_points = _parse_svg_path_with_arcs(abs_path)
        rel_points = _parse_svg_path_with_arcs(rel_path)
        assert len(abs_points) == len(rel_points)
        for ap, rp in zip(abs_points, rel_points):
            assert abs(ap[0] - rp[0]) < 1e-10
            assert abs(ap[1] - rp[1]) < 1e-10

    def test_relative_h_v_commands(self):
        """Lowercase h/v commands use offsets from current position."""
        abs_path = "M 100 100 H 200 V 200 H 100 V 100 Z"
        rel_path = "M 100 100 h 100 v 100 h -100 v -100 Z"
        abs_points = _parse_svg_path_with_arcs(abs_path)
        rel_points = _parse_svg_path_with_arcs(rel_path)
        assert len(abs_points) == len(rel_points)
        for ap, rp in zip(abs_points, rel_points):
            assert abs(ap[0] - rp[0]) < 1e-10
            assert abs(ap[1] - rp[1]) < 1e-10

    def test_relative_m_command(self):
        """Lowercase m command uses offset from current position."""
        # Start at (100, 100), then relative move to (110, 110)
        path = "M 100 100 L 200 100 m 10 10 L 300 200 Z"
        points = _parse_svg_path_with_arcs(path)
        # After L 200 100, m 10 10 -> (210, 110)
        assert abs(points[2][0] - mil_to_mm(210)) < 1e-10
        assert abs(points[2][1] - mil_to_mm(110)) < 1e-10

    def test_relative_arc_command(self):
        """Lowercase a command offsets endpoint from current position."""
        abs_path = "M 100 90 A 10 10 0 0 1 100 110 Z"
        rel_path = "M 100 90 a 10 10 0 0 1 0 20 Z"
        abs_points = _parse_svg_path_with_arcs(abs_path)
        rel_points = _parse_svg_path_with_arcs(rel_path)
        assert len(abs_points) == len(rel_points)
        for ap, rp in zip(abs_points, rel_points):
            assert abs(ap[0] - rp[0]) < 1e-6
            assert abs(ap[1] - rp[1]) < 1e-6

    def test_elliptical_arc_different_rx_ry(self):
        """Full ellipse with rx != ry produces correct bounding box."""
        # Two semicircular arcs forming a full ellipse: rx=20, ry=10
        # Start at left (80, 100), arc to right (120, 100), arc back
        path = "M 80 100 A 20 10 0 0 1 120 100 A 20 10 0 0 1 80 100 Z"
        points = _parse_svg_path_with_arcs(path)
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)
        # Full ellipse: width = 2*rx = 40 mils, height = 2*ry = 20 mils
        expected_width = mil_to_mm(40)
        expected_height = mil_to_mm(20)
        assert abs(width - expected_width) < mil_to_mm(1)
        assert abs(height - expected_height) < mil_to_mm(1)

    def test_adaptive_segments_large_vs_small_arc(self):
        """Large arc gets more segments than small arc."""
        # Small arc: radius 1 mil, 90 degree sweep
        small_path = "M 100 100 A 1 1 0 0 1 101 101 Z"
        small_points = _parse_svg_path_with_arcs(small_path)
        small_arc_points = len(small_points) - 1  # subtract M point

        # Large arc: radius 50 mils, full semicircle
        large_path = "M 100 50 A 50 50 0 1 1 100 150 Z"
        large_points = _parse_svg_path_with_arcs(large_path)
        large_arc_points = len(large_points) - 1  # subtract M point

        assert large_arc_points > small_arc_points


class TestParseSolidRegionArcDetection:
    """Test arc detection heuristic in _parse_solid_region."""

    def test_spaceless_arc_detected(self):
        """Path where A has no space before it still routes to arc parser."""
        # "...3008.5A8.5..." — digit immediately before A
        path = "M3000,3000A8.5,8.5,0,1,1,3017,3000A8.5,8.5,0,1,1,3000,3000Z"
        shape = f"SOLIDREGION~3~~{path}~solid~gge1~~~~0"
        parts = shape.split("~")
        region = _parse_solid_region(parts)
        assert region is not None
        # Should have arc-generated points (many more than 3)
        assert len(region.points) > 8

    def test_npth_region_with_arcs(self):
        """NPTH region with rounded corners (arc commands) should preserve arcs."""
        # Rounded rectangle: straight edges with arc corners
        path = (
            "M 100 90 L 190 90 A 10 10 0 0 1 200 100 "
            "L 200 190 A 10 10 0 0 1 190 200 "
            "L 110 200 A 10 10 0 0 1 100 190 "
            "L 100 100 A 10 10 0 0 1 110 90 Z"
        )
        shape = f"SOLIDREGION~99~~{path}~npth~gge1~~~~0"
        parts = shape.split("~")
        region = _parse_solid_region(parts)
        assert region is not None
        assert region.layer == "Edge.Cuts"
        assert region.region_type == "npth"
        # Arc parser generates many points for each corner arc;
        # without arc support we'd only get 8 points (M + L endpoints)
        assert len(region.points) > 16


class TestC17451410PinRotations:
    """Test pin rotations for C17451410 (IS01EBFRGB) - has pins in all 4 directions."""

    def test_all_pin_rotations(self):
        """Validate all 10 pins have correct rotations based on their direction."""
        import json
        from pathlib import Path

        testdata = Path(__file__).parent.parent / "testdata" / "C17451410_symbol.json"
        with open(testdata) as f:
            data = json.load(f)

        ds = data["result"]["dataStr"]
        shapes = ds.get("shape", [])
        head = ds.get("head", {})
        origin_x = head.get("x", 0)
        origin_y = head.get("y", 0)

        symbol = parse_symbol_shapes(shapes, origin_x, origin_y)

        assert len(symbol.pins) == 10

        # Build a dict of pin number -> rotation
        pin_rotations = {pin.number: pin.rotation for pin in symbol.pins}

        # Pins 1-3: left side, extending right (h +N) -> 0°
        assert pin_rotations["1"] == 0, "Pin 1 (SCK) should extend right"
        assert pin_rotations["2"] == 0, "Pin 2 (SS#) should extend right"
        assert pin_rotations["3"] == 0, "Pin 3 (SDO) should extend right"

        # Pins 4-6: right side, extending left (h -N) -> 180°
        assert pin_rotations["4"] == 180, "Pin 4 (SDI) should extend left"
        assert pin_rotations["5"] == 180, "Pin 5 (GND) should extend left"
        assert pin_rotations["6"] == 180, "Pin 6 (VDD) should extend left"

        # Pins 7-8: top, extending down (v +N) -> 270°
        assert pin_rotations["7"] == 270, "Pin 7 (EH) should extend down"
        assert pin_rotations["8"] == 270, "Pin 8 (EH) should extend down"

        # Pins 9-10: bottom, extending up (v -N) -> 90°
        assert pin_rotations["9"] == 90, "Pin 9 (EH) should extend up"
        assert pin_rotations["10"] == 90, "Pin 10 (EH) should extend up"


class TestParseSymPath:
    """Test _parse_sym_path function (PT~ symbol paths)."""

    def test_space_separated_coordinates(self):
        """Test parsing path with space-separated coordinates."""
        shape = "PT~M 100 200 L 150 250 L 100 250 Z~#880000~1~0~#880000~gge1~0~"
        result = _parse_sym_path(shape, 100, 200)
        assert result is not None
        assert len(result.points) == 3
        assert result.closed is True
        assert result.fill is True

    def test_comma_separated_coordinates(self):
        """Test parsing path with comma-separated coordinates (no spaces)."""
        shape = "PT~M100,200L150,250L100,250Z~#880000~1~0~#880000~gge1~0~"
        result = _parse_sym_path(shape, 100, 200)
        assert result is not None
        assert len(result.points) == 3
        assert result.closed is True

    def test_mixed_separators(self):
        """Test parsing path with mixed comma and space separators."""
        shape = "PT~M 100,200 L 150 250 L100,250Z~#880000~1~0~#880000~gge1~0~"
        result = _parse_sym_path(shape, 100, 200)
        assert result is not None
        assert len(result.points) == 3

    def test_open_path(self):
        """Test parsing open path (no Z command)."""
        shape = "PT~M 100 200 L 150 250~#880000~1~0~none~gge1~0~"
        result = _parse_sym_path(shape, 100, 200)
        assert result is not None
        assert len(result.points) == 2
        assert result.closed is False
        assert result.fill is False

    def test_no_fill(self):
        """Test parsing path with fill set to 'none'."""
        shape = "PT~M 100 200 L 150 250 Z~#880000~1~0~none~gge1~0~"
        result = _parse_sym_path(shape, 100, 200)
        assert result is not None
        assert result.fill is False

    def test_empty_path(self):
        """Test parsing empty path returns None."""
        shape = "PT~~#880000~1~0~none~gge1~0~"
        result = _parse_sym_path(shape, 0, 0)
        assert result is None

    def test_malformed_path(self):
        """Test parsing malformed path with invalid coordinates returns None."""
        shape = "PT~M abc def L 150 250~#880000~1~0~none~gge1~0~"
        result = _parse_sym_path(shape, 0, 0)
        # Should return None or have fewer points since invalid coords are skipped
        assert result is None or len(result.points) < 2

    def test_insufficient_parts(self):
        """Test parsing shape with insufficient parts returns None."""
        shape = "PT"
        result = _parse_sym_path(shape, 0, 0)
        assert result is None

    def test_path_with_arcs(self):
        """Symbol path with arc commands should produce many interpolated points."""
        # Rounded triangle with arc corners, origin at (100, 200)
        svg = (
            "M 100 190 L 110 190 A 5 5 0 0 1 115 195 "
            "L 115 205 A 5 5 0 0 1 110 210 "
            "L 100 210 A 5 5 0 0 1 95 205 "
            "L 95 195 A 5 5 0 0 1 100 190 Z"
        )
        shape = f"PT~{svg}~#880000~1~0~#880000~gge1~0~"
        result = _parse_sym_path(shape, 100, 200)
        assert result is not None
        assert result.closed is True
        # Arc parser generates many points; without arc support we'd get ≤8
        assert len(result.points) > 16
