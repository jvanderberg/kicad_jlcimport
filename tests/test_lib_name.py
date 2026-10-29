"""Tests for library-name validation across the core and all three front-ends."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kicad_jlcimport import importer
from kicad_jlcimport.kicad import library
from kicad_jlcimport.kicad.library import LibraryNameError, _scan_symbol_lib, validate_lib_name

try:
    import wx  # noqa: F401

    _has_wx = True
except ImportError:
    _has_wx = False

try:
    import textual  # noqa: F401

    _has_textual = True
except ImportError:
    _has_textual = False


BAD_NAMES = [
    "",
    "   ",
    "../escaped",
    "a/b",
    "a\\b",
    "Lib:Sub",
    'My "Lib',
    "a<b",
    "a>b",
    "a|b",
    "a?b",
    "a*b",
    "${HOME}",
    "tab\there",
    "new\nline",
    "del\x7f",
    ".",
    "..",
    "Lib.",
    "CON",
    "con",
    "LPT1",
]


class TestValidateLibName:
    @pytest.mark.parametrize("name", ["JLCImport", "My Lib", "Lib-2.0", "lib_v3", "Bibliothèque", "Lib (Power)"])
    def test_accepts_safe_names(self, name):
        assert validate_lib_name(name) == name

    def test_strips_surrounding_whitespace(self):
        assert validate_lib_name("  MyLib  ") == "MyLib"

    @pytest.mark.parametrize("name", BAD_NAMES)
    def test_rejects_unsafe_names(self, name):
        with pytest.raises(LibraryNameError):
            validate_lib_name(name)

    def test_error_names_the_offending_character(self):
        with pytest.raises(LibraryNameError, match="':'"):
            validate_lib_name("Lib:Sub")

    def test_is_a_value_error(self):
        assert issubclass(LibraryNameError, ValueError)


class TestCoreBackstops:
    def test_ensure_lib_structure_refuses_path_escape(self, tmp_path):
        base = tmp_path / "project"
        base.mkdir()
        with pytest.raises(LibraryNameError):
            library.ensure_lib_structure(str(base), "../escaped")
        assert os.listdir(str(tmp_path)) == ["project"]
        assert os.listdir(str(base)) == []

    def test_lib_table_refuses_bad_name_and_leaves_table(self, tmp_path):
        table = tmp_path / "sym-lib-table"
        original = (
            '(sym_lib_table\n  (version 7)\n  (lib (name "Other")(type "KiCad")(uri "/x")(options "")(descr ""))\n)\n'
        )
        table.write_text(original)
        with pytest.raises(LibraryNameError):
            library._update_lib_table(str(table), "sym_lib_table", 'My "Lib', "KiCad", "/y")
        assert table.read_text() == original

    def test_lib_table_escapes_uri(self, tmp_path):
        table = tmp_path / "sym-lib-table"
        library._update_lib_table(str(table), "sym_lib_table", "JLCImport", "KiCad", '/Users/a"b/JLCImport.kicad_sym')
        text = table.read_text()
        assert _scan_symbol_lib(text) is not None
        assert '(uri "/Users/a\\"b/JLCImport.kicad_sym")' in text


class TestImportComponentValidatesFirst:
    def test_bad_name_raises_before_fetch(self, tmp_path, monkeypatch):
        fetched = []
        monkeypatch.setattr(importer, "fetch_full_component", lambda lcsc: fetched.append(lcsc))
        with pytest.raises(LibraryNameError):
            importer.import_component("C123", str(tmp_path), "../escaped", log=lambda m: None)
        assert fetched == []
        assert os.listdir(str(tmp_path)) == []


class TestCli:
    def test_bad_lib_name_exits_with_error(self, tmp_path, monkeypatch, capsys):
        import kicad_jlcimport.cli as cli

        monkeypatch.setattr(importer, "fetch_full_component", MagicMock(side_effect=AssertionError("fetched")))
        args = SimpleNamespace(
            part="C123",
            show=None,
            output=None,
            project=str(tmp_path),
            global_dest=False,
            overwrite=False,
            lib_name='My "Lib',
            kicad_version=9,
        )
        with pytest.raises(SystemExit) as exc:
            cli.cmd_import(args)
        assert exc.value.code == 1
        assert "Library name" in capsys.readouterr().out
        assert os.listdir(str(tmp_path)) == []


@pytest.mark.skipif(not _has_wx, reason="wx not available")
class TestDialogLibNameInput:
    def _dialog(self, typed, logged):
        name_input = MagicMock()
        name_input.GetValue.return_value = typed
        return SimpleNamespace(lib_name_input=name_input, _lib_name="Old", _log=logged.append)

    def test_invalid_name_is_reverted_and_not_saved(self, monkeypatch):
        from kicad_jlcimport.dialog import JLCImportDialog

        saved = MagicMock()
        monkeypatch.setattr("kicad_jlcimport.dialog.save_config", saved)
        logged = []
        dlg = self._dialog("../escaped", logged)
        event = MagicMock()

        JLCImportDialog._on_lib_name_change(dlg, event)

        assert dlg._lib_name == "Old"
        dlg.lib_name_input.SetValue.assert_called_once_with("Old")
        saved.assert_not_called()
        assert logged and logged[0].startswith("Error: Library name")
        event.Skip.assert_called_once()

    def test_valid_name_is_saved(self, monkeypatch):
        from kicad_jlcimport.dialog import JLCImportDialog

        saved = MagicMock()
        monkeypatch.setattr("kicad_jlcimport.dialog.save_config", saved)
        monkeypatch.setattr("kicad_jlcimport.dialog.load_config", lambda: {})
        dlg = self._dialog("New Lib", [])

        JLCImportDialog._on_lib_name_change(dlg, MagicMock())

        assert dlg._lib_name == "New Lib"
        saved.assert_called_once_with({"lib_name": "New Lib"})


@pytest.mark.skipif(not _has_textual, reason="textual not available")
@pytest.mark.skipif(sys.version_info < (3, 10), reason="textual_image requires Python 3.10+")
class TestTuiLibNameInput:
    def _app(self, typed, logged):
        name_input = SimpleNamespace(value=typed)
        return SimpleNamespace(_lib_name="Old", _log=logged.append, query_one=lambda *a: name_input)

    def test_invalid_name_is_reverted_and_not_saved(self, monkeypatch):
        from kicad_jlcimport.tui import app as tui_app

        saved = MagicMock()
        monkeypatch.setattr(tui_app, "save_config", saved)
        logged = []
        app = self._app("Lib:Sub", logged)

        tui_app.JLCImportTUI._persist_lib_name(app)

        assert app._lib_name == "Old"
        assert app.query_one().value == "Old"
        saved.assert_not_called()
        assert "Library name" in logged[0]

    def test_valid_name_is_saved(self, monkeypatch):
        from kicad_jlcimport.tui import app as tui_app

        saved = MagicMock()
        monkeypatch.setattr(tui_app, "save_config", saved)
        monkeypatch.setattr(tui_app, "load_config", lambda: {})
        app = self._app("New Lib", [])

        tui_app.JLCImportTUI._persist_lib_name(app)

        assert app._lib_name == "New Lib"
        saved.assert_called_once_with({"lib_name": "New Lib"})
