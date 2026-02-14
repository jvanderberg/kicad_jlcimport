"""Main TUI application for JLCImport."""

from __future__ import annotations

import os
import re
import threading
import traceback
import webbrowser

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Container, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    RichLog,
    Select,
    Static,
)
from textual_image.widget import HalfcellImage

from kicad_jlcimport.categories import CATEGORIES
from kicad_jlcimport.easyeda import api
from kicad_jlcimport.easyeda.api import (
    APIError,
    SSLCertError,
    fetch_product_image,
    filter_by_min_stock,
    filter_by_type,
    search_components,
    validate_lcsc_id,
)
from kicad_jlcimport.importer import import_component
from kicad_jlcimport.kicad.library import (
    get_global_lib_dir,
    load_config,
    save_config,
)
from kicad_jlcimport.kicad.version import DEFAULT_KICAD_VERSION, SUPPORTED_VERSIONS

from .gallery import GalleryScreen
from .helpers import TIImage, make_no_image, make_skeleton_frame, pil_from_bytes


class SSLWarningScreen(Screen):
    """Modal warning shown when TLS certificate verification fails."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Dismiss"),
    ]

    CSS = """
    SSLWarningScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #ssl-dialog {
        width: 60;
        height: auto;
        max-height: 16;
        background: #1a1a1a;
        border: solid #ff6600;
        padding: 1 2;
    }
    #ssl-title {
        text-style: bold;
        color: #ff6600;
        width: 100%;
        content-align: center middle;
    }
    #ssl-message {
        color: #cccccc;
        margin: 1 0;
    }
    #ssl-ok {
        margin-top: 1;
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="ssl-dialog"):
            yield Static("!! TLS Certificate Warning", id="ssl-title")
            yield Static(
                "Certificate verification failed. A proxy or firewall may be "
                "intercepting HTTPS traffic.\n\n"
                "The session will continue without certificate verification. "
                "Consider downloading the latest version of this plugin which "
                "may include updated CA certificates.",
                id="ssl-message",
            )
            with Center():
                yield Button("OK", id="ssl-ok", variant="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ssl-ok":
            self.dismiss(True)

    def action_dismiss_screen(self) -> None:
        self.dismiss(True)


class PathInputScreen(Screen):
    """Modal screen for entering a custom global library directory path."""

    BINDINGS = [
        Binding("escape", "dismiss_screen", "Cancel"),
    ]

    CSS = """
    PathInputScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }
    #path-dialog {
        width: 70;
        height: auto;
        max-height: 10;
        background: #1a1a1a;
        border: solid #333333;
        padding: 1 2;
    }
    #path-title {
        text-style: bold;
        color: #33ff33;
        width: 100%;
    }
    #path-input {
        margin: 1 0;
    }
    #path-error {
        color: #ff4444;
        height: auto;
    }
    #path-buttons {
        height: 1;
        margin-top: 1;
    }
    #path-buttons Button { margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="path-dialog"):
            yield Static("Global library directory", id="path-title")
            yield Input(placeholder="/path/to/libraries", id="path-input")
            yield Static("", id="path-error")
            with Horizontal(id="path-buttons"):
                yield Button("OK", id="path-ok", variant="success")
                yield Button("Cancel", id="path-cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "path-input":
            self._try_accept()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "path-ok":
            self._try_accept()
        elif event.button.id == "path-cancel":
            self.dismiss("")

    def action_dismiss_screen(self) -> None:
        self.dismiss("")

    def _try_accept(self):
        path = self.query_one("#path-input", Input).value.strip()
        if not path:
            self.dismiss("")
            return
        if not os.path.isdir(path):
            self.query_one("#path-error", Static).update("Directory does not exist")
            return
        self.dismiss(path)


class JLCImportTUI(App):
    """TUI application for JLCImport - search and import JLCPCB components."""

    TITLE = "JLCImport"
    SUB_TITLE = ""

    CSS = """
    Screen {
        background: #0a0a0a;
        layers: default overlay;
    }

    /* Compact all widgets globally */
    Button {
        height: 1;
        min-height: 1;
        border: none;
        padding: 0 1;
        background: #1a1a1a;
        color: #33ff33;
    }
    Button:hover { background: #2a2a2a; }
    Button:focus { background: #2a2a2a; text-style: bold; }
    Button.-primary { color: #33ff33; }
    Button.-success { color: #33ff33; text-style: bold; }
    Input {
        height: 1;
        border: none;
        padding: 0;
        background: #1a1a1a;
        color: #cccccc;
    }
    Input:focus { border: none; background: #222222; }
    Select {
        height: 1;
        border: none;
        padding: 0;
        background: #1a1a1a;
    }
    SelectCurrent {
        height: 1;
        border: none;
        padding: 0;
    }
    RadioButton {
        height: 1;
        padding: 0;
        border: none;
        background: transparent;
    }
    RadioSet {
        height: 1;
        layout: horizontal;
        background: transparent;
        border: none;
    }
    Checkbox {
        height: 1;
        border: none;
        padding: 0;
        background: transparent;
    }
    DataTable {
        background: #0a0a0a;
    }
    DataTable > .datatable--header { color: #33ff33; text-style: bold; background: #1a1a1a; }
    DataTable > .datatable--cursor { background: #1a3a1a; }
    Header { background: #1a1a1a; color: #33ff33; }
    Footer { background: #1a1a1a; }
    RichLog { background: #0a0a0a; }
    Label { color: #aaaaaa; }

    #main-container {
        width: 100%;
        height: 100%;
    }

    /* Search: single compact row */
    #search-section {
        height: auto;
        padding: 1 0 0 1;
    }
    #search-row {
        height: 1;
        width: 100%;
    }
    #search-input { width: 1fr; }
    #search-btn { margin-left: 1; }
    #category-suggestions {
        display: none;
        layer: overlay;
        dock: top;
        margin: 3 1 0 1;
        height: auto;
        max-height: 10;
        background: #1a1a1a;
        border: solid #333333;
        padding: 0;
    }
    #category-suggestions.visible { display: block; }
    #category-suggestions > .option-list--option-highlighted {
        background: #1a3a1a;
        color: #33ff33;
    }
    #filter-row {
        height: 1;
        width: 100%;
        margin-top: 1;
    }
    #type-filter RadioButton { width: auto; margin-right: 1; }
    #min-stock-select { width: 14; margin: 0 1; }
    #package-select { width: 18; margin: 0 1; }
    #results-count {
        height: 1;
        color: #666666;
        padding-left: 1;
    }

    /* Results */
    #results-section {
        height: 1fr;
        min-height: 6;
    }
    #results-table { height: 100%; overflow-x: hidden; }

    /* Detail: compact horizontal layout */
    #detail-section {
        height: auto;
        border-top: solid #333333;
        padding: 0;
    }
    #detail-content { height: auto; }
    #detail-image-wrap {
        width: 22;
        height: 10;
        margin-left: 1;
        margin-right: 1;
    }
    #detail-skeleton {
        dock: top;
        width: 22;
        height: 10;
        display: none;
    }
    #detail-image {
        width: 22;
        height: 10;
    }
    #detail-info { width: 1fr; height: 10; }
    .detail-row { height: 1; width: 100%; }
    .detail-left { width: 1fr; height: 1; }
    .detail-right { width: 1fr; height: 1; }
    #detail-desc { height: auto; max-height: 3; width: 100%; color: #666666; }
    #detail-buttons {
        dock: bottom;
        height: 1;
        margin-left: 1;
        margin-bottom: 1;
    }
    #detail-buttons Button { margin-right: 1; }

    /* Import section – 3 rows */
    #import-section {
        height: auto;
        border-top: solid #333333;
        padding: 0;
    }
    #import-row-1, #import-row-2, #import-row-3 {
        height: 1;
        width: 100%;
    }
    #dest-project { width: 1fr; }
    #dest-global { width: auto; max-width: 64; }
    #global-browse-btn { width: 3; min-width: 0; padding: 0 0; }
    #global-reset-btn { width: 3; min-width: 0; padding: 0 0; }
    #part-input { width: 16; }
    #overwrite-cb { margin: 0 1; width: auto; }
    #import-btn { margin-left: 1; }
    #lib-name-label { width: auto; margin: 0 1; }
    #lib-name-input { width: 16; margin-right: 2; }
    #kicad-version-label { width: auto; margin: 0 1; }
    #kicad-version-select { width: 10; margin-right: 1; }

    /* Status */
    #status-section {
        height: 6;
        min-height: 4;
        border-top: solid #333333;
    }
    #status-log { height: 100%; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+g", "gallery", "Gallery", show=True),
        Binding("ctrl+s", "focus_search", "Search", show=True),
        Binding("f5", "do_search", "Search", show=False),
    ]

    _MIN_STOCK_OPTIONS = [
        ("Any", 0),
        ("1+", 1),
        ("10+", 10),
        ("100+", 100),
        ("1K+", 1000),
        ("10K+", 10000),
        ("100K+", 100000),
    ]

    def __init__(self, project_dir: str = "", kicad_version: int | None = None, global_lib_dir: str = ""):
        super().__init__()
        self._project_dir = project_dir
        self._kicad_version = kicad_version or DEFAULT_KICAD_VERSION
        _config = load_config()
        self._lib_name = _config.get("lib_name", "JLCImport")
        self._use_global = _config.get("use_global", False)
        self._global_lib_dir_override = global_lib_dir
        if global_lib_dir:
            self._global_lib_dir = global_lib_dir
        else:
            try:
                self._global_lib_dir = get_global_lib_dir(self._kicad_version)
            except ValueError:
                config = load_config()
                config["global_lib_dir"] = ""
                save_config(config)
                self._global_lib_dir = get_global_lib_dir(self._kicad_version)
        self._search_results: list = []
        self._raw_search_results: list = []
        self._sort_col: int = -1
        self._sort_ascending: bool = True
        self._imported_ids: set = set()
        self._selected_index: int = -1
        self._image_request_id: int = 0
        self._skeleton_timer = None
        self._skeleton_phase: int = 0
        self._datasheet_url: str = ""
        self._lcsc_page_url: str = ""
        self._pulse_timer = None
        self._pulse_phase: int = 0
        self._ssl_warning_shown: bool = False
        self._toggling_dest: bool = False
        self._col_names = ["LCSC", "Type", "Price", "Stock", "Part", "Package", "Description"]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="main-container"):
            with Vertical(id="search-section"):
                with Horizontal(id="search-row"):
                    yield Input(
                        placeholder="Search JLCPCB parts...",
                        id="search-input",
                    )
                    yield Button("Search", id="search-btn", variant="primary")
                with Horizontal(id="filter-row"):
                    with RadioSet(id="type-filter"):
                        yield RadioButton("Both", value=True, id="type-both")
                        yield RadioButton("Basic", id="type-basic")
                        yield RadioButton("Extended", id="type-extended")
                    yield Select(
                        [(label, val) for label, val in self._MIN_STOCK_OPTIONS],
                        value=1,
                        id="min-stock-select",
                        allow_blank=False,
                    )
                    yield Select(
                        [("All", "")],
                        value="",
                        id="package-select",
                        allow_blank=False,
                    )

            yield Label("", id="results-count")

            with Container(id="results-section"):
                yield DataTable(id="results-table", cursor_type="row")

            with Vertical(id="detail-section"):
                with Horizontal(id="detail-content"):
                    with Container(id="detail-image-wrap"):
                        yield TIImage(id="detail-image")
                        yield HalfcellImage(id="detail-skeleton")
                    with Vertical(id="detail-info"):
                        with Horizontal(classes="detail-row"):
                            yield Label("", id="detail-part", classes="detail-left")
                            yield Label("", id="detail-lcsc", classes="detail-right")
                        with Horizontal(classes="detail-row"):
                            yield Label("", id="detail-brand", classes="detail-left")
                            yield Label("", id="detail-package", classes="detail-right")
                        with Horizontal(classes="detail-row"):
                            yield Label("", id="detail-price", classes="detail-left")
                            yield Label("", id="detail-stock", classes="detail-right")
                        yield Label("", id="detail-desc")
                        with Horizontal(id="detail-buttons"):
                            yield Button("Import", id="detail-import-btn", variant="success", disabled=True)
                            yield Button("Datasheet", id="detail-datasheet-btn", disabled=True)
                            yield Button("LCSC", id="detail-lcsc-btn", disabled=True)

            with Vertical(id="import-section"):
                with Horizontal(id="import-row-1"):
                    _select_global = self._use_global or not self._project_dir
                    yield RadioButton(
                        f"Proj [b]{self._project_dir or 'n/a'}[/b]",
                        value=not _select_global,
                        id="dest-project",
                    )
                    yield Input(placeholder="C427602", id="part-input")
                    yield Checkbox("Overwrite", id="overwrite-cb")
                    yield Button("Import", id="import-btn", variant="success")
                with Horizontal(id="import-row-2"):
                    yield RadioButton(
                        self._global_label(self._global_lib_dir),
                        value=_select_global,
                        id="dest-global",
                    )
                    yield Button("\u2026", id="global-browse-btn")
                    yield Button("\u2715", id="global-reset-btn")
                with Horizontal(id="import-row-3"):
                    yield Label("Lib", id="lib-name-label")
                    yield Input(value=self._lib_name, id="lib-name-input")
                    yield Label("KiCad", id="kicad-version-label")
                    yield Select(
                        [(f"v{v}", v) for v in sorted(SUPPORTED_VERSIONS)],
                        value=self._kicad_version,
                        id="kicad-version-select",
                        allow_blank=False,
                    )

            with Container(id="status-section"):
                yield RichLog(id="status-log", highlight=True, markup=True)

        yield OptionList(id="category-suggestions")
        yield Footer()

    def on_mount(self):
        """Set up the results table columns."""
        table = self.query_one("#results-table", DataTable)
        table.add_columns("LCSC", "Type", "Price", "Stock", "Part", "Package", "Description")
        if not self._project_dir:
            self.query_one("#dest-project", RadioButton).disabled = True
        self.query_one("#dest-global", RadioButton).tooltip = self._global_lib_dir
        self.query_one("#search-input", Input).focus()

    def _log(self, msg: str):
        """Write a message to the status log."""
        log = self.query_one("#status-log", RichLog)
        log.write(msg)

    def _handle_ssl_cert_error(self):
        """Handle an SSLCertError from a worker thread.

        On first call, pushes a modal warning screen and blocks the worker
        until the user dismisses it.  On subsequent calls, silently enables
        unverified SSL (the user has already been warned).
        """
        if not self._ssl_warning_shown:
            self._ssl_warning_shown = True
            event = threading.Event()

            def _on_dismiss(result):
                event.set()

            self.app.call_from_thread(self.push_screen, SSLWarningScreen(), _on_dismiss)
            event.wait()
            self.app.call_from_thread(
                self._log,
                "[yellow]TLS certificate verification disabled for this session.[/yellow]",
            )
        api.allow_unverified_ssl()

    # --- Search ---

    def on_key(self, event):
        """Hide suggestions on Escape or Tab."""
        if event.key == "escape" or event.key == "tab":
            self._hide_suggestions()

    def on_input_submitted(self, event: Input.Submitted):
        """Handle Enter key in inputs."""
        if event.input.id == "search-input":
            self._hide_suggestions()
            self._do_search()
        elif event.input.id == "lib-name-input":
            self._persist_lib_name()

    def on_input_changed(self, event: Input.Changed):
        """Show category suggestions as user types in search."""
        if event.input.id != "search-input":
            return
        text = event.value.strip().lower()
        suggestions = self.query_one("#category-suggestions", OptionList)
        if len(text) < 2:
            self._hide_suggestions()
            return
        # Match at word boundaries
        pattern = re.compile(r"\b" + re.escape(text), re.IGNORECASE)
        matches = [c for c in CATEGORIES if pattern.search(c)]
        if matches and len(matches) <= 20:
            if len(matches) == 1 and matches[0].lower() == text:
                self._hide_suggestions()
            else:
                suggestions.clear_options()
                for m in matches:
                    suggestions.add_option(m)
                suggestions.add_class("visible")
        else:
            self._hide_suggestions()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        """Append selected category to search input."""
        if event.option_list.id == "category-suggestions":
            search_input = self.query_one("#search-input", Input)
            current = search_input.value.strip()
            category = str(event.option.prompt)
            # Remove words that are part of the category match
            cat_lower = category.lower()
            remaining = [w for w in current.split() if w.lower() not in cat_lower.lower()]
            if remaining:
                new_value = " ".join(remaining) + " " + category
            else:
                new_value = category
            search_input.value = new_value + " "
            self._hide_suggestions()
            search_input.focus()
            self.set_timer(0.05, lambda: self._deselect_search(len(new_value) + 1))

    def _deselect_search(self, pos: int):
        """Move cursor to end without selection after a brief delay."""
        search_input = self.query_one("#search-input", Input)
        from textual.widgets._input import Selection

        search_input.selection = Selection(pos, pos)
        search_input.cursor_position = pos

    def _hide_suggestions(self):
        """Hide the category suggestions dropdown."""
        suggestions = self.query_one("#category-suggestions", OptionList)
        suggestions.remove_class("visible")

    def on_input_blurred(self, event: Input.Blurred):
        """Persist lib name when input loses focus."""
        if event.input.id == "lib-name-input":
            self._persist_lib_name()

    def _persist_lib_name(self):
        """Save the library name if it changed."""
        new_name = self.query_one("#lib-name-input", Input).value.strip()
        if new_name and new_name != self._lib_name:
            self._lib_name = new_name
            config = load_config()
            config["lib_name"] = new_name
            save_config(config)
        elif not new_name:
            self.query_one("#lib-name-input", Input).value = self._lib_name

    def _persist_destination(self, use_global: bool):
        """Save the destination choice to config."""
        config = load_config()
        config["use_global"] = use_global
        save_config(config)

    @staticmethod
    def _global_label(path: str, max_len: int = 50) -> str:
        """Return a truncated label for the global destination radio button."""
        if len(path) <= max_len:
            return f"Global [b]{path}[/b]"
        keep = max_len - 3  # room for ellipsis
        left = keep // 2
        right = keep - left
        truncated = path[:left] + "\u2026" + path[-right:]
        return f"Global [b]{truncated}[/b]"

    def _update_global_button(self, path: str) -> None:
        """Update the global radio button label and tooltip."""
        btn = self.query_one("#dest-global", RadioButton)
        btn.label = self._global_label(path)
        btn.tooltip = path

    def _on_global_browse_result(self, path: str):
        """Handle result from PathInputScreen."""
        if not path:
            return
        config = load_config()
        config["global_lib_dir"] = path
        save_config(config)
        self._global_lib_dir = path
        self._global_lib_dir_override = ""
        self._update_global_button(self._global_lib_dir)

    def _reset_global_dir(self):
        """Clear the custom global library directory and revert to default."""
        config = load_config()
        config["global_lib_dir"] = ""
        save_config(config)
        self._global_lib_dir_override = ""
        self._global_lib_dir = get_global_lib_dir(self._get_kicad_version())
        self._update_global_button(self._global_lib_dir)

    def on_button_pressed(self, event: Button.Pressed):
        """Handle button clicks."""
        self._hide_suggestions()
        button_id = event.button.id
        if button_id == "search-btn":
            self._do_search()
        elif button_id == "import-btn" or button_id == "detail-import-btn":
            self._do_import_action()
        elif button_id == "detail-datasheet-btn":
            if self._datasheet_url:
                webbrowser.open(self._datasheet_url)
        elif button_id == "detail-lcsc-btn":
            if self._lcsc_page_url:
                webbrowser.open(self._lcsc_page_url)
        elif button_id == "global-browse-btn":
            self.push_screen(PathInputScreen(), self._on_global_browse_result)
        elif button_id == "global-reset-btn":
            self._reset_global_dir()

    def on_radio_button_changed(self, event: RadioButton.Changed):
        """Manual mutual exclusion for standalone destination radio buttons."""
        if self._toggling_dest:
            return
        if event.radio_button.id not in ("dest-project", "dest-global"):
            return
        self._toggling_dest = True
        try:
            other_id = "dest-global" if event.radio_button.id == "dest-project" else "dest-project"
            other = self.query_one(f"#{other_id}", RadioButton)
            if event.value:
                other.value = False
            elif not other.value:
                event.radio_button.value = True
        finally:
            self._toggling_dest = False
        if event.value:
            self._persist_destination(event.radio_button.id == "dest-global")
            if self._search_results:
                self._refresh_imported_ids()
                self._repopulate_results()

    def action_focus_search(self):
        """Focus the search input."""
        self.query_one("#search-input", Input).focus()

    def action_do_search(self):
        self._do_search()

    def action_gallery(self):
        """Open gallery view."""
        if self._search_results:
            idx = max(0, self._selected_index)
            self.push_screen(GalleryScreen(self._search_results, idx), self._on_gallery_return)

    def on_click(self, event):
        """Handle clicks."""
        widget = event.widget
        # Hide suggestions when clicking outside them
        suggestions = self.query_one("#category-suggestions", OptionList)
        if widget is not suggestions:
            self._hide_suggestions()
        # Open gallery when thumbnail is clicked
        wrap = self.query_one("#detail-image-wrap")
        if widget is wrap or widget in wrap.query("*"):
            self.action_gallery()

    def _on_gallery_return(self, index: int):
        """Update table selection to match gallery position."""
        table = self.query_one("#results-table", DataTable)
        if 0 <= index < len(self._search_results):
            self._selected_index = -1  # Force detail refresh
            table.move_cursor(row=index)
            self._show_detail(index)

    def _start_search_pulse(self):
        """Animate dots on the search button."""
        self._pulse_phase = 0
        btn = self.query_one("#search-btn", Button)
        btn.label = "\u00b7"
        btn.disabled = True
        self._pulse_timer = self.set_interval(0.3, self._on_pulse_tick)

    def _on_pulse_tick(self):
        """Cycle through dot animation."""
        self._pulse_phase = (self._pulse_phase + 1) % 3
        self.query_one("#search-btn", Button).label = "\u00b7" * (self._pulse_phase + 1)

    def _stop_search_pulse(self):
        """Stop animation and restore button."""
        if self._pulse_timer:
            self._pulse_timer.stop()
            self._pulse_timer = None
        btn = self.query_one("#search-btn", Button)
        btn.label = "Search"
        btn.disabled = False

    def _start_skeleton(self):
        """Start skeleton overlay animation, clear main image."""
        self._stop_skeleton()
        self._skeleton_phase = 0
        self.query_one("#detail-image", TIImage).image = None
        skeleton = self.query_one("#detail-skeleton", HalfcellImage)
        skeleton.image = make_skeleton_frame(100, 100, 0)
        skeleton.display = True
        self._skeleton_timer = self.set_interval(1 / 15, self._on_skeleton_tick)

    def _on_skeleton_tick(self):
        """Advance skeleton shimmer."""
        if not self._skeleton_timer:
            return
        self._skeleton_phase = (self._skeleton_phase + 5) % 100
        self.query_one("#detail-skeleton", HalfcellImage).image = make_skeleton_frame(100, 100, self._skeleton_phase)

    def _stop_skeleton(self):
        """Stop skeleton animation, hide overlay."""
        if self._skeleton_timer:
            self._skeleton_timer.stop()
            self._skeleton_timer = None
        self.query_one("#detail-skeleton", HalfcellImage).display = False

    @work(thread=True)
    def _do_search(self):
        """Perform the search in a background thread."""
        search_input = self.query_one("#search-input", Input)
        keyword = search_input.value.strip()
        if not keyword:
            return

        self.app.call_from_thread(self._log, f'Searching for "{keyword}"...')
        self.app.call_from_thread(self._start_search_pulse)

        try:
            try:
                result = search_components(keyword, page_size=500)
            except SSLCertError:
                self._handle_ssl_cert_error()
                result = search_components(keyword, page_size=500)

            results = result["results"]

            results.sort(key=lambda r: r["stock"] or 0, reverse=True)

            self._raw_search_results = results
            self._sort_col = 3  # sorted by stock
            self._sort_ascending = False
            self._selected_index = -1

            self.app.call_from_thread(self._populate_package_choices)
            self.app.call_from_thread(self._apply_filters)
            self.app.call_from_thread(
                self._log,
                f"  {result['total']} total results, showing {len(self._search_results)}",
            )
            self.app.call_from_thread(self._refresh_imported_ids)
            self.app.call_from_thread(self._update_sort_indicators)
            self.app.call_from_thread(self._repopulate_results)

        except APIError as e:
            self.app.call_from_thread(self._log, f"[red]Search error: {e}[/red]")
        except Exception as e:
            self.app.call_from_thread(self._log, f"[red]Error: {type(e).__name__}: {e}[/red]")
        finally:
            self.app.call_from_thread(self._stop_search_pulse)

    # --- Filtering ---

    def _get_type_filter(self) -> str:
        """Return the selected type filter value ('Basic', 'Extended', or '')."""
        type_filter = self.query_one("#type-filter", RadioSet)
        if type_filter.pressed_index == 1:
            return "Basic"
        elif type_filter.pressed_index == 2:
            return "Extended"
        return ""

    def _get_min_stock(self) -> int:
        """Return the minimum stock threshold from the dropdown."""
        select = self.query_one("#min-stock-select", Select)
        val = select.value
        return val if isinstance(val, int) else 0

    def _get_package_filter(self) -> str:
        """Return the selected package filter value."""
        select = self.query_one("#package-select", Select)
        val = select.value
        return val if isinstance(val, str) else ""

    def _populate_package_choices(self):
        """Populate the package dropdown from current raw results."""
        packages = sorted({r.get("package", "") for r in self._raw_search_results if r.get("package")})
        options = [("All", "")] + [(p, p) for p in packages]
        select = self.query_one("#package-select", Select)
        select.set_options(options)

    def _apply_filters(self):
        """Apply type, stock, and package filters to _raw_search_results."""
        filtered = filter_by_type(self._raw_search_results, self._get_type_filter())
        filtered = filter_by_min_stock(filtered, self._get_min_stock())
        pkg = self._get_package_filter()
        if pkg:
            filtered = [r for r in filtered if r.get("package") == pkg]
        self._search_results = filtered

    def on_select_changed(self, event: Select.Changed):
        """Re-filter when min-stock or package selection changes, update path on version change."""
        if event.select.id in ("min-stock-select", "package-select"):
            if self._raw_search_results:
                self._apply_filters()
                self._repopulate_results()
        elif event.select.id == "kicad-version-select":
            config = load_config()
            if not config.get("global_lib_dir", "") and not self._global_lib_dir_override:
                version = self._get_kicad_version()
                self._global_lib_dir = get_global_lib_dir(version)
                self._update_global_button(self._global_lib_dir)

    def on_radio_set_changed(self, event: RadioSet.Changed):
        """Re-filter when type filter changes."""
        if event.radio_set.id == "type-filter":
            if self._raw_search_results:
                self._apply_filters()
                self._repopulate_results()

    def _refresh_imported_ids(self):
        """Scan the symbol library for the currently selected destination."""
        import re as _re

        self._imported_ids = set()
        lib_name = self._lib_name
        use_global = self.query_one("#dest-global", RadioButton).value
        if use_global:
            lib_dir = self._global_lib_dir
        else:
            lib_dir = self._project_dir
        if not lib_dir:
            return
        p = os.path.join(lib_dir, f"{lib_name}.kicad_sym")
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    for match in _re.finditer(r'\(property "LCSC" "(C\d+)"', f.read()):
                        self._imported_ids.add(match.group(1))
        except (PermissionError, OSError):
            pass

    def _repopulate_results(self):
        """Repopulate the DataTable from search results."""
        table = self.query_one("#results-table", DataTable)
        table.clear()
        for r in self._search_results:
            lcsc = r["lcsc"]
            prefix = "\u2713 " if lcsc in self._imported_ids else ""
            price_str = f"${r['price']:.4f}" if r["price"] else "N/A"
            stock_str = f"{r['stock']:,}" if r["stock"] else "N/A"
            table.add_row(
                prefix + lcsc,
                r["type"],
                price_str,
                stock_str,
                r["model"],
                r.get("package", ""),
                r.get("description", ""),
            )
        self._update_results_count()

    def _update_results_count(self):
        """Update the results count label."""
        shown = len(self._search_results)
        total = len(self._raw_search_results)
        label = self.query_one("#results-count", Label)
        if total == 0:
            label.update("")
        elif shown == total:
            label.update(f"{total} {'result' if total == 1 else 'results'}")
        else:
            label.update(f"{shown} of {total} {'result' if total == 1 else 'results'}")

    # --- Sorting ---

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected):
        """Sort by clicked column."""
        col_idx = event.column_index
        if col_idx == self._sort_col:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_col = col_idx
            self._sort_ascending = col_idx not in (2, 3)

        key_map = {
            0: lambda r: r.get("lcsc", ""),
            1: lambda r: r.get("type", ""),
            2: lambda r: r.get("price") or 0,
            3: lambda r: r.get("stock") or 0,
            4: lambda r: r.get("model", "").lower(),
            5: lambda r: r.get("package", "").lower(),
            6: lambda r: r.get("description", "").lower(),
        }
        key_fn = key_map.get(col_idx)
        if key_fn:
            self._search_results.sort(key=key_fn, reverse=not self._sort_ascending)
            self._update_sort_indicators()
            self._repopulate_results()

    def _update_sort_indicators(self):
        """Update column headers with sort direction arrows."""
        table = self.query_one("#results-table", DataTable)
        col_keys = list(table.columns.keys())
        for i, key in enumerate(col_keys):
            name = self._col_names[i]
            if i == self._sort_col:
                arrow = "\u25b2" if self._sort_ascending else "\u25bc"
                table.columns[key].label = f"{name} {arrow}"
            else:
                table.columns[key].label = name

    # --- Selection ---

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        """Update detail when cursor moves."""
        self._show_detail(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        """Handle row selection in results table."""
        self._show_detail(event.cursor_row)

    def _show_detail(self, row_idx: int):
        """Update the detail panel for the given row."""
        if row_idx < 0 or row_idx >= len(self._search_results):
            return
        if row_idx == self._selected_index:
            return
        self._selected_index = row_idx
        r = self._search_results[row_idx]

        # Update part input
        self.query_one("#part-input", Input).value = r["lcsc"]

        # Update detail fields
        self.query_one("#detail-part", Label).update(f"Part [b]{r['model']}[/b]")
        self.query_one("#detail-lcsc", Label).update(f"LCSC [b]{r['lcsc']}[/b]  ({r['type']})")
        self.query_one("#detail-brand", Label).update(f"Brand [b]{r['brand']}[/b]")
        self.query_one("#detail-package", Label).update(f"Package [b]{r['package']}[/b]")
        price_str = f"${r['price']:.4f}" if r["price"] else "N/A"
        stock_str = f"{r['stock']:,}" if r["stock"] else "N/A"
        self.query_one("#detail-price", Label).update(f"Price [b]{price_str}[/b]")
        self.query_one("#detail-stock", Label).update(f"Stock [b]{stock_str}[/b]")
        self.query_one("#detail-desc", Label).update(r.get("description", ""))

        # URLs
        self._datasheet_url = r.get("datasheet", "")
        self._lcsc_page_url = r.get("url", "")
        self.query_one("#detail-datasheet-btn", Button).disabled = not self._datasheet_url
        self.query_one("#detail-lcsc-btn", Button).disabled = not self._lcsc_page_url
        self.query_one("#detail-import-btn", Button).disabled = False

        # Fetch image
        self._image_request_id += 1
        request_id = self._image_request_id
        lcsc_url = r.get("url", "")
        if lcsc_url:
            self._start_skeleton()
            self._fetch_detail_image(lcsc_url, request_id)
        else:
            self._stop_skeleton()
            self.query_one("#detail-image", TIImage).image = make_no_image(100, 100)

    @work(thread=True)
    def _fetch_detail_image(self, lcsc_url: str, request_id: int):
        """Fetch product image in background."""
        img_data = None
        try:
            try:
                img_data = fetch_product_image(lcsc_url)
            except SSLCertError:
                self._handle_ssl_cert_error()
                img_data = fetch_product_image(lcsc_url)
        except Exception:
            pass
        self.call_from_thread(self._set_detail_image, img_data, request_id)

    def _set_detail_image(self, img_data: bytes | None, request_id: int):
        """Set the detail image (called on main thread)."""
        if self._image_request_id != request_id:
            return
        self._stop_skeleton()
        img = pil_from_bytes(img_data)
        if img is None:
            img = make_no_image(100, 100)
        self.query_one("#detail-image", TIImage).image = img

    # --- Import ---

    def _do_import_action(self):
        """Start the import process."""
        self._persist_lib_name()
        part_input = self.query_one("#part-input", Input)
        raw_id = part_input.value.strip()
        if not raw_id:
            self._log("[red]Error: Enter an LCSC part number[/red]")
            return

        try:
            lcsc_id = validate_lcsc_id(raw_id)
        except ValueError as e:
            self._log(f"[red]Error: {e}[/red]")
            return

        use_global = self.query_one("#dest-global", RadioButton).value
        kicad_version = self._get_kicad_version()

        if use_global:
            lib_dir = self._global_lib_dir
        else:
            lib_dir = self._project_dir
            if not lib_dir:
                self._log("[red]Error: No project directory. Use Global destination.[/red]")
                return

        overwrite = self.query_one("#overwrite-cb", Checkbox).value

        self.query_one("#import-btn", Button).disabled = True
        self.query_one("#detail-import-btn", Button).disabled = True
        self._run_import(lcsc_id, lib_dir, overwrite, use_global, kicad_version)

    def _get_kicad_version(self) -> int:
        """Return the selected KiCad version from the dropdown."""
        select = self.query_one("#kicad-version-select", Select)
        val = select.value
        return val if isinstance(val, int) else DEFAULT_KICAD_VERSION

    @work(thread=True)
    def _run_import(self, lcsc_id: str, lib_dir: str, overwrite: bool, use_global: bool, kicad_version: int):
        """Run the import in a background thread."""
        try:
            try:
                self._do_import(lcsc_id, lib_dir, overwrite, use_global, kicad_version)
            except SSLCertError:
                self._handle_ssl_cert_error()
                self._do_import(lcsc_id, lib_dir, overwrite, use_global, kicad_version)
        except APIError as e:
            self.app.call_from_thread(self._log, f"[red]API Error: {e}[/red]")
        except Exception as e:
            self.app.call_from_thread(self._log, f"[red]Error: {e}[/red]")
            self.app.call_from_thread(self._log, traceback.format_exc())
        finally:
            self.app.call_from_thread(self.query_one("#import-btn", Button).__setattr__, "disabled", False)
            self.app.call_from_thread(self.query_one("#detail-import-btn", Button).__setattr__, "disabled", False)

    def _do_import(self, lcsc_id: str, lib_dir: str, overwrite: bool, use_global: bool, kicad_version: int):
        """Execute the import process."""
        lib_name = self._lib_name

        def log(msg):
            self.app.call_from_thread(self._log, msg)

        result = import_component(
            lcsc_id,
            lib_dir,
            lib_name,
            overwrite=overwrite,
            use_global=use_global,
            log=log,
            kicad_version=kicad_version,
        )

        title = result["title"]
        name = result["name"]
        log(f"\n[green bold]Done! '{title}' imported as {lib_name}:{name}[/green bold]")
        self.app.call_from_thread(self._persist_destination, use_global)
        self.app.call_from_thread(self._refresh_imported_ids)
        self.app.call_from_thread(self._repopulate_results)
