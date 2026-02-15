"""Tests for --global-lib-dir session-only override across all entry points."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

try:
    import textual  # noqa: F401

    _has_textual = True
except ImportError:
    _has_textual = False

try:
    import wx

    _has_wx = True
except ImportError:
    wx = None
    _has_wx = False

needs_textual = pytest.mark.skipif(not _has_textual, reason="textual not installed")
needs_wx = pytest.mark.skipif(not _has_wx, reason="wxPython not installed")


@needs_textual
def test_tui_entry_validates_nonexistent_dir(tmp_path, monkeypatch, capsys):
    """TUI entry point exits with error for nonexistent --global-lib-dir."""
    bad_path = str(tmp_path / "nonexistent")
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--global-lib-dir", bad_path],
    )
    with patch("kicad_jlcimport.tui.app.JLCImportTUI"):
        with pytest.raises(SystemExit) as exc_info:
            from kicad_jlcimport.tui import main

            main()
        assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "--global-lib-dir does not exist" in err


@needs_textual
def test_tui_entry_passes_global_lib_dir(tmp_path, monkeypatch):
    """TUI entry point passes validated --global-lib-dir to JLCImportTUI."""
    real_dir = str(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--global-lib-dir", real_dir],
    )
    with patch("kicad_jlcimport.tui.app.JLCImportTUI") as mock_cls:
        mock_app = MagicMock()
        mock_cls.return_value = mock_app
        from kicad_jlcimport.tui import main

        main()
        mock_cls.assert_called_once()
        call_kwargs = mock_cls.call_args
        assert call_kwargs.kwargs["global_lib_dir"] == os.path.abspath(real_dir)


@needs_wx
def test_gui_entry_validates_nonexistent_dir(tmp_path, monkeypatch, capsys):
    """GUI entry point exits with error for nonexistent --global-lib-dir."""
    bad_path = str(tmp_path / "nonexistent")
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--global", "--global-lib-dir", bad_path],
    )
    with pytest.raises(SystemExit) as exc_info:
        from kicad_jlcimport.gui_entry import main

        main()
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "--global-lib-dir does not exist" in err


@needs_textual
def test_tui_app_constructor_stores_override(tmp_path, monkeypatch):
    """JLCImportTUI stores the override and uses it as _global_lib_dir."""
    monkeypatch.setattr(
        "kicad_jlcimport.tui.app.load_config",
        lambda: {"lib_name": "JLCImport"},
    )
    from kicad_jlcimport.tui.app import JLCImportTUI

    app = JLCImportTUI(global_lib_dir=str(tmp_path))
    assert app._global_lib_dir == str(tmp_path)
    assert app._global_lib_dir_override == str(tmp_path)


@needs_textual
def test_tui_app_constructor_without_override(tmp_path, monkeypatch):
    """JLCImportTUI without override uses get_global_lib_dir."""
    monkeypatch.setattr(
        "kicad_jlcimport.tui.app.load_config",
        lambda: {"lib_name": "JLCImport"},
    )
    monkeypatch.setattr(
        "kicad_jlcimport.tui.app.get_global_lib_dir",
        lambda _v: str(tmp_path / "default"),
    )
    from kicad_jlcimport.tui.app import JLCImportTUI

    app = JLCImportTUI()
    assert app._global_lib_dir == str(tmp_path / "default")
    assert app._global_lib_dir_override == ""


# Dialog tests use SimpleNamespace as a stand-in for self because
# JLCImportDialog inherits from wx.Dialog (C extension) which cannot
# be instantiated without a running wx.App.


@needs_wx
def test_dialog_version_change_preserves_override(monkeypatch):
    """Changing KiCad version does not overwrite the CLI override in dialog."""
    monkeypatch.setattr(
        "kicad_jlcimport.dialog.load_config",
        lambda: {"global_lib_dir": ""},
    )
    from kicad_jlcimport.dialog import JLCImportDialog

    dlg = SimpleNamespace(
        _global_lib_dir_override="/cli/override",
        _global_lib_dir="/cli/override",
        _global_path_label=MagicMock(),
        version_choice=MagicMock(),
        _version_labels=["8", "9"],
    )
    dlg.version_choice.GetSelection.return_value = 0

    class FakeEvent:
        def Skip(self):
            pass

    JLCImportDialog._on_version_change(dlg, FakeEvent())

    # Override should be preserved — _global_lib_dir not changed
    assert dlg._global_lib_dir == "/cli/override"


@needs_wx
def test_dialog_version_change_updates_without_override(monkeypatch):
    """Changing KiCad version updates _global_lib_dir when no override is set."""
    monkeypatch.setattr(
        "kicad_jlcimport.dialog.load_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "kicad_jlcimport.dialog.get_global_lib_dir",
        lambda _v: "/new/default/path",
    )
    from kicad_jlcimport.dialog import JLCImportDialog

    dlg = SimpleNamespace(
        _global_lib_dir_override="",
        _global_lib_dir="/old/path",
        _global_path_label=MagicMock(),
        _set_global_path=MagicMock(),
        version_choice=MagicMock(),
        _version_labels=["8", "9"],
        _get_kicad_version=lambda: 8,
    )
    dlg.version_choice.GetSelection.return_value = 0

    class FakeEvent:
        def Skip(self):
            pass

    JLCImportDialog._on_version_change(dlg, FakeEvent())

    assert dlg._global_lib_dir == "/new/default/path"
    dlg._set_global_path.assert_called_once_with("/new/default/path")


@needs_wx
def test_dialog_browse_clears_override(monkeypatch):
    """Browsing to a new directory clears the CLI override."""
    monkeypatch.setattr(
        "kicad_jlcimport.dialog.load_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "kicad_jlcimport.dialog.save_config",
        lambda _c: None,
    )
    from kicad_jlcimport.dialog import JLCImportDialog

    mock_dir_dlg = MagicMock()
    mock_dir_dlg.ShowModal.return_value = wx.ID_OK
    mock_dir_dlg.GetPath.return_value = "/new/path"
    monkeypatch.setattr(
        "kicad_jlcimport.dialog.wx.DirDialog",
        lambda *a, **k: mock_dir_dlg,
    )

    dlg = SimpleNamespace(
        _global_lib_dir_override="/cli/override",
        _global_lib_dir="/cli/override",
        _global_path_label=MagicMock(),
        _set_global_path=MagicMock(),
    )

    JLCImportDialog._on_global_browse(dlg, None)

    assert dlg._global_lib_dir == "/new/path"
    assert dlg._global_lib_dir_override == ""
    dlg._set_global_path.assert_called_once_with("/new/path")


@needs_wx
def test_dialog_reset_clears_override(monkeypatch):
    """Resetting the global dir clears the CLI override."""
    monkeypatch.setattr(
        "kicad_jlcimport.dialog.load_config",
        lambda: {},
    )
    monkeypatch.setattr(
        "kicad_jlcimport.dialog.save_config",
        lambda _c: None,
    )
    monkeypatch.setattr(
        "kicad_jlcimport.dialog.get_global_lib_dir",
        lambda _v: "/default/path",
    )
    from kicad_jlcimport.dialog import JLCImportDialog

    dlg = SimpleNamespace(
        _global_lib_dir_override="/cli/override",
        _global_lib_dir="/cli/override",
        _global_path_label=MagicMock(),
        _set_global_path=MagicMock(),
        version_choice=MagicMock(),
        _version_labels=["8", "9"],
        _get_kicad_version=lambda: 9,
    )
    dlg.version_choice.GetSelection.return_value = 1

    JLCImportDialog._on_global_reset(dlg, None)

    assert dlg._global_lib_dir == "/default/path"
    assert dlg._global_lib_dir_override == ""
    dlg._set_global_path.assert_called_once_with("/default/path")
