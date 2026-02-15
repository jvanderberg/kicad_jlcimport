"""wxPython dialog for JLCImport plugin."""

import io
import os
import re
import threading
import traceback
import webbrowser

import wx

from .categories import CATEGORIES
from .easyeda import api as _api_module
from .easyeda.api import (
    APIError,
    SSLCertError,
    fetch_product_image,
    filter_by_min_stock,
    filter_by_type,
    search_components,
    validate_lcsc_id,
)
from .importer import import_component
from .kicad.library import get_global_lib_dir, load_config, save_config
from .kicad.version import DEFAULT_KICAD_VERSION, SUPPORTED_VERSIONS


class _CategoryPopup(wx.PopupWindow):
    """Owner-drawn category suggestions popup.

    Draws items directly on the popup surface rather than using a child
    wx.ListBox.  This avoids two cross-platform issues with PopupWindow:
    Windows does not forward mouse events to child controls, and macOS
    requires an extra click to activate the popup before children respond.
    """

    ITEM_PAD = 6  # vertical padding per item

    def __init__(self, parent, on_select=None):
        super().__init__(parent, flags=wx.BORDER_SIMPLE)
        self._items = []
        self._hover = -1
        self._selection = wx.NOT_FOUND
        self._on_select = on_select
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_click)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

    # -- public API matching the subset used by the dialog --

    def Set(self, items):
        self._items = list(items)
        self._hover = -1
        self._selection = wx.NOT_FOUND
        self.Refresh()

    def GetSelection(self):
        return self._selection

    def GetString(self, idx):
        if 0 <= idx < len(self._items):
            return self._items[idx]
        return ""

    def GetCharHeight(self):
        dc = wx.ClientDC(self.GetParent())
        return dc.GetCharHeight()

    def Popup(self):
        self.Show()

    def Dismiss(self):
        self.Hide()

    # -- internals --

    def _item_height(self):
        return self.GetCharHeight() + self.ITEM_PAD

    def _hit_test(self, y):
        ih = self._item_height()
        if ih <= 0:
            return -1
        idx = y // ih
        return idx if 0 <= idx < len(self._items) else -1

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetFont(self.GetParent().GetFont())
        w, _ = self.GetClientSize()
        ih = self._item_height()
        dc.SetBackground(wx.Brush(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)))
        dc.Clear()
        for i, item in enumerate(self._items):
            y = i * ih
            if i == self._hover:
                dc.SetBrush(wx.Brush(wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT)))
                dc.SetPen(wx.TRANSPARENT_PEN)
                dc.DrawRectangle(0, y, w, ih)
                dc.SetTextForeground(wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHTTEXT))
            else:
                dc.SetTextForeground(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
            dc.DrawText(item, 4, y + self.ITEM_PAD // 2)

    def _on_motion(self, event):
        idx = self._hit_test(event.GetY())
        if idx != self._hover:
            self._hover = idx
            self.Refresh()

    def _on_leave(self, event):
        if self._hover != -1:
            self._hover = -1
            self.Refresh()

    def _on_click(self, event):
        idx = self._hit_test(event.GetY())
        if idx >= 0:
            self._selection = idx
            if self._on_select:
                self._on_select()


class JLCImportDialog(wx.Dialog):
    def __init__(self, parent, board, project_dir=None, kicad_version=None, global_lib_dir=""):
        super().__init__(parent, title="JLCImport", size=(700, 600), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self._project_dir = project_dir  # Used when board is None (standalone mode)
        self._kicad_version = kicad_version or DEFAULT_KICAD_VERSION
        self._global_lib_dir_override = global_lib_dir
        self._search_results = []
        self._raw_search_results = []
        self._search_request_id = 0
        self._image_request_id = 0
        self._gallery_request_id = 0
        self._ssl_warning_shown = False
        self._init_ui()
        self.Centre()

    def _init_ui(self):
        self._root_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- Main panel (search/results/details/import) ---
        panel = wx.Panel(self)
        self._main_panel = panel
        vbox = wx.BoxSizer(wx.VERTICAL)

        # --- Search section ---
        search_box = wx.BoxSizer(wx.VERTICAL)

        # Search input row
        hbox_search = wx.BoxSizer(wx.HORIZONTAL)
        self.search_input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search_input.SetHint("Search JLCPCB parts...")
        self.search_input.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        self.search_input.Bind(wx.EVT_TEXT, self._on_search_text_changed)
        hbox_search.Add(self.search_input, 1, wx.EXPAND | wx.RIGHT, 5)
        self.search_btn = wx.Button(panel, label="Search")
        self.search_btn.Bind(wx.EVT_BUTTON, self._on_search)
        hbox_search.Add(self.search_btn, 0)
        search_box.Add(hbox_search, 0, wx.EXPAND | wx.ALL, 5)

        # Filter row
        hbox_filter = wx.BoxSizer(wx.HORIZONTAL)
        hbox_filter.Add(wx.StaticText(panel, label="Type:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.type_both = wx.RadioButton(panel, label="Both", style=wx.RB_GROUP)
        self.type_basic = wx.RadioButton(panel, label="Basic")
        self.type_extended = wx.RadioButton(panel, label="Extended")
        self.type_both.SetValue(True)
        self.type_both.Bind(wx.EVT_RADIOBUTTON, self._on_type_change)
        self.type_basic.Bind(wx.EVT_RADIOBUTTON, self._on_type_change)
        self.type_extended.Bind(wx.EVT_RADIOBUTTON, self._on_type_change)
        hbox_filter.Add(self.type_both, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        hbox_filter.Add(self.type_basic, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        hbox_filter.Add(self.type_extended, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        hbox_filter.Add(wx.StaticText(panel, label="Min stock:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._min_stock_choices = [0, 1, 10, 100, 1000, 10000, 100000]
        self._min_stock_labels = ["Any", "1+", "10+", "100+", "1000+", "10000+", "100000+"]
        self.min_stock_choice = wx.Choice(panel, choices=self._min_stock_labels)
        self.min_stock_choice.SetSelection(1)  # Default to "1+" (in stock)
        self.min_stock_choice.Bind(wx.EVT_CHOICE, self._on_min_stock_change)
        hbox_filter.Add(self.min_stock_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        hbox_filter.Add(wx.StaticText(panel, label="Package:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.package_choice = wx.Choice(panel, choices=["All"])
        self.package_choice.SetSelection(0)
        self.package_choice.Bind(wx.EVT_CHOICE, self._on_filter_change)
        hbox_filter.Add(self.package_choice, 0, wx.ALIGN_CENTER_VERTICAL)
        search_box.Add(hbox_filter, 0, wx.LEFT | wx.RIGHT, 5)

        vbox.Add(search_box, 0, wx.EXPAND | wx.ALL, 5)

        self.results_count_label = wx.StaticText(panel, label="")
        vbox.Add(self.results_count_label, 0, wx.LEFT | wx.RIGHT, 10)

        # --- Results list ---
        self.results_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.results_list.InsertColumn(0, "LCSC", width=80)
        self.results_list.InsertColumn(1, "Type", width=55)
        self.results_list.InsertColumn(2, "Price", width=60)
        self.results_list.InsertColumn(3, "Stock", width=75)
        self.results_list.InsertColumn(4, "Part", width=200)
        self.results_list.InsertColumn(5, "Package", width=80)
        self.results_list.InsertColumn(6, "Description", width=300)
        self.results_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_result_select)
        self.results_list.Bind(wx.EVT_LIST_COL_CLICK, self._on_col_click)
        self._sort_col = -1
        self._sort_ascending = True
        self._imported_ids = set()
        vbox.Add(self.results_list, 2, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)

        # --- Detail panel (shown on selection) ---
        self._detail_box = wx.BoxSizer(wx.HORIZONTAL)

        # Image on left (click to zoom)
        self.detail_image = wx.StaticBitmap(panel, size=(100, 100))
        self.detail_image.SetMinSize((100, 100))
        self.detail_image.SetCursor(wx.Cursor(wx.CURSOR_MAGNIFIER))
        self.detail_image.Bind(wx.EVT_LEFT_DOWN, self._on_image_click)
        self._full_image_data = None
        self._detail_box.Add(self.detail_image, 0, wx.ALL, 5)

        # Info on right
        info_sizer = wx.BoxSizer(wx.VERTICAL)
        detail_grid = wx.FlexGridSizer(cols=4, hgap=10, vgap=4)
        detail_grid.AddGrowableCol(1)
        detail_grid.AddGrowableCol(3)

        bold_font = wx.Font(wx.FontInfo().Bold())

        bold_font = panel.GetFont().Bold()

        detail_grid.Add(wx.StaticText(panel, label="Part"), 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.detail_part = wx.StaticText(panel, label="")
        self.detail_part.SetFont(bold_font)
        detail_grid.Add(self.detail_part, 1, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)
        detail_grid.Add(wx.StaticText(panel, label="LCSC"), 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.detail_lcsc = wx.StaticText(panel, label="")
        self.detail_lcsc.SetFont(bold_font)
        detail_grid.Add(self.detail_lcsc, 0, wx.ALIGN_CENTER_VERTICAL)

        detail_grid.Add(wx.StaticText(panel, label="Brand"), 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.detail_brand = wx.StaticText(panel, label="")
        self.detail_brand.SetFont(bold_font)
        detail_grid.Add(self.detail_brand, 1, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)
        detail_grid.Add(wx.StaticText(panel, label="Package"), 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.detail_package = wx.StaticText(panel, label="")
        self.detail_package.SetFont(bold_font)
        detail_grid.Add(self.detail_package, 0, wx.ALIGN_CENTER_VERTICAL)

        detail_grid.Add(wx.StaticText(panel, label="Price"), 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.detail_price = wx.StaticText(panel, label="")
        self.detail_price.SetFont(bold_font)
        detail_grid.Add(self.detail_price, 0, wx.ALIGN_CENTER_VERTICAL)
        detail_grid.Add(wx.StaticText(panel, label="Stock"), 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.detail_stock = wx.StaticText(panel, label="")
        self.detail_stock.SetFont(bold_font)
        detail_grid.Add(self.detail_stock, 0, wx.ALIGN_CENTER_VERTICAL)

        info_sizer.Add(detail_grid, 0, wx.EXPAND | wx.BOTTOM, 4)

        self.detail_desc = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE
        )
        self.detail_desc.SetMinSize((-1, 40))
        info_sizer.Add(self.detail_desc, 1, wx.EXPAND | wx.BOTTOM, 4)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.detail_import_btn = wx.Button(panel, label="Import")
        self.detail_import_btn.Bind(wx.EVT_BUTTON, self._on_import)
        self.detail_import_btn.Disable()
        btn_sizer.Add(self.detail_import_btn, 0, wx.RIGHT, 5)
        self.detail_datasheet_btn = wx.Button(panel, label="Datasheet")
        self.detail_datasheet_btn.Bind(wx.EVT_BUTTON, self._on_datasheet)
        self.detail_datasheet_btn.Disable()
        btn_sizer.Add(self.detail_datasheet_btn, 0, wx.RIGHT, 5)
        self.detail_lcsc_btn = wx.Button(panel, label="LCSC Page")
        self.detail_lcsc_btn.Bind(wx.EVT_BUTTON, self._on_lcsc_page)
        self.detail_lcsc_btn.Disable()
        btn_sizer.Add(self.detail_lcsc_btn, 0)
        self._datasheet_url = ""
        self._lcsc_page_url = ""
        info_sizer.Add(btn_sizer, 0)

        self._detail_box.Add(info_sizer, 1, wx.EXPAND | wx.ALL, 5)

        vbox.Add(self._detail_box, 0, wx.EXPAND | wx.ALL, 5)

        # --- Import section ---
        import_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Import")

        project_dir = self._get_project_dir()
        if self._global_lib_dir_override:
            global_dir = self._global_lib_dir_override
        else:
            try:
                global_dir = get_global_lib_dir(self._kicad_version)
            except ValueError:
                # Custom dir in config doesn't exist; clear it and fall back
                config = load_config()
                config["global_lib_dir"] = ""
                save_config(config)
                global_dir = get_global_lib_dir(self._kicad_version)
        self._global_lib_dir = global_dir
        bold_font = panel.GetFont().Bold()

        # Row 1: Project destination | Part # input | Overwrite
        proj_row = wx.BoxSizer(wx.HORIZONTAL)
        self.dest_project = wx.RadioButton(panel, label="Project", style=wx.RB_GROUP)
        self.dest_project.Bind(wx.EVT_RADIOBUTTON, self._on_dest_change)
        proj_row.Add(self.dest_project, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        proj_path_label = wx.StaticText(panel, label=project_dir or "(no board open)")
        proj_path_label.SetFont(bold_font)
        proj_row.Add(proj_path_label, 0, wx.ALIGN_CENTER_VERTICAL)
        proj_row.AddStretchSpacer()
        proj_row.Add(wx.StaticText(panel, label="Part #"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.part_input = wx.TextCtrl(panel, size=(100, -1))
        self.part_input.SetHint("C427602")
        proj_row.Add(self.part_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.overwrite_cb = wx.CheckBox(panel, label="Overwrite")
        proj_row.Add(self.overwrite_cb, 0, wx.ALIGN_CENTER_VERTICAL)
        import_box.Add(proj_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        # Row 2: Global destination | Browse | Reset | Import button
        global_row = wx.BoxSizer(wx.HORIZONTAL)
        self.dest_global = wx.RadioButton(panel, label="Global")
        self.dest_global.Bind(wx.EVT_RADIOBUTTON, self._on_dest_change)
        global_row.Add(self.dest_global, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._global_path_label = wx.StaticText(panel, label=self._truncate_path(global_dir))
        self._global_path_label.SetFont(bold_font)
        self._global_path_label.SetToolTip(global_dir)
        global_row.Add(self._global_path_label, 0, wx.ALIGN_CENTER_VERTICAL)
        self._global_browse_btn = wx.Button(panel, label="...", size=(30, -1))
        self._global_browse_btn.Bind(wx.EVT_BUTTON, self._on_global_browse)
        global_row.Add(self._global_browse_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)
        self._global_reset_btn = wx.Button(panel, label="\u2715", size=(30, -1))
        self._global_reset_btn.Bind(wx.EVT_BUTTON, self._on_global_reset)
        global_row.Add(self._global_reset_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 2)
        global_row.AddStretchSpacer()
        self.import_btn = wx.Button(panel, label="Import")
        self.import_btn.Bind(wx.EVT_BUTTON, self._on_import)
        global_row.Add(self.import_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        import_box.Add(global_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        _config = load_config()
        self._apply_saved_destination(project_dir, _config)

        # Row 3: Library name | KiCad version
        lib_name_sizer = wx.BoxSizer(wx.HORIZONTAL)
        lib_name_sizer.Add(wx.StaticText(panel, label="Library"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._lib_name = _config.get("lib_name", "JLCImport")
        self.lib_name_input = wx.TextCtrl(panel, size=(120, -1), value=self._lib_name)
        self.lib_name_input.Bind(wx.EVT_KILL_FOCUS, self._on_lib_name_change)
        lib_name_sizer.Add(self.lib_name_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 15)
        lib_name_sizer.Add(wx.StaticText(panel, label="KiCad"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._version_labels = [str(v) for v in sorted(SUPPORTED_VERSIONS)]
        self.version_choice = wx.Choice(panel, choices=self._version_labels)
        default_idx = self._version_labels.index(str(self._kicad_version))
        self.version_choice.SetSelection(default_idx)
        self.version_choice.Bind(wx.EVT_CHOICE, self._on_version_change)
        lib_name_sizer.Add(self.version_choice, 0, wx.ALIGN_CENTER_VERTICAL)
        import_box.Add(lib_name_sizer, 0, wx.ALL, 5)

        vbox.Add(import_box, 0, wx.EXPAND | wx.ALL, 5)

        # --- Status log ---
        self.status_text = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.status_text.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        vbox.Add(self.status_text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)

        panel.SetSizer(vbox)

        # Category suggestions popup (owner-drawn for cross-platform compatibility)
        self._category_popup = _CategoryPopup(self, on_select=lambda: self._on_category_selected(None))

        # --- Gallery panel (hidden by default) ---
        self._gallery_panel = wx.Panel(self)
        self._gallery_panel.Hide()
        gbox = wx.BoxSizer(wx.VERTICAL)

        # Top row: back button
        top_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._gallery_back = wx.Button(self._gallery_panel, label="\u2190 Back")
        self._gallery_back.Bind(wx.EVT_BUTTON, self._on_gallery_close)
        top_sizer.Add(self._gallery_back, 0)
        gbox.Add(top_sizer, 0, wx.LEFT | wx.TOP, 5)

        # Navigation row: [<] image [>]
        nav_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._gallery_prev = wx.Button(self._gallery_panel, label="\u25c0", size=(40, -1))
        self._gallery_prev.Bind(wx.EVT_BUTTON, self._on_gallery_prev)
        nav_sizer.Add(self._gallery_prev, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)

        self._gallery_image = wx.StaticBitmap(self._gallery_panel)
        self._gallery_image.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self._gallery_image.Bind(wx.EVT_LEFT_DOWN, self._on_gallery_close)
        nav_sizer.Add(self._gallery_image, 1, wx.EXPAND)

        self._gallery_next = wx.Button(self._gallery_panel, label="\u25b6", size=(40, -1))
        self._gallery_next.Bind(wx.EVT_BUTTON, self._on_gallery_next)
        nav_sizer.Add(self._gallery_next, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)

        gbox.Add(nav_sizer, 1, wx.EXPAND | wx.ALL, 5)

        # Details below image
        self._gallery_info = wx.StaticText(self._gallery_panel, label="", style=wx.ST_NO_AUTORESIZE)
        self._gallery_info.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        gbox.Add(self._gallery_info, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self._gallery_desc = wx.StaticText(self._gallery_panel, label="", style=wx.ST_NO_AUTORESIZE)
        gbox.Add(self._gallery_desc, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self._gallery_panel.SetSizer(gbox)
        self._gallery_index = 0

        # Root sizer holds both panels
        self._root_sizer.Add(panel, 1, wx.EXPAND)
        self._root_sizer.Add(self._gallery_panel, 1, wx.EXPAND)
        self.SetSizer(self._root_sizer)

        # Escape key to close gallery
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)

    def _get_project_dir(self) -> str:
        if self.board:
            board_path = self.board.GetFileName()
            if board_path:
                return os.path.dirname(board_path)
        # Standalone mode: use provided project_dir
        if self._project_dir:
            return self._project_dir
        return ""

    def _apply_saved_destination(self, project_dir: str, config=None):
        """Set the destination radio buttons from the saved config preference."""
        if config is None:
            config = load_config()
        saved_use_global = config.get("use_global", False)
        if not project_dir:
            self.dest_project.Disable()
            self.dest_global.SetValue(True)
        elif saved_use_global:
            self.dest_global.SetValue(True)
        else:
            self.dest_project.SetValue(True)

    @staticmethod
    def _truncate_path(path: str, max_len: int = 50) -> str:
        """Truncate a path with a middle ellipsis if it exceeds *max_len*."""
        if len(path) <= max_len:
            return path
        keep = max_len - 3
        left = keep // 2
        right = keep - left
        return path[:left] + "\u2026" + path[-right:]

    def _set_global_path(self, path: str) -> None:
        """Update the global path label and tooltip."""
        self._global_path_label.SetLabel(self._truncate_path(path))
        self._global_path_label.SetToolTip(path)
        self._global_path_label.GetParent().Layout()

    def _persist_destination(self):
        """Save the current destination choice to config."""
        use_global = self.dest_global.GetValue()
        config = load_config()
        config["use_global"] = use_global
        save_config(config)

    def _on_lib_name_change(self, event):
        """Persist library name when the input loses focus."""
        new_name = self.lib_name_input.GetValue().strip()
        if new_name and new_name != self._lib_name:
            self._lib_name = new_name
            config = load_config()
            config["lib_name"] = new_name
            save_config(config)
        elif not new_name:
            self.lib_name_input.SetValue(self._lib_name)
        event.Skip()

    def _on_version_change(self, event):
        """Update global path label when KiCad version changes."""
        config = load_config()
        if not config.get("global_lib_dir", "") and not self._global_lib_dir_override:
            new_dir = get_global_lib_dir(self._get_kicad_version())
            self._global_lib_dir = new_dir
            self._set_global_path(new_dir)
        event.Skip()

    def _on_global_browse(self, event):
        """Open a directory picker to choose a custom global library directory."""
        dlg = wx.DirDialog(self, "Choose global library directory", style=wx.DD_DEFAULT_STYLE)
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            config = load_config()
            config["global_lib_dir"] = path
            save_config(config)
            self._global_lib_dir = path
            self._global_lib_dir_override = ""
            self._set_global_path(path)
        dlg.Destroy()

    def _on_global_reset(self, event):
        """Clear the custom global library directory and revert to default."""
        config = load_config()
        config["global_lib_dir"] = ""
        save_config(config)
        self._global_lib_dir_override = ""
        default_dir = get_global_lib_dir(self._get_kicad_version())
        self._global_lib_dir = default_dir
        self._set_global_path(default_dir)

    def _log(self, msg: str):
        self.status_text.AppendText(msg + "\n")
        wx.Yield()

    def _handle_ssl_cert_error(self):
        """Show a one-time SSL warning and enable unverified HTTPS."""
        if not self._ssl_warning_shown:
            self._ssl_warning_shown = True
            wx.CallAfter(
                wx.MessageBox,
                "TLS certificate verification failed.\n\n"
                "A proxy or firewall may be intercepting HTTPS traffic. "
                "The session will continue without certificate verification.\n\n"
                "Consider downloading the latest version of this plugin which "
                "may include updated CA certificates.",
                "TLS Certificate Warning",
                wx.OK | wx.ICON_WARNING,
            )
            wx.CallAfter(
                self._log,
                "TLS certificate verification disabled for this session.",
            )
        _api_module.allow_unverified_ssl()

    def _show_category_list(self, matches):
        """Position and show the category suggestion popup below the search input."""
        self._category_popup.Set(matches)
        # Position in screen coordinates (PopupWindow uses screen coords)
        screen_pos = self.search_input.ClientToScreen(wx.Point(0, 0))
        sz = self.search_input.GetSize()
        height = min(len(matches), 10) * self._category_popup.GetCharHeight() + 20
        self._category_popup.SetPosition(wx.Point(screen_pos.x, screen_pos.y + sz.height))
        self._category_popup.SetSize(sz.width, height)
        self._category_popup.Popup()

    def _on_search_text_changed(self, event):
        """Show category suggestions as user types."""
        text = self.search_input.GetValue().strip().lower()
        if len(text) < 2:
            self._category_popup.Dismiss()
            return
        pattern = re.compile(r"\b" + re.escape(text), re.IGNORECASE)
        matches = [c for c in CATEGORIES if pattern.search(c)]
        if matches and len(matches) <= 20:
            if len(matches) == 1 and matches[0].lower() == text:
                self._category_popup.Dismiss()
            else:
                self._show_category_list(matches)
        else:
            self._category_popup.Dismiss()

    def _on_category_selected(self, event):
        """Handle category selection from suggestions popup."""
        sel = self._category_popup.GetSelection()
        if sel != wx.NOT_FOUND:
            self.search_input.SetValue(self._category_popup.GetString(sel))
            self._category_popup.Dismiss()
            self.search_input.SetInsertionPointEnd()

    def _on_search(self, event):
        self._category_popup.Dismiss()
        keyword = self.search_input.GetValue().strip()
        if not keyword:
            return

        self.search_btn.Disable()
        self.results_list.DeleteAllItems()
        self._search_results = []
        self._raw_search_results = []
        self.package_choice.Set(["All"])
        self.package_choice.SetSelection(0)
        self.results_count_label.SetLabel("")
        self.status_text.Clear()
        self._log(f'Searching for "{keyword}"...')

        self._search_request_id += 1
        request_id = self._search_request_id
        self._start_search_pulse()
        threading.Thread(
            target=self._fetch_search_results,
            args=(keyword, request_id),
            daemon=True,
        ).start()

    def _start_search_pulse(self):
        """Start animating dots on the search button."""
        self._pulse_phase = 0
        if not hasattr(self, "_pulse_timer"):
            self._pulse_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_pulse_tick, self._pulse_timer)
        self._pulse_timer.Start(300)
        self.search_btn.SetLabel("\u00b7")

    def _on_pulse_tick(self, event):
        """Cycle the search button through animated dots."""
        self._pulse_phase = (self._pulse_phase + 1) % 3
        self.search_btn.SetLabel("\u00b7" * (self._pulse_phase + 1))

    def _stop_search_pulse(self):
        """Stop pulsing and restore the search button."""
        if hasattr(self, "_pulse_timer"):
            self._pulse_timer.Stop()
        self.search_btn.SetLabel("Search")
        self.search_btn.Enable()

    def _fetch_search_results(self, keyword, request_id):
        """Background thread: fetch search results from API."""
        try:
            try:
                result = search_components(keyword, page_size=500)
            except SSLCertError:
                self._handle_ssl_cert_error()
                result = search_components(keyword, page_size=500)
            wx.CallAfter(self._on_search_complete, result, request_id)
        except APIError as e:
            wx.CallAfter(self._on_search_error, f"Search error: {e}", request_id)
        except Exception as e:
            wx.CallAfter(self._on_search_error, f"Unexpected error: {type(e).__name__}: {e}", request_id)

    def _on_search_complete(self, result, request_id):
        """Handle search results on the main thread."""
        if request_id != self._search_request_id:
            return
        self._stop_search_pulse()

        results = result["results"]
        results.sort(key=lambda r: r["stock"] or 0, reverse=True)

        self._raw_search_results = results
        self._populate_package_choices()
        self._sort_col = 3  # sorted by stock
        self._sort_ascending = False
        self._apply_filters()
        self._log(f"  {result['total']} total results, showing {len(self._search_results)}")
        self._refresh_imported_ids()
        self._update_col_headers()
        self._repopulate_results()

    def _on_search_error(self, msg, request_id):
        """Handle search error on the main thread."""
        if request_id != self._search_request_id:
            return
        self._stop_search_pulse()
        self._log(msg)

    def _on_col_click(self, event):
        """Sort results by clicked column."""
        col = event.GetColumn()
        # Toggle direction if same column clicked again
        if col == self._sort_col:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_col = col
            # Default descending for numeric columns, ascending for text
            self._sort_ascending = col not in (2, 3)

        # Map column index to sort key
        key_map = {
            0: lambda r: r.get("lcsc", ""),
            1: lambda r: r.get("type", ""),
            2: lambda r: r.get("price") or 0,
            3: lambda r: r.get("stock") or 0,
            4: lambda r: r.get("model", "").lower(),
            5: lambda r: r.get("package", "").lower(),
            6: lambda r: r.get("description", "").lower(),
        }
        key_fn = key_map.get(col)
        if key_fn:
            self._search_results.sort(key=key_fn, reverse=not self._sort_ascending)
            self._update_col_headers()
            self._repopulate_results()

    _col_names = ["LCSC", "Type", "Price", "Stock", "Part", "Package", "Description"]

    def _update_col_headers(self):
        """Update column headers with sort indicator."""
        for i, name in enumerate(self._col_names):
            if i == self._sort_col:
                arrow = " \u25b2" if self._sort_ascending else " \u25bc"
                label = name + arrow
            else:
                label = name
            col = self.results_list.GetColumn(i)
            col.SetText(label)
            self.results_list.SetColumn(i, col)

    def _refresh_imported_ids(self):
        """Scan the symbol library for the currently selected destination."""
        import re

        self._imported_ids = set()
        lib_name = self._lib_name
        if self.dest_global.GetValue():
            lib_dir = self._global_lib_dir
        else:
            lib_dir = self._get_project_dir()
        if not lib_dir:
            return
        p = os.path.join(lib_dir, f"{lib_name}.kicad_sym")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    for match in re.finditer(r'\(property "LCSC" "(C\d+)"', f.read()):
                        self._imported_ids.add(match.group(1))
            except Exception:
                pass

    def _get_min_stock(self) -> int:
        """Return the minimum stock threshold from the dropdown."""
        idx = self.min_stock_choice.GetSelection()
        if idx == wx.NOT_FOUND:
            return 0
        return self._min_stock_choices[idx]

    def _get_type_filter(self) -> str:
        """Return the selected type filter value."""
        if self.type_basic.GetValue():
            return "Basic"
        elif self.type_extended.GetValue():
            return "Extended"
        return ""

    def _populate_package_choices(self):
        """Populate the package dropdown from current raw results."""
        packages = sorted({r.get("package", "") for r in self._raw_search_results if r.get("package")})
        self.package_choice.Set(["All"] + packages)
        self.package_choice.SetSelection(0)

    def _get_package_filter(self) -> str:
        """Return the selected package filter value."""
        idx = self.package_choice.GetSelection()
        if idx <= 0:  # "All" or nothing selected
            return ""
        return self.package_choice.GetString(idx)

    def _apply_filters(self):
        """Apply type, stock, and package filters to _raw_search_results."""
        filtered = filter_by_type(self._raw_search_results, self._get_type_filter())
        filtered = filter_by_min_stock(filtered, self._get_min_stock())
        pkg = self._get_package_filter()
        if pkg:
            filtered = [r for r in filtered if r.get("package") == pkg]
        self._search_results = filtered

    def _on_filter_change(self, event):
        """Re-filter and repopulate results when any filter changes."""
        if not self._raw_search_results:
            return
        self._apply_filters()
        self._repopulate_results()

    _on_min_stock_change = _on_filter_change
    _on_type_change = _on_filter_change

    def _on_dest_change(self, event):
        """Persist destination choice and refresh checkmarks."""
        self._persist_destination()
        if self._search_results:
            self._refresh_imported_ids()
            self._repopulate_results()
        event.Skip()

    def _repopulate_results(self):
        """Repopulate the list control from _search_results."""
        self.results_list.DeleteAllItems()
        for i, r in enumerate(self._search_results):
            lcsc = r["lcsc"]
            prefix = "\u2713 " if lcsc in self._imported_ids else ""
            self.results_list.InsertItem(i, prefix + lcsc)
            self.results_list.SetItem(i, 1, r["type"])
            price_str = f"${r['price']:.4f}" if r["price"] else "N/A"
            self.results_list.SetItem(i, 2, price_str)
            stock_str = f"{r['stock']:,}" if r["stock"] else "N/A"
            self.results_list.SetItem(i, 3, stock_str)
            self.results_list.SetItem(i, 4, r["model"])
            self.results_list.SetItem(i, 5, r.get("package", ""))
            self.results_list.SetItem(i, 6, r.get("description", ""))
        self._update_results_count()

    def _update_results_count(self):
        """Update the results count label."""
        shown = len(self._search_results)
        total = len(self._raw_search_results)
        if total == 0:
            self.results_count_label.SetLabel("")
        elif shown == total:
            self.results_count_label.SetLabel(f"{total} {'result' if total == 1 else 'results'}")
        else:
            self.results_count_label.SetLabel(f"{shown} of {total}")

    def _on_result_select(self, event):
        """Select a search result to populate the part number and show details."""
        idx = event.GetIndex()
        if idx < 0 or idx >= len(self._search_results):
            return
        r = self._search_results[idx]
        self.part_input.SetValue(r["lcsc"])

        # Populate detail fields
        self.detail_lcsc.SetLabel(f"{r['lcsc']}  ({r['type']})")
        self.detail_part.SetLabel(r["model"])
        self.detail_brand.SetLabel(r["brand"])
        self.detail_package.SetLabel(r["package"])
        price_str = f"${r['price']:.4f}" if r["price"] else "N/A"
        self.detail_price.SetLabel(price_str)
        stock_str = f"{r['stock']:,}" if r["stock"] else "N/A"
        self.detail_stock.SetLabel(stock_str)
        self.detail_desc.SetValue(r["description"])

        self._datasheet_url = r.get("datasheet", "")
        self.detail_datasheet_btn.Enable(bool(self._datasheet_url))

        self._lcsc_page_url = r.get("url", "")
        self.detail_lcsc_btn.Enable(bool(self._lcsc_page_url))

        self.detail_import_btn.Enable()

        # Fetch image in background
        lcsc_url = r.get("url", "")
        self._image_request_id += 1
        request_id = self._image_request_id
        if lcsc_url:
            self._show_skeleton()
            threading.Thread(target=self._fetch_image, args=(lcsc_url, request_id), daemon=True).start()
        else:
            self._stop_skeleton()
            self._show_no_image()

        self.Layout()

    def _show_skeleton(self):
        """Show an animated skeleton placeholder while image loads."""
        self._skeleton_phase = 0
        if not hasattr(self, "_skeleton_timer"):
            self._skeleton_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_skeleton_tick, self._skeleton_timer)
        self._skeleton_timer.Start(30)
        self._draw_skeleton_frame()

    def _stop_skeleton(self):
        """Stop skeleton animation."""
        if hasattr(self, "_skeleton_timer"):
            self._skeleton_timer.Stop()

    def _show_no_image(self):
        """Show a subtle 'no image' placeholder."""
        bmp = wx.Bitmap(100, 100)
        dc = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(245, 245, 245)))
        dc.Clear()
        # Draw a subtle image icon (rectangle with mountain/sun)
        dc.SetPen(wx.Pen(wx.Colour(200, 200, 200), 1))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRoundedRectangle(25, 30, 50, 40, 4)
        # Mountain shape
        dc.SetPen(wx.Pen(wx.Colour(200, 200, 200), 1))
        dc.DrawLine(32, 62, 50, 48)
        dc.DrawLine(50, 48, 58, 55)
        dc.DrawLine(58, 55, 68, 45)
        # Sun circle
        dc.DrawCircle(60, 40, 5)
        dc.SelectObject(wx.NullBitmap)
        self.detail_image.SetBitmap(bmp)

    def _on_skeleton_tick(self, event):
        """Advance skeleton animation."""
        self._skeleton_phase = (self._skeleton_phase + 3) % 200
        self._draw_skeleton_frame()

    def _draw_skeleton_frame(self):
        """Draw one frame of the skeleton shimmer over a rounded rect."""
        import math

        bmp = wx.Bitmap(100, 100)
        dc = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(240, 240, 240)))
        dc.Clear()

        # Draw the base rounded rectangle
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(wx.Colour(225, 225, 225)))
        dc.DrawRoundedRectangle(4, 4, 92, 92, 6)

        # Shimmer: a soft gradient band sweeping left to right
        phase = self._skeleton_phase
        band_center = phase - 50  # range: -50 to 150
        band_width = 60

        for x in range(4, 96):
            dist = abs(x - band_center)
            if dist < band_width // 2:
                # Smooth falloff using cosine
                t = dist / (band_width / 2.0)
                alpha = int(25 * (1 + math.cos(t * math.pi)) / 2)
                if alpha > 0:
                    c = min(255, 225 + alpha)
                    dc.SetPen(wx.Pen(wx.Colour(c, c, c), 1))
                    dc.DrawLine(x, 4, x, 96)

        dc.SelectObject(wx.NullBitmap)
        self.detail_image.SetBitmap(bmp)

    def _on_image_click(self, event):
        """Open gallery view for the current selection."""
        if not self._search_results:
            return
        # Find current selection index
        sel = self.results_list.GetFirstSelected()
        if sel < 0:
            sel = 0
        self._gallery_index = sel
        self._enter_gallery()

    def _enter_gallery(self):
        """Switch to gallery view."""
        self._main_panel.Hide()
        self._gallery_panel.Show()
        self._update_gallery()
        self._root_sizer.Layout()

    def _exit_gallery(self):
        """Switch back to main view, selecting the current gallery item."""
        self._stop_gallery_skeleton()
        self._gallery_panel.Hide()
        self._main_panel.Show()
        # Select the item we were viewing in the gallery
        idx = self._gallery_index
        if 0 <= idx < self.results_list.GetItemCount():
            self.results_list.Select(idx)
            self.results_list.EnsureVisible(idx)
        self._root_sizer.Layout()

    def _update_gallery(self):
        """Update the gallery for the current index."""
        if not self._search_results:
            return
        idx = self._gallery_index
        r = self._search_results[idx]

        # Update info
        price_str = f"${r['price']:.4f}" if r["price"] else "N/A"
        stock_str = f"{r['stock']:,}" if r["stock"] else "N/A"
        info = (
            f"{r['lcsc']}  |  {r['model']}  |  {r['brand']}  |  {r['package']}  |  {price_str}  |  Stock: {stock_str}"
        )
        self._gallery_info.SetLabel(info)
        self._gallery_desc.SetLabel(r.get("description", ""))
        self._gallery_desc.Wrap(self.GetSize().width - 30)

        # Update nav buttons
        self._gallery_prev.Enable(idx > 0)
        self._gallery_next.Enable(idx < len(self._search_results) - 1)

        # Show skeleton while loading
        self._show_gallery_skeleton()

        # Fetch image
        lcsc_url = r.get("url", "")
        self._gallery_request_id += 1
        request_id = self._gallery_request_id
        if lcsc_url:
            threading.Thread(target=self._fetch_gallery_image, args=(lcsc_url, request_id), daemon=True).start()
        else:
            self._stop_gallery_skeleton()
            self._show_gallery_no_image()

    def _show_gallery_skeleton(self):
        """Show an animated skeleton placeholder in gallery."""
        self._gallery_skeleton_phase = 0
        if not hasattr(self, "_gallery_skeleton_timer"):
            self._gallery_skeleton_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_gallery_skeleton_tick, self._gallery_skeleton_timer)
        self._gallery_skeleton_timer.Start(30)
        self._draw_gallery_skeleton_frame()

    def _stop_gallery_skeleton(self):
        """Stop gallery skeleton animation."""
        if hasattr(self, "_gallery_skeleton_timer"):
            self._gallery_skeleton_timer.Stop()

    def _on_gallery_skeleton_tick(self, event):
        """Advance gallery skeleton animation."""
        self._gallery_skeleton_phase = (self._gallery_skeleton_phase + 3) % 200
        self._draw_gallery_skeleton_frame()

    def _draw_gallery_skeleton_frame(self):
        """Draw one frame of the gallery skeleton shimmer."""
        import math

        size = self._get_gallery_image_size()
        bmp = wx.Bitmap(size, size)
        dc = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(240, 240, 240)))
        dc.Clear()

        pad = 10
        inner = size - 2 * pad
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(wx.Colour(225, 225, 225)))
        dc.DrawRoundedRectangle(pad, pad, inner, inner, 8)

        # Shimmer band sweeping left to right (scaled to image size)
        phase = self._gallery_skeleton_phase
        band_width = max(80, inner // 3)
        band_center = int(phase / 200.0 * (inner + band_width)) - band_width // 2 + pad

        for x in range(pad, pad + inner):
            dist = abs(x - band_center)
            if dist < band_width // 2:
                t = dist / (band_width / 2.0)
                alpha = int(25 * (1 + math.cos(t * math.pi)) / 2)
                if alpha > 0:
                    c = min(255, 225 + alpha)
                    dc.SetPen(wx.Pen(wx.Colour(c, c, c), 1))
                    dc.DrawLine(x, pad, x, pad + inner)

        dc.SelectObject(wx.NullBitmap)
        self._gallery_image.SetBitmap(bmp)
        self._gallery_panel.Layout()

    def _show_gallery_no_image(self):
        """Show no-image placeholder in gallery."""
        size = self._get_gallery_image_size()
        bmp = wx.Bitmap(size, size)
        dc = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(245, 245, 245)))
        dc.Clear()
        dc.SetPen(wx.Pen(wx.Colour(200, 200, 200), 2))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        cx, cy = size // 2, size // 2
        dc.DrawRoundedRectangle(cx - 30, cy - 20, 60, 40, 4)
        dc.DrawLine(cx - 20, cy + 12, cx, cy - 5)
        dc.DrawLine(cx, cy - 5, cx + 8, cy + 5)
        dc.DrawLine(cx + 8, cy + 5, cx + 20, cy - 10)
        dc.DrawCircle(cx + 12, cy - 12, 6)
        dc.SelectObject(wx.NullBitmap)
        self._gallery_image.SetBitmap(bmp)
        self._gallery_panel.Layout()

    def _get_gallery_image_size(self):
        """Get the max square image size for the gallery."""
        w, h = self.GetClientSize()
        return max(min(w - 100, h - 120), 100)

    def _fetch_gallery_image(self, lcsc_url, request_id):
        """Fetch full-size image for gallery."""
        try:
            try:
                img_data = fetch_product_image(lcsc_url)
            except SSLCertError:
                self._handle_ssl_cert_error()
                img_data = fetch_product_image(lcsc_url)
        except Exception:
            img_data = None
        if self._gallery_request_id == request_id:
            wx.CallAfter(self._set_gallery_image, img_data, request_id)

    def _set_gallery_image(self, img_data, request_id):
        """Set gallery image on main thread."""
        if self._gallery_request_id != request_id:
            return
        self._stop_gallery_skeleton()
        if not img_data:
            self._show_gallery_no_image()
            return
        try:
            img = wx.Image(io.BytesIO(img_data), type=wx.BITMAP_TYPE_JPEG)
            if not img.IsOk():
                img = wx.Image(io.BytesIO(img_data), type=wx.BITMAP_TYPE_PNG)
            if img.IsOk():
                size = self._get_gallery_image_size()
                w, h = img.GetWidth(), img.GetHeight()
                scale = min(size / w, size / h)
                img = img.Scale(int(w * scale), int(h * scale), wx.IMAGE_QUALITY_HIGH)
                self._gallery_image.SetBitmap(wx.Bitmap(img))
                self._gallery_panel.Layout()
            else:
                self._show_gallery_no_image()
        except Exception:
            self._show_gallery_no_image()

    def _on_gallery_prev(self, event):
        if self._gallery_index > 0:
            self._gallery_index -= 1
            self._update_gallery()

    def _on_gallery_next(self, event):
        if self._gallery_index < len(self._search_results) - 1:
            self._gallery_index += 1
            self._update_gallery()

    def _on_gallery_close(self, event):
        self._exit_gallery()

    def _on_key(self, event):
        if self._gallery_panel.IsShown():
            key = event.GetKeyCode()
            if key == wx.WXK_ESCAPE:
                self._exit_gallery()
                return
            elif key == wx.WXK_LEFT:
                self._on_gallery_prev(None)
                return
            elif key == wx.WXK_RIGHT:
                self._on_gallery_next(None)
                return
        event.Skip()

    def _on_datasheet(self, event):
        """Open datasheet URL in browser."""
        if self._datasheet_url:
            webbrowser.open(self._datasheet_url)

    def _on_lcsc_page(self, event):
        """Open LCSC product page in browser."""
        if self._lcsc_page_url:
            webbrowser.open(self._lcsc_page_url)

    def _fetch_image(self, lcsc_url, request_id):
        """Fetch product image in background thread."""
        try:
            try:
                img_data = fetch_product_image(lcsc_url)
            except SSLCertError:
                self._handle_ssl_cert_error()
                img_data = fetch_product_image(lcsc_url)
        except Exception:
            img_data = None
        if self._image_request_id == request_id:
            wx.CallAfter(self._set_image, img_data, request_id)

    def _set_image(self, img_data, request_id):
        """Set the detail image from raw bytes (called on main thread)."""
        if self._image_request_id != request_id:
            return  # User selected a different item
        self._stop_skeleton()
        if not img_data:
            self._full_image_data = None
            self._show_no_image()
            self.Layout()
            return
        try:
            stream = io.BytesIO(img_data)
            img = wx.Image(stream, type=wx.BITMAP_TYPE_JPEG)
            if not img.IsOk():
                img = wx.Image(io.BytesIO(img_data), type=wx.BITMAP_TYPE_PNG)
            if img.IsOk():
                self._full_image_data = img_data
                thumb = img.Scale(100, 100, wx.IMAGE_QUALITY_HIGH)
                self.detail_image.SetBitmap(wx.Bitmap(thumb))
            else:
                self._full_image_data = None
                self._show_no_image()
            self.Layout()
        except Exception:
            self._full_image_data = None
            self._show_no_image()
            self.Layout()

    def _on_import(self, event):
        raw_id = self.part_input.GetValue().strip()
        if not raw_id:
            self._log("Error: Enter an LCSC part number or double-click a search result")
            return

        try:
            lcsc_id = validate_lcsc_id(raw_id)
        except ValueError as e:
            self._log(f"Error: {e}")
            return

        use_global = self.dest_global.GetValue()
        if use_global:
            lib_dir = self._global_lib_dir
        else:
            lib_dir = self._get_project_dir()
            if not lib_dir:
                self._log("Error: No board file open. Use Global destination or open a board.")
                return

        overwrite = self.overwrite_cb.GetValue()
        self.import_btn.Disable()
        self.status_text.Clear()

        try:
            try:
                self._do_import(lcsc_id, lib_dir, overwrite, use_global)
            except SSLCertError:
                self._handle_ssl_cert_error()
                self._do_import(lcsc_id, lib_dir, overwrite, use_global)
            self._persist_destination()
        except APIError as e:
            self._log(f"API Error: {e}")
        except Exception as e:
            self._log(f"Error: {e}")
            self._log(traceback.format_exc())
        finally:
            self.import_btn.Enable()

    def _get_kicad_version(self) -> int:
        """Return the selected KiCad version from the dropdown."""
        idx = self.version_choice.GetSelection()
        return int(self._version_labels[idx])

    def _do_import(self, lcsc_id: str, lib_dir: str, overwrite: bool, use_global: bool):
        lib_name = self._lib_name

        result = import_component(
            lcsc_id,
            lib_dir,
            lib_name,
            overwrite=overwrite,
            use_global=use_global,
            log=self._log,
            kicad_version=self._get_kicad_version(),
        )

        title = result["title"]
        name = result["name"]
        self._log(f"\nDone! '{title}' imported as {lib_name}:{name}")
        self._refresh_imported_ids()
        self._repopulate_results()
