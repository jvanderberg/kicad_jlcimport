"""Extended tests for library.py to improve coverage."""

import json
import os
import sys
from unittest.mock import MagicMock

from kicad_jlcimport.kicad import library


class TestConfigPath:
    """Tests for _config_path function."""

    def test_config_path_returns_string(self):
        result = library._config_path()
        assert isinstance(result, str)
        assert "jlcimport.json" in result


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_config_returns_defaults_when_no_file(self, tmp_path, monkeypatch):
        # Make _config_path return a non-existent file
        monkeypatch.setattr(library, "_config_path", lambda: str(tmp_path / "nonexistent.json"))
        config = library.load_config()
        assert config["lib_name"] == "JLCImport"

    def test_load_config_reads_existing_file(self, tmp_path, monkeypatch):
        config_file = tmp_path / "jlcimport.json"
        config_file.write_text(json.dumps({"lib_name": "CustomLib", "custom_key": "value"}))
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        config = library.load_config()
        assert config["lib_name"] == "CustomLib"
        assert config["custom_key"] == "value"

    def test_load_config_handles_invalid_json(self, tmp_path, monkeypatch):
        config_file = tmp_path / "jlcimport.json"
        config_file.write_text("not valid json {{{")
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        config = library.load_config()
        assert config["lib_name"] == "JLCImport"  # Falls back to default

    def test_load_config_handles_non_dict_json(self, tmp_path, monkeypatch):
        config_file = tmp_path / "jlcimport.json"
        config_file.write_text('"just a string"')
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        config = library.load_config()
        assert config["lib_name"] == "JLCImport"  # Falls back to default

    def test_default_config_includes_global_lib_dir(self):
        assert "global_lib_dir" in library._DEFAULT_CONFIG
        assert library._DEFAULT_CONFIG["global_lib_dir"] == ""

    def test_load_config_auto_creates_file_when_missing(self, tmp_path, monkeypatch):
        config_file = tmp_path / "jlcimport.json"
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        library.load_config()
        assert config_file.exists()
        stored = json.loads(config_file.read_text())
        assert "lib_name" in stored
        assert "global_lib_dir" in stored

    def test_load_config_backfills_missing_keys(self, tmp_path, monkeypatch):
        config_file = tmp_path / "jlcimport.json"
        config_file.write_text(json.dumps({"lib_name": "Custom"}))
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        config = library.load_config()
        assert config["global_lib_dir"] == ""
        # File on disk should also be updated
        stored = json.loads(config_file.read_text())
        assert "global_lib_dir" in stored
        assert stored["lib_name"] == "Custom"

    def test_load_config_preserves_unknown_keys(self, tmp_path, monkeypatch):
        config_file = tmp_path / "jlcimport.json"
        config_file.write_text(json.dumps({"lib_name": "X", "extra_key": 42}))
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        config = library.load_config()
        assert config["extra_key"] == 42
        # Verify it survives on disk too
        stored = json.loads(config_file.read_text())
        assert stored["extra_key"] == 42


