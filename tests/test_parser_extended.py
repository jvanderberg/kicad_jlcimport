"""Extended tests for parser.py to improve coverage."""

import json

from kicad_jlcimport.easyeda.parser import (
    LAYER_MAP,
    _parse_circle,
    _parse_fp_arc,
    _parse_hole,
    _parse_pad,
    _parse_pin,
    _parse_rect_as_tracks,
    _parse_solid_region,
    _parse_svg_polygon,
    _parse_svgnode,
    _parse_sym_arc,
    _parse_sym_circle,
    _parse_sym_polyline,
    _parse_sym_rect,
    _parse_track,
    compute_arc_midpoint,
    mil_to_mm,
    parse_footprint_shapes,
    parse_symbol_shapes,
)


class TestParsePad:
    """Tests for _parse_pad function."""

    def test_parse_basic_rect_pad(self):
        parts = ["PAD", "RECT", "100", "200", "20", "10", "1", "", "1", "0", "", "0"]
        pad = _parse_pad(parts)
        assert pad.shape == "RECT"
        assert pad.x == mil_to_mm(100)
        assert pad.y == mil_to_mm(200)
        assert pad.number == "1"
        assert pad.drill == 0.0

    def test_parse_oval_pad_with_drill(self):
        parts = ["PAD", "OVAL", "100", "200", "20", "10", "11", "", "2", "5", "", "0"]
        pad = _parse_pad(parts)
        assert pad.shape == "OVAL"
        assert pad.number == "2"
        assert pad.drill == mil_to_mm(10)  # drill radius * 2

    def test_parse_pad_with_rotation(self):
        parts = ["PAD", "RECT", "100", "200", "20", "10", "1", "", "3", "0", "", "45"]
        pad = _parse_pad(parts)
        assert pad.rotation == 45.0

    def test_parse_polygon_pad(self):
        parts = ["PAD", "POLYGON", "100", "200", "20", "10", "1", "", "4", "0", "0 0 10 0 10 10 0 10", "0"]
        pad = _parse_pad(parts)
        assert pad.shape == "POLYGON"

    def test_parse_oval_pad_with_slot_length(self):
        """OVAL pad with non-zero parts[13] should set slot_length."""
        parts = [
            "PAD",
            "OVAL",
            "100",
            "200",
            "4.3307",
            "7.874",
            "11",
            "",
            "13",
            "1.1811",
            "100 198 100 202",
            "0",
            "gge1",
            "5.9055",
        ]
        pad = _parse_pad(parts)
        assert pad.slot_length == mil_to_mm(5.9055)
        assert pad.slot_length > 0

    def test_parse_pad_no_slot_length(self):
        """Pad without parts[13] should have slot_length=0."""
        parts = ["PAD", "OVAL", "100", "200", "20", "10", "11", "", "2", "5", "", "0"]
        pad = _parse_pad(parts)
        assert pad.slot_length == 0.0

    def test_parse_pad_slot_length_zero(self):
        """Pad with parts[13]=0 should have slot_length=0."""
        parts = [
            "PAD",
            "RECT",
            "100",
            "200",
            "20",
            "10",
            "1",
            "",
            "1",
            "0",
            "",
            "0",
            "gge1",
            "0",
        ]
        pad = _parse_pad(parts)
        assert pad.slot_length == 0.0

    def test_parse_pad_slot_length_non_numeric(self):
        """Non-numeric parts[13] (e.g. an ID) should fall back to slot_length=0."""
        parts = [
            "PAD",
            "OVAL",
            "100",
            "200",
            "20",
            "10",
            "11",
            "",
            "2",
            "5",
            "",
            "0",
            "gge1",
            "some_text",
        ]
        pad = _parse_pad(parts)
        assert pad.slot_length == 0.0

    def test_parse_pad_layer_mapping(self):
        parts = ["PAD", "RECT", "100", "200", "20", "10", "2", "", "1", "0", "", "0"]
        pad = _parse_pad(parts)
        assert pad.layer == "2"  # B.Cu


