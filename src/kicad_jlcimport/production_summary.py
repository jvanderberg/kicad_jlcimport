"""Screens of the Production tab: start (options + Check), summary, part details, and the files-written dialog.

Each screen fills the tab.  Actions sit in a header row at the top; nothing
is pinned below the content.  The screens only lay out data and report
button presses through callbacks; ``production_panel`` does the work.
"""

import os
import subprocess
import sys
from typing import Callable, List, Optional, Sequence

import wx

from .part_detail import PartDetailPanel
from .production.report import ERROR, INFO, WARNING, ProductionReport, row_cells

COLUMNS = [  # the last column (Notes) is stretched to fill whatever width is left
    ("Ref", "ref", 60),
    ("Value", "value", 110),
    ("LCSC", "lcsc", 85),
    ("Stock", "stock", 150),
    ("Type", "type", 70),
    ("Rotation", "rotation", 65),
    ("Confidence", "confidence", 85),
    ("Notes", "notes", 150),
]

_LIGHT_ROW_COLOURS = {ERROR: wx.Colour(255, 215, 215), WARNING: wx.Colour(255, 240, 200)}
_DARK_ROW_COLOURS = {ERROR: wx.Colour(115, 35, 35), WARNING: wx.Colour(105, 80, 15)}
_SEVERITY_LABEL = {ERROR: "Error", WARNING: "Warning", INFO: "Note"}
_SEVERITY_TEXT = {ERROR: wx.Colour(200, 40, 40), WARNING: wx.Colour(190, 120, 0)}
_OK_TEXT = wx.Colour(30, 140, 60)
_MARGIN = 16
_SUMMARY_WIDTH = 820  # the summary is a centred column at most this wide
_START_WIDTH = 640  # the start screen's centred column


def row_colours(background: wx.Colour) -> dict:
    """Issue-row tints matched to the list's theme (only the background is tinted)."""
    luma = 0.299 * background.Red() + 0.587 * background.Green() + 0.114 * background.Blue()
    return _DARK_ROW_COLOURS if luma < 128 else _LIGHT_ROW_COLOURS


def headline(report: ProductionReport) -> tuple:
    """(text, colour) for the top of the summary."""
    errors, warnings = report.count(ERROR), report.count(WARNING)
    if errors:
        return f"{errors} errors and {warnings} warnings need a look before ordering", _SEVERITY_TEXT[ERROR]
    if warnings:
        return f"Ready to generate: {warnings} warnings to review, no errors", _SEVERITY_TEXT[WARNING]
    return "Ready to generate: no errors or warnings", _OK_TEXT


def open_folder(path: str) -> None:
    """Show *path* in the system file browser."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opening a folder the user chose
    else:
        subprocess.Popen(["xdg-open", path])


def _heading(parent, text: str, scale: float = 1.3) -> wx.StaticText:
    label = wx.StaticText(parent, label=text)
    font = label.GetFont().Bold()
    font.SetPointSize(int(round(font.GetPointSize() * scale)))
    label.SetFont(font)
    return label


def _button_row(parent, title: wx.Window, buttons: Sequence[wx.Button]) -> wx.BoxSizer:
    row = wx.BoxSizer(wx.HORIZONTAL)
    row.Add(title, 1, wx.ALIGN_CENTER_VERTICAL)
    for button in buttons:
        row.Add(button, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 8)
    return row


class _Wrapped(wx.StaticText):
    """Static text that re-wraps to a share of its page's width whenever the page is resized."""

    def __init__(self, parent, text: str, share: float, bold: bool = False, colour: Optional[wx.Colour] = None):
        super().__init__(parent, label=text)
        self._text = text
        self.share = share
        if bold:
            self.SetFont(self.GetFont().Bold())
        if colour is not None:
            self.SetForegroundColour(colour)

    def rewrap(self, page_width: int) -> None:
        self.SetLabel(self._text)
        self.Wrap(max(int(page_width * self.share), 80))


