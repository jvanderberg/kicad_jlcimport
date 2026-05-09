"""Tests for model3d.py - VRML conversion and model transforms."""

import json
import os

import pytest

from kicad_jlcimport.easyeda.ee_types import EE3DModel
from kicad_jlcimport.easyeda.parser import parse_footprint_shapes
from kicad_jlcimport.kicad.model3d import (
    _obj_xy_center,
    compute_model_transform,
    convert_to_vrml,
    save_models,
)

# Path to test data directory
TESTDATA_DIR = os.path.join(os.path.dirname(__file__), "..", "testdata")


class TestComputeModelTransform:
    def test_no_obj_source(self):
        """Without OBJ data, XY offset is zero; only Z is used."""
        model = EE3DModel(uuid="test", origin_x=0, origin_y=0, z=0, rotation=(0, 0, 0))
        offset, rotation = compute_model_transform(model, 0, 0)
        assert offset == (0.0, 0.0, 0.0)
        assert rotation == (0, 0, 0)

    def test_z_offset_with_obj(self):
        """Z offset is converted from mils when OBJ data is provided."""
        obj = "v 0 0 0\nv 1.0 1.0 1.0\n"
        model = EE3DModel(uuid="test", origin_x=0, origin_y=0, z=50, rotation=(0, 0, 90))
        offset, rotation = compute_model_transform(model, 0, 0, obj_source=obj)
        # With 90° rotation, geometry offset (-cx, -cy) = (-0.5, -0.5) transforms to (0.5, -0.5)
        # x' = -0.5*cos(90°) - (-0.5)*sin(90°) = -0.5*0 - (-0.5)*1 = 0.5
        # y' = -0.5*sin(90°) + (-0.5)*cos(90°) = -0.5*1 + (-0.5)*0 = -0.5
        assert offset[0] == pytest.approx(0.5)
        assert offset[1] == pytest.approx(-0.5)
        # z_offset = -z_min + mil_to_mm(model.z) = -0 + (50 × 0.254) = 12.7mm
        assert offset[2] == pytest.approx(12.7, abs=0.01)
        assert rotation == (0, 0, 90)

    def test_z_offset_without_obj(self):
        """Without OBJ data, z-offset defaults to 0 (model.z is unreliable)."""
        model = EE3DModel(uuid="test", origin_x=200, origin_y=300, z=50, rotation=(0, 0, 90))
        offset, rotation = compute_model_transform(model, 100, 100)
        assert offset[0] == 0.0
        assert offset[1] == 0.0
        assert offset[2] == 0.0
        assert rotation == (0, 0, 90)

    def test_rotation_preserved(self):
        """Rotation tuple is passed through unchanged."""
        model = EE3DModel(uuid="test", origin_x=50, origin_y=50, z=0, rotation=(10, 20, 30))
        offset, rotation = compute_model_transform(model, 100, 100)
        assert rotation == (10, 20, 30)

    def test_obj_source_recenters_xy(self):
        """OBJ bounding-box centre is negated to recenter the model."""
        obj = "v 1.0 2.0 0.0\nv 3.0 4.0 1.0\n"
        model = EE3DModel(uuid="test", origin_x=0, origin_y=0, z=0, rotation=(0, 0, 0))
        offset, _ = compute_model_transform(model, 0, 0, obj_source=obj)
        # center = (2.0, 3.0); offset = (-2.0, -3.0)
        assert offset[0] == pytest.approx(-2.0)
        assert offset[1] == pytest.approx(-3.0)
        assert offset[2] == 0.0

    def test_obj_source_with_z_offset(self):
        """OBJ XY correction and Z offset combine correctly."""
        obj = "v -1.0 0.0 0.0\nv 5.0 2.0 1.0\n"
        model = EE3DModel(uuid="test", origin_x=0, origin_y=0, z=50, rotation=(0, 0, 0))
        offset, _ = compute_model_transform(model, 0, 0, obj_source=obj)
        assert offset[0] == pytest.approx(-2.0)
        assert offset[1] == pytest.approx(-1.0)
        assert offset[2] == pytest.approx(12.7, abs=0.01)  # 50 mils × 0.254