class TestParseTrack:
    """Tests for _parse_track function."""

    def test_parse_basic_track(self):
        parts = ["TRACK", "2", "3", "100 100 200 200"]
        track = _parse_track(parts)
        assert track.layer == "F.SilkS"
        assert len(track.points) == 2

    def test_parse_track_with_id(self):
        parts = ["TRACK", "2", "3", "id123", "100 100 200 200 300 300"]
        track = _parse_track(parts)
        assert len(track.points) == 3

    def test_parse_track_no_points(self):
        parts = ["TRACK", "2", "3", "id123"]
        track = _parse_track(parts)
        assert track is None

    def test_parse_track_single_point(self):
        parts = ["TRACK", "2", "3", "100 100"]
        track = _parse_track(parts)
        assert track is None  # Needs at least 2 points

    def test_parse_track_invalid_coords(self):
        parts = ["TRACK", "2", "3", "abc def"]
        track = _parse_track(parts)
        assert track is None

    def test_parse_track_layer_mapping(self):
        """Verify that EasyEDA layer IDs are correctly mapped to KiCad layer names."""
        # Layer "1" should map to F.Cu, not the default F.SilkS
        parts = ["TRACK", "2", "1", "100 100 200 200"]
        track = _parse_track(parts)
        assert track.layer == "F.Cu"

        # Layer "10" should map to Edge.Cuts
        parts = ["TRACK", "2", "10", "100 100 200 200"]
        track = _parse_track(parts)
        assert track.layer == "Edge.Cuts"


class TestParseFpArc:
    """Tests for _parse_fp_arc function."""

    def test_parse_basic_arc(self):
        parts = ["ARC", "2", "3", "S$id", "M 100 200 A 50 50 0 0 1 150 250"]
        arc = _parse_fp_arc(parts)
        assert arc is not None
        assert arc.layer == "F.SilkS"
        assert arc.width == mil_to_mm(2)

    def test_parse_arc_no_svg_path(self):
        parts = ["ARC", "2", "3", "id123", "not_an_svg_path"]
        arc = _parse_fp_arc(parts)
        assert arc is None

    def test_parse_arc_invalid_svg(self):
        parts = ["ARC", "2", "3", "M 100 200 L 150 250"]  # Line, not arc
        arc = _parse_fp_arc(parts)
        assert arc is None


class TestParseCircle:
    """Tests for _parse_circle function."""

    def test_parse_basic_circle(self):
        parts = ["CIRCLE", "100", "200", "50", "2", "3"]
        circle = _parse_circle(parts)
        assert circle.cx == mil_to_mm(100)
        assert circle.cy == mil_to_mm(200)
        assert circle.radius == mil_to_mm(50)
        assert circle.width == mil_to_mm(2)
        assert circle.layer == "F.SilkS"

    def test_parse_decorative_circle_layer_100(self):
        parts = ["CIRCLE", "100", "200", "50", "2", "100"]
        circle = _parse_circle(parts)
        assert circle is None  # Decorative circles are skipped

    def test_filled_circle_when_stroke_covers_area(self):
        """Circle is filled when stroke width >= 2 * radius (stroke covers entire circle)."""
        # radius=50, width=100 -> width >= 2*radius -> filled
        parts = ["CIRCLE", "100", "200", "50", "100", "3"]
        circle = _parse_circle(parts)
        assert circle.filled is True

    def test_hollow_circle_when_stroke_smaller(self):
        """Circle is hollow when stroke width < 2 * radius."""
        # radius=50, width=2 -> width < 2*radius -> not filled
        parts = ["CIRCLE", "100", "200", "50", "2", "3"]
        circle = _parse_circle(parts)
        assert circle.filled is False

    def test_zero_radius_circle_not_filled(self):
        """Zero-radius circle should not be marked as filled (degenerate case)."""
        parts = ["CIRCLE", "100", "200", "0", "10", "3"]
        circle = _parse_circle(parts)
        assert circle.filled is False


class TestParseHole:
    """Tests for _parse_hole function."""

    def test_parse_basic_hole(self):
        parts = ["HOLE", "100", "200", "25"]
        hole = _parse_hole(parts)
        assert hole.x == mil_to_mm(100)
        assert hole.y == mil_to_mm(200)
        assert hole.radius == mil_to_mm(25)


