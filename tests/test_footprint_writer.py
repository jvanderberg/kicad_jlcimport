"""Tests for footprint_writer.py - KiCad footprint generation."""

from kicad_jlcimport.easyeda.ee_types import EEArc, EECircle, EEFootprint, EEHole, EEPad, EESolidRegion, EETrack
from kicad_jlcimport.kicad.footprint_writer import (
    _COURTYARD_CLEARANCE,
    _COURTYARD_CLEARANCE_SMALL,
    _COURTYARD_GRID,
    _arc_bounds,
    _compute_courtyard,
    _convex_hull,
    _offset_hull,
    _pad_corners,
    write_footprint,
)
from kicad_jlcimport.kicad.version import KICAD_V8, KICAD_V9


def _make_footprint(**kwargs):
    """Create an EEFootprint with optional components."""
    fp = EEFootprint()
    if "pads" in kwargs:
        fp.pads = kwargs["pads"]
    if "tracks" in kwargs:
        fp.tracks = kwargs["tracks"]
    if "circles" in kwargs:
        fp.circles = kwargs["circles"]
    if "holes" in kwargs:
        fp.holes = kwargs["holes"]
    if "arcs" in kwargs:
        fp.arcs = kwargs["arcs"]
    return fp


class TestWriteFootprint:
    def test_minimal_footprint(self):
        fp = _make_footprint()
        result = write_footprint(fp, "Test")
        assert '(footprint "Test"' in result
        assert "(version 20241229)" in result
        assert '(generator "JLCImport")' in result
        assert "(embedded_fonts no)" in result
        assert result.endswith(")\n")

    def test_smd_attribute(self):
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert "(attr smd)" in result

    def test_tht_attribute(self):
        pad = EEPad(shape="OVAL", x=0, y=0, width=1, height=1, layer="11", number="1", drill=0.8)
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert "(attr through_hole)" in result

    def test_pad_generation(self):
        pad = EEPad(shape="RECT", x=1.0, y=2.0, width=0.5, height=0.8, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert '(pad "1" smd rect' in result
        assert "(size" in result
        assert "(layers" in result

    def test_pad_with_drill(self):
        pad = EEPad(shape="OVAL", x=0, y=0, width=1.5, height=1.5, layer="11", number="A1", drill=1.0)
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert "(drill" in result
        assert '(pad "A1" thru_hole' in result

    def test_oval_pad_shape(self):
        """OVAL pads must produce 'oval' shape in KiCad output, not 'rect'."""
        pad = EEPad(shape="OVAL", x=0, y=0, width=1.5, height=0.8, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert "smd oval" in result

    def test_pad_with_rotation(self):
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=2, layer="1", number="1", drill=0, rotation=45.0)
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert "45" in result

    def test_polygon_pad_emits_primitives(self):
        """POLYGON pad with polygon_points should emit gr_poly primitives."""
        pad = EEPad(
            shape="POLYGON",
            x=0,
            y=0,
            width=1,
            height=1,
            layer="1",
            number="1",
            drill=0,
            polygon_points=[-0.5, -0.5, 0.5, -0.5, 0.0, 0.5],
        )
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert "smd custom" in result
        assert "(primitives" in result
        assert "(gr_poly" in result
        assert "(fill yes)" in result

    def test_polygon_pad_omits_rotation(self):
        """Custom polygon pad should not include rotation in (at ...) to avoid double-rotation."""
        pad = EEPad(
            shape="POLYGON",
            x=1.0,
            y=2.0,
            width=1,
            height=1,
            layer="1",
            number="1",
            drill=0,
            rotation=90.0,
            polygon_points=[-0.5, -0.5, 0.5, -0.5, 0.0, 0.5],
        )
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        # Should use (at x y) without rotation, not (at x y 90)
        assert "(at 1 2)" in result
        assert "(at 1 2 90)" not in result

    def test_polygon_pad_without_data_falls_back_to_rect(self):
        """POLYGON pad with no polygon_points should fall back to rect shape."""
        pad = EEPad(
            shape="POLYGON",
            x=0,
            y=0,
            width=1,
            height=1,
            layer="1",
            number="1",
            drill=0,
            polygon_points=[],
        )
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert "smd rect" in result
        assert "(primitives" not in result

    def test_track_generation(self):
        track = EETrack(width=0.2, layer="F.SilkS", points=[(0, 0), (1, 0), (1, 1)])
        fp = _make_footprint(tracks=[track])
        result = write_footprint(fp, "Test")
        assert "(fp_line" in result
        # Two silkscreen segments for 3 points
        silk_lines = [line for line in result.split("\n") if "(fp_line" in line and "F.SilkS" in line]
        assert len(silk_lines) == 2

    def test_circle_generation(self):
        circle = EECircle(cx=0, cy=0, radius=2.0, width=0.15, layer="F.SilkS")
        fp = _make_footprint(circles=[circle])
        result = write_footprint(fp, "Test")
        assert "(fp_circle" in result
        assert "(center" in result
        assert "(end" in result

    def test_hole_generation(self):
        hole = EEHole(x=0, y=0, radius=0.5)
        fp = _make_footprint(holes=[hole])
        result = write_footprint(fp, "Test")
        assert "np_thru_hole" in result
        assert "(drill" in result

    def test_hole_diameter_is_twice_radius(self):
        """NPTH holes must use diameter (radius*2) for size and drill."""
        hole = EEHole(x=0, y=0, radius=0.75)
        fp = _make_footprint(holes=[hole])
        result = write_footprint(fp, "Test")
        # diameter = 0.75 * 2 = 1.5
        assert "(size 1.5 1.5)" in result
        assert "(drill 1.5)" in result

    def test_properties_added(self):
        fp = _make_footprint()
        result = write_footprint(
            fp, "Test", lcsc_id="C123456", description="A test part", datasheet="https://example.com/ds.pdf"
        )
        assert '(property "LCSC" "C123456"' in result
        assert '(property "Description"' in result
        assert '(property "Datasheet"' in result

    def test_model_reference(self):
        fp = _make_footprint()
        result = write_footprint(
            fp,
            "Test",
            model_path="${KIPRJMOD}/lib.3dshapes/Test.step",
            model_offset=(1.0, 2.0, 3.0),
            model_rotation=(0, 0, 90),
        )
        assert '(model "${KIPRJMOD}/lib.3dshapes/Test.step"' in result
        assert "(offset" in result
        assert "(rotate" in result

    def test_special_chars_escaped(self):
        fp = _make_footprint()
        result = write_footprint(fp, "Test", description='Has "quotes" and\nnewlines')
        assert '\\"quotes\\"' in result
        assert "\n" not in result.split("Description")[1].split(")")[0]

    def test_uuid_uniqueness(self):
        pad1 = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0)
        pad2 = EEPad(shape="RECT", x=2, y=0, width=1, height=1, layer="1", number="2", drill=0)
        fp = _make_footprint(pads=[pad1, pad2])
        result = write_footprint(fp, "Test")
        # Extract all UUIDs
        import re

        uuids = re.findall(r'uuid "([^"]+)"', result)
        assert len(uuids) == len(set(uuids))  # All unique

    def test_back_copper_pad(self):
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="2", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert '"B.Cu"' in result
        assert '"B.Mask"' in result
        assert '"B.Paste"' in result

    def test_oval_slot_drill_vertical(self):
        """Vertical slot pad (height >= width) should emit drill oval W H."""
        pad = EEPad(
            shape="OVAL",
            x=0,
            y=0,
            width=1.1,
            height=2.0,
            layer="11",
            number="1",
            drill=0.6,
            slot_length=1.5,
        )
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert "(drill oval 0.6 1.5)" in result

    def test_oval_slot_drill_horizontal(self):
        """Horizontal slot pad (width > height) should emit drill oval H W."""
        pad = EEPad(
            shape="OVAL",
            x=0,
            y=0,
            width=2.0,
            height=1.1,
            layer="11",
            number="1",
            drill=0.6,
            slot_length=1.5,
        )
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert "(drill oval 1.5 0.6)" in result

    def test_circular_drill_no_slot(self):
        """Pad with slot_length=0 should emit plain circular drill."""
        pad = EEPad(
            shape="OVAL",
            x=0,
            y=0,
            width=1.5,
            height=1.5,
            layer="11",
            number="1",
            drill=0.8,
        )
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        assert "(drill 0.8)" in result
        assert "oval" not in result.split("drill")[1].split(")")[0]

    def test_arc_sweep_zero_swaps_start_end(self):
        """When sweep==0, arc start/end must be swapped in KiCad output."""
        arc = EEArc(
            width=0.2,
            layer="F.SilkS",
            start=(1.0, 2.0),
            end=(3.0, 4.0),
            rx=5.0,
            ry=5.0,
            large_arc=0,
            sweep=0,
        )
        fp = _make_footprint(arcs=[arc])
        result = write_footprint(fp, "Test")
        assert "(fp_arc" in result
        # With sweep==0, start/end should be swapped: output start=(3,4), end=(1,2)
        start_idx = result.index("(start")
        end_idx = result.index("(end")
        start_section = result[start_idx : start_idx + 30]
        end_section = result[end_idx : end_idx + 30]
        # The output start should contain the original end coords (3.0, 4.0)
        assert "3" in start_section
        assert "4" in start_section
        # The output end should contain the original start coords (1.0, 2.0)
        assert "1" in end_section
        assert "2" in end_section


