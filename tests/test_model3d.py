"""Tests for model3d.py - 3D model placement, VRML conversion and saving."""

import glob
import json
import math
import os

import pytest

from kicad_jlcimport.easyeda.ee_types import EE3DModel, EEPad
from kicad_jlcimport.easyeda.parser import _svgnode_outline_bbox, parse_footprint_shapes
from kicad_jlcimport.kicad.model3d import (
    ANCHOR_C_ORIGIN,
    ANCHOR_FOOTPRINT,
    ANCHOR_NONE,
    ANCHOR_OUTLINE,
    PIN_FIT_TOLERANCE_MM,
    PINS_ALIGNED,
    PINS_NOT_CHECKED,
    PINS_OFFSET,
    PINS_UNMATCHED,
    _kicad_file_rotation,
    _kicad_rotation,
    _matmul,
    _parse_obj_mesh,
    _placed_vertices,
    _rot,
    compute_model_transform,
    convert_to_vrml,
    place_model,
    save_models,
)

TESTDATA_DIR = os.path.join(os.path.dirname(__file__), "..", "testdata")

MM = 1 / 0.254  # EasyEDA units per mm


# ---------------------------------------------------------------------------
# Synthetic geometry helpers
# ---------------------------------------------------------------------------


def _obj(*boxes):
    """OBJ text for axis-aligned boxes given as (x0, x1, y0, y1, z0, z1) in mm."""
    verts, faces = [], []
    for x0, x1, y0, y1, z0, z1 in boxes:
        base = len(verts)
        verts += [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
        for quad in ((0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)):
            faces.append(tuple(base + i + 1 for i in quad))
    lines = [f"v {x} {y} {z}" for x, y, z in verts] + ["usemtl m"]
    lines += ["f " + " ".join(str(i) for i in face) for face in faces]
    return "\n".join(lines)


def _model(origin=(0.0, 0.0), outline=None, z=0.0, c_rotation=(0.0, 0.0, 0.0)):
    """EE3DModel as the parser builds it (rotation is c_rotation negated)."""
    return EE3DModel(
        uuid="m",
        origin_x=origin[0],
        origin_y=origin[1],
        z=z,
        rotation=tuple(-a for a in c_rotation),
        outline=outline,
    )


def _outline_mm(cx, cy, w, h):
    """Outline bbox in EasyEDA units for a w x h mm box centred at (cx, cy) mm."""
    return ((cx - w / 2) * MM, (cx + w / 2) * MM, (cy - h / 2) * MM, (cy + h / 2) * MM)


def _tht_pad(x, y, number="1", size=1.6):
    return EEPad(shape="ELLIPSE", x=x, y=y, width=size, height=size, layer="11", number=number, drill=1.0)


def _smd_pad(x, y, w, h, number="1", layer="1"):
    return EEPad(shape="RECT", x=x, y=y, width=w, height=h, layer=layer, number=number, drill=0.0)


# Two 0.6 mm square pins at x = ±1.27 through the board, under a body.
TWO_PIN_PART = _obj(
    (-1.57, -0.97, -0.3, 0.3, -3.0, 1.0),
    (0.97, 1.57, -0.3, 0.3, -3.0, 1.0),
    (-2.5, 2.5, -1.0, 1.0, 1.0, 3.0),
)
TWO_PIN_OUTLINE = _outline_mm(0, 0, 5.0, 2.0)
TWO_PIN_Z = -3.0 * MM  # pin tips 3 mm below the board


def _approx(a, b, tol=1e-6):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Step 1: rotation
# ---------------------------------------------------------------------------


class TestRotation:
    @pytest.mark.parametrize("rz", [0, 90, -90, 180, 270, 45])
    def test_z_only_rotation_is_negated(self, rz):
        rot = _kicad_file_rotation((0.0, 0.0, -rz))
        expected = ((-rz + 180) % 360) - 180 or 0.0
        assert rot[0] == 0.0 and rot[1] == 0.0
        assert math.isclose((rot[2] - expected + 180) % 360 - 180, 0.0, abs_tol=1e-6)

    @pytest.mark.parametrize(
        "c_rotation",
        [(270, 0, 180), (90, 0, 0), (0, 90, 0), (30, 45, 60), (90, 90, 0), (180, 0, 90), (0, -90, 30)],
    )
    def test_kicad_rotation_matches_easyeda_composition(self, c_rotation):
        """KiCad's Rz·Ry·Rx of the converted angles equals EasyEDA's Rx·Ry·Rz."""
        ex, ey, ez = c_rotation
        easyeda = _matmul(_matmul(_rot("x", ex), _rot("y", ey)), _rot("z", ez))
        kicad = _kicad_rotation(_kicad_file_rotation(tuple(-a for a in c_rotation)))
        for row_a, row_b in zip(easyeda, kicad):
            assert _approx(row_a, row_b)

    def test_c2318_vertical_connector_rotation(self):
        """c_rotation 270,0,180 must stand the connector up, not turn it upside down."""
        assert _kicad_file_rotation((-270.0, 0.0, -180.0)) == (-90.0, 0.0, 180.0)


# ---------------------------------------------------------------------------
# Step 2: anchor
# ---------------------------------------------------------------------------


class TestAnchor:
    # 2 x 1 mm block whose bounding box is centred at (0.5, 0.25) mm, z in [-0.25, 0.25]
    BLOCK = _obj((-0.5, 1.5, -0.25, 0.75, -0.25, 0.25))

    def test_no_obj_gives_zero_offset(self):
        placement = place_model(_model(c_rotation=(0, 0, 90)), 0, 0)
        assert placement.offset == (0.0, 0.0, 0.0)
        assert placement.rotation == (0.0, 0.0, -90.0)
        assert placement.anchor == ANCHOR_NONE

    def test_empty_obj_gives_zero_offset(self):
        assert place_model(_model(), 0, 0, "usemtl m\n").offset == (0.0, 0.0, 0.0)

    def test_centres_model_on_outline(self):
        model = _model(origin=(500 * MM, 500 * MM), outline=_outline_mm(2.0, 1.0, 2.0, 1.0))
        placement = place_model(model, 0, 0, self.BLOCK)
        assert placement.anchor == ANCHOR_OUTLINE
        # KiCad offset Y points up: board y = 1.0 mm down -> -1.0
        assert _approx(placement.offset, (2.0 - 0.5, -1.0 - 0.25, 0.25))

    def test_outline_is_relative_to_footprint_origin(self):
        model = _model(outline=_outline_mm(102.0, 51.0, 2.0, 1.0))
        offset, _ = compute_model_transform(model, 100 * MM, 50 * MM, self.BLOCK)
        assert _approx(offset[:2], (2.0 - 0.5, -1.0 - 0.25))

    def test_outline_compared_after_z_rotation(self):
        """A model turned 90° about Z has a 1 x 2 mm outline."""
        model = _model(outline=_outline_mm(0, 0, 1.0, 2.0), c_rotation=(0, 0, 90))
        assert place_model(model, 0, 0, self.BLOCK).anchor == ANCHOR_OUTLINE

    def test_mismatched_outline_falls_back_to_c_origin(self):
        model = _model(origin=(1.0 * MM, 0.0), outline=_outline_mm(0, 0, 0.01, 0.5))
        placement = place_model(model, 0, 0, self.BLOCK)
        assert placement.anchor == ANCHOR_C_ORIGIN
        assert _approx(placement.offset[:2], (1.0 - 0.5, -0.25))

    def test_implausible_c_origin_centres_on_footprint(self):
        """c_origin hundreds of mm away (seen on C33696) is not trusted."""
        model = _model(origin=(-875.0 * MM, 798.0 * MM))
        placement = place_model(model, 0, 0, self.BLOCK)
        assert placement.anchor == ANCHOR_FOOTPRINT
        assert _approx(placement.offset[:2], (-0.5, -0.25))

    def test_c_origin_must_cover_the_pads(self):
        pads = [_smd_pad(-5.0, 0, 1, 1), _smd_pad(-3.0, 0, 1, 1)]
        far = place_model(_model(origin=(5.0 * MM, 0.0)), 0, 0, self.BLOCK, pads)
        near = place_model(_model(origin=(-4.0 * MM, 0.0)), 0, 0, self.BLOCK, pads)
        assert far.anchor == ANCHOR_FOOTPRINT
        assert near.anchor == ANCHOR_C_ORIGIN

    def test_lowest_point_goes_to_z(self):
        model = _model(outline=_outline_mm(0.5, -0.25, 2.0, 1.0), z=-3.5 * MM)
        offset, _ = compute_model_transform(model, 0, 0, self.BLOCK)
        assert offset[2] == pytest.approx(-3.5 + 0.25)

    def test_lift_is_applied_before_x_rotation(self):
        """C2318: EasyEDA lifts by z - z_min, then rotates; turned 90° about X it is horizontal."""
        obj = _obj((-2.5, 12.5, -2.5, 7.0, -2.4, 3.3))
        model = _model(outline=_outline_mm(5.0, 0.0, 15.0, 9.5), z=2.3 * MM, c_rotation=(270, 0, 180))
        offset, rotation = compute_model_transform(model, 0, 0, obj)
        assert rotation == (-90.0, 0.0, 180.0)
        assert offset[2] == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Step 3: pin fit
# ---------------------------------------------------------------------------


class TestPinFit:
    def test_shifts_pins_onto_holes(self):
        pads = [_tht_pad(-1.27 + 0.1, 0.2, "1"), _tht_pad(1.27 + 0.1, 0.2, "2")]
        placement = place_model(_model(outline=TWO_PIN_OUTLINE, z=TWO_PIN_Z), 0, 0, TWO_PIN_PART, pads)
        assert placement.pins == PINS_ALIGNED
        assert placement.pin_rms == pytest.approx(0.0, abs=1e-9)
        # board +0.2 mm (down) is KiCad -0.2 mm
        assert _approx(placement.offset[:2], (0.1, -0.2))

    def test_without_pads_pins_are_not_checked(self):
        placement = place_model(_model(outline=TWO_PIN_OUTLINE, z=TWO_PIN_Z), 0, 0, TWO_PIN_PART)
        assert placement.pins == PINS_NOT_CHECKED
        assert _approx(placement.offset[:2], (0.0, 0.0))

    def test_keeps_anchor_when_pin_spacing_differs(self):
        pads = [_tht_pad(-1.5, 0, "1"), _tht_pad(1.5, 0, "2")]
        placement = place_model(_model(outline=TWO_PIN_OUTLINE, z=TWO_PIN_Z), 0, 0, TWO_PIN_PART, pads)
        assert placement.pins == PINS_OFFSET
        assert placement.pin_rms > PIN_FIT_TOLERANCE_MM
        assert _approx(placement.offset[:2], (0.0, 0.0))

    def test_reports_pins_that_miss_the_pads(self):
        """C8760-style: the model's leads are nowhere near the holes."""
        pads = [_tht_pad(-3.75, 0, "1"), _tht_pad(3.75, 0, "2")]
        placement = place_model(_model(outline=TWO_PIN_OUTLINE, z=TWO_PIN_Z), 0, 0, TWO_PIN_PART, pads)
        assert placement.pins == PINS_UNMATCHED
        assert _approx(placement.offset[:2], (0.0, 0.0))

    def test_smd_terminals_larger_than_pads_still_pair(self):
        """C94598-style: every corner of the terminal lies outside the pad."""
        obj = _obj((-2.8, -1.8, -0.4, 0.4, 0.0, 0.5), (1.8, 2.8, -0.4, 0.4, 0.0, 0.5), (-1.8, 1.8, -1, 1, 0.1, 1.0))
        pads = [_smd_pad(-2.3, 0, 0.7, 0.6, "1"), _smd_pad(2.3, 0, 0.7, 0.6, "2")]
        placement = place_model(_model(outline=_outline_mm(0, 0, 5.6, 2.0)), 0, 0, obj, pads)
        assert placement.pins == PINS_ALIGNED

    def test_smd_terminals_are_centred(self):
        obj = _obj((-2.8, -1.8, -0.4, 0.4, 0.0, 0.5), (1.8, 2.8, -0.4, 0.4, 0.0, 0.5))
        pads = [_smd_pad(-2.3 + 0.05, 0, 1.6, 1.2, "1"), _smd_pad(2.3 + 0.05, 0, 1.6, 1.2, "2")]
        placement = place_model(_model(outline=_outline_mm(0, 0, 5.6, 0.8)), 0, 0, obj, pads)
        assert placement.pins == PINS_ALIGNED
        assert placement.offset[0] == pytest.approx(0.05)

    def test_bottom_side_smd_pads_are_skipped(self):
        obj = _obj((-2.8, -1.8, -0.4, 0.4, 0.0, 0.5), (1.8, 2.8, -0.4, 0.4, 0.0, 0.5))
        pads = [_smd_pad(-2.3, 0, 1.6, 1.2, "1", layer="2"), _smd_pad(2.3, 0, 1.6, 1.2, "2", layer="2")]
        placement = place_model(_model(outline=_outline_mm(0, 0, 5.6, 0.8)), 0, 0, obj, pads)
        assert placement.pins == PINS_UNMATCHED

    def test_polygon_pads_use_their_outline(self):
        square = [-0.8, -0.8, 0.8, -0.8, 0.8, 0.8, -0.8, 0.8]
        pads = [
            EEPad("POLYGON", -1.27 + 0.1, 0, 0, 0, "11", "1", 1.0, polygon_points=square),
            EEPad("POLYGON", 1.27 + 0.1, 0, 0, 0, "11", "2", 1.0, polygon_points=square),
        ]
        placement = place_model(_model(outline=TWO_PIN_OUTLINE, z=TWO_PIN_Z), 0, 0, TWO_PIN_PART, pads)
        assert placement.pins == PINS_ALIGNED
        assert placement.offset[0] == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# outline3D parsing
# ---------------------------------------------------------------------------


def _svgnode(children, transform="scale(1) translate(0, 0)"):
    return {"attrs": {"transform": transform}, "childNodes": children}


def _polyline(points):
    return {"nodeName": "polyline", "attrs": {"points": points}}


class TestOutlineParsing:
    def test_polyline_bbox(self):
        node = _svgnode([_polyline("10 20 30 20 30 5 10 5"), _polyline("12 8 40 8")])
        assert _svgnode_outline_bbox(node) == (10, 40, 5, 20)

    def test_translate_and_scale_applied(self):
        node = _svgnode([_polyline("0 0 4 2")], transform="scale(2) translate(1, -1)")
        assert _svgnode_outline_bbox(node) == (2, 10, -2, 2)

    def test_missing_transform_is_identity(self):
        assert _svgnode_outline_bbox({"childNodes": [_polyline("1 2 3 4")]}) == (1, 3, 2, 4)

    def test_leading_dot_numbers(self):
        assert _svgnode_outline_bbox(_svgnode([_polyline(".5 .5 1.5 1.5")])) == (0.5, 1.5, 0.5, 1.5)

    @pytest.mark.parametrize(
        "node",
        [
            _svgnode([]),
            {"attrs": {}},
            _svgnode([{"nodeName": "path", "attrs": {"d": "M 10 10 A 5 5 0 0 1 20 10"}}]),
            _svgnode([_polyline("1 2 3")]),
            _svgnode([_polyline("1 2 3 4")], transform="rotate(45)"),
            _svgnode([{"nodeName": "polyline", "attrs": {"points": "1 2 3 4", "transform": "rotate(9)"}}]),
        ],
    )
    def test_unrecognised_outline_is_none(self, node):
        assert _svgnode_outline_bbox(node) is None


# ---------------------------------------------------------------------------
# Real parts (testdata/)
# ---------------------------------------------------------------------------


def _load_part(lcsc_id):
    with open(os.path.join(TESTDATA_DIR, f"{lcsc_id}_footprint.json")) as f:
        data = json.load(f)["dataStr"]
    ox, oy = data["head"]["x"], data["head"]["y"]
    footprint = parse_footprint_shapes(data["shape"], ox, oy)
    with open(os.path.join(TESTDATA_DIR, f"{lcsc_id}_model.obj")) as f:
        obj = f.read()
    return footprint, ox, oy, obj


def _parts_with_models():
    ids = []
    for path in sorted(glob.glob(os.path.join(TESTDATA_DIR, "*_model.obj"))):
        lcsc_id = os.path.basename(path).split("_")[0]
        if os.path.exists(os.path.join(TESTDATA_DIR, f"{lcsc_id}_footprint.json")):
            ids.append(lcsc_id)
    return ids


# Through-hole parts whose pins go exactly through their holes once placed.
THT_ALIGNED = [
    "C10081",
    "C138392",
    "C2203",
    "C2316",
    "C2318",
    "C2320",
    "C2321",
    "C2474",
    "C2562",
    "C33478",
    "C3794",
    "C385834",
    "C386757",
    "C386758",
    "C395958",
    "C5213",
    "C668119",
]


class TestRealParts:
    @pytest.mark.parametrize("lcsc_id", THT_ALIGNED)
    def test_through_hole_pins_land_in_holes(self, lcsc_id):
        footprint, ox, oy, obj = _load_part(lcsc_id)
        placement = place_model(footprint.model, ox, oy, obj, footprint.pads)
        assert placement.pins == PINS_ALIGNED
        assert placement.pin_rms <= PIN_FIT_TOLERANCE_MM

    @pytest.mark.parametrize("lcsc_id", _parts_with_models())
    def test_model_covers_its_footprint(self, lcsc_id):
        """Every placed model overlaps its pads: never offset off the part."""
        footprint, ox, oy, obj = _load_part(lcsc_id)
        placement = place_model(footprint.model, ox, oy, obj, footprint.pads)
        placed = _placed_vertices(_parse_obj_mesh(obj), placement.rotation, placement.offset)
        xs = [v[0] for v in placed]
        ys = [v[1] for v in placed]
        for pad in footprint.pads:
            assert min(xs) - 0.5 <= pad.x <= max(xs) + 0.5, lcsc_id
            assert min(ys) - 0.5 <= pad.y <= max(ys) + 0.5, lcsc_id

    def test_c2318_stands_upright(self):
        """Vertical XH-5A: housing on the board, 2.5 mm pins below it."""
        footprint, ox, oy, obj = _load_part("C2318")
        placement = place_model(footprint.model, ox, oy, obj, footprint.pads)
        placed = _placed_vertices(_parse_obj_mesh(obj), placement.rotation, placement.offset)
        assert min(v[2] for v in placed) == pytest.approx(-2.5, abs=0.01)
        assert max(v[2] for v in placed) == pytest.approx(7.0, abs=0.01)

    def test_c33696_ignores_bogus_c_origin(self):
        """c_origin is ~1180 mm away; the outline puts the model on its pads."""
        footprint, ox, oy, obj = _load_part("C33696")
        placement = place_model(footprint.model, ox, oy, obj, footprint.pads)
        assert placement.anchor == ANCHOR_OUTLINE
        assert math.hypot(*placement.offset[:2]) < 0.5

    def test_c3116_degenerate_outline_uses_c_origin(self):
        footprint, ox, oy, obj = _load_part("C3116")
        assert place_model(footprint.model, ox, oy, obj, footprint.pads).anchor == ANCHOR_C_ORIGIN

    def test_c8760_mismatched_model_is_reported(self):
        """EasyEDA's model has its leads on a 9.8 mm diagonal; the pads are 7.5 mm apart in a line."""
        footprint, ox, oy, obj = _load_part("C8760")
        assert place_model(footprint.model, ox, oy, obj, footprint.pads).pins == PINS_UNMATCHED

    def test_c94598_terminals_pair_with_pads(self):
        footprint, ox, oy, obj = _load_part("C94598")
        assert place_model(footprint.model, ox, oy, obj, footprint.pads).pins != PINS_UNMATCHED


class TestConvertToVrml:
    def test_empty_source(self):
        assert convert_to_vrml("") is None

    def test_no_vertices(self):
        source = "newmtl red\nKd 1 0 0\nendmtl\nusemtl red\nf 1 2 3"
        assert convert_to_vrml(source) is None

    def test_no_faces(self):
        source = "v 1 0 0\nv 0 1 0\nv 0 0 1\n"
        assert convert_to_vrml(source) is None

    def test_basic_triangle(self):
        source = (
            "newmtl mat1\n"
            "Kd 0.8 0.2 0.1\n"
            "Ks 0.1 0.1 0.1\n"
            "d 0\n"
            "endmtl\n"
            "v 0 0 0\n"
            "v 2.54 0 0\n"
            "v 0 2.54 0\n"
            "usemtl mat1\n"
            "f 1 2 3\n"
        )
        result = convert_to_vrml(source)
        assert result is not None
        assert "#VRML V2.0 utf8" in result
        assert "Shape {" in result
        assert "IndexedFaceSet" in result
        assert "diffuseColor" in result
        assert "coordIndex" in result

    def test_unit_conversion(self):
        # Vertices should be divided by 2.54
        source = "newmtl m\nKd 0.5 0.5 0.5\nendmtl\nv 2.54 5.08 7.62\nv 0 0 0\nv 2.54 2.54 0\nusemtl m\nf 1 2 3\n"
        result = convert_to_vrml(source)
        # 2.54 / 2.54 = 1.0, 5.08 / 2.54 = 2.0, 7.62 / 2.54 = 3.0
        assert "1.000000 2.000000 3.000000" in result

    def test_multiple_materials(self):
        source = (
            "newmtl red\nKd 1 0 0\nendmtl\n"
            "newmtl blue\nKd 0 0 1\nendmtl\n"
            "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
            "v 2 0 0\nv 3 0 0\nv 2 1 0\n"
            "usemtl red\nf 1 2 3\n"
            "usemtl blue\nf 4 5 6\n"
        )
        result = convert_to_vrml(source)
        assert result.count("Shape {") == 2
        assert "1.0000 0.0000 0.0000" in result  # red
        assert "0.0000 0.0000 1.0000" in result  # blue

    def test_face_with_normals(self):
        # f v1//n1 v2//n2 v3//n3 format
        source = "newmtl m\nKd 0.5 0.5 0.5\nendmtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl m\nf 1//1 2//2 3//3\n"
        result = convert_to_vrml(source)
        assert result is not None
        assert "coordIndex" in result

    def test_face_with_texture_coords(self):
        # f v1/t1 v2/t2 v3/t3 format
        source = "newmtl m\nKd 0.5 0.5 0.5\nendmtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl m\nf 1/1 2/2 3/3\n"
        result = convert_to_vrml(source)
        assert result is not None

    def test_transparency(self):
        source = "newmtl glass\nKd 0.9 0.9 0.9\nd 0.5\nendmtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl glass\nf 1 2 3\n"
        result = convert_to_vrml(source)
        assert "transparency 0.5000" in result

    def test_quad_face(self):
        source = "newmtl m\nKd 0.5 0.5 0.5\nendmtl\nv 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nusemtl m\nf 1 2 3 4\n"
        result = convert_to_vrml(source)
        assert result is not None
        assert "-1" in result  # face terminator

    def test_vrml_header(self):
        source = "newmtl m\nKd 0.5 0.5 0.5\nendmtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl m\nf 1 2 3\n"
        result = convert_to_vrml(source)
        lines = result.strip().split("\n")
        assert lines[0] == "#VRML V2.0 utf8"


class TestSaveModels:
    def test_returns_existing_files_when_no_data(self, tmp_path):
        """When no data is provided, existing files are still returned."""
        step_path = tmp_path / "part.step"
        wrl_path = tmp_path / "part.wrl"
        step_path.write_bytes(b"old-step")
        wrl_path.write_text("old-wrl", encoding="utf-8")

        step_out, wrl_out = save_models(str(tmp_path), "part")
        assert step_out == str(step_path)
        assert wrl_out == str(wrl_path)
        assert step_path.read_bytes() == b"old-step"
        assert wrl_path.read_text(encoding="utf-8") == "old-wrl"

    def test_saves_new_data(self, tmp_path, monkeypatch):
        """New data is written and paths are returned."""
        import kicad_jlcimport.kicad.model3d as model3d

        step_path = tmp_path / "part.step"
        wrl_path = tmp_path / "part.wrl"
        step_path.write_bytes(b"old-step")
        wrl_path.write_text("old-wrl", encoding="utf-8")

        monkeypatch.setattr(model3d, "convert_to_vrml", lambda *_a, **_k: "new-wrl")

        step_out, wrl_out = save_models(str(tmp_path), "part", step_data=b"new-step", wrl_source="src")
        assert step_out == str(step_path)
        assert wrl_out == str(wrl_path)
        assert step_path.read_bytes() == b"new-step"
        assert wrl_path.read_text(encoding="utf-8") == "new-wrl"

    def test_returns_none_when_no_data_and_no_file(self, tmp_path):
        """Returns None for files that don't exist and have no data."""
        step_out, wrl_out = save_models(str(tmp_path), "part")
        assert step_out is None
        assert wrl_out is None


# ---------------------------------------------------------------------------
# Physical checks (model_checks.py)
# ---------------------------------------------------------------------------


class TestModelChecks:
    @staticmethod
    def _check(obj, pads, offset=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0), holes=(), pins=PINS_ALIGNED):
        from kicad_jlcimport.easyeda.ee_types import EEFootprint, EEHole
        from kicad_jlcimport.kicad.model3d import ModelPlacement
        from kicad_jlcimport.kicad.model_checks import check_model

        footprint = EEFootprint(pads=list(pads), holes=[EEHole(*h) for h in holes])
        return check_model(footprint, ModelPlacement(offset, rotation, ANCHOR_OUTLINE, pins), obj)

    THT_PADS = [_tht_pad(-1.27, 0, "1"), _tht_pad(1.27, 0, "2")]

    def test_through_hole_part_passes(self):
        result = self._check(TWO_PIN_PART, self.THT_PADS, offset=(0, 0, 0.0))
        assert result.ok and not result.warnings

    def test_floating(self):
        result = self._check(TWO_PIN_PART, self.THT_PADS, offset=(0, 0, 3.5))
        assert result.issues == ["floating"]

    def test_upside_down_body_through_the_board(self):
        # Rotated 180° about X the body (z 1..3) ends up 1..3 mm under the board
        result = self._check(TWO_PIN_PART, self.THT_PADS, rotation=(180.0, 0.0, 0.0))
        assert "below_board" in result.issues

    def test_sunk_body_is_a_warning(self):
        result = self._check(TWO_PIN_PART, self.THT_PADS, offset=(0, 0, -1.3))
        assert result.ok
        assert result.warnings == ["sunk"]

    def test_pegs_may_go_through_mounting_holes(self):
        obj = _obj((-0.3, 0.3, -0.3, 0.3, -3.0, 0.0), (-2.0, 2.0, -1.0, 1.0, 0.0, 2.0))
        pads = [_smd_pad(-1.5, 0, 0.5, 0.5, "1"), _smd_pad(1.5, 0, 0.5, 0.5, "2")]
        assert self._check(obj, pads, holes=[(0.0, 0.0, 0.5)]).ok
        assert "below_board" in self._check(obj, pads).issues

    def test_pad_outside_model(self):
        pads = self.THT_PADS + [_tht_pad(10.0, 0, "3")]
        assert "off_footprint" in self._check(TWO_PIN_PART, pads).issues

    def test_unmatched_pins_reported(self):
        assert "pins_unmatched" in self._check(TWO_PIN_PART, self.THT_PADS, pins=PINS_UNMATCHED).issues

    def test_no_model_has_nothing_to_check(self):
        assert self._check(None, self.THT_PADS).ok
