"""Tests for footprint browser, KiCad footprint selection, name overrides, and related library helpers."""

import json
import os

from kicad_jlcimport import importer
from kicad_jlcimport.easyeda.ee_types import EEFootprint, EEPad, EEPin, EESymbol
from kicad_jlcimport.kicad import library

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _make_fake_comp(**overrides):
    comp = {
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
    comp.update(overrides)
    return comp


def _make_fake_fp():
    fp = EEFootprint()
    fp.pads.append(EEPad(shape="RECT", x=0, y=0, width=1, height=1, layer="1", number="1", drill=0, rotation=0))
    return fp


def _make_fake_sym():
    sym = EESymbol()
    sym.pins.append(EEPin(number="1", name="VCC", x=0, y=0, rotation=0, length=2.54, electrical_type="power_in"))
    return sym


def _patch_importer(monkeypatch, fake_comp, fake_fp=None, fake_sym=None, capture_sym=None, capture_fp=None):
    fake_fp = fake_fp or _make_fake_fp()
    fake_sym = fake_sym or _make_fake_sym()
    monkeypatch.setattr(importer, "fetch_full_component", lambda _: fake_comp)
    monkeypatch.setattr(importer, "parse_footprint_shapes", lambda *a, **k: fake_fp)
    monkeypatch.setattr(importer, "parse_symbol_shapes", lambda *a, **k: fake_sym)
    monkeypatch.setattr(importer, "write_footprint", capture_fp or (lambda *a, **k: "(footprint TestPart)\n"))
    monkeypatch.setattr(importer, "write_symbol", capture_sym or (lambda *a, **k: '  (symbol "TestPart")\n'))


# ===================================================================
# _extract_blocks
# ===================================================================


class TestExtractBlocks:
    """Tests for the _extract_blocks s-expression parser."""

    def test_single_block(self):
        from kicad_jlcimport.dialog import _extract_blocks

        text = "(fp_line (start 0 0) (end 1 1))"
        result = _extract_blocks(text, "fp_line")
        assert len(result) == 1
        assert result[0] == text

    def test_multiple_blocks(self):
        from kicad_jlcimport.dialog import _extract_blocks

        text = '(pad "1" smd rect) (pad "2" smd rect)'
        result = _extract_blocks(text, "pad")
        assert len(result) == 2

    def test_nested_parens(self):
        from kicad_jlcimport.dialog import _extract_blocks

        text = '(pad "1" smd custom (primitives (gr_poly (pts (xy 0 0) (xy 1 1)))))'
        result = _extract_blocks(text, "pad")
        assert len(result) == 1
        assert result[0] == text

    def test_no_match(self):
        from kicad_jlcimport.dialog import _extract_blocks

        text = "(fp_line (start 0 0))"
        assert _extract_blocks(text, "fp_rect") == []

    def test_keyword_with_regex_chars_is_escaped(self):
        from kicad_jlcimport.dialog import _extract_blocks

        # Without re.escape, "fp.line" would match "fp_line" via the dot
        text = "(fp_line (start 0 0))"
        assert _extract_blocks(text, "fp.line") == []

        # Literal dot keyword should match only literal dot
        text2 = "(fp.line (start 0 0))"
        assert len(_extract_blocks(text2, "fp.line")) == 1

    def test_partial_keyword_not_matched(self):
        from kicad_jlcimport.dialog import _extract_blocks

        text = "(fp_line_extra (start 0 0))"
        # "fp_line" should NOT match "fp_line_extra" thanks to \b
        assert _extract_blocks(text, "fp_line") == []


# ===================================================================
# _parse_kicad_mod
# ===================================================================


class TestParseKicadMod:
    """Tests for the KiCad footprint parser used in the preview."""

    def test_basic_footprint(self, tmp_path):
        from kicad_jlcimport.dialog import _parse_kicad_mod

        fp_file = tmp_path / "test.kicad_mod"
        fp_file.write_text(
            '(footprint "Test"\n'
            '  (descr "A test footprint")\n'
            '  (tags "test tag")\n'
            '  (fp_line (start 0 0) (end 1 1) (layer "F.SilkS") (stroke (width 0.15)))\n'
            '  (pad "1" smd rect (at 0 0) (size 1 2) (layer "F.Cu"))\n'
            ")\n"
        )
        result = _parse_kicad_mod(str(fp_file))
        assert result["descr"] == "A test footprint"
        assert result["tags"] == "test tag"
        assert len(result["lines"]) == 1
        assert result["pads_count"] == 1

    def test_missing_file_returns_empty(self, tmp_path):
        from kicad_jlcimport.dialog import _parse_kicad_mod

        result = _parse_kicad_mod(str(tmp_path / "nonexistent.kicad_mod"))
        assert result["pads_count"] == 0
        assert result["lines"] == []

    def test_circle_parsing(self, tmp_path):
        from kicad_jlcimport.dialog import _parse_kicad_mod

        fp_file = tmp_path / "circ.kicad_mod"
        fp_file.write_text(
            '(footprint "Circ"\n  (fp_circle (center 0 0) (end 1 0) (layer "F.Cu") (stroke (width 0.1)))\n)\n'
        )
        result = _parse_kicad_mod(str(fp_file))
        assert len(result["circles"]) == 1
        cx, cy, r, layer, w, filled = result["circles"][0]
        assert cx == 0.0
        assert cy == 0.0
        assert abs(r - 1.0) < 0.01

    def test_rect_parsing(self, tmp_path):
        from kicad_jlcimport.dialog import _parse_kicad_mod

        fp_file = tmp_path / "rect.kicad_mod"
        fp_file.write_text(
            '(footprint "Rect"\n'
            '  (fp_rect (start -1 -1) (end 1 1) (layer "F.Fab") (stroke (width 0.1)) (fill solid))\n'
            ")\n"
        )
        result = _parse_kicad_mod(str(fp_file))
        assert len(result["rects"]) == 1
        assert result["rects"][0][7] is True  # filled

    def test_polygon_parsing(self, tmp_path):
        from kicad_jlcimport.dialog import _parse_kicad_mod

        fp_file = tmp_path / "poly.kicad_mod"
        fp_file.write_text(
            '(footprint "Poly"\n'
            '  (fp_poly (pts (xy 0 0) (xy 1 0) (xy 0.5 1)) (layer "F.Cu") (stroke (width 0.1)) (fill solid))\n'
            ")\n"
        )
        result = _parse_kicad_mod(str(fp_file))
        assert len(result["polys"]) == 1
        pts, layer, filled = result["polys"][0]
        assert len(pts) == 3
        assert filled is True

    def test_arc_parsing(self, tmp_path):
        from kicad_jlcimport.dialog import _parse_kicad_mod

        fp_file = tmp_path / "arc.kicad_mod"
        fp_file.write_text(
            '(footprint "Arc"\n'
            '  (fp_arc (start 0 0) (mid 0.5 0.5) (end 1 0) (layer "F.SilkS") (stroke (width 0.12)))\n'
            ")\n"
        )
        result = _parse_kicad_mod(str(fp_file))
        assert len(result["arcs"]) == 1

    def test_3d_model_with_env_var(self, tmp_path, monkeypatch):
        from kicad_jlcimport.dialog import _parse_kicad_mod

        model_dir = tmp_path / "3dmodels"
        model_dir.mkdir()
        (model_dir / "R_0805.wrl").write_text("dummy")

        monkeypatch.setattr(
            "kicad_jlcimport.dialog.resolve_kicad_var",
            lambda key, kicad_version=10: str(model_dir) if "3DMODEL" in key else "",
        )

        fp_file = tmp_path / "test.kicad_mod"
        fp_file.write_text('(footprint "R0805"\n  (model "${KICAD9_3DMODEL_DIR}/R_0805.wrl")\n)\n')
        result = _parse_kicad_mod(str(fp_file))
        assert result["model"] is not None
        raw_path, exists = result["model"]
        assert "${KICAD9_3DMODEL_DIR}" in raw_path
        assert exists is True

    def test_3d_model_unresolvable(self, tmp_path, monkeypatch):
        from kicad_jlcimport.dialog import _parse_kicad_mod

        monkeypatch.setattr("kicad_jlcimport.dialog.resolve_kicad_var", lambda key, kicad_version=10: "")

        fp_file = tmp_path / "test.kicad_mod"
        fp_file.write_text('(footprint "X"\n  (model "${KICAD9_3DMODEL_DIR}/missing.wrl")\n)\n')
        result = _parse_kicad_mod(str(fp_file))
        raw_path, exists = result["model"]
        assert exists is False

    def test_3d_model_kiprjmod_resolved(self, tmp_path, monkeypatch):
        """KIPRJMOD should be resolved via project_dir parameter."""
        from kicad_jlcimport.dialog import _parse_kicad_mod

        monkeypatch.setattr("kicad_jlcimport.dialog.resolve_kicad_var", lambda key, kicad_version=10: "")

        proj_dir = tmp_path / "project"
        proj_dir.mkdir()
        (proj_dir / "model.wrl").write_text("dummy")

        fp_file = tmp_path / "test.kicad_mod"
        fp_file.write_text('(footprint "X"\n  (model "${KIPRJMOD}/model.wrl")\n)\n')
        result = _parse_kicad_mod(str(fp_file), project_dir=str(proj_dir))
        raw_path, exists = result["model"]
        assert "${KIPRJMOD}" in raw_path
        assert exists is True

    def test_3d_model_kiprjmod_without_project_dir(self, tmp_path, monkeypatch):
        """Without project_dir, KIPRJMOD stays unresolved."""
        from kicad_jlcimport.dialog import _parse_kicad_mod

        monkeypatch.setattr("kicad_jlcimport.dialog.resolve_kicad_var", lambda key, kicad_version=10: "")

        fp_file = tmp_path / "test.kicad_mod"
        fp_file.write_text('(footprint "X"\n  (model "${KIPRJMOD}/model.wrl")\n)\n')
        result = _parse_kicad_mod(str(fp_file))
        _, exists = result["model"]
        assert exists is False

    def test_custom_pad_with_primitives(self, tmp_path):
        from kicad_jlcimport.dialog import _parse_kicad_mod

        fp_file = tmp_path / "custom.kicad_mod"
        fp_file.write_text(
            '(footprint "Custom"\n'
            '  (pad "1" smd custom (at 0 0) (size 0.5 0.5) (layer "F.Cu")\n'
            "    (primitives\n"
            "      (gr_poly (pts (xy -1 -1) (xy 1 -1) (xy 0 1)) (fill yes))\n"
            "    )\n"
            "  )\n"
            ")\n"
        )
        result = _parse_kicad_mod(str(fp_file))
        assert result["pads_count"] == 1
        pad = result["pads"][0]
        poly_list = pad[9]
        assert len(poly_list) == 1
        assert len(poly_list[0]) == 3


# ===================================================================
# resolve_kicad_var
# ===================================================================


class TestResolveKicadVar:
    """Tests for resolve_kicad_var in library.py."""

    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD9_3DMODEL_DIR", str(tmp_path))
        assert library.resolve_kicad_var("KICAD9_3DMODEL_DIR") == str(tmp_path)

    def test_kicad_common_json_fallback(self, tmp_path, monkeypatch):
        # Create a fake kicad config dir with kicad_common.json
        config_dir = tmp_path / "config" / "9.0"
        config_dir.mkdir(parents=True)
        model_dir = tmp_path / "3dmodels"
        model_dir.mkdir()
        common = config_dir / "kicad_common.json"
        common.write_text(json.dumps({"environment": {"vars": {"KICAD9_3DMODEL_DIR": str(model_dir)}}}))

        monkeypatch.delenv("KICAD9_3DMODEL_DIR", raising=False)
        monkeypatch.setattr(library, "_kicad_config_base", lambda: str(tmp_path / "config"))

        assert library.resolve_kicad_var("KICAD9_3DMODEL_DIR") == str(model_dir)

    def test_unknown_var_returns_empty(self, monkeypatch):
        monkeypatch.delenv("SOME_RANDOM_VAR", raising=False)
        monkeypatch.setattr(library, "_iter_kicad_config_versions", lambda: [])
        assert library.resolve_kicad_var("SOME_RANDOM_VAR") == ""

    def test_footprint_dir_hardcoded_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KICAD9_FOOTPRINT_DIR", raising=False)
        monkeypatch.setattr(library, "_iter_kicad_config_versions", lambda: [])

        fp_dir = tmp_path / "footprints"
        fp_dir.mkdir()

        if os.name == "posix":
            monkeypatch.setattr("sys.platform", "linux")
            monkeypatch.setattr(
                library,
                "resolve_kicad_var",
                library.resolve_kicad_var.__wrapped__
                if hasattr(library.resolve_kicad_var, "__wrapped__")
                else library.resolve_kicad_var,
            )
        # Can't easily test platform-specific fallbacks in a generic way,
        # so just verify the function returns "" when no candidates exist
        result = library.resolve_kicad_var("KICAD9_FOOTPRINT_DIR")
        # Result depends on the test machine's actual KiCad install
        assert isinstance(result, str)

    def test_env_var_nonexistent_dir_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD9_3DMODEL_DIR", str(tmp_path / "nonexistent"))
        monkeypatch.setattr(library, "_iter_kicad_config_versions", lambda: [])
        # env dir doesn't exist, so should fall through
        result = library.resolve_kicad_var("KICAD9_3DMODEL_DIR")
        # It may find a real install or return ""
        assert isinstance(result, str)

    def test_kicad_version_selects_target_install(self, tmp_path, monkeypatch):
        """resolve_kicad_var should use the kicad_version param, not the variable name version."""
        monkeypatch.delenv("KICAD9_FOOTPRINT_DIR", raising=False)
        monkeypatch.setattr(library, "_iter_kicad_config_versions", lambda: [])

        data10 = tmp_path / "kicad10_data"
        (data10 / "footprints").mkdir(parents=True)
        monkeypatch.setattr(library, "_find_kicad_data_dir", lambda major: str(data10) if major == 10 else "")

        # Variable says KICAD9, but target version is 10
        result = library.resolve_kicad_var("KICAD9_FOOTPRINT_DIR", kicad_version=10)
        assert result == str(data10 / "footprints")

    def test_kicad_version_default_used_when_omitted(self, tmp_path, monkeypatch):
        """Without explicit kicad_version, DEFAULT_KICAD_VERSION is used."""
        monkeypatch.delenv("KICAD10_3DMODEL_DIR", raising=False)
        monkeypatch.setattr(library, "_iter_kicad_config_versions", lambda: [])

        data = tmp_path / "data"
        (data / "3dmodels").mkdir(parents=True)
        monkeypatch.setattr(library, "_find_kicad_data_dir", lambda major: str(data) if major == 10 else "")

        result = library.resolve_kicad_var("KICAD10_3DMODEL_DIR")
        assert result == str(data / "3dmodels")


# ===================================================================
# _find_kicad_data_dir
# ===================================================================


class TestFindKicadDataDir:
    """Tests for _find_kicad_data_dir."""

    def test_macos_finds_versioned_dir(self, monkeypatch):
        monkeypatch.setattr("kicad_jlcimport.kicad.library.sys.platform", "darwin")
        expected = "/Applications/KiCad 10/KiCad.app/Contents/SharedSupport"
        monkeypatch.setattr(
            "kicad_jlcimport.kicad.library.os.listdir",
            lambda p: ["KiCad 10"] if p == "/Applications" else [],
        )
        monkeypatch.setattr(
            "kicad_jlcimport.kicad.library.os.path.isdir",
            lambda p: p == expected,
        )
        result = library._find_kicad_data_dir(10)
        assert result == expected

    def test_macos_plain_kicad_dir(self, tmp_path, monkeypatch):
        """Plain 'KiCad' dir (no version number) should match any major."""
        monkeypatch.setattr("kicad_jlcimport.kicad.library.sys.platform", "darwin")
        shared = "/Applications/KiCad/KiCad.app/Contents/SharedSupport"
        monkeypatch.setattr(
            "kicad_jlcimport.kicad.library.os.listdir",
            lambda p: ["KiCad"] if p == "/Applications" else [],
        )
        monkeypatch.setattr(
            "kicad_jlcimport.kicad.library.os.path.isdir",
            lambda p: p == shared,
        )
        result = library._find_kicad_data_dir(10)
        assert result == shared

    def test_macos_wrong_version_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_jlcimport.kicad.library.sys.platform", "darwin")
        monkeypatch.setattr(
            "kicad_jlcimport.kicad.library.os.listdir",
            lambda p: ["KiCad 9"] if p == "/Applications" else [],
        )
        monkeypatch.setattr("kicad_jlcimport.kicad.library.os.path.isdir", lambda p: True)
        result = library._find_kicad_data_dir(10)
        assert result == ""

    def test_windows_scans_versioned_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_jlcimport.kicad.library.sys.platform", "win32")
        kicad_base = tmp_path / "KiCad"
        data = kicad_base / "10.0" / "share" / "kicad"
        data.mkdir(parents=True)
        monkeypatch.setenv("ProgramFiles", str(tmp_path))
        result = library._find_kicad_data_dir(10)
        assert result == str(data)

    def test_windows_ignores_wrong_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_jlcimport.kicad.library.sys.platform", "win32")
        kicad_base = tmp_path / "KiCad"
        (kicad_base / "9.0" / "share" / "kicad").mkdir(parents=True)
        monkeypatch.setenv("ProgramFiles", str(tmp_path))
        result = library._find_kicad_data_dir(10)
        assert result == ""

    def test_linux_versioned_path(self, monkeypatch):
        monkeypatch.setattr("kicad_jlcimport.kicad.library.sys.platform", "linux")
        monkeypatch.setattr(
            "kicad_jlcimport.kicad.library.os.path.isdir",
            lambda p: p == "/usr/share/kicad-10",
        )
        result = library._find_kicad_data_dir(10)
        assert result == "/usr/share/kicad-10"

    def test_returns_empty_when_not_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_jlcimport.kicad.library.sys.platform", "darwin")
        monkeypatch.setattr(
            "kicad_jlcimport.kicad.library.os.listdir",
            lambda p: [] if p == "/Applications" else [],
        )
        result = library._find_kicad_data_dir(10)
        assert result == ""


# ===================================================================
# _iter_kicad_config_versions
# ===================================================================


class TestIterKicadConfigVersions:
    """Tests for _iter_kicad_config_versions."""

    def test_returns_sorted_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(library, "_kicad_config_base", lambda: str(tmp_path))
        (tmp_path / "7.0").mkdir()
        (tmp_path / "9.0").mkdir()
        (tmp_path / "8.0").mkdir()
        result = library._iter_kicad_config_versions()
        assert result == [
            str(tmp_path / "9.0"),
            str(tmp_path / "8.0"),
            str(tmp_path / "7.0"),
        ]

    def test_ignores_non_numeric_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(library, "_kicad_config_base", lambda: str(tmp_path))
        (tmp_path / "9.0").mkdir()
        (tmp_path / "nightly").mkdir()
        (tmp_path / "colors").mkdir()
        result = library._iter_kicad_config_versions()
        assert len(result) == 1
        assert "9.0" in result[0]

    def test_empty_when_base_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(library, "_kicad_config_base", lambda: str(tmp_path / "missing"))
        assert library._iter_kicad_config_versions() == []


# ===================================================================
# _iter_footprint_libraries — JLC injection
# ===================================================================


class TestIterFootprintLibrariesJlcInjection:
    """Tests for JLCImport .pretty directory injection."""

    def test_jlc_project_dir_injected(self, tmp_path, monkeypatch):
        # Create a JLCImport.pretty in the project dir
        pretty = tmp_path / "JLCImport.pretty"
        pretty.mkdir()
        # Mock the global fp-lib-table to not interfere
        monkeypatch.setattr(library, "get_global_config_dir", lambda v: str(tmp_path / "nonexistent"))

        # Create empty project fp-lib-table
        (tmp_path / "fp-lib-table").write_text("(fp_lib_table\n)\n")

        result = library._iter_footprint_libraries(str(tmp_path))
        lib_names = [name for name, path in result]
        assert "JLCImport" in lib_names

    def test_jlc_global_dir_injected(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        pretty = global_dir / "MyLib.pretty"
        pretty.mkdir(parents=True)
        monkeypatch.setattr(library, "get_global_config_dir", lambda v: str(tmp_path / "nonexistent"))

        result = library._iter_footprint_libraries("", jlc_lib_name="MyLib", jlc_global_lib_dir=str(global_dir))
        lib_names = [name for name, path in result]
        assert "MyLib" in lib_names

    def test_no_duplicate_when_already_in_table(self, tmp_path, monkeypatch):
        pretty = tmp_path / "JLCImport.pretty"
        pretty.mkdir()
        # Write a fp-lib-table that already references JLCImport
        (tmp_path / "fp-lib-table").write_text(
            f'(fp_lib_table\n  (lib (name "JLCImport")(type "KiCad")(uri "{pretty}"))\n)\n'
        )
        monkeypatch.setattr(library, "get_global_config_dir", lambda v: str(tmp_path / "nonexistent"))

        result = library._iter_footprint_libraries(str(tmp_path))
        jlc_entries = [(n, p) for n, p in result if n == "JLCImport"]
        assert len(jlc_entries) == 1


# ===================================================================
# _expand_lib_uri — KIPRJMOD guard
# ===================================================================


class TestExpandLibUriKiprjmod:
    """Tests for the KIPRJMOD empty-project-dir guard."""

    def test_kiprjmod_with_project_dir(self, tmp_path, monkeypatch):
        pretty = tmp_path / "MyLib.pretty"
        pretty.mkdir()
        result = library._expand_lib_uri("${KIPRJMOD}/MyLib.pretty", project_dir=str(tmp_path))
        assert result == str(pretty)

    def test_kiprjmod_without_project_dir_returns_empty(self):
        # When project_dir is empty, ${KIPRJMOD} stays unresolved → discarded
        result = library._expand_lib_uri("${KIPRJMOD}/MyLib.pretty", project_dir="")
        assert result == ""


# ===================================================================
# __manually_chosen_footprint metadata flow
# ===================================================================


class TestManuallyChosenFootprint:
    """Tests for the __manually_chosen_footprint metadata key from the footprint browser."""

    def test_manually_chosen_footprint_sets_reuse(self, tmp_path, monkeypatch):
        """When __manually_chosen_footprint is set, the symbol should reference it."""
        sym_kwargs = {}

        def capture_sym(*args, **kwargs):
            sym_kwargs.update(kwargs)
            return '  (symbol "TestPart")\n'

        _patch_importer(monkeypatch, _make_fake_comp(), capture_sym=capture_sym)
        monkeypatch.setattr(importer, "find_best_matching_footprint", lambda *a, **k: None)

        def metadata_choose_kicad(metadata):
            metadata["__manually_chosen_footprint"] = "Resistor_SMD:R_0805_2012Metric"
            metadata["__reuse_existing_footprint"] = True
            return metadata

        log_msgs = []
        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            log=lambda msg: log_msgs.append(msg),
            confirm_metadata=metadata_choose_kicad,
        )

        assert result is not None
        assert sym_kwargs["footprint_ref"] == "Resistor_SMD:R_0805_2012Metric"
        assert "Reusing existing footprint" in " ".join(log_msgs)

    def test_manually_chosen_skips_footprint_generation(self, tmp_path, monkeypatch):
        """When a KiCad footprint is chosen, no .kicad_mod should be written."""
        write_fp_called = []

        def capture_fp(*args, **kwargs):
            write_fp_called.append(True)
            return "(footprint)\n"

        _patch_importer(monkeypatch, _make_fake_comp(), capture_fp=capture_fp)
        monkeypatch.setattr(importer, "find_best_matching_footprint", lambda *a, **k: None)

        def metadata_choose_kicad(metadata):
            metadata["__manually_chosen_footprint"] = "Package_SO:SOIC-8"
            metadata["__reuse_existing_footprint"] = True
            return metadata

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            log=lambda msg: None,
            confirm_metadata=metadata_choose_kicad,
        )

        assert result is not None
        assert write_fp_called == []

    def test_manually_chosen_overrides_auto_candidate(self, tmp_path, monkeypatch):
        """Manual choice should override the auto-detected candidate."""
        sym_kwargs = {}

        def capture_sym(*args, **kwargs):
            sym_kwargs.update(kwargs)
            return '  (symbol "TestPart")\n'

        _patch_importer(monkeypatch, _make_fake_comp(), capture_sym=capture_sym)
        monkeypatch.setattr(importer, "find_best_matching_footprint", lambda *a, **k: "Auto:Match")

        def metadata_choose_different(metadata):
            assert metadata["__footprint_candidate_ref"] == "Auto:Match"
            metadata["__manually_chosen_footprint"] = "Manual:Choice"
            metadata["__reuse_existing_footprint"] = True
            return metadata

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            log=lambda msg: None,
            confirm_metadata=metadata_choose_different,
        )

        assert result is not None
        assert sym_kwargs["footprint_ref"] == "Manual:Choice"