class TestParseSolidRegion:
    """Tests for _parse_solid_region function."""

    def test_parse_npth_region(self):
        parts = ["SOLIDREGION", "10", "id123", "M 0 0 L 100 0 L 100 100 L 0 100 Z", "npth"]
        region = _parse_solid_region(parts)
        assert region is not None
        assert region.region_type == "npth"
        assert len(region.points) == 4

    def test_parse_solid_region_skipped(self):
        parts = ["SOLIDREGION", "1", "id123", "M 0 0 L 100 0 L 100 100 Z", "solid"]
        region = _parse_solid_region(parts)
        assert region is None  # Only npth regions are processed

    def test_parse_cutout_region_skipped(self):
        parts = ["SOLIDREGION", "1", "id123", "M 0 0 L 100 0 L 100 100 Z", "cutout"]
        region = _parse_solid_region(parts)
        assert region is None

    def test_parse_region_no_svg_path(self):
        parts = ["SOLIDREGION", "1", "id123", "not_a_path", "npth"]
        region = _parse_solid_region(parts)
        assert region is None


class TestParseSvgPolygon:
    """Tests for _parse_svg_polygon function."""

    def test_parse_simple_polygon(self):
        points = _parse_svg_polygon("M 0 0 L 100 0 L 100 100 Z")
        assert len(points) == 3
        assert points[0] == (mil_to_mm(0), mil_to_mm(0))
        assert points[1] == (mil_to_mm(100), mil_to_mm(0))
        assert points[2] == (mil_to_mm(100), mil_to_mm(100))

    def test_parse_polygon_with_lowercase_z(self):
        points = _parse_svg_polygon("M 0 0 L 50 50 z")
        assert len(points) == 2

    def test_parse_polygon_invalid_coords(self):
        points = _parse_svg_polygon("M abc def")
        assert len(points) == 0


class TestParseSvgnode:
    """Tests for _parse_svgnode function."""

    def test_parse_basic_svgnode(self):
        attrs = {
            "uuid": "abc123",
            "c_origin": "100,200",
            "z": "5",
            "c_rotation": "0,0,90",
        }
        json_str = json.dumps({"attrs": attrs})
        parts = ["SVGNODE", json_str]
        model = _parse_svgnode(parts)

        assert model.uuid == "abc123"
        assert model.origin_x == 100
        assert model.origin_y == 200
        assert model.z == 5
        assert model.rotation == (0, 0, -90)  # Negated

    def test_parse_svgnode_no_uuid(self):
        json_str = json.dumps({"attrs": {"z": "0"}})
        parts = ["SVGNODE", json_str]
        model = _parse_svgnode(parts)
        assert model is None

    def test_parse_svgnode_invalid_json(self):
        parts = ["SVGNODE", "not valid json {{{"]
        model = _parse_svgnode(parts)
        assert model is None

    def test_parse_svgnode_minimal(self):
        attrs = {"uuid": "test123"}
        json_str = json.dumps({"attrs": attrs})
        parts = ["SVGNODE", json_str]
        model = _parse_svgnode(parts)

        assert model.uuid == "test123"
        assert model.origin_x == 0
        assert model.origin_y == 0
        assert model.z == 0


class TestParseRectAsTracks:
    """Tests for _parse_rect_as_tracks function."""

    def test_parse_basic_rect(self):
        parts = ["RECT", "100", "100", "50", "50", "3", "", "", "2"]
        tracks = _parse_rect_as_tracks(parts)
        assert len(tracks) == 1
        assert len(tracks[0].points) == 5  # Closed rectangle
        assert tracks[0].layer == "F.SilkS"

    def test_parse_rect_no_width(self):
        parts = ["RECT", "100", "100", "50", "50", "3", "", ""]
        tracks = _parse_rect_as_tracks(parts)
        assert len(tracks) == 1
        assert tracks[0].width == 0.1  # Default

    def test_parse_rect_invalid(self):
        parts = ["RECT", "abc", "def"]
        tracks = _parse_rect_as_tracks(parts)
        assert tracks == []


