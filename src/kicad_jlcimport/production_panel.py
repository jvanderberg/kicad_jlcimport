"""Production tab of the JLCImport dialog: availability check, BOM, CPL, Gerbers.

Shown only when the dialog is opened from the PCB editor (a board is loaded).
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
from .production.export import board_basename, default_output_dir, make_caches, write_bom_cpl
from .production.placement import POSITION_OFFSET_FIELD, ROTATION_OFFSET_FIELD
from .production.report import ERROR, WARNING, row_cells, run_check

# The last column (Notes) is stretched to fill whatever width is left.
COLUMNS = [
    ("Ref", "ref", 60),
    ("Value", "value", 100),
    ("LCSC", "lcsc", 80),
    ("Stock", "stock", 135),
    ("Type", "type", 65),
    ("Rotation", "rotation", 60),
    ("Confidence", "confidence", 80),
    ("Notes", "notes", 120),
]

_LIGHT_ROW_COLOURS = {
    ERROR: wx.Colour(255, 215, 215),
    WARNING: wx.Colour(255, 240, 200),
}
_DARK_ROW_COLOURS = {
    ERROR: wx.Colour(115, 35, 35),
    WARNING: wx.Colour(105, 80, 15),
}

# Generate's confirmation lists at most this many issue lines.
_MAX_PROMPT_ISSUES = 12


def _row_colours(background):
    """Issue-row tints matched to the list's theme.

    Only the background is tinted; the theme's own text colour stays, so it
    contrasts in both light and dark mode.
    """
    luma = 0.299 * background.Red() + 0.587 * background.Green() + 0.114 * background.Blue()
    return _DARK_ROW_COLOURS if luma < 128 else _LIGHT_ROW_COLOURS


_TITLE = "JLCPCB Production Files"


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
        self._part_cache, self._footprint_cache = make_caches()
        board_file = adapter.board_path(board)
        self._basename = board_basename(board_file) if board_file else "board"
        self._init_ui(default_output_dir(board_file) if board_file else "")

    def shutdown(self):
        """Invalidate in-flight checks so their CallAfter callbacks no-op."""
        self._closing = True
        self._request_id += 1

    # ------------------------------------------------------------------ UI

    def _init_ui(self, out_dir):
        panel = self
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
        self.table.Bind(wx.EVT_SIZE, self._on_table_size)
        self.table.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_row_selected)
        vbox.Add(self.table, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.details = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 75))
        self.details.SetValue("Select a row to see all of its notes.")
        vbox.Add(self.details, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

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
        vbox.Add(hbox, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(vbox)

    def _on_table_size(self, event):
        event.Skip()
        fixed = sum(self.table.GetColumnWidth(i) for i in range(len(COLUMNS) - 1))
        notes_width = max(self.table.GetClientSize().width - fixed - 4, COLUMNS[-1][2])
        self.table.SetColumnWidth(len(COLUMNS) - 1, notes_width)

    def _on_row_selected(self, event):
        idx = event.GetIndex()
        if self._report is None or not 0 <= idx < len(self._report.rows):
            return
        row = self._report.rows[idx]
        cells = row_cells(row)
        head = "  ".join(v for v in (cells["ref"], cells["value"], cells["lcsc"], cells["stock"]) if v)
        if cells["rotation"]:
            head += f"  |  CPL rotation {cells['rotation']}, confidence {cells['confidence']}"
        issues = [f"[{i.severity.upper()}] {i.message}" for i in row.issues] or ["No issues."]
        self.details.SetValue("\n".join([head] + issues))

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
            wx.MessageBox(f"Could not read the board:\n{e}", _TITLE, wx.OK | wx.ICON_ERROR)
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
        if self._on_ssl_error is not None:
            self._on_ssl_error()

    def _on_progress(self, done, total, lcsc, request_id):
        if self._closing or request_id != self._request_id:
            return
        self.status.SetLabel(f"Checking parts {done}/{total} ({lcsc})...")

    def _on_check_failed(self, message, request_id):
        if self._closing or request_id != self._request_id:
            return
        self._set_busy(False)
        self.status.SetLabel("Check failed")
        wx.MessageBox(message, _TITLE, wx.OK | wx.ICON_ERROR)

    def _on_check_done(self, report, request_id, generate):
        if self._closing or request_id != self._request_id:
            return
        self._set_busy(False)
        self._show_report(report)
        self.status.SetLabel(f"{report.count(ERROR)} errors, {report.count(WARNING)} warnings")
        if generate:
            self._generate(report)

    def _show_report(self, report):
        self._report = report
        self.table.DeleteAllItems()
        self.details.SetValue("Select a row to see all of its notes.")
        row_colours = _row_colours(self.table.GetBackgroundColour())
        for row in report.rows:
            cells = row_cells(row)
            idx = self.table.InsertItem(self.table.GetItemCount(), cells["ref"])
            for col, (_title, key, _width) in enumerate(COLUMNS[1:], start=1):
                self.table.SetItem(idx, col, cells[key])
            colour = row_colours.get(row.severity)
            if colour is not None:
                self.table.SetItemBackgroundColour(idx, colour)
        # Issues first: they are what the user has to act on.
        issues = report.issue_lines()
        lines = issues + [""] if issues else []
        lines.extend(report.summary_lines())
        self.summary.SetValue("\n".join(lines))

    # ------------------------------------------------------------ generate

    def _generate(self, report):
        out_dir = self.dir_text.GetValue().strip()
        if not out_dir:
            wx.MessageBox("Choose an output folder first.", _TITLE, wx.OK | wx.ICON_WARNING)
            return
        errors, warnings = report.count(ERROR), report.count(WARNING)
        if errors or warnings:
            issues = report.issue_lines()
            shown = issues[:_MAX_PROMPT_ISSUES]
            if len(issues) > len(shown):
                shown.append(f"...and {len(issues) - len(shown)} more (see the list below the table)")
            answer = wx.MessageBox(
                f"The check found {errors} errors and {warnings} warnings:\n\n"
                + "\n".join(shown)
                + "\n\nGenerate production files anyway?",
                _TITLE,
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
                _TITLE,
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
            _TITLE,
            wx.OK | wx.ICON_INFORMATION,
        )