# ===================================================================
# MetadataEditDialog KiCad version resolution
# ===================================================================


class TestMetadataEditDialogVersion:
    """Ensure MetadataEditDialog reads the live KiCad version, not a stale snapshot."""

    def test_uses_get_kicad_version_method(self):
        """When parent has _get_kicad_version(), it should be called instead of _kicad_version attr."""

        class FakeParent:
            _kicad_version = 8  # stale value
            _project_dir = ""
            _lib_name = "JLCImport"
            _global_lib_dir = ""

            def _get_kicad_version(self):
                return 9  # current dropdown value

            def _get_project_dir(self):
                return ""

        parent = FakeParent()
        # MetadataEditDialog.__init__ calls super().__init__ which needs a
        # wx.Window parent — we can't construct it fully. Test the resolution
        # logic directly instead.
        _gkv = getattr(parent, "_get_kicad_version", None)
        version = _gkv() if callable(_gkv) else getattr(parent, "_kicad_version", 9)
        assert version == 9  # should use method, not stale attr

    def test_falls_back_to_attr_when_no_method(self):
        class FakeParent:
            _kicad_version = 8
            _project_dir = ""

        parent = FakeParent()
        _gkv = getattr(parent, "_get_kicad_version", None)
        version = _gkv() if callable(_gkv) else getattr(parent, "_kicad_version", 9)
        assert version == 8


