"""Tests for library.py - file management and sanitization."""

import os
import tempfile

from kicad_jlcimport.kicad.library import (
    _remove_symbol,
    _update_lib_table,
    add_symbol_to_lib,
    ensure_lib_structure,
    find_best_matching_footprint,
    sanitize_name,
    save_footprint,
)


class TestSanitizeName:
    def test_simple_name(self):
        assert sanitize_name("Resistor") == "Resistor"

    def test_spaces_to_underscores(self):
        assert sanitize_name("100nF 0402") == "100nF_0402"

    def test_special_chars_replaced(self):
        assert sanitize_name("Part/Model<1>") == "Part_Model_1"

    def test_dots_replaced(self):
        assert sanitize_name("0.1uF") == "0_1uF"

    def test_collapse_underscores(self):
        assert sanitize_name("a  b   c") == "a_b_c"

    def test_strip_leading_trailing(self):
        assert sanitize_name("__name__") == "name"

    def test_empty_returns_unnamed(self):
        assert sanitize_name("") == "unnamed"

    def test_only_special_chars(self):
        assert sanitize_name("///") == "unnamed"

    def test_windows_reserved_con(self):
        result = sanitize_name("CON")
        assert result == "_CON"

    def test_windows_reserved_nul(self):
        result = sanitize_name("NUL")
        assert result == "_NUL"

    def test_windows_reserved_com1(self):
        result = sanitize_name("COM1")
        assert result == "_COM1"

    def test_windows_reserved_lpt9(self):
        result = sanitize_name("LPT9")
        assert result == "_LPT9"

    def test_windows_reserved_case_insensitive(self):
        result = sanitize_name("con")
        assert result == "_con"

    def test_path_traversal_blocked(self):
        result = sanitize_name("../../etc/passwd")
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result

    def test_unicode_replaced(self):
        result = sanitize_name("Resistance\u00b5F")
        assert all(c.isalnum() or c in ("_", "-") for c in result)

    def test_hyphen_preserved(self):
        assert sanitize_name("ESP32-S3") == "ESP32-S3"

    def test_normal_component_name(self):
        assert sanitize_name("ESP32-S3-WROOM-1-N16R8") == "ESP32-S3-WROOM-1-N16R8"

    def test_backslash_replaced(self):
        result = sanitize_name("path\\to\\file")
        assert "\\" not in result


class TestEnsureLibStructure:
    def test_creates_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = ensure_lib_structure(tmpdir, "TestLib")
            assert os.path.isdir(os.path.join(tmpdir, "TestLib.pretty"))
            assert os.path.isdir(os.path.join(tmpdir, "TestLib.3dshapes"))
            assert paths["sym_path"] == os.path.join(tmpdir, "TestLib.kicad_sym")
            assert paths["fp_dir"] == os.path.join(tmpdir, "TestLib.pretty")
            assert paths["models_dir"] == os.path.join(tmpdir, "TestLib.3dshapes")

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ensure_lib_structure(tmpdir, "TestLib")
            # Should not raise on second call
            paths = ensure_lib_structure(tmpdir, "TestLib")
            assert os.path.isdir(paths["fp_dir"])