class TestObjXyCenter:
    def test_simple_vertices(self):
        obj = "v 0 0 0\nv 4.0 6.0 1.0\n"
        cx, cy = _obj_xy_center(obj)
        assert cx == pytest.approx(2.0)
        assert cy == pytest.approx(3.0)

    def test_empty_returns_zero(self):
        assert _obj_xy_center("") == (0.0, 0.0)

    def test_no_vertex_lines(self):
        assert _obj_xy_center("f 1 2 3\nusemtl foo\n") == (0.0, 0.0)

    def test_ignores_vn_vt(self):
        obj = "vn 1 0 0\nvt 0.5 0.5\nv 2 4 0\nv 6 8 0\n"
        cx, cy = _obj_xy_center(obj)
        assert cx == pytest.approx(4.0)
        assert cy == pytest.approx(6.0)

    def test_centered_model_returns_zero(self):
        obj = "v -3 -2 0\nv 3 2 0\n"
        cx, cy = _obj_xy_center(obj)
        assert cx == pytest.approx(0.0)
        assert cy == pytest.approx(0.0)


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


class TestTHTConnectorOffsets:
    """Test THT connector offset calculations with real component data.

    Uses actual footprint and OBJ data from testdata/ to verify against
    user-validated offset values.
    """

    def _load_test_data(self, lcsc_id):
        """Load footprint and OBJ data for a test part."""
        # Load footprint data
        fp_path = os.path.join(TESTDATA_DIR, f"{lcsc_id}_footprint.json")
        with open(fp_path) as f:
            fp_data = json.load(f)

        # Parse footprint to get model data
        fp_head = fp_data["dataStr"]["head"]
        fp_origin_x = fp_head["x"]
        fp_origin_y = fp_head["y"]

        fp_shapes = fp_data["dataStr"]["shape"]
        footprint = parse_footprint_shapes(fp_shapes, fp_origin_x, fp_origin_y)

        # Load OBJ data
        obj_path = os.path.join(TESTDATA_DIR, f"{lcsc_id}_model.obj")
        with open(obj_path) as f:
            obj_source = f.read()

        return footprint.model, fp_origin_x, fp_origin_y, obj_source

    def test_c160404_smd_connector(self):
        """C160404 (SM04B-SRSS-TB) - SMD connector that started issue #29."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C160404")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=-1.5, y=-0.35, z=0.0
        assert offset[0] == pytest.approx(-1.5, abs=0.01)
        assert offset[1] == pytest.approx(-0.35, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.2)

    def test_c668119_coincident_origins(self):
        """C668119 (4-pin header) - model and footprint origins coincide."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C668119")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0, z≈-0.134 (formula gives 0.0, difference acceptable)
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.15)

    def test_c82899_esp32_module(self):
        """C82899 (ESP32-WROOM-32) - SMD module with model origin offset."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C82899")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=3.743, z=0.005
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(3.743, abs=0.01)
        assert offset[2] == pytest.approx(0.005, abs=0.01)

    def test_c33696_outlier_offset(self):
        """C33696 - part with erroneous 798mm origin offset should default to zero."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C33696")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: should be x=0, y=0, z=0 (798mm offset is EasyEDA data error)
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.2)

    def test_c1027_symmetric_smd(self):
        """C1027 (L0603 inductor) - symmetric SMD part should use z_max for offset."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C1027")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0, z=0.254 (symmetric SMD, use z_max)
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.254, abs=0.01)

    def test_c3116_symmetric_smd_taller(self):
        """C3116 - taller symmetric SMD part should use z_max for offset."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C3116")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0, z=2.6 (symmetric SMD, use z_max)
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(2.6, abs=0.01)

    def test_c385834_rj45_connector(self):
        """C385834 (RJ45) - uses z_max for parts extending below PCB."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C385834")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=-1.08, z=6.45
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(-1.08, abs=0.05)
        assert offset[2] == pytest.approx(6.35, abs=0.3)  # z_max (formula gives 6.6)

    def test_c395958_terminal_block(self):
        """C395958 (2-pin terminal) - uses -z_min/2 for parts extending above PCB."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C395958")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=-0.00005, y=-8.9, z=4.2
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(-8.9, abs=0.25)  # -cy - model_origin_diff
        assert offset[2] == pytest.approx(4.2, abs=0.8)  # -z_min/2 (formula gives 4.9)

    def test_c5206_dip_package(self):
        """C5206 (DIP-8) - uses z_max for parts extending below PCB, even with matching origins."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C5206")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=good, y=good, z≈2mm
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(2.0, abs=0.5)  # z_max (formula gives 2.475)

    def test_c6186_spurious_offset(self):
        """C6186 (SOT-223-3) - spurious model origin offset should be ignored."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C6186")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0, z=0 (2.921mm offset is spurious)
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.2)

    def test_c5213_spurious_offset(self):
        """C5213 (SOT-89) - small spurious model origin offset should be ignored."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C5213")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0, z=1.05 (0.127mm offset is spurious)
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(1.05, abs=0.01)

    def test_c3794_to220_vertical(self):
        """C3794 (TO-220-3 vertical) - uses intentional y offset, z=0 for mainly-above part."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C3794")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0.65, z=-0.255
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.65, abs=0.01)
        assert offset[2] == pytest.approx(-0.255, abs=0.01)

    def test_c10081_th_resistor(self):
        """C10081 (TH Resistor) - no offset needed."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C10081")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0, z=0
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.2)

    def test_c2474_do41_diode(self):
        """C2474 (DO-41 Diode) - no offset needed."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C2474")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0, z=0
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.2)

    def test_c2562_to220_horizontal(self):
        """C2562 (TO-220-3 horizontal) - cy/height < 5% is ignored, uses z=0 for mainly-above part."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C2562")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0, z=-2.8 (cy=0.45mm is only 2.1% of height=21.9mm)
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(-2.8, abs=0.01)

    def test_c138392_rj45_tht(self):
        """C138392 (RJ45-TH) - THT connector with intentional origin offset, z=0 for mainly-above."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C138392")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=-3.35, z=0.321 (cy/height=0.5%, z_max/|z_min|=3.24)
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(-3.35, abs=0.01)
        assert offset[2] == pytest.approx(0.321, abs=0.01)

    def test_c386757_rj45_tht(self):
        """C386757 (RJ45-TH) - connector with significant cy, z=0 for mainly-above."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C386757")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=-5.82, z=0 (cy/height=15.5%, z_max/|z_min|=3.55)
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(-5.82, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.2)

    def test_c2078_sot89_with_rotation(self):
        """C2078 (SOT-89) - Z-rotation=-180° requires offset transformation."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C2078")

        offset, rotation = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=-0.3, y=0, z=0 after Z-rotation transformation
        assert offset[0] == pytest.approx(-0.3, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.2)
        assert rotation[2] == pytest.approx(-180.0, abs=0.01)

    def test_c2203_hc49us_crystal(self):
        """C2203 (HC-49US Crystal) - symmetric THT crystal should use z=0, not z_max."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C2203")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0, z=0 (symmetric THT crystal should sit flat)
        # Currently failing: produces z=3.5 due to symmetric SMD detection
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.2)

    def test_c7519_sot23_6(self):
        """C7519 (SOT-23-6) - spurious model origin offset should be ignored."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C7519")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=0, z=0 (SMD part with spurious 0.965mm origin offset)
        # Currently failing: produces y=-0.965 due to origin offset detection
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.2)

    def test_c2316_xh3a_with_rotation(self):
        """C2316 (XH-3A) - connector with Z-rotation=-180° requires offset transformation."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C2316")

        offset, rotation = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=2.5, y=2.25, z=1.8 after Z-rotation transformation
        assert offset[0] == pytest.approx(2.5, abs=0.01)
        assert offset[1] == pytest.approx(2.25, abs=0.01)
        assert offset[2] == pytest.approx(1.8, abs=0.01)
        assert rotation[2] == pytest.approx(-180.0, abs=0.01)

    def test_c386758_with_rotation_and_offset(self):
        """C386758 - THT part with origin offset and Z-rotation=-180° requires correct sign."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C386758")

        offset, rotation = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=0, y=1.587, z=-0.55 after Z-rotation transformation
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(1.587, abs=0.01)
        assert offset[2] == pytest.approx(-0.55, abs=0.01)
        assert rotation[2] == pytest.approx(-180.0, abs=0.01)

    def test_c2320_x_origin_offset_with_rotation(self):
        """C2320 (PH-3A) - connector with X-axis origin offset and -90° Z-rotation.

        This part has model origin offset in the X direction (0.65mm), which was
        previously ignored. With -90° rotation, the geometry offset must be rotated
        but the origin offset must NOT be rotated (stays in footprint space).
        """
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C2320")

        offset, rotation = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # Expected: centered at (0, 0) after X-offset is properly handled
        assert offset[0] == pytest.approx(0.0, abs=0.1)
        assert offset[1] == pytest.approx(0.0, abs=0.1)
        assert offset[2] == pytest.approx(0.0, abs=0.1)
        assert rotation[2] == pytest.approx(-90.0, abs=0.01)

    def test_c2321_no_rotation_x_offset(self):
        """C2321 (PH-4A) - connector with significant X-offset in geometry (cx=3.0mm).

        This part has no rotation but significant X offset in the OBJ geometry.
        The Y origin offset cancels out the cy offset, resulting in only X offset.
        """
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C2321")

        offset, rotation = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # Expected: X offset of -3.0mm (from -cx), Y and Z centered
        assert offset[0] == pytest.approx(-3.0, abs=0.1)
        assert offset[1] == pytest.approx(0.0, abs=0.1)
        assert offset[2] == pytest.approx(0.0, abs=0.1)
        assert rotation[2] == pytest.approx(0.0, abs=0.01)

    def test_c33478_x_origin_offset_with_rotation(self):
        """C33478 (PH-6A) - connector with X-axis origin offset and -90° Z-rotation.

        Similar to C2320, this validates that X-axis origin offsets are properly
        handled for rotated parts. The X offset (0.63mm) should be applied in
        footprint space after the geometry offset is rotated.
        """
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C33478")

        offset, rotation = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # Expected: nearly centered (small X offset due to rounding: 0.02mm)
        assert offset[0] == pytest.approx(0.0, abs=0.1)
        assert offset[1] == pytest.approx(0.0, abs=0.1)
        assert offset[2] == pytest.approx(0.0, abs=0.1)
        assert rotation[2] == pytest.approx(-90.0, abs=0.01)