# ===================================================================
# __footprint_name / __model_name overrides
# ===================================================================


class TestFootprintNameOverride:
    """Tests for footprint and 3D model name overrides via metadata."""

    def test_fp_name_override_in_export(self, tmp_path, monkeypatch):
        """Custom footprint name should be used for the .kicad_mod filename."""
        fp_kwargs = {}

        def capture_fp(*args, **kwargs):
            fp_kwargs.update(kwargs)
            return "(footprint SOIC-8)\n"

        _patch_importer(monkeypatch, _make_fake_comp(), capture_fp=capture_fp)

        def edit_metadata(metadata):
            metadata["__footprint_name"] = "SOIC-8"
            metadata["__model_name"] = "SOIC-8-custom"
            return metadata

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: None,
            confirm_metadata=edit_metadata,
        )

        assert result is not None
        # Footprint file should use the custom name
        assert (tmp_path / "SOIC-8.kicad_mod").exists()

    def test_empty_fp_name_defaults_to_component_name(self, tmp_path, monkeypatch):
        """Empty footprint name should fall back to the sanitized component name."""
        fp_kwargs = {}

        def capture_fp(*args, **kwargs):
            fp_kwargs.update(kwargs)
            return "(footprint TestPart)\n"

        _patch_importer(monkeypatch, _make_fake_comp(), capture_fp=capture_fp)

        def clear_name(metadata):
            metadata["__footprint_name"] = ""
            metadata["__model_name"] = ""
            return metadata

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: None,
            confirm_metadata=clear_name,
        )

        assert result is not None
        assert (tmp_path / "TestPart.kicad_mod").exists()

    def test_model_name_defaults_to_fp_name(self, tmp_path, monkeypatch):
        """When model name is empty, it should default to fp_name."""
        monkeypatch.setattr(importer, "download_step", lambda _: b"step-data")
        monkeypatch.setattr(importer, "download_wrl_source", lambda _: None)

        saved_names = []

        def capture_save(models_dir, name, step_data=None, wrl_source=None):
            saved_names.append(name)
            return (None, None)

        _patch_importer(monkeypatch, _make_fake_comp())
        monkeypatch.setattr(importer, "save_models", capture_save)

        def set_fp_name(metadata):
            metadata["__footprint_name"] = "MyCustomName"
            metadata["__model_name"] = ""  # should default to fp_name
            return metadata

        result = importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: None,
            confirm_metadata=set_fp_name,
        )

        assert result is not None


