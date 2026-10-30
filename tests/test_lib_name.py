"""Tests for library-name validation across the core and all three front-ends."""

import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kicad_jlcimport import importer
from kicad_jlcimport.kicad import library
from kicad_jlcimport.kicad.library import LibraryNameError, validate_lib_name

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
    "$(KIPRJMOD)",
    "tab\there",
    "new\nline",
    "del\x7f",
    ".",
    "..",
    "Lib.",
    "CON",
    "con",
    "LPT1",
    "CON.backup",
    "NUL.txt",
    "con .x",
    "COM\u00b9",
    "LPT\u00b3",
]


class TestValidateLibName:
    @pytest.mark.parametrize(
        "name",
        ["JLCImport", "My Lib", "Lib-2.0", "lib_v3", "Bibliothèque", "Lib (Power)", "Lib$", "CONSOLE", "Console.lib"],
    )
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

    @pytest.mark.parametrize("value", [123, 1.5, ["Lib"], {"name": "Lib"}])
    def test_rejects_non_text(self, value):
        with pytest.raises(LibraryNameError, match="must be text"):
            validate_lib_name(value)

    def test_none_is_empty(self):
        with pytest.raises(LibraryNameError, match="empty"):
            validate_lib_name(None)

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
        # Read the value back the way KiCad's s-expression reader does.
        raw = re.search(r'\(uri "((?:[^"\\]|\\.)*)"\)', text).group(1)
        assert re.sub(r"\\(.)", r"\1", raw) == '/Users/a"b/JLCImport.kicad_sym'
        assert text.count("(lib ") == 1


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
        out = capsys.readouterr().out
        assert "Library name" in out
        assert "--lib-name" in out
        assert "jlcimport.json" in out
        assert os.listdir(str(tmp_path)) == []


@pytest.mark.skipif(not _has_wx, reason="wx not available")
class TestDialogLibNameInput:
    def _dialog(self, typed, logged):
        from kicad_jlcimport.dialog import JLCImportDialog

        name_input = MagicMock()
        name_input.GetValue.return_value = typed
        dlg = SimpleNamespace(lib_name_input=name_input, _lib_name="Old", _log=logged.append)
        dlg._commit_lib_name = lambda: JLCImportDialog._commit_lib_name(dlg)
        return dlg

    def test_invalid_name_is_kept_for_editing_and_not_saved(self, monkeypatch):
        from kicad_jlcimport.dialog import JLCImportDialog

        saved = MagicMock()
        monkeypatch.setattr("kicad_jlcimport.dialog.save_config", saved)
        logged = []
        dlg = self._dialog("../escaped", logged)
        event = MagicMock()

        JLCImportDialog._on_lib_name_change(dlg, event)

        assert dlg._lib_name == "Old"
        dlg.lib_name_input.SetValue.assert_not_called()
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

    def test_empty_name_reverts(self, monkeypatch):
        from kicad_jlcimport.dialog import JLCImportDialog

        monkeypatch.setattr("kicad_jlcimport.dialog.save_config", MagicMock(side_effect=AssertionError))
        dlg = self._dialog("   ", [])
        assert JLCImportDialog._commit_lib_name(dlg) is True
        dlg.lib_name_input.SetValue.assert_called_once_with("Old")

    def test_invalid_name_blocks_import(self, monkeypatch):
        from kicad_jlcimport.dialog import JLCImportDialog

        started = MagicMock()
        monkeypatch.setattr("kicad_jlcimport.dialog.threading.Thread", started)
        logged = []
        dlg = self._dialog("Parts/Passives", logged)
        dlg._selected_result = {"lcsc": "C123"}
        dlg.status_text = MagicMock()

        JLCImportDialog._on_import(dlg, MagicMock())

        started.assert_not_called()
        dlg.status_text.Clear.assert_not_called()
        assert dlg._lib_name == "Old"
        assert "Library name" in logged[0]

    def test_import_worker_reports_name_error_without_traceback(self, monkeypatch):
        from kicad_jlcimport.dialog import JLCImportDialog

        monkeypatch.setattr("kicad_jlcimport.dialog.wx.CallAfter", lambda fn, *a: fn(*a))
        errors = []

        def bad_import(*_a):
            raise LibraryNameError("Library name 'x:y' contains ':'")

        dlg = SimpleNamespace(_closing=False, _do_import=bad_import, _on_import_error=errors.append)
        JLCImportDialog._import_worker(dlg, "C123", "/tmp", "x:y", False, None, 9)

        assert errors == ["Error: Library name 'x:y' contains ':'"]


@pytest.mark.skipif(not _has_textual, reason="textual not available")
@pytest.mark.skipif(sys.version_info < (3, 10), reason="textual_image requires Python 3.10+")
class TestTuiLibNameInput:
    def _app(self, typed, logged):
        name_input = SimpleNamespace(value=typed)
        return SimpleNamespace(_lib_name="Old", _log=logged.append, query_one=lambda *a: name_input)

    def test_invalid_name_is_kept_for_editing_and_not_saved(self, monkeypatch):
        from kicad_jlcimport.tui import app as tui_app

        saved = MagicMock()
        monkeypatch.setattr(tui_app, "save_config", saved)
        logged = []
        app = self._app("Lib:Sub", logged)

        assert tui_app.JLCImportTUI._persist_lib_name(app) is False

        assert app._lib_name == "Old"
        assert app.query_one().value == "Lib:Sub"
        saved.assert_not_called()
        assert "Library name" in logged[0]

    def test_valid_name_is_saved(self, monkeypatch):
        from kicad_jlcimport.tui import app as tui_app

        saved = MagicMock()
        monkeypatch.setattr(tui_app, "save_config", saved)
        monkeypatch.setattr(tui_app, "load_config", lambda: {})
        app = self._app("New Lib", [])

        assert tui_app.JLCImportTUI._persist_lib_name(app) is True

        assert app._lib_name == "New Lib"
        saved.assert_called_once_with({"lib_name": "New Lib"})

    def test_invalid_name_blocks_import(self, monkeypatch):
        from kicad_jlcimport.tui import app as tui_app

        monkeypatch.setattr(tui_app, "save_config", MagicMock(side_effect=AssertionError))
        logged = []
        app = self._app("Parts/Passives", logged)
        app._persist_lib_name = lambda: tui_app.JLCImportTUI._persist_lib_name(app)
        app._selected_result = {"lcsc": "C123"}
        app._run_import = MagicMock()

        tui_app.JLCImportTUI._do_import_action(app)

        app._run_import.assert_not_called()
        assert app._lib_name == "Old"

    def test_import_worker_reports_name_error_without_traceback(self):
        from kicad_jlcimport.tui import app as tui_app

        logged = []

        def bad_import(*_a):
            raise LibraryNameError("Library name 'x:y' contains ':'")

        button = SimpleNamespace(disabled=True)
        app = SimpleNamespace(
            _do_import=bad_import,
            _log=logged.append,
            query_one=lambda *a: button,
            app=SimpleNamespace(call_from_thread=lambda fn, *a: fn(*a)),
        )
        tui_app.JLCImportTUI._run_import.__wrapped__(app, "C123", "/tmp", False, 9)

        assert logged == ["[red]Error: Library name 'x:y' contains ':'[/red]"]
        assert button.disabled is False
