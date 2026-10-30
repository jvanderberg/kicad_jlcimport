"""Production tab of the JLCImport dialog: availability check, BOM, CPL, Gerbers.

Shown only when the dialog is opened from the PCB editor (a board is loaded).
Three screens, one at a time: start (options and Check JLCPCB), summary
(Cancel / Review details / Generate) and part details (Cancel / Generate);
see :mod:`.production_summary`.

Board access (reading footprints, plotting) happens on the UI thread because
pcbnew is not thread-safe; only the network-bound check runs in a worker
thread, reporting back through ``wx.CallAfter`` with a request id so stale
results are ignored (same pattern as ``dialog.py``).
"""

import threading
import traceback

import wx

from . import production_pcbnew as adapter
from .production.export import board_basename, default_output_dir, make_caches, resolve_output_dir, write_bom_cpl
from .production.report import run_check
from .production_summary import DetailsScreen, FilesWrittenDialog, StartScreen, SummaryScreen

_TITLE = "JLCPCB Production Files"
START, SUMMARY, DETAILS = range(3)


class ProductionPanel(wx.Panel):
    """Notebook page hosted by ``JLCImportDialog``.

    ``on_ssl_error`` is the host dialog's TLS-warning handler (safe to call
    from worker threads).  The host calls :meth:`shutdown` when it closes.
    """

    def __init__(self, parent, board, on_ssl_error=None):
        super().__init__(parent)
        self.board = board
        self._on_ssl_error = on_ssl_error
        self._closing = False
        self._busy = False
        self._request_id = 0
        self._report = None
        self._records = None
        self._part_cache, self._footprint_cache = make_caches()
        board_file = adapter.board_path(board)
        self._default_dir = default_output_dir(board_file) if board_file else ""

        self.book = wx.Simplebook(self)
        self.start = StartScreen(self.book, self._default_dir, on_check=self._on_check)
        self.summary = SummaryScreen(
            self.book,
            on_cancel=lambda: self._show(START),
            on_review=lambda: self._show(DETAILS),
            on_generate=self._on_generate,
        )
        self.details = DetailsScreen(
            self.book,
            on_cancel=lambda: self._show(SUMMARY),
            on_generate=self._on_generate,
            on_ssl_error=self._handle_ssl_cert_error,
        )
        for page in (self.start, self.summary, self.details):
            self.book.AddPage(page, "")
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.book, 1, wx.EXPAND)
        self.SetSizer(sizer)

    def _show(self, page: int) -> None:
        self.book.SetSelection(page)

    def _current_board(self):
        """The board open now (File > Revert or Open replaces the BOARD object)."""
        self.board = adapter.current_board(self.board)
        return self.board

    def _follow_board_file(self, board_file):
        """Keep the default output folder in step with Save As, unless the user chose another."""
        new_default = default_output_dir(board_file) if board_file else ""
        if self.start.dir_text.GetValue().strip() in ("", self._default_dir):
            self.start.dir_text.SetValue(new_default)
        self._default_dir = new_default

    def shutdown(self):
        """Invalidate in-flight checks so their CallAfter callbacks no-op."""
        self._closing = True
        self._request_id += 1
        self.details.shutdown()

    def _handle_ssl_cert_error(self):
        if self._on_ssl_error is not None:
            self._on_ssl_error()

    # --------------------------------------------------------------- check

    def _on_check(self):
        if self._busy:
            return
        try:
            board = self._current_board()
            self._follow_board_file(adapter.board_path(board))
            records = adapter.extract_footprints(board)
        except Exception as e:
            wx.MessageBox(f"Could not read the board:\n{e}", _TITLE, wx.OK | wx.ICON_ERROR)
            return
        self._part_cache.clear()  # always fetch fresh stock
        # Retry footprints whose symbol (pin names, i.e. polarity) failed to download.
        self._footprint_cache.discard_where(lambda jlc_fp: bool(getattr(jlc_fp, "symbol_error", "")))
        self._busy = True
        self.start.set_running(True)
        self._request_id += 1
        threading.Thread(
            target=self._run_check,
            args=(records, self.start.qty_spin.GetValue(), self._request_id, self.start.stock_cb.GetValue()),
            daemon=True,
        ).start()

    def _run_check(self, records, boards, request_id, check_stock):
        """Background thread: network lookups and solving."""

        def progress(done, total, lcsc):
            if not self._closing:
                wx.CallAfter(self._on_progress, done, total, lcsc, request_id)

        try:
            report = run_check(
                records,
                boards,
                self._part_cache,
                self._footprint_cache,
                progress=progress,
                on_ssl_error=self._handle_ssl_cert_error,
                check_stock=check_stock,
            )
        except Exception as e:
            if not self._closing:
                wx.CallAfter(self._on_check_failed, f"{type(e).__name__}: {e}", request_id)
            return
        if not self._closing:
            wx.CallAfter(self._on_check_done, report, records, request_id)

    def _on_progress(self, done, total, lcsc, request_id):
        if self._closing or request_id != self._request_id:
            return
        self.start.set_progress(done, total, lcsc)

    def _on_check_failed(self, message, request_id):
        if self._closing or request_id != self._request_id:
            return
        self._busy = False
        self.start.set_running(False)
        wx.MessageBox(f"The check failed:\n{message}", _TITLE, wx.OK | wx.ICON_ERROR)

    def _on_check_done(self, report, records, request_id):
        if self._closing or request_id != self._request_id:
            return
        self._busy = False
        self.start.set_running(False)
        self._report, self._records = report, records
        out_dir = resolve_output_dir(self.start.dir_text.GetValue(), adapter.board_path(self.board)) or ""
        self.summary.set_report(report, out_dir)
        self.details.set_report(report)
        self._show(SUMMARY)

    # ------------------------------------------------------------ generate

    def _on_generate(self):
        report, records = self._report, self._records
        if report is None:
            return
        try:
            board = self._current_board()
            board_file = adapter.board_path(board)
            # BOM/CPL come from the board as it was checked, Gerbers from the
            # live board, so they must still agree.
            changed = adapter.extract_footprints(board) != records
        except Exception as e:
            wx.MessageBox(f"Could not read the board:\n{e}", _TITLE, wx.OK | wx.ICON_ERROR)
            return
        if changed:
            wx.MessageBox(
                "The board has changed since the check, so the placement file would not match the Gerbers.\n\n"
                "Check JLCPCB again.",
                _TITLE,
                wx.OK | wx.ICON_WARNING,
            )
            self._show(START)
            return
        out_dir = resolve_output_dir(self.start.dir_text.GetValue(), board_file)
        if not out_dir:
            wx.MessageBox(
                "Choose an output folder on the start screen (a relative folder needs a saved board).",
                _TITLE,
                wx.OK | wx.ICON_WARNING,
            )
            self._show(START)
            return
        basename = board_basename(board_file) if board_file else "board"

        wx.BeginBusyCursor()
        try:
            # Gerbers first: if plotting fails, no new BOM/CPL sit next to an old zip.
            zip_path, _files = adapter.generate_fabrication_zip(
                board, out_dir, basename, refill=self.start.refill_cb.GetValue()
            )
            paths = write_bom_cpl(report, out_dir, basename)
        except Exception as e:
            wx.MessageBox(
                f"Generating files failed:\n{e}\n\n{traceback.format_exc()}",
                _TITLE,
                wx.OK | wx.ICON_ERROR,
            )
            return
        finally:
            wx.EndBusyCursor()

        dlg = FilesWrittenDialog(self, out_dir, [zip_path, paths["bom"], paths["cpl"], paths["report"]])
        dlg.ShowModal()
        dlg.Destroy()
