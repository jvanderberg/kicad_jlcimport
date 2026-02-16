"""Generate KiCad .kicad_sym symbol blocks (v8 and v9)."""

import math
from typing import List

from ..easyeda.ee_types import EESymbol
from ..easyeda.parser import compute_arc_midpoint
from ._format import escape_sexpr as _escape
from ._format import fmt_float as _fmt
from .version import DEFAULT_KICAD_VERSION, has_generator_version, symbol_format_version


def _rounded_rect_points(x1: float, y1: float, x2: float, y2: float, r: float) -> List[tuple]:
    """Generate points for a rounded rectangle as a closed polyline.

    Uses 8 segments per corner for smooth arcs.
    """
    # Ensure r doesn't exceed half the smallest dimension
    r = min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2)

    left = min(x1, x2)
    right = max(x1, x2)
    top = min(y1, y2)
    bottom = max(y1, y2)

    segments = 8
    points = []

    # Top-right corner
    for i in range(segments + 1):
        angle = -math.pi / 2 + (math.pi / 2) * i / segments
        points.append((right - r + r * math.cos(angle), top + r + r * math.sin(angle)))

    # Bottom-right corner
    for i in range(1, segments + 1):
        angle = 0 + (math.pi / 2) * i / segments
        points.append((right - r + r * math.cos(angle), bottom - r + r * math.sin(angle)))

    # Bottom-left corner
    for i in range(1, segments + 1):
        angle = math.pi / 2 + (math.pi / 2) * i / segments
        points.append((left + r + r * math.cos(angle), bottom - r + r * math.sin(angle)))

    # Top-left corner
    for i in range(1, segments + 1):
        angle = math.pi + (math.pi / 2) * i / segments
        points.append((left + r + r * math.cos(angle), top + r + r * math.sin(angle)))

    # Close the path back to the first point
    points.append(points[0])

    return points


