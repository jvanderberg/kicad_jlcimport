"""KiCad ActionPlugin for JLCPCB production export."""

import os

import pcbnew
import wx

from .production_dialog import ProductionExportDialog


class JLCProductionExportPlugin(pcbnew.ActionPlugin):
    _dialog = None

    def defaults(self):
        self.name = "JLCPCB Production Export"
        self.category = "Fabrication"
        self.description = "Check JLCPCB part availability and generate Gerbers, BOM and CPL (pick-and-place) files"
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), "icon.png")

    @staticmethod
    def _clear_dialog():
        JLCProductionExportPlugin._dialog = None

    def Run(self):
        if JLCProductionExportPlugin._dialog is not None:
            JLCProductionExportPlugin._dialog.Raise()
            return
        board = pcbnew.GetBoard()
        parent = wx.FindWindowByName("PcbFrame")
        dlg = ProductionExportDialog(parent, board, on_close=self._clear_dialog)
        JLCProductionExportPlugin._dialog = dlg
        dlg.Show()
