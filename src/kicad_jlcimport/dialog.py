"""wxPython dialog for JLCImport plugin."""

from __future__ import annotations

import io
import math
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
    fetch_component_uuids,
    fetch_product_image,
    filter_by_min_stock,
    filter_by_type,
    search_components,
    search_components_cn,
)
from .gui.symbol_renderer import has_svg_support, render_svg_bitmap
from .importer import import_component
from .kicad.footprint_parser import _parse_kicad_mod
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
        dc = wx.ClientDC(self)
        dc.SetFont(self.GetParent().GetFont())
        return dc.GetTextExtent("Aq")[1]

    def Popup(self):
        self.Show()

    def Dismiss(self):
        self.Hide()

    # -- internals --

    def item_height(self):
        return self.GetCharHeight() + self.ITEM_PAD

    def _hit_test(self, y):
        ih = self.item_height()
        if ih <= 0:
            return -1
        idx = y // ih
        return idx if 0 <= idx < len(self._items) else -1

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetFont(self.GetParent().GetFont())
        w, _ = self.GetClientSize()
        ih = self.item_height()
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


def _no_footprint_placeholder(size: int, svg_unsupported: bool) -> wx.Bitmap:
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


def _no_easyeda_placeholder(size: int) -> wx.Bitmap:
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


class _PageIndicator(wx.Control):
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


# KiCad layer colours — "kicad_default" dark theme, matching the PCB editor exactly.
_LAYER_COLOURS: dict[str, tuple[int, int, int]] = {
    "F.Cu": (200, 52, 52),  # #C83434  red
    "B.Cu": (77, 127, 196),  # #4D7FC4  blue
    "F.SilkS": (242, 237, 161),  # #F2EDA1  pale yellow
    "B.SilkS": (232, 178, 167),  # #E8B2A7  salmon
    "F.Fab": (175, 175, 175),  # #AFAFAF  light grey
    "B.Fab": (99, 99, 99),  # #636363  dark grey
    # KiCad 8+ names (new canonical names)
    "F.Courtyard": (255, 38, 226),  # #FF26E2  hot magenta
    "B.Courtyard": (38, 233, 255),  # #26E9FF  sky cyan
    # KiCad ≤7 / standard-library names (alias — same colours)
    "F.CrtYd": (255, 38, 226),  # #FF26E2  hot magenta
    "B.CrtYd": (38, 233, 255),  # #26E9FF  sky cyan
    "F.Paste": (180, 60, 180),  # #B43CB4  purple
    "B.Paste": (60, 180, 180),  # #3CB4B4  teal
    "F.Mask": (255, 100, 150),  # #FF6496  pink
    "B.Mask": (70, 140, 255),  # #468CFF  periwinkle
    "Edge.Cuts": (255, 241, 52),  # #FFF134  yellow
    "Cmts.User": (99, 99, 99),  # #636363  dark grey
    "Eco1.User": (99, 182, 44),  # #63B62C  green
    "Eco2.User": (153, 71, 71),  # #994747  dark red
    "User.1": (206, 206, 206),  # #CECECE  silver
    "User.2": (160, 160, 160),  # #A0A0A0  grey
}
_DEFAULT_LAYER_COLOUR = (128, 128, 128)  # #808080  mid grey

# Draw order: back layers first, front on top, pads drawn separately last
# Draw order matches KiCad's layer stack:
#   Courtyard → Paste → Mask → Cu → Fab → Silkscreen
# Fab and Silkscreen are drawn AFTER copper so component body outlines and
# reference markers are visible on top of the copper pads.
_LAYER_ORDER = [
    "B.CrtYd",
    "B.Courtyard",
    "B.Paste",
    "B.Mask",
    "B.Cu",
    "B.Fab",
    "B.SilkS",
    "Edge.Cuts",
    "Cmts.User",
    "User.1",
    "User.2",
    "Eco1.User",
    "Eco2.User",
    "F.CrtYd",
    "F.Courtyard",
    "F.Paste",
    "F.Mask",
    "F.Cu",
    "F.Fab",
    "F.SilkS",
]

# Minimum rendered stroke in pixels — 1.5px keeps thin courtyard/fab lines crisp
# and prevents GraphicsContext sub-pixel anti-aliasing from making them invisible.
_MIN_STROKE_PX = 1.5