class TestSaveConfig:
    """Tests for save_config function."""

    def test_save_config_creates_file(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config" / "jlcimport.json"
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        library.save_config({"lib_name": "TestLib"})

        assert config_file.exists()
        data = json.loads(config_file.read_text())
        assert data["lib_name"] == "TestLib"

    def test_save_config_overwrites_existing(self, tmp_path, monkeypatch):
        config_file = tmp_path / "jlcimport.json"
        config_file.write_text(json.dumps({"lib_name": "OldLib"}))
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        library.save_config({"lib_name": "NewLib"})

        data = json.loads(config_file.read_text())
        assert data["lib_name"] == "NewLib"


class TestDetectKicadVersion:
    """Tests for _detect_kicad_version function."""

    def test_detect_version_from_pcbnew(self, monkeypatch):
        mock_pcbnew = MagicMock()
        mock_pcbnew.Version.return_value = "(9.0.1)"
        monkeypatch.setitem(sys.modules, "pcbnew", mock_pcbnew)

        result = library._detect_kicad_version()
        assert result == "9.0"

    def test_detect_version_from_directory(self, tmp_path, monkeypatch):
        # Ensure pcbnew is not available
        monkeypatch.delitem(sys.modules, "pcbnew", raising=False)

        # Create version directories
        (tmp_path / "8.0").mkdir()
        (tmp_path / "9.0").mkdir()

        monkeypatch.setattr(library, "_kicad_data_base", lambda: str(tmp_path))

        result = library._detect_kicad_version()
        assert result == "9.0"  # Should pick newest

    def test_detect_version_default(self, tmp_path, monkeypatch):
        # Ensure pcbnew is not available
        monkeypatch.delitem(sys.modules, "pcbnew", raising=False)

        # Empty directory
        monkeypatch.setattr(library, "_kicad_data_base", lambda: str(tmp_path))

        result = library._detect_kicad_version()
        assert result == "9.0"  # Default

    def test_detect_version_ignores_non_numeric_dirs(self, tmp_path, monkeypatch):
        # Ensure pcbnew is not available
        monkeypatch.delitem(sys.modules, "pcbnew", raising=False)

        (tmp_path / "7.0").mkdir()
        (tmp_path / "plugins").mkdir()
        (tmp_path / "symbols").mkdir()

        monkeypatch.setattr(library, "_kicad_data_base", lambda: str(tmp_path))

        result = library._detect_kicad_version()
        assert result == "7.0"


class TestKicadDataBase:
    """Tests for _kicad_data_base function."""

    def test_darwin_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        result = library._kicad_data_base()
        assert "Documents/KiCad" in result

    def test_win32_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(os.environ, "get", lambda k, d="": "/users/test" if k == "APPDATA" else d)
        result = library._kicad_data_base()
        assert "kicad" in result.lower()

    def test_linux_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        result = library._kicad_data_base()
        assert ".local/share/kicad" in result


class TestKicadConfigBase:
    """Tests for _kicad_config_base function."""

    def test_darwin_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        result = library._kicad_config_base()
        assert "Library/Preferences/kicad" in result

    def test_win32_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        result = library._kicad_config_base()
        assert "kicad" in result.lower()

    def test_linux_path(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        result = library._kicad_config_base()
        assert ".config/kicad" in result


class TestGetGlobalLibDir:
    """Tests for get_global_lib_dir function."""

    def test_returns_path_with_3rdparty_v9(self, tmp_path, monkeypatch):
        monkeypatch.setattr(library, "_kicad_data_base", lambda: "/home/user/kicad")
        monkeypatch.setattr(library, "_config_path", lambda: str(tmp_path / "cfg.json"))

        result = library.get_global_lib_dir(kicad_version=9)
        assert "3rdparty" in result
        assert "9.0" in result

    def test_returns_path_with_3rdparty_v8(self, tmp_path, monkeypatch):
        monkeypatch.setattr(library, "_kicad_data_base", lambda: "/home/user/kicad")
        monkeypatch.setattr(library, "_config_path", lambda: str(tmp_path / "cfg.json"))

        result = library.get_global_lib_dir(kicad_version=8)
        assert "3rdparty" in result
        assert "8.0" in result

    def test_get_global_lib_dir_uses_custom_when_set(self, tmp_path, monkeypatch):
        custom_dir = str(tmp_path / "custom_libs")
        os.makedirs(custom_dir)
        config_file = tmp_path / "cfg.json"
        config_file.write_text(json.dumps({"lib_name": "JLCImport", "global_lib_dir": custom_dir}))
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        result = library.get_global_lib_dir(kicad_version=9)
        assert result == custom_dir

    def test_get_global_lib_dir_falls_back_when_empty(self, tmp_path, monkeypatch):
        config_file = tmp_path / "cfg.json"
        config_file.write_text(json.dumps({"lib_name": "JLCImport", "global_lib_dir": ""}))
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))
        monkeypatch.setattr(library, "_kicad_data_base", lambda: "/home/user/kicad")

        result = library.get_global_lib_dir(kicad_version=9)
        assert "3rdparty" in result
        assert "9.0" in result

    def test_get_global_lib_dir_custom_ignores_version(self, tmp_path, monkeypatch):
        custom_dir = str(tmp_path / "custom_libs")
        os.makedirs(custom_dir)
        config_file = tmp_path / "cfg.json"
        config_file.write_text(json.dumps({"lib_name": "JLCImport", "global_lib_dir": custom_dir}))
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        result_v8 = library.get_global_lib_dir(kicad_version=8)
        result_v9 = library.get_global_lib_dir(kicad_version=9)
        assert result_v8 == result_v9 == custom_dir

    def test_get_global_lib_dir_raises_on_invalid_custom_dir(self, tmp_path, monkeypatch):
        config_file = tmp_path / "cfg.json"
        config_file.write_text(json.dumps({"lib_name": "JLCImport", "global_lib_dir": "/nonexistent/path"}))
        monkeypatch.setattr(library, "_config_path", lambda: str(config_file))

        import pytest

        with pytest.raises(ValueError, match="does not exist"):
            library.get_global_lib_dir(kicad_version=9)


