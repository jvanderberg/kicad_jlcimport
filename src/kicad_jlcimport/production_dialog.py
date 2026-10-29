"""wxPython dialog for JLCPCB production export (availability check, BOM, CPL, Gerbers).

Board access (reading footprints, plotting) happens on the UI thread because
pcbnew is not thread-safe; only the network-bound check runs in a worker
thread, reporting back through ``wx.CallAfter`` with a request id so stale
results are ignored (same pattern as ``dialog.py``).
"""

import os
import threading
import traceback

import wx

from . import production_pcbnew as adapter
from .easyeda import api as _api_module
from .production.export import board_basename, default_output_dir, make_caches, write_bom_cpl
from .production.placement import POSITION_OFFSET_FIELD, ROTATION_OFFSET_FIELD
from .production.report import ERROR, WARNING, row_cells, run_check

COLUMNS = [
    ("Ref", "ref", 60),
    ("Value", "value", 110),
    ("LCSC", "lcsc", 85),
    ("Stock", "stock", 180),
    ("Type", "type", 70),
    ("Rotation", "rotation", 65),
    ("Confidence", "confidence", 80),
    ("Notes", "notes", 400),
]

_ROW_COLOURS = {
    ERROR: wx.Colour(255, 215, 215),
    WARNING: wx.Colour(255, 240, 200),
}