class _FootprintPreviewPanel(wx.Panel):
    """Owner-drawn panel that renders a parsed .kicad_mod footprint.

    Renders lines, arcs, circles, polygons and pads using wx.GraphicsContext
    with KiCad layer colours on a dark background.
    Supports mouse-wheel zoom and click-drag pan.
    Right-click or double-click resets zoom to fit.
    """

    # Pad colours mirror the copper layer colour, exactly as KiCad does.
    # F.Cu  = red #C83434 → pads on front copper are red.
    # B.Cu  = blue        → pads on back copper are blue.
    # np_thru_hole has no copper so rendered as dark grey with just the drill hole.
    _PAD_FILL = {
        "smd": wx.Colour(200, 52, 52, 230),  # F.Cu red, semi-transparent
        "thru_hole": wx.Colour(200, 52, 52, 230),  # F.Cu red (annular ring)
        "np_thru_hole": wx.Colour(60, 60, 60, 180),  # no copper — dark grey
    }
    _PAD_OUTLINE = wx.Colour(255, 255, 255, 80)  # subtle white edge
    _DRILL_COLOUR = wx.Colour(15, 15, 15)  # near-black drill hole
    _PIN1_COLOUR = wx.Colour(255, 255, 80)  # bright yellow pin-1 marker
    _TEXT_COLOUR = wx.Colour(200, 200, 200)

    def __init__(self, parent):
        super().__init__(parent, style=wx.BORDER_SUNKEN)
        self._fp: dict | None = None
        self._scale = 10.0
        self._offset = wx.Point(0, 0)
        self._dragging = False
        self._drag_start = wx.Point(0, 0)
        self._drag_offset_start = wx.Point(0, 0)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((320, 320))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_ldown)
        self.Bind(wx.EVT_LEFT_UP, self._on_lup)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_RIGHT_DOWN, self._on_rclick)
        self.Bind(wx.EVT_LEFT_DCLICK, self._on_rclick)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, fp: dict | None) -> None:
        self._fp = fp
        self._fit()
        self.Refresh()

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _all_points(self) -> list:
        if not self._fp:
            return []
        pts = []
        for (x1, y1), (x2, y2), *_ in self._fp["lines"]:
            pts += [(x1, y1), (x2, y2)]
        for x1, y1, x2, y2, *_ in self._fp["rects"]:
            pts += [(x1, y1), (x2, y2)]
        for cx, cy, r, *_ in self._fp["circles"]:
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
        for sx, sy, _mx, _my, ex, ey, *_ in self._fp["arcs"]:
            pts += [(sx, sy), (ex, ey)]
        for poly_pts, *_ in self._fp["polys"]:
            pts += poly_pts
        for pad in self._fp["pads"]:
            _num, x, y, w, h = pad[0], pad[1], pad[2], pad[3], pad[4]
            poly_list = pad[9] if len(pad) > 9 else []
            if poly_list:
                # Custom pad: bounds come from the actual polygon vertices (pad-local
                # coords) translated by the pad centre position.
                for pts_local in poly_list:
                    for lx, ly in pts_local:
                        pts.append((x + lx, y + ly))
            else:
                hw, hh = w / 2 + 0.2, h / 2 + 0.2
                pts += [(x - hw, y - hh), (x + hw, y + hh)]
        return pts

    def _fit(self) -> None:
        pts = self._all_points()
        w, h = self.GetClientSize()
        if w < 10:
            w, h = 320, 320
        if not pts:
            self._scale = 10.0
            self._offset = wx.Point(w // 2, h // 2)
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        span_x = max(max(xs) - min(xs), 0.5)
        span_y = max(max(ys) - min(ys), 0.5)
        self._scale = min(w * 0.82 / span_x, h * 0.82 / span_y)
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        self._offset = wx.Point(
            int(w / 2 - cx * self._scale),
            int(h / 2 - cy * self._scale),
        )

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _px(self, x: float, y: float):
        return (x * self._scale + self._offset.x, y * self._scale + self._offset.y)

    def _pxlen(self, mm: float) -> float:
        return mm * self._scale

    def _stroke_w(self, mm: float) -> float:
        return max(_MIN_STROKE_PX, self._pxlen(mm))

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _on_size(self, event):
        self._fit()
        self.Refresh()
        event.Skip()

    def _on_rclick(self, event):
        self._fit()
        self.Refresh()

    def _on_wheel(self, event):
        factor = 1.15 if event.GetWheelRotation() > 0 else 1.0 / 1.15
        mx, my = event.GetX(), event.GetY()
        self._offset = wx.Point(
            int(mx + (self._offset.x - mx) * factor),
            int(my + (self._offset.y - my) * factor),
        )
        self._scale *= factor
        self.Refresh()

    def _on_ldown(self, event):
        self._dragging = True
        self._drag_start = event.GetPosition()
        self._drag_offset_start = wx.Point(self._offset.x, self._offset.y)
        self.CaptureMouse()

    def _on_lup(self, event):
        if self._dragging:
            self._dragging = False
            if self.HasCapture():
                self.ReleaseMouse()

    def _on_motion(self, event):
        if self._dragging:
            pos = event.GetPosition()
            self._offset = wx.Point(
                self._drag_offset_start.x + pos.x - self._drag_start.x,
                self._drag_offset_start.y + pos.y - self._drag_start.y,
            )
            self.Refresh()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.Colour(26, 26, 26)))
        dc.Clear()

        if not self._fp:
            dc.SetTextForeground(wx.Colour(90, 90, 90))
            dc.DrawText("No footprint selected", 10, 10)
            return

        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return

        fp = self._fp
        w_px, h_px = self.GetClientSize()

        # ── Crosshair at origin ──────────────────────────────────────
        ox, oy = self._px(0, 0)
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo().Colour(wx.Colour(55, 55, 55)).Width(1)))
        gc.StrokeLine(0, oy, w_px, oy)
        gc.StrokeLine(ox, 0, ox, h_px)

        # ── Bucket geometry by layer ─────────────────────────────────
        layer_lines: dict[str, list] = {}
        layer_rects: dict[str, list] = {}
        layer_circles: dict[str, list] = {}
        layer_arcs: dict[str, list] = {}
        layer_polys: dict[str, list] = {}

        for seg in fp["lines"]:
            layer_lines.setdefault(seg[2], []).append(seg)
        for rect in fp["rects"]:
            layer_rects.setdefault(rect[4], []).append(rect)
        for circ in fp["circles"]:
            layer_circles.setdefault(circ[3], []).append(circ)
        for arc in fp["arcs"]:
            layer_arcs.setdefault(arc[6], []).append(arc)
        for poly in fp["polys"]:
            layer_polys.setdefault(poly[1], []).append(poly)

        all_layers = set(layer_lines) | set(layer_rects) | set(layer_circles) | set(layer_arcs) | set(layer_polys)
        ordered = [lyr for lyr in _LAYER_ORDER if lyr in all_layers]
        ordered += sorted(lyr for lyr in all_layers if lyr not in _LAYER_ORDER)

        for layer in ordered:
            r, g, b = _LAYER_COLOURS.get(layer, _DEFAULT_LAYER_COLOUR)
            # Paste and mask are rendered as thin outlines only so they don't
            # obscure the copper beneath them — just a subtle aperture indicator.
            _is_paste_mask = layer in ("F.Paste", "B.Paste", "F.Mask", "B.Mask")
            alpha = 140 if _is_paste_mask else 255
            colour = wx.Colour(r, g, b, alpha)

            def _pen(w_mm, _is_paste_mask=_is_paste_mask, colour=colour):
                # Use .Colour() chain: more reliable than constructor arg on GTK/macOS.
                # Paste/mask use a fixed thin stroke so aperture outlines stay subtle.
                w_px = _MIN_STROKE_PX if _is_paste_mask else self._stroke_w(w_mm)
                return gc.CreatePen(wx.GraphicsPenInfo().Colour(colour).Width(w_px))

            # ── Lines ────────────────────────────────────────────────
            for (x1, y1), (x2, y2), _layer, w_mm in layer_lines.get(layer, []):
                gc.SetPen(_pen(w_mm))
                px1, py1 = self._px(x1, y1)
                px2, py2 = self._px(x2, y2)
                gc.StrokeLine(px1, py1, px2, py2)

            # ── Rectangles (fp_rect, KiCad 8+) ───────────────────────
            for x1, y1, x2, y2, _layer, w_mm, corner_r, filled in layer_rects.get(layer, []):
                gc.SetPen(_pen(w_mm))
                # Paste/mask: outline only — filled would obscure pad copper
                gc.SetBrush(
                    gc.CreateBrush(wx.Brush(colour)) if (filled and not _is_paste_mask) else wx.TRANSPARENT_BRUSH
                )
                px1, py1 = self._px(x1, y1)
                px2, py2 = self._px(x2, y2)
                pw = abs(px2 - px1)
                ph = abs(py2 - py1)
                left = min(px1, px2)
                top = min(py1, py2)
                corner_px = max(0.0, self._pxlen(corner_r))
                if corner_px > 0:
                    path = gc.CreatePath()
                    path.AddRoundedRectangle(left, top, pw, ph, corner_px)
                    gc.DrawPath(path)
                else:
                    gc.DrawRectangle(left, top, pw, ph)

            # ── Circles ──────────────────────────────────────────────
            for cx, cy, radius, _layer, w_mm, filled in layer_circles.get(layer, []):
                gc.SetPen(_pen(w_mm))
                if filled:
                    gc.SetBrush(gc.CreateBrush(wx.Brush(colour)))
                else:
                    gc.SetBrush(wx.TRANSPARENT_BRUSH)
                px, py = self._px(cx - radius, cy - radius)
                diameter = self._pxlen(radius) * 2
                gc.DrawEllipse(px, py, diameter, diameter)

            # ── Arcs ─────────────────────────────────────────────────
            for sx, sy, mx_, my_, ex, ey, _layer, w_mm in layer_arcs.get(layer, []):
                gc.SetPen(_pen(w_mm))
                try:
                    # Circumcircle through start / mid / end
                    ax, ay = sx, sy
                    bx, by = mx_, my_
                    cx2, cy2 = ex, ey
                    d = 2 * (ax * (by - cy2) + bx * (cy2 - ay) + cx2 * (ay - by))
                    if abs(d) < 1e-9:
                        px1, py1 = self._px(sx, sy)
                        px2, py2 = self._px(ex, ey)
                        gc.StrokeLine(px1, py1, px2, py2)
                        continue
                    ux = (
                        (ax**2 + ay**2) * (by - cy2) + (bx**2 + by**2) * (cy2 - ay) + (cx2**2 + cy2**2) * (ay - by)
                    ) / d
                    uy = (
                        (ax**2 + ay**2) * (cx2 - bx) + (bx**2 + by**2) * (ax - cx2) + (cx2**2 + cy2**2) * (bx - ax)
                    ) / d
                    radius_arc = math.hypot(ax - ux, ay - uy)
                    a_start = math.atan2(ay - uy, ax - ux)
                    a_mid = math.atan2(by - uy, bx - ux)
                    a_end = math.atan2(cy2 - uy, cx2 - ux)

                    # Determine sweep direction using the midpoint angle
                    # Normalise to go from a_start in the same direction as mid
                    def _norm(a, ref):
                        while a < ref - math.pi:
                            a += 2 * math.pi
                        while a > ref + math.pi:
                            a -= 2 * math.pi
                        return a

                    a_mid_n = _norm(a_mid, a_start)
                    a_end_n = _norm(a_end, a_start)
                    if (a_mid_n > a_start) != (a_end_n > a_start):
                        a_end_n += 2 * math.pi if a_end_n < a_start else -2 * math.pi
                    steps = max(12, int(abs(a_end_n - a_start) / math.radians(4)))
                    pts_arc = [
                        self._px(
                            ux + radius_arc * math.cos(a_start + (a_end_n - a_start) * i / steps),
                            uy + radius_arc * math.sin(a_start + (a_end_n - a_start) * i / steps),
                        )
                        for i in range(steps + 1)
                    ]
                    if len(pts_arc) >= 2:
                        path = gc.CreatePath()
                        path.MoveToPoint(*pts_arc[0])
                        for pt in pts_arc[1:]:
                            path.AddLineToPoint(*pt)
                        gc.StrokePath(path)
                except Exception:
                    import sys as _sys

                    print(f"[FootprintPreview] arc render error: {traceback.format_exc()}", file=_sys.stderr)

            # ── Polygons ─────────────────────────────────────────────
            for poly_pts, _layer, filled in layer_polys.get(layer, []):
                if len(poly_pts) < 2:
                    continue
                gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo().Colour(colour).Width(_MIN_STROKE_PX)))
                gc.SetBrush(
                    gc.CreateBrush(wx.Brush(colour)) if (filled and not _is_paste_mask) else wx.TRANSPARENT_BRUSH
                )
                path = gc.CreatePath()
                path.MoveToPoint(*self._px(*poly_pts[0]))
                for pt in poly_pts[1:]:
                    path.AddLineToPoint(*self._px(*pt))
                path.CloseSubpath()
                gc.DrawPath(path)

        # ── Pads ─────────────────────────────────────────────────────
        # Two-pass rendering so thru-hole vias always appear on top of SMD pads:
        #   Pass 1 — SMD / np_thru_hole copper (fills, annular rings, labels)
        #   Pass 2 — thru_hole pads + drill holes on top of everything
        # Within each pass larger pads are drawn first so smaller ones sit on top.
        def _draw_pad_shape(gc, num, wpx, hpx, shape, pad_type, drill_d, poly_list):
            s = shape.lower()
            if s == "custom":
                # KiCad custom pad copper = anchor_shape UNION all gr_poly primitives.
                # Draw the anchor rect first (wpx/hpx = anchor size in pixels),
                # then each gr_poly on top with the same fill brush already set.
                gc.DrawRectangle(-wpx / 2, -hpx / 2, wpx, hpx)
                for poly_pts in poly_list:
                    path = gc.CreatePath()
                    path.MoveToPoint(self._pxlen(poly_pts[0][0]), self._pxlen(poly_pts[0][1]))
                    for pt in poly_pts[1:]:
                        path.AddLineToPoint(self._pxlen(pt[0]), self._pxlen(pt[1]))
                    path.CloseSubpath()
                    gc.DrawPath(path)
            elif s == "circle":
                r2 = wpx / 2
                gc.DrawEllipse(-r2, -r2, r2 * 2, r2 * 2)
            elif s in ("oval", "roundrect"):
                corner = min(wpx, hpx) / 2 if s == "oval" else min(wpx, hpx) * 0.2
                path = gc.CreatePath()
                path.AddRoundedRectangle(-wpx / 2, -hpx / 2, wpx, hpx, corner)
                gc.DrawPath(path)
            else:  # rect / trapezoid / default
                gc.DrawRectangle(-wpx / 2, -hpx / 2, wpx, hpx)
            if pad_type in ("thru_hole", "np_thru_hole") and drill_d > 0:
                # drill_d is diameter in mm; _pxlen gives pixels for that diameter.
                # Clamp so the drill never visually exceeds the copper pad.
                dr_px = min(self._pxlen(drill_d) / 2, min(wpx, hpx) * 0.45)
                dr_px = max(0.5, dr_px)
                gc.SetBrush(gc.CreateBrush(wx.Brush(self._DRILL_COLOUR)))
                gc.SetPen(wx.NullGraphicsPen)
                gc.DrawEllipse(-dr_px, -dr_px, dr_px * 2, dr_px * 2)

        def _draw_pad_label(gc, num, wpx, hpx):
            if not num:
                return
            min_side = min(wpx, hpx)
            if min_side < 4:
                return
            # Scale font to fit the pad — no minimum pt floor so text shrinks
            # as the view zooms out rather than disappearing entirely.
            # Try progressively smaller sizes until the label fits, or give up
            # if it would be unreadably tiny (< 4px tall).
            for pt in range(min(22, int(min_side * 0.7)), 2, -1):
                gc.SetFont(wx.Font(wx.FontInfo(pt).AntiAliased()), self._TEXT_COLOUR)
                tw, th = gc.GetTextExtent(num)
                if tw <= wpx * 0.88 and th <= hpx * 0.88:
                    if th >= 4:
                        gc.DrawText(num, -tw / 2, -th / 2)
                    return

        outline_w = max(0.8, self._scale * 0.04)
        smd_pads = sorted([p for p in fp["pads"] if p[7] == "smd"], key=lambda p: p[3] * p[4], reverse=True)
        thru_pads = sorted(
            [p for p in fp["pads"] if p[7] in ("thru_hole", "np_thru_hole")], key=lambda p: p[3] * p[4], reverse=True
        )

        for pass_pads in (smd_pads, thru_pads):
            for num, x, y, pw, ph, shape, rot, pad_type, drill_d, poly_list in pass_pads:
                fill_col = self._PAD_FILL.get(pad_type, wx.Colour(160, 160, 160, 200))
                gc.SetBrush(gc.CreateBrush(wx.Brush(fill_col)))
                gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(self._PAD_OUTLINE).Width(outline_w)))
                px_c, py_c = self._px(x, y)
                wpx = max(2.0, self._pxlen(pw))
                hpx = max(2.0, self._pxlen(ph))
                gc.PushState()
                gc.Translate(px_c, py_c)
                if rot:
                    gc.Rotate(math.radians(rot))
                _draw_pad_shape(gc, num, wpx, hpx, shape, pad_type, drill_d, poly_list)
                _draw_pad_label(gc, num, wpx, hpx)
                gc.PopState()

        # ── Hint ─────────────────────────────────────────────────────
        hint_font = wx.Font(wx.FontInfo(8))
        gc.SetFont(hint_font, wx.Colour(70, 70, 70))
        gc.DrawText("scroll=zoom  drag=pan  dbl-click=fit", 4, h_px - 14)