class TestGetGlobalConfigDir:
    """Tests for get_global_config_dir function."""

    def test_returns_path_with_version_v9(self, monkeypatch):
        monkeypatch.setattr(library, "_kicad_config_base", lambda: "/home/user/.config/kicad")

        result = library.get_global_config_dir(kicad_version=9)
        assert "9.0" in result

    def test_returns_path_with_version_v8(self, monkeypatch):
        monkeypatch.setattr(library, "_kicad_config_base", lambda: "/home/user/.config/kicad")

        result = library.get_global_config_dir(kicad_version=8)
        assert "8.0" in result


class TestUpdateProjectLibTables:
    """Tests for update_project_lib_tables function."""

    def test_creates_both_tables(self, tmp_path):
        result = library.update_project_lib_tables(str(tmp_path), "TestLib")
        assert result is True  # At least one table was created
        assert (tmp_path / "sym-lib-table").exists()
        assert (tmp_path / "fp-lib-table").exists()

    def test_returns_false_if_already_exists(self, tmp_path):
        # First call creates the tables
        library.update_project_lib_tables(str(tmp_path), "TestLib")
        # Second call should not create new tables
        result = library.update_project_lib_tables(str(tmp_path), "TestLib")
        assert result is False


class TestUpdateGlobalLibTables:
    """Tests for update_global_lib_tables function."""

    def test_creates_tables_in_config_dir(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "config"
        monkeypatch.setattr(library, "get_global_config_dir", lambda _v=9: str(config_dir))

        library.update_global_lib_tables(str(tmp_path / "libs"), "TestLib")

        assert (config_dir / "sym-lib-table").exists()
        assert (config_dir / "fp-lib-table").exists()

    def test_creates_config_dir_if_needed(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "new" / "config"
        monkeypatch.setattr(library, "get_global_config_dir", lambda _v=9: str(config_dir))

        library.update_global_lib_tables(str(tmp_path / "libs"), "TestLib")

        assert config_dir.exists()


class TestUpdateLibTableExtended:
    """Extended tests for _update_lib_table function."""

    def test_appends_to_malformed_table(self, tmp_path):
        """Test handling of table file without closing paren."""
        table_path = tmp_path / "sym-lib-table"
        table_path.write_text("(sym_lib_table\n  (version 7)\n")  # No closing paren

        result = library._update_lib_table(str(table_path), "sym_lib_table", "TestLib", "KiCad", "/path/to/lib")
        assert result is False  # Appended to existing


class TestRemoveSymbolExtended:
    """Extended tests for _remove_symbol function."""

    def test_removes_nested_symbol(self):
        """Test removal of symbol with nested parens."""
        content = """(kicad_symbol_lib
  (symbol "R_100"
    (pin_names)
    (property "Reference" "R" (at 0 0 0))
    (symbol "R_100_0_1"
      (rectangle (start -1 2) (end 1 -2))
    )
  )
)
"""
        result = library._remove_symbol(content, "R_100")
        assert '(symbol "R_100"' not in result
        assert "(kicad_symbol_lib" in result


class TestUpdateGlobalLibTablesBackslash:
    """Tests for backslash path handling in update_global_lib_tables."""

    def test_global_lib_table_uri_uses_forward_slashes(self, tmp_path, monkeypatch):
        """Ensure URIs in sym-lib-table and fp-lib-table never contain backslashes."""
        config_dir = tmp_path / "config"
        monkeypatch.setattr(library, "get_global_config_dir", lambda _v=9: str(config_dir))

        # Pass a Windows-style backslash path
        lib_dir = r"C:\Users\admin\AppData\kicad\9.0\3rdparty"
        library.update_global_lib_tables(lib_dir, "JLCImport")

        for table_name in ("sym-lib-table", "fp-lib-table"):
            content = (config_dir / table_name).read_text()
            # Extract URI value from the (uri "...") field
            import re

            uri_match = re.search(r'\(uri "([^"]+)"\)', content)
            assert uri_match, f"No URI found in {table_name}"
            uri = uri_match.group(1)
            assert "\\" not in uri, f"Backslash found in {table_name} URI: {uri}"
            assert "/" in uri, f"No forward slash in {table_name} URI: {uri}"


class TestAddSymbolToLibExtended:
    """Extended tests for add_symbol_to_lib function."""

    def test_malformed_library_no_closing_paren(self, tmp_path):
        """Test handling of library without any closing paren."""
        sym_path = tmp_path / "test.kicad_sym"
        sym_path.write_text("(kicad_symbol_lib\n")  # No closing paren at all

        result = library.add_symbol_to_lib(str(sym_path), "Test", '  (symbol "Test")\n')
        # rfind(")") returns -1 when no ), so function returns False
        assert result is False