class TestParseSymRect:
    """Tests for _parse_sym_rect function."""

    def test_parse_short_format(self):
        shape = "R~100~200~50~30~0~0"
        rect = _parse_sym_rect(shape, 0, 0)
        assert rect.x == mil_to_mm(100)
        assert rect.y == -mil_to_mm(200)
        assert rect.width == mil_to_mm(50)

    def test_parse_long_format(self):
        shape = "R~100~200~0~0~50~30~0~0~0~1~0~id1"
        rect = _parse_sym_rect(shape, 0, 0)
        assert rect.width == mil_to_mm(50)
        assert rect.height == -mil_to_mm(30)

    def test_parse_rect_invalid(self):
        shape = "R~abc~def"
        rect = _parse_sym_rect(shape, 0, 0)
        assert rect is None


class TestParseSymCircle:
    """Tests for _parse_sym_circle function."""

    def test_parse_basic_circle(self):
        shape = "E~100~200~25~25~0~0"
        circle = _parse_sym_circle(shape, 0, 0)
        assert circle.cx == mil_to_mm(100)
        assert circle.cy == -mil_to_mm(200)
        assert circle.radius == mil_to_mm(25)

    def test_parse_circle_invalid(self):
        shape = "E~abc~def~ghi"
        circle = _parse_sym_circle(shape, 0, 0)
        assert circle is None


class TestParseSymPolyline:
    """Tests for _parse_sym_polyline function."""

    def test_parse_polyline_tilde_separated(self):
        shape = "PL~100~100~200~200~0~3"
        poly = _parse_sym_polyline(shape, 0, 0)
        assert poly.closed is False
        assert len(poly.points) == 2

    def test_parse_polygon(self):
        shape = "PG~100~100~200~200~100~200~0~3"
        poly = _parse_sym_polyline(shape, 0, 0)
        assert poly.closed is True
        assert poly.fill is True

    def test_parse_polyline_too_few_points(self):
        shape = "PL~100~0~3"  # Only one coord
        poly = _parse_sym_polyline(shape, 0, 0)
        assert poly is None


class TestParseSymArc:
    """Tests for _parse_sym_arc function."""

    def test_parse_basic_arc(self):
        shape = "A~0~0~M 100 200 A 50 50 0 0 1 150 250"
        arc = _parse_sym_arc(shape, 0, 0)
        assert arc is not None
        assert arc.width == 0.254  # Default symbol arc width

    def test_parse_arc_no_path(self):
        shape = "A~0~0~not_a_path"
        arc = _parse_sym_arc(shape, 0, 0)
        assert arc is None

    def test_parse_arc_sweep_flipped(self):
        """Symbol arcs must flip sweep direction because Y-axis is inverted."""
        # SVG arc with sweep=1 should become sweep=0 in KiCad (Y-inverted)
        shape = "A~0~0~M 100 200 A 50 50 0 0 1 150 250"
        arc = _parse_sym_arc(shape, 0, 0)
        assert arc.sweep == 0  # flipped from SVG sweep=1

        # SVG arc with sweep=0 should become sweep=1 in KiCad
        shape = "A~0~0~M 100 200 A 50 50 0 0 0 150 250"
        arc = _parse_sym_arc(shape, 0, 0)
        assert arc.sweep == 1  # flipped from SVG sweep=0


