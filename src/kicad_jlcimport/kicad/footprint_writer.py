"""Generate KiCad .kicad_mod footprint files (v8 and v9)."""

import math
from typing import List, Optional, Tuple

from ..easyeda.ee_types import EEFootprint
from ..easyeda.parser import compute_arc_midpoint
from ._format import escape_sexpr as _escape
from ._format import fmt_float as _fmt
from ._format import gen_uuid as _uuid
from .version import DEFAULT_KICAD_VERSION, footprint_format_version, has_embedded_fonts, has_generator_version

# Courtyard generation constants (IPC-7351 / KiCad KLC F5.3)
_COURTYARD_CLEARANCE = 0.25  # mm clearance for standard parts
_COURTYARD_CLEARANCE_SMALL = 0.15  # mm clearance for parts < 1.5mm in any dimension
_COURTYARD_SMALL_THRESHOLD = 1.5  # mm — parts smaller than this use reduced clearance
_COURTYARD_LINE_WIDTH = 0.05  # mm stroke width
_COURTYARD_GRID = 0.05  # mm grid for rounding coordinates


def write_footprint(
    footprint: EEFootprint,
    name: str,
    lcsc_id: str = "",
    description: str = "",
    keywords: str = "",
    datasheet: str = "",
    model_path: str = "",
    model_offset: Tuple[float, float, float] = (0, 0, 0),
    model_rotation: Tuple[float, float, float] = (0, 0, 0),
    kicad_version: int = DEFAULT_KICAD_VERSION,
) -> str:
    """Generate complete .kicad_mod content for a footprint."""
    lines = []

    # Determine if SMD or through-hole
    has_tht = any(pad.layer == "11" for pad in footprint.pads)
    attr = "through_hole" if has_tht else "smd"

    # Calculate bounding box for reference/value placement
    all_y = []
    for pad in footprint.pads:
        all_y.extend([pad.y - pad.height / 2, pad.y + pad.height / 2])
    for track in footprint.tracks:
        all_y.extend([p[1] for p in track.points])
    min_y = min(all_y) if all_y else -2
    max_y = max(all_y) if all_y else 2

    ref_y = min_y - 1.0
    val_y = max_y + 1.0

    lines.append(f'(footprint "{name}"')
    lines.append(f"  (version {footprint_format_version(kicad_version)})")
    lines.append('  (generator "JLCImport")')
    if has_generator_version(kicad_version):
        lines.append('  (generator_version "1.0")')
    lines.append('  (layer "F.Cu")')
    if description:
        lines.append(f'  (descr "{_escape(description)}")')
    if keywords:
        lines.append(f'  (tags "{_escape(keywords)}")')

    # Properties
    lines.append(f'  (property "Reference" "REF**" (at 0 {_fmt(ref_y)} 0) (layer "F.SilkS") (uuid "{_uuid()}")')
    lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
    lines.append("  )")
    lines.append(f'  (property "Value" "~" (at 0 {_fmt(val_y)} 0) (layer "F.Fab") (uuid "{_uuid()}")')
    lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
    lines.append("  )")
    if datasheet:
        lines.append(f'  (property "Datasheet" "{datasheet}" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{_uuid()}")')
        lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
        lines.append("  )")
    if description:
        lines.append(
            f'  (property "Description" "{_escape(description)}" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{_uuid()}")'
        )
        lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
        lines.append("  )")
    if lcsc_id:
        lines.append(f'  (property "LCSC" "{lcsc_id}" (at 0 0 0) (layer "F.Fab") (hide yes) (uuid "{_uuid()}")')
        lines.append("    (effects (font (size 1 1) (thickness 0.15)))")
        lines.append("  )")

    lines.append(f"  (attr {attr})")

    # Tracks (fp_line segments)
    for track in footprint.tracks:
        for i in range(len(track.points) - 1):
            x1, y1 = track.points[i]
            x2, y2 = track.points[i + 1]
            lines.append(
                f"  (fp_line (start {_fmt(x1)} {_fmt(y1)}) (end {_fmt(x2)} {_fmt(y2)})"
                f" (stroke (width {_fmt(track.width)}) (type solid))"
                f' (layer "{track.layer}") (uuid "{_uuid()}"))'
            )

    # Circles
    for circle in footprint.circles:
        end_x = circle.cx + circle.radius
        fill_str = " (fill solid)" if circle.filled else ""
        lines.append(
            f"  (fp_circle (center {_fmt(circle.cx)} {_fmt(circle.cy)})"
            f" (end {_fmt(end_x)} {_fmt(circle.cy)})"
            f" (stroke (width {_fmt(circle.width)}) (type solid))"
            f"{fill_str}"
            f' (layer "{circle.layer}") (uuid "{_uuid()}"))'
        )

    # Arcs
    for arc in footprint.arcs:
        mid = compute_arc_midpoint(arc.start, arc.end, arc.rx, arc.ry, arc.large_arc, arc.sweep)
        # If sweep == 0, swap start and end
        if arc.sweep == 0:
            s, e = arc.end, arc.start
        else:
            s, e = arc.start, arc.end
        lines.append(
            f"  (fp_arc (start {_fmt(s[0])} {_fmt(s[1])})"
            f" (mid {_fmt(mid[0])} {_fmt(mid[1])})"
            f" (end {_fmt(e[0])} {_fmt(e[1])})"
            f" (stroke (width {_fmt(arc.width)}) (type solid))"
            f' (layer "{arc.layer}") (uuid "{_uuid()}"))'
        )

    # Solid regions (e.g., pin 1 indicators)
    for region in footprint.regions:
        pts_str = " ".join(f"(xy {_fmt(x)} {_fmt(y)})" for x, y in region.points)
        lines.append(
            f"  (fp_poly (pts {pts_str})"
            f" (stroke (width 0) (type solid))"
            f" (fill solid)"
            f' (layer "{region.layer}") (uuid "{_uuid()}"))'
        )

    # Courtyard — convex hull around pads and holes
    crtyd = _compute_courtyard(footprint)
    if crtyd is not None:
        w = _COURTYARD_LINE_WIDTH
        pts_str = " ".join(f"(xy {_fmt(x)} {_fmt(y)})" for x, y in crtyd)
        lines.append(
            f"  (fp_poly (pts {pts_str})"
            f" (stroke (width {_fmt(w)}) (type solid))"
            f" (fill none)"
            f' (layer "F.CrtYd") (uuid "{_uuid()}"))'
        )

    # Pads
    for pad in footprint.pads:
        pad_type, pad_shape, layers = _pad_type_info(pad)
        at_str = f"(at {_fmt(pad.x)} {_fmt(pad.y)}"
        if pad.rotation != 0:
            at_str += f" {_fmt(pad.rotation)}"
        at_str += ")"

        size_str = f"(size {_fmt(pad.width)} {_fmt(pad.height)})"
        layers_str = " ".join(f'"{layer}"' for layer in layers)

        if pad_shape == "custom" and pad.polygon_points:
            # Custom pad with polygon primitives.  The polygon vertices
            # already define the final shape, so omit pad rotation to
            # avoid double-rotating.
            custom_at = f"(at {_fmt(pad.x)} {_fmt(pad.y)})"
            pts = pad.polygon_points
            pts_str = " ".join(f"(xy {_fmt(pts[i])} {_fmt(pts[i + 1])})" for i in range(0, len(pts) - 1, 2))
            lines.append(f'  (pad "{pad.number}" {pad_type} {pad_shape} {custom_at} {size_str}')
            if pad.drill > 0:
                lines.append(f"    {_drill_str(pad)}")
            lines.append(f"    (layers {layers_str})")
            lines.append("    (primitives")
            lines.append(f"      (gr_poly (pts {pts_str}) (width 0) (fill yes))")
            lines.append(f'    ) (uuid "{_uuid()}"))')
        else:
            pad_line = f'  (pad "{pad.number}" {pad_type} {pad_shape} {at_str} {size_str}'
            if pad.drill > 0:
                pad_line += f" {_drill_str(pad)}"
            pad_line += f' (layers {layers_str}) (uuid "{_uuid()}"))'
            lines.append(pad_line)

    # Holes (NPTH)
    for hole in footprint.holes:
        diameter = hole.radius * 2
        lines.append(
            f'  (pad "" np_thru_hole circle (at {_fmt(hole.x)} {_fmt(hole.y)})'
            f" (size {_fmt(diameter)} {_fmt(diameter)})"
            f" (drill {_fmt(diameter)})"
            f' (layers "*.Cu" "*.Mask") (uuid "{_uuid()}"))'
        )

    # 3D model
    if model_path:
        ox, oy, oz = model_offset
        rx, ry, rz = model_rotation
        lines.append(f'  (model "{model_path}"')
        lines.append(f"    (offset (xyz {_fmt(ox)} {_fmt(oy)} {_fmt(oz)}))")
        lines.append("    (scale (xyz 1 1 1))")
        lines.append(f"    (rotate (xyz {_fmt(rx)} {_fmt(ry)} {_fmt(rz)}))")
        lines.append("  )")

    if has_embedded_fonts(kicad_version):
        lines.append("  (embedded_fonts no)")
    lines.append(")")

    return "\n".join(lines) + "\n"