def write_symbol(
    symbol: EESymbol,
    name: str,
    prefix: str = "U",
    footprint_ref: str = "",
    lcsc_id: str = "",
    datasheet: str = "",
    description: str = "",
    keywords: str = "",
    manufacturer: str = "",
    manufacturer_part: str = "",
    unit_index: int = 0,
    total_units: int = 1,
    include_pin_dots: bool = False,
    hide_properties: bool = False,
) -> str:
    """Generate a complete (symbol ...) block.

    For multi-unit components, call once per unit with appropriate unit_index.
    If unit_index == 0 and total_units == 1, generates a single-unit symbol.
    If include_pin_dots is True, adds small circles at pin connection points
    (useful for visual comparison since kicad-cli SVG export omits these).
    If hide_properties is True, Reference and Value are hidden.
    """
    lines = []

    if unit_index == 0:
        # Outer symbol wrapper
        lines.append(f'  (symbol "{name}"')
        lines.append("    (pin_names (offset 1.016))")
        lines.append("    (in_bom yes)")
        lines.append("    (on_board yes)")

        # Properties
        ref_x = 0
        ref_y = _estimate_top(symbol) + 2.0
        hide_suffix = " hide" if hide_properties else ""
        lines.append(f'    (property "Reference" "{prefix}" (at {_fmt(ref_x)} {_fmt(ref_y)} 0)')
        lines.append(f"      (effects (font (size 1.27 1.27)){hide_suffix})")
        lines.append("    )")
        val_y = _estimate_bottom(symbol) - 2.0
        lines.append(f'    (property "Value" "{name}" (at {_fmt(ref_x)} {_fmt(val_y)} 0)')
        lines.append(f"      (effects (font (size 1.27 1.27)){hide_suffix})")
        lines.append("    )")
        if footprint_ref:
            lines.append(f'    (property "Footprint" "{footprint_ref}" (at 0 0 0)')
            lines.append("      (effects (font (size 1.27 1.27)) hide)")
            lines.append("    )")
        if datasheet:
            lines.append(f'    (property "Datasheet" "{datasheet}" (at 0 0 0)')
            lines.append("      (effects (font (size 1.27 1.27)) hide)")
            lines.append("    )")
        if description:
            lines.append(f'    (property "Description" "{_escape(description)}" (at 0 0 0)')
            lines.append("      (effects (font (size 1.27 1.27)) hide)")
            lines.append("    )")
        if keywords:
            lines.append(f'    (property "ki_keywords" "{_escape(keywords)}" (at 0 0 0)')
            lines.append("      (effects (font (size 1.27 1.27)) hide)")
            lines.append("    )")
        if lcsc_id:
            lines.append(f'    (property "LCSC" "{lcsc_id}" (at 0 0 0)')
            lines.append("      (effects (font (size 1.27 1.27)) hide)")
            lines.append("    )")
        if manufacturer:
            lines.append(f'    (property "Manufacturer" "{_escape(manufacturer)}" (at 0 0 0)')
            lines.append("      (effects (font (size 1.27 1.27)) hide)")
            lines.append("    )")
        if manufacturer_part:
            lines.append(f'    (property "Manufacturer Part" "{_escape(manufacturer_part)}" (at 0 0 0)')
            lines.append("      (effects (font (size 1.27 1.27)) hide)")
            lines.append("    )")

    # Unit sub-symbol for graphics
    unit_num = unit_index if total_units > 1 else 0
    lines.append(f'    (symbol "{name}_{unit_num}_1"')

    # Rectangles
    for rect in symbol.rectangles:
        x2 = rect.x + rect.width
        y2 = rect.y + rect.height
        if rect.corner_radius > 0:
            # Rounded rectangle: draw as closed polyline
            pts = _rounded_rect_points(rect.x, rect.y, x2, y2, rect.corner_radius)
            lines.append("      (polyline")
            lines.append("        (pts")
            for px, py in pts:
                lines.append(f"          (xy {_fmt(px)} {_fmt(py)})")
            lines.append("        )")
            lines.append("        (stroke (width 0.254) (type solid))")
            lines.append("        (fill (type background))")
            lines.append("      )")
        else:
            lines.append(f"      (rectangle (start {_fmt(rect.x)} {_fmt(rect.y)}) (end {_fmt(x2)} {_fmt(y2)})")
            lines.append("        (stroke (width 0.254) (type solid))")
            lines.append("        (fill (type background))")
            lines.append("      )")

    # Circles
    for circle in symbol.circles:
        lines.append(f"      (circle (center {_fmt(circle.cx)} {_fmt(circle.cy)}) (radius {_fmt(circle.radius)})")
        lines.append("        (stroke (width 0.254) (type solid))")
        # Use "outline" for filled circles (solid dark dot) vs "none" for hollow
        fill_type = "outline" if circle.filled else "none"
        lines.append(f"        (fill (type {fill_type}))")
        lines.append("      )")

    # Polylines
    for poly in symbol.polylines:
        points = list(poly.points)
        # Close the path by adding first point to end if marked closed
        if poly.closed and len(points) >= 2 and points[0] != points[-1]:
            points.append(points[0])
        pts_str = " ".join(f"(xy {_fmt(x)} {_fmt(y)})" for x, y in points)
        lines.append("      (polyline")
        lines.append(f"        (pts {pts_str})")
        lines.append("        (stroke (width 0.254) (type solid))")
        fill_type = "outline" if poly.fill else "none"
        lines.append(f"        (fill (type {fill_type}))")
        lines.append("      )")

    # Arcs
    for arc in symbol.arcs:
        mid = compute_arc_midpoint(arc.start, arc.end, arc.rx, arc.ry, arc.large_arc, arc.sweep)
        if arc.sweep == 0:
            s, e = arc.end, arc.start
        else:
            s, e = arc.start, arc.end
        lines.append(
            f"      (arc (start {_fmt(s[0])} {_fmt(s[1])})"
            f" (mid {_fmt(mid[0])} {_fmt(mid[1])})"
            f" (end {_fmt(e[0])} {_fmt(e[1])})"
        )
        lines.append("        (stroke (width 0.254) (type solid))")
        lines.append("        (fill (type none))")
        lines.append("      )")

    # Texts
    for text in symbol.texts:
        lines.append(f'      (text "{_escape(text.text)}" (at {_fmt(text.x)} {_fmt(text.y)} {_fmt(text.rotation)})')
        lines.append(f"        (effects (font (size {_fmt(text.font_size)} {_fmt(text.font_size)})))")
        lines.append("      )")

    # Pins
    for pin in symbol.pins:
        elec_type = pin.electrical_type
        # Direction is encoded in the angle field of (at x y angle)
        pin_line = f"      (pin {elec_type} line (at {_fmt(pin.x)} {_fmt(pin.y)} {_fmt(pin.rotation)})"
        pin_line += f" (length {_fmt(pin.length)})"
        lines.append(pin_line)

        name_effects = "(effects (font (size 1.27 1.27)))"
        if not pin.name_visible:
            name_effects = "(effects (font (size 1.27 1.27)) hide)"
        lines.append(f'        (name "{_escape(pin.name)}" {name_effects})')

        num_effects = "(effects (font (size 1.27 1.27)))"
        if not pin.number_visible:
            num_effects = "(effects (font (size 1.27 1.27)) hide)"
        lines.append(f'        (number "{pin.number}" {num_effects})')
        lines.append("      )")

    # Pin connection point dots (optional, for visual comparison)
    if include_pin_dots:
        for pin in symbol.pins:
            lines.append(f"      (circle (center {_fmt(pin.x)} {_fmt(pin.y)}) (radius 0.254)")
            lines.append("        (stroke (width 0.127) (type solid))")
            lines.append("        (fill (type none))")
            lines.append("      )")

    lines.append("    )")  # Close unit sub-symbol

    if unit_index == 0 or (unit_index == total_units - 1 and total_units > 1):
        lines.append("  )")  # Close outer symbol

    return "\n".join(lines) + "\n"


def write_symbol_library(
    symbols_content: List[str],
    kicad_version: int = DEFAULT_KICAD_VERSION,
) -> str:
    """Wrap symbol blocks in a complete library file."""
    lines = [
        "(kicad_symbol_lib",
        f"  (version {symbol_format_version(kicad_version)})",
        '  (generator "JLCImport")',
    ]
    if has_generator_version(kicad_version):
        lines.append('  (generator_version "1.0")')
    for sym in symbols_content:
        lines.append(sym)
    lines.append(")")
    return "\n".join(lines) + "\n"


def _estimate_top(symbol: EESymbol) -> float:
    """Estimate the top Y coordinate of the symbol."""
    ys = []
    for rect in symbol.rectangles:
        ys.extend([rect.y, rect.y + rect.height])
    for pin in symbol.pins:
        ys.append(pin.y)
    return max(ys) if ys else 5.0


def _estimate_bottom(symbol: EESymbol) -> float:
    """Estimate the bottom Y coordinate of the symbol."""
    ys = []
    for rect in symbol.rectangles:
        ys.extend([rect.y, rect.y + rect.height])
    for pin in symbol.pins:
        ys.append(pin.y)
    return min(ys) if ys else -5.0
