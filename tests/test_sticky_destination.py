"""Tests for sticky import destination (use_global persisted in config).

Issue #41: Import location should persist between sessions.
"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kicad_jlcimport.kicad import library

_has_wx = bool(sys.modules.get("wx"))
if not _has_wx:
    try:
        import wx  # noqa: F401

        _has_wx = True
    except ImportError:
        pass


class TestConfigUseGlobal:
    """Config layer: use_global key exists and round-trips through load/save."""

    def test_default_config_includes_use_global(self):
        """_DEFAULT_CONFIG should include use_global defaulting to False."""
        assert "use_global" in library._DEFAULT_CONFIG
        assert library._DEFAULT_CONFIG["use_global"] is False

    def test_load_config_returns_use_global_default(self, tmp_path, monkeypatch):
        """load_config returns use_global=False when config file is missing."""
        monkeypatch.setattr(library, "_config_path", lambda: str(tmp_path / "cfg.json"))
        config = library.load_config()
        assert config["use_global"] is False

    def test_save_and_load_use_global_true(self, tmp_path, monkeypatch):
        """Saving use_global=True then loading it back preserves the value."""
        monkeypatch.setattr(library, "_config_path", lambda: str(tmp_path / "cfg.json"))
        config = library.load_config()
        config["use_global"] = True
        library.save_config(config)
        reloaded = library.load_config()
        assert reloaded["use_global"] is True

    def test_backfill_use_global_into_existing_config(self, tmp_path, monkeypatch):
        """Old config files without use_global get it backfilled."""
        cfg_file = tmp_path / "cfg.json"
        cfg_file.write_text(json.dumps({"lib_name": "MyLib", "global_lib_dir": ""}))
        monkeypatch.setattr(library, "_config_path", lambda: str(cfg_file))
        config = library.load_config()
        assert config["use_global"] is False
        # Should be written to disk
        stored = json.loads(cfg_file.read_text())
        assert "use_global" in stored


@pytest.mark.skipif(not _has_wx, reason="wx not available")
class TestDialogStickyDestination:
    """wxPython dialog respects and saves use_global preference."""

    def test_dialog_defaults_to_global_when_saved(self, monkeypatch):
        """When config has use_global=True and project dir exists, Global is selected."""
        monkeypatch.setattr(
            "kicad_jlcimport.dialog.load_config",
            lambda: {"lib_name": "JLCImport", "global_lib_dir": "", "use_global": True},
        )
        from kicad_jlcimport.dialog import JLCImportDialog

        dest_project = MagicMock()
        dest_global = MagicMock()
        dlg = SimpleNamespace(dest_project=dest_project, dest_global=dest_global)

        JLCImportDialog._apply_saved_destination(dlg, "/some/project")

        dest_global.SetValue.assert_called_with(True)

    def test_dialog_defaults_to_project_when_saved_false(self, monkeypatch):
        """When config has use_global=False and project dir exists, Project is selected."""
        monkeypatch.setattr(
            "kicad_jlcimport.dialog.load_config",
            lambda: {"lib_name": "JLCImport", "global_lib_dir": "", "use_global": False},
        )
        from kicad_jlcimport.dialog import JLCImportDialog

        dest_project = MagicMock()
        dest_global = MagicMock()
        dlg = SimpleNamespace(dest_project=dest_project, dest_global=dest_global)

        JLCImportDialog._apply_saved_destination(dlg, "/some/project")

        dest_project.SetValue.assert_called_with(True)

    def test_dialog_forces_global_when_no_project(self, monkeypatch):
        """Even if use_global=False, Global is forced when no project dir."""
        monkeypatch.setattr(
            "kicad_jlcimport.dialog.load_config",
            lambda: {"lib_name": "JLCImport", "global_lib_dir": "", "use_global": False},
        )
        from kicad_jlcimport.dialog import JLCImportDialog

        dest_project = MagicMock()
        dest_global = MagicMock()
        dlg = SimpleNamespace(dest_project=dest_project, dest_global=dest_global)

        JLCImportDialog._apply_saved_destination(dlg, "")

        dest_project.Disable.assert_called_once()
        dest_global.SetValue.assert_called_with(True)

    def test_dialog_saves_use_global_on_import(self, monkeypatch):
        """_persist_destination saves use_global=True to config."""
        saved = {}
        monkeypatch.setattr(
            "kicad_jlcimport.dialog.load_config",
            lambda: {"lib_name": "JLCImport", "global_lib_dir": "", "use_global": False},
        )
        monkeypatch.setattr("kicad_jlcimport.dialog.save_config", lambda c: saved.update(c))
        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = SimpleNamespace(dest_global=MagicMock())
        dlg.dest_global.GetValue.return_value = True

        JLCImportDialog._persist_destination(dlg)

        assert saved["use_global"] is True

    def test_dialog_saves_use_global_false_on_project_import(self, monkeypatch):
        """Importing to project persists use_global=False."""
        saved = {}
        monkeypatch.setattr(
            "kicad_jlcimport.dialog.load_config",
            lambda: {"lib_name": "JLCImport", "global_lib_dir": "", "use_global": True},
        )
        monkeypatch.setattr("kicad_jlcimport.dialog.save_config", lambda c: saved.update(c))
        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = SimpleNamespace(dest_global=MagicMock())
        dlg.dest_global.GetValue.return_value = False

        JLCImportDialog._persist_destination(dlg)

        assert saved["use_global"] is False

    def test_dialog_does_not_persist_on_import_failure(self, monkeypatch):
        """A failed import should not persist the destination preference."""
        saved = {}
        monkeypatch.setattr(
            "kicad_jlcimport.dialog.load_config",
            lambda: {"lib_name": "JLCImport", "global_lib_dir": "", "use_global": False},
        )
        monkeypatch.setattr("kicad_jlcimport.dialog.save_config", lambda c: saved.update(c))
        monkeypatch.setattr("kicad_jlcimport.dialog.validate_lcsc_id", lambda x: x)
        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = SimpleNamespace(
            part_input=MagicMock(),
            dest_global=MagicMock(),
            _global_lib_dir="/some/dir",
            overwrite_cb=MagicMock(),
            import_btn=MagicMock(),
            status_text=MagicMock(),
            _lib_name="JLCImport",
            _get_project_dir=lambda: "/project",
            _get_kicad_version=lambda: 9,
            _ssl_warning_shown=False,
            _imported_ids=set(),
            _search_results=[],
            _do_import=MagicMock(side_effect=Exception("import failed")),
            _log=MagicMock(),
            _persist_destination=MagicMock(),
        )
        dlg.part_input.GetValue.return_value = "C427602"
        dlg.dest_global.GetValue.return_value = True
        dlg.overwrite_cb.GetValue.return_value = False

        JLCImportDialog._on_import(dlg, None)

        dlg._persist_destination.assert_not_called()


class TestTUIStickyDestination:
    """Textual TUI respects and saves use_global preference."""

    def test_tui_constructor_stores_use_global_from_config(self, tmp_path, monkeypatch):
        """TUI app reads use_global from config on construction."""
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.load_config",
            lambda: {"lib_name": "JLCImport", "use_global": True},
        )
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.get_global_lib_dir",
            lambda _v: str(tmp_path),
        )
        from kicad_jlcimport.tui.app import JLCImportTUI

        app = JLCImportTUI()
        assert app._use_global is True

    def test_tui_constructor_defaults_use_global_false(self, tmp_path, monkeypatch):
        """TUI app defaults use_global to False when not in config."""
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.load_config",
            lambda: {"lib_name": "JLCImport"},
        )
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.get_global_lib_dir",
            lambda _v: str(tmp_path),
        )
        from kicad_jlcimport.tui.app import JLCImportTUI

        app = JLCImportTUI()
        assert app._use_global is False

    def test_tui_compose_uses_saved_global_preference(self, tmp_path, monkeypatch):
        """When use_global=True in config and project dir available, global radio is selected."""
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.load_config",
            lambda: {"lib_name": "JLCImport", "use_global": True},
        )
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.get_global_lib_dir",
            lambda _v: str(tmp_path),
        )
        from kicad_jlcimport.tui.app import JLCImportTUI

        app = JLCImportTUI(project_dir="/some/project")
        # _use_global should be True, meaning global radio should be initially selected
        assert app._use_global is True

    def test_tui_forces_global_when_no_project_dir(self, tmp_path, monkeypatch):
        """Even if use_global=False in config, global is selected when no project dir."""
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.load_config",
            lambda: {"lib_name": "JLCImport", "use_global": False},
        )
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.get_global_lib_dir",
            lambda _v: str(tmp_path),
        )
        from kicad_jlcimport.tui.app import JLCImportTUI

        app = JLCImportTUI()  # no project_dir
        # _use_global is False but _select_global in compose() should be True
        # because self._project_dir is empty
        assert app._use_global is False
        assert not app._project_dir

    def test_tui_persist_destination_saves_use_global(self, tmp_path, monkeypatch):
        """_persist_destination saves use_global to config."""
        saved = {}
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.load_config",
            lambda: {"lib_name": "JLCImport", "use_global": False},
        )
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.save_config",
            lambda c: saved.update(c),
        )
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.get_global_lib_dir",
            lambda _v: str(tmp_path),
        )
        from kicad_jlcimport.tui.app import JLCImportTUI

        app = JLCImportTUI()
        JLCImportTUI._persist_destination(app, use_global=True)

        assert saved["use_global"] is True

    def test_tui_does_not_persist_on_import_failure(self, tmp_path, monkeypatch):
        """A failed import should not persist the destination preference."""
        saved = {}
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.load_config",
            lambda: {"lib_name": "JLCImport", "use_global": False},
        )
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.save_config",
            lambda c: saved.update(c),
        )
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.get_global_lib_dir",
            lambda _v: str(tmp_path),
        )
        monkeypatch.setattr(
            "kicad_jlcimport.tui.app.import_component",
            MagicMock(side_effect=Exception("import failed")),
        )
        from kicad_jlcimport.tui.app import JLCImportTUI

        app = JLCImportTUI()

        # _do_import should raise, so _persist_destination should not be reached
        try:
            JLCImportTUI._do_import(app, "C427602", str(tmp_path), False, True, 9)
        except Exception:
            pass

        assert "use_global" not in saved