def _drill_str(pad) -> str:
    """Return the KiCad drill specification string for a pad.

    For oval slot drills (slot_length > 0), returns ``(drill oval W H)``
    with the slot oriented to match the pad aspect ratio.
    For circular drills, returns ``(drill D)``.
    """
    if pad.slot_length > 0:
        if pad.height >= pad.width:
            # Vertical slot: narrow dimension = drill, long dimension = slot_length
            return f"(drill oval {_fmt(pad.drill)} {_fmt(pad.slot_length)})"
        else:
            # Horizontal slot: long dimension = slot_length, narrow dimension = drill
            return f"(drill oval {_fmt(pad.slot_length)} {_fmt(pad.drill)})"
    return f"(drill {_fmt(pad.drill)})"


def _pad_type_info(pad):
    """Determine pad type, shape, and layers."""
    # Shape mapping
    shape_map = {
        "RECT": "rect",
        "OVAL": "oval",
        "ELLIPSE": "oval",
        "POLYGON": "custom",
    }
    pad_shape = shape_map.get(pad.shape, "rect")

    # Only use custom shape if polygon data is available; fall back to rect
    if pad_shape == "custom" and not pad.polygon_points:
        pad_shape = "rect"

    if pad.layer == "11":
        # Through-hole
        pad_type = "thru_hole"
        layers = ["*.Cu", "*.Mask"]
        # First pad is often rect for THT
        if pad.number == "1" and pad_shape == "rect":
            pad_shape = "rect"
    elif pad.layer == "2":
        pad_type = "smd"
        layers = ["B.Cu", "B.Mask", "B.Paste"]
    else:
        pad_type = "smd"
        layers = ["F.Cu", "F.Mask", "F.Paste"]

    return pad_type, pad_shape, layers


