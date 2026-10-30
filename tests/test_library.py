"""Tests for library.py - file management and sanitization."""

import os
import tempfile

from kicad_jlcimport.kicad.library import (
    _update_lib_table,
    add_symbol_to_lib,
    ensure_lib_structure,
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


class TestOverwriteReplacesSymbol:
    def _overwrite(self, tmp_path, content, name, new):
        sym_path = str(tmp_path / "test.kicad_sym")
        with open(sym_path, "w", encoding="utf-8") as f:
            f.write(content)
        assert add_symbol_to_lib(sym_path, name, new, overwrite=True) is True
        with open(sym_path, encoding="utf-8") as f:
            return f.read()

    def test_replaces_simple_symbol(self, tmp_path):
        content = '(kicad_symbol_lib\n  (symbol "R_100"\n    (pin_names)\n  )\n)\n'
        result = self._overwrite(tmp_path, content, "R_100", '  (symbol "R_100"\n    (new)\n  )\n')
        assert "(pin_names)" not in result
        assert "(new)" in result
        assert result.startswith("(kicad_symbol_lib")

    def test_replaces_one_of_many(self, tmp_path):
        content = (
            '(kicad_symbol_lib\n  (symbol "R_100"\n    (pin_names)\n  )\n  (symbol "C_100"\n    (pin_names)\n  )\n)\n'
        )
        result = self._overwrite(tmp_path, content, "R_100", '  (symbol "R_100"\n    (new)\n  )\n')
        assert result.count("(pin_names)") == 1
        assert '(symbol "C_100"' in result
        assert result.index('(symbol "R_100"') < result.index('(symbol "C_100"')

    def test_absent_symbol_is_appended(self, tmp_path):
        content = '(kicad_symbol_lib\n  (symbol "R_100")\n)\n'
        result = self._overwrite(tmp_path, content, "X_999", '  (symbol "X_999")\n')
        assert '(symbol "R_100")' in result
        assert '(symbol "X_999")' in result


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