class TestWriteFootprintVersions:
    def test_v9_footprint(self):
        fp = _make_footprint()
        result = write_footprint(fp, "Test", kicad_version=KICAD_V9)
        assert "(version 20241229)" in result
        assert '(generator_version "1.0")' in result
        assert "(embedded_fonts no)" in result

    def test_v8_footprint_version(self):
        fp = _make_footprint()
        result = write_footprint(fp, "Test", kicad_version=KICAD_V8)
        assert "(version 20240108)" in result

    def test_v8_footprint_no_generator_version(self):
        fp = _make_footprint()
        result = write_footprint(fp, "Test", kicad_version=KICAD_V8)
        assert "generator_version" not in result

    def test_v8_footprint_no_embedded_fonts(self):
        fp = _make_footprint()
        result = write_footprint(fp, "Test", kicad_version=KICAD_V8)
        assert "embedded_fonts" not in result

    def test_v8_footprint_has_generator(self):
        fp = _make_footprint()
        result = write_footprint(fp, "Test", kicad_version=KICAD_V8)
        assert '(generator "JLCImport")' in result

    def test_v8_footprint_still_valid(self):
        """Verify v8 footprint has basic structure."""
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test", kicad_version=KICAD_V8)
        assert '(footprint "Test"' in result
        assert '(pad "1" smd rect' in result
        assert result.endswith(")\n")