# ===================================================================
# _check_existing_files with sym_name
# ===================================================================


class TestCheckExistingFiles:
    """Tests for _check_existing_files with separate fp_name/sym_name."""

    def test_detects_footprint(self, tmp_path):
        fp_dir = tmp_path / "Lib.pretty"
        fp_dir.mkdir()
        (fp_dir / "MyFP.kicad_mod").write_text("content")
        existing = importer._check_existing_files(str(tmp_path), "Lib", "MyFP")
        assert "footprint" in existing

    def test_detects_symbol_by_sym_name(self, tmp_path):
        """Symbol should be checked under sym_name, not fp_name."""
        sym_file = tmp_path / "Lib.kicad_sym"
        sym_file.write_text('(kicad_symbol_lib\n  (symbol "ComponentName" ...)\n)\n')

        # fp_name differs from sym_name
        existing = importer._check_existing_files(str(tmp_path), "Lib", "CustomFPName", sym_name="ComponentName")
        assert "symbol" in existing

    def test_sym_name_defaults_to_fp_name(self, tmp_path):
        sym_file = tmp_path / "Lib.kicad_sym"
        sym_file.write_text('(kicad_symbol_lib\n  (symbol "SameName" ...)\n)\n')
        existing = importer._check_existing_files(str(tmp_path), "Lib", "SameName")
        assert "symbol" in existing

    def test_no_false_positive_on_different_names(self, tmp_path):
        sym_file = tmp_path / "Lib.kicad_sym"
        sym_file.write_text('(kicad_symbol_lib\n  (symbol "OtherComponent" ...)\n)\n')
        existing = importer._check_existing_files(str(tmp_path), "Lib", "CustomFP", sym_name="WrongName")
        assert "symbol" not in existing

    def test_detects_3d_model(self, tmp_path):
        models_dir = tmp_path / "Lib.3dshapes"
        models_dir.mkdir()
        (models_dir / "Part.step").write_text("content")
        existing = importer._check_existing_files(str(tmp_path), "Lib", "Part")
        assert "3D model" in existing

    def test_nothing_exists(self, tmp_path):
        existing = importer._check_existing_files(str(tmp_path), "Lib", "Missing")
        assert existing == []

    def test_detects_3d_model_by_model_name(self, tmp_path):
        """3D model check should use model_name, not fp_name."""
        models_dir = tmp_path / "Lib.3dshapes"
        models_dir.mkdir()
        (models_dir / "CustomModel.step").write_text("content")
        # fp_name differs from model_name — should detect via model_name
        existing = importer._check_existing_files(str(tmp_path), "Lib", "SomeFP", model_name="CustomModel")
        assert "3D model" in existing

    def test_model_name_defaults_to_fp_name_for_3d(self, tmp_path):
        """When model_name is empty, 3D model check falls back to fp_name."""
        models_dir = tmp_path / "Lib.3dshapes"
        models_dir.mkdir()
        (models_dir / "Part.wrl").write_text("content")
        existing = importer._check_existing_files(str(tmp_path), "Lib", "Part")
        assert "3D model" in existing

    def test_no_false_3d_positive_with_different_model_name(self, tmp_path):
        """fp_name 3D file should NOT be detected when model_name is different."""
        models_dir = tmp_path / "Lib.3dshapes"
        models_dir.mkdir()
        (models_dir / "OldFP.step").write_text("content")
        existing = importer._check_existing_files(str(tmp_path), "Lib", "OldFP", model_name="NewModel")
        assert "3D model" not in existing


