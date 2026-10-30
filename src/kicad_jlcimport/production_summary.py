"""Check results as tables: a summary of the order and a list of distinct issues.

Shown on the Production tab under the parts table, and in the dialog that
asks before Generate writes any files.
"""

from typing import List, Optional, Sequence

import wx

from .production.report import ERROR, INFO, WARNING, ProductionReport

_LIGHT_ROW_COLOURS = {
    ERROR: wx.Colour(255, 215, 215),
    WARNING: wx.Colour(255, 240, 200),
}
_DARK_ROW_COLOURS = {
    ERROR: wx.Colour(115, 35, 35),
    WARNING: wx.Colour(105, 80, 15),
}

_SEVERITY_LABEL = {ERROR: "Error", WARNING: "Warning", INFO: "Note"}
_MAX_REFS_SHOWN = 6


def row_colours(background: wx.Colour) -> dict:
    """Issue-row tints matched to the list's theme.

    Only the background is tinted; the theme's own text colour stays, so it
    contrasts in both light and dark mode.
    """
    luma = 0.299 * background.Red() + 0.587 * background.Green() + 0.114 * background.Blue()
    return _DARK_ROW_COLOURS if luma < 128 else _LIGHT_ROW_COLOURS


def refs_text(refs: Sequence[str]) -> str:
    """Up to a few references, then a count: ``R1, R2, R3, … (12 parts)``."""
    if not refs:
        return "Board"
    if len(refs) <= _MAX_REFS_SHOWN:
        return ", ".join(refs)
    return ", ".join(refs[:3]) + f", … ({len(refs)} parts)"


class ReportSummaryView(wx.Panel):
    """Two tables side by side: Summary (item / value) and Issues (severity / parts / issue)."""

    def __init__(self, parent, height: int = 150):
        super().__init__(parent)
        box = wx.BoxSizer(wx.HORIZONTAL)
        self.facts = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_NO_HEADER | wx.LC_SINGLE_SEL, size=(-1, height))
        self.facts.InsertColumn(0, "Item", width=170)
        self.facts.InsertColumn(1, "Value", width=200)
        box.Add(self.facts, 2, wx.EXPAND)
        self.issues = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(-1, height))
        self.issues.InsertColumn(0, "Severity", width=70)
        self.issues.InsertColumn(1, "Parts", width=170)
        self.issues.InsertColumn(2, "Issue", width=400)
        self.issues.Bind(wx.EVT_SIZE, self._on_issues_size)
        box.Add(self.issues, 5, wx.EXPAND | wx.LEFT, 8)
        self.SetSizer(box)
        self._issue_rows: List[tuple] = []

    def clear(self, message: str = "") -> None:
        self.facts.DeleteAllItems()
        self.issues.DeleteAllItems()
        self._issue_rows = []
        if message:
            self.issues.InsertItem(0, "")
            self.issues.SetItem(0, 2, message)

    def set_report(self, report: Optional[ProductionReport]) -> None:
        self.clear()
        if report is None:
            return
        for label, value in report.summary_items():
            idx = self.facts.InsertItem(self.facts.GetItemCount(), label)
            self.facts.SetItem(idx, 1, value)
        colours = row_colours(self.issues.GetBackgroundColour())
        self._issue_rows = report.issue_groups()
        for severity, refs, message in self._issue_rows:
            idx = self.issues.InsertItem(self.issues.GetItemCount(), _SEVERITY_LABEL.get(severity, severity))
            self.issues.SetItem(idx, 1, refs_text(refs))
            self.issues.SetItem(idx, 2, message)
            colour = colours.get(severity)
            if colour is not None:
                self.issues.SetItemBackgroundColour(idx, colour)
        if not self._issue_rows:
            self.issues.InsertItem(0, "")
            self.issues.SetItem(0, 2, "No errors or warnings.")
        for col in (0, 1):
            self.facts.SetColumnWidth(col, wx.LIST_AUTOSIZE)

    def _on_issues_size(self, event) -> None:
        event.Skip()
        fixed = self.issues.GetColumnWidth(0) + self.issues.GetColumnWidth(1)
        self.issues.SetColumnWidth(2, max(self.issues.GetClientSize().width - fixed - 4, 200))


class GenerateConfirmDialog(wx.Dialog):
    """Asks before writing files: where they go, the order summary, and every issue.

    ``ShowModal()`` returns ``wx.ID_OK`` for Generate files, ``wx.ID_CANCEL`` otherwise.
    """

    def __init__(self, parent, report: ProductionReport, out_dir: str, file_names: Sequence[str]):
        super().__init__(
            parent,
            title="Generate production files?",
            size=(820, 520),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        vbox = wx.BoxSizer(wx.VERTICAL)
        errors, warnings = report.count(ERROR), report.count(WARNING)
        if errors:
            headline = f"The check found {errors} errors and {warnings} warnings."
        elif warnings:
            headline = f"The check found {warnings} warnings and no errors."
        else:
            headline = "The check found no errors or warnings."
        title = wx.StaticText(self, label=headline)
        title.SetFont(title.GetFont().Bold().Larger())
        vbox.Add(title, 0, wx.ALL, 12)
        vbox.Add(
            wx.StaticText(
                self,
                label="Review the summary and issues below. Rows on the Production tab show every part's notes, "
                "and JLCPCB's placement preview is the place to check polarity and orientation.",
            ),
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            12,
        )
        self.summary = ReportSummaryView(self, height=260)
        self.summary.set_report(report)
        vbox.Add(self.summary, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        dest = wx.FlexGridSizer(cols=2, hgap=8, vgap=4)
        dest.Add(wx.StaticText(self, label="Folder:"), 0)
        folder = wx.StaticText(self, label=out_dir)
        folder.SetFont(folder.GetFont().Bold())
        dest.Add(folder, 0)
        dest.Add(wx.StaticText(self, label="Files:"), 0)
        dest.Add(wx.StaticText(self, label=", ".join(file_names)), 0)
        vbox.Add(dest, 0, wx.ALL, 12)

        buttons = wx.StdDialogButtonSizer()
        self.generate_btn = wx.Button(self, wx.ID_OK, label="Generate files")
        self.generate_btn.SetDefault()
        buttons.AddButton(self.generate_btn)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, label="Cancel"))
        buttons.Realize()
        vbox.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizer(vbox)
        self.CentreOnParent()
