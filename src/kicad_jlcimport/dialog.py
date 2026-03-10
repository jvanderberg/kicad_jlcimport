"""wxPython dialog for JLCImport plugin.

Three top-level UI classes:
  JLCImportDialog       – main search / results / import dialog (plugin + standalone).
  FootprintBrowserDialog – three-pane library browser with live 2D footprint preview.
  MetadataEditDialog    – edits description / keywords / manufacturer before import;
                          also lets the user choose an existing KiCad footprint.

Supporting classes:
  _FootprintPreviewPanel – owner-drawn 2D renderer for .kicad_mod geometry.
  _CategoryPopup         – owner-drawn autocomplete popup for the search field.
  _PageIndicator         – two-dot page switcher (photo ↔ footprint-SVG).
  _SpinnerOverlay        – transparent animated spinner shown during long operations.
"""

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
)
from .gui.symbol_renderer import has_svg_support, render_svg_bitmap
from .importer import import_component
from .kicad.library import get_global_lib_dir, load_config, save_config
from .kicad.version import DEFAULT_KICAD_VERSION, SUPPORTED_VERSIONS


# ===========================================================================
# KiCad layer colours — "kicad_default" dark theme, matching the PCB editor.
# ===========================================================================

_LAYER_COLOURS: dict[str, tuple[int, int, int]] = {
    "F.Cu":        (200,  52,  52),
    "B.Cu":        ( 77, 127, 196),
    "F.SilkS":     (242, 237, 161),
    "B.SilkS":     (232, 178, 167),
    "F.Fab":       (175, 175, 175),
    "B.Fab":       ( 99,  99,  99),
    "F.Courtyard": (255,  38, 226),   # KiCad 8+ canonical name
    "B.Courtyard": ( 38, 233, 255),
    "F.CrtYd":     (255,  38, 226),   # KiCad ≤7 alias — same colour
    "B.CrtYd":     ( 38, 233, 255),
    "F.Paste":     (180,  60, 180),
    "B.Paste":     ( 60, 180, 180),
    "F.Mask":      (255, 100, 150),
    "B.Mask":      ( 70, 140, 255),
    "Edge.Cuts":   (255, 241,  52),
    "Cmts.User":   ( 99,  99,  99),
    "Eco1.User":   ( 99, 182,  44),
    "Eco2.User":   (153,  71,  71),
    "User.1":      (206, 206, 206),
    "User.2":      (160, 160, 160),
}
_DEFAULT_LAYER_COLOUR = (128, 128, 128)

# Back layers first so front layers paint on top; pads are drawn separately last.
_LAYER_ORDER = [
    "B.CrtYd", "B.Courtyard", "B.Fab", "B.SilkS", "B.Cu", "B.Paste", "B.Mask",
    "Edge.Cuts", "Cmts.User", "User.1", "User.2", "Eco1.User", "Eco2.User",
    "F.CrtYd", "F.Courtyard", "F.Fab", "F.SilkS", "F.Cu", "F.Paste", "F.Mask",
]

# Sub-pixel strokes disappear on some backends; clamp to this minimum.
_MIN_STROKE_PX = 1.5


# ===========================================================================
# .kicad_mod parser — pure regex, no external deps, KiCad 8/9/10 only
# ===========================================================================

def _extract_blocks(text: str, keyword: str) -> list[str]:
    """Extract all top-level ``(keyword …)`` S-expression blocks from *text*.

    Uses bracket counting so nested parens inside a block (e.g. drill
    primitives inside a pad) don't truncate the match prematurely.
    """
    results: list[str] = []
    for m in re.finditer(rf"\({keyword}\b", text):
        start, depth, i = m.start(), 0, m.start()
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    results.append(text[start:i + 1])
                    break
            i += 1
    return results


def _parse_kicad_mod(path: str) -> dict:
    """Parse a KiCad 8/9/10 ``.kicad_mod`` file into geometry lists.

    Returns a dict with:
      lines   – ``[((x1,y1),(x2,y2), layer, width_mm), …]``
      rects   – ``[(x1,y1,x2,y2, layer, width_mm, corner_r, filled), …]``
      circles – ``[(cx,cy,r, layer, width_mm, filled), …]``
      arcs    – ``[(sx,sy,mx,my,ex,ey, layer, width_mm), …]``
      polys   – ``[([(x,y),…], layer, filled), …]``
      pads    – ``[(num,x,y,w,h,shape,rotation,pad_type,drill_d), …]``
      model   – ``(raw_path, exists:bool)`` or ``None``
      descr, tags, pads_count
    """
    N = r"[\d.eE+\-]+"   # numeric token pattern

    result: dict = {
        "lines": [], "rects": [], "circles": [], "arcs": [], "polys": [],
        "pads": [], "model": None, "descr": "", "tags": "", "pads_count": 0,
    }

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return result

    def _f(s: str) -> float:
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    def _field(name: str) -> str:
        m = re.search(rf'\({name}\s+"([^"]*)"\)', text)
        return m.group(1) if m else ""

    def _layer(block: str) -> str:
        m = re.search(r'\(layer\s+"([^"]+)"\)', block)
        return m.group(1) if m else ""

    def _stroke_width(block: str) -> float:
        m = re.search(rf'\(stroke\s*\(width\s+({N})\)', block, re.DOTALL)
        return _f(m.group(1)) if m else 0.1

    result["descr"] = _field("descr")
    result["tags"]  = _field("tags")

    # fp_line
    for block in _extract_blocks(text, "fp_line"):
        c = re.search(
            rf'\(start\s+({N})\s+({N})\)\s*\(end\s+({N})\s+({N})\)',
            block, re.DOTALL,
        )
        layer = _layer(block)
        if c and layer:
            result["lines"].append((
                (_f(c.group(1)), _f(c.group(2))),
                (_f(c.group(3)), _f(c.group(4))),
                layer, _stroke_width(block),
            ))

    # fp_rect  (KiCad 8+)
    for block in _extract_blocks(text, "fp_rect"):
        s = re.search(rf'\(start\s+({N})\s+({N})\)', block)
        e = re.search(rf'\(end\s+({N})\s+({N})\)', block)
        layer = _layer(block)
        if s and e and layer:
            cr = re.search(rf'\(corner_radius\s+({N})\)', block)
            filled = "(fill solid)" in block or "(fill yes)" in block
            result["rects"].append((
                _f(s.group(1)), _f(s.group(2)),
                _f(e.group(1)), _f(e.group(2)),
                layer, _stroke_width(block),
                _f(cr.group(1)) if cr else 0.0,
                filled,
            ))

    # fp_circle
    for block in _extract_blocks(text, "fp_circle"):
        cx_m = re.search(rf'\(center\s+({N})\s+({N})\)', block)
        en_m = re.search(rf'\(end\s+({N})\s+({N})\)', block)
        layer = _layer(block)
        if cx_m and en_m and layer:
            cx, cy = _f(cx_m.group(1)), _f(cx_m.group(2))
            radius = math.hypot(_f(en_m.group(1)) - cx, _f(en_m.group(2)) - cy)
            filled = "(fill solid)" in block or "(fill yes)" in block
            result["circles"].append((cx, cy, radius, layer, _stroke_width(block), filled))

    # fp_arc  (start / mid / end, KiCad 8+)
    for block in _extract_blocks(text, "fp_arc"):
        layer = _layer(block)
        if not layer:
            continue
        s = re.search(rf'\(start\s+({N})\s+({N})\)', block)
        m = re.search(rf'\(mid\s+({N})\s+({N})\)',   block)
        e = re.search(rf'\(end\s+({N})\s+({N})\)',   block)
        if s and m and e:
            result["arcs"].append((
                _f(s.group(1)), _f(s.group(2)),
                _f(m.group(1)), _f(m.group(2)),
                _f(e.group(1)), _f(e.group(2)),
                layer, _stroke_width(block),
            ))

    # fp_poly
    for block in _extract_blocks(text, "fp_poly"):
        layer = _layer(block)
        if not layer:
            continue
        # Use bracket-counting to find the (pts …) sub-block; a greedy regex
        # would stop at the first nested close-paren.
        pts_blocks = _extract_blocks(block, "pts")
        if not pts_blocks:
            continue
        pts = [
            (_f(p.group(1)), _f(p.group(2)))
            for p in re.finditer(rf"\(xy\s+({N})\s+({N})\)", pts_blocks[0])
        ]
        if pts:
            filled = "(fill solid)" in block or "(fill yes)" in block
            result["polys"].append((pts, layer, filled))

    # pads — use separate patterns so attribute order inside a pad block
    # doesn't matter (drill sometimes precedes size in ThermalVias footprints).
    _head_pat  = re.compile(rf'\(pad\s+"([^"]*)"\s+(\w+)\s+(\w+)', re.DOTALL)
    _at_pat    = re.compile(rf'\(at\s+({N})\s+({N})(?:\s+({N}))?\)', re.DOTALL)
    _size_pat  = re.compile(rf'\(size\s+({N})\s+({N})\)', re.DOTALL)
    _drill_pat = re.compile(rf'\(drill(?:\s+oval)?\s+({N})', re.DOTALL)

    for block in _extract_blocks(text, "pad"):
        h = _head_pat.search(block)
        a = _at_pat.search(block)
        s = _size_pat.search(block)
        if not (h and a and s):
            continue
        d = _drill_pat.search(block)
        result["pads"].append((
            h.group(1),                                # pad number
            _f(a.group(1)), _f(a.group(2)),            # x, y
            _f(s.group(1)), _f(s.group(2)),            # width, height
            h.group(3),                                # shape
            _f(a.group(3)) if a.group(3) else 0.0,    # rotation
            h.group(2),                                # pad_type
            _f(d.group(1)) if d else 0.0,             # drill diameter
        ))

    result["pads_count"] = len(result["pads"])

    # 3D model — expand common KiCad path variables
    m3d = re.search(r'\(model\s+"([^"]+)"', text)
    if m3d:
        raw_path = m3d.group(1).strip()
        resolved = raw_path
        for var, env_key in (
            ("${KICAD9_3DMODEL_DIR}", "KICAD9_3DMODEL_DIR"),
            ("${KICAD8_3DMODEL_DIR}", "KICAD8_3DMODEL_DIR"),
            ("${KICAD7_3DMODEL_DIR}", "KICAD7_3DMODEL_DIR"),
            ("${KIPRJMOD}",           "KIPRJMOD"),
        ):
            val = os.environ.get(env_key, "")
            if val:
                resolved = resolved.replace(var, val)
        result["model"] = (raw_path, os.path.isfile(resolved))

    return result


# ===========================================================================
# _FootprintPreviewPanel
# ===========================================================================