class FootprintBrowserDialog(wx.Dialog):
    """Three-pane footprint library browser with live 2D preview.

    Left pane   – library list (from fp-lib-table via _iter_footprint_libraries).
    Middle pane – footprint list within the selected library.
    Right pane  – 2D rendered preview of the selected footprint drawn with
                  KiCad layer colours, plus a 3D model info section below it.

    The preview panel supports mouse-wheel zoom and click-drag pan.
    Double-clicking a footprint or clicking OK confirms the selection and
    returns a ``"LibraryName:FootprintName"`` reference via ``get_selection()``.
    """

    def __init__(
        self,
        parent,
        project_dir: str = "",
        kicad_version: int = DEFAULT_KICAD_VERSION,
        initial_selection: str = "",
        jlc_lib_name: str = "JLCImport",
        jlc_global_lib_dir: str = "",
    ):
        super().__init__(
            parent,
            title="Select Footprint",
            size=(1000, 600),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        from .kicad.library import _iter_footprint_libraries

        self._libs: list[tuple[str, str]] = list(
            _iter_footprint_libraries(
                project_dir,
                kicad_version,
                jlc_lib_name=jlc_lib_name,
                jlc_global_lib_dir=jlc_global_lib_dir,
            )
        )
        self._selection = ""
        self._current_fp_path = ""
        self._project_dir = project_dir
        self._kicad_version = kicad_version
        self._build_ui()
        self.Centre()
        # Pre-navigate to the initial selection (e.g. the auto-matched candidate)
        if initial_selection:
            self._navigate_to(initial_selection)

    def _build_ui(self) -> None:
        outer = wx.BoxSizer(wx.VERTICAL)

        # Filter bar
        filter_row = wx.BoxSizer(wx.HORIZONTAL)
        filter_row.Add(wx.StaticText(self, label="Filter:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._filter = wx.TextCtrl(self)
        self._filter.SetHint("type to filter libraries and footprints…")
        self._filter.Bind(wx.EVT_TEXT, self._on_filter)
        filter_row.Add(self._filter, 1)
        outer.Add(filter_row, 0, wx.EXPAND | wx.ALL, 6)

        # ── Three-pane splitter ──────────────────────────────────────
        # outer_split: [lib_list | inner_split]
        # inner_split: [fp_list  | preview_panel]
        outer_split = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        inner_split = wx.SplitterWindow(outer_split, style=wx.SP_LIVE_UPDATE)

        self._lib_list = wx.ListBox(outer_split, style=wx.LB_SINGLE)
        self._lib_list.Bind(wx.EVT_LISTBOX, self._on_lib_select)

        self._fp_list = wx.ListBox(inner_split, style=wx.LB_SINGLE)
        self._fp_list.Bind(wx.EVT_LISTBOX, self._on_fp_select)
        self._fp_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_fp_dclick)

        # Right pane: preview + info
        right_panel = wx.Panel(inner_split)
        right_sizer = wx.BoxSizer(wx.VERTICAL)

        self._preview = _FootprintPreviewPanel(right_panel)
        right_sizer.Add(self._preview, 1, wx.EXPAND)

        # Info bar below preview: description, pad count, 3D model
        info_panel = wx.Panel(right_panel)
        info_sizer = wx.FlexGridSizer(cols=2, hgap=8, vgap=2)
        info_sizer.AddGrowableCol(1)
        bold = info_panel.GetFont().Bold()

        info_sizer.Add(wx.StaticText(info_panel, label="Description:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._info_descr = wx.StaticText(info_panel, label="", style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_END)
        info_sizer.Add(self._info_descr, 1, wx.EXPAND)

        info_sizer.Add(wx.StaticText(info_panel, label="Tags:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._info_tags = wx.StaticText(info_panel, label="", style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_END)
        info_sizer.Add(self._info_tags, 1, wx.EXPAND)

        info_sizer.Add(wx.StaticText(info_panel, label="Pads:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._info_pads = wx.StaticText(info_panel, label="")
        self._info_pads.SetFont(bold)
        info_sizer.Add(self._info_pads, 0)

        info_sizer.Add(wx.StaticText(info_panel, label="3D Model:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._info_model = wx.StaticText(info_panel, label="", style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_START)
        info_sizer.Add(self._info_model, 1, wx.EXPAND)

        info_panel.SetSizer(info_sizer)
        right_sizer.Add(info_panel, 0, wx.EXPAND | wx.ALL, 4)
        right_panel.SetSizer(right_sizer)

        inner_split.SplitVertically(self._fp_list, right_panel, 220)
        outer_split.SplitVertically(self._lib_list, inner_split, 200)
        outer.Add(outer_split, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        # Current selection label
        self._sel_label = wx.StaticText(self, label="", style=wx.ST_NO_AUTORESIZE)
        outer.Add(self._sel_label, 0, wx.ALL, 6)

        btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindowById(wx.ID_OK).Disable()
        outer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.SetSizer(outer)
        self._populate_lib_list("")

    # ------------------------------------------------------------------
    # Library / footprint list population
    # ------------------------------------------------------------------

    def _populate_lib_list(self, filt: str) -> None:
        """Fill the library list, including libraries that contain matching footprints."""
        filt_up = filt.strip().upper()
        self._lib_list.Clear()
        self._filtered_libs: list[tuple[str, str]] = []
        for lib_name, lib_path in self._libs:
            if not filt_up or filt_up in lib_name.upper():
                self._filtered_libs.append((lib_name, lib_path))
            else:
                try:
                    if any(filt_up in e.upper() for e in os.listdir(lib_path) if e.lower().endswith(".kicad_mod")):
                        self._filtered_libs.append((lib_name, lib_path))
                except OSError:
                    pass
        for lib_name, _ in self._filtered_libs:
            self._lib_list.Append(lib_name)
        self._fp_list.Clear()
        self._selection = ""
        self._sel_label.SetLabel("")
        self._clear_preview()
        self.FindWindowById(wx.ID_OK).Disable()

    def _on_filter(self, event) -> None:
        filt = self._filter.GetValue()
        self._populate_lib_list(filt)
        if self._lib_list.GetCount() > 0:
            self._lib_list.SetSelection(0)
            self._on_lib_select(None)

    def _on_lib_select(self, event) -> None:
        idx = self._lib_list.GetSelection()
        if idx == wx.NOT_FOUND or idx >= len(self._filtered_libs):
            return
        _, lib_path = self._filtered_libs[idx]
        filt_up = self._filter.GetValue().strip().upper()
        self._fp_list.Clear()
        try:
            names = sorted(
                e[: -len(".kicad_mod")]
                for e in os.listdir(lib_path)
                if e.lower().endswith(".kicad_mod") and (not filt_up or filt_up in e.upper())
            )
        except OSError:
            names = []
        for name in names:
            self._fp_list.Append(name)
        self._selection = ""
        self._sel_label.SetLabel("")
        self._clear_preview()
        self.FindWindowById(wx.ID_OK).Disable()

    def _on_fp_select(self, event) -> None:
        lib_idx = self._lib_list.GetSelection()
        fp_idx = self._fp_list.GetSelection()
        if lib_idx == wx.NOT_FOUND or lib_idx >= len(self._filtered_libs) or fp_idx == wx.NOT_FOUND:
            return
        lib_name, lib_path = self._filtered_libs[lib_idx]
        fp_name = self._fp_list.GetString(fp_idx)
        self._selection = f"{lib_name}:{fp_name}"
        self._sel_label.SetLabel(self._selection)
        self.FindWindowById(wx.ID_OK).Enable()
        # Load and render preview in a background thread to keep UI responsive
        fp_path = os.path.join(lib_path, f"{fp_name}.kicad_mod")
        self._current_fp_path = fp_path
        threading.Thread(target=self._load_preview, args=(fp_path,), daemon=True).start()

    def _on_fp_dclick(self, event) -> None:
        self._on_fp_select(event)
        if self._selection:
            self.EndModal(wx.ID_OK)

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _clear_preview(self) -> None:
        self._preview.load(None)
        self._info_descr.SetLabel("")
        self._info_tags.SetLabel("")
        self._info_pads.SetLabel("")
        self._info_model.SetLabel("")
        self._info_model.SetForegroundColour(wx.NullColour)

    def _load_preview(self, fp_path: str) -> None:
        """Parse the .kicad_mod on a background thread, then update UI."""
        fp = _parse_kicad_mod(fp_path, project_dir=self._project_dir, kicad_version=self._kicad_version)
        if not wx.IsMainThread():
            wx.CallAfter(self._apply_preview, fp_path, fp)
        else:
            self._apply_preview(fp_path, fp)

    def _apply_preview(self, fp_path: str, fp: dict) -> None:
        """Apply parsed footprint data to the preview panel (main thread)."""
        # Guard against a newer selection having arrived
        if fp_path != self._current_fp_path:
            return
        self._preview.load(fp)
        self._info_descr.SetLabel(fp.get("descr", "") or "—")
        self._info_tags.SetLabel(fp.get("tags", "") or "—")
        self._info_pads.SetLabel(str(fp.get("pads_count", 0)))

        model = fp.get("model")
        if model:
            raw_path, exists = model
            # Show just the filename portion for brevity; full path in tooltip
            model_name = os.path.basename(raw_path)
            self._info_model.SetLabel(model_name)
            self._info_model.SetToolTip(raw_path)
            if exists:
                self._info_model.SetForegroundColour(wx.Colour(80, 200, 80))  # green = found
            else:
                self._info_model.SetForegroundColour(wx.Colour(200, 120, 50))  # orange = missing
        else:
            self._info_model.SetLabel("(none)")
            self._info_model.SetForegroundColour(wx.Colour(120, 120, 120))

        self.Layout()

    def _navigate_to(self, ref: str) -> None:
        """Pre-select a ``"LibraryName:FootprintName"`` reference in both list panes.

        Called after the UI is built so the user opens the dialog already
        positioned at the previously matched or chosen footprint.
        """
        if ":" not in ref:
            return
        lib_name, fp_name = ref.split(":", 1)
        # Find the library in the filtered list
        for i, (name, _) in enumerate(self._filtered_libs):
            if name == lib_name:
                self._lib_list.SetSelection(i)
                self._on_lib_select(None)
                # Now find the footprint in the right pane
                for j in range(self._fp_list.GetCount()):
                    if self._fp_list.GetString(j) == fp_name:
                        self._fp_list.SetSelection(j)
                        self._fp_list.EnsureVisible(j)
                        self._on_fp_select(None)
                        break
                break

    def get_selection(self) -> str:
        """Return the selected ``"LibraryName:FootprintName"`` reference, or ``""``."""
        return self._selection


class MetadataEditDialog(wx.Dialog):
    """Modal dialog for editing component metadata before import."""

    def __init__(self, parent, metadata: dict):
        self._footprint_candidate_ref = metadata.get("__footprint_candidate_ref", "")
        # Start with the auto-matched candidate pre-selected (may be overridden by Browse)
        self._kicad_footprint_ref = self._footprint_candidate_ref
        _gkv = getattr(parent, "_get_kicad_version", None)
        self._kicad_version = _gkv() if callable(_gkv) else getattr(parent, "_kicad_version", DEFAULT_KICAD_VERSION)
        # In plugin mode _project_dir is empty; _get_project_dir() reads the board path instead.
        _gpd = getattr(parent, "_get_project_dir", None)
        self._project_dir = (_gpd() if callable(_gpd) else getattr(parent, "_project_dir", "")) or ""
        # Used to make JLCImport-managed .pretty dirs visible in the browser even
        # when the lib-table hasn't been reloaded by KiCad yet.
        self._jlc_lib_name = getattr(parent, "_lib_name", "JLCImport")
        self._jlc_global_lib_dir = getattr(parent, "_global_lib_dir", "") or ""
        super().__init__(
            parent,
            title="Edit Metadata",
            size=(540, 460),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._build_ui(metadata)
        self.Fit()  # shrink-to-fit after all widgets are created
        self.SetMinSize(self.GetSize())  # prevent it being squashed below fit size
        self.Centre()

    def _build_ui(self, metadata: dict):
        vbox = wx.BoxSizer(wx.VERTICAL)

        grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=8)
        grid.AddGrowableCol(1)

        grid.Add(wx.StaticText(self, label="Description"), 0, wx.ALIGN_TOP)
        self._desc = wx.TextCtrl(self, value=metadata.get("description", ""), style=wx.TE_MULTILINE, size=(-1, 60))
        grid.Add(self._desc, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Keywords"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._keywords = wx.TextCtrl(self, value=metadata.get("keywords", ""))
        grid.Add(self._keywords, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Manufacturer"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._manufacturer = wx.TextCtrl(self, value=metadata.get("manufacturer", ""))
        grid.Add(self._manufacturer, 1, wx.EXPAND)

        # Footprint file name — editable only when importing from EasyEDA.
        # Defaults to the sanitized component name; user can rename to something
        # shorter/cleaner (e.g. "SOIC-8" instead of "AO9926B").
        self._fp_name_label = wx.StaticText(self, label="Footprint name")
        grid.Add(self._fp_name_label, 0, wx.ALIGN_CENTER_VERTICAL)
        component_name = metadata.get("__component_name", "")
        self._fp_name = wx.TextCtrl(self, value=component_name)
        self._fp_name.SetToolTip(
            "File name for the imported .kicad_mod footprint.\nGreyed out when using a KiCad library footprint."
        )
        grid.Add(self._fp_name, 1, wx.EXPAND)

        # 3D model file name — tracks the footprint name by default but can be
        # set independently (e.g. reuse an existing .step/.wrl under a different name).
        self._model_name_label = wx.StaticText(self, label="3D model name")
        grid.Add(self._model_name_label, 0, wx.ALIGN_CENTER_VERTICAL)
        self._model_name = wx.TextCtrl(self, value=component_name)
        self._model_name.SetToolTip(
            "File name for the downloaded .step and .wrl 3D models.\nGreyed out when using a KiCad library footprint."
        )
        grid.Add(self._model_name, 1, wx.EXPAND)

        vbox.Add(grid, 1, wx.EXPAND | wx.ALL, 10)

        # ── Footprint selection ──────────────────────────────────────
        fp_box = wx.BoxSizer(wx.VERTICAL)

        # Radio 1: import from EasyEDA
        self._rb_import = wx.RadioButton(self, label="Import footprint from EasyEDA", style=wx.RB_GROUP)
        self._rb_import.Bind(wx.EVT_RADIOBUTTON, lambda _e: self._update_name_fields_state())
        fp_box.Add(self._rb_import, 0, wx.BOTTOM, 6)

        # Radio 2: use a KiCad footprint — auto-matched ref shown as the label,
        # Browse… button lets the user pick a different one from the library browser.
        kicad_row = wx.BoxSizer(wx.HORIZONTAL)
        self._rb_kicad = wx.RadioButton(self, label="Use KiCad footprint:")
        self._rb_kicad.Bind(wx.EVT_RADIOBUTTON, lambda _e: self._update_name_fields_state())
        self._kicad_ref_label = wx.StaticText(
            self,
            label=self._kicad_footprint_ref or "(none selected)",
            style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_START,
        )
        self._browse_btn = wx.Button(self, label="Browse…", style=wx.BU_EXACTFIT)
        self._browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_footprint)
        kicad_row.Add(self._rb_kicad, 0, wx.ALIGN_CENTER_VERTICAL)
        kicad_row.Add(self._kicad_ref_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 6)
        kicad_row.Add(self._browse_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        fp_box.Add(kicad_row, 0, wx.EXPAND)

        vbox.Add(fp_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Default selection: pre-select "Use KiCad footprint" when a candidate exists
        if self._footprint_candidate_ref:
            self._rb_kicad.SetValue(True)
        else:
            self._rb_import.SetValue(True)

        btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        vbox.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.SetSizer(vbox)
        # Apply initial enable/disable state to the name fields
        self._update_name_fields_state()

    def _update_name_fields_state(self) -> None:
        """Grey out Footprint name / 3D model name when a KiCad library footprint is selected.

        Those names only apply when importing from EasyEDA; when the user picks
        an existing KiCad footprint the fields are irrelevant so we disable them
        to make that clear.
        """
        easyeda_active = self._rb_import.GetValue()
        for ctrl in (self._fp_name, self._model_name, self._fp_name_label, self._model_name_label):
            ctrl.Enable(easyeda_active)

    def _on_browse_footprint(self, event) -> None:
        """Open the footprint library browser, pre-navigating to the current ref."""
        dlg = FootprintBrowserDialog(
            self,
            project_dir=self._project_dir,
            kicad_version=self._kicad_version,
            initial_selection=self._kicad_footprint_ref,
            jlc_lib_name=self._jlc_lib_name,
            jlc_global_lib_dir=self._jlc_global_lib_dir,
        )
        if dlg.ShowModal() == wx.ID_OK:
            chosen = dlg.get_selection()
            if chosen:
                self._kicad_footprint_ref = chosen
                self._kicad_ref_label.SetLabel(chosen)
                self._rb_kicad.SetValue(True)
                self._update_name_fields_state()
                self.Layout()
        dlg.Destroy()

    def get_metadata(self) -> dict:
        """Return the edited metadata dict."""
        result = {
            "description": self._desc.GetValue(),
            "keywords": self._keywords.GetValue(),
            "manufacturer": self._manufacturer.GetValue(),
        }
        if self._rb_kicad.GetValue() and self._kicad_footprint_ref:
            result["__reuse_existing_footprint"] = True
            result["__manually_chosen_footprint"] = self._kicad_footprint_ref
        else:
            result["__reuse_existing_footprint"] = False
            # Only pass name overrides when actually importing from EasyEDA
            result["__footprint_name"] = self._fp_name.GetValue().strip()
            result["__model_name"] = self._model_name.GetValue().strip()
        return result


class _SpinnerOverlay(wx.Window):
    """Transparent spinner overlay drawn on top of a parent widget.

    Uses ``wx.TRANSPARENT_WINDOW`` so the parent content shows through
    and only the spinning arc is visible.  Interaction is blocked by
    disabling the parent widget separately.
    """

    _ARC_RADIUS = 18
    _ARC_WIDTH = 3
    _SEGMENTS = 30
    _ARC_SWEEP = 300
    _TICK_MS = 50  # 20 fps — fast enough visually, slow enough not to starve CallAfter

    def __init__(self, parent, target=None):
        super().__init__(parent, style=wx.TRANSPARENT_WINDOW)
        self._target = target
        self._angle = 0
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_TIMER, self._on_tick, self._timer)
        self.Hide()

    def show(self):
        """Show the spinner and start animation. Safe to call from main thread only."""
        if not self or not self.GetParent():
            return
        self._sync_position()
        self.Show()
        self.Raise()
        # Use _TICK_MS — a 25 ms timer fires 40×/sec and floods the event queue,
        # starving wx.CallAfter callbacks posted from background threads.
        self._timer.Start(self._TICK_MS)

    def pause(self):
        """Stop the timer without hiding. Prevents EVT_TIMER from starving wx.CallAfter."""
        self._timer.Stop()

    def dismiss(self):
        """Hide the spinner and stop animation. Safe to call from main thread only."""
        self._timer.Stop()
        if self and self.IsShown():
            self.Hide()

    def _sync_position(self):
        if self._target:
            rect = self._target.GetRect()
            self.SetPosition(rect.GetPosition())
            self.SetSize(rect.GetSize())
        else:
            self.SetPosition((0, 0))
            self.SetSize(self.GetParent().GetClientSize())

    def _on_tick(self, event):
        self._angle = (self._angle + 12) % 360  # faster apparent spin at lower fps
        # Only re-sync position if we have a target (cheap check avoids layout churn)
        if self._target:
            self._sync_position()
        self.Refresh()

    def _on_paint(self, event):
        dc = wx.PaintDC(self)
        w, h = self.GetClientSize()

        # Derive spinner colours from the background luminance
        bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        lum = (bg.Red() * 299 + bg.Green() * 587 + bg.Blue() * 114) // 1000
        if lum < 128:
            tail_grey, head_grey = 60, 200
        else:
            tail_grey, head_grey = 210, 80

        cx, cy = w / 2.0, h / 2.0
        r = self._ARC_RADIUS
        for i in range(self._SEGMENTS):
            frac = i / self._SEGMENTS
            t1 = self._angle + frac * self._ARC_SWEEP
            t2 = self._angle + (i + 1) / self._SEGMENTS * self._ARC_SWEEP
            a1 = math.radians(t1)
            a2 = math.radians(t2)
            x1 = cx + r * math.cos(a1)
            y1 = cy + r * math.sin(a1)
            x2 = cx + r * math.cos(a2)
            y2 = cy + r * math.sin(a2)
            grey = int(tail_grey + (head_grey - tail_grey) * frac)
            dc.SetPen(wx.Pen(wx.Colour(grey, grey, grey), self._ARC_WIDTH))
            dc.DrawLine(int(x1), int(y1), int(x2), int(y2))


class JLCImportDialog(wx.Dialog):
    def __init__(self, parent, board, project_dir=None, kicad_version=None, global_lib_dir="", on_close=None):
        super().__init__(parent, title="JLCImport", size=(700, 640), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self._project_dir = project_dir  # Used when board is None (standalone mode)
        self._kicad_version = kicad_version or DEFAULT_KICAD_VERSION
        self._global_lib_dir_override = global_lib_dir
        self._on_close_callback = on_close
        self._closing = False
        self._search_results = []
        self._raw_search_results = []
        self._search_request_id = 0
        self._image_request_id = 0
        self._gallery_request_id = 0
        self._gallery_svg_request_id = 0
        self._gallery_page = 0  # 0=photo, 1=footprint in gallery view
        self._gallery_photo_bitmap = None
        self._gallery_svg_string = None

        self._ssl_warning_shown = False
        self._selected_result = None
        self._has_easyeda_data = True  # cleared when API returns no data
        self._photo_bitmap = None
        self._symbol_bitmap = None
        self._symbol_svg_string = None  # raw SVG for re-rendering at gallery size
        self._detail_page = 0  # 0=photo, 1=symbol
        self._symbol_request_id = 0
        self._init_ui()
        self.Centre()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _on_close(self, event):
        if self._closing:
            return
        # Warn if an import is in progress (main panel disabled during import)
        if not self._main_panel.IsEnabled():
            if (
                wx.MessageBox(
                    "An import is in progress. Close anyway?",
                    "Confirm",
                    wx.YES_NO | wx.ICON_WARNING,
                )
                != wx.YES
            ):
                return
        self._closing = True
        # Stop all timers to prevent callbacks on destroyed widgets
        self._stop_search_pulse()
        self._stop_skeleton()
        self._stop_gallery_skeleton()
        self._busy_overlay.dismiss()
        self._search_overlay.dismiss()
        self._category_popup.Dismiss()
        # Invalidate all in-flight background requests so their CallAfter
        # callbacks will no-op when they check the request ID
        self._search_request_id += 1
        self._image_request_id += 1
        self._gallery_request_id += 1
        self._gallery_svg_request_id += 1
        self._symbol_request_id += 1
        if self._on_close_callback:
            self._on_close_callback()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Destroy()

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
        _init_region = load_config().get("region", "global")
        self.search_input.SetHint("Search SZLCSC parts..." if _init_region == "cn" else "Search JLCPCB parts...")
        self.search_input.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        self.search_input.Bind(wx.EVT_TEXT, self._on_search_text_changed)
        hbox_search.Add(self.search_input, 1, wx.EXPAND | wx.RIGHT, 5)
        self.search_btn = wx.Button(panel, label="Search")
        self.search_btn.Bind(wx.EVT_BUTTON, self._on_search)
        hbox_search.Add(self.search_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self._region_choices = ["Global (JLCPCB)", "China (SZLCSC)"]
        self._region_values = ["global", "cn"]
        self.region_choice = wx.Choice(panel, choices=self._region_choices)
        saved_region = load_config().get("region", "global")
        sel = self._region_values.index(saved_region) if saved_region in self._region_values else 0
        self.region_choice.SetSelection(sel)
        self.region_choice.Bind(wx.EVT_CHOICE, self._on_region_change)
        hbox_search.Add(wx.StaticText(panel, label="Region"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        hbox_search.Add(self.region_choice, 0, wx.ALIGN_CENTER_VERTICAL)
        search_box.Add(hbox_search, 0, wx.EXPAND | wx.ALL, 5)

        # Filter row
        hbox_filter = wx.BoxSizer(wx.HORIZONTAL)
        hbox_filter.Add(wx.StaticText(panel, label="Type"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
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
        hbox_filter.Add(wx.StaticText(panel, label="Min stock"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._min_stock_choices = [0, 1, 10, 100, 1000, 10000, 100000]
        self._min_stock_labels = ["Any", "1+", "10+", "100+", "1000+", "10000+", "100000+"]
        self.min_stock_choice = wx.Choice(panel, choices=self._min_stock_labels)
        self.min_stock_choice.SetSelection(1)  # Default to "1+" (in stock)
        self.min_stock_choice.Bind(wx.EVT_CHOICE, self._on_min_stock_change)
        hbox_filter.Add(self.min_stock_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        hbox_filter.Add(wx.StaticText(panel, label="Package"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.package_choice = wx.Choice(panel, choices=["All"])
        self.package_choice.SetSelection(0)
        self.package_choice.Bind(wx.EVT_CHOICE, self._on_filter_change)
        hbox_filter.Add(self.package_choice, 0, wx.ALIGN_CENTER_VERTICAL)
        search_box.Add(hbox_filter, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)

        vbox.Add(search_box, 0, wx.EXPAND | wx.ALL, 5)

        self.results_count_label = wx.StaticText(panel, label="")
        vbox.Add(self.results_count_label, 0, wx.LEFT | wx.RIGHT, 10)

        # --- Results list ---
        self.results_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.results_list.InsertColumn(0, "LCSC", width=80)
        self.results_list.InsertColumn(1, "Type", width=55)
        self.results_list.InsertColumn(2, "Price", width=60)
        self.results_list.InsertColumn(3, "Stock", width=75)
        self.results_list.InsertColumn(4, "Brand", width=100)
        self.results_list.InsertColumn(5, "Part", width=180)
        self.results_list.InsertColumn(6, "Package", width=80)
        self.results_list.InsertColumn(7, "Description", width=280)
        self.results_list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_result_select)
        self.results_list.Bind(wx.EVT_LIST_COL_CLICK, self._on_col_click)
        self._sort_col = -1
        self._sort_ascending = True
        self._imported_ids = set()
        vbox.Add(self.results_list, 2, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)

        # --- Detail panel (shown on selection) ---
        self._detail_box = wx.BoxSizer(wx.HORIZONTAL)

        # Image on left (click to zoom) with page indicator below
        image_col = wx.BoxSizer(wx.VERTICAL)
        self.detail_image = wx.StaticBitmap(panel, size=(160, 160))
        self.detail_image.SetMinSize((160, 160))
        self.detail_image.SetCursor(wx.Cursor(wx.CURSOR_MAGNIFIER))
        self.detail_image.Bind(wx.EVT_LEFT_DOWN, self._on_image_click)
        self._full_image_data = None
        image_col.Add(self.detail_image, 0)
        self._page_indicator = _PageIndicator(panel, on_page_change=self._on_page_change)
        image_col.Add(self._page_indicator, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 2)
        self._detail_box.Add(image_col, 0, wx.ALL, 5)

        # Info on right
        info_sizer = wx.BoxSizer(wx.VERTICAL)
        detail_grid = wx.FlexGridSizer(cols=4, hgap=10, vgap=4)
        detail_grid.AddGrowableCol(1)
        detail_grid.AddGrowableCol(3)

        bold_font = panel.GetFont().Bold()
        bg = panel.GetBackgroundColour()

        def _info_field(label: str, expand: bool = False) -> wx.TextCtrl:
            """Read-only field that looks like a bold label but is selectable/copyable."""
            detail_grid.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
            ctrl = wx.TextCtrl(panel, value="", style=wx.TE_READONLY | wx.BORDER_NONE | wx.TE_NOHIDESEL)
            ctrl.SetFont(bold_font)
            ctrl.SetBackgroundColour(bg)
            detail_grid.Add(ctrl, 1 if expand else 0, (wx.EXPAND if expand else 0) | wx.ALIGN_CENTER_VERTICAL)
            return ctrl

        self.detail_part = _info_field("Part", expand=True)
        self.detail_lcsc = _info_field("LCSC", expand=True)
        self.detail_brand = _info_field("Brand", expand=True)
        self.detail_package = _info_field("Package", expand=True)
        self.detail_price = _info_field("Price")
        self.detail_stock = _info_field("Stock")

        info_sizer.Add(detail_grid, 0, wx.EXPAND | wx.BOTTOM, 4)

        self.detail_desc = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE
        )
        self.detail_desc.SetMinSize((-1, 48))
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
        import_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Destination")

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

        # Row 1: Project destination
        proj_row = wx.BoxSizer(wx.HORIZONTAL)
        self.dest_project = wx.RadioButton(panel, label="Project", style=wx.RB_GROUP)
        self.dest_project.Bind(wx.EVT_RADIOBUTTON, self._on_dest_change)
        proj_row.Add(self.dest_project, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        proj_path_label = wx.StaticText(panel, label=project_dir or "(no board open)")
        proj_path_label.SetFont(bold_font)
        proj_row.Add(proj_path_label, 0, wx.ALIGN_CENTER_VERTICAL)
        import_box.Add(proj_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        # Row 2: Global destination | Browse | Reset
        global_row = wx.BoxSizer(wx.HORIZONTAL)
        self.dest_global = wx.RadioButton(panel, label="Global")
        self.dest_global.Bind(wx.EVT_RADIOBUTTON, self._on_dest_change)
        global_row.Add(self.dest_global, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._global_path_label = wx.StaticText(panel, label=self._truncate_path(global_dir))
        self._global_path_label.SetFont(bold_font)
        self._global_path_label.SetToolTip(global_dir)
        global_row.Add(self._global_path_label, 0, wx.ALIGN_CENTER_VERTICAL)
        self._global_browse_btn = wx.Button(panel, label="...", style=wx.BU_EXACTFIT)
        self._global_browse_btn.Bind(wx.EVT_BUTTON, self._on_global_browse)
        global_row.Add(self._global_browse_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)
        self._global_reset_btn = wx.Button(panel, label="\u2715", style=wx.BU_EXACTFIT)
        self._global_reset_btn.Bind(wx.EVT_BUTTON, self._on_global_reset)
        global_row.Add(self._global_reset_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 2)
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
        self._version_label = wx.StaticText(panel, label="KiCad")
        lib_name_sizer.Add(self._version_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._version_labels = [str(v) for v in sorted(SUPPORTED_VERSIONS, reverse=True)]
        self.version_choice = wx.Choice(panel, choices=self._version_labels)
        default_idx = self._version_labels.index(str(self._kicad_version))
        self.version_choice.SetSelection(default_idx)
        self.version_choice.Bind(wx.EVT_CHOICE, self._on_version_change)
        lib_name_sizer.Add(self.version_choice, 0, wx.ALIGN_CENTER_VERTICAL)
        import_box.Add(lib_name_sizer, 0, wx.ALL, 5)
        self._update_version_visibility()

        vbox.Add(import_box, 0, wx.EXPAND | wx.ALL, 5)

        # --- Status log ---
        self.status_text = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.status_text.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.status_text.SetMinSize((-1, 60))
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

        # Navigation row: [<] image+dots [>]
        nav_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._gallery_prev = wx.Button(self._gallery_panel, label="\u25c0", style=wx.BU_EXACTFIT)
        self._gallery_prev.Bind(wx.EVT_BUTTON, self._on_gallery_prev)
        nav_sizer.Add(self._gallery_prev, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)

        # Vertical stack: image bitmap + page dots (keeps dots tight to image)
        img_stack = wx.BoxSizer(wx.VERTICAL)
        self._gallery_image = wx.StaticBitmap(self._gallery_panel)
        self._gallery_image.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self._gallery_image.Bind(wx.EVT_LEFT_DOWN, self._on_gallery_close)
        img_stack.Add(self._gallery_image, 0, wx.ALIGN_CENTER_HORIZONTAL)
        self._gallery_page_indicator = _PageIndicator(self._gallery_panel, on_page_change=self._on_gallery_page_change)
        img_stack.Add(self._gallery_page_indicator, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 2)
        nav_sizer.Add(img_stack, 1, wx.ALIGN_CENTER_VERTICAL)

        self._gallery_next = wx.Button(self._gallery_panel, label="\u25b6", style=wx.BU_EXACTFIT)
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

        # Spinner overlays (children of the main panel, transparent background)
        self._busy_overlay = _SpinnerOverlay(panel)
        self._search_overlay = _SpinnerOverlay(panel, target=self.results_list)

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

    def _update_version_visibility(self):
        """Show KiCad version dropdown only when using the default 3rd-party directory."""
        config = load_config()
        custom = config.get("global_lib_dir", "") or self._global_lib_dir_override
        show = not custom
        self._version_label.Show(show)
        self.version_choice.Show(show)
        self._version_label.GetParent().Layout()

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
            self._update_version_visibility()
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
        self._update_version_visibility()

    def _log(self, msg: str):
        if self._closing:
            return
        self.status_text.AppendText(msg + "\n")

    def _handle_ssl_cert_error(self):
        """Show a one-time SSL warning and enable unverified HTTPS."""
        if not self._ssl_warning_shown:
            self._ssl_warning_shown = True
            if not self._closing:
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
        height = min(len(matches), 10) * self._category_popup.item_height()
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
        self._clear_detail()
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
        search_fn = self._get_search_fn()
        self._start_search_pulse()
        self._search_overlay.show()
        threading.Thread(
            target=self._fetch_search_results,
            args=(keyword, request_id, search_fn),
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

    def _get_search_fn(self):
        """Return the search function for the currently selected region."""
        idx = self.region_choice.GetSelection()
        return search_components_cn if self._region_values[idx] == "cn" else search_components

    def _fetch_search_results(self, keyword, request_id, search_fn):
        """Background thread: fetch search results from API."""
        try:
            try:
                result = search_fn(keyword, page_size=500)
            except SSLCertError:
                self._handle_ssl_cert_error()
                result = search_fn(keyword, page_size=500)
            if not self._closing:
                wx.CallAfter(self._on_search_complete, result, request_id)
        except APIError as e:
            if not self._closing:
                wx.CallAfter(self._on_search_error, f"Search error: {e}", request_id)
        except Exception as e:
            if not self._closing:
                wx.CallAfter(self._on_search_error, f"Unexpected error: {type(e).__name__}: {e}", request_id)

    def _on_search_complete(self, result, request_id):
        """Handle search results on the main thread."""
        if request_id != self._search_request_id:
            return
        self._stop_search_pulse()
        self._search_overlay.dismiss()

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
        self._search_overlay.dismiss()
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
            4: lambda r: r.get("brand", "").lower(),
            5: lambda r: r.get("model", "").lower(),
            6: lambda r: r.get("package", "").lower(),
            7: lambda r: r.get("description", "").lower(),
        }
        key_fn = key_map.get(col)
        if key_fn:
            self._search_results.sort(key=key_fn, reverse=not self._sort_ascending)
            self._update_col_headers()
            self._repopulate_results()

    _col_names = ["LCSC", "Type", "Price", "Stock", "Brand", "Part", "Package", "Description"]

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

    def _on_region_change(self, event):
        """Persist region choice when user changes it."""
        idx = self.region_choice.GetSelection()
        region = self._region_values[idx]
        cfg = load_config()
        cfg["region"] = region
        save_config(cfg)
        hint = "Search SZLCSC parts..." if region == "cn" else "Search JLCPCB parts..."
        self.search_input.SetHint(hint)

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
        reselect_idx = -1
        for i, r in enumerate(self._search_results):
            lcsc = r["lcsc"]
            if self._selected_result and lcsc == self._selected_result["lcsc"]:
                reselect_idx = i
            prefix = "\u2713 " if lcsc in self._imported_ids else ""
            self.results_list.InsertItem(i, prefix + lcsc)
            self.results_list.SetItem(i, 1, r["type"])
            price_str = f"{r.get('currency', '$')}{r['price']:.4f}" if r["price"] else "N/A"
            self.results_list.SetItem(i, 2, price_str)
            stock_str = f"{r['stock']:,}" if r["stock"] else "N/A"
            self.results_list.SetItem(i, 3, stock_str)
            self.results_list.SetItem(i, 4, r.get("brand", ""))
            self.results_list.SetItem(i, 5, r["model"])
            self.results_list.SetItem(i, 6, r.get("package", ""))
            self.results_list.SetItem(i, 7, r.get("description", ""))
        self._update_results_count()
        if reselect_idx >= 0:
            self.results_list.Select(reselect_idx)
        elif len(self._search_results) == 1:
            self.results_list.Select(0)
        elif self._selected_result:
            self._clear_detail()

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

    def _clear_detail(self):
        """Clear the detail panel when nothing is selected."""
        self._selected_result = None
        self._stop_skeleton()
        self._image_request_id += 1  # cancel any in-flight image fetch
        self._symbol_request_id += 1  # cancel any in-flight symbol fetch
        self._photo_bitmap = None
        self._symbol_bitmap = None
        self._symbol_svg_string = None
        self._detail_page = 0
        self._page_indicator.set_page(0)
        self.detail_lcsc.SetValue("")
        self.detail_part.SetValue("")
        self.detail_brand.SetValue("")
        self.detail_package.SetValue("")
        self.detail_price.SetValue("")
        self.detail_stock.SetValue("")
        self.detail_desc.SetValue("")
        self._show_no_image()
        self._datasheet_url = ""
        self._lcsc_page_url = ""
        self.detail_import_btn.Disable()
        self.detail_datasheet_btn.Disable()
        self.detail_lcsc_btn.Disable()

    def _on_result_select(self, event):
        """Select a search result and show details."""
        idx = event.GetIndex()
        if idx < 0 or idx >= len(self._search_results):
            return
        r = self._search_results[idx]
        if self._selected_result and r["lcsc"] == self._selected_result["lcsc"]:
            return  # same item already displayed
        self._selected_result = r
        self._has_easyeda_data = True  # assume yes until the API says otherwise

        # Clear cached bitmaps but keep current page selection
        self._photo_bitmap = None
        self._symbol_bitmap = None
        self._symbol_svg_string = None

        # Populate detail fields
        self.detail_lcsc.SetValue(f"{r['lcsc']}  ({r['type']})")
        self.detail_part.SetValue(r["model"])
        self.detail_brand.SetValue(r["brand"])
        self.detail_package.SetValue(r["package"])
        price_str = f"{r.get('currency', '$')}{r['price']:.4f}" if r["price"] else "N/A"
        self.detail_price.SetValue(price_str)
        stock_str = f"{r['stock']:,}" if r["stock"] else "N/A"
        self.detail_stock.SetValue(stock_str)
        self.detail_desc.SetValue(r["description"])

        self._datasheet_url = r.get("datasheet", "")
        self.detail_datasheet_btn.Enable(bool(self._datasheet_url))

        self._lcsc_page_url = r.get("url", "")
        self.detail_lcsc_btn.Enable(bool(self._lcsc_page_url))
        self.detail_lcsc_btn.SetLabel("SZLCSC Page" if "szlcsc.com" in self._lcsc_page_url else "LCSC Page")

        self.detail_import_btn.Enable()

        # Fetch image in background
        lcsc_url = r.get("url", "")
        image_url = r.get("image_url", "")
        self._image_request_id += 1
        request_id = self._image_request_id
        if lcsc_url or image_url:
            if self._detail_page == 0:
                self._show_skeleton()
            threading.Thread(target=self._fetch_image, args=(lcsc_url, image_url, request_id), daemon=True).start()
        else:
            self._stop_skeleton()
            if self._detail_page == 0:
                self._show_no_image()

        # Fetch symbol data in background
        lcsc_id = r["lcsc"]
        self._symbol_request_id += 1
        sym_request_id = self._symbol_request_id
        if self._detail_page == 1:
            self._show_no_footprint()
        threading.Thread(
            target=self._fetch_footprint_svg,
            args=(lcsc_id, sym_request_id),
            daemon=True,
        ).start()

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
        self._photo_bitmap = None
        bmp = wx.Bitmap(160, 160)
        dc = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(245, 245, 245)))
        dc.Clear()
        # Draw a subtle image icon (rectangle with mountain/sun)
        dc.SetPen(wx.Pen(wx.Colour(200, 200, 200), 1))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRoundedRectangle(55, 60, 50, 40, 4)
        # Mountain shape
        dc.SetPen(wx.Pen(wx.Colour(200, 200, 200), 1))
        dc.DrawLine(62, 92, 80, 78)
        dc.DrawLine(80, 78, 88, 85)
        dc.DrawLine(88, 85, 98, 75)
        # Sun circle
        dc.DrawCircle(90, 70, 5)
        dc.SelectObject(wx.NullBitmap)
        self.detail_image.SetBitmap(bmp)

    def _on_skeleton_tick(self, event):
        """Advance skeleton animation."""
        if self._detail_page != 0:
            return
        self._skeleton_phase = (self._skeleton_phase + 3) % 200
        self._draw_skeleton_frame()

    def _draw_skeleton_frame(self):
        """Draw one frame of the skeleton shimmer over a rounded rect."""

        bmp = wx.Bitmap(160, 160)
        dc = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(240, 240, 240)))
        dc.Clear()

        # Draw the base rounded rectangle
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(wx.Colour(225, 225, 225)))
        dc.DrawRoundedRectangle(4, 4, 152, 152, 6)

        # Shimmer: a soft gradient band sweeping left to right
        phase = self._skeleton_phase
        band_center = phase - 50  # range: -50 to 150
        band_width = 60

        for x in range(4, 156):
            dist = abs(x - band_center)
            if dist < band_width // 2:
                # Smooth falloff using cosine
                t = dist / (band_width / 2.0)
                alpha = int(25 * (1 + math.cos(t * math.pi)) / 2)
                if alpha > 0:
                    c = min(255, 225 + alpha)
                    dc.SetPen(wx.Pen(wx.Colour(c, c, c), 1))
                    dc.DrawLine(x, 4, x, 156)

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
        # Transfer current page (photo/footprint) to gallery
        self._gallery_page = self._detail_page
        self._gallery_page_indicator.set_page(self._gallery_page)
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
        price_str = f"{r.get('currency', '$')}{r['price']:.4f}" if r["price"] else "N/A"
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

        # Reset gallery caches
        self._gallery_photo_bitmap = None
        self._gallery_svg_string = None

        # Show skeleton while loading
        self._show_gallery_skeleton()

        # Fetch photo image
        lcsc_url = r.get("url", "")
        image_url = r.get("image_url", "")
        self._gallery_request_id += 1
        request_id = self._gallery_request_id
        if lcsc_url or image_url:
            threading.Thread(
                target=self._fetch_gallery_image, args=(lcsc_url, image_url, request_id), daemon=True
            ).start()
        else:
            if self._gallery_page == 0:
                self._stop_gallery_skeleton()
                self._show_gallery_no_image()

        # Fetch footprint SVG
        lcsc_id = r["lcsc"]
        self._gallery_svg_request_id += 1
        svg_request_id = self._gallery_svg_request_id
        threading.Thread(
            target=self._fetch_gallery_svg,
            args=(lcsc_id, svg_request_id),
            daemon=True,
        ).start()

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

    def _show_gallery_no_footprint(self):
        """Show a footprint placeholder in gallery."""
        size = self._get_gallery_image_size()
        self._gallery_image.SetBitmap(_no_footprint_placeholder(size, not has_svg_support()))
        self._gallery_panel.Layout()

    def _show_gallery_footprint(self):
        """Render and display footprint SVG at gallery size."""
        if not self._gallery_svg_string:
            self._show_gallery_no_footprint()
            return
        size = self._get_gallery_image_size()
        bmp = render_svg_bitmap(self._gallery_svg_string, size=size)
        if bmp:
            self._gallery_image.SetBitmap(bmp)
            self._gallery_panel.Layout()
        else:
            self._show_gallery_no_footprint()

    def _fetch_gallery_svg(self, lcsc_id, request_id):
        """Fetch footprint SVG in background for gallery view."""
        try:
            try:
                uuids = fetch_component_uuids(lcsc_id)
            except SSLCertError:
                self._handle_ssl_cert_error()
                uuids = fetch_component_uuids(lcsc_id)
            svg_string = uuids[-1].get("svg", "") if uuids else ""
            if not self._closing and self._gallery_svg_request_id == request_id and svg_string:
                wx.CallAfter(self._set_gallery_svg, svg_string, request_id)
        except APIError:
            if not self._closing and self._gallery_svg_request_id == request_id:
                wx.CallAfter(self._on_no_easyeda_data, self._symbol_request_id)
        except Exception:
            pass  # Footprint preview is best-effort

    def _set_gallery_svg(self, svg_string, request_id):
        """Set gallery footprint SVG on main thread."""
        if self._gallery_svg_request_id != request_id:
            return
        self._gallery_svg_string = svg_string
        if self._gallery_page == 1:
            self._stop_gallery_skeleton()
            self._show_gallery_footprint()

    def _on_gallery_page_change(self, page):
        """Handle gallery page indicator click to switch photo/footprint."""
        self._gallery_page = page
        if page == 0:
            if self._gallery_photo_bitmap:
                self._gallery_image.SetBitmap(self._gallery_photo_bitmap)
                self._gallery_panel.Layout()
            else:
                self._show_gallery_no_image()
        else:
            if self._gallery_svg_string:
                self._show_gallery_footprint()
            else:
                self._show_gallery_no_footprint()

    def _get_gallery_image_size(self):
        """Get the max square image size for the gallery."""
        w, h = self.GetClientSize()
        return max(min(w - 100, h - 120), 100)

    def _fetch_gallery_image(self, lcsc_url, image_url, request_id):
        """Fetch full-size image for gallery."""
        try:
            try:
                img_data = fetch_product_image(lcsc_url, direct_image_url=image_url)
            except SSLCertError:
                self._handle_ssl_cert_error()
                img_data = fetch_product_image(lcsc_url, direct_image_url=image_url)
        except Exception:
            img_data = None
        if not self._closing and self._gallery_request_id == request_id:
            wx.CallAfter(self._set_gallery_image, img_data, request_id)

    def _set_gallery_image(self, img_data, request_id):
        """Set gallery image on main thread."""
        if self._gallery_request_id != request_id:
            return
        if not img_data:
            self._gallery_photo_bitmap = None
            if self._gallery_page == 0:
                self._stop_gallery_skeleton()
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
                bmp = wx.Bitmap(img.Scale(int(w * scale), int(h * scale), wx.IMAGE_QUALITY_HIGH))
                self._gallery_photo_bitmap = bmp
                if self._gallery_page == 0:
                    self._stop_gallery_skeleton()
                    self._gallery_image.SetBitmap(bmp)
                    self._gallery_panel.Layout()
            else:
                self._gallery_photo_bitmap = None
                if self._gallery_page == 0:
                    self._stop_gallery_skeleton()
                    self._show_gallery_no_image()
        except Exception:
            self._gallery_photo_bitmap = None
            if self._gallery_page == 0:
                self._stop_gallery_skeleton()
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
        key = event.GetKeyCode()
        if self._gallery_panel.IsShown():
            if key == wx.WXK_ESCAPE:
                self._exit_gallery()
                return
            elif key == wx.WXK_LEFT:
                self._on_gallery_prev(None)
                return
            elif key == wx.WXK_RIGHT:
                self._on_gallery_next(None)
                return
        elif key == wx.WXK_ESCAPE:
            self.Close()
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

    def _fetch_image(self, lcsc_url, image_url, request_id):
        """Fetch product image in background thread."""
        try:
            try:
                img_data = fetch_product_image(lcsc_url, direct_image_url=image_url)
            except SSLCertError:
                self._handle_ssl_cert_error()
                img_data = fetch_product_image(lcsc_url, direct_image_url=image_url)
        except Exception:
            img_data = None
        if not self._closing and self._image_request_id == request_id:
            wx.CallAfter(self._set_image, img_data, request_id)

    def _set_image(self, img_data, request_id):
        """Set the detail image from raw bytes (called on main thread)."""
        if self._image_request_id != request_id:
            return  # User selected a different item
        self._stop_skeleton()
        if not self._has_easyeda_data:
            return  # placeholder already showing
        if not img_data:
            self._full_image_data = None
            self._photo_bitmap = None
            if self._detail_page == 0:
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
                thumb = img.Scale(160, 160, wx.IMAGE_QUALITY_HIGH)
                self._photo_bitmap = wx.Bitmap(thumb)
                if self._detail_page == 0:
                    self.detail_image.SetBitmap(self._photo_bitmap)
            else:
                self._full_image_data = None
                self._photo_bitmap = None
                if self._detail_page == 0:
                    self._show_no_image()
            self.Layout()
        except Exception:
            self._full_image_data = None
            self._photo_bitmap = None
            if self._detail_page == 0:
                self._show_no_image()
            self.Layout()

    def _fetch_footprint_svg(self, lcsc_id, request_id):
        """Background thread: fetch footprint SVG from the API."""
        try:
            try:
                uuids = fetch_component_uuids(lcsc_id)
            except SSLCertError:
                self._handle_ssl_cert_error()
                uuids = fetch_component_uuids(lcsc_id)

            # Last entry is the footprint
            svg_string = uuids[-1].get("svg", "") if uuids else ""

            if not self._closing and self._symbol_request_id == request_id and svg_string:
                wx.CallAfter(self._set_footprint_svg, svg_string, request_id)
        except APIError:
            if not self._closing and self._symbol_request_id == request_id:
                wx.CallAfter(self._on_no_easyeda_data, request_id)
        except Exception:
            pass  # Footprint preview is best-effort

    def _set_footprint_svg(self, svg_string, request_id):
        """Main thread: render the footprint SVG and cache it."""
        if self._symbol_request_id != request_id:
            return
        self._symbol_svg_string = svg_string
        self._symbol_bitmap = render_svg_bitmap(svg_string)
        if self._detail_page == 1:
            if self._symbol_bitmap:
                self.detail_image.SetBitmap(self._symbol_bitmap)
            else:
                self._show_no_footprint()

    def _on_no_easyeda_data(self, request_id):
        """Main thread: the API has no schematic/footprint for this part."""
        if self._symbol_request_id != request_id:
            return
        self._has_easyeda_data = False
        self.detail_import_btn.Disable()
        self._stop_skeleton()
        bmp = _no_easyeda_placeholder(160)
        self._photo_bitmap = bmp
        self._symbol_bitmap = bmp
        self.detail_image.SetBitmap(bmp)

    def _on_page_change(self, page):
        """Handle page indicator click to switch between photo and symbol."""
        self._detail_page = page
        if page == 0:
            if self._photo_bitmap:
                self.detail_image.SetBitmap(self._photo_bitmap)
            else:
                self._show_no_image()
        else:
            if self._symbol_bitmap:
                self.detail_image.SetBitmap(self._symbol_bitmap)
            else:
                self._show_no_footprint()

    def _show_no_footprint(self):
        """Show a placeholder when footprint preview is unavailable."""
        self.detail_image.SetBitmap(_no_footprint_placeholder(160, not has_svg_support()))

    def _on_import(self, event):
        if not self._selected_result:
            self._log("Error: Select a search result first")
            return

        lcsc_id = self._selected_result["lcsc"]

        use_global = self.dest_global.GetValue()
        if use_global:
            lib_dir = self._global_lib_dir
        else:
            lib_dir = self._get_project_dir()
            if not lib_dir:
                self._log("Error: No board file open. Use Global destination or open a board.")
                return

        self.status_text.Clear()
        self._main_panel.Disable()
        self._busy_overlay.show()

        search_result = self._selected_result
        lib_name = self._lib_name
        kicad_version = self._get_kicad_version()

        threading.Thread(
            target=self._import_worker,
            args=(lcsc_id, lib_dir, lib_name, use_global, search_result, kicad_version),
            daemon=True,
        ).start()

    def _import_worker(self, lcsc_id, lib_dir, lib_name, use_global, search_result, kicad_version):
        """Background thread: run the import."""
        _dispatched = False
        try:
            try:
                result = self._do_import(lcsc_id, lib_dir, lib_name, use_global, search_result, kicad_version)
            except SSLCertError:
                self._handle_ssl_cert_error()
                result = self._do_import(lcsc_id, lib_dir, lib_name, use_global, search_result, kicad_version)
            if not self._closing:
                _dispatched = True
                wx.CallAfter(self._on_import_complete, result)
        except APIError as e:
            if not self._closing:
                _dispatched = True
                wx.CallAfter(self._on_import_error, f"API Error: {e}")
        except Exception as e:
            if not self._closing:
                _dispatched = True
                wx.CallAfter(self._on_import_error, f"Error: {e}\n{traceback.format_exc()}")
        finally:
            # Guarantee the overlay is always dismissed even if an unexpected
            # exception bypassed all the error handlers above.
            if not _dispatched and not self._closing:
                wx.CallAfter(self._on_import_error, "Import failed unexpectedly.")

    def _on_import_complete(self, result):
        """Main thread: handle successful import completion."""
        if self._closing:
            return
        self._busy_overlay.dismiss()
        self._main_panel.Enable()
        if result is None:
            self._log("Import cancelled.")
        else:
            title = result["title"]
            name = result["name"]
            self._log(f"\nDone! '{title}' imported as {self._lib_name}:{name}")
            self._refresh_imported_ids()
            self._repopulate_results()
            self._persist_destination()

    def _on_import_error(self, msg):
        """Main thread: handle import error."""
        if self._closing:
            return
        self._busy_overlay.dismiss()
        self._main_panel.Enable()
        self._log(msg)

    def _get_kicad_version(self) -> int:
        """Return the selected KiCad version from the dropdown."""
        idx = self.version_choice.GetSelection()
        return int(self._version_labels[idx])

    def _confirm_metadata(self, metadata: dict) -> dict | None:
        """Show the metadata edit dialog and return edited values, or None to cancel."""
        dlg = MetadataEditDialog(self, metadata)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                return dlg.get_metadata()
            return None
        finally:
            dlg.Destroy()

    def _confirm_overwrite(self, name, existing):
        items = ", ".join(existing)
        msg = f"'{name}' already exists ({items}). Overwrite?"
        dlg = wx.MessageDialog(self, msg, "Confirm Overwrite", wx.YES_NO | wx.ICON_QUESTION)
        result = dlg.ShowModal() == wx.ID_YES
        dlg.Destroy()
        return result

    def _do_import(self, lcsc_id, lib_dir, lib_name, use_global, search_result, kicad_version):
        """Run import_component on a background thread with thread-safe callbacks."""

        def log(msg):
            if not self._closing:
                wx.CallAfter(self._log, msg)

        def confirm_metadata(metadata):
            result = [None]
            done = threading.Event()

            def _ask():
                try:
                    if self._closing:
                        return
                    self._main_panel.Enable()
                    self._busy_overlay.dismiss()
                    result[0] = self._confirm_metadata(metadata)
                    if not self._closing:
                        self._main_panel.Disable()
                        self._busy_overlay.show()
                finally:
                    done.set()

            # Stop the timer BEFORE posting — the 25 ms EVT_TIMER/EVT_PAINT flood
            # (and status_text.Update() reentrancy) can starve CallAfter callbacks.
            self._busy_overlay.pause()
            wx.CallAfter(_ask)
            done.wait()
            return result[0]

        def confirm_overwrite(name, existing):
            result = [False]
            done = threading.Event()

            def _ask():
                try:
                    if self._closing:
                        return
                    self._main_panel.Enable()
                    self._busy_overlay.dismiss()
                    result[0] = self._confirm_overwrite(name, existing)
                    if not self._closing:
                        self._main_panel.Disable()
                        self._busy_overlay.show()
                finally:
                    done.set()

            self._busy_overlay.pause()
            wx.CallAfter(_ask)
            done.wait()
            return result[0]

        return import_component(
            lcsc_id,
            lib_dir,
            lib_name,
            overwrite=False,
            use_global=use_global,
            log=log,
            kicad_version=kicad_version,
            search_result=search_result,
            confirm_metadata=confirm_metadata,
            confirm_overwrite=confirm_overwrite,
        )