class BigButton(wx.Control):
    """A large rounded button in the system accent colour (native buttons have a fixed height on macOS).

    Sends ``wx.EVT_BUTTON`` when clicked, or when focused and Return or Space is pressed.
    """

    def __init__(self, parent, label: str, size=(320, 84)):
        super().__init__(parent, style=wx.BORDER_NONE | wx.WANTS_CHARS, size=size)
        self.SetMinSize(size)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._label = label
        self._hover = self._pressed = False
        font = self.GetFont().Bold()
        font.SetPointSize(int(round(font.GetPointSize() * 1.6)))
        self.SetFont(font)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, lambda e: self._set(hover=True))
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: self._set(hover=False, pressed=False))
        self.Bind(wx.EVT_LEFT_DOWN, lambda e: self._set(pressed=True))
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key)

    def AcceptsFocus(self):  # noqa: N802 - wx override
        return self.IsEnabled()

    def Enable(self, enable=True):  # noqa: N802 - wx override
        result = super().Enable(enable)
        self.Refresh()
        return result

    def _set(self, hover=None, pressed=None):
        if hover is not None:
            self._hover = hover
        if pressed is not None:
            self._pressed = pressed
        self.Refresh()

    def _click(self):
        if self.IsEnabled():
            event = wx.CommandEvent(wx.wxEVT_BUTTON, self.GetId())
            event.SetEventObject(self)
            self.GetEventHandler().ProcessEvent(event)

    def _on_left_up(self, event):
        clicked = self._pressed and self.GetClientRect().Contains(event.GetPosition())
        self._set(pressed=False)
        if clicked:
            self._click()

    def _on_key(self, event):
        if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            self._click()
        else:
            event.Skip()

    def _on_paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        if self.IsEnabled():
            colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT)
            if self._pressed:
                colour = colour.ChangeLightness(85)
            elif self._hover:
                colour = colour.ChangeLightness(110)
            text = wx.WHITE
        else:
            colour = wx.Colour(128, 128, 128)
            text = wx.Colour(220, 220, 220)
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(colour))
        gc.DrawRoundedRectangle(0, 0, w - 1, h - 1, 12)
        gc.SetFont(self.GetFont(), text)
        tw, th = gc.GetTextExtent(self._label)[:2]
        gc.DrawText(self._label, (w - tw) / 2, (h - th) / 2)


# --------------------------------------------------------------------- start


