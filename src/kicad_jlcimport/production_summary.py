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
from .production.models import natural_key
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
_CARD_MIN = 200  # narrowest summary card before the cards wrap onto another row
_CARD_GAP = 12


HELP_TEXT = (
    "Check JLCPCB\n"
    "Looks up every LCSC part on the board in JLCPCB's parts library (stock for this many boards, Basic or "
    "Extended, price) and fetches JLCPCB's own footprint for it. Each part's position and rotation are then "
    "worked out by fitting JLCPCB's footprint onto your pads, so the placement file matches how JLCPCB "
    "places the part.\n\n"
    "Summary and details\n"
    "The summary shows the order, the fees and anything that needs a look. Review details lists every part "
    "with its notes. Polarized parts and loose fits are worth checking in JLCPCB's placement preview.\n\n"
    "Generate\n"
    "Writes a Gerber and drill zip, a BOM and a placement (CPL) file, ready to upload to JLCPCB, plus a "
    'report. If a part is placed wrongly, add a "JLCPCB Rotation Offset" or "JLCPCB Position Offset" '
    "field to its footprint and check again."
)


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


# --------------------------------------------------------------------- start


class StartScreen(wx.Panel):
    """Order options at the top and a large Check JLCPCB button in the middle (with progress while it runs)."""

    def __init__(self, parent, out_dir: str, on_check: Callable[[], None]):
        super().__init__(parent)
        vbox = wx.BoxSizer(wx.VERTICAL)
        title = wx.BoxSizer(wx.HORIZONTAL)
        title.Add(_heading(self, "JLCPCB production files"), 0, wx.ALIGN_CENTER_VERTICAL)
        help_btn = wx.BitmapButton(self, bitmap=wx.ArtProvider.GetBitmap(wx.ART_HELP, wx.ART_BUTTON, (16, 16)))
        help_btn.SetToolTip("How the check and export work")
        help_btn.Bind(wx.EVT_BUTTON, self._on_help)
        title.Add(help_btn, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 8)
        vbox.Add(title, 0, wx.TOP | wx.BOTTOM, _MARGIN)

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
        self.check_btn = wx.Button(self, label="Check JLCPCB")
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

    def _on_help(self, _event) -> None:
        wx.MessageDialog(self, HELP_TEXT, "JLCPCB production files", wx.OK | wx.ICON_INFORMATION).ShowModal()

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

        cards = wx.WrapSizer(wx.HORIZONTAL)
        self._cards = []
        for name, rows in report.summary_sections():
            box = wx.StaticBoxSizer(wx.VERTICAL, self, name)
            grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=12)
            grid.AddGrowableCol(0)
            for label, value in rows:
                grid.Add(wx.StaticText(box.GetStaticBox(), label=label), 0)
                strong = wx.StaticText(box.GetStaticBox(), label=value)
                strong.SetFont(strong.GetFont().Bold())
                grid.Add(strong, 0, wx.ALIGN_RIGHT)
            box.Add(grid, 1, wx.EXPAND | wx.ALL, 10)
            self._cards.append(cards.Add(box, 0, wx.EXPAND | wx.RIGHT | wx.BOTTOM, _CARD_GAP))
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
        cards = getattr(self, "_cards", [])
        if cards:
            # As many cards per row as fit at a readable width; the rest wrap to the next row.
            per_row = max(1, min(len(cards), (width + _CARD_GAP) // (_CARD_MIN + _CARD_GAP)))
            card_width = (width - per_row * _CARD_GAP) // per_row
            for item in cards:
                item.SetMinSize((card_width, -1))
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
    """Every part in a full-width, sortable list; the selected part (picture, facts, notes) below it."""

    def __init__(self, parent, on_cancel: Callable[[], None], on_generate: Callable[[], None], on_ssl_error=None):
        super().__init__(parent)
        self._report: Optional[ProductionReport] = None
        self._order: List[int] = []  # report row index shown on each table line
        self._sort_col = -1
        self._ascending = True
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
        self.table.Bind(wx.EVT_LIST_COL_CLICK, self._on_col_click)
        vbox.Add(self.table, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, _MARGIN)

        self.part_detail = PartDetailPanel(self, on_ssl_error=on_ssl_error, show_notes=True)
        vbox.Add(self.part_detail, 0, wx.EXPAND | wx.ALL, _MARGIN)
        self.SetSizer(vbox)

    def set_report(self, report: ProductionReport) -> None:
        self._report = report
        self.title.SetLabel(f"{len(report.rows)} parts")
        self._order = list(range(len(report.rows)))
        if self._sort_col >= 0:
            self._sort()
        self._fill()
        self.part_detail.clear()
        self.part_detail.set_notes([(None, "Select a part to see its picture, details and notes.")])
        self.Layout()

    def shutdown(self) -> None:
        self.part_detail.shutdown()

    def _fill(self) -> None:
        self.table.DeleteAllItems()
        colours = row_colours(self.table.GetBackgroundColour())
        for index in self._order:
            row = self._report.rows[index]
            cells = row_cells(row)
            idx = self.table.InsertItem(self.table.GetItemCount(), cells["ref"])
            for col, (_title, key, _width) in enumerate(COLUMNS[1:], start=1):
                self.table.SetItem(idx, col, cells[key])
            colour = colours.get(row.severity)
            if colour is not None:
                self.table.SetItemBackgroundColour(idx, colour)

    def _on_col_click(self, event) -> None:
        col = event.GetColumn()
        if self._report is None or col < 0:
            return
        self._ascending = not self._ascending if col == self._sort_col else True
        self._sort_col = col
        self._sort()
        self._fill()
        if hasattr(self.table, "ShowSortIndicator"):
            self.table.ShowSortIndicator(col, self._ascending)
        self.part_detail.clear()
        self.part_detail.set_notes([(None, "Select a part to see its picture, details and notes.")])

    def _sort(self) -> None:
        key = COLUMNS[self._sort_col][1]
        rows = self._report.rows
        self._order.sort(key=lambda i: sort_key(rows[i], key), reverse=not self._ascending)

    def _on_row_selected(self, event) -> None:
        line = event.GetIndex()
        if self._report is None or not 0 <= line < len(self._order):
            return
        row = self._report.rows[self._order[line]]
        cells = row_cells(row)
        head = f"{cells['ref']}  {cells['value']}"
        if cells["rotation"]:
            head += f"   CPL rotation {cells['rotation']}°, confidence {cells['confidence']}"
        notes = [(None, head)]
        notes += [(_SEVERITY_TEXT.get(i.severity), f"{_SEVERITY_LABEL[i.severity]}: {i.message}") for i in row.issues]
        if not row.issues:
            notes.append((None, "No issues."))
        self.part_detail.set_notes(notes)
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


_SEVERITY_RANK = {ERROR: 0, WARNING: 1, INFO: 2, "": 3}
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "manual": 2, "low": 3, "unverified": 4, "": 5}


def sort_key(row, key: str):
    """Sort key for a parts-list column: numbers as numbers, references in natural order."""
    cells = row_cells(row)
    if key in ("ref", "value", "lcsc"):
        return (natural_key(cells[key]),)
    if key == "stock":
        stock = row.availability.stock if row.availability is not None else None
        return (stock is None, stock or 0, cells["stock"])
    if key == "rotation":
        return (cells["rotation"] == "", float(cells["rotation"] or 0))
    if key == "confidence":
        return (_CONFIDENCE_RANK.get(cells["confidence"], 5),)
    if key == "notes":  # worst first, then by text
        return (_SEVERITY_RANK.get(row.severity, 3), cells["notes"])
    return (cells[key],)


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
