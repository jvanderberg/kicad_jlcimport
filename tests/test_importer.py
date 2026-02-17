"""Tests for importer.py to improve coverage."""

import os

from kicad_jlcimport import importer
from kicad_jlcimport.easyeda.ee_types import EE3DModel, EEFootprint, EEPad, EEPin, EESymbol
from kicad_jlcimport.importer import _build_description, _build_keywords


class TestImportComponent:
    """Tests for import_component function."""

    def _make_fake_comp(self, with_symbol=True, with_3d=False):
        """Create a fake component dict for testing."""
        comp = {
            "title": "TestPart",
            "prefix": "U",
            "description": "Test description",
            "datasheet": "https://example.com/ds.pdf",
            "manufacturer": "ACME",
            "manufacturer_part": "MPN123",
            "footprint_data": {"dataStr": {"shape": []}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
        }
        if with_symbol:
            comp["symbol_data_list"] = [{"dataStr": {"shape": []}}]
        if with_3d:
            comp["uuid_3d"] = "model_uuid_123"
        return comp

    def _make_fake_footprint(self, with_model=False):
        """Create a fake footprint for testing."""
        fp = EEFootprint()
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0, rotation=0)
        fp.pads.append(pad)
        if with_model:
            fp.model = EE3DModel(uuid="model_uuid", origin_x=100, origin_y=200, z=5, rotation=(0, 0, 0))
        return fp

    def _make_fake_symbol(self):
        """Create a fake symbol for testing."""
        sym = EESymbol()
        pin = EEPin(number="1", name="VCC", x=0, y=0, rotation=0, length=2.54, electrical_type="power_in")
        sym.pins.append(pin)
        return sym

    def test_import_export_only(self, tmp_path, monkeypatch):
        """Test export_only mode writes raw files."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=True, with_3d=False)
        fake_fp = self._make_fake_footprint()
        fake_sym = self._make_fake_symbol()

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *a, **k: fake_sym)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "write_symbol", lambda *a, **k: '  (symbol "TestPart")\n')

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: log_messages.append(msg),
        )

        assert result["title"] == "TestPart"
        assert result["name"] == "TestPart"
        assert "(footprint" in result["fp_content"]
        assert (tmp_path / "TestPart.kicad_mod").exists()
        assert (tmp_path / "TestPart.kicad_sym").exists()

    def test_import_export_only_with_3d_model(self, tmp_path, monkeypatch):
        """Test export_only mode downloads 3D models."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=False, with_3d=True)
        fake_fp = self._make_fake_footprint()

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "download_step", lambda _: b"step-data")
        monkeypatch.setattr(importer, "download_wrl_source", lambda _: None)
        monkeypatch.setattr(
            importer,
            "save_models",
            lambda dir, name, step_data=None, wrl_source=None: (
                os.path.join(dir, f"{name}.step") if step_data else None,
                None,
            ),
        )

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: log_messages.append(msg),
        )

        assert "No symbol data" in " ".join(log_messages)

    def test_import_to_library_project(self, tmp_path, monkeypatch):
        """Test import to project library."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=True, with_3d=False)
        fake_fp = self._make_fake_footprint()
        fake_sym = self._make_fake_symbol()

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *a, **k: fake_sym)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "write_symbol", lambda *a, **k: '  (symbol "TestPart")\n')

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            use_global=False,
            log=lambda msg: log_messages.append(msg),
        )

        assert (tmp_path / "TestLib.pretty" / "TestPart.kicad_mod").exists()
        assert (tmp_path / "TestLib.kicad_sym").exists()
        assert "Project library tables updated" in " ".join(log_messages)

    def test_import_to_library_global(self, tmp_path, monkeypatch):
        """Test import to global library."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=True, with_3d=False)
        fake_fp = self._make_fake_footprint()
        fake_sym = self._make_fake_symbol()

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *a, **k: fake_sym)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "write_symbol", lambda *a, **k: '  (symbol "TestPart")\n')
        monkeypatch.setattr(importer, "update_global_lib_tables", lambda *a, **k: None)

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            use_global=True,
            log=lambda msg: log_messages.append(msg),
        )

        assert "Global library tables updated" in " ".join(log_messages)

    def test_import_with_3d_model(self, tmp_path, monkeypatch):
        """Test import with 3D model download."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=False, with_3d=True)
        fake_fp = self._make_fake_footprint()

        def fake_save(dir, name, step_data=None, wrl_source=None):
            step_path = os.path.join(dir, f"{name}.step")
            wrl_path = os.path.join(dir, f"{name}.wrl")
            os.makedirs(dir, exist_ok=True)
            if step_data:
                with open(step_path, "wb") as f:
                    f.write(step_data)
            if wrl_source:
                with open(wrl_path, "w") as f:
                    f.write("WRL")
            return (
                step_path if step_data else None,
                wrl_path if wrl_source else None,
            )

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "download_step", lambda _: b"STEP")
        monkeypatch.setattr(importer, "download_wrl_source", lambda _: "wrl-src")
        monkeypatch.setattr(importer, "save_models", fake_save)

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            use_global=False,
            log=lambda msg: log_messages.append(msg),
        )

        assert "Downloading 3D model" in " ".join(log_messages)
        assert "STEP saved" in " ".join(log_messages)
        assert "WRL saved" in " ".join(log_messages)

    def test_import_3d_model_skipped_without_overwrite(self, tmp_path, monkeypatch):
        """Test that existing 3D models are skipped without overwrite."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=False, with_3d=True)
        fake_fp = self._make_fake_footprint()

        # Pre-create the 3D model files
        models_dir = tmp_path / "TestLib.3dshapes"
        models_dir.mkdir(parents=True)
        (models_dir / "TestPart.step").write_text("existing")
        (models_dir / "TestPart.wrl").write_text("existing")

        def _step_should_not_download(_):
            raise AssertionError("STEP download should be skipped for existing files")

        def fake_save(dir, name, step_data=None, wrl_source=None):
            step_path = os.path.join(dir, f"{name}.step")
            wrl_path = os.path.join(dir, f"{name}.wrl")
            return step_path, wrl_path

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "download_step", _step_should_not_download)
        # WRL source is always fetched for 3D model offset computation
        monkeypatch.setattr(importer, "download_wrl_source", lambda _: "v 0 0 0\n")
        monkeypatch.setattr(importer, "save_models", fake_save)

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            overwrite=False,
            log=lambda msg: log_messages.append(msg),
        )

        assert "STEP skipped" in " ".join(log_messages)
        assert "WRL skipped" in " ".join(log_messages)

    def test_import_no_3d_model(self, tmp_path, monkeypatch):
        """Test import when no 3D model is available."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=False, with_3d=False)
        fake_fp = self._make_fake_footprint()

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            log=lambda msg: log_messages.append(msg),
        )

        assert "No 3D model available" in " ".join(log_messages)

    def test_import_footprint_skipped_without_overwrite(self, tmp_path, monkeypatch):
        """Test that existing footprints are skipped without overwrite."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=False, with_3d=False)
        fake_fp = self._make_fake_footprint()

        # Pre-create the footprint
        fp_dir = tmp_path / "TestLib.pretty"
        fp_dir.mkdir(parents=True)
        (fp_dir / "TestPart.kicad_mod").write_text("existing")

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            overwrite=False,
            log=lambda msg: log_messages.append(msg),
        )

        assert "Skipped:" in " ".join(log_messages)

    def test_import_symbol_skipped_without_overwrite(self, tmp_path, monkeypatch):
        """Test that existing symbols are skipped without overwrite."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=True, with_3d=False)
        fake_fp = self._make_fake_footprint()
        fake_sym = self._make_fake_symbol()

        # Pre-create the symbol library with the symbol
        sym_path = tmp_path / "TestLib.kicad_sym"
        sym_path.write_text('(kicad_symbol_lib\n  (version 20241209)\n  (generator "test")\n  (symbol "TestPart")\n)\n')

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *a, **k: fake_sym)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "write_symbol", lambda *a, **k: '  (symbol "TestPart")\n')

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            overwrite=False,
            log=lambda msg: log_messages.append(msg),
        )

        assert "Symbol skipped" in " ".join(log_messages)

    def test_import_with_footprint_model(self, tmp_path, monkeypatch):
        """Test import when footprint has embedded model info."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=False, with_3d=False)
        fake_fp = self._make_fake_footprint(with_model=True)

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "download_step", lambda _: None)
        monkeypatch.setattr(importer, "download_wrl_source", lambda _: None)
        monkeypatch.setattr(importer, "save_models", lambda *a, **k: (None, None))

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            log=lambda msg: log_messages.append(msg),
        )

        # Should use model from footprint
        assert "Downloading 3D model" in " ".join(log_messages)

    def test_import_newly_created_lib_tables(self, tmp_path, monkeypatch):
        """Test note about reopening project when lib tables are created."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=False, with_3d=False)
        fake_fp = self._make_fake_footprint()

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            log=lambda msg: log_messages.append(msg),
        )

        # First import creates new lib tables
        assert "NOTE: Reopen project" in " ".join(log_messages)

    def test_import_with_global_model_path(self, tmp_path, monkeypatch):
        """Test that global imports use absolute model paths."""
        log_messages = []

        fake_comp = self._make_fake_comp(with_symbol=False, with_3d=True)
        fake_fp = self._make_fake_footprint()

        def fake_save(dir, name, step_data=None, wrl_source=None):
            step_path = os.path.join(dir, f"{name}.step")
            os.makedirs(dir, exist_ok=True)
            if step_data:
                with open(step_path, "wb") as f:
                    f.write(step_data)
            return (step_path if step_data else None, None)

        captured_model_path = []

        def capture_write_footprint(*args, **kwargs):
            captured_model_path.append(kwargs.get("model_path", ""))
            return "(footprint TestPart)\n"

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "write_footprint", capture_write_footprint)
        monkeypatch.setattr(importer, "download_step", lambda _: b"STEP")
        monkeypatch.setattr(importer, "download_wrl_source", lambda _: None)
        monkeypatch.setattr(importer, "save_models", fake_save)
        monkeypatch.setattr(importer, "update_global_lib_tables", lambda *a, **k: None)

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            use_global=True,
            log=lambda msg: log_messages.append(msg),
        )

        # Global imports should use absolute paths
        assert len(captured_model_path) == 1
        assert "${KIPRJMOD}" not in captured_model_path[0]


class TestImportBackslashPaths:
    """Tests for backslash path handling in importer."""

    def _make_fake_comp(self, with_3d=True):
        """Create a fake component dict for testing."""
        return {
            "title": "TestPart",
            "prefix": "U",
            "description": "Test description",
            "datasheet": "https://example.com/ds.pdf",
            "manufacturer": "ACME",
            "manufacturer_part": "MPN123",
            "footprint_data": {"dataStr": {"shape": []}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
            "uuid_3d": "model_uuid_123" if with_3d else "",
        }

    def _make_fake_footprint(self):
        """Create a fake footprint for testing."""
        fp = EEFootprint()
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0, rotation=0)
        fp.pads.append(pad)
        return fp

    def test_global_model_path_uses_forward_slashes(self, tmp_path, monkeypatch):
        """Ensure model_path passed to write_footprint never contains backslashes."""
        fake_comp = self._make_fake_comp(with_3d=True)
        fake_fp = self._make_fake_footprint()

        # Make ensure_lib_structure return paths with backslashes (simulating Windows)
        def fake_ensure(base_path, lib_name="JLCImport"):
            models_dir = base_path + "\\TestLib.3dshapes"
            os.makedirs(os.path.join(base_path, "TestLib.3dshapes"), exist_ok=True)
            os.makedirs(os.path.join(base_path, "TestLib.pretty"), exist_ok=True)
            return {
                "sym_path": os.path.join(base_path, f"{lib_name}.kicad_sym"),
                "fp_dir": os.path.join(base_path, "TestLib.pretty"),
                "models_dir": models_dir,
            }

        def fake_save(dir, name, step_data=None, wrl_source=None):
            actual_dir = dir.replace("\\", "/")
            os.makedirs(actual_dir, exist_ok=True)
            wrl_path = os.path.join(actual_dir, f"{name}.wrl")
            with open(wrl_path, "w") as f:
                f.write("WRL")
            return (None, wrl_path)

        captured_model_path = []

        def capture_write_footprint(*args, **kwargs):
            captured_model_path.append(kwargs.get("model_path", ""))
            return "(footprint TestPart)\n"

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "write_footprint", capture_write_footprint)
        monkeypatch.setattr(importer, "download_step", lambda _: b"STEP")
        monkeypatch.setattr(importer, "download_wrl_source", lambda _: "wrl-src")
        monkeypatch.setattr(importer, "save_models", fake_save)
        monkeypatch.setattr(importer, "ensure_lib_structure", fake_ensure)
        monkeypatch.setattr(importer, "update_global_lib_tables", lambda *a, **k: None)

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            use_global=True,
            log=lambda msg: None,
        )

        assert len(captured_model_path) == 1
        assert "\\" not in captured_model_path[0], f"Backslash found in model path: {captured_model_path[0]}"


class TestBuildDescription:
    """Tests for _build_description helper."""

    def test_uses_existing_description(self):
        comp = {"description": "A voltage regulator", "title": "ME6217"}
        assert _build_description(comp) == "A voltage regulator"

    def test_empty_description_builds_from_metadata(self):
        comp = {
            "description": "",
            "title": "XH-5A",
            "manufacturer_part": "ME6217C33M5G",
            "package": "SOT-23-5",
            "manufacturer": "MICRONE",
        }
        assert _build_description(comp) == "ME6217C33M5G; SOT-23-5; MICRONE"

    def test_description_matching_title_builds_from_metadata(self):
        comp = {
            "description": "XH-5A",
            "title": "XH-5A",
            "manufacturer_part": "MPN1",
            "package": "DIP-8",
            "manufacturer": "",
        }
        assert _build_description(comp) == "MPN1; DIP-8"

    def test_no_metadata_returns_empty(self):
        comp = {"description": "", "title": "X"}
        assert _build_description(comp) == ""

    def test_partial_metadata(self):
        comp = {
            "description": "",
            "title": "X",
            "manufacturer": "Acme",
        }
        assert _build_description(comp) == "Acme"


class TestBuildKeywords:
    """Tests for _build_keywords helper."""

    def test_all_fields(self):
        comp = {
            "lcsc_id": "C427602",
            "manufacturer_part": "ME6217C33M5G",
            "manufacturer": "MICRONE",
            "package": "SOT-23-5",
        }
        result = _build_keywords(comp)
        assert result == "C427602 ME6217C33M5G MICRONE SOT-23-5"

    def test_empty_fields_excluded(self):
        comp = {
            "lcsc_id": "C100",
            "manufacturer_part": "",
            "manufacturer": "Acme",
            "package": "",
        }
        result = _build_keywords(comp)
        assert result == "Acme C100"

    def test_no_fields_returns_empty(self):
        comp = {}
        assert _build_keywords(comp) == ""

    def test_deduplication(self):
        """If a value appears in multiple fields, it should appear only once."""
        comp = {
            "lcsc_id": "C100",
            "manufacturer_part": "C100",
            "manufacturer": "Acme",
        }
        result = _build_keywords(comp)
        assert result == "Acme C100"

    def test_search_description_not_in_keywords(self):
        """search_description should NOT appear in keywords (it belongs in description)."""
        comp = {
            "lcsc_id": "C427602",
            "manufacturer_part": "ME6217C33M5G",
            "manufacturer": "MICRONE",
            "package": "SOT-23-5",
            "search_description": "LDO Voltage Regulators SOT-89-3",
        }
        result = _build_keywords(comp)
        assert "LDO Voltage Regulators" not in result
        assert "C427602" in result


class TestSearchResultMerge:
    """Tests for search_result parameter in import_component."""

    def _make_fake_comp(self):
        return {
            "title": "TestPart",
            "prefix": "U",
            "description": "Test description",
            "datasheet": "https://example.com/ds.pdf",
            "manufacturer": "UMW(\u53cb\u53f0\u534a\u5bfc\u4f53)",
            "manufacturer_part": "MPN123",
            "footprint_data": {"dataStr": {"shape": []}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
        }

    def _make_fake_footprint(self):
        fp = EEFootprint()
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0, rotation=0)
        fp.pads.append(pad)
        return fp

    def test_brand_overrides_manufacturer(self, tmp_path, monkeypatch):
        """search_result brand should replace c_para manufacturer."""
        fake_comp = self._make_fake_comp()
        fake_fp = self._make_fake_footprint()

        captured_kwargs = {}

        def capture_write_symbol(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return '  (symbol "TestPart")\n'

        fake_comp["symbol_data_list"] = [{"dataStr": {"shape": []}}]
        fake_sym = EESymbol()
        fake_sym.pins.append(
            EEPin(number="1", name="VCC", x=0, y=0, rotation=0, length=2.54, electrical_type="power_in")
        )

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *a, **k: fake_sym)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "write_symbol", capture_write_symbol)

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: None,
            search_result={"brand": "UMW", "description": "LDO Voltage Regulators SOT-89-3"},
        )

        assert captured_kwargs["manufacturer"] == "UMW"
        assert captured_kwargs["description"] == "LDO Voltage Regulators SOT-89-3"

    def test_none_search_result_preserves_behavior(self, tmp_path, monkeypatch):
        """search_result=None should use existing c_para manufacturer."""
        fake_comp = self._make_fake_comp()
        fake_fp = self._make_fake_footprint()

        captured_kwargs = {}

        def capture_write_symbol(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return '  (symbol "TestPart")\n'

        fake_comp["symbol_data_list"] = [{"dataStr": {"shape": []}}]
        fake_sym = EESymbol()
        fake_sym.pins.append(
            EEPin(number="1", name="VCC", x=0, y=0, rotation=0, length=2.54, electrical_type="power_in")
        )

        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *a, **k: fake_sym)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "write_symbol", capture_write_symbol)

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: None,
        )

        assert captured_kwargs["manufacturer"] == "UMW(\u53cb\u53f0\u534a\u5bfc\u4f53)"


class TestConfirmMetadata:
    """Tests for confirm_metadata callback in import_component."""

    def _make_fake_comp(self):
        return {
            "title": "TestPart",
            "prefix": "U",
            "description": "Test description",
            "datasheet": "https://example.com/ds.pdf",
            "manufacturer": "ACME",
            "manufacturer_part": "MPN123",
            "lcsc_id": "C123",
            "package": "SOT-23",
            "footprint_data": {"dataStr": {"shape": []}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [{"dataStr": {"shape": []}}],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
        }

    def _make_fake_footprint(self):
        fp = EEFootprint()
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0, rotation=0)
        fp.pads.append(pad)
        return fp

    def _make_fake_symbol(self):
        sym = EESymbol()
        sym.pins.append(EEPin(number="1", name="VCC", x=0, y=0, rotation=0, length=2.54, electrical_type="power_in"))
        return sym

    def _patch_importer(self, monkeypatch, fake_comp, fake_fp, fake_sym, capture_sym=None, capture_fp=None):
        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *a, **k: fake_sym)
        monkeypatch.setattr(
            importer,
            "write_footprint",
            capture_fp or (lambda *a, **k: "(footprint TestPart)\n"),
        )
        monkeypatch.setattr(
            importer,
            "write_symbol",
            capture_sym or (lambda *a, **k: '  (symbol "TestPart")\n'),
        )

    def test_callback_receives_correct_defaults(self, tmp_path, monkeypatch):
        """confirm_metadata receives the computed description, keywords, manufacturer."""
        fake_comp = self._make_fake_comp()
        received = []

        def capture_metadata(metadata):
            received.append(metadata)
            return metadata

        self._patch_importer(monkeypatch, fake_comp, self._make_fake_footprint(), self._make_fake_symbol())

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: None,
            confirm_metadata=capture_metadata,
        )

        assert len(received) == 1
        meta = received[0]
        assert meta["description"] == "Test description"
        assert meta["manufacturer"] == "ACME"
        assert "ACME" in meta["keywords"]
        assert "MPN123" in meta["keywords"]

    def test_edited_values_used_in_output(self, tmp_path, monkeypatch):
        """Edited metadata from callback should be passed to write_symbol and write_footprint."""
        fake_comp = self._make_fake_comp()
        sym_kwargs = {}
        fp_kwargs = {}

        def capture_sym(*args, **kwargs):
            sym_kwargs.update(kwargs)
            return '  (symbol "TestPart")\n'

        def capture_fp(*args, **kwargs):
            fp_kwargs.update(kwargs)
            return "(footprint TestPart)\n"

        def edit_metadata(metadata):
            return {
                "description": "Custom description",
                "keywords": "custom kw",
                "manufacturer": "Custom Mfg",
            }

        self._patch_importer(
            monkeypatch,
            fake_comp,
            self._make_fake_footprint(),
            self._make_fake_symbol(),
            capture_sym=capture_sym,
            capture_fp=capture_fp,
        )

        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: None,
            confirm_metadata=edit_metadata,
        )

        assert sym_kwargs["description"] == "Custom description"
        assert sym_kwargs["keywords"] == "custom kw"
        assert sym_kwargs["manufacturer"] == "Custom Mfg"
        assert fp_kwargs["description"] == "Custom description"
        assert fp_kwargs["keywords"] == "custom kw"

    def test_returning_none_cancels_import(self, tmp_path, monkeypatch):
        """Returning None from confirm_metadata should cancel the import."""
        fake_comp = self._make_fake_comp()
        self._patch_importer(monkeypatch, fake_comp, self._make_fake_footprint(), self._make_fake_symbol())

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: None,
            confirm_metadata=lambda m: None,
        )

        assert result is None

    def test_none_callback_preserves_behavior(self, tmp_path, monkeypatch):
        """confirm_metadata=None should use default metadata without prompting."""
        fake_comp = self._make_fake_comp()
        sym_kwargs = {}

        def capture_sym(*args, **kwargs):
            sym_kwargs.update(kwargs)
            return '  (symbol "TestPart")\n'

        self._patch_importer(
            monkeypatch,
            fake_comp,
            self._make_fake_footprint(),
            self._make_fake_symbol(),
            capture_sym=capture_sym,
        )

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: None,
            confirm_metadata=None,
        )

        assert result is not None
        assert sym_kwargs["description"] == "Test description"
        assert sym_kwargs["manufacturer"] == "ACME"


class TestConfirmOverwrite:
    """Tests for confirm_overwrite callback in import_component."""

    def _make_fake_comp(self):
        return {
            "title": "TestPart",
            "prefix": "U",
            "description": "Test description",
            "datasheet": "https://example.com/ds.pdf",
            "manufacturer": "ACME",
            "manufacturer_part": "MPN123",
            "lcsc_id": "C123",
            "package": "SOT-23",
            "footprint_data": {"dataStr": {"shape": []}},
            "fp_origin_x": 0,
            "fp_origin_y": 0,
            "symbol_data_list": [{"dataStr": {"shape": []}}],
            "sym_origin_x": 0,
            "sym_origin_y": 0,
        }

    def _make_fake_footprint(self):
        fp = EEFootprint()
        pad = EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0, rotation=0)
        fp.pads.append(pad)
        return fp

    def _make_fake_symbol(self):
        sym = EESymbol()
        sym.pins.append(EEPin(number="1", name="VCC", x=0, y=0, rotation=0, length=2.54, electrical_type="power_in"))
        return sym

    def _patch_importer(self, monkeypatch, fake_comp, fake_fp, fake_sym):
        monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
        monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
        monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *a, **k: fake_sym)
        monkeypatch.setattr(importer, "write_footprint", lambda *a, **k: "(footprint TestPart)\n")
        monkeypatch.setattr(importer, "write_symbol", lambda *a, **k: '  (symbol "TestPart")\n')

    def test_callback_called_when_files_exist_returns_true(self, tmp_path, monkeypatch):
        """confirm_overwrite returning True should proceed with overwrite."""
        fake_comp = self._make_fake_comp()
        self._patch_importer(monkeypatch, fake_comp, self._make_fake_footprint(), self._make_fake_symbol())

        # Pre-create the footprint so it already exists
        fp_dir = tmp_path / "TestLib.pretty"
        fp_dir.mkdir(parents=True)
        (fp_dir / "TestPart.kicad_mod").write_text("existing")

        callback_args = []

        def confirm(name, existing):
            callback_args.append((name, existing))
            return True

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            overwrite=False,
            log=lambda msg: None,
            confirm_overwrite=confirm,
        )

        assert result is not None
        assert len(callback_args) == 1
        assert callback_args[0][0] == "TestPart"
        assert "footprint" in callback_args[0][1]

    def test_callback_returns_false_cancels_import(self, tmp_path, monkeypatch):
        """confirm_overwrite returning False should cancel the import."""
        fake_comp = self._make_fake_comp()
        self._patch_importer(monkeypatch, fake_comp, self._make_fake_footprint(), self._make_fake_symbol())

        # Pre-create the footprint
        fp_dir = tmp_path / "TestLib.pretty"
        fp_dir.mkdir(parents=True)
        (fp_dir / "TestPart.kicad_mod").write_text("existing")

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            overwrite=False,
            log=lambda msg: None,
            confirm_overwrite=lambda name, existing: False,
        )

        assert result is None

    def test_no_callback_preserves_existing_behavior(self, tmp_path, monkeypatch):
        """Without confirm_overwrite, overwrite=False should skip silently."""
        fake_comp = self._make_fake_comp()
        self._patch_importer(monkeypatch, fake_comp, self._make_fake_footprint(), self._make_fake_symbol())
        log_messages = []

        # Pre-create the footprint
        fp_dir = tmp_path / "TestLib.pretty"
        fp_dir.mkdir(parents=True)
        (fp_dir / "TestPart.kicad_mod").write_text("existing")

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            overwrite=False,
            log=lambda msg: log_messages.append(msg),
        )

        assert result is not None
        assert "Skipped:" in " ".join(log_messages)

    def test_callback_not_called_when_no_existing_files(self, tmp_path, monkeypatch):
        """confirm_overwrite should not be called when nothing exists."""
        fake_comp = self._make_fake_comp()
        self._patch_importer(monkeypatch, fake_comp, self._make_fake_footprint(), self._make_fake_symbol())

        callback_called = []

        def confirm(name, existing):
            callback_called.append(True)
            return True

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            overwrite=False,
            log=lambda msg: None,
            confirm_overwrite=confirm,
        )

        assert result is not None
        assert len(callback_called) == 0