def _compute_courtyard(footprint: EEFootprint) -> Optional[List[Tuple[float, float]]]:
    """Compute a courtyard convex-hull polygon from pads and holes.

    Only copper features (pads) and physical board penetrations (holes)
    contribute to the courtyard.  Silkscreen, fab-layer markings, and
    solid regions are informational and do not affect keep-out spacing.

    Returns polygon vertices (counter-clockwise) offset by the appropriate
    clearance and snapped to the courtyard grid, or *None* when the footprint
    has no pads or holes.
    """
    points: List[Tuple[float, float]] = []

    for pad in footprint.pads:
        points.extend(_pad_corners(pad))

    for hole in footprint.holes:
        # Approximate circle with 8 perimeter points (every 45°)
        r = hole.radius
        for k in range(8):
            a = k * math.pi / 4
            points.append((hole.x + r * math.cos(a), hole.y + r * math.sin(a)))

    if not points:
        return None

    hull = _convex_hull(points)
    if len(hull) < 3:
        return None

    # Determine clearance based on bounding box of the hull
    xs = [p[0] for p in hull]
    ys = [p[1] for p in hull]
    raw_w = max(xs) - min(xs)
    raw_h = max(ys) - min(ys)

    if raw_w < _COURTYARD_SMALL_THRESHOLD or raw_h < _COURTYARD_SMALL_THRESHOLD:
        clearance = _COURTYARD_CLEARANCE_SMALL
    else:
        clearance = _COURTYARD_CLEARANCE

    offset = _offset_hull(hull, clearance)

    # Snap each vertex outward (away from centroid) to the courtyard grid
    cx = sum(x for x, y in offset) / len(offset)
    cy = sum(y for x, y in offset) / len(offset)
    g = _COURTYARD_GRID
    snapped = []
    for x, y in offset:
        sx = math.floor(x / g) * g if x < cx else math.ceil(x / g) * g
        sy = math.floor(y / g) * g if y < cy else math.ceil(y / g) * g
        snapped.append((round(sx, 6), round(sy, 6)))
    return snapped