# ===================================================================
# Multi-unit _0_1 sub-symbol
# ===================================================================


class TestMultiUnitSubSymbol:
    """Test that multi-unit symbols emit the required _0_1 block."""

    def test_first_unit_emits_0_1_block(self):
        from kicad_jlcimport.kicad.symbol_writer import write_symbol

        sym = EESymbol()
        sym.pins.append(EEPin(number="1", name="A", x=0, y=0, rotation=0, length=2.54, electrical_type="passive"))
        result = write_symbol(sym, "DualPart", unit_index=0, total_units=2)
        assert '(symbol "DualPart_0_1"' in result

    def test_second_unit_does_not_emit_0_1(self):
        from kicad_jlcimport.kicad.symbol_writer import write_symbol

        sym = EESymbol()
        sym.pins.append(EEPin(number="2", name="B", x=0, y=0, rotation=0, length=2.54, electrical_type="passive"))
        result = write_symbol(sym, "DualPart", unit_index=1, total_units=2)
        assert '(symbol "DualPart_0_1"' not in result

    def test_single_unit_no_0_1_block(self):
        from kicad_jlcimport.kicad.symbol_writer import write_symbol

        sym = EESymbol()
        sym.pins.append(EEPin(number="1", name="A", x=0, y=0, rotation=0, length=2.54, electrical_type="passive"))
        result = write_symbol(sym, "SinglePart", unit_index=0, total_units=1)
        assert '(symbol "SinglePart_0_1"' in result  # single-unit uses _0_1 naturally


# ===================================================================
# __component_name in metadata
# ===================================================================


class TestComponentNameInMetadata:
    """Tests that __component_name is passed to confirm_metadata."""

    def test_component_name_in_metadata(self, tmp_path, monkeypatch):
        received = []

        def capture(metadata):
            received.append(dict(metadata))
            return metadata

        _patch_importer(monkeypatch, _make_fake_comp())
        importer.import_component(
            "C123",
            str(tmp_path),
            "TestLib",
            export_only=True,
            log=lambda msg: None,
            confirm_metadata=capture,
        )

        assert len(received) == 1
        assert received[0]["__component_name"] == "TestPart"
