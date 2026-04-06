"""Tests for non-modal dialog behavior: singleton guard, close cleanup, and lifecycle."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_has_wx = bool(sys.modules.get("wx"))
if not _has_wx:
    try:
        import wx  # noqa: F401

        _has_wx = True
    except ImportError:
        pass

needs_wx = pytest.mark.skipif(not _has_wx, reason="wxPython not installed")


@needs_wx
class TestOnCloseCleanup:
    """_on_close stops timers, invalidates requests, fires callback, and destroys."""

    def _make_dialog(self, on_close=None, panel_enabled=True):
        """Build a SimpleNamespace that mimics JLCImportDialog for _on_close."""
        return SimpleNamespace(
            _closing=False,
            _main_panel=MagicMock(IsEnabled=MagicMock(return_value=panel_enabled)),
            _on_close_callback=on_close,
            _search_request_id=0,
            _image_request_id=0,
            _gallery_request_id=0,
            _gallery_svg_request_id=0,
            _symbol_request_id=0,
            _busy_overlay=MagicMock(),
            _search_overlay=MagicMock(),
            _stop_search_pulse=MagicMock(),
            _stop_skeleton=MagicMock(),
            _stop_gallery_skeleton=MagicMock(),
            _category_popup=MagicMock(),
            IsModal=MagicMock(return_value=False),
            Destroy=MagicMock(),
        )

    def test_on_close_fires_callback(self):
        from kicad_jlcimport.dialog import JLCImportDialog

        callback = MagicMock()
        dlg = self._make_dialog(on_close=callback)
        JLCImportDialog._on_close(dlg, None)
        callback.assert_called_once()

    def test_on_close_destroys_window(self):
        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = self._make_dialog()
        JLCImportDialog._on_close(dlg, None)
        dlg.Destroy.assert_called_once()

    def test_on_close_stops_all_timers(self):
        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = self._make_dialog()
        JLCImportDialog._on_close(dlg, None)
        dlg._stop_search_pulse.assert_called_once()
        dlg._stop_skeleton.assert_called_once()
        dlg._stop_gallery_skeleton.assert_called_once()
        dlg._busy_overlay.dismiss.assert_called_once()
        dlg._search_overlay.dismiss.assert_called_once()

    def test_on_close_invalidates_request_ids(self):
        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = self._make_dialog()
        JLCImportDialog._on_close(dlg, None)
        assert dlg._search_request_id == 1
        assert dlg._image_request_id == 1
        assert dlg._gallery_request_id == 1
        assert dlg._gallery_svg_request_id == 1
        assert dlg._symbol_request_id == 1

    def test_on_close_sets_closing_flag(self):
        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = self._make_dialog()
        JLCImportDialog._on_close(dlg, None)
        assert dlg._closing is True

    def test_double_close_is_noop(self):
        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = self._make_dialog()
        JLCImportDialog._on_close(dlg, None)
        dlg.Destroy.reset_mock()
        # Second call should be a no-op
        JLCImportDialog._on_close(dlg, None)
        dlg.Destroy.assert_not_called()

    def test_on_close_without_callback(self):
        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = self._make_dialog(on_close=None)
        JLCImportDialog._on_close(dlg, None)
        dlg.Destroy.assert_called_once()


@needs_wx
class TestCloseBlockedDuringImport:
    """Close is blocked when an import is in progress (panel disabled)."""

    def test_close_blocked_when_user_declines(self):
        import wx

        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = SimpleNamespace(
            _closing=False,
            _main_panel=MagicMock(IsEnabled=MagicMock(return_value=False)),
            _on_close_callback=None,
            Destroy=MagicMock(),
        )
        with patch.object(wx, "MessageBox", return_value=wx.NO):
            JLCImportDialog._on_close(dlg, None)
        dlg.Destroy.assert_not_called()
        assert dlg._closing is False

    def test_close_allowed_when_user_confirms(self):
        import wx

        from kicad_jlcimport.dialog import JLCImportDialog

        callback = MagicMock()
        dlg = SimpleNamespace(
            _closing=False,
            _main_panel=MagicMock(IsEnabled=MagicMock(return_value=False)),
            _on_close_callback=callback,
            _search_request_id=0,
            _image_request_id=0,
            _gallery_request_id=0,
            _gallery_svg_request_id=0,
            _symbol_request_id=0,
            _busy_overlay=MagicMock(),
            _search_overlay=MagicMock(),
            _stop_search_pulse=MagicMock(),
            _stop_skeleton=MagicMock(),
            _stop_gallery_skeleton=MagicMock(),
            _category_popup=MagicMock(),
            IsModal=MagicMock(return_value=False),
            Destroy=MagicMock(),
        )
        with patch.object(wx, "MessageBox", return_value=wx.YES):
            JLCImportDialog._on_close(dlg, None)
        dlg.Destroy.assert_called_once()
        callback.assert_called_once()


@needs_wx
class TestClosingGuardsCallAfter:
    """Background thread callbacks are suppressed when _closing is True."""

    def test_import_worker_skips_callafter_when_closing(self):
        """_import_worker should not CallAfter when dialog is closing."""
        import wx

        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = SimpleNamespace(
            _closing=True,
            _do_import=MagicMock(return_value={"title": "X", "name": "Y"}),
            _handle_ssl_cert_error=MagicMock(),
        )
        with patch.object(wx, "CallAfter") as mock_call_after:
            JLCImportDialog._import_worker(dlg, "C123", "/d", "lib", False, {}, 9)
        mock_call_after.assert_not_called()

    def test_do_import_log_skips_when_closing(self):
        """The log() closure in _do_import should not CallAfter when closing."""
        import wx

        from kicad_jlcimport.dialog import JLCImportDialog

        log_fn = None

        def capture_import_component(*args, **kwargs):
            nonlocal log_fn
            log_fn = kwargs["log"]
            return {"title": "X", "name": "Y"}

        dlg = SimpleNamespace(
            _closing=True,
            _main_panel=MagicMock(),
            _busy_overlay=MagicMock(),
            _confirm_metadata=MagicMock(),
            _confirm_overwrite=MagicMock(),
        )
        with patch("kicad_jlcimport.dialog.import_component", side_effect=capture_import_component):
            JLCImportDialog._do_import(dlg, "C123", "/d", "lib", False, {}, 9)
        assert log_fn is not None
        with patch.object(wx, "CallAfter") as mock_call_after:
            log_fn("test message")
        mock_call_after.assert_not_called()

    def test_ssl_handler_skips_callafter_when_closing(self):
        """_handle_ssl_cert_error should not CallAfter when closing."""
        import wx

        from kicad_jlcimport.dialog import JLCImportDialog

        dlg = SimpleNamespace(
            _closing=True,
            _ssl_warning_shown=False,
        )
        with (
            patch.object(wx, "CallAfter") as mock_call_after,
            patch("kicad_jlcimport.dialog._api_module.allow_unverified_ssl"),
        ):
            JLCImportDialog._handle_ssl_cert_error(dlg)
        mock_call_after.assert_not_called()