class TestAddSymbolToLib:
    def test_creates_new_library(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sym_path = os.path.join(tmpdir, "test.kicad_sym")
            content = '  (symbol "R_100")\n'
            result = add_symbol_to_lib(sym_path, "R_100", content)
            assert result is True
            assert os.path.exists(sym_path)
            with open(sym_path) as f:
                text = f.read()
            assert '(symbol "R_100")' in text
            assert "(kicad_symbol_lib" in text

    def test_appends_to_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sym_path = os.path.join(tmpdir, "test.kicad_sym")
            add_symbol_to_lib(sym_path, "R_100", '  (symbol "R_100")\n')
            result = add_symbol_to_lib(sym_path, "C_100", '  (symbol "C_100")\n')
            assert result is True
            with open(sym_path) as f:
                text = f.read()
            assert '(symbol "R_100")' in text
            assert '(symbol "C_100")' in text

    def test_skip_existing_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sym_path = os.path.join(tmpdir, "test.kicad_sym")
            add_symbol_to_lib(sym_path, "R_100", '  (symbol "R_100")\n')
            result = add_symbol_to_lib(sym_path, "R_100", '  (symbol "R_100" new)\n')
            assert result is False

    def test_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sym_path = os.path.join(tmpdir, "test.kicad_sym")
            add_symbol_to_lib(sym_path, "R_100", '  (symbol "R_100"\n    (old)\n  )\n')
            result = add_symbol_to_lib(sym_path, "R_100", '  (symbol "R_100"\n    (new)\n  )\n', overwrite=True)
            assert result is True
            with open(sym_path) as f:
                text = f.read()
            assert "(new)" in text
            assert "(old)" not in text


class TestSaveFootprint:
    def test_saves_new_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            content = "(footprint test)\n"
            result = save_footprint(tmpdir, "test", content)
            assert result is True
            fp_path = os.path.join(tmpdir, "test.kicad_mod")
            assert os.path.exists(fp_path)
            with open(fp_path) as f:
                assert f.read() == content

    def test_skip_existing_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_footprint(tmpdir, "test", "original")
            result = save_footprint(tmpdir, "test", "new content")
            assert result is False
            with open(os.path.join(tmpdir, "test.kicad_mod")) as f:
                assert f.read() == "original"

    def test_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_footprint(tmpdir, "test", "original")
            result = save_footprint(tmpdir, "test", "new content", overwrite=True)
            assert result is True
            with open(os.path.join(tmpdir, "test.kicad_mod")) as f:
                assert f.read() == "new content"


class TestUpdateLibTable:
    def test_creates_new_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = os.path.join(tmpdir, "sym-lib-table")
            result = _update_lib_table(table_path, "sym_lib_table", "JLCImport", "KiCad", "/path/to/lib.kicad_sym")
            assert result is True
            with open(table_path) as f:
                text = f.read()
            assert "(sym_lib_table" in text
            assert '(name "JLCImport")' in text
            assert '(uri "/path/to/lib.kicad_sym")' in text

    def test_appends_to_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = os.path.join(tmpdir, "sym-lib-table")
            with open(table_path, "w") as f:
                f.write("(sym_lib_table\n  (version 7)\n)\n")
            result = _update_lib_table(table_path, "sym_lib_table", "JLCImport", "KiCad", "/path/to/lib")
            assert result is False
            with open(table_path) as f:
                text = f.read()
            assert '(name "JLCImport")' in text

    def test_skip_if_already_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = os.path.join(tmpdir, "sym-lib-table")
            _update_lib_table(table_path, "sym_lib_table", "JLCImport", "KiCad", "/path")
            # Read content after first add
            with open(table_path) as f:
                text1 = f.read()
            # Try adding again
            _update_lib_table(table_path, "sym_lib_table", "JLCImport", "KiCad", "/path")
            with open(table_path) as f:
                text2 = f.read()
            assert text1 == text2  # No change


class TestRemoveSymbol:
    def test_removes_simple_symbol(self):
        content = '(kicad_symbol_lib\n  (symbol "R_100"\n    (pin_names)\n  )\n)\n'
        result = _remove_symbol(content, "R_100")
        assert '(symbol "R_100"' not in result
        assert "(kicad_symbol_lib" in result

    def test_removes_one_of_many(self):
        content = (
            '(kicad_symbol_lib\n  (symbol "R_100"\n    (pin_names)\n  )\n  (symbol "C_100"\n    (pin_names)\n  )\n)\n'
        )
        result = _remove_symbol(content, "R_100")
        assert '(symbol "R_100"' not in result
        assert '(symbol "C_100"' in result

    def test_nonexistent_symbol_unchanged(self):
        content = '(kicad_symbol_lib\n  (symbol "R_100")\n)\n'
        result = _remove_symbol(content, "X_999")
        assert result == content


class TestFindBestMatchingFootprint:
    def test_finds_match_from_project_fp_lib_table(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        empty_global = tmp_path / "empty_global"
        empty_global.mkdir()
        local_pretty = project_dir / "Local.pretty"
        local_pretty.mkdir()
        (local_pretty / "DIP-8_W7.62mm.kicad_mod").write_text("(footprint)")

        (project_dir / "fp-lib-table").write_text(
            "(fp_lib_table\n"
            "  (version 7)\n"
            '  (lib (name "Local")(type "KiCad")(uri "${KIPRJMOD}/Local.pretty")(options "")(descr ""))\n'
            ")\n"
        )
        monkeypatch.setattr("kicad_jlcimport.kicad.library.get_global_config_dir", lambda _v=9: str(empty_global))

        match = find_best_matching_footprint("DIP-8", str(project_dir), kicad_version=9)
        assert match == "Local:DIP-8_W7.62mm"

    def test_finds_match_from_global_fp_lib_table(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        fp_root = tmp_path / "fp_root"
        so_pretty = fp_root / "Package_SO.pretty"
        so_pretty.mkdir(parents=True)
        (so_pretty / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod").write_text("(footprint)")

        (config_dir / "fp-lib-table").write_text(
            "(fp_lib_table\n"
            "  (version 7)\n"
            '  (lib (name "Package_SO")(type "KiCad")(uri "${KICAD9_FOOTPRINT_DIR}/Package_SO.pretty")(options "")(descr ""))\n'
            ")\n"
        )

        monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", str(fp_root))
        monkeypatch.setattr("kicad_jlcimport.kicad.library.get_global_config_dir", lambda _v=9: str(config_dir))

        match = find_best_matching_footprint("SOIC-8_3.9x4.9mm_P1.27mm", "", kicad_version=9)
        assert match == "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"

    def test_returns_none_when_no_sufficient_match(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "fp-lib-table").write_text("(fp_lib_table\n  (version 7)\n)\n")
        monkeypatch.setattr("kicad_jlcimport.kicad.library.get_global_config_dir", lambda _v=9: str(config_dir))

        match = find_best_matching_footprint("BGA-100", str(tmp_path), kicad_version=9)
        assert match is None

    def test_uses_platform_fallback_when_kicad_env_var_missing(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "fp-lib-table").write_text(
            "(fp_lib_table\n"
            "  (version 7)\n"
            '  (lib (name "Diode_SMD")(type "KiCad")(uri "${KICAD9_FOOTPRINT_DIR}/Diode_SMD.pretty")(options "")(descr ""))\n'
            ")\n"
        )

        fake_data_root = tmp_path / "kicad_data"
        fp_root = fake_data_root / "footprints"
        diode_lib = fp_root / "Diode_SMD.pretty"
        diode_lib.mkdir(parents=True)
        (diode_lib / "D_SOD-323.kicad_mod").write_text("(footprint)")

        monkeypatch.delenv("KICAD9_FOOTPRINT_DIR", raising=False)
        monkeypatch.setattr("kicad_jlcimport.kicad.library.get_global_config_dir", lambda _v=9: str(config_dir))
        monkeypatch.setattr("kicad_jlcimport.kicad.library._find_kicad_data_dir", lambda _major: str(fake_data_root))

        match = find_best_matching_footprint("SOD-323", "", kicad_version=9)
        assert match == "Diode_SMD:D_SOD-323"

    def test_prefers_qfn_80_over_qfn_40_when_package_is_qfn_80(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        empty_global = tmp_path / "empty_global"
        empty_global.mkdir()
        qfn_lib = project_dir / "Package_DFN_QFN.pretty"
        qfn_lib.mkdir()
        (qfn_lib / "QFN-40-1EP_5x5mm_P0.4mm_EP3.6x3.6mm.kicad_mod").write_text("(footprint)")
        (qfn_lib / "QFN-80-1EP_10x10mm_P0.4mm_EP3.4x3.4mm.kicad_mod").write_text("(footprint)")

        (project_dir / "fp-lib-table").write_text(
            "(fp_lib_table\n"
            "  (version 7)\n"
            '  (lib (name "Package_DFN_QFN")(type "KiCad")(uri "${KIPRJMOD}/Package_DFN_QFN.pretty")(options "")(descr ""))\n'
            ")\n"
        )
        monkeypatch.setattr("kicad_jlcimport.kicad.library.get_global_config_dir", lambda _v=9: str(empty_global))

        package = "QFN-80_L10.0-W10.0-P0.40-TL-EP3.4"
        match = find_best_matching_footprint(package, str(project_dir), kicad_version=9)
        assert match == "Package_DFN_QFN:QFN-80-1EP_10x10mm_P0.4mm_EP3.4x3.4mm"


class TestAddSymbolToLibVersions:
    def test_new_library_v9_has_generator_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sym_path = os.path.join(tmpdir, "test.kicad_sym")
            add_symbol_to_lib(sym_path, "R_100", '  (symbol "R_100")\n', kicad_version=9)
            with open(sym_path) as f:
                text = f.read()
            assert "(version 20241209)" in text
            assert '(generator_version "1.0")' in text

    def test_new_library_v8_no_generator_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sym_path = os.path.join(tmpdir, "test.kicad_sym")
            add_symbol_to_lib(sym_path, "R_100", '  (symbol "R_100")\n', kicad_version=8)
            with open(sym_path) as f:
                text = f.read()
            assert "(version 20231120)" in text
            assert "generator_version" not in text
            assert '(generator "JLCImport")' in text