class StartScreen(wx.Panel):
    """Order options at the top and a large Check JLCPCB button in the middle (with progress while it runs)."""

    def __init__(self, parent, out_dir: str, on_check: Callable[[], None]):
        super().__init__(parent)
        vbox = wx.BoxSizer(wx.VERTICAL)
        vbox.Add(_heading(self, "JLCPCB production files"), 0, wx.TOP | wx.BOTTOM, _MARGIN)
        intro = wx.StaticText(
            self,
            label="Check looks up every LCSC part on the board (stock, basic/extended, price) and solves each "
            "part's placement against JLCPCB's own footprint. Then review the results and generate the "
            "Gerbers, BOM and CPL.",
        )
        intro.Wrap(_START_WIDTH)
        vbox.Add(intro, 0)

        grid = wx.FlexGridSizer(cols=3, vgap=8, hgap=8)
        grid.AddGrowableCol(1)
        grid.Add(wx.StaticText(self, label="Boards to assemble:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.qty_spin = wx.SpinCtrl(self, min=1, max=1000000, initial=5)
        grid.Add(self.qty_spin, 0)
        grid.Add((0, 0))
        grid.Add(wx.StaticText(self, label="Output folder:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.dir_text = wx.TextCtrl(self, value=out_dir)
        grid.Add(self.dir_text, 1, wx.EXPAND)
        browse_btn = wx.Button(self, label="Browse...")
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)
        grid.Add(browse_btn, 0)
        vbox.Add(grid, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, _MARGIN)

        self.refill_cb = wx.CheckBox(
            self, label="Refill zones before plotting Gerbers (changes the open board; not undoable)"
        )
        self.refill_cb.SetToolTip(
            "Refills every zone on the open board so the Gerbers match its copper. "
            "Save the board afterwards to keep the new fills."
        )
        self.refill_cb.SetValue(True)
        vbox.Add(self.refill_cb, 0, wx.BOTTOM, 8)
        self.stock_cb = wx.CheckBox(self, label="Check stock and prices")
        self.stock_cb.SetToolTip(
            "Look up every part in the JLCPCB library (stock for this many boards, basic/extended, price). "
            "Untick to check placements only, which is faster."
        )
        self.stock_cb.SetValue(True)
        vbox.Add(self.stock_cb, 0)

        vbox.AddStretchSpacer(1)
        self.check_btn = BigButton(self, label="Check JLCPCB")
        self.check_btn.Bind(wx.EVT_BUTTON, lambda _e: on_check())
        vbox.Add(self.check_btn, 0, wx.ALIGN_CENTER_HORIZONTAL)
        self.gauge = wx.Gauge(self, range=1, size=(360, -1))
        vbox.Add(self.gauge, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 16)
        self.progress = wx.StaticText(self, label="", style=wx.ALIGN_CENTRE_HORIZONTAL)
        vbox.Add(self.progress, 0, wx.EXPAND | wx.TOP, 6)
        vbox.AddStretchSpacer(2)
        # A centred column of fixed width, however wide the window is.
        outer = wx.BoxSizer(wx.HORIZONTAL)
        outer.AddStretchSpacer(1)
        outer.Add(vbox, 0, wx.EXPAND)
        outer.AddStretchSpacer(1)
        vbox.SetMinSize((_START_WIDTH, -1))
        self.SetSizer(outer)
        self.set_running(False)

    def set_running(self, running: bool) -> None:
        for ctrl in (self.check_btn, self.qty_spin, self.dir_text, self.stock_cb, self.refill_cb):
            ctrl.Enable(not running)
        self.gauge.Show(running)
        self.progress.Show(running)
        if running:
            self.gauge.SetRange(1)
            self.gauge.SetValue(0)
            self.progress.SetLabel("Reading the board...")
        self.Layout()

    def set_progress(self, done: int, total: int, lcsc: str) -> None:
        self.gauge.SetRange(max(total, 1))
        self.gauge.SetValue(min(done, max(total, 1)))
        self.progress.SetLabel(f"Checking part {done} of {total} ({lcsc})")
        self.Layout()

    def _on_browse(self, _event) -> None:
        dlg = wx.DirDialog(self, "Choose output folder", self.dir_text.GetValue())
        if dlg.ShowModal() == wx.ID_OK:
            self.dir_text.SetValue(dlg.GetPath())
        dlg.Destroy()


# ------------------------------------------------------------------- summary


class SummaryScreen(wx.ScrolledWindow):
    """The check at a glance: headline, Order / Fees / Placements, and every distinct error and warning.

    The whole screen scrolls when it doesn't fit; there are no inner scroll boxes.
    """

    def __init__(
        self, parent, on_cancel: Callable[[], None], on_review: Callable[[], None], on_generate: Callable[[], None]
    ):
        super().__init__(parent, style=wx.VSCROLL)
        self.SetScrollRate(0, 16)
        self._on_cancel, self._on_review, self._on_generate = on_cancel, on_review, on_generate
        self._wrapped: List[_Wrapped] = []
        self.Bind(wx.EVT_SIZE, self._on_size)

    def set_report(self, report: ProductionReport, out_dir: str) -> None:
        self.DestroyChildren()
        self._wrapped = []
        col = wx.BoxSizer(wx.VERTICAL)

        text, colour = headline(report)
        title = self._wrap(text, 0.55, bold=True)
        font = title.GetFont()
        font.SetPointSize(int(round(font.GetPointSize() * 1.3)))
        title.SetFont(font)
        title.SetForegroundColour(colour)
        cancel = wx.Button(self, label="Cancel")
        cancel.Bind(wx.EVT_BUTTON, lambda _e: self._on_cancel())
        review = wx.Button(self, label="Review details")
        review.Bind(wx.EVT_BUTTON, lambda _e: self._on_review())
        generate = wx.Button(self, label="Generate")
        generate.Bind(wx.EVT_BUTTON, lambda _e: self._on_generate())
        generate.SetDefault()
        col.Add(_button_row(self, title, [cancel, review, generate]), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, _MARGIN)
        folder = f"Files go to {out_dir}" if out_dir else "Choose an output folder on the start screen."
        col.Add(self._wrap(folder, 1.0), 0, wx.BOTTOM, _MARGIN)

        cards = wx.BoxSizer(wx.HORIZONTAL)
        for i, (name, rows) in enumerate(report.summary_sections()):
            box = wx.StaticBoxSizer(wx.VERTICAL, self, name)
            grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=12)
            grid.AddGrowableCol(0)
            for label, value in rows:
                grid.Add(wx.StaticText(box.GetStaticBox(), label=label), 0)
                strong = wx.StaticText(box.GetStaticBox(), label=value)
                strong.SetFont(strong.GetFont().Bold())
                grid.Add(strong, 0, wx.ALIGN_RIGHT)
            box.Add(grid, 1, wx.EXPAND | wx.ALL, 10)
            cards.Add(box, 1, wx.EXPAND | (wx.LEFT if i else 0), 12)
        col.Add(cards, 0, wx.EXPAND | wx.BOTTOM, _MARGIN)

        groups = report.issue_groups()
        issues = wx.StaticBoxSizer(wx.VERTICAL, self, "Issues")
        parent = issues.GetStaticBox()
        if groups:
            grid = wx.FlexGridSizer(cols=3, vgap=10, hgap=16)
            grid.AddGrowableCol(2)
            for severity, refs, message in groups:
                sev = wx.StaticText(parent, label=_SEVERITY_LABEL.get(severity, severity))
                sev.SetFont(sev.GetFont().Bold())
                if severity in _SEVERITY_TEXT:
                    sev.SetForegroundColour(_SEVERITY_TEXT[severity])
                grid.Add(sev, 0, wx.ALIGN_TOP)
                grid.Add(wx.StaticText(parent, label=_part_count(refs)), 0, wx.ALIGN_TOP)
                grid.Add(self._wrap(message, 0.7, parent=parent), 1, wx.EXPAND)
            issues.Add(grid, 0, wx.EXPAND | wx.ALL, 10)
            issues.Add(
                wx.StaticText(parent, label="Review details lists the parts and every note."),
                0,
                wx.LEFT | wx.RIGHT | wx.BOTTOM,
                10,
            )
        else:
            ok = wx.StaticText(parent, label="No errors or warnings.")
            ok.SetForegroundColour(_OK_TEXT)
            issues.Add(ok, 0, wx.ALL, 10)
        col.Add(issues, 0, wx.EXPAND | wx.BOTTOM, _MARGIN)

        # A centred column of readable width, however wide the window is.
        outer = wx.BoxSizer(wx.HORIZONTAL)
        outer.AddStretchSpacer(1)
        self._column = outer.Add(col, 0, wx.EXPAND)
        outer.AddStretchSpacer(1)
        self.SetSizer(outer)
        self._rewrap()

    def _wrap(self, text: str, share: float, parent=None, bold: bool = False) -> _Wrapped:
        label = _Wrapped(parent or self, text, share, bold=bold)
        self._wrapped.append(label)
        return label

    def _rewrap(self) -> None:
        width = min(self.GetClientSize().width - 2 * _MARGIN, _SUMMARY_WIDTH)
        width = max(width, 400)
        if getattr(self, "_column", None) is not None:
            self._column.SetMinSize((width, -1))
        for label in self._wrapped:
            label.rewrap(width)
        self.FitInside()
        self.Layout()

    def _on_size(self, event) -> None:
        event.Skip()
        wx.CallAfter(self._rewrap)


def _part_count(refs: Sequence[str]) -> str:
    if not refs:
        return "Board"
    return f"{len(refs)} part{'s' if len(refs) != 1 else ''}"


# ------------------------------------------------------------------- details


class DetailsScreen(wx.Panel):
    """Every part in a full-width list; the selected part (picture, facts, notes) below it."""

    def __init__(self, parent, on_cancel: Callable[[], None], on_generate: Callable[[], None], on_ssl_error=None):
        super().__init__(parent)
        self._report: Optional[ProductionReport] = None
        vbox = wx.BoxSizer(wx.VERTICAL)
        self.title = _heading(self, "Parts")
        cancel = wx.Button(self, label="Cancel")
        cancel.Bind(wx.EVT_BUTTON, lambda _e: on_cancel())
        generate = wx.Button(self, label="Generate")
        generate.Bind(wx.EVT_BUTTON, lambda _e: on_generate())
        vbox.Add(_button_row(self, self.title, [cancel, generate]), 0, wx.EXPAND | wx.ALL, _MARGIN)

        self.table = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.table.SetMinSize((400, 200))  # scrolls sideways rather than widening the window
        for i, (title, _key, width) in enumerate(COLUMNS):
            self.table.InsertColumn(i, title, width=width)
        self.table.Bind(wx.EVT_SIZE, self._on_table_size)
        self.table.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_row_selected)
        vbox.Add(self.table, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, _MARGIN)

        detail = wx.BoxSizer(wx.HORIZONTAL)
        self.part_detail = PartDetailPanel(self, on_ssl_error=on_ssl_error)
        detail.Add(self.part_detail, 0, wx.EXPAND)
        notes_box = wx.StaticBoxSizer(wx.VERTICAL, self, "Notes")
        self.notes = notes_box.GetStaticBox()
        self.notes_sizer = wx.BoxSizer(wx.VERTICAL)
        notes_box.Add(self.notes_sizer, 1, wx.EXPAND | wx.ALL, 8)
        detail.Add(notes_box, 1, wx.EXPAND | wx.LEFT, 12)
        vbox.Add(detail, 0, wx.EXPAND | wx.ALL, _MARGIN)
        self.SetSizer(vbox)

    def set_report(self, report: ProductionReport) -> None:
        self._report = report
        self.title.SetLabel(f"{len(report.rows)} parts")
        self.table.DeleteAllItems()
        colours = row_colours(self.table.GetBackgroundColour())
        for row in report.rows:
            cells = row_cells(row)
            idx = self.table.InsertItem(self.table.GetItemCount(), cells["ref"])
            for col, (_title, key, _width) in enumerate(COLUMNS[1:], start=1):
                self.table.SetItem(idx, col, cells[key])
            colour = colours.get(row.severity)
            if colour is not None:
                self.table.SetItemBackgroundColour(idx, colour)
        self.part_detail.clear()
        self._set_notes([("", "Select a part to see its picture, details and notes.")])
        self.Layout()

    def shutdown(self) -> None:
        self.part_detail.shutdown()

    def _set_notes(self, notes: Sequence[tuple]) -> None:
        self.notes_sizer.Clear(delete_windows=True)
        width = max(self.notes.GetClientSize().width - 20, 200)
        for severity, message in notes:
            text = f"{_SEVERITY_LABEL[severity]}: {message}" if severity in _SEVERITY_LABEL else message
            label = wx.StaticText(self.notes, label=text)
            if severity in _SEVERITY_TEXT:
                label.SetForegroundColour(_SEVERITY_TEXT[severity])
            label.Wrap(width)
            self.notes_sizer.Add(label, 0, wx.BOTTOM, 6)
        self.Layout()

    def _on_row_selected(self, event) -> None:
        idx = event.GetIndex()
        if self._report is None or not 0 <= idx < len(self._report.rows):
            return
        row = self._report.rows[idx]
        cells = row_cells(row)
        head = f"{cells['ref']}  {cells['value']}"
        if cells["rotation"]:
            head += f"   CPL rotation {cells['rotation']}°, confidence {cells['confidence']}"
        notes = [("", head)] + [(i.severity, i.message) for i in row.issues]
        if not row.issues:
            notes.append(("", "No issues."))
        self._set_notes(notes)
        if cells["lcsc"]:
            info = row.availability.info if row.availability is not None else {}
            self.part_detail.show(cells["lcsc"], info)
        else:
            self.part_detail.clear()

    def _on_table_size(self, event) -> None:
        event.Skip()
        fixed = sum(self.table.GetColumnWidth(i) for i in range(len(COLUMNS) - 1))
        notes_width = max(self.table.GetClientSize().width - fixed - 4, COLUMNS[-1][2])
        self.table.SetColumnWidth(len(COLUMNS) - 1, notes_width)


# -------------------------------------------------------------- files written


class FilesWrittenDialog(wx.Dialog):
    """Where the files went, with Open folder and OK."""

    def __init__(self, parent, out_dir: str, paths: Sequence[str]):
        super().__init__(parent, title="Production files written")
        self._out_dir = out_dir
        vbox = wx.BoxSizer(wx.VERTICAL)
        vbox.Add(wx.StaticText(self, label="Production files written to:"), 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        folder = wx.StaticText(self, label=out_dir)
        folder.SetFont(folder.GetFont().Bold())
        vbox.Add(folder, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)
        vbox.Add(wx.StaticText(self, label="\n".join(os.path.basename(p) for p in paths)), 0, wx.ALL, 16)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.AddStretchSpacer(1)
        open_btn = wx.Button(self, label="Open folder")
        open_btn.Bind(wx.EVT_BUTTON, self._on_open)
        buttons.Add(open_btn, 0, wx.RIGHT, 8)
        ok = wx.Button(self, wx.ID_OK, label="OK")
        ok.SetDefault()
        buttons.Add(ok, 0)
        vbox.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)
        self.SetSizerAndFit(vbox)
        self.CentreOnParent()

    def _on_open(self, _event) -> None:
        open_folder(self._out_dir)
        self.EndModal(wx.ID_OK)