class TestConvexHull:
    """Tests for the convex hull algorithm."""

    def test_triangle(self):
        pts = [(0.0, 0.0), (4.0, 0.0), (2.0, 3.0)]
        hull = _convex_hull(pts)
        assert len(hull) == 3

    def test_square(self):
        pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        hull = _convex_hull(pts)
        assert len(hull) == 4

    def test_interior_points_excluded(self):
        """Points inside the hull should not appear in the result."""
        pts = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (2.0, 2.0)]
        hull = _convex_hull(pts)
        assert len(hull) == 4
        assert (2.0, 2.0) not in hull

    def test_collinear_points(self):
        """Collinear points should produce a degenerate hull."""
        pts = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
        hull = _convex_hull(pts)
        assert len(hull) <= 2

    def test_ccw_winding(self):
        """Hull should be in counter-clockwise order."""
        pts = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        hull = _convex_hull(pts)
        # Cross product of consecutive edges should be positive (CCW)
        n = len(hull)
        for i in range(n):
            o = hull[i]
            a = hull[(i + 1) % n]
            b = hull[(i + 2) % n]
            cross = (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
            assert cross >= 0


class TestOffsetHull:
    """Tests for convex hull outward offset."""

    def test_square_offset(self):
        """Offsetting a unit square by 0.25 should expand it by 0.25 on each side."""
        hull = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        result = _offset_hull(hull, 0.25)
        assert len(result) == 4
        xs = [p[0] for p in result]
        ys = [p[1] for p in result]
        assert min(xs) < 0
        assert max(xs) > 1
        assert min(ys) < 0
        assert max(ys) > 1

    def test_offset_preserves_vertex_count(self):
        hull = [(0.0, 0.0), (3.0, 0.0), (3.0, 2.0), (1.0, 3.0), (0.0, 2.0)]
        result = _offset_hull(hull, 0.5)
        assert len(result) == len(hull)


class TestPadCorners:
    """Tests for pad corner computation with rotation."""

    def test_no_rotation(self):
        pad = EEPad(shape="RECT", x=0, y=0, width=2.0, height=1.0, layer="1", number="1", drill=0)
        corners = _pad_corners(pad)
        assert len(corners) == 4
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        assert min(xs) == -1.0
        assert max(xs) == 1.0
        assert min(ys) == -0.5
        assert max(ys) == 0.5

    def test_90_degree_rotation(self):
        """90° rotation should swap width and height."""
        pad = EEPad(shape="RECT", x=0, y=0, width=2.0, height=1.0, layer="1", number="1", drill=0, rotation=90.0)
        corners = _pad_corners(pad)
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        assert abs(min(xs) - (-0.5)) < 1e-10
        assert abs(max(xs) - 0.5) < 1e-10
        assert abs(min(ys) - (-1.0)) < 1e-10
        assert abs(max(ys) - 1.0) < 1e-10

    def test_offset_pad(self):
        """Pad at non-origin position should have offset corners."""
        pad = EEPad(shape="RECT", x=5.0, y=3.0, width=2.0, height=1.0, layer="1", number="1", drill=0)
        corners = _pad_corners(pad)
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        assert min(xs) == 4.0
        assert max(xs) == 6.0
        assert min(ys) == 2.5
        assert max(ys) == 3.5


class TestCourtyard:
    """Tests for courtyard generation from pads and holes."""

    def test_no_geometry_returns_none(self):
        fp = _make_footprint()
        assert _compute_courtyard(fp) is None

    def test_no_courtyard_for_empty_footprint(self):
        fp = _make_footprint()
        result = write_footprint(fp, "Test")
        assert "F.CrtYd" not in result

    def test_tracks_only_returns_none(self):
        """Tracks (silkscreen) should not generate a courtyard."""
        track = EETrack(width=0.2, layer="F.SilkS", points=[(-3, 0), (3, 0)])
        fp = _make_footprint(tracks=[track])
        assert _compute_courtyard(fp) is None

    def test_circles_only_returns_none(self):
        """Silkscreen circles should not generate a courtyard."""
        circle = EECircle(cx=0, cy=0, radius=2.0, width=0.15, layer="F.SilkS")
        fp = _make_footprint(circles=[circle])
        assert _compute_courtyard(fp) is None

    def test_arcs_only_returns_none(self):
        """Silkscreen arcs should not generate a courtyard."""
        arc = EEArc(width=0.2, layer="F.SilkS", start=(5.0, 0.0), end=(-5.0, 0.0), rx=5.0, ry=5.0, large_arc=0, sweep=1)
        fp = _make_footprint(arcs=[arc])
        assert _compute_courtyard(fp) is None

    def test_regions_only_returns_none(self):
        """Solid regions (silkscreen) should not generate a courtyard."""
        region = EESolidRegion(layer="F.SilkS", points=[(-1, -1), (1, -1), (1, 1), (-1, 1)], region_type="solid")
        fp = EEFootprint(regions=[region])
        assert _compute_courtyard(fp) is None

    def test_courtyard_from_pads(self):
        """Courtyard polygon should encompass pads with clearance."""
        pad = EEPad(shape="RECT", x=0, y=0, width=2.0, height=1.0, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        poly = _compute_courtyard(fp)
        assert poly is not None
        assert len(poly) >= 3
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        # Pad extents: x=[-1, 1], y=[-0.5, 0.5] — height 1.0mm < 1.5mm → small clearance
        assert min(xs) <= -1.0 - _COURTYARD_CLEARANCE_SMALL
        assert min(ys) <= -0.5 - _COURTYARD_CLEARANCE_SMALL
        assert max(xs) >= 1.0 + _COURTYARD_CLEARANCE_SMALL
        assert max(ys) >= 0.5 + _COURTYARD_CLEARANCE_SMALL

    def test_courtyard_from_holes(self):
        hole = EEHole(x=0, y=0, radius=1.5)
        fp = _make_footprint(holes=[hole])
        poly = _compute_courtyard(fp)
        assert poly is not None
        xs = [p[0] for p in poly]
        assert min(xs) <= -(1.5 + _COURTYARD_CLEARANCE)
        assert max(xs) >= 1.5 + _COURTYARD_CLEARANCE

    def test_courtyard_grid_alignment(self):
        """All courtyard vertex coordinates must snap to the 0.05mm grid."""
        pad = EEPad(shape="RECT", x=0.123, y=0.456, width=1.0, height=1.0, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        poly = _compute_courtyard(fp)
        assert poly is not None
        for x, y in poly:
            for val in (x, y):
                remainder = round(val / _COURTYARD_GRID, 6) % 1
                assert remainder < 1e-6 or remainder > 1 - 1e-6, f"{val} not on {_COURTYARD_GRID}mm grid"

    def test_courtyard_clearance_expands_outward(self):
        """Courtyard extents must be at least clearance beyond pad extents."""
        pad = EEPad(shape="RECT", x=0, y=0, width=4.0, height=2.0, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        poly = _compute_courtyard(fp)
        assert poly is not None
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        assert min(xs) <= -2.0 - _COURTYARD_CLEARANCE
        assert min(ys) <= -1.0 - _COURTYARD_CLEARANCE
        assert max(xs) >= 2.0 + _COURTYARD_CLEARANCE
        assert max(ys) >= 1.0 + _COURTYARD_CLEARANCE

    def test_small_part_uses_reduced_clearance(self):
        """Parts smaller than 1.5mm in any dimension get 0.15mm clearance (KLC F5.3)."""
        pad = EEPad(shape="RECT", x=0, y=0, width=1.0, height=0.5, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        poly = _compute_courtyard(fp)
        assert poly is not None
        xs = [p[0] for p in poly]
        assert min(xs) <= -0.5 - _COURTYARD_CLEARANCE_SMALL
        assert max(xs) >= 0.5 + _COURTYARD_CLEARANCE_SMALL
        # Should NOT expand as far as standard clearance would
        assert min(xs) > -0.5 - _COURTYARD_CLEARANCE - _COURTYARD_GRID
        assert max(xs) < 0.5 + _COURTYARD_CLEARANCE + _COURTYARD_GRID

    def test_standard_part_uses_full_clearance(self):
        """Parts >= 1.5mm in both dimensions get standard 0.25mm clearance."""
        pad = EEPad(shape="RECT", x=0, y=0, width=4.0, height=3.0, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        poly = _compute_courtyard(fp)
        assert poly is not None
        xs = [p[0] for p in poly]
        assert min(xs) <= -2.0 - _COURTYARD_CLEARANCE
        assert max(xs) >= 2.0 + _COURTYARD_CLEARANCE

    def test_small_in_one_dimension_uses_reduced_clearance(self):
        """If only one dimension is under 1.5mm, still use reduced clearance."""
        pad = EEPad(shape="RECT", x=0, y=0, width=3.0, height=1.0, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        poly = _compute_courtyard(fp)
        assert poly is not None
        ys = [p[1] for p in poly]
        assert min(ys) <= -0.5 - _COURTYARD_CLEARANCE_SMALL
        assert min(ys) > -0.5 - _COURTYARD_CLEARANCE - _COURTYARD_GRID

    def test_write_footprint_emits_courtyard_poly(self):
        """write_footprint should emit an fp_poly on F.CrtYd."""
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        crtyd_lines = [line for line in result.split("\n") if "F.CrtYd" in line]
        assert len(crtyd_lines) == 1
        assert "(fp_poly" in crtyd_lines[0]
        assert "(fill none)" in crtyd_lines[0]
        assert f"(stroke (width {0.05})" in crtyd_lines[0]

    def test_courtyard_before_pads_in_output(self):
        """Courtyard must appear before pad definitions."""
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0)
        fp = _make_footprint(pads=[pad])
        result = write_footprint(fp, "Test")
        crtyd_pos = result.index("F.CrtYd")
        pad_pos = result.index('(pad "1"')
        assert crtyd_pos < pad_pos

    def test_tracks_ignored_in_courtyard(self):
        """Courtyard should only use pad extents, ignoring silkscreen tracks."""
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0)
        track = EETrack(width=0.1, layer="F.SilkS", points=[(0, 0), (20, 0)])
        fp = _make_footprint(pads=[pad], tracks=[track])
        poly = _compute_courtyard(fp)
        assert poly is not None
        xs = [p[0] for p in poly]
        # Courtyard should be based on pad (±0.5), not track (up to 20)
        assert max(xs) < 5.0

    def test_l_shaped_pads_tighter_than_bbox(self):
        """L-shaped pad arrangement should produce a tighter hull than a bounding box."""
        # Two pads forming an L: one at bottom-left, one at top-right
        pad1 = EEPad(shape="RECT", x=-5, y=5, width=2, height=2, layer="1", number="1", drill=0)
        pad2 = EEPad(shape="RECT", x=5, y=-5, width=2, height=2, layer="1", number="2", drill=0)
        fp = _make_footprint(pads=[pad1, pad2])
        poly = _compute_courtyard(fp)
        assert poly is not None
        # The convex hull should have more than 4 vertices for an L-shape
        # (a bounding box would be 4 vertices)
        assert len(poly) > 4

    def test_rotated_pad_courtyard(self):
        """Courtyard should account for pad rotation."""
        # 4x1 pad rotated 45°: extends further in both axes than non-rotated
        pad = EEPad(shape="RECT", x=0, y=0, width=4.0, height=1.0, layer="1", number="1", drill=0, rotation=45.0)
        fp = _make_footprint(pads=[pad])
        poly = _compute_courtyard(fp)
        assert poly is not None
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        # Non-rotated extent would be x=±2, y=±0.5
        # At 45°, the diagonal extent is about ±(4*cos45 + 1*sin45)/2 ≈ ±1.77
        assert max(xs) > 1.5
        assert max(ys) > 1.5

    def test_arc_bounds_degenerate(self):
        """Degenerate arc (start==end) should still return valid bounds."""
        arc = EEArc(
            width=0.2,
            layer="F.SilkS",
            start=(1.0, 2.0),
            end=(1.0, 2.0),
            rx=5.0,
            ry=5.0,
            large_arc=0,
            sweep=1,
        )
        bx1, by1, bx2, by2 = _arc_bounds(arc)
        assert bx1 < bx2
        assert by1 < by2


def _segments_intersect(p1, p2, p3, p4):
    """Return True if line segment p1-p2 intersects p3-p4 (excluding shared endpoints)."""
    d1 = (p4[0] - p3[0]) * (p1[1] - p3[1]) - (p4[1] - p3[1]) * (p1[0] - p3[0])
    d2 = (p4[0] - p3[0]) * (p2[1] - p3[1]) - (p4[1] - p3[1]) * (p2[0] - p3[0])
    d3 = (p2[0] - p1[0]) * (p3[1] - p1[1]) - (p2[1] - p1[1]) * (p3[0] - p1[0])
    d4 = (p2[0] - p1[0]) * (p4[1] - p1[1]) - (p2[1] - p1[1]) * (p4[0] - p1[0])
    if d1 * d2 < 0 and d3 * d4 < 0:
        return True
    return False


def _polygon_no_self_intersection(poly):
    """Return True if the closed polygon has no self-intersecting edges."""
    n = len(poly)
    edges = [(poly[i], poly[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # adjacent edges (first & last share a vertex)
            if _segments_intersect(edges[i][0], edges[i][1], edges[j][0], edges[j][1]):
                return False
    return True


def _point_in_polygon(point, poly):
    """Ray-casting algorithm: return True if point is inside the closed polygon."""
    x, y = point
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


class TestCourtyardGeometricValidity:
    """Verify courtyard polygon is non-self-intersecting, enclosed, and fully contains all copper."""

    def _assert_valid_courtyard(self, fp):
        """Run all geometric checks on a footprint's courtyard."""
        poly = _compute_courtyard(fp)
        assert poly is not None, "Expected courtyard but got None"
        assert len(poly) >= 3, f"Courtyard must have >= 3 vertices, got {len(poly)}"

        # 1. No self-intersection
        assert _polygon_no_self_intersection(poly), "Courtyard polygon self-intersects"

        # 2. All pad corners must be inside the courtyard
        for pad in fp.pads:
            for corner in _pad_corners(pad):
                assert _point_in_polygon(corner, poly), f"Pad {pad.number} corner {corner} is outside the courtyard"

        # 3. All hole perimeter points must be inside the courtyard
        import math as m

        for hole in fp.holes:
            for k in range(16):
                a = k * m.pi / 8
                pt = (hole.x + hole.radius * m.cos(a), hole.y + hole.radius * m.sin(a))
                assert _point_in_polygon(pt, poly), f"Hole perimeter point {pt} is outside the courtyard"

        return poly

    def test_single_pad(self):
        pad = EEPad(shape="RECT", x=0, y=0, width=2.0, height=1.0, layer="1", number="1", drill=0)
        self._assert_valid_courtyard(_make_footprint(pads=[pad]))

    def test_two_pads_inline(self):
        """Simple resistor-like footprint: two pads in a line."""
        pad1 = EEPad(shape="RECT", x=-1.5, y=0, width=1.0, height=0.8, layer="1", number="1", drill=0)
        pad2 = EEPad(shape="RECT", x=1.5, y=0, width=1.0, height=0.8, layer="1", number="2", drill=0)
        self._assert_valid_courtyard(_make_footprint(pads=[pad1, pad2]))

    def test_qfp_style_four_sides(self):
        """QFP-like footprint with pads on 4 sides."""
        pads = []
        n = 1
        for i in range(5):
            offset = -2 + i
            pads.append(EEPad(shape="RECT", x=offset, y=-4, width=0.5, height=1.5, layer="1", number=str(n), drill=0))
            n += 1
            pads.append(EEPad(shape="RECT", x=offset, y=4, width=0.5, height=1.5, layer="1", number=str(n), drill=0))
            n += 1
            pads.append(EEPad(shape="RECT", x=-4, y=offset, width=1.5, height=0.5, layer="1", number=str(n), drill=0))
            n += 1
            pads.append(EEPad(shape="RECT", x=4, y=offset, width=1.5, height=0.5, layer="1", number=str(n), drill=0))
            n += 1
        self._assert_valid_courtyard(_make_footprint(pads=pads))

    def test_l_shaped_pads(self):
        """L-shaped arrangement — convex hull is not a simple rectangle."""
        pad1 = EEPad(shape="RECT", x=-5, y=5, width=2, height=2, layer="1", number="1", drill=0)
        pad2 = EEPad(shape="RECT", x=5, y=-5, width=2, height=2, layer="1", number="2", drill=0)
        self._assert_valid_courtyard(_make_footprint(pads=[pad1, pad2]))

    def test_rotated_pad(self):
        pad = EEPad(shape="RECT", x=0, y=0, width=4.0, height=1.0, layer="1", number="1", drill=0, rotation=45.0)
        self._assert_valid_courtyard(_make_footprint(pads=[pad]))

    def test_mixed_pads_and_holes(self):
        """Pads and NPTH holes must all be contained."""
        pad = EEPad(shape="RECT", x=0, y=0, width=2, height=2, layer="1", number="1", drill=0)
        hole1 = EEHole(x=-3, y=0, radius=1.0)
        hole2 = EEHole(x=3, y=0, radius=1.0)
        self._assert_valid_courtyard(_make_footprint(pads=[pad], holes=[hole1, hole2]))

    def test_holes_only(self):
        """Mounting holes with no pads should still produce a valid courtyard."""
        hole1 = EEHole(x=-5, y=0, radius=1.5)
        hole2 = EEHole(x=5, y=0, radius=1.5)
        self._assert_valid_courtyard(_make_footprint(holes=[hole1, hole2]))

    def test_many_pads_scattered(self):
        """Stress test: many pads at various positions."""
        import math as m

        pads = []
        for i in range(12):
            angle = i * m.pi / 6
            x = 5 * m.cos(angle)
            y = 5 * m.sin(angle)
            pads.append(EEPad(shape="RECT", x=x, y=y, width=1, height=0.5, layer="1", number=str(i + 1), drill=0))
        self._assert_valid_courtyard(_make_footprint(pads=pads))