class TestParsePin:
    """Tests for _parse_pin function."""

    def test_parse_basic_pin(self):
        # Real format: visible~x~y~rotation~text~alignment~...
        shape = "P~show~0~1~400~300~0~id1^^section1^^M400,300h10^^1~0~0~0~TestPin~start^^1~0~0~0~1~end"
        pin = _parse_pin(shape, 400, 300)
        assert pin.number == "1"
        assert pin.name == "TestPin"
        assert pin.electrical_type == "unspecified"

    def test_parse_pin_with_input_type(self):
        shape = "P~show~1~2~400~300~90~id1^^section1^^M400,300v10^^1~0~0~0~IN~start^^1~0~0~0~2~end"
        pin = _parse_pin(shape, 400, 300)
        assert pin.number == "2"
        assert pin.electrical_type == "input"
        assert pin.rotation == 270  # (90 + 180) % 360 - EasyEDA points inward, KiCad outward

    def test_parse_pin_output_type(self):
        shape = "P~show~2~3~400~300~180~id1^^section1^^M400,300h-10^^1~0~0~0~OUT~start^^1~0~0~0~3~end"
        pin = _parse_pin(shape, 400, 300)
        assert pin.electrical_type == "output"

    def test_parse_pin_bidirectional_type(self):
        shape = "P~show~3~4~400~300~0~id1^^section1^^M400,300h10^^1~0~0~0~IO~start^^1~0~0~0~4~end"
        pin = _parse_pin(shape, 400, 300)
        assert pin.electrical_type == "bidirectional"

    def test_parse_pin_power_type(self):
        shape = "P~show~4~5~400~300~270~id1^^section1^^M400,300v-10^^1~0~0~0~VCC~start^^1~0~0~0~5~end"
        pin = _parse_pin(shape, 400, 300)
        assert pin.electrical_type == "power_in"

    def test_parse_pin_hidden_name(self):
        shape = "P~show~0~1~400~300~0~id1^^section1^^M400,300h10^^0~0~0~0~HiddenName~start^^1~0~0~0~1~end"
        pin = _parse_pin(shape, 400, 300)
        assert pin.name_visible is False

    def test_parse_pin_hidden_number(self):
        shape = "P~show~0~1~400~300~0~id1^^section1^^M400,300h10^^1~0~0~0~Name~start^^0~0~0~0~1~end"
        pin = _parse_pin(shape, 400, 300)
        assert pin.number_visible is False

    def test_parse_pin_y_inversion(self):
        """Pin Y coordinates must be negated (Y-axis inversion from EasyEDA to KiCad)."""
        # Pin at y=350, origin at y=300 → kicad_y should be -mil_to_mm(50) (negative)
        shape = "P~show~0~1~400~350~0~id1^^section1^^M400,350h10^^1~0~0~0~TestPin~start^^1~0~0~0~1~end"
        pin = _parse_pin(shape, 400, 300)
        assert pin.y < 0  # Y must be negated: -(350-300) mapped to mm is negative
        assert pin.y == -mil_to_mm(50)

    def test_parse_pin_invalid(self):
        shape = "P~invalid"
        pin = _parse_pin(shape, 0, 0)
        assert pin is None

    def test_parse_pin_vertical_length(self):
        """Test pin length calculation from vertical path."""
        shape = "P~show~0~1~400~300~90~id1^^section1^^M 400 300 v 20^^1~0~0~0~0~Name^^1~0~0~0~0~1"
        pin = _parse_pin(shape, 400, 300)
        assert pin.length == mil_to_mm(20)


class TestParseFootprintShapesExtended:
    """Extended tests for parse_footprint_shapes."""

    def test_parse_arc(self):
        shapes = ["ARC~2~3~id1~M 100 100 A 50 50 0 0 1 150 150"]
        fp = parse_footprint_shapes(shapes, 0, 0)
        assert len(fp.arcs) == 1

    def test_parse_solid_region(self):
        shapes = ["SOLIDREGION~10~id1~M 0 0 L 100 0 L 100 100 Z~npth"]
        fp = parse_footprint_shapes(shapes, 0, 0)
        assert len(fp.regions) == 1

    def test_parse_svgnode_model(self):
        attrs = {"uuid": "model123", "c_origin": "0,0", "z": "0", "c_rotation": "0,0,0"}
        shapes = [f"SVGNODE~{json.dumps({'attrs': attrs})}"]
        fp = parse_footprint_shapes(shapes, 0, 0)
        assert fp.model is not None
        assert fp.model.uuid == "model123"

    def test_parse_rect(self):
        shapes = ["RECT~100~100~50~50~3~~~2"]
        fp = parse_footprint_shapes(shapes, 0, 0)
        assert len(fp.tracks) == 1

    def test_origin_offset_on_arcs(self):
        shapes = ["ARC~2~3~id1~M 100 100 A 50 50 0 0 1 150 150"]
        fp = parse_footprint_shapes(shapes, 50, 50)
        arc = fp.arcs[0]
        # Start should be offset
        expected_x = mil_to_mm(100) - mil_to_mm(50)
        assert abs(arc.start[0] - expected_x) < 0.001

    def test_origin_offset_on_regions(self):
        shapes = ["SOLIDREGION~10~id1~M 100 100 L 200 100 L 200 200 Z~npth"]
        fp = parse_footprint_shapes(shapes, 50, 50)
        region = fp.regions[0]
        expected_x = mil_to_mm(100) - mil_to_mm(50)
        assert abs(region.points[0][0] - expected_x) < 0.001


