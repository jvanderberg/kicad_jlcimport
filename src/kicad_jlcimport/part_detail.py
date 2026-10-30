"""Part details shown beside a parts list: photo / footprint preview, key facts and links.

Used by the import tab and the production tab.  The image column flips
between the LCSC product photo and JLC's footprint drawing; both are fetched
in background threads and tagged with a request id so a slow answer for a
part the user has already left is dropped.
"""

import io
import math
import threading
import webbrowser
from typing import Any, Callable, Dict, Optional

import wx

from .easyeda.api import APIError, SSLCertError, fetch_component_uuids, fetch_product_image
from .gui.symbol_renderer import has_svg_support, render_svg_bitmap

IMAGE_SIZE = 160


def no_footprint_placeholder(size: int, svg_unsupported: bool) -> wx.Bitmap:
    """Create a placeholder bitmap for the footprint preview.

    When *svg_unsupported* is True, shows a message explaining the platform
    limitation.  Otherwise draws simple pad outlines as a generic icon.
    """
    bmp = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(248, 248, 248)))
    dc.Clear()

    if svg_unsupported:
        dc.SetTextForeground(wx.Colour(140, 140, 140))
        font = dc.GetFont()
        font.SetPointSize(max(8, size // 18))
        dc.SetFont(font)
        msg = "Footprint preview\nnot supported\non this platform"
        tw, th = dc.GetMultiLineTextExtent(msg)[:2]
        dc.DrawText(msg, (size - tw) // 2, (size - th) // 2)
    else:
        # Simple pad-outline icon
        dc.SetPen(wx.Pen(wx.Colour(200, 200, 200), max(1, size // 80)))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        cx, cy = size // 2, size // 2
        pw, ph = max(20, size // 8), max(12, size // 14)
        gap = max(16, size // 6)
        dc.DrawRoundedRectangle(cx - gap // 2 - pw, cy - gap // 2 - ph, pw, ph, 3)
        dc.DrawRoundedRectangle(cx - gap // 2 - pw, cy + gap // 2, pw, ph, 3)
        dc.DrawRoundedRectangle(cx + gap // 2, cy - gap // 2 - ph, pw, ph, 3)
        dc.DrawRoundedRectangle(cx + gap // 2, cy + gap // 2, pw, ph, 3)

    dc.SelectObject(wx.NullBitmap)
    return bmp


def no_easyeda_placeholder(size: int) -> wx.Bitmap:
    """Create a placeholder bitmap indicating no EasyEDA data is available."""
    bmp = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(248, 248, 248)))
    dc.Clear()
    dc.SetTextForeground(wx.Colour(160, 100, 100))
    font = dc.GetFont()
    font.SetPointSize(max(8, size // 16))
    dc.SetFont(font)
    msg = "No EasyEDA data\navailable for\nthis part"
    tw, th = dc.GetMultiLineTextExtent(msg)[:2]
    dc.DrawText(msg, (size - tw) // 2, (size - th) // 2)
    dc.SelectObject(wx.NullBitmap)
    return bmp


class PageIndicator(wx.Control):
    """Owner-drawn two-dot page indicator for switching between photo and symbol views."""

    DOT_RADIUS = 4
    DOT_GAP = 12

    def __init__(self, parent, on_page_change=None):
        super().__init__(
            parent,
            style=wx.BORDER_NONE,
            size=(2 * self.DOT_GAP + 2 * self.DOT_RADIUS, 2 * self.DOT_RADIUS + 4),
        )
        self._page = 0
        self._num_pages = 2
        self._on_page_change = on_page_change
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_click)

    def set_page(self, page: int):
        if page != self._page and 0 <= page < self._num_pages:
            self._page = page
            self.Refresh()

    def _dot_positions(self):
        w, h = self.GetClientSize()
        total = (self._num_pages - 1) * self.DOT_GAP
        start_x = (w - total) // 2
        cy = h // 2
        return [(start_x + i * self.DOT_GAP, cy) for i in range(self._num_pages)]

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        bg = self.GetParent().GetBackgroundColour()
        dc.SetBackground(wx.Brush(bg))
        dc.Clear()
        # Selected dot should contrast strongly with background:
        # dark dot on light bg, light dot on dark bg
        lum = (bg.Red() * 299 + bg.Green() * 587 + bg.Blue() * 114) // 1000
        if lum >= 128:
            active_colour = wx.Colour(80, 80, 80)
            inactive_colour = wx.Colour(200, 200, 200)
        else:
            active_colour = wx.Colour(200, 200, 200)
            inactive_colour = wx.Colour(80, 80, 80)
        dc.SetPen(wx.TRANSPARENT_PEN)
        for i, (cx, cy) in enumerate(self._dot_positions()):
            if i == self._page:
                dc.SetBrush(wx.Brush(active_colour))
            else:
                dc.SetBrush(wx.Brush(inactive_colour))
            dc.DrawCircle(cx, cy, self.DOT_RADIUS)

    def _on_click(self, event):
        x = event.GetX()
        positions = self._dot_positions()
        best = -1
        best_dist = float("inf")
        for i, (cx, _cy) in enumerate(positions):
            dist = abs(x - cx)
            if dist < best_dist:
                best_dist = dist
                best = i
        if best >= 0 and best != self._page:
            self._page = best
            self.Refresh()
            if self._on_page_change:
                self._on_page_change(best)


def no_image_bitmap(size: int = IMAGE_SIZE) -> wx.Bitmap:
    """A subtle 'no image' placeholder (a picture-frame icon)."""
    bmp = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(245, 245, 245)))
    dc.Clear()
    s = size / 160.0
    dc.SetPen(wx.Pen(wx.Colour(200, 200, 200), 1))
    dc.SetBrush(wx.TRANSPARENT_BRUSH)
    dc.DrawRoundedRectangle(int(55 * s), int(60 * s), int(50 * s), int(40 * s), 4)
    dc.DrawLine(int(62 * s), int(92 * s), int(80 * s), int(78 * s))
    dc.DrawLine(int(80 * s), int(78 * s), int(88 * s), int(85 * s))
    dc.DrawLine(int(88 * s), int(85 * s), int(98 * s), int(75 * s))
    dc.DrawCircle(int(90 * s), int(70 * s), max(2, int(5 * s)))
    dc.SelectObject(wx.NullBitmap)
    return bmp


def skeleton_bitmap(phase: int, size: int = IMAGE_SIZE) -> wx.Bitmap:
    """One frame of the loading shimmer: a soft band sweeping across a rounded rect."""
    bmp = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(240, 240, 240)))
    dc.Clear()
    dc.SetPen(wx.TRANSPARENT_PEN)
    dc.SetBrush(wx.Brush(wx.Colour(225, 225, 225)))
    dc.DrawRoundedRectangle(4, 4, size - 8, size - 8, 6)
    band_center = phase - 50
    band_width = 60
    for x in range(4, size - 4):
        dist = abs(x - band_center)
        if dist < band_width // 2:
            t = dist / (band_width / 2.0)
            alpha = int(25 * (1 + math.cos(t * math.pi)) / 2)
            if alpha > 0:
                c = min(255, 225 + alpha)
                dc.SetPen(wx.Pen(wx.Colour(c, c, c), 1))
                dc.DrawLine(x, 4, x, size - 4)
    dc.SelectObject(wx.NullBitmap)
    return bmp


def thumbnail_bitmap(img_data: Optional[bytes], size: int = IMAGE_SIZE) -> Optional[wx.Bitmap]:
    """Decode JPEG or PNG bytes into a *size* square bitmap, or None."""
    if not img_data:
        return None
    try:
        img = wx.Image(io.BytesIO(img_data), type=wx.BITMAP_TYPE_JPEG)
        if not img.IsOk():
            img = wx.Image(io.BytesIO(img_data), type=wx.BITMAP_TYPE_PNG)
        if not img.IsOk():
            return None
        return wx.Bitmap(img.Scale(size, size, wx.IMAGE_QUALITY_HIGH))
    except Exception:
        return None


class PartDetailPanel(wx.Panel):
    """Photo / footprint preview plus part facts and Datasheet / LCSC buttons.

    :meth:`show` takes an LCSC number and, when known, a part lookup result
    (the :func:`easyeda.api.search_components` schema).  Without one, only
    the LCSC number and the footprint drawing are shown.  ``on_ssl_error`` is
    called (from a worker thread) before a fetch is retried after a TLS
    certificate failure.
    """

    def __init__(self, parent, on_ssl_error: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self._on_ssl_error = on_ssl_error
        self._closing = False
        self._request_id = 0
        self._page = 0  # 0 = photo, 1 = footprint
        self._photo: Optional[wx.Bitmap] = None
        self._footprint: Optional[wx.Bitmap] = None
        self._loading = False
        self._phase = 0
        self._datasheet_url = ""
        self._page_url = ""
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_tick, self._timer)
        self._build()
        self.clear()

    def _build(self) -> None:
        box = wx.BoxSizer(wx.HORIZONTAL)
        image_col = wx.BoxSizer(wx.VERTICAL)
        self.image = wx.StaticBitmap(self, size=(IMAGE_SIZE, IMAGE_SIZE))
        self.image.SetMinSize((IMAGE_SIZE, IMAGE_SIZE))
        image_col.Add(self.image, 0)
        self.page_indicator = PageIndicator(self, on_page_change=self._on_page_change)
        image_col.Add(self.page_indicator, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 2)
        box.Add(image_col, 0, wx.ALL, 5)

        info = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=4, hgap=10, vgap=4)
        grid.AddGrowableCol(1)
        grid.AddGrowableCol(3)
        bold = self.GetFont().Bold()
        bg = self.GetBackgroundColour()
        self._fields: Dict[str, wx.TextCtrl] = {}
        for key, label, expand in (
            ("part", "Part", True),
            ("lcsc", "LCSC", True),
            ("brand", "Brand", True),
            ("package", "Package", True),
            ("price", "Price", False),
            ("stock", "Stock", False),
        ):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
            ctrl = wx.TextCtrl(self, value="", style=wx.TE_READONLY | wx.BORDER_NONE | wx.TE_NOHIDESEL)
            ctrl.SetFont(bold)
            ctrl.SetBackgroundColour(bg)
            grid.Add(ctrl, 1 if expand else 0, (wx.EXPAND if expand else 0) | wx.ALIGN_CENTER_VERTICAL)
            self._fields[key] = ctrl
        info.Add(grid, 0, wx.EXPAND | wx.BOTTOM, 4)
        self.description = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE)
        self.description.SetMinSize((-1, 48))
        info.Add(self.description, 1, wx.EXPAND | wx.BOTTOM, 4)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.datasheet_btn = wx.Button(self, label="Datasheet")
        self.datasheet_btn.Bind(wx.EVT_BUTTON, lambda _e: self._open(self._datasheet_url))
        buttons.Add(self.datasheet_btn, 0, wx.RIGHT, 5)
        self.page_btn = wx.Button(self, label="LCSC Page")
        self.page_btn.Bind(wx.EVT_BUTTON, lambda _e: self._open(self._page_url))
        buttons.Add(self.page_btn, 0)
        info.Add(buttons, 0)
        box.Add(info, 1, wx.EXPAND | wx.ALL, 5)
        self.SetSizer(box)

    # ---------------------------------------------------------------- public

    def clear(self) -> None:
        """Show nothing (no part selected)."""
        self._request_id += 1
        self._stop_loading()
        self._photo = self._footprint = None
        for ctrl in self._fields.values():
            ctrl.SetValue("")
        self.description.SetValue("")
        self._datasheet_url = self._page_url = ""
        self.datasheet_btn.Disable()
        self.page_btn.Disable()
        self._show_page()

    def show(self, lcsc: str, part: Optional[Dict[str, Any]] = None) -> None:
        """Show *lcsc*, with the facts from *part* when given, and start fetching its pictures."""
        self.clear()
        request_id = self._request_id
        part = part or {}
        kind = part.get("type") or ""
        self._fields["lcsc"].SetValue(f"{lcsc}  ({kind})" if kind else lcsc)
        self._fields["part"].SetValue(part.get("model") or "")
        self._fields["brand"].SetValue(part.get("brand") or "")
        self._fields["package"].SetValue(part.get("package") or "")
        price = part.get("price")
        self._fields["price"].SetValue(f"{part.get('currency', '$')}{price:.4f}" if price else "")
        stock = part.get("stock")
        self._fields["stock"].SetValue(f"{stock:,}" if isinstance(stock, int) else "")
        self.description.SetValue(part.get("description") or "")
        self._datasheet_url = part.get("datasheet") or ""
        self._page_url = part.get("url") or (f"https://www.lcsc.com/product-detail/{lcsc}.html" if lcsc else "")
        self.datasheet_btn.Enable(bool(self._datasheet_url))
        self.page_btn.Enable(bool(self._page_url))
        self.page_btn.SetLabel("SZLCSC Page" if "szlcsc.com" in self._page_url else "LCSC Page")
        if not lcsc:
            return
        image_url = part.get("image_url") or ""
        if self._page_url or image_url:
            self._start_loading()
            threading.Thread(
                target=self._fetch_photo, args=(self._page_url, image_url, request_id), daemon=True
            ).start()
        threading.Thread(target=self._fetch_footprint, args=(lcsc, request_id), daemon=True).start()

    def shutdown(self) -> None:
        """Drop in-flight fetches (the host window is closing)."""
        self._closing = True
        self._request_id += 1
        self._timer.Stop()

    # ------------------------------------------------------------ fetching

    def _retrying_ssl(self, fetch: Callable[[], Any]) -> Any:
        try:
            return fetch()
        except SSLCertError:
            if self._on_ssl_error is not None:
                self._on_ssl_error()
            return fetch()

    def _fetch_photo(self, page_url: str, image_url: str, request_id: int) -> None:
        try:
            data = self._retrying_ssl(lambda: fetch_product_image(page_url, direct_image_url=image_url))
        except Exception:
            data = None
        if not self._closing:
            wx.CallAfter(self._set_photo, data, request_id)

    def _fetch_footprint(self, lcsc: str, request_id: int) -> None:
        try:
            uuids = self._retrying_ssl(lambda: fetch_component_uuids(lcsc))
            svg = uuids[-1].get("svg", "") if uuids else ""
        except APIError:
            svg = None  # EasyEDA has nothing for this part
        except Exception:
            svg = ""
        if not self._closing:
            wx.CallAfter(self._set_footprint, svg, request_id)

    def _set_photo(self, data: Optional[bytes], request_id: int) -> None:
        if self._closing or request_id != self._request_id:
            return
        self._stop_loading()
        self._photo = thumbnail_bitmap(data)
        self._show_page()

    def _set_footprint(self, svg: Optional[str], request_id: int) -> None:
        if self._closing or request_id != self._request_id:
            return
        if svg is None:
            self._stop_loading()
            self._photo = self._footprint = no_easyeda_placeholder(IMAGE_SIZE)
        else:
            self._footprint = render_svg_bitmap(svg) if svg else None
        self._show_page()

    # ----------------------------------------------------------- display

    def _show_page(self) -> None:
        if self._page == 0:
            if self._loading:
                self.image.SetBitmap(skeleton_bitmap(self._phase))
            else:
                self.image.SetBitmap(self._photo or no_image_bitmap())
        else:
            self.image.SetBitmap(self._footprint or no_footprint_placeholder(IMAGE_SIZE, not has_svg_support()))

    def _on_page_change(self, page: int) -> None:
        self._page = page
        self._show_page()

    def _start_loading(self) -> None:
        self._loading = True
        self._phase = 0
        self._timer.Start(30)
        self._show_page()

    def _stop_loading(self) -> None:
        self._loading = False
        self._timer.Stop()

    def _on_tick(self, _event) -> None:
        self._phase = (self._phase + 3) % 200
        if self._page == 0 and self._loading:
            self.image.SetBitmap(skeleton_bitmap(self._phase))

    @staticmethod
    def _open(url: str) -> None:
        if url:
            webbrowser.open(url)