class ProductionExportDialog(wx.Dialog):
    def __init__(self, parent, board, on_close=None):
        super().__init__(
            parent,
            title="JLCPCB Production Export",
            size=(1050, 660),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.board = board
        self._on_close_callback = on_close
        self._closing = False
        self._busy = False
        self._request_id = 0
        self._ssl_warning_shown = False
        self._part_cache, self._footprint_cache = make_caches()
        board_file = adapter.board_path(board)
        self._basename = board_basename(board_file) if board_file else "board"
        self._init_ui(default_output_dir(board_file) if board_file else "")
        self.Centre()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ------------------------------------------------------------------ UI

    def _init_ui(self, out_dir):
        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(cols=3, vgap=6, hgap=8)
        grid.AddGrowableCol(1)
        grid.Add(wx.StaticText(panel, label="Boards to assemble:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.qty_spin = wx.SpinCtrl(panel, min=1, max=1000000, initial=5)
        grid.Add(self.qty_spin, 0)
        grid.Add((0, 0))
        grid.Add(wx.StaticText(panel, label="Output folder:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.dir_text = wx.TextCtrl(panel, value=out_dir)
        grid.Add(self.dir_text, 1, wx.EXPAND)
        browse_btn = wx.Button(panel, label="Browse...")
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)
        grid.Add(browse_btn, 0)
        vbox.Add(grid, 0, wx.EXPAND | wx.ALL, 8)

        self.refill_cb = wx.CheckBox(panel, label="Refill zones before plotting Gerbers")
        self.refill_cb.SetValue(True)
        vbox.Add(self.refill_cb, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.table = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, (title, _key, width) in enumerate(COLUMNS):
            self.table.InsertColumn(i, title, width=width)
        vbox.Add(self.table, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.summary = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 120))
        self.summary.SetValue(
            "Check looks up every LCSC part on the board (stock, basic/extended) and solves each part's "
            "CPL rotation and centre against JLCPCB's own footprint.\n"
            f'Manual corrections: add footprint fields "{ROTATION_OFFSET_FIELD}" (degrees) and/or '
            f'"{POSITION_OFFSET_FIELD}" ("dx, dy" mm, Y up) to use KiCad\'s placement plus that offset.'
        )
        vbox.Add(self.summary, 0, wx.EXPAND | wx.ALL, 8)

        hbox = wx.BoxSizer(wx.HORIZONTAL)
        self.status = wx.StaticText(panel, label="")
        hbox.Add(self.status, 1, wx.ALIGN_CENTER_VERTICAL)
        self.check_btn = wx.Button(panel, label="Check")
        self.check_btn.Bind(wx.EVT_BUTTON, self._on_check)
        hbox.Add(self.check_btn, 0, wx.LEFT, 6)
        self.generate_btn = wx.Button(panel, label="Generate")
        self.generate_btn.Bind(wx.EVT_BUTTON, self._on_generate)
        hbox.Add(self.generate_btn, 0, wx.LEFT, 6)
        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        hbox.Add(close_btn, 0, wx.LEFT, 6)
        vbox.Add(hbox, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(vbox)

    def _set_busy(self, busy):
        self._busy = busy
        self.check_btn.Enable(not busy)
        self.generate_btn.Enable(not busy)
        self.qty_spin.Enable(not busy)

    def _on_browse(self, event):
        dlg = wx.DirDialog(self, "Choose output folder", self.dir_text.GetValue())
        if dlg.ShowModal() == wx.ID_OK:
            self.dir_text.SetValue(dlg.GetPath())
        dlg.Destroy()

    def _on_close(self, event):
        if self._closing:
            return
        self._closing = True
        self._request_id += 1  # stale CallAfter callbacks become no-ops
        if self._on_close_callback:
            self._on_close_callback()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Destroy()

    # --------------------------------------------------------------- check

    def _on_check(self, event):
        self._part_cache.clear()  # always fetch fresh stock on an explicit check
        self._start_check(generate=False)

    def _on_generate(self, event):
        # Re-check (cached lookups make this fast) so files match the current board.
        self._start_check(generate=True)

    def _start_check(self, generate):
        if self._busy:
            return
        try:
            records = adapter.extract_footprints(self.board)
        except Exception as e:
            wx.MessageBox(f"Could not read the board:\n{e}", "JLCPCB Production Export", wx.OK | wx.ICON_ERROR)
            return
        self._set_busy(True)
        self._request_id += 1
        request_id = self._request_id
        boards = self.qty_spin.GetValue()
        self.status.SetLabel("Checking parts...")
        threading.Thread(
            target=self._run_check,
            args=(records, boards, request_id, generate),
            daemon=True,
        ).start()

    def _run_check(self, records, boards, request_id, generate):
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
            )
        except Exception as e:
            if not self._closing:
                wx.CallAfter(self._on_check_failed, f"{type(e).__name__}: {e}", request_id)
            return
        if not self._closing:
            wx.CallAfter(self._on_check_done, report, request_id, generate)

    def _handle_ssl_cert_error(self):
        """Called from worker threads: warn once and continue without verification."""
        if not self._ssl_warning_shown:
            self._ssl_warning_shown = True
            if not self._closing:
                wx.CallAfter(
                    wx.MessageBox,
                    "TLS certificate verification failed.\n\n"
                    "A proxy or firewall may be intercepting HTTPS traffic. "
                    "The session will continue without certificate verification.",
                    "TLS Certificate Warning",
                    wx.OK | wx.ICON_WARNING,
                )
        _api_module.allow_unverified_ssl()

    def _on_progress(self, done, total, lcsc, request_id):
        if self._closing or request_id != self._request_id:
            return
        self.status.SetLabel(f"Checking parts {done}/{total} ({lcsc})...")

    def _on_check_failed(self, message, request_id):
        if self._closing or request_id != self._request_id:
            return
        self._set_busy(False)
        self.status.SetLabel("Check failed")
        wx.MessageBox(message, "JLCPCB Production Export", wx.OK | wx.ICON_ERROR)

    def _on_check_done(self, report, request_id, generate):
        if self._closing or request_id != self._request_id:
            return
        self._set_busy(False)
        self._show_report(report)
        self.status.SetLabel(f"{report.count(ERROR)} errors, {report.count(WARNING)} warnings")
        if generate:
            self._generate(report)

    def _show_report(self, report):
        self.table.DeleteAllItems()
        for row in report.rows:
            cells = row_cells(row)
            idx = self.table.InsertItem(self.table.GetItemCount(), cells["ref"])
            for col, (_title, key, _width) in enumerate(COLUMNS[1:], start=1):
                self.table.SetItem(idx, col, cells[key])
            colour = _ROW_COLOURS.get(row.severity)
            if colour is not None:
                self.table.SetItemBackgroundColour(idx, colour)
                self.table.SetItemTextColour(idx, wx.BLACK)
        lines = report.summary_lines()
        lines.extend(f"[{i.severity.upper()}] {i.message}" for i in report.board_issues)
        self.summary.SetValue("\n".join(lines))

    # ------------------------------------------------------------ generate

    def _generate(self, report):
        out_dir = self.dir_text.GetValue().strip()
        if not out_dir:
            wx.MessageBox("Choose an output folder first.", "JLCPCB Production Export", wx.OK | wx.ICON_WARNING)
            return
        errors, warnings = report.count(ERROR), report.count(WARNING)
        if errors or warnings:
            answer = wx.MessageBox(
                f"The check found {errors} errors and {warnings} warnings "
                "(see the highlighted rows).\n\nGenerate production files anyway?",
                "JLCPCB Production Export",
                wx.YES_NO | wx.ICON_WARNING,
            )
            if answer != wx.YES:
                return

        self.status.SetLabel("Generating files...")
        wx.BeginBusyCursor()
        try:
            paths = write_bom_cpl(report, out_dir, self._basename)
            zip_path, _files = adapter.generate_fabrication_zip(
                self.board, out_dir, self._basename, refill=self.refill_cb.GetValue()
            )
        except Exception as e:
            self.status.SetLabel("Generation failed")
            wx.MessageBox(
                f"Generating files failed:\n{e}\n\n{traceback.format_exc()}",
                "JLCPCB Production Export",
                wx.OK | wx.ICON_ERROR,
            )
            return
        finally:
            wx.EndBusyCursor()

        self.status.SetLabel(f"Files written to {out_dir}")
        written = [zip_path, paths["bom"], paths["cpl"], paths["report"]]
        self.summary.AppendText("\n\nWritten:\n" + "\n".join(os.path.basename(p) for p in written))
        wx.MessageBox(
            "Production files written to:\n" + out_dir + "\n\n" + "\n".join(os.path.basename(p) for p in written),
            "JLCPCB Production Export",
            wx.OK | wx.ICON_INFORMATION,
        )