class TestParseSymbolShapesExtended:
    """Extended tests for parse_symbol_shapes."""

    def test_parse_arc(self):
        shapes = ["A~0~0~M 100 100 A 50 50 0 0 1 150 150"]
        sym = parse_symbol_shapes(shapes, 0, 0)
        assert len(sym.arcs) == 1

    def test_parse_c_circle(self):
        """Verify C~ circle shapes are parsed through parse_symbol_shapes."""
        shapes = ["C~200~300~50~#880000~2~#880000~gge1~0~"]
        sym = parse_symbol_shapes(shapes, 0, 0)
        assert len(sym.circles) == 1
        assert sym.circles[0].radius == mil_to_mm(50)

    def test_parse_text_font_size_conversion(self):
        """T~ text font size in pt must be converted to mm (× 0.3528)."""
        # Format: T~type~x~y~rotation~color~font~size~?~?~anchor~?~text~...
        shapes = ["T~L~400~300~0~#0000FF~Tahoma~10pt~0.1~~middle~comment~Hello~1~middle~gge1~0~"]
        sym = parse_symbol_shapes(shapes, 400, 300)
        assert len(sym.texts) == 1
        # 10pt * 0.3528 = 3.528 mm
        expected = 10.0 * 0.3528
        assert abs(sym.texts[0].font_size - expected) < 0.01, (
            f"Font size should be {expected} mm but got {sym.texts[0].font_size}"
        )


class TestComputeArcMidpointExtended:
    """Extended tests for compute_arc_midpoint."""

    def test_arc_with_sweep_zero(self):
        """Test arc with sweep=0 (counter-clockwise)."""
        mid = compute_arc_midpoint((0, 0), (2, 0), 2, 2, 0, 0)
        # Midpoint should be on the arc, not at the midpoint of the chord
        assert mid is not None
        # x should be between the endpoints
        assert 0 <= mid[0] <= 2
        # Midpoint shouldn't be on the chord (y != 0)
        assert mid[1] != 0

    def test_arc_large_arc_sweep_same(self):
        """Test when large_arc == sweep."""
        mid = compute_arc_midpoint((0, 0), (2, 0), 2, 2, 1, 1)
        # Midpoint should be roughly in the middle (x around 1)
        assert mid is not None
        assert 0 < mid[0] < 2  # x should be between endpoints

    def test_arc_small_radius_clamped(self):
        """Test when radius is smaller than half chord length."""
        # Chord from (0,0) to (10,0) = length 10, radius 3 < 5
        # Radius gets clamped to chord/2 = 5
        mid = compute_arc_midpoint((0, 0), (10, 0), 3, 3, 0, 1)
        assert mid is not None
        # Midpoint x should be between endpoints
        assert 0 <= mid[0] <= 10


class TestLayerMap:
    """Tests for LAYER_MAP constant."""

    def test_front_copper(self):
        assert LAYER_MAP["1"] == "F.Cu"

    def test_back_copper(self):
        assert LAYER_MAP["2"] == "B.Cu"

    def test_front_silkscreen(self):
        assert LAYER_MAP["3"] == "F.SilkS"

    def test_edge_cuts(self):
        assert LAYER_MAP["10"] == "Edge.Cuts"