def _no_footprint_placeholder(size: int, svg_unsupported: bool) -> wx.Bitmap:
    """Return a placeholder bitmap for the footprint preview slot.

    When *svg_unsupported* is True, shows explanatory text.  Otherwise draws
    simple pad outlines as a generic icon.
    """
    bmp = wx.Bitmap(size, size)
    dc  = wx.MemoryDC(bmp)
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
        # Simple four-pad icon
        dc.SetPen(wx.Pen(wx.Colour(200, 200, 200), max(1, size // 80)))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        cx, cy = size // 2, size // 2
        pw = max(20, size // 8)
        ph = max(12, size // 14)
        gap = max(16, size // 6)
        for dx, dy in ((-gap // 2 - pw, -gap // 2 - ph),
                       (-gap // 2 - pw,  gap // 2),
                       ( gap // 2,       -gap // 2 - ph),
                       ( gap // 2,        gap // 2)):
            dc.DrawRoundedRectangle(cx + dx, cy + dy, pw, ph, 3)

    dc.SelectObject(wx.NullBitmap)
    return bmp


class _FootprintPreviewPanel(wx.Panel):
    """Owner-drawn 2D footprint renderer using KiCad layer colours.

    Renders lines, arcs, circles, rectangles, polygons, and pads from a
    parsed ``.kicad_mod`` geometry dict.  Supports:
      - Mouse-wheel zoom (zoom towards cursor)
      - Click-drag pan
      - Right-click or double-click to reset zoom-to-fit
    """

    # Pad fill colours mirror the copper layer, exactly as KiCad does.
    _PAD_FILL = {
        "smd":          wx.Colour(200,  52,  52, 230),   # F.Cu red, semi-transparent
        "thru_hole":    wx.Colour(200,  52,  52, 230),
        "np_thru_hole": wx.Colour( 60,  60,  60, 180),   # no copper — dark grey
    }
    _PAD_OUTLINE  = wx.Colour(255, 255, 255,  80)
    _DRILL_COLOUR = wx.Colour( 15,  15,  15)
    _TEXT_COLOUR  = wx.Colour(200, 200, 200)

    def __init__(self, parent: wx.Window) -> None:
        super().__init__(parent, style=wx.BORDER_SUNKEN)
        self._fp:     dict | None = None
        self._scale   = 10.0
        self._offset  = wx.Point(0, 0)
        self._dragging         = False
        self._drag_start       = wx.Point(0, 0)
        self._drag_offset_start = wx.Point(0, 0)

        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((320, 320))

        for event, handler in (
            (wx.EVT_PAINT,       self._on_paint),
            (wx.EVT_SIZE,        self._on_size),
            (wx.EVT_MOUSEWHEEL,  self._on_wheel),
            (wx.EVT_LEFT_DOWN,   self._on_ldown),
            (wx.EVT_LEFT_UP,     self._on_lup),
            (wx.EVT_MOTION,      self._on_motion),
            (wx.EVT_RIGHT_DOWN,  self._on_reset_zoom),
            (wx.EVT_LEFT_DCLICK, self._on_reset_zoom),
        ):
            self.Bind(event, handler)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, fp: dict | None) -> None:
        """Load a new footprint geometry dict and repaint."""
        self._fp = fp
        self._fit()
        self.Refresh()

    # ------------------------------------------------------------------
    # Zoom / pan helpers
    # ------------------------------------------------------------------

    def _all_points(self) -> list[tuple[float, float]]:
        """Collect all geometry bounding-box corner points for auto-fit."""
        if not self._fp:
            return []
        pts: list[tuple[float, float]] = []
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
        for _n, x, y, w, h, *_ in self._fp["pads"]:
            hw, hh = w / 2 + 0.2, h / 2 + 0.2
            pts += [(x - hw, y - hh), (x + hw, y + hh)]
        return pts

    def _fit(self) -> None:
        """Compute scale and offset to fit all geometry in the panel."""
        pts = self._all_points()
        w, h = self.GetClientSize()
        if w < 10:
            w = h = 320

        if not pts:
            self._scale  = 10.0
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

    def _px(self, x: float, y: float) -> tuple[float, float]:
        """Convert mm coordinates to panel pixels."""
        return x * self._scale + self._offset.x, y * self._scale + self._offset.y

    def _pxlen(self, mm: float) -> float:
        return mm * self._scale

    def _stroke_w(self, mm: float) -> float:
        return max(_MIN_STROKE_PX, self._pxlen(mm))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_size(self, event: wx.SizeEvent) -> None:
        self._fit()
        self.Refresh()
        event.Skip()

    def _on_reset_zoom(self, _event) -> None:
        self._fit()
        self.Refresh()

    def _on_wheel(self, event: wx.MouseEvent) -> None:
        factor = 1.15 if event.GetWheelRotation() > 0 else 1.0 / 1.15
        mx, my = event.GetX(), event.GetY()
        self._offset = wx.Point(
            int(mx + (self._offset.x - mx) * factor),
            int(my + (self._offset.y - my) * factor),
        )
        self._scale *= factor
        self.Refresh()

    def _on_ldown(self, event: wx.MouseEvent) -> None:
        self._dragging = True
        self._drag_start       = event.GetPosition()
        self._drag_offset_start = wx.Point(self._offset.x, self._offset.y)
        self.CaptureMouse()

    def _on_lup(self, _event) -> None:
        if self._dragging:
            self._dragging = False
            if self.HasCapture():
                self.ReleaseMouse()

    def _on_motion(self, event: wx.MouseEvent) -> None:
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

    def _on_paint(self, _event) -> None:
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

        # Origin crosshair
        ox, oy = self._px(0, 0)
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo().Colour(wx.Colour(55, 55, 55)).Width(1)))
        gc.StrokeLine(0, oy, w_px, oy)
        gc.StrokeLine(ox, 0, ox, h_px)

        # Bucket geometry by layer
        layer_lines:   dict[str, list] = {}
        layer_rects:   dict[str, list] = {}
        layer_circles: dict[str, list] = {}
        layer_arcs:    dict[str, list] = {}
        layer_polys:   dict[str, list] = {}

        for seg  in fp["lines"]:   layer_lines  .setdefault(seg[2],  []).append(seg)
        for rect in fp["rects"]:   layer_rects  .setdefault(rect[4], []).append(rect)
        for circ in fp["circles"]: layer_circles.setdefault(circ[3], []).append(circ)
        for arc  in fp["arcs"]:    layer_arcs   .setdefault(arc[6],  []).append(arc)
        for poly in fp["polys"]:   layer_polys  .setdefault(poly[1], []).append(poly)

        all_layers = (set(layer_lines) | set(layer_rects) | set(layer_circles)
                      | set(layer_arcs)  | set(layer_polys))
        ordered = [l for l in _LAYER_ORDER if l in all_layers]
        ordered += sorted(l for l in all_layers if l not in _LAYER_ORDER)

        for layer in ordered:
            r, g, b = _LAYER_COLOURS.get(layer, _DEFAULT_LAYER_COLOUR)
            colour  = wx.Colour(r, g, b, 255)

            def _pen(w_mm: float):
                return gc.CreatePen(
                    wx.GraphicsPenInfo().Colour(colour).Width(self._stroke_w(w_mm))
                )

            # Lines
            for (x1, y1), (x2, y2), _l, w_mm in layer_lines.get(layer, []):
                gc.SetPen(_pen(w_mm))
                gc.StrokeLine(*self._px(x1, y1), *self._px(x2, y2))

            # Rectangles
            for x1, y1, x2, y2, _l, w_mm, corner_r, filled in layer_rects.get(layer, []):
                gc.SetPen(_pen(w_mm))
                gc.SetBrush(gc.CreateBrush(wx.Brush(colour)) if filled
                            else wx.TRANSPARENT_BRUSH)
                px1, py1 = self._px(x1, y1)
                px2, py2 = self._px(x2, y2)
                pw, ph   = abs(px2 - px1), abs(py2 - py1)
                left, top = min(px1, px2), min(py1, py2)
                corner_px = max(0.0, self._pxlen(corner_r))
                if corner_px > 0:
                    path = gc.CreatePath()
                    path.AddRoundedRectangle(left, top, pw, ph, corner_px)
                    gc.DrawPath(path)
                else:
                    gc.DrawRectangle(left, top, pw, ph)

            # Circles
            for cx, cy, radius, _l, w_mm, filled in layer_circles.get(layer, []):
                gc.SetPen(_pen(w_mm))
                gc.SetBrush(gc.CreateBrush(wx.Brush(colour)) if filled
                            else wx.TRANSPARENT_BRUSH)
                px, py   = self._px(cx - radius, cy - radius)
                diameter = self._pxlen(radius) * 2
                gc.DrawEllipse(px, py, diameter, diameter)

            # Arcs — reconstruct the circumcircle from start/mid/end
            for sx, sy, mx_, my_, ex, ey, _l, w_mm in layer_arcs.get(layer, []):
                gc.SetPen(_pen(w_mm))
                self._draw_arc(gc, sx, sy, mx_, my_, ex, ey)

            # Polygons
            for poly_pts, _l, filled in layer_polys.get(layer, []):
                if len(poly_pts) < 2:
                    continue
                gc.SetPen(gc.CreatePen(
                    wx.GraphicsPenInfo().Colour(colour).Width(_MIN_STROKE_PX)
                ))
                gc.SetBrush(gc.CreateBrush(wx.Brush(colour)) if filled
                            else wx.TRANSPARENT_BRUSH)
                path = gc.CreatePath()
                path.MoveToPoint(*self._px(*poly_pts[0]))
                for pt in poly_pts[1:]:
                    path.AddLineToPoint(*self._px(*pt))
                path.CloseSubpath()
                gc.DrawPath(path)

        # Pads — two passes: SMD/NPTH first, then thru-hole on top.
        # Within each pass larger pads paint first so smaller ones sit on top.
        smd_pads  = sorted(
            (p for p in fp["pads"] if p[7] != "thru_hole"),
            key=lambda p: p[3] * p[4], reverse=True,
        )
        thru_pads = sorted(
            (p for p in fp["pads"] if p[7] == "thru_hole"),
            key=lambda p: p[3] * p[4], reverse=True,
        )
        outline_w = max(0.8, self._scale * 0.04)

        for pad_list in (smd_pads, thru_pads):
            for num, x, y, pw, ph, shape, rot, pad_type, drill_d in pad_list:
                fill_col = self._PAD_FILL.get(pad_type, wx.Colour(160, 160, 160, 200))
                gc.SetBrush(gc.CreateBrush(wx.Brush(fill_col)))
                gc.SetPen(gc.CreatePen(
                    wx.GraphicsPenInfo(self._PAD_OUTLINE).Width(outline_w)
                ))
                px_c, py_c = self._px(x, y)
                wpx = max(2.0, self._pxlen(pw))
                hpx = max(2.0, self._pxlen(ph))
                gc.PushState()
                gc.Translate(px_c, py_c)
                if rot:
                    gc.Rotate(math.radians(rot))
                self._draw_pad_shape(gc, num, wpx, hpx, shape, pad_type, drill_d)
                self._draw_pad_label(gc, num, wpx, hpx)
                gc.PopState()

        # Usage hint
        gc.SetFont(wx.Font(wx.FontInfo(8)), wx.Colour(70, 70, 70))
        gc.DrawText("scroll=zoom  drag=pan  dbl-click=fit", 4, h_px - 14)

    def _draw_arc(
        self, gc: wx.GraphicsContext,
        sx: float, sy: float,
        mx_: float, my_: float,
        ex: float, ey: float,
    ) -> None:
        """Draw an arc defined by start / mid / end points via circumcircle."""
        try:
            # Circumcircle centre from three points
            ax, ay = sx, sy
            bx, by = mx_, my_
            cx2, cy2 = ex, ey
            d = 2 * (ax * (by - cy2) + bx * (cy2 - ay) + cx2 * (ay - by))
            if abs(d) < 1e-9:
                # Degenerate — draw as a straight line
                gc.StrokeLine(*self._px(sx, sy), *self._px(ex, ey))
                return

            ux = ((ax**2 + ay**2) * (by - cy2)
                  + (bx**2 + by**2) * (cy2 - ay)
                  + (cx2**2 + cy2**2) * (ay - by)) / d
            uy = ((ax**2 + ay**2) * (cx2 - bx)
                  + (bx**2 + by**2) * (ax - cx2)
                  + (cx2**2 + cy2**2) * (bx - ax)) / d
            r = math.hypot(ax - ux, ay - uy)

            a_start = math.atan2(ay - uy, ax - ux)
            a_mid   = math.atan2(by - uy, bx - ux)
            a_end   = math.atan2(cy2 - uy, cx2 - ux)

            def _norm(a: float, ref: float) -> float:
                while a < ref - math.pi: a += 2 * math.pi
                while a > ref + math.pi: a -= 2 * math.pi
                return a

            a_mid_n = _norm(a_mid, a_start)
            a_end_n = _norm(a_end, a_start)
            if (a_mid_n > a_start) != (a_end_n > a_start):
                a_end_n += 2 * math.pi if a_end_n < a_start else -2 * math.pi

            steps = max(12, int(abs(a_end_n - a_start) / math.radians(4)))
            pts = [
                self._px(
                    ux + r * math.cos(a_start + (a_end_n - a_start) * i / steps),
                    uy + r * math.sin(a_start + (a_end_n - a_start) * i / steps),
                )
                for i in range(steps + 1)
            ]
            if len(pts) >= 2:
                path = gc.CreatePath()
                path.MoveToPoint(*pts[0])
                for pt in pts[1:]:
                    path.AddLineToPoint(*pt)
                gc.StrokePath(path)
        except Exception:
            pass

    def _draw_pad_shape(
        self, gc: wx.GraphicsContext,
        num: str, wpx: float, hpx: float,
        shape: str, pad_type: str, drill_d: float,
    ) -> None:
        """Draw the filled pad shape (and drill hole if thru/npth).

        Coordinate system: origin is the pad centre; gc is already translated.
        Pin 1 rectangular pads get a chamfer on the top-left corner.
        """
        s = shape.lower()
        if s == "circle":
            r = wpx / 2
            gc.DrawEllipse(-r, -r, r * 2, r * 2)
        elif s in ("oval", "roundrect"):
            corner = min(wpx, hpx) / 2 if s == "oval" else min(wpx, hpx) * 0.2
            path = gc.CreatePath()
            path.AddRoundedRectangle(-wpx / 2, -hpx / 2, wpx, hpx, corner)
            gc.DrawPath(path)
        else:
            # rect / trapezoid / custom — pin 1 gets a chamfer
            if num == "1":
                chamfer = min(wpx, hpx) * 0.25
                path = gc.CreatePath()
                path.MoveToPoint(-wpx / 2 + chamfer, -hpx / 2)
                path.AddLineToPoint( wpx / 2,         -hpx / 2)
                path.AddLineToPoint( wpx / 2,          hpx / 2)
                path.AddLineToPoint(-wpx / 2,          hpx / 2)
                path.AddLineToPoint(-wpx / 2,         -hpx / 2 + chamfer)
                path.CloseSubpath()
                gc.DrawPath(path)
            else:
                gc.DrawRectangle(-wpx / 2, -hpx / 2, wpx, hpx)

        if pad_type in ("thru_hole", "np_thru_hole") and drill_d > 0:
            dr = max(1.5, self._pxlen(drill_d) / 2)
            gc.SetBrush(gc.CreateBrush(wx.Brush(self._DRILL_COLOUR)))
            gc.SetPen(wx.NullGraphicsPen)
            gc.DrawEllipse(-dr, -dr, dr * 2, dr * 2)

    def _draw_pad_label(
        self, gc: wx.GraphicsContext,
        num: str, wpx: float, hpx: float,
    ) -> None:
        """Draw the pad number, scaled to fit inside the pad rectangle.

        Tries progressively smaller font sizes until the label fits (or gives
        up below 4 px to avoid unreadable micro-text).
        """
        if not num:
            return
        min_side = min(wpx, hpx)
        if min_side < 4:
            return
        for pt in range(min(22, int(min_side * 0.7)), 2, -1):
            gc.SetFont(wx.Font(wx.FontInfo(pt).AntiAliased()), self._TEXT_COLOUR)
            tw, th = gc.GetTextExtent(num)
            if tw <= wpx * 0.88 and th <= hpx * 0.88:
                if th >= 4:
                    gc.DrawText(num, -tw / 2, -th / 2)
                return


# ===========================================================================
# _CategoryPopup
# ===========================================================================

class _CategoryPopup(wx.PopupWindow):
    """Owner-drawn autocomplete popup for the search field.

    Draws items directly on the popup surface rather than using a child
    wx.ListBox, which avoids two cross-platform problems with PopupWindow:
    Windows doesn't forward mouse events to child controls, and macOS
    requires an extra click to activate the popup before children respond.
    """

    ITEM_PAD = 6   # vertical padding (px) per item

    def __init__(self, parent: wx.Window, on_select=None) -> None:
        super().__init__(parent, flags=wx.BORDER_SIMPLE)
        self._items: list[str] = []
        self._hover     = -1
        self._selection = wx.NOT_FOUND
        self._on_select = on_select

        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT,       self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN,   self._on_click)
        self.Bind(wx.EVT_MOTION,      self._on_motion)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

    def Set(self, items: list[str]) -> None:
        self._items     = list(items)
        self._hover     = -1
        self._selection = wx.NOT_FOUND
        self.Refresh()

    def GetSelection(self) -> int:
        return self._selection

    def GetString(self, idx: int) -> str:
        return self._items[idx] if 0 <= idx < len(self._items) else ""

    def GetCharHeight(self) -> int:
        return wx.ClientDC(self.GetParent()).GetCharHeight()

    def Popup(self)  -> None: self.Show()
    def Dismiss(self) -> None: self.Hide()

    def _item_height(self) -> int:
        return self.GetCharHeight() + self.ITEM_PAD

    def _hit_test(self, y: int) -> int:
        ih = self._item_height()
        if ih <= 0:
            return -1
        idx = y // ih
        return idx if 0 <= idx < len(self._items) else -1

    def _on_paint(self, _event) -> None:
        dc  = wx.AutoBufferedPaintDC(self)
        dc.SetFont(self.GetParent().GetFont())
        w, _  = self.GetClientSize()
        ih    = self._item_height()
        hl_bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHT)
        hl_fg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHTTEXT)
        norm_fg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        dc.SetBackground(wx.Brush(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)))
        dc.Clear()
        for i, item in enumerate(self._items):
            y = i * ih
            if i == self._hover:
                dc.SetBrush(wx.Brush(hl_bg))
                dc.SetPen(wx.TRANSPARENT_PEN)
                dc.DrawRectangle(0, y, w, ih)
                dc.SetTextForeground(hl_fg)
            else:
                dc.SetTextForeground(norm_fg)
            dc.DrawText(item, 4, y + self.ITEM_PAD // 2)

    def _on_motion(self, event: wx.MouseEvent) -> None:
        idx = self._hit_test(event.GetY())
        if idx != self._hover:
            self._hover = idx
            self.Refresh()

    def _on_leave(self, _event) -> None:
        if self._hover != -1:
            self._hover = -1
            self.Refresh()

    def _on_click(self, event: wx.MouseEvent) -> None:
        idx = self._hit_test(event.GetY())
        if idx >= 0:
            self._selection = idx
            if self._on_select:
                self._on_select()


# ===========================================================================
# _PageIndicator
# ===========================================================================

class _PageIndicator(wx.Control):
    """Owner-drawn two-dot page indicator (photo ↔ footprint-SVG view)."""

    DOT_RADIUS = 4
    DOT_GAP    = 12

    def __init__(self, parent: wx.Window, on_page_change=None) -> None:
        w = 2 * self.DOT_GAP + 2 * self.DOT_RADIUS
        h = 2 * self.DOT_RADIUS + 4
        super().__init__(parent, style=wx.BORDER_NONE, size=(w, h))
        self._page          = 0
        self._num_pages     = 2
        self._on_page_change = on_page_change

        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT,      self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN,  self._on_click)

    def set_page(self, page: int) -> None:
        if page != self._page and 0 <= page < self._num_pages:
            self._page = page
            self.Refresh()

    def _dot_positions(self) -> list[tuple[int, int]]:
        w, h = self.GetClientSize()
        total   = (self._num_pages - 1) * self.DOT_GAP
        start_x = (w - total) // 2
        cy      = h // 2
        return [(start_x + i * self.DOT_GAP, cy) for i in range(self._num_pages)]

    def _on_paint(self, _event) -> None:
        dc  = wx.AutoBufferedPaintDC(self)
        bg  = self.GetParent().GetBackgroundColour()
        dc.SetBackground(wx.Brush(bg))
        dc.Clear()
        lum = (bg.Red() * 299 + bg.Green() * 587 + bg.Blue() * 114) // 1000
        active   = wx.Colour( 80,  80,  80) if lum >= 128 else wx.Colour(200, 200, 200)
        inactive = wx.Colour(200, 200, 200) if lum >= 128 else wx.Colour( 80,  80,  80)
        dc.SetPen(wx.TRANSPARENT_PEN)
        for i, (cx, cy) in enumerate(self._dot_positions()):
            dc.SetBrush(wx.Brush(active if i == self._page else inactive))
            dc.DrawCircle(cx, cy, self.DOT_RADIUS)

    def _on_click(self, event: wx.MouseEvent) -> None:
        x = event.GetX()
        positions = self._dot_positions()
        best      = min(range(len(positions)), key=lambda i: abs(x - positions[i][0]))
        if best != self._page:
            self._page = best
            self.Refresh()
            if self._on_page_change:
                self._on_page_change(best)


# ===========================================================================
# FootprintBrowserDialog
# ===========================================================================

class FootprintBrowserDialog(wx.Dialog):
    """Three-pane footprint library browser with live 2D preview.

    Panes:
      Left   – library list (from fp-lib-table via ``_iter_footprint_libraries``).
      Middle – footprint list for the selected library.
      Right  – ``_FootprintPreviewPanel`` + description / pad count / 3D-model info.

    Double-clicking a footprint or clicking OK confirms the selection.
    ``get_selection()`` returns the chosen ``"LibraryName:FootprintName"`` ref.
    """

    def __init__(
        self,
        parent: wx.Window,
        project_dir: str = "",
        kicad_version: int = DEFAULT_KICAD_VERSION,
        initial_selection: str = "",
    ) -> None:
        super().__init__(
            parent,
            title="Select Footprint",
            size=(1000, 600),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        from .kicad.library import _iter_footprint_libraries
        self._libs: list[tuple[str, str]] = list(_iter_footprint_libraries(project_dir, kicad_version))
        self._selection       = ""
        self._current_fp_path = ""
        self._filtered_libs:  list[tuple[str, str]] = []
        self._build_ui()
        self.Centre()
        if initial_selection:
            self._navigate_to(initial_selection)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = wx.BoxSizer(wx.VERTICAL)

        # Filter bar
        filter_row = wx.BoxSizer(wx.HORIZONTAL)
        filter_row.Add(wx.StaticText(self, label="Filter:"), 0,
                       wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._filter = wx.TextCtrl(self)
        self._filter.SetHint("type to filter libraries and footprints…")
        self._filter.Bind(wx.EVT_TEXT, self._on_filter)
        filter_row.Add(self._filter, 1)
        outer.Add(filter_row, 0, wx.EXPAND | wx.ALL, 6)

        # Three-pane splitter: [lib_list | [fp_list | preview]]
        outer_split = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        inner_split = wx.SplitterWindow(outer_split, style=wx.SP_LIVE_UPDATE)

        self._lib_list = wx.ListBox(outer_split, style=wx.LB_SINGLE)
        self._lib_list.Bind(wx.EVT_LISTBOX, self._on_lib_select)

        self._fp_list = wx.ListBox(inner_split, style=wx.LB_SINGLE)
        self._fp_list.Bind(wx.EVT_LISTBOX,       self._on_fp_select)
        self._fp_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_fp_dclick)

        # Right pane: preview + info grid
        right_panel = wx.Panel(inner_split)
        right_sizer = wx.BoxSizer(wx.VERTICAL)
        self._preview = _FootprintPreviewPanel(right_panel)
        right_sizer.Add(self._preview, 1, wx.EXPAND)

        info_panel = wx.Panel(right_panel)
        info_sizer = wx.FlexGridSizer(cols=2, hgap=8, vgap=2)
        info_sizer.AddGrowableCol(1)
        bold = info_panel.GetFont().Bold()

        def _info_row(label: str, bold_val: bool = False):
            info_sizer.Add(wx.StaticText(info_panel, label=label + ":"),
                           0, wx.ALIGN_CENTER_VERTICAL)
            ctrl = wx.StaticText(info_panel, label="",
                                 style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_END)
            if bold_val:
                ctrl.SetFont(bold)
            info_sizer.Add(ctrl, 1, wx.EXPAND)
            return ctrl

        self._info_descr = _info_row("Description")
        self._info_tags  = _info_row("Tags")
        self._info_pads  = _info_row("Pads", bold_val=True)
        self._info_model = wx.StaticText(info_panel, label="",
                                         style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_START)
        info_sizer.Add(wx.StaticText(info_panel, label="3D Model:"),
                       0, wx.ALIGN_CENTER_VERTICAL)
        info_sizer.Add(self._info_model, 1, wx.EXPAND)

        info_panel.SetSizer(info_sizer)
        right_sizer.Add(info_panel, 0, wx.EXPAND | wx.ALL, 4)
        right_panel.SetSizer(right_sizer)

        inner_split.SplitVertically(self._fp_list, right_panel, 220)
        outer_split.SplitVertically(self._lib_list, inner_split, 200)
        outer.Add(outer_split, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        self._sel_label = wx.StaticText(self, label="", style=wx.ST_NO_AUTORESIZE)
        outer.Add(self._sel_label, 0, wx.ALL, 6)

        btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self._ok_btn = self.FindWindowById(wx.ID_OK)
        self._ok_btn.Disable()
        outer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self.SetSizer(outer)
        self._populate_lib_list("")

    # ------------------------------------------------------------------
    # Library / footprint list population
    # ------------------------------------------------------------------

    def _populate_lib_list(self, filt: str) -> None:
        """Rebuild the library list, including libs that contain matching footprints."""
        filt_up = filt.strip().upper()
        self._filtered_libs = []
        for lib_name, lib_path in self._libs:
            if not filt_up or filt_up in lib_name.upper():
                self._filtered_libs.append((lib_name, lib_path))
            else:
                # Include library if any of its footprints match the filter
                try:
                    if any(filt_up in e.upper()
                           for e in os.listdir(lib_path)
                           if e.lower().endswith(".kicad_mod")):
                        self._filtered_libs.append((lib_name, lib_path))
                except OSError:
                    pass

        self._lib_list.Set([name for name, _ in self._filtered_libs])
        self._fp_list.Clear()
        self._clear_selection()

    def _on_filter(self, _event) -> None:
        self._populate_lib_list(self._filter.GetValue())
        if self._lib_list.GetCount() > 0:
            self._lib_list.SetSelection(0)
            self._on_lib_select(None)

    def _on_lib_select(self, _event) -> None:
        idx = self._lib_list.GetSelection()
        if idx == wx.NOT_FOUND or idx >= len(self._filtered_libs):
            return
        _, lib_path = self._filtered_libs[idx]
        filt_up = self._filter.GetValue().strip().upper()
        try:
            names = sorted(
                e[:-len(".kicad_mod")]
                for e in os.listdir(lib_path)
                if e.lower().endswith(".kicad_mod")
                and (not filt_up or filt_up in e.upper())
            )
        except OSError:
            names = []
        self._fp_list.Set(names)
        self._clear_selection()

    def _on_fp_select(self, _event) -> None:
        lib_idx = self._lib_list.GetSelection()
        fp_idx  = self._fp_list.GetSelection()
        if lib_idx == wx.NOT_FOUND or lib_idx >= len(self._filtered_libs) or fp_idx == wx.NOT_FOUND:
            return
        lib_name, lib_path = self._filtered_libs[lib_idx]
        fp_name = self._fp_list.GetString(fp_idx)
        self._selection = f"{lib_name}:{fp_name}"
        self._sel_label.SetLabel(self._selection)
        self._ok_btn.Enable()

        fp_path = os.path.join(lib_path, f"{fp_name}.kicad_mod")
        self._current_fp_path = fp_path
        threading.Thread(target=self._load_preview_bg, args=(fp_path,), daemon=True).start()

    def _on_fp_dclick(self, event) -> None:
        self._on_fp_select(event)
        if self._selection:
            self.EndModal(wx.ID_OK)

    def _clear_selection(self) -> None:
        self._selection = ""
        self._sel_label.SetLabel("")
        self._ok_btn.Disable()
        self._clear_preview()

    # ------------------------------------------------------------------
    # Footprint preview
    # ------------------------------------------------------------------

    def _clear_preview(self) -> None:
        self._preview.load(None)
        for ctrl in (self._info_descr, self._info_tags, self._info_pads, self._info_model):
            ctrl.SetLabel("")
        self._info_model.SetForegroundColour(wx.NullColour)

    def _load_preview_bg(self, fp_path: str) -> None:
        """Parse the footprint on a background thread, then marshal to main thread."""
        fp = _parse_kicad_mod(fp_path)
        wx.CallAfter(self._apply_preview, fp_path, fp)

    def _apply_preview(self, fp_path: str, fp: dict) -> None:
        """Apply parsed geometry to the preview panel (must run on main thread)."""
        if fp_path != self._current_fp_path:
            return   # A newer selection arrived while we were loading
        self._preview.load(fp)
        self._info_descr.SetLabel(fp.get("descr", "") or "—")
        self._info_tags .SetLabel(fp.get("tags",  "") or "—")
        self._info_pads .SetLabel(str(fp.get("pads_count", 0)))

        model = fp.get("model")
        if model:
            raw_path, exists = model
            self._info_model.SetLabel(os.path.basename(raw_path))
            self._info_model.SetToolTip(raw_path)
            self._info_model.SetForegroundColour(
                wx.Colour(80, 200, 80) if exists else wx.Colour(200, 120, 50)
            )
        else:
            self._info_model.SetLabel("(none)")
            self._info_model.SetForegroundColour(wx.Colour(120, 120, 120))
        self.Layout()

    def _navigate_to(self, ref: str) -> None:
        """Pre-select ``"LibraryName:FootprintName"`` in both list panes.

        Called after the UI is built so the dialog opens already positioned at
        the auto-matched or previously chosen footprint.
        """
        if ":" not in ref:
            return
        lib_name, fp_name = ref.split(":", 1)
        for i, (name, _) in enumerate(self._filtered_libs):
            if name != lib_name:
                continue
            self._lib_list.SetSelection(i)
            self._on_lib_select(None)
            for j in range(self._fp_list.GetCount()):
                if self._fp_list.GetString(j) == fp_name:
                    self._fp_list.SetSelection(j)
                    self._fp_list.EnsureVisible(j)
                    self._on_fp_select(None)
                    break
            break

    def get_selection(self) -> str:
        """Return the selected ``"LibraryName:FootprintName"`` ref, or ``""``."""
        return self._selection


# ===========================================================================
# MetadataEditDialog
# ===========================================================================

class MetadataEditDialog(wx.Dialog):
    """Edit component metadata (description / keywords / manufacturer) before import.

    Also lets the user choose between importing the EasyEDA footprint or
    reusing an existing KiCad library footprint (with a browse button that
    opens ``FootprintBrowserDialog`` pre-navigated to the auto-matched candidate).
    """

    def __init__(self, parent: wx.Window, metadata: dict) -> None:
        self._footprint_candidate_ref = metadata.get("__footprint_candidate_ref", "")
        # Starts with the auto-matched candidate; may be overridden via Browse…
        self._kicad_footprint_ref = self._footprint_candidate_ref
        self._kicad_version = getattr(parent, "_kicad_version", DEFAULT_KICAD_VERSION)
        self._project_dir   = getattr(parent, "_project_dir",   "") or ""
        super().__init__(
            parent, title="Edit Metadata", size=(520, 340),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._build_ui(metadata)
        self.Centre()

    def _build_ui(self, metadata: dict) -> None:
        vbox = wx.BoxSizer(wx.VERTICAL)

        # Metadata fields
        grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=8)
        grid.AddGrowableCol(1)

        grid.Add(wx.StaticText(self, label="Description"), 0, wx.ALIGN_TOP)
        self._desc = wx.TextCtrl(self, value=metadata.get("description", ""),
                                 style=wx.TE_MULTILINE, size=(-1, 60))
        grid.Add(self._desc, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Keywords"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._keywords = wx.TextCtrl(self, value=metadata.get("keywords", ""))
        grid.Add(self._keywords, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Manufacturer"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._manufacturer = wx.TextCtrl(self, value=metadata.get("manufacturer", ""))
        grid.Add(self._manufacturer, 1, wx.EXPAND)

        vbox.Add(grid, 1, wx.EXPAND | wx.ALL, 10)

        # Footprint source selection
        fp_box = wx.BoxSizer(wx.VERTICAL)

        self._rb_import = wx.RadioButton(self, label="Import footprint from EasyEDA",
                                          style=wx.RB_GROUP)
        fp_box.Add(self._rb_import, 0, wx.BOTTOM, 6)

        # "Use KiCad footprint" row: radio + label showing the ref + Browse button
        kicad_row = wx.BoxSizer(wx.HORIZONTAL)
        self._rb_kicad = wx.RadioButton(self, label="Use KiCad footprint:")
        self._kicad_ref_label = wx.StaticText(
            self,
            label=self._kicad_footprint_ref or "(none selected)",
            style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_START,
        )
        self._browse_btn = wx.Button(self, label="Browse…", style=wx.BU_EXACTFIT)
        self._browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)
        kicad_row.Add(self._rb_kicad,         0, wx.ALIGN_CENTER_VERTICAL)
        kicad_row.Add(self._kicad_ref_label,  1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 6)
        kicad_row.Add(self._browse_btn,       0, wx.ALIGN_CENTER_VERTICAL)
        fp_box.Add(kicad_row, 0, wx.EXPAND)
        vbox.Add(fp_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # Pre-select "Use KiCad footprint" when a candidate was auto-matched
        if self._footprint_candidate_ref:
            self._rb_kicad.SetValue(True)
        else:
            self._rb_import.SetValue(True)

        vbox.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL),
                 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.SetSizer(vbox)

    def _on_browse(self, _event) -> None:
        """Open the footprint browser, pre-navigated to the current ref."""
        dlg = FootprintBrowserDialog(
            self,
            project_dir=self._project_dir,
            kicad_version=self._kicad_version,
            initial_selection=self._kicad_footprint_ref,
        )
        if dlg.ShowModal() == wx.ID_OK:
            chosen = dlg.get_selection()
            if chosen:
                self._kicad_footprint_ref = chosen
                self._kicad_ref_label.SetLabel(chosen)
                self._rb_kicad.SetValue(True)
                self.Layout()
        dlg.Destroy()

    def get_metadata(self) -> dict:
        """Return the edited metadata, including footprint choice flags."""
        result = {
            "description":  self._desc.GetValue(),
            "keywords":     self._keywords.GetValue(),
            "manufacturer": self._manufacturer.GetValue(),
        }
        if self._rb_kicad.GetValue() and self._kicad_footprint_ref:
            result["__reuse_existing_footprint"]  = True
            result["__manually_chosen_footprint"] = self._kicad_footprint_ref
        else:
            result["__reuse_existing_footprint"] = False
        return result


# ===========================================================================
# _SpinnerOverlay
# ===========================================================================

class _SpinnerOverlay(wx.Window):
    """Transparent animated spinner drawn on top of a target widget.

    Uses ``wx.TRANSPARENT_WINDOW`` so the parent content shows through and
    only the spinning arc is visible.  The parent widget should be disabled
    separately to block user interaction.
    """

    _ARC_RADIUS = 18
    _ARC_WIDTH  = 3
    _SEGMENTS   = 30
    _ARC_SWEEP  = 300

    def __init__(self, parent: wx.Window, target: wx.Window | None = None) -> None:
        super().__init__(parent, style=wx.TRANSPARENT_WINDOW)
        self._target = target
        self._angle  = 0
        self._timer  = wx.Timer(self)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_TIMER, self._on_tick, self._timer)
        self.Hide()

    def show(self) -> None:
        self._sync_geometry()
        self.Show()
        self.Raise()
        self._timer.Start(25)

    def dismiss(self) -> None:
        self._timer.Stop()
        self.Hide()

    def _sync_geometry(self) -> None:
        if self._target:
            rect = self._target.GetRect()
            self.SetPosition(rect.GetPosition())
            self.SetSize(rect.GetSize())
        else:
            self.SetPosition((0, 0))
            self.SetSize(self.GetParent().GetClientSize())

    def _on_tick(self, _event) -> None:
        self._angle = (self._angle + 8) % 360
        self._sync_geometry()
        self.Refresh()

    def _on_paint(self, _event) -> None:
        dc = wx.PaintDC(self)
        w, h = self.GetClientSize()
        bg  = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        lum = (bg.Red() * 299 + bg.Green() * 587 + bg.Blue() * 114) // 1000
        tail_grey, head_grey = (60, 200) if lum < 128 else (210, 80)

        cx, cy = w / 2.0, h / 2.0
        r = self._ARC_RADIUS
        for i in range(self._SEGMENTS):
            frac = i / self._SEGMENTS
            a1   = math.radians(self._angle + frac * self._ARC_SWEEP)
            a2   = math.radians(self._angle + (i + 1) / self._SEGMENTS * self._ARC_SWEEP)
            grey = int(tail_grey + (head_grey - tail_grey) * frac)
            dc.SetPen(wx.Pen(wx.Colour(grey, grey, grey), self._ARC_WIDTH))
            dc.DrawLine(
                int(cx + r * math.cos(a1)), int(cy + r * math.sin(a1)),
                int(cx + r * math.cos(a2)), int(cy + r * math.sin(a2)),
            )


# ===========================================================================
# JLCImportDialog — main plugin / standalone dialog
# ===========================================================================

class JLCImportDialog(wx.Dialog):
    """Main search / results / import dialog.

    Used both as a KiCad ActionPlugin dialog (non-modal, singleton) and as a
    standalone desktop app dialog (modal).  All long-running operations
    (search, image fetch, import) run on background threads and marshal
    results back to the main thread via ``wx.CallAfter``.

    Request IDs prevent stale callbacks from applying to a newer selection:
    each operation family has an integer counter (``_search_request_id``,
    ``_image_request_id``, …) that is incremented before each new request.
    Background callbacks no-op if their captured ID no longer matches.
    """

    def __init__(
        self,
        parent: wx.Window,
        board,
        project_dir: str | None = None,
        kicad_version: int | None = None,
        global_lib_dir: str = "",
        on_close=None,
    ) -> None:
        super().__init__(
            parent, title="JLCImport", size=(700, 640),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.board                    = board
        self._project_dir             = project_dir
        self._kicad_version           = kicad_version or DEFAULT_KICAD_VERSION
        self._global_lib_dir_override = global_lib_dir
        self._on_close_callback       = on_close
        self._closing                 = False

        # Search state
        self._search_results:     list = []
        self._raw_search_results: list = []
        self._sort_col       = -1
        self._sort_ascending = True
        self._imported_ids:   set  = set()
        self._selected_result: dict | None = None

        # Request IDs (incremented to cancel in-flight background operations)
        self._search_request_id      = 0
        self._image_request_id       = 0
        self._symbol_request_id      = 0
        self._gallery_request_id     = 0
        self._gallery_svg_request_id = 0

        # Image / SVG caches
        self._photo_bitmap:       wx.Bitmap | None = None
        self._symbol_bitmap:      wx.Bitmap | None = None
        self._symbol_svg_string:  str | None       = None
        self._full_image_data:    bytes | None      = None
        self._detail_page         = 0  # 0=photo, 1=footprint SVG
        self._gallery_index       = 0
        self._gallery_page        = 0
        self._gallery_photo_bitmap: wx.Bitmap | None = None
        self._gallery_svg_string:   str | None       = None

        self._ssl_warning_shown = False
        self._datasheet_url     = ""
        self._lcsc_page_url     = ""

        self._init_ui()
        self.Centre()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ------------------------------------------------------------------
    # Close / lifecycle
    # ------------------------------------------------------------------

    def _on_close(self, event) -> None:
        if self._closing:
            return
        if not self._main_panel.IsEnabled():
            if wx.MessageBox(
                "An import is in progress. Close anyway?",
                "Confirm", wx.YES_NO | wx.ICON_WARNING,
            ) != wx.YES:
                return

        self._closing = True
        # Stop timers before widgets are destroyed
        self._stop_search_pulse()
        self._stop_skeleton()
        self._stop_gallery_skeleton()
        self._busy_overlay.dismiss()
        self._search_overlay.dismiss()
        self._category_popup.Dismiss()
        # Invalidate all in-flight requests so their callbacks no-op
        self._search_request_id      += 1
        self._image_request_id       += 1
        self._gallery_request_id     += 1
        self._gallery_svg_request_id += 1
        self._symbol_request_id      += 1

        if self._on_close_callback:
            self._on_close_callback()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Destroy()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _init_ui(self) -> None:
        self._root_sizer = wx.BoxSizer(wx.VERTICAL)

        panel = wx.Panel(self)
        self._main_panel = panel
        vbox = wx.BoxSizer(wx.VERTICAL)

        vbox.Add(self._build_search_section(panel), 0, wx.EXPAND | wx.ALL, 5)

        self.results_count_label = wx.StaticText(panel, label="")
        vbox.Add(self.results_count_label, 0, wx.LEFT | wx.RIGHT, 10)

        vbox.Add(self._build_results_list(panel), 2, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        vbox.Add(self._build_detail_panel(panel),  0, wx.EXPAND | wx.ALL, 5)
        vbox.Add(self._build_import_section(panel), 0, wx.EXPAND | wx.ALL, 5)

        self.status_text = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL
        )
        self.status_text.SetFont(
            wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        )
        self.status_text.SetMinSize((-1, 60))
        vbox.Add(self.status_text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)

        panel.SetSizer(vbox)

        # Category autocomplete popup
        self._category_popup = _CategoryPopup(
            self, on_select=lambda: self._on_category_selected(None)
        )

        # Gallery panel (hidden until user clicks the detail image)
        self._gallery_panel = self._build_gallery_panel()
        self._gallery_panel.Hide()

        self._root_sizer.Add(panel,                1, wx.EXPAND)
        self._root_sizer.Add(self._gallery_panel,  1, wx.EXPAND)
        self.SetSizer(self._root_sizer)

        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)

        # Spinners (transparent children of the main panel)
        self._busy_overlay   = _SpinnerOverlay(panel)
        self._search_overlay = _SpinnerOverlay(panel, target=self.results_list)

    def _build_search_section(self, panel: wx.Panel) -> wx.Sizer:
        """Search input row + filter row."""
        search_box = wx.BoxSizer(wx.VERTICAL)

        hbox = wx.BoxSizer(wx.HORIZONTAL)
        self.search_input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search_input.SetHint("Search JLCPCB parts…")
        self.search_input.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        self.search_input.Bind(wx.EVT_TEXT,       self._on_search_text_changed)
        hbox.Add(self.search_input, 1, wx.EXPAND | wx.RIGHT, 5)
        self.search_btn = wx.Button(panel, label="Search")
        self.search_btn.Bind(wx.EVT_BUTTON, self._on_search)
        hbox.Add(self.search_btn, 0)
        search_box.Add(hbox, 0, wx.EXPAND | wx.ALL, 5)

        filter_row = wx.BoxSizer(wx.HORIZONTAL)
        filter_row.Add(wx.StaticText(panel, label="Type"), 0,
                       wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.type_both     = wx.RadioButton(panel, label="Both", style=wx.RB_GROUP)
        self.type_basic    = wx.RadioButton(panel, label="Basic")
        self.type_extended = wx.RadioButton(panel, label="Extended")
        self.type_both.SetValue(True)
        for rb in (self.type_both, self.type_basic, self.type_extended):
            rb.Bind(wx.EVT_RADIOBUTTON, self._on_filter_change)
            filter_row.Add(rb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)

        filter_row.Add(wx.StaticText(panel, label="Min stock"), 0,
                       wx.ALIGN_CENTER_VERTICAL | wx.RIGHT | wx.LEFT, 5)
        self._min_stock_choices = [0, 1, 10, 100, 1000, 10000, 100000]
        self._min_stock_labels  = ["Any", "1+", "10+", "100+", "1000+", "10000+", "100000+"]
        self.min_stock_choice = wx.Choice(panel, choices=self._min_stock_labels)
        self.min_stock_choice.SetSelection(1)
        self.min_stock_choice.Bind(wx.EVT_CHOICE, self._on_filter_change)
        filter_row.Add(self.min_stock_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)

        filter_row.Add(wx.StaticText(panel, label="Package"), 0,
                       wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.package_choice = wx.Choice(panel, choices=["All"])
        self.package_choice.SetSelection(0)
        self.package_choice.Bind(wx.EVT_CHOICE, self._on_filter_change)
        filter_row.Add(self.package_choice, 0, wx.ALIGN_CENTER_VERTICAL)

        search_box.Add(filter_row, 0, wx.LEFT | wx.RIGHT, 5)
        return search_box

    def _build_results_list(self, panel: wx.Panel) -> wx.ListCtrl:
        lc = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for col, (heading, width) in enumerate((
            ("LCSC", 80), ("Type", 55), ("Price", 60), ("Stock", 75),
            ("Part", 200), ("Package", 80), ("Description", 300),
        )):
            lc.InsertColumn(col, heading, width=width)
        lc.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_result_select)
        lc.Bind(wx.EVT_LIST_COL_CLICK,     self._on_col_click)
        self.results_list = lc
        return lc

    def _build_detail_panel(self, panel: wx.Panel) -> wx.Sizer:
        """Image thumbnail (click to zoom) + part info grid + action buttons."""
        box = wx.BoxSizer(wx.HORIZONTAL)

        # Left column: thumbnail + page dots
        image_col = wx.BoxSizer(wx.VERTICAL)
        self.detail_image = wx.StaticBitmap(panel, size=(160, 160))
        self.detail_image.SetMinSize((160, 160))
        self.detail_image.SetCursor(wx.Cursor(wx.CURSOR_MAGNIFIER))
        self.detail_image.Bind(wx.EVT_LEFT_DOWN, self._on_image_click)
        image_col.Add(self.detail_image, 0)
        self._page_indicator = _PageIndicator(panel, on_page_change=self._on_page_change)
        image_col.Add(self._page_indicator, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 2)
        box.Add(image_col, 0, wx.ALL, 5)

        # Right column: info grid + description + buttons
        info_sizer = wx.BoxSizer(wx.VERTICAL)
        detail_grid = wx.FlexGridSizer(cols=4, hgap=10, vgap=4)
        detail_grid.AddGrowableCol(1)
        detail_grid.AddGrowableCol(3)
        bold = panel.GetFont().Bold()

        def _field(label: str) -> wx.StaticText:
            detail_grid.Add(wx.StaticText(panel, label=label), 0,
                            wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
            ctrl = wx.StaticText(panel, label="")
            ctrl.SetFont(bold)
            detail_grid.Add(ctrl, 1, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)
            return ctrl

        self.detail_part    = _field("Part")
        self.detail_lcsc    = _field("LCSC")
        self.detail_brand   = _field("Brand")
        self.detail_package = _field("Package")
        self.detail_price   = _field("Price")
        self.detail_stock   = _field("Stock")

        info_sizer.Add(detail_grid, 0, wx.EXPAND | wx.BOTTOM, 4)

        self.detail_desc = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_NO_VSCROLL | wx.BORDER_NONE
        )
        self.detail_desc.SetMinSize((-1, 48))
        info_sizer.Add(self.detail_desc, 1, wx.EXPAND | wx.BOTTOM, 4)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.detail_import_btn    = wx.Button(panel, label="Import")
        self.detail_datasheet_btn = wx.Button(panel, label="Datasheet")
        self.detail_lcsc_btn      = wx.Button(panel, label="LCSC Page")
        self.detail_import_btn   .Bind(wx.EVT_BUTTON, self._on_import)
        self.detail_datasheet_btn.Bind(wx.EVT_BUTTON, self._on_datasheet)
        self.detail_lcsc_btn     .Bind(wx.EVT_BUTTON, self._on_lcsc_page)
        for btn in (self.detail_import_btn, self.detail_datasheet_btn, self.detail_lcsc_btn):
            btn.Disable()
            btn_row.Add(btn, 0, wx.RIGHT, 5)
        info_sizer.Add(btn_row, 0)
        box.Add(info_sizer, 1, wx.EXPAND | wx.ALL, 5)

        self._detail_box = box
        return box

    def _build_import_section(self, panel: wx.Panel) -> wx.Sizer:
        """Destination radio buttons, library name field, and KiCad version picker."""
        import_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Destination")
        bold = panel.GetFont().Bold()

        project_dir = self._get_project_dir()
        if self._global_lib_dir_override:
            global_dir = self._global_lib_dir_override
        else:
            try:
                global_dir = get_global_lib_dir(self._kicad_version)
            except ValueError:
                config = load_config()
                config["global_lib_dir"] = ""
                save_config(config)
                global_dir = get_global_lib_dir(self._kicad_version)
        self._global_lib_dir = global_dir

        # Row 1: project destination
        proj_row = wx.BoxSizer(wx.HORIZONTAL)
        self.dest_project = wx.RadioButton(panel, label="Project", style=wx.RB_GROUP)
        self.dest_project.Bind(wx.EVT_RADIOBUTTON, self._on_dest_change)
        proj_path_label = wx.StaticText(panel, label=project_dir or "(no board open)")
        proj_path_label.SetFont(bold)
        proj_row.Add(self.dest_project,  0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        proj_row.Add(proj_path_label,    0, wx.ALIGN_CENTER_VERTICAL)
        import_box.Add(proj_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        # Row 2: global destination + browse/reset buttons
        global_row = wx.BoxSizer(wx.HORIZONTAL)
        self.dest_global = wx.RadioButton(panel, label="Global")
        self.dest_global.Bind(wx.EVT_RADIOBUTTON, self._on_dest_change)
        self._global_path_label = wx.StaticText(panel, label=self._truncate_path(global_dir))
        self._global_path_label.SetFont(bold)
        self._global_path_label.SetToolTip(global_dir)
        self._global_browse_btn = wx.Button(panel, label="…", style=wx.BU_EXACTFIT)
        self._global_browse_btn.Bind(wx.EVT_BUTTON, self._on_global_browse)
        self._global_reset_btn = wx.Button(panel, label="\u00d7", style=wx.BU_EXACTFIT)
        self._global_reset_btn.Bind(wx.EVT_BUTTON, self._on_global_reset)
        global_row.Add(self.dest_global,         0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        global_row.Add(self._global_path_label,  0, wx.ALIGN_CENTER_VERTICAL)
        global_row.Add(self._global_browse_btn,  0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)
        global_row.Add(self._global_reset_btn,   0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 2)
        import_box.Add(global_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        config = load_config()
        self._apply_saved_destination(project_dir, config)

        # Row 3: library name + KiCad version
        lib_row = wx.BoxSizer(wx.HORIZONTAL)
        lib_row.Add(wx.StaticText(panel, label="Library"), 0,
                    wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._lib_name = config.get("lib_name", "JLCImport")
        self.lib_name_input = wx.TextCtrl(panel, size=(120, -1), value=self._lib_name)
        self.lib_name_input.Bind(wx.EVT_KILL_FOCUS, self._on_lib_name_change)
        lib_row.Add(self.lib_name_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 15)

        self._version_label = wx.StaticText(panel, label="KiCad")
        lib_row.Add(self._version_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._version_labels = [str(v) for v in sorted(SUPPORTED_VERSIONS, reverse=True)]
        self.version_choice = wx.Choice(panel, choices=self._version_labels)
        self.version_choice.SetSelection(
            self._version_labels.index(str(self._kicad_version))
        )
        self.version_choice.Bind(wx.EVT_CHOICE, self._on_version_change)
        lib_row.Add(self.version_choice, 0, wx.ALIGN_CENTER_VERTICAL)
        import_box.Add(lib_row, 0, wx.ALL, 5)
        self._update_version_visibility()

        return import_box

    def _build_gallery_panel(self) -> wx.Panel:
        """Full-screen result gallery (shown when user clicks the detail thumbnail)."""
        panel = wx.Panel(self)
        gbox  = wx.BoxSizer(wx.VERTICAL)

        back_btn = wx.Button(panel, label="\u2190 Back")
        back_btn.Bind(wx.EVT_BUTTON, self._on_gallery_close)
        gbox.Add(back_btn, 0, wx.LEFT | wx.TOP, 5)

        nav_row = wx.BoxSizer(wx.HORIZONTAL)
        self._gallery_prev = wx.Button(panel, label="\u25c0", style=wx.BU_EXACTFIT)
        self._gallery_next = wx.Button(panel, label="\u25b6", style=wx.BU_EXACTFIT)
        self._gallery_prev.Bind(wx.EVT_BUTTON, self._on_gallery_prev)
        self._gallery_next.Bind(wx.EVT_BUTTON, self._on_gallery_next)

        img_stack = wx.BoxSizer(wx.VERTICAL)
        self._gallery_image = wx.StaticBitmap(panel)
        self._gallery_image.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self._gallery_image.Bind(wx.EVT_LEFT_DOWN, self._on_gallery_close)
        img_stack.Add(self._gallery_image, 0, wx.ALIGN_CENTER_HORIZONTAL)
        self._gallery_page_indicator = _PageIndicator(
            panel, on_page_change=self._on_gallery_page_change
        )
        img_stack.Add(self._gallery_page_indicator, 0,
                      wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 2)

        nav_row.Add(self._gallery_prev,   0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        nav_row.Add(img_stack,            1, wx.ALIGN_CENTER_VERTICAL)
        nav_row.Add(self._gallery_next,   0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT,  5)
        gbox.Add(nav_row, 1, wx.EXPAND | wx.ALL, 5)

        self._gallery_info = wx.StaticText(panel, label="", style=wx.ST_NO_AUTORESIZE)
        self._gallery_info.SetFont(
            wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        )
        self._gallery_desc = wx.StaticText(panel, label="", style=wx.ST_NO_AUTORESIZE)
        gbox.Add(self._gallery_info, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        gbox.Add(self._gallery_desc, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        panel.SetSizer(gbox)
        return panel

    # ------------------------------------------------------------------
    # Destination helpers
    # ------------------------------------------------------------------

    def _get_project_dir(self) -> str:
        if self.board:
            path = self.board.GetFileName()
            if path:
                return os.path.dirname(path)
        return self._project_dir or ""

    def _get_kicad_version(self) -> int:
        return int(self._version_labels[self.version_choice.GetSelection()])

    def _apply_saved_destination(self, project_dir: str, config: dict) -> None:
        """Set destination radio state from saved config."""
        if not project_dir:
            self.dest_project.Disable()
            self.dest_global.SetValue(True)
        elif config.get("use_global", False):
            self.dest_global.SetValue(True)
        else:
            self.dest_project.SetValue(True)

    def _persist_destination(self) -> None:
        config = load_config()
        config["use_global"] = self.dest_global.GetValue()
        save_config(config)

    @staticmethod
    def _truncate_path(path: str, max_len: int = 50) -> str:
        """Shorten a long path with a middle ellipsis."""
        if len(path) <= max_len:
            return path
        keep  = max_len - 3
        left  = keep // 2
        right = keep - left
        return path[:left] + "\u2026" + path[-right:]

    def _set_global_path(self, path: str) -> None:
        self._global_path_label.SetLabel(self._truncate_path(path))
        self._global_path_label.SetToolTip(path)
        self._global_path_label.GetParent().Layout()

    # ------------------------------------------------------------------
    # Settings event handlers
    # ------------------------------------------------------------------

    def _on_lib_name_change(self, event) -> None:
        new_name = self.lib_name_input.GetValue().strip()
        if new_name and new_name != self._lib_name:
            self._lib_name = new_name
            config = load_config()
            config["lib_name"] = new_name
            save_config(config)
        elif not new_name:
            self.lib_name_input.SetValue(self._lib_name)
        event.Skip()

    def _update_version_visibility(self) -> None:
        """Show the KiCad version picker only when using the default global dir."""
        config = load_config()
        use_custom = bool(config.get("global_lib_dir", "") or self._global_lib_dir_override)
        self._version_label.Show(not use_custom)
        self.version_choice .Show(not use_custom)
        self._version_label.GetParent().Layout()

    def _on_version_change(self, event) -> None:
        config = load_config()
        if not config.get("global_lib_dir", "") and not self._global_lib_dir_override:
            new_dir = get_global_lib_dir(self._get_kicad_version())
            self._global_lib_dir = new_dir
            self._set_global_path(new_dir)
        event.Skip()

    def _on_global_browse(self, _event) -> None:
        dlg = wx.DirDialog(self, "Choose global library directory",
                           style=wx.DD_DEFAULT_STYLE)
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

    def _on_global_reset(self, _event) -> None:
        config = load_config()
        config["global_lib_dir"] = ""
        save_config(config)
        self._global_lib_dir_override = ""
        default_dir = get_global_lib_dir(self._get_kicad_version())
        self._global_lib_dir = default_dir
        self._set_global_path(default_dir)
        self._update_version_visibility()

    def _on_dest_change(self, event) -> None:
        self._persist_destination()
        if self._search_results:
            self._refresh_imported_ids()
            self._repopulate_results()
        event.Skip()

    # ------------------------------------------------------------------
    # SSL warning
    # ------------------------------------------------------------------

    def _handle_ssl_cert_error(self) -> None:
        """Show a one-time SSL warning and enable unverified HTTPS for the session."""
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
                wx.CallAfter(self._log, "TLS certificate verification disabled for this session.")
        _api_module.allow_unverified_ssl()

    # ------------------------------------------------------------------
    # Status log
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        if not self._closing:
            self.status_text.AppendText(msg + "\n")
            self.status_text.Update()

    # ------------------------------------------------------------------
    # Category autocomplete
    # ------------------------------------------------------------------

    def _on_search_text_changed(self, _event) -> None:
        text = self.search_input.GetValue().strip().lower()
        if len(text) < 2:
            self._category_popup.Dismiss()
            return
        pattern = re.compile(r"\b" + re.escape(text), re.IGNORECASE)
        matches = [c for c in CATEGORIES if pattern.search(c)]
        if 1 < len(matches) <= 20 and not (len(matches) == 1 and matches[0].lower() == text):
            self._show_category_list(matches)
        else:
            self._category_popup.Dismiss()

    def _show_category_list(self, matches: list[str]) -> None:
        self._category_popup.Set(matches)
        screen_pos = self.search_input.ClientToScreen(wx.Point(0, 0))
        sz         = self.search_input.GetSize()
        height     = min(len(matches), 10) * self._category_popup.GetCharHeight() + 20
        self._category_popup.SetPosition(wx.Point(screen_pos.x, screen_pos.y + sz.height))
        self._category_popup.SetSize(sz.width, height)
        self._category_popup.Popup()

    def _on_category_selected(self, _event) -> None:
        sel = self._category_popup.GetSelection()
        if sel != wx.NOT_FOUND:
            self.search_input.SetValue(self._category_popup.GetString(sel))
            self._category_popup.Dismiss()
            self.search_input.SetInsertionPointEnd()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_search(self, _event) -> None:
        self._category_popup.Dismiss()
        keyword = self.search_input.GetValue().strip()
        if not keyword:
            return

        self.search_btn.Disable()
        self._clear_detail()
        self.results_list.DeleteAllItems()
        self._search_results     = []
        self._raw_search_results = []
        self.package_choice.Set(["All"])
        self.package_choice.SetSelection(0)
        self.results_count_label.SetLabel("")
        self.status_text.Clear()
        self._log(f'Searching for "{keyword}"…')

        self._search_request_id += 1
        request_id = self._search_request_id
        self._start_search_pulse()
        self._search_overlay.show()
        threading.Thread(
            target=self._fetch_search_results,
            args=(keyword, request_id),
            daemon=True,
        ).start()

    def _start_search_pulse(self) -> None:
        self._pulse_phase = 0
        if not hasattr(self, "_pulse_timer"):
            self._pulse_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_pulse_tick, self._pulse_timer)
        self._pulse_timer.Start(300)
        self.search_btn.SetLabel("\u00b7")

    def _on_pulse_tick(self, _event) -> None:
        self._pulse_phase = (self._pulse_phase + 1) % 3
        self.search_btn.SetLabel("\u00b7" * (self._pulse_phase + 1))

    def _stop_search_pulse(self) -> None:
        if hasattr(self, "_pulse_timer"):
            self._pulse_timer.Stop()
        self.search_btn.SetLabel("Search")
        self.search_btn.Enable()

    def _fetch_search_results(self, keyword: str, request_id: int) -> None:
        """Background thread: call the JLCPCB search API."""
        try:
            try:
                result = search_components(keyword, page_size=500)
            except SSLCertError:
                self._handle_ssl_cert_error()
                result = search_components(keyword, page_size=500)
            if not self._closing:
                wx.CallAfter(self._on_search_complete, result, request_id)
        except APIError as e:
            if not self._closing:
                wx.CallAfter(self._on_search_error, f"Search error: {e}", request_id)
        except Exception as e:
            if not self._closing:
                wx.CallAfter(self._on_search_error,
                             f"Unexpected error: {type(e).__name__}: {e}", request_id)

    def _on_search_complete(self, result: dict, request_id: int) -> None:
        if request_id != self._search_request_id:
            return
        self._stop_search_pulse()
        self._search_overlay.dismiss()

        results = sorted(result["results"], key=lambda r: r.get("stock") or 0, reverse=True)
        self._raw_search_results = results
        self._sort_col = 3        # sorted by stock
        self._sort_ascending = False
        self._populate_package_choices()
        self._apply_filters()
        self._log(f"  {result['total']} total results, showing {len(self._search_results)}")
        self._refresh_imported_ids()
        self._update_col_headers()
        self._repopulate_results()

    def _on_search_error(self, msg: str, request_id: int) -> None:
        if request_id != self._search_request_id:
            return
        self._stop_search_pulse()
        self._search_overlay.dismiss()
        self._log(msg)

    # ------------------------------------------------------------------
    # Results list — sorting / filtering / display
    # ------------------------------------------------------------------

    _COL_NAMES = ["LCSC", "Type", "Price", "Stock", "Part", "Package", "Description"]
    _NUMERIC_COLS = {2, 3}   # columns where descending sort is the default

    def _on_col_click(self, event: wx.ListEvent) -> None:
        col = event.GetColumn()
        if col == self._sort_col:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_col       = col
            self._sort_ascending = col not in self._NUMERIC_COLS

        key_map = {
            0: lambda r: r.get("lcsc",        ""),
            1: lambda r: r.get("type",        ""),
            2: lambda r: r.get("price")    or 0,
            3: lambda r: r.get("stock")    or 0,
            4: lambda r: r.get("model",       "").lower(),
            5: lambda r: r.get("package",     "").lower(),
            6: lambda r: r.get("description", "").lower(),
        }
        key_fn = key_map.get(col)
        if key_fn:
            self._search_results.sort(key=key_fn, reverse=not self._sort_ascending)
            self._update_col_headers()
            self._repopulate_results()

    def _update_col_headers(self) -> None:
        for i, name in enumerate(self._COL_NAMES):
            if i == self._sort_col:
                label = name + (" \u25b2" if self._sort_ascending else " \u25bc")
            else:
                label = name
            col = self.results_list.GetColumn(i)
            col.SetText(label)
            self.results_list.SetColumn(i, col)

    def _populate_package_choices(self) -> None:
        packages = sorted({r.get("package", "") for r in self._raw_search_results
                           if r.get("package")})
        self.package_choice.Set(["All"] + packages)
        self.package_choice.SetSelection(0)

    def _get_type_filter(self) -> str:
        if self.type_basic.GetValue():    return "Basic"
        if self.type_extended.GetValue(): return "Extended"
        return ""

    def _get_min_stock(self) -> int:
        idx = self.min_stock_choice.GetSelection()
        return self._min_stock_choices[idx] if idx != wx.NOT_FOUND else 0

    def _get_package_filter(self) -> str:
        idx = self.package_choice.GetSelection()
        return self.package_choice.GetString(idx) if idx > 0 else ""

    def _apply_filters(self) -> None:
        filtered = filter_by_type(self._raw_search_results, self._get_type_filter())
        filtered = filter_by_min_stock(filtered, self._get_min_stock())
        pkg = self._get_package_filter()
        if pkg:
            filtered = [r for r in filtered if r.get("package") == pkg]
        self._search_results = filtered

    def _on_filter_change(self, _event) -> None:
        if self._raw_search_results:
            self._apply_filters()
            self._repopulate_results()

    def _on_dest_change(self, event) -> None:
        self._persist_destination()
        if self._search_results:
            self._refresh_imported_ids()
            self._repopulate_results()
        event.Skip()

    def _refresh_imported_ids(self) -> None:
        """Scan the current symbol library for already-imported LCSC IDs."""
        self._imported_ids = set()
        lib_dir = self._global_lib_dir if self.dest_global.GetValue() else self._get_project_dir()
        if not lib_dir:
            return
        sym_path = os.path.join(lib_dir, f"{self._lib_name}.kicad_sym")
        if not os.path.exists(sym_path):
            return
        try:
            with open(sym_path, encoding="utf-8") as f:
                for m in re.finditer(r'\(property "LCSC" "(C\d+)"', f.read()):
                    self._imported_ids.add(m.group(1))
        except OSError:
            pass

    def _repopulate_results(self) -> None:
        self.results_list.DeleteAllItems()
        reselect_idx = -1
        for i, r in enumerate(self._search_results):
            lcsc = r["lcsc"]
            if self._selected_result and lcsc == self._selected_result["lcsc"]:
                reselect_idx = i
            prefix = "\u2713 " if lcsc in self._imported_ids else ""
            self.results_list.InsertItem(i, prefix + lcsc)
            self.results_list.SetItem(i, 1, r["type"])
            self.results_list.SetItem(i, 2, f"${r['price']:.4f}" if r["price"] else "N/A")
            self.results_list.SetItem(i, 3, f"{r['stock']:,}" if r["stock"] else "N/A")
            self.results_list.SetItem(i, 4, r["model"])
            self.results_list.SetItem(i, 5, r.get("package", ""))
            self.results_list.SetItem(i, 6, r.get("description", ""))
        self._update_results_count()
        if reselect_idx >= 0:
            self.results_list.Select(reselect_idx)
        elif len(self._search_results) == 1:
            self.results_list.Select(0)
        elif self._selected_result:
            self._clear_detail()

    def _update_results_count(self) -> None:
        shown = len(self._search_results)
        total = len(self._raw_search_results)
        if total == 0:
            self.results_count_label.SetLabel("")
        elif shown == total:
            self.results_count_label.SetLabel(f"{total} result{'s' if total != 1 else ''}")
        else:
            self.results_count_label.SetLabel(f"{shown} of {total}")

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def _clear_detail(self) -> None:
        self._selected_result   = None
        self._photo_bitmap      = None
        self._symbol_bitmap     = None
        self._symbol_svg_string = None
        self._detail_page       = 0
        self._stop_skeleton()
        self._image_request_id  += 1
        self._symbol_request_id += 1
        self._page_indicator.set_page(0)
        for ctrl in (self.detail_lcsc, self.detail_part, self.detail_brand,
                     self.detail_package, self.detail_price, self.detail_stock):
            ctrl.SetLabel("")
        self.detail_desc.SetValue("")
        self._show_no_image()
        self._datasheet_url = self._lcsc_page_url = ""
        for btn in (self.detail_import_btn, self.detail_datasheet_btn, self.detail_lcsc_btn):
            btn.Disable()

    def _on_result_select(self, event: wx.ListEvent) -> None:
        idx = event.GetIndex()
        if not (0 <= idx < len(self._search_results)):
            return
        r = self._search_results[idx]
        if self._selected_result and r["lcsc"] == self._selected_result["lcsc"]:
            return   # Same item — nothing to update
        self._selected_result   = r
        self._photo_bitmap      = None
        self._symbol_bitmap     = None
        self._symbol_svg_string = None

        self.detail_lcsc   .SetLabel(f"{r['lcsc']}  ({r['type']})")
        self.detail_part   .SetLabel(r["model"])
        self.detail_brand  .SetLabel(r["brand"])
        self.detail_package.SetLabel(r["package"])
        self.detail_price  .SetLabel(f"${r['price']:.4f}" if r["price"] else "N/A")
        self.detail_stock  .SetLabel(f"{r['stock']:,}"   if r["stock"] else "N/A")
        self.detail_desc   .SetValue(r["description"])

        self._datasheet_url = r.get("datasheet", "")
        self._lcsc_page_url = r.get("url", "")
        self.detail_datasheet_btn.Enable(bool(self._datasheet_url))
        self.detail_lcsc_btn     .Enable(bool(self._lcsc_page_url))
        self.detail_import_btn   .Enable()

        # Kick off background fetches for the product image and footprint SVG
        self._image_request_id  += 1
        self._symbol_request_id += 1
        lcsc_url = r.get("url", "")
        if lcsc_url:
            if self._detail_page == 0:
                self._show_skeleton()
            threading.Thread(
                target=self._fetch_image, args=(lcsc_url, self._image_request_id), daemon=True
            ).start()
        elif self._detail_page == 0:
            self._stop_skeleton()
            self._show_no_image()

        if self._detail_page == 1:
            self._show_no_footprint()
        threading.Thread(
            target=self._fetch_footprint_svg,
            args=(r["lcsc"], self._symbol_request_id),
            daemon=True,
        ).start()

        self.Layout()

    # ------------------------------------------------------------------
    # Skeleton / placeholder bitmaps
    # ------------------------------------------------------------------

    def _show_no_image(self) -> None:
        self._photo_bitmap = None
        size = 160
        bmp  = wx.Bitmap(size, size)
        dc   = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(245, 245, 245)))
        dc.Clear()
        dc.SetPen(wx.Pen(wx.Colour(200, 200, 200), 1))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        # Simple "image" icon: rounded rect + mountain + sun
        cx, cy = size // 2, size // 2
        dc.DrawRoundedRectangle(cx - 25, cy - 20, 50, 40, 4)
        dc.DrawLine(cx - 18, cy + 12, cx, cy - 5)
        dc.DrawLine(cx, cy - 5, cx + 8, cy + 5)
        dc.DrawLine(cx + 8, cy + 5, cx + 18, cy - 10)
        dc.DrawCircle(cx + 10, cy - 12, 5)
        dc.SelectObject(wx.NullBitmap)
        self.detail_image.SetBitmap(bmp)

    def _show_no_footprint(self) -> None:
        self.detail_image.SetBitmap(_no_footprint_placeholder(160, not has_svg_support()))

    def _show_skeleton(self) -> None:
        self._skeleton_phase = 0
        if not hasattr(self, "_skeleton_timer"):
            self._skeleton_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_skeleton_tick, self._skeleton_timer)
        self._skeleton_timer.Start(30)
        self._draw_skeleton_frame(self.detail_image, 160)

    def _stop_skeleton(self) -> None:
        if hasattr(self, "_skeleton_timer"):
            self._skeleton_timer.Stop()

    def _on_skeleton_tick(self, _event) -> None:
        if self._detail_page == 0:
            self._skeleton_phase = (self._skeleton_phase + 3) % 200
            self._draw_skeleton_frame(self.detail_image, 160)

    @staticmethod
    def _draw_skeleton_frame(target: wx.StaticBitmap, size: int,
                             phase: int = 0) -> None:
        """Render one shimmer frame onto *target*."""
        bmp = wx.Bitmap(size, size)
        dc  = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(wx.Colour(240, 240, 240)))
        dc.Clear()
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(wx.Colour(225, 225, 225)))
        dc.DrawRoundedRectangle(4, 4, size - 8, size - 8, 6)

        band_center = phase - 50
        band_half   = 30
        for x in range(4, size - 4):
            dist = abs(x - band_center)
            if dist < band_half:
                t     = dist / band_half
                alpha = int(25 * (1 + math.cos(t * math.pi)) / 2)
                if alpha > 0:
                    c = min(255, 225 + alpha)
                    dc.SetPen(wx.Pen(wx.Colour(c, c, c), 1))
                    dc.DrawLine(x, 4, x, size - 4)

        dc.SelectObject(wx.NullBitmap)
        target.SetBitmap(bmp)

    # ------------------------------------------------------------------
    # Image / SVG fetching
    # ------------------------------------------------------------------

    def _fetch_image(self, lcsc_url: str, request_id: int) -> None:
        """Background: fetch the product thumbnail."""
        try:
            try:
                img_data = fetch_product_image(lcsc_url)
            except SSLCertError:
                self._handle_ssl_cert_error()
                img_data = fetch_product_image(lcsc_url)
        except Exception:
            img_data = None
        if not self._closing and self._image_request_id == request_id:
            wx.CallAfter(self._set_image, img_data, request_id)

    def _set_image(self, img_data: bytes | None, request_id: int) -> None:
        if self._image_request_id != request_id:
            return
        self._stop_skeleton()
        self._full_image_data = img_data
        bmp = self._decode_bitmap(img_data, 160)
        self._photo_bitmap = bmp
        if self._detail_page == 0:
            if bmp:
                self.detail_image.SetBitmap(bmp)
            else:
                self._show_no_image()
        self.Layout()

    def _fetch_footprint_svg(self, lcsc_id: str, request_id: int) -> None:
        """Background: fetch the EasyEDA footprint SVG for detail preview."""
        try:
            try:
                uuids = fetch_component_uuids(lcsc_id)
            except SSLCertError:
                self._handle_ssl_cert_error()
                uuids = fetch_component_uuids(lcsc_id)
            svg_string = uuids[-1].get("svg", "") if uuids else ""
            if not self._closing and self._symbol_request_id == request_id and svg_string:
                wx.CallAfter(self._set_footprint_svg, svg_string, request_id)
        except Exception:
            pass   # SVG preview is best-effort

    def _set_footprint_svg(self, svg_string: str, request_id: int) -> None:
        if self._symbol_request_id != request_id:
            return
        self._symbol_svg_string = svg_string
        self._symbol_bitmap     = render_svg_bitmap(svg_string)
        if self._detail_page == 1:
            if self._symbol_bitmap:
                self.detail_image.SetBitmap(self._symbol_bitmap)
            else:
                self._show_no_footprint()

    # ------------------------------------------------------------------
    # Detail page switcher (photo ↔ footprint SVG)
    # ------------------------------------------------------------------

    def _on_page_change(self, page: int) -> None:
        self._detail_page = page
        if page == 0:
            self.detail_image.SetBitmap(self._photo_bitmap) if self._photo_bitmap \
                else self._show_no_image()
        else:
            self.detail_image.SetBitmap(self._symbol_bitmap) if self._symbol_bitmap \
                else self._show_no_footprint()

    # ------------------------------------------------------------------
    # Gallery (full-screen result browsing)
    # ------------------------------------------------------------------

    def _on_image_click(self, _event) -> None:
        if not self._search_results:
            return
        sel = self.results_list.GetFirstSelected()
        self._gallery_index = max(sel, 0)
        self._gallery_page  = self._detail_page
        self._gallery_page_indicator.set_page(self._gallery_page)
        self._enter_gallery()

    def _enter_gallery(self) -> None:
        self._main_panel.Hide()
        self._gallery_panel.Show()
        self._update_gallery()
        self._root_sizer.Layout()

    def _exit_gallery(self) -> None:
        self._stop_gallery_skeleton()
        self._gallery_panel.Hide()
        self._main_panel.Show()
        idx = self._gallery_index
        if 0 <= idx < self.results_list.GetItemCount():
            self.results_list.Select(idx)
            self.results_list.EnsureVisible(idx)
        self._root_sizer.Layout()

    def _update_gallery(self) -> None:
        if not self._search_results:
            return
        r   = self._search_results[self._gallery_index]
        n   = len(self._search_results)

        price_str = f"${r['price']:.4f}" if r["price"] else "N/A"
        stock_str = f"{r['stock']:,}"    if r["stock"] else "N/A"
        self._gallery_info.SetLabel(
            f"{r['lcsc']}  |  {r['model']}  |  {r['brand']}  |  "
            f"{r['package']}  |  {price_str}  |  Stock: {stock_str}"
        )
        self._gallery_desc.SetLabel(r.get("description", ""))
        self._gallery_desc.Wrap(self.GetSize().width - 30)
        self._gallery_prev.Enable(self._gallery_index > 0)
        self._gallery_next.Enable(self._gallery_index < n - 1)

        self._gallery_photo_bitmap = None
        self._gallery_svg_string   = None
        self._show_gallery_skeleton()

        # Fetch photo
        lcsc_url = r.get("url", "")
        self._gallery_request_id += 1
        if lcsc_url:
            threading.Thread(
                target=self._fetch_gallery_image,
                args=(lcsc_url, self._gallery_request_id), daemon=True,
            ).start()
        elif self._gallery_page == 0:
            self._stop_gallery_skeleton()
            self._show_gallery_no_image()

        # Fetch footprint SVG
        self._gallery_svg_request_id += 1
        threading.Thread(
            target=self._fetch_gallery_svg,
            args=(r["lcsc"], self._gallery_svg_request_id), daemon=True,
        ).start()

    def _gallery_image_size(self) -> int:
        w, h = self.GetClientSize()
        return max(min(w - 100, h - 120), 100)

    # Gallery skeleton / placeholders

    def _show_gallery_skeleton(self) -> None:
        self._gallery_skeleton_phase = 0
        if not hasattr(self, "_gallery_skeleton_timer"):
            self._gallery_skeleton_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_gallery_skeleton_tick,
                      self._gallery_skeleton_timer)
        self._gallery_skeleton_timer.Start(30)
        self._draw_gallery_skeleton_frame()

    def _stop_gallery_skeleton(self) -> None:
        if hasattr(self, "_gallery_skeleton_timer"):
            self._gallery_skeleton_timer.Stop()

    def _on_gallery_skeleton_tick(self, _event) -> None:
        self._gallery_skeleton_phase = (self._gallery_skeleton_phase + 3) % 200
        self._draw_gallery_skeleton_frame()

    def _draw_gallery_skeleton_frame(self) -> None:
        size  = self._gallery_image_size()
        phase = self._gallery_skeleton_phase
        # Reuse the detail skeleton renderer with gallery-appropriate sizing
        self._draw_skeleton_frame(self._gallery_image, size, phase)
        self._gallery_panel.Layout()

    def _show_gallery_no_image(self) -> None:
        size = self._gallery_image_size()
        bmp  = wx.Bitmap(size, size)
        dc   = wx.MemoryDC(bmp)
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

    def _show_gallery_no_footprint(self) -> None:
        size = self._gallery_image_size()
        self._gallery_image.SetBitmap(_no_footprint_placeholder(size, not has_svg_support()))
        self._gallery_panel.Layout()

    def _show_gallery_footprint(self) -> None:
        if not self._gallery_svg_string:
            self._show_gallery_no_footprint()
            return
        size = self._gallery_image_size()
        bmp  = render_svg_bitmap(self._gallery_svg_string, size=size)
        if bmp:
            self._gallery_image.SetBitmap(bmp)
            self._gallery_panel.Layout()
        else:
            self._show_gallery_no_footprint()

    # Gallery image / SVG fetching

    def _fetch_gallery_image(self, lcsc_url: str, request_id: int) -> None:
        try:
            try:
                img_data = fetch_product_image(lcsc_url)
            except SSLCertError:
                self._handle_ssl_cert_error()
                img_data = fetch_product_image(lcsc_url)
        except Exception:
            img_data = None
        if not self._closing and self._gallery_request_id == request_id:
            wx.CallAfter(self._set_gallery_image, img_data, request_id)

    def _set_gallery_image(self, img_data: bytes | None, request_id: int) -> None:
        if self._gallery_request_id != request_id:
            return
        size = self._gallery_image_size()
        bmp  = self._decode_bitmap(img_data, size)
        self._gallery_photo_bitmap = bmp
        if self._gallery_page == 0:
            self._stop_gallery_skeleton()
            if bmp:
                self._gallery_image.SetBitmap(bmp)
                self._gallery_panel.Layout()
            else:
                self._show_gallery_no_image()

    def _fetch_gallery_svg(self, lcsc_id: str, request_id: int) -> None:
        try:
            try:
                uuids = fetch_component_uuids(lcsc_id)
            except SSLCertError:
                self._handle_ssl_cert_error()
                uuids = fetch_component_uuids(lcsc_id)
            svg_string = uuids[-1].get("svg", "") if uuids else ""
            if not self._closing and self._gallery_svg_request_id == request_id and svg_string:
                wx.CallAfter(self._set_gallery_svg, svg_string, request_id)
        except Exception:
            pass

    def _set_gallery_svg(self, svg_string: str, request_id: int) -> None:
        if self._gallery_svg_request_id != request_id:
            return
        self._gallery_svg_string = svg_string
        if self._gallery_page == 1:
            self._stop_gallery_skeleton()
            self._show_gallery_footprint()

    def _on_gallery_page_change(self, page: int) -> None:
        self._gallery_page = page
        if page == 0:
            if self._gallery_photo_bitmap:
                self._gallery_image.SetBitmap(self._gallery_photo_bitmap)
                self._gallery_panel.Layout()
            else:
                self._show_gallery_no_image()
        else:
            self._show_gallery_footprint() if self._gallery_svg_string \
                else self._show_gallery_no_footprint()

    def _on_gallery_prev(self, _event) -> None:
        if self._gallery_index > 0:
            self._gallery_index -= 1
            self._update_gallery()

    def _on_gallery_next(self, _event) -> None:
        if self._gallery_index < len(self._search_results) - 1:
            self._gallery_index += 1
            self._update_gallery()

    def _on_gallery_close(self, _event) -> None:
        self._exit_gallery()

    # ------------------------------------------------------------------
    # Shared bitmap decode helper
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_bitmap(img_data: bytes | None, size: int) -> wx.Bitmap | None:
        """Decode raw image bytes and scale to *size* × *size*, or return None."""
        if not img_data:
            return None
        try:
            for fmt in (wx.BITMAP_TYPE_JPEG, wx.BITMAP_TYPE_PNG):
                img = wx.Image(io.BytesIO(img_data), type=fmt)
                if img.IsOk():
                    w, h  = img.GetWidth(), img.GetHeight()
                    scale = min(size / w, size / h)
                    return wx.Bitmap(
                        img.Scale(int(w * scale), int(h * scale), wx.IMAGE_QUALITY_HIGH)
                    )
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _on_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if self._gallery_panel.IsShown():
            if key == wx.WXK_ESCAPE:
                self._exit_gallery(); return
            if key == wx.WXK_LEFT:
                self._on_gallery_prev(None); return
            if key == wx.WXK_RIGHT:
                self._on_gallery_next(None); return
        elif key == wx.WXK_ESCAPE:
            self.Close(); return
        event.Skip()

    # ------------------------------------------------------------------
    # External link buttons
    # ------------------------------------------------------------------

    def _on_datasheet(self, _event) -> None:
        if self._datasheet_url:
            webbrowser.open(self._datasheet_url)

    def _on_lcsc_page(self, _event) -> None:
        if self._lcsc_page_url:
            webbrowser.open(self._lcsc_page_url)

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def _on_import(self, _event) -> None:
        if not self._selected_result:
            self._log("Error: select a search result first.")
            return

        use_global = self.dest_global.GetValue()
        lib_dir = self._global_lib_dir if use_global else self._get_project_dir()
        if not lib_dir:
            self._log("Error: No board file open. Use Global destination or open a board.")
            return

        self.status_text.Clear()
        self._main_panel.Disable()
        self._busy_overlay.show()

        threading.Thread(
            target=self._import_worker,
            args=(
                self._selected_result["lcsc"],
                lib_dir,
                self._lib_name,
                use_global,
                self._selected_result,
                self._get_kicad_version(),
            ),
            daemon=True,
        ).start()

    def _import_worker(
        self,
        lcsc_id: str,
        lib_dir: str,
        lib_name: str,
        use_global: bool,
        search_result: dict,
        kicad_version: int,
    ) -> None:
        """Background thread: run import, with SSL retry on first failure."""
        try:
            try:
                result = self._do_import(lcsc_id, lib_dir, lib_name,
                                         use_global, search_result, kicad_version)
            except SSLCertError:
                self._handle_ssl_cert_error()
                result = self._do_import(lcsc_id, lib_dir, lib_name,
                                         use_global, search_result, kicad_version)
            if not self._closing:
                wx.CallAfter(self._on_import_complete, result)
        except APIError as e:
            if not self._closing:
                wx.CallAfter(self._on_import_error, f"API Error: {e}")
        except Exception as e:
            if not self._closing:
                wx.CallAfter(self._on_import_error,
                             f"Error: {e}\n{traceback.format_exc()}")

    def _on_import_complete(self, result: dict | None) -> None:
        if self._closing:
            return
        self._busy_overlay.dismiss()
        self._main_panel.Enable()
        if result is None:
            self._log("Import cancelled.")
        else:
            self._log(f"\nDone! '{result['title']}' imported as {self._lib_name}:{result['name']}")
            self._refresh_imported_ids()
            self._repopulate_results()
            self._persist_destination()

    def _on_import_error(self, msg: str) -> None:
        if self._closing:
            return
        self._busy_overlay.dismiss()
        self._main_panel.Enable()
        self._log(msg)

    def _confirm_metadata(self, metadata: dict) -> dict | None:
        """Show MetadataEditDialog on the main thread; return edited dict or None."""
        dlg = MetadataEditDialog(self, metadata)
        try:
            return dlg.get_metadata() if dlg.ShowModal() == wx.ID_OK else None
        finally:
            dlg.Destroy()

    def _confirm_overwrite(self, name: str, existing: list[str]) -> bool:
        items = ", ".join(existing)
        dlg   = wx.MessageDialog(
            self, f"'{name}' already exists ({items}). Overwrite?",
            "Confirm Overwrite", wx.YES_NO | wx.ICON_QUESTION,
        )
        result = dlg.ShowModal() == wx.ID_YES
        dlg.Destroy()
        return result

    def _do_import(
        self,
        lcsc_id: str,
        lib_dir: str,
        lib_name: str,
        use_global: bool,
        search_result: dict,
        kicad_version: int,
    ) -> dict | None:
        """Run ``import_component`` with thread-safe UI callbacks.

        ``confirm_metadata`` and ``confirm_overwrite`` are called on the
        background thread but must show wx dialogs.  They marshal the call to
        the main thread via ``wx.CallAfter`` and block on a ``threading.Event``
        until the user responds.
        """
        def log(msg: str) -> None:
            if not self._closing:
                wx.CallAfter(self._log, msg)

        def _blocking_dialog(show_fn) -> any:
            """Run *show_fn* on the main thread and return its result."""
            result = [None]
            done   = threading.Event()
            def _run():
                if self._closing:
                    done.set()
                    return
                # Temporarily re-enable UI so dialogs are interactive
                self._main_panel.Enable()
                self._busy_overlay.dismiss()
                result[0] = show_fn()
                self._main_panel.Disable()
                self._busy_overlay.show()
                done.set()
            wx.CallAfter(_run)
            done.wait()
            return result[0]

        def confirm_metadata(metadata: dict) -> dict | None:
            return _blocking_dialog(lambda: self._confirm_metadata(metadata))

        def confirm_overwrite(name: str, existing: list[str]) -> bool:
            return _blocking_dialog(lambda: self._confirm_overwrite(name, existing))

        return import_component(
            lcsc_id, lib_dir, lib_name,
            overwrite=False,
            use_global=use_global,
            log=log,
            kicad_version=kicad_version,
            search_result=search_result,
            confirm_metadata=confirm_metadata,
            confirm_overwrite=confirm_overwrite,
        )