def _pad_corners(pad) -> List[Tuple[float, float]]:
    """Return the 4 corners of a pad's bounding rectangle, with rotation."""
    hw, hh = pad.width / 2, pad.height / 2
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    if pad.rotation == 0:
        return [(pad.x + dx, pad.y + dy) for dx, dy in corners]
    rad = math.radians(pad.rotation)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    return [(pad.x + dx * cos_a - dy * sin_a, pad.y + dx * sin_a + dy * cos_a) for dx, dy in corners]


def _convex_hull(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Compute convex hull using Andrew's monotone chain (CCW order)."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    lower: List[Tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper: List[Tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def _cross(o: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """2D cross product of vectors OA and OB."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _offset_hull(hull: List[Tuple[float, float]], offset: float) -> List[Tuple[float, float]]:
    """Offset a convex polygon outward by *offset* mm.

    Each edge is shifted outward along its normal, and adjacent shifted
    edges are intersected to produce the new vertices.  The hull must be
    in counter-clockwise order with at least 3 vertices.
    """
    n = len(hull)

    # Unit outward normals for each edge (CCW winding → outward is right-hand)
    normals: List[Tuple[float, float]] = []
    for i in range(n):
        dx = hull[(i + 1) % n][0] - hull[i][0]
        dy = hull[(i + 1) % n][1] - hull[i][1]
        length = math.hypot(dx, dy)
        if length < 1e-10:
            normals.append((0.0, -1.0))
        else:
            normals.append((dy / length, -dx / length))

    result: List[Tuple[float, float]] = []
    for i in range(n):
        n1 = normals[(i - 1) % n]  # normal of edge ending at vertex i
        n2 = normals[i]  # normal of edge starting at vertex i

        # Bisector of the two outward normals
        bx = n1[0] + n2[0]
        by = n1[1] + n2[1]
        dot = n1[0] * bx + n1[1] * by  # = 1 + cos(angle between normals)

        if abs(dot) < 1e-10:
            # Nearly anti-parallel normals (shouldn't happen for valid convex hull)
            result.append((hull[i][0] + offset * n2[0], hull[i][1] + offset * n2[1]))
        else:
            scale = offset / dot
            result.append((hull[i][0] + scale * bx, hull[i][1] + scale * by))
    return result


def _arc_bounds(arc) -> Tuple[float, float, float, float]:
    """Return ``(min_x, min_y, max_x, max_y)`` for an arc including stroke width."""
    sx, sy = arc.start
    ex, ey = arc.end
    hw = arc.width / 2
    r = (arc.rx + arc.ry) / 2

    # Compute arc centre (same derivation as compute_arc_midpoint)
    mx, my = (sx + ex) / 2, (sy + ey) / 2
    dx, dy = ex - sx, ey - sy
    chord = math.hypot(dx, dy)
    if chord < 1e-10:
        return (sx - hw, sy - hw, sx + hw, sy + hw)
    if r < chord / 2:
        r = chord / 2
    h = math.sqrt(max(0, r * r - (chord / 2) ** 2))
    px, py = -dy / chord, dx / chord
    if arc.large_arc != arc.sweep:
        cx, cy = mx + h * px, my + h * py
    else:
        cx, cy = mx - h * px, my - h * py

    # Angles swept by the arc
    a_start = math.atan2(sy - cy, sx - cx)
    a_end = math.atan2(ey - cy, ex - cx)
    if arc.sweep == 1:
        if a_end <= a_start:
            a_end += 2 * math.pi
    else:
        if a_end >= a_start:
            a_end -= 2 * math.pi

    # Start/end always contribute
    pts_x = [sx, ex]
    pts_y = [sy, ey]

    # Check if the arc passes through any cardinal direction; if so the
    # circle extremum at that angle must be included in the bounding box.
    lo, hi = (a_start, a_end) if a_start <= a_end else (a_end, a_start)
    for base in (0.0, math.pi / 2, math.pi, -math.pi / 2):
        for k in range(-2, 3):
            a = base + 2 * math.pi * k
            if lo <= a <= hi:
                pts_x.append(cx + r * math.cos(a))
                pts_y.append(cy + r * math.sin(a))

    return (min(pts_x) - hw, min(pts_y) - hw, max(pts_x) + hw, max(pts_y) + hw)