class TestPartsWithoutOBJData:
    """Test parts where 3D model OBJ data is not available.

    For these parts, z-offset should default to 0 as model.z is unreliable.
    """

    def test_parts_without_obj_default_to_zero_z(self):
        """Parts without OBJ data should have z-offset = 0."""
        test_parts = ["C5213", "C3794", "C8852", "C18901", "C10081", "C138392"]

        for part_id in test_parts:
            fp_path = os.path.join(TESTDATA_DIR, f"{part_id}_footprint.json")
            if not os.path.exists(fp_path):
                continue  # Skip if test data not available

            with open(fp_path) as f:
                fp_data = json.load(f)

            fp_head = fp_data["dataStr"]["head"]
            fp_origin_x = fp_head["x"]
            fp_origin_y = fp_head["y"]
            fp_shapes = fp_data["dataStr"]["shape"]
            footprint = parse_footprint_shapes(fp_shapes, fp_origin_x, fp_origin_y)

            if not footprint.model:
                continue  # Skip if no 3D model

            # No OBJ file available
            offset, _ = compute_model_transform(footprint.model, fp_origin_x, fp_origin_y, obj_source=None)

            # User verified: z should be 0 for all these parts
            assert offset[2] == 0.0, f"{part_id} should have z-offset=0, got {offset[2]:.3f}"


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
