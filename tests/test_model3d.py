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

    def test_z_offset_always_zero(self):
        """Z offset is always zero; OBJ geometry already encodes Z positioning."""
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

    def test_obj_source_with_z_always_zero(self):
        """OBJ XY correction applied; Z is always zero."""
        obj = "v -1.0 0.0 0.0\nv 5.0 2.0 1.0\n"
        model = EE3DModel(uuid="test", origin_x=0, origin_y=0, z=50, rotation=(0, 0, 0))
        offset, _ = compute_model_transform(model, 0, 0, obj_source=obj)
        assert offset[0] == pytest.approx(-2.0)
        assert offset[1] == pytest.approx(-1.0)
        assert offset[2] == 0.0


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


class TestRealPartOffsets:
    """Test offset calculations with real component data from testdata/.

    Uses actual footprint and OBJ data to verify against
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
        """C160404 (SM04B-SRSS-TB) - SMD connector."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C160404")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # User verified: x=-1.5, y=-0.35, z=0.0
        assert offset[0] == pytest.approx(-1.5, abs=0.01)
        assert offset[1] == pytest.approx(-0.35, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.01)

    def test_c668119_coincident_origins(self):
        """C668119 (4-pin header) - model and footprint origins coincide."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C668119")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # z=0: OBJ geometry already has correct Z positioning
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.01)

    def test_c385834_rj45_connector(self):
        """C385834 (RJ45) - THT connector with origin offset."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C385834")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # z=0: OBJ geometry already has correct Z positioning
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(-1.08, abs=0.05)
        assert offset[2] == pytest.approx(0.0, abs=0.01)

    def test_c395958_terminal_block(self):
        """C395958 (2-pin terminal block) - THT with large Y offset."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C395958")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        # z=0: OBJ geometry already has correct Z positioning
        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[1] == pytest.approx(-8.9, abs=0.25)
        assert offset[2] == pytest.approx(0.0, abs=0.01)

    def test_c5213_dip8_opamp(self):
        """C5213 (LM358P DIP-8) - THT IC, z should be 0."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C5213")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.01)

    def test_c3794_tht_part(self):
        """C3794 - THT part, z should be 0."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C3794")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.01)

    def test_c8852_part(self):
        """C8852 - z should be 0."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C8852")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.01)

    def test_c18901_part(self):
        """C18901 - z should be 0."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C18901")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.01)

    def test_c10081_part(self):
        """C10081 - z should be 0."""
        model, fp_origin_x, fp_origin_y, obj_source = self._load_test_data("C10081")

        offset, _ = compute_model_transform(model, fp_origin_x, fp_origin_y, obj_source)

        assert offset[0] == pytest.approx(0.0, abs=0.01)
        assert offset[2] == pytest.approx(0.0, abs=0.01)


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
